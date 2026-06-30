"""Structured parsing of test-command output (cargo / pytest).

The orchestrator used to treat a test run as a bare ``returncode == 0`` boolean,
which conflates several genuinely different outcomes: a real test failure, a
compile error, a transient build-lock contention, a timeout, and a clean pass.
This module classifies the combined stdout+stderr of a test command into a
:class:`TestVerdict` so the orchestrator can act on *what happened* rather than
the exit code alone — notably retrying on transient lock contention instead of
aborting a correct rebase.

Pure functions over text; no I/O, no subprocess. Each parser recognizes its
tool's distinctive signals (cargo's ``Blocking waiting for file lock``,
``error[EXXXX]``, ``test result: FAILED.``; pytest's ``FAILED``/``ERROR``/
``error:``). Unknown tools fall back to a returncode-based verdict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

TestVerdictKind = Literal[
    "passed",        # tests ran and passed
    "failed",        # tests ran, at least one assertion failed
    "compile_error", # the build failed before tests could run
    "lock_contention", # transient: another process holds the build lock
    "timed_out",     # the run hit the wall-clock timeout
    "no_tests",      # the command ran but matched/ran no tests (inconclusive)
    "unknown",       # couldn't classify — caller falls back to returncode
]


@dataclass
class TestVerdict:
    """A classified test-run outcome parsed from stdout/stderr.

    ``kind`` drives the orchestrator's decision (retry on lock contention;
    report compile vs. test failures distinctly; pass on passed/no_tests).
    ``diagnostics`` carries the extracted signal lines (error codes, failed test
    names, the lock message) for the journal/bundle so the human sees *why*.
    """

    kind: TestVerdictKind
    tool: str = ""  # "cargo" | "pytest" | "" (unrecognized)
    diagnostics: list[str] = field(default_factory=list)
    # A one-line human summary suitable for the orchestrator's status output.
    summary: str = ""

    @property
    def is_transient(self) -> bool:
        """Whether the failure is transient and a retry is worthwhile.

        Lock contention is the canonical transient case: the merge is fine,
        another process just holds the build directory. Retrying (after a short
        backoff) is correct; aborting would reject a correct rebase.
        """
        return self.kind == "lock_contention"


# ---------------------------------------------------------------------------
# Cargo (cargo test / cargo check)
# ---------------------------------------------------------------------------

# ``Blocking waiting for file lock on build directory`` — cargo emits this to
# stderr while another cargo invocation holds the target/ lock. Transient.
_CARGO_LOCK_RE = re.compile(r"Blocking waiting for file lock", re.IGNORECASE)
# A compile error: ``error[E0433]: ...`` or a bare ``error: ...`` from rustc.
# Cargo's structured errors carry a bracketed code; bare ``error:`` is rarer but
# also indicates a build failure.
_CARGO_COMPILE_ERROR_RE = re.compile(r"^\s*error(?:\[[A-Z]\d+\])?:\s", re.MULTILINE)
# ``could not compile `crate` ...`` — cargo's build-abort line.
_CARGO_COULD_NOT_COMPILE_RE = re.compile(r"could not compile", re.IGNORECASE)
# The test-suite result line: ``test result: FAILED.`` / ``test result: ok.``.
# Multiple targets (lib/bin) each emit one; FAILED wins if any is present.
_CARGO_TEST_RESULT_RE = re.compile(r"test result:\s*(ok|FAILED|ignored)", re.IGNORECASE)


def parse_cargo(stdout: str, stderr: str, *, returncode: int) -> TestVerdict:
    """Classify cargo test/check output into a :class:`TestVerdict`.

    Precedence (first match wins):
      1. ``lock_contention`` — transient, always checked first so a build-lock
         never reads as a real failure.
      2. ``compile_error`` — the build failed (``error[E0XXX]`` / ``could not
         compile``); tests never ran. Distinct from a test failure because the
         cause is the merge not compiling, not a behavioral regression.
      3. ``failed`` — tests compiled and ran but at least one failed
         (``test result: FAILED.`` / a panic).
      4. ``passed`` — ``test result: ok.`` present and no failure.
      5. ``no_tests`` — cargo ran but emitted no ``test result:`` line (e.g. a
         ``cargo check`` that doesn't run tests, or zero tests collected).
      6. ``unknown`` — nothing recognized; the caller falls back to returncode.
    """
    combined = f"{stdout}\n{stderr}"
    diags: list[str] = []

    if _CARGO_LOCK_RE.search(combined):
        diags.append("build directory is locked by another cargo process")
        return TestVerdict(
            kind="lock_contention", tool="cargo", diagnostics=diags,
            summary="transient: cargo build lock held by another process",
        )

    # Compile errors. Collect the distinctive error lines (capped) for the report.
    compile_errs = _CARGO_COMPILE_ERROR_RE.findall(combined)
    if compile_errs or _CARGO_COULD_NOT_COMPILE_RE.search(combined):
        # Extract up to 5 actual error lines for the journal/bundle.
        for line in combined.splitlines():
            if _CARGO_COMPILE_ERROR_RE.match(line):
                diags.append(line.strip())
            if len(diags) >= 5:
                break
        return TestVerdict(
            kind="compile_error", tool="cargo", diagnostics=diags,
            summary=f"cargo build failed: {len(compile_errs) or 1} compile error(s)",
        )

    # Test outcomes. FAILED dominates: any failing target means the suite failed.
    results = _CARGO_TEST_RESULT_RE.findall(combined)
    if any(r.strip().upper() == "FAILED" for r in results):
        # Extract panicked/failed test names if present. Match the per-test
        # ``... FAILED`` line (cargo's inline failure marker) and panics.
        for line in combined.splitlines():
            if "panicked at" in line or "... FAILED" in line:
                diags.append(line.strip())
            if len(diags) >= 5:
                break
        return TestVerdict(
            kind="failed", tool="cargo", diagnostics=diags,
            summary="cargo test: at least one test failed",
        )

    if any(r.strip().upper() == "OK" for r in results):
        return TestVerdict(
            kind="passed", tool="cargo", summary="cargo test: all targets passed",
        )

    # No ``test result:`` line at all: either zero tests ran or this was a
    # check-only invocation. Returncode 0 → no_tests (inconclusive-but-clean);
    # non-zero → unknown (let the caller decide).
    if returncode == 0:
        return TestVerdict(
            kind="no_tests", tool="cargo",
            summary="cargo: ran but emitted no test-result line",
        )
    return TestVerdict(kind="unknown", tool="cargo", diagnostics=diags)


# ---------------------------------------------------------------------------
# pytest
# ---------------------------------------------------------------------------

# pytest's summary line: ``===== 2 failed, 5 passed in 0.3s =====`` etc.
_PYTEST_SUMMARY_RE = re.compile(r"(=+)\s*(.*?)\s*\1\s*$", re.MULTILINE)
_PYTEST_LOCK_RE = re.compile(r"(?:lock|in use|already being used|EBUSY)", re.IGNORECASE)


def parse_pytest(stdout: str, stderr: str, *, returncode: int) -> TestVerdict:
    """Classify pytest output into a :class:`TestVerdict`.

    pytest exit codes: 0 = pass, 1 = some tests failed, 2 = test execution
    interrupted, 5 = no tests collected. The summary line (``N failed, M
    passed``) is the reliable signal; returncode disambiguates the rest.
    """
    combined = f"{stdout}\n{stderr}"
    diags: list[str] = []

    if returncode == 5:
        return TestVerdict(
            kind="no_tests", tool="pytest",
            summary="pytest: no tests collected (rc=5)",
        )

    # Scan for the final summary line (``=== N failed, M passed ===``).
    failed = passed = 0
    for m in _PYTEST_SUMMARY_RE.finditer(combined):
        summary = m.group(2).lower()
        if "failed" in summary or "passed" in summary or "error" in summary:
            fm = re.search(r"(\d+)\s*failed", summary)
            pm = re.search(r"(\d+)\s*passed", summary)
            if fm:
                failed = max(failed, int(fm.group(1)))
            if pm:
                passed = max(passed, int(pm.group(1)))

    if "error" in combined.lower() and returncode != 0 and failed == 0:
        # A collection/import error (not a test assertion failure).
        for line in combined.splitlines():
            if "error" in line.lower() and len(diags) < 5:
                diags.append(line.strip())
        return TestVerdict(
            kind="compile_error", tool="pytest", diagnostics=diags,
            summary="pytest: collection/import error before tests ran",
        )

    if failed > 0 or returncode == 1:
        return TestVerdict(
            kind="failed", tool="pytest",
            summary=f"pytest: {failed} test(s) failed",
        )

    if passed > 0 or returncode == 0:
        return TestVerdict(
            kind="passed", tool="pytest",
            summary=f"pytest: {passed} test(s) passed",
        )

    return TestVerdict(kind="unknown", tool="pytest", diagnostics=diags)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def classify_test_output(
    command: str,
    stdout: str,
    stderr: str,
    *,
    returncode: int,
    timed_out: bool = False,
) -> TestVerdict:
    """Classify a test command's output into a :class:`TestVerdict`.

    Dispatches to the right parser by command (cargo → :func:`parse_cargo`,
    pytest → :func:`parse_pytest`), with a timeout short-circuit (a timed-out
    run is ``timed_out`` regardless of partial output — though a lock contention
    detected in the partial output is still transient and wins). Unknown
    commands fall back to a returncode-based verdict so non-cargo/pytest test
    suites still work.
    """
    # Lock contention wins even on timeout: the build never started compiling
    # because another process held the lock — retrying is correct, and the
    # timeout was just the wall clock while blocked.
    combined = f"{stdout}\n{stderr}"
    if _CARGO_LOCK_RE.search(combined):
        return parse_cargo(stdout, stderr, returncode=returncode)

    if timed_out:
        # Distinguish a timeout mid-compile (slow, retry-friendly) from a
        # timeout mid-test-run (a hanging test). Without structured signals we
        # report ``timed_out``; the orchestrator decides retry vs. escalate.
        still_compiling = bool(re.search(r"^\s*Compiling\s", combined, re.MULTILINE))
        return TestVerdict(
            kind="timed_out", tool=_tool_of(command),
            diagnostics=["timed out" + (" during compilation" if still_compiling else "")],
            summary=(
                "timed out during compilation (slow build)"
                if still_compiling
                else "timed out"
            ),
        )

    tool = _tool_of(command)
    if tool == "cargo":
        return parse_cargo(stdout, stderr, returncode=returncode)
    if tool == "pytest":
        return parse_pytest(stdout, stderr, returncode=returncode)

    # Unknown command: returncode-based fallback.
    if returncode == 0:
        return TestVerdict(kind="passed", tool=tool, summary="test command passed")
    return TestVerdict(
        kind="unknown", tool=tool,
        summary=f"test command failed (rc={returncode})",
    )


def _tool_of(command: str) -> str:
    """The tool name for dispatch: 'cargo' / 'pytest' / '' ."""
    first = (command or "").strip().split()[0] if (command or "").strip() else ""
    if first.endswith("cargo"):
        return "cargo"
    if "pytest" in first or first == "py.test":
        return "pytest"
    return ""

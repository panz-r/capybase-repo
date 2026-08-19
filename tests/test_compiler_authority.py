"""Compiler-authority override at the final gate — sprint-19 P4 (D4).

protobuf-0065: the final pre_continue build completed with rc=2 — the
merge's real compile defect surfaced in make's output — but the verdict
parsed as ``unknown`` with empty diagnostics, and with
``tests.required=False`` the advisory gate let a build-broken merge ship
(sim 0.997). Two hardenings tested here:

1. Make-output parsing: error-carrying lines surface as diagnostics in
   the tests_finished event even when the runner's verdict parser found
   none.
2. Strict positive attribution: when the gate command IS a build and
   error lines positively locate in a file the session wrote, the gate
   escalates regardless of tests.required (compiler authority).
   Unattributable failures (sibling files, driver summaries, timeouts,
   unparseable lines) keep the advisory behavior.
"""

from __future__ import annotations

from types import SimpleNamespace

from capybase.orchestrator import Orchestrator, _phase2_fallback_build_cmd


class _RecJournal:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, payload, **_kw):
        self.events.append((event, payload))


class _StubOrch(Orchestrator):
    """Just enough orchestrator for _run_tests; no git, no engine."""

    def __init__(self, *, tests_required=False, pre_continue="make -j4"):
        self.journal = _RecJournal()
        self.step = 1
        self.config = SimpleNamespace(
            tests=SimpleNamespace(
                required=tests_required, pre_continue=pre_continue))
        self._last_tests_compiler_indictment = False

    def out(self, msg):
        pass

    def _warn(self, msg):
        return msg

    def _resolve_test_command(self, cmd):
        return cmd

    def _cargo_test_cwd(self, result, cmd):
        return None

    def _test_continuity_regressions(self, stdout, cmd):
        return []


def _run(cmd_output: str, *, rc=2, passed=False, kind="unknown",
         diagnostics=None, timed_out=False):
    return SimpleNamespace(
        passed=passed, returncode=rc, timed_out=timed_out,
        verdict=SimpleNamespace(
            kind=kind, summary="unknown", diagnostics=diagnostics or []),
        stdout=cmd_output, stderr="",
    )


def _result(paths=("src/text_format.cc",)):
    return SimpleNamespace(
        units_by_path={p: [] for p in paths}, escalated=False, reason="")


def _attach(orch, run):
    orch._run_test_command = lambda cmd, *, cwd=None: run
    return orch


# ---------------------------------------------------------------------------
# _phase2_fallback_build_cmd — the build-gate recognition
# ---------------------------------------------------------------------------

def test_build_cmd_recognition():
    assert _phase2_fallback_build_cmd("make -j4") == "make -j4"
    assert _phase2_fallback_build_cmd("./configure && make -j4").endswith("make -j4")
    assert _phase2_fallback_build_cmd("cmake --build build") == "cmake --build build"
    assert _phase2_fallback_build_cmd("true") == ""
    assert _phase2_fallback_build_cmd("pytest") == ""


# ---------------------------------------------------------------------------
# Attribution + override in _run_tests
# ---------------------------------------------------------------------------

def test_attributed_in_file_error_fails_gate_under_advisory():
    # 0065's shape: make rc=2, gcc error line in the merged file
    run = _run(
        "make[1]: Entering directory\n"
        "  CC src/text_format.cc\n"
        "src/text_format.cc:2553:41: error: 'kUtf8DebugString' was not "
        "declared in this scope\n"
        "make[1]: *** [Makefile:1234: src/text_format.o] Error 1\n"
    )
    orch = _attach(_StubOrch(tests_required=False), run)
    ok = orch._run_tests("pre_continue", _result())
    assert ok is False
    assert orch._last_tests_compiler_indictment is True
    override = [p for e, p in orch.journal.events
                if e == "compiler_authority_override"]
    assert override and override[0]["tests_required"] is False
    assert override[0]["attributed_merge_errors"]
    # D4.1: the diagnostics surfaced despite the empty parsed verdict
    fin = [p for e, p in orch.journal.events if e == "tests_finished"][0]
    assert fin["diagnostics"]
    assert fin["build_gate"] is True


def test_sibling_error_respects_advisory():
    run = _run(
        "tool/lemon.c:88:12: error: use of undeclared thing\n"
        "make: *** [Makefile:1: all] Error 2\n"
    )
    orch = _attach(_StubOrch(tests_required=False), run)
    ok = orch._run_tests("pre_continue", _result())
    assert ok is False  # run.passed is False — but...
    assert orch._last_tests_compiler_indictment is False  # ...no override


def test_unparseable_lines_never_trigger_override():
    run = _run("something went error wrong\n")
    orch = _attach(_StubOrch(tests_required=False), run)
    orch._run_tests("pre_continue", _result())
    assert orch._last_tests_compiler_indictment is False


def test_timeout_respects_advisory():
    run = _run("", timed_out=True)
    orch = _attach(_StubOrch(tests_required=False), run)
    orch._run_tests("pre_continue", _result())
    assert orch._last_tests_compiler_indictment is False


def test_non_build_command_never_overrides():
    run = _run("src/text_format.cc:1:1: error: boom\n")
    orch = _attach(
        _StubOrch(tests_required=False, pre_continue="pytest"), run)
    orch._run_tests("pre_continue", _result())
    assert orch._last_tests_compiler_indictment is False


def test_passing_build_sets_no_indictment():
    run = _run("", rc=0, passed=True, kind="pass")
    orch = _attach(_StubOrch(), run)
    ok = orch._run_tests("pre_continue", _result())
    assert ok is True
    assert orch._last_tests_compiler_indictment is False


def test_required_gate_still_fails_normally():
    run = _run("some test failure\n", kind="test_failure",
               diagnostics=["test_x failed"])
    orch = _attach(_StubOrch(tests_required=True), run)
    ok = orch._run_tests("pre_continue", _result())
    assert ok is False
    # no attribution, no override event — the required path is unchanged
    assert orch._last_tests_compiler_indictment is False

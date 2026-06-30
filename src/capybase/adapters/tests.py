"""Test-command runner.

Runs the configured pre-continue / final test command (e.g. ``pytest``) in
the repo, with a timeout, and reports whether the worktree changed in
*unrelated* files (a guard against tests that mutate the tree). Returns
structured output the orchestrator journals and feeds to risk policy.

The result carries a parsed :class:`~capybase.test_output.TestVerdict` so the
orchestrator can act on *what happened* (transient lock contention vs. a real
compile error vs. a test failure) rather than the bare return code.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass, field

from capybase.git_backend import GitBackend
from capybase.test_output import TestVerdict, classify_test_output


@dataclass
class TestRunResult:
    passed: bool
    returncode: int
    stdout: str
    stderr: str
    command: str
    timed_out: bool = False
    # The parsed verdict (cargo/pytest output classification). Set by run();
    # an empty TestVerdict (kind "unknown") when parsing didn't run.
    verdict: TestVerdict = field(default_factory=lambda: TestVerdict(kind="unknown"))


class TestRunner:
    def __init__(self, git: GitBackend, *, timeout_seconds: int = 300) -> None:
        self.git = git
        self.timeout = timeout_seconds

    def run(self, command: str, *, cwd: str | None = None) -> TestRunResult:
        argv = shlex.split(command)
        try:
            proc = subprocess.run(
                argv,
                cwd=cwd or str(self.git.repo),
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            passed = proc.returncode == 0
            verdict = classify_test_output(
                command, proc.stdout, proc.stderr, returncode=proc.returncode
            )
            return TestRunResult(
                passed=passed,
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                command=command,
                verdict=verdict,
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return TestRunResult(
                passed=False,
                returncode=-1,
                stdout=out,
                stderr=err,
                command=command,
                timed_out=True,
                verdict=classify_test_output(
                    command, out, err, returncode=-1, timed_out=True
                ),
            )
        except FileNotFoundError as exc:
            # The command itself wasn't found (e.g. pytest missing in a Rust
            # repo). classify as unknown so the orchestrator's verdict-aware
            # path surfaces the real problem rather than a bare "tests failed".
            err = str(exc)
            return TestRunResult(
                passed=False,
                returncode=-1,
                stdout="",
                stderr=err,
                command=command,
                verdict=TestVerdict(
                    kind="unknown", tool="",
                    summary=f"test command not found: {err}",
                ),
            )

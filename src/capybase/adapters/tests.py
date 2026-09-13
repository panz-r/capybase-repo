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

import os
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
        # s27-74 (ninth pass): reap the WHOLE process tree on timeout —
        # subprocess.run(timeout=...) kills only the direct child, and a
        # test suite that spawns servers/daemons (pytest fixtures, cargo
        # test binaries) leaves orphans holding ports/locks; the next
        # attempt then fails "address already in use" permanently. Same
        # session+killpg pattern as verification.py's build-command runner.
        import signal
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd or str(self.git.repo),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
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
        try:
            out, err = proc.communicate(timeout=self.timeout)
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            out, err = proc.communicate()
            return TestRunResult(
                passed=False,
                returncode=-1,
                stdout=out or "",
                stderr=err or "",
                command=command,
                timed_out=True,
                verdict=classify_test_output(
                    command, out or "", err or "", returncode=-1, timed_out=True
                ),
            )
        passed = returncode == 0
        verdict = classify_test_output(
            command, out, err, returncode=returncode
        )
        return TestRunResult(
            passed=passed,
            returncode=returncode,
            stdout=out,
            stderr=err,
            command=command,
            verdict=verdict,
        )

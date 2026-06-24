"""Test-command runner.

Runs the configured pre-continue / final test command (e.g. ``pytest``) in
the repo, with a timeout, and reports whether the worktree changed in
*unrelated* files (a guard against tests that mutate the tree). Returns
structured output the orchestrator journals and feeds to risk policy.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass

from capybase.git_backend import GitBackend


@dataclass
class TestRunResult:
    passed: bool
    returncode: int
    stdout: str
    stderr: str
    command: str
    timed_out: bool = False


class TestRunner:
    def __init__(self, git: GitBackend, *, timeout_seconds: int = 300) -> None:
        self.git = git
        self.timeout = timeout_seconds

    def run(self, command: str) -> TestRunResult:
        argv = shlex.split(command)
        try:
            proc = subprocess.run(
                argv,
                cwd=str(self.git.repo),
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            return TestRunResult(
                passed=proc.returncode == 0,
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                command=command,
            )
        except subprocess.TimeoutExpired as exc:
            return TestRunResult(
                passed=False,
                returncode=-1,
                stdout=exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
                stderr=exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or ""),
                command=command,
                timed_out=True,
            )
        except FileNotFoundError as exc:
            return TestRunResult(
                passed=False,
                returncode=-1,
                stdout="",
                stderr=str(exc),
                command=command,
            )

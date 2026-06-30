"""Tests for the test runner's verdict-aware retry behavior.

The runner parses cargo/pytest output into a verdict and the orchestrator retries
on transient lock contention instead of aborting a correct rebase. These mock
subprocess (no real cargo) to drive the verdict → retry path.
"""

from __future__ import annotations

from unittest.mock import patch

from capybase.adapters.tests import TestRunner
from capybase.config import Config
from capybase.orchestrator import Orchestrator


class _Proc:
    def __init__(self, rc: int, out: str, err: str):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def _orch(repo) -> Orchestrator:
    return Orchestrator(Config(), repo=str(repo), out=lambda *_a, **_k: None)


def test_runner_parses_cargo_pass(repo):
    """A passing cargo run gets verdict kind=passed."""
    runner = TestRunner(_orch(repo).git)
    with patch("capybase.adapters.tests.subprocess.run") as mock:
        mock.return_value = _Proc(0, "test result: ok. 5 passed; 0 failed\n", "")
        r = runner.run("cargo test")
    assert r.passed
    assert r.verdict.kind == "passed"
    assert r.verdict.tool == "cargo"


def test_runner_parses_cargo_lock_contention(repo):
    """``Blocking waiting for file lock`` → verdict kind=lock_contention (transient)."""
    runner = TestRunner(_orch(repo).git)
    with patch("capybase.adapters.tests.subprocess.run") as mock:
        mock.return_value = _Proc(
            -1, "",
            "   Blocking waiting for file lock on build directory\n",
        )
        r = runner.run("cargo test")
    assert r.verdict.kind == "lock_contention"
    assert r.verdict.is_transient


def test_runner_parses_compile_error(repo):
    runner = TestRunner(_orch(repo).git)
    with patch("capybase.adapters.tests.subprocess.run") as mock:
        mock.return_value = _Proc(
            101, "",
            "error[E0433]: could not find `tools`\ncould not compile `x`\n",
        )
        r = runner.run("cargo test")
    assert not r.passed
    assert r.verdict.kind == "compile_error"


def test_orchestrator_retries_on_lock_contention_then_succeeds(repo):
    """Lock contention on the first two attempts, then success → no abort.

    The orchestrator should retry transient lock contention (bounded) rather
    than abort a correct rebase when another cargo process holds the build lock.
    """
    orch = _orch(repo)
    seq = [
        _Proc(-1, "", "   Blocking waiting for file lock on build directory\n"),  # retry
        _Proc(-1, "", "   Blocking waiting for file lock on build directory\n"),  # retry
        _Proc(0, "test result: ok. 5 passed\n", ""),  # success
    ]
    with patch("capybase.adapters.tests.subprocess.run", side_effect=seq), \
         patch("time.sleep"):  # don't actually backoff in the test
        run = orch._run_test_command("cargo test")
    assert run.passed
    assert run.verdict.kind == "passed"


def test_orchestrator_gives_up_after_max_lock_retries(repo):
    """Persistent lock contention exhausts retries → returns the (failed) run."""
    orch = _orch(repo)
    locked = _Proc(-1, "", "   Blocking waiting for file lock on build directory\n")
    with patch("capybase.adapters.tests.subprocess.run", return_value=locked), \
         patch("time.sleep"):
        run = orch._run_test_command("cargo test")
    assert not run.passed
    assert run.verdict.kind == "lock_contention"


def test_orchestrator_does_not_retry_non_transient_failure(repo):
    """A compile error is NOT retried (it's a real failure, not transient)."""
    orch = _orch(repo)
    with patch("capybase.adapters.tests.subprocess.run") as mock:
        mock.return_value = _Proc(
            101, "", "error[E0433]: could not find `tools`\ncould not compile\n"
        )
        run = orch._run_test_command("cargo test")
    assert mock.call_count == 1  # no retry
    assert run.verdict.kind == "compile_error"

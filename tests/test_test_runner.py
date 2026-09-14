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
    """A fake Popen: communicate() returns the canned output (s27-74 —
    the runner drives Popen+communicate so a timeout can killpg the
    whole process tree)."""

    def __init__(self, rc: int, out: str, err: str):
        self._rc = rc
        self._out = out
        self._err = err
        self.pid = 0

    def communicate(self, timeout=None):
        self.returncode = self._rc
        return self._out, self._err

    def kill(self):
        pass


def _orch(repo) -> Orchestrator:
    return Orchestrator(Config(), repo=str(repo), out=lambda *_a, **_k: None)


def test_runner_parses_cargo_pass(repo):
    """A passing cargo run gets verdict kind=passed."""
    runner = TestRunner(_orch(repo).git)
    with patch("capybase.adapters.tests.subprocess.Popen") as mock:
        mock.return_value = _Proc(0, "test result: ok. 5 passed; 0 failed\n", "")
        r = runner.run("cargo test")
    assert r.passed
    assert r.verdict.kind == "passed"
    assert r.verdict.tool == "cargo"


def test_runner_parses_cargo_lock_contention(repo):
    """``Blocking waiting for file lock`` → verdict kind=lock_contention (transient)."""
    runner = TestRunner(_orch(repo).git)
    with patch("capybase.adapters.tests.subprocess.Popen") as mock:
        mock.return_value = _Proc(
            -1, "",
            "   Blocking waiting for file lock on build directory\n",
        )
        r = runner.run("cargo test")
    assert r.verdict.kind == "lock_contention"
    assert r.verdict.is_transient


def test_runner_parses_compile_error(repo):
    runner = TestRunner(_orch(repo).git)
    with patch("capybase.adapters.tests.subprocess.Popen") as mock:
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
    with patch("capybase.adapters.tests.subprocess.Popen", side_effect=seq), \
         patch("time.sleep"):  # don't actually backoff in the test
        run = orch._run_test_command("cargo test")
    assert run.passed
    assert run.verdict.kind == "passed"


def test_orchestrator_gives_up_after_max_lock_retries(repo):
    """Persistent lock contention exhausts retries → returns the (failed) run."""
    orch = _orch(repo)
    locked = _Proc(-1, "", "   Blocking waiting for file lock on build directory\n")
    with patch("capybase.adapters.tests.subprocess.Popen", return_value=locked), \
         patch("time.sleep"):
        run = orch._run_test_command("cargo test")
    assert not run.passed
    assert run.verdict.kind == "lock_contention"


def test_orchestrator_does_not_retry_non_transient_failure(repo):
    """A compile error is NOT retried (it's a real failure, not transient)."""
    orch = _orch(repo)
    with patch("capybase.adapters.tests.subprocess.Popen") as mock:
        mock.return_value = _Proc(
            101, "", "error[E0433]: could not find `tools`\ncould not compile\n"
        )
        run = orch._run_test_command("cargo test")
    assert mock.call_count == 1  # no retry
    assert run.verdict.kind == "compile_error"


def test_lock_verdict_requires_real_transient_phrasing():
    """s27-76: the ninth pass wired _PYTEST_LOCK_RE with the BROAD form —
    bare 'lock' matched 'blocked'/'block' in ordinary failing output and
    burned 3 full-suite retries on non-transient failures. Only real
    transient-resource phrasing classifies as contention now."""
    from capybase.test_output import classify_test_output
    v_fail = classify_test_output(
        "pytest", "FAILED test_blocked.py::test_block - assert 0\n",
        "", returncode=1)
    assert v_fail.kind == "failed"
    v_lock = classify_test_output(
        "pytest", "OSError: [Errno 98] address already in use\n",
        "", returncode=1)
    assert v_lock.kind == "lock_contention"
    assert v_lock.is_transient


def test_runner_never_retries_a_passed_run(repo):
    """s27-76: a passing run whose output merely CONTAINS transient-looking
    text must not burn retries."""
    from unittest.mock import patch
    orch = _orch(repo)
    ok = _Proc(0, "test_x.py::test_lock PASSED\n"
                   "warning: resource temporarily unavailable\n", "")
    with patch("capybase.adapters.tests.subprocess.Popen",
               return_value=ok) as mock:
        run = orch._run_test_command("pytest -q")
    assert run.passed
    assert mock.call_count == 1

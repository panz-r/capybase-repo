"""S28-168 — single-flight builds + the SYNTAX_ONLY degrade honored at
the test gate.

The harvest census showed the slow duckdb PASS walls (930-1086s) were
87-98% build seconds, dominated by a CONCURRENT double build at
acceptance (two capped cmake processes starting within ~100ms —
duckdb-0080) plus a third capped gate build AFTER the session had
degraded to SYNTAX_ONLY. Covered here:

1. The BuildStateTracker single-flight registry: one owner, waiters
   adopt its outcome, one-shot release, same-thread reentry never
   self-waits, wait-timeout and foreign publish fail open.
2. The test gate skips an advisory BUILD command under the degrade
   (the gate's own doctrine: a re-run at the same cap adds zero
   information) and keeps running it when required or non-build.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from capybase.verification import BuildStateTracker


# ---------------------------------------------------------------------------
# The single-flight registry
# ---------------------------------------------------------------------------

def test_first_acquire_owns_and_publish_releases():
    t = BuildStateTracker()
    key = t.build_key("cmake --build build", "int main(){}\n")
    assert key is not None
    assert t.build_acquire(key, 0.1) == (True, None)
    t.build_publish(key, True, "all ok")
    # one-shot: the next acquire runs its own build
    assert t.build_acquire(key, 0.1) == (True, None)


def test_waiter_adopts_owner_outcome():
    t = BuildStateTracker()
    key = t.build_key("cmake --build build", "same content")
    assert t.build_acquire(key, 0.1) == (True, None)
    got: list = []

    def waiter():
        got.append(t.build_acquire(key, 5.0))

    w = threading.Thread(target=waiter)
    w.start()
    time.sleep(0.05)  # let the waiter block on the event
    t.build_publish(key, False, "make: *** Error 1")
    w.join(5)
    assert got == [(False, (False, "make: *** Error 1", False))]


def test_waiter_adopts_timed_out_outcome():
    t = BuildStateTracker()
    key = t.build_key("cmake --build build", "cold tree")
    t.build_acquire(key, 0.1)
    got: list = []

    def waiter():
        got.append(t.build_acquire(key, 5.0))

    w = threading.Thread(target=waiter)
    w.start()
    time.sleep(0.05)
    t.build_publish(key, False, "[ 87%] Building CXX object", timed_out=True)
    w.join(5)
    assert got == [(False, (False, "[ 87%] Building CXX object", True))]


def test_same_thread_reentry_never_self_waits():
    t = BuildStateTracker()
    key = t.build_key("make -j4", "x.c")
    assert t.build_acquire(key, 5.0) == (True, None)
    # a same-thread re-acquire (recursive validation of the same file)
    # must return ownership immediately, never block on itself
    assert t.build_acquire(key, 5.0) == (True, None)


def test_wait_timeout_fails_open_to_ownership():
    t = BuildStateTracker()
    key = t.build_key("make -j4", "x.c")
    t.build_acquire(key, 0.1)  # owner never publishes
    t0 = time.monotonic()
    assert t.build_acquire(key, 0.05) == (True, None)  # fail-open
    assert time.monotonic() - t0 < 1.0


def test_foreign_publish_is_ignored():
    t = BuildStateTracker()
    key = t.build_key("make -j4", "x.c")
    t.build_acquire(key, 0.1)  # owned by this thread
    # a foreign thread cannot publish into someone else's key
    foreign: list = []

    def interloper():
        t.build_publish(key, True, "bogus")
        foreign.append(True)

    th = threading.Thread(target=interloper)
    th.start()
    th.join(5)
    assert foreign == [True]
    # the key is still owned here: this thread's publish wins
    t.build_publish(key, False, "real")
    assert t.build_acquire(key, 0.1) == (True, None)  # released, one-shot


def test_disabled_single_flight_never_shares():
    t = BuildStateTracker(single_flight=False)
    assert t.build_key("make -j4", "x.c") is None
    assert t.build_acquire(None, 5.0) == (True, None)
    t.build_publish(None, True, "out")  # no-op, never raises


def test_build_key_distinguishes_content_and_command():
    t = BuildStateTracker()
    a = t.build_key("cmake --build build", "int x;\n")
    b = t.build_key("cmake --build build", "int y;\n")
    c = t.build_key("make -j4", "int x;\n")
    assert a != b and a != c


# ---------------------------------------------------------------------------
# The test gate honors the SYNTAX_ONLY degrade (advisory build gates skip)
# ---------------------------------------------------------------------------

class _RecJournal:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, payload, **_kw):
        self.events.append((event, payload))


class _StubOrch:
    """Just enough orchestrator for _run_tests (the compiler-authority
    stub shape); verification attachable per-test."""

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


def _result(paths=("src/text_format.cc",)):
    return SimpleNamespace(
        units_by_path={p: [] for p in paths}, escalated=False, reason="")


def _run(cmd_output: str, *, rc=0, passed=True):
    return SimpleNamespace(
        passed=passed, returncode=rc, timed_out=False,
        verdict=SimpleNamespace(kind="pass", summary="ok", diagnostics=[]),
        stdout=cmd_output, stderr="",
    )


def _degraded_tracker():
    t = BuildStateTracker()
    t.note_timeout("generic", "cmake --build build", 300)
    assert t.full_build_available is False  # the degrade fired
    return t


def test_degraded_advisory_build_gate_skips():
    from capybase.orchestrator import Orchestrator
    orch = _StubOrch(tests_required=False, pre_continue="cmake --build build")
    orch.verification = SimpleNamespace(build_state=_degraded_tracker())
    ran = {"build": False}

    def _no_run(cmd, *, cwd=None):  # the gate must never launch
        ran["build"] = True
        return _run("irrelevant")

    orch._run_test_command = _no_run
    ok = Orchestrator._run_tests(orch, "pre_continue", _result())
    assert ok is True  # advisory continue — same outcome as the rc=-1 kill
    assert ran["build"] is False
    assert orch._last_tests_compiler_indictment is False
    # REVIEW 2026-09-23: the report surface must not carry a previous
    # step's verdict into a skipped gate
    assert getattr(orch, "_last_test_verdict", None) is None
    events = [e for e, _ in orch.journal.events]
    assert "tests_build_skipped_degraded" in events
    assert "tests_started" not in events  # skipped BEFORE the gate launches


def test_degraded_required_gate_still_runs():
    from capybase.orchestrator import Orchestrator
    orch = _StubOrch(tests_required=True, pre_continue="cmake --build build")
    orch.verification = SimpleNamespace(build_state=_degraded_tracker())
    orch._run_test_command = lambda cmd, *, cwd=None: _run("ok")
    ok = Orchestrator._run_tests(orch, "pre_continue", _result())
    events = [e for e, _ in orch.journal.events]
    assert "tests_started" in events  # policy outranks the economics
    assert "tests_build_skipped_degraded" not in events
    assert ok is True


def test_healthy_session_never_skips_the_gate():
    from capybase.orchestrator import Orchestrator
    orch = _StubOrch(tests_required=False, pre_continue="cmake --build build")
    orch.verification = SimpleNamespace(build_state=BuildStateTracker())
    orch._run_test_command = lambda cmd, *, cwd=None: _run("ok")
    ok = Orchestrator._run_tests(orch, "pre_continue", _result())
    events = [e for e, _ in orch.journal.events]
    assert "tests_started" in events
    assert "tests_build_skipped_degraded" not in events
    assert ok is True


def test_degraded_non_build_gate_still_runs():
    from capybase.orchestrator import Orchestrator
    orch = _StubOrch(tests_required=False, pre_continue="pytest")
    orch.verification = SimpleNamespace(build_state=_degraded_tracker())
    orch._run_test_command = lambda cmd, *, cwd=None: _run("ok")
    ok = Orchestrator._run_tests(orch, "pre_continue", _result())
    events = [e for e, _ in orch.journal.events]
    assert "tests_started" in events  # a test-suite gate is not a build
    assert "tests_build_skipped_degraded" not in events
    assert ok is True

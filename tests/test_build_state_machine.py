"""Session build state machine + conditional retry — sprint-19 P3 (D1/D2).

protobuf-0067/0071: ~1020 of each case's 1200s budget went to four
sequential cold full-tree builds that could never finish under their
caps — every gate degraded to its tolerant fallback while burning wall
clock. The fixes tested here:

1. One doomed full build per session: after the first generic full-build
   timeout, verify_file skips further full builds (straight to the
   syntax-only fallback, journaled) and Phase-2's full-build fallback is
   skipped (journaled).
2. Conditional retry: recoverable failures (lock contention, compiler
   crash, network) get ONE retry at 2× the cap; generic timeouts get
   zero retries.
3. Every build probes and transitions are journaled (build_probe /
   build_state / build_retry) — the sprint-18 300s gaps were silent.
"""

from __future__ import annotations

import subprocess
import time
from types import SimpleNamespace

from capybase.verification import (
    BuildStateTracker,
    VerificationEngine,
    ValidationConfig,
    _classify_build_failure_kind,
)


# ---------------------------------------------------------------------------
# Failure-kind classification
# ---------------------------------------------------------------------------

def test_classify_lock_contention():
    assert _classify_build_failure_kind(
        "cargo: Blocking waiting for file lock on package cache") == (
        "lock_contention")


def test_classify_compiler_crash():
    assert _classify_build_failure_kind(
        "g++: internal compiler error: Segmentation fault\n"
        "Please submit a full bug report") == "compiler_crash"


def test_classify_network_transient():
    assert _classify_build_failure_kind(
        "fatal: unable to access: Could not resolve host: example.com"
    ) == "network_transient"


def test_classify_generic():
    assert _classify_build_failure_kind("") == "generic"
    assert _classify_build_failure_kind(
        "src/foo.c:12:5: error: use of undeclared identifier 'x'") == "generic"


# ---------------------------------------------------------------------------
# Tracker state machine
# ---------------------------------------------------------------------------

def test_generic_timeout_degrades_once():
    bs = BuildStateTracker()
    assert bs.full_build_available is True
    bs.note_timeout("generic", "make -j4", 300)
    assert bs.full_build_available is False
    # idempotent: no second transition event, no crash
    bs.note_timeout("generic", "make -j4", 300)
    assert bs.full_build_available is False


def test_recoverable_kind_does_not_degrade():
    bs = BuildStateTracker()
    bs.note_timeout("lock_contention", "cargo check", 120)
    assert bs.full_build_available is True


def test_events_flow_through_sink():
    events: list[tuple[str, dict]] = []
    bs = BuildStateTracker(event_sink=lambda e, p: events.append((e, p)))
    bs.record_probe("make -j4", 12.3, "pass", path="src/a.c")
    bs.note_timeout("generic", "make -j4", 300)
    kinds = [e for e, _ in events]
    assert "build_probe" in kinds and "build_state" in kinds
    probe = next(p for e, p in events if e == "build_probe")
    assert probe["outcome"] == "pass" and probe["duration_s"] == 12.3
    state = next(p for e, p in events if e == "build_state")
    assert state["state"] == "SYNTAX_ONLY"


def test_sink_failure_never_raises():
    def boom(event, payload):
        raise RuntimeError("journal down")
    bs = BuildStateTracker(event_sink=boom)
    bs.record_probe("make", 1.0, "pass")  # must not raise
    bs.note_timeout("generic", "make", 10)


def test_engine_has_tracker_by_default():
    eng = VerificationEngine.default(ValidationConfig())
    assert isinstance(eng.build_state, BuildStateTracker)
    assert eng.build_state.full_build_available is True


# ---------------------------------------------------------------------------
# verify_file build-branch behavior (fake subprocess, real engine)
# ---------------------------------------------------------------------------

class _FakeRun:
    """Patch target for subprocess.run inside verify_file."""

    def __init__(self, outcomes):
        # outcomes: list of callables raising or returning CompletedProcess
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def __call__(self, cmd, **kw):
        self.calls.append({"cmd": cmd, "timeout": kw.get("timeout")})
        out = self.outcomes.pop(0)
        return out()


def _engine_cc() -> tuple[VerificationEngine, _FakeRun]:
    eng = VerificationEngine.default(ValidationConfig())
    eng.config.cc_build_command = "make -j4"
    events: list[tuple[str, dict]] = []
    eng.build_state = BuildStateTracker(
        event_sink=lambda e, p: events.append((e, p)))
    eng.events = events
    return eng, None


def _completed(rc=0, err=""):
    return SimpleNamespace(returncode=rc, stderr=err, stdout="")


def test_timeout_degrades_and_next_call_skips(monkeypatch):
    import capybase.verification as V

    eng = VerificationEngine.default(ValidationConfig())
    eng.config.cc_build_command = "make -j4"
    eng.config.require_syntax_if_supported = True
    events: list[tuple[str, dict]] = []
    eng.build_state = BuildStateTracker(
        event_sink=lambda e, p: events.append((e, p)))
    calls: list[dict] = []

    def fake_run(cmd, **kw):
        calls.append({"timeout": kw.get("timeout")})
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    # The syntax-only fallback compiles via _compile_ccs (subprocess) —
    # neuter it to a deterministic pass.
    monkeypatch.setattr(V, "_compile_ccs",
                        lambda whole, **kw: (True, "ok"))
    monkeypatch.setattr(
        V.subprocess, "run", fake_run)

    code = "int main(void) { return 0; }\n"
    r1 = eng.verify_file(
        "a.c", "c", code, [], repo_root="/tmp", whole_text=code)
    assert eng.build_state.full_build_available is False
    assert calls and calls[0]["timeout"] == 300
    probes = [p for e, p in events if e == "build_probe"]
    assert any(p["outcome"] == "timeout" for p in probes)

    # Second verify_file on the same session: no subprocess build at all
    n_calls = len(calls)
    r2 = eng.verify_file(
        "b.c", "c", code, [], repo_root="/tmp", whole_text=code)
    assert len(calls) == n_calls  # skipped the build entirely
    probes2 = [p for e, p in events if e == "build_probe"]
    assert any(p["outcome"] == "skipped" for p in probes2)


def test_recoverable_timeout_retries_at_double_cap(monkeypatch):
    import capybase.verification as V

    eng = VerificationEngine.default(ValidationConfig())
    eng.config.cc_build_command = "make -j4"
    eng.build_state = BuildStateTracker()
    calls: list[dict] = []

    def fake_run(cmd, **kw):
        calls.append({"timeout": kw.get("timeout")})
        if len(calls) == 1:
            # timed out while waiting on a lock — recoverable
            raise subprocess.TimeoutExpired(
                cmd, kw.get("timeout"),
                output=b"cargo: Blocking waiting for file lock")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(V.subprocess, "run", fake_run)
    code = "int x;\n"
    eng.verify_file("a.c", "c", code, [], repo_root="/tmp", whole_text=code)
    # retried once at 2x cap, then succeeded
    assert [c["timeout"] for c in calls] == [300, 600]
    assert eng.build_state.full_build_available is True
    assert eng.build_state.recoverable_retry_count == 1


def test_second_recoverable_timeout_degrades(monkeypatch):
    import capybase.verification as V

    eng = VerificationEngine.default(ValidationConfig())
    eng.config.cc_build_command = "make -j4"
    eng.build_state = BuildStateTracker()
    monkeypatch.setattr(V, "_compile_ccs", lambda whole, **kw: (True, "ok"))
    calls: list[int] = []

    def fake_run(cmd, **kw):
        calls.append(kw.get("timeout"))
        raise subprocess.TimeoutExpired(
            cmd, kw.get("timeout"),
            output=b"internal compiler error")

    monkeypatch.setattr(V.subprocess, "run", fake_run)
    code = "int x;\n"
    eng.verify_file("a.c", "c", code, [], repo_root="/tmp", whole_text=code)
    # attempt 1 at 300 (recoverable) -> retry at 600 -> still timeout -> degrade
    assert calls == [300, 600]
    assert eng.build_state.full_build_available is False


def test_targeted_build_timeout_does_not_degrade(monkeypatch):
    import capybase.verification as V

    eng = VerificationEngine.default(ValidationConfig())
    eng.config.cc_build_command = "make -j4"
    eng.config.cc_build_target_template = "make {stem}.o"
    eng.build_state = BuildStateTracker()
    monkeypatch.setattr(V, "_compile_ccs", lambda whole, **kw: (True, "ok"))
    monkeypatch.setattr(
        V.subprocess, "run",
        lambda cmd, **kw: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd, kw.get("timeout"))))

    code = "int x;\n"
    eng.verify_file("a.c", "c", code, [], repo_root="/tmp", whole_text=code)
    # a targeted .o timeout is not evidence the full tree can't finish
    assert eng.build_state.full_build_available is True

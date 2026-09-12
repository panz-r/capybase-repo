"""The batch runner + harness enrichment (s27-61): lockfile, resume,
hang detection, journal counters, stop-cascade classification, smoke.

Every operational behavior here encodes a real incident: the lockfile is
the s27-60 two-batches-one-endpoint race; INFRA_HANG is the libuv-0019
endpoint-hang signature; result_is_complete is the crash-recovery rule
(a killed run's partial JSON must not read as done).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_batch = _load("run_scenario_batch", _SCRIPTS / "run_scenario_batch.py")
_harness = _load(
    "live_eval_scenarios_for_batch_tests",
    _SCRIPTS / "live_eval_scenarios.py")


# ---------------------------------------------------------------------------
# lockfile
# ---------------------------------------------------------------------------

def test_lockfile_acquire_and_release(tmp_path):
    assert _batch.acquire_lock(tmp_path) is True
    assert (tmp_path / "batch.lock").exists()
    _batch.release_lock(tmp_path)
    assert not (tmp_path / "batch.lock").exists()


def test_lockfile_refuses_live_holder(tmp_path):
    assert _batch.acquire_lock(tmp_path) is True
    # The lock is held by THIS process's pid — a second acquire (as if
    # from another batch) must refuse, not take over.
    assert _batch.acquire_lock(tmp_path) is False
    _batch.release_lock(tmp_path)


def test_lockfile_takes_over_stale(tmp_path):
    (tmp_path / "batch.lock").write_text("999999 0\n")  # dead pid
    assert _batch.acquire_lock(tmp_path) is True
    _batch.release_lock(tmp_path)


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------

def test_result_is_complete(tmp_path):
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps([{"id": "x", "verdict": "PASS"}]))
    assert _batch.result_is_complete(ok) is True

    partial = tmp_path / "partial.json"
    partial.write_text('[{"id": "x", "verdict"')  # killed mid-write
    assert _batch.result_is_complete(partial) is False

    noverdict = tmp_path / "noverdict.json"
    noverdict.write_text(json.dumps([{"id": "x"}]))
    assert _batch.result_is_complete(noverdict) is False

    assert _batch.result_is_complete(tmp_path / "missing.json") is False


def test_result_verdict(tmp_path):
    f = tmp_path / "r.json"
    f.write_text(json.dumps([{"id": "x", "verdict": "INFRA_HANG"}]))
    assert _batch.result_verdict(f) == "INFRA_HANG"


def test_manifest_roundtrip_preserves_entries(tmp_path):
    assert _batch.read_manifest(tmp_path) == []
    _batch.write_manifest(tmp_path, [{"scenario": "a", "verdict": "PASS"}])
    _batch.write_manifest(tmp_path, [
        {"scenario": "a", "verdict": "PASS"},
        {"scenario": "b", "verdict": "PARTIAL"}])
    got = _batch.read_manifest(tmp_path)
    assert [e["scenario"] for e in got] == ["a", "b"]


# ---------------------------------------------------------------------------
# hang detector
# ---------------------------------------------------------------------------

def test_hang_detector_flat_is_hang():
    d = _batch.HangDetector(threshold_s=900.0)
    assert d.update(0.0, 100, 1000, 50) is False       # first snapshot
    assert d.update(400.0, 100, 1000, 50) is False     # flat but < threshold
    assert d.update(1000.0, 100, 1000, 50) is True     # flat past threshold


def test_hang_detector_any_movement_resets():
    d = _batch.HangDetector(threshold_s=900.0)
    d.update(0.0, 100, 1000, 50)
    # journal grows (LLM candidate journaled) — not a hang
    assert d.update(1000.0, 5000, 1000, 50) is False
    # io moves (git replay) — not a hang
    assert d.update(1900.0, 5000, 999999, 50) is False
    # cpu moves — not a hang
    assert d.update(2800.0, 5000, 999999, 5000) is False
    # then truly flat past the threshold → hang
    assert d.update(3700.0, 5000, 999999, 5000) is True


def test_worktree_line_regex():
    m = _batch._WORKTREE_RE.match("  worktree=/tmp/capy-scen-abc123\n")
    assert m and m.group(1) == "/tmp/capy-scen-abc123"
    assert _batch._WORKTREE_RE.match("[scenario] x ...\n") is None


# ---------------------------------------------------------------------------
# journal counters (harness enrichment)
# ---------------------------------------------------------------------------

def test_journal_counters(tmp_path):
    j = tmp_path / "journal.jsonl"
    lines = [
        {"event_type": "session_started"},
        {"event_type": "candidate_generated", "payload": {"n_candidates": 1}},
        {"event_type": "candidate_accepted",
         "payload": {"via": "convergence_seed"}},
        {"event_type": "candidate_accepted",
         "payload": {"via": "convergence_seed"}},
        {"event_type": "candidate_accepted", "payload": {"via": "structural"}},
        {"event_type": "candidate_generated", "payload": {}},
        {"event_type": "file_staged", "payload": {"path": "x"}},
        # trailing partial line (killed mid-write) — skipped, not fatal
    ]
    with open(j, "w") as fh:
        for e in lines:
            fh.write(json.dumps(e) + "\n")
        fh.write('{"event_type": "candidate_gen')
    got = _harness._journal_counters(j)
    assert got["llm_calls"] == 2
    assert got["mechanism_accepts"] == {
        "convergence_seed": 2, "structural": 1}


def test_journal_counters_missing_file(tmp_path):
    got = _harness._journal_counters(tmp_path / "nope.jsonl")
    assert got == {"llm_calls": 0, "mechanism_accepts": {}}


# ---------------------------------------------------------------------------
# stop-cascade classification shape (the enrichment's contract)
# ---------------------------------------------------------------------------

def test_stop_cascade_classification_contract():
    # The harness sets stop_cascade on ok=False files that still carry
    # markers when the row escalated: those misses measure the STOP, not
    # the resolver. (Executed inline in run_scenario; this pins the rule
    # as documented behavior.)
    results = [
        {"path": "a.py", "sim": 0.0, "markers": True, "ok": False},
        {"path": "b.py", "sim": 0.85, "markers": False, "ok": False},
        {"path": "c.py", "sim": 1.0, "markers": False, "ok": True},
    ]
    escalated = True
    cascade = 0
    if escalated:
        for r in results:
            if r.get("ok") is False and r.get("markers"):
                r["stop_cascade"] = True
                cascade += 1
    assert cascade == 1
    assert results[0].get("stop_cascade") is True
    assert "stop_cascade" not in results[1]


# ---------------------------------------------------------------------------
# smoke
# ---------------------------------------------------------------------------

def test_smoke_pattern_assertions_pass():
    # In the hermetic test env no provider is configured: the stub-path
    # arm is loudly skipped and the generator-pattern assertions run.
    fails = _harness.smoke()
    assert fails == []


# ---------------------------------------------------------------------------
# synthetic results
# ---------------------------------------------------------------------------

def test_synthetic_result_writes_infra_hang(tmp_path):
    f = tmp_path / "r-x.json"
    _batch._synthetic_result(
        f, "x", "INFRA_HANG", "reason here", 2)
    row = json.loads(f.read_text())[0]
    assert row["verdict"] == "INFRA_HANG"
    assert row["attempts"] == 2
    assert _batch.result_is_complete(f) is True

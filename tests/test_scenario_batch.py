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
    """s27-61: escalated + markers ⇒ the miss measures the replay STOP, not
    the resolver. Now tests the REAL harness function (the old copy-paste
    inline version had zero coupling)."""
    _harness = _load("live_eval_scenarios_for_batch_tests",
                     _SCRIPTS / "live_eval_scenarios.py")
    results = [
        {"path": "a.py", "sim": 0.0, "markers": True, "ok": False},
        {"path": "b.py", "sim": 0.85, "markers": False, "ok": False},
        {"path": "c.py", "sim": 1.0, "markers": False, "ok": True},
    ]
    n = _harness.classify_stop_cascade(results, escalated=True)
    assert n == 1
    assert results[0].get("stop_cascade") is True
    assert "stop_cascade" not in results[1]
    # Not escalated → no flags at all (fresh dicts — the first call
    # mutated results[0] in place)
    results2 = [{"path": "a.py", "sim": 0.0, "markers": True, "ok": False}]
    assert _harness.classify_stop_cascade(results2, escalated=False) == 0
    assert "stop_cascade" not in results2[0]
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


def test_all_absent_agreeing_replay_is_not_divergent():
    """s27-71 (fifth-pass C3): a scenario whose every touched path is
    absent at the oracle AND the replay agreed on every deletion has
    nothing to score — the old chain fell through to ORACLE_DIVERGENT (a
    false FAIL no resolver quality could avoid)."""
    _harness = _load("live_eval_scenarios_for_batch_tests",
                     _SCRIPTS / "live_eval_scenarios.py")
    v = _harness._chain_verdict(
        escalated=False, holes=False, scored=[], n_ok=0,
        absent=[{"path": "a"}], kept_in_replay=0)
    assert v == "ALL_ABSENT"
    # any kept-in-replay file is the genuine resurrection signal
    v2 = _harness._chain_verdict(
        escalated=False, holes=False, scored=[], n_ok=0,
        absent=[{"path": "a"}], kept_in_replay=1)
    assert v2 == "ORACLE_DIVERGENT"
    # ordinary chains unchanged
    assert _harness._chain_verdict(False, False, [{"ok": True}], 1, [], 0) == "PASS"
    assert _harness._chain_verdict(True, False, [], 0, [], 0) == "ESCALATE"


def test_smoke_uses_production_generator_pattern():
    """s27-71 (fifth-pass C2): smoke must assert the PRODUCTION _GEN_OUTPUT
    (module-scope) — its old inline copy had silently reverted to the dead
    pre-s27-67 `/generated_` alternative and could not catch divergence."""
    _harness = _load("live_eval_scenarios_for_batch_tests",
                     _SCRIPTS / "live_eval_scenarios.py")
    gen = _harness._GEN_OUTPUT
    # production families still match (incl. the s27-60 broadened set)
    for name in ("compiled_grammar.cpp", "transform_generated_trampoline.cpp",
                 "messages.pb.cc", "autogenerated_settings.cpp"):
        assert gen.search(name), name
    for name in ("Cargo.lock", "package-lock.json", "main.cpp"):
        assert not gen.search(name), name


def test_journal_counters_subtract_superseded(tmp_path):
    """s27-72 (sixth pass): a whole-file takeover supersedes earlier per-unit
    accepts — both journaled candidate_accepted events. The counters must
    subtract the superseded ids or every takeover double-counts (and the
    stale mechanism wins the attribution)."""
    _harness = _load("live_eval_scenarios_for_batch_tests",
                     _SCRIPTS / "live_eval_scenarios.py")
    j = tmp_path / "journal.jsonl"
    lines = [
        {"event_type": "candidate_accepted",
         "payload": {"via": "plain_llm", "candidate_id": "u1:1:c1"}},
        {"event_type": "candidate_accepted",
         "payload": {"via": "plain_llm", "candidate_id": "u1:2:c2"}},
        {"event_type": "outcomes_superseded",
         "payload": {"candidate_ids": ["u1:1:c1", "u1:2:c2"]}},
        {"event_type": "candidate_accepted",
         "payload": {"via": "deterministic_source_current_only",
                     "candidate_id": "f:floor"}},
    ]
    j.write_text("\n".join(json.dumps(l) for l in lines))
    got = _harness._journal_counters(j)
    assert got["mechanism_accepts"] == {
        "deterministic_source_current_only": 1}


def test_reconcile_emits_outcomes_superseded(tmp_path):
    """s27-73: the counters' superseded subtraction consumes the
    outcomes_superseded event — pin the EMIT half too (the s27-72 test
    covered only the subtraction; reader and writer shared an unpinned
    key)."""
    from types import SimpleNamespace
    from capybase.orchestrator import (
        Orchestrator, UnitOutcome, reconcile_whole_file_outcomes)

    events = []

    class _Journal:
        def emit(self, *args, **kwargs):
            events.append(args)

    class _U:
        unit_id = "f:1:0"
        path = "f"
        unit_kind = "text_marker_block"

    class _C1:
        candidate_id = "f:1:0:c1"

    class _C2:
        candidate_id = "f:1:0:wf"

    outcome = UnitOutcome(unit=SimpleNamespace(
        unit_id=_U.unit_id, path=_U.path, unit_kind=_U.unit_kind))
    outcome.accepted = _C1()
    result = SimpleNamespace(outcomes=[outcome], step_index=3)

    from capybase.conflict_model import ConflictUnit
    wf_unit = SimpleNamespace(unit_id="f:wf", path="f",
                              unit_kind="whole_file")
    pairs = [(wf_unit, _C2())]
    # reconcile first (marks superseded), then the orchestrator's
    # _reconcile_and_record tail emits the journal event.
    reconcile_whole_file_outcomes(result, {"f": pairs})
    assert outcome.superseded is True

    stub = SimpleNamespace(journal=_Journal(), step=3,
                           memory_store=None,
                           _step_accepted_by_path={"f": pairs},
                           _record_outcomes_to_memory=lambda r: None)
    Orchestrator._reconcile_and_record(stub, result)
    sup = [a for a in events if a[0] == "outcomes_superseded"]
    assert sup, "outcomes_superseded not emitted"
    payload = sup[0][1]
    assert "f:1:0:c1" in payload["candidate_ids"]


def test_reconcile_emits_candidate_accepted_for_whole_file_take():
    """s27-74 fix: the fresh whole-file outcome's candidate_accepted emit
    must fire even though reconcile appends the outcome BEFORE the emit
    loop — the s27-74 first attempt emitted after the append and its
    already-appended id check skipped every emit (the whole-file
    attribution stayed dead while the commit claimed to fix it)."""
    from types import SimpleNamespace
    from capybase.orchestrator import Orchestrator, UnitOutcome

    events = []

    class _Journal:
        def emit(self, *args, **kwargs):
            events.append(args)

    class _C1:
        candidate_id = "g:1:0:c1"

    class _CWF:
        candidate_id = "g:1:0:tsp"
        model_name = "true_side_portfolio"

    outcome = UnitOutcome(unit=SimpleNamespace(
        unit_id="g:1:0", path="g", unit_kind="text_marker_block"))
    outcome.accepted = _C1()
    result = SimpleNamespace(outcomes=[outcome], step_index=6)

    wf_unit = SimpleNamespace(unit_id="g:wf", path="g",
                              unit_kind="whole_file")
    pairs = [(wf_unit, _CWF())]
    stub = SimpleNamespace(journal=_Journal(), step=6,
                           memory_store=None,
                           _step_accepted_by_path={"g": pairs},
                           _record_outcomes_to_memory=lambda r: None)
    Orchestrator._reconcile_and_record(stub, result)
    # a SECOND call (the escalation-exit + tail pattern) must not
    # double-emit — _pre_ids now contains the whole-file id
    Orchestrator._reconcile_and_record(stub, result)

    takes = [a for a in events if a[0] == "candidate_accepted"
             and a[1].get("candidate_id") == "g:1:0:tsp"]
    assert len(takes) == 1, (
        f"expected exactly 1 whole-file accept event, got {len(takes)}")
    assert takes[0][1]["via"] == "true_side_portfolio"


def test_stub_path_leak_armor():
    """s27-73: C1's confinement armor, now a testable function. The two
    neutralized fields MAY carry the stub path (run_scenario overwrites
    them); any OTHER tests-field surface is a leak."""
    _harness = _load("live_eval_scenarios_for_batch_tests",
                     _SCRIPTS / "live_eval_scenarios.py")
    # instance-attribute objects (vars()-readable), like a real pydantic
    # config section is via model_fields + getattr.
    from types import SimpleNamespace
    tests = SimpleNamespace(
        pre_continue="python3 -m py_compile scenario.rs",   # allowed
        final="true",                                        # clean
        post_continue="cat scenario.rs",                     # LEAK
    )
    leaks = _harness._stub_path_leaks(SimpleNamespace(tests=tests))
    assert leaks == ["tests.post_continue"], leaks
    assert _harness._stub_path_leaks(SimpleNamespace(
        tests=SimpleNamespace(pre_continue="true", final="true"))) == []


def test_smoke_uses_production_pattern_and_armor():
    """s27-73: pin the WIRING, not just the content — smoke must reference
    the hoisted _GEN_OUTPUT (the C2 divergence guard) and the extracted
    _stub_path_leaks (the C1 armor), not inline copies."""
    import inspect
    _harness = _load("live_eval_scenarios_for_batch_tests",
                     _SCRIPTS / "live_eval_scenarios.py")
    smoke_src = inspect.getsource(_harness.smoke)
    assert "_GEN_OUTPUT" in smoke_src, (
        "smoke no longer asserts the production generator pattern")
    assert "_stub_path_leaks" in smoke_src, (
        "smoke no longer drives the stub-path leak armor")
    assert "re.compile" not in smoke_src, (
        "smoke re-gained an inline pattern copy (the C2 defect shape)")


def test_wiring_no_dead_generator_alternative():
    """s27-74: precise version of the wiring guard — the dead pre-s27-67
    alternative (/generated_ on a basename) must not reappear inside
    smoke(); unrelated legitimate regexes are fine."""
    import inspect
    _harness = _load("live_eval_scenarios_for_batch_tests",
                     _SCRIPTS / "live_eval_scenarios.py")
    import ast as _ast
    tree = _ast.parse(inspect.getsource(_harness.smoke))
    _ast.Str = getattr(_ast, "Str", _ast.Constant)
    code_only = _ast.get_source_segment or None
    # strip docstrings/comments the robust way: compare the AST dump for a
    # string constant CONTAINING the dead regex fragment (a code literal,
    # not prose).
    dead_in_code = any(
        isinstance(node, _ast.Constant) and isinstance(node.value, str)
        and "/generated_)" in node.value
        for node in _ast.walk(tree))
    assert not dead_in_code, (
        "smoke re-gained the dead /generated_ inline alternative")

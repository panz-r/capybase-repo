"""Unit tests for make_results_round's metric layer.

Pins the 2026-09-18 corrections: the llm column is WHOLE-PROCESS model
involvement (row flag first, journal predicate fallback — never the
final resolution bucket), per-language percentages use era-excluded
denominators rounded once, `accepted` = counted − escalated, and Δ is
the difference of unrounded P+W values rounded once.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load_mod():
    spec = importlib.util.spec_from_file_location(
        "make_results_round",
        Path(__file__).resolve().parent.parent / "scripts"
        / "make_results_round.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["make_results_round"] = mod
    spec.loader.exec_module(mod)  # type: ignore[arg-type]
    return mod


_mod = _load_mod()


def _journal(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


# --- llm: the row flag is the source of truth -------------------------

def test_llm_flag_true_short_circuits():
    assert _mod._llm_involved({"id": "x", "model_involved": True}, {}) is True


def test_llm_flag_false_short_circuits():
    # Even with journals present, an explicit flag wins (the harness set
    # it at run time from its own call counter — strictly more exact).
    assert _mod._llm_involved({"id": "x", "model_involved": False}, {}) is False


def test_llm_missing_flag_none_value_is_absent():
    # Extract rows materialize model_involved: None via _FIELDS — None
    # must be treated as absent so the journal fallback still runs (the
    # bug that zeroed the whole column during the reproduction gate).
    idx = {"x": [None]}  # placeholder replaced below
    tmp = pytest.AdvancedPath if False else None
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        j = Path(td) / "x" / "sess1" / "journal.jsonl"
        _journal(j, [{"event_type": "candidate_generated", "payload": {}}])
        assert _mod._llm_involved(
            {"id": "x", "model_involved": None}, {"x": [str(j)]}) is True


# --- llm: the journal predicate ----------------------------------------

def test_llm_journal_predicate_each_event():
    import tempfile
    cases = [
        ("candidate_generated", {}),
        ("comment_plan_generated", {}),
        ("comment_model_call_failed", {}),
        ("f1_tier2_adjudication_declined", {}),
        ("block_capture_decision", {"reason": "live functionality"}),
        ("midband_subsumption_gate",
         {"adjudication": {"verdict": "keep", "confidence": 0.95}}),
    ]
    for event_type, payload in cases:
        with tempfile.TemporaryDirectory() as td:
            j = Path(td) / "c" / "s" / "journal.jsonl"
            _journal(j, [{"event_type": event_type, "payload": payload}])
            assert _mod._llm_involved({"id": "c"}, {"c": [str(j)]}) is True, \
                event_type


def test_llm_journal_no_hits_is_false():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        j = Path(td) / "c" / "s" / "journal.jsonl"
        _journal(j, [
            {"event_type": "structurally_resolved", "payload": {}},
            {"event_type": "candidate_accepted", "payload": {
                "via": "structural"}},
            {"event_type": "block_capture_decision", "payload": {
                "reason": None}},
        ])
        assert _mod._llm_involved({"id": "c"}, {"c": [str(j)]}) is False


def test_llm_no_flag_no_flights_is_unknown():
    # Old results files rerun without --flights: honest unknown (None),
    # never a silent bucket fallback.
    assert _mod._llm_involved(
        {"id": "x", "resolution_bucket": "llm_cegis"}, {}) is None


def test_llm_never_uses_resolution_bucket():
    # The bug this module exists to prevent: bucket-llm rows without any
    # model-call evidence in the journal must NOT count as involved via
    # the bucket.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        j = Path(td) / "c" / "s" / "journal.jsonl"
        _journal(j, [{"event_type": "file_validated", "payload": {}}])
        rec = {"id": "c", "resolution_bucket": "llm_cegis"}
        assert _mod._llm_involved(rec, {"c": [str(j)]}) is False


# --- flights index ------------------------------------------------------

def test_flights_index_orders_newest_noncrashed_first():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _journal(root / "flights" / "c" / "s-crashed" / "journal.jsonl",
                 [{"event_type": "session_started"}])
        _journal(root / "flights" / "c" / "s2" / "journal.jsonl",
                 [{"event_type": "session_started"}])
        idx = _mod._flights_index(root)
        assert list(idx) == ["c"]
        assert "s2" in idx["c"][0] and "s-crashed" in idx["c"][1]


# --- recount arithmetic (era-excluded, rounded once) --------------------

def _run_main(tmp_path: Path, records: list[dict], extra_args=None):
    results = tmp_path / "results.json"
    results.write_text(json.dumps(records))
    argv = ["make_results_round.py", "--results", str(results),
            "--out", str(tmp_path / "out"), "--round", "t"]
    if extra_args:
        argv += extra_args
    old_argv, sys.argv = sys.argv, argv
    try:
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _mod.main()
    finally:
        sys.argv = old_argv
    return json.loads((tmp_path / "out" / "meta.json").read_text())


def test_recount_percentages_and_accepted(tmp_path):
    recs = [
        {"id": "a", "language": "c", "verdict": "PASS",
         "toolchain_dead": False, "model_involved": True},
        {"id": "b", "language": "c", "verdict": "PASS",
         "toolchain_dead": False, "model_involved": False},
        {"id": "c", "language": "c", "verdict": "WORKING",
         "toolchain_dead": False, "model_involved": True},
        {"id": "d", "language": "c", "verdict": "ESCALATE",
         "toolchain_dead": False, "model_involved": True},
        {"id": "e", "language": "c", "verdict": "ESCALATE",
         "toolchain_dead": True, "model_involved": False},
        # SAFE_SKIP: excluded from every denominator
        {"id": "f", "language": "c", "verdict": "ESCALATE",
         "terminal_reason": "SAFE_SKIP"},
    ]
    meta = _run_main(tmp_path, recs)
    r = meta["recount"]
    c = r["by_language"]["c"]
    # cases 5 (skip excluded), pass 2, working 1, era 1, llm 3
    assert (c["cases"], c["pass"], c["working"], c["era_dead"]) == (5, 2, 1, 1)
    assert c["llm"] == 3
    # era-excluded denominators, rounded once from unrounded values
    assert c["pass_pct"] == 40.0          # 2/5
    assert c["adj_pct"] == 50.0           # 2/(5-1)
    assert c["pw_adj_pct"] == 75.0        # 3/4
    # accepted = counted − escalated (ESCALATE + ESCALATE_TOOLCHAIN)
    assert r["accepted"] == 3
    # header + separator + one language row + total = 4 lines
    assert len(r["readme_table"].splitlines()) == 4
    assert "| c | 5 | 2 | 1 | 1 | 3 | 40.0% | 50.0% | 75.0% |" in r["readme_table"]


def test_delta_convention_unrounded_difference(tmp_path):
    cur = [
        {"id": "a", "language": "c", "verdict": "PASS"},
        {"id": "b", "language": "c", "verdict": "PASS"},
        {"id": "c", "language": "c", "verdict": "WORKING"},
    ]
    prior = [
        {"id": "a", "language": "c", "verdict": "PASS"},
        {"id": "b", "language": "c", "verdict": "ESCALATE"},
        {"id": "c", "language": "c", "verdict": "ESCALATE"},
    ]
    prior_path = tmp_path / "prior.json"
    prior_path.write_text(json.dumps(prior))
    meta = _run_main(tmp_path, cur,
                     extra_args=["--prior", str(prior_path)])
    r = meta["recount"]
    # current P+W = 3/3 = 100.0; prior = 1/3 = 33.333 → Δ = 66.7
    assert r["delta_pw_adj_pct"]["c"] == 66.7
    assert r["delta_convention"] == (
        "difference of unrounded P+W adj values, rounded once")
    assert "Δ P+W |" in r["readme_table"]
    assert "+66.7pp" in r["readme_table"]


def test_no_prior_means_no_delta_column(tmp_path):
    meta = _run_main(tmp_path, [
        {"id": "a", "language": "c", "verdict": "PASS"}])
    assert "Δ P+W |" not in meta["recount"]["readme_table"]

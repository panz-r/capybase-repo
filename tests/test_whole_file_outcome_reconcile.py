"""Whole-file outcome reconciliation (s27-63): whole-file swaps
(true_side_portfolio, phase1 fast path, deletion-respect, midband)
replace per-unit candidates without recording outcomes — the bucket
classifier attributed the file to the stale mechanism (or saw nothing,
the ~77-case harvest gap). The reconciliation supersedes the stale
per-unit outcomes and appends the whole-file ones; the classifier
skips superseded outcomes.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path

from capybase.conflict_model import (
    ConflictUnit, CandidateResolution, ConflictSide)
from capybase.orchestrator import (
    UnitOutcome,
    StepResult,
    reconcile_whole_file_outcomes,
)

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_lrw = _load("live_eval_realworld_reconcile_tests",
             _SCRIPTS / "live_eval_realworld.py")


def _unit(path="f.rs", kind="text_marker_block"):
    return ConflictUnit(
        session_id="s", step_index=0, path=path, language="rust",
        unit_id=f"{path}:u", unit_kind=kind,
        base=ConflictSide(label="BASE", text=""),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=""),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=""),
        original_worktree_text="", marker_span=(0, 1),
    )


def _cand(cid, prov):
    return CandidateResolution(
        candidate_id=cid, unit_id=cid, model_name="m",
        prompt_version="v", resolved_text="x", provenance=prov,
    )


def _whole_file_unit(path="f.rs"):
    u = _unit(path, kind="whole_file")
    u.marker_span = None
    return u


def test_reconcile_supersedes_stale_and_appends_whole_file():
    stale_unit = _unit()
    stale = UnitOutcome(
        unit=stale_unit,
        accepted=_cand("stale-llm", "plain_llm"))
    result = StepResult(step_index=0, outcomes=[stale])
    wf_unit = _whole_file_unit()
    pairs = [(wf_unit, _cand("f.rs:true_side_stage:current",
                             "deterministic_source_current_only_stage"))]
    n = reconcile_whole_file_outcomes(result, {"f.rs": pairs})
    assert n == 1
    assert stale.superseded is True
    fresh = [o for o in result.outcomes if not o.superseded]
    assert len(fresh) == 1
    assert fresh[0].accepted.provenance == (
        "deterministic_source_current_only_stage")


def test_classify_skips_superseded_outcomes():
    stale = UnitOutcome(
        unit=_unit(), accepted=_cand("stale-llm", "plain_llm"))
    stale.superseded = True
    fresh = UnitOutcome(
        unit=_whole_file_unit(),
        accepted=_cand("wf", "deterministic_source_current_only_stage"))
    bucket, mix = _lrw.classify_resolution_bucket([stale, fresh])
    # Without the skip, the stale plain_llm outcome would bucket the case
    # llm_one_shot even though the whole-file swap produced the file.
    assert bucket == "deterministic"
    assert mix == {"deterministic_source_current_only_stage": 1}


def test_classify_superseded_cegis_does_not_count():
    stale1 = UnitOutcome(unit=_unit(), accepted=_cand("a", "plain_llm"))
    stale2 = UnitOutcome(unit=_unit(), accepted=_cand("b", "plain_llm"))
    stale1.superseded = stale2.superseded = True
    fresh = UnitOutcome(
        unit=_whole_file_unit(),
        accepted=_cand("wf", "deterministic_source_replayed_only_stage"))
    bucket, mix = _lrw.classify_resolution_bucket([stale1, stale2, fresh])
    assert bucket == "deterministic"


def test_reconcile_ignores_per_unit_paths():
    """Convergence-seed-style pairs (original text_marker_block units) are
    NOT whole-file — their accurate outcomes are untouched."""
    unit = _unit()
    acc = _cand("conv", "deterministic_convergence_seed")
    outcome = UnitOutcome(unit=unit, accepted=acc)
    result = StepResult(step_index=0, outcomes=[outcome])
    n = reconcile_whole_file_outcomes(
        result, {"f.rs": [(unit, acc)]})
    assert n == 0
    assert outcome.superseded is False
    assert len(result.outcomes) == 1


def test_reconcile_is_idempotent():
    wf_unit = _whole_file_unit()
    pairs = [(wf_unit, _cand("wf", "deterministic_source_current_only_stage"))]
    result = StepResult(step_index=0, outcomes=[])
    n1 = reconcile_whole_file_outcomes(result, {"f.rs": pairs})
    n2 = reconcile_whole_file_outcomes(result, {"f.rs": pairs})
    assert (n1, n2) == (1, 0)
    assert len([o for o in result.outcomes if not o.superseded]) == 1


def test_reconcile_mixed_paths_only_touches_whole_file_paths():
    stale_wf = UnitOutcome(unit=_unit("wf.rs"),
                           accepted=_cand("stale", "plain_llm"))
    keep = UnitOutcome(unit=_unit("keep.rs"),
                       accepted=_cand("keep", "plain_llm"))
    result = StepResult(step_index=0, outcomes=[stale_wf, keep])
    reconcile_whole_file_outcomes(result, {
        "wf.rs": [(_whole_file_unit("wf.rs"),
                   _cand("wf", "deterministic_source_current_only_stage"))],
        "keep.rs": [( _unit("keep.rs"),
                      _cand("keep2", "deterministic_structural"))],
    })
    assert stale_wf.superseded is True
    assert keep.superseded is False

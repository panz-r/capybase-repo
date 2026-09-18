"""S28-38: the empty_fast_fail fallback iterates sides in CHURN order.

Pre-fix the fallback tried ("current", "replayed") in fixed iteration
order, so when both pristine sides pass verification the FIRST side won
regardless of evidence — the churn loser in 11 of the 14 corpus firings
(zenodo-hdiff-0100's oracle sat in replayed; the fallback took current).
The S28-38 census licenses churn-ordering: +1.36 oracle-sim over the 14
firings, ZERO counterexamples. Both directions here: the churn winner
must win whichever side it is, and a churn-computation failure must
degrade to the old fixed order without crashing.
"""

from __future__ import annotations

from pathlib import Path

from capybase.conflict_model import CandidateResolution
from capybase.config import Config
from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.orchestrator import Orchestrator
from capybase.resolution_engine import ResolutionEngine
from capybase.verification import VerificationResult

from tests.conftest import git


def _unit(base: str, current: str, replayed: str) -> ConflictUnit:
    return ConflictUnit(
        session_id="s", step_index=0, path="f.py", language="python",
        unit_id="f.py:0",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text=(
            f"<<<<<<< HEAD\n{current}=======\n{replayed}>>>>>>> branch\n"),
    )


def _pass_all_engine():
    """Stub verification: both pristine sides pass (the S28-38 firing
    precondition — the fallback only picks when a side verifies)."""

    class _PassAll:
        def verify(self, unit, cand, **kw):
            return VerificationResult(
                candidate_id=cand.candidate_id, unit_id=cand.unit_id,
                passed=True, hard_failures=[], warnings=[], features={},
            )

        def verify_file(self, *a, **kw):
            raise AssertionError(
                "P2 whole-side fallback must not run when a unit-level "
                "side already passed")

    return _PassAll()


def _orch(tmp_path: Path) -> Orchestrator:
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    orch = Orchestrator(
        cfg, repo=str(repo),
        resolution_engine=ResolutionEngine(cfg.model),
        out=lambda *_a, **_k: None,
    )
    orch.verification = _pass_all_engine()
    return orch


def _failed_candidate(unit: ConflictUnit) -> CandidateResolution:
    return CandidateResolution(
        candidate_id=f"{unit.unit_id}:empty", unit_id=unit.unit_id,
        model_name="fake", prompt_version="t", resolved_text="",
    )


# 6-line base; current flips one line (churn 2); replayed flips three
# (churn 6) — the zenodo-hdiff-0100 shape: the oracle side churns more.
_ZENODO_BASE = "\n".join(f"line{i} = {i}" for i in range(6)) + "\n"
_ZENODO_CURRENT = _ZENODO_BASE.replace("line2 = 2", "line2 = 22")
_ZENODO_REPLAYED = "\n".join(
    f"line{i} = {i}0" if i in (1, 3, 4) else f"line{i} = {i}"
    for i in range(6)) + "\n"


def test_churn_winner_replayed_is_picked(tmp_path: Path):
    """zenodo-hdiff-0100 direction: replayed churns more and BOTH sides
    verify — the fallback must take replayed (pre-fix took current by
    iteration order)."""
    orch = _orch(tmp_path)
    unit = _unit(_ZENODO_BASE, _ZENODO_CURRENT, _ZENODO_REPLAYED)
    outcome = orch._empty_fast_fail_recovery(unit, _failed_candidate(unit))
    assert outcome is not None
    assert outcome.accepted.provenance == "deterministic_source_replayed_only", (
        "the churn winner (replayed) must be tried first when both sides "
        "pass verification")


def test_churn_winner_current_is_picked(tmp_path: Path):
    """cython-history-0092 direction: current is the churn winner — the
    pick is unchanged (churn-ordering must not bias against current)."""
    orch = _orch(tmp_path)
    unit = _unit(_ZENODO_BASE, _ZENODO_REPLAYED, _ZENODO_CURRENT)
    outcome = orch._empty_fast_fail_recovery(unit, _failed_candidate(unit))
    assert outcome is not None
    assert outcome.accepted.provenance == "deterministic_source_current_only"


def test_churn_failure_degrades_to_fixed_order(tmp_path: Path, monkeypatch):
    """A broken churn computation must not crash the fallback — it
    degrades to the old fixed order and still lands a passing side."""
    def _boom(*a, **kw):
        raise RuntimeError("simulated churn failure")
    monkeypatch.setattr("capybase.merge_intent.side_churn", _boom)
    orch = _orch(tmp_path)
    unit = _unit(_ZENODO_BASE, _ZENODO_CURRENT, _ZENODO_REPLAYED)
    outcome = orch._empty_fast_fail_recovery(unit, _failed_candidate(unit))
    assert outcome is not None
    assert outcome.accepted.provenance == "deterministic_source_current_only", (
        "degraded ordering must reproduce the pre-fix behavior exactly")

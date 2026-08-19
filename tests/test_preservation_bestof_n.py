"""Churn-aware preservation heuristic + Best-of-N recovery — sprint-19 P2.

tokio-0037: the model's first candidate was oracle-correct (current
verbatim) and validation-passing; the preservation_heuristic's forced
retries degraded into syntax errors and the case escalated. Two fixes
(tested here, both from the synthesized reviewer plan):

1. Churn-aware heuristic — when the loser side's only unaccounted churn
   is a PURE DELETION of base content, the verbatim copy passes
   (validation.preservation_deletion_carveout).
2. Best-of-N recovery — when the heuristic still fires and every forced
   retry validates strictly worse, the rejected candidate is restored
   instead of escalating, tagged flagged_by_preservation_heuristic.

No network; engines and verifications are fakes/stubs.
"""

from __future__ import annotations

from types import SimpleNamespace

from capybase.conflict_model import CandidateResolution, ConflictSide, ConflictUnit
from capybase.orchestrator import Orchestrator
from capybase.verification import (
    PreservationHeuristicValidator,
    VerificationContext,
)


# ---------------------------------------------------------------------------
# Fixtures — the 0037 shape: replayed's only churn is deleting base content
# ---------------------------------------------------------------------------

_BASE = (
    "fn a() {}\n"
    "fn b() {}\n"
    "fn dead() {}\n"
    "fn c() {}\n"
)
_CUR = (
    "fn a() {}\n"
    "fn b2() {}\n"
    "fn dead() {}\n"
    "fn c() {}\n"
)
_REP = (
    "fn a() {}\n"
    "fn b() {}\n"
    "fn c() {}\n"
)
# replayed ADDS functionality instead of only deleting — the heuristic
# must keep firing (the sea-orm-0027 defense). The added line's anchor
# and structural suffix must differ from every candidate line, or the
# change-accounting classifies it as an exclusive/rename choice.
_REP_ADDS = (
    "fn a() {}\n"
    "fn b() {}\n"
    "fn dead() {}\n"
    "fn c() {}\n"
    "struct Widget;\n"
)


def _unit(cur: str = _CUR, rep: str = _REP,
          base: str = _BASE) -> ConflictUnit:
    return ConflictUnit(
        session_id="s", step_index=1, path="f.rs", language="rust",
        unit_id="f.rs:0:0", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=cur),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=rep),
        original_worktree_text=base,
        marker_span=(0, 1),
    )


def _ctx(candidate_text: str, *, cur: str = _CUR, rep: str = _REP,
         base: str = _BASE, carveout: bool = True) -> VerificationContext:
    return VerificationContext(
        unit=_unit(cur, rep, base),
        candidate=CandidateResolution(
            candidate_id="c1", unit_id="f.rs:0:0", model_name="test",
            prompt_version="t", resolved_text=candidate_text,
        ),
        config=SimpleNamespace(
            preservation_deletion_carveout=carveout,
            reject_if_copies_one_side=True,
        ),
    )


# ---------------------------------------------------------------------------
# Churn-aware carve-out — PreservationHeuristicValidator
# ---------------------------------------------------------------------------

def test_pure_deletion_loser_churn_passes():
    res = PreservationHeuristicValidator().verify(_ctx(_CUR))
    assert res.passed is True
    assert res.features["preservation_result"] == "deletion_superseded"
    assert res.features["copied_one_side"] is True
    assert res.detail["deletion_lines"] == ["fn dead() {}"]


def test_mirror_pure_deletion_also_passes():
    # candidate copies REPLAYED; current's only churn deleted base content
    base_m = "fn a(){}\nfn gone(){}\nfn c(){}\n"
    cur_m = "fn a(){}\nfn c(){}\n"                # current only deleted gone()
    rep_m = "fn a(){}\nfn gone(){}\nfn c2(){}\n"  # replayed modified c
    res = PreservationHeuristicValidator().verify(
        _ctx(rep_m, cur=cur_m, rep=rep_m, base=base_m))
    assert res.passed is True
    assert res.features["preservation_result"] == "deletion_superseded"
    assert res.features["copied_replayed_side"] is True


def test_loser_additions_still_fire():
    res = PreservationHeuristicValidator().verify(_ctx(_CUR, rep=_REP_ADDS))
    assert res.passed is False
    assert "REPLAYED has unaccounted changes" in res.message


def test_carveout_disabled_still_fires():
    res = PreservationHeuristicValidator().verify(
        _ctx(_CUR, carveout=False))
    assert res.passed is False
    assert res.detail["conflict_type"] == "deletion"


# ---------------------------------------------------------------------------
# Best-of-N recovery — the _resolve_unit wrapper
# ---------------------------------------------------------------------------

class _RecJournal:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, payload, **_kw):
        self.events.append((event, payload))


def _stub_orch(*, enabled: bool = True) -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch.journal = _RecJournal()
    orch.step = 1
    orch.config = SimpleNamespace(
        future=SimpleNamespace(enable_preservation_bestof_n=enabled))
    orch._recorded: list[dict] = []
    orch._record_resolution_attempt = (
        lambda outcome, *, mechanism, candidate, validation, decision,
        reason: orch._recorded.append({
            "mechanism": mechanism, "decision": decision}))
    return orch


def _stash(cand, validation, later: list[bool]) -> dict:
    return {"candidate": cand, "validation": validation,
            "later_attempts": later}


def _core_outcome(unit, *, accepted=None):
    out = SimpleNamespace(
        unit=unit, accepted=accepted, validation=None,
        escalated=accepted is None, reason="core reason",
        retry_count=2, attempts=[])
    return out


def _cand(text: str = _CUR) -> CandidateResolution:
    return CandidateResolution(
        candidate_id="c1", unit_id="f.rs:0:0", model_name="test",
        prompt_version="t", resolved_text=text)


def test_bestof_n_restores_when_all_retries_worse():
    orch = _stub_orch()
    unit = _unit()
    cand = _cand()
    val = SimpleNamespace(passed=True, hard_failures=[], warnings=[])
    stash = _stash(cand, val, later=[False, False])
    orch._step_preservation_stash = {unit.unit_id: stash}
    # core escalated: retries produced syntax errors (never passed)
    orch._resolve_unit_core = (
        lambda u, *, seed_failures=None, seed_candidate=None,
        wall_deadline=None, max_retries=None: _core_outcome(u))
    out = orch._resolve_unit(unit)
    assert out.accepted is cand
    assert out.escalated is False
    assert "best-of-N recovery" in out.reason
    assert cand.flagged_by_preservation_heuristic is True
    acc = [p for e, p in orch.journal.events if e == "candidate_accepted"]
    assert acc and acc[0]["via"] == "preservation_bestof_n_recovery"
    assert acc[0]["strictly_worse_retries"] == 2
    assert orch._recorded[0]["decision"] == "accept"
    assert unit.unit_id not in orch._step_preservation_stash


def test_bestof_n_blocked_when_a_retry_passed():
    # a retry that PASSED validation is equal-or-better: restoring would
    # preempt the core loop's own acceptance path
    orch = _stub_orch()
    unit = _unit()
    orch._step_preservation_stash = {
        unit.unit_id: _stash(_cand(), SimpleNamespace(passed=True), [False, True])}
    orch._resolve_unit_core = (
        lambda u, *, seed_failures=None, seed_candidate=None,
        wall_deadline=None, max_retries=None: _core_outcome(u))
    out = orch._resolve_unit(unit)
    assert out.accepted is None  # core outcome untouched


def test_bestof_n_blocked_when_no_retry_ran():
    orch = _stub_orch()
    unit = _unit()
    orch._step_preservation_stash = {
        unit.unit_id: _stash(_cand(), SimpleNamespace(passed=True), [])}
    orch._resolve_unit_core = (
        lambda u, *, seed_failures=None, seed_candidate=None,
        wall_deadline=None, max_retries=None: _core_outcome(u))
    assert orch._resolve_unit(unit).accepted is None


def test_bestof_n_skipped_when_accepted():
    orch = _stub_orch()
    unit = _unit()
    winner = _cand("fn woven(){}")
    orch._step_preservation_stash = {
        unit.unit_id: _stash(_cand(), SimpleNamespace(passed=True), [False])}
    orch._resolve_unit_core = (
        lambda u, *, seed_failures=None, seed_candidate=None,
        wall_deadline=None, max_retries=None: _core_outcome(u, accepted=winner))
    out = orch._resolve_unit(unit)
    assert out.accepted is winner


def test_bestof_n_flag_disabled():
    orch = _stub_orch(enabled=False)
    unit = _unit()
    orch._step_preservation_stash = {
        unit.unit_id: _stash(_cand(), SimpleNamespace(passed=True), [False])}
    orch._resolve_unit_core = (
        lambda u, *, seed_failures=None, seed_candidate=None,
        wall_deadline=None, max_retries=None: _core_outcome(u))
    assert orch._resolve_unit(unit).accepted is None


def test_candidate_flag_defaults_false():
    c = _cand()
    assert c.flagged_by_preservation_heuristic is False

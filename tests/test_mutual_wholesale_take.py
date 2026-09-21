"""S28-160 — mutual-wholesale take telemetry (journal-only).

duckdb-0014's shape: BOTH sides rewrote the whole file (churn 209 vs 211
on a 210-line base) and the deterministic replayed-only take landed at
WORKING while the current side was oracle-perfect. The replayed
tie-break is a measured coin flip there; `mutual_wholesale_take` counts
every source-only take landing on the shape so the harvest can measure
tie-break accuracy. Zero behavior change.
"""

from __future__ import annotations

from types import SimpleNamespace

from capybase.conflict_model import (
    CandidateResolution,
    ConflictSide,
    ConflictUnit,
)
from capybase.orchestrator import Orchestrator


class _RecJournal:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, payload, **_kw):
        self.events.append((event, payload))


def _orch():
    orch = object.__new__(Orchestrator)
    orch.journal = _RecJournal()
    orch.step = 1
    return orch


def _unit(base, cur, rep):
    return ConflictUnit(
        session_id="s", step_index=1, path="identifier.hpp",
        language="cpp", unit_id="f:1:0", unit_kind="whole_file",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=cur),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=rep),
        original_worktree_text=cur, marker_span=None,
    )


def _cand(unit, side):
    return CandidateResolution(
        candidate_id=f"{unit.unit_id}:take", unit_id=unit.unit_id,
        model_name="deterministic", resolved_text="",
        prompt_version="v",
        provenance=f"deterministic_source_{side}_only_stage",
    )


_BASE = "\n".join(f"int fn{i}(void) {{ return {i}; }}" for i in range(40))
_CUR = "\n".join(f"int cur{i}(void) {{ return {i}; }}" for i in range(42))
_REP = "\n".join(f"int rep{i}(void) {{ return {i}; }}" for i in range(41))


def test_mutual_wholesale_take_journaled():
    orch = _orch()
    u = _unit(_BASE, _CUR, _REP)
    orch._maybe_journal_mutual_wholesale(u, _cand(u, "replayed"))
    ev = [p for e, p in orch.journal.events
          if e == "mutual_wholesale_take"]
    assert ev, "the mutual-rewrite take must be counted"
    assert ev[0]["side"] == "replayed"
    assert ev[0]["base_lines"] == 40
    assert ev[0]["churn_current"] >= 0.8 * 40
    assert ev[0]["churn_replayed"] >= 0.8 * 40


def test_no_event_for_partial_rewrites():
    """The ordinary shape (small churns) is not mutual-wholesale — the
    event must stay silent so the census counts only real coin flips."""
    orch = _orch()
    cur = _BASE.replace("return 3;", "return 30;")
    rep = _BASE.replace("return 5;", "return 50;")
    u = _unit(_BASE, cur, rep)
    orch._maybe_journal_mutual_wholesale(u, _cand(u, "replayed"))
    assert not [e for e, _ in orch.journal.events
                if e == "mutual_wholesale_take"]


def test_no_event_for_non_source_provenance():
    orch = _orch()
    u = _unit(_BASE, _CUR, _REP)
    cand = CandidateResolution(
        candidate_id="x", unit_id=u.unit_id, model_name="plain_llm",
        resolved_text="", prompt_version="v", provenance="plain_llm")
    orch._maybe_journal_mutual_wholesale(u, cand)
    assert orch.journal.events == []

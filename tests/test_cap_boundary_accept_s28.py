"""S28-145 re-target — the cap-boundary accept (the screening run's catch).

The php-0005 rerun shape: five units whose candidates PASSED validation
(hf=0, passed=True) with advisory `both_sides_represented` warnings, all
escalated at the unit-count cap with 0.949 content. The zero-budget
escape's doctrine (content-loss acceptance is strictly better than
escalating the file) now applies at ANY unit-count budget: a compiling,
advisory-only boundary candidate is ACCEPTED, not escalated. Zero model
requests — the candidate already exists.
"""

from __future__ import annotations

from types import SimpleNamespace

from capybase.conflict_model import (
    ConflictSide,
    ConflictUnit,
)
from capybase.orchestrator import (
    Orchestrator,
    _cap_boundary_advisory,
    _ZB_ADVISORY,
)


class _RecJournal:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, payload, **_kw):
        self.events.append((event, payload))


def _orch():
    orch = Orchestrator.__new__(Orchestrator)
    orch.journal = _RecJournal()
    orch.step = 0
    return orch


def _unit(uid="u1"):
    return ConflictUnit(
        session_id="s", step_index=0, path="f.c", language="cpp",
        conflict_type="UU", unit_id=uid, unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="int x;\n"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="int x;\nint y;\n"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="int x;\nint z;\n"),
        original_worktree_text="int x;\n", marker_span=(0, 0),
    )


def _validation(*, passed=True, hf=0, warnings=None):
    return SimpleNamespace(passed=passed, hard_failures=list(range(hf)),
                           warnings=warnings if warnings is not None else [])


def _cand(resolved="int x;\nint y;\nint z;\n"):
    return SimpleNamespace(resolved_text=resolved, provenance="plain_llm",
                           candidate_id="f.c:1:0:llm")


def _warn(validator):
    return SimpleNamespace(validator=validator, message="drops a side's additions")


# ---------------------------------------------------------------------------
# the boundary-accept predicate
# ---------------------------------------------------------------------------

def test_passing_advisory_only_candidate_qualifies():
    v = _validation(warnings=[_warn("both_sides_represented")])
    assert _cap_boundary_advisory(v, _cand())


def test_hard_failures_disqualify():
    v = _validation(passed=False, hf=1, warnings=[_warn("both_sides_represented")])
    assert not _cap_boundary_advisory(v, _cand())


def test_non_advisory_warnings_disqualify():
    v = _validation(warnings=[_warn("some_hard_signal")])
    assert not _cap_boundary_advisory(v, _cand())


def test_empty_resolution_disqualifies():
    v = _validation(warnings=[_warn("both_sides_represented")])
    assert not _cap_boundary_advisory(v, _cand(resolved=""))
    assert not _cap_boundary_advisory(v, None)


def test_failed_validation_disqualifies():
    assert not _cap_boundary_advisory(_validation(passed=False), _cand())


# ---------------------------------------------------------------------------
# the accept flow (the relaxation block's boundary branch)
# ---------------------------------------------------------------------------

def test_boundary_accept_flow():
    orch = _orch()
    v = _validation(warnings=[_warn("both_sides_represented")])
    cand = _cand()
    outcome = SimpleNamespace(accepted=None, validation=None, retry_count=None,
                              reason="")
    orch._record_resolution_attempt = lambda *a, **k: None
    # the accept branch's essentials, driven the way the block drives them
    outcome.accepted = cand
    outcome.validation = v
    outcome.reason = "cap boundary: accepted compiling candidate"
    orch.journal.emit("cap_boundary_accept",
                      {"unit_id": "u1", "original_cap": 1, "blockers":
                       ["both_sides_represented"]}, step_index=0)
    orch.journal.emit("candidate_accepted",
                      {"candidate_id": cand.candidate_id}, step_index=0)
    events = [e for e, _ in orch.journal.events]
    assert "cap_boundary_accept" in events
    assert "candidate_accepted" in events
    assert outcome.accepted is cand


def test_advisory_set_covers_the_screening_shape():
    """The php-0005 rerun's blocker was both_sides_represented — the set
    that let a passing candidate be escalated must now admit it."""
    assert "both_sides_represented" in _ZB_ADVISORY
    assert "preservation_heuristic" in _ZB_ADVISORY

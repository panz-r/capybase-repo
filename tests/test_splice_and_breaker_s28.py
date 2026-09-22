"""S28-173(1) + S28-174 — the splice's preservation net (telemetry) and
the comment phase's transport-weather breaker.

S28-173: the deterministic accept path bypasses every rescue arm, so a
splice that dropped the loser side's changes shipped silently
(sqlite-0109: NEAR_MATCH 0.875 at loser_preservation 0.31). The
telemetry flags accepted units whose loser-side preservation falls
under the WORKING judge's bar; side-take provenances are excluded (a
wholesale take IS one side — low loser preservation by design).

S28-174: two consecutive comment passes with RAISED model calls
(transport errors — not content failures) latch the comment phase off
for the rest of the session (the 168-failure census: endpoint weather
where every unit independently paid a dead call). Model-REDUCING;
comments are decoration, so code outcomes are untouched.
"""

from __future__ import annotations

from types import SimpleNamespace

from capybase.conflict_model import (
    ConflictSide,
    ConflictUnit,
)
from capybase.orchestrator import Orchestrator, _SPLICE_LOSER_DROP_BAR


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


def _unit(base, current, replayed, uid="u1"):
    return ConflictUnit(
        session_id="s", step_index=0, path="f.txt", language="text",
        conflict_type="UU", unit_id=uid, unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text=base, marker_span=(0, 0),
    )


# ---------------------------------------------------------------------------
# S28-173(1): the splice loser-dropped telemetry
# ---------------------------------------------------------------------------

BASE = "a\nb\nc\n"
CURRENT = "a\nB2\nc\nNEWCUR\n"     # churn 3: b removed, B2 + NEWCUR added
REPLAYED = "a\nb\nc\nNEWREP\n"     # churn 1: NEWREP added — the LOSER side


def test_splice_dropping_loser_is_flagged():
    orch = _orch()
    unit = _unit(BASE, CURRENT, REPLAYED)
    cand = SimpleNamespace(
        resolved_text=CURRENT,  # NEWREP gone — the loser's work is dropped
        provenance="structural", candidate_id="f.txt:1:0:structural")
    out = orch._splice_loser_dropped([(unit, cand)])
    assert out is not None
    assert out["units"] == ["u1"] and out["dropped"] == 1
    assert out["bar"] == _SPLICE_LOSER_DROP_BAR == 0.5


def test_splice_preserving_loser_is_clean():
    orch = _orch()
    unit = _unit(BASE, CURRENT, REPLAYED)
    cand = SimpleNamespace(
        resolved_text=CURRENT + "NEWREP\n",  # both sides' work survives
        provenance="structural", candidate_id="f.txt:1:0:structural")
    assert orch._splice_loser_dropped([(unit, cand)]) is None


def test_side_take_provenance_is_excluded():
    """A wholesale take IS one side — its low loser preservation is the
    documented tradeoff, not a silent splice defect."""
    orch = _orch()
    unit = _unit(BASE, CURRENT, REPLAYED)
    cand = SimpleNamespace(
        resolved_text=CURRENT,
        provenance="side_take_over", candidate_id="f.txt:side_take:replayed")
    assert orch._splice_loser_dropped([(unit, cand)]) is None


def test_empty_accepted_and_unresolved_candidates_are_skipped():
    orch = _orch()
    unit = _unit(BASE, CURRENT, REPLAYED)
    assert orch._splice_loser_dropped([]) is None
    cand = SimpleNamespace(resolved_text=None, provenance="structural",
                           candidate_id="f:1:0:structural")
    assert orch._splice_loser_dropped([(unit, cand)]) is None


# ---------------------------------------------------------------------------
# S28-174: the transport-weather breaker
# ---------------------------------------------------------------------------

def _pass(*, failed=False):
    return [("comment_model_call_failed", {})] if failed else []


def test_second_consecutive_transport_failure_latches():
    orch = _orch()
    assert orch._note_comment_transport(_pass(failed=True)) is False
    assert orch._note_comment_transport(_pass(failed=True)) is True
    assert orch._comment_phase_latched is True


def test_latching_pass_only_returns_true_once():
    orch = _orch()
    orch._note_comment_transport(_pass(failed=True))
    orch._note_comment_transport(_pass(failed=True))
    # later passes are already-skipped by the entry check; the note itself
    # no longer re-reports the latch
    assert orch._note_comment_transport(_pass(failed=True)) is False


def test_healthy_pass_resets_the_streak():
    orch = _orch()
    assert orch._note_comment_transport(_pass(failed=True)) is False
    assert orch._note_comment_transport(_pass()) is False  # endpoint works
    assert orch._comment_transport_failures == 0
    assert orch._note_comment_transport(_pass(failed=True)) is False


def test_content_failures_do_not_feed_the_breaker():
    """Content failures own the phase's budget loop — only RAISED calls
    are transport weather."""
    orch = _orch()
    assert orch._note_comment_transport(
        [("comment_reconciliation_failed", {})]) is False
    assert orch._comment_transport_failures == 0


def test_latched_session_skips_the_phase_at_entry():
    orch = _orch()
    orch._comment_phase_latched = True
    out = orch._run_comment_pass(
        "a.py", "x = 1\n", [], "x = 1\n", [], "python")
    assert out is None  # skipped before any model surface
    assert any(e == "comment_phase_skipped" for e, _ in orch.journal.events)

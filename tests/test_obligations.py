"""Tests for the side-obligation contract (#3).

Two pure pieces + the validator + prompt integration:
- :func:`extract_obligations` derives per-side added/changed/removed line content
  (the replace-opcode gap no prior helper covered — both old and new lines).
- :func:`obligations_satisfied` checks a candidate carries them: an added line
  must appear; a changed line must NOT be reverted to base (a synthesis of two
  same-line edits is correct); a removed line is honored, not required.
- :class:`ObligationValidator` wires the check into Phase A.
- :func:`_side_intent_block` renders the contract into the resolve prompt.

These target the failure modes the token-set/verbatim heuristics miss: a dropped
*modification* of an existing line (no new distinctive token) and a reverted edit.
"""

from __future__ import annotations

from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.obligations import (
    extract_obligations,
    obligations_satisfied,
    render_obligation_block,
)


def _unit(base: str, current: str, replayed: str) -> ConflictUnit:
    return ConflictUnit(
        session_id="s", step_index=1, path="cfg.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text=base, marker_span=(0, 0),
    )


# ---------------------------------------------------------------------------
# extract_obligations
# ---------------------------------------------------------------------------


def test_extract_added_lines():
    """A pure addition is captured as ``added`` (line content, not indices)."""
    ob = extract_obligations(_unit("a = 1", "a = 1\nb = 2", "a = 1\nc = 3"))
    assert ob.current.added == ["b = 2"]
    assert ob.replayed.added == ["c = 3"]
    assert ob.current.changed == [] and ob.current.removed == []


def test_extract_changed_lines_capture_both_old_and_new():
    """A replace is captured as ``changed`` with BOTH the old base line and the
    new side line — the gap no prior helper covered."""
    ob = extract_obligations(_unit("port = 8080", "port = 9090", "port = 7070"))
    assert ob.current.changed == [("port = 8080", "port = 9090")]
    assert ob.replayed.changed == [("port = 8080", "port = 7070")]
    assert ob.current.added == [] and ob.current.removed == []


def test_extract_removed_lines():
    """A clean deletion is captured as ``removed`` (base line content)."""
    ob = extract_obligations(_unit("a = 1\nb = 2", "a = 1", "a = 1\nb = 2"))
    assert ob.current.removed == ["b = 2"]
    assert ob.replayed.empty


def test_extract_unchanged_side_is_empty():
    """A side that conceded (== base) imposes no obligation."""
    ob = extract_obligations(_unit("x = 1", "x = 2", "x = 1"))
    assert ob.replayed.empty
    assert ob.current.changed == [("x = 1", "x = 2")]


def test_extract_length_changing_replace_routes_tail_correctly():
    """A replace where the side is longer/shorter than base routes the unpaired
    tail to added/removed so no line is lost.

    Note difflib aligns the matching prefix: ``a = 1`` (kept) + ``b = 2, c = 3``
    (inserted), so this shows as added — not a replace. A genuine length-changing
    REPLACE (where the first line ALSO changes) routes the tail correctly."""
    # base 2 lines, side 3 lines, first line changed: one replace pair + one added.
    ob = extract_obligations(_unit("a = 1\nb = 2", "A = 1\nb = 2\nc = 3", "a = 1\nb = 2"))
    assert ob.current.changed == [("a = 1", "A = 1")]
    assert ob.current.added == ["c = 3"]


# ---------------------------------------------------------------------------
# obligations_satisfied
# ---------------------------------------------------------------------------


def test_synthesis_of_two_same_line_edits_is_satisfied():
    """Both sides changed the SAME base line; the correct resolution is a
    synthesis of both edits (neither side's exact line). It must be SATISFIED —
    requiring either side's exact line would wrongly flag a valid merge."""
    ob = extract_obligations(_unit(
        'S = ["core"]',
        'S = ["core", "scheduler"]',
        'S = ["core", "reloader"]',
    ))
    ok, dropped = obligations_satisfied(ob, 'S = ["core", "scheduler", "reloader"]')
    assert ok, dropped


def test_reverted_edit_is_flagged():
    """A resolution that kept the OLD base line where a side edited is flagged —
    the silent-undo case the token-set heuristics miss."""
    ob = extract_obligations(_unit("port = 8080", "port = 9090", "port = 9090"))
    ok, dropped = obligations_satisfied(ob, "port = 8080")  # reverted to base
    assert not ok
    assert any("reverted to base" in d for d in dropped)


def test_dropped_added_line_is_flagged():
    """A side's added line missing from the resolution is flagged."""
    ob = extract_obligations(_unit("a = 1", "a = 1\nb = 2", "a = 1\nc = 3"))
    ok, dropped = obligations_satisfied(ob, "a = 1\nc = 3")  # dropped b = 2
    assert not ok
    assert any("b = 2" in d for d in dropped)


def test_deliberate_deletion_is_honored_not_required():
    """A side's removed obligation is HONORED — the resolution need not keep the
    deleted line (flagging it would conflict with the modify/delete machinery)."""
    ob = extract_obligations(_unit("a = 1\nb = 2", "a = 1", "a = 1\nb = 2\nc = 3"))
    # current deleted b=2; resolution omits it → satisfied (deletion honored).
    ok, _ = obligations_satisfied(ob, "a = 1\nc = 3")
    assert ok


def test_both_added_distinct_lines_satisfied_when_both_present():
    ob = extract_obligations(_unit("a = 1", "a = 1\nb = 2", "a = 1\nc = 3"))
    ok, _ = obligations_satisfied(ob, "a = 1\nb = 2\nc = 3")
    assert ok


# ---------------------------------------------------------------------------
# render_obligation_block (prompt integration)
# ---------------------------------------------------------------------------


def test_render_block_lists_both_sides_obligations():
    ob = extract_obligations(_unit(
        'S = ["core"]', 'S = ["core", "scheduler"]', 'S = ["core", "reloader"]'
    ))
    block = render_obligation_block(ob)
    assert "CURRENT_UPSTREAM_SIDE must preserve:" in block
    assert "REPLAYED_COMMIT_SIDE must preserve:" in block
    assert "scheduler" in block and "reloader" in block


def test_render_block_empty_for_unchanged_conflict():
    """Both sides unchanged → no obligations → empty block (caller omits it)."""
    ob = extract_obligations(_unit("x = 1", "x = 1", "x = 1"))
    assert render_obligation_block(ob) == ""


# ---------------------------------------------------------------------------
# ObligationValidator (Phase A integration)
# ---------------------------------------------------------------------------


def _ctx(unit: ConflictUnit, resolved: str):
    from capybase.conflict_model import CandidateResolution
    from capybase.verification import VerificationContext, ValidationConfig

    cand = CandidateResolution(
        candidate_id="t", unit_id=unit.unit_id, model_name="test",
        prompt_version="v", resolved_text=resolved,
    )
    return VerificationContext(unit=unit, candidate=cand, config=ValidationConfig())


def test_validator_flags_reverted_edit():
    from capybase.verification import ObligationValidator

    unit = _unit("port = 8080", "port = 9090", "port = 9090")
    res = ObligationValidator().verify(_ctx(unit, "port = 8080"))
    assert not res.passed
    assert res.severity == "warning"
    assert res.features["dropped_obligation"] is True


def test_validator_passes_synthesis():
    from capybase.verification import ObligationValidator

    unit = _unit('S = ["core"]', 'S = ["core", "scheduler"]', 'S = ["core", "reloader"]')
    res = ObligationValidator().verify(_ctx(unit, 'S = ["core", "scheduler", "reloader"]'))
    assert res.passed
    assert res.features["dropped_obligation"] is False


def test_validator_no_op_on_unchanged_conflict():
    """Both sides unchanged → no obligations → the validator passes cleanly and
    records that it didn't check."""
    from capybase.verification import ObligationValidator

    unit = _unit("x = 1", "x = 1", "x = 1")
    res = ObligationValidator().verify(_ctx(unit, "x = 1"))
    assert res.passed
    assert res.features["obligation_checked"] is False


def test_prompt_side_intent_block_includes_obligations():
    """The resolve prompt's side-intent block now carries the obligations
    contract (not just the conflict-shape label)."""
    from capybase.resolution_engine import _side_intent_block

    unit = _unit('S = ["core"]', 'S = ["core", "scheduler"]', 'S = ["core", "reloader"]')
    block = _side_intent_block(unit)
    assert "must preserve" in block
    assert "scheduler" in block and "reloader" in block

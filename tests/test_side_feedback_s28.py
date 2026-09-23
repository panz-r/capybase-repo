"""S28-180 — side-consistent repair feedback (pilot-gated, default OFF).

The five semantic side-mismatch rows (0137/0113/0052/0129/0039) failed
because the file mixed the two sides' API conventions while the repair
feedback carried only the bare gcc error. The enrichment attaches BOTH
sides' declaration of the failing symbol — same repair rounds, sharper
evidence per round (the anti-S28-74: information, not budget). Contract:
zero new model requests.
"""

from __future__ import annotations

from types import SimpleNamespace

from capybase.conflict_model import (
    ConflictSide,
    ConflictUnit,
    VerificationFailure,
)
import capybase.resolution_engine as re_mod


def _unit():
    return ConflictUnit(
        session_id="s", step_index=0, path="f.cpp", language="cpp",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="struct T {};\n"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE",
                             text="struct T {};\nT* make();\n"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE",
                              text="struct T {};\nT& make();\n"),
        original_worktree_text="struct T {};\n", marker_span=(0, 0),
    )


def _fail(msg):
    return VerificationFailure(validator="syntax", message=msg)


def test_divergent_symbol_gets_both_variants():
    re_mod.set_side_consistent_feedback(True)
    try:
        note = re_mod._side_convention_note(
            _unit(),
            [_fail("no matching function for call to 'make()'")])
    finally:
        re_mod.set_side_consistent_feedback(False)
    assert note is not None and "side convention note" in note
    assert "CURRENT side:" in note and "T* make();" in note
    assert "REPLAYED side:" in note and "T& make();" in note


def test_toggle_off_is_a_noop():
    re_mod.set_side_consistent_feedback(False)
    assert re_mod._side_convention_note(
        _unit(),
        [_fail("no matching function for call to 'make()'")]) == ""


def test_identical_declarations_no_note():
    """When both sides declare the symbol the same way there is no
    convention conflict — the note must stay silent."""
    re_mod.set_side_consistent_feedback(True)
    try:
        unit = _unit()
        unit.current = ConflictSide(label="CURRENT_UPSTREAM_SIDE",
                                    text="struct T {};\nT& make();\n")
        unit.replayed = ConflictSide(label="REPLAYED_COMMIT_SIDE",
                                     text="struct T {};\nT& make();\n")
        assert re_mod._side_convention_note(
            unit, [_fail("no matching function for call to 'make()'")]) == ""
    finally:
        re_mod.set_side_consistent_feedback(False)


def test_no_matching_symbol_no_note():
    re_mod.set_side_consistent_feedback(True)
    try:
        assert re_mod._side_convention_note(
            _unit(), [_fail("unterminated triple-quoted string")]) == ""
    finally:
        re_mod.set_side_consistent_feedback(False)


def test_member_shape_is_recognized():
    re_mod.set_side_consistent_feedback(True)
    try:
        note = re_mod._side_convention_note(
            _unit(), [_fail("no member named 'make' in 'struct T'")])
    finally:
        re_mod.set_side_consistent_feedback(False)
    assert note is not None and "make" in note


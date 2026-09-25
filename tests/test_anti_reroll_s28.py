"""S28-247.2 (queue item 11) — the anti-reroll line (pilot-gated, OFF).

Trial15's waste census: 13 draws (~19% of prompts) were
byte-identical resubmissions the C7 fast-fail caught only AFTER the
tokens were spent. On such a round the repair prompt now carries an
explicit change-the-approach instruction. Feedback-only — same repair
rounds, zero new model requests (the S28-180 contract).
"""

from __future__ import annotations

import capybase.resolution_engine as re_mod
from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.context_builder import ContextBuilder
from capybase.resolution_engine import (
    _anti_reroll_note,
    _anti_reroll_repeats,
    build_repair_prompt,
)
from capybase.verification import VerificationFailure


def _unit():
    worktree = "def f():\n<<<<<<< H\n    return 0\n=======\n    return 9\n>>>>>>> b\n"
    return ConflictUnit(
        session_id="s", step_index=1, path="f.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="def f():\n    pass"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="    return 0"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="    return 9"),
        original_worktree_text=worktree, marker_span=(1, 5),
    )


def _fail(msg: str) -> VerificationFailure:
    return VerificationFailure(validator="test", severity="error", message=msg)


# ---------------------------------------------------------------------------
# the repeats detector (engine-side identity, pre-append semantics)
# ---------------------------------------------------------------------------

def test_repeats_decline_on_empty_or_first_attempt():
    # empty/absent text never repeats
    assert _anti_reroll_repeats("", ["a", "b"]) == 0
    # a first attempt: the prior list does not contain it
    assert _anti_reroll_repeats("t2", ["t0", "t1"]) == 0
    # a reroll: the failed candidate byte-identically repeats an
    # earlier attempt
    assert _anti_reroll_repeats("t1", ["t0", "t1"]) == 1


# ---------------------------------------------------------------------------
# the note, flag-gated
# ---------------------------------------------------------------------------

def test_note_off_by_default():
    re_mod._ANTI_REROLL_ENABLED = False
    try:
        assert _anti_reroll_note(1) == ""
        re_mod._ANTI_REROLL_ENABLED = True
        assert _anti_reroll_note(0) == ""
        note = _anti_reroll_note(1)
        assert "ANTI-REROLL" in note
        assert "byte-identical" in note
        assert "Change the APPROACH" in note
    finally:
        re_mod._ANTI_REROLL_ENABLED = False


# ---------------------------------------------------------------------------
# the carrier: the note rides the repair prompt
# ---------------------------------------------------------------------------

def test_repair_prompt_carries_the_note_when_on():
    re_mod._ANTI_REROLL_ENABLED = True
    try:
        prompt = build_repair_prompt(
            _unit(), ContextBuilder().build(_unit()),
            _candidate_with_text("    return [0, 9]"),
            [_fail("preservation: dropped CURRENT side")],
            anti_reroll_repeats=1)
        assert "ANTI-REROLL" in prompt
    finally:
        re_mod._ANTI_REROLL_ENABLED = False


def test_repair_prompt_clean_when_off_or_not_a_reroll():
    re_mod._ANTI_REROLL_ENABLED = False
    prompt = build_repair_prompt(
        _unit(), ContextBuilder().build(_unit()),
        _candidate_with_text("    return [0, 9]"),
        [_fail("preservation: dropped CURRENT side")],
        anti_reroll_repeats=1)
    assert "ANTI-REROLL" not in prompt
    re_mod._ANTI_REROLL_ENABLED = True
    try:
        prompt = build_repair_prompt(
            _unit(), ContextBuilder().build(_unit()),
            _candidate_with_text("    return [0, 9]"),
            [_fail("preservation: dropped CURRENT side")],
            anti_reroll_repeats=0)
        assert "ANTI-REROLL" not in prompt
    finally:
        re_mod._ANTI_REROLL_ENABLED = False


def _candidate_with_text(text):
    from capybase.conflict_model import CandidateResolution
    return CandidateResolution(
        candidate_id="c1", unit_id="u", model_name="m",
        prompt_version="resolve_text_block.v6", resolved_text=text,
        provenance="plain_llm", self_reported_confidence=0.9,
    )

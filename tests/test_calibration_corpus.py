"""Tests for the blessed calibration corpus.

Two invariants: (1) every conflict is a well-formed ConflictUnit that the
resolve prompt builder accepts; (2) every ``expected_text`` is genuinely
ACHIEVABLE — a candidate whose resolved_text equals it scores as correct under
the quality scorer. This guards against authoring an unresolvable corpus (the
failing property of the old x=1/2/3 probe).
"""

from __future__ import annotations

import pytest

from capybase.calibration_corpus import (
    CALIBRATION_CONFLICTS,
    conflicts_with_context,
)
from capybase.conflict_model import ConflictUnit, ContextBundle
from capybase.resolution_engine import build_resolve_prompt


def test_corpus_has_expected_shape():
    assert len(CALIBRATION_CONFLICTS) >= 5
    titles = [c.title for c in CALIBRATION_CONFLICTS]
    # The five intended shapes are present (breadth of conflict type).
    assert "list-combine" in titles
    assert "dict-combine" in titles
    assert "both-sides-add" in titles
    assert "indent-sensitive" in titles
    assert "text-combine" in titles


@pytest.mark.parametrize("conflict", CALIBRATION_CONFLICTS)
def test_each_conflict_is_well_formed(conflict):
    unit = conflict.unit
    assert isinstance(unit, ConflictUnit)
    # All three sides non-empty (a real conflict, not a degenerate one).
    assert unit.base.text and unit.current.text and unit.replayed.text
    # The two sides must actually diverge from base (else no conflict).
    assert unit.current.text != unit.base.text
    assert unit.replayed.text != unit.base.text
    assert conflict.expected_text  # non-empty blessed merge


@pytest.mark.parametrize("conflict", CALIBRATION_CONFLICTS)
def test_each_conflict_builds_a_resolve_prompt(conflict):
    # The real resolve prompt builder must accept it without raising.
    prompt = build_resolve_prompt(conflict.unit, ContextBundle(primary_text=""))
    assert "resolved_text" in prompt  # JSON contract present
    assert len(prompt) > 50


@pytest.mark.parametrize("conflict", CALIBRATION_CONFLICTS)
def test_expected_text_is_achievable(conflict):
    """The blessed merge must score as CORRECT when a candidate emits it.
    Guards against authoring an unresolvable corpus."""
    from capybase.conflict_model import CandidateResolution
    from capybase.quality import score_candidate

    cand = CandidateResolution(
        candidate_id="t", unit_id=conflict.unit.unit_id,
        model_name="test", prompt_version="resolve_text_block.v5",
        resolved_text=conflict.expected_text,
    )
    score = score_candidate(cand, conflict)
    assert score.correct, (
        f"blessed text for {conflict.title!r} did not score correct: "
        f"{score.detail}"
    )


def test_conflicts_with_context_pairs_resolve_prompt_ready():
    pairs = conflicts_with_context()
    assert len(pairs) == len(CALIBRATION_CONFLICTS)
    for conflict, context in pairs:
        assert isinstance(context, ContextBundle)
        # Resolving under the real engine needs a buildable prompt.
        build_resolve_prompt(conflict.unit, context)

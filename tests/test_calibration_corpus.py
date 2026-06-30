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
    assert len(CALIBRATION_CONFLICTS) >= 15
    titles = [c.title for c in CALIBRATION_CONFLICTS]
    # Breadth across conflict shapes (the original five + the expanded coverage).
    for t in (
        "list-combine", "dict-combine", "both-sides-add", "indent-sensitive",
        "text-combine",
        "rust-struct-fields", "rust-impl-methods",  # Rust syntax
        "multi-hunk", "import-combine", "distinct-functions",  # structural tension
        "same-line-pick",  # semantically-incompatible (must pick one)
        "modify-delete-keeper-wins",  # modify/delete shape
        "long-block-combine",  # long-context reproduction
        "rename-plus-add",  # rename + independent addition
    ):
        assert t in titles, f"missing corpus shape: {t}"


def test_corpus_covers_both_languages():
    """Python and Rust are both first-class; the corpus must exercise both."""
    langs = {c.unit.language for c in CALIBRATION_CONFLICTS}
    assert "python" in langs
    assert "rust" in langs


def test_corpus_at_or_above_mechanism_selection_floor():
    """The corpus must be large enough for probe_mechanisms to A/B-select
    (below the floor it refuses and leaves mechanisms off)."""
    from capybase.probes import _MIN_CORPUS_FOR_MECHANISM_SELECTION

    assert len(CALIBRATION_CONFLICTS) >= _MIN_CORPUS_FOR_MECHANISM_SELECTION


@pytest.mark.parametrize("conflict", CALIBRATION_CONFLICTS)
def test_each_conflict_is_well_formed(conflict):
    unit = conflict.unit
    assert isinstance(unit, ConflictUnit)
    # base must be non-empty (a real three-way conflict has shared ancestry).
    # A side may be empty ONLY for a modify/delete shape (one side deleted the
    # block); both sides empty would be degenerate.
    assert unit.base.text
    assert unit.current.text or unit.replayed.text
    # The two sides must actually diverge from base (else no conflict). For a
    # modify/delete the empty side trivially diverges; the keeper must too.
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

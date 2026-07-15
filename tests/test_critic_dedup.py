"""Critic-feedback deduplication.

The PoLL jury may emit multiple ``verifier_model*`` warnings for the SAME issue
under different wording — feeding both to the plan-first step dilutes the model's
attention. This test covers ``_dedupe_critic_warnings``: the embedding-similarity
merge of equivalent flags before the repair prompt is seeded.

A deterministic fake embedder (vectors from a caller-specified text→vector map)
makes the cosine thresholds assertable without a live endpoint.
"""

from __future__ import annotations

from capybase.conflict_model import VerificationWarning
from capybase.orchestrator import (
    _all_critic_warnings,
    _critic_cosine,
    _critic_warning_text,
    _dedupe_critic_warnings,
)
from capybase.conflict_model import VerificationResult


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _MapEmbedder:
    """Returns a caller-specified vector per text."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = 0

    def embed(self, texts):
        if isinstance(texts, str):
            texts = [texts]
        self.calls += 1
        return [self.mapping.get(t, [0.0, 0.0]) for t in texts]


def _w(validator, message, dropped_units=None):
    detail = {}
    if dropped_units:
        detail["dropped_units"] = list(dropped_units)
    return VerificationWarning(validator=validator, message=message, detail=detail)


# ---------------------------------------------------------------------------
# _critic_warning_text
# ---------------------------------------------------------------------------


def test_critic_warning_text_includes_dropped_units():
    w = _w("verifier_model", "drops replayed side", [("function", "validate_token")])
    assert "drops replayed side" in _critic_warning_text(w)
    assert "function validate_token" in _critic_warning_text(w)


def test_critic_warning_text_no_detail_safe():
    w = _w("verifier_model", "vague issue")
    assert _critic_warning_text(w) == "vague issue"


# ---------------------------------------------------------------------------
# _all_critic_warnings
# ---------------------------------------------------------------------------


def _validation(*warnings):
    return VerificationResult(
        candidate_id="c", unit_id="u", passed=True, warnings=list(warnings),
    )


def test_all_critic_warnings_collects_verifier_model_star():
    v = _validation(
        _w("verifier_model", "a"),
        _w("intent_coverage", "b"),  # not a critic
        _w("verifier_model_conflict", "c"),
        _w("both_sides_represented", "d"),  # not a critic
    )
    out = _all_critic_warnings(v)
    assert [w.message for w in out] == ["a", "c"]


# ---------------------------------------------------------------------------
# _dedupe_critic_warnings
# ---------------------------------------------------------------------------


def test_equivalent_flags_merged_to_one():
    """Two flags for the same issue (cosine ≥ 0.90) → keep one (more specific)."""
    w1 = _w("verifier_model", "may drop the replayed side intent")
    w2 = _w("verifier_model_conflict", "the replayed side's intent is dropped")
    # Map both texts to the same vector → cosine 1.0.
    emb = _MapEmbedder({
        _critic_warning_text(w1): [1.0, 0.0],
        _critic_warning_text(w2): [1.0, 0.0],
    })
    out = _dedupe_critic_warnings([w1, w2], emb)
    assert len(out) == 1


def test_equivalent_flags_keeps_more_specific():
    """When two flags are equivalent, the more specific (more dropped_units) wins."""
    w1 = _w("verifier_model", "drops a side")  # vague
    w2 = _w("verifier_model_conflict", "drops a side",
            [("function", "validate_token"), ("class", "Token")])  # specific
    emb = _MapEmbedder({
        _critic_warning_text(w1): [1.0, 0.0],
        _critic_warning_text(w2): [1.0, 0.0],  # cosine 1.0
    })
    out = _dedupe_critic_warnings([w1, w2], emb)
    assert len(out) == 1
    # The more specific one (w2, with dropped_units) survives.
    assert out[0].detail.get("dropped_units") is not None


def test_distinct_flags_both_kept():
    """Two flags for different issues (cosine < 0.60) → keep both."""
    w1 = _w("verifier_model", "drops the replayed side")
    w2 = _w("verifier_model_conflict", "introduces unattributed hallucinated code")
    # Orthogonal vectors → cosine 0.0.
    emb = _MapEmbedder({
        _critic_warning_text(w1): [1.0, 0.0],
        _critic_warning_text(w2): [0.0, 1.0],
    })
    out = _dedupe_critic_warnings([w1, w2], emb)
    assert len(out) == 2


def test_related_but_distinct_flags_both_kept_specificity_ordered():
    """Mid-band (0.60–0.90): keep both, order by specificity."""
    # cos([1,0,0], [0.8, 0.6, 0]) = 0.8
    w1 = _w("verifier_model", "drops a side")
    w2 = _w("verifier_model_conflict", "drops a side",
            [("function", "foo")])  # more specific
    emb = _MapEmbedder({
        _critic_warning_text(w1): [1.0, 0.0, 0.0],
        _critic_warning_text(w2): [0.8, 0.6, 0.0],
    })
    out = _dedupe_critic_warnings([w1, w2], emb)
    assert len(out) == 2
    # More specific (w2) ordered first.
    assert out[0].detail.get("dropped_units") is not None


def test_embedder_none_returns_input_unchanged():
    """No embedder → passthrough (the prior behavior, first-found only)."""
    w1 = _w("verifier_model", "a")
    w2 = _w("verifier_model_conflict", "b")
    out = _dedupe_critic_warnings([w1, w2], None)
    assert out == [w1, w2]


def test_single_warning_returns_unchanged():
    w = _w("verifier_model", "solo")
    out = _dedupe_critic_warnings([w], _MapEmbedder({}))
    assert out == [w]


def test_embed_failure_returns_input_unchanged():
    """A failing embedder → no dedup (best-effort, never raises)."""
    w1 = _w("verifier_model", "a")
    w2 = _w("verifier_model_conflict", "b")

    class _Boom:
        def embed(self, texts):
            raise RuntimeError("down")

    out = _dedupe_critic_warnings([w1, w2], _Boom())
    assert out == [w1, w2]


def test_three_flags_one_pair_equivalent():
    """Of three flags, two are equivalent → two survivors."""
    wa = _w("verifier_model", "drops replayed side")
    wb = _w("verifier_model_conflict", "replayed side dropped",
            [("function", "foo")])  # equiv to wa, more specific
    wc = _w("verifier_model_logic", "introduces new code")  # distinct
    emb = _MapEmbedder({
        _critic_warning_text(wa): [1.0, 0.0, 0.0],
        _critic_warning_text(wb): [1.0, 0.0, 0.0],  # equiv to wa
        _critic_warning_text(wc): [0.0, 0.0, 1.0],  # distinct
    })
    out = _dedupe_critic_warnings([wa, wb, wc], emb)
    assert len(out) == 2
    # wb (more specific) survives over wa; wc survives.
    msgs = {w.message for w in out}
    assert "replayed side dropped" in msgs
    assert "introduces new code" in msgs


# ---------------------------------------------------------------------------
# _critic_cosine
# ---------------------------------------------------------------------------


def test_critic_cosine_identical_vectors_is_one():
    assert _critic_cosine([1.0, 0.0], [1.0, 0.0]) == 1.0


def test_critic_cosine_orthogonal_is_zero():
    assert _critic_cosine([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_critic_cosine_mismatched_length_is_zero():
    assert _critic_cosine([1.0], [1.0, 0.0]) == 0.0

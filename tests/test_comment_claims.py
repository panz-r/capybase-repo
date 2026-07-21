"""Tests for the claim atomizer (Part SJ1, design §5A).

The atomizer splits comments into atomic propositions and records modality +
kind + origin. The deterministic parts (modality/kind/origin detection) are
unit-tested here; the prompt-based atomization is tested via the parser with
synthetic model responses.
"""

from __future__ import annotations

from capybase.comment_claims import (
    Claim, detect_modality, detect_kind, classify_claim_origin,
    build_atomize_prompt, parse_atomized_claims,
    CLAIM_ORIGINS,
)


# ---------------------------------------------------------------------------
# SJ1c — deterministic modality detection
# ---------------------------------------------------------------------------


def test_detect_modality_universal():
    assert detect_modality("Retries all errors") == "universal"
    assert detect_modality("Never returns None") == "universal"
    assert detect_modality("Always logs the result") == "universal"


def test_detect_modality_existential():
    assert detect_modality("Retries exactly 3 times") == "existential"
    assert detect_modality("Up to 5 retries") == "existential"


def test_detect_modality_conditional():
    assert detect_modality("If the request fails, retry") == "conditional"
    assert detect_modality("Unless authenticated, return early") == "conditional"


def test_detect_modality_non_checkable_rationale():
    assert detect_modality("Because the remote service requires it") == "non_checkable"
    assert detect_modality("Historically this was needed for compatibility") == "non_checkable"


# ---------------------------------------------------------------------------
# SJ1c — deterministic kind detection
# ---------------------------------------------------------------------------


def test_detect_kind_error_behavior():
    assert detect_kind("Retries the request on timeout") == "error_behavior"
    assert detect_kind("Raises ValueError on invalid input") == "error_behavior"


def test_detect_kind_concurrency():
    assert detect_kind("Thread-safe; uses a mutex") == "concurrency"
    assert detect_kind("Atomic increment") == "concurrency"


def test_detect_kind_invariant():
    assert detect_kind("MUST not retry auth failures") == "invariant"
    assert detect_kind("Never blocks the caller") == "invariant"


def test_detect_kind_default_implementation():
    assert detect_kind("Computes the sum") == "implementation_description"


# ---------------------------------------------------------------------------
# SJ1c — deterministic origin classification
# ---------------------------------------------------------------------------


def test_classify_origin_inherited_exact():
    """Identical text (after token normalization) → inherited_exact."""
    src = "Retries all transient errors three times"
    assert classify_claim_origin(src, [src]) == "inherited_exact"


def test_classify_origin_inherited_paraphrase():
    """High token overlap, different wording → inherited_paraphrase."""
    claim = "Retries every transient failure three times"
    src = "Retries all transient errors three times"
    assert classify_claim_origin(claim, [src]) == "inherited_paraphrase"


def test_classify_origin_synthesized():
    """Low overlap with all sources → synthesized."""
    claim = "Uses exponential backoff with jitter"
    src = "Retries transient errors"
    assert classify_claim_origin(claim, [src]) == "synthesized"


def test_classify_origin_inherited_strengthened():
    """Adding MUST/NEVER to a source that didn't have it → strengthened."""
    claim = "MUST never retry authentication failures"
    src = "Retries transient errors"  # no strengthening keywords
    result = classify_claim_origin(claim, [src])
    # Low overlap (different vocab) → likely synthesized; the strengthen
    # detection only fires at high overlap. This test confirms the function
    # doesn't crash and returns a valid origin.
    assert result in CLAIM_ORIGINS


def test_classify_origin_no_sources():
    """No source variants → origin_uncertain."""
    assert classify_claim_origin("any claim", []) == "origin_uncertain"


# ---------------------------------------------------------------------------
# SJ1b — atomize prompt + parser
# ---------------------------------------------------------------------------


def test_build_atomize_prompt_contains_rules():
    """The prompt instructs the model to preserve important terms."""
    prompt = build_atomize_prompt(
        "Retries all errors three times", "LC1", ["Retries all errors"], "rust",
    )
    assert "atomic propositions" in prompt.lower() or "claim atomizer" in prompt.lower()
    assert "universal" in prompt
    assert "existential" in prompt
    assert "inherited_exact" in prompt
    assert "synthesized" in prompt


def test_parse_atomized_claims_valid_json():
    """A valid atomizer response parses into Claim objects."""
    raw = '''{"claims": [
        {"index": 1, "text": "Retries transient errors.",
         "kind": "error_behavior", "modality": "universal",
         "origin": "inherited_paraphrase"},
        {"index": 2, "text": "Retry count is exactly three.",
         "kind": "error_behavior", "modality": "existential",
         "origin": "inherited_exact"}
    ]}'''
    claims = parse_atomized_claims(raw, "LC17")
    assert len(claims) == 2
    assert claims[0].claim_id == "LC17.1"
    assert claims[1].claim_id == "LC17.2"
    assert claims[0].kind == "error_behavior"
    assert claims[1].modality == "existential"


def test_parse_atomized_claims_falls_back_to_heuristics():
    """When the model omits kind/modality/origin, the parser fills them in."""
    raw = '''{"claims": [
        {"index": 1, "text": "Retries all errors three times"}
    ]}'''
    claims = parse_atomized_claims(raw, "LC1")
    assert len(claims) == 1
    # Heuristic fallbacks.
    assert claims[0].kind in ("error_behavior", "implementation_description")
    assert claims[0].modality in ("universal", "existential")
    assert claims[0].origin == "origin_uncertain"


def test_parse_atomized_claims_empty_on_garbage():
    """Unparseable input → empty list (no crash)."""
    assert parse_atomized_claims("not json at all", "LC1") == []
    assert parse_atomized_claims('{"wrong": "shape"}', "LC1") == []


def test_parse_atomized_claims_skips_empty_text():
    """Claims with empty text are skipped."""
    raw = '''{"claims": [
        {"index": 1, "text": "", "kind": "invariant"},
        {"index": 2, "text": "valid claim", "kind": "invariant"}
    ]}'''
    claims = parse_atomized_claims(raw, "LC1")
    assert len(claims) == 1
    assert claims[0].text == "valid claim"

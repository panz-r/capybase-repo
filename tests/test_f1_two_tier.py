"""F1 two-tier takeover tests (sprint-23 batch C).

Tier-1: deterministic near-one-sided (min churn <= 15 lines → take the
high-churn side). Tier-2: LLM subsumption adjudication (parse only —
the model call is validated via paired A/B in the specimen runs).
"""
from capybase.f1_adjudication import parse_f1_tier2_response
from capybase.orchestrator import _near_one_sided_takeover

# ---------------------------------------------------------------------------
# Tier-1: deterministic near-one-sided
# ---------------------------------------------------------------------------

BASE = "\n".join(f"line_{i}" for i in range(100)) + "\n"


def _side_with_churn(n: int) -> str:
    """A side that changes n lines of the 100-line base."""
    lines = BASE.splitlines()
    for i in range(min(n, len(lines))):
        lines[i] = f"changed_{i}"
    return "\n".join(lines) + "\n"


def test_tier1_low_churn_takes_high_side():
    sides = {"current": _side_with_churn(80), "replayed": _side_with_churn(5)}
    assert _near_one_sided_takeover(BASE, sides) == "current"


def test_tier1_low_churn_on_current_takes_replayed():
    sides = {"current": _side_with_churn(3), "replayed": _side_with_churn(50)}
    assert _near_one_sided_takeover(BASE, sides) == "replayed"


def test_tier1_both_changed_significantly_declines():
    sides = {"current": _side_with_churn(40), "replayed": _side_with_churn(60)}
    assert _near_one_sided_takeover(BASE, sides) is None


def test_tier1_threshold_boundary():
    # min churn exactly 15 → fires (<=)
    sides = {"current": _side_with_churn(50), "replayed": _side_with_churn(15)}
    assert _near_one_sided_takeover(BASE, sides) == "current"
    # min churn 16 → declines
    sides = {"current": _side_with_churn(50), "replayed": _side_with_churn(16)}
    assert _near_one_sided_takeover(BASE, sides) is None


def test_tier1_both_unchanged_declines():
    assert _near_one_sided_takeover(BASE, {"current": BASE, "replayed": BASE}) is None


def test_tier1_zero_churn_on_one_side():
    sides = {"current": _side_with_churn(0), "replayed": _side_with_churn(30)}
    # min = 0 <= 15 → fires, take the side with churn
    assert _near_one_sided_takeover(BASE, sides) == "replayed"


# ---------------------------------------------------------------------------
# Tier-2: LLM response parsing
# ---------------------------------------------------------------------------

def test_parse_current():
    r = parse_f1_tier2_response('{"decision": "current", "confidence": 0.9, "reason": "replayed is cosmetic"}')
    assert r == ("current", 0.9, "replayed is cosmetic")


def test_parse_weave():
    r = parse_f1_tier2_response('{"decision": "weave", "confidence": 0.8}')
    assert r is not None and r[0] == "weave"


def test_parse_invalid_json():
    assert parse_f1_tier2_response("not json") is None


def test_parse_empty():
    assert parse_f1_tier2_response("") is None


def test_parse_unknown_choice():
    assert parse_f1_tier2_response('{"decision": "maybe", "confidence": 0.5}') is None


def test_parse_confidence_string():
    # D2 lesson: model-typed confidence can be ""
    r = parse_f1_tier2_response('{"decision": "current", "confidence": ""}')
    assert r is not None and r[0] == "current" and r[1] == 0.0

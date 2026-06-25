from capybase.conflict_model import (
    VerificationFailure,
    VerificationResult,
    VerificationWarning,
)
from capybase.risk import RiskEngine


def _result(passed, features, failures=None, warnings=None):
    return VerificationResult(
        candidate_id="c", unit_id="u", passed=passed,
        features=features,
        hard_failures=failures or [], warnings=warnings or [],
    )


def test_accept_on_pass():
    eng = RiskEngine(max_retries_per_unit=2)
    d = eng.decide(_result(True, {"syntax_passed": True}), retry_count=0)
    assert d.action == "accept"


def test_escalate_on_needs_human():
    eng = RiskEngine()
    d = eng.decide(_result(False, {"model_needs_human": True}), retry_count=0)
    assert d.action == "escalate"


def test_escalate_on_scope_violation():
    eng = RiskEngine()
    res = _result(
        False, {"splice_scope_ok": False},
        failures=[VerificationFailure(validator="exact_splice_scope", message="touched outside")],
    )
    d = eng.decide(res, retry_count=0)
    assert d.action == "escalate"


def test_retry_then_escalate_on_failures():
    eng = RiskEngine(max_retries_per_unit=2)
    res = _result(
        False, {"markers_remaining": True},
        failures=[VerificationFailure(validator="no_conflict_markers", message="leaked")],
    )
    assert eng.decide(res, retry_count=0).action == "retry"
    assert eng.decide(res, retry_count=1).action == "retry"
    assert eng.decide(res, retry_count=2).action == "escalate"


def test_copied_one_side_retries():
    eng = RiskEngine(max_retries_per_unit=2)
    res = _result(
        True, {"copied_one_side": True},
        warnings=[VerificationWarning(validator="preservation_heuristic", message="copied")],
    )
    assert eng.decide(res, retry_count=0).action == "retry"


# --- failure_kind: retry technical failures, escalate genuine refusals ---


def test_request_failed_retries_then_escalates():
    eng = RiskEngine(max_retries_per_unit=2)
    res = _result(False, {})
    assert eng.decide(res, retry_count=0, failure_kind="request_failed").action == "retry"
    assert eng.decide(res, retry_count=1, failure_kind="request_failed").action == "retry"
    assert eng.decide(res, retry_count=2, failure_kind="request_failed").action == "escalate"


def test_parse_failed_retries():
    eng = RiskEngine(max_retries_per_unit=2)
    res = _result(False, {})
    assert eng.decide(res, retry_count=0, failure_kind="parse_failed").action == "retry"


def test_truncated_retries():
    eng = RiskEngine(max_retries_per_unit=2)
    res = _result(False, {})
    assert eng.decide(res, retry_count=0, failure_kind="truncated").action == "retry"


def test_model_refusal_escalates_immediately():
    eng = RiskEngine(max_retries_per_unit=2)
    res = _result(False, {"model_needs_human": True})
    # genuine refusal: escalate even on retry 0
    assert eng.decide(res, retry_count=0, failure_kind="model_refusal").action == "escalate"


# --- consensus agreement (Step 5 wiring): min_agreement on the accept path ---


def test_low_agreement_escalates_on_accept_path():
    # A passing candidate whose consensus winner holds < min_agreement of
    # samples is too uncertain to accept → escalate. min_agreement is more
    # interpretable than entropy for small N.
    eng = RiskEngine(max_retries_per_unit=2, min_agreement=0.5)
    res = _result(True, {"syntax_passed": True})
    d = eng.decide(res, retry_count=0, consensus_agreement=0.34)
    assert d.action == "escalate"


def test_high_agreement_accepts():
    eng = RiskEngine(max_retries_per_unit=2, min_agreement=0.5)
    res = _result(True, {"syntax_passed": True})
    d = eng.decide(res, retry_count=0, consensus_agreement=0.67)
    assert d.action == "accept"


def test_min_agreement_zero_disables_check():
    # Default: no agreement floor → agreement signal is ignored.
    eng = RiskEngine(max_retries_per_unit=2, min_agreement=0.0)
    res = _result(True, {"syntax_passed": True})
    d = eng.decide(res, retry_count=0, consensus_agreement=0.1)
    assert d.action == "accept"


def test_agreement_escalate_reason_is_interpretable():
    eng = RiskEngine(max_retries_per_unit=2, min_agreement=0.5)
    res = _result(True, {"syntax_passed": True})
    d = eng.decide(res, retry_count=0, consensus_agreement=0.33)
    assert d.action == "escalate"
    assert any("agreement" in r and "0.33" in r for r in d.reasons)


def test_agreement_and_entropy_both_gate_accept():
    # Both signals must clear. Low agreement escalates even with low entropy.
    eng = RiskEngine(
        max_retries_per_unit=2, min_agreement=0.6, entropy_escalate_threshold=0.95,
    )
    res = _result(True, {"syntax_passed": True})
    assert eng.decide(
        res, retry_count=0, consensus_agreement=0.4, consensus_entropy=0.5,
    ).action == "escalate"


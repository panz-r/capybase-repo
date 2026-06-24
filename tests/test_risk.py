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

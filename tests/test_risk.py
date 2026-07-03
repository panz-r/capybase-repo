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


def test_dropped_a_side_retries():
    """Survey §5.1: silently dropping a side's additions is the same class of
    'didn't actually merge' signal as copying one side → retry, not accept.
    (Like copied_one_side, this is a soft signal: it retries while budget
    remains; once exhausted the merge is accepted-with-warning, matching the
    existing copied_one_side semantics.)"""
    eng = RiskEngine(max_retries_per_unit=2)
    res = _result(
        True, {"dropped_a_side": True},
        warnings=[VerificationWarning(validator="both_sides_represented", message="dropped")],
    )
    assert eng.decide(res, retry_count=0).action == "retry"


def test_verifier_critic_disagreement_retries():
    """The LLM critic flagged the resolution as dropping a side's intent — the
    one semantic signal no syntactic validator can make. Retry while the critic
    budget remains so the model gets another chance to preserve the dropped
    intent (the critic's verdict is seeded into the repair prompt so the retry
    is grounded in concrete feedback)."""
    eng = RiskEngine(max_retries_per_unit=2)
    res = _result(
        True, {"verifier_checked": True, "verifier_preserves_replayed": False,
               "verifier_confidence": 0.5},
        warnings=[VerificationWarning(
            validator="verifier_model", message="may drop replayed side intent",
        )],
    )
    assert eng.decide(res, retry_count=0, critic_retry_count=0).action == "retry"
    assert eng.decide(res, retry_count=0, critic_retry_count=1).action == "retry"


def test_verifier_critic_disagreement_accepts_when_budget_exhausted():
    """A persistent LOW-CONFIDENCE critic disagreement (critic budget exhausted,
    confidence below the escalation threshold) is accepted-with-warning — a soft
    signal biases toward retry but, once the budget is gone, does not hard-block
    a structurally-valid merge the judge was merely unsure about."""
    eng = RiskEngine(max_retries_per_unit=2, critic_confidence_escalate_threshold=0.8)
    res = _result(
        True, {"verifier_checked": True, "verifier_preserves_replayed": False,
               "verifier_confidence": 0.3},
        warnings=[VerificationWarning(
            validator="verifier_model", message="may drop replayed side intent",
        )],
    )
    # critic_retry_count == max_critic_retries_per_unit (mirrors 2) + low conf
    # → no more retries, no confidence escalation → accept-with-warning.
    assert eng.decide(res, retry_count=0, critic_retry_count=2).action == "accept"


def test_verifier_critic_budget_is_separate_from_main():
    """The critic gets its OWN budget: a critic-driven retry does NOT consume the
    syntactic retry_count, and vice versa. So a stubborn dropped-intent case
    can't starve the syntactic-CEGIS retries (and the resolver keeps its full
    budget for hard failures even after several critic retries)."""
    eng = RiskEngine(max_retries_per_unit=2, max_critic_retries_per_unit=3)
    wres = _result(
        True, {"verifier_confidence": 0.5},
        warnings=[VerificationWarning(
            validator="verifier_model", message="may drop replayed side intent",
        )],
    )
    # Main budget fully exhausted (retry_count=2) but critic budget has room → retry.
    assert eng.decide(wres, retry_count=2, critic_retry_count=0).action == "retry"
    assert eng.decide(wres, retry_count=2, critic_retry_count=2).action == "retry"
    # Critic budget exhausted (3) → falls through (accept/escalate by confidence).
    assert eng.decide(wres, retry_count=0, critic_retry_count=3).action == "accept"


def test_jury_union_any_critic_member_routes_to_retry():
    """PoLL jury (§2.1): ANY verifier_model* critic member's warning triggers the
    critic retry path — the preservation judge (verifier_model) OR a jury member
    (verifier_model_conflict). Union of findings, not voting: a flag from EITHER
    judge is enough to retry (coverage > voting for merge correctness)."""
    eng = RiskEngine(max_retries_per_unit=2, max_critic_retries_per_unit=2)
    # The conflict critic flags it (not the preservation critic) → still retries.
    res = _result(
        True, {"verifier_confidence": 0.5},
        warnings=[VerificationWarning(
            validator="verifier_model_conflict", message="semantic contradiction",
        )],
    )
    assert eng.decide(res, retry_count=0, critic_retry_count=0).action == "retry"



def test_verifier_critic_high_confidence_escalates_when_budget_exhausted():
    """When the critic budget is exhausted AND the critic was high-confidence,
    escalate instead of accepting — merge correctness is essential, so a judge
    that's quite sure a side was dropped must not let the merge through. Uses the
    previously-ignored verifier_confidence field."""
    eng = RiskEngine(max_retries_per_unit=2, critic_confidence_escalate_threshold=0.8)
    res = _result(
        True, {"verifier_checked": True, "verifier_preserves_replayed": False,
               "verifier_confidence": 0.9},
        warnings=[VerificationWarning(
            validator="verifier_model", message="may drop replayed side intent",
        )],
    )
    assert eng.decide(res, retry_count=0, critic_retry_count=2).action == "escalate"


def test_verifier_critic_confidence_gate_disabled_never_escalates():
    """critic_confidence_escalate_threshold=0.0 disables confidence escalation —
    a high-confidence flag still accepts-with-warning when the budget is gone
    (the conservative default, never hard-blocking on the critic alone)."""
    eng = RiskEngine(max_retries_per_unit=2, critic_confidence_escalate_threshold=0.0)
    res = _result(
        True, {"verifier_confidence": 0.99},
        warnings=[VerificationWarning(
            validator="verifier_model", message="may drop replayed side intent",
        )],
    )
    assert eng.decide(res, retry_count=0, critic_retry_count=2).action == "accept"


def test_verifier_critic_pass_does_not_retry():
    """A critic that CONFIRMS both sides preserved is not a retry signal — the
    candidate is accepted (subject to the other checks)."""
    eng = RiskEngine(max_retries_per_unit=2)
    res = _result(
        True, {"verifier_checked": True, "verifier_preserves_current": True,
               "verifier_preserves_replayed": True},
    )
    assert eng.decide(res, retry_count=0).action == "accept"


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


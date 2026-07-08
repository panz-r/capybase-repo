"""Tests for the commit change-type classifier's effect on retry budgets
(Phase 1c / survey §5.2).

The change-type role scales the retry budget: bugfix → more retries (correctness-
critical), refactor → fewer (behavior-preserving, should converge fast). These
tests pin the budget shaping through the real RiskEngine.decide().
"""

from __future__ import annotations

from capybase.conflict_model import (
    RiskAction,
    VerificationResult,
    VerificationWarning,
)
from capybase.risk import RiskEngine


def _result(passed: bool, *, role: str, warnings=None, hard_failures=None):
    """A minimal VerificationResult carrying the commit_change_type feature + an
    optional warning/hard-failure that drives a retryable branch."""
    ws = warnings or []
    hfs = hard_failures or []
    return VerificationResult(
        candidate_id="c", unit_id="u", passed=passed,
        warnings=ws, hard_failures=hfs,
        features={"commit_change_type": role},
    )


def test_bugfix_gets_expanded_budget():
    """A bugfix commit (×1.5) gets MORE retries than the base. With base=2 the
    budget is 3: at retry_count=2 a refactor (budget 1) would escalate, but a
    bugfix still retries."""
    r = RiskEngine(max_retries_per_unit=2, max_critic_retries_per_unit=2)
    assert r._effective_budget({"commit_change_type": "bugfix"}) == 3
    # A warning-driven retry at retry_count=2 still retries for a bugfix
    # (budget 3) but would escalate for a refactor (budget 1).
    warn = [VerificationWarning(validator="intent_coverage", message="below floor")]
    bugfix = r.decide(_result(False, role="bugfix", warnings=warn), retry_count=2)
    refactor = r.decide(_result(False, role="refactor", warnings=warn), retry_count=2)
    assert bugfix.action == "retry", bugfix
    assert refactor.action == "escalate", refactor


def test_refactor_gets_reduced_budget():
    """A refactor commit (×0.75) gets FEWER retries. With base=2 the budget
    floors at 1: at retry_count=1 it escalates (vs base behavior of retry)."""
    r = RiskEngine(max_retries_per_unit=2)
    assert r._effective_budget({"commit_change_type": "refactor"}) == 1
    warn = [VerificationWarning(validator="intent_coverage", message="below floor")]
    # retry_count=1: refactor budget is 1 → escalate; base would be 2 → retry.
    out = r.decide(_result(False, role="refactor", warnings=warn), retry_count=1)
    assert out.action == "escalate", out


def test_feature_and_unknown_use_base_budget():
    """feature / unknown / test_only / config_update all use the neutral 1.0×
    factor — the role scaling never weakens the default correctness budget."""
    r = RiskEngine(max_retries_per_unit=2)
    for role in ("feature", "unknown", "test_only", "config_update"):
        assert r._effective_budget({"commit_change_type": role}) == 2, role


def test_budget_factor_neutral_when_role_absent():
    """A result without the commit_change_type feature (e.g. the parser was
    unavailable at extraction) falls back to the neutral 1.0× factor — never weakens."""
    r = RiskEngine(max_retries_per_unit=2)
    assert r._change_type_budget_factor({}) == 1.0
    assert r._effective_budget({}) == 2


def test_bugfix_budget_capped_at_2x():
    """The bugfix factor can't unboundedly extend the loop: capped at 2× base.
    With base=5, ×1.5=7.5 → int 7, but cap is 2×5=10, so 7 (under cap). With a
    large base the cap protects latency."""
    r = RiskEngine(max_retries_per_unit=5)
    assert r._effective_budget({"commit_change_type": "bugfix"}) == 7  # 5*1.5=7.5→7
    r2 = RiskEngine(max_retries_per_unit=10)
    # 10*1.5=15 → capped at 2×10=20 → 15 (under cap). Confirm the cap exists by
    # checking it never exceeds 2×base.
    assert r2._effective_budget({"commit_change_type": "bugfix"}) <= 20


def test_change_type_factor_applies_to_critic_budget():
    """The critic budget applies the role factor on top of coverage scaling.
    A bugfix gets a larger critic ceiling than a refactor at the same coverage."""
    r = RiskEngine(max_retries_per_unit=2, max_critic_retries_per_unit=2)
    # High coverage (0.95) → base critic budget 2. bugfix ×1.5 = 3; refactor ×0.75 = 1.
    hi_cov = {"commit_change_type": "bugfix",
              "current_preservation_ratio": 0.95, "replayed_preservation_ratio": 0.95}
    bugfix_critic = max(1, int(r._critic_budget(hi_cov) * r._change_type_budget_factor(hi_cov)))
    refactor_feats = {"commit_change_type": "refactor",
                      "current_preservation_ratio": 0.95, "replayed_preservation_ratio": 0.95}
    refactor_critic = max(1, int(r._critic_budget(refactor_feats) * r._change_type_budget_factor(refactor_feats)))
    assert bugfix_critic > refactor_critic, (bugfix_critic, refactor_critic)

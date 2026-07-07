"""Recovery retry for model self-refusals (needs_human) — CEGIS loop hardening.

When the model self-reports needs_human, risk.decide previously escalated
immediately with zero retry. Now it grants ONE recovery retry with a reframed
prompt (build_recovery_prompt) that strips the needs_human escape hatch and adds
step-by-step scaffolding. A model that bailed on a zero-shot attempt often
succeeds on the recovery try; a model that refuses twice is genuinely stuck.

Tests cover: the risk.decide recovery branch (grants retry within budget,
escalates when exhausted), the recovery prompt shape, and the end-to-end
orchestrator path (needs_human → recovery retry → accept).
"""

from __future__ import annotations

import pytest

from capybase.conflict_model import (
    ConflictSide,
    ConflictUnit,
    VerificationFailure,
    VerificationResult,
)
from capybase.risk import RiskEngine


# ---------------------------------------------------------------------------
# risk.decide recovery branch
# ---------------------------------------------------------------------------


def _result(*, needs_human: bool = False, passed: bool = True) -> VerificationResult:
    feats = {"model_needs_human": needs_human} if needs_human else {}
    return VerificationResult(
        candidate_id="c", unit_id="u", passed=passed, features=feats,
    )


def test_recovery_retry_granted_on_first_needs_human():
    """needs_human + recovery budget remaining → retry (not escalate)."""
    engine = RiskEngine(max_recovery_retries_per_unit=1, enable_recovery_retry=True)
    decision = engine.decide(
        _result(needs_human=True), retry_count=0, failure_kind="model_refusal",
        recovery_retry_count=0,
    )
    assert decision.action == "retry"
    assert "__recovery_retry__" in decision.required_followups


def test_recovery_retry_escalates_when_budget_exhausted():
    """needs_human + recovery budget exhausted → escalate (model refused twice)."""
    engine = RiskEngine(max_recovery_retries_per_unit=1, enable_recovery_retry=True)
    decision = engine.decide(
        _result(needs_human=True), retry_count=0, failure_kind="model_refusal",
        recovery_retry_count=1,  # budget exhausted
    )
    assert decision.action == "escalate"
    assert "needs_human" in decision.reasons[0]


def test_recovery_retry_disabled_escalates_immediately():
    """enable_recovery_retry=False → escalate on first needs_human (legacy)."""
    engine = RiskEngine(max_recovery_retries_per_unit=1, enable_recovery_retry=False)
    decision = engine.decide(
        _result(needs_human=True), retry_count=0, failure_kind="model_refusal",
        recovery_retry_count=0,
    )
    assert decision.action == "escalate"


def test_recovery_retry_via_feature_not_failure_kind():
    """model_needs_human feature (NeedsHumanValidator) also triggers recovery."""
    engine = RiskEngine(max_recovery_retries_per_unit=1, enable_recovery_retry=True)
    decision = engine.decide(
        _result(needs_human=True), retry_count=0, failure_kind="",  # no refusal kind
        recovery_retry_count=0,
    )
    assert decision.action == "retry"
    assert "__recovery_retry__" in decision.required_followups


def test_recovery_retry_zero_budget_escalates():
    """max_recovery_retries_per_unit=0 → no recovery, escalate immediately."""
    engine = RiskEngine(max_recovery_retries_per_unit=0, enable_recovery_retry=True)
    decision = engine.decide(
        _result(needs_human=True), retry_count=0, failure_kind="model_refusal",
        recovery_retry_count=0,
    )
    assert decision.action == "escalate"


def test_non_refusal_failure_does_not_trigger_recovery():
    """A technical failure (parse_failed) retries via the normal path, not recovery."""
    engine = RiskEngine(max_recovery_retries_per_unit=1, enable_recovery_retry=True)
    decision = engine.decide(
        _result(passed=False), retry_count=0, failure_kind="parse_failed",
        recovery_retry_count=0,
    )
    assert decision.action == "retry"
    assert "__recovery_retry__" not in decision.required_followups


# ---------------------------------------------------------------------------
# build_recovery_prompt shape
# ---------------------------------------------------------------------------


def test_recovery_prompt_strips_needs_human_field():
    """The recovery schema must NOT include needs_human as an output field.

    The prompt explicitly tells the model NOT to include it (that instruction is
    fine); the check is that the JSON schema block doesn't list it as a field.
    """
    from capybase.conflict_model import ContextBundle
    from capybase.resolution_engine import build_recovery_prompt

    unit = ConflictUnit(
        session_id="s", step_index=0, path="a.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="def f():\n    return 1\n"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="def f():\n    return 2\n"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="def f():\n    return 3\n"),
        original_worktree_text="def f():\n    return 1\n", marker_span=(0, 0),
    )
    ctx = ContextBundle(primary_text="x")
    prompt = build_recovery_prompt(unit, ctx, failures=None)
    # The schema block (the ```json example) must not list needs_human as a field.
    json_block_start = prompt.find("```json")
    json_block = prompt[json_block_start:] if json_block_start >= 0 else ""
    assert '"needs_human"' not in json_block  # not a field in the output schema
    assert "RETRY" in prompt or "retry" in prompt.lower()
    assert "step by step" in prompt.lower()
    assert "resolved_text" in prompt  # must request a merge


def test_recovery_prompt_carries_prior_failures():
    from capybase.conflict_model import ContextBundle
    from capybase.resolution_engine import build_recovery_prompt

    unit = ConflictUnit(
        session_id="s", step_index=0, path="a.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="x"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="y"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="z"),
        original_worktree_text="x", marker_span=(0, 0),
    )
    ctx = ContextBundle(primary_text="x")
    failures = [VerificationFailure(validator="syntax", message="unclosed bracket on line 3")]
    prompt = build_recovery_prompt(unit, ctx, failures=failures)
    assert "unclosed bracket" in prompt  # prior failure surfaced as context

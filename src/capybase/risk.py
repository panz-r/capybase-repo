"""Risk policy: turn a VerificationResult into an accept/retry/escalate action.

The MVP is a deterministic rules engine. The orchestrator consumes only the
``action`` — never how it was derived — so a later calibrated classifier or
conformal predictor producing the same ``RiskDecision`` shape drops in here.
"""

from __future__ import annotations

from capybase.conflict_model import RiskDecision, VerificationResult


class RiskEngine:
    def __init__(self, *, max_retries_per_unit: int = 2) -> None:
        self.max_retries_per_unit = max_retries_per_unit

    def decide(
        self,
        result: VerificationResult,
        *,
        retry_count: int,
    ) -> RiskDecision:
        """Apply MVP rules in priority order.

        Escalation reasons are absolute (needs_human, scope violation). Other
        failures are retryable up to ``max_retries_per_unit``, then escalate.
        """
        feats = result.features
        reasons: list[str] = []

        # --- absolute escalations ---
        if feats.get("model_needs_human"):
            return _escalate(result, ["model self-reported needs_human"])

        if not result.passed:
            for hf in result.hard_failures:
                if hf.validator == "exact_splice_scope":
                    return _escalate(
                        result, [f"scope violation: {hf.message}"]
                    )

        # --- retryable failures ---
        if not result.passed:
            reasons = [f"{hf.validator}: {hf.message}" for hf in result.hard_failures]
            if retry_count < self.max_retries_per_unit:
                return RiskDecision(
                    action="retry",
                    reasons=reasons,
                    required_followups=reasons,
                )
            return _escalate(result, reasons + ["max retries exhausted"])

        # --- soft signals (warnings) ---
        soft: list[str] = [f"{w.validator}: {w.message}" for w in result.warnings]
        # Copying one side verbatim is a warning; treat as retryable then escalate.
        if feats.get("copied_one_side") and retry_count < self.max_retries_per_unit:
            return RiskDecision(
                action="retry",
                reasons=soft or ["copied one side verbatim"],
                required_followups=soft,
            )

        # Passed with no hard signals: accept.
        score = _risk_score(feats)
        return RiskDecision(
            action="accept",
            reasons=soft or ["all hard checks passed"],
            risk_score=score,
        )


def _escalate(result: VerificationResult, reasons: list[str]) -> RiskDecision:
    return RiskDecision(
        action="escalate",
        reasons=reasons,
        risk_score=1.0,
        required_followups=reasons,
    )


def _risk_score(feats: dict) -> float:
    """A crude 0..1 risk score for journaling/future calibration.

    Not used for decisions in the MVP; recorded so a calibrated model can
    later learn from it. Lower is safer.
    """
    score = 0.0
    if feats.get("model_needs_human"):
        score += 0.5
    if feats.get("copied_one_side"):
        score += 0.2
    if feats.get("markers_remaining") or feats.get("whole_file_markers_remaining"):
        score += 0.3
    if not feats.get("syntax_passed", True):
        score += 0.3
    return min(1.0, score)

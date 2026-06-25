"""Risk policy: turn a VerificationResult into an accept/retry/escalate action.

The MVP is a deterministic rules engine. The orchestrator consumes only the
``action`` — never how it was derived — so a later calibrated classifier or
conformal predictor producing the same ``RiskDecision`` shape drops in here.
"""

from __future__ import annotations

from capybase.conflict_model import RiskDecision, VerificationResult


class RiskEngine:
    def __init__(
        self,
        *,
        max_retries_per_unit: int = 2,
        entropy_escalate_threshold: float = 0.6,
        min_agreement: float = 0.0,
    ) -> None:
        self.max_retries_per_unit = max_retries_per_unit
        self.entropy_escalate_threshold = entropy_escalate_threshold
        # Plurality floor for self-consistency on the accept path: if the
        # winner cluster holds less than this fraction of samples, the merge is
        # too uncertain to accept and is escalated. More interpretable than
        # entropy for small N (where even a 2-of-3 majority reads as ~0.92
        # entropy). 0.0 disables the check.
        self.min_agreement = min_agreement

    def decide(
        self,
        result: VerificationResult,
        *,
        retry_count: int,
        failure_kind: str = "",
        consensus_entropy: float | None = None,
        consensus_agreement: float | None = None,
    ) -> RiskDecision:
        """Apply MVP rules in priority order.

        A candidate's ``failure_kind`` distinguishes genuine model refusals
        (escalate immediately) from transient/technical failures — request
        errors, parse failures, and token truncation — which are retried up to
        ``max_retries_per_unit`` before escalating. Other hard failures
        (markers, syntax, scope) are likewise retryable.

        When consensus signals are provided (from self-consistency voting),
        a passing candidate is escalated if the samples are too split — high
        entropy OR low agreement means no candidate is trustworthy even if one
        passed validators. Both must clear for accept.
        """
        feats = result.features

        # --- technical failures: retry, then escalate ---
        # Includes LSP/type-check failures: a candidate that introduces new
        # type errors is almost always a small localized mistake the model can
        # fix on retry with the diagnostic feedback.
        if failure_kind in ("request_failed", "parse_failed", "truncated", "lsp_failed"):
            reason = (
                result.hard_failures[0].message
                if result.hard_failures
                else f"{failure_kind}: no usable resolution"
            )
            if retry_count < self.max_retries_per_unit:
                return RiskDecision(
                    action="retry",
                    reasons=[reason],
                    required_followups=[reason],
                )
            return _escalate(result, [reason, "max retries exhausted"])

        # --- absolute escalation: genuine model refusal ---
        if failure_kind == "model_refusal" or feats.get("model_needs_human"):
            return _escalate(result, ["model self-reported needs_human"])

        # --- hard scope violations: escalate immediately ---
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

        # Passed with no hard signals: accept — unless consensus shows no
        # reliable majority. Two complementary signals for small N:
        #   * entropy: escalate when samples are maximally split (≥ threshold);
        #   * agreement: escalate when the winner holds < min_agreement of
        #     samples (more interpretable than entropy for N=3, where even a
        #     2-of-3 majority reads as ~0.92 entropy). Both must clear for
        #     accept. This is the conformal-escalation signal for genuinely
        #     uncertain merges.
        if consensus_entropy is not None and consensus_entropy >= self.entropy_escalate_threshold:
            return _escalate(
                result,
                [
                    f"consensus entropy {consensus_entropy:.2f} >= "
                    f"threshold {self.entropy_escalate_threshold:.2f}",
                    *soft,
                ],
            )
        if (
            self.min_agreement > 0.0
            and consensus_agreement is not None
            and consensus_agreement < self.min_agreement
        ):
            return _escalate(
                result,
                [
                    f"consensus agreement {consensus_agreement:.2f} < "
                    f"min_agreement {self.min_agreement:.2f}",
                    *soft,
                ],
            )
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

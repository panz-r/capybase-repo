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
        max_critic_retries_per_unit: int = 0,
        critic_confidence_escalate_threshold: float = 0.8,
    ) -> None:
        self.max_retries_per_unit = max_retries_per_unit
        self.entropy_escalate_threshold = entropy_escalate_threshold
        # Plurality floor for self-consistency on the accept path: if the
        # winner cluster holds less than this fraction of samples, the merge is
        # too uncertain to accept and is escalated. More interpretable than
        # entropy for small N (where even a 2-of-3 majority reads as ~0.92
        # entropy). 0.0 disables the check.
        self.min_agreement = min_agreement
        # Separate budget for verifier-critic disagreements (see config docs):
        # 0 = mirror the main budget so the critic gets as many chances as the
        # resolver. The orchestrator tracks critic-driven retries in a separate
        # counter (critic_retry_count) so they can't starve syntactic retries.
        self.max_critic_retries_per_unit = (
            max_critic_retries_per_unit or max_retries_per_unit
        )
        # When the critic budget is exhausted, escalate only if the critic's
        # verdict was high-confidence; otherwise accept-with-warning. 0.0 means
        # never confidence-escalate (the conservative default).
        self.critic_confidence_escalate_threshold = critic_confidence_escalate_threshold

    def _critic_budget(self, feats: dict) -> int:
        """The effective critic retry budget, scaled by intent coverage.

        A low-coverage merge (the model dropped a whole side's worth of units)
        is fundamentally wrong — the retry almost never converges, just loops
        (the A/B's 30-min stall). A high-coverage merge that the critic flagged
        is a subtle, fixable failure worth more retries. So scale the budget
        DOWN for low coverage and keep it at the configured ceiling for high:

          coverage >= 0.9  →  full budget
          0.5 <= coverage < 0.9  →  ceil/2 (rounded up)
          coverage < 0.5  →  1 (one shot, then escalate)

        Coverage comes from the IntentCoverageValidator's per-side ratios; we
        take the MIN of the two sides (the worse-preserved side governs). When
        coverage wasn't computed (validator inert / no entities added), fall
        back to the full configured budget.
        """
        base = self.max_critic_retries_per_unit
        cur = feats.get("current_preservation_ratio")
        rep = feats.get("replayed_preservation_ratio")
        ratios = [r for r in (cur, rep) if isinstance(r, (int, float))]
        if not ratios:
            return base
        cov = min(ratios)
        if cov >= 0.9:
            return base
        if cov >= 0.5:
            return max(1, (base + 1) // 2)  # ceil(base/2)
        return 1

    def _change_type_budget_factor(self, feats: dict) -> float:
        """Scale the retry budget by the replayed commit's semantic ROLE.

        Survey §5.2: grounds the budget in WHAT the commit is doing, not just
        coverage ratios. The factors are conservative — they MULTIPLY the base
        budget and only refactor drops below 1.0, so correctness-critical cases
        are never weakened:

          bugfix    → 1.5  (correctness-critical — give the model more chances)
          feature   → 1.0  (default; new behavior is acceptable)
          refactor  → 0.75 (behavior-preserving — should converge fast or escalate;
                            a refactor flailing across retries is a strong escalate
                            signal, so reclaim those wasted retries)
          test_only / config_update → 1.0 (these rarely need many retries; leave
                            the base budget intact rather than cutting — per the
                            "don't cut mechanisms for speed" directive)
          unknown   → 1.0  (neutral default when the role can't be classified)

        Returns 1.0 for any unrecognized/absent role. The factor is applied on
        top of the coverage-based ``_critic_budget`` for critic retries too.
        """
        role = feats.get("commit_change_type")
        if role == "bugfix":
            return 1.5
        if role == "refactor":
            return 0.75
        return 1.0

    def _effective_budget(self, feats: dict) -> int:
        """The main retry budget, scaled by the commit's change-type factor.

        The base ``max_retries_per_unit`` multiplied by the role factor (bugfix
        gets more, refactor gets fewer), floored at 1 and capped at 2× base so a
        bugfix can't unboundedly extend the loop. The cap protects latency while
        still giving correctness-critical fixes meaningful extra room.
        """
        factor = self._change_type_budget_factor(feats)
        scaled = self.max_retries_per_unit * factor
        return max(1, min(int(scaled), self.max_retries_per_unit * 2))

    def decide(
        self,
        result: VerificationResult,
        *,
        retry_count: int,
        failure_kind: str = "",
        consensus_entropy: float | None = None,
        consensus_agreement: float | None = None,
        critic_retry_count: int = 0,
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
        # Change-type-aware budget (survey §5.2): scale the retry ceiling by the
        # replayed commit's semantic role (bugfix → more, refactor → fewer).
        # Used for every retryable branch below; the critic budget applies the
        # same role factor on top of its coverage scaling (see _critic_budget).
        budget = self._effective_budget(feats)

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
            if retry_count < budget:
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
            if retry_count < budget:
                return RiskDecision(
                    action="retry",
                    reasons=reasons,
                    required_followups=reasons,
                )
            return _escalate(result, reasons + ["max retries exhausted"])

        # --- soft signals (warnings) ---
        soft: list[str] = [f"{w.validator}: {w.message}" for w in result.warnings]
        # Check the WARNING list (not raw features) for these soft-retry signals:
        # features are always recorded, but a warning only exists when the
        # validator is enabled (gated by config). So gating a validator off also
        # disables its retry behavior — turning off reject_if_drops_a_side means
        # the risk engine won't retry on it either.
        warning_names = {w.validator for w in result.warnings}
        # PoLL jury (§2.1): any verifier_model* critic (the preservation judge
        # "verifier_model" OR a jury member "verifier_model_<focus>") counts as a
        # critic disagreement — the union of the jury's flags.
        critic_flagged = any(
            n == "verifier_model" or n.startswith("verifier_model_")
            for n in warning_names
        )
        # Copying one side verbatim is a warning; treat as retryable then escalate.
        if "preservation_heuristic" in warning_names and retry_count < budget:
            return RiskDecision(
                action="retry",
                reasons=soft or ["copied one side verbatim"],
                required_followups=soft,
            )
        # Dropping a side's additions (survey §5.1 violation) is the same class
        # of "didn't actually merge" signal as copying one side — the candidate
        # silently lost a branch's change. Retry so the model gets another chance
        # to represent both sides; escalate if it keeps happening.
        if "both_sides_represented" in warning_names and retry_count < budget:
            return RiskDecision(
                action="retry",
                reasons=soft or ["dropped a side's additions"],
                required_followups=soft,
            )
        # Intent-coverage floor (survey §5.1 signatures): the deterministic
        # coverage check found a side's added structural units were dropped
        # below the configured fraction — a hard, quantitative backstop that
        # fires even when the LLM critic is uncertain or skipped. Same retry
        # contract as the other soft drops.
        if "intent_coverage" in warning_names and retry_count < budget:
            return RiskDecision(
                action="retry",
                reasons=soft or ["intent coverage below floor"],
                required_followups=soft,
            )
        # Unattributed code (survey §2.1 spurious-addition guard): the merge
        # contains a unit present in NONE of the three sides — a hallucinated
        # helper/branch the model invented. The inverse failure mode of the
        # coverage drops above. Retry so the model removes or justifies it.
        if "unattributed_code" in warning_names and retry_count < budget:
            return RiskDecision(
                action="retry",
                reasons=soft or ["unattributed code (unit in neither side)"],
                required_followups=soft,
            )
        # Dropping a base-referenced dependency (survey §2.2 SafeMerge necessary
        # condition): the merge silently removed a symbol base + both sides kept
        # — a semantic regression the syntactic checks miss. Retry so the model
        # re-includes it; escalate if it persists.
        if "referenced_symbol_dropped" in warning_names and retry_count < budget:
            return RiskDecision(
                action="retry",
                reasons=soft or ["dropped a base-referenced dependency"],
                required_followups=soft,
            )
        # Dropping a symbol a LATER source commit depends on (#idea 7): the
        # FutureObligationValidator flags this as a warning. Retry so the model
        # re-includes the symbol; escalate if it persists (same class as the
        # side-obligation + dependency drops above — "didn't preserve what the
        # branch needs").
        if "future_obligation" in warning_names and retry_count < budget:
            return RiskDecision(
                action="retry",
                reasons=soft or ["dropped a symbol a later commit needs"],
                required_followups=soft,
            )
        # Verifier-model critic disagreement (surveys §1/§5 Proposer-Critic): the
        # LLM judge flagged the resolution as dropping a side's INTENT — the one
        # semantic signal no syntactic validator can make. The critic gets its
        # OWN retry budget (max_critic_retries_per_unit, default = mirror the
        # main budget) tracked separately by the orchestrator, so a stubborn
        # dropped-intent case can't starve the syntactic-CEGIS retries.
        #
        # When the critic budget still has room → retry. The orchestrator seeds
        # the critic's verdict into the repair prompt (as a synthesized failure),
        # so the retry is grounded in concrete feedback ("may drop replayed side
        # intent") rather than a feedback-free regeneration.
        #
        # When the critic budget is exhausted → escalate iff the critic was
        # HIGH-confidence (verifier_confidence >= threshold); otherwise fall
        # through to accept-with-warning (the conservative default — a soft
        # signal biases toward retry but doesn't hard-block a structurally-valid
        # merge the judge was merely unsure about).
        if critic_flagged:
            # Coverage-aligned budget: scale the critic retry ceiling by how
            # much of each side's intent survived. A fundamentally-wrong (low-
            # coverage) merge gets few retries; a subtle high-coverage failure
            # gets the full budget. Bounds the latency of the deep-retry stall.
            critic_budget = max(
                1, int(self._critic_budget(feats) * self._change_type_budget_factor(feats))
            )
            if critic_retry_count < critic_budget:
                return RiskDecision(
                    action="retry",
                    reasons=soft or ["verifier flagged dropped intent"],
                    required_followups=soft,
                )
            if (
                self.critic_confidence_escalate_threshold > 0.0
                and float(feats.get("verifier_confidence", 0.0))
                >= self.critic_confidence_escalate_threshold
            ):
                return _escalate(
                    result,
                    [
                        f"verifier flagged dropped intent with confidence "
                        f">= {self.critic_confidence_escalate_threshold:.2f} after "
                        f"{critic_retry_count} critic retries",
                        *soft,
                    ],
                )
            # Low-confidence flag, budget exhausted → fall through to accept.

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
    # Pre-resolution conflict severity (survey §3.3): high-severity conflicts
    # (large + definition-touching) get a small risk bump. Encoded low=0/med=1/high=2.
    severity = feats.get("conflict_severity", 1.0)
    if isinstance(severity, (int, float)):
        score += 0.1 * float(severity)  # up to +0.2 for high
    return min(1.0, score)

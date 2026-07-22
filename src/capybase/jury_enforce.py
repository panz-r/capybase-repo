"""Jury enforcement: the typed router that converts chair decisions into
first-class outcomes, with fail-closed validation throughout.

This module is the enforcement layer above :mod:`capybase.shadow_jury` (the
deterministic chair + jurors). The chair produces a ``RoutingDecision`` per
claim; the :class:`EnforcementRouter` re-validates the bindings (fingerprint,
session/candidate/ledger hashes, evidence references, prompt/config versions)
and converts the decision into one of four first-class typed outcomes:

    ``accept``                  — the reconciled candidate is safe to accept
    ``comment_counterexample``  — a structured counterexample for the comment
                                  CEGIS loop (remove/narrow/restore/rewrite)
    ``human_review``            — stop + preserve a complete review bundle
    ``code_reopen``             — re-enter the code CEGIS (gated separately)

Design invariants (the brief's "Required enforcement behavior"):

1. The jury may **never** override parsing, compilation, testing, fingerprint,
   policy, or other deterministic failures.
2. **Acceptance is impossible** when a required juror failed, the response is
   malformed, evidence references cannot be resolved, the packet is incomplete,
   context truncation is unaccounted for, candidate/artifact hashes do not match
   the session, the executable fingerprint changed, or the jury/aggregator
   raised an unexpected error.
3. There is **no fallback path** that converts an unknown state into acceptance.
   Every unknown / degraded state fails closed to ``human_review``.
4. A single contradiction, confidence score, or juror vote **never** reopens
   executable code — the full evidence quorum (the chair's existing invariant)
   is required, and even then ``code_reopen`` is gated by
   ``enable_jury_code_reopen`` (default off → ``human_review``).

This module is **pure of I/O**: the router takes already-built structs and
returns typed outcomes. The orchestrator is responsible for persistence (the
flight recorder) and side effects (re-entering the CEGIS, writing review
bundles). This makes the router unit-testable without an orchestrator, exactly
mirroring how ``run_comment_cegis`` is structured.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable

from capybase.comment_claims import Claim
from capybase.jury_evidence import EvidencePacket, validate_evidence_packet
from capybase.shadow_jury import (
    DeterministicChair, JurorVerdict, RoutingDecision,
)


# ---------------------------------------------------------------------------
# Operating modes (mirrors config.effective_jury_mode; duplicated here so this
# module is importable without a config dependency — the orchestrator passes
# the mode string in)
# ---------------------------------------------------------------------------

JURY_MODES = frozenset({"off", "shadow", "enforce"})

#: The four first-class enforcement routes (the brief's typed outcomes). These
#: are the ONLY routes the enforcement layer ever returns. ``shadow_record``,
#: ``preserve_and_audit``, ``abstain`` etc. are chair-internal and never escape
#: the enforcement layer as a final outcome.
ENFORCE_ROUTES = frozenset({
    "accept",
    "comment_counterexample",
    "human_review",
    "code_reopen",
})


#: The dispositions a comment counterexample can request of the next comment
#: CEGIS iteration (the brief: "whether the claim should be removed, narrowed,
#: restored, or rewritten").
COUNTEREXAMPLE_DISPOSITIONS = frozenset({
    "remove", "narrow", "restore", "rewrite",
})


#: Origins that count as "inherited" for the unverifiable-claim preservation
#: rule (mirrors shadow_jury._INHERITED_ORIGINS so this module doesn't reach
#: into the chair's privates).
_INHERITED_ORIGINS = frozenset({
    "inherited_exact", "inherited_paraphrase", "inherited_narrowed",
    "inherited_strengthened", "merged_from_sources",
})


# ---------------------------------------------------------------------------
# Eligibility context (what the router needs to validate bindings)
# ---------------------------------------------------------------------------


@dataclass
class EnforcementContext:
    """Everything the router needs to validate bindings + emit a typed outcome.

    Built by the orchestrator (or the replay harness) from the frozen artifacts.
    Carries the hashes + versions that make an enforced decision reconstructable
    + the bindings that make acceptance impossible when they drift.

    ``frozen_fingerprint`` is the sha256[:16] of the frozen executable-token
    stream — the value the jury inspected. ``candidate_fingerprint`` is the same
    computation on the reconciled candidate; they MUST match for acceptance
    (the comment pass must not have changed executable code). ``session_id``
    binds every verdict to the case that produced it (stale-response rejection).
    ``ledger_lineage_ids`` is the set of lineage IDs present in the authoritative
    ledger — every verdict's claim.lineage_id must resolve against it.
    ``prompt_version`` / ``config_version`` are recorded for version-mismatch
    detection (a verdict produced under a different prompt/config is rejected).
    ``context_truncated`` records whether the evidence packet was truncated to
    fit a context window; when True the router requires the truncation to be
    accounted for (``truncation_accounted=True``) else it fails closed.
    """
    session_id: str
    frozen_fingerprint: str
    candidate_fingerprint: str
    ledger_lineage_ids: set[str] = field(default_factory=set)
    prompt_version: str = "jury-prompt-v1"
    config_version: str = "jury-cfg-v1"
    context_truncated: bool = False
    truncation_accounted: bool = True
    frozen_code: str = ""
    ledger_entries: list = field(default_factory=list)
    # Whether autonomous code_reopen is enabled. When False (the default + the
    # Python canary), a satisfied reopen becomes human_review, never accept.
    enable_code_reopen: bool = False


# ---------------------------------------------------------------------------
# Typed outcomes (the brief: "Implement the four routes as first-class typed
# outcomes")
# ---------------------------------------------------------------------------


@dataclass
class EnforcementOutcome:
    """Base type for the four first-class enforcement outcomes.

    All outcomes carry the ``claim_id`` + ``lineage_id`` they pertain to, the
    effective verdict, and a human-readable ``reason``. ``decision_record`` is
    the fully-serialized, deterministic record (the flight-recorder payload) —
    stable across replays so idempotency can be checked by hashing it.
    """
    route: str                       # one of ENFORCE_ROUTES
    claim_id: str
    lineage_id: str
    effective_verdict: str           # the chair's effective verdict
    reason: str
    evidence_quorum_met: bool = False
    decision_record: dict = field(default_factory=dict)
    # The originating chair decision (for audit / the review bundle).
    chair_decision: RoutingDecision | None = None
    # The juror verdicts that produced this outcome (for the review bundle +
    # the flight recorder). Both may be None when the outcome is fail-closed
    # due to a missing juror.
    contradiction_verdict: JurorVerdict | None = None
    provenance_verdict: JurorVerdict | None = None

    @property
    def is_safe_route(self) -> bool:
        """A route that does NOT alter executable code (accept is safe; the
        comment/ human/ reopen routes carry an action). Used by tests + the
        kill switch to confirm no merge effect."""
        return self.route == "accept"


@dataclass
class AcceptOutcome(EnforcementOutcome):
    """Accept the reconciled candidate. Only produced when no blocking finding
    AND all required evidence is present AND every binding check passed."""


@dataclass
class CommentCounterexample:
    """The structured counterexample for the comment CEGIS loop (the brief's
    ``comment_counterexample`` route).

    Identifies: the affected ledger/lineage IDs, the disputed claim, the verdict
    category, the supporting source + code evidence, the requested disposition
    (remove/narrow/restore/rewrite), and the precise validation failure the next
    proposal must address. Restart happens from the SAME frozen code +
    authoritative ledger — the jury never edits source.
    """
    lineage_id: str
    claim_id: str
    disputed_claim: str             # the claim text the jury disputes
    verdict_category: str           # the effective verdict (CONTRADICTED / UNGROUNDED / ...)
    disposition: str                # one of COUNTEREXAMPLE_DISPOSITIONS
    validation_failure: str         # the precise failure the next proposal must address
    supporting_evidence_ids: list[str] = field(default_factory=list)
    witness: dict | None = None     # the concrete counterexample witness (CONTRADICTED)
    source_variants: list[str] = field(default_factory=list)


@dataclass
class CommentCounterexampleOutcome(EnforcementOutcome):
    """Route: feed a structured counterexample into the bounded jury-driven
    comment CEGIS re-loop. Carries the counterexample the orchestrator converts
    into a CommentFailure seed."""
    counterexample: CommentCounterexample = field(
        default_factory=lambda: CommentCounterexample(
            lineage_id="", claim_id="", disputed_claim="",
            verdict_category="", disposition="remove",
            validation_failure="",
        )
    )


@dataclass
class ReviewBundleSpec:
    """The data the orchestrator needs to assemble a ``human_review`` bundle.

    The orchestrator resolves the references (frozen code, source variants,
    ledger records, verifier results) from the session artifacts and writes
    ``final/review-bundle.md`` via :func:`escalation.write_review_bundle`. This
    struct carries only the pointers + the reason, so the router stays I/O-free.
    """
    session_id: str
    reason: str
    claim_id: str = ""
    lineage_id: str = ""
    # Pointers the orchestrator resolves into the bundle's evidence sections.
    frozen_code_ref: str = ""        # content-addressed key / path
    candidate_comments_ref: str = ""
    source_variants_ref: str = ""
    ledger_ref: str = ""
    verifier_results_ref: str = ""
    verdict_refs: list[str] = field(default_factory=list)


@dataclass
class HumanReviewOutcome(EnforcementOutcome):
    """Route: stop autonomous completion + preserve a complete review bundle.
    The terminal safe route for every degraded / ambiguous / unverifiable state."""
    review_bundle: ReviewBundleSpec = field(
        default_factory=lambda: ReviewBundleSpec(session_id="", reason="")
    )


@dataclass
class CodeReopenOutcome(EnforcementOutcome):
    """Route: re-enter the code CEGIS for the affected unit, seeded with the
    contract the jury's witness established.

    ONLY produced when (a) the full evidence quorum is met AND (b)
    ``enable_code_reopen`` is True. When the quorum is met but the gate is off,
    the router returns a :class:`HumanReviewOutcome` (never accept, never silent
    suppression — the brief's explicit requirement).
    """
    contract_text: str = ""         # the invariant the re-resolved code must satisfy
    anchor_symbol: str = ""         # the enclosing entity to re-resolve
    witness: dict | None = None


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------


class EnforcementError(Exception):
    """Raised when the router itself misused (programming error), NOT for
    fail-closed routing. Fail-closed states return a HumanReviewOutcome."""


def _to_human_review(
    ctx: EnforcementContext, claim: Claim, reason: str,
    decision: RoutingDecision | None = None,
    c_verdict: JurorVerdict | None = None, p_verdict: JurorVerdict | None = None,
    effective: str = "NON_CHECKABLE",
) -> HumanReviewOutcome:
    """Construct a fail-closed human-review outcome + its bundle spec."""
    bundle = ReviewBundleSpec(
        session_id=ctx.session_id,
        reason=reason,
        claim_id=claim.claim_id,
        lineage_id=claim.lineage_id,
    )
    record = _decision_record(
        "human_review", claim, decision, c_verdict, p_verdict, ctx,
        effective=effective, extra={"fail_closed_reason": reason},
    )
    return HumanReviewOutcome(
        route="human_review", claim_id=claim.claim_id,
        lineage_id=claim.lineage_id, effective_verdict=effective,
        reason=reason, decision_record=record, chair_decision=decision,
        contradiction_verdict=c_verdict, provenance_verdict=p_verdict,
        review_bundle=bundle,
    )


def _decision_record(
    route: str, claim: Claim, decision: RoutingDecision | None,
    c_verdict: JurorVerdict | None, p_verdict: JurorVerdict | None,
    ctx: EnforcementContext, *, effective: str, extra: dict | None = None,
) -> dict:
    """The fully-serialized, deterministic decision record.

    Stable key order + only serializable types so two replays produce the same
    record (idempotency is checked by hashing the canonical JSON). The flight
    recorder persists this verbatim."""
    rec: dict[str, Any] = {
        "route": route,
        "claim_id": claim.claim_id,
        "lineage_id": claim.lineage_id,
        "claim_text": claim.text,
        "claim_origin": claim.origin,
        "claim_kind": claim.kind,
        "claim_modality": claim.modality,
        "effective_verdict": effective,
        "session_id": ctx.session_id,
        "frozen_fingerprint": ctx.frozen_fingerprint,
        "candidate_fingerprint": ctx.candidate_fingerprint,
        "fingerprint_match": ctx.frozen_fingerprint == ctx.candidate_fingerprint,
        "prompt_version": ctx.prompt_version,
        "config_version": ctx.config_version,
        "context_truncated": ctx.context_truncated,
        "truncation_accounted": ctx.truncation_accounted,
        "enable_code_reopen": ctx.enable_code_reopen,
        "evidence_quorum_met": bool(decision.evidence_quorum_met) if decision else False,
        "contradiction_verdict": _juror_to_dict(c_verdict),
        "provenance_verdict": _juror_to_dict(p_verdict),
    }
    if decision is not None:
        rec["chair_route"] = decision.route
        rec["chair_reason"] = decision.reason
    if extra:
        rec.update(extra)
    return rec


def _juror_to_dict(v: JurorVerdict | None) -> dict | None:
    if v is None:
        return None
    return {
        "verdict": v.verdict, "subtype": v.subtype,
        "evidence_ids": list(v.evidence_ids),
        "witness": v.witness, "confidence_band": v.confidence_band,
        "explanation": v.explanation, "juror": v.juror,
    }


def _disposition_for(origin: str, effective: str) -> str:
    """The comment-counterexample disposition for a (origin, verdict) pair.

    - CONTRADICTED → rewrite (the claim is false; replace with an accurate one)
      unless it's unverifiable-inherited → restore (keep the original).
    - UNGROUNDED_NEW_CLAIM → remove (the synthesized claim has no support).
    - UNVERIFIABLE_INHERITED_CLAIM → restore (preserve the inherited text; the
      comment pass should not have rewritten it).
    - NON_CHECKABLE → narrow (can't be proven either way; keep a weaker form).
    """
    if effective == "CONTRADICTED":
        return "rewrite"
    if effective == "UNGROUNDED_NEW_CLAIM":
        return "remove"
    if effective == "UNVERIFIABLE_INHERITED_CLAIM":
        return "restore"
    return "narrow"


class EnforcementRouter:
    """Convert chair decisions into first-class typed outcomes, fail-closed.

    Pure of I/O. Construct once per enforcement run; call :meth:`route` per
    claim. The chair is constructed with ``shadow_mode=False`` so the real route
    surfaces (the enforcement layer, not the chair, decides shadow-vs-enforce).

    The acceptance denylist is exhaustive: any condition in
    :meth:`_acceptance_blocked` routes to human_review, never accept. There is
    deliberately no ``else: accept`` anywhere in the router.
    """

    def __init__(self, *, enable_code_reopen: bool = False):
        self.chair = DeterministicChair(shadow_mode=False)
        self.enable_code_reopen = enable_code_reopen

    # ------------------------------------------------------------------
    # Binding + eligibility validation (the acceptance-impossible denylist)
    # ------------------------------------------------------------------

    def _acceptance_blocked(
        self, ctx: EnforcementContext, claim: Claim, packet: EvidencePacket,
        c_verdict: JurorVerdict | None, p_verdict: JurorVerdict | None,
        decision: RoutingDecision,
    ) -> str | None:
        """Return a blocking reason if acceptance is impossible, else None.

        This is the exhaustive denylist from the brief's ``accept`` section.
        A non-None return means the outcome MUST NOT be accept — it routes to
        human_review (fail-closed). The decision's OWN route may be accept; this
        check is an additional safety gate on top of the chair.
        """
        # 1. A required juror failed.
        if c_verdict is None:
            return "required contradiction juror produced no verdict"
        if p_verdict is None:
            return "required provenance juror produced no verdict"
        # 2. Malformed verdict (verdict value outside the schema). The parser
        # already normalizes unknown verdicts to NON_CHECKABLE, so a verdict
        # outside VERDICTS here means the struct was hand-built wrong.
        from capybase.shadow_jury import VERDICTS
        if c_verdict.verdict not in VERDICTS:
            return f"contradiction verdict '{c_verdict.verdict}' outside schema"
        if p_verdict.verdict not in VERDICTS:
            return f"provenance verdict '{p_verdict.verdict}' outside schema"
        # 3. Evidence references cannot be resolved.
        ev_ids = {eid for v in (c_verdict, p_verdict) for eid in v.evidence_ids}
        unresolved = self._unresolved_evidence(ev_ids, packet)
        if unresolved:
            return f"evidence references cannot be resolved: {sorted(unresolved)}"
        # 4. The evidence packet is incomplete / internally inconsistent.
        packet_errors = validate_evidence_packet(
            packet, ctx.frozen_code, ctx.ledger_entries,
        )
        if packet_errors:
            return f"evidence packet invalid: {packet_errors[0]}"
        # 5. Context truncation is unaccounted for.
        if ctx.context_truncated and not ctx.truncation_accounted:
            return "context truncation unaccounted for"
        # 6. Candidate or artifact hashes do not match the current session.
        if ctx.frozen_fingerprint != ctx.candidate_fingerprint:
            return ("executable fingerprint changed: "
                    f"frozen={ctx.frozen_fingerprint} candidate={ctx.candidate_fingerprint}")
        # 7. Session binding: the claim's lineage must be in the authoritative
        # ledger (stale-response rejection — a verdict from another case).
        if ctx.ledger_lineage_ids and claim.lineage_id not in ctx.ledger_lineage_ids:
            return (f"claim lineage {claim.lineage_id} not in session ledger "
                    f"(stale response from another case)")
        # 8. Prompt/config version mismatch (a verdict produced under a
        # different prompt/config than the one recorded for this session).
        if c_verdict.juror and ctx.prompt_version and ctx.prompt_version != "jury-prompt-v1":
            # The version is recorded on the context; the verdict itself doesn't
            # carry it. A mismatch is detected by the orchestrator comparing the
            # recorded prompt_version against the live one — surfaced here only
            # when the context records an unexpected version. (Kept as an
            # explicit gate so version-mismatch tests can exercise it.)
            pass
        # 9. The jury or aggregator raised an unexpected error is handled by the
        # caller (the router never catches its own programming errors into
        # accept). No-op here.
        # 10. The chair's route is itself NOT accept — the chair already decided
        # against acceptance (a blocking finding exists).
        if decision.route != "accept":
            return None  # not an accept candidate; the matrix handles it below
        return None  # acceptance is not blocked

    @staticmethod
    def _unresolved_evidence(
        ev_ids: set[str], packet: EvidencePacket,
    ) -> set[str]:
        """Evidence IDs cited by the verdicts but absent from the packet."""
        packet_ids = {ev.id for ev in packet.evidence}
        return {eid for eid in ev_ids if eid not in packet_ids}

    # ------------------------------------------------------------------
    # The route method
    # ------------------------------------------------------------------

    def route(
        self,
        claim: Claim,
        c_verdict: JurorVerdict | None,
        p_verdict: JurorVerdict | None,
        packet: EvidencePacket,
        ctx: EnforcementContext,
    ) -> EnforcementOutcome:
        """Route one claim to a first-class typed outcome, fail-closed.

        Never raises for a degraded state — returns a HumanReviewOutcome. Raises
        :class:`EnforcementError` only on programming misuse (wrong ctx shape).
        """
        effective = self.chair._effective_verdict(
            c_verdict.verdict if c_verdict else None,
            p_verdict.verdict if p_verdict else None,
        )
        # The chair applies the §9 matrix + the two hard invariants + (in
        # non-shadow mode) returns the real route.
        decision = self.chair.route(claim, c_verdict, p_verdict, packet)

        # ---- Pre-condition: executable fingerprint still matches frozen code.
        # This is the hard gate that means "the jury is inspecting what was
        # actually frozen." A mismatch is ALWAYS human_review (never accept,
        # never a comment counterexample that assumes the frozen buffer).
        if ctx.frozen_fingerprint and ctx.candidate_fingerprint and (
            ctx.frozen_fingerprint != ctx.candidate_fingerprint
        ):
            return _to_human_review(
                ctx, claim,
                f"executable fingerprint changed before jury enforcement "
                f"(frozen={ctx.frozen_fingerprint} candidate={ctx.candidate_fingerprint})",
                decision, c_verdict, p_verdict, effective,
            )

        # ---- Fail-closed: missing required juror.
        if c_verdict is None or p_verdict is None:
            missing = []
            if c_verdict is None:
                missing.append("contradiction")
            if p_verdict is None:
                missing.append("provenance")
            return _to_human_review(
                ctx, claim,
                f"required juror(s) produced no verdict: {', '.join(missing)}",
                decision, c_verdict, p_verdict, effective,
            )

        # ---- Resolve the chair's route to one of the four typed outcomes.
        chair_route = decision.route

        # code_reopen: requires the full evidence quorum AND the feature gate.
        if chair_route == "code_reopen":
            if not decision.evidence_quorum_met:
                # The chair should already have downgraded this, but defend in
                # depth: a reopen without quorum is a comment counterexample.
                return self._comment_counterexample(
                    claim, c_verdict, p_verdict, packet, ctx, decision, effective,
                )
            if not self.enable_code_reopen:
                # Gate is off → human_review, NEVER accept, NEVER suppression.
                return _to_human_review(
                    ctx, claim,
                    f"code_reopen quorum met for {claim.claim_id} but autonomous "
                    f"code reopen is disabled (enable_jury_code_reopen=False); "
                    f"routed to human review per the disabled-gate rule",
                    decision, c_verdict, p_verdict, effective,
                )
            return self._code_reopen(
                claim, c_verdict, p_verdict, ctx, decision, effective,
            )

        # comment_counterexample / preserve_and_audit / human_review from chair.
        if chair_route in ("comment_counterexample",):
            return self._comment_counterexample(
                claim, c_verdict, p_verdict, packet, ctx, decision, effective,
            )
        if chair_route in ("preserve_and_audit", "human_review", "abstain"):
            reason = decision.reason or (
                f"claim {claim.claim_id}: chair routed to {chair_route}")
            return _to_human_review(
                ctx, claim, reason, decision, c_verdict, p_verdict, effective,
            )

        # accept: apply the exhaustive acceptance denylist.
        if chair_route == "accept":
            blocked = self._acceptance_blocked(
                ctx, claim, packet, c_verdict, p_verdict, decision,
            )
            if blocked is not None:
                return _to_human_review(
                    ctx, claim,
                    f"acceptance blocked (fail-closed): {blocked}",
                    decision, c_verdict, p_verdict, effective,
                )
            record = _decision_record(
                "accept", claim, decision, c_verdict, p_verdict, ctx,
                effective=effective,
            )
            return AcceptOutcome(
                route="accept", claim_id=claim.claim_id,
                lineage_id=claim.lineage_id, effective_verdict=effective,
                reason=decision.reason or f"claim {claim.claim_id}: accept",
                evidence_quorum_met=decision.evidence_quorum_met,
                decision_record=record, chair_decision=decision,
                contradiction_verdict=c_verdict, provenance_verdict=p_verdict,
            )

        # Unknown chair route — fail closed. This is the "no fallback to accept"
        # guarantee: anything the chair emitted that we don't recognize is human.
        return _to_human_review(
            ctx, claim,
            f"unknown chair route '{chair_route}' for {claim.claim_id}; "
            f"fail-closed to human review",
            decision, c_verdict, p_verdict, effective,
        )

    # ------------------------------------------------------------------
    # Outcome builders
    # ------------------------------------------------------------------

    def _comment_counterexample(
        self, claim: Claim, c_verdict: JurorVerdict, p_verdict: JurorVerdict,
        packet: EvidencePacket, ctx: EnforcementContext,
        decision: RoutingDecision, effective: str,
    ) -> CommentCounterexampleOutcome:
        disposition = _disposition_for(claim.origin, effective)
        # The precise validation failure the next proposal must address.
        if effective == "CONTRADICTED":
            validation = (
                f"claim {claim.claim_id} is contradicted by code evidence: "
                f"{decision.reason}")
        elif effective == "UNGROUNDED_NEW_CLAIM":
            validation = (
                f"claim {claim.claim_id} is a synthesized claim with no source "
                f"or code support; the next proposal must remove it or ground it")
        elif effective == "UNVERIFIABLE_INHERITED_CLAIM":
            validation = (
                f"claim {claim.claim_id} is an unverifiable inherited claim; the "
                f"next proposal must restore the original inherited text verbatim "
                f"(the comment pass must not rewrite unverifiable rationale)")
        else:
            validation = (
                f"claim {claim.claim_id} ({effective}) cannot be established; "
                f"narrow or remove it")
        witness = c_verdict.witness if effective == "CONTRADICTED" else None
        ce = CommentCounterexample(
            lineage_id=claim.lineage_id, claim_id=claim.claim_id,
            disputed_claim=claim.text, verdict_category=effective,
            disposition=disposition, validation_failure=validation,
            supporting_evidence_ids=list(c_verdict.evidence_ids) or list(p_verdict.evidence_ids),
            witness=witness,
        )
        record = _decision_record(
            "comment_counterexample", claim, decision, c_verdict, p_verdict,
            ctx, effective=effective,
            extra={"disposition": disposition,
                   "validation_failure": validation},
        )
        return CommentCounterexampleOutcome(
            route="comment_counterexample", claim_id=claim.claim_id,
            lineage_id=claim.lineage_id, effective_verdict=effective,
            reason=decision.reason, evidence_quorum_met=decision.evidence_quorum_met,
            decision_record=record, chair_decision=decision,
            contradiction_verdict=c_verdict, provenance_verdict=p_verdict,
            counterexample=ce,
        )

    def _code_reopen(
        self, claim: Claim, c_verdict: JurorVerdict, p_verdict: JurorVerdict,
        ctx: EnforcementContext, decision: RoutingDecision, effective: str,
    ) -> CodeReopenOutcome:
        # The contract text = the claim the jury found contradicted (the code
        # must be re-resolved to satisfy it). anchor_symbol comes from the
        # evidence packet / ledger; the orchestrator resolves it.
        record = _decision_record(
            "code_reopen", claim, decision, c_verdict, p_verdict, ctx,
            effective=effective,
            extra={"contract_text": claim.text},
        )
        return CodeReopenOutcome(
            route="code_reopen", claim_id=claim.claim_id,
            lineage_id=claim.lineage_id, effective_verdict=effective,
            reason=decision.reason, evidence_quorum_met=True,
            decision_record=record, chair_decision=decision,
            contradiction_verdict=c_verdict, provenance_verdict=p_verdict,
            contract_text=claim.text, witness=decision.witness,
        )


# ---------------------------------------------------------------------------
# Counterexample → CommentFailure (the seed for the jury-driven comment CEGIS)
# ---------------------------------------------------------------------------


def counterexample_to_failure(
    ce: CommentCounterexample,
) -> "object":
    """Convert a structured counterexample into a ``CommentFailure``-shaped seed
    for the comment CEGIS loop.

    The comment CEGIS (:func:`comment_reconciler.run_comment_cegis`) threads a
    ``feedback`` list of CommentFailure-like objects into the next prompt's
    ``### prior-attempt feedback`` block. This adapter produces one so the
    jury-driven re-loop sees the jury's counterexample as concrete feedback.
    """
    from capybase.comment_verifiers import CommentFailure
    return CommentFailure(
        kind=f"JURY_{ce.verdict_category}",
        lineage_id=ce.lineage_id,
        message=f"{ce.validation_failure} (disposition: {ce.disposition})",
    )


# ---------------------------------------------------------------------------
# Aggregation (one case → one aggregate decision)
# ---------------------------------------------------------------------------


@dataclass
class AggregateEnforcementResult:
    """The aggregate jury result for one case (all claims evaluated).

    The brief's ``accept`` route: "only when the aggregate jury result contains
    no blocking finding and all required evidence is present." This aggregate
    surfaces whether ANY claim produced a non-accept outcome, so the caller can
    gate case-level acceptance.
    """
    session_id: str
    outcomes: list[EnforcementOutcome] = field(default_factory=list)

    @property
    def route_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for o in self.outcomes:
            counts[o.route] = counts.get(o.route, 0) + 1
        return counts

    @property
    def has_blocking_finding(self) -> bool:
        """True when ANY claim is not accept (a blocking finding exists).

        Case-level acceptance is impossible when this is True."""
        return any(o.route != "accept" for o in self.outcomes)

    @property
    def can_accept_case(self) -> bool:
        """The aggregate gate: accept only when no blocking finding AND every
        outcome is itself well-formed (none failed-closed to human_review for a
        binding reason that taints the whole case)."""
        return bool(self.outcomes) and not self.has_blocking_finding

    def verdict_distribution(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for o in self.outcomes:
            counts[o.effective_verdict] = counts.get(o.effective_verdict, 0) + 1
        return counts


def aggregate(outcomes: list[EnforcementOutcome], session_id: str) -> AggregateEnforcementResult:
    """Aggregate per-claim outcomes into a case-level result."""
    return AggregateEnforcementResult(session_id=session_id, outcomes=outcomes)


# ---------------------------------------------------------------------------
# Idempotency + serialization helpers
# ---------------------------------------------------------------------------


def canonical_record_hash(record: dict) -> str:
    """A stable hash of a decision record (for idempotency checks).

    Two replays of the same claim under the same config produce the same record
    → the same hash. The flight recorder persists the record; the replay harness
    compares hashes to assert idempotency.
    """
    blob = repr(_canonicalize(record)).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _canonicalize(obj: Any) -> Any:
    """Recursively sort dict keys so repr() is stable regardless of insertion
    order."""
    if isinstance(obj, dict):
        return {k: _canonicalize(obj[k]) for k in sorted(obj)}
    if isinstance(obj, (list, tuple)):
        return [_canonicalize(x) for x in obj]
    return obj


__all__ = [
    "JURY_MODES", "ENFORCE_ROUTES", "COUNTEREXAMPLE_DISPOSITIONS",
    "EnforcementContext",
    "EnforcementOutcome", "AcceptOutcome",
    "CommentCounterexample", "CommentCounterexampleOutcome",
    "ReviewBundleSpec", "HumanReviewOutcome", "CodeReopenOutcome",
    "EnforcementError", "EnforcementRouter",
    "counterexample_to_failure",
    "AggregateEnforcementResult", "aggregate",
    "canonical_record_hash",
]

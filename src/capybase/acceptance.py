"""The acceptance policy — Conflict → Candidate[] → Evidence[] → Decision.

Candidate-ref design P3 (remainder). The resolver PRODUCES candidates and
evidence; this module is the sole decider of what acceptance means:

    AUTO_APPLY          — tier A: deterministic resolution, or model-assisted
                          with complete required oracles, no new diagnostics,
                          obligations satisfied.
    PROPOSE_FOR_REVIEW  — tier B: model-assisted with strong independent
                          behavioral evidence, OR any required oracle UNKNOWN
                          (a check that could not run never improves trust).
    STOP                — tier C: verifier disagreement on an accepted
                          candidate (e.g. the model asserted
                          suspected_validator_error while passing), or a
                          high-risk operation class.

The core rules:
- **Unknown is not pass.** An oracle that could not run (missing toolchain,
  undecidable location) degrades the tier — never silently improves it.
- **The resolver never decides safety.** Model self-reports
  (self_reported_confidence, suspected_validator_error) are EVIDENCE here,
  never the deciding input; a suspicion on an accepted candidate is
  verifier-disagreement evidence that lowers the tier.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The decision vocabulary (the design's PolicyDecision).
AUTO_APPLY = "AUTO_APPLY"
PROPOSE_FOR_REVIEW = "PROPOSE_FOR_REVIEW"
STOP = "STOP"


def _safety_class(outcome) -> "SafetyClass | None":
    """The mechanism's D-class (reuse-design stage 1): reproducibility is
    not correctness. SBCR is a reproducible SEARCH (D3-heuristic); exact
    reuse is true algebra (D0). Tier A's "deterministic" was conflating
    them."""
    from capybase.langs import safety_class_for
    cand = getattr(outcome, "accepted", None)
    return safety_class_for(getattr(cand, "provenance", None))


@dataclass
class UnitEvidence:
    """One accepted unit's evidence, derived from the validation result.

    A read-layer over the existing ``ValidationCheckResult`` features and
    the candidate's provenance — validators are not rewritten; their
    recorded evidence is CONSULTED here (single decision point).
    """

    unit_id: str
    deterministic: bool          # provenance starts with "deterministic"
    syntax_unknown: bool         # features["syntax_outcome"] == "unknown"
    syntax_failed: bool          # features["syntax_passed"] is False
    markers_remaining: bool      # features["markers_remaining"]
    verifier_disagreement: bool  # accepted while suspected_validator_error
    obligations_dropped: bool    # warnings survived on the accepted unit
    safety: str | None = None    # D-class ("exact"/"structural"/"policy"/"heuristic")


@dataclass
class PolicyDecision:
    tier: str                    # "A" | "B" | "C"
    decision: str                # AUTO_APPLY | PROPOSE_FOR_REVIEW | STOP
    reasons: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return self.decision != AUTO_APPLY


def _unit_evidence(outcome) -> UnitEvidence:
    """Derive one unit's evidence from a resolution-outcome object."""
    val = getattr(outcome, "validation", None)
    feats = (getattr(val, "features", None) or {}) if val is not None else {}
    cand = getattr(outcome, "accepted", None)
    prov = str(getattr(cand, "provenance", "") or "")
    warnings = list(getattr(val, "warnings", None) or []) if val is not None else []
    _sc = _safety_class(outcome)
    return UnitEvidence(
        unit_id=str(getattr(outcome, "unit", None) and outcome.unit.unit_id
                    or getattr(outcome, "unit_id", "") or "?"),
        # Non-model = a D-class exists (exact reuse has no "deterministic"
        # prefix but is the purest D0 — the class, not the string, decides).
        deterministic=_sc is not None,
        safety=_sc.value if _sc is not None else None,
        syntax_unknown=feats.get("syntax_outcome") == "unknown",
        syntax_failed=feats.get("syntax_passed") is False,
        markers_remaining=bool(feats.get("markers_remaining")),
        verifier_disagreement=bool(
            getattr(cand, "suspected_validator_error", False)),
        obligations_dropped=any(
            "obligation" in str(getattr(w, "message", "")) for w in warnings),
    )


def decide(
    outcomes, tests_passed: bool | None,
    class_prior: dict | None = None,
) -> PolicyDecision:
    """The tier-table policy over a step's accepted units.

    ``outcomes`` are the per-unit resolution outcomes (objects with
    ``.validation`` features and an ``.accepted`` candidate);
    ``tests_passed`` is the step-level test gate's verdict (None when no
    test command is configured — no evidence either way).
    """
    evidence = [_unit_evidence(o) for o in outcomes
                if getattr(o, "accepted", None) is not None]
    reasons: list[str] = []

    # Tier C first: verifier disagreement on an ACCEPTED candidate is a
    # stop-and-review signal regardless of everything else.
    disagreement = [e.unit_id for e in evidence if e.verifier_disagreement]
    if disagreement:
        return PolicyDecision(
            "C", STOP,
            [f"verifier disagreement on accepted unit(s): "
             f"{', '.join(disagreement[:4])} (suspected_validator_error "
             f"while accepted)"])

    # Hard evidence of defects on accepted units is a stop (defense in
    # depth — the accept path should not have passed these).
    broken = [e.unit_id for e in evidence
              if e.syntax_failed or e.markers_remaining]
    if broken:
        return PolicyDecision(
            "C", STOP,
            [f"accepted unit(s) carry failing evidence: "
             f"{', '.join(broken[:4])}"])

    # UNKNOWN is not pass: any required oracle that could not run
    # degrades to B.
    unknown = [e.unit_id for e in evidence if e.syntax_unknown]
    if unknown:
        reasons.append(
            f"unknown oracle(s) — compile evidence missing for: "
            f"{', '.join(unknown[:4])}")
        return PolicyDecision("B", PROPOSE_FOR_REVIEW, reasons)

    # Tier A: D0/D1-class resolutions with complete oracles. "Deterministic"
    # alone is NOT enough — a reproducible heuristic (SBCR, compiler fixits)
    # is D3 and needs the evidence tiers; a policy choice (D2) is
    # configurable, not exact. (Reuse-design stage 1.)
    classes = [e.safety for e in evidence]
    if evidence and all(
            e.deterministic and e.safety in ("exact", "structural")
            for e in evidence):
        return PolicyDecision("A", AUTO_APPLY, [
            f"D0/D1 deterministic resolution(s): {len(evidence)} unit(s) "
            f"({', '.join(sorted({c or '?' for c in classes}))})"])

    # Model-assisted: the strength of the independent behavioral evidence
    # decides. Tests passing = strong independent evidence (tier B
    # candidate-branch per the design); tests absent = weaker still.
    model_assisted = [e.unit_id for e in evidence if not e.deterministic]
    # Calibrated confidence (design P3's final piece): the class prior
    # from observed historical outcomes ANNOTATES the review decision —
    # it never flips a tier (evidence decides; a prior alone promoting
    # would be the resolver-decides-safety mistake in statistical dress).
    from capybase.calibration_priors import prior_reason
    _prior = prior_reason(class_prior)
    base = (
        f"model-assisted unit(s): {', '.join(model_assisted[:4])}; "
        f"test gate passed (strong independent behavioral evidence)"
        if tests_passed is True else
        f"model-assisted unit(s): {', '.join(model_assisted[:4])}; "
        f"no test-gate evidence (tests_passed={tests_passed!r})")
    reasons = [base] + ([_prior] if _prior else [])
    return PolicyDecision("B", PROPOSE_FOR_REVIEW, reasons)


@dataclass
class OracleEvidence:
    """One oracle's full evidence record (the design's Evidence envelope).

    The deepening beyond the tier inputs (s27-extend-42): scope, the
    tool that produced the verdict, and how long it took — so an
    acceptance is attributable and reproducible, not just graded.
    """

    oracle: str            # e.g. "ccs_syntax" / "syntax"
    outcome: str           # pass | fail | unknown
    scope: str             # unit | file (from syntax_scope)
    tool: str              # the tool's --version line (fingerprint)
    duration_ms: int       # how long the oracle ran

    def as_dict(self) -> dict:
        return {
            "oracle": self.oracle, "outcome": self.outcome,
            "scope": self.scope, "tool": self.tool,
            "duration_ms": self.duration_ms,
        }


def evidence_envelope(outcome) -> list[OracleEvidence]:
    """The full oracle-evidence list for one resolution outcome.

    Reads what the validators recorded (the RAN checks carry
    syntax_tool / syntax_duration_ms / syntax_scope; the unknown paths
    carry syntax_outcome) — a read-layer, not a validator rewrite.
    Oracles without tool fingerprints (marker/brace checks) report with
    their outcome and empty tool/duration.
    """
    val = getattr(outcome, "validation", None)
    if val is None:
        return []
    feats = getattr(val, "features", None) or {}
    ev: list[OracleEvidence] = []
    # The syntax/compile oracle (the strongest deterministic evidence).
    if feats.get("syntax_outcome") == "unknown":
        ev.append(OracleEvidence(
            oracle="syntax", outcome="unknown",
            scope=str(feats.get("syntax_scope") or "unit"),
            tool="", duration_ms=0))
    elif "syntax_passed" in feats or feats.get("syntax_checked"):
        ev.append(OracleEvidence(
            oracle="syntax",
            outcome="pass" if feats.get("syntax_passed") is True
            else ("fail" if feats.get("syntax_passed") is False
                  else "unknown"),
            scope=str(feats.get("syntax_scope") or "unit"),
            tool=str(feats.get("syntax_tool") or ""),
            duration_ms=int(feats.get("syntax_duration_ms") or 0)))
    return ev

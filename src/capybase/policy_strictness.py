"""Acceptance-strictness policy (#10): how boldly capybase auto-accepts a merge.

A wrapper around :class:`capybase.risk.RiskEngine` that tightens the ``accept``
decision per the configured :class:`PolicyMode`. The same candidate may be
accepted in interactive mode (a human is at the terminal) but escalated in
unattended/CI mode (no human in the loop mid-step), with an explicit reason.

Modes (least → most cautious):
- ``interactive`` (default) / ``dry_run`` — pass-through: the wrapped engine's
  decision stands. Bold is fine; the fallback catches a bad one.
- ``ci`` — escalate anything the engine would accept that is NOT a deterministic
  merge or a high-confidence candidate.
- ``unattended`` — the strictest: accept ONLY a deterministic merge, or a
  candidate that clears ALL of: high self-reported confidence (≥ floor), no
  dropped obligations (#3), no introduced diagnostics (#7), no needs-human /
  low-confidence signal, and a classification band (#2) not in the escalate set.

The wrapper consumes signals already on the candidate / validation result, so it
adds no recomputation — it composes #2/#3/#7 into a single accept gate. The
wrapped engine still owns retry/escalate for failures; this layer only tightens
the ``accept`` branch (it never relaxes a retry/escalate). The orchestrator
calls :meth:`StrictnessPolicy.accept_pre_llm` on the deterministic pre-LLM paths
(structural/SBCR/block-capture), which the base engine never sees, so the mode
gates those too — closing the asymmetry where unattended mode would otherwise
auto-accept a deterministic merge the engine never judged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from capybase.conflict_model import CandidateResolution, VerificationResult
    from capybase.conflict_model import ConflictUnit


PolicyMode = Literal["interactive", "dry_run", "ci", "unattended"]


@dataclass
class StrictnessPolicy:
    """Wraps a RiskEngine, tightening the accept branch per ``mode``.

    Construct once from config; the orchestrator sets ``self.deterministic`` per
    candidate (whether a pre-LLM rule produced it) before calling
    :meth:`should_accept`. Inert in interactive/dry_run mode (returns the wrapped
    decision unchanged), so the default behavior is unaffected.
    """

    mode: PolicyMode
    min_confidence: float = 0.6
    escalate_bands: tuple[str, ...] = ("hard",)

    @property
    def strict(self) -> bool:
        """True iff this mode tightens acceptance (ci / unattended)."""
        return self.mode in ("ci", "unattended")

    @property
    def unattended(self) -> bool:
        return self.mode == "unattended"

    def accept_pre_llm(
        self,
        unit: "ConflictUnit",
        candidate: "CandidateResolution",
        validation: "VerificationResult",
        *,
        band: str | None = None,
    ) -> tuple[bool, str]:
        """Should a DETERMINISTIC pre-LLM resolution (structural/SBCR/block-capture)
        be accepted under this mode?

        Returns ``(accept, reason)``. The deterministic path already passed the
        full validation pipeline, so the only question is whether the MODE trusts
        a non-LLM resolution. In interactive/dry_run: always accept (it passed
        validation). In ci/unattended: still accept a deterministic merge (it's
        the strongest evidence — no model judgment involved), UNLESS it dropped a
        side obligation (#3) or introduced diagnostics (#7).
        """
        if not self.strict:
            return True, ""
        block_reason = self._block_reason(unit, candidate, validation, band)
        if block_reason:
            return False, block_reason
        return True, ""

    def should_accept(
        self,
        unit: "ConflictUnit",
        candidate: "CandidateResolution",
        validation: "VerificationResult",
        *,
        band: str | None = None,
        deterministic: bool = False,
    ) -> tuple[bool, str]:
        """Should an LLM-produced candidate be accepted under this mode?

        The wrapped engine has already decided ``accept`` (this is only called on
        the accept branch). In strict modes this may OVERRIDE to escalate.
        Returns ``(accept, reason)`` — ``reason`` is empty when accepted, the
        escalation rationale when overridden.
        """
        if not self.strict:
            return True, ""
        # A deterministic resolution is the strongest evidence — accept it on
        # the same terms as the pre-LLM path.
        if deterministic:
            block = self._block_reason(unit, candidate, validation, band)
            return (not bool(block), block)
        # ALL strict modes (ci + unattended) apply the shared block reasons
        # (dropped obligation / introduced diagnostic / needs-human / band).
        block = self._block_reason(unit, candidate, validation, band)
        if block:
            return False, block
        # ci mode: also gate on confidence. unattended adds the same floor (the
        # _block_reason band check already fired above; the confidence floor is
        # the extra unattended gate, but ci applies it too for caution).
        # SafetyClass exemption (reuse-design stage 2): D0/D1 candidates
        # don't need a model-opinion floor — their mechanism's exactness
        # IS their ticket. The floats on deterministic repairs (0.85/0.9)
        # were gaming this gate; the SafetyClass carries the real meaning.
        from capybase.langs import safety_class_for
        _sc = safety_class_for(getattr(candidate, "provenance", None))
        if _sc is not None and _sc.value in ("exact", "structural"):
            return True, ""  # D0/D1: exactness, not opinion, admits it
        conf = float(getattr(candidate, "self_reported_confidence", 0.0) or 0.0)
        if conf < self.min_confidence:
            # Deterministic confidence override: when the model's self-reported
            # confidence is low, check if deterministic signals (compiles,
            # intent coverage, line count) are strong enough to accept anyway.
            # This prevents false escalations of structurally-sound candidates
            # that the model under-rated (e.g. clickhouse-0041 where two valid
            # implementations exist). Safe: the compiler and all hard validators
            # already passed; this only relaxes the model-opinion floor.
            det_conf = _deterministic_confidence(unit, candidate, validation)
            if det_conf >= 0.8:
                return True, ""  # accept despite low self-reported confidence
            label = "unattended" if self.unattended else "ci"
            return False, f"{label} mode: confidence {conf:.2f} < floor {self.min_confidence:.2f} (det={det_conf:.2f})"
        return True, ""

    # ------------------------------------------------------------------ shared

    def _block_reason(
        self,
        unit: "ConflictUnit",
        candidate: "CandidateResolution",
        validation: "VerificationResult",
        band: str | None,
    ) -> str:
        """A reason to block acceptance in ANY strict mode (ci/unattended), or ''.

        Fires on: a dropped side obligation (#3), an introduced diagnostic (#7),
        a needs-human flag, or (unattended only) a band in the escalate set.
        """
        feats = getattr(validation, "features", {}) or {}
        if feats.get("dropped_obligation"):
            return "dropped a side obligation"
        if int(feats.get("introduced_diagnostics", 0) or 0) > 0:
            return f"introduced {feats['introduced_diagnostics']} new diagnostic(s)"
        if feats.get("model_needs_human"):
            return "model self-reported needs_human"
        if self.unattended and band in self.escalate_bands:
            return f"unattended mode: {band} conflict needs a human"
        return ""


def _deterministic_confidence(
    unit: "ConflictUnit",
    candidate: "CandidateResolution",
    validation: "VerificationResult",
) -> float:
    """Compute a deterministic confidence score in [0, 1] from candidate
    properties — NOT from the model's self-report.

    Signals (each adds to the score):
    - Candidate passed all hard checks (no hard failures): +0.3
    - Intent coverage > 0.9 (side-specific additions preserved): +0.3
    - Line count within 20% of expected: +0.2
    - No preservation/both-sides warnings: +0.1
    - No needs_human flag: +0.1

    A candidate scoring ≥ 0.8 has strong deterministic evidence of correctness
    and can be accepted even when the model's self-reported confidence is low.
    """
    score = 0.0
    feats = getattr(validation, "features", {}) or {}
    # 1. Passed all hard checks (compiler, syntax, scope)
    if validation.passed and not validation.hard_failures:
        score += 0.3
    # 2. Intent coverage > 0.9
    cur_ratio = feats.get("current_preservation_ratio")
    rep_ratio = feats.get("replayed_preservation_ratio")
    ratios = [r for r in (cur_ratio, rep_ratio) if isinstance(r, (int, float))]
    if ratios and min(ratios) > 0.9:
        score += 0.3
    elif not ratios:
        # No ratios computed (no entities added) — can't penalize for missing
        # signal. Give partial credit (the candidate didn't drop anything we
        # can measure).
        score += 0.15
    # 3. Line count within 20% of expected (max of both sides)
    resolved_lines = len((candidate.resolved_text or "").splitlines())
    cur_lines = len((unit.current.text or "").splitlines())
    rep_lines = len((unit.replayed.text or "").splitlines())
    expected = max(cur_lines, rep_lines, 1)
    if expected > 0 and abs(resolved_lines - expected) / expected <= 0.2:
        score += 0.2
    # 4. No preservation/both-sides warnings
    warning_validators = {w.validator for w in validation.warnings}
    if not warning_validators & {"preservation_heuristic", "both_sides_represented"}:
        score += 0.1
    # 5. No needs_human
    if not feats.get("model_needs_human") and not getattr(candidate, "needs_human", False):
        score += 0.1
    return min(score, 1.0)

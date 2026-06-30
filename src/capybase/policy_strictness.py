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
        conf = float(getattr(candidate, "self_reported_confidence", 0.0) or 0.0)
        if conf < self.min_confidence:
            label = "unattended" if self.unattended else "ci"
            return False, f"{label} mode: confidence {conf:.2f} < floor {self.min_confidence:.2f}"
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

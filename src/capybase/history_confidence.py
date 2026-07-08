"""History confidence — how trustworthy is the history context for a conflict?

History context can change decisions (future-apply probing, future obligations,
prompt augmentation), but not all history signals are equally reliable. A
future-region match found by precise diff-overlap should count far more than one
guessed from a commit subject string; a region key with a parsed kind and
structural hash is more trustworthy than ``kind == "unknown"``. This module
summarizes those signals into one :class:`HistoryConfidence` so the orchestrator
can avoid over-trusting weak history (e.g. refusing to re-stamp a candidate as
``history_augmented_llm`` when the only signal is a subject heuristic).

Pure: it reads a :class:`~capybase.history.HistoryContext` (+ optional probe
mode) and returns a score. No I/O, never raises — a missing context yields the
zero-score sentinel.

Score model
-----------
Five equally-weighted signals (each contributing 0.0–0.2, summing to 0.0–1.0):

- ``has_rebase_plan`` (0.2) — is there any plan at all?
- ``replay_identity_known`` (0.2) — do we know WHICH commit is replaying?
- ``region_key_quality`` (0.2) — high/medium/low from the RegionKey.
- ``future_region_detection_quality`` (0.2) — diff > heuristic > none.
- ``future_probe_quality`` (0.2) — sequence_patch > path_patch > none.

The weights are documented constants (no magic numbers inline); tuning them is
a one-line change here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from capybase.history import HistoryContext

RegionKeyQuality = Literal["low", "medium", "high"]
FutureRegionDetectionQuality = Literal["none", "heuristic", "diff"]
FutureProbeQuality = Literal["none", "path_patch", "sequence_patch"]

# Equal-weighted signals (sum to 1.0). Kept as named constants so the model is
# self-documenting and tunable in one place.
_W_PLAN = 0.2
_W_IDENTITY = 0.2
_W_REGION_KEY = 0.2
_W_DETECTION = 0.2
_W_PROBE = 0.2

# Sub-scores for the ordinal quality fields. ``none`` always = 0.
_REGION_KEY_SCORE: dict[str, float] = {"low": 0.0, "medium": 0.5, "high": 1.0}
_DETECTION_SCORE: dict[str, float] = {"none": 0.0, "heuristic": 0.35, "diff": 1.0}
_PROBE_SCORE: dict[str, float] = {"none": 0.0, "path_patch": 0.6, "sequence_patch": 1.0}

#: Confidence threshold for re-stamping an LLM candidate's provenance to
#: ``history_augmented_llm``. Below this, history may be present but isn't strong
#: enough to attribute the resolution to it. This is a DOCUMENTED CONSTANT, not a
#: config knob: history features are always-on and adaptive, and exposing this as
#: per-deploy config would invite mis-tuning (the 0.4 weight was chosen so a lone
#: subject-heuristic match qualifies but scores below a diff match). Tuning is a
#: one-line change here, surfaced in tests, not a hidden setting.
DEFAULT_AUGMENT_THRESHOLD = 0.4


@dataclass(frozen=True)
class HistoryConfidence:
    """A trust summary for one conflict's history context.

    ``score`` is a 0.0–1.0 blend of the five quality signals; the individual
    fields carry the *why* so reports can explain a low score rather than
    presenting an opaque number.
    """

    has_rebase_plan: bool
    replay_identity_known: bool
    region_key_quality: RegionKeyQuality
    future_region_detection_quality: FutureRegionDetectionQuality
    future_probe_quality: FutureProbeQuality
    score: float

    @property
    def is_augmenting(self) -> bool:
        """Whether history is strong enough to attribute a resolution to.

        True when score >= :data:`DEFAULT_AUGMENT_THRESHOLD` AND there's an
        actual future-region signal (a high score from probe/plan alone, with no
        future touches, should not re-stamp an LLM candidate).
        """
        return (
            self.score >= DEFAULT_AUGMENT_THRESHOLD
            and self.future_region_detection_quality != "none"
        )


@dataclass(frozen=True)
class HistoryDecisionContext:
    """One unit's complete history-decision snapshot (#idea 5 cohesion).

    Built ONCE per unit by the orchestrator and consumed by every downstream
    mechanism (prompt block, accept gate, future probe, features spine, recording,
    exact reuse). This replaces the scattered re-queries where ``for_conflict``
    ran ~4× per unit, the obligation patch-loop ~2×, and features 2×.

    Carries the memoized expensive results: the HistoryContext query, the derived
    confidence/region-kind/shape, the future obligations (with patches already
    fetched), the branch-intent excerpt, and the exact-reuse candidate. The
    non-bulky fields are journaled as one ``history_decision_snapshot`` event per
    unit — the single per-unit history-decision record.
    """

    unit_id: str
    context: "object | None" = None  # HistoryContext | None
    region_key_kind: str = ""
    conflict_shape: str = ""
    confidence: "object | None" = None  # HistoryConfidence | None
    future_obligations: "object | None" = None  # FutureObligations | None
    branch_intent_excerpt: str = ""
    exact_reuse: "object | None" = None  # ReuseCandidate | None
    reuse_match_reason: str = ""

    def to_journal_payload(self) -> dict:
        """The non-bulky fields for the ``history_decision_snapshot`` event.

        Excludes the large objects (the 3-way blobs, the full obligation list
        text) — those live in the candidate/validation artifacts. This is the
        per-unit audit record of what history decided.
        """
        obls = self.future_obligations
        return {
            "unit_id": self.unit_id,
            "region_key_kind": self.region_key_kind,
            "conflict_shape": self.conflict_shape,
            "confidence_score": round(self.confidence.score, 4)
            if self.confidence is not None else None,
            "is_augmenting": self.confidence.is_augmenting
            if self.confidence is not None else False,
            "future_obligation_count": len(obls.obligations)
            if obls is not None else 0,
            "future_obligation_required_symbols": sorted(obls.required_symbols)
            if obls is not None else [],
            "branch_intent_present": bool(self.branch_intent_excerpt),
            "exact_reuse_matched": self.exact_reuse is not None,
            "reuse_match_reason": self.reuse_match_reason,
        }


def _region_key_quality(ctx: "HistoryContext") -> RegionKeyQuality:
    """Derive region-key quality from the detection method + region touch count.

    We don't have the RegionKey object here (the HistoryContext doesn't carry
    it), but the detection method is a strong proxy: diff detection requires a
    concrete span (``start_line``/``end_line``), which only exists when tree-
    sititter ran and produced an enclosing-node span. The heuristic fallback
    runs precisely when the span is missing. So:
    - ``diff``  → high (span known + diff matched)
    - ``heuristic`` → medium (name known, but span missing → guessed)
    - ``none`` with region touches → medium (matched somehow, method not recorded)
    - ``none`` with no region touches → low (no region signal at all)
    """
    method = ctx.region_detection_method
    if method == "diff":
        return "high"
    if method == "heuristic":
        return "medium"
    # "none": if there are region touches but no method, we got them from a path
    # we don't fully trust; if there are none at all, the key quality is moot.
    if ctx.future_source_commits_touching_region:
        return "medium"
    return "low"


def history_confidence_for(
    ctx: "HistoryContext | None",
    *,
    probe_mode_used: str | None = None,
) -> HistoryConfidence:
    """Score the trustworthiness of ``ctx``.

    ``probe_mode_used`` is the future-apply probe mode that actually ran (or will
    run) for this conflict: ``"path_patch"`` / ``"sequence_patch"`` / ``None``.
    Pass ``None`` when no probe ran (the common case — probes only fire when
    there are future region touches). Returns the zero-score sentinel when
    ``ctx`` is None (no history service / no plan).
    """
    if ctx is None or ctx.current_replay_commit is None:
        return HistoryConfidence(
            has_rebase_plan=False,
            replay_identity_known=False,
            region_key_quality="low",
            future_region_detection_quality="none",
            future_probe_quality="none",
            score=0.0,
        )

    has_plan = ctx.source_commit_count > 0
    identity_known = ctx.source_commit_index is not None
    detection = ctx.region_detection_method or "none"
    if detection not in _DETECTION_SCORE:
        detection = "none"
    probe = _probe_quality(probe_mode_used)

    rk_quality = _region_key_quality(ctx)

    score = (
        (_W_PLAN if has_plan else 0.0)
        + (_W_IDENTITY if identity_known else 0.0)
        + _W_REGION_KEY * _REGION_KEY_SCORE[rk_quality]
        + _W_DETECTION * _DETECTION_SCORE[detection]
        + _W_PROBE * _PROBE_SCORE[probe]
    )
    # Clamp against floating-point drift.
    score = max(0.0, min(1.0, score))

    return HistoryConfidence(
        has_rebase_plan=has_plan,
        replay_identity_known=identity_known,
        region_key_quality=rk_quality,
        future_region_detection_quality=detection,  # type: ignore[arg-type]
        future_probe_quality=probe,  # type: ignore[arg-type]
        score=score,
    )


def _probe_quality(probe_mode_used: str | None) -> FutureProbeQuality:
    """Map a probe mode string to the confidence quality enum."""
    if probe_mode_used == "sequence_patch":
        return "sequence_patch"
    if probe_mode_used == "path_patch":
        return "path_patch"
    return "none"

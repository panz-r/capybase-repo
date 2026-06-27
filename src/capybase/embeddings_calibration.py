"""Statistical calibration of the embedding-retrieval similarity floor.

Companion to :mod:`capybase.probes` (which probes whether the embeddings
*endpoint works*) and :mod:`capybase.calibration_profile` (which stores the
result). This module derives a model-specific ``min_similarity`` threshold from
the measured score distribution, replacing the 0.35 class-constant guess with a
statistically-grounded value.

Method (per the design: quantile-gap):
- Embed the corpus's ``(query, related, unrelated)`` triples.
- Compute cosine similarity for each related pair and each unrelated pair → two
  score distributions.
- The applied threshold is the midpoint of the LARGEST GAP between the related
  and unrelated sorted-score arrays — the natural "valley" separating the two
  classes. Robust to model scale (different embedding models produce different
  cosine magnitudes) and to corpus bias (it adapts to wherever the model places
  the separation, not an absolute number).
- Two reference estimates are also computed for the report:
  ``related_p10`` (10th percentile of related — conservative-keep) and
  ``unrelated_p90`` (90th percentile of unrelated — conservative-reject).

Never raises: a failed/unavailable endpoint yields a calibration with ``ok=False``
so the caller keeps the default floor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from capybase.embeddings_corpus import SimilarityProbe, probes
from capybase.memory.retriever import _cosine
from capybase.stats import (
    huber_isotonic_fit,
    isotonic_fit,
    ks_stat,
    mad as _mad,
    median as _median,
    percentile as _percentile,
)


# The default floor the EmbeddingRetriever ships with — used as the fallback
# when calibration can't run, and as the baseline the report compares against.
DEFAULT_MIN_SIMILARITY = 0.35

# Multiplier for the MAD-based zone thresholds (survey 2 §4.1): a zone boundary
# sits k robust-σ from the class median. k=2.5 ≈ the ~99% band under mild
# assumptions, balancing precision (green) against recall (red).
_ZONE_K = 2.5

# A fit residual exceeding this many robust-σ flags a (likely mislabeled) probe,
# triggering the Huber-loss refit (survey 2 §3.1).
_NOISE_OUTLIER_C = 1.345


@dataclass(frozen=True)
class ScoreDistribution:
    """Summary statistics of one class's measured similarity scores.

    The robust ``median``/``mad`` (survey 2 §4.1) are recorded alongside the
    classic min/max/mean so drift detection can compare distributions on
    robust statistics and the report can show dispersion that ignores outliers.
    """

    count: int
    minimum: float
    maximum: float
    mean: float
    median: float = 0.0
    mad: float = 0.0

    def to_dict(self) -> dict:
        return {
            "count": self.count,
            "min": round(self.minimum, 4),
            "max": round(self.maximum, 4),
            "mean": round(self.mean, 4),
            "median": round(self.median, 4),
            "mad": round(self.mad, 4),
        }


@dataclass(frozen=True)
class EmbeddingCalibration:
    """The result of a calibration run.

    ``min_similarity`` is the applied threshold (what the retriever should use) —
    kept equal to ``red_threshold`` for backward-compat (the retriever floor is
    unchanged in meaning). ``quantile_gap`` / ``related_p10`` / ``unrelated_p90``
    are the three estimates; the report shows all three for transparency.

    ``isotonic_points`` is the fitted score-calibration transform (survey §2.1):
    a monotone stepwise map from raw cosine to a model-agnostic calibrated scale,
    stored as ``(raw, calibrated)`` breakpoints. The three-zone thresholds
    (``green``/``amber``/``red``, survey §3.2) are derived on that calibrated
    scale — green/amber are advisory (journaled), red is the hard filter floor.

    ``related`` and ``unrelated`` are the measured score distributions. ``ok`` is
    False when the endpoint was unreachable or produced no valid scores (caller
    keeps the default floor).
    """

    model: str
    min_similarity: float
    quantile_gap: float
    related_p10: float
    unrelated_p90: float
    related: ScoreDistribution
    unrelated: ScoreDistribution
    ok: bool
    probed_at: str
    notes: list[str] = field(default_factory=list)
    # Score-calibration transform + three-zone thresholds (survey §2.1, §3.2).
    # Empty when calibration fell back to quantile-gap-only (older profiles load
    # with these defaults — backward-compatible).
    isotonic_points: list[tuple[float, float]] = field(default_factory=list)
    green_threshold: float = 0.0
    amber_threshold: float = 0.0
    red_threshold: float = 0.0
    # KS separation on the calibrated scale: 1.0 = classes fully separated, 0.0
    # = identical. A fit-quality signal for the report.
    ks_separation: float = 0.0
    # Robust-estimator provenance (survey 2 §3.1, §4.1). ``fit_loss`` records
    # whether the isotonic transform used L2 (default) or Huber (selected when
    # label noise was detected). ``zone_method`` records whether the zones used
    # MAD (default, robust) or fell back to percentile (MAD=0 degeneracy).
    # ``related_mad``/``unrelated_mad`` expose the robust scales for drift.
    fit_loss: str = "l2"
    zone_method: str = "mad"
    related_mad: float = 0.0
    unrelated_mad: float = 0.0

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "min_similarity": round(self.min_similarity, 4),
            "estimates": {
                "quantile_gap": round(self.quantile_gap, 4),
                "related_p10": round(self.related_p10, 4),
                "unrelated_p90": round(self.unrelated_p90, 4),
            },
            "related": self.related.to_dict(),
            "unrelated": self.unrelated.to_dict(),
            "ok": self.ok,
            "probed_at": self.probed_at,
            "notes": list(self.notes),
            # The fitted score-calibration transform (survey §2.1).
            "isotonic_points": [
                [round(r, 4), round(c, 4)] for r, c in self.isotonic_points
            ],
            # Three-zone thresholds on the calibrated scale (survey §3.2).
            "zones": {
                "green": round(self.green_threshold, 4),
                "amber": round(self.amber_threshold, 4),
                "red": round(self.red_threshold, 4),
            },
            "ks_separation": round(self.ks_separation, 4),
            # Robust-estimator provenance (survey 2 §3.1, §4.1).
            "fit_loss": self.fit_loss,
            "zone_method": self.zone_method,
            "related_mad": round(self.related_mad, 4),
            "unrelated_mad": round(self.unrelated_mad, 4),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EmbeddingCalibration":
        """Reconstruct from a serialized envelope (``to_dict`` round-trip).

        Tolerant: missing/additive keys default gracefully so older envelopes
        (pre-isotonic) still load. ``isotonic_points`` may be stored as a list
        of ``[raw, cal]`` pairs (JSON has no tuples).
        """
        estimates = d.get("estimates", {}) or {}
        zones = d.get("zones", {}) or {}
        raw_pts = d.get("isotonic_points", []) or []
        # Accept either tuple/list pairs; coerce to (float, float).
        iso_pts: list[tuple[float, float]] = []
        for pair in raw_pts:
            try:
                iso_pts.append((float(pair[0]), float(pair[1])))
            except (TypeError, IndexError, ValueError):
                continue
        return cls(
            model=str(d.get("model", "")),
            min_similarity=float(d.get("min_similarity", DEFAULT_MIN_SIMILARITY)),
            quantile_gap=float(estimates.get("quantile_gap", DEFAULT_MIN_SIMILARITY)),
            related_p10=float(estimates.get("related_p10", DEFAULT_MIN_SIMILARITY)),
            unrelated_p90=float(estimates.get("unrelated_p90", DEFAULT_MIN_SIMILARITY)),
            related=_distribution_from_dict(d.get("related", {})),
            unrelated=_distribution_from_dict(d.get("unrelated", {})),
            ok=bool(d.get("ok", False)),
            probed_at=str(d.get("probed_at", "")),
            notes=list(d.get("notes", []) or []),
            isotonic_points=iso_pts,
            green_threshold=float(zones.get("green", 0.0)),
            amber_threshold=float(zones.get("amber", 0.0)),
            red_threshold=float(zones.get("red", 0.0)),
            ks_separation=float(d.get("ks_separation", 0.0)),
            fit_loss=str(d.get("fit_loss", "l2") or "l2"),
            zone_method=str(d.get("zone_method", "mad") or "mad"),
            related_mad=float(d.get("related_mad", 0.0)),
            unrelated_mad=float(d.get("unrelated_mad", 0.0)),
        )

    @property
    def has_isotonic_fit(self) -> bool:
        """True when an isotonic score-calibration transform was fit."""
        return bool(self.isotonic_points)

    def calibrated_score(self, raw: float) -> float:
        """Apply the isotonic transform to a raw cosine.

        Returns ``raw`` unchanged when no fit exists (degrades to identity, so
        the floor is evaluated on the raw scale exactly as before isotonic).
        """
        if not self.has_isotonic_fit:
            return raw
        return _apply_isotonic(self.isotonic_points, raw)


# ``_percentile`` is imported from :mod:`capybase.stats` (shared numerics).


def _largest_gap_threshold(related: list[float], unrelated: list[float]) -> float:
    """The midpoint of the largest separation between the two score distributions.

    A good embedding model places related pairs high and unrelated pairs low,
    ideally with empty space between them. This finds the largest gap in the
    merged sorted score sequence that separates the two classes — the natural
    decision boundary. Robust to scale and to a few outliers (it looks for the
    widest *class-switching* gap, not a single adjacency).

    Algorithm: merge and sort all scores with their class tag. Walk the sequence;
    whenever the class changes (r→u or u→r), the gap between the two adjacent
    values is a candidate boundary. The LARGEST such gap is where the two
    distributions are most separated — the threshold goes at its midpoint. If the
    classes are well-separated, this is the empty space between them; if they
    overlap heavily, the gaps shrink (correctly signaling a weak model).
    """
    if not related or not unrelated:
        if related:
            return _percentile(sorted(related), 50)
        if unrelated:
            return _percentile(sorted(unrelated), 50)
        return DEFAULT_MIN_SIMILARITY
    merged = [(s, "r") for s in related] + [(s, "u") for s in unrelated]
    merged.sort(key=lambda t: t[0])
    best_gap = 0.0
    # Default: midpoint of the two medians (a reasonable cut when no switch-gap).
    best_mid = (_percentile(sorted(related), 50) + _percentile(sorted(unrelated), 50)) / 2.0
    for i in range(len(merged) - 1):
        lo_val, lo_tag = merged[i]
        hi_val, hi_tag = merged[i + 1]
        if lo_tag != hi_tag:  # a class-switching boundary
            gap = hi_val - lo_val
            if gap > best_gap:
                best_gap = gap
                best_mid = (lo_val + hi_val) / 2.0
    return best_mid


def _distribution(scores: list[float]) -> ScoreDistribution:
    if not scores:
        return ScoreDistribution(0, 0.0, 0.0, 0.0)
    return ScoreDistribution(
        count=len(scores),
        minimum=min(scores),
        maximum=max(scores),
        mean=sum(scores) / len(scores),
        median=_median(scores),
        mad=_mad(scores),
    )


def _distribution_from_dict(d: dict | None) -> ScoreDistribution:
    """Reconstruct a ScoreDistribution from its serialized form (tolerant).

    Older envelopes (pre-robust) omit median/mad — they default to 0.0.
    """
    if not d:
        return ScoreDistribution(0, 0.0, 0.0, 0.0)
    try:
        return ScoreDistribution(
            count=int(d.get("count", 0)),
            minimum=float(d.get("min", 0.0)),
            maximum=float(d.get("max", 0.0)),
            mean=float(d.get("mean", 0.0)),
            median=float(d.get("median", 0.0)),
            mad=float(d.get("mad", 0.0)),
        )
    except (TypeError, ValueError):
        return ScoreDistribution(0, 0.0, 0.0, 0.0)


def _apply_isotonic(points: list[tuple[float, float]], x: float) -> float:
    """Evaluate a serialized isotonic transform (its breakpoints) at ``x``.

    A thin wrapper that rebuilds the callable from the stored breakpoints and
    applies it. Constant extrapolation outside the fitted range (matches
    :func:`capybase.stats.isotonic_fit`).
    """
    if not points:
        return x
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return isotonic_fit(xs, ys)(x)


def calibrate_thresholds(
    client: object,
    embeddings_model: str = "",
) -> EmbeddingCalibration:
    """Derive a model-specific ``min_similarity`` from the corpus score distribution.

    ``client`` is an :class:`~capybase.memory.embeddings.EmbeddingsClient` (the
    ``embed`` method); ``embeddings_model`` is recorded in the envelope for
    traceability. Never raises — a failed endpoint yields ``ok=False`` with the
    default floor so the caller keeps working.
    """
    corpus = probes()
    notes: list[str] = []
    # Collect all texts to embed in one batch (queries + related + unrelated).
    texts: list[str] = []
    index_map: list[tuple[int, str]] = []  # (corpus_index, role)
    for i, p in enumerate(corpus):
        texts.append(p.query)
        index_map.append((i, "query"))
        texts.append(p.related)
        index_map.append((i, "related"))
        texts.append(p.unrelated)
        index_map.append((i, "unrelated"))

    try:
        vectors = client.embed(texts)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - unreachable endpoint → keep default
        notes.append(f"embeddings request failed: {exc}")
        return _failed(embeddings_model, notes)

    if not vectors or len(vectors) != len(texts):
        notes.append(
            f"embeddings count mismatch: requested {len(texts)}, got "
            f"{len(vectors) if vectors else 0}"
        )
        return _failed(embeddings_model, notes)

    # Group vectors by (corpus_index, role).
    vec_by_role: dict[tuple[int, str], list[float]] = {}
    for vec, (i, role) in zip(vectors, index_map):
        vec_by_role[(i, role)] = vec

    # Compute per-probe cosine similarities.
    related_scores: list[float] = []
    unrelated_scores: list[float] = []
    for i, _ in enumerate(corpus):
        q = vec_by_role.get((i, "query"))
        r = vec_by_role.get((i, "related"))
        u = vec_by_role.get((i, "unrelated"))
        if q is None or r is None or u is None:
            continue
        related_scores.append(_cosine(q, r))
        unrelated_scores.append(_cosine(q, u))

    if not related_scores or not unrelated_scores:
        notes.append("no valid similarity pairs produced from the corpus")
        return _failed(embeddings_model, notes)

    related_sorted = sorted(related_scores)
    unrelated_sorted = sorted(unrelated_scores)
    gap_threshold = _largest_gap_threshold(related_scores, unrelated_scores)
    related_p10 = _percentile(related_sorted, 10)
    unrelated_p90 = _percentile(unrelated_sorted, 90)

    # The applied threshold is the quantile-gap estimate. Clamp to [0, 1]; if the
    # distributions badly overlap (gap < 0), fall back to the more conservative
    # of the two reference estimates so we don't admit noise. This stays on the
    # RAW scale — the meaning of ``min_similarity`` is unchanged from before the
    # isotonic work, so existing retrievers/tests keep working byte-identically.
    applied = max(0.0, min(1.0, gap_threshold))
    if applied <= 0.0:
        # Distributions overlap entirely — use the stricter of the two references
        # (highest unrelated score is the safe cut when there's no clear gap).
        applied = max(unrelated_p90, 0.0)
        notes.append(
            "related/unrelated distributions overlap; using unrelated_p90 as a "
            "conservative floor (this model may be too weak for reliable RAG)"
        )

    # Score calibration (survey §2.1): fit an isotonic transform mapping raw
    # cosines onto a model-agnostic [0,1] scale, then derive three-zone
    # thresholds on that calibrated scale (survey §3.2). This is ADDITIVE to the
    # raw-scale floor above — ``min_similarity`` keeps its old meaning, and the
    # zones/isotonic transform are the new, richer capability. The fit needs both
    # classes non-empty (guaranteed here: we returned _failed above otherwise).
    iso_points: list[tuple[float, float]] = []
    green_t = amber_t = red_t = 0.0
    ks_sep = 0.0
    fit_loss = "l2"
    zone_method = "mad"
    try:
        fit_xs = related_scores + unrelated_scores
        fit_ys = [1.0] * len(related_scores) + [0.0] * len(unrelated_scores)
        # First the L2 (squared-loss) isotonic fit.
        cal_f = isotonic_fit(fit_xs, fit_ys)
        # Label-noise detection (survey 2 §3.1): if any probe's L2 residual is a
        # robust outlier (> _NOISE_OUTLIER_C robust-σ), refit under Huber loss so
        # the mislabeled probe has bounded influence on the curve.
        l2_pts = list(getattr(cal_f, "isotonic_points", []))
        if l2_pts:
            l2_fitted = [cal_f(x) for x in fit_xs]
            residuals = [fit_ys[i] - l2_fitted[i] for i in range(len(fit_xs))]
            from capybase.stats import mad_scaled

            resid_scale = mad_scaled(residuals)
            if resid_scale > 0 and any(
                abs(r) > _NOISE_OUTLIER_C * resid_scale for r in residuals
            ):
                cal_f = huber_isotonic_fit(fit_xs, fit_ys)
                fit_loss = "huber"
                notes.append(
                    "label noise detected (large isotonic residual); refit under "
                    "Huber loss for bounded-influence calibration (survey 2 §3.1)"
                )
        iso_points = list(getattr(cal_f, "isotonic_points", []))
        if iso_points:
            cal_related = [cal_f(s) for s in related_scores]
            cal_unrelated = [cal_f(s) for s in unrelated_scores]
            # Zone derivation on the calibrated scale (related -> ~1.0, unrelated
            # -> ~0.0). MAD-based thresholds (survey 2 §4.1): a zone boundary
            # sits k·MAD from the class median — robust (50% breakdown) so a few
            # outliers don't move it. Falls back to percentile zones when MAD=0
            # (degenerate, e.g. a constant-vector model).
            mad_rel = _mad(cal_related)
            mad_unrel = _mad(cal_unrelated)
            if mad_rel > 0 and mad_unrel > 0:
                med_rel = _median(cal_related)
                med_unrel = _median(cal_unrelated)
                green_t = med_unrel + _ZONE_K * mad_unrel
                red_t = med_rel - _ZONE_K * mad_rel
                amber_t = (green_t + red_t) / 2.0
                zone_method = "mad"
            else:
                # MAD=0 degeneracy: fall back to percentile zones (survey §3.2).
                green_t = _percentile(sorted(cal_unrelated), 95)
                red_t = _percentile(sorted(cal_related), 5)
                amber_t = (green_t + red_t) / 2.0
                zone_method = "percentile"
            ks_sep = ks_stat(sorted(cal_related), sorted(cal_unrelated))
            if ks_sep <= 0.0:
                # No calibrated separation either — drop the fit, keep raw floor.
                iso_points = []
                notes.append(
                    "isotonic fit produced no class separation on the calibrated "
                    "scale; keeping the raw quantile-gap floor only"
                )
    except Exception as exc:  # noqa: BLE001 - calibration is best-effort
        notes.append(f"isotonic fit failed ({exc}); keeping the raw floor only")
        iso_points = []

    return EmbeddingCalibration(
        model=embeddings_model,
        min_similarity=applied,
        quantile_gap=gap_threshold,
        related_p10=related_p10,
        unrelated_p90=unrelated_p90,
        related=_distribution(related_scores),
        unrelated=_distribution(unrelated_scores),
        ok=True,
        probed_at=datetime.now(timezone.utc).isoformat(),
        notes=notes,
        isotonic_points=iso_points,
        green_threshold=green_t,
        amber_threshold=amber_t,
        red_threshold=red_t,
        ks_separation=ks_sep,
        fit_loss=fit_loss,
        zone_method=zone_method,
        related_mad=_mad(related_scores) if related_scores else 0.0,
        unrelated_mad=_mad(unrelated_scores) if unrelated_scores else 0.0,
    )


def _failed(model: str, notes: list[str]) -> EmbeddingCalibration:
    """A calibration result for an unreachable/failed endpoint (keeps the default)."""
    empty = ScoreDistribution(0, 0.0, 0.0, 0.0)
    return EmbeddingCalibration(
        model=model,
        min_similarity=DEFAULT_MIN_SIMILARITY,
        quantile_gap=DEFAULT_MIN_SIMILARITY,
        related_p10=DEFAULT_MIN_SIMILARITY,
        unrelated_p90=DEFAULT_MIN_SIMILARITY,
        related=empty,
        unrelated=empty,
        ok=False,
        probed_at=datetime.now(timezone.utc).isoformat(),
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Offline drift detection (survey 2 §7) — compare a new calibration against the
# stored baseline. Pure summary-statistic comparison (no raw scores persisted).
# ---------------------------------------------------------------------------

# Drift thresholds (conservative — drift is advisory, never blocks a write).
# A median shift is "large" relative to the baseline's own robust spread.
_MEDIAN_SHIFT_K = 2.0
# A scale (MAD) change outside this ratio band signals dispersion drift.
_MAD_RATIO_BAND = (0.5, 2.0)
# A KS-separation swing larger than this flags a class-separation change.
_KS_DELTA = 0.15


@dataclass(frozen=True)
class DriftReport:
    """The result of comparing a new calibration against the stored baseline.

    Offline-only (survey 2 §7): computed at calibrate-embeddings time against the
    previous run's envelope. ``drifted`` is advisory — it never blocks writing the
    new calibration; it surfaces that the model's score behavior changed enough
    to warrant attention (the floor/zones the retriever now uses may differ).
    """

    drifted: bool
    reasons: list[str] = field(default_factory=list)
    # Hodges-Lehmann-style location shift (survey 2 §4.3): how far the related
    # distribution's median moved, in absolute calibrated-scale units.
    related_median_shift: float = 0.0
    unrelated_median_shift: float = 0.0
    # MAD ratio (current/baseline): 1.0 = no dispersion change.
    related_mad_ratio: float = 1.0
    unrelated_mad_ratio: float = 1.0
    # KS-separation change (current - baseline): positive = better separated.
    ks_separation_delta: float = 0.0

    def to_dict(self) -> dict:
        return {
            "drifted": self.drifted,
            "reasons": list(self.reasons),
            "related_median_shift": round(self.related_median_shift, 4),
            "unrelated_median_shift": round(self.unrelated_median_shift, 4),
            "related_mad_ratio": round(self.related_mad_ratio, 4),
            "unrelated_mad_ratio": round(self.unrelated_mad_ratio, 4),
            "ks_separation_delta": round(self.ks_separation_delta, 4),
        }


def _mad_ratio(current: float, baseline: float) -> float:
    """Safe MAD ratio (current/baseline); 1.0 when baseline is zero/absent."""
    if baseline <= 0:
        return 1.0 if current <= 0 else float("inf")
    return current / baseline


def _median_shift(current: ScoreDistribution, baseline: ScoreDistribution) -> float:
    """Absolute median shift between two distributions (0.0 if either is empty)."""
    if current.count == 0 or baseline.count == 0:
        return 0.0
    return current.median - baseline.median


def compare_calibration(
    current: "EmbeddingCalibration", baseline: "EmbeddingCalibration"
) -> DriftReport:
    """Compare a new calibration against the stored baseline (survey 2 §7).

    Uses summary statistics (median/MAD/KS-separation) — the envelope persists
    these, not raw score lists, to avoid bloat. A drift signal fires when the
    related/unrelated distributions' location or dispersion moved meaningfully,
    or the class separation changed. Pure advisory; never raises (a degenerate
    baseline yields ``drifted=False`` with empty reasons).
    """
    reasons: list[str] = []
    rel_shift = _median_shift(current.related, baseline.related)
    unrel_shift = _median_shift(current.unrelated, baseline.unrelated)
    rel_ratio = _mad_ratio(current.related_mad, baseline.related_mad)
    unrel_ratio = _mad_ratio(current.unrelated_mad, baseline.unrelated_mad)
    ks_delta = current.ks_separation - baseline.ks_separation

    # Location drift: a median shift beyond k·(baseline robust spread). The
    # baseline's own MAD (scaled to σ) is the yardstick.
    for name, shift, base_mad in (
        ("related", rel_shift, baseline.related_mad),
        ("unrelated", unrel_shift, baseline.unrelated_mad),
    ):
        yardstick = max(1.345 * base_mad, 1e-3)
        if abs(shift) > _MEDIAN_SHIFT_K * yardstick:
            reasons.append(
                f"{name} score median shifted {shift:+.3f} (> {_MEDIAN_SHIFT_K}·"
                f"baseline-σ {yardstick:.3f})"
            )

    # Dispersion drift: MAD ratio outside the band.
    lo, hi = _MAD_RATIO_BAND
    for name, ratio in (("related", rel_ratio), ("unrelated", unrel_ratio)):
        if ratio != float("inf") and (ratio < lo or ratio > hi):
            reasons.append(
                f"{name} score dispersion changed (MAD ratio {ratio:.2f} outside "
                f"[{lo}, {hi}])"
            )
        elif ratio == float("inf"):
            reasons.append(f"{name} score dispersion emerged (baseline MAD was 0)")

    # Separation drift: KS-separation swing.
    if abs(ks_delta) > _KS_DELTA:
        direction = "improved" if ks_delta > 0 else "degraded"
        reasons.append(
            f"class separation {direction} (KS Δ {ks_delta:+.3f}, > ±{_KS_DELTA})"
        )

    return DriftReport(
        drifted=bool(reasons),
        reasons=reasons,
        related_median_shift=rel_shift,
        unrelated_median_shift=unrel_shift,
        related_mad_ratio=rel_ratio,
        unrelated_mad_ratio=unrel_ratio,
        ks_separation_delta=ks_delta,
    )

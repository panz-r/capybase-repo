"""Shared pure-stdlib numerical helpers.

The codebase runs with zero runtime numeric dependencies (no numpy/scipy/sklearn)
to stay portable across small/local model servers. The statistics the calibration
and retrieval layers need are simple enough to implement by hand and live here so
both ``calibration.py`` (the LLM risk classifier) and ``embeddings_calibration.py``
(the retriever floor) share a single source of truth rather than each keeping a
private copy.

Everything here is pure-Python over plain ``list[float]`` and never raises on
degenerate input — the "never-crash-on-a-bad-distribution" contract that the
calibration layer depends on.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Callable

__all__ = [
    "percentile",
    "sigmoid",
    "isotonic_fit",
    "isotonic_points",
    "ks_stat",
    "median",
    "mad",
    "mad_scaled",
    "trimmed",
    "huber_loss",
    "huber_isotonic_fit",
    "hodges_lehmann",
    "trimmed_ks",
]


def percentile(sorted_scores: list[float], p: float) -> float:
    """The ``p``-th percentile (0..100) of a **sorted-ascending** score list.

    Linear interpolation between closest ranks (the numpy default). Returns 0.0
    for an empty list so callers don't crash on a degenerate corpus, and is the
    identity for a single element.
    """
    if not sorted_scores:
        return 0.0
    if len(sorted_scores) == 1:
        return sorted_scores[0]
    # Rank, 1-indexed, interpolated.
    rank = (p / 100.0) * (len(sorted_scores) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return sorted_scores[lo]
    frac = rank - lo
    return sorted_scores[lo] + (sorted_scores[hi] - sorted_scores[lo]) * frac


def sigmoid(z: float) -> float:
    """Numerically stable logistic sigmoid.

    Branches on the sign of ``z`` so the ``exp`` argument is always ≤ 0 — avoids
    overflow for large-magnitude ``z``. The calibrated-risk model (logistic /
    conformal) routes its dot product through here.
    """
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


# ---------------------------------------------------------------------------
# Isotonic regression (Pool-Adjacent-Violators) —
# ---------------------------------------------------------------------------


def isotonic_fit(
    xs: list[float], ys: list[float]
) -> Callable[[float], float]:
    """Fit a monotone-nondecreasing stepwise function ``f(x)`` to ``(xs, ys)``.

    Isotonic regression (PAV — pool-adjacent-violators) is a non-parametric,
    order-preserving calibration: it never reorders the inputs, so the ranking
    and nearest-neighbor structure of raw similarity scores is preserved — only
    the *meaning* of score magnitudes changes. That makes it ideal for mapping a
    raw cosine (whose useful range is model-specific) onto a common, human-aligned
    scale.

    Returns a callable ``f(x) -> float``. For ``x`` outside the fitted range it
    clamps to the nearest endpoint value (constant extrapolation). On degenerate
    input (empty, length-mismatched, or a single point) it returns a constant
    function so downstream thresholding degrades rather than crashes.

    The fitted function is a monotone step function; we keep its breakpoints as
    ``isotonic_points`` (``list[(x, y)]``) for serialization/replay elsewhere.
    """
    n = len(xs)
    if n == 0 or n != len(ys):
        # Degenerate: return a constant 0 (a no-op-ish transform). Callers that
        # care distinguish this by checking the fitted points list separately.
        return lambda _x: 0.0
    if n == 1:
        v = float(ys[0])
        return lambda _x: v

    # Sort by x (stable on ties), then enforce monotonicity on y via PAV. Each
    # "block" is a run of points pooled to a shared mean. When the next block's
    # mean drops below the previous (a violation), pool them together.
    order = sorted(range(n), key=lambda i: xs[i])
    sx = [float(xs[i]) for i in order]
    sy = [float(ys[i]) for i in order]

    # Each block holds its total y, count, and the x-range it covers.
    blocks_xlo: deque[float] = deque()
    blocks_xhi: deque[float] = deque()
    blocks_sum: deque[float] = deque()
    blocks_n: deque[int] = deque()

    for x, y in zip(sx, sy):
        blocks_xlo.append(x)
        blocks_xhi.append(x)
        blocks_sum.append(y)
        blocks_n.append(1)
        # While the last two blocks violate monotonicity, pool them.
        while len(blocks_sum) >= 2:
            prev_mean = blocks_sum[-2] / blocks_n[-2]
            last_mean = blocks_sum[-1] / blocks_n[-1]
            if last_mean < prev_mean - 1e-12:
                # Pool last into prev.
                blocks_xhi[-2] = blocks_xhi[-1]
                blocks_sum[-2] += blocks_sum[-1]
                blocks_n[-2] += blocks_n[-1]
                blocks_xlo.pop()
                blocks_xhi.pop()
                blocks_sum.pop()
                blocks_n.pop()
            else:
                break

    xs_lo = list(blocks_xlo)
    xs_hi = list(blocks_xhi)
    means = [s / c for s, c in zip(blocks_sum, blocks_n)]

    def f(x: float) -> float:
        # Constant extrapolation outside the fitted range.
        if not means:
            return 0.0
        if x <= xs_lo[0]:
            return means[0]
        if x >= xs_hi[-1]:
            return means[-1]
        # Find the block containing x; within a block the value is constant
        # (a step function). Interpolate only across the *gap* between blocks.
        for i in range(len(means)):
            if x < xs_lo[i]:
                # Between means[i-1] (ends at xs_hi[i-1]) and means[i] (starts at
                # xs_lo[i]); linear interp across that gap.
                if i == 0:
                    return means[0]
                lo_x, hi_x = xs_hi[i - 1], xs_lo[i]
                lo_y, hi_y = means[i - 1], means[i]
                if hi_x <= lo_x:
                    return hi_y
                t = (x - lo_x) / (hi_x - lo_x)
                return lo_y + (hi_y - lo_y) * t
            if x <= xs_hi[i]:
                return means[i]
        return means[-1]

    # Stash the breakpoints on the callable for callers that serialize the fit.
    f.isotonic_points = list(zip(sx, sy))  # type: ignore[attr-defined]
    return f


def isotonic_points(xs: list[float], ys: list[float]) -> list[tuple[float, float]]:
    """The fitted step-breakpoints ``(x, y)`` for serialization.

    Separate from :func:`isotonic_fit` so callers can persist the raw calibration
    data without holding the closure. Returns ``[]`` on degenerate input.
    """
    f = isotonic_fit(xs, ys)
    return getattr(f, "isotonic_points", [])  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Kolmogorov–Smirnov two-sample statistic — (drift), also used as a
# fit-quality check: how well do two distributions separate after calibration?
# ---------------------------------------------------------------------------


def ks_stat(a: list[float], b: list[float]) -> float:
    """The two-sample Kolmogorov–Smirnov statistic: the maximum vertical gap
    between the empirical CDFs of ``a`` and ``b``.

    Returns a float in ``[0, 1]`` — 0 means the distributions are identical, 1
    means fully separated (no overlap). Empty input yields 0.0 (no evidence of a
    difference). Pure-Python: walk the merged sorted distinct values, advancing
    each ECDF by its count at that value BEFORE measuring the gap (so tied values
    shared by both samples don't create a spurious mid-tie difference).
    """
    if not a or not b:
        return 0.0
    na, nb = len(a), len(b)
    # Count both samples at each distinct value, then walk distinct values in
    # ascending order. Ties shared by both samples advance together.
    counts: dict[tuple[float, int], int] = {}
    for v in a:
        counts[(v, 0)] = counts.get((v, 0), 0) + 1
    for v in b:
        counts[(v, 1)] = counts.get((v, 1), 0) + 1
    distinct = sorted({v for v, _ in counts})
    ca = cb = 0
    d = 0.0
    for v in distinct:
        ca += counts.get((v, 0), 0)
        cb += counts.get((v, 1), 0)
        gap = abs(ca / na - cb / nb)
        if gap > d:
            d = gap
    return d


# ---------------------------------------------------------------------------
# Robust L-estimators — (MAD thresholds), §4.3 (Hodges-Lehmann),
# §7.1 (trimmed KS). Order-statistic based, 50% breakdown point.
# ---------------------------------------------------------------------------


def median(xs: list[float]) -> float:
    """The median (50th percentile) — the canonical robust location estimator.

    50% breakdown point: up to half the sample can be arbitrarily corrupted
    without moving the median, unlike the mean. Empty input → 0.0.
    """
    if not xs:
        return 0.0
    return percentile(sorted(xs), 50)


def mad(xs: list[float]) -> float:
    """Median Absolute Deviation: ``median(|x - median(xs)|)``.

    The robust scale estimator. 50% breakdown point — a
    handful of extreme scores barely move it, unlike standard deviation. Returns
    0.0 on empty input, or when all values are identical (zero spread).
    """
    if not xs:
        return 0.0
    m = median(xs)
    return median([abs(x - m) for x in xs])


def mad_scaled(xs: list[float]) -> float:
    """MAD scaled to estimate σ: ``mad(xs) * 1.4826``.

    The 1.4826 factor is the conventional normal-consistency constant, so
    ``mad_scaled`` approximates the standard deviation under mild assumptions
    while keeping MAD's 50% breakdown. Use this where a σ-interpretation matters
    (e.g. choosing a Huber cutoff k·σ); use :func:`mad` for the raw statistic.
    """
    return mad(xs) * 1.4826


def trimmed(xs: list[float], pct: float = 5.0) -> list[float]:
    """Drop ``pct``% from each tail, returning the central sample.

    ``pct`` is a percentage in [0, 50). At 0 the sample is unchanged. The trimmed
    sample underlies trimmed-KS and trimmed-mean estimators — focusing the
    comparison/estimate on the central mass where thresholds and decisions lie,
    ignoring tail contamination. Empty input → empty list.
    """
    if not xs:
        return []
    pct = max(0.0, min(pct, 49.999))
    n = len(xs)
    k = int(math.floor(n * pct / 100.0))
    s = sorted(xs)
    return s[k : n - k] if k > 0 else s


def hodges_lehmann(a: list[float], b: list[float]) -> float:
    """Hodges–Lehmann location-shift estimator.

    Returns ``median([ai - bj for ai in a for bj in b])`` — a robust estimate of
    how much one sample is shifted relative to another. High efficiency and
    ~0.29 breakdown. Used on a model swap to estimate how much "easier"/"harder"
    the new model separates classes, for a first-cut threshold adjustment.
    O(n·m); fine at calibration-set sizes. Empty input → 0.0.
    """
    if not a or not b:
        return 0.0
    diffs = [ai - bj for ai in a for bj in b]
    return median(diffs)


def trimmed_ks(a: list[float], b: list[float], pct: float = 5.0) -> float:
    """Two-sample KS on the trimmed samples.

    Drops ``pct``% from each tail before computing :func:`ks_stat`, so the drift
    signal reflects the central 90% of scores — where thresholds live — rather
    than being driven by a few tail outliers. Empty input → 0.0.
    """
    ta = trimmed(a, pct)
    tb = trimmed(b, pct)
    if not ta or not tb:
        return 0.0
    return ks_stat(ta, tb)


# ---------------------------------------------------------------------------
# Huber-loss isotonic regression (M-estimation flavor) —
# ---------------------------------------------------------------------------


def huber_loss(r: float, c: float) -> float:
    """The Huber loss of residual ``r`` with cutoff ``c``.

    Quadratic for ``|r| <= c`` (sensitive in the middle), linear beyond
    (bounded influence for outliers). Combined with isotonic constraints this
    yields a calibration curve resistant to a fraction of mislabeled pairs.
    """
    ar = abs(r)
    if ar <= c:
        return 0.5 * r * r
    return c * (ar - 0.5 * c)


def huber_isotonic_fit(
    xs: list[float], ys: list[float], *, c: float | None = None, iters: int = 10
) -> Callable[[float], float]:
    """Monotone isotonic fit under Huber loss.

    Robust alternative to :func:`isotonic_fit` (which uses L2 / squared loss): a
    handful of mislabeled calibration pairs have bounded influence on the fit.
    Implemented as **iteratively reweighted PAV** — PAV solves the
    monotone-constrained L2 problem; Huber is L2 with per-point weights
    ``w_i = min(1, c / |r_i|)``, so we alternate: fit weighted-PAV, recompute
    residuals/weights, repeat. Converges in ~3–5 iters; capped at ``iters``.

    The cutoff ``c`` defaults to ``1.345 * mad_scaled(residuals from the L2 fit)``
    — the standard 95%-efficiency choice. Same return contract as
    :func:`isotonic_fit`: a callable ``f(x)`` with an ``.isotonic_points`` attr
    (the final weighted-fit breakpoints). Degrades to plain ``isotonic_fit`` on
    degenerate input or non-convergence (never raises).
    """
    n = len(xs)
    if n == 0 or n != len(ys):
        return isotonic_fit(xs, ys)
    if n == 1:
        return isotonic_fit(xs, ys)

    # Weighted-PAV: reuse the PAV machinery with per-point weights by replicating
    # each point's contribution. Simplest faithful route: solve the weighted
    # monotone L2 via the same block-pooling but tracking weighted sums. To avoid
    # duplicating PAV, we fold weights into y (weighted regression on (x, w*y)
    # with count w) — exact for the pooled-block means.
    order = sorted(range(n), key=lambda i: xs[i])
    sx = [float(xs[i]) for i in order]
    sy = [float(ys[i]) for i in order]

    # Iteratively reweighted PAV: start at the L2 solution (weights = 1), then
    # down-weight points with large residuals. The cutoff is set once from the
    # L2 residuals' robust scale (1.345·MAD ≈ 95% efficiency at the normal).
    weights = [1.0] * n
    cutoff = float(c) if c is not None else None
    for _ in range(max(1, iters)):
        fitted = _pav_fitted_per_point(sx, sy, weights)
        residuals = [sy[i] - fitted[i] for i in range(n)]
        if cutoff is None:
            cutoff = 1.345 * mad_scaled(residuals)
            if cutoff <= 0:
                cutoff = 1.345  # fall back to a fixed scale if MAD=0
        new_weights = [min(1.0, cutoff / (abs(r) + 1e-12)) for r in residuals]
        # Convergence: weights stable.
        if max(abs(new_weights[i] - weights[i]) for i in range(n)) < 1e-4:
            weights = new_weights
            break
        weights = new_weights

    fitted = _pav_fitted_per_point(sx, sy, weights)
    return _step_callable(list(zip(sx, fitted)))


def _pav_fitted_per_point(
    sx: list[float], sy: list[float], weights: list[float]
) -> list[float]:
    """Weighted PAV: return each input point's pooled-block mean.

    Tracks block membership so each input index maps to its final block's mean
    (needed for the per-point residuals in the reweighting loop).
    """
    sums: deque[float] = deque()
    cnts: deque[float] = deque()
    members: deque[list[int]] = deque()  # input indices in each block
    for i, (y, w) in enumerate(zip(sy, weights)):
        sums.append(y * w); cnts.append(w); members.append([i])
        while len(sums) >= 2:
            prev = sums[-2] / cnts[-2] if cnts[-2] else 0.0
            last = sums[-1] / cnts[-1] if cnts[-1] else 0.0
            if last < prev - 1e-12:
                sums[-2] += sums[-1]; cnts[-2] += cnts[-1]
                members[-2] = members[-2] + members[-1]
                sums.pop(); cnts.pop(); members.pop()
            else:
                break
    means = [s / c_ if c_ else 0.0 for s, c_ in zip(sums, cnts)]
    out = [0.0] * len(sx)
    for blk_idx, idxs in enumerate(members):
        for i in idxs:
            out[i] = means[blk_idx]
    return out


def _step_callable(pts: list[tuple[float, float]]) -> Callable[[float], float]:
    """Build a monotone step evaluation closure from (x, y) breakpoints.

    Constant extrapolation outside the range, linear interpolation between
    breakpoints — matching :func:`isotonic_fit`'s contract.
    """
    def f(x: float) -> float:
        if not pts:
            return 0.0
        xs_only = [p[0] for p in pts]
        if x <= xs_only[0]:
            return pts[0][1]
        if x >= xs_only[-1]:
            return pts[-1][1]
        for i in range(len(pts)):
            if x < pts[i][0]:
                if i == 0:
                    return pts[0][1]
                lo_x, hi_x = pts[i - 1][0], pts[i][0]
                lo_y, hi_y = pts[i - 1][1], pts[i][1]
                if hi_x <= lo_x:
                    return hi_y
                t = (x - lo_x) / (hi_x - lo_x)
                return lo_y + (hi_y - lo_y) * t
            if x == pts[i][0]:
                return pts[i][1]
        return pts[-1][1]

    f.isotonic_points = pts  # type: ignore[attr-defined]
    return f

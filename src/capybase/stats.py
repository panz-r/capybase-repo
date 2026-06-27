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

__all__ = ["percentile", "sigmoid", "isotonic_fit", "ks_stat"]


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
# Isotonic regression (Pool-Adjacent-Violators) — survey §2.1
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
# Kolmogorov–Smirnov two-sample statistic — survey §6.1 (drift), also used as a
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

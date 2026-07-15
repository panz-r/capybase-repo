"""Tests for the shared numerics module (capybase.stats).

Covers: percentile/sigmoid parity with their prior module-private behavior;
isotonic regression (PAV) monotonicity, known-input fits, and graceful
degradation; the two-sample KS statistic on separated vs identical distributions;
robust L-estimators (median, MAD, trimmed, Hodges-Lehmann, trimmed-KS) and the
Huber-loss isotonic variant ( 2 §3.1, §4.1, §4.3, §7.1).
"""

from __future__ import annotations

import math

import pytest

from capybase.stats import (
    percentile,
    sigmoid,
    isotonic_fit,
    isotonic_points,
    ks_stat,
    median,
    mad,
    mad_scaled,
    trimmed,
    huber_loss,
    huber_isotonic_fit,
    hodges_lehmann,
    trimmed_ks,
)


# ---------------------------------------------------------------------------
# percentile — parity + edge cases
# ---------------------------------------------------------------------------


def test_percentile_empty_returns_zero():
    assert percentile([], 50) == 0.0


def test_percentile_single_element_identity():
    assert percentile([7.0], 50) == 7.0
    assert percentile([7.0], 99) == 7.0


def test_percentile_linear_interpolation_matches_numpy_default():
    """The numpy-default linear-interpolation percentile on a known set."""
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]  # already sorted ascending
    # p=0 -> min, p=100 -> max, p=50 -> median (rank 2 -> 3.0)
    assert percentile(xs, 0) == 1.0
    assert percentile(xs, 100) == 5.0
    assert percentile(xs, 50) == 3.0
    # p=25 -> rank 1 -> value 2.0
    assert percentile(xs, 25) == 2.0


def test_percentile_interpolates_between_ranks():
    """A non-rank p interpolates linearly between the two bracketing values."""
    xs = [0.0, 1.0]  # rank 0..1
    # p=50 -> rank 0.5 -> 0.5
    assert percentile(xs, 50) == pytest.approx(0.5)
    # p=10 -> rank 0.1 -> 0.1
    assert percentile(xs, 10) == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# sigmoid — parity with the old module-private _sigmoid
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("z", [-10.0, -1.0, -0.1, 0.0, 0.1, 1.0, 10.0])
def test_sigmoid_matches_analytic(z):
    """The stable sigmoid equals the closed form and is in (0,1)."""
    expected = 1.0 / (1.0 + math.exp(-z))
    assert sigmoid(z) == pytest.approx(expected, rel=1e-12, abs=1e-15)
    assert 0.0 < sigmoid(z) < 1.0


def test_sigmoid_zero_is_half():
    assert sigmoid(0.0) == pytest.approx(0.5)


def test_sigmoid_no_overflow_on_large_magnitude():
    """Branch-on-sign must not overflow for |z| large."""
    assert sigmoid(1000.0) == pytest.approx(1.0)
    assert sigmoid(-1000.0) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# isotonic regression (PAV) —
# ---------------------------------------------------------------------------


def test_isotonic_perfectly_separable_is_a_step():
    """When the two classes don't overlap, PAV recovers a near-step function."""
    xs = [0.1, 0.2, 0.3, 0.7, 0.8, 0.9]
    ys = [0, 0, 0, 1, 1, 1]
    f = isotonic_fit(xs, ys)
    # Low scores map to ~0, high scores to ~1.
    assert f(0.15) == pytest.approx(0.0, abs=1e-9)
    assert f(0.85) == pytest.approx(1.0, abs=1e-9)
    # Monotone: f never decreases as x increases.
    vals = [f(x) for x in [0.1, 0.25, 0.5, 0.6, 0.75, 0.95]]
    assert vals == sorted(vals)


def test_isotonic_monotone_input_is_unchanged():
    """Already-monotone data passes through PAV unchanged (each point its own block)."""
    xs = [0.0, 0.25, 0.5, 0.75, 1.0]
    ys = [0.1, 0.2, 0.4, 0.7, 0.9]
    f = isotonic_fit(xs, ys)
    # At the fitted points the block means equal the (already-monotone) ys.
    for x, y in zip(xs, ys):
        assert f(x) == pytest.approx(y, abs=1e-9)


def test_isotonic_pools_violations():
    """A violating pair is pooled to its mean. (1,0) after (0,1) → both = 0.5."""
    xs = [0.0, 1.0]
    ys = [1.0, 0.0]
    f = isotonic_fit(xs, ys)
    # The monotone fit pools both to 0.5; constant across [0,1].
    assert f(0.0) == pytest.approx(0.5)
    assert f(1.0) == pytest.approx(0.5)
    assert f(0.5) == pytest.approx(0.5)


def test_isotonic_extrapolates_constant_outside_range():
    """Outside the fitted range, clamp to the nearest endpoint (constant extrapolation)."""
    xs = [2.0, 3.0, 4.0]
    ys = [0.0, 0.5, 1.0]
    f = isotonic_fit(xs, ys)
    assert f(-100.0) == pytest.approx(0.0)  # below min -> first mean
    assert f(100.0) == pytest.approx(1.0)  # above max -> last mean


def test_isotonic_empty_returns_constant_zero():
    f = isotonic_fit([], [])
    assert f(0.5) == 0.0


def test_isotonic_length_mismatch_returns_constant_zero():
    f = isotonic_fit([0.1, 0.2], [0.0])
    assert f(0.1) == 0.0


def test_isotonic_single_point_is_constant():
    f = isotonic_fit([0.4], [0.7])
    assert f(0.4) == pytest.approx(0.7)
    assert f(-1.0) == pytest.approx(0.7)
    assert f(99.0) == pytest.approx(0.7)


def test_isotonic_points_stashes_breakpoints():
    """The fit records its input breakpoints for serialization/replay."""
    xs = [0.1, 0.2, 0.8]
    ys = [0.0, 0.0, 1.0]
    f = isotonic_fit(xs, ys)
    pts = getattr(f, "isotonic_points", [])
    assert len(pts) == 3
    assert pts == [(0.1, 0.0), (0.2, 0.0), (0.8, 1.0)]


def test_isotonic_points_helper_returns_breakpoints():
    """The standalone helper returns the same breakpoints without the closure."""
    pts = isotonic_points([0.1, 0.9], [0.0, 1.0])
    assert pts == [(0.1, 0.0), (0.9, 1.0)]


def test_isotonic_points_empty_on_degenerate():
    assert isotonic_points([], []) == []


# ---------------------------------------------------------------------------
# KS two-sample statistic —
# ---------------------------------------------------------------------------


def test_ks_identical_distributions_is_zero():
    a = [0.1, 0.2, 0.3, 0.4, 0.5]
    assert ks_stat(a, a) == pytest.approx(0.0)


def test_ks_fully_separated_is_one():
    """Disjoint support with a clean gap → max ECDF difference is 1.0."""
    a = [0.0, 0.1, 0.2]
    b = [0.8, 0.9, 1.0]
    assert ks_stat(a, b) == pytest.approx(1.0)


def test_ks_partially_overlapping_between_zero_and_one():
    """Some overlap → statistic strictly between 0 and 1."""
    a = [0.0, 0.2, 0.4]
    b = [0.3, 0.6, 0.9]
    d = ks_stat(a, b)
    assert 0.0 < d < 1.0


def test_ks_empty_returns_zero():
    assert ks_stat([], [0.1, 0.2]) == 0.0
    assert ks_stat([0.1, 0.2], []) == 0.0


# ---------------------------------------------------------------------------
# Robust L-estimators ( 2 §4.1, §4.3, §7.1)
# ---------------------------------------------------------------------------


def test_median_matches_percentile_50():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert median(xs) == percentile(sorted(xs), 50)


def test_median_empty_returns_zero():
    assert median([]) == 0.0


def test_median_resists_outlier():
    """The 50% breakdown property: one extreme value barely moves the median
    (while it would dominate the mean)."""
    core = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert median(core) == 3.0
    assert median(core + [10_000.0]) == 3.5  # shifted only slightly, not dragged


def test_mad_zero_spread_is_zero():
    """All-equal values → MAD is 0 (no dispersion)."""
    assert mad([4.0, 4.0, 4.0]) == 0.0


def test_mad_empty_returns_zero():
    assert mad([]) == 0.0


def test_mad_resists_outlier():
    """MAD (50% breakdown) ignores an extreme outlier that would dominate std."""
    core = [1.0, 2.0, 3.0, 4.0, 5.0]
    m_core = mad(core)
    m_with_outlier = mad(core + [10_000.0])
    # MAD barely changes (within a factor of ~2), unlike std which would explode.
    assert m_with_outlier < 2 * m_core + 1e-9


def test_mad_scaled_uses_consistency_factor():
    """mad_scaled = mad * 1.4826 (the normal-consistency constant)."""
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert mad_scaled(xs) == pytest.approx(mad(xs) * 1.4826)


def test_trimmed_drops_equal_tails():
    """Trimming 10% from each tail of a 1..10 sequence removes 1 and 10."""
    out = trimmed([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 10)
    assert out == [2, 3, 4, 5, 6, 7, 8, 9]


def test_trimmed_zero_pct_unchanged():
    assert trimmed([3, 1, 2], 0) == [1, 2, 3]  # sorted but complete


def test_trimmed_empty_returns_empty():
    assert trimmed([], 10) == []


def test_hodges_lehmann_known_shift():
    """HL of two samples differing by a constant shift ≈ that shift."""
    a = [10.0, 11.0, 12.0]
    b = [1.0, 2.0, 3.0]  # shifted by ~9
    assert hodges_lehmann(a, b) == pytest.approx(9.0)


def test_hodges_lehmann_identical_is_zero():
    assert hodges_lehmann([1.0, 2.0], [1.0, 2.0]) == pytest.approx(0.0)


def test_hodges_lehmann_empty_returns_zero():
    assert hodges_lehmann([], [1.0]) == 0.0


def test_trimmed_ks_equals_ks_when_no_trim():
    """With pct=0, trimmed_ks is identical to plain ks_stat (no points removed)."""
    a = [0.1, 0.3, 0.5, 0.7, 0.9]
    b = [0.2, 0.4, 0.6, 0.8, 1.0]
    assert trimmed_ks(a, b, 0) == pytest.approx(ks_stat(a, b))


def test_trimmed_ks_identical_samples_is_zero():
    """Identical samples → trimmed_ks is 0 regardless of trim."""
    a = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    assert trimmed_ks(a, a, 10) == pytest.approx(0.0)


def test_trimmed_ks_empty_returns_zero():
    assert trimmed_ks([], [1.0]) == 0.0


# ---------------------------------------------------------------------------
# Huber-loss isotonic regression ( 2 §3.1)
# ---------------------------------------------------------------------------


def test_huber_loss_quadratic_then_linear():
    """|r| <= c is quadratic (0.5 r^2); beyond c it's linear: c*(|r| - 0.5c)."""
    assert huber_loss(1.0, 2.0) == pytest.approx(0.5)  # 0.5 * 1^2 (within cutoff)
    assert huber_loss(3.0, 2.0) == pytest.approx(2.0 * (3.0 - 0.5 * 2.0))  # 4.0 (linear)


def test_huber_isotonic_matches_l2_on_clean_data():
    """With no label noise, Huber and L2 fits agree (weights stay ~1 everywhere)."""
    xs = [0.1, 0.2, 0.3, 0.7, 0.8, 0.9]
    ys = [0, 0, 0, 1, 1, 1]
    f_l2 = isotonic_fit(xs, ys)
    f_hub = huber_isotonic_fit(xs, ys)
    for x in [0.15, 0.5, 0.85]:
        assert f_hub(x) == pytest.approx(f_l2(x), abs=1e-6)


def test_huber_isotonic_resists_mislabeled_points():
    """Several mislabeled probes bend the L2 fit upward; the Huber fit resists —
    the mislabels' fitted value stays closer to the true 0-class. (A single
    mislabel is already pooled by PAV's monotonicity constraint, so the robust
    advantage shows with multiple noisy labels.)"""
    # Two low-score points mislabeled as 1 (should be 0).
    xs = [0.05, 0.1, 0.15, 0.2, 0.25, 0.75, 0.8, 0.85, 0.9, 0.95]
    ys = [1, 1, 0, 0, 0, 1, 1, 1, 1, 1]
    f_l2 = isotonic_fit(xs, ys)
    f_hub = huber_isotonic_fit(xs, ys)
    # L2 fits the mislabeled low points to ~0.40; Huber resists toward ~0.27.
    assert f_l2(0.05) > f_hub(0.05)
    assert f_hub(0.05) < f_l2(0.05)  # Huber closer to the true 0-class


def test_huber_isotonic_is_monotone():
    """The Huber fit is still monotone-nondecreasing (isotonic constraint holds)."""
    xs = [0.1, 0.2, 0.3, 0.4, 0.7, 0.8, 0.9]
    ys = [0, 1, 0, 0, 1, 1, 1]  # noisy
    f = huber_isotonic_fit(xs, ys)
    pts = getattr(f, "isotonic_points", [])
    vals = [p[1] for p in pts]
    assert vals == sorted(vals)


def test_huber_isotonic_extrapolates_constant():
    """Outside the fitted range, clamp to the nearest endpoint."""
    xs = [2.0, 3.0, 4.0]
    ys = [0.0, 0.5, 1.0]
    f = huber_isotonic_fit(xs, ys)
    assert f(-100.0) == pytest.approx(0.0)
    assert f(100.0) == pytest.approx(1.0)


def test_huber_isotonic_degenerate_returns_constant():
    assert huber_isotonic_fit([], [])(0.5) == 0.0
    assert huber_isotonic_fit([0.4], [0.7])(0.4) == pytest.approx(0.7)


def test_huber_isotonic_points_stashed():
    """The fit records its breakpoints for serialization/replay."""
    xs = [0.1, 0.2, 0.8]
    ys = [0.0, 0.0, 1.0]
    f = huber_isotonic_fit(xs, ys)
    pts = getattr(f, "isotonic_points", [])
    assert len(pts) == 3

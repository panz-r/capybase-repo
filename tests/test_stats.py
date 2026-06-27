"""Tests for the shared numerics module (capybase.stats).

Covers: percentile/sigmoid parity with their prior module-private behavior;
isotonic regression (PAV) monotonicity, known-input fits, and graceful
degradation; the two-sample KS statistic on separated vs identical distributions.
"""

from __future__ import annotations

import math

import pytest

from capybase.stats import percentile, sigmoid, isotonic_fit, isotonic_points, ks_stat


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
# isotonic regression (PAV) — survey §2.1
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
# KS two-sample statistic — survey §6.1
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

"""Tests for the design-of-experiments primitives (calibration_design).

These pin the statistical contracts the two-phase calibration relies on:
1. The screening design samples every factor at both levels (balanced, no
   main-effect aliasing) — Phase 1 genuinely spans the factor space.
2. main_effects / effect_tstats recover known effect sizes from synthetic data.
3. rank_factors surfaces the dominant factor.
4. Phase 2's full_factorial + select_best_point recover the best config.
"""

from __future__ import annotations

import itertools

from capybase.calibration_design import (
    DesignPoint,
    Factor,
    effect_tstats,
    fractional_factorial_2k,
    full_factorial,
    main_effects,
    rank_factors,
    select_best_point,
)


def _factors(n: int) -> list[Factor]:
    return [Factor(f"f{i}", low=False, high=True) for i in range(n)]


# ---------------------------------------------------------------------------
# Screening design: balance + no aliasing + spans all factors
# ---------------------------------------------------------------------------


def test_fractional_factorial_run_counts():
    """Each k produces the expected designed-fraction run count."""
    assert len(fractional_factorial_2k(_factors(3))) == 8   # full 2^3
    assert len(fractional_factorial_2k(_factors(4))) == 16  # full 2^4
    assert len(fractional_factorial_2k(_factors(5))) == 16  # 2^(5-1)
    assert len(fractional_factorial_2k(_factors(6))) == 16  # 2^(6-2)
    assert len(fractional_factorial_2k(_factors(7))) == 16  # 2^(7-3)
    assert len(fractional_factorial_2k(_factors(8))) == 16  # 2^(8-4)


def test_screening_samples_every_factor_at_both_levels():
    """Phase 1's core contract: every factor appears at low AND high, balanced."""
    factors = _factors(7)
    design = fractional_factorial_2k(factors)
    for factor in factors:
        lows = sum(1 for p in design if p.levels[factor.name] == factor.low)
        highs = sum(1 for p in design if p.levels[factor.name] == factor.high)
        assert lows == highs == len(design) // 2, (
            f"{factor.name} unbalanced: {lows} low / {highs} high"
        )


def test_screening_no_main_effect_aliasing():
    """No two factor columns are identical or negated (Res IV: mains are clean)."""
    factors = _factors(7)
    design = fractional_factorial_2k(factors)
    # Encode each factor column as a sign vector (+1 high / -1 low).
    cols = {}
    for factor in factors:
        cols[factor.name] = tuple(
            1 if p.levels[factor.name] == factor.high else -1 for p in design
        )
    names = list(cols)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            ci, cj = cols[names[i]], cols[names[j]]
            assert ci != cj, f"{names[i]} aliased with {names[j]}"
            assert ci != tuple(-x for x in cj), f"{names[i]} aliased with -{names[j]}"


def test_screening_all_runs_distinct():
    """A designed sample should not repeat a configuration."""
    for k in (4, 5, 6, 7, 8):
        design = fractional_factorial_2k(_factors(k))
        seen = set()
        for p in design:
            key = tuple(sorted(p.levels.items()))
            assert key not in seen, f"k={k}: duplicate design point"
            seen.add(key)


# ---------------------------------------------------------------------------
# Effect estimation
# ---------------------------------------------------------------------------


def test_main_effects_recover_known_effect():
    """A factor that adds +1 to the score when high has effect +1."""
    factors = [Factor("a", low=-1, high=1), Factor("b", low=-1, high=1)]
    # 2^2 full factorial
    design = fractional_factorial_2k(factors)
    # Synthetic score: a's level is the ONLY driver (score = a).
    scores = [p.levels["a"] for p in design]
    effects = main_effects(scores, design, factors)
    assert abs(effects["a"] - 2.0) < 1e-9  # mean(high=+1) - mean(low=-1) = 1 - (-1)
    assert abs(effects["b"]) < 1e-9        # b has no effect


def test_effect_tstats_ranks_significant_above_noisy():
    """A stable factor with a clear effect ranks above a no-effect factor."""
    factors = [Factor("signal", low=0, high=10), Factor("noise", low=0, high=10)]
    design = fractional_factorial_2k(factors)
    # 'signal' drives the score cleanly; 'noise' doesn't.
    scores = [float(p.levels["signal"]) for p in design]
    tstats = effect_tstats(scores, design, factors)
    assert abs(tstats["signal"]) > abs(tstats["noise"])


def test_rank_factors_returns_dominant_first():
    """The factor that drives the score is ranked #1."""
    factors = [Factor("dominant", low=0, high=1), Factor("null", low=0, high=1)]
    design = fractional_factorial_2k(factors)
    scores = [float(p.levels["dominant"]) for p in design]
    ranking = rank_factors(scores, design, factors)
    assert ranking[0].name == "dominant"
    assert ranking[0].direction == "high"
    assert ranking[-1].name == "null"
    assert ranking[-1].direction == "≈"


def test_rank_factors_direction_low():
    """A factor whose LOW level is better is marked direction='low'."""
    factors = [Factor("x", low=0, high=1)]
    design = fractional_factorial_2k(factors)
    # Score is HIGHER when x is low → low is better.
    scores = [1.0 if p.levels["x"] == 0 else 0.0 for p in design]
    ranking = rank_factors(scores, design, factors)
    assert ranking[0].direction == "low"
    assert ranking[0].effect < 0


# ---------------------------------------------------------------------------
# Phase 2: full factorial + selection
# ---------------------------------------------------------------------------


def test_full_factorial_3_factors_8_runs_plus_center():
    factors = [Factor("a", low=0, high=1), Factor("b", low=0.0, high=1.0), Factor("c", low=False, high=True)]
    design = full_factorial(factors, center_points=2)
    assert len(design) == 8 + 2  # 2^3 + 2 center points
    # The 8 factorial points are all distinct combos.
    factorial_points = design[:8]
    combos = set()
    for p in factorial_points:
        combos.add(tuple(sorted(p.levels.items())))
    assert len(combos) == 8
    # Center points: numeric factors at their mid-level (0.5).
    assert design[8].levels["b"] == 0.5


def test_select_best_point_picks_highest_score():
    factors = [Factor("a", low=0, high=1)]
    design = full_factorial(factors)
    scores = [0.0, 5.0]  # index 1 (a=high) is best

    def compare(x, y):
        return (x > y) - (x < y)

    best, score = select_best_point(design, scores, compare)
    assert best.levels["a"] == 1
    assert score == 5.0


def test_round_trip_recovers_best_config():
    """A synthetic scoring function that rewards 'a=high, b=low' — Phase 1+2
    should identify a and b as the top factors and select that combo."""
    factors = [
        Factor("a", low=0, high=1), Factor("b", low=0, high=1),
        Factor("c", low=0, high=1), Factor("d", low=0, high=1),
    ]

    def score(point: DesignPoint) -> float:
        # Reward a=high and b=low; c and d are noise.
        s = 0.0
        if point.levels["a"] == 1:
            s += 1.0
        if point.levels["b"] == 0:
            s += 1.0
        return s

    # Phase 1: screen all 4 factors.
    p1 = fractional_factorial_2k(factors)
    p1_scores = [score(p) for p in p1]
    ranking = rank_factors(p1_scores, p1, factors)
    top_names = {r.name for r in ranking[:2]}
    assert top_names == {"a", "b"}, f"top factors were {top_names}, expected a,b"

    # Phase 2: full factorial on the top 2.
    top = [f for f in factors if f.name in top_names]
    p2 = full_factorial(top)
    p2_scores = [score(p) for p in p2]

    def compare(x, y):
        return (x > y) - (x < y)

    best, best_score = select_best_point(p2, p2_scores, compare)
    assert best.levels["a"] == 1
    assert best.levels["b"] == 0
    assert best_score == 2.0

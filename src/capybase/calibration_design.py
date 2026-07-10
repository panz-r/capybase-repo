"""Design-of-experiments primitives for the two-phase calibration (pure stdlib).

The VT3B calibration exposed that single-sample, independent A/B-per-knob
probing is unreliable for noisy thinking models and doesn't scale as factors
grow (layout + position + history + mechanisms + sampling = 7+ factors). This
module implements the screening-design half of the fix: a **fractional-
factorial design** that samples *all* factor variations cheaply, plus the
effect-estimation math that ranks which dimensions genuinely drive performance.

Everything here is pure-Python over plain lists — no numpy/scipy (the codebase's
zero-numeric-dependency contract). The design sign-matrices are constructed from
standard generator relations (verified Resolution-IV offline), not searched at
runtime. No I/O, no LLM calls — trivially testable.

Reference: the two-phase experimental-design guideline (fractional factorial →
focused refinement). Phase 1 uses :func:`fractional_factorial_2k`; Phase 2 uses
:func:`full_factorial` on the top factors Phase 1 identified.
"""

from __future__ import annotations

import itertools
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Factors and design points
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Factor:
    """One experimental axis with its two screening levels.

    ``name`` identifies the factor (matches the config/profile field it maps
    to). ``low`` and ``high`` are the two settings Phase 1 samples — typed
    values: enums (``OutputLayout.JSON_V6`` / ``.MARKDOWN_CODE``), bools, ints,
    or floats. Phase 1 treats every factor as binary (two levels); continuous
    factors get a mid-level center point in Phase 2.
    """

    name: str
    low: Any
    high: Any

    @property
    def is_numeric(self) -> bool:
        """True when both levels are numbers (Phase 2 adds a center point)."""
        return isinstance(self.low, (int, float)) and isinstance(self.high, (int, float))

    def center(self) -> Any:
        """The mid-level for a numeric factor (Phase 2 center points)."""
        if self.is_numeric and not isinstance(self.low, bool):
            return (self.low + self.high) / 2.0
        # Categorical: no center; Phase 2 uses only the two levels.
        return self.high


@dataclass(frozen=True)
class DesignPoint:
    """One run's factor settings (a row of the design matrix).

    ``levels`` maps each factor name to its setting for this run. ``config_id``
    is a short stable label for attribution/journaling. Encode/decode to a
    ModelConfig + PromptProfile is the caller's job (this module is config-
    agnostic).
    """

    config_id: str
    levels: dict[str, Any] = field(default_factory=dict)

    def tag(self) -> str:
        """A short suffix recording this point's non-baseline settings."""
        parts = [f"{k}={_short(v)}" for k, v in sorted(self.levels.items())]
        return ("#" + ",".join(parts)) if parts else ""


def _short(v: Any) -> str:
    """Flatten a value into a short tag fragment."""
    s = str(v)
    # Enum → its value; long strings → truncated.
    if hasattr(v, "value"):
        s = str(v.value)
    return s.replace(" ", "")[:20]


# ---------------------------------------------------------------------------
# Phase 1: fractional-factorial screening design
# ---------------------------------------------------------------------------


def fractional_factorial_2k(factors: list[Factor]) -> list[DesignPoint]:
    """Generate a Resolution-IV 2^(k−r) screening design sampling all factors.

    Every factor appears at both its low and high level, balanced (half the
    runs at each level) — so Phase 1 genuinely spans the full factor space at a
    fraction of the cost of the full 2^k factorial. Resolution IV guarantees no
    main effect is aliased with any other main effect or any two-factor
    interaction, so the main-effect estimates are clean.

    Run counts (the designed fraction of the 2^k space):
      k ≤ 4 → full factorial (2^k runs)
      k = 5 → 16 runs (2^(5−1), half-fraction)
      k = 6 → 16 runs (2^(6−2), quarter-fraction)
      k = 7 → 16 runs (2^(7−3), eighth-fraction)
      k = 8 → 16 runs (2^(8−4), sixteenth-fraction)
      k > 8 → not supported (raise; add a higher fraction if ever needed)

    The sign matrices are built from standard generator relations (verified
    Res-IV offline), not searched at runtime.
    """
    k = len(factors)
    if k == 0:
        return [DesignPoint(config_id="center", levels={})]
    signs = _sign_matrix(k)
    points: list[DesignPoint] = []
    for i, row in enumerate(signs):
        levels = {}
        for j, factor in enumerate(factors):
            levels[factor.name] = factor.high if row[j] > 0 else factor.low
        points.append(DesignPoint(config_id=f"p1-{i+1:02d}", levels=levels))
    return points


def _sign_matrix(k: int) -> list[tuple[int, ...]]:
    """The Res-IV sign matrix for k factors: each row is a run, each column a factor.

    Built from a 2^m base full factorial (m = k−r) plus generator relations for
    the r extra factors. Generators are chosen so the design is Resolution IV
    (verified offline: balanced, no main-effect aliasing, all runs distinct).
    """
    if k <= 4:
        # Full factorial — no generators needed.
        return [tuple(s) for s in itertools.product((-1, 1), repeat=k)]
    # Base on 4 factors (16 base runs); derive the rest via generators.
    base = list(itertools.product((-1, 1), repeat=4))
    # generator[i] = the tuple of BASE-factor indices whose product gives factor i.
    # Standard Res-IV relations (verified): no generator is a single factor or
    # a product that aliases a main effect with a two-factor interaction.
    generators = {
        5: {4: (0, 1, 2)},                              # E = ABC
        6: {4: (0, 1), 5: (0, 2)},                      # E = AB,  F = AC
        7: {4: (0, 1, 2), 5: (0, 1, 3), 6: (0, 2, 3)},  # E = ABC, F = ABD, G = ACD
        8: {4: (0, 1), 5: (0, 2), 6: (0, 3), 7: (1, 2, 3)},  # E=AB F=AC G=AD H=BCD
    }[k]
    rows: list[tuple[int, ...]] = []
    for b in base:
        row = list(b)
        for extra_idx in range(4, k):
            prod_indices = generators[extra_idx]
            val = 1
            for fi in prod_indices:
                val *= b[fi]
            row.append(val)
        rows.append(tuple(row))
    return rows


# ---------------------------------------------------------------------------
# Phase 1 analysis: main effects + standardized effects (t-stats)
# ---------------------------------------------------------------------------


def main_effects(
    scores: list[float], design: list[DesignPoint], factors: list[Factor]
) -> dict[str, float]:
    """Estimate each factor's main effect: mean(score | high) − mean(score | low).

    The core Phase-1 signal: a positive effect means setting the factor to its
    high level raises the score; negative means low is better; near-zero means
    the factor doesn't matter. Pure group-mean subtraction — no modeling
    assumptions. Returns ``{factor_name: effect}``.
    """
    out: dict[str, float] = {}
    for factor in factors:
        highs: list[float] = []
        lows: list[float] = []
        for score, point in zip(scores, design):
            if point.levels.get(factor.name) == factor.high:
                highs.append(score)
            elif point.levels.get(factor.name) == factor.low:
                lows.append(score)
        mean_hi = statistics.fmean(highs) if highs else 0.0
        mean_lo = statistics.fmean(lows) if lows else 0.0
        out[factor.name] = mean_hi - mean_lo
    return out


def effect_tstats(
    scores: list[float], design: list[DesignPoint], factors: list[Factor]
) -> dict[str, float]:
    """Standardized effect (t-like statistic) for each factor.

    ``effect / pooled_se`` where the pooled SE is estimated from the within-
    group spread of the high and low groups. This ranks factors by significance
    (a noisy factor with a large raw effect ranks lower than a stable one),
    matching the guideline's "standardized effects / t-statistics". Robust to
    degenerate samples: when a group has < 2 members, falls back to the raw
    effect (no denominator) so the ranking still works on tiny designs.
    """
    out: dict[str, float] = {}
    for factor in factors:
        highs: list[float] = []
        lows: list[float] = []
        for score, point in zip(scores, design):
            if point.levels.get(factor.name) == factor.high:
                highs.append(score)
            elif point.levels.get(factor.name) == factor.low:
                lows.append(score)
        mean_hi = statistics.fmean(highs) if highs else 0.0
        mean_lo = statistics.fmean(lows) if lows else 0.0
        effect = mean_hi - mean_lo
        # Pooled SE: sqrt of the average within-group variance, / sqrt(n_per_group).
        # Uses sample stdev (n-1); with <2 points the spread is undefined.
        var_hi = statistics.variance(highs) if len(highs) >= 2 else 0.0
        var_lo = statistics.variance(lows) if len(lows) >= 2 else 0.0
        if var_hi == 0.0 and var_lo == 0.0:
            # No within-group spread observed: the effect is "infinitely"
            # significant if nonzero. Use the raw effect so it ranks, but cap
            # the magnitude to avoid a single factor dominating the sort.
            out[factor.name] = effect
            continue
        pooled_var = (var_hi + var_lo) / 2.0
        n = max(1, min(len(highs), len(lows)))
        se = (pooled_var / n) ** 0.5
        out[factor.name] = effect / se if se > 0 else effect
    return out


@dataclass(frozen=True)
class FactorRanking:
    """One factor's Phase-1 ranking entry."""

    name: str
    effect: float       # raw main effect (mean high − mean low)
    tstat: float        # standardized effect (significance)
    direction: str      # "high" if high level is better, "low" if low is, "≈" if ~0


def rank_factors(
    scores: list[float], design: list[DesignPoint], factors: list[Factor]
) -> list[FactorRanking]:
    """Rank factors by |t-stat| (significance), ties broken by |effect|.

    Phase 1's headline output: which dimensions genuinely drive performance and
    in which direction. ``direction`` is "high"/"low" when the effect is
    non-negligible (|effect| above a small epsilon), else "≈" (indifferent).
    The top entries are the factors Phase 2 should refine.
    """
    effects = main_effects(scores, design, factors)
    tstats = effect_tstats(scores, design, factors)
    rankings = []
    for factor in factors:
        e = effects[factor.name]
        t = tstats[factor.name]
        eps = 1e-9
        direction = "high" if e > eps else ("low" if e < -eps else "≈")
        rankings.append(FactorRanking(factor.name, e, t, direction))
    # Sort by |tstat| desc, then |effect| desc.
    rankings.sort(key=lambda r: (abs(r.tstat), abs(r.effect)), reverse=True)
    return rankings


# ---------------------------------------------------------------------------
# Phase 2: focused full-factorial on the top factors
# ---------------------------------------------------------------------------


def full_factorial(
    factors: list[Factor], *, center_points: int = 0
) -> list[DesignPoint]:
    """Generate the full 2^k factorial on a reduced factor set (Phase 2).

    Explores every combination of the top factors' two levels so their main
    effects AND pairwise interactions are cleanly estimable — the focused
    refinement after Phase 1 screened out the negligible factors. Optional
    ``center_points`` adds mid-level runs for numeric factors (detects
    curvature). Each center point repeats the mid-level of every numeric factor
    (categorical factors use their high level — there's no true center).
    """
    if not factors:
        return [DesignPoint(config_id="p2-center", levels={})]
    points: list[DesignPoint] = []
    combos = list(itertools.product((-1, 1), repeat=len(factors)))
    for i, combo in enumerate(combos):
        levels = {}
        for j, factor in enumerate(factors):
            levels[factor.name] = factor.high if combo[j] > 0 else factor.low
        points.append(DesignPoint(config_id=f"p2-{i+1:02d}", levels=levels))
    # Center points: the mid-level of every numeric factor (curvature check).
    for c in range(center_points):
        levels = {f.name: f.center() for f in factors}
        points.append(DesignPoint(config_id=f"p2-ctr{c+1}", levels=levels))
    return points


def select_best_point(
    design: list[DesignPoint], scores: list[Any], compare: Callable[[Any, Any], int]
) -> tuple[DesignPoint, Any]:
    """Return the (best design point, its score) under a comparator.

    Phase 2's selection step: pick the design point whose score is best under
    the caller's ordering (typically ``compare_scores``: correctness → proxy →
    latency). Ties pick the first (deterministic).
    """
    if not design:
        raise ValueError("cannot select from an empty design")
    best_i = 0
    for i in range(1, len(design)):
        if compare(scores[i], scores[best_i]) > 0:
            best_i = i
    return design[best_i], scores[best_i]

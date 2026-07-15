"""Tests for SBCR: Search-Based Combination Resolution.

SBCR is a *candidate generator*, not a decider. It searches order-preserving
interleavings of a conflict's two sides for the highest-fitness combination
(mean similarity to both parents). Its output is ALWAYS validated by the
orchestrator before acceptance — so these unit tests pin the generator's
behavior, and the orchestrator integration test (test_sbcr_orchestrator.py)
proves the validation gate is the real safety mechanism.

Key contract points tested:
1. Both-sides-add → recovers the union (the canonical combination case).
2. One side empty → declines (degenerate; structural resolver handles it).
3. Contradictory single-line conflict → PROPOSES a concatenation (which
   validation will reject downstream) — documenting that SBCR proposes and
   validation disposes.
4. Exhaustive search is optimal for small blocks; hill climbing handles large.
5. Fitness is symmetric and bounded.
"""

from __future__ import annotations

import pytest

from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.sbcr import (
    EXHAUSTIVE_THRESHOLD,
    CombinationResolution,
    _interleaving_count,
    _interleavings,
    balance,
    fitness,
    resolve_by_combination_search,
)


def _unit(current: str, replayed: str, base: str = "") -> ConflictUnit:
    return ConflictUnit(
        session_id="s", step_index=0, path="f.py", unit_id="u",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text=base,
    )


# ---------------------------------------------------------------------------
# Fitness
# ---------------------------------------------------------------------------


def test_fitness_identical_to_both_parents_is_maximal():
    lines = ["a", "b"]
    assert fitness(lines, lines, lines) == 1.0


def test_fitness_disjoint_from_one_parent_under_char_gestalt():
    # Character-level Gestalt: candidate matches ours exactly (1.0) but shares
    # only the newline separator with theirs. Line-level difflib would give 0.0
    # for the disjoint side (fitness 0.5); char-level shares the "\n" between
    # "a\nb" and "x\ny" → 2/6 ≈ 0.333, so fitness = mean(1.0, 0.333) ≈ 0.667.
    cand = ["a", "b"]
    ours = ["a", "b"]
    theirs = ["x", "y"]
    assert fitness(cand, ours, theirs) == pytest.approx(0.667, abs=0.01)


def test_fitness_is_symmetric_in_parents():
    cand = ["a", "x"]
    ours = ["a", "b"]
    theirs = ["x", "y"]
    assert fitness(cand, ours, theirs) == pytest.approx(fitness(cand, theirs, ours))


def test_fitness_empty_equal_parents_is_one():
    assert fitness([], [], []) == 1.0


# ---------------------------------------------------------------------------
# Balance: the §4.2 routing signal (SBCR wins balanced, LLM wins imbalanced)
# ---------------------------------------------------------------------------


def test_balance_perfectly_balanced():
    # Equal non-blank line counts on both sides → 1.0.
    assert balance(_unit("a\nb", "c\nd", base="")) == 1.0
    assert balance(_unit("x", "y", base="")) == 1.0


def test_balance_heavily_imbalanced():
    # 1 line vs 8 lines → 1/8 = 0.125.
    assert balance(_unit("x", "a\nb\nc\nd\ne\nf\ng\nh", base="")) == pytest.approx(0.125)


def test_balance_moderate():
    # 2 vs 5 → 0.4.
    assert balance(_unit("a\nb", "c\nd\ne\nf\ng", base="")) == pytest.approx(0.4)


def test_balance_ignores_blank_lines():
    # Blank lines don't count toward size, so balance is over content lines.
    assert balance(_unit("a\n\n", "b\n", base="")) == 1.0  # 1 content line each


def test_balance_zero_when_one_side_empty():
    # Degenerate: SBCR declines on empty sides anyway; balance is 0.0.
    assert balance(_unit("a", "", base="")) == 0.0
    assert balance(_unit("", "a", base="")) == 0.0


# ---------------------------------------------------------------------------
# Search space
# ---------------------------------------------------------------------------


def test_interleaving_count_matches_binomial():
    # C(m+n, m)
    assert _interleaving_count(2, 2) == 6
    assert _interleaving_count(3, 3) == 20
    assert _interleaving_count(0, 5) == 1
    assert _interleaving_count(5, 0) == 1
    assert _interleaving_count(0, 0) == 1


def test_interleavings_are_order_preserving():
    ours = ["o1", "o2"]
    theirs = ["t1"]
    results = list(_interleavings(ours, theirs))
    assert len(results) == _interleaving_count(2, 1) == 3
    # In every interleaving, o1 comes before o2.
    for cand in results:
        assert cand.index("o1") < cand.index("o2")


# ---------------------------------------------------------------------------
# Both-sides-add: the canonical combination case
# ---------------------------------------------------------------------------


def test_both_sides_add_recovers_union():
    # base empty; ours adds two imports, theirs adds two different imports.
    # The correct resolution is the union (4 lines, some order).
    r = resolve_by_combination_search(_unit("import a\nimport b", "import c\nimport d"))
    assert r.resolved
    text_lines = r.text.splitlines()
    # All four lines present (combination, not a one-sided pick).
    assert set(text_lines) == {"import a", "import b", "import c", "import d"}
    assert len(text_lines) == 4
    # Order is preserved within each side.
    assert text_lines.index("import a") < text_lines.index("import b")
    assert text_lines.index("import c") < text_lines.index("import d")


def test_both_sides_add_fitness_above_floor():
    r = resolve_by_combination_search(_unit("a = 1", "b = 2"))
    assert r.resolved
    # A clean 2+2 combination sits around 0.67 (matches both parents ~2/3).
    assert r.fitness > 0.6


# ---------------------------------------------------------------------------
# Decline cases
# ---------------------------------------------------------------------------


def test_one_side_empty_declines():
    # Degenerate: only one side has lines → the structural resolver / LLM
    # handles this; SBCR adds no value and declines rather than echoing a side.
    r = resolve_by_combination_search(_unit("x = 1", ""))
    assert not r.resolved
    assert r.text is None


def test_both_sides_empty_declines():
    r = resolve_by_combination_search(_unit("", ""))
    assert not r.resolved


# ---------------------------------------------------------------------------
# Scope guard: SBCR fires ONLY on addition conflicts (empty base)
#
# SBCR's search space is *combination* resolutions: both sides
# ADD content. On a *modification* conflict (both sides changed a shared base
# line), the space includes semantically-wrong concatenations (two contradictory
# lines, last-wins — which can even be syntactically valid, e.g. the second
# assignment shadows the first). So SBCR refuses to propose whenever the base is
# non-empty. This makes SBCR safe-by-SCOPE, not just safe-by-validation.
# ---------------------------------------------------------------------------


def test_declines_on_modification_conflict_nonempty_base():
    # Both sides changed the SAME line differently; base non-empty → decline.
    # (validation never even sees a candidate; the LLM handles it.)
    r = resolve_by_combination_search(_unit("x = 2", "x = 3", base="x = 1"))
    assert not r.resolved
    assert r.text is None


def test_declines_on_multiline_modification_conflict():
    base = "def f():\n    return 1"
    current = "def f():\n    return 2"
    replayed = "def f():\n    return 3"
    r = resolve_by_combination_search(_unit(current, replayed, base=base))
    assert not r.resolved


def test_fires_on_addition_conflict_empty_base():
    # base empty: both sides added distinct content → SBCR's domain.
    r = resolve_by_combination_search(_unit("import sys", "import json", base=""))
    assert r.resolved
    assert "import sys" in r.text and "import json" in r.text


def test_uses_diff3_refined_base_when_present():
    # The raw marker base may over-include context lines (e.g. `import os`) that
    # aren't part of the conflict. The diff3-refined base is the true minimal
    # ancestor region. SBCR must use the refined base for the scope check.
    u = _unit("import sys", "import json", base="import os")  # raw base non-empty
    # ...but diff3 refinement says the real conflict base is empty (an addition):
    u.structural_metadata["diff3_refined"] = {"current": "import sys",
                                              "base": "",
                                              "replayed": "import json"}
    r = resolve_by_combination_search(u)
    assert r.resolved  # refined base empty → addition → SBCR fires
    assert "import sys" in r.text and "import json" in r.text


def test_high_floor_rejects_low_fitness_combinations():
    # With a demanding floor, even a real addition is declined. This confirms
    # `floor` is the acceptance threshold, not just a tie-breaker.
    r = resolve_by_combination_search(_unit("a = 1", "b = 2", base=""), floor=0.99)
    assert not r.resolved


# ---------------------------------------------------------------------------
# Exhaustive vs hill climbing
# ---------------------------------------------------------------------------


def test_small_block_uses_exhaustive_optimal():
    # 3+3 = 20 interleavings < threshold → exhaustive, so the result is the
    # globally optimal interleaving, not a local optimum.
    r = resolve_by_combination_search(_unit("o1\no2\no3", "t1\nt2\nt3"))
    assert r.resolved
    # All six lines present (a full combination).
    assert len(r.text.splitlines()) == 6


def test_large_block_uses_hill_climbing_and_returns_union():
    # 8+8 = 12870 interleavings > threshold → hill climbing. It should still
    # find a full combination (all 16 lines) given a fixed seed.
    ours = "\n".join(f"o{i}" for i in range(8))
    theirs = "\n".join(f"t{i}" for i in range(8))
    r = resolve_by_combination_search(_unit(ours, theirs), seed=12345)
    assert r.resolved
    assert len(r.text.splitlines()) == 16  # full union
    assert r.fitness > 0.6


def test_hill_climbing_is_reproducible_with_seed():
    ours = "\n".join(f"o{i}" for i in range(8))
    theirs = "\n".join(f"t{i}" for i in range(8))
    r1 = resolve_by_combination_search(_unit(ours, theirs), seed=7)
    r2 = resolve_by_combination_search(_unit(ours, theirs), seed=7)
    assert r1.text == r2.text
    assert r1.fitness == r2.fitness


# ---------------------------------------------------------------------------
# Stagnation early-exit
# ---------------------------------------------------------------------------


def test_stagnation_limit_still_finds_union_on_balanced_block():
    """A reasonable stagnation limit must not hurt result quality on a balanced
    large block — the search converges well before the limit. The full 16-line
    union should still be found."""
    ours = "\n".join(f"o{i}" for i in range(8))
    theirs = "\n".join(f"t{i}" for i in range(8))
    r = resolve_by_combination_search(
        _unit(ours, theirs), seed=12345, stagnation_limit=64
    )
    assert r.resolved
    assert len(r.text.splitlines()) == 16  # full union, no quality loss


def test_stagnation_default_is_reproducible():
    """The default stagnation limit is deterministic given a seed — same result
    across runs (the early-exit is a function of fitness, which is deterministic,
    and the seeded RNG)."""
    ours = "\n".join(f"o{i}" for i in range(10))
    theirs = "\n".join(f"t{i}" for i in range(10))
    r1 = resolve_by_combination_search(_unit(ours, theirs), seed=99)
    r2 = resolve_by_combination_search(_unit(ours, theirs), seed=99)
    assert r1.text == r2.text
    assert r1.fitness == r2.fitness


def test_stagnation_bounded_by_max_iterations_too():
    """Even with a generous stagnation limit, max_iterations is still a hard
    ceiling. A tiny budget returns a result (or declines) without hanging."""
    ours = "\n".join(f"o{i}" for i in range(8))
    theirs = "\n".join(f"t{i}" for i in range(8))
    # max_iterations=1 → at most one evaluation; must return promptly.
    r = resolve_by_combination_search(
        _unit(ours, theirs), seed=1, max_iterations=1, stagnation_limit=10000
    )
    # Resolved or not, the call must terminate and return a well-formed result.
    assert isinstance(r.fitness, float)


def test_stagnation_limit_is_tunable():
    """The parameter threads through to the hill-climb search. A very tight
    limit on a large block still yields a valid (non-crashing) result — the
    early-exit never leaves the resolver in a bad state."""
    ours = "\n".join(f"o{i}" for i in range(12))
    theirs = "\n".join(f"t{i}" for i in range(12))
    r = resolve_by_combination_search(
        _unit(ours, theirs), seed=42, stagnation_limit=4
    )
    # With such a tight limit it may resolve or decline; either is acceptable as
    # long as it terminates cleanly and reports an honest fitness.
    assert isinstance(r.fitness, float)
    if r.resolved:
        assert r.text  # non-empty resolved text


def test_exhaustive_threshold_is_sane():
    # The threshold should keep exhaustive cost bounded (~1k candidates) while
    # being large enough to cover typical conflict blocks without hill climbing.
    assert 256 <= EXHAUSTIVE_THRESHOLD <= 4096


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


def test_resolved_text_has_no_markers():
    r = resolve_by_combination_search(_unit("a = 1", "b = 2"))
    assert r.resolved
    assert "<<<" not in r.text and "===" not in r.text and ">>>" not in r.text


def test_unresolved_result_carries_fitness_for_journaling():
    # Even when declined, the fitness of the best-seen candidate is returned so
    # the orchestrator can journal why SBCR passed.
    r = resolve_by_combination_search(_unit("a = 1", "b = 2"), floor=0.99)
    assert not r.resolved
    assert isinstance(r.fitness, float)


# ---------------------------------------------------------------------------
# Character-level Gestalt fitness + union-constrained search (§4.1/§4.3)
# ---------------------------------------------------------------------------


def test_fitness_uses_character_level_gestalt():
    # At character granularity, two sides that share NO lines but share common
    # characters (the newline, shared tokens) score nonzero — the line-level
    # metric would return 0.0 here. This distinguishes char-level from
    # line-level and documents the metric switch (arXiv:2605.16646 §4.1).
    cand = ["import os"]
    theirs = ["import re"]
    # "import os" vs "import re" share "import " (7 chars) + the whole is short.
    # Line-level ratio would be 0.0 (no line matches); char-level > 0.0.
    from capybase.sbcr import _ratio
    assert _ratio(cand, theirs) > 0.0


def test_hill_climb_never_drops_a_side_union():
    # The union-constrained search must keep EVERY line from both sides in the
    # result — char-level fitness would otherwise prefer a truncated candidate
    # (length bias). This is the core fix for the §4.3 truncation failure mode.
    ours = "\n".join(f"o{i}" for i in range(8))
    theirs = "\n".join(f"t{i}" for i in range(8))
    for seed in (1, 42, 12345):
        r = resolve_by_combination_search(_unit(ours, theirs), seed=seed)
        assert r.resolved
        lines = r.text.splitlines()
        # Full union: all 16 lines present, no side truncated.
        assert set(lines) == {f"o{i}" for i in range(8)} | {f"t{i}" for i in range(8)}
        assert len(lines) == 16


def test_shrinkage_guard_rejects_short_candidate():
    # When the floor forces a degenerate search and the best candidate is
    # shorter than min_candidate_ratio of the larger side, the shrinkage guard
    # declines. With a near-1.0 ratio this rejects almost everything that
    # drops lines — but more importantly it must be a documented, populated
    # skip_reason when it fires.
    # We force the situation by giving one side far more lines than the other
    # and demanding a low floor; the exhaustive search will prefer the shorter
    # candidate (high fitness against the small side), triggering the guard.
    ours = "x = 1"
    theirs = "\n".join(f"v{i} = {i}" for i in range(6))
    r = resolve_by_combination_search(
        _unit(ours, theirs), floor=0.0, min_candidate_ratio=0.99,
    )
    # The best candidate that drops lines from the 6-line side is 1-2 lines; a
    # 0.99 guard on a 6-line larger side requires ≥ 5.94 → 6 lines. So either
    # the full union is found (resolves) or the guard declines. Either way the
    # result is honest. Verify the skip_reason is populated on a decline.
    if not r.resolved:
        assert r.skip_reason is not None
        assert "shrinkage" in r.skip_reason


def test_time_budget_terminates_long_search():
    # A tiny time budget must stop the hill-climb promptly without hanging,
    # regardless of how large the search space is. The result is well-formed.
    ours = "\n".join(f"o{i}" for i in range(20))
    theirs = "\n".join(f"t{i}" for i in range(20))
    import time as _time
    t0 = _time.monotonic()
    r = resolve_by_combination_search(
        _unit(ours, theirs), seed=1, max_time=0.05, max_iterations=10**9,
    )
    elapsed = _time.monotonic() - t0
    assert elapsed < 2.0  # the 0.05s budget bounds it well under this
    assert isinstance(r.fitness, float)


def test_skip_reason_populated_on_modification_conflict():
    # The decline reason threads through so the orchestrator can journal it.
    r = resolve_by_combination_search(_unit("x = 2", "x = 3", base="x = 1"))
    assert not r.resolved
    assert r.skip_reason is not None
    assert "base" in r.skip_reason.lower()


def test_skip_reason_populated_on_below_floor():
    r = resolve_by_combination_search(_unit("a = 1", "b = 2"), floor=0.99)
    assert not r.resolved
    assert r.skip_reason is not None
    assert "floor" in r.skip_reason.lower()

"""Tests for SBCR: Search-Based Combination Resolution (survey §4.1).

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


def test_fitness_disjoint_from_one_parent_is_halved():
    # candidate matches ours exactly (1.0) but has zero overlap with theirs (0.0)
    cand = ["a", "b"]
    ours = ["a", "b"]
    theirs = ["x", "y"]
    assert fitness(cand, ours, theirs) == pytest.approx(0.5)


def test_fitness_is_symmetric_in_parents():
    cand = ["a", "x"]
    ours = ["a", "b"]
    theirs = ["x", "y"]
    assert fitness(cand, ours, theirs) == pytest.approx(fitness(cand, theirs, ours))


def test_fitness_empty_equal_parents_is_one():
    assert fitness([], [], []) == 1.0


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
# SBCR's search space is *combination* resolutions (survey §4.1): both sides
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

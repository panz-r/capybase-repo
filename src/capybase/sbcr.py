"""SBCR: Search-Based Combination Resolution (survey §4.1).

A deterministic, language-agnostic resolver that searches for the best
*combination* resolution of a conflict: a merge made entirely of lines taken
from the two sides, each side's lines kept in their original relative order (an
order-preserving interleaving). The survey found ~98.6% of real-world
combination resolutions contain no newly-invented lines, so this search space
covers the overwhelming majority of "both sides added / both sides restructured"
conflicts that have no single correct side.

Fitness (the evaluation function) is the **mean textual similarity of the
candidate to both parents** (survey: correlation ~0.64 with developer-chosen
resolution quality). Similarity uses stdlib ``difflib`` — no training, no model,
no new dependencies.

Search strategy:
- **Exhaustive** enumeration of all order-preserving interleavings when the
  block is small (product of side line counts ≤ ``EXHAUSTIVE_THRESHOLD``).
  This is optimal over the space and fast (≤ ~1k candidates).
- **Random-restart hill climbing** for larger blocks, with the survey's three
  operators (add a line, remove a line, exchange positions). Bounded by
  ``max_iterations`` so cost is predictable.

Safety contract: like the structural resolver, SBCR is a *candidate generator*.
Its output is STILL run through the full validation pipeline
(markers/splice/AST/syntax) by the orchestrator before being accepted. So SBCR
can only help — when its combination guess is wrong, validation rejects it and
the conflict falls through to the LLM. It is wired in AFTER the structural
pre-resolver and BEFORE the model, so the cheap provably-safe rules always run
first; SBCR only fires on conflicts the structural resolver declined.

Scope guard: SBCR fires ONLY when the diff3-refined base is empty — i.e. a true
*addition* conflict where both sides added content with no shared base line. The
survey's search space is combination resolutions, which presuppose additions;
applying it to *modifications* of a shared line (e.g. both sides changed
``x = 1`` differently) would let the fitness rank the two contradictory lines'
concatenation above either side alone — a semantically-wrong last-wins merge
that can even be syntactically valid (the second assignment shadows the first).
Restricting to empty-base conflicts keeps SBCR safe-by-scope in addition to
safe-by-validation: it never even proposes on a modification conflict.

All functions here are pure (no I/O, no model, no git) and exhaustively
unit-testable.
"""

from __future__ import annotations

import difflib
import random
from dataclasses import dataclass
from math import comb
from typing import Iterator

from capybase.conflict_model import ConflictUnit

# Exhaustive search when C(len(ours)+len(theirs), len(ours)) is at most this.
# Keeps exhaustive cost ≤ ~1k candidates / sub-millisecond. Beyond it, hill
# climbing takes over (near-optimal in practice, bounded cost).
EXHAUSTIVE_THRESHOLD = 1024


@dataclass(frozen=True)
class CombinationResolution:
    """Result of a combination-search attempt.

    ``text`` is the resolved block-interior (same shape as an LLM candidate's
    ``resolved_text`` — splices identically). ``None`` means no candidate was
    found (the search space was empty or every candidate was rejected as
    degenerate). ``fitness`` is the candidate's mean similarity to the two
    parents, recorded for journaling/tuning.
    """

    text: str | None
    fitness: float

    @property
    def resolved(self) -> bool:
        return self.text is not None


def _effective_base(unit: ConflictUnit) -> str:
    """The conflict's true base region: the diff3-refined base when the extractor
    recorded one, else the raw marker base.

    The raw ``unit.base.text`` is the text between the ``|||||||`` and ``=======``
    markers as git wrote them — which can include adjacent context lines that
    aren't actually part of the conflict's ancestor region. The extractor's
    ``_refine_with_diff3`` recomputes the minimal conflict base via
    ``git merge-file``; when available, that is the accurate ancestor region and
    is what the empty/non-empty scope check should see. Missing keys are treated
    as "no refinement" (fall back to raw) — refinement is advisory.
    """
    refined = unit.structural_metadata.get("diff3_refined")
    if isinstance(refined, dict) and "base" in refined:
        return refined.get("base") or ""
    return unit.base.text or ""


def _nonblank_lines(text: str) -> int:
    """Count of non-blank lines — the size signal for the balance metric."""
    return sum(1 for line in (text or "").splitlines() if line.strip())


def balance(unit: ConflictUnit) -> float:
    """How balanced the two conflict sides are, in ``[0, 1]``.

    Defined as ``min(cur, rep) / max(cur, rep)`` over non-blank line counts.
    1.0 = perfectly balanced (both sides the same size); →0 = heavily imbalanced
    (one side much larger). Survey §4.2: SBCR (combination search) WINS on
    balanced conflicts and LOSES to the LLM on imbalanced ones — the LLM is
    better when one side changed far more than the other. So the orchestrator
    uses this to decide whether an SBCR result is accepted outright (balanced)
    or treated as advisory while the LLM runs (imbalanced).

    Pure function of the unit's current/replayed side texts; no I/O. Returns 0.0
    if either side is empty (those are degenerate — SBCR declines anyway).
    """
    cur = _nonblank_lines(unit.current.text or "")
    rep = _nonblank_lines(unit.replayed.text or "")
    if cur == 0 or rep == 0:
        return 0.0
    return min(cur, rep) / max(cur, rep)


# ---------------------------------------------------------------------------
# Fitness: mean textual similarity to both parents (survey §4.1)
# ---------------------------------------------------------------------------


def _ratio(a: list[str], b: list[str]) -> float:
    """``difflib`` similarity ratio over line lists. 1.0 for identical."""
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(a=a, b=b, autojunk=False).ratio()


def fitness(candidate: list[str], ours: list[str], theirs: list[str]) -> float:
    """Mean similarity of the candidate to each parent.

    This is the survey's evaluation function: a good combination is close in
    text to BOTH sides (it contains lines from both, in a sensible order).
    Correlation ~0.64 with developer resolution quality, per the survey.
    """
    return (_ratio(candidate, ours) + _ratio(candidate, theirs)) / 2.0


# ---------------------------------------------------------------------------
# Search space: order-preserving interleavings
# ---------------------------------------------------------------------------


def _interleavings(ours: list[str], theirs: list[str]) -> Iterator[list[str]]:
    """Yield every order-preserving interleaving of ``ours`` and ``theirs``.

    A candidate is a sequence where each side's lines appear in their original
    relative order, but the two sides may be merged in any way. This is exactly
    the survey's search space (line-wise combinations, no new lines). The count
    is C(m+n, m); the caller bounds this before calling.
    """
    if not ours:
        yield list(theirs)
        return
    if not theirs:
        yield list(ours)
        return
    head_o, *rest_o = ours
    for rest in _interleavings(rest_o, theirs):
        yield [head_o, *rest]
    head_t, *rest_t = theirs
    for rest in _interleavings(ours, rest_t):
        yield [head_t, *rest]


def _interleaving_count(m: int, n: int) -> int:
    """Number of order-preserving interleavings of m and n lines = C(m+n, m)."""
    if m < 0 or n < 0:
        return 0
    return comb(m + n, m)


# ---------------------------------------------------------------------------
# Search: exhaustive (small) or random-restart hill climbing (large)
# ---------------------------------------------------------------------------


def _exhaustive_best(
    ours: list[str], theirs: list[str], *, floor: float
) -> tuple[list[str] | None, float]:
    """Try every interleaving; return the highest-fitness one (ties → first).

    Optimal over the full search space. Only called when the space is small
    (≤ ``EXHAUSTIVE_THRESHOLD`` candidates). ``floor`` is the minimum fitness a
    candidate must clear to be accepted — the empty/one-sided candidates that
    would just drop a whole side are filtered here.
    """
    best: list[str] | None = None
    best_fit = -1.0
    for cand in _interleavings(ours, theirs):
        f = fitness(cand, ours, theirs)
        if f > best_fit:
            best_fit, best = f, cand
    if best is None or best_fit < floor:
        return None, best_fit
    return best, best_fit


def _hill_climb_best(
    ours: list[str],
    theirs: list[str],
    *,
    floor: float,
    max_iterations: int,
    rng: random.Random,
) -> tuple[list[str] | None, float]:
    """Random-restart hill climbing over the interleaving space (survey §4.1).

    Operators: ADD a not-yet-included line, REMOVE an included line, EXCHANGE
    two positions. Restart from a random valid interleaving when stuck. Bounded
    by ``max_iterations`` total evaluations so cost is predictable on large
    blocks. Returns the best candidate found above ``floor``, else (None, score).
    """
    pool = ours + theirs  # the universe of lines; an interleaving draws from it
    if not pool:
        return None, -1.0

    def _random_interleaving() -> list[str]:
        # Start from a random shuffle that respects each side's order: merge the
        # two ordered lists choosing at random which side to draw from next.
        merged: list[str] = []
        i = j = 0
        while i < len(ours) and j < len(theirs):
            if rng.random() < 0.5:
                merged.append(ours[i]); i += 1
            else:
                merged.append(theirs[j]); j += 1
        merged.extend(ours[i:]); merged.extend(theirs[j:])
        return merged

    def _neighbors(cand: list[str]) -> Iterator[list[str]]:
        # REMOVE: drop any line (yield each single removal).
        for k in range(len(cand)):
            yield cand[:k] + cand[k + 1:]
        # ADD: insert any not-yet-present line at any position that keeps each
        # side's order. We only insert lines absent from cand.
        present = set(cand)  # note: lines are unique by identity here only if so
        remaining = [ln for ln in pool if ln not in cand]
        for ln in remaining:
            for pos in range(len(cand) + 1):
                yield cand[:pos] + [ln] + cand[pos:]
        # EXCHANGE: swap any two positions.
        for a in range(len(cand)):
            for b in range(a + 1, len(cand)):
                nb = list(cand)
                nb[a], nb[b] = nb[b], nb[a]
                yield nb

    best: list[str] | None = None
    best_fit = -1.0
    iters = 0
    while iters < max_iterations:
        current = _random_interleaving()
        current_fit = fitness(current, ours, theirs)
        iters += 1
        if current_fit > best_fit:
            best_fit, best = current_fit, list(current)
        # Climb until no neighbor improves.
        improved = True
        while improved and iters < max_iterations:
            improved = False
            for nb in _neighbors(current):
                if iters >= max_iterations:
                    break
                iters += 1
                f = fitness(nb, ours, theirs)
                if f > current_fit:
                    current, current_fit = nb, f
                    improved = True
                    if f > best_fit:
                        best_fit, best = list(nb), f
            # continue climbing from the best neighbor of this run
        # random restart
    if best is None or best_fit < floor:
        return None, best_fit
    return best, best_fit


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def resolve_by_combination_search(
    unit: ConflictUnit,
    *,
    floor: float = 0.6,
    max_iterations: int = 2000,
    seed: int | None = None,
) -> CombinationResolution:
    """Attempt a search-based combination resolution of ``unit``.

    Searches order-preserving interleavings of the conflict's ours/theirs side
    lines for the one with maximal mean similarity to both parents. Returns the
    best candidate if its fitness clears ``floor``; otherwise an unresolved
    result (the search found nothing worth proposing, so defer to the LLM).

    Parameters
    ----------
    floor : float
        Minimum fitness to accept a candidate. The survey's fitness tops out at
        ~0.83 for a clean both-sides combination; below ~0.6 the candidate is
        essentially one-sided (it drops most of a side), which is not a genuine
        combination and is better left to the LLM.
    max_iterations : int
        Bound on total fitness evaluations for hill climbing on large blocks.
    seed : int | None
        RNG seed for reproducible hill climbing (tests pass a fixed seed).

    The resolver reads ``unit.current.text`` / ``unit.replayed.text`` (the
    diff3-refined sides are preferred at extraction, so these are the tightest
    available). Base gates the *scope*: SBCR fires only when base is empty (a
    true addition conflict). On a non-empty base the sides modify shared content,
    where the search space includes semantically-wrong concatenations (two
    contradictory lines, last-wins) — so we decline and defer to the LLM. This
    makes SBCR safe-by-scope, not just safe-by-validation.

    The base used for the scope check is the **diff3-refined** base when the
    extractor recorded one (``structural_metadata["diff3_refined"]["base"]``),
    else the raw ``unit.base.text``. The refined base is the true minimal
    conflict-ancestor region; the raw marker base can over-include adjacent
    context lines that aren't actually part of the conflict, which would wrongly
    trip the non-empty guard on a genuine addition conflict.
    """
    base = _effective_base(unit)
    if base.strip():
        # Non-empty base ⇒ a modification conflict, not an addition. The
        # combination search space is unsafe here (see module docstring), so we
        # refuse to propose. The structural resolver already declined (it runs
        # first); the LLM will handle this.
        return CombinationResolution(text=None, fitness=0.0)
    ours = (unit.current.text or "").splitlines()
    theirs = (unit.replayed.text or "").splitlines()
    if not ours and not theirs:
        return CombinationResolution(text=None, fitness=0.0)
    # The trivial degenerate cases: if one side is empty, the only interleaving
    # is the other side verbatim — that's a one-sided resolution the structural
    # resolver already handles (and the LLM would too). SBCR adds no value, so
    # decline rather than echo a side back.
    if not ours or not theirs:
        return CombinationResolution(text=None, fitness=0.0)

    space = _interleaving_count(len(ours), len(theirs))
    if space == 0:
        return CombinationResolution(text=None, fitness=0.0)

    if space <= EXHAUSTIVE_THRESHOLD:
        best, best_fit = _exhaustive_best(ours, theirs, floor=floor)
    else:
        best, best_fit = _hill_climb_best(
            ours, theirs, floor=floor, max_iterations=max_iterations,
            rng=random.Random(seed),
        )

    if best is None:
        return CombinationResolution(text=None, fitness=best_fit)
    return CombinationResolution(text="\n".join(best), fitness=best_fit)

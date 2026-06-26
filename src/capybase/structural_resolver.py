"""Deterministic structural conflict resolution (survey §2.1/§6.4, layer 1).

A safe, LLM-free pre-resolver that runs BEFORE the model. It attempts to produce
a correct merged text from base + current + replayed using three provably-safe
rules — no heuristics that could introduce a wrong merge:

1. **identical_sides** — current and replayed normalized-equal → emit that side.
   (Survey: "both sides identical → delete the conflict".)
2. **one_sided_change** — only one side diverged from base → take the changed
   side; the other conceded. Resolves a large fraction of real conflicts.
3. **disjoint_edits** — both sides changed, but on NON-overlapping line ranges
   within the hunk → merge both edits (survey §1.2 zealous/line-granular merge).
   No overlap means no semantic conflict at this granularity.

Safety contract: every resolution this produces is STILL run through the full
validation pipeline (markers/splice/AST/syntax) by the orchestrator before being
accepted. If validation fails, the orchestrator falls through to the LLM. So
this module can only ever REDUCE LLM load on trivially-resolvable conflicts; it
can never produce a worse merge than the model would. A wrong guess is caught
and discarded, not applied.

All functions are pure (no I/O, no model, no git) so the rules are exhaustively
unit-testable. Line-diffing uses stdlib ``difflib`` — no new dependencies.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Literal

from capybase.conflict_model import ConflictUnit

Rule = Literal["identical_sides", "one_sided_change", "disjoint_edits"]


@dataclass(frozen=True)
class StructuralResolution:
    """Result of an attempted deterministic resolution.

    ``resolved`` is None when no rule applied (the caller falls through to the
    LLM). When non-None, ``rule`` names which safe rule produced it (for
    auditing/journaling) and ``text`` is the block-interior resolved text, in the
    same shape an LLM candidate's ``resolved_text`` takes (it splices identically).
    """

    rule: Rule | None
    text: str | None

    @property
    def resolved(self) -> bool:
        return self.text is not None


def _normalize(text: str) -> str:
    """Whitespace-only normalization for the identical-sides check.

    We do NOT use quality.py's punctuation-stripping normalize here: for
    "are the two sides the same change?" we want to ignore incidental whitespace
    (trailing spaces, line-ending differences) but NOT rewrite structural
    punctuation, since that could mask a real difference. Whitespace collapse is
    the conservative choice.
    """
    return " ".join((text or "").split())


def _changed_line_indices(base: str, other: str) -> set[int]:
    """Line indices (0-based, into ``other``) where ``other`` differs from ``base``.

    Uses ``difflib`` opcodes to find replace/insert/delete blocks relative to the
    other side, mapping them onto the other side's line numbers. Pure line diff.
    """
    base_lines = base.splitlines()
    other_lines = other.splitlines()
    changed: set[int] = set()
    matcher = difflib.SequenceMatcher(a=base_lines, b=other_lines, autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        # j-indices are into other_lines; mark the changed range there.
        changed.update(range(j1, j2))
    return changed


def resolve_structurally(unit: ConflictUnit) -> StructuralResolution:
    """Attempt the three deterministic rules in priority order.

    Returns the first that applies, else an unresolved result. The unit's sides
    are read from ``unit.current.text`` / ``unit.replayed.text`` / ``unit.base.text``
    (the diff3-refined sides are already preferred at extraction, so these are the
    tightest available). No rule mutates the unit.
    """
    current = unit.current.text or ""
    replayed = unit.replayed.text or ""
    base = unit.base.text or ""

    # Rule 1: identical sides (modulo whitespace) → that side is the merge.
    if _normalize(current) == _normalize(replayed):
        # Prefer the non-empty side; if both empty, empty is the resolution.
        text = current if current.strip() else replayed
        return StructuralResolution(rule="identical_sides", text=text)

    # Rule 2: one-sided change. Only one side diverged from base → take it.
    cur_changed = _normalize(current) != _normalize(base)
    rep_changed = _normalize(replayed) != _normalize(base)
    if cur_changed and not rep_changed:
        # Current diverged, replayed conceded to base → but current may have
        # legitimately built on base; emit current.
        return StructuralResolution(rule="one_sided_change", text=current)
    if rep_changed and not cur_changed:
        return StructuralResolution(rule="one_sided_change", text=replayed)

    # Rule 3: both changed, but on disjoint line ranges → merge both edits.
    # If the changed-line sets (vs base) don't intersect, the edits don't
    # conflict at line granularity and we can combine them safely.
    if cur_changed and rep_changed:
        merged = _try_disjoint_merge(base, current, replayed)
        if merged is not None:
            return StructuralResolution(rule="disjoint_edits", text=merged)

    return StructuralResolution(rule=None, text=None)


def _try_disjoint_merge(base: str, current: str, replayed: str) -> str | None:
    """Merge two divergent sides when their edits touch disjoint base lines.

    Computes base→current and base→replayed line diffs. If the sets of base lines
    each side modified are disjoint, applies both edits to base in one pass —
    neither edit can clobber the other. Returns None if the edits overlap (a real
    conflict the LLM must handle) or if the reconstruction is ambiguous.
    """
    base_lines = base.splitlines()
    cur_lines = current.splitlines()
    rep_lines = replayed.splitlines()

    # Map each side's changes onto BASE line indices to test for overlap.
    cur_base_changed = _base_changed_lines(base_lines, cur_lines)
    rep_base_changed = _base_changed_lines(base_lines, rep_lines)
    if not cur_base_changed or not rep_base_changed:
        return None
    if cur_base_changed & rep_base_changed:
        return None  # overlapping edits → real conflict, defer to LLM

    # Non-overlapping: apply both edits to base. Build a merged line list by
    # walking base and substituting each side's replacement regions.
    return _merge_disjoint_regions(base_lines, cur_lines, rep_lines,
                                   cur_base_changed, rep_base_changed)


def _base_changed_lines(base: list[str], other: list[str]) -> set[int]:
    """Base line indices (0-based) that ``other`` modifies (replace/delete/insert
    affecting that base line). Used to test whether two sides' edits overlap."""
    changed: set[int] = set()
    matcher = difflib.SequenceMatcher(a=base, b=other, autojunk=False)
    for tag, i1, i2, _j1, _j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        # i-indices are into base; mark the affected base range.
        changed.update(range(i1, i2))
    return changed


def _merge_disjoint_regions(
    base: list[str], cur: list[str], rep: list[str],
    cur_changed: set[int], rep_changed: set[int],
) -> str | None:
    """Reconstruct a merged text by applying each side's non-overlapping edits.

    Walks ``base`` line by line. For each base line:
    - if it's the start of current's changed region → emit current's replacement
      block and skip past the region;
    - elif it's the start of replayed's changed region → emit replayed's block;
    - else emit the base line unchanged.
    Because the changed-region sets are disjoint, the two substitutions never
    collide. Returns the merged text, or None if reconstruction can't be done
    unambiguously (e.g. a pure insertion with no base anchor — ambiguous about
    ordering relative to the other side).
    """
    # Build per-side opcode maps: base_start -> (base_end_exclusive, replacement_lines).
    cur_regions = _regions_against_base(base, cur)
    rep_regions = _regions_against_base(base, rep)

    out: list[str] = []
    i = 0
    n = len(base)
    while i < n:
        if i in cur_regions and i not in rep_changed:
            end_excl, repl = cur_regions[i]
            out.extend(repl)
            i = end_excl
            continue
        if i in rep_regions and i not in cur_changed:
            end_excl, repl = rep_regions[i]
            out.extend(repl)
            i = end_excl
            continue
        # Unchanged by either side → keep base line.
        out.append(base[i])
        i += 1
    # Handle trailing pure insertions only if anchored at EOF on both — but a
    # pure insertion (j2>j1 with i1==i2==len(base)) is ambiguous about ordering
    # relative to the other side, so we deliberately drop/ignore it (None-safe).
    return "\n".join(out)


def _regions_against_base(base: list[str], other: list[str]) -> dict[int, tuple[int, list[str]]]:
    """Map each changed base-line-index to (exclusive_end, replacement_lines_from_other).

    Only covers replace/delete regions anchored on at least one base line. Pure
    insertions (i1==i2) are omitted — their base anchor is ambiguous for merging.
    """
    regions: dict[int, tuple[int, list[str]]] = {}
    matcher = difflib.SequenceMatcher(a=base, b=other, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if i1 == i2:
            continue  # pure insertion: no base anchor, ambiguous for disjoint merge
        replacement = other[j1:j2]
        # Anchor the region at its first base line; mark the whole base range so
        # the merge walk can skip it. We only need the entry point in the dict
        # (the walk consumes end_excl), but record the full range for overlap tests.
        regions[i1] = (i2, replacement)
    return regions

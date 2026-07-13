"""Merge-intent analysis: what each side of a conflict *did*, and silent-
resurrection detection.

Two pure, git-free analyses that disambiguate the two hardest auto-rebase
failure modes:

1. **Modify/delete ambiguity.** A conflict unit's three sides (base / current
   / replayed) are shown as raw text, but never labelled by what each side
   *did*. When upstream deliberately deleted a block (``current`` empty, base
   non-empty) and the replayed branch kept it, the bundle presents the
   non-empty replayed side as if it were an *addition* — misleading both the
   model and the human. :func:`classify_side` / :func:`direction` label each
   side's intent (``added`` / ``deleted`` / ``modified`` / ``unchanged``) so
   the display and the ``delete_side`` structural rule can act on it.

2. **Silent resurrection.** Git's 3-way merge can resolve *cleanly* (no
   markers) while resurrecting dead code the ``onto`` branch deliberately
   deleted — because the replayed branch predates the cleanup. Git sees no
   conflict; capybase historically saw no conflict either, and the cleanup
   was silently undone. :func:`detect_resurrection` finds content blocks
   present in ``base``, removed by ``ours`` (the deletion intent), that
   reappear in the merge ``result``. The git layer
   (:mod:`capybase.resurrection`) feeds it the right blobs.

Everything here is a pure function of text — no git, no model, no I/O — so the
hard logic is exhaustively unit-testable without a repository. Line diffing
uses histogram diff (:mod:`capybase.diff`, no new dependencies), the same approach
:mod:`structural_resolver` and :mod:`conflict_extractor` already use.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from capybase.diff import line_matcher

SideKind = Literal["unchanged", "added", "deleted", "modified"]

# Direction summary kinds. ``modify_delete`` is the dangerous ambiguous case:
# one side deleted base content while the other kept/changed it — exactly the
# edit_file.rs situation where the bundle made a deletion look like an addition.
ConflictKind = Literal[
    "both_unchanged",
    "one_unchanged",
    "modify_delete",
    "delete_delete",
    "both_add",
    "both_modify",
    "add_modify",
]


@dataclass(frozen=True)
class SideDirections:
    """Per-side intent labels + a summary ``kind`` for the whole conflict.

    ``current`` is the upstream/``onto`` side; ``replayed`` is the replayed
    commit side; ``base`` is the common ancestor. ``kind`` classifies the
    conflict shape so callers (the bundle display, the structural resolver's
    ``delete_side`` rule) can branch on it without recomputing the diffs.
    """

    base: SideKind
    current: SideKind
    replayed: SideKind
    kind: ConflictKind
    # Human-readable summary, e.g. "modify/delete: CURRENT_UPSTREAM_SIDE deleted
    # this block". Ready to drop into a bundle / interactive view verbatim.
    summary: str
    # Which side (if any) deliberately deleted base content. None unless a side
    # is classified ``deleted``. Values: "current" | "replayed" | None. When both
    # sides deleted, this is None (delete_delete is not ambiguous).
    deleting_side: Literal["current", "replayed"] | None = None


@dataclass(frozen=True)
class ResurrectedBlock:
    """A block of base content that ``ours`` removed but ``result`` brought back.

    ``coverage`` ∈ [0, 1] is the fraction of the deleted block's lines that
    reappear (contiguously) in ``result`` — 1.0 means the block is back whole.
    ``base_span`` is the 0-based [start, end) line range within ``base``.
    """

    text: str
    base_span: tuple[int, int]
    coverage: float
    result_line_count: int = 0
    # The ``ours`` side's lines that were removed (length of the block), for
    # the caller's size filtering.
    block_line_count: int = 0
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Side classification
# ---------------------------------------------------------------------------


def _nonblank(lines: list[str]) -> int:
    """Count of non-blank lines — the size signal for classification."""
    return sum(1 for ln in lines if ln.strip())


def classify_side(base: str, side: str) -> SideKind:
    """What ``side`` did to ``base``: unchanged / added / deleted / modified.

    Pure line-diff classification. Definitions:

    - ``unchanged`` — ``side`` is textually identical to ``base`` (the side
      conceded; no edit).
    - ``deleted`` — ``side`` removed base lines and added ~nothing. The pure
      deletion case (includes ``side`` empty while ``base`` non-empty).
    - ``added`` — ``base`` was empty/near-empty and ``side`` grew it. The pure
      addition case.
    - ``modified`` — both insertions and deletions, or a same-size replace.

    The split between ``deleted`` and ``modified`` uses the diff opcodes: if
    the side has deletions but no insertions/replaces (nothing new added), it
    is a clean deletion; if it also adds content it is a modification. This is
    what distinguishes "upstream deleted the block" from "upstream rewrote it".
    """
    base_lines = base.splitlines()
    side_lines = side.splitlines()
    if base_lines == side_lines:
        return "unchanged"

    # An effectively-empty base (no nonblank lines) grown into real content is a
    # pure addition — regardless of stray blank lines, which a naive opcode walk
    # would miscount as a replace/deletion. Check this before the empty-side
    # branch so base="" / base="\n\n" both classify as additions, not modified.
    nb_base = _nonblank(base_lines)
    nb_side = _nonblank(side_lines)
    if nb_base == 0:
        return "added" if nb_side > 0 else "unchanged"

    # Pure deletion: side dropped base content and added nothing of substance.
    # The canonical modify/delete conflict has ``side`` empty with base full.
    if nb_side == 0:
        return "deleted"

    matcher = line_matcher(base_lines, side_lines)
    deleted = 0  # base lines removed
    added = 0  # side lines introduced (insert or replace's b-half)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            added += j2 - j1
        elif tag == "delete":
            deleted += i2 - i1
        elif tag == "replace":
            deleted += i2 - i1
            added += j2 - j1

    # Pure addition: base was empty/tiny and side grew; no meaningful deletion.
    if deleted == 0 and added > 0:
        return "added"
    # Pure deletion: nothing new added; only base content removed.
    if added == 0 and deleted > 0:
        return "deleted"
    if deleted == 0 and added == 0:
        return "unchanged"
    return "modified"


def direction(base: str, current: str, replayed: str) -> SideDirections:
    """Classify both sides' intent and summarize the conflict shape.

    ``current`` is the upstream/``onto`` side, ``replayed`` the replayed commit.
    The returned :class:`SideDirections` carries per-side labels, a summary
    ``kind``, a ready-to-render ``summary`` string, and ``deleting_side``
    (which side, if any, made a clean deletion — the hook for the
    ``delete_side`` structural rule and the bundle annotation).
    """
    cur_kind = classify_side(base, current)
    rep_kind = classify_side(base, replayed)

    cur_deleted = cur_kind == "deleted"
    rep_deleted = rep_kind == "deleted"
    cur_added = cur_kind == "added"
    rep_added = rep_kind == "added"

    deleting_side: Literal["current", "replayed"] | None = None
    if cur_deleted and not rep_deleted:
        deleting_side = "current"
    elif rep_deleted and not cur_deleted:
        deleting_side = "replayed"

    # Determine the summary kind. The delete cases are checked before
    # one_unchanged because a delete vs unchanged is modify/delete (the
    # dangerous ambiguous case), NOT a one-sided concession.
    if cur_kind == "unchanged" and rep_kind == "unchanged":
        kind: ConflictKind = "both_unchanged"
        summary = "both sides identical to base (no real conflict)"
    elif cur_deleted and rep_deleted:
        kind = "delete_delete"
        summary = "both sides deleted this block (no ambiguity)"
    elif cur_deleted or rep_deleted:
        # modify/delete: the dangerous ambiguous case. This includes a delete
        # vs unchanged (one side removed base content, the other kept it), so
        # it must outrank the one_unchanged classification below.
        kind = "modify_delete"
        who = "CURRENT_UPSTREAM_SIDE" if cur_deleted else "REPLAYED_COMMIT_SIDE"
        other = "REPLAYED_COMMIT_SIDE" if cur_deleted else "CURRENT_UPSTREAM_SIDE"
        summary = f"modify/delete: {who} DELETED this block; {other} kept/changed it"
    elif cur_kind == "unchanged" or rep_kind == "unchanged":
        kind = "one_unchanged"
        summary = "one-sided change (one side conceded to base)"
    elif cur_added and rep_added:
        kind = "both_add"
        summary = "both sides added content (no shared base in this block)"
    elif (cur_added or rep_added) and not (cur_deleted or rep_deleted):
        kind = "add_modify"
        summary = "one side added, the other modified"
    else:
        kind = "both_modify"
        summary = "both sides modified shared base content"

    return SideDirections(
        base="unchanged",  # base is the reference; it didn't "do" anything
        current=cur_kind,
        replayed=rep_kind,
        kind=kind,
        summary=summary,
        deleting_side=deleting_side,
    )


# ---------------------------------------------------------------------------
# Silent-resurrection detection
# ---------------------------------------------------------------------------


def _removed_regions(base: str, ours: str) -> list[tuple[int, int, list[str]]]:
    """Maximal runs of ``base`` lines that ``ours`` removed.

    Returns ``(base_start, base_end, lines)`` tuples (0-based, end exclusive)
    for each clean deletion — base content that ``ours`` dropped without
    replacing it. ``replace`` opcodes are excluded: those are modifications
    (ours changed the content, so the original is not cleanly "gone"), and a
    rewritten block reappearing in result is a weaker, noisier signal than a
    pure deletion reappearing. Only adjacent ``delete`` runs (already maximal
    in a single opcode) are reported.
    """
    base_lines = base.splitlines()
    ours_lines = ours.splitlines()
    regions: list[tuple[int, int, list[str]]] = []
    for tag, i1, i2, _j1, _j2 in line_matcher(
        base_lines, ours_lines
    ).get_opcodes():
        if tag == "delete" and i2 > i1:
            regions.append((i1, i2, base_lines[i1:i2]))
    return regions


def _coverage_against(block_lines: list[str], result_lines: list[str]) -> float:
    """Fraction of ``block_lines`` that reappears contiguously in ``result``.

    Uses :meth:`SequenceMatcher.get_matching_blocks` to find how much of the
    deleted block survives in ``result``. A coverage near 1.0 means the block
    is back whole; near 0.0 means it stayed deleted. This is robust to the
    block appearing anywhere in ``result`` and to unrelated surrounding context.
    """
    if not block_lines:
        return 0.0
    matcher = line_matcher(block_lines, result_lines)
    matched = sum(m.size for m in matcher.get_matching_blocks())
    return matched / len(block_lines)


def detect_resurrection(
    base: str,
    ours: str,
    result: str,
    *,
    min_block_lines: int = 3,
    min_coverage: float = 0.85,
) -> list[ResurrectedBlock]:
    """Find base content that ``ours`` deleted but ``result`` resurrected.

    ``ours`` is the side that expressed a *deletion intent* (typically the
    ``onto``/upstream branch that cleaned up dead code). ``result`` is the
    merged content (typically the post-rebase file). The function reports each
    maximal block of base content that ``ours`` removed — and that reappears
    (at ``>= min_coverage`` line coverage) in ``result``. Blocks smaller than
    ``min_block_lines`` non-blank lines are ignored, since tiny reappearances
    (a lone blank line, a one-line import) are usually coincidental, not a
    resurrection of deliberately-removed code.

    Returns findings sorted largest-first (by block size, then coverage). Empty
    when ``ours`` deleted nothing or none of the deletions came back — i.e. the
    common, safe case. Pure function; no git, no I/O.
    """
    result_lines = result.splitlines()
    findings: list[ResurrectedBlock] = []
    for start, end, lines in _removed_regions(base, ours):
        if _nonblank(lines) < min_block_lines:
            continue
        cov = _coverage_against(lines, result_lines)
        if cov >= min_coverage:
            findings.append(
                ResurrectedBlock(
                    text="\n".join(lines),
                    base_span=(start, end),
                    coverage=round(cov, 4),
                    result_line_count=len(result_lines),
                    block_line_count=len(lines),
                )
            )
    findings.sort(key=lambda b: (b.block_line_count, b.coverage), reverse=True)
    return findings

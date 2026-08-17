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
DeletionStability = Literal["stable", "transient", "absent"]

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


# ---------------------------------------------------------------------------
# Deletion stability (bounded history-walk verification)
# ---------------------------------------------------------------------------


def classify_deletion_stability(
    block_lines: list[str],
    blob_sequence: list[str],
    *,
    min_coverage: float = 0.85,
) -> DeletionStability:
    """Classify whether a deleted block's removal was permanent on its branch.

    Takes the deleted block's lines and the **sequence of blob texts** along one
    branch (oldest→newest, from merge-base to tip), and determines whether the
    block's deletion was:

    - **``stable``** — the block was present in early blobs, removed in a later
      blob, and ABSENT in every subsequent blob up to the tip. This is a
      deliberate cleanup: the content was removed and never came back on this
      branch. A stable deletion reappearing in the merge result is a real
      resurrection.
    - **``transient``** — the block was removed at some point but REAPPEARS in
      a later blob on the same branch. The removal was not permanent — the
      content was re-added (a revert, a re-introduction, or a divergent edit).
      A transient removal reappearing in the merge result is NOT a resurrection.
    - **``absent``** — the block was never present in any blob in the sequence.
      This means the deletion predates the merge-base (the block was already
      gone at the branch point), so its absence in the branch tip is not a
      cleanup decision within the merged window.

    Uses :func:`_coverage_against` (the same line-coverage metric
    :func:`detect_resurrection` uses) to check block presence at each commit.
    Pure function; no git, no I/O.

    ``blob_sequence`` is the per-commit blob texts for the path along the
    branch, oldest first. An empty sequence returns ``"absent"``.
    """
    if not blob_sequence or not block_lines:
        return "absent"

    # Check block presence at each commit in the sequence.
    # present[i] = True if the block's lines appear in blob_sequence[i].
    present = [
        _coverage_against(block_lines, blob.splitlines()) >= min_coverage
        for blob in blob_sequence
    ]

    # If the block was never present in the sequence, it's "absent" (the
    # deletion predates the merge-base window).
    if not any(present):
        return "absent"

    # If the block is present at the tip, it was never permanently deleted in
    # this window → not a resurrection scenario → "transient".
    if present[-1]:
        return "transient"

    # Walk oldest→newest looking for a present→absent→present transition.
    # If the block disappears and then reappears at ANY later point, the
    # deletion was transient (the content was re-introduced). This catches both
    # the simple "deleted then re-added" and the "deleted, re-added, deleted
    # again" patterns — in both, there's a present→absent→present transition.
    seen_absent = False
    for is_present in present:
        if not is_present:
            seen_absent = True
        elif seen_absent:
            # Block reappeared after being absent → transient.
            return "transient"

    # The block was present in early commits, then went absent and never
    # reappeared → stable (deliberate permanent deletion).
    return "stable"


# ---------------------------------------------------------------------------
# Full-file context — whole-file asymmetry signals for region-level decisions
# ---------------------------------------------------------------------------
#
# Region-level units (and their entity/statement sub-splits) carry FRAGMENT
# side texts; any decision computed from them about which side "deleted" is
# unreliable — the reverted parent_deletion_override rule (14681db) failed
# exactly there: full-file base diffed against region fragments inflated
# deletion counts for both sides, and fragment-size ratios cannot distinguish
# a true wholesale deletion (protobuf-0073) from a symmetric additive conflict
# (protobuf-0061). The functions below compute the authoritative whole-file
# signals from the pristine stage blobs, for stamping on every unit at
# extraction (``structural_metadata["full_file_context"]``) and for the
# whole-file-side takeover gates.

# Thresholds calibrated on the realworld corpus (74 cases). churn_ratio is
# |c-r|/max(c,r) of full-file changed-line counts vs base; >= 0.90 separates
# a clean category — in all 17 such cases the oracle is one side verbatim
# (winner token-Jaccard >= 0.973, and the oracle's stale-fraction vs the
# winner is <= 0.021), and below ~0.75 a whole-side takeover becomes unsafe.
# Dominance (winner churn >= 0.3 x base lines) excludes small-churn
# high-ratio files where both sides made minor edits. Stale fraction is the
# share of the merge's stripped non-blank line-set absent from the winner —
# the failing protobuf-0073 merge measures 0.364 (kept the stale side's
# conflict-region content on top of the winner's clean deletions, which git
# had already applied to the shared context — so only the region's ~70 lines
# are stale, not all 573 deleted lines; a deletion-count metric reads 0.15
# and misses it).
FULL_FILE_ASYMMETRY_RATIO = 0.90
FULL_FILE_DOMINANCE_FRACTION = 0.30
FULL_FILE_STALE_FRACTION = 0.15
# Absolute contamination floor: a fractional threshold alone mis-fires on
# tiny merges (winner reduced to 5 lines + 1 legitimate loser line = 17%
# stale). Require a meaningful amount of stale content too.
FULL_FILE_STALE_MIN_LINES = 3

# Mid-band subsumption gates (jsonc-0004 class): one side's churn dominates
# the other's by a large multiple while the normalized ratio sits BELOW the
# wholesale band — 0.55 <= ratio < 0.90 with winner/loser churn >= 2.5x.
# Corpus measurement (372 C cases, 116 in-band): 100/116 mid-band oracles
# equal the winner side (token-Jaccard >= 0.95), but the 16 counter-examples
# (jsonc-0015, clickhouse-0015/0021/0043, sqlite-0098/0099/0109, ...) are
# genuine both-sides merges whose numbers are indistinguishable from the
# safe cases on every shape metric — the discriminator is semantic, so the
# mid-band takeover fires only when the LLM subsumption adjudication
# agrees (orchestrator `_adjudicate_subsumption`), never on numbers alone.
FULL_FILE_MIDBAND_RATIO_MIN = 0.55
FULL_FILE_MIDBAND_DOMINANCE_MULT = 2.5


def side_churn(base_text: str, side_text: str) -> int:
    """Absolute changed-line count of one side vs the base (both directions).

    The full-file analogue of the fragment diffs in structural_resolver —
    same histogram-diff seam (``line_matcher``), applied to whole files.
    """
    b = base_text.splitlines()
    s = side_text.splitlines()
    n = 0
    for tag, i1, i2, j1, j2 in line_matcher(b, s).get_opcodes():
        if tag != "equal":
            n += (i2 - i1) + (j2 - j1)
    return n


def full_file_context(base_text: str, current_text: str, replayed_text: str) -> dict:
    """Whole-file three-way shape summary, for stamping on every unit.

    Keys: line counts; ``current_churn``/``replayed_churn`` (changed lines
    vs base); ``churn_ratio`` (normalized asymmetry); ``asymmetry_side``
    (the higher-churn side when the ratio clears
    ``FULL_FILE_ASYMMETRY_RATIO``, else None — the side whose wholesale
    rewrite/deletion carries the merge intent); ``deleting_side`` (the
    asymmetry side when it also NET-removed >= 30% of the base's lines —
    the side whose deletions are at resurrection risk; informational);
    ``dominant_churn``. Pure numbers — no texts, so it is cheap to stamp
    on every unit and inherit into sub-splits.
    """
    c = side_churn(base_text, current_text)
    r = side_churn(base_text, replayed_text)
    ratio = abs(c - r) / max(c, r, 1)
    winner = "current" if c >= r else "replayed"
    base_n = len(base_text.splitlines())
    winner_lines = len(
        (current_text if winner == "current" else replayed_text).splitlines()
    )
    asymmetry_side = winner if ratio >= FULL_FILE_ASYMMETRY_RATIO else None
    return {
        "base_lines": base_n,
        "current_lines": len(current_text.splitlines()),
        "replayed_lines": len(replayed_text.splitlines()),
        "current_churn": c,
        "replayed_churn": r,
        "churn_ratio": round(ratio, 4),
        "asymmetry_side": asymmetry_side,
        "deleting_side": (
            asymmetry_side
            if asymmetry_side is not None and winner_lines <= 0.70 * base_n
            else None
        ),
        "dominant_churn": max(c, r),
    }


def midband_subsumption_gates(
    base_text: str,
    current_text: str,
    replayed_text: str,
) -> dict:
    """Mid-band takeover gates: churn-dominant but not wholesale (0004 class).

    ``full_file_context`` covers the >= 0.90 wholesale band where taking the
    winner verbatim is safe on numbers alone. This gate covers the band just
    below it — one side's churn dominates the other's by >=
    ``FULL_FILE_MIDBAND_DOMINANCE_MULT`` while ``0.55 <= churn_ratio < 0.90``.
    Numbers-only in-band is NOT sufficient to act (16/116 corpus
    counter-examples are genuine both-sides merges — see the constants); the
    orchestrator additionally requires the LLM subsumption adjudication to
    say the winner's rewrite covers the loser's intent before firing.

    Returns the gate values (journalable) with an ``in_band`` key and the
    churn-``winner``/``loser`` side names the adjudication prompt needs.
    """
    ctx = full_file_context(base_text, current_text, replayed_text)
    c, r = ctx["current_churn"], ctx["replayed_churn"]
    mult = max(c, r) / max(1, min(c, r))
    winner = "current" if c >= r else "replayed"
    in_band = (
        FULL_FILE_MIDBAND_RATIO_MIN <= ctx["churn_ratio"] < FULL_FILE_ASYMMETRY_RATIO
        and mult >= FULL_FILE_MIDBAND_DOMINANCE_MULT
    )
    return {
        "churn_ratio": ctx["churn_ratio"],
        "churn_mult": round(mult, 2),
        "current_churn": c,
        "replayed_churn": r,
        "base_lines": ctx["base_lines"],
        "winner": winner,
        "loser": "replayed" if winner == "current" else "current",
        "in_band": in_band,
    }


def asymmetry_takeover_gates(
    base_text: str,
    current_text: str,
    replayed_text: str,
    merged_text: str,
) -> dict:
    """Evaluate the whole-file-side takeover gates for a candidate merge.

    Fires only when ALL of these hold (corpus-calibrated, see constants):

    1. ``churn_ratio >= 0.90`` — one side rewrote the file wholesale while
       the other barely touched it (the oracle in this regime is one side
       verbatim; below ~0.75 taking a side loses real mixed-merge content).
    2. dominance — the winning side's churn is >= 0.3 x base lines, so
       small-churn high-ratio files (both sides made minor edits) decline.
    3. staleness — >= 15% of the merge's line-set AND >= 3 lines are absent
       from the winning side's file: the merge kept meaningful stale-side
       content the winner superseded. Lines are whitespace-collapsed before
       set insertion so indentation drift doesn't inflate the fraction.
       Corpus separation is 17x (failing protobuf-0073 merge: 0.364; worst
       good merge in the band: 0.021). Note git's shared context already
       carries the winner's CLEAN deletions, so this — not a deleted-line
       resurrection count — is the measurable trace of the failure.

    The gate payload journals the stale-line count and a sample of the
    stale lines so post-hoc analysis can see WHAT the takeover would drop
    (the loser's-small-fix edge case: a legitimate small addition riding
    inside the stale content).

    Returns the gate values (all journalable) with a ``fires`` key.
    """
    ctx = full_file_context(base_text, current_text, replayed_text)
    ratio_ok = ctx["churn_ratio"] >= FULL_FILE_ASYMMETRY_RATIO
    dominance_ok = ctx["dominant_churn"] >= (
        FULL_FILE_DOMINANCE_FRACTION * max(ctx["base_lines"], 1)
    )
    winner = ctx["asymmetry_side"]
    gates: dict = {
        "churn_ratio": ctx["churn_ratio"],
        "ratio_ok": ratio_ok,
        "dominance_ok": dominance_ok,
        "winner": winner,
        "current_churn": ctx["current_churn"],
        "replayed_churn": ctx["replayed_churn"],
        "base_lines": ctx["base_lines"],
        "stale_fraction": 0.0,
        "stale_lines": 0,
        "stale_ok": False,
        "stale_sample": [],
    }
    if not (ratio_ok and dominance_ok and winner is not None):
        gates["fires"] = False
        return gates
    winner_text = current_text if winner == "current" else replayed_text

    def _line_set(text: str) -> set[str]:
        # Whitespace-collapsed: semantically identical lines with different
        # indentation must not count as stale.
        return {
            "".join(ln.split())
            for ln in text.splitlines()
            if ln.strip()
        }

    merged_set = _line_set(merged_text)
    winner_set = _line_set(winner_text)
    stale = merged_set - winner_set
    frac = len(stale) / len(merged_set) if merged_set else 0.0
    gates["stale_fraction"] = round(frac, 4)
    gates["stale_lines"] = len(stale)
    gates["stale_ok"] = (
        frac >= FULL_FILE_STALE_FRACTION
        and len(stale) >= FULL_FILE_STALE_MIN_LINES
    )
    # First few stale lines, whitespace-resqueezed for readability.
    gates["stale_sample"] = [
        (s[:70] + "…") if len(s) > 70 else s
        for s in sorted(stale)[:3]
    ]
    gates["fires"] = bool(gates["stale_ok"])
    return gates


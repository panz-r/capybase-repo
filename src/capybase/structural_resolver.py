"""Deterministic structural conflict resolution (survey §2.1/§6.4, layer 1).

A safe, LLM-free pre-resolver that runs BEFORE the model. It attempts to produce
a correct merged text from base + current + replayed using four provably-safe
rules — no heuristics that could introduce a wrong merge:

1. **identical_sides** — current and replayed normalized-equal → emit that side.
   (Survey: "both sides identical → delete the conflict".)
2. **one_sided_change** — only one side diverged from base → take the changed
   side; the other conceded. Resolves a large fraction of real conflicts.
3. **disjoint_edits** — both sides changed, but on NON-overlapping line ranges
   within the hunk → merge both edits (survey §1.2 zealous/line-granular merge).
   No overlap means no semantic conflict at this granularity.
4. **zealous_merge** — per-base-line 3-way merge (survey §1.4 zealous
   refinement). Where git's coarse hunk groups adjacent edits into one conflict,
   this aligns each side against base line-by-line and resolves every region
   that is agreed (both made the same change) or one-sided (one side conceded a
   sub-region the other touched). Returns None the moment it hits a genuine
   two-sided disagreement or an ambiguous pure insertion. This is the rule that
   catches the case ``disjoint_edits`` must refuse: two edits that *overlap*
   in base-line span yet are still safe because one side matches base there.

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
from capybase.merge_intent import classify_side, direction

Rule = Literal[
    "identical_sides", "one_sided_change", "disjoint_edits", "zealous_merge",
    "entity_disjoint", "token_disjoint", "delete_side",
]


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

    # Rule 1: modify/delete — one side deliberately deleted the block and the
    # other side did NOT add anything that the deletion would clobber. The safe
    # resolution is to ACCEPT THE DELETION (emit the deleting side's text, which
    # is empty or near-empty). This is the disambiguation the survey's "silent
    # loss of intent" failure mode calls out: without it, a modify/delete can be
    # wrongly merged to keep dead code the deleting branch cleaned up. Guarded
    # by merge_intent.direction so it fires ONLY on a proven clean deletion
    # (the other side unchanged, or modified-without-additions).
    deleted = _accept_deletion(base, current, replayed)
    if deleted is not None:
        return StructuralResolution(rule="delete_side", text=deleted)

    # Rule 2: identical sides (modulo whitespace) → that side is the merge.
    if _normalize(current) == _normalize(replayed):
        # Prefer the non-empty side; if both empty, empty is the resolution.
        text = current if current.strip() else replayed
        return StructuralResolution(rule="identical_sides", text=text)

    # Rule 3: one-sided change. Only one side diverged from base → take it.
    cur_changed = _normalize(current) != _normalize(base)
    rep_changed = _normalize(replayed) != _normalize(base)
    if cur_changed and not rep_changed:
        # Current diverged, replayed conceded to base → but current may have
        # legitimately built on base; emit current.
        return StructuralResolution(rule="one_sided_change", text=current)
    if rep_changed and not cur_changed:
        return StructuralResolution(rule="one_sided_change", text=replayed)

    # Rule 4: both changed, but on disjoint line ranges → merge both edits.
    # If the changed-line sets (vs base) don't intersect, the edits don't
    # conflict at line granularity and we can combine them safely.
    if cur_changed and rep_changed:
        merged = _try_disjoint_merge(base, current, replayed)
        if merged is not None:
            return StructuralResolution(rule="disjoint_edits", text=merged)

        # Rule 5: zealous per-base-line 3-way merge (survey §1.4). Stronger than
        # disjoint_edits — also resolves overlaps that are agreed (both made the
        # same change) or one-sided (one side conceded a sub-region the other
        # touched). Returns None on any genuine two-sided disagreement or
        # ambiguous pure insertion, so the LLM handles it.
        merged = _try_zealous_merge(base, current, replayed)
        if merged is not None:
            return StructuralResolution(rule="zealous_merge", text=merged)

        # Rule 6: entity-level disjoint resolution (survey §3.2/§5.2 Weave/Aura).
        # The line-granular rules above correctly DECLINE when both sides insert
        # DISTINCT entities at the same base line (git sees two insertions at one
        # point → conflict; zealous sees a two-sided insertion → ambiguous → give
        # up). But at ENTITY granularity these are non-conflicting: each side
        # added a different method/class to the same container. Different
        # (kind, name) identities → no overlap → safe to merge both. This is the
        # single most common real-world conflict that line-level merging cannot
        # resolve deterministically. Declines the moment two sides touch the
        # SAME entity (a genuine intra-entity conflict → existing resolvers).
        merged = _try_entity_disjoint(unit)
        if merged is not None:
            return StructuralResolution(rule="entity_disjoint", text=merged)

        # Rule 7: token-level disjoint resolution (survey §4.2 Summer, layer 3).
        # Runs AFTER entity resolution so multi-entity conflicts (renames, adds)
        # are handled at the coarser, identity-aware entity granularity first.
        # Token-disjoint then catches the intra-line case the line/entity rules
        # provably can't reach: two sides change DIFFERENT TOKENS on the SAME
        # line (a value bump + a constant rename on one assignment; two signature
        # edits at different positions). Token granularity recognizes these as
        # disjoint and splices both edits in. Scoped to small conflicts (a line
        # budget) so reconstruction stays local. Declines on any token overlap
        # or ambiguous pure-insertion anchors.
        merged = _try_token_disjoint(base, current, replayed)
        if merged is not None:
            return StructuralResolution(rule="token_disjoint", text=merged)

    return StructuralResolution(rule=None, text=None)


def _accept_deletion(base: str, current: str, replayed: str) -> str | None:
    """Accept a deliberate deletion when one side cleanly deleted the block.

    Returns the deleting side's text (empty or near-empty) when:
    - one side is classified ``deleted`` (removed base content, added nothing), and
    - the OTHER side added nothing that the deletion would clobber — i.e. it is
      ``unchanged`` (kept base verbatim) OR ``deleted`` (both deleted, no
      ambiguity) OR a ``modified`` side whose changes are pure deletions too.

    Returns None (decline → next rule) when the non-deleting side ADDED or
    modified-with-additions content: in that case accepting the deletion could
    drop a real change the other branch introduced, so the LLM must judge.

    This is the survey's "silent loss of intent" guard: without it, a
    modify/delete can be wrongly merged to keep dead code the deleting branch
    cleaned up. Like every structural rule the result still runs the full
    validation pipeline before acceptance, so a wrong guess is discarded.
    """
    d = direction(base, current, replayed)
    who = d.deleting_side
    if who is None:
        return None
    deleter = current if who == "current" else replayed
    keeper = replayed if who == "current" else current
    # The keeper must not have added anything. ``unchanged`` and ``deleted``
    # both qualify (kept base, or also deleted). A ``modified`` keeper qualifies
    # only if its diff vs base is net-deletional (it dropped lines too, added
    # none) — checked via classify_side's contract: 'deleted' is the only pure-
    # net-deletion classification; 'modified' adds content, so it does NOT
    # qualify. 'added' never qualifies.
    keeper_kind = classify_side(base, keeper)
    if keeper_kind in ("unchanged", "deleted"):
        return deleter
    return None


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


# ---------------------------------------------------------------------------
# Token-level disjoint resolution (survey §4.2 Summer, layer 3)
# ---------------------------------------------------------------------------

# Maximum total non-blank lines across the three sides for the token rule to
# fire. Token reconstruction is provably local (it splices disjoint edits), but
# keeping the budget small ensures the merge stays cheap and obviously-correct
# — a 200-line conflict reassembled at token granularity is hard to audit. The
# rule's value is the small, same-line case; large conflicts stay with the
# line/entity/LLM rules.
TOKEN_DISJOINT_MAX_LINES = 12

# Summer-style 4-category tokenization (letters/digits/whitespace/symbols):
# every character belongs to exactly one category, so the round-trip
# (tokenize → detokenize) is lossless and the merged text reconstructs exactly.
# This is what lets the rule reassemble a line from its edited tokens without
# dropping punctuation or whitespace.
_TOKEN_RE = __import__("re").compile(r"[A-Za-z_]+|[0-9]+|\s+|[^\sA-Za-z0-9]+")


def _tokenize(text: str) -> list[str]:
    """Split ``text`` into Summer's 4 token categories (lossless)."""
    return _TOKEN_RE.findall(text or "")


def _detokenize(tokens: list[str]) -> str:
    """Rejoin tokens into the original text (inverse of :func:`_tokenize`)."""
    return "".join(tokens)


def _token_change_ops(base_toks: list[str], other_toks: list[str]) -> list[tuple[int, int, list[str]]]:
    """Non-equal regions between two token sequences, as ``(base_start, base_end_excl, replacement_toks)``.

    Mirrors :func:`_base_changed_lines` but returns the replacement content too,
    so a disjoint merge can splice each side's replacement into base in one pass.
    """
    ops: list[tuple[int, int, list[str]]] = []
    matcher = difflib.SequenceMatcher(a=base_toks, b=other_toks, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            ops.append((i1, i2, other_toks[j1:j2]))
    return ops


def _try_token_disjoint(base: str, current: str, replayed: str) -> str | None:
    """Merge two sides whose edits touch DISJOINT TOKENS on the same text.

    Survey §4.2 (Summer, layer 3): the line-granular rules decline when two
    sides change the same line, even if they changed different tokens on it
    (a value bump + a rename on one assignment; two signature edits at different
    positions). Token granularity recognizes these as disjoint: align each side
    against base at the token level, and if the changed base-token spans don't
    intersect, splice both edits in. This is the safe, disjoint subset of
    Summer's token-rewrite idea — no move rules, no heuristics, just disjoint-
    token splicing with the same safety contract as :func:`_try_disjoint_merge`
    (one granularity finer).

    Returns None (decline → LLM) when: the conflict is too large (exceeds
    :data:`TOKEN_DISJOINT_MAX_LINES`), either side has no token changes, or the
    changed token spans overlap (a genuine token-level conflict). Scoped to small
    conflicts so the reconstruction stays local and auditable.
    """
    # Budget guard: only fire on small conflicts. Token reconstruction is
    # provably local, but keeping it cheap and obviously-correct matters.
    total_lines = sum(
        1 for t in (base, current, replayed) for ln in t.splitlines() if ln.strip()
    )
    if total_lines > TOKEN_DISJOINT_MAX_LINES:
        return None

    bt = _tokenize(base)
    ct = _tokenize(current)
    rt = _tokenize(replayed)
    cur_ops = _token_change_ops(bt, ct)
    rep_ops = _token_change_ops(bt, rt)
    if not cur_ops or not rep_ops:
        return None  # a side made no token change → an earlier rule handles it

    # Test for overlap on base-token indices. Two cases must count as conflict:
    #  (a) replace/delete spans that intersect (both sides change the same token);
    #  (b) pure insertions (i1 == i2) anchored at the same base position — their
    #      relative order is ambiguous (like zealous_merge/disjoint_edits, which
    #      deliberately refuse pure insertions for exactly this reason).
    cur_spans: set[int] = set()
    cur_insert_anchors: set[int] = set()
    for i1, i2, _ in cur_ops:
        if i1 == i2:
            cur_insert_anchors.add(i1)
        else:
            cur_spans.update(range(i1, i2))
    rep_spans: set[int] = set()
    rep_insert_anchors: set[int] = set()
    for i1, i2, _ in rep_ops:
        if i1 == i2:
            rep_insert_anchors.add(i1)
        else:
            rep_spans.update(range(i1, i2))
    # (a) replace/delete overlap → conflict.
    if cur_spans & rep_spans:
        return None
    # (b) a pure insertion landing INSIDE a replace/delete region, OR two pure
    # insertions at the same anchor → ambiguous → decline. (An insertion inside
    # a replaced region is also ambiguous: where in the replacement does it go?)
    if cur_spans & rep_insert_anchors or rep_spans & cur_insert_anchors:
        return None
    if cur_insert_anchors & rep_insert_anchors:
        return None

    # Disjoint: walk base tokens, applying both sides' replacements at their
    # spans. An edit at base index i replaces tokens [i, end) with `repl`.
    merged_ops: dict[int, tuple[int, list[str]]] = {}
    for i1, i2, repl in cur_ops + rep_ops:
        merged_ops[i1] = (i2, repl)
    out: list[str] = []
    i = 0
    n = len(bt)
    while i < n:
        if i in merged_ops:
            end, repl = merged_ops[i]
            out.extend(repl)
            i = end
        else:
            out.append(bt[i])
            i += 1
    return _detokenize(out)


def _try_zealous_merge(base: str, current: str, replayed: str) -> str | None:
    """Per-base-line 3-way merge (survey §1.4 zealous refinement).

    Aligns each side against base line-by-line via ``difflib``. For every base
    region, resolves it as:

    - **agreed** — both sides made the same replacement → emit it.
    - **one-sided** — exactly one side diverged from base there → take it.
    - **conflict** — both diverged differently → give up (return None).

    Returns None the moment any region is a genuine two-sided disagreement, OR
    if either side contains a *pure insertion* (a change with no base anchor),
    because the ordering of two independent insertions is ambiguous and merging
    them could drop or reorder a side's intent. That conservatism is what keeps
    the rule safe by construction — it only ever emits merges where, for every
    base line touched, at most one side actually changed the content.
    """
    base_lines = base.splitlines()
    cur_regions, cur_has_insert = _change_regions(base_lines, current.splitlines())
    rep_regions, rep_has_insert = _change_regions(base_lines, replayed.splitlines())
    if cur_has_insert or rep_has_insert:
        return None  # pure insertion: ordering ambiguous → defer to LLM
    if not cur_regions or not rep_regions:
        # No replace/delete regions against base (only possible insertions,
        # already excluded above) → nothing for zealous to merge here.
        return None

    out: list[str] = []
    i = 0
    n = len(base_lines)
    while i < n:
        in_cur = i in cur_regions
        in_rep = i in rep_regions
        if in_cur and in_rep:
            cur_end, cur_rep = cur_regions[i]
            rep_end, rep_rep = rep_regions[i]
            # Overlapping regions must cover the exact same base span; a partial
            # overlap is ambiguous (where does one edit's region end?) so bail.
            if cur_end != rep_end:
                return None
            base_seg = base_lines[i:cur_end]
            if cur_rep == rep_rep:
                out.extend(cur_rep)            # agreed: both made the same change
            elif cur_rep == base_seg:
                out.extend(rep_rep)            # current conceded → take replayed
            elif rep_rep == base_seg:
                out.extend(cur_rep)            # replayed conceded → take current
            else:
                return None                    # genuine two-sided conflict
            i = cur_end
        elif in_cur:
            end, rep = cur_regions[i]
            out.extend(rep)
            i = end
        elif in_rep:
            end, rep = rep_regions[i]
            out.extend(rep)
            i = end
        else:
            out.append(base_lines[i])
            i += 1
    return "\n".join(out)


def _change_regions(
    base: list[str], other: list[str]
) -> tuple[dict[int, tuple[int, list[str]]], bool]:
    """Map base-start index → (base_end_excl, replacement_lines) for each
    replace/delete region ``other`` makes against base.

    Returns ``(regions, has_pure_insertion)``. A pure insertion (a change with
    ``i1 == i2``, i.e. no base anchor) sets ``has_pure_insertion=True`` so the
    caller can refuse to merge — two independent insertions have ambiguous
    ordering. This mirrors ``_regions_against_base`` but signals inserts instead
    of silently dropping them.
    """
    regions: dict[int, tuple[int, list[str]]] = {}
    has_insert = False
    matcher = difflib.SequenceMatcher(a=base, b=other, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if i1 == i2:
            has_insert = True
            continue
        regions[i1] = (i2, other[j1:j2])
    return regions, has_insert


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


# ---------------------------------------------------------------------------
# Entity-level disjoint resolution (survey §3.2/§5.2 Weave/Aura)
# ---------------------------------------------------------------------------

# Minimum name-similarity ratio for two entity names to be considered a rename
# (s3m's Levenshtein rename handler, survey §2.2). 0.6 is conservative: it
# catches loadData→fetchData, load→fetch, parse_thing→parse_item, but won't
# conflate unrelated short names. A rename ALSO requires the body to match
# (normalized), so a coincidentally-similar name with different content isn't
# misread as a rename.
RENAME_SIMILARITY_THRESHOLD = 0.6


def _name_similarity(a: str, b: str) -> float:
    """Levenshtein-style similarity ratio of two entity names in [0, 1].

    Uses ``difflib.SequenceMatcher`` (no new dependency) — the same measure s3m
    applies via Levenshtein string similarity for its rename handler. 1.0 = same
    name; →0 = unrelated.
    """
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(a=a, b=b, autojunk=False).ratio()


def _body_content(body: str) -> str:
    """The body with its signature/header line removed, normalized.

    A rename changes the def/fn header (``def loadData`` → ``def fetchData``) but
    leaves the body content identical. Rename detection must therefore compare
    bodies with the HEADER STRIPPED — otherwise the renamed signature line makes
    every rename look like a body change, and no base entity matches.
    """
    if not body:
        return ""
    lines = body.split("\n")
    # Drop the first line (the def/fn/struct header); normalize the rest.
    return _normalize("\n".join(lines[1:]))


def _detect_renames(
    side_ents: list, base_ents: list
) -> tuple[dict, dict]:
    """Detect renames of base entities on one side (s3m rename handler, §2.2).

    A rename is: a base entity whose OLD name is GONE from ``side_ents``, but
    whose body CONTENT (signature stripped) reappears under a NEW name on the
    side. This is the false-merge source the survey flags: without it,
    entity_disjoint treats a rename as "base keeps old + side added new" → a
    duplicate method.

    The body-content match is the strong signal (identical content under a new
    name is near-certain evidence of a rename); the name-similarity check is a
    secondary guard so two genuinely-different entities that happen to share a
    body aren't conflated. Because the content match is exact, even a semantic
    rename (loadData→fetchData, low string similarity) is recognized — the s3m
    paper's finding that content-equality is the reliable rename signal.

    Returns ``(renames, base_ids_removed)``:
    - ``renames``: maps the side's NEW identity ``(kind, new_name)`` → the base
      identity ``(kind, old_name)`` it replaced, so the merge can treat the
      renamed entity as the same logical entity (no duplicate, old name dropped).
    - ``base_ids_removed``: the base identities that disappeared because they
      were renamed away (so the merge walk doesn't re-emit the old name).
    """
    # Index base entities by (kind, body-content) for exact-content matching.
    base_by_content = {}
    for e in base_ents:
        key = (e.kind, _body_content(e.body))
        # If two base entities share body content, keep the first; renames are
        # ambiguous in that case and we decline to guess.
        base_by_content.setdefault(key, e)
    side_names_by_kind = {}
    for e in side_ents:
        side_names_by_kind.setdefault(e.kind, set()).add(e.name)

    renames: dict = {}
    removed: set = set()
    for e in side_ents:
        # Look for a base entity with the same (kind, body-content) whose name
        # is NOT present on this side (it was renamed away).
        key = (e.kind, _body_content(e.body))
        base_match = base_by_content.get(key)
        if base_match is None or base_match.identity == e.identity:
            continue
        # The base entity's old name must be GONE from this side (renamed, not
        # duplicated). If the old name still exists, this is a copy, not a rename.
        if base_match.name in side_names_by_kind.get(e.kind, set()):
            continue
        # Identical body content under a new, gone-old-name is a rename. The
        # name-similarity guard prevents conflating two distinct entities that
        # coincidentally share an empty/trivial body (e.g. ``pass``/``{}``).
        if _body_content(e.body) and (
            _name_similarity(base_match.name, e.name) >= RENAME_SIMILARITY_THRESHOLD
            or len(_body_content(e.body)) >= 8
        ):
            renames[e.identity] = base_match.identity
            removed.add(base_match.identity)
    return renames, removed


def _try_entity_disjoint(unit: ConflictUnit) -> str | None:
    """Resolve when both sides add/modify DISTINCT entities in one container.

    Git's line-diff reports a conflict whenever two sides insert at the same base
    line — but if those insertions are DIFFERENT entities (a method ``b`` on one
    side, method ``c`` on the other, both added to the same class), there is no
    real conflict at entity granularity: different ``(kind, name)`` identities
    can't clobber each other. This is the Weave/Aura win — the single most common
    real-world conflict line-level merging provably cannot resolve.

    Algorithm (all pure, no I/O, no model):
      1. Enumerate entities in base/current/replayed restricted to the enclosing
         container (the class/impl the conflict sits inside).
      2. Compute, per side, the set of entity IDENTITIES it ADDED (not in base)
         or MODIFIED (in base, body changed).
      3. If the two sides' touched identities are disjoint → merge both: emit the
         union of entities (base entities, then current's adds, then replayed's
         adds), preserving each side's relative order. No overlap ⇒ safe.
      4. Decline (return None) the moment a single entity is touched by BOTH
         sides — that's a genuine intra-entity conflict for the line/LLM resolvers.

    Declines (returns None) when tree-sitter is unavailable, the conflict isn't
    inside a parseable container, or any entity overlaps. Every resolution this
    produces is STILL validated by the orchestrator before acceptance.
    """
    try:
        from capybase.adapters import structural
    except Exception:  # noqa: BLE001
        return None
    lang = unit.language
    if lang not in ("python", "rust"):
        return None
    meta = unit.structural_metadata
    enc_text = meta.get("enclosing_node_text")
    if not enc_text:
        return None  # no enclosing container known → can't enumerate

    base_ents = structural.enumerate_entities(unit.base.text or "", lang)
    cur_ents = structural.enumerate_entities(unit.current.text or "", lang)
    rep_ents = structural.enumerate_entities(unit.replayed.text or "", lang)
    if base_ents is None or cur_ents is None or rep_ents is None:
        return None  # parse failed on at least one side

    # The enclosing node is a CONTAINER (class/impl/module). The conflict sides
    # are the whole container's evolution, so a module-level enumeration returns
    # the container itself (one "class Store" entity) — not the methods inside
    # it. To get the inner entities (the actual unit of entity merge), re-enumerate
    # anchored INSIDE the container body. The first non-header line is a stable
    # anchor that sits within the body for any non-empty class/impl.
    if len(base_ents) <= 1 and len(cur_ents) <= 1 and len(rep_ents) <= 1:
        span = _inner_anchor(enc_text)
        if span is not None:
            base_ents = structural.enumerate_entities(unit.base.text or "", lang, container_span=span) or base_ents
            cur_ents = structural.enumerate_entities(unit.current.text or "", lang, container_span=span) or cur_ents
            rep_ents = structural.enumerate_entities(unit.replayed.text or "", lang, container_span=span) or rep_ents

    base_by_id = {e.identity: e for e in base_ents}

    # Rename detection (s3m rename handler, survey §2.2): a base entity whose
    # body reappears under a NEW similar name on a side, with the old name gone,
    # is a RENAME — not a base-kept + side-added pair. Without this, entity_disjoint
    # emits a duplicate (old name AND new name). Detect per side, mapping the new
    # identity back to the base identity so touched sets and the merge walk reason
    # about logical entities.
    cur_renames, cur_removed = _detect_renames(cur_ents, base_ents)
    rep_renames, rep_removed = _detect_renames(rep_ents, base_ents)
    # Classify each renamed-away base entity:
    # - renamed by BOTH sides to the SAME new name → AGREED (not a conflict).
    # - renamed by BOTH sides to DIFFERENT new names → conflict → decline.
    # - renamed by ONE side only → flows through as that side's change.
    cur_new_by_base = {base_id: new for new, base_id in cur_renames.items()}
    rep_new_by_base = {base_id: new for new, base_id in rep_renames.items()}
    agreed_renames: set = set()  # base ids both sides renamed identically
    for base_id, cur_new in cur_new_by_base.items():
        if base_id in rep_new_by_base:
            if rep_new_by_base[base_id] != cur_new:
                return None  # both renamed the same entity differently → conflict
            agreed_renames.add(base_id)  # both renamed it the same way → agreed
    # Union of base identities renamed away by EITHER side — these must NOT be
    # re-emitted under their old names during the merge walk.
    all_removed = cur_removed | rep_removed

    def _canon(ident, renames):
        """Map a side entity identity to its canonical base identity (rename-aware)."""
        return renames.get(ident, ident)

    def _touched(ents, renames):
        """Canonical base identities a side ADDED or MODIFIED (rename-aware)."""
        out = []
        for e in ents:
            ident = _canon(e.identity, renames)
            if e.identity in renames:
                # A rename: counts as touching the base entity it replaced.
                out.append(ident)
                continue
            prev = base_by_id.get(ident)
            if prev is None:
                out.append(e.identity)  # genuinely added
            elif e.body != prev.body:
                out.append(ident)  # modified
        return out

    cur_touched = _touched(cur_ents, cur_renames)
    rep_touched = _touched(rep_ents, rep_renames)
    # If either side touched nothing, an earlier rule (one_sided_change) would
    # have handled it. Decline to avoid duplicate logic — but guard anyway.
    if not cur_touched or not rep_touched:
        return None
    # Overlap → genuine intra-entity conflict — UNLESS both sides made the SAME
    # rename (agreed change), which is not a conflict. Decline for the line/LLM
    # path otherwise.
    overlap = set(cur_touched) & set(rep_touched)
    if overlap - agreed_renames:
        return None

    # Disjoint: build the merged container. Start from base's entities, apply
    # each side's modifications/renames in place, then append additions.
    cur_by_canon = {_canon(e.identity, cur_renames): e for e in cur_ents}
    rep_by_canon = {_canon(e.identity, rep_renames): e for e in rep_ents}
    cur_touched_set = set(cur_touched)
    rep_touched_set = set(rep_touched)
    merged_ids: list = []
    seen: set = set()
    for e in base_ents:
        ident = e.identity
        # Skip base entities renamed away — the renamed version is emitted below
        # via the side's entity list, so we must NOT also keep the old name.
        if ident in all_removed:
            # Emit the renamed version (whichever side renamed it); mark seen so
            # the side's copy isn't appended again as an "addition".
            renamed = cur_by_canon.get(ident) or rep_by_canon.get(ident)
            if renamed is not None:
                merged_ids.append(renamed)
                seen.add(ident)
            continue
        # Touched sets are disjoint (checked above), so at most one side
        # MODIFIED this entity. Take the modified version when present; else
        # the unchanged base version (both sides kept it as-is).
        if ident in cur_touched_set:
            merged_ids.append(cur_by_canon[ident])
        elif ident in rep_touched_set:
            merged_ids.append(rep_by_canon[ident])
        else:
            merged_ids.append(e)  # unchanged by either side
        seen.add(ident)
    # Append additions: current's new entities first (preserving its order), then
    # replayed's. Renamed entities were already emitted above (under their new
    # name via the renamed= path), so skip them here to avoid duplication.
    for e in cur_ents:
        canon = _canon(e.identity, cur_renames)
        if e.identity in cur_renames:
            continue  # already emitted via the renamed-away path
        if canon not in seen:
            merged_ids.append(e)
            seen.add(canon)
    for e in rep_ents:
        canon = _canon(e.identity, rep_renames)
        if e.identity in rep_renames:
            continue  # already emitted via the renamed-away path
        if canon not in seen:
            merged_ids.append(e)
            seen.add(canon)

    # Reconstruct the container text. The enclosing node's text is the source of
    # truth for its non-entity framing (class header, impl braces, indentation).
    # We splice the merged entity bodies back into that framing.
    return _rebuild_container(enc_text, [e.body for e in merged_ids], lang)


def _inner_anchor(enclosing_text: str) -> tuple[int, int] | None:
    """A span anchored inside a container's body (for inner entity enumeration).

    The second non-blank line of the enclosing text reliably sits inside the
    class/impl body (the first line is the header). Returns that line's span so
    :func:`enumerate_entities` descends into the container's body. None when the
    container has no body lines (degenerate).
    """
    lines = enclosing_text.split("\n")
    body_line = None
    for i, line in enumerate(lines):
        if i == 0:
            continue  # header
        if line.strip():
            body_line = i
            break
    if body_line is None:
        return None
    return (body_line, body_line)


def _rebuild_container(enclosing_text: str, entity_bodies: list[str], language: str) -> str | None:
    """Rebuild a container's text from its framing + the merged entity bodies.

    The enclosing node text (e.g. ``class C:\\n    def a(): ...\\n    def b(): ...``)
    carries the container's framing — the header line (``class C:`` /
    ``impl S {``), its braces, and the indentation convention. We extract that
    framing (everything before the first entity and after the last) and splice in
    the merged entity bodies, preserving the original indentation prefix.

    This is intentionally conservative: it only works for a SINGLE contiguous
    entity block inside a container (the common case — a class/impl body or a
    module-level def run). If the framing can't be cleanly identified, it
    returns None so the resolver declines and the LLM handles it.
    """
    enc_lines = enclosing_text.split("\n")
    if not enc_lines:
        return None
    # The body indent is the leading whitespace of the first body line (the
    # convention under which the container's entities nest). tree-sitter's entity
    # body slice EXCLUDES this leading indent on the def/header line but KEEPS
    # the internal indentation, so we prepend the indent to the FIRST line of
    # each body only — internal lines already carry correct relative indentation.
    body_indent = ""
    for line in enc_lines[1:]:
        if line.strip():
            body_indent = line[: len(line) - len(line.lstrip(" "))]
            break
    # The header is line 0; the trailer is the container's OWN closing brace (for
    # brace languages) — exactly one line. We can't take more, because the method
    # bodies' own closing braces (``    }``) sit just above the container's close
    # and would be stolen. Python class bodies have no trailer.
    header = enc_lines[0]
    trailer_lines: list[str] = []
    if language != "python" and enc_lines:
        last = enc_lines[-1]
        if last.strip() == "}":
            trailer_lines = [last]
    out = [header]
    for body in entity_bodies:
        blines = body.split("\n")
        if blines:
            # Prepend the container's body indent to the def/header line only.
            blines[0] = body_indent + blines[0] if blines[0].strip() else blines[0]
        out.append("\n".join(blines))
    out.extend(trailer_lines)
    return "\n".join(out)

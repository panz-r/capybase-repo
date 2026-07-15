"""Deterministic structural conflict resolution.

A safe, LLM-free pre-resolver that runs BEFORE the model. It attempts to produce
a correct merged text from base + current + replayed using four provably-safe
rules — no heuristics that could introduce a wrong merge:

1. **identical_sides** — current and replayed normalized-equal → emit that side.
   (Survey: "both sides identical → delete the conflict".)
2. **one_sided_change** — only one side diverged from base → take the changed
   side; the other conceded. Resolves a large fraction of real conflicts.
3. **disjoint_edits** — both sides changed, but on NON-overlapping line ranges
   within the hunk → merge both edits.
   No overlap means no semantic conflict at this granularity.
4. **zealous_merge** — per-base-line 3-way merge ( zealous
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
unit-testable. Line-diffing uses histogram diff (:mod:`capybase.diff`) — no new
dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from capybase.conflict_model import ConflictUnit
from capybase.diff import line_matcher
from capybase.merge_intent import classify_side, direction

Rule = Literal[
    "identical_sides", "one_sided_change", "disjoint_edits", "zealous_merge",
    "entity_disjoint", "token_disjoint", "delete_side",
    # Refactoring-aware composition (RefMerge): when entity_disjoint
    # DECLINED on overlap, but the overlap is entirely a clean rename-vs-body-
    # modify partition, compose the renamer's header with the modifier's body.
    "refactoring_aware_merge",
    # Easy-merge union rules (the gap every prior rule declines): both sides
    # append distinct items to a collection, or insert distinct lines at the
    # same anchor. An opinionated, deterministic ordering (current-appends
    # before replayed-appends) resolves them; a wrong guess still fails the
    # validation pipeline and falls through to the LLM, so the policy is safe.
    "list_union", "dict_union", "insertion_union",
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
    # is empty or near-empty). This is the disambiguation prior work's "silent
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

        # Rule 5: zealous per-base-line 3-way merge. Stronger than
        # disjoint_edits — also resolves overlaps that are agreed (both made the
        # same change) or one-sided (one side conceded a sub-region the other
        # touched). Returns None on any genuine two-sided disagreement or
        # ambiguous pure insertion, so the LLM handles it.
        merged = _try_zealous_merge(base, current, replayed)
        if merged is not None:
            return StructuralResolution(rule="zealous_merge", text=merged)

        # Rule 6: entity-level disjoint resolution (Weave/Aura).
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

        # Rule 6b: refactoring-aware composition (RefMerge). Fires
        # when entity_disjoint DECLINED on overlap, but the overlap is entirely a
        # clean rename-vs-body-modify partition: one side renamed an entity (pure
        # header change, body content identical to base), the other modified its
        # body (header line identical to base, body content changed). Composing
        # the renamer's header with the modifier's body preserves BOTH intents.
        # Declines the moment any overlapping pair isn't a clean partition (both
        # modified the body, both renamed differently, a signature change, …).
        merged = _try_refactoring_aware_merge(unit)
        if merged is not None:
            return StructuralResolution(rule="refactoring_aware_merge", text=merged)

        # Rule 7: token-level disjoint resolution (Summer, layer 3).
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

        # Rules 8-10: easy-merge unions. Every rule above DELIBERATELY declines
        # pure insertions/appends (their relative order is ambiguous). These
        # rules resolve the common "both sides appended distinct items" shapes
        # with an opinionated, deterministic ordering (current-appends first,
        # then replayed-appends). The merge is still validated before it's
        # applied, so an ordering that produces invalid code falls through to
        # the LLM — the policy can be opinionated without being unsafe.
        merged = _try_list_union(base, current, replayed)
        if merged is not None:
            return StructuralResolution(rule="list_union", text=merged)
        merged = _try_dict_union(base, current, replayed)
        if merged is not None:
            return StructuralResolution(rule="dict_union", text=merged)
        merged = _try_insertion_union(base, current, replayed)
        if merged is not None:
            return StructuralResolution(rule="insertion_union", text=merged)

    return StructuralResolution(rule=None, text=None)


def deterministically_mergeable(unit: ConflictUnit) -> bool:
    """Whether the structural resolver can merge ``unit`` with zero LLM calls.

    A pure feasibility probe: runs :func:`resolve_structurally` and reports
    whether it produced a resolution, WITHOUT committing to it. Used by
    :mod:`classifier` to mark union-combine / one-sided / identical conflicts
    ``trivial`` (they need no model judgment) and available to any caller that
    wants to ask "can this skip the LLM?" cheaply.
    """
    return resolve_structurally(unit).resolved


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

    This is prior work's "silent loss of intent" guard: without it, a
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
    matcher = line_matcher(base, other)
    for tag, i1, i2, _j1, _j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        # i-indices are into base; mark the affected base range.
        changed.update(range(i1, i2))
    return changed


# ---------------------------------------------------------------------------
# Token-level disjoint resolution (Summer, layer 3)
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
    matcher = line_matcher(base_toks, other_toks)
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
    # spans. An edit at base index i replaces tokens [i, end) with `repl`. A
    # PURE INSERTION (i1 == i2) is anchored BEFORE base token i: emit the
    # insertion, then ALSO emit base[i] and advance (i += 1) — otherwise the
    # walk sets i=end=i and loops forever. (Two disjoint pure insertions at
    # different anchors are unambiguous: each lands before its own anchor.)
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
            if end > i:
                # A replace/delete: jump past the consumed base span.
                i = end
            else:
                # A pure insertion: keep base[i] and advance (the insertion
                # lands before it). Guards against the i=end=i infinite loop.
                out.append(bt[i])
                i += 1
        else:
            out.append(bt[i])
            i += 1
    # Trailing pure insertions anchored AT n (after the last base token) are
    # recorded at index n, which the loop above (i < n) never reaches. Emit them.
    if n in merged_ops:
        _, repl = merged_ops[n]
        out.extend(repl)
    return _detokenize(out)


# ---------------------------------------------------------------------------
# Easy-merge union rules (the insertion-union gap every prior rule declines)
# ---------------------------------------------------------------------------
#
# These resolve the common "both sides appended distinct items to a collection"
# shapes with a deterministic ordering (current-appends, then replayed-appends).
# A wrong guess still fails the validation pipeline and falls through, so the
# opinionated ordering is safe. Each rule is a pure ``str | None`` function.


def _try_list_union(base: str, current: str, replayed: str) -> str | None:
    """Merge two sides that each APPEND distinct items to a ``[...]`` list.

    Fires when each side is ``base`` with extra items appended inside the SAME
    list literal, the appended item-sets are disjoint, and neither side removed
    or reordered base items. The merge is base-items + current-appends +
    replayed-appends (current first — a deterministic, documented choice).

    Declines (→ None) when: there's no single list literal; a side changed the
    list's non-item structure (e.g. the assignment target, or removed an item);
    the two sides appended the SAME item; or either side touched base items.
    Handles a list that spans multiple lines (indentation preserved) or one line.
    """
    import re

    b = _find_single_list(base)
    if b is None:
        return None
    _, base_inner, base_open_off, base_close_off = b
    base_items = _split_list_items(base_inner)
    cur = _find_single_list(current)
    rep = _find_single_list(replayed)
    if cur is None or rep is None:
        return None
    # Each side must preserve base items verbatim (same order, no removal) and
    # differ only by appending. Compute the appended tail.
    cur_items = _split_list_items(cur[1])
    rep_items = _split_list_items(rep[1])
    cur_appended = _appended_tail(base_items, cur_items)
    rep_appended = _appended_tail(base_items, rep_items)
    if cur_appended is None or rep_appended is None:
        return None  # a side reordered/removed/edited base items
    # Disjoint appends (no shared new item). A shared item means both sides made
    # the same addition — let identical_sides/zealous handle it, not us.
    if set(cur_appended) & set(rep_appended):
        return None
    merged_items = base_items + cur_appended + rep_appended
    # Reconstruct BLOCK-SCOPED output (not whole-file): ``base`` is the unit's
    # full base stage blob, so slicing ``base[:open]`` / ``base[close:]`` would
    # drag the ENTIRE rest of the file into the resolution and, when spliced
    # into the marker span, duplicate every definition after the list. The
    # block-scoped sides (``current``/``replayed``) carry the exact same
    # prefix/suffix around the list — use one of them as the template instead.
    # Both sides share the base prefix/suffix by construction (the rule only
    # fires when each preserves base items in place), so ``current`` is a safe
    # template; mirror its surrounding text and swap in the merged list.
    return (
        current[: cur[2]]
        + "["
        + ", ".join(merged_items)
        + "]"
        + current[cur[3]:]
    )


def _try_dict_union(base: str, current: str, replayed: str) -> str | None:
    """Merge two sides that each ADD distinct keys to a ``{...}`` dict.

    Fires when each side is ``base`` with extra key entries inside the SAME dict
    literal, the added key-sets are disjoint, and neither side changed a value
    of a shared base key. The merge is base-keys + current-keys + replayed-keys.

    Declines when: there's no single dict literal; the dict spans multiple lines
    (reconstructing multi-line indentation is fiddly and error-prone — leave
    those to the LLM); a side removed/reordered base keys; both sides added the
    SAME key; or a side changed the value of a key the other side also touched.
    Handles inline (single-line) dicts.
    """
    b = _find_single_dict(base)
    if b is None:
        return None
    base_inner = b[1]
    # Only inline (single-line) dicts: multi-line reconstruction would mangle
    # indentation. The base dict literal must not contain a newline.
    if "\n" in base_inner:
        return None
    base_entries = _split_dict_entries(base_inner)
    cur = _find_single_dict(current)
    rep = _find_single_dict(replayed)
    if cur is None or rep is None:
        return None
    cur_entries = _split_dict_entries(cur[1])
    rep_entries = _split_dict_entries(rep[1])
    # Each side must preserve base entries (same keys, same values, same order)
    # and differ only by appending new entries.
    cur_added = _appended_tail(base_entries, cur_entries)
    rep_added = _appended_tail(base_entries, rep_entries)
    if cur_added is None or rep_added is None:
        return None
    base_keys = {e.split(":", 1)[0].strip() for e in base_entries if ":" in e}
    cur_added_keys = {e.split(":", 1)[0].strip() for e in cur_added if ":" in e}
    rep_added_keys = {e.split(":", 1)[0].strip() for e in rep_added if ":" in e}
    # No key added by both, and no added key collides with a base key.
    if cur_added_keys & rep_added_keys:
        return None
    if cur_added_keys & base_keys or rep_added_keys & base_keys:
        return None
    merged = base_entries + cur_added + rep_added
    # Reconstruct BLOCK-SCOPED output (see _try_list_union for the rationale):
    # ``base`` is the unit's whole base blob, so rebuilding from it would drag
    # the rest of the file into the resolution and duplicate surrounding defs.
    # The block-scoped ``current`` carries the same dict-surrounding text, so
    # use it as the rebuild template instead.
    return _rebuild_dict(current, cur, merged)


def _try_insertion_union(base: str, current: str, replayed: str) -> str | None:
    """Merge two sides that each INSERT distinct whole lines after base anchors.

    The line-granular analog of the list/dict union: both sides added whole new
    lines (no base line modified), and the added line-sets are disjoint. The
    merge interleaves both sides' insertion RUNS at their base anchors (current's
    run before replayed's run at a shared anchor). Unlike the pure-insertion
    DECLINE in disjoint/zealous/token (which treat ordering at a single shared
    anchor as ambiguous), this rule accepts distinct-line insertions.

    Declines when either side MODIFIED or DELETED a base line (only pure
    insertions qualify), or the inserted line-sets overlap.
    """
    base_lines = base.split("\n")
    cur_lines = current.split("\n")
    rep_lines = replayed.split("\n")
    cur_ins = _pure_insertion_runs(base_lines, cur_lines)
    rep_ins = _pure_insertion_runs(base_lines, rep_lines)
    if cur_ins is None or rep_ins is None:
        return None  # a side modified/deleted a base line
    # Disjoint inserted lines (a line both sides added → ambiguous, decline).
    # Blank lines are ignored in the overlap check: a blank separator inserted
    # by both sides is not meaningful shared content (it carries no semantic
    # weight and re-appears naturally between two inserted blocks).
    cur_flat = [ln for run in cur_ins.values() for ln in run if ln.strip()]
    rep_flat = [ln for run in rep_ins.values() for ln in run if ln.strip()]
    if set(cur_flat) & set(rep_flat):
        return None
    # Merge: walk base, emitting each base line preceded by any insertion runs
    # anchored before it (current's run first, then replayed's). Trailing runs
    # (anchored after the last base line) append at the end.
    out: list[str] = []
    for i, bl in enumerate(base_lines):
        out.extend(cur_ins.get(i, []))
        out.extend(rep_ins.get(i, []))
        out.append(bl)
    out.extend(cur_ins.get(len(base_lines), []))
    out.extend(rep_ins.get(len(base_lines), []))
    return "\n".join(out)


# Helpers for the union rules (pure, regex-based — no AST needed).


def _find_single_list(text: str):
    """The ``(before_unused, inner, open_offset, close_offset)`` of the SOLE
    ``[...]`` list in text, or None.

    ``inner`` is the text between the brackets; ``open_offset``/``close_offset``
    are the char offsets of the ``[`` and ``]`` (so the caller can splice).
    Rejects nested/extra brackets (a single list with no inner ``[``).
    """
    import re

    m = re.search(r"\[(.*)\]", text, re.DOTALL)
    if m is None:
        return None
    inner = m.group(1)
    if "[" in inner or "]" in inner:
        return None
    open_off = m.start()  # offset of '['
    close_off = m.end()   # offset just after ']'
    return (None, inner, open_off, close_off)


def _split_list_items(inner: str) -> list[str]:
    """Split a list literal's interior into stripped items (no surrounding [])."""
    if not inner.strip():
        return []
    return [it.strip() for it in inner.split(",") if it.strip()]


def _find_single_dict(text: str):
    """The (before, inner, after-unused) of the SOLE ``{...}`` dict in text.

    Returns None if there's not exactly one brace-delimited dict. ``inner`` is
    the text between the braces.
    """
    import re

    m = re.search(r"\{(.*)\}", text, re.DOTALL)
    if m is None:
        return None
    inner = m.group(1)
    if "{" in inner or "}" in inner:
        return None
    return (None, inner, None, None)


def _split_dict_entries(inner: str) -> list[str]:
    """Split a dict interior into entries (``key: value``), preserving text.

    Splits on top-level commas (the regex form ``key: value`` is assumed; nested
    commas inside values — e.g. a function call — are NOT handled, which keeps
    the rule conservative: a dict with complex values declines rather than
    mis-splitting).
    """
    if not inner.strip():
        return []
    # Conservative: split on commas only when every segment looks like `key: val`.
    parts = [p.strip() for p in inner.split(",") if p.strip()]
    if not all(":" in p for p in parts):
        return []  # ambiguous (nested commas) → decline via empty
    return parts


def _appended_tail(base_items: list, side_items: list):
    """The items ``side`` appended after ``base``, or None if it didn't.

    Returns the suffix of ``side_items`` following a verbatim copy of
    ``base_items`` as a prefix (base preserved in order, unchanged). None means
    the side reordered, removed, or edited base items — not a pure append.
    """
    n = len(base_items)
    if len(side_items) < n:
        return None
    if side_items[:n] != base_items:
        return None
    tail = side_items[n:]
    return tail if tail else None  # no append → not our shape (let other rules)


def _rebuild_dict(base: str, found, entries: list[str]) -> str:
    """Rebuild ``base``'s dict literal with the given ``entries`` (comma-joined)."""
    import re

    m = re.search(r"\{.*\}", base, re.DOTALL)
    if m is None:
        return base  # defensive; _find_single_dict already validated this
    inner = ", ".join(entries)
    return base[: m.start()] + "{" + inner + "}" + base[m.end():]


def _pure_insertion_runs(
    base_lines: list[str], side_lines: list[str]
) -> dict[int, list[str]] | None:
    """Map each base-line index to the RUN of lines ``side`` inserted before it.

    Returns None if ``side`` is not a pure insertion (it modified or deleted a
    base line). Uses histogram diff to align ``side_lines`` against ``base_lines``:
    every base line must appear unchanged and in order; the only allowed
    difference is ``insert`` opcodes, each recorded as a run keyed by the base
    index it precedes. A run anchored at ``len(base_lines)`` is a trailing
    insertion (after the last base line). This run-based model (vs. per-line
    keys) correctly handles multi-line insertion blocks.
    """
    sm = line_matcher(base_lines, side_lines)
    runs: dict[int, list[str]] = {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag != "insert":
            # replace/delete → the side touched a base line, not a pure insert.
            return None
        # An insert at base range [i1, i2) (i1 == i2 for a pure insert) precedes
        # base line i1; the inserted side-lines [j1, j2) are the run.
        runs.setdefault(i1, []).extend(side_lines[j1:j2])
    return runs


def _try_zealous_merge(base: str, current: str, replayed: str) -> str | None:
    """Per-base-line 3-way merge.

    Aligns each side against base line-by-line via histogram diff. For every base
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
    matcher = line_matcher(base, other)
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
    matcher = line_matcher(base, other)
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
# Entity-level disjoint resolution (Weave/Aura)
# ---------------------------------------------------------------------------

# Minimum name-similarity ratio for two entity names to be considered a rename
# (0.6 is conservative: it catches loadData→fetchData, load→fetch,
# parse_thing→parse_item, but won't conflate unrelated short names. A rename
# ALSO requires the body to match (normalized), so a coincidentally-similar
# name with different content isn't misread as a rename. The threshold and
# name-similarity now live canonically in abstract_parser
# (RENAME_NAME_SIMILARITY_THRESHOLD / name_similarity).

#: Function-declaration keywords that, when leading a header line, identify the
#: enclosing node as a bare FUNCTION (not a class/impl container). Used by
#: ``_rebuild_container`` to decide whether to emit merged entities flat (bare-
#: function conflict — the entities ARE the output) or splice them inside the
#: container's header+trailer. Covers both supported families (Python/Rust) plus
#: the other Family-A languages the parser recognizes, with the leading
#: visibility/async modifiers that may precede the keyword.
_FN_DECL_KEYWORDS = (
    "def", "fn", "func", "fun", "function",
)
_VISIBILITY_PREFIXES = (
    "pub", "export", "public", "private", "protected",
    "internal", "extern", "unsafe", "async",
)


def _is_bare_function_header(header_line: str) -> bool:
    """True when ``header_line`` declares a bare function (not a container).

    Handles visibility/async modifiers preceding the function keyword:
    ``pub fn``, ``export function``, ``async def``, ``unsafe extern fn``, etc.
    A class/struct/impl/enum header (``class C`` / ``struct S`` / ``impl T``)
    returns False — those are containers warranting the header+trailer splice.
    """
    toks = header_line.lstrip().split()
    if not toks:
        return False
    # Strip leading visibility/async modifiers, then check the first real token.
    i = 0
    while i < len(toks) and toks[i] in _VISIBILITY_PREFIXES:
        i += 1
    return i < len(toks) and toks[i] in _FN_DECL_KEYWORDS


def _has_name_collision(merged_ids: list) -> bool:
    """True when two merged entities share the same resulting ``(kind, name)``.

    The merge-walk's ``seen`` set is keyed by canonical BASE identity, so a
    rename (cur: foo->bar, recorded under canonical foo) and an independent
    addition (rep: fresh bar, canonical bar) both emit a ``bar`` — a malformed
    container with a doubled method. Callers use this to DECLINE (return None)
    so the conflict escalates to the line/LLM path rather than producing a
    silently-wrong doubled entity.

    Overloads are already declined upstream by ``has_duplicate_identities``
    (same identity in one version), so any collision here is always a
    malformation, never a legitimate merge.
    """
    emitted: set = set()
    for e in merged_ids:
        key = (e.kind, e.name)
        if key in emitted:
            return True
        emitted.add(key)
    return False


def _body_content(body: str, lang: str | None = None) -> str:
    """The body with its signature/header line removed, normalized.

    Thin delegate to the canonical :func:`abstract_parser.entity_body_content`
    (consolidation #2). Both strip the header line and normalize the rest via
    the parser's comment/string-aware :func:`normalize_body`, so the
    resolver's rename signal AGREES with the parser's ``unit_body_fingerprint``
    by construction — no longer by manually-maintained coincidence.

    ``lang`` selects the comment marker (``//`` for Family-A, ``#`` for
    Python/Ruby) so the fingerprint and this signal stay consistent per language.
    """
    from capybase.adapters.abstract_parser import entity_body_content
    return entity_body_content(body, lang=lang)


def _ws_collapse(body: str, lang: str | None = None) -> str:
    """Whitespace-collapse a body, stripping comment-only lines (string-preserving).

    Used for the agreed-rename body-divergence check: a string-VALUE edit
    (``return "v2"`` vs ``return "v3"``) must register as a divergence so a
    same-name-two-sided rename with different values is flagged a conflict,
    not silently resolved by dropping one side. But a COMMENT-only difference
    must NOT register — the rest of the system (3-way diff, detect_renames_2way,
    match_entities) treats a comment-only diff as a non-divergence (an agreed
    rename), and the resolver must agree. The comment/string-stripping
    :func:`_body_content` would blank string values away (too aggressive);
    a naive whitespace collapse would flag comment diffs (too sensitive).

    Delegates to the 3-way diff's :func:`_normalize_body_ws_only` (the single
    lang-aware, string-preserving, comment-stripping normalizer) so all paths
    agree on what counts as a body divergence.
    """
    from capybase.adapters.structural_diff import _normalize_body_ws_only
    return _normalize_body_ws_only(body, lang=lang)


def _detect_renames(
    side_ents: list, base_ents: list, lang: str | None = None,
) -> tuple[dict, dict]:
    """Detect renames of base entities on one side (rename handler, §2.2).

    Thin delegate to the canonical :func:`abstract_parser.detect_renames_2way`
    (consolidation #2). The 2-way rename algorithm — index base by body-content,
    find side entities whose old name is gone but whose body matches, apply the
    name-similarity/substantial-body guard — now lives in ONE place
    (``abstract_parser``), shared by this resolver, the 3-way diff, and
    ``semantic_diff``. Returns ``(renames, base_ids_removed)`` unchanged.

    ``lang`` is forwarded so the body-content match strips the RIGHT comment
    marker per language — otherwise a Rust rename that also edits a ``//``
    comment won't pair, disagreeing with the lang-aware parse-time fingerprint.
    """
    from capybase.adapters.abstract_parser import detect_renames_2way
    return detect_renames_2way(base_ents, side_ents, lang=lang)


@dataclass
class _EntityMergeCtx:
    """Shared context for the entity-level merge strategies.

    Both :func:`_try_entity_disjoint` and :func:`_try_refactoring_aware_merge`
    need the same setup: enumerate entities in the enclosing container on all
    three sides, detect renames, and build the base identity index. This
    dataclass carries that context so the two strategies share ONE preamble
    (extracted below) instead of ~40 lines of near-verbatim scaffolding each.
    """

    enc_text: str
    lang: str
    base_ents: list
    cur_ents: list
    rep_ents: list
    base_by_id: dict
    cur_renames: dict
    cur_removed: set
    rep_renames: dict
    rep_removed: set


def _prepare_entity_merge(unit: ConflictUnit) -> _EntityMergeCtx | None:
    """The shared preamble for entity-level merge strategies.

    Both :func:`_try_entity_disjoint` and :func:`_try_refactoring_aware_merge`
    do the same setup — the only code that previously differed between their
    ~40-line preambles was whitespace and comment wording. This consolidates it:

      1. Guard: structural parser available, language is python/rust, an
         enclosing container is known.
      2. Enumerate entities (base/current/replayed), descending into the
         container body when the top-level enumeration returned only the
         container itself.
      3. Decline on duplicate identities.
      4. Build the base identity index and detect per-side renames.

    Returns the shared context, or ``None`` when any precondition fails (the
    caller treats ``None`` as "decline — escalate to the next strategy").
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
    # anchored INSIDE the container body.
    if len(base_ents) <= 1 and len(cur_ents) <= 1 and len(rep_ents) <= 1:
        span = _inner_anchor(enc_text)
        if span is not None:
            base_ents = structural.enumerate_entities(unit.base.text or "", lang, container_span=span) or base_ents
            cur_ents = structural.enumerate_entities(unit.current.text or "", lang, container_span=span) or cur_ents
            rep_ents = structural.enumerate_entities(unit.replayed.text or "", lang, container_span=span) or rep_ents

    # Decline on duplicate identities: two entities sharing an identity (e.g.
    # Java/C++/Python method overloads, re-definitions) collide silently in the
    # identity-keyed dicts below, dropping all but one — a missed-conflict
    # data-loss bug. Decline so the conflict escalates to the line/LLM resolvers.
    try:
        from capybase.adapters.abstract_parser import has_duplicate_identities
    except Exception:  # noqa: BLE001
        return None
    if (
        has_duplicate_identities(base_ents)
        or has_duplicate_identities(cur_ents)
        or has_duplicate_identities(rep_ents)
    ):
        return None

    base_by_id = {e.identity: e for e in base_ents}
    # Rename detection per side (s3m rename handler, a base entity
    # whose body reappears under a NEW similar name on a side, with the old name
    # gone, is a RENAME — not a base-kept + side-added pair.
    cur_renames, cur_removed = _detect_renames(cur_ents, base_ents, lang)
    rep_renames, rep_removed = _detect_renames(rep_ents, base_ents, lang)

    return _EntityMergeCtx(
        enc_text=enc_text,
        lang=lang,
        base_ents=base_ents,
        cur_ents=cur_ents,
        rep_ents=rep_ents,
        base_by_id=base_by_id,
        cur_renames=cur_renames,
        cur_removed=cur_removed,
        rep_renames=rep_renames,
        rep_removed=rep_removed,
    )


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

    Declines (returns None) when the structural parser is unavailable, the conflict isn't
    inside a parseable container, or any entity overlaps. Every resolution this
    produces is STILL validated by the orchestrator before acceptance.
    """
    ctx = _prepare_entity_merge(unit)
    if ctx is None:
        return None
    base_ents, cur_ents, rep_ents = ctx.base_ents, ctx.cur_ents, ctx.rep_ents
    base_by_id = ctx.base_by_id
    enc_text, lang = ctx.enc_text, ctx.lang
    cur_renames, cur_removed = ctx.cur_renames, ctx.cur_removed
    rep_renames, rep_removed = ctx.rep_renames, ctx.rep_removed

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
            # Both renamed to the same NEW NAME — but if their BODIES diverge
            # (e.g. a different string value), it's still a conflict. The name
            # check alone would let both sides' renames pass and the merge-walk
            # would emit only current's body, silently dropping replayed's
            # divergent value. Mirror the 3-way diff's cross-side body guard.
            # Use a string-PRESERVING whitespace collapse so a string-value
            # edit (return "v2" vs "v3") registers as a divergence — the
            # comment/string-stripping _body_content would blank it away.
            cur_e = next((e for e in cur_ents if e.identity == cur_new), None)
            rep_e = next((e for e in rep_ents if e.identity == cur_new), None)
            if (
                cur_e is not None
                and rep_e is not None
                and _ws_collapse(cur_e.body, lang) != _ws_collapse(rep_e.body, lang)
            ):
                return None  # same rename name, divergent bodies → conflict
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

    # Name-collision guard (see _has_name_collision): decline if two merged
    # entities would share the same resulting (kind, name).
    if _has_name_collision(merged_ids):
        return None

    # Reconstruct the container text. The enclosing node's text is the source of
    # truth for its non-entity framing (class header, impl braces, indentation).
    # We splice the merged entity bodies back into that framing.
    return _rebuild_container(enc_text, [e.body for e in merged_ids], lang)


def _entity_header_line(body: str) -> str:
    """The first (header/signature) line of an entity body, stripped.

    A rename changes this line (``def foo`` → ``def bar``); a body-only modify
    leaves it identical to base. So the header line is the discriminator between
    a rename and a body modification. Stripped so incidental indentation doesn't
    mask a match.
    """
    for line in (body or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _compose_entity(renamer, modifier) -> "Entity":
    """Build a composed entity: ``renamer``'s header line + ``modifier``'s body.

    The result has the NEW name (from the renamer's header) and the MODIFIED body
    content (from the modifier). The modifier's header line is replaced by the
    renamer's; the rest of the modifier's body (the changed lines) is kept
    verbatim. Returns a new Entity with the composed body and the renamer's
    identity. ``span`` is carried from the modifier (the body's position).
    """
    from capybase.adapters.structural import Entity

    ren_header = _entity_header_line(renamer.body)
    mod_lines = (modifier.body or "").split("\n")
    # Replace the modifier's header line (the first non-blank line) with the
    # renamer's, preserving the modifier's indentation on that line.
    composed: list[str] = []
    replaced = False
    indent = ""
    for ln in mod_lines:
        if not replaced and ln.strip():
            indent = ln[: len(ln) - len(ln.lstrip(" "))]
            composed.append(indent + ren_header if ren_header else ln)
            replaced = True
        else:
            composed.append(ln)
    if not replaced:
        composed = [ren_header]
    new_body = "\n".join(composed)
    return Entity(
        kind=renamer.kind, name=renamer.name, body=new_body,
        span=modifier.span,
    )


def _try_refactoring_aware_merge(unit: ConflictUnit) -> str | None:
    """Compose a rename + body-modify that both touch the SAME entity (§3.2 RefMerge).

    ``_try_entity_disjoint`` resolves when the two sides touch DIFFERENT entities.
    It DECLINES when they touch the SAME canonical entity (overlap) — that's a
    genuine conflict for the line/LLM resolvers UNLESS the overlap decomposes into
    orthogonal refactoring intents:

      - Side A RENAMED an entity (new header, body content identical to base).
      - Side B MODIFIED that entity's body (header identical to base, body changed).

    Both touch the same canonical identity → overlap. But the changes are
    orthogonal (one moved the name, one changed the body), so they compose: take
    the renamer's header + the modifier's body. This is exactly the RefMerge
    pattern (normalize → merge → reapply), specialized to renames.

    Declines (returns None) when:
      - the parser/container is unavailable (same preconditions as entity_disjoint),
      - the overlap is NOT a clean {rename, modify} partition (both modified the
        body, both renamed differently, a signature change touched the header on
        both, or an entity was touched in an unclassifiable way),
      - composition would be ambiguous.

    The algorithm reuses ``_detect_renames`` for rename detection and the same
    entity enumeration as entity_disjoint. It runs ONLY when entity_disjoint
    already declined (it's dispatched immediately after), so its re-parse cost is
    paid only on the hard overlap tail.
    """
    ctx = _prepare_entity_merge(unit)
    if ctx is None:
        return None
    base_ents, cur_ents, rep_ents = ctx.base_ents, ctx.cur_ents, ctx.rep_ents
    base_by_id = ctx.base_by_id
    enc_text, lang = ctx.enc_text, ctx.lang
    cur_renames, rep_renames = ctx.cur_renames, ctx.rep_renames

    def _is_pure_rename(side_ents, renames, ent):
        """True if ``ent`` is a rename whose body content == the base entity's."""
        if ent.identity not in renames:
            return False
        base_id = renames[ent.identity]
        base_e = base_by_id.get(base_id)
        if base_e is None:
            return False
        return _body_content(ent.body, lang) == _body_content(base_e.body, lang)

    def _is_body_modify(ent):
        """True if ``ent`` has the same identity as a base entity, the same header
        line, but changed body content (a body-only modification — NOT a signature
        change)."""
        base_e = base_by_id.get(ent.identity)
        if base_e is None:
            return False  # not a base entity → it's an addition, not a modify
        if _entity_header_line(ent.body) != _entity_header_line(base_e.body):
            return False  # header changed → signature change, not body-only
        return _body_content(ent.body, lang) != _body_content(base_e.body, lang)

    def _touched(side_ents, renames):
        """Canonical base identities a side touched (rename-aware), as Entity objs
        keyed by canonical id — reusing entity_disjoint's notion of 'touched'."""
        out = {}
        for e in side_ents:
            canon = renames.get(e.identity, e.identity)
            if e.identity in renames:
                out[canon] = e
                continue
            prev = base_by_id.get(canon)
            if prev is None or e.body != prev.body:
                out[canon] = e
        return out

    cur_touched = _touched(cur_ents, cur_renames)
    rep_touched = _touched(rep_ents, rep_renames)
    overlap = set(cur_touched) & set(rep_touched)
    if not overlap:
        return None  # no overlap → entity_disjoint already handled (or declined for other reasons)

    # For each overlapping entity, classify the pair and build a composition.
    # If ANY overlapping entity can't be cleanly composed, decline entirely.
    compositions: dict = {}  # base_id → composed Entity
    for base_id in overlap:
        cur_e = cur_touched[base_id]
        rep_e = rep_touched[base_id]
        cur_rename = _is_pure_rename(cur_ents, cur_renames, cur_e)
        rep_rename = _is_pure_rename(rep_ents, rep_renames, rep_e)
        cur_modify = _is_body_modify(cur_e)
        rep_modify = _is_body_modify(rep_e)
        # Need exactly one rename and one body-modify.
        if cur_rename and rep_modify and not rep_rename and not cur_modify:
            compositions[base_id] = _compose_entity(cur_e, rep_e)
        elif rep_rename and cur_modify and not cur_rename and not rep_modify:
            compositions[base_id] = _compose_entity(rep_e, cur_e)
        else:
            # Both modified body, both renamed, a signature change, an addition,
            # or an unclassifiable touch → genuine conflict, decline.
            return None

    # All overlapping entities composed cleanly. Now build the merged container
    # the same way entity_disjoint does, substituting composed entities for the
    # overlapping ones and taking single-side touches otherwise. Non-overlapping
    # touched entities and additions flow through unchanged from entity_disjoint's
    # logic (we re-derive the walk here for clarity, since this path only fires on
    # the overlap tail).
    seen: set = set()
    merged_ids: list = []
    # Walk base entities in order; substitute composed/touched versions.
    for e in base_ents:
        ident = e.identity
        if ident in compositions:
            merged_ids.append(compositions[ident])
            seen.add(ident)
        elif ident in cur_touched and ident not in rep_touched:
            merged_ids.append(cur_touched[ident])
            seen.add(ident)
        elif ident in rep_touched and ident not in cur_touched:
            merged_ids.append(rep_touched[ident])
            seen.add(ident)
        else:
            merged_ids.append(e)
            seen.add(ident)
    # Append additions (entities in a side not in base and not seen).
    for e in cur_ents:
        canon = cur_renames.get(e.identity, e.identity)
        if canon not in base_by_id and canon not in seen:
            merged_ids.append(e)
            seen.add(canon)
    for e in rep_ents:
        canon = rep_renames.get(e.identity, e.identity)
        if canon not in base_by_id and canon not in seen:
            merged_ids.append(e)
            seen.add(canon)

    # Name-collision guard (see _has_name_collision): decline if two merged
    # entities would share the same resulting (kind, name). The compose step
    # can rename an entity to a name the other side independently added.
    if _has_name_collision(merged_ids):
        return None

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
    # When the enclosing node is itself a FUNCTION (``def``/``fn``/``func``/
    # ``fun``/``function`` leading the header, possibly visibility/async-
    # prefixed), the conflict is inside a bare top-level function — NOT a
    # class/impl container. The entity bodies ARE the whole output (joined at
    # module level), and the function's own header must NOT be re-emitted as a
    # wrapper (that produced ``def foo():\\n    def foo():`` — a nested/
    # recursive malformation). Only a real container (class/impl/struct)
    # warrants the header+trailer splice below.
    if _is_bare_function_header(enc_lines[0]):
        # Bare-function conflict: emit the entity bodies flat, separated by a
        # blank line (module-level convention), no wrapper.
        return "\n\n".join(entity_bodies) if entity_bodies else ""
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

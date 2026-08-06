"""Deterministic structural conflict resolution.

A safe, LLM-free pre-resolver that runs BEFORE the model. It attempts to produce
a correct merged text from base + current + replayed using provably-safe rules.
The early rules are exact (no guess); the later union/value rules use a
deterministic 'newer' or ordered-union heuristic, but a wrong guess is still
caught by the validation pipeline and falls through to the LLM — see the safety
contract below. The complete dispatch order lives in :func:`resolve_structurally`;
the rules, in firing order, are:

1. **delete_side** — one side deleted and the other is empty/base → take the
   surviving side (handles modify/delete).
2. **identical_sides** — current and replayed normalized-equal → emit that side.
3. **one_sided_change** — only one side diverged from base → take the changed
   side; the other conceded. Resolves a large fraction of real conflicts.
4. **disjoint_edits** — both sides changed, but on NON-overlapping line ranges
   within the hunk → merge both edits.
   No overlap means no semantic conflict at this granularity.
5. **zealous_merge** — per-base-line 3-way merge. Where git's coarse hunk
   groups adjacent edits into one conflict, this aligns each side against base
   line-by-line and resolves every region that is agreed (both made the same
   change) or one-sided (one side conceded a sub-region the other touched).
   Returns None the moment it hits a genuine two-sided disagreement or an
   ambiguous pure insertion. This is the rule that catches the case
   ``disjoint_edits`` must refuse: two edits that *overlap* in base-line span
   yet are still safe because one side matches base there.
6. **entity_disjoint** / **refactoring_aware_merge** — entity-level
   counterparts of (4)/(5): partition the conflict by top-level entity and
   merge disjoint entity changes; when an overlap is purely a rename-vs-body-
   modify split, compose the renamer's header with the modifier's body.
7. **token_disjoint** — line-level generalization of (4) for token-aligned
   within-line edits.
8. **text_value_resolution** — pure-prose value bumps (CHANGELOG headings,
   release notes) where there are no braces/``=``; takes the
   lexicographically-later token. Declines when one side is version-like and
   the other is prose (a heading reorganization, not a bump).
9. **dependency_version_resolution** — TOML ``name = "X.Y.Z"`` (incl. inline
   tables and fenced TOML in Markdown) version-bump conflicts the prose rule's
   brace gate excludes; takes the semver-greater version.
10. **list_union / dict_union / insertion_union** — both sides append distinct
    items/keys/lines at the same anchor; an opinionated deterministic ordering
    (current-appends before replayed-appends) resolves them.

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
    # Mechanical re-application: when one side's edits are purely small token
    # substitutions (API rename, lint) and the other side rewrote the region
    # wholesale, take the rewriter's text and re-apply the mechanical
    # substitutions onto it (where the anchors survive).
    "mechanical_reapply_merge",
    # Easy-merge union rules (the gap every prior rule declines): both sides
    # append distinct items to a collection, or insert distinct lines at the
    # same anchor. An opinionated, deterministic ordering (current-appends
    # before replayed-appends) resolves them; a wrong guess still fails the
    # validation pipeline and falls through to the LLM, so the policy is safe.
    "list_union", "dict_union", "brace_union", "insertion_union",
    # C/C++ preprocessor directive dedup: when both sides add the SAME #include/
    # #define, insertion_union declines (shared addition). directive_union
    # collapses the duplicate to one copy. C/C++ only.
    "directive_union",
    # Value-resolution rules for prose/config conflicts the code-shaped rules
    # above decline. text_value_resolution handles pure-prose bumps (no
    # braces/=); dependency_version_resolution handles the TOML inline-table
    # shape (Cargo.toml, fenced-TOML-in-markdown) the prose rule's brace gate
    # excludes. Both take the semver/lexicographic 'newer' value.
    "text_value_resolution", "dependency_version_resolution",
]


@dataclass(frozen=True)
class StructuralResolution:
    """Result of an attempted deterministic resolution.

    ``resolved`` is None when no rule applied (the caller falls through to the
    LLM). When non-None, ``rule`` names which safe rule produced it (for
    auditing/journaling) and ``text`` is the block-interior resolved text, in the
    same shape an LLM candidate's ``resolved_text`` takes (it splices identically).

    ``deferred_core`` carries a mini-conflict's 3-way texts (base, current,
    replayed) when ``partial_disjoint_merge`` resolved the deterministic tails
    but couldn't resolve the overlap core. The orchestrator feeds ONLY the core
    to the LLM (a tiny prompt) and patches the result back into the resolved text.
    ``deferred_core_offset`` is the character offset of ``core_cur`` within
    ``text`` — needed because ``core_cur`` (e.g. a lone ``}``) may appear in the
    reconstructed tails too, so the orchestrator cannot find it by searching.
    """

    rule: Rule | None
    text: str | None
    deferred_core: tuple[str, str, str] | None = None
    deferred_core_offset: int | None = None

    @property
    def resolved(self) -> bool:
        return self.text is not None


def _normalize(text: str) -> str:
    """Whitespace-only normalization for the identical-sides check.

    We do NOT use quality.py's punctuation-stripping normalize here: for
    "are the two sides the same change?" we want to ignore incidental whitespace
    (trailing spaces, line-ending differences) but NOT rewrite structural
    punctuation, since that could mask a real difference.

    CRITICAL: newline boundaries are PRESERVED. Collapsing newlines to spaces
    masks semantic divergence in Python (``return foo`` is one statement;
    ``return\\nfoo`` is two — both parse as valid Python with DIFFERENT ASTs).
    The prior ``" ".join(text.split())`` collapsed newlines, so two sides
    differing only by ``\\n`` vs space were treated as identical, silently
    picking one and dropping the other's structural intent. Now each line is
    independently whitespace-stripped, with newline boundaries preserved.
    """
    if not text:
        return ""
    lines = text.split("\n")
    # Strip trailing/leading space within each line, collapse runs of
    # spaces/tabs to a single space — but keep newlines as line separators.
    return "\n".join(" ".join(line.split()) for line in lines)


def resolve_structurally(unit: ConflictUnit) -> StructuralResolution:
    """Attempt the deterministic resolution rules in priority order.

    Returns the first that applies, else an unresolved result. The unit's sides
    are read from ``unit.current.text`` / ``unit.replayed.text`` / ``unit.base.text``,
    preferring the diff3-refined sides (the tight conflict hunk) when available —
    the rules are designed for hunks, not whole files. Without refinement (e.g.
    a whole-file conflict with no diff3 pass), the raw sides are used. No rule
    mutates the unit. See the module docstring for the complete rule list and
    firing order.
    """
    # Prefer diff3-refined sides when available. The conflict extractor runs
    # git merge-file --diff3 to produce the tightest conflict boundaries (the
    # lines git actually marked as conflicting, without the non-conflicting
    # context the worktree markers sometimes include). The rules are designed
    # for hunks — insertion_union, zealous_merge, disjoint_edits all key on
    # line-level diff opcodes, and a whole-file diff is dominated by `replace`
    # opcodes that make their preconditions fail. The refined hunk gives them
    # the clean `insert` opcodes they expect. Same pattern as
    # _try_dependency_version_resolution (which already prefers refined sides).
    refined = unit.refined_sides
    if refined is not None:
        current, base, replayed = refined  # (current, base, replayed)
    else:
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

        # Mechanical re-application: when token_disjoint DECLINED because the
        # spans overlap, but one side's changes are purely small mechanical
        # substitutions (API rename, operator keyword lint) and the other side
        # is a wholesale rewrite, take the rewriter's text and re-apply the
        # mechanical substitutions where the base-token anchors survive.
        merged = _try_mechanical_reapply_merge(base, current, replayed)
        if merged is not None:
            return StructuralResolution(rule="mechanical_reapply_merge", text=merged)

        # Prose value-resolution: both sides edited the SAME prose line
        # differently (a version-string bump, a changelog header, a date). Every
        # code-shaped rule above declines (no entities, same-line two-sided
        # edit); the LLM struggles on these. Takes the lexicographically-later
        # value (the 'newer version' heuristic). Conservative: fires only on
        # non-code languages (markdown/text/yaml) AND small, single-value-diff
        # conflicts.
        merged = _try_text_value_resolution(unit)
        if merged is not None:
            return StructuralResolution(rule="text_value_resolution", text=merged)

        # Dependency version-resolution: the TOML counterpart to the prose rule
        # above. Fires on the brace-bearing `name = { version = "X" }` shape the
        # prose rule's gates exclude — dependency version literals in Cargo.toml
        # or a fenced-TOML block in a README. Takes the semver-greater version.
        merged = _try_dependency_version_resolution(unit)
        if merged is not None:
            return StructuralResolution(rule="dependency_version_resolution", text=merged)

        # Easy-merge unions. Every rule above DELIBERATELY declines pure
        # insertions/appends (their relative order is ambiguous). These rules
        # resolve the common "both sides appended distinct items" shapes with an
        # opinionated, deterministic ordering (current-appends first, then
        # replayed-appends). The merge is still validated before it's applied,
        # so an ordering that produces invalid code falls through to the LLM —
        # the policy can be opinionated without being unsafe.
        merged = _try_list_union(base, current, replayed)
        if merged is not None:
            return StructuralResolution(rule="list_union", text=merged)
        merged = _try_dict_union(base, current, replayed)
        if merged is not None:
            return StructuralResolution(rule="dict_union", text=merged)
        merged = _try_brace_union(base, current, replayed)
        if merged is not None:
            return StructuralResolution(rule="brace_union", text=merged)
        merged = _try_insertion_union(base, current, replayed)
        if merged is not None:
            return StructuralResolution(rule="insertion_union", text=merged)
        # Convergent-addition merge: when both sides independently added the
        # same content (overlapping adds), keep one copy + append unique extras.
        # Covers the both_add shape where additions overlap but aren't disjoint.
        merged = _try_convergent_addition_merge(base, current, replayed)
        if merged is not None:
            return StructuralResolution(rule="convergent_addition_merge", text=merged)
        merged = _try_directive_union(unit)
        if merged is not None:
            return StructuralResolution(rule="directive_union", text=merged)

        # Partial-disjoint merge (last-resort deterministic): when ALL rules
        # above declined due to a small overlap (1-3 base lines both sides
        # changed), decompose the conflict into deterministic tails + a tiny
        # core. The disjoint tails resolve without an LLM call; the core gets
        # a conservative default validated by Phase B. This is the
        # highest-leverage rule for real C++ conflicts where both sides modify
        # a shared signature but add different code. Runs LAST so the more
        # specific rules (insertion_union, token_disjoint, etc.) get priority.
        merged = _try_partial_disjoint_merge(base, current, replayed)
        if merged is not None:
            if isinstance(merged, StructuralResolution):
                # The deferred-core path returns a StructuralResolution directly
                # (with deferred_core set). Pass it through without re-wrapping.
                return merged
            return StructuralResolution(rule="partial_disjoint_merge", text=merged)

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


def _restore_trailing_newline(merged: str | None, base: str, current: str, replayed: str) -> str | None:
    """Re-append a trailing newline lost by ``splitlines`` + ``"\\n".join``.

    The line-based rules (disjoint, zealous) use ``splitlines()`` which drops a
    trailing empty element, so a result rebuilt with ``"\\n".join`` loses the
    trailing newline present in all three sides. Restoring it (when base,
    current, AND replayed all end with ``"\\n"`` and the merged result doesn't)
    keeps the spliced output from joining its last line to the following
    conflict marker. ``_try_insertion_union`` uses ``split("\\n")`` and is
    unaffected, so it need not call this.
    """
    if not merged or merged.endswith("\n"):
        return merged
    if base.endswith("\n") and current.endswith("\n") and replayed.endswith("\n"):
        return merged + "\n"
    return merged


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
    # Modify/delete blind spot: a line one side DELETES that the other side
    # KEPT (unchanged) is a genuine modify/delete conflict, but the changed-set
    # overlap test misses it (the kept line is an ``equal`` opcode, not in the
    # other side's changed set, so the sets look disjoint). Without this guard
    # the merge applies the deletion and silently drops the kept line. Decline
    # so the conflict escalates (mirrors ``_accept_deletion``'s modify/delete
    # guard, which ``disjoint_edits`` otherwise bypassed).
    cur_deleted = _base_deleted_lines(base_lines, cur_lines)
    rep_deleted = _base_deleted_lines(base_lines, rep_lines)
    # A line current deleted that replayed did NOT touch (kept) → conflict.
    if cur_deleted - rep_base_changed:
        return None
    if rep_deleted - cur_base_changed:
        return None

    # Decline if either side has a pure insertion (a new line with no base
    # anchor). _merge_disjoint_regions silently drops pure insertions, which
    # means a side's addition would be lost. The entity-level rules
    # (entity_disjoint, insertion_union) handle insertions correctly, so
    # defer to them.
    cur_has_insert = _has_pure_insertion(base_lines, cur_lines)
    rep_has_insert = _has_pure_insertion(base_lines, rep_lines)
    if cur_has_insert or rep_has_insert:
        return None

    # Non-overlapping: apply both edits to base. Build a merged line list by
    # walking base and substituting each side's replacement regions.
    merged = _merge_disjoint_regions(base_lines, cur_lines, rep_lines,
                                     cur_base_changed, rep_base_changed)
    return _restore_trailing_newline(merged, base, current, replayed)


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


def _base_deleted_lines(base: list[str], other: list[str]) -> set[int]:
    """Base line indices (0-based) that ``other`` DELETES (a ``delete`` opcode —
    lines present in base but absent from ``other``). Distinct from
    :func:`_base_changed_lines` (which also counts replaces/inserts): a line one
    side deletes that the other side KEEPS is a modify/delete conflict, not a
    disjoint change, and must escalate rather than silently drop the kept line.
    """
    deleted: set[int] = set()
    matcher = line_matcher(base, other)
    for tag, i1, i2, _j1, _j2 in matcher.get_opcodes():
        if tag == "delete":
            deleted.update(range(i1, i2))
    return deleted


def _has_delete_adjacent_to_other_change(
    base: list[str], current: list[str], replayed: list[str]
) -> bool:
    """True if one side DELETES a base line whose NEIGHBOR the other side changed.

    A deletion adjacent to the other side's edit is a genuine conflict (git
    flags it — the diffs overlap when aligned), but a line-by-line walk would
    apply the deletion as a one-sided change and silently drop the line the
    other side's edit abuts. ``adjacent`` = the deleted line's immediate
    neighbor (line±1) is in the other side's changed set. A deletion far from
    any of the other side's changes is safe to apply (a real one-sided delete).
    """
    cur_deleted = _base_deleted_lines(base, current)
    rep_deleted = _base_deleted_lines(base, replayed)
    cur_changed = _base_changed_lines(base, current)
    rep_changed = _base_changed_lines(base, replayed)
    n = len(base)

    def _adjacent(deleted: set[int], other_deleted: set[int], other_changed: set[int]) -> bool:
        for idx in deleted:
            # Only a line the OTHER side did NOT also delete is a real
            # modify/delete conflict. When both sides "delete" the same line
            # (e.g. the conflict unit's base is the whole file but current/
            # replayed are just the marker block — lines outside the block
            # appear deleted by both), it's agreed context, not a conflict.
            if idx in other_deleted:
                continue
            if idx - 1 >= 0 and (idx - 1) in other_changed:
                return True
            if idx + 1 < n and (idx + 1) in other_changed:
                return True
        return False

    return _adjacent(cur_deleted, rep_deleted, rep_changed) or _adjacent(rep_deleted, cur_deleted, cur_changed)


# ---------------------------------------------------------------------------
# Token-level disjoint resolution (Summer, layer 3)
# ---------------------------------------------------------------------------

# Maximum total non-blank lines across the three sides for the token rule to
# fire. Token reconstruction is provably local (it splices disjoint edits), but
# Maximum total non-blank lines (base + current + replayed) for token_disjoint
# to engage. Originally 12 (scoped to small inline conflicts); raised to 500
# after the C++ corpus analysis showed the overlap computation is correct at
# scale but the guard was blocking it on real-world conflict blocks (100-2000+
# lines). The histogram diff is efficient enough for larger inputs, and the
# splice is correct regardless of size. Cases exceeding 500 lines are the
# entity-splitting targets (handled separately).
TOKEN_DISJOINT_MAX_LINES = 500

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
# Mechanical re-application: rewrite-vs-rename interleave
# ---------------------------------------------------------------------------

#: Max token-change-ops for a side to be classified as "mechanical".
_MECHANICAL_MAX_OPS = 20
#: Each mechanical op replaces at most this many base tokens / replacement tokens.
_MECHANICAL_MAX_OP_SIZE = 4
#: Max base lines a mechanical side may change (beyond this = semantic rewrite).
_MECHANICAL_MAX_LINES = 3


def _base_changed_line_count(base: str, side: str) -> int:
    """Number of base lines that differ from ``side`` (non-equal opcodes)."""
    matcher = line_matcher(base.split("\n"), side.split("\n"))
    changed = 0
    for tag, i1, i2, _j1, _j2 in matcher.get_opcodes():
        if tag != "equal":
            changed += max(i2 - i1, 1)
    return changed


def _is_mechanical_side(
    ops: list[tuple[int, int, list[str]]],
    base_tok_count: int,
    base: str,
    side: str,
) -> bool:
    """True when a side's changes are purely small, sparse substitutions.

    A mechanical side makes only small, local swaps (API rename,
    operator-keyword lint). Three conditions:
    1. Each token op is small: ≤ ``_MECHANICAL_MAX_OP_SIZE`` base/replacement tokens.
    2. The total changed base tokens are a SMALL FRACTION of the base (≤25%).
    3. The side changes FEW base lines (≤ ``_MECHANICAL_MAX_LINES``). A semantic
       rewrite changes many lines even when each token-level op is small
       (renaming identifiers across a function). This line-level check is what
       distinguishes "rename one keyword" (mechanical) from "rewrite a function
       body with new identifiers" (semantic).
    """
    if not ops or len(ops) > _MECHANICAL_MAX_OPS:
        return False
    changed_base_toks = 0
    for i1, i2, repl in ops:
        span = i2 - i1
        if span > _MECHANICAL_MAX_OP_SIZE:
            return False
        if len(repl) > _MECHANICAL_MAX_OP_SIZE:
            return False
        changed_base_toks += span
    # Sparse: the mechanical edits touch at most 25% of base tokens.
    if base_tok_count > 0 and changed_base_toks > base_tok_count * 0.25:
        return False
    # Few changed lines: a mechanical edit touches a handful of base lines,
    # not a multi-line rewrite.
    changed_lines = _base_changed_line_count(base, side)
    if changed_lines > _MECHANICAL_MAX_LINES:
        return False
    return True


def _try_mechanical_reapply_merge(
    base: str, current: str, replayed: str,
) -> str | None:
    """Merge a wholesale rewrite with a set of small mechanical substitutions.

    ``token_disjoint`` declines when the two sides' changed base-token spans
    overlap. But when one side is a large rewrite (semantic change) and the
    other made only small mechanical substitutions (API rename, lint), the
    correct merge is: take the rewriter's text, then re-apply the mechanical
    substitutions where their base-token anchors survived the rewrite.

    Detection: classify each side via ``_is_mechanical_side``. Require exactly
    one mechanical + one semantic side; decline when both are mechanical
    (token_disjoint should've handled) or both semantic (genuine conflict).

    Merge: walk the mechanical side's ops against base. For each, check whether
    the semantic side's replacement for that base region still contains the
    original base tokens (the anchor survives). If yes, apply the substitution
    onto the semantic text; if no (the rewrite removed those tokens), skip.
    """
    bt = _tokenize(base)
    ct = _tokenize(current)
    rt = _tokenize(replayed)
    cur_ops = _token_change_ops(bt, ct)
    rep_ops = _token_change_ops(bt, rt)
    if not cur_ops or not rep_ops:
        return None
    # Decline delete-side conflicts (one side empty): a deletion is not a
    # mechanical substitution, and the empty side produces a degenerate merge.
    if not current.strip() or not replayed.strip():
        return None

    cur_mech = _is_mechanical_side(cur_ops, len(bt), base, current)
    rep_mech = _is_mechanical_side(rep_ops, len(bt), base, replayed)
    # Require exactly one mechanical side.
    if cur_mech == rep_mech:
        return None

    if cur_mech:
        mech_ops, sem_text = cur_ops, replayed
    else:
        mech_ops, sem_text = rep_ops, current

    # Build the semantic side's token sequence. We'll apply mechanical subs
    # onto it. The semantic side may have completely different tokens, so we
    # search for the mechanical op's BASE anchor tokens within the semantic
    # text's token stream.
    sem_toks = _tokenize(sem_text)

    # For each mechanical op, try to locate the base anchor tokens (bt[i1:i2])
    # within sem_toks. If found, replace them with the op's replacement tokens.
    applied = list(sem_toks)  # mutable copy
    for i1, i2, repl in mech_ops:
        anchor = bt[i1:i2]
        if not anchor:
            continue
        # Search for the anchor in the (current state of) applied tokens.
        idx = _find_subsequence(applied, anchor)
        if idx < 0:
            continue  # anchor not found — the rewrite removed it; skip this op
        applied[idx:idx + len(anchor)] = repl

    result = _detokenize(applied)
    # Note: when no substitutions could be applied (the semantic rewrite
    # subsumed all the mechanical anchors), result == sem_text. That's still a
    # valid merge — the rewrite already handled those spots. Returning it is
    # correct: the mechanical edits are redundant, not conflicting. The
    # validation pipeline will catch any real defect.
    return result


def _find_subsequence(haystack: list[str], needle: list[str]) -> int:
    """Index of the first occurrence of ``needle`` in ``haystack``, or -1."""
    if not needle:
        return 0
    n, m = len(haystack), len(needle)
    if m > n:
        return -1
    for i in range(n - m + 1):
        if haystack[i:i + m] == needle:
            return i
    return -1


# ---------------------------------------------------------------------------
# Partial-disjoint merge: resolve independent parts, shrink the conflict
# ---------------------------------------------------------------------------

# Maximum number of base lines BOTH sides changed for partial_disjoint_merge
# to engage. When the overlap exceeds this, the conflict is genuinely entangled
# and must go to the LLM. The C++ corpus analysis showed 1-3 overlapping lines
# (typically a function signature or return type) is the sweet spot.
PARTIAL_DISJOINT_MAX_OVERLAP = 5


def _try_partial_disjoint_merge(base: str, current: str, replayed: str) -> str | None:
    """Resolve a conflict with a small overlap core + disjoint tails.

    Fires when ``token_disjoint`` declined because both sides changed the same
    1-3 base lines (e.g. both modified a function signature), but the rest of
    the block has only one-sided or disjoint edits. Decomposes the conflict
    into three zones:

    - **Pre-overlap zone**: base lines before the overlap. Only one side
      changed each line → deterministic splice.
    - **Overlap core**: the 1-3 base lines both sides changed. Try concession
      logic (one side's edit equals base → take the other); if that fails,
      take the current (upstream) side's version as the conservative default.
    - **Post-overlap zone**: base lines after the overlap → same deterministic
      splice.

    Returns the merged text, or None when the overlap exceeds
    :data:`PARTIAL_DISJOINT_MAX_OVERLAP` or the zones can't be cleanly
    partitioned. The splice is safe by construction: the tails have no
    conflicting changes, and the core's conservative default is validated by
    Phase B's compiler check.
    """
    base_lines = base.split("\n")
    cur_lines = current.split("\n")
    rep_lines = replayed.split("\n")

    cur_changed = _base_changed_lines(base_lines, cur_lines)
    rep_changed = _base_changed_lines(base_lines, rep_lines)
    overlap = sorted(cur_changed & rep_changed)

    if not overlap or len(overlap) > PARTIAL_DISJOINT_MAX_OVERLAP:
        return None

    # The overlap region may have gaps (non-contiguous overlapping lines).
    # Only fire when the overlap is a single contiguous run — multiple
    # scattered overlap points indicate a more entangled conflict.
    if len(overlap) > 1:
        expected = list(range(overlap[0], overlap[0] + len(overlap)))
        if overlap != expected:
            return None

    overlap_start = overlap[0]
    overlap_end = overlap[-1] + 1  # exclusive

    # Partition into zones. For the pre/post zones, we need to reconstruct
    # each side's version of those base lines so we can pick the changed one.
    # Walk the opcodes to map base ranges → side ranges.
    def _zone_text(side_lines: list[str], z_start: int, z_end: int) -> str:
        """Reconstruct ``side_lines`` for base range [z_start, z_end)."""
        matcher = line_matcher(base_lines, side_lines)
        out: list[str] = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                # Map base [i1,i2) → side [j1,j2). Emit the portion in [z_start,z_end).
                lo = max(i1, z_start)
                hi = min(i2, z_end)
                if lo < hi:
                    offset = lo - i1
                    out.extend(side_lines[j1 + offset : j1 + offset + (hi - lo)])
            elif tag in ("replace", "insert"):
                # If the replace/insert overlaps the zone, emit the side's version.
                if i1 < z_end and i2 > z_start:
                    out.extend(side_lines[j1:j2])
            elif tag == "delete":
                pass  # deleted lines don't appear in the side's zone
        return "\n".join(out)

    # Pre-overlap zone: base[0 : overlap_start]
    pre_cur = _zone_text(cur_lines, 0, overlap_start)
    pre_rep = _zone_text(rep_lines, 0, overlap_start)
    pre_base = "\n".join(base_lines[:overlap_start])

    # Post-overlap zone: base[overlap_end :]
    post_cur = _zone_text(cur_lines, overlap_end, len(base_lines))
    post_rep = _zone_text(rep_lines, overlap_end, len(base_lines))
    post_base = "\n".join(base_lines[overlap_end:])

    # Overlap core: both sides' versions of base[overlap_start:overlap_end]
    core_cur = _zone_text(cur_lines, overlap_start, overlap_end)
    core_rep = _zone_text(rep_lines, overlap_start, overlap_end)
    core_base = "\n".join(base_lines[overlap_start:overlap_end])

    # Resolve each zone.
    # Pre/post zones: deterministic — pick whichever side changed (or base if
    # neither changed). Use the same _normalize comparison as one_sided_change.
    def _resolve_zone(base_z: str, cur_z: str, rep_z: str) -> str:
        cur_diff = _normalize(cur_z) != _normalize(base_z)
        rep_diff = _normalize(rep_z) != _normalize(base_z)
        if cur_diff and not rep_diff:
            return cur_z
        if rep_diff and not cur_diff:
            return rep_z
        if not cur_diff and not rep_diff:
            return base_z
        # Both changed — try disjoint merge on the sub-zone.
        sub = _try_disjoint_merge(base_z, cur_z, rep_z)
        return sub if sub is not None else cur_z  # conservative default

    pre_resolved = _resolve_zone(pre_base, pre_cur, pre_rep)
    post_resolved = _resolve_zone(post_base, post_cur, post_rep)

    # Overlap core: try concession logic first (normalize-based).
    if _normalize(core_cur) == _normalize(core_base):
        core_resolved = core_rep  # current conceded → take replayed
    elif _normalize(core_rep) == _normalize(core_base):
        core_resolved = core_cur  # replayed conceded → take current
    elif _normalize(core_cur) == _normalize(core_rep):
        core_resolved = core_cur  # agreed → take either
    else:
        # Concession logic failed. Try zealous_merge on the core — it handles
        # the per-line agreed/conceded case at exact-equality granularity,
        # catching multi-line cores where SOME lines agreed and others didn't.
        zealous = _try_zealous_merge(core_base, core_cur, core_rep)
        if zealous is not None:
            core_resolved = zealous
        else:
            # Genuine two-sided conflict in the core. We can't resolve it
            # deterministically. Only emit a deferred_core when the
            # deterministic tails have real content — otherwise the whole
            # conflict is the core and there's nothing to decompose (the
            # rule would just be picking one side, silently dropping the
            # other's intent). Require at least one non-empty tail zone.
            has_tails = bool(pre_resolved.strip()) or bool(post_resolved.strip())
            if not has_tails:
                return None  # no decomposition value; defer to the LLM
            core_resolved = core_cur  # conservative default; patched by LLM
            parts = [p for p in [pre_resolved, core_resolved, post_resolved] if p]
            # Record the core's character offset in the assembled text. The
            # orchestrator splices the LLM-resolved core back at this offset;
            # searching for core_cur is unsafe (it may recur in the tails).
            core_offset = len(pre_resolved) + (1 if pre_resolved else 0)
            return StructuralResolution(
                rule="partial_disjoint_merge",
                text="\n".join(parts),
                deferred_core=(core_base, core_cur, core_rep),
                deferred_core_offset=core_offset,
            )

    # Assemble the full resolution.
    parts = [p for p in [pre_resolved, core_resolved, post_resolved] if p]
    return "\n".join(parts)



# ---------------------------------------------------------------------------
# Easy-merge union rules (the insertion-union gap every prior rule declines)
# ---------------------------------------------------------------------------
#
# These resolve the common "both sides appended distinct items to a collection"
# shapes with a deterministic ordering (current-appends, then replayed-appends).
# A wrong guess still fails the validation pipeline and falls through, so the
# opinionated ordering is safe. Each rule is a pure ``str | None`` function.


#: Keywords/punctuation that signal REAL CODE (not prose). If any appears in the
#: changed lines, the prose value-resolution rule declines — it must not pick
#: one side's code over the other's without understanding semantics.
_CODE_SIGNALS = frozenset({
    "fn", "def", "func", "function", "fun", "struct", "class", "impl",
    "enum", "trait", "interface", "module", "pub", "private", "public",
    "static", "const", "let", "var", "import", "use", "package",
    "return", "if", "else", "for", "while", "match", "switch", "case",
    "async", "await", "yield", "try", "catch", "throw", "raise",
})
#: The line budget for the prose rule — a "value bump" is small (a header, a
#: version line, a date). Larger regions are structural changes, not value bumps.
_TEXT_VALUE_MAX_LINES = 8

#: Code languages where the prose rule must NOT fire (a ``x = 1`` value bump in
#: a .py file is a real assignment, not prose). Markdown/text/yaml/toml/None
#: (unknown) qualify for the prose rule.
_CODE_LANGUAGES_FOR_TEXT_RULE = frozenset({
    "python", "py", "rust", "rs", "javascript", "js", "typescript", "ts",
    "jsx", "tsx", "go", "golang", "java", "c", "cpp", "c++", "csharp", "cs",
    "kotlin", "swift", "scala", "dart", "php",
})


def _try_text_value_resolution(unit: ConflictUnit) -> str | None:
    """Resolve a prose value-bump conflict by taking the lexicographically-later
    value (the 'newer version' heuristic).

    Fires when ALL hold:
    - The conflict's language is NOT a code language (python/rust/js/ts/go/
      java/c/cpp/csharp/kotlin/swift/scala/dart/php). Markdown, text, yaml,
      tol, and None (unknown) qualify — these are prose/config where a value
      bump is a safe deterministic pick.
    - Each side (base/current/replayed) is ≤ :data:`_TEXT_VALUE_MAX_LINES` lines
      (a value bump, not a structural change).
    - The conflict is NOT code-shaped: no code keyword and no ``;``/``{``/``}``
      in the text (defense in depth even for non-code languages; some YAML/JSON
      has braces and should go through dict_union instead).
    - Both sides differ from base, AND from each other, AND all three tokenize
      to the SAME token count with exactly one contiguous differing token span.
      This is the "same context, one value changed" shape (a version string,
      a date, a URL).

    When all conditions hold, the merge takes the lexicographically-later of
    current/replayed for the differing span (the winner). Declines (returns
    None) otherwise.

    The motivating case: CHANGELOG.md / release-notes prose conflicts (version-
    string bumps) that every code-shaped rule declines and the LLM struggles
    on. These files are classified as language='markdown' (or None for plain
    text), so the language gate admits them while excluding real code
    (``x = 1`` in a .py file).
    """
    import re as _re
    _is_version_like = lambda s: bool(_re.search(r"\d+\.\d+", s))
    # Language gate: only fire for non-code (prose/config) languages. A .py
    # assignment ``x = 1`` looks like a value bump to the tokenizer but IS code
    # — the language gate excludes it. Markdown/text/yaml/toml/None qualify.
    lang = (unit.language or "").strip().lower()
    if lang in _CODE_LANGUAGES_FOR_TEXT_RULE:
        return None
    base = unit.base.text or ""
    current = unit.current.text or ""
    replayed = unit.replayed.text or ""
    # All three sides must be small (a value bump, not a structural change).
    for s in (base, current, replayed):
        if s.count("\n") + 1 > _TEXT_VALUE_MAX_LINES:
            return None
    # Must NOT be code-shaped (defense in depth). Check for code signals.
    combined = (base + " " + current + " " + replayed).split()
    for tok in combined:
        head = _re.split(r"[^A-Za-z_]", tok, maxsplit=1)[0]
        if head.lower() in _CODE_SIGNALS:
            return None
    for ch in (";", "{", "}"):
        if ch in base or ch in current or ch in replayed:
            return None
    # The ``=`` operator (assignment) is a strong code signal: ``x = 1`` is a
    # Python/Rust assignment, not a prose value bump. Markdown headings (``===``)
    # or URLs are caught by the 3+ run check below — only a SINGLE ``=`` token
    # (an assignment) triggers the decline.
    for s in (base, current, replayed):
        toks = s.split()
        if any(t == "=" or (t.endswith("=") and len(t) <= 3 and t != "==") for t in toks):
            return None
        if any("=" in t and not t.startswith("http") and len([c for c in t if c == "="]) == 1
               and t not in ("==", "===") and not t.startswith("#")
               for t in toks):
            # A token with a single embedded ``=`` that isn't a heading/URL/==.
            # e.g. ``key=value`` is ambiguous; decline conservatively.
            return None
    cur_toks = current.split()
    rep_toks = replayed.split()
    base_toks = base.split()
    if not cur_toks or len(cur_toks) != len(rep_toks) or len(cur_toks) != len(base_toks):
        return None
    if cur_toks == base_toks or rep_toks == base_toks:
        return None  # one-sided — other rules handle this
    if cur_toks == rep_toks:
        return None  # identical_sides handles this
    first_diff = -1
    last_diff = -1
    for i, (a, b) in enumerate(zip(cur_toks, rep_toks)):
        if a != b:
            if first_diff < 0:
                first_diff = i
            last_diff = i
    cur_span = " ".join(cur_toks[first_diff : last_diff + 1])
    rep_span = " ".join(rep_toks[first_diff : last_diff + 1])
    # Version-vs-prose guard: a CHANGELOG heading reorganization has a version-
    # like token on one side ("0.12.6") and a prose token on the other
    # ("Unreleased"). The correct merge keeps BOTH headings (a section
    # reorganization), not a value pick. Without this guard the rule takes the
    # lexicographically-later token — "Unreleased" (uppercase U > "0") —
    # silently dropping the version section. A genuine version bump has
    # version-like tokens on BOTH sides (1.47.2 vs 1.43.4); a reorganization has
    # a version on one side and prose on the other. Decline the mixed case.
    if _is_version_like(cur_span) != _is_version_like(rep_span):
        return None  # one side is a version, the other is prose — reorg, not a bump
    winner_toks = rep_toks if rep_span > cur_span else cur_toks
    merged_toks = list(cur_toks)
    merged_toks[first_diff : last_diff + 1] = winner_toks[first_diff : last_diff + 1]
    return " ".join(merged_toks)


def _semver_key(version: str) -> tuple:
    """Sort key for a version string, comparing numeric components numerically.

    Parses dotted numeric components (``1.47.2`` → ``(1, 47, 2)``) so that
    ``1.10.0`` sorts AFTER ``1.9.0`` (raw lexicographic would order them wrong).
    A fully-numeric version returns ``(1, nums, suffix_key)`` where ``nums`` is
    the tuple of numeric components and ``suffix_key`` orders pre-release
    suffixes before the suffix-less version (semver precedence). A non-numeric
    version falls back to ``(0, version, "")`` so it still sorts deterministically
    (the caller takes the larger key).
    """
    import re as _re
    # Split off any pre-release/build suffix after the numeric core.
    core = version
    suffix = ""
    m = _re.match(r"^[\d.]+", version)
    if m:
        core = m.group(0)
        suffix = version[len(core):]
    parts = core.split(".")
    try:
        nums = tuple(int(p) for p in parts if p != "")
    except ValueError:
        # Non-numeric component — fall back to plain string ordering.
        return (0, version, "")
    # A version WITH a pre-release suffix sorts BEFORE the same version without
    # one (semver rule); represent "no suffix" as a high-sentinel so it wins.
    suffix_key = (1, "") if suffix == "" else (0, suffix)
    return (1, nums, suffix_key)


def _try_dependency_version_resolution(unit: ConflictUnit) -> str | None:
    """Resolve a dependency version-bump conflict by taking the semver-greater
    version (the 'newer release' heuristic).

    This is the TOML/Cargo counterpart to ``_try_text_value_resolution``. The
    prose rule declines on the brace-bearing TOML inline-table shape
    (``name = { version = "X", features = [...] }``) because its brace/``=``
    gates exclude anything that looks like code — correctly, for real code, but
    too conservative for a dependency version literal in a markdown code fence
    or Cargo.toml. This rule recognizes that specific shape.

    Fires when ALL hold:
    - The conflict's language is NOT a code language (the prose rule's gate).
      Markdown/text/yaml/toml/None qualify. At runtime a fenced-TOML block in a
      README has ``language='markdown'``; a Cargo.toml conflict has ``'toml'``.
    - Each side (base/current/replayed) is ≤ :data:`_TEXT_VALUE_MAX_LINES` lines.
    - Each side is a dependency declaration in ONE of these shapes:
        * ``name = "X.Y.Z"``              (simple version string), or
        * ``name = { ..., version = "X.Y.Z", ... }``  (TOML inline table).
    - The current and replayed sides are IDENTICAL except for the version
      literal (tokenized comparison: same token count, exactly one differing
      token span, and that span is a quoted version string). The base may differ
      more (it's the older state both sides bumped from) but must itself carry a
      version literal in the same position.

    When all conditions hold, the merge takes the side whose version literal is
    semver-greater (falling back to lexicographic for non-semver strings) and
    returns it verbatim. Declines (returns None) otherwise. The rule is
    general — any dependency version literal, not a corpus-specific patch.
    """
    import re as _re
    # Language gate: identical to the prose rule. A real-code assignment
    # (``x = "1.2"`` in a .py file) must NOT fire here.
    lang = (unit.language or "").strip().lower()
    if lang in _CODE_LANGUAGES_FOR_TEXT_RULE:
        return None
    # Prefer the diff3-refined sides (tightest view) when present: the worktree
    # marker block may include adjacent non-conflicting lines git's 3-way merge
    # stripped, and the version bump lives in the refined (smaller) region.
    refined = unit.refined_sides
    if refined is not None:
        current, base, replayed = refined  # (current, base, replayed)
    else:
        base = unit.base.text or ""
        current = unit.current.text or ""
        replayed = unit.replayed.text or ""
    # Size gate: a value bump, not a structural rewrite.
    for s in (base, current, replayed):
        if s.count("\n") + 1 > _TEXT_VALUE_MAX_LINES:
            return None
    if not current.strip() or not replayed.strip():
        return None
    if _normalize(current) == _normalize(replayed):
        return None  # identical_sides handles this

    # Recognize the dependency-version shape and extract the version literal
    # from each side. Two accepted shapes:
    #   name = "X.Y.Z"
    #   name = { ..., version = "X.Y.Z", ... }
    _VERSION_IN_TABLE = _re.compile(r'version\s*=\s*"([^"]*)"')
    _SIMPLE_VERSION = _re.compile(r'=\s*"([^"]*)"')

    def _extract_version(text: str) -> str | None:
        # Prefer the TOML inline-table `version = "..."` form; fall back to the
        # simple `name = "..."` form only when there's no `{` (an inline table
        # would also match the simple regex on its closing — avoid that).
        if "{" in text:
            m = _VERSION_IN_TABLE.search(text)
            return m.group(1) if m else None
        m = _SIMPLE_VERSION.search(text)
        return m.group(1) if m else None

    cur_v = _extract_version(current)
    rep_v = _extract_version(replayed)
    base_v = _extract_version(base)
    if not cur_v or not rep_v or not base_v:
        return None  # not a recognizable version-literal shape on all three

    # The sides must be IDENTICAL except for the version literal. Normalize by
    # replacing each side's version with a placeholder, then compare tokens.
    def _mask(text: str, v: str) -> str:
        return text.replace(f'"{v}"', '"__VER__"', 1)
    cur_masked = _mask(current, cur_v)
    rep_masked = _mask(replayed, rep_v)
    if cur_masked != rep_masked:
        return None  # the sides differ in MORE than just the version

    # Resolve: take the semver-greater version. _semver_key handles numeric
    # ordering (1.10.0 > 1.9.0); non-semver falls back to string ordering.
    winner = current if _semver_key(cur_v) >= _semver_key(rep_v) else replayed
    return winner


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
    b = _find_single_list(base)
    if b is None:
        return None
    base_inner = b[0]
    # Decline multi-line lists — the rebuild flattens to one line, destroying
    # formatting. (_try_dict_union already has this guard.)
    if "\n" in base_inner:
        return None
    base_items = _split_list_items(base_inner)
    cur = _find_single_list(current)
    rep = _find_single_list(replayed)
    if cur is None or rep is None:
        return None
    # Each side must preserve base items verbatim (same order, no removal) and
    # differ only by appending. Compute the appended tail.
    cur_items = _split_list_items(cur[0])
    rep_items = _split_list_items(rep[0])
    cur_appended = _appended_tail(base_items, cur_items)
    rep_appended = _appended_tail(base_items, rep_items)
    if cur_appended is None or rep_appended is None:
        return None  # a side reordered/removed/edited base items
    # Disjoint appends (no shared new item). A shared item means both sides made
    # the same addition — let identical_sides/zealous handle it, not us.
    if set(cur_appended) & set(rep_appended):
        return None
    merged_items = base_items + cur_appended + rep_appended
    # Verify the surrounding text (prefix before ``[`` and suffix after ``]``)
    # is invariant across all three sides. If a side changed the assignment
    # target, a comment, or other non-item structure, the merge would silently
    # drop that side's intent by inheriting the other side's surrounding text.
    b_pre, b_suf = base[: b[1]], base[b[2]:]
    if current[:cur[1]] != b_pre or current[cur[2]:] != b_suf:
        return None
    if replayed[:rep[1]] != b_pre or replayed[rep[2]:] != b_suf:
        return None
    return (
        b_pre
        + "["
        + ", ".join(merged_items)
        + "]"
        + b_suf
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
    base_inner = b[0]
    # Only inline (single-line) dicts: multi-line reconstruction would mangle
    # indentation. The base dict literal must not contain a newline.
    if "\n" in base_inner:
        return None
    base_entries = _split_dict_entries(base_inner)
    cur = _find_single_dict(current)
    rep = _find_single_dict(replayed)
    if cur is None or rep is None:
        return None
    cur_entries = _split_dict_entries(cur[0])
    rep_entries = _split_dict_entries(rep[0])
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
    # Verify surrounding text is invariant (same rationale as _try_list_union).
    b_pre, b_suf = base[: b[1]], base[b[2]:]
    if current[:cur[1]] != b_pre or current[cur[2]:] != b_suf:
        return None
    if replayed[:rep[1]] != b_pre or replayed[rep[2]:] != b_suf:
        return None
    return _rebuild_dict(base, merged)


def _try_brace_union(base: str, current: str, replayed: str) -> str | None:
    """Merge two sides that each APPEND distinct items to a single ``{...}`` brace
    list (C/C++ enum variants, struct field lists, initializer sets).

    The ``{...}`` analog of :func:`_try_list_union` (which matches ``[...]``) and
    the bare-comma analog of :func:`_try_dict_union` (which requires ``key:
    value`` entries). Fires for shapes like ``enum X { A, B }`` where both sides
    append a variant: list_union won't match (wrong brackets), dict_union
    declines (no ``:`` in the segments), insertion_union needs whole lines. This
    rule splits on top-level commas WITHOUT a ``:`` requirement, so enum variants
    and bare struct-field lists qualify.

    Declines (→ None) when: there's no single ``{...}``; a side changed non-item
    structure; the two sides appended the SAME item; either side touched base
    items; or the ``{...}`` spans multiple lines (the rebuild flattens to one
    line, destroying formatting — multi-line shapes defer to insertion_union,
    which is line-granular and preserves them).

    Dispatches AFTER dict_union (the more-specific keyed-entry rule) so a real
    dict ``{a: 1, b: 2}`` is handled by dict_union; brace_union only fires when
    dict_union declined (segments lack ``:``).
    """
    b = _find_single_dict(base)
    if b is None:
        return None
    base_inner = b[0]
    if "\n" in base_inner:
        return None  # multi-line → insertion_union territory
    base_items = _split_list_items(base_inner)
    cur = _find_single_dict(current)
    rep = _find_single_dict(replayed)
    if cur is None or rep is None:
        return None
    cur_items = _split_list_items(cur[0])
    rep_items = _split_list_items(rep[0])
    cur_appended = _appended_tail(base_items, cur_items)
    rep_appended = _appended_tail(base_items, rep_items)
    if cur_appended is None or rep_appended is None:
        return None  # a side reordered/removed/edited base items
    if set(cur_appended) & set(rep_appended):
        return None  # shared addition → identical_sides/zealous territory
    merged_items = base_items + cur_appended + rep_appended
    # Surrounding text (prefix before ``{`` and suffix after ``}``) must be
    # invariant — a side that changed the enum/struct name or a trailing comment
    # would be silently dropped by inheriting the other side's surrounding text.
    b_pre, b_suf = base[: b[1]], base[b[2]:]
    if current[:cur[1]] != b_pre or current[cur[2]:] != b_suf:
        return None
    if replayed[:rep[1]] != b_pre or replayed[rep[2]:] != b_suf:
        return None
    return b_pre + "{" + ", ".join(merged_items) + "}" + b_suf


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


# C/C++ preprocessor directives eligible for directive_union. Only the ADDITIVE
# directives (#include, #define, #pragma, #undef) — NOT conditional directives
# (#ifdef/#if/#else/#endif), which form a tree structure that needs parsing.
_DIRECTIVE_RE = __import__("re").compile(
    r"^\s*#\s*(include|define|pragma|undef)\b"
)


def _is_directive_line(line: str) -> bool:
    return bool(_DIRECTIVE_RE.match(line))


def _try_directive_union(unit) -> str | None:
    """Merge C/C++ preprocessor directive additions that insertion_union declined.

    The one gap ``_try_insertion_union`` (which fires earlier in the dispatch)
    leaves: when both sides add the IDENTICAL directive line (e.g. both add
    ``#include <newheader.h>``), insertion_union treats the shared addition as
    ambiguous and declines (its disjoint-set check). This rule closes that gap
    with DEDUPLICATION — identical directive additions collapse to one copy.

    Strictly additive: fires only when each side is a pure insertion of
    ``#include``/``#define``/``#pragma``/``#undef`` lines against base (no base
    line modified or deleted), exactly like insertion_union's preconditions. The
    difference is only in how OVERLAPPING additions are handled: insertion_union
    declines on overlap; directive_union dedupes (a shared ``#include`` is
    unambiguous — keep one copy, not two).

    NOT in scope: ``#ifdef``/``#if``/``#else``/``#endif`` (conditional structure
    — needs tree parsing); include sorting/reordering (the feedback warns
    include order can be semantic; this rule preserves base order, current's
    distinct directives before replayed's). C/C++ only — other languages have no
    ``#`` directives.
    """
    lang = unit.language
    if lang not in ("c", "cpp", "c++"):
        return None
    base = unit.base.text or ""
    current = unit.current.text or ""
    replayed = unit.replayed.text or ""
    base_lines = base.split("\n")
    cur_ins = _pure_insertion_runs(base_lines, current.split("\n"))
    rep_ins = _pure_insertion_runs(base_lines, replayed.split("\n"))
    if cur_ins is None or rep_ins is None:
        return None  # a side modified/deleted a base line
    # Only fire when the added lines are ALL directives (otherwise this is a
    # mixed add that insertion_union already handled or declined for good reason).
    cur_flat = [ln for run in cur_ins.values() for ln in run]
    rep_flat = [ln for run in rep_ins.values() for ln in run]
    if not cur_flat or not rep_flat:
        return None  # nothing added on a side → not the directive-add shape
    if not all(_is_directive_line(ln) for ln in cur_flat + rep_flat):
        return None  # non-directive additions → insertion_union's territory
    # Dedupe: a directive both sides added collapses to one copy. Distinct
    # directives from each side are kept (current's before replayed's).
    cur_set = set(cur_flat)
    rep_set = set(rep_flat)
    shared = cur_set & rep_set
    if not shared:
        return None  # no overlap → insertion_union already resolved the disjoint case
    cur_distinct = [ln for ln in cur_flat if ln not in shared]
    rep_distinct = [ln for ln in rep_flat if ln not in shared]
    # The SHARED directives only, deduped and in current's first-seen order.
    # (Previously this was ``dict.fromkeys(cur_flat)`` — ALL of current's
    # directives, which made cur_distinct get emitted a second time.)
    shared_ordered = [ln for ln in dict.fromkeys(cur_flat) if ln in shared]
    # Walk base, emitting the directive block at the shared anchor(s). Since all
    # additions are directives and they overlap, emit a single deduped block at
    # the lowest insertion anchor (the common case: both added after the same
    # base #include). current's distinct first, then shared (once), then
    # replayed's distinct — matches insertion_union's current-before-replayed
    # ordering with shared collapsed.
    out: list[str] = []
    emitted = False
    min_anchor = min(min(cur_ins), min(rep_ins))
    for i, bl in enumerate(base_lines):
        if i == min_anchor and not emitted:
            out.extend(cur_distinct)
            out.extend(shared_ordered)
            out.extend(rep_distinct)
            emitted = True
        out.append(bl)
    if not emitted:  # additions were all trailing
        out.extend(cur_distinct)
        out.extend(shared_ordered)
        out.extend(rep_distinct)
    return "\n".join(out)


def _extract_definition_names(lines: list[str]) -> dict[str, str]:
    """Map function/method definition NAME → its full signature text.

    Scans for lines that look like C/C++/Rust function/method definitions or
    declarations: ``<type> <name>(...)`` followed by ``{`` or ``;`` (either on
    the same line or the next line). Returns a dict of name → the trimmed
    signature line. Used by the convergent-add guard to detect "both sides
    added a function with the same name but different signatures."
    """
    import re

    names: dict[str, str] = {}
    # Match: identifier immediately followed by ``(`` with parameters, ending
    # with ``)``, optionally followed by ``const``/``{``/``;``. The ``{`` or
    # ``;`` may be on the NEXT line (common C++ brace-on-next-line style).
    sig_pat = re.compile(
        r"\b([A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?:const\s*)?"
        r"(?:[;{]\s*$)?\s*$"
    )
    skip_keywords = frozenset({
        "if", "while", "for", "switch", "return", "sizeof", "catch",
        "fn", "def", "class", "struct", "enum", "namespace", "typedef",
    })
    for idx, ln in enumerate(lines):
        stripped = ln.strip()
        if not stripped or stripped.startswith(("#", "//", "/*", "*")):
            continue
        # Exclude lines that are clearly statements, not definitions.
        if stripped.startswith(("return ", "if ", "while ", "for ", "throw ",
                                "assert", "static_assert")):
            continue
        m = sig_pat.search(stripped)
        if not m:
            continue
        name = m.group(1)
        if name in skip_keywords:
            continue
        # Confirm this is a definition/declaration: the line or the next
        # non-blank line must end with ``{`` or ``;``.
        next_stripped = ""
        if idx + 1 < len(lines):
            next_stripped = lines[idx + 1].strip()
        is_def = stripped.endswith((";", "{")) or next_stripped.startswith(("{",))
        if is_def:
            names[name] = stripped
    return names


def _additions_have_same_name_conflict(
    cur_flat: list[str], rep_flat: list[str],
) -> bool:
    """True when both sides' additions define a function with the same name but
    different signature text — a signature conflict, not an additive merge."""
    cur_defs = _extract_definition_names(cur_flat)
    rep_defs = _extract_definition_names(rep_flat)
    shared_names = set(cur_defs) & set(rep_defs)
    for name in shared_names:
        if cur_defs[name] != rep_defs[name]:
            return True  # same function name, different signatures
    return False


def _try_convergent_addition_merge(base: str, current: str, replayed: str) -> str | None:
    """Merge when both sides added the same (or nearly the same) content.

    When both sides independently added identical lines relative to base, the
    correct merge is to keep one copy and append any unique extras from each
    side. This is the ``both_add`` shape where the additions overlap (shared
    lines) but aren't fully disjoint.

    Returns None when the sides aren't pure additions (either side modified or
    deleted base lines), or when the additions don't share enough content to be
    considered convergent (≥50% of the smaller side's additions are shared).
    """
    base_lines = base.split("\n")
    cur_lines = current.split("\n")
    rep_lines = replayed.split("\n")
    cur_ins = _pure_insertion_runs(base_lines, cur_lines)
    rep_ins = _pure_insertion_runs(base_lines, rep_lines)
    if cur_ins is None or rep_ins is None:
        return None  # a side modified/deleted base lines
    cur_flat = [ln for run in cur_ins.values() for ln in run if ln.strip()]
    rep_flat = [ln for run in rep_ins.values() for ln in run if ln.strip()]
    if not cur_flat or not rep_flat:
        return None
    cur_set = set(cur_flat)
    rep_set = set(rep_flat)
    shared = cur_set & rep_set
    smaller = min(len(cur_set), len(rep_set))
    if smaller == 0 or len(shared) < smaller * 0.5:
        return None  # not convergent enough
    # Same-name definition guard: if both sides' additions each introduce a
    # function/method with the SAME name but DIFFERENT text, this is a
    # signature conflict (both sides wrote alternative versions of the same
    # function), not an additive merge. Concatenating both would produce two
    # real definitions → duplicate_definition failure. Decline so the LLM
    # picks one. (The shared doc-comment lines create enough overlap for the
    # convergence threshold to pass, but the function bodies diverge.)
    if _additions_have_same_name_conflict(cur_flat, rep_flat):
        return None
    # Build the merge: walk base, at each anchor emit current's additions first,
    # then any replayed additions not already emitted. Both sides check
    # emitted_added so shared content at different anchors is deduplicated.
    emitted_added: set[str] = set()
    out: list[str] = []
    for i, bl in enumerate(base_lines):
        if i in cur_ins:
            for ln in cur_ins[i]:
                if ln.strip() and ln in emitted_added:
                    continue
                out.append(ln)
                if ln.strip():
                    emitted_added.add(ln)
        if i in rep_ins:
            for ln in rep_ins[i]:
                if ln.strip() and ln in emitted_added:
                    continue
                out.append(ln)
                if ln.strip():
                    emitted_added.add(ln)
        out.append(bl)
    # Trailing insertions.
    for ln in cur_ins.get(len(base_lines), []):
        if ln.strip() and ln in emitted_added:
            continue
        out.append(ln)
        if ln.strip():
            emitted_added.add(ln)
    for ln in rep_ins.get(len(base_lines), []):
        if ln.strip() and ln in emitted_added:
            continue
        out.append(ln)
    return "\n".join(out)


# Helpers for the union rules (pure, regex-based — no AST needed).


def _find_single_list(text: str):
    """The ``(inner, open_offset, close_offset)`` of the SOLE ``[...]`` list in
    text, or None.

    ``inner`` is the text between the brackets; ``open_offset``/``close_offset``
    are the char offsets of the ``[`` and ``]`` (so the caller can splice).
    Rejects nested/extra brackets (a single list with no inner ``[``).

    Also rejects SUBSCRIPT / index expressions: a ``[`` immediately preceded by
    an identifier char, ``]``, or ``)`` is an indexing operation (``a[0]``,
    ``call()[1]``, ``grid[r][c]``), NOT a list literal. Without this guard, two
    sides editing a subscript (``a[0]`` → ``a[0, 1]`` / ``a[0, 2]``) would be
    wrongly merged into ``a[0, 1, 2]`` (turning an integer subscript into a
    tuple subscript) — a wrong merge that's valid Python and can slip through.
    A list literal's ``[`` follows ``=`` / whitespace / ``(`` / ``,`` / start.
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
    # Reject subscript: the char right before '[' is an identifier char / ']'
    # / ')' → it's an indexing operation, not a list literal.
    if open_off > 0:
        prev = text[open_off - 1]
        if prev.isalnum() or prev == "_" or prev in "])":
            return None
    return (inner, open_off, close_off)


def _split_list_items(inner: str) -> list[str]:
    """Split a list literal's interior into stripped items (no surrounding []).

    String-aware AND escape-aware: a ``,`` inside a string literal (``"a,b"``)
    is NOT a separator, and an escaped quote (``\\"``) does NOT close the string.
    Without escape-awareness, ``"c\\"d"`` closes the string at the escaped quote,
    corrupting the split and causing a false decline of a valid list-union merge.
    """
    if not inner.strip():
        return []
    items: list[str] = []
    cur = ""
    in_str: str | None = None
    chars = list(inner)
    i = 0
    n = len(chars)
    while i < n:
        ch = chars[i]
        if in_str is not None:
            cur += ch
            if ch == "\\" and i + 1 < n:
                # Escape: skip the next char (it can't close the string).
                cur += chars[i + 1]
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in ('"', "'"):
            in_str = ch
            cur += ch
            i += 1
            continue
        if ch == ",":
            if cur.strip():
                items.append(cur.strip())
            cur = ""
            i += 1
            continue
        cur += ch
        i += 1
    if cur.strip():
        items.append(cur.strip())
    return items


def _find_single_dict(text: str):
    """The ``(inner, open_off, close_off)`` of the SOLE ``{...}`` dict in text.

    Returns None if there's not exactly one brace-delimited dict. ``inner`` is
    the text between the braces. ``open_off`` / ``close_off`` are the byte
    offsets of the ``{`` and just after the ``}``.
    """
    import re

    m = re.search(r"\{(.*)\}", text, re.DOTALL)
    if m is None:
        return None
    inner = m.group(1)
    if "{" in inner or "}" in inner:
        return None
    return (inner, m.start(), m.end())


def _split_dict_entries(inner: str) -> list[str]:
    """Split a dict interior into entries (``key: value``), preserving text.

    STRING-AWARE and escape-aware (delegates to :func:`_split_list_items`, the
    same string/escape-aware top-level-comma splitter the list rule uses): a
    comma INSIDE a string value (``"hello, world"``, ``"a:b,c"``) must NOT split
    the entry. The naive ``inner.split(",")`` tore such values apart; the
    post-hoc ``all(':')`` guard happened to decline the corrupted halves (so no
    wrong merge ever resulted) but it caused dict_union to wrongly DECLINE a
    legitimate both-sides-add-keys merge whenever a value contained a comma.

    Remains conservative: a comma in a NON-string context whose segment lacks a
    ``:`` (e.g. a function-call value ``f(a, b)``) still declines via the guard.
    """
    if not inner.strip():
        return []
    # Reuse the string/escape-aware top-level-comma splitter. A comma inside a
    # string literal is no longer a separator; a top-level comma still is.
    parts = _split_list_items(inner)
    # Conservative: keep only when every segment looks like `key: val`. A
    # top-level comma whose segment lacks ':' (e.g. ``f(a, b)``) → decline.
    if not all(":" in p for p in parts):
        return []
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


def _rebuild_dict(base: str, entries: list[str]) -> str:
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
    # Modify/delete adjacency guard (mirrors the disjoint rule's guard). A
    # deletion by one side of a base line ADJACENT to a line the other side
    # changed is a genuine conflict (git flags it — the diffs overlap when
    # aligned), but the line-by-line walk would apply the deletion as a one-
    # sided change and silently drop the line the other side's edit abuts.
    # Decline so the conflict escalates. Only applied when current and replayed
    # have DIFFERENT line counts (an asymmetric add/remove — a real deletion):
    # when they have the SAME (shorter-than-base) count, the "deletions" are
    # typically shared context outside a conflict marker block (base is the
    # whole file, current/replayed are the block), not a real modify/delete.
    cur_split = current.splitlines()
    rep_split = replayed.splitlines()
    if (
        len(cur_split) != len(rep_split)
        and _has_delete_adjacent_to_other_change(base_lines, cur_split, rep_split)
    ):
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
            # Span-overlap guard: if a replayed region starts WITHIN (i, end)
            # (this cur region spans past it), the jump to `end` would skip it.
            # That's only safe if the skipped region's replacement is already
            # covered by `rep` at the CORRECT positional offset. Otherwise the
            # replayed edit is silently dropped — decline.
            for rs in range(i + 1, end):
                if rs in rep_regions:
                    re_, rrep = rep_regions[rs]
                    if not _region_covered(rep, i, end, rs, re_, rrep):
                        return None
            out.extend(rep)
            i = end
        elif in_rep:
            end, rep = rep_regions[i]
            # Symmetric guard: a current region starting within (i, end).
            for cs in range(i + 1, end):
                if cs in cur_regions:
                    ce, crep = cur_regions[cs]
                    if not _region_covered(rep, i, end, cs, ce, crep):
                        return None
            out.extend(rep)
            i = end
        else:
            out.append(base_lines[i])
            i += 1
    return _restore_trailing_newline("\n".join(out), base, current, replayed)


def _region_covered(
    emitted: list[str], span_start: int, span_end: int, r_start: int, r_end: int,
    r_replacement: list[str],
) -> bool:
    """True if the other side's region replacement is already covered by ``emitted``.

    When one side's region (spanning base lines ``[span_start, span_end)``)
    spans past the other's region (``[r_start, r_end)``), the walk emits the
    spanning side and jumps past the other. This is only safe if the jumped-past
    region's edit is redundant — its replacement already appears at the CORRECT
    POSITIONAL offset within ``emitted`` (not just as a coincidental suffix). If
    the jumped-past edit differs or is a pure deletion, it would be silently
    dropped.

    ``span_start`` is the spanning region's base-line start; ``r_start`` is the
    jumped-past region's base-line start. The positional offset is
    ``r_start - span_start``. The replacement must align exactly there.

    LENGTH INVARIANT: the 1:1 base-line→emitted-line correspondence only holds
    when the spanning side's replacement is the SAME length as its base span
    (``len(emitted) == span_end - span_start``). A grown or shrunk replacement
    shifts every position after the change, so the naive offset is an arbitrary
    position and a coincidental textual match could wrongly declare the inner
    edit "covered" — silently dropping the other side's change. When the lengths
    differ, decline (return False) so the conflict escalates.

    Exception: a pure DELETION (``r_replacement == []``) is covered whenever the
    spanning side emitted no content at the deletion's positional offset AND the
    deletion is fully contained within the spanning side's span. The
    ``offset >= len(emitted)`` check is sound regardless of span length (a
    deletion adds nothing, so a shorter emitted list still correctly indicates
    the line is gone). The span-containment check (``r_end <= span_end``) is
    essential: without it, a deletion that extends PAST the spanning side's span
    would be declared "covered" and the base lines beyond the span (which the
    spanning side deliberately kept) would be silently dropped from the merge.
    Applying the length guard to deletions would wrongly reject agreed deletions
    (both sides drop the same trailing line), so only containment is required.
    """
    if not r_replacement:
        # A pure deletion is covered if (a) the deletion region is fully
        # contained within the spanning side's span — otherwise base lines past
        # the span get dropped — AND (b) the spanning side ALSO deleted that base
        # line (emitted no content at the deletion's positional offset).
        if r_end > span_end:
            return False
        offset = r_start - span_start
        return offset >= len(emitted)
    if len(emitted) != (span_end - span_start):
        return False
    offset = r_start - span_start
    if offset < 0 or offset + len(r_replacement) > len(emitted):
        return False
    return emitted[offset : offset + len(r_replacement)] == r_replacement


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
) -> str:
    """Reconstruct a merged text by applying each side's non-overlapping edits.

    Walks ``base`` line by line. For each base line:
    - if it's the start of current's changed region → emit current's replacement
      block and skip past the region;
    - elif it's the start of replayed's changed region → emit replayed's block;
    - else emit the base line unchanged.
    Because the changed-region sets are disjoint, the two substitutions never
    collide. Pure insertions (no base anchor) are handled by the caller
    (``_try_disjoint_merge`` declines them via ``_has_pure_insertion`` before
    calling this), so this function always returns a complete merged text.
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
    return "\n".join(out)


def _has_pure_insertion(base: list[str], other: list[str]) -> bool:
    """True if ``other`` has a pure insertion (new lines with no base anchor).

    Used by _try_disjoint_merge to decline when a side's insertion would be
    silently dropped by _merge_disjoint_regions (which only handles
    replace/delete regions anchored on base lines).
    """
    matcher = line_matcher(base, other)
    for tag, i1, i2, _j1, _j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if i1 == i2:
            return True  # pure insertion: no base anchor
    return False


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
_FIELD_DECL_KEYWORDS = frozenset({"const", "static", "let", "type", "var"})


def _first_real_keyword(header_line: str) -> str:
    r"""Strip leading visibility prefixes (including ``pub(crate)``/``pub(super)``
    /``pub(in path)``) and return the first real declaration keyword, or ``""``.

    Handles stacked modifiers: ``pub unsafe static``, ``pub(crate) async fn``,
    ``unsafe pub(in crate::m) extern fn``, etc. Also handles ``extern "C"``
    (the ABI string after ``extern`` is consumed as part of the modifier).
    """
    toks = header_line.lstrip().split()
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in _VISIBILITY_PREFIXES:
            i += 1
            # After ``extern``, skip an optional ABI string (``"C"``, ``"system"``).
            if t == "extern" and i < len(toks) and toks[i].startswith('"'):
                i += 1
            continue
        if t.startswith("pub(") and not t.endswith(")"):
            # ``pub(in crate::m)`` split into multiple tokens — consume until ``)``.
            i += 1
            while i < len(toks) and not toks[i].endswith(")"):
                i += 1
            if i < len(toks):
                i += 1  # consume the ``)`` token
            continue
        if t.startswith("pub("):
            # Single-token ``pub(crate)`` — consumed.
            i += 1
            continue
        break
    return toks[i] if i < len(toks) else ""


def _is_bare_function_header(header_line: str) -> bool:
    """True when ``header_line`` declares a bare function (not a container).

    Handles visibility/async modifiers preceding the function keyword:
    ``pub fn``, ``pub(crate) fn``, ``export function``, ``async def``,
    ``unsafe extern fn``, etc. A class/struct/impl/enum header returns False.
    """
    return _first_real_keyword(header_line) in _FN_DECL_KEYWORDS


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


def _body_below_header(body: str, lang: str | None = None) -> str:
    """Strip the declaration header and whitespace-collapse the body content.

    Used by ``_is_pure_rename`` to compare a renamed entity's body against the
    base WITHOUT the header (a rename changes the header by definition) while
    preserving string values (so a string-value edit registers as a divergence).
    Handles one-liners (``def f(): x``) — a naive ``split("\\n", 1)`` strips their
    entire content. Uses a STRING-PRESERVING header split (not the parser's
    ``split_header_body``, which blanks string values via ``normalize_body``).
    """
    rest = _strip_decl_header(body, lang)
    return _ws_collapse(rest, lang=lang)


def _strip_decl_header(body: str, lang: str | None = None) -> str:
    """Strip the declaration header line from ``body``, preserving strings.

    For multi-line bodies, drops the first line. For one-liners, splits at the
    header boundary: Python ``def f(): BODY`` → ``BODY`` (after the first ``:)``);
    Family-A ``fn f() { BODY }`` → the content after the first line or within the
    braces. Does NOT blank string values (unlike ``split_header_body``).

    The header boundary is found on a STRING-BLANKED copy so a ``:`` or ``{``
    inside a string literal in the header (e.g. a default arg ``x="a:b"``) is
    not mistaken for the boundary. The slice is then applied to the ORIGINAL
    body so string values in the body portion are preserved.
    """
    if "\n" in body:
        return body.split("\n", 1)[1]
    # Find the header boundary on a string-blanked copy (so ':'/'{' inside a
    # string in the header doesn't trigger a false split). Uses the canonical
    # lexer so EVERY string form is handled (Rust raw, C++ raw, triple-quote);
    # a naive regex-only blanker would leak raw-string content and let a ':'
    # or '{' inside a raw string false-trigger the header split.
    from capybase.adapters.string_lexer import blank_strings
    blanked = blank_strings(body, lang, string_char=" ")
    if lang in (None, "python", "ruby"):
        # Python one-liner: ``def f(): return 1`` → header is ``def f():``
        idx = blanked.find(":")
        if 0 <= idx < len(body) - 1:
            return body[idx + 1:].strip()
    else:
        # Brace-lang one-liner: ``fn f() { x }`` → body is ``x``
        idx = blanked.find("{")
        if 0 <= idx < len(body) - 1:
            return body[idx + 1:].strip()
    return body


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
    if lang not in ("python", "rust", "c", "cpp", "c++"):
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
    # Agreed ADDITIONS: an entity both sides ADDED with the same body is an
    # agreed change, not a conflict (mirrors ``agreed_renames`` for renames).
    # Without this, a shared addition landed in both touched sets, counted as
    # overlap, and the merge wrongly declined even though the only real conflict
    # was the distinct additions (which are disjoint by construction).
    agreed_additions: set = set()
    overlap_ids = set(cur_touched) & set(rep_touched)
    for ident in overlap_ids:
        if ident in base_by_id:
            continue  # a base entity both touched — handled by agreed_renames
        cur_e = next((e for e in cur_ents if e.identity == ident), None)
        rep_e = next((e for e in rep_ents if e.identity == ident), None)
        if (
            cur_e is not None
            and rep_e is not None
            and _ws_collapse(cur_e.body, lang) == _ws_collapse(rep_e.body, lang)
        ):
            agreed_additions.add(ident)
    # Overlap → genuine intra-entity conflict — UNLESS both sides made the SAME
    # rename (agreed change) or the SAME addition (agreed), which is not a
    # conflict. Decline for the line/LLM path otherwise.
    overlap = set(cur_touched) & set(rep_touched)
    if overlap - agreed_renames - agreed_additions:
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
            indent = ln[: len(ln) - len(ln.lstrip(" \t"))]
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
        """True if ``ent`` is a rename whose body content == the base entity's.

        Compares the bodies EXCLUDING the header line (a rename changes the
        header by definition) using the string-PRESERVING
        :func:`_ws_collapse` — not the string-blanking :func:`_body_content`.
        Without string preservation, a rename that ALSO edits a string value
        (``return "hello"`` → ``return "world"``) is misclassified as a pure
        rename and the value change is silently dropped. Mirrors the same
        string-preservation fix already applied in :func:`_try_entity_disjoint`.
        """
        if ent.identity not in renames:
            return False
        base_id = renames[ent.identity]
        base_e = base_by_id.get(base_id)
        if base_e is None:
            return False
        # Forward ``lang`` so one-liner headers are stripped correctly: a Rust
        # one-liner ``fn foo() -> i32 { BODY }`` has no ':', so the default
        # ``lang=None`` (Python colon-branch) leaves the header intact and the
        # renamed-vs-base comparison always differs — the rename is never
        # recognized, forcing an unnecessary LLM escalation. Multi-line bodies
        # are unaffected (they take the lang-independent first-line-drop path).
        return _body_below_header(ent.body, lang) == _body_below_header(base_e.body, lang)

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

    TRAILER PRESERVATION: content between the last entity and the container's
    close brace (a trailing comment, attribute, blank-separated note) that was
    present in the enclosing text is preserved — it sat in all three sides and
    dropping it is silent data loss. The trailer is the run of lines after the
    last entity's content up to and including the closing brace.
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
    # A flat module of top-level fields (const/static/let/type/var) is NOT a
    # container with framing — there's no class/impl header to splice around.
    # Treating the first field as a "header" and re-emitting it would duplicate
    # the field definition. Emit flat like the bare-function case.
    if _first_real_keyword(enc_lines[0]) in _FIELD_DECL_KEYWORDS:
        return "\n\n".join(entity_bodies) if entity_bodies else ""
    # The body indent is the leading whitespace of the first body line (the
    # convention under which the container's entities nest). tree-sitter's entity
    # body slice EXCLUDES this leading indent on the def/header line but KEEPS
    # the internal indentation, so we prepend the indent to the FIRST line of
    # each body only — internal lines already carry correct relative indentation.
    body_indent = ""
    for line in enc_lines[1:]:
        if line.strip():
            body_indent = line[: len(line) - len(line.lstrip(" \t"))]
            break
    # The header is line 0. The trailer is the container's OWN closing brace (for
    # brace languages) PLUS any non-entity content between the last entity and
    # that brace (a trailing comment / attribute present in all sides). We can't
    # take method bodies' own closing braces (``    }``) — only the trailing run
    # after the last entity. Locate where the last entity ends in the enclosing
    # text and take everything from there to the container's close.
    header = enc_lines[0]
    trailer_lines = _container_trailer(enc_lines, entity_bodies, language)
    out = [header]
    for body in entity_bodies:
        blines = body.split("\n")
        if blines:
            # Prepend the container's body indent to the def/header line only.
            blines[0] = body_indent + blines[0] if blines[0].strip() else blines[0]
        out.append("\n".join(blines))
    out.extend(trailer_lines)
    return "\n".join(out)


def _container_trailer(
    enc_lines: list[str], entity_bodies: list[str], language: str | None
) -> list[str]:
    """The trailing lines of a container after its last entity, up to the close.

    For brace languages this is the run of lines from just after the last
    entity's content through the container's closing ``}``/``};`` — preserving
    any trailing comment / attribute that sat between the last entity and the
    close (present in all three sides; dropping it was silent data loss). For
    Python (no closing brace) there is no trailer.

    Falls back to the prior single-line behavior (just the ``}``/``};`` line)
    when the last entity's end can't be located, so behavior is unchanged for
    shapes the locator can't handle.
    """
    if language == "python" or not enc_lines:
        return []
    last = enc_lines[-1]
    if last.strip() not in ("}", "};"):
        return []  # not a brace-closed container
    # Locate the last entity's LAST line within the enclosing text. Entity bodies
    # are dedented; in the enclosing text they appear indented by body_indent.
    # Match the last non-blank line of the last entity body (stripped) against
    # the enclosing lines to find where the entity block ends.
    if not entity_bodies:
        return [last]
    # The enclosing text is the BASE version; the merged entity_bodies include
    # additions that aren't in it. Find the LAST entity whose last line IS
    # present in the enclosing text (the last BASE entity) and take the trailer
    # from just after it. Build a set of needles (last stripped line of each body)
    # and scan the enclosing lines (excluding the closing brace) from the end.
    needles = {
        next((ln for ln in reversed(body.split("\n")) if ln.strip()), "").strip()
        for body in entity_bodies
    }
    needles.discard("")
    end_idx = -1
    for idx in range(len(enc_lines) - 2, 0, -1):  # skip the closing brace line
        if enc_lines[idx].strip() in needles:
            end_idx = idx
            break
    if end_idx < 0:
        # The merged last entity's last line isn't in the base enclosing text —
        # the last base entity was renamed/replaced. Falling back to just ``[last]``
        # would silently drop any trailing comment/attribute between the last
        # entity and the close brace. Recover the trailer by scanning backwards
        # from the close brace through trailing comment/blank lines: a comment
        # line (per the language's comment markers) or a blank line is trailer;
        # the first non-comment, non-blank line is the last entity's close.
        from capybase.adapters.language import adapter_for
        try:
            prefixes = adapter_for(language).comment_line_prefixes
        except Exception:
            prefixes = ("//",)
        trailer_start = len(enc_lines) - 1  # the close brace line
        for idx in range(len(enc_lines) - 2, 0, -1):  # skip header (idx 0)
            stripped_ln = enc_lines[idx].strip()
            if not stripped_ln:
                trailer_start = idx
                continue
            if any(stripped_ln.startswith(p) for p in prefixes):
                trailer_start = idx
                continue
            break  # hit the last entity's content; stop
        return enc_lines[trailer_start:]
    # Trailer = everything after the last base entity's last line, through close.
    return enc_lines[end_idx + 1:]

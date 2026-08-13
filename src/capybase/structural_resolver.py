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
    broadened_base: bool = False

    @property
    def resolved(self) -> bool:
        return self.text is not None


def intent_coverage_score(
    candidate: str, base: str, current: str, replayed: str,
) -> float:
    """Fraction of side-specific lines preserved in the candidate.

    Computes the ratio of lines added by each side (relative to base) that
    survive in the candidate's resolved text. Returns the minimum of the two
    sides' coverage ratios — the worst-case side preservation. A score of 1.0
    means all side-specific additions are present; 0.0 means none survived.

    Used to rank LLM candidates: among candidates that pass validation, prefer
    the one that preserves more of both sides' intent. This directly targets
    the sim gap where the model drops lines the oracle kept.
    """
    base_set = {l.strip() for l in base.split("\n") if l.strip()}
    cur_added = {l.strip() for l in current.split("\n") if l.strip()} - base_set
    rep_added = {l.strip() for l in replayed.split("\n") if l.strip()} - base_set
    cand_set = {l.strip() for l in candidate.split("\n") if l.strip()}
    cur_cov = len(cur_added & cand_set) / len(cur_added) if cur_added else 1.0
    rep_cov = len(rep_added & cand_set) / len(rep_added) if rep_added else 1.0
    return min(cur_cov, rep_cov)


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


#: C++ alternative tokens (ISO C++ §16.4) that are syntactically equivalent to
#: their symbolic counterparts. A side whose ONLY changes are these token
#: substitutions + whitespace/formatting is a mechanical lint pass, not a
#: semantic change. Normalizing them collapses a "purely lint" side back to the
#: base text — exposing the lint-vs-refactor conflict shape.
_CPP_ALT_TOKEN_MAP: dict[str, str] = {
    "and": "&&", "or": "||", "not": "!", "not_eq": "!=",
    "bitand": "&", "bitor": "|", "xor": "^", "compl": "~",
    "and_eq": "&=", "or_eq": "|=", "xor_eq": "^=",
}
_CPP_ALT_TOKEN_RE = __import__("re").compile(
    r"\b(" + "|".join(_CPP_ALT_TOKEN_MAP) + r")\b"
)
_LINT_TOKEN_RE = __import__("re").compile(r"\w+|[^\w\s]")


def _normalize_cpp_lint(text: str, lang: str | None = None) -> str:
    """Normalize C++ lint/formatting to detect purely cosmetic changes.

    Replaces C++ alternative tokens (``and``→``&&``, ``or``→``||``, etc.) and
    tokenizes to ignore ALL whitespace/punctuation-spacing differences. Two
    texts that differ ONLY in alternative tokens and formatting produce the
    same normalized form.

    For non-C/C++ languages, only tokenization is applied (the alternative-token
    replacement is C++-specific — Python's ``and``/``or`` are keywords, not
    operator aliases).
    """
    if not text:
        return ""
    if lang and lang.lower() in ("cpp", "c++", "c", "h", "hpp", "cc", "cxx", "hxx"):
        text = _CPP_ALT_TOKEN_RE.sub(
            lambda m: _CPP_ALT_TOKEN_MAP[m.group(1)], text)
    return " ".join(_LINT_TOKEN_RE.findall(text))


def _try_lint_vs_refactor(
    base: str, current: str, replayed: str, *, lang: str | None = None,
) -> str | None:
    """Resolve a lint-vs-refactor conflict by taking the semantic side verbatim.

    When one side's changes are PURELY mechanical lint (C++ alternative tokens,
    whitespace, template spacing) and the other side makes a real semantic
    change, the lint side carries no unique intent — the correct merge takes
    the semantic (refactor) side verbatim, preventing the Frankenstein merges
    the LLM produces when it tries to combine old-API code with new-API code.

    Detection: normalize both sides + base via ``_normalize_cpp_lint``. If one
    side's normalized tokens are a contiguous subsequence of the base's (or
    equal when both are hunk-sized), that side is purely lint. Take the other.

    The subsequence check handles the common case where ``base`` is the full
    file (481 lines) while ``current``/``replayed`` are tiny conflict hunks
    (2–7 lines) — diff3 refinement isn't always stored, so the resolver passes
    the full-file base. Equality would never match; token-aligned containment
    finds the base region that the lint side corresponds to.

    Declines (returns ``None``) when:
    - Both sides are lint (no real conflict — other rules handle).
    - Neither is lint (both semantic — genuine conflict for the LLM).
    """
    base_n = _normalize_cpp_lint(base, lang)
    if not base_n:
        return None  # no base to compare against
    cur_n = _normalize_cpp_lint(current, lang)
    rep_n = _normalize_cpp_lint(replayed, lang)

    def _lint_match(side_n: str) -> bool:
        """True if side_n's normalized tokens are in base_n (equality or
        token-aligned contiguous subsequence)."""
        if not side_n:
            return False
        if side_n == base_n:
            return True
        # Token-aligned containment: wrap in spaces so partial-token matches
        # ('a' in 'aa') are rejected. ' a ' in ' aa ' → False; ' b ' in
        # ' aa b ' → True.
        return f" {side_n} " in f" {base_n} "

    # A side is "lint" only if it CHANGED from base (standard whitespace
    # normalization) AND the change is purely cosmetic (lint normalization).
    # A side identical to base is NOT lint — it's just unchanged, and
    # one_sided_change handles it.
    cur_changed = _normalize(current) != _normalize(base)
    rep_changed = _normalize(replayed) != _normalize(base)
    cur_is_lint = cur_changed and _lint_match(cur_n)
    rep_is_lint = rep_changed and _lint_match(rep_n)
    if cur_is_lint and not rep_is_lint:
        return replayed  # current is purely lint → take the refactor
    if rep_is_lint and not cur_is_lint:
        return current   # replayed is purely lint → take current's changes
    return None


def _classify_conflict_shape(unit: ConflictUnit) -> str:
    """Classify the conflict shape from cached features for rule routing.

    Returns one of: ``rewrite_vs_edit``, ``pure_insertion``,
    ``stable_token_edit``, or ``general``. The classification is advisory —
    it only makes the cascade MORE conservative by skipping rules that are
    unsafe for certain shapes (e.g. token_disjoint on rewrite_vs_edit).

    Reads ``conflict_features`` and ``merge_direction`` from
    ``structural_metadata`` — both populated at extraction time.
    """
    feats = unit.structural_metadata.get("conflict_features") or {}
    md = unit.structural_metadata.get("merge_direction") or {}
    imbalance = feats.get("imbalance_ratio", 1.0)
    merge_kind = md.get("kind")
    same_line = feats.get("same_line_overlap", False)
    # rewrite_vs_edit: one side rewrote, the other made a small edit.
    # token_disjoint garbles these because the token splice crosses
    # the rewrite boundary (clickhouse-0024).
    if imbalance > 3.0 and merge_kind == "both_modify":
        return "rewrite_vs_edit"
    # pure_insertion: both sides (or one) added content without modifying base.
    if merge_kind in ("both_add", "one_unchanged") and not same_line:
        return "pure_insertion"
    # stable_token_edit: small balanced conflict where token-level rules
    # are designed to work.
    if same_line and imbalance <= 2.0:
        return "stable_token_edit"
    return "general"


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

    # When the parent conflict has large side-size asymmetry (one side rewrote
    # while the other made a small edit), the union/additive rules can produce
    # Frankenstein merges by keeping both sides' content. Skip them so the LLM
    # gets a chance. (Catches the nlohmann-0020 pattern where entity splitting
    # made each sub-unit look like pure insertion while the parent had 102
    # deleted lines on one side.)
    _skip_union_rules = unit.structural_metadata.get("parent_has_asymmetry", False)

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

    # Rule 2.5: lint-vs-refactor. One side's changes are PURELY mechanical
    # lint (C++ alternative tokens, whitespace, template spacing) and the
    # other side makes a real semantic change. The lint carries no unique
    # intent — take the semantic (refactor) side verbatim. This prevents the
    # Frankenstein merges the LLM produces when it tries to combine old-API
    # code with new-API code (e.g., mixing cursor-based and iterator-based
    # input adapters). Must fire BEFORE one_sided_change (Rule 3): when the
    # refined base is empty (entity-split sub-unit) and the refactor side is
    # also empty (deletion), Rule 3 would take the lint side — the wrong
    # choice. The subsequence check against the full-file base catches this.
    lang = getattr(unit, "language", None)
    # When the refined base is empty (entity-split sub-unit where the entity
    # didn't exist in base), fall back progressively: first unit.base.text
    # (full-file base for non-split units), then original_worktree_text (the
    # full file WITH conflict markers — the last resort for entity-split
    # sub-units where unit.base.text is also empty). The subsequence check
    # still works: the lint side's "addition" is old code that exists in the
    # file, and the markers add only a few noise tokens.
    lint_base = (
        base if base.strip()
        else (unit.base.text or "")
    )
    if not lint_base.strip():
        lint_base = getattr(unit, "original_worktree_text", "") or ""
    lint_res = _try_lint_vs_refactor(lint_base, current, replayed, lang=lang)
    if lint_res is not None:
        return StructuralResolution(rule="lint_vs_refactor", text=lint_res)

    # Rule 3: one-sided change. Only one side diverged from base → take it.
    cur_changed = _normalize(current) != _normalize(base)
    rep_changed = _normalize(replayed) != _normalize(base)
    if cur_changed and not rep_changed:
        # Current diverged, replayed conceded to base → but current may have
        # legitimately built on base; emit current.
        return StructuralResolution(rule="one_sided_change", text=current)
    if rep_changed and not cur_changed:
        return StructuralResolution(rule="one_sided_change", text=replayed)

    # Rule 3.6: move detection. If one side "moved" a block (deleted it from
    # one location and re-added it verbatim elsewhere), and the other side
    # modified the original, transplant the modifications to the moved copy.
    # This resolves the common "refactor moved code + bugfix in original
    # location" conflict shape without the LLM.
    if cur_changed and rep_changed:
        moved = _try_move_transplant(base, current, replayed)
        if moved is not None:
            return StructuralResolution(rule="move_transplant", text=moved)

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
        #
        # Shape router: skip token_disjoint on rewrite_vs_edit shapes — the
        # token splice crosses the rewrite boundary, producing garbled output
        # (clickhouse-0024). The LLM gets this shape instead.
        _shape = _classify_conflict_shape(unit)
        if _shape != "rewrite_vs_edit":
            merged = _try_token_disjoint(base, current, replayed)
            if merged is not None:
                return StructuralResolution(rule="token_disjoint", text=merged)

        # Mechanical re-application: when token_disjoint DECLINED because the
        # spans overlap, but one side's changes are purely small mechanical
        # substitutions (API rename, operator keyword lint) and the other side
        # is a wholesale rewrite, take the rewriter's text and re-apply the
        # mechanical substitutions where the base-token anchors survive.
        # Also gated by the shape router — mechanical_reapply is unsafe on
        # rewrite_vs_edit (the substitution anchors may not survive the rewrite).
        if _shape != "rewrite_vs_edit":
            merged = _try_mechanical_reapply_merge(base, current, replayed)
            if merged is not None:
                return StructuralResolution(rule="mechanical_reapply_merge", text=merged)

        # File-level lint transform: when the FILE-level analysis detected a
        # lint pass (e.g. 17 and→&& changes across 6 regions), apply the
        # transforms to whichever side is NOT the lint side. This fires even
        # when no single unit has enough changes to meet the per-unit frequency
        # threshold (5). Unlike token_disjoint/mechanical_reapply, lint_transform
        # is SAFE on rewrite_vs_edit — it takes the refactor side as-is and
        # applies known-safe word-boundary substitutions; it never crosses the
        # rewrite boundary.
        file_transforms = unit.structural_metadata.get("file_level_lint_transforms")
        if file_transforms:
            linted = _try_file_level_lint(base, current, replayed, file_transforms)
            if linted is not None:
                return StructuralResolution(rule="lint_transform", text=linted)

        # Lint transform (per-unit): when mechanical_reapply declined because
        # base anchors didn't survive the refactor, try applying known-safe lint
        # substitutions (NULL→nullptr, and→&&, etc.) directly to the refactor
        # side's text. NOT gated by the shape router — unlike token_disjoint
        # and mechanical_reapply, lint_transform does NOT cross the rewrite
        # boundary; it takes the refactor text as-is and applies word-boundary
        # substitutions. It is exactly the rule designed for rewrite_vs_edit.
        linted = _try_lint_transform(base, current, replayed)
        if linted is not None:
            return StructuralResolution(rule="lint_transform", text=linted)

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
        # Skip union rules when the parent conflict has large side-size asymmetry
        # (one side rewrote while the other made a small edit) — the union would
        # keep both sides' content, producing a Frankenstein merge.
        if not _skip_union_rules:
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

    # Rule N: generalized mini-conflict extraction. If all rules above declined,
    # try to shrink the conflict to its ambiguous core by resolving provably
    # deterministic regions (identical lines, one-sided changes, agreed
    # deletions). If the entire conflict is deterministic, accept it without
    # the LLM. If a core remains, emit a deferred_core for the LLM.
    mini = _try_generalized_mini_conflict(base, current, replayed)
    if mini is not None:
        return mini

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


def _try_move_transplant(base: str, current: str, replayed: str) -> str | None:
    """Detect when one side moved a code block (the base content appears at a
    DIFFERENT position in the side's text) and the other side modified it.

    Git's diff3 sees a move as content at a shifted position — the diff matcher
    aligns the moved lines as "equal" (not "delete"). This rule detects the
    positional shift directly: a contiguous block of >10 base lines that appears
    in the side's text at a completely different position (the mover added
    significant new content BEFORE the block, shifting it).

    Conservative: requires >10 lines to avoid coincidental matches, AND the
    mover must have added >5 lines before the block (a real structural shift,
    not a 1-2 line prepend). Only fires when the other side has NO similar
    shift (the other side modified in-place, not moved).

    Returns the merged text, or None to defer.
    """
    base_lines = (base or "").splitlines()
    if len(base_lines) < 12:
        return None

    for mover_text, other_text in ((current, replayed), (replayed, current)):
        mover_lines = (mover_text or "").splitlines()
        if len(mover_lines) < len(base_lines) + 6:
            continue  # mover must have added significant content
        # Look for a 10-line block from base appearing shifted in the mover.
        # The block must NOT appear at the same position (offset 0 from base).
        block = base_lines[:10]
        for i in range(6, len(mover_lines) - 9):  # start search at offset 6+
            if mover_lines[i:i + 10] == block:
                # Found a moved block — verify the other side did NOT move it.
                other_lines = (other_text or "").splitlines()
                other_moved = False
                for j in range(6, len(other_lines) - 9):
                    if other_lines[j:j + 10] == block:
                        other_moved = True
                        break
                if not other_moved:
                    return mover_text
    return None


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


# Token equivalence classes: semantically identical tokens mapped to a canonical
# form for COMPARISON ONLY (overlap detection, disjointness checking). The
# original tokens are always preserved in the splice output. This lets the
# resolver recognize that NULL→nullptr or and→&& are the same change, resolving
# via identical_sides instead of escalating to the LLM.
_TOKEN_EQUIV: dict[str, str] = {
    # Null pointers
    "NULL": "nullptr",
    # Boolean literals (C macros)
    "TRUE": "true", "FALSE": "false",
    # C++ operator keyword alternatives
    "and": "&&", "or": "||", "not": "!",
    "bitand": "&", "bitor": "|", "xor": "^", "compl": "~",
    "and_eq": "&=", "or_eq": "|=", "xor_eq": "^=", "not_eq": "!=",
    # std:: prefix normalization (for comparison only)
    "std::size_t": "size_t", "std::nullptr_t": "nullptr_t",
    "std::move": "move", "std::forward": "forward",
}


def _norm_tok(tok: str) -> str:
    """Return the canonical form of a token for comparison purposes."""
    return _TOKEN_EQUIV.get(tok, tok)


def _norm_tokens(toks: list[str]) -> list[str]:
    """Normalize a token list for comparison (preserves length)."""
    return [_norm_tok(t) for t in toks]


# Macro-atomic tokenization: detect ALL-CAPS macro invocations and treat each
# as a single atomic token so token_disjoint can't interleave inside it.
_MACRO_NAME_RE = __import__("re").compile(r"^([A-Z_][A-Z_0-9]*)\s*$")


def _tokenize_with_macros(text: str) -> tuple[list[str], dict[str, list[str]]]:
    """Tokenize text, replacing ALL-CAPS macro invocations with atomic tokens.

    Returns ``(tokens, macro_lookup)`` where macro_lookup maps each
    ``__MACRO_N`` placeholder to the original token sequence. After the
    token-level diff/merge, call ``_detokenize_with_macros`` to restore
    the original macro text.

    A macro invocation is an ALL-CAPS identifier immediately followed by ``(``,
    extending to the matching ``)`` with balanced paren tracking. This prevents
    token_disjoint from splicing tokens inside macro arguments (a common source
    of garbled output in C++ codebases that use macros, like ClickHouse).
    """
    toks = _tokenize(text)
    out: list[str] = []
    lookup: dict[str, list[str]] = {}
    macro_count = 0
    i = 0
    while i < len(toks):
        tok = toks[i]
        # Check if this is an ALL-CAPS identifier followed by '('
        if (
            _MACRO_NAME_RE.match(tok)
            and i + 1 < len(toks)
            and toks[i + 1].lstrip().startswith("(")
        ):
            # Find the matching ')' via paren-depth tracking
            depth = 0
            j = i + 1  # start at '('
            macro_toks: list[str] = []
            found_close = False
            while j < len(toks):
                macro_toks.append(toks[j])
                stripped = toks[j].lstrip()
                # Count parens (they may share tokens with whitespace)
                for ch in stripped:
                    if ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                        if depth == 0:
                            found_close = True
                            break
                if found_close:
                    break
                j += 1
            if found_close and depth == 0:
                # Replace the macro invocation with an atomic placeholder
                placeholder = f"__MACRO_{macro_count}"
                # The placeholder includes the macro name token + the rest
                full_macro = [tok] + macro_toks
                lookup[placeholder] = full_macro
                out.append(placeholder)
                macro_count += 1
                i = j + 1
                continue
        out.append(tok)
        i += 1
    return out, lookup


def _detokenize_with_macros(tokens: list[str], lookup: dict[str, list[str]]) -> str:
    """Restore macro placeholders to their original token sequences."""
    out: list[str] = []
    for tok in tokens:
        if tok in lookup:
            out.extend(lookup[tok])
        else:
            out.append(tok)
    return "".join(out)


def _detokenize(tokens: list[str]) -> str:
    """Rejoin tokens into the original text (inverse of :func:`_tokenize`)."""
    return "".join(tokens)


# NOTE: _try_broaden_base was removed — base broadening for token rules is
# fundamentally unsafe. token_disjoint reconstructs its output by walking bt
# (the base tokens) and applying replacements; if bt is the full enclosing
# function, the output is the full function text, not the conflict hunk —
# corrupting the splice. mechanical_reapply_merge diffs the base against the
# sides; a full-function base vs a hunk-sized side produces massive diffs that
# fail the mechanical-side guard. The enclosing_node_text remains available on
# structural_metadata for other uses (prompt context, entity rules).


def _token_change_ops(base_toks: list[str], other_toks: list[str]) -> list[tuple[int, int, list[str]]]:
    """Non-equal regions between two token sequences, as ``(base_start, base_end_excl, replacement_toks)``.

    Mirrors :func:`_base_changed_lines` but returns the replacement content too,
    so a disjoint merge can splice each side's replacement into base in one pass.

    Uses token equivalence normalization for the diff: ``NULL`` vs ``nullptr``
    or ``and`` vs ``&&`` are treated as equal (no change). The original
    (unnormalized) tokens are returned in the replacement, preserving the
    actual text for the splice.
    """
    ops: list[tuple[int, int, list[str]]] = []
    # Normalize for comparison; the replacement uses original tokens.
    nb = _norm_tokens(base_toks)
    no = _norm_tokens(other_toks)
    matcher = line_matcher(nb, no)
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

    # Macro-atomic tokenization: replace ALL-CAPS macro invocations with
    # single atomic tokens so the diff can't interleave inside them.
    bt, _macro_lookup = _tokenize_with_macros(base)
    ct = _tokenize_with_macros(current)[0]
    rt = _tokenize_with_macros(replayed)[0]
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
    result_text = _detokenize_with_macros(out, _macro_lookup)
    # Line-expansion guard: token_disjoint edits tokens WITHIN existing lines.
    # When one side expanded the base into many more lines (a rewrite, not a
    # token-level edit), the token splice pulls tokens from different lines of
    # the expanding side into a new multi-line structure — producing garbled
    # hybrid lines that are individually plausible but collectively wrong. This
    # is NOT the disjoint-token scenario the rule was designed for. Decline when
    # one side's line count is more than 2x the base's (a rewrite) while the
    # other side is close to the base (a token edit). (Catches the clickhouse-
    # 0024 defect where a 1-line base was expanded by current into 4 lines, and
    # the token splice mixed those 4 lines with replayed's token edit.)
    _base_nb = sum(1 for l in base.split("\n") if l.strip())
    _cur_nb = sum(1 for l in current.split("\n") if l.strip())
    _rep_nb = sum(1 for l in replayed.split("\n") if l.strip())
    if _base_nb > 0:
        _cur_expansion = _cur_nb / _base_nb
        _rep_expansion = _rep_nb / _base_nb
        # If one side expanded >>2x while the other is ~1x, it's a rewrite vs
        # token-edit — not a safe disjoint-token merge.
        if (
            (_cur_expansion > 2.0 and _rep_expansion <= 1.5)
            or (_rep_expansion > 2.0 and _cur_expansion <= 1.5)
        ):
            return None  # one side rewrote — not safe for token_disjoint
    return result_text


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
    # Macro-atomic tokenization: replace ALL-CAPS macro invocations with
    # single atomic tokens so substitution anchors can't match inside macros.
    bt, _macro_lookup_mr = _tokenize_with_macros(base)
    ct = _tokenize_with_macros(current)[0]
    rt = _tokenize_with_macros(replayed)[0]
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

    # Filter mechanical ops to SAFE substitutions only:
    # - Skip pure insertions (i1==i2): no base anchor to re-apply onto the
    #   semantic text; the insertion's content can't be placed reliably.
    # - Skip ambiguous anchors (appear >1× in the semantic text): can't tell
    #   which occurrence the mechanical op targeted.
    # - Keep unambiguous substitutions (anchor appears exactly 1×).
    # Previously the rule DECLINED entirely if ANY op was an insertion or
    # ambiguous. Now it applies the safe ops and skips the rest — partial
    # application. This unlocks refactor+bugfix merges where the bugfix has
    # a mix of unambiguous renames and ambiguous/insertion changes (e.g.,
    # clickhouse-0024: an API rename that IS unambiguous + a type-cast
    # wrapping that is an insertion). The unambiguous rename gets applied;
    # the cast is skipped (the code compiles without it).
    sem_toks = _tokenize(sem_text)
    safe_ops: list[tuple[int, int, list[str]]] = []
    for i1_m, i2_m, repl_m in mech_ops:
        if i1_m == i2_m:
            continue  # pure insertion — skip
        anchor_m = bt[i1_m:i2_m]
        if not anchor_m:
            continue
        occurrences = _count_subsequence(sem_toks, anchor_m)
        if occurrences == 1:
            safe_ops.append((i1_m, i2_m, repl_m))
        # occurrences == 0: anchor removed by the rewrite — skip
        # occurrences > 1: ambiguous — skip
    if not safe_ops:
        return None  # no unambiguous substitutions to apply → defer

    # Build the semantic side's token sequence. We'll apply mechanical subs
    # onto it. The semantic side may have completely different tokens, so we
    # search for the mechanical op's BASE anchor tokens within the semantic
    # text's token stream.
    applied = list(sem_toks)  # mutable copy
    applied_count = 0
    for i1, i2, repl in safe_ops:
        anchor = bt[i1:i2]
        if not anchor:
            continue
        idx = _find_subsequence(applied, anchor)
        if idx < 0:
            continue  # anchor not found — the rewrite removed it; skip
        # Re-check ambiguity in the CURRENT state (a prior op may have
        # changed the token landscape).
        next_idx = _find_subsequence(applied[idx + len(anchor):], anchor)
        if next_idx >= 0:
            continue  # ambiguous — skip
        applied[idx:idx + len(anchor)] = repl
        applied_count += 1

    if applied_count == 0:
        return None  # nothing was applicable → no improvement over semantic side

    return _detokenize_with_macros(applied, _macro_lookup_mr)


# Directional lint substitutions: apply the mechanical side's lint transforms
# directly to the refactor side's text. Unlike _TOKEN_EQUIV (which is for
# COMPARISON only), this is for APPLICATION — it rewrites old → new. Used when
# mechanical_reapply_merge declines because base anchors didn't survive the
# refactor (the refactor restructured or removed the base text).
_LINT_TRANSFORMS: list[tuple[str, str]] = [
    ("NULL", "nullptr"),
    ("TRUE", "true"),
    ("FALSE", "false"),
    ("and", "&&"),
    ("or", "||"),
    ("not", "!"),
    ("bitand", "&"),
    ("bitor", "|"),
    ("xor", "^"),
    ("compl", "~"),
]


def _apply_lint_transforms(
    text: str, transforms: list[tuple[str, str]],
) -> str:
    """Apply directional lint substitutions with word-boundary matching.

    Each (old, new) pair replaces whole-word occurrences of ``old`` with
    ``new``. Word boundaries prevent partial matches (``Anderson`` won't
    match ``and``). Only fires when ``old`` is not already in ``new`` form.
    """
    import re as _re_lt
    result = text
    for old, new in transforms:
        if old == new:
            continue
        # Word-boundary replacement: \b ensures we don't match inside identifiers
        result = _re_lt.sub(r"\b" + _re_lt.escape(old) + r"\b", new, result)
    return result


def _try_lint_transform(base: str, current: str, replayed: str) -> str | None:
    """When one side is a refactor and the other is a mechanical lint pass,
    apply the lint transforms directly to the refactor side's text.

    Unlike ``_try_mechanical_reapply_merge``, this rule does NOT require base
    anchors to survive — it applies known-safe lint substitutions (NULL→nullptr,
    and→&&, etc.) directly to the refactor text using word-boundary matching.

    Two detection paths:
    1. Mechanical-side classification (via _is_mechanical_side) — conservative,
       works for small conflicts.
    2. Frequency-based detection — scans both sides' diffs for repeated known
       lint substitutions. If the same transform appears >5 times in one side,
       it's a lint pass regardless of total op count. This handles large
       refactor-vs-lint conflicts (e.g. nlohmann-0020) where the conservative
       _is_mechanical_side guards reject the lint side because the total
       number of ops exceeds their thresholds.

    Returns the transformed semantic text, or None to defer.
    """
    bt = _tokenize_with_macros(base)[0]
    ct = _tokenize_with_macros(current)[0]
    rt = _tokenize_with_macros(replayed)[0]
    cur_ops = _token_change_ops(bt, ct)
    rep_ops = _token_change_ops(bt, rt)
    # Note: don't bail when one side has no NORMALIZED ops — token equivalence
    # normalization (and→&&, NULL→nullptr) can make a side's changes invisible.
    # The frequency-based path (below) uses raw un-normalized ops which will
    # detect these. Only bail when BOTH sides are truly unchanged.
    if not cur_ops and not rep_ops:
        return None

    # Path 1: mechanical-side classification (original path)
    cur_mech = _is_mechanical_side(cur_ops, len(bt), base, current)
    rep_mech = _is_mechanical_side(rep_ops, len(bt), base, replayed)
    if cur_mech != rep_mech:
        if cur_mech:
            mech_text, sem_text = current, replayed
            mech_ops = cur_ops
        else:
            mech_text, sem_text = replayed, current
            mech_ops = rep_ops
        detected_transforms = _detect_lint_transforms_from_ops(mech_ops)
        if detected_transforms:
            result = _apply_lint_transforms(sem_text, detected_transforms)
            if result != sem_text:
                return result

    # Path 2: frequency-based detection — handles large conflicts where
    # _is_mechanical_side rejects both sides (too many ops).
    # Use a RAW (un-normalized) diff here, because _token_change_ops normalizes
    # tokens via _norm_tokens which maps and→&& — making lint transforms invisible.
    from capybase.diff import line_matcher as _lm_lt
    def _raw_ops(base_toks, other_toks):
        ops_r = []
        m = _lm_lt(base_toks, other_toks)
        for tag, i1, i2, j1, j2 in m.get_opcodes():
            if tag != "equal":
                ops_r.append((i1, i2, other_toks[j1:j2]))
        return ops_r
    cur_raw_ops = _raw_ops(bt, ct)
    rep_raw_ops = _raw_ops(bt, rt)
    cur_transforms = _detect_lint_transforms_from_ops(cur_raw_ops)
    rep_transforms = _detect_lint_transforms_from_ops(rep_raw_ops)
    # Count how many times each transform appears
    from collections import Counter
    cur_counts = Counter(cur_transforms)
    rep_counts = Counter(rep_transforms)
    # If one side has a lint transform appearing >5 times, it's the lint side
    cur_dominant = sum(1 for t, c in cur_counts.items() if c >= 5)
    rep_dominant = sum(1 for t, c in rep_counts.items() if c >= 5)
    if cur_dominant > 0 and rep_dominant == 0:
        # Current is the lint side, replayed is the refactor
        detected = list(set(cur_transforms))
        result = _apply_lint_transforms(replayed, detected)
        if result != replayed:
            return result
    elif rep_dominant > 0 and cur_dominant == 0:
        # Replayed is the lint side, current is the refactor
        detected = list(set(rep_transforms))
        result = _apply_lint_transforms(current, detected)
        if result != current:
            return result

    return None


def _try_file_level_lint(
    base: str, current: str, replayed: str,
    file_transforms: list[tuple[str, str]],
) -> str | None:
    """Apply file-level lint transforms to whichever side is the refactor.

    Called when the FILE-level analysis detected a lint pass (e.g. 17 and→&&
    changes across 6 regions). Each individual unit may have too few changes
    to meet the per-unit frequency threshold, but the aggregate count confirms
    a file-wide lint pass.

    Determines which side is the lint side by counting how many of the
    file-level transforms appear in each side's raw diff vs base. The side
    with more transform applications is the lint side; the other is the
    refactor side (apply transforms to it).
    """
    bt = _tokenize_with_macros(base)[0]
    ct = _tokenize_with_macros(current)[0]
    rt = _tokenize_with_macros(replayed)[0]
    from capybase.diff import line_matcher as _lm_fl
    def _raw_ops(base_toks, other_toks):
        ops_r = []
        m = _lm_fl(base_toks, other_toks)
        for tag, i1, i2, j1, j2 in m.get_opcodes():
            if tag != "equal":
                ops_r.append((i1, i2, other_toks[j1:j2]))
        return ops_r
    cur_raw_ops = _raw_ops(bt, ct)
    rep_raw_ops = _raw_ops(bt, rt)
    cur_transforms = _detect_lint_transforms_from_ops(cur_raw_ops)
    rep_transforms = _detect_lint_transforms_from_ops(rep_raw_ops)
    cur_set = set(t for t in cur_transforms if t in file_transforms)
    rep_set = set(t for t in rep_transforms if t in file_transforms)
    # The lint side has MORE of the file-level transforms applied.
    # The refactor side has FEWER (it may have picked up some incidentally).
    if len(cur_set) > len(rep_set):
        # Current is the lint side, replayed is the refactor.
        result = _apply_lint_transforms(replayed, file_transforms)
        if result != replayed:
            return result
    elif len(rep_set) > len(cur_set):
        # Replayed is the lint side, current is the refactor.
        result = _apply_lint_transforms(current, file_transforms)
        if result != current:
            return result
    elif cur_set == rep_set and cur_set:
        # Both sides applied the same transforms (or same count). Fall back:
        # try applying to whichever side has FEWER total raw changes (the
        # "simpler" side is more likely the refactor — it changed less).
        if len(cur_raw_ops) <= len(rep_raw_ops):
            result = _apply_lint_transforms(current, file_transforms)
            if result != current:
                return result
        else:
            result = _apply_lint_transforms(replayed, file_transforms)
            if result != replayed:
                return result
    return None


def detect_file_level_lint_transforms(units) -> list[tuple[str, str]]:
    """Scan ALL units for repeated lint substitutions across the file.

    Aggregates transform frequency across every unit's diff. If a known-safe
    lint transform appears ≥5 times total across the file, it's promoted to a
    file-level transform applied to every unit's refactor side. This catches
    the pattern where each unit has only 2-3 and→&& changes (below the per-unit
    threshold) but the file has 17 total (clearly a lint pass).

    Returns a deduplicated list of (old, new) transform pairs.
    """
    from collections import Counter
    from capybase.diff import line_matcher as _lm_fld

    def _raw_ops(base_toks, other_toks):
        ops_r = []
        m = _lm_fld(base_toks, other_toks)
        for tag, i1, i2, j1, j2 in m.get_opcodes():
            if tag != "equal":
                ops_r.append((i1, i2, other_toks[j1:j2]))
        return ops_r

    transform_counts: Counter[tuple[str, str]] = Counter()
    for unit in units:
        refined = unit.refined_sides
        if refined is not None:
            current, base, replayed = refined
        else:
            current = unit.current.text or ""
            base = unit.base.text or ""
            replayed = unit.replayed.text or ""
        bt = _tokenize_with_macros(base)[0]
        ct = _tokenize_with_macros(current)[0]
        rt = _tokenize_with_macros(replayed)[0]
        cur_raw_ops = _raw_ops(bt, ct)
        rep_raw_ops = _raw_ops(bt, rt)
        for t in _detect_lint_transforms_from_ops(cur_raw_ops):
            transform_counts[t] += 1
        for t in _detect_lint_transforms_from_ops(rep_raw_ops):
            transform_counts[t] += 1
    # ≥5 total across the file → lint pass
    return [t for t, c in transform_counts.items() if c >= 5]


def _detect_lint_transforms_from_ops(
    ops: list[tuple[int, int, list[str]]],
) -> list[tuple[str, str]]:
    """Extract known lint transforms from token change ops.

    Returns a list of (old, new) pairs where the replacement matches a
    known lint transform. May contain duplicates (for frequency counting).
    """
    detected: list[tuple[str, str]] = []
    for _i1, _i2, repl in ops:
        new_text = "".join(repl).strip()
        for old, new in _LINT_TRANSFORMS:
            if new_text == new:
                detected.append((old, new))
    return detected


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


def _count_subsequence(haystack: list[str], needle: list[str]) -> int:
    """Count non-overlapping occurrences of ``needle`` in ``haystack``."""
    if not needle:
        return 0
    n, m = len(haystack), len(needle)
    if m > n:
        return 0
    count = 0
    i = 0
    while i <= n - m:
        if haystack[i:i + m] == needle:
            count += 1
            i += m
        else:
            i += 1
    return count


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
    #
    # Straddle guard: a replace opcode whose base span crosses a zone boundary
    # (e.g. base[0:2] when the zone is [0,1)) would be emitted into BOTH zones,
    # duplicating content. We detect this and signal the caller to decline.
    _straddled = [False]  # mutable closure flag

    def _zone_text(side_lines: list[str], z_start: int, z_end: int) -> str:
        """Reconstruct ``side_lines`` for base range [z_start, z_end).

        Sets the _straddled flag if a replace opcode's base span crosses the
        zone boundary — the caller must check this and decline.
        """
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
                    # Straddle check: the opcode's base span crosses a zone
                    # boundary. Splitting the replacement proportionally is
                    # unreliable (we don't know which side lines map to which
                    # base lines within a replace). Decline the whole merge.
                    if i1 < z_start or i2 > z_end:
                        _straddled[0] = True
                        return ""
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

    # Straddle guard: if any _zone_text call detected a replace opcode
    # crossing a zone boundary, decline — proportional splitting is
    # unreliable and would duplicate content. (Fixes a defect where a
    # one-sided edit spanning the pre/core boundary was emitted into both.)
    if _straddled[0]:
        return None

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


def _try_generalized_mini_conflict(
    base: str, current: str, replayed: str,
) -> StructuralResolution | None:
    """Generalized mini-conflict extraction: shrink a conflict to its ambiguous
    core by resolving provably deterministic regions (identical lines, one-sided
    additions, agreed deletions), then emit a deferred_core for the remaining
    ambiguous regions.

    Unlike ``_try_partial_disjoint_merge`` (which requires ≤5 overlap lines and
    a single contiguous core), this rule handles arbitrary conflict sizes and
    multiple disjoint overlap regions. It merges all ambiguous regions into a
    single deferred core, separated by deterministic tails as read-only context.

    Conservative by design — only resolves regions that are provably safe:
    - Lines identical in all sides (or both sides agree on the change)
    - Lines only one side changed (the other kept base verbatim)
    - Lines both sides deleted (agreed deletion)
    Everything else stays in the core for the LLM.

    Returns a StructuralResolution:
    - With ``text`` set and ``deferred_core=None``: fully deterministic (no
      LLM call needed — all regions were provably safe)
    - With ``text`` and ``deferred_core`` set: deterministic tails assembled,
      core deferred for LLM resolution
    - None: no shrinking was possible or side-intent would be lost
    """
    base_lines = (base or "").splitlines()
    cur_lines = (current or "").splitlines()
    rep_lines = (replayed or "").splitlines()
    if not base_lines:
        return None  # empty base → nothing to classify

    # Only fire on conflicts large enough that shrinking would help.
    # Small conflicts (<10 non-blank lines total) should go directly to
    # the LLM — the mini-conflict overhead isn't worth it.
    total_nb = sum(
        1 for t in (base, current, replayed)
        for ln in (t or "").split("\n") if ln.strip()
    )
    if total_nb < 10:
        return None

    # Classify each base line's fate in both sides via opcodes.
    cur_changed = _base_changed_lines(base_lines, cur_lines)
    rep_changed = _base_changed_lines(base_lines, rep_lines)
    cur_deleted = _base_deleted_lines(base_lines, cur_lines)
    rep_deleted = _base_deleted_lines(base_lines, rep_lines)

    # Walk base lines, classifying into deterministic (tail) vs ambiguous (core).
    # Build a list of segments: ("det", text) or ("amb", base_range, cur_range, rep_range)
    # For deterministic regions, resolve them immediately (pick the changed side).
    tail_parts: list[str] = []  # accumulated deterministic tail text
    core_parts: list[tuple[str, str, str]] = []  # (base, current, replayed) per core
    # Track side-specific additions that fall between base lines (inserts)
    # via a walk of both opcodes simultaneously.

    # Build per-base-line classification
    n = len(base_lines)
    # For each base line index, determine: is it ambiguous?
    ambiguous_indices: list[int] = []
    for i in range(n):
        in_cur_changed = i in cur_changed
        in_rep_changed = i in rep_changed
        if in_cur_changed and in_rep_changed:
            # Both sides changed this line → ambiguous
            ambiguous_indices.append(i)
        # else: deterministic (at most one side changed it)

    if not ambiguous_indices:
        # All lines are deterministic! Resolve them all.
        resolved_lines: list[str] = []
        # Use a simple line-by-line resolution: for each base line,
        # pick whichever side changed it (or base if neither).
        # Also handle inserts (one-sided additions between base lines).
        cur_matcher = line_matcher(base_lines, cur_lines)
        rep_matcher = line_matcher(base_lines, rep_lines)
        # Walk all three together using a base-aligned merge.
        # Simple approach: use _try_disjoint_merge on the whole thing.
        # If that fails, just pick the side with more content.
        merged = _try_disjoint_merge(base, current, replayed)
        if merged is not None:
            # Verify side-intent coverage
            cov = intent_coverage_score(merged, base, current, replayed)
            if cov >= 0.5:
                return StructuralResolution(rule="mini_conflict_deterministic", text=merged)
        # Fall through: try line-by-line resolution
        for i in range(n):
            cur_line = cur_lines[i] if i < len(cur_lines) else ""
            rep_line = rep_lines[i] if i < len(rep_lines) else ""
            base_line = base_lines[i]
            if _normalize(cur_line) != _normalize(base_line):
                resolved_lines.append(cur_line)  # current changed it
            elif _normalize(rep_line) != _normalize(base_line):
                resolved_lines.append(rep_line)  # replayed changed it
            else:
                resolved_lines.append(base_line)  # unchanged
        result_text = "\n".join(resolved_lines)
        cov = intent_coverage_score(result_text, base, current, replayed)
        if cov >= 0.5:
            return StructuralResolution(rule="mini_conflict_deterministic", text=result_text)
        return None  # side-intent dropped — unsafe

    # There ARE ambiguous lines. Find maximal contiguous runs.
    amb_runs: list[tuple[int, int]] = []  # (start, end_inclusive)
    start = ambiguous_indices[0]
    prev = start
    for idx in ambiguous_indices[1:]:
        if idx == prev + 1:
            prev = idx
        else:
            amb_runs.append((start, prev))
            start = idx
            prev = idx
    amb_runs.append((start, prev))

    # If ALL base lines are ambiguous (no deterministic tails), the
    # mini-conflict pass provides no value — the entire conflict is the core.
    # Decline and let the LLM handle it.
    total_ambiguous = sum(e - s + 1 for s, e in amb_runs)
    if total_ambiguous >= n:
        return None

    # Build the resolved text: deterministic tails + core markers.
    # For each deterministic region (between ambiguous runs), resolve the lines.
    resolved_tail_lines: list[str] = []
    cur_pos = 0  # position in current/replayed for insert tracking
    for run_idx, (amb_start, amb_end) in enumerate(amb_runs):
        # Resolve the deterministic region before this ambiguous run
        det_start = amb_runs[run_idx - 1][1] + 1 if run_idx > 0 else 0
        det_end = amb_start  # exclusive
        for i in range(det_start, det_end):
            base_line = base_lines[i]
            cur_line = cur_lines[i] if i < len(cur_lines) else ""
            rep_line = rep_lines[i] if i < len(rep_lines) else ""
            if _normalize(cur_line) != _normalize(base_line):
                resolved_tail_lines.append(cur_line)
            elif _normalize(rep_line) != _normalize(base_line):
                resolved_tail_lines.append(rep_line)
            else:
                resolved_tail_lines.append(base_line)
        # Extract the ambiguous core's 3-way text
        core_base = "\n".join(base_lines[amb_start:amb_end + 1])
        core_cur = "\n".join(cur_lines[amb_start:amb_end + 1]) if amb_end < len(cur_lines) else ""
        core_rep = "\n".join(rep_lines[amb_start:amb_end + 1]) if amb_end < len(rep_lines) else ""
        core_parts.append((core_base, core_cur, core_rep))

    # Resolve the deterministic region after the last ambiguous run
    det_start = amb_runs[-1][1] + 1
    for i in range(det_start, n):
        base_line = base_lines[i]
        cur_line = cur_lines[i] if i < len(cur_lines) else ""
        rep_line = rep_lines[i] if i < len(rep_lines) else ""
        if _normalize(cur_line) != _normalize(base_line):
            resolved_tail_lines.append(cur_line)
        elif _normalize(rep_line) != _normalize(base_line):
            resolved_tail_lines.append(rep_line)
        else:
            resolved_tail_lines.append(base_line)

    # If there's only one core, use it directly. If multiple, merge into one.
    if len(core_parts) == 1:
        merged_core_base, merged_core_cur, merged_core_rep = core_parts[0]
    else:
        # Merge multiple cores: concatenate with the deterministic tail lines
        # between them as read-only context. The LLM sees all cores at once.
        # Build the merged core by interleaving cores with their separating tails.
        merged_core_base_parts = []
        merged_core_cur_parts = []
        merged_core_rep_parts = []
        for ci, (cb, cc, cr) in enumerate(core_parts):
            if ci > 0:
                # Insert the deterministic tail lines between this core and the previous
                prev_end = amb_runs[ci - 1][1] + 1
                this_start = amb_runs[ci][0]
                between = []
                for i in range(prev_end, this_start):
                    base_line = base_lines[i]
                    cur_line = cur_lines[i] if i < len(cur_lines) else ""
                    rep_line = rep_lines[i] if i < len(rep_lines) else ""
                    if _normalize(cur_line) != _normalize(base_line):
                        between.append(cur_line)
                    elif _normalize(rep_line) != _normalize(base_line):
                        between.append(rep_line)
                    else:
                        between.append(base_line)
                sep = "\n".join(between)
                merged_core_base_parts.append(sep)
                merged_core_cur_parts.append(sep)
                merged_core_rep_parts.append(sep)
            merged_core_base_parts.append(cb)
            merged_core_cur_parts.append(cc)
            merged_core_rep_parts.append(cr)
        merged_core_base = "\n".join(merged_core_base_parts)
        merged_core_cur = "\n".join(merged_core_cur_parts)
        merged_core_rep = "\n".join(merged_core_rep_parts)

    # Assemble the full resolved text: tails + core_cur (conservative default)
    # The core will be replaced by the LLM via deferred_core.
    # Find where the first core sits in the assembled text.
    pre_core_lines: list[str] = []
    first_amb_start = amb_runs[0][0]
    for i in range(first_amb_start):
        base_line = base_lines[i]
        cur_line = cur_lines[i] if i < len(cur_lines) else ""
        rep_line = rep_lines[i] if i < len(rep_lines) else ""
        if _normalize(cur_line) != _normalize(base_line):
            pre_core_lines.append(cur_line)
        elif _normalize(rep_line) != _normalize(base_line):
            pre_core_lines.append(rep_line)
        else:
            pre_core_lines.append(base_line)

    # Assemble: pre_core + merged_core_cur + post_core
    full_text_parts = []
    if pre_core_lines:
        full_text_parts.append("\n".join(pre_core_lines))
    full_text_parts.append(merged_core_cur)
    # post_core_lines are the resolved_tail_lines after the last ambiguous run
    # (already computed above as the tail after the last run)
    # We need to extract them from resolved_tail_lines
    # Actually, resolved_tail_lines was built incrementally including all
    # deterministic regions. Let's rebuild the full text properly.
    # The simplest approach: assemble all resolved tails + core_cur interleaved
    # at the right position.

    # Rebuild: walk all base lines, emitting resolved tail or core_cur marker
    full_lines: list[str] = []
    amb_ranges_set = set()
    for s, e in amb_runs:
        amb_ranges_set.update(range(s, e + 1))

    # For single core: core_offset = character position of core in full text
    core_char_offset = 0
    core_inserted = False
    for i in range(n):
        if i in amb_ranges_set:
            if not core_inserted:
                # Insert the merged core here
                core_char_offset = len("\n".join(full_lines)) + (1 if full_lines else 0)
                full_lines.extend(merged_core_cur.split("\n"))
                core_inserted = True
            # Skip individual ambiguous lines (they're in the core)
            continue
        # Deterministic line
        base_line = base_lines[i]
        cur_line = cur_lines[i] if i < len(cur_lines) else ""
        rep_line = rep_lines[i] if i < len(rep_lines) else ""
        if _normalize(cur_line) != _normalize(base_line):
            full_lines.append(cur_line)
        elif _normalize(rep_line) != _normalize(base_line):
            full_lines.append(rep_line)
        else:
            full_lines.append(base_line)

    full_text = "\n".join(full_lines)

    # Side-intent guard: verify the deterministic TAILS preserve additions.
    # The core is deferred to the LLM — we only check that the tails didn't
    # silently drop side-specific additions OUTSIDE the ambiguous region.
    # Build a "tails-only" text (excluding the core) and check coverage.
    tails_only_lines = [
        l for i, l in enumerate(full_lines)
        if not (
            core_char_offset <= sum(len(fl) + 1 for fl in full_lines[:i])
            and sum(len(fl) + 1 for fl in full_lines[:i]) < core_char_offset + len(merged_core_cur)
        )
    ]
    tails_text = "\n".join(tails_only_lines)
    # Compute additions that are OUTSIDE the ambiguous region — these must
    # survive in the tails. Lines inside the ambiguous region are the LLM's job.
    base_set = {l.strip() for l in base.split("\n") if l.strip()}
    cur_added = {l.strip() for l in current.split("\n") if l.strip()} - base_set
    rep_added = {l.strip() for l in replayed.split("\n") if l.strip()} - base_set
    # Lines in the ambiguous core region are expected to be resolved by the LLM
    core_cur_set = {l.strip() for l in merged_core_cur.split("\n") if l.strip()}
    core_rep_set = {l.strip() for l in merged_core_rep.split("\n") if l.strip()}
    # Only check additions NOT in the core's version (those are the LLM's job)
    cur_tail_adds = cur_added - core_cur_set
    rep_tail_adds = rep_added - core_rep_set
    tails_set = {l.strip() for l in tails_text.split("\n") if l.strip()}
    cur_tail_cov = len(cur_tail_adds & tails_set) / len(cur_tail_adds) if cur_tail_adds else 1.0
    rep_tail_cov = len(rep_tail_adds & tails_set) / len(rep_tail_adds) if rep_tail_adds else 1.0
    tail_cov = min(cur_tail_cov, rep_tail_cov)
    if tail_cov < 0.5:
        return None  # shrinking dropped side-specific additions from the tails

    return StructuralResolution(
        rule="mini_conflict",
        text=full_text,
        deferred_core=(merged_core_base, merged_core_cur, merged_core_rep),
        deferred_core_offset=core_char_offset,
    )


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


#: Lines that are pure structural punctuation — braces, semicolons, closing
#: brackets. They carry no semantic weight and appear in any block-structured
#: code (C/C++/Rust/Java). Ignored in the insertion_union overlap check and
#: line-explosion guard: two independent SECTION/function blocks naturally
#: share ``{``/``}`` lines, and their braces multiply when concatenated —
#: neither indicates semantic overlap or content duplication.
_STRUCTURAL_NOISE_LINES = frozenset({"{", "}", "};", "});"})


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
    # Blank lines AND pure structural punctuation ({, }, };) are ignored in
    # the overlap check: they carry no semantic weight and appear in any
    # block-structured code. Without this, two independent SECTION/function
    # blocks that share a standalone ``{`` brace line would falsely register
    # as overlapping content, declining a trivial additive merge and forcing
    # the LLM (which may truncate on large insertions like test data arrays).
    cur_flat = [ln for run in cur_ins.values() for ln in run
                if ln.strip() and ln.strip() not in _STRUCTURAL_NOISE_LINES]
    rep_flat = [ln for run in rep_ins.values() for ln in run
                if ln.strip() and ln.strip() not in _STRUCTURAL_NOISE_LINES]
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
    # Size sanity guard: a pure insertion union should produce exactly
    # base_lines + both sides' distinct insertions (including blank lines).
    # If the output is larger than that, the rule kept extra (deleted) content
    # alongside the new — a sign the entity-splitting made a sub-unit look like
    # pure insertion when the whole conflict had deletions. Decline so the LLM
    # handles it. (Catches the nlohmann-0020 defect where deleted code was
    # Line-explosion guard: if any normalized line appears MORE times in the
    # output than in any side (base/current/replayed), the rule duplicated
    # content beyond what either side contains. This catches cases where
    # entity-splitting made a sub-unit look like pure insertion but the parent
    # conflict had content that was duplicated by the union. (The prior size
    # guard was a tautology — len(out) always equals base + insertions by
    # construction. This per-line check catches the actual duplication pattern.)
    from collections import Counter as _Ctr
    def _nl(text):
        return _Ctr(" ".join(l.split()) for l in text.split("\n") if l.strip())
    _bc = _nl("\n".join(base_lines))
    _cc = _nl(current)
    _rc = _nl(replayed)
    _oc = _nl("\n".join(out))
    for _line, _cnt in _oc.items():
        if _line in _STRUCTURAL_NOISE_LINES:
            continue  # braces multiply when blocks concatenate — expected
        _allowed = max(_bc.get(_line, 0), _cc.get(_line, 0), _rc.get(_line, 0))
        if _cnt > _allowed:
            return None  # line duplicated beyond what any side contains
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
    # with ``)``, optionally followed by trailing C++ qualifiers (const,
    # noexcept, override, final, &, &&) and then ``{`` or ``;``. The ``{`` or
    # ``;`` may be on the NEXT line (common C++ brace-on-next-line style), or
    # the full body may be on one line (``void foo() { return 1; }``).
    sig_pat = re.compile(
        r"\b([A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*\([^)]*\)"
        r"(?:\s*(?:const|noexcept|override|final|&|\|\||=\s*(?:default|delete)))*"
        r"\s*(?:[;{].*)?\s*$"
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
        # Exclude bare function CALL statements: a definition/declaration has
        # a return type before the name. A call like ``log_error(msg);`` has
        # the callee name at the start of the line (empty prefix before it).
        # A definition like ``void foo();`` or ``std::string bar()`` has a
        # type prefix before the name.
        pre_call = stripped[:m.start()].strip()
        if not pre_call:
            # No return type before the name → likely a function call.
            continue
        # Confirm this is a definition/declaration: the line ends with ``;``,
        # ``{`` (brace on same line), or ``}`` (one-line body like
        # ``void foo() { return 1; }``); OR the next non-blank line starts
        # with ``{`` (brace on next line).
        next_stripped = ""
        if idx + 1 < len(lines):
            next_stripped = lines[idx + 1].strip()
        is_def = (
            stripped.endswith((";", "{", "}"))
            or next_stripped.startswith(("{",))
        )
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
    # then any replayed additions not already emitted. Only SHARED lines (present
    # in both sides' additions) are deduplicated across anchors — lines that
    # repeat WITHIN one side's own additions (e.g. multiple break; statements in
    # a switch) are legitimate repetitions and must NOT be collapsed.
    emitted_shared: set[str] = set()
    out: list[str] = []
    for i, bl in enumerate(base_lines):
        if i in cur_ins:
            for ln in cur_ins[i]:
                # Only deduplicate shared lines (in both sides) that were
                # already emitted. Non-shared lines always pass through.
                if ln.strip() and ln in shared and ln in emitted_shared:
                    continue
                out.append(ln)
                if ln.strip() and ln in shared:
                    emitted_shared.add(ln)
        if i in rep_ins:
            for ln in rep_ins[i]:
                if ln.strip() and ln in shared and ln in emitted_shared:
                    continue
                out.append(ln)
                if ln.strip() and ln in shared:
                    emitted_shared.add(ln)
        out.append(bl)
    # Trailing insertions.
    for ln in cur_ins.get(len(base_lines), []):
        if ln.strip() and ln in shared and ln in emitted_shared:
            continue
        out.append(ln)
        if ln.strip() and ln in shared:
            emitted_shared.add(ln)
    for ln in rep_ins.get(len(base_lines), []):
        if ln.strip() and ln in shared and ln in emitted_shared:
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

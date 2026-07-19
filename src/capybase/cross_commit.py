"""Cross-commit dependency guardian — a deterministic window-level
check that closes the per-commit blind spot.

Every per-commit validator operates on one replayed commit in isolation. A
window like:

    commit A: renames ``foo`` → ``bar``           (locally valid)
    commit B: calls ``foo`` (the old name)        (locally valid)

replays with both commits passing every per-commit gate — A's rename is a
legitimate local change, B's call is a legitimate local reference — yet the
final rebased branch is broken: ``foo`` no longer exists. Neither commit's
validator sees the other. The existing ``future_obligations`` check only covers
a resolution's OWN defined symbols against future references; it cannot catch a
rename elsewhere in the window.

This module builds a lightweight per-commit DEFINES/USES map across the whole
rebase window and, after the rebase completes, verifies every cross-commit
dependency edge (commit B uses a symbol commit A defines) still resolves in the
final rebased tree — by name OR by a recognized rename (body-fingerprint match).

Pure and deterministic. The orchestrator supplies the per-commit file contents
(via ``git.blob_at``) and the final tree's entities; this module reports
``DependencyBreak`` violations. No LLM is involved — prior work's finding that
this gap is "entirely deterministic, no LLM required".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from capybase.adapters.structural import Entity

#: Languages the cross-commit guardian can build a defines/uses map for. Derived
#: from the abstract parser's own family map so the guardian tracks parser
#: coverage by construction — adding a language to the parser automatically
#: extends the guardian here. The guardian is a no-op for other languages
#: (degrades gracefully, like every structural check). Resolved lazily to avoid
#: an import cycle at module load.
_SUPPORTED_LANGUAGES: tuple[str, ...] | None = None
_LANG_BY_EXT: dict[str, str] | None = None


def _ensure_lang_maps() -> None:
    """Populate ``_SUPPORTED_LANGUAGES`` / ``_LANG_BY_EXT`` from the parser.

    Done lazily (first call) rather than at import time so ``cross_commit`` can
    be imported without pulling in the adapter stack, mirroring the lazy
    ``from capybase.adapters import structural`` pattern used throughout this
    module.
    """
    global _SUPPORTED_LANGUAGES, _LANG_BY_EXT
    if _LANG_BY_EXT is not None:
        return
    from capybase.adapters import abstract_parser
    _SUPPORTED_LANGUAGES = tuple(abstract_parser._LANG_FAMILY.keys())
    # The parser's ext→lang map is the source of truth; copy it so the guardian
    # sees every extension the parser recognizes (.go/.java/.js/.ts/.c/.cpp/...).
    _LANG_BY_EXT = dict(abstract_parser._EXT_LANG)


def _language_for_path(path: str) -> str | None:
    """The canonical language for a file path, or None if unsupported.

    Routes through the abstract parser's extension map, so the guardian covers
    every language the grammar-free parser recognizes (Family A + Family B).
    Returns None for unrecognized extensions — callers treat that as "no
    structural signal, skip this file" (the guardian degrades gracefully).

    Also gates on ``detect_family``: text/config formats (markdown, json, yaml,
    toml, shell) are in the consolidated extension map for language-tagging but
    have no structural family, so the guardian skips them — there are no
    definitions to track in a markdown file. Fix #11 made the map the single
    source of truth, which surfaced this distinction.
    """
    _ensure_lang_maps()
    dot = path.rfind(".")
    if dot < 0:
        return None
    lang = _LANG_BY_EXT.get(path[dot:].lower())  # type: ignore[union-attr]
    if lang is None:
        return None
    # Only scan languages the parser can structurally parse (have a family).
    from capybase.adapters import abstract_parser
    if abstract_parser.detect_family(lang, path) is None:
        return None
    return lang


@dataclass(frozen=True)
class CommitSymbols:
    """The defines/uses picture for one replayed commit's touched files.

    ``defines`` is the set of (kind, name) entities the commit's content
    introduces; ``uses`` is the set of bare names the commit references but does
    NOT define itself (a dependency on an earlier commit's definition). Both are
    derived from the commit's post-image file contents (the symbols present after
    the commit applied), scoped to the files it touched — a commit that doesn't
    touch a file doesn't depend on or define symbols there.
    """

    defines: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    uses: frozenset[str] = field(default_factory=frozenset)
    # name → (kind, body_fingerprint) for each defined entity. Used by the audit
    # to recognize a CONSISTENT rename: a symbol gone by name but whose body
    # survives under a different name (and whose call sites were updated) is not
    # a missing definition.
    defines_body: Mapping[str, tuple[str, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class DependencyEdge:
    """Commit B references a symbol commit A defines (B depends on A).

    ``symbol`` is the used name; ``definer`` is the commit OID that defines it;
    ``user`` is the commit OID that references it. ``body_fingerprint`` is the
    ``(kind, body_fp)`` of the definer's original definition, used by the audit
    to recognize a consistent rename (the symbol survives under a new name with
    the same body, and the user's call site was updated).
    """

    symbol: str
    definer: str
    user: str
    body_fingerprint: tuple[str, str] | None = None


@dataclass(frozen=True)
class DependencyBreak:
    """A cross-commit dependency that the final rebased tree no longer satisfies.

    ``symbol`` is the name the user-commit references; ``definer``/``user`` are
    the two commit OIDs; ``break_type`` is ``missing_definition`` (the symbol is
    gone from the final tree entirely) or ``stale_reference`` (the symbol exists
    only under a renamed name the user-commit didn't adopt).
    """

    symbol: str
    definer: str
    user: str
    break_type: str  # "missing_definition" | "stale_reference"

    def render(self) -> str:
        return (
            f"commit {self.user[:8]} references `{self.symbol}` defined by "
            f"commit {self.definer[:8]}, but the final tree {self.break_type} "
            f"(symbol missing/renamed away)"
        )


def _defined_names(defined: frozenset[tuple[str, str]]) -> set[str]:
    return {name for _kind, name in defined}


def build_commit_symbols(
    files: "dict[str, str]",
    *,
    added_text: "dict[str, str] | None" = None,
    defined_kinds: tuple[str, ...] = ("function", "method", "class"),
) -> CommitSymbols:
    """Build the defines/uses map for one commit's touched files.

    Args:
        files: ``{path: file_text}`` for the files the commit touched (post-image
            contents — what the file looks like AFTER the commit applied). Used
            to derive DEFINES.
        added_text: optional ``{path: only_the_added_lines}`` — the commit's
            actual contribution (its ``+`` lines). When provided, USES is
            computed from these added lines only (a name a commit references in
            content it ADDED is a cross-commit dependency), which is the correct
            signal: a later commit whose post-image re-includes an earlier
            definition should NOT count that name as locally-used. When None,
            USES falls back to the full post-image text (looser; for callers
            without diff access).
        defined_kinds: the entity kinds that count as "definitions" a later
            commit may depend on (functions/methods/classes; fields/config are
            advisory-only and excluded, matching future_obligations' policy).

    Returns a :class:`CommitSymbols`. Defines = entities of the given kinds
    present in the files; uses = bare names referenced in the (added) text that
    are NOT locally defined. Never raises — a parse failure on one file is skipped.
    """
    from capybase.adapters import structural

    defines: set[tuple[str, str]] = set()
    defines_body: dict[str, tuple[str, str]] = {}
    for path, text in files.items():
        lang = _language_for_path(path)
        if lang is None or not structural.is_available(lang):
            continue
        # recursive=True: methods/fields NESTED inside a class must enter
        # ``defines`` (bug #6/BUG C — the guardian's whole point is catching
        # cross-commit method drops, which requires method-level visibility).
        ents = structural.enumerate_entities(text, lang, recursive=True)
        if ents is None:
            continue
        for e in ents:
            if e.kind in defined_kinds:
                defines.add((e.kind, e.name))
                bf = structural.entity_body_fingerprint(e, "") or ""
                if bf:
                    defines_body.setdefault(e.name, (e.kind, bf))
    # When added_text is provided, USES is computed from the commit's actual
    # contribution (its + lines), and "locally defined" for the subtraction is
    # the names the commit DEFINES in that added content — NOT the full post-image
    # (which re-includes earlier definitions). This is the key correctness point:
    # a later commit re-including an earlier definition and referencing it in new
    # code IS a cross-commit dependency (the new code depends on the earlier def).
    source = added_text if added_text is not None else files
    if added_text is not None:
        locally_defined: set[str] = set()
        for path, text in added_text.items():
            lang = _language_for_path(path)
            if lang is None or not structural.is_available(lang):
                continue
            ents = structural.enumerate_entities(text, lang, recursive=True)
            if ents is None:
                continue
            for e in ents:
                if e.kind in defined_kinds:
                    locally_defined.add(e.name)
    else:
        locally_defined = {name for _k, name in defines}
    uses: set[str] = set()
    for path, text in source.items():
        lang = _language_for_path(path)
        if lang is None or not structural.is_available(lang):
            continue
        for name in structural.referenced_symbols(text, lang):
            if name not in locally_defined:
                uses.add(name)
    return CommitSymbols(
        defines=frozenset(defines), uses=frozenset(uses),
        defines_body=dict(defines_body),
    )


def build_dependency_graph(
    commit_symbols: "dict[str, CommitSymbols]",
    commit_order: "list[str]",
) -> list[DependencyEdge]:
    """Build cross-commit dependency edges from the per-commit defines/uses map.

    An edge A→B (here represented with definer=A, user=B) exists when commit B
    USES a name that commit A DEFINES and A comes BEFORE B in the replay order.
    This captures "B depends on a symbol A introduced." A self-edge (a commit
    using a symbol it defines) is excluded by build_commit_symbols.

    Args:
        commit_symbols: ``{commit_oid: CommitSymbols}`` for each replayed commit.
        commit_order: the commit OIDs oldest-first (replay order), used to direct
            edges earlier→later only (a later commit can't be depended on by an
            earlier one in a linear rebase).

    Returns the dependency edges. OIDs missing from ``commit_symbols`` are
    skipped (the guardian degrades to whatever symbols it could build).
    """
    # Map each defined name → the EARLIEST commit that defines it, plus that
    # definer's (kind, body_fingerprint) for consistent-rename recognition.
    first_definer: dict[str, str] = {}
    first_definer_body: dict[str, tuple[str, str] | None] = {}
    position: dict[str, int] = {oid: i for i, oid in enumerate(commit_order)}
    for oid in commit_order:
        syms = commit_symbols.get(oid)
        if syms is None:
            continue
        for _kind, name in syms.defines:
            if name not in first_definer:
                first_definer[name] = oid
                first_definer_body[name] = syms.defines_body.get(name)
    edges: list[DependencyEdge] = []
    for user in commit_order:
        syms = commit_symbols.get(user)
        if syms is None:
            continue
        for name in syms.uses:
            definer = first_definer.get(name)
            if definer is None or definer == user:
                continue
            # Forward dependency only: the definer must PRECEDE the user in the
            # replay order. An earlier commit using a symbol a later commit
            # defines is not a rebase-introduced dependency (it resolved to a
            # builtin/external at the earlier commit's time, or was already so
            # ordered in the source branch); only "user depends on an EARLIER
            # definer" is a relationship the rebase must preserve.
            if position.get(definer, -1) < position.get(user, -1):
                edges.append(DependencyEdge(
                    symbol=name, definer=definer, user=user,
                    body_fingerprint=first_definer_body.get(name),
                ))
    return edges


def audit_cross_commit_dependencies(
    edges: "list[DependencyEdge]",
    final_tree_entities: "dict[str, list[Entity]]",
    final_tree_text: "dict[str, str] | None" = None,
) -> list[DependencyBreak]:
    """Verify each dependency edge survives in the final rebased tree.

    For each edge (definer defines ``symbol``, user references it), check that
    ``symbol`` is still resolvable in the final rebased tree — present by name in
    ANY file, OR present under a different name that body-matches the original
    definition (a recognized rename). A symbol that's gone entirely is a
    ``missing_definition`` break; one that survives only under a renamed name the
    user-commit didn't adopt is informational (the rename is consistent if the
    user was updated) — reported as ``stale_reference`` only when the name is
    truly absent everywhere.

    Args:
        edges: the cross-commit dependency edges (from build_dependency_graph).
        final_tree_entities: ``{path: [Entity]}`` for the final rebased tree's
            files (the orchestrator enumerates these from the final HEAD).

    Returns the violations. Empty list = every dependency resolves.
    """
    from capybase.adapters import structural

    # Index the final tree: (name, kind) pairs present anywhere, names present
    # anywhere, plus bodies for rename match. The kind matters: a kind-changing
    # refactor at the same name (function→class) is a behavior change for any
    # call site — foo() now constructs an instance instead of calling a function.
    # Indexing name-only would silently treat that as "still defined".
    final_name_kinds: set[tuple[str, str]] = set()
    final_names: set[str] = set()
    final_by_body: dict[tuple[str, str], str] = {}  # (kind, body_fp) → name
    for path, ents in final_tree_entities.items():
        for e in ents:
            final_name_kinds.add((e.name, e.kind))
            final_names.add(e.name)
            bf = structural.entity_body_fingerprint(e, "") or ""
            if bf:
                final_by_body.setdefault((e.kind, bf), e.name)

    breaks: list[DependencyBreak] = []
    seen: set[tuple[str, str, str]] = set()
    for edge in edges:
        # The original definer's kind (from body_fingerprint); None when the edge
        # has no fingerprint (older call sites). When known, the symbol must
        # survive under the SAME kind — a kind change at the same name is a
        # behavior change, not a resolution.
        edge_kind = edge.body_fingerprint[0] if edge.body_fingerprint else None
        if edge.symbol in final_names:
            if edge_kind is None or (edge.symbol, edge_kind) in final_name_kinds:
                continue  # still defined by name (and same kind) → resolved
            # Same name, DIFFERENT kind → fall through to the missing/rename
            # check below. The body won't match (different kind), so it reports
            # as missing_definition — the kind change broke the call site.
        key = (edge.symbol, edge.definer, edge.user)
        if key in seen:
            continue
        seen.add(key)
        # Not present by name. Check whether the original definition's BODY
        # survives under a different name (a consistent rename). If it does,
        # the symbol was renamed, not dropped. The rename is COMPLETE (not a
        # break) only if the old name is no longer REFERENCED anywhere in the
        # final tree — a stale reference (the user's call site still uses the
        # old name) is a genuine ``stale_reference`` break. Requires the final
        # tree text; without it, fall through to the conservative report.
        if edge.body_fingerprint and edge.body_fingerprint in final_by_body:
            if final_tree_text is not None:
                old_name_still_referenced = any(
                    edge.symbol in structural.referenced_symbols(text, _language_for_path(path) or "")
                    for path, text in final_tree_text.items()
                )
                if not old_name_still_referenced:
                    continue  # consistent rename: old name gone, body survives → resolved
                # Stale reference — report as informational, not missing_definition.
                breaks.append(DependencyBreak(
                    symbol=edge.symbol, definer=edge.definer, user=edge.user,
                    break_type="stale_reference",
                ))
                continue
            # No text to check staleness — fall through to the conservative
            # missing_definition report (preserves prior behavior for callers
            # that don't supply final_tree_text).
        breaks.append(DependencyBreak(
            symbol=edge.symbol, definer=edge.definer, user=edge.user,
            break_type="missing_definition",
        ))
    return breaks


# ---------------------------------------------------------------------------
# Intent evolution trace — post-window assurance audit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvolutionStep:
    """One step in an entity's evolution across the rebase window.

    ``commit_oid`` is the commit that touched the entity; ``change`` is the
    entity-level change (body_changed/renamed/signature_changed) relative to the
    PRIOR step; ``body_fingerprint`` is the entity's body content AFTER this
    commit (the post-image), so the chain's last step's fingerprint is what the
    final merged version SHOULD match.
    """

    commit_oid: str
    change: "EntityChange | None"
    body_fingerprint: str


@dataclass(frozen=True)
class EvolutionChain:
    """The ordered evolution of one entity across the commits that touched it.

    ``name`` is the entity's name (matched by name across commits); ``kind`` is
    its coarse kind; ``steps`` are ordered oldest-first. A chain of length ≥2
    means the entity evolved across multiple commits — the case the evolution
    audit examines for lost intermediate steps.
    """

    name: str
    kind: str
    steps: list[EvolutionStep] = field(default_factory=list)

    @property
    def expected_body_fingerprint(self) -> str:
        """The body fingerprint the final merge SHOULD have — the LAST step's
        post-image (the most recent evolution of the entity in the source branch)."""
        return self.steps[-1].body_fingerprint if self.steps else ""


@dataclass(frozen=True)
class EvolutionGap:
    """A divergence between an entity's expected final evolution and the merge.

    ``name``/``kind`` identify the entity; ``commit_count`` is how many commits
    touched it; ``expected_from_commit`` is the OID of the last commit that
    evolved it; the final merged tree's body fingerprint does NOT match that
    step's — the merge likely reverted to or kept an earlier version.
    """

    name: str
    kind: str
    commit_count: int
    expected_from_commit: str
    actual_body_fingerprint: str
    expected_body_fingerprint: str

    def render(self) -> str:
        return (
            f"{self.kind} `{self.name}` evolved across {self.commit_count} commit(s); "
            f"final merge does not match the last evolution (from "
            f"{self.expected_from_commit[:8]}) — a step may have been lost or reverted"
        )


def build_evolution_chains(
    per_commit_files: "dict[str, dict[str, str]]",
    commit_order: "list[str]",
    language: str,
) -> "list[EvolutionChain]":
    """Build the per-entity evolution chains across the rebase window.

    For each entity NAME that appears in ≥2 commits' post-images, the chain is
    the ordered list of commits that defined it, each carrying the entity's
    body fingerprint AFTER that commit. The chain's last step's fingerprint is
    the expected final body the merge should preserve.

    Args:
        per_commit_files: ``{commit_oid: {path: file_text}}`` for each replayed
            commit's touched files (post-image contents).
        commit_order: the commit OIDs oldest-first (replay order).
        language: the language to enumerate entities in.

    Returns chains of length ≥2 only (single-commit entities don't evolve).
    Empty list when the structural parser is unavailable or no entity spans ≥2 commits.
    Pure; takes pre-fetched contents so it's testable without a repo.
    """
    from capybase.adapters import structural

    if not structural.is_available(language):
        return []
    # For each commit, enumerate entities (name → (kind, body_fingerprint)).
    per_commit_ents: dict[str, dict[str, tuple[str, str]]] = {}
    for oid in commit_order:
        files = per_commit_files.get(oid)
        if not files:
            per_commit_ents[oid] = {}
            continue
        ents_map: dict[str, tuple[str, str]] = {}
        for path, text in files.items():
            if _language_for_path(path) != language:
                continue
            ents = structural.enumerate_entities(text, language)
            if ents is None:
                continue
            for e in ents:
                ents_map[e.name] = (e.kind, structural.entity_body_fingerprint(e, language) or "")
        per_commit_ents[oid] = ents_map

    # Group: name → list of (commit_oid, kind, body_fingerprint) in order.
    by_name: dict[str, list[tuple[str, str, str]]] = {}
    for oid in commit_order:
        for name, (kind, bf) in per_commit_ents.get(oid, {}).items():
            by_name.setdefault(name, []).append((oid, kind, bf))

    chains: list[EvolutionChain] = []
    for name, hits in by_name.items():
        if len(hits) < 2:
            continue  # single-commit entity — no evolution to lose
        steps = [
            EvolutionStep(commit_oid=oid, change=None, body_fingerprint=bf)
            for oid, _kind, bf in hits
        ]
        chains.append(EvolutionChain(name=name, kind=hits[0][1], steps=steps))
    return chains


def audit_evolution(
    chains: "list[EvolutionChain]",
    final_tree_entities: "dict[str, list[Entity]]",
) -> list[EvolutionGap]:
    """Flag entities whose final merged body diverges from their last evolution.

    For each chain (an entity that evolved across ≥2 commits), the final rebased
    tree's entity with the same name should have the body fingerprint of the
    chain's LAST step (the most recent source-branch evolution). A mismatch
    means the merge kept/reverted to an earlier version — a lost intermediate
    evolution step no per-commit validator sees.

    This is the SOUND version of prior work's evolution check: rather than
    speculatively composing body deltas (which don't compose like arithmetic),
    it verifies the merge matches the LATEST known evolution of the entity. A
    merge that silently drops the last step (the practical bug) is caught; the
    rare case where a merge legitimately revises the body further is flagged for
    review (advisory, not a hard gate).

    Args:
        chains: the evolution chains (from build_evolution_chains).
        final_tree_entities: ``{path: [Entity]}`` for the final rebased tree.

    Returns the gaps. Empty list = every evolved entity matches its last step.
    """
    from capybase.adapters import structural

    # Index the final tree by name → body fingerprint.
    final_by_name: dict[str, str] = {}
    for ents in final_tree_entities.values():
        for e in ents:
            final_by_name.setdefault(
                e.name, structural.entity_body_fingerprint(e, "") or ""
            )

    gaps: list[EvolutionGap] = []
    for chain in chains:
        if not chain.steps:
            continue
        last = chain.steps[-1]
        actual = final_by_name.get(chain.name)
        if actual is None:
            continue  # entity gone entirely — the dependency guardian covers that
        if actual == last.body_fingerprint:
            continue  # matches the latest evolution → no gap
        gaps.append(EvolutionGap(
            name=chain.name, kind=chain.kind,
            commit_count=len(chain.steps),
            expected_from_commit=last.commit_oid,
            actual_body_fingerprint=actual,
            expected_body_fingerprint=last.body_fingerprint,
        ))
    return gaps

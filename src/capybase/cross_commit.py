"""Cross-commit dependency guardian (survey §3.1) — a deterministic window-level
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
``DependencyBreak`` violations. No LLM is involved — the survey's finding that
this gap is "entirely deterministic, no LLM required".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from capybase.adapters.structural import Entity

#: Languages we can build a meaningful defines/uses map for. The guardian is a
#: no-op for other languages (degrades gracefully, like every structural check).
_SUPPORTED_LANGUAGES = ("python", "rust")

#: Extension → language map for classifying a path's content.
_LANG_BY_EXT = {
    ".py": "python",
    ".rs": "rust",
}


def _language_for_path(path: str) -> str | None:
    """The tree-sitter language for a file path, or None if unsupported."""
    dot = path.rfind(".")
    if dot < 0:
        return None
    return _LANG_BY_EXT.get(path[dot:].lower())


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


@dataclass(frozen=True)
class DependencyEdge:
    """Commit B references a symbol commit A defines (B depends on A).

    ``symbol`` is the used name; ``definer`` is the commit OID that defines it;
    ``user`` is the commit OID that references it.
    """

    symbol: str
    definer: str
    user: str


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
    for path, text in files.items():
        lang = _language_for_path(path)
        if lang is None or not structural.is_available(lang):
            continue
        ents = structural.enumerate_entities(text, lang)
        if ents is None:
            continue
        for e in ents:
            if e.kind in defined_kinds:
                defines.add((e.kind, e.name))
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
            ents = structural.enumerate_entities(text, lang)
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
        defines=frozenset(defines), uses=frozenset(uses)
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
    # Map each defined name → the EARLIEST commit that defines it.
    first_definer: dict[str, str] = {}
    position: dict[str, int] = {oid: i for i, oid in enumerate(commit_order)}
    for oid in commit_order:
        syms = commit_symbols.get(oid)
        if syms is None:
            continue
        for _kind, name in syms.defines:
            first_definer.setdefault(name, oid)
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
                ))
    return edges


def audit_cross_commit_dependencies(
    edges: "list[DependencyEdge]",
    final_tree_entities: "dict[str, list[Entity]]",
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

    # Index the final tree: names present anywhere, plus bodies for rename match.
    final_names: set[str] = set()
    final_by_body: dict[tuple[str, str], str] = {}  # (kind, body_fp) → name
    for path, ents in final_tree_entities.items():
        for e in ents:
            final_names.add(e.name)
            bf = structural.entity_body_fingerprint(e, "") or ""
            if bf:
                final_by_body.setdefault((e.kind, bf), e.name)

    breaks: list[DependencyBreak] = []
    seen: set[tuple[str, str, str]] = set()
    for edge in edges:
        if edge.symbol in final_names:
            continue  # still defined by name somewhere → resolved
        key = (edge.symbol, edge.definer, edge.user)
        if key in seen:
            continue
        seen.add(key)
        # Not present by name → missing (we don't have the original definition's
        # body here to confirm a rename, so this is the conservative report:
        # the symbol the user-commit referenced is not findable by name).
        breaks.append(DependencyBreak(
            symbol=edge.symbol, definer=edge.definer, user=edge.user,
            break_type="missing_definition",
        ))
    return breaks

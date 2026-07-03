"""Tree-sitter structural analysis for capybase.

Parses source into an AST, finds the lowest common ancestor node enclosing a
conflict span, and computes a canonical structural fingerprint for AST-level
preservation checks. This replaces the semantically-blind ``<<<<<<<`` marker
window with a logical block (the specific ``def``/``fn``/``impl``/``struct``)
so the resolver and validators reason about code structure, not line counts.

All ``tree_sitter`` imports are lazy and the module degrades gracefully:
every public function returns ``None`` (or an empty result) if the library or
a grammar is absent or parsing fails. Callers must treat a ``None`` result as
"no structural signal available" and fall back to the line-window behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from capybase.conflict_model import RelatedSnippet


# ---------------------------------------------------------------------------
# Node-level results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeInfo:
    """A resolved enclosing AST node for a conflict span.

    ``span`` is a 0-based inclusive ``(start_row, end_row)`` line range,
    matching capybase's marker_span convention. ``text`` is the source slice
    of the node (the isolated logical block). ``signature`` is a short label
    like ``def greet()`` / ``fn foo() -> Bar`` / ``struct Config`` for the
    enclosing-symbol seam.
    """

    node_type: str
    span: tuple[int, int]
    text: str
    signature: str | None


@dataclass(frozen=True)
class Entity:
    """A coarse, identity-stable top-level definition inside a container.

    The unit of entity-level merge (survey §3.2 Weave/Aura): a function, method,
    class, or field matched by ``(kind, name)`` so two sides that each add a
    DISTINCT entity at the same insertion point can be recognized as
    non-conflicting. ``kind`` is a coarse, language-neutral label
    (``"function"``/``"class"``/``"method"``/``"field"``) so matching doesn't
    depend on grammar-specific node type strings; ``name`` is the bare
    identifier; ``body`` is the exact source text (the text-carrying leaf the
    survey prescribes). ``span`` is the 0-based ``(start_row, end_row)`` range.
    """

    kind: str
    name: str
    body: str
    span: tuple[int, int]

    @property
    def identity(self) -> tuple[str, str]:
        """The stable entity key ``(kind, name)`` used for matching."""
        return (self.kind, self.name)


# Tree-sitter node types that represent a self-contained logical definition.
# The lowest enclosing node is only "useful" if it is one of these; otherwise
# we keep walking up (or give up at module level).
_DEFINITION_TYPES = {
    # Python
    "function_definition",
    "class_definition",
    "decorated_definition",
    # Rust
    "function_item",
    "impl_item",
    "struct_item",
    "enum_item",
    "trait_item",
    "mod_item",
    "macro_definition",
}

# Node types whose children are definitions (we descend into these to find the
# real definition node rather than reporting the container).
_CONTAINER_TYPES = {
    "decorated_definition",  # Python @decorator\ndef ...
    "implementation_list",  # Rust impl { ... }
    "declaration_list",  # Rust mod { ... }
    "struct_body",
    "enum_body",
}


# ---------------------------------------------------------------------------
# Lazy grammar loading
# ---------------------------------------------------------------------------


def _language_for(language: str):
    """Build a tree-sitter Language for ``language`` or return ``None``.

    Delegates to the language adapter (#5) for grammar loading so the
    python/rust branch lives in one place; imports stay deferred so capybase
    works without the ``structural`` extra.
    """
    try:
        from capybase.adapters.language import adapter_for
        return adapter_for(language).tree_sitter_language()
    except Exception:  # noqa: BLE001
        return None


def _make_parser(language: str):
    """Return a configured Parser for ``language`` or ``None`` if unavailable."""
    lang = _language_for(language)
    if lang is None:
        return None
    try:
        from tree_sitter import Parser

        return Parser(lang)
    except Exception:  # noqa: BLE001
        return None


def _parse(source: str, language: str):
    """Parse ``source`` into a tree-sitter Tree, or return ``None``."""
    parser = _make_parser(language)
    if parser is None:
        return None
    try:
        return parser.parse(source.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Span → enclosing node
# ---------------------------------------------------------------------------


def _contains(node, start_row: int, end_row: int) -> bool:
    """True if ``node``'s row range fully contains [start_row, end_row]."""
    ns, ne = node.start_point.row, node.end_point.row
    return ns <= start_row and ne >= end_row


def _is_trivia(node_type: str) -> bool:
    """Nodes that carry no structural meaning and should be ignored in a
    fingerprint so comments/formatting don't perturb the digest."""
    return node_type in {
        "comment",
        "line_comment",
        "block_comment",
        "documentation",  # rust doc comments
    }


def _is_useful(node_type: str) -> bool:
    """A node worth recording in a structural fingerprint.

    Excludes trivia (comments) and anonymous punctuation/bracket nodes whose
    presence is implied by their parent's type — keeping them would make the
    fingerprint sensitive to formatting noise (e.g. optional trailing commas).
    """
    if _is_trivia(node_type):
        return False
    return node_type not in {
        # anonymous punctuation/brackets present in many grammars
        "",
        "(",
        ")",
        "[",
        "]",
        "{",
        "}",
        ",",
        ";",
        ":",
        ".",
        "=",
        "->",
        "=>",
        "+",
        "-",
        "*",
        "/",
        "\\",
        "|",
        "&",
        "!",
        "?",
        "@",
        "#",
        "$",
        "%",
        "^",
        "~",
        "<",
        ">",
    }


def enclosing_node(
    source: str, span: tuple[int, int], language: str
) -> NodeInfo | None:
    """Find the lowest useful AST node enclosing ``span``.

    Descends from the root into the deepest child whose row range still fully
    contains the span, but STOPS as soon as it reaches a definition-typed node
    (``function_definition``, ``impl_item``, ``struct_item`` ...). A conflict
    inside a function resolves to that function, not the bare ``return``
    statement within it — the model needs the whole logical block. Returns
    ``None`` if tree-sitter is unavailable or no parse is possible.

    Note: ``span`` is a line range in the CONFLICTED worktree (the marker
    block). Callers should pass a *clean, parseable* source whose line layout
    matches the worktree outside the conflict — typically the BASE blob — so
    the parser sees valid structure. Passing the raw marker-laden worktree
    produces ERROR nodes and a useless enclosing ``module``.
    """
    tree = _parse(source, language)
    if tree is None:
        return None
    # Use the START line of the span as the anchor: it reliably sits inside the
    # enclosing definition even when the span end extends past the definition
    # boundary (which happens because a conflict marker block is wider than the
    # base content it replaces). This is more robust than requiring the node to
    # contain the full span.
    anchor_row = span[0]
    node = tree.root_node
    while node.type not in _DEFINITION_TYPES:
        child = None
        for c in node.children:
            if c.start_point.row <= anchor_row <= c.end_point.row:
                child = c
                break
        if child is None or child == node:
            break
        node = child
    text = source.encode("utf-8")[node.start_byte : node.end_byte].decode(
        "utf-8", errors="replace"
    )
    nspan = (node.start_point.row, node.end_point.row)
    return NodeInfo(
        node_type=node.type,
        span=nspan,
        text=text,
        signature=_signature(node, language),
    )


def _signature(node, language: str) -> str | None:
    """Best-effort short label for a definition node (e.g. ``def greet()``).

    Only definition-typed nodes have a meaningful signature header; other node
    types (module, block) return ``None`` rather than a misleading first line.
    """
    if node.type not in _DEFINITION_TYPES:
        return None
    try:
        raw = node.text
        if raw is None:
            return None
        first_line = raw.decode("utf-8", errors="replace").split("\n", 1)[0]
        return first_line.strip() or None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Entity-level structure (survey §3.2 coarse AST / entity merge)
# ---------------------------------------------------------------------------

# Map grammar-specific definition node types to a coarse, language-neutral
# ``kind`` label. Entity identity is (kind, name), so two sides that each add a
# DISTINCT entity at the same insertion point are recognized as non-conflicting
# even when git's line-diff reports a conflict (both insert at one base line).
# "method" vs "function" distinguishes defs inside an impl/class body from
# module-level ones — needed because Rust tracks ``fn`` in ``impl_item``
# separately from free ``function_item``, and Python methods are just
# ``function_definition`` inside a ``class_definition``.
_KIND_BY_NODE_TYPE: dict[str, str] = {
    # Python
    "function_definition": "function",
    "class_definition": "class",
    # Rust
    "function_item": "function",
    "struct_item": "class",
    "enum_item": "class",
    "trait_item": "class",
    "const_item": "field",
    "static_item": "field",
    "type_item": "field",
    # common leaf-ish definitions (e.g. assignments inside class bodies) are
    # NOT mapped — they don't carry a stable name from the node type alone.
}

# Node types whose direct children are the definitions we enumerate (i.e. a
# container). Module root is always a container; class/impl/mod bodies too.
# ``block``/``statement_block`` are the body wrappers Python/some grammars use
# inside class/impl bodies — including them lets the scan reach the methods.
# This is safe because the scan records each definition and does NOT recurse
# into it (a function-body ``block`` is never scanned — its owning function is
# recorded first and ``continue``s).
_CONTAINER_NODE_TYPES = {
    "module",
    "class_definition",
    "impl_item",
    "implementation_list",
    "declaration_list",
    "struct_body",
    "enum_body",
    "trait_body",
    "block",
    "statement_block",
}


def _entity_name(node, language: str) -> str | None:
    """The bare identifier name of a definition node, or None if unresolvable.

    Walks the node's first child for a ``name``/``identifier``-typed token. This
    avoids brittle regex on the source text and matches how tree-sitter exposes
    names across grammars.
    """
    for child in node.children:
        ctype = child.type
        if ctype in ("identifier", "type_identifier", "field_identifier"):
            try:
                return child.text.decode("utf-8", errors="replace") if child.text else None
            except Exception:  # noqa: BLE001
                return None
    return None


def _coerce_kind(node_type: str, parent_type: str | None, language: str) -> str | None:
    """Coarse ``kind`` for a definition node.

    Functions inside an impl/class body are relabeled ``method`` so a method and
    a same-named free function aren't conflated (different identity). Other
    definitions keep their mapped kind. Returns None for unmapped node types
    (we only enumerate stable-nameable top-level defs).
    """
    kind = _KIND_BY_NODE_TYPE.get(node_type)
    if kind is None:
        return None
    if kind == "function" and parent_type in _CONTAINER_NODE_TYPES and parent_type != "module":
        return "method"
    return kind


def enumerate_entities(
    source: str, language: str, container_span: tuple[int, int] | None = None
) -> list[Entity] | None:
    """List the coarse top-level entities in ``source`` (survey §3.2/§5.3).

    Parses ``source`` with tree-sitter and returns one :class:`Entity` per
    definition-typed node that is a *direct child of a container* (module,
    class, impl, mod body). Each carries a coarse ``kind`` (function/method/
    class/field), its ``name``, and exact source ``body`` text — the text-
    carrying leaves the survey prescribes. Identity is ``(kind, name)``.

    ``container_span`` restricts the enumeration to the children of the container
    enclosing that span (the typical case: "what entities live in the same
    class/impl as this conflict?"). When None, the whole module is enumerated.

    Returns ``None`` when tree-sitter is unavailable or parsing fails (callers
    fall back to line-level handling). An empty list means the container had no
    enumeratable entities.
    """
    tree = _parse(source, language)
    if tree is None:
        return None
    root = tree.root_node

    # Resolve the container node to enumerate within.
    container = root  # module
    if container_span is not None:
        anchor = container_span[0]
        # Descend to the deepest container enclosing the anchor, but stop at a
        # definition node (the enclosing class/impl is itself a container).
        node = root
        while True:
            child = None
            for c in node.children:
                if c.start_point.row <= anchor <= c.end_point.row:
                    child = c
                    break
            if child is None or child == node:
                break
            node = child
            if node.type in _CONTAINER_NODE_TYPES:
                container = node
                # Keep descending only while we're in containers; a def node is
                # a leaf for this purpose.
                if node.type in _DEFINITION_TYPES:
                    break
        # If the enclosing container is a definition node (e.g. class/impl),
        # its CHILDREN are the entities, so don't break out yet — but we already
        # set container=node above. For a class/impl, the body list is the child.

    return _collect_entities(container, source, language)


def _collect_entities(container, source: str, language: str) -> list[Entity] | None:
    """Gather direct definition children of ``container`` as entities.

    For class/impl bodies the definitions sit inside a body list node
    (``implementation_list``/``declaration_list``/block); we look one level down
    into that list when present so method/field definitions are found.
    """
    src = source.encode("utf-8")
    entities: list[Entity] = []
    parent_type = container.type

    def _scan(node, ptype):
        for child in node.children:
            ctype = child.type
            kind = _coerce_kind(ctype, ptype, language)
            if kind is not None:
                name = _entity_name(child, language)
                if name:
                    text = src[child.start_byte : child.end_byte].decode("utf-8", errors="replace")
                    entities.append(
                        Entity(
                            kind=kind,
                            name=name,
                            body=text,
                            span=(child.start_point.row, child.end_point.row),
                        )
                    )
                    continue  # don't recurse into a definition we already took
            # Recurse into body-list containers to find nested defs.
            if ctype in _CONTAINER_NODE_TYPES or ctype in _DEFINITION_TYPES:
                _scan(child, ctype)

    _scan(container, parent_type)
    return entities


def duplicate_definitions(
    source: str, language: str
) -> list[tuple[str, str, list[int]]] | None:
    """Find top-level definition names defined more than once PER SCOPE.

    Catches the silent "duplicate block" merge a small model produces when it
    concatenates both sides' versions of a class/struct/function instead of
    merging them — a merge that passes line/token validators because both
    sides' content is present, just twice. ``BothSidesRepresented`` sees every
    distinctive token; this check sees the same ``(kind, name)`` twice in one
    container.

    Scope is the same container notion :func:`enumerate_entities` uses (module,
    class, impl, mod body): a ``fn foo`` in one ``impl`` does not collide with
    ``fn foo`` in another. Collisions are exact ``(kind, name)`` matches, not
    fuzzy — a rename shows up as two distinct names.

    Returns a list of ``(kind, name, line_numbers)`` tuples (one per collided
    name within a scope; ``line_numbers`` are the 1-based start rows of each
    duplicate occurrence, ordered, for repair attribution). ``None`` when
    tree-sitter is unavailable or parsing fails. An empty list means no
    per-scope duplicates were found.
    """
    tree = _parse(source, language)
    if tree is None:
        return None

    findings: list[tuple[str, str, list[int]]] = []

    def _scan_container(node, ptype):
        """Record entities in this container, recursing into nested scopes.

        Mirrors ``_collect_entities._scan`` but keeps a per-container name→rows
        map so a collision is detected within ONE scope only, then descends
        into each child scope separately (so the recursion doesn't flatten
        cross-scope names together).
        """
        seen: dict[tuple[str, str], list[int]] = {}
        for child in node.children:
            ctype = child.type
            kind = _coerce_kind(ctype, ptype, language)
            if kind is not None:
                name = _entity_name(child, language)
                if name:
                    key = (kind, name)
                    # tree-sitter rows are 0-based; report 1-based for messages.
                    seen.setdefault(key, []).append(child.start_point.row + 1)
                    continue  # don't recurse into a definition we already took
            # Recurse into nested scopes (class/impl/mod bodies) separately.
            if ctype in _CONTAINER_NODE_TYPES or ctype in _DEFINITION_TYPES:
                _scan_container(child, ctype)
        for (kind, name), rows in seen.items():
            if len(rows) > 1:
                findings.append((kind, name, sorted(rows)))

    _scan_container(tree.root_node, "module")
    return findings


def sibling_signatures(
    source: str, language: str, container_span: tuple[int, int], *, exclude: str | None = None, limit: int = 8
) -> list[str] | None:
    """Signatures of the OTHER entities co-located in a conflict's container.

    Survey §4.1/§5.4 (Rover): a small LLM merges better when it sees the entity
    neighborhood it must stay consistent with — the sibling methods/fields of the
    class/impl it's merging inside. This returns just their SIGNATURE lines (the
    def/fn/struct header), capped by ``limit`` and excluding the enclosing entity
    itself (``exclude`` = its name) so the model isn't shown the very block it's
    resolving. Bodies are omitted to keep the prompt cheap — the survey's finding
    that *some* structured context helps, distinct from the cross-file callee
    definitions surfaced elsewhere.

    Returns ``None`` when tree-sitter is unavailable; an empty list when the
    container has no other entities.
    """
    ents = enumerate_entities(source, language, container_span=container_span)
    if ents is None:
        return None
    out: list[str] = []
    for e in ents:
        if exclude is not None and e.name == exclude:
            continue
        # The signature is the first line of the body (the def/fn header).
        sig = e.body.split("\n", 1)[0].strip() if e.body else None
        if sig:
            out.append(sig)
        if len(out) >= limit:
            break
    return out


def ast_fingerprint(source: str, language: str) -> str | None:
    """A canonical structural digest of ``source``.

    The fingerprint is the sequence of AST node *types* in pre-order traversal
    (skipping comments and anonymous punctuation), joined by spaces. It is
    invariant under whitespace, comment, and formatting changes — two programs
    with the same structure produce the same fingerprint. Used by
    ``AstPreservationValidator`` to prove that nodes outside a conflict span are
    structurally unchanged after a resolution is spliced in. Returns ``None``
    if parsing is unavailable.
    """
    tree = _parse(source, language)
    if tree is None:
        return None

    parts: list[str] = []

    def walk(n) -> None:
        if _is_useful(n.type):
            parts.append(n.type)
        for c in n.children:
            walk(c)

    walk(tree.root_node)
    return " ".join(parts)


def fingerprint_region(
    source: str, language: str, span: tuple[int, int] | None
) -> tuple[str | None, str | None]:
    """Return (outside_fingerprint, inside_fingerprint) for a span.

    For AST preservation, we compare the structure of nodes OUTSIDE the conflict
    span before and after splicing. ``outside`` is the node-type sequence of all
    nodes that do not fall within ``span``; ``inside`` is the sequence of nodes
    within it. If ``span`` is None, ``outside`` is the whole-file fingerprint and
    ``inside`` is None.
    """
    tree = _parse(source, language)
    if tree is None:
        return None, None
    if span is None:
        return ast_fingerprint(source, language), None
    start_row, end_row = span

    outside: list[str] = []
    inside: list[str] = []

    def walk(n) -> None:
        ns, ne = n.start_point.row, n.end_point.row
        useful = _is_useful(n.type)
        # Node entirely inside the span → inside only.
        if ns >= start_row and ne <= end_row:
            if useful:
                inside.append(n.type)
            return
        # Node entirely outside the span → outside only (recurse).
        if ne < start_row or ns > end_row:
            if useful:
                outside.append(n.type)
            for c in n.children:
                walk(c)
            return
        # Node straddles the span → record its type as outside (structure) and
        # recurse into children to partition them.
        if useful:
            outside.append(n.type)
        for c in n.children:
            walk(c)

    walk(tree.root_node)
    return " ".join(outside), " ".join(inside)


# ---------------------------------------------------------------------------
# Cross-file symbol slicing
# ---------------------------------------------------------------------------


class SymbolResolver(Protocol):
    """Locate definitions of symbols referenced in a conflict block."""

    def resolve(self, names: list[str], language: str) -> list[RelatedSnippet]: ...


def referenced_symbols(text: str, language: str) -> list[str]:
    """Extract likely symbol names referenced in ``text``.

    A coarse, regex-free heuristic: identifiers that look like definitions or
    call targets. Sufficient for the MVP's cross-file slicing; a precise
    resolver would walk the AST and resolve scopes. Deduplicates, preserving
    order. Excludes language keywords.
    """
    import keyword

    out: list[str] = []
    seen: set[str] = set()
    cur = ""
    for ch in text:
        if ch.isalnum() or ch == "_":
            cur += ch
        else:
            if cur and cur not in seen and not keyword.iskeyword(cur):
                # Skip trivially short / all-digit tokens.
                if len(cur) > 1 and not cur.isdigit():
                    out.append(cur)
                    seen.add(cur)
            cur = ""
    if cur and cur not in seen and not keyword.iskeyword(cur):
        if len(cur) > 1 and not cur.isdigit():
            out.append(cur)
    return out


def find_symbol_definitions(
    names: list[str], search_paths: list[str], language: str, *, max_per: int = 1
) -> list[RelatedSnippet]:
    """Search ``search_paths`` for definitions of ``names``.

    Scans files matching the language extension for a definition pattern of each
    name and returns the enclosing logical block as a RelatedSnippet. This is a
    lightweight grep+parse; a precise implementation would use an LSP. Returns
    snippets in (file, name) order, capped at ``max_per`` per name.
    """
    import glob as globmod
    import os
    from capybase.adapters.language import adapter_for

    ext = adapter_for(language).source_extension
    if not ext or not names:
        return []
    snippets: list[RelatedSnippet] = []
    # #13: hard caps to prevent resource exhaustion on large/hostile repos.
    _SKIP_DIRS = {".git", "node_modules", "target", "dist", ".venv", "venv",
                  "__pycache__", "build", ".mypy_cache", ".pytest_cache"}
    _MAX_FILES = 50
    _MAX_FILE_BYTES = 100_000
    files_scanned = 0
    for pat in search_paths:
        for path in globmod.glob(pat, recursive=True):
            # Skip files in generated/vendor directories.
            path_parts = path.replace("\\", "/").split("/")
            if any(d in _SKIP_DIRS for d in path_parts):
                continue
            # Skip symlinks (security + avoids loops).
            if os.path.islink(path):
                continue
            if not os.path.isfile(path) or not path.endswith(ext):
                continue
            # Cap files scanned.
            if files_scanned >= _MAX_FILES:
                break
            files_scanned += 1
            # Skip overly large files.
            try:
                if os.path.getsize(path) > _MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    src = fh.read()
            except Exception:  # noqa: BLE001
                continue
            for name in names:
                if len([s for s in snippets if s.reason == name]) >= max_per:
                    continue
                span = _find_definition_span(src, name, language)
                if span is None:
                    continue
                node = enclosing_node(src, span, language)
                if node is None:
                    continue
                snippets.append(
                    RelatedSnippet(
                        path=path,
                        text=node.text,
                        reason=name,
                    )
                )
    return snippets


def _find_definition_span(source: str, name: str, language: str) -> tuple[int, int] | None:
    """Find the line span of a definition of ``name`` in ``source``.

    Returns the (start, end) row of the first line that looks like a definition
    of ``name``. The keyword patterns (``def name``/``class name`` for Python,
    ``fn name``/``struct name``/... for Rust) come from the language adapter
    (#5) so adding a language is a new adapter, not an edit here.
    """
    from capybase.adapters.language import adapter_for
    pats = tuple(pat.replace("{name}", name) for pat in adapter_for(language).definition_patterns())
    lines = source.split("\n")
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        for pat in pats:
            if stripped.startswith(pat):
                return (i, min(i + 1, len(lines) - 1))
    return None


def is_available(language: str) -> bool:
    """True if tree-sitter and the ``language`` grammar are importable."""
    return _make_parser(language) is not None

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
# Backend selection — abstract parser (default) vs tree-sitter (legacy)
# ---------------------------------------------------------------------------


#: The active parser backend. ``"abstract"`` (default, Round 1): the grammar-free
#: abstract parser in :mod:`capybase.adapters.abstract_parser`. ``"tree_sitter"``:
#: the legacy path via :func:`_parse`, kept as an A/B parity escape hatch before
#: full removal. Tests that need to force a backend set this directly; production
#: reads :attr:`ValidationConfig.parser_backend` (default ``"abstract"``).
_BACKEND_OVERRIDE: str | None = None


def _active_backend(language: str) -> str:
    """The parser backend to use for ``language``.

    Honors ``_BACKEND_OVERRIDE`` (set by tests / the orchestrator) then falls
    back to the default. The abstract parser advertises support for the same
    language set as the legacy path (``python``/``rust`` in Round 1), so
    ``is_available`` agrees across backends and the skip-path tests stay green.
    """
    if _BACKEND_OVERRIDE is not None:
        return _BACKEND_OVERRIDE
    return "abstract"


def set_parser_backend(backend: str | None) -> None:
    """Force the parser backend process-wide (``"abstract"``/``"tree_sitter"``/None).

    Used by the orchestrator to apply ``ValidationConfig.parser_backend`` and by
    tests to pin a backend. ``None`` restores the default (abstract).
    """
    global _BACKEND_OVERRIDE
    _BACKEND_OVERRIDE = backend


def _abstract_parse(source: str, language: str):
    """Parse via the abstract parser; return a :class:`FileIR` or ``None``.

    Thin wrapper that imports lazily so the abstract parser is only loaded when
    needed (mirroring the tree-sitter lazy-import discipline). Returns ``None``
    when the language has no family mapping (same sentinel tree-sitter produces).
    """
    try:
        from capybase.adapters import abstract_parser
    except Exception:  # noqa: BLE001
        return None
    return abstract_parser.parse_file(source, language=language)


def _unit_to_entity(unit) -> Entity:
    """Convert an abstract-parser ``StructuralUnit`` to the public ``Entity``."""
    return Entity(
        kind=unit.kind,
        name=unit.name or "",
        body=unit.body,
        span=unit.span,
    )


def _all_flat_entities(ir) -> list[Entity]:
    """Flatten a FileIR's units (top-level + nested) to ``Entity`` objects."""
    try:
        from capybase.adapters import abstract_parser
    except Exception:  # noqa: BLE001
        return []
    return [_unit_to_entity(u) for u in abstract_parser._all_units_flat(ir)]


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
    # Abstract backend: resolve via FileIR's enclosing_unit (coarse kinds).
    if _active_backend(language) == "abstract":
        return _enclosing_node_abstract(source, span, language)
    return _enclosing_node_tree_sitter(source, span, language)


def _enclosing_node_abstract(
    source: str, span: tuple[int, int], language: str
) -> NodeInfo | None:
    """enclosing_node on the abstract parser's FileIR (coarse kinds)."""
    ir = _abstract_parse(source, language)
    if ir is None:
        return None
    try:
        from capybase.adapters import abstract_parser
    except Exception:  # noqa: BLE001
        return None
    unit = abstract_parser.enclosing_unit(ir, span)
    if unit is None:
        return None
    # Signature = first line of the body (the def/fn header); None when empty.
    first_line = unit.body.split("\n", 1)[0].strip() if unit.body else ""
    return NodeInfo(
        node_type=unit.kind,  # coarse: function/class/method/field/...
        span=unit.span,
        text=unit.body,
        signature=first_line or None,
    )


def _enclosing_node_tree_sitter(
    source: str, span: tuple[int, int], language: str
) -> NodeInfo | None:
    """enclosing_node on the tree-sitter parse (legacy backend)."""
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

    Parses ``source`` and returns one :class:`Entity` per definition-typed unit
    that is a *direct child of a container* (module, class, impl, mod body).
    Each carries a coarse ``kind`` (function/method/class/field), its ``name``,
    and exact source ``body`` text — the text-carrying leaves the survey
    prescribes. Identity is ``(kind, name)``.

    ``container_span`` restricts the enumeration to the children of the container
    enclosing that span (the typical case: "what entities live in the same
    class/impl as this conflict?"). When None, the whole module is enumerated.

    Returns ``None`` when parsing is unavailable or fails (callers fall back to
    line-level handling). An empty list means the container had no enumeratable
    entities.
    """
    if _active_backend(language) == "abstract":
        return _enumerate_entities_abstract(source, language, container_span)
    return _enumerate_entities_tree_sitter(source, language, container_span)


def _enumerate_entities_abstract(
    source: str, language: str, container_span: tuple[int, int] | None
) -> list[Entity] | None:
    """enumerate_entities on the abstract parser's FileIR."""
    ir = _abstract_parse(source, language)
    if ir is None:
        return None
    try:
        from capybase.adapters import abstract_parser
    except Exception:  # noqa: BLE001
        return None

    def to_entities(units) -> list[Entity]:
        # Skip module_stmt (imports) to match tree-sitter's entity set — imports
        # are not definition-typed nodes (not in _KIND_BY_NODE_TYPE), so the
        # existing drop/coverage/unattributed analyzers (calibrated on that set)
        # must not see them. Imports surface via referenced_symbols/
        # find_symbol_definitions, not entity enumeration.
        return [
            _unit_to_entity(u)
            for u in units
            if u.kind != abstract_parser.KIND_MODULE_STMT
        ]

    if container_span is None:
        # Whole module: top-level units only (mirrors tree-sitter's module-root
        # enumeration — methods/fields are children, surfaced only via their
        # parent or a container_span query).
        return to_entities(ir.units)
    # Container-scoped: the siblings inside the container enclosing the span.
    # tree-sitter flattens to the container's direct children; the abstract
    # parser returns the same via units_in_container. For an impl container,
    # those are its methods (the impl itself is container-only and not emitted).
    units = abstract_parser.units_in_container(ir, container_span)
    if not units:
        # Fall back to flattening the enclosing unit's whole subtree if the
        # container query found nothing (e.g. anchor on the class header itself).
        enc = abstract_parser.enclosing_unit(ir, container_span)
        if enc is not None:
            return to_entities([enc] + list(enc.children))
        return []
    return to_entities(units)


def _enumerate_entities_tree_sitter(
    source: str, language: str, container_span: tuple[int, int] | None
) -> list[Entity] | None:
    """enumerate_entities on the tree-sitter parse (legacy backend)."""
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
    duplicate occurrence, ordered, for repair attribution). ``None`` when the
    parser is unavailable or parsing fails. An empty list means no per-scope
    duplicates were found.
    """
    if _active_backend(language) == "abstract":
        return _duplicate_definitions_abstract(source, language)
    return _duplicate_definitions_tree_sitter(source, language)


def _duplicate_definitions_abstract(
    source: str, language: str
) -> list[tuple[str, str, list[int]]] | None:
    """duplicate_definitions on the abstract parser's FileIR.

    Walks each container's direct children, counting ``(kind, name)`` collisions
    per scope (module, then each class/impl body). Reports 1-based start rows.
    """
    ir = _abstract_parse(source, language)
    if ir is None:
        return None
    try:
        from capybase.adapters import abstract_parser
    except Exception:  # noqa: BLE001
        return None

    findings: list[tuple[str, str, list[int]]] = []

    def scan_scope(units: list, rows_out: None = None) -> None:
        seen: dict[tuple[str, str], list[int]] = {}
        for u in units:
            # Container-scope units (impl/mod/namespace) are scopes, NOT entities
            # — they don't collide with a same-named struct/trait at this level,
            # and they're never emitted as entities. Only real entities count.
            if u.is_container_scope or not u.name:
                continue
            key = (u.kind, u.name)
            # 1-based start row for repair attribution (matches tree-sitter path).
            seen.setdefault(key, []).append(u.span[0] + 1)
        for (kind, name), rows in seen.items():
            if len(rows) > 1:
                findings.append((kind, name, sorted(rows)))
        # Recurse into each child's children (nested scopes) separately — a
        # container-scope's children are a distinct scope.
        for u in units:
            if u.children:
                scan_scope(u.children)

    scan_scope(ir.units)
    return findings


def _duplicate_definitions_tree_sitter(
    source: str, language: str
) -> list[tuple[str, str, list[int]]] | None:
    """duplicate_definitions on the tree-sitter parse (legacy backend).

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


#: Match classification for a (source entity → target entity) pair.
#:
#: - ``same_name``: paired by exact ``(kind, name)`` identity.
#: - ``renamed``: paired across DIFFERENT names by body-fingerprint equality or
#:   near-equality (Jaccard ≥ threshold) — the source's old name is gone in target.
#: - ``unmatched``: no counterpart found in target (neither by name nor by body).
#:
#: Produced by :func:`match_entities`; consumed by the analyzers below so a
#: legitimate rename is recognized rather than read as a drop + spurious add.
MATCH_SAME_NAME = "same_name"
MATCH_RENAMED = "renamed"
MATCH_UNMATCHED = "unmatched"


@dataclass(frozen=True)
class EntityMatch:
    """One source entity's classification against a target entity set.

    ``target`` is the paired :class:`Entity` for ``same_name`` / ``renamed`` (the
    counterpart in the target set), or ``None`` for ``unmatched``. For a rename,
    ``target.name`` is the new name. ``source`` is always the original entity.
    """

    source: Entity
    target: Entity | None
    kind: str  # one of the MATCH_* constants above


def match_entities(
    sources: "list[Entity]", targets: "list[Entity]"
) -> list[EntityMatch]:
    """Classify each ``source`` entity against the ``targets`` set.

    Mirrors the rename-pairing logic of :func:`semantic_diff` but returns a
    per-source match record so the analyzers can ask "does this side entity
    survive in the resolution under ANY name?" — recognizing renames instead of
    treating a renamed entity as dropped (old name gone) + unattributed (new name
    novel). A rename requires the source's old name to be GONE from targets (a
    copy is NOT a rename), a body-fingerprint match (exact, or Jaccard ≥ 0.80 for
    a rename-with-edit), AND name-similarity ≥ 0.6 or a substantial body — so two
    distinct entities sharing a trivial body don't false-pair.

    Pure (no parsing); operates on already-enumerated entity lists. Deterministic.
    """
    target_by_name: dict[tuple[str, str], Entity] = {(e.kind, e.name): e for e in targets}
    target_names_by_kind: dict[str, set[str]] = {}
    for e in targets:
        target_names_by_kind.setdefault(e.kind, set()).add(e.name)
    # Index targets by (kind, body-fingerprint) for rename pairing.
    target_by_body: dict[tuple[str, str], Entity] = {}
    target_body_tokens: dict[tuple[str, str], frozenset[str]] = {}
    for e in targets:
        bf = entity_body_fingerprint(e, "") or ""
        if bf:
            key = (e.kind, bf)
            target_by_body.setdefault(key, e)
            target_body_tokens[key] = frozenset(_token_set(bf))

    out: list[EntityMatch] = []
    for src in sources:
        # 1. Exact (kind, name) match.
        exact = target_by_name.get((src.kind, src.name))
        if exact is not None:
            out.append(EntityMatch(source=src, target=exact, kind=MATCH_SAME_NAME))
            continue
        # 2. Rename: body-fingerprint match across a different name.
        bf = entity_body_fingerprint(src, "") or ""
        target: Entity | None = None
        if bf:
            direct = target_by_body.get((src.kind, bf))
            if (
                direct is not None
                and src.name not in target_names_by_kind.get(src.kind, set())
                and (
                    _name_similarity(direct.name, src.name) >= _RENAME_NAME_SIMILARITY_THRESHOLD
                    or _body_is_substantial(bf)
                )
            ):
                target = direct
            else:
                # Jaccard fallback for a rename that also edited the body.
                tk = frozenset(_token_set(bf))
                best: tuple[float, Entity] | None = None
                for key, oks in target_body_tokens.items():
                    if key[0] != src.kind:
                        continue
                    cand = target_by_body[key]
                    if src.name in target_names_by_kind.get(src.kind, set()):
                        break  # source name still present → not a rename
                    inter = len(tk & oks)
                    union = len(tk | oks)
                    if union == 0:
                        continue
                    j = inter / union
                    if (
                        j >= _RENAME_BODY_JACCARD_THRESHOLD
                        and _body_is_substantial(bf)
                        and (best is None or j > best[0])
                    ):
                        best = (j, cand)
                if best is not None:
                    target = best[1]
        if target is not None:
            out.append(EntityMatch(source=src, target=target, kind=MATCH_RENAMED))
        else:
            out.append(EntityMatch(source=src, target=None, kind=MATCH_UNMATCHED))
    return out


def dropped_entities(
    base: str, side: str, resolved: str, language: str
) -> list[Entity] | None:
    """Entities a ``side`` ADDED that are ABSENT from ``resolved``.

    The quantitative per-side preservation signal for the verifier critic and
    the CEGIS retry feedback: instead of a boolean "dropped a side", this lists
    the SPECIFIC logical units (function/method/class/field by name) that the
    side introduced beyond ``base`` and that the resolution dropped — giving the
    model exact targets to reintroduce on retry ("reintroduce: function `foo`,
    class `Bar`") and the LLM judge concrete evidence.

    An entity is "added by the side" if its ``(kind, name)`` identity appears in
    ``side`` but not ``base``. It's "dropped" if that identity is then absent
    from ``resolved``. A renamed-but-present entity counts as preserved (a rename
    is a legitimate merge, not a drop): rename-aware matching (``match_entities``)
    recognizes a side entity whose body content reappears in the resolution under
    a different name, so a legitimate rename does NOT surface as a false drop.
    Module-level bare assignments (``X = ...``) are NOT enumerated (see
    _KIND_BY_NODE_TYPE), so this catches structural defs only; the token-set
    BothSidesRepresented validator remains the backstop for value/assignment drops.

    Returns ``None`` when tree-sitter is unavailable or any of the three texts
    fail to parse (the critic degrades gracefully). An empty list means the side
    added nothing that's missing.
    """
    base_ents = enumerate_entities(base, language)
    side_ents = enumerate_entities(side, language)
    resolved_ents = enumerate_entities(resolved, language)
    if base_ents is None or side_ents is None or resolved_ents is None:
        return None
    base_names = {e.name for e in base_ents}
    # Added by the side = name not in base. A dropped entity is one that's
    # UNMATCHED in the resolution (neither same-name nor a recognized rename),
    # so a legitimate rename survives rather than counting as a false drop.
    matches = match_entities(side_ents, resolved_ents)
    dropped: list[Entity] = []
    for m in matches:
        if m.source.name not in base_names and m.kind == MATCH_UNMATCHED:
            dropped.append(m.source)
    return dropped


@dataclass(frozen=True)
class CoverageReport:
    """Quantitative per-side preservation coverage (survey §5.1 intent signatures).

    Of the ``added`` entities a side introduced beyond ``base``, ``preserved``
    survive in the resolution and ``dropped`` are absent. The ratio
    ``preserved / added`` is the coverage floor the IntentCoverageValidator
    gates on — a hard, deterministic guarantee that no side's structural intent
    is silently lost beyond a configured fraction.
    """

    added: int          # entities the side added beyond base (the denominator)
    preserved: int      # of those, present in the resolution
    dropped: list[Entity]  # of those, absent from the resolution

    @property
    def ratio(self) -> float:
        return self.preserved / self.added if self.added else 1.0


def preservation_coverage(
    base: str, side: str, resolved: str, language: str
) -> CoverageReport | None:
    """How much of a ``side``'s added structural intent survives in ``resolved``.

    The deterministic coverage signal behind the IntentCoverageValidator and the
    hard "no silent drop > X%" guarantee: of the M logical units (function/
    method/class/field) the side ADDED beyond ``base``, how many are present in
    the resolution. A rename counts as preserved (a renamed-but-present entity
    survives under a different name), so it does not lower coverage. Returns a
    :class:`CoverageReport` with the ratio; ``None`` when tree-sitter is
    unavailable or any text fails to parse (coverage undefined, not a failure).
    An ``added == 0`` report means the side added no structural entities
    (ratio 1.0 — nothing to drop).
    """
    base_ents = enumerate_entities(base, language)
    side_ents = enumerate_entities(side, language)
    resolved_ents = enumerate_entities(resolved, language)
    if base_ents is None or side_ents is None or resolved_ents is None:
        return None
    base_names = {e.name for e in base_ents}
    # Added by the side = name not in base. Of those, the ones UNMATCHED in the
    # resolution (neither same-name nor a recognized rename) are dropped; a
    # rename is preserved (survives under a new name) and so not counted dropped.
    matches = match_entities(side_ents, resolved_ents)
    added: list[Entity] = []
    dropped: list[Entity] = []
    for m in matches:
        if m.source.name in base_names:
            continue  # entity present in base → not an "add" by this side
        added.append(m.source)
        if m.kind == MATCH_UNMATCHED:
            dropped.append(m.source)
    return CoverageReport(
        added=len(added),
        preserved=len(added) - len(dropped),
        dropped=dropped,
    )


def unattributed_entities(
    base: str, current: str, replayed: str, resolved: str, language: str
) -> list[Entity] | None:
    """Logical units in ``resolved`` that appear in NONE of the three sides.

    The INVERSE of :func:`dropped_entities` (which finds side-units missing
    from the merge). This catches the spurious-addition failure mode: a unit the
    merge introduces that no side asked for — a hallucinated helper, an extra
    branch, a synthesized function. Every other preservation check is drop-
    directional ("did the merge LOSE a side's unit?"); this is the only check
    for surplus code, completing the "neither dropped nor spurious" guarantee.

    An entity is "unattributed" if it has no counterpart in ANY of base/current/
    replayed — matched by name OR by body fingerprint (a rename). A resolved
    entity that body-matches a side entity under a different name is attributed
    (a legitimate rename, not a hallucination), so it does not flag here. Only a
    unit whose name AND body are both novel — appearing in no side in any form —
    is unattributed. This reduces false positives when the model legitimately
    renames an entity to reconcile the sides.

    Returns ``None`` when tree-sitter is unavailable or any text fails to parse.
    An empty list means every resolved unit derives from (by name or by a
    recognized rename of) a unit in at least one side.
    """
    base_ents = enumerate_entities(base, language)
    cur_ents = enumerate_entities(current, language)
    rep_ents = enumerate_entities(replayed, language)
    res_ents = enumerate_entities(resolved, language)
    if any(x is None for x in (base_ents, cur_ents, rep_ents, res_ents)):
        return None
    # Match each resolved entity against the union of all side entities. A
    # same-name OR rename match (body-fingerprint equal/near) counts as
    # attributed; only an unmatched resolved entity is unattributed.
    sides = list(base_ents) + list(cur_ents) + list(rep_ents)
    matches = match_entities(res_ents, sides)
    return [m.source for m in matches if m.kind == MATCH_UNMATCHED]


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

    The fingerprint is the sequence of structural unit kinds in pre-order
    traversal (the coarse function/class/method/field/... kinds, plus the unit
    names), joined by spaces. It is invariant under whitespace, comment, and
    formatting changes — two programs with the same structure produce the same
    fingerprint. Used by ``AstPreservationValidator`` to prove that nodes outside
    a conflict span are structurally unchanged after a resolution is spliced in.
    Returns ``None`` if parsing is unavailable.
    """
    if _active_backend(language) == "abstract":
        return _ast_fingerprint_abstract(source, language)
    return _ast_fingerprint_tree_sitter(source, language)


def _ast_fingerprint_abstract(source: str, language: str) -> str | None:
    """ast_fingerprint on the abstract parser's FileIR.

    Emits a pre-order walk of ``kind:name:fingerprint`` tokens for every unit
    (top-level + nested children), where ``fingerprint`` is the unit's body
    content digest (stable under whitespace/comments, but sensitive to body
    edits). This captures BOTH structural identity (kind+name) and internal
    structure (the body digest) — so the AstPreservationValidator detects a
    resolution that changes a unit's body, not just one that renames/reorders
    units. Invariant under whitespace/comment/formatting changes.
    """
    ir = _abstract_parse(source, language)
    if ir is None:
        return None
    parts: list[str] = []

    def walk(units: list) -> None:
        for u in units:
            # Container-scope units (impl/mod) are scopes, not entities — emit
            # their structure via their children, not themselves.
            if u.is_container_scope:
                parts.append(f"scope:{u.name or '<anon>'}")
            else:
                parts.append(f"{u.kind}:{u.name or '<anon>'}:{u.fingerprint}")
            if u.children:
                walk(u.children)

    walk(ir.units)
    return " ".join(parts)


def _ast_fingerprint_tree_sitter(source: str, language: str) -> str | None:
    """ast_fingerprint on the tree-sitter parse (legacy backend).

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
    span before and after splicing. ``outside`` is the structural-unit sequence
    of all units that do not fall within ``span``; ``inside`` is the sequence of
    units within it. If ``span`` is None, ``outside`` is the whole-file
    fingerprint and ``inside`` is None.
    """
    if _active_backend(language) == "abstract":
        return _fingerprint_region_abstract(source, language, span)
    return _fingerprint_region_tree_sitter(source, language, span)


def _fingerprint_region_abstract(
    source: str, language: str, span: tuple[int, int] | None
) -> tuple[str | None, str | None]:
    """fingerprint_region on the abstract parser's FileIR."""
    ir = _abstract_parse(source, language)
    if ir is None:
        return None, None
    if span is None:
        return _ast_fingerprint_abstract(source, language), None
    start_row, end_row = span

    def token(u, *, with_body: bool) -> str:
        # Fold the body fingerprint in so body edits are detected (matches
        # _ast_fingerprint_abstract's per-unit token shape) — but ONLY for units
        # entirely outside the span. A unit that STRADDLES the span has its body
        # partially inside the conflict region and will legitimately change after
        # a resolution is spliced in, so it contributes only its kind:name
        # (structural shape, not content) to the outside digest.
        if u.is_container_scope:
            return f"scope:{u.name or '<anon>'}"
        if with_body:
            return f"{u.kind}:{u.name or '<anon>'}:{u.fingerprint}"
        return f"{u.kind}:{u.name or '<anon>'}"

    outside: list[str] = []
    inside: list[str] = []

    def walk(units: list) -> None:
        for u in units:
            ns, ne = u.span
            # Unit entirely inside the span → inside only.
            if ns >= start_row and ne <= end_row:
                inside.append(token(u, with_body=True))
                continue
            # Unit entirely outside the span → outside only, with body fingerprint.
            if ne < start_row or ns > end_row:
                outside.append(token(u, with_body=True))
                if u.children:
                    walk(u.children)
                continue
            # Unit straddles the span → record OUTSIDE with kind:name only (its
            # body is partially inside and will change), recurse to partition kids.
            outside.append(token(u, with_body=False))
            if u.children:
                walk(u.children)

    walk(ir.units)
    return " ".join(outside), " ".join(inside)


def _fingerprint_region_tree_sitter(
    source: str, language: str, span: tuple[int, int] | None
) -> tuple[str | None, str | None]:
    """fingerprint_region on the tree-sitter parse (legacy backend).

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
# Per-entity semantic diff (survey §5 foundational layer)
# ---------------------------------------------------------------------------
#
# The analyzers above (dropped_entities / preservation_coverage /
# unattributed_entities) match entities by EXACT name, so a legitimate rename is
# invisible: a side that renames ``foo``→``bar`` reads as "added bar" (covered)
# while ``dropped_entities`` sees nothing dropped. The whole-file ``ast_fingerprint``
# is name-agnostic (node-type sequence), so it can't pair an entity across names
# either. This section provides the two missing primitives:
#
# 1. ``entity_body_fingerprint`` / ``entity_sig_fingerprint`` — content-aware
#    per-entity digests (body vs signature), normalized so a rename is the ONLY
#    difference between a base entity and its renamed counterpart.
# 2. ``semantic_diff`` — classifies each entity across two snapshots as
#    added / removed / renamed / signature_changed / body_changed, using the
#    fingerprints to pair an entity across names (a rename).
#
# Both build on ``enumerate_entities`` and degrade to ``None`` when tree-sitter is
# unavailable (the same graceful-degradation contract as every analyzer here).

# Token-set Jaccard floor for pairing an entity across names when the body is
# NOT exactly equal (a rename WITH a small edit, e.g. a renamed fn whose body
# also gained a line). Exact body-content equality always pairs first (the strong
# signal from structural_resolver._detect_renames); this is the fallback for
# near-equal bodies. Tuned conservatively — too low conflates distinct entities.
_RENAME_BODY_JACCARD_THRESHOLD = 0.80

# When body-content equality pairs an old→new entity across DIFFERENT names, also
# require either name similarity above this floor OR a non-trivial body — mirrors
# structural_resolver._detect_renames' guard so two distinct entities that happen
# to share a trivial body (``pass`` / ``return 1``) aren't misread as a rename.
_RENAME_NAME_SIMILARITY_THRESHOLD = 0.6


def _name_similarity(a: str, b: str) -> float:
    """String similarity ratio in [0, 1] via difflib (no new dependency)."""
    if not a or not b:
        return 0.0
    import difflib

    return difflib.SequenceMatcher(a=a, b=b, autojunk=False).ratio()


def _body_is_substantial(body_fp: str) -> bool:
    """True when a body has enough content to be a reliable rename signal."""
    return len(body_fp) >= 8


def _split_header_body(entity: Entity) -> tuple[str, str]:
    """Split an entity's body into (header, rest), whitespace-normalized.

    A rename changes the def/fn header (``def foo`` → ``def bar``) but leaves the
    body content identical, so rename detection must compare the header-STRIPPED
    body. Mirrors ``structural_resolver._body_content`` but returns both parts so
    the signature fingerprint can use the header.
    """
    body = entity.body or ""
    if not body:
        return "", ""
    lines = body.split("\n")
    header = lines[0]
    rest = "\n".join(lines[1:])
    return _norm(header), _norm(rest)


def _norm(text: str) -> str:
    """Whitespace-collapse normalization (stable across formatting changes)."""
    return " ".join((text or "").split())


def _token_set(text: str) -> set[str]:
    """The bag of non-whitespace tokens, for Jaccard body comparison."""
    return set((text or "").split())


def entity_body_fingerprint(entity: Entity, language: str) -> str | None:
    """A normalized digest of an entity's body CONTENT (signature-stripped).

    Invariant under whitespace, comment, and formatting changes; RENAME-SENSITIVE
    only in the header (which is stripped) — two functions differing only in name
    produce the SAME body fingerprint, which is what lets ``semantic_diff`` pair a
    renamed entity to its base original. This is the per-entity, content-aware
    counterpart to the name-agnostic whole-file ``ast_fingerprint``.

    Returns the normalized body-without-header; ``None`` is reserved for the
    "tree-sitter unavailable" sentinel at a higher level (an entity already
    enumerated has a parseable body, so "" indicates an empty body, not failure).
    """
    _ = language  # entity.body is exact source; language not needed to split it
    _, rest = _split_header_body(entity)
    return rest


def entity_sig_fingerprint(entity: Entity, language: str) -> str:
    """A normalized digest of an entity's SIGNATURE (kind + name + header).

    Two entities with the same name but different parameter lists differ here, so
    ``semantic_diff`` can flag a ``signature_changed``. The kind is folded in so a
    function→class collision doesn't silently match. The header is the def/fn
    line; the name is included explicitly so a rename (same body, different
    header) is detectable as a header change even when the rest is identical.
    """
    header, _ = _split_header_body(entity)
    return f"{entity.kind}|{entity.name}|{header}"


def _header_sans_name(entity: Entity) -> str:
    """The signature header with the entity's own name removed.

    Two methods with the same body and parameters but DIFFERENT names produce the
    same header-sans-name → strong evidence of a rename rather than an add. Used
    alongside body-fingerprint equality to confirm a rename.
    """
    header, _ = _split_header_body(entity)
    if entity.name and entity.name in header:
        # Remove the bare name token (word-boundary safe) to neutralize the rename.
        import re

        return re.sub(rf"\b{re.escape(entity.name)}\b", "", header)
    return header


@dataclass(frozen=True)
class EntityChange:
    """One classified change between two snapshots of an entity set.

    ``change_type`` is one of ``added`` / ``removed`` / ``renamed`` /
    ``signature_changed`` / ``body_changed`` / ``moved``. For ``renamed``,
    ``old_name``/``new_name`` carry the rename pair; for the rest both equal
    ``name`` (and ``new_name``/``old_name`` are empty when not applicable).
    ``kind`` is the entity's coarse kind (function/class/method/field).
    """

    kind: str
    name: str
    change_type: str
    old_name: str = ""
    new_name: str = ""

    def render(self) -> str:
        """One-line human rendering for prompts/reports."""
        if self.change_type == "renamed":
            return f"renamed `{self.old_name}`→`{self.new_name}` ({self.kind})"
        if self.change_type == "signature_changed":
            return f"signature_changed `{self.name}` ({self.kind})"
        if self.change_type == "body_changed":
            return f"body_changed `{self.name}` ({self.kind})"
        if self.change_type == "added":
            return f"added `{self.name}` ({self.kind})"
        if self.change_type == "removed":
            return f"removed `{self.name}` ({self.kind})"
        if self.change_type == "moved":
            return f"moved `{self.name}` ({self.kind})"
        return f"{self.change_type} `{self.name}` ({self.kind})"


def semantic_diff(
    old_text: str, new_text: str, language: str
) -> list[EntityChange] | None:
    """Classify the entity-level changes between two snapshots (survey §5.1).

    Enumerates entities in ``old_text`` and ``new_text``, then classifies each by
    name-match + body/signature fingerprint:

    - name in ``new`` only → ``added``
    - name in ``old`` only → ``removed``, UNLESS a new entity has the same body
      fingerprint (content-equal) or a near-equal body (Jaccard ≥ threshold) AND
      its old name is gone → ``renamed`` (old_name → new_name)
    - name in both, signature fingerprint differs → ``signature_changed``
    - name in both, signature same but body differs → ``body_changed``

    Returns ``None`` when tree-sitter is unavailable or either text fails to
    parse (callers degrade gracefully). An empty list means no entity-level
    change. ``moved`` (cross-file) is NOT detected here — it requires multi-file
    input (see ``detect_cross_file_moves``).

    The rename-pairing logic generalizes ``structural_resolver._detect_renames``:
    body-content equality is the strong signal, with a Jaccard fallback so a
    rename that also touches the body is still recognized.
    """
    old_ents = enumerate_entities(old_text, language)
    new_ents = enumerate_entities(new_text, language)
    if old_ents is None or new_ents is None:
        return None

    old_by_name: dict[tuple[str, str], Entity] = {(e.kind, e.name): e for e in old_ents}
    new_by_name: dict[tuple[str, str], Entity] = {(e.kind, e.name): e for e in new_ents}

    # Index old entities by (kind, body-fingerprint) for rename pairing.
    old_by_body: dict[tuple[str, str], Entity] = {}
    old_body_tokens: dict[tuple[str, str], frozenset[str]] = {}
    for e in old_ents:
        bf = entity_body_fingerprint(e, language) or ""
        key = (e.kind, bf)
        if bf:  # skip empty bodies (ambiguous)
            old_by_body.setdefault(key, e)
            old_body_tokens[key] = frozenset(_token_set(bf))

    new_names_by_kind: dict[str, set[str]] = {}
    for e in new_ents:
        new_names_by_kind.setdefault(e.kind, set()).add(e.name)

    changes: list[EntityChange] = []
    renamed_old_names: set[tuple[str, str]] = set()

    # Pass 1: classify NEW entities (added / renamed / signature_changed / body_changed).
    for e in new_ents:
        ident = (e.kind, e.name)
        old = old_by_name.get(ident)
        if old is not None:
            # Same name exists in old — classify the modification (if any).
            old_sig = entity_sig_fingerprint(old, language)
            new_sig = entity_sig_fingerprint(e, language)
            if old_sig == new_sig:
                # Signature identical — is the body content different?
                old_body = entity_body_fingerprint(old, language) or ""
                new_body = entity_body_fingerprint(e, language) or ""
                if old_body != new_body:
                    changes.append(EntityChange(
                        kind=e.kind, name=e.name, change_type="body_changed",
                    ))
            else:
                # Signature differs: distinguish a param/signature change from a
                # body-only change via the header with the name neutralized.
                old_hdr = _header_sans_name(old)
                new_hdr = _header_sans_name(e)
                if _norm(old_hdr) != _norm(new_hdr):
                    changes.append(EntityChange(
                        kind=e.kind, name=e.name, change_type="signature_changed",
                    ))
                else:
                    changes.append(EntityChange(
                        kind=e.kind, name=e.name, change_type="body_changed",
                    ))
            continue
        # Name is new → either added OR a rename of an old entity.
        bf = entity_body_fingerprint(e, language) or ""
        rename_target: Entity | None = None
        if bf:
            exact = old_by_body.get((e.kind, bf))
            # Exact body match: the old name must be GONE from new (renamed, not
            # copied), and we need name-similarity OR a substantial body so a
            # trivial shared body (``pass``/``return 1``) doesn't false-pair.
            if (
                exact is not None
                and exact.name not in new_names_by_kind.get(e.kind, set())
                and (
                    _name_similarity(exact.name, e.name) >= _RENAME_NAME_SIMILARITY_THRESHOLD
                    or _body_is_substantial(bf)
                )
            ):
                rename_target = exact
            else:
                # Jaccard fallback: a renamed entity whose body also changed.
                tk = frozenset(_token_set(bf))
                best: tuple[float, Entity] | None = None
                for key, oks in old_body_tokens.items():
                    if key[0] != e.kind:
                        continue
                    old_e = old_by_body[key]
                    # Old name must be gone from new (renamed away, not copied).
                    if old_e.name in new_names_by_kind.get(e.kind, set()):
                        continue
                    inter = len(tk & oks)
                    union = len(tk | oks)
                    if union == 0:
                        continue
                    j = inter / union
                    # Require a substantial body so trivial shared bodies don't
                    # pair across distinct names.
                    if (
                        j >= _RENAME_BODY_JACCARD_THRESHOLD
                        and _body_is_substantial(bf)
                        and (best is None or j > best[0])
                    ):
                        best = (j, old_e)
                if best is not None:
                    rename_target = best[1]
        if rename_target is not None:
            renamed_old_names.add((rename_target.kind, rename_target.name))
            changes.append(EntityChange(
                kind=e.kind, name=e.name, change_type="renamed",
                old_name=rename_target.name, new_name=e.name,
            ))
        else:
            changes.append(EntityChange(
                kind=e.kind, name=e.name, change_type="added",
            ))

    # Pass 2: classify OLD entities not yet accounted for → removed.
    for e in old_ents:
        ident = (e.kind, e.name)
        if ident in renamed_old_names:
            continue  # renamed away (already reported as a rename)
        if ident not in new_by_name:
            changes.append(EntityChange(
                kind=e.kind, name=e.name, change_type="removed",
            ))

    return changes


# ---------------------------------------------------------------------------
# Commit change-type classifier (survey Tier 5 §5.2)
# ---------------------------------------------------------------------------

#: The commit-role labels produced by :func:`classify_commit_change`. Grounds
#: retry-budget decisions and the LLM prompt in the SEMANTIC ROLE of the commit
#: rather than just hunk-size/coverage heuristics.
COMMIT_TEST_ONLY = "test_only"
COMMIT_CONFIG_UPDATE = "config_update"
COMMIT_FEATURE = "feature"
COMMIT_BUGFIX = "bugfix"
COMMIT_REFACTOR = "refactor"
COMMIT_UNKNOWN = "unknown"

#: Test-file path patterns (path-relative basename or directory). A conflict
#: whose path matches AND touches no non-test exports is a ``test_only`` commit.
_TEST_PATH_PATTERNS = (
    "test_", "_test.py", "_test.rs", "/tests/", "tests/", "\\tests\\",
    "spec_", "_spec.py",
)

#: Config-file extensions. A conflict in one of these is a ``config_update``
#: (the change is a value/key edit, not code structure).
_CONFIG_EXTS = (".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".conf", ".env")


def _is_test_path(path: str) -> bool:
    p = path.lower()
    return any(pat in p for pat in _TEST_PATH_PATTERNS)


def _is_config_path(path: str) -> bool:
    p = path.lower()
    dot = p.rfind(".")
    if dot < 0:
        return False
    return p[dot:] in _CONFIG_EXTS


def _is_public_name(name: str) -> bool:
    """A public (non-private) identifier — not ``_``-prefixed (dunder excluded)."""
    if not name:
        return False
    if name.startswith("__") and name.endswith("__"):
        return True  # dunder like __init__ is public API surface
    return not name.startswith("_")


def classify_commit_change(
    base_text: str, replayed_text: str, path: str, language: str
) -> str:
    """Classify the SEMANTIC ROLE of a replayed commit (survey §5.2).

    Determines whether the commit being replayed is a ``test_only`` /
    ``config_update`` / ``feature`` / ``bugfix`` / ``refactor`` change, using
    deterministic rules over the file path + the entity-level ``semantic_diff``
    of BASE→REPLAYED (the replayed side IS the commit being replayed). This
    grounds retry budgets (a bugfix is correctness-critical → more retries; a
    refactor should converge fast) and the LLM prompt ("this commit is a bugfix
    — preserve existing behavior exactly") in the commit's role rather than just
    hunk-size/coverage heuristics.

    Rules, applied in priority order:
    - ``config_update`` — config-file extension, OR no code entities changed
      (pure value/assignment edits).
    - ``test_only`` — a test-file path AND no public exports added/changed.
    - ``feature`` — the diff ADDED a public (non-``_``) entity → new behavior.
    - ``bugfix`` — code touched (body/signature/rename on existing entities),
      no new public exports → modifies existing behavior.
    - ``refactor`` — code touched with no behavior-observable signal (only
      private-member renames / restructuring).
    - ``unknown`` — ``semantic_diff`` unavailable (tree-sitter down / parse fail).

    Pure and deterministic. Never raises — a parse failure degrades to
    ``unknown`` (callers treat ``unknown`` as the neutral default budget).
    """
    # Config files: classify by extension before any parse attempt.
    if _is_config_path(path):
        return COMMIT_CONFIG_UPDATE
    changes = semantic_diff(base_text, replayed_text, language)
    if changes is None:
        # Couldn't parse → if it's a test path, that's a safe structural signal;
        # otherwise we can't tell the role.
        return COMMIT_TEST_ONLY if _is_test_path(path) else COMMIT_UNKNOWN
    if not changes:
        # No entity-level change. If the file is a test file, it's test_only;
        # if it's a config-ish file with no code entities, config_update;
        # otherwise a value-only edit (treat as config_update — no code changed).
        if _is_test_path(path):
            return COMMIT_TEST_ONLY
        return COMMIT_CONFIG_UPDATE

    added_public = any(
        c.change_type == "added"
        and _is_public_name(c.name)
        and not (_is_test_path(path) and c.name.lower().startswith("test"))
        for c in changes
    )
    # A test file that adds/changes only test entities (no public production
    # export) is test_only.
    if _is_test_path(path) and not added_public:
        return COMMIT_TEST_ONLY
    if added_public:
        return COMMIT_FEATURE
    # Code touched but no new public export. Distinguish bugfix (behavior change
    # on existing public/private surface) from refactor (pure restructuring).
    has_body_or_sig = any(
        c.change_type in ("body_changed", "signature_changed") for c in changes
    )
    if has_body_or_sig:
        return COMMIT_BUGFIX
    # Only renames / removals / moves on existing entities → restructuring.
    return COMMIT_REFACTOR


#: Human guidance per commit role, for the LLM prompt. Tells the model what
#: "correct" means for this commit's role (bugfix = preserve behavior; feature =
#: new behavior acceptable; refactor = behavior-preserving).
COMMIT_ROLE_GUIDANCE: dict[str, str] = {
    COMMIT_TEST_ONLY: "test-only change (assertions/coverage)",
    COMMIT_CONFIG_UPDATE: "config/value change (no code behavior)",
    COMMIT_FEATURE: "new feature (new public export — new behavior is expected)",
    COMMIT_BUGFIX: "bugfix (correctness-critical — preserve the existing behavior, fix the defect)",
    COMMIT_REFACTOR: "refactor (behavior-preserving — output must behave identically to the inputs)",
    COMMIT_UNKNOWN: "change (role undetermined)",
}


@dataclass(frozen=True)
class EntityMove:
    """One cross-file entity movement between two file-set snapshots.

    ``name``/``kind`` identify the entity; ``old_path`` is the file it lived in
    before; ``new_path`` is where it now lives. ``new_name`` differs from
    ``name`` only when the move coincided with a rename.
    """

    kind: str
    name: str
    old_path: str
    new_path: str
    new_name: str = ""

    def render(self) -> str:
        nm = self.new_name or self.name
        return f"{self.kind} `{self.name}` moved {self.old_path} → {self.new_path} (now `{nm}`)"


def detect_cross_file_moves(
    old_files: "dict[str, str]",
    new_files: "dict[str, str]",
    language: str,
) -> list[EntityMove] | None:
    """Detect entities that moved from one file to another across snapshots.

    For each entity in an ``old_files`` entry that has NO counterpart (by name)
    in that same file under ``new_files``, search every OTHER new file for a
    body-fingerprint match. A match in a different path is a ``moved`` event —
    the entity relocated rather than being deleted. This catches the case where
    the upstream side reorganized code (e.g. ``auth.py`` → ``auth/core.py``) and
    the replayed side's edits to that entity must apply at the NEW location; the
    LLM, told only the old file, would apply edits to the now-empty old path.

    Args:
        old_files: ``{path: file_text}`` for the base/old snapshot.
        new_files: ``{path: file_text}`` for the current/new snapshot.
        language: the tree-sitter language to enumerate entities in.

    Returns ``None`` when tree-sitter is unavailable (callers degrade). An empty
    list means no cross-file movement was detected. Pure; takes pre-fetched file
    contents so it's testable without a repo. Rename-aware: a moved entity that
    ALSO renamed pairs by body fingerprint across the new name.
    """
    # Build a global index of NEW entities by (kind, body-fingerprint) → (path, Entity).
    new_by_body: dict[tuple[str, str], tuple[str, Entity]] = {}
    for path, text in new_files.items():
        ents = enumerate_entities(text, language)
        if ents is None:
            continue  # parse failure on one file doesn't sink the whole scan
        for e in ents:
            bf = entity_body_fingerprint(e, language) or ""
            if bf:
                new_by_body.setdefault((e.kind, bf), (path, e))

    moves: list[EntityMove] = []
    for old_path, text in old_files.items():
        old_ents = enumerate_entities(text, language)
        if old_ents is None:
            continue
        # The new version of THIS file (if any): entities still here by name
        # are NOT moves; only absent ones are candidates.
        new_same_path = enumerate_entities(new_files.get(old_path, ""), language) or []
        present_names = {e.name for e in new_same_path}
        for e in old_ents:
            if e.name in present_names:
                continue  # still in the same file → not a move
            bf = entity_body_fingerprint(e, language) or ""
            if not bf:
                continue
            hit = new_by_body.get((e.kind, bf))
            if hit is None:
                continue  # no body match anywhere → genuinely removed
            new_path, new_ent = hit
            if new_path == old_path:
                continue  # matched back to the same file (shouldn't happen, guard)
            moves.append(EntityMove(
                kind=e.kind, name=e.name,
                old_path=old_path, new_path=new_path,
                new_name=new_ent.name if new_ent.name != e.name else "",
            ))
    return moves


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
    """True if a structural parser is available for ``language``.

    Honors the active backend (default ``abstract``): the abstract parser is
    available for the Family-A/Family-B languages it dispatches on (in Round 1,
    ``python`` and ``rust`` — the same surface the legacy tree-sitter path
    covered, so skip-path tests stay green). For the legacy backend this probes
    tree-sitter + the grammar wheel.
    """
    backend = _active_backend(language)
    if backend == "tree_sitter":
        return _make_parser(language) is not None
    # abstract backend: available when the language maps to a family AND it's in
    # the supported set (Round 1 keeps python/rust; broader coverage is Round 3).
    try:
        from capybase.adapters import abstract_parser
    except Exception:  # noqa: BLE001
        return False
    return (
        abstract_parser.detect_family(language, None) is not None
        and language in abstract_parser._SUPPORTED_LANGUAGES
    )

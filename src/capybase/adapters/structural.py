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

    Imports are deferred so capybase works without the ``structural`` extra.
    """
    try:
        from tree_sitter import Language
    except Exception:  # noqa: BLE001
        return None
    try:
        if language == "python":
            import tree_sitter_python

            return Language(tree_sitter_python.language())
        if language == "rust":
            import tree_sitter_rust

            return Language(tree_sitter_rust.language())
    except Exception:  # noqa: BLE001
        return None
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
# Structural fingerprint (for AST preservation checks)
# ---------------------------------------------------------------------------


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

    ext = {"python": ".py", "rust": ".rs"}.get(language, "")
    if not ext or not names:
        return []
    snippets: list[RelatedSnippet] = []
    for pat in search_paths:
        for path in globmod.glob(pat, recursive=True):
            if not os.path.isfile(path) or not path.endswith(ext):
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
    of ``name``. Coarse: matches ``def name``/``class name`` (Python) or
    ``fn name``/``struct name``/``enum name``/``trait name``/``mod name``
    (Rust) at line start (after optional whitespace).
    """
    patterns: dict[str, tuple[str, ...]] = {
        "python": (f"def {name}", f"class {name}", f"{name} ="),
        "rust": (
            f"fn {name}",
            f"struct {name}",
            f"enum {name}",
            f"trait {name}",
            f"mod {name}",
            f"const {name}",
            f"static {name}",
        ),
    }
    pats = patterns.get(language, ())
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

"""Pluggable language adapters (#5): language-specific behavior behind one interface.

The verifier, structural analyzer, consensus ranker, and context builder each
carry language-specific logic (comment syntax, source extension, definition
patterns, grammar loading). Historically that lived as scattered
``if language == "python"`` / ``== "rust"`` conditionals — duplicated across
modules (the comment-prefix decision alone had three copies). This module gives
that logic a single home so adding a language is a new adapter, not edits to the
verifier/orchestrator.

Scope — the PURE, low-risk behaviors first (this phase):
- ``comment_prefix`` / ``comment_line_prefixes`` — the `//` vs `#` decision
  (collapses the three duplicated implementations).
- ``source_extension`` — ``.py`` / ``.rs`` (used by structural symbol resolution).
- ``definition_patterns`` — the ``def {name}`` / ``fn {name}`` keyword prefixes.
- ``container_has_braces`` — whether a container body ends in ``}`` (Rust) or not
  (Python), used by the structural resolver's trailer logic.
- ``tree_sitter_language`` — deprecated; always returns ``None``. The abstract
  parser (:mod:`capybase.adapters.abstract_parser`) is the sole structural
  backend. Retained on the Protocol for API compatibility.

The I/O-heavy behaviors (syntax_check / cargo check / LSP / clippy / shadow
tests) stay in their existing helpers for now — they're deeply interleaved with
repo/path context and the diagnostic-delta machinery, so migrating them is a
separate, behavior-preservation-gated phase. The registry is the seam they'll
dispatch through when that migration lands.

A ``LanguageAdapterRegistry`` is keyed by the language string
:func:`conflict_extractor.detect_language` produces. Unsupported languages get
a ``NullAdapter`` (pure no-ops / safe defaults) so every caller can dispatch
unconditionally without a None-check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


#: The single source of truth for file-extension → language-name mapping (fix #11).
#: The union of the two previously-divergent maps (conflict_extractor._EXT_LANG
#: and abstract_parser._EXT_LANG). Both ``conflict_extractor.detect_language``
#: and the abstract parser's family dispatch read from this so a newly-added
#: extension is recognized everywhere — previously the two maps disagreed (e.g.
#: ``.cc``/``.kt``/``.swift`` were parser-known but extractor-unknown, and
#: ``.rb``/``.sh``/``.json`` were extractor-known but parser-unknown).
EXTENSION_TO_LANGUAGE: dict[str, str] = {
    # Family B (indentation-delimited)
    ".py": "python",
    ".rb": "ruby",
    # Family A (brace-delimited)
    ".rs": "rust",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".cs": "csharp",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".swift": "swift",
    ".scala": "scala",
    ".dart": "dart",
    ".php": "php",
    # Non-source (text/config) — recognized by the extractor for language tagging
    # but not structurally parseable (no family mapping in the abstract parser).
    ".sh": "shell",
    ".bash": "shell",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
}


class LanguageAdapter(Protocol):
    """The pure, language-specific behavior a conflict/verification path needs.

    Every method is a pure query (no I/O). Adapters are stateless values; the
    registry hands out the right one by language string.
    """

    name: str

    @property
    def comment_prefix(self) -> str:
        """The line-comment prefix (``#`` / ``//``). Used to blank conflict
        markers without breaking the surrounding syntax."""
        ...

    @property
    def comment_line_prefixes(self) -> tuple[str, ...]:
        """All prefixes a stripped line can start with to count as a comment
        (``#``; ``//``, ``/*``, ``*``, ``*/`` for brace-comment languages).
        Used by consensus ranking + context windowing to recognize comment lines."""
        ...

    @property
    def source_extension(self) -> str:
        """The canonical source extension (``.py`` / ``.rs``). Used when a helper
        needs to synthesize a file path for a symbol-definition search."""
        ...

    def definition_patterns(self) -> tuple[str, ...]:
        """The keyword patterns a top-level definition matches against, with
        ``{name}`` as the placeholder (``def {name}``, ``fn {name}``, ...).
        Used by structural symbol resolution to locate a definition's span."""
        ...

    @property
    def container_has_braces(self) -> bool:
        """Whether a container body (class/impl/struct) is brace-delimited.
        Rust: yes (a ``}`` trailer); Python: no (indentation-delimited)."""
        ...

    def tree_sitter_language(self) -> Any:
        """Deprecated: tree-sitter is no longer used. Always returns ``None``.

        Retained on the Protocol for API compatibility; the abstract parser
        (:mod:`capybase.adapters.abstract_parser`) is the sole structural
        backend. Callers should not depend on a non-None return."""
        ...


@dataclass(frozen=True)
class _BaseAdapter:
    """Shared base for the concrete adapters (frozen value objects).

    Carries the deprecated ``tree_sitter_language`` (always None) so every
    subclass inherits it without redefining — the abstract parser is the sole
    structural backend. Fix #12: registering adapters for all parser-supported
    languages means callers no longer get the wrong-comment-prefix NullAdapter
    fallback (``#``) for JS/TS/Go/Java/C/C++/... — they get the correct ``//``.
    """

    name: str
    comment_prefix: str
    comment_line_prefixes: tuple[str, ...]
    source_extension: str
    _definition_patterns: tuple[str, ...]
    container_has_braces: bool

    def definition_patterns(self) -> tuple[str, ...]:
        return self._definition_patterns

    def tree_sitter_language(self) -> Any:
        # Deprecated: tree-sitter is no longer used. The abstract parser is the
        # sole structural backend. Returns None for API compatibility.
        return None


@dataclass(frozen=True)
class PythonAdapter(_BaseAdapter):
    """The Python language adapter."""

    def __init__(self) -> None:
        super().__init__(
            name="python",
            comment_prefix="#",
            comment_line_prefixes=("#",),
            source_extension=".py",
            _definition_patterns=("def {name}", "class {name}", "{name} ="),
            container_has_braces=False,
        )


@dataclass(frozen=True)
class RustAdapter(_BaseAdapter):
    """The Rust language adapter."""

    def __init__(self) -> None:
        super().__init__(
            name="rust",
            comment_prefix="//",
            # Superset of the three prior implementations (includes `*/`).
            comment_line_prefixes=("//", "/*", "*", "*/"),
            source_extension=".rs",
            _definition_patterns=(
                "fn {name}", "struct {name}", "enum {name}", "trait {name}",
                "mod {name}", "const {name}", "static {name}",
            ),
            container_has_braces=True,
        )


#: Per-language configuration for the brace-family adapters (fix #12). Each
#: entry is the keyword set for ``definition_patterns``; the rest (``//``
#: comment prefix, ``container_has_braces=True``) is shared. Languages not
#: listed here but with a parser family still get a sensible default via
#: ``_BraceLangAdapter``. Keys are the language strings the parser produces.
_BRACE_LANG_DEFINITION_PATTERNS: dict[str, tuple[str, ...]] = {
    "javascript": ("function {name}", "class {name}", "const {name}", "let {name}", "var {name}"),
    "typescript": ("function {name}", "class {name}", "const {name}", "let {name}", "interface {name}", "type {name}"),
    "go": ("func {name}", "type {name}", "var {name}", "const {name}"),
    "java": ("class {name}", "interface {name}", "enum {name}", "void {name}", "public {name}", "private {name}", "protected {name}", "static {name}"),
    "c": ("void {name}", "int {name}", "char {name}", "double {name}", "float {name}", "struct {name}", "static {name}"),
    "cpp": ("void {name}", "int {name}", "char {name}", "double {name}", "float {name}", "struct {name}", "class {name}", "template {name}"),
    "csharp": ("void {name}", "public {name}", "private {name}", "protected {name}", "static {name}", "class {name}", "interface {name}"),
    "kotlin": ("fun {name}", "class {name}", "object {name}", "interface {name}", "val {name}", "var {name}"),
    "swift": ("func {name}", "class {name}", "struct {name}", "enum {name}", "protocol {name}", "let {name}", "var {name}"),
    "scala": ("def {name}", "class {name}", "object {name}", "trait {name}", "val {name}", "var {name}"),
    "dart": ("void {name}", "class {name}", "enum {name}", "final {name}", "const {name}", "var {name}"),
    "php": ("function {name}", "class {name}", "interface {name}", "trait {name}", "const {name}"),
}

#: Per-language source extension for the brace-family adapters. Falls back to
#: the reverse of ``EXTENSION_TO_LANGUAGE`` when a language isn't listed.
_BRACE_LANG_EXTENSIONS: dict[str, str] = {
    "javascript": ".js",
    "typescript": ".ts",
    "go": ".go",
    "java": ".java",
    "c": ".c",
    "cpp": ".cpp",
    "csharp": ".cs",
    "kotlin": ".kt",
    "swift": ".swift",
    "scala": ".scala",
    "dart": ".dart",
    "php": ".php",
}


@dataclass(frozen=True)
class _BraceLangAdapter(_BaseAdapter):
    """A brace-delimited (Family A) language adapter (fix #12).

    One class serves all the C-syntax-family languages the parser supports
    (JS/TS/Go/Java/C/C++/C#/Kotlin/Swift/Scala/Dart/PHP): they share ``//``
    line comments, ``/* */`` block comments, and brace-delimited containers,
    differing only in their definition keywords and source extension. Before
    this, every non-Python/Rust language got the NullAdapter (``comment_prefix
    '#'`` — wrong for all of them, which use ``//``), silently breaking
    comment-line detection in consensus/context-building and symbol search.
    """

    def __init__(self, name: str) -> None:
        patterns = _BRACE_LANG_DEFINITION_PATTERNS.get(name, ())
        ext = _BRACE_LANG_EXTENSIONS.get(name, "")
        super().__init__(
            name=name,
            comment_prefix="//",
            comment_line_prefixes=("//", "/*", "*", "*/"),
            source_extension=ext,
            _definition_patterns=patterns,
            container_has_braces=True,
        )


@dataclass(frozen=True)
class NullAdapter:
    """A safe no-op adapter for unsupported languages.

    Every method returns a safe default so callers dispatch unconditionally
    (no None-check): the comment prefix is ``#`` (the most common), comment-line
    recognition is conservative, definition patterns empty (no symbol search),
    no grammar. This preserves the old behavior where unknown languages were
    treated as text-only with `#` comments.
    """

    name: str = "text"

    @property
    def comment_prefix(self) -> str:
        return "#"

    @property
    def comment_line_prefixes(self) -> tuple[str, ...]:
        return ("#", "//")

    @property
    def source_extension(self) -> str:
        return ""

    def definition_patterns(self) -> tuple[str, ...]:
        return ()

    @property
    def container_has_braces(self) -> bool:
        return False

    def tree_sitter_language(self) -> Any:
        return None


class LanguageAdapterRegistry:
    """Maps a language string to its :class:`LanguageAdapter`.

    Constructed once with the built-in adapters; :meth:`get` returns the
    matching adapter or :class:`NullAdapter` for unknown languages. Adding a
    language is :meth:`register` (or a new adapter class + a registration line),
    not edits scattered across the verifier.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, LanguageAdapter] = {}
        self._null = NullAdapter()

    def register(self, adapter: LanguageAdapter) -> None:
        self._adapters[adapter.name] = adapter

    def get(self, language: str | None) -> LanguageAdapter:
        """The adapter for ``language``, or the NullAdapter when unsupported/None."""
        if language is None:
            return self._null
        return self._adapters.get(language, self._null)

    @property
    def supported(self) -> tuple[str, ...]:
        """The registered (non-null) language names."""
        return tuple(self._adapters)


# The process-wide default registry. Built-ins are registered at import; tests
# and callers use :func:`adapter_for` for the common case. A future phase wires
# the I/O-heavy behaviors (syntax_check / LSP / clippy) to dispatch through here.
#
# Fix #12: register adapters for every language the abstract parser supports.
# Before this, only python/rust had adapters; every other parser-supported
# language (JS/TS/Go/Java/C/C++/C#/Kotlin/Swift/Scala/Dart/PHP) fell through to
# NullAdapter, whose comment_prefix is '#' — wrong for all brace languages,
# which use '//'. This silently broke comment-line detection in consensus
# ranking and context building, and definition-span symbol search.
_REGISTRY = LanguageAdapterRegistry()
_REGISTRY.register(PythonAdapter())
_REGISTRY.register(RustAdapter())
for _lang in _BRACE_LANG_DEFINITION_PATTERNS:
    _REGISTRY.register(_BraceLangAdapter(_lang))


def adapter_for(language: str | None) -> LanguageAdapter:
    """The :class:`LanguageAdapter` for ``language`` (or NullAdapter)."""
    return _REGISTRY.get(language)


def registry() -> LanguageAdapterRegistry:
    """The process-wide registry (for registration / inspection)."""
    return _REGISTRY

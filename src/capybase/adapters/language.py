"""Pluggable language adapters (#5): language-specific behavior behind one interface.

The verifier, structural analyzer, consensus ranker, and context builder each
carry language-specific logic (comment syntax, source extension, definition
patterns, grammar loading). Historically that lived as scattered
``if language == "python"`` / ``== "rust"`` conditionals — duplicated across
modules (the comment-prefix decision alone had three copies). This module gives
that logic a single home so adding a language is a new adapter, not edits to the
verifier/orchestrator.

Scope — the pure, low-risk behaviors the registry consolidates:
- ``comment_prefix`` / ``comment_line_prefixes`` — the `//` vs `#` decision
  (collapses the three duplicated implementations).
- ``source_extension`` — ``.py`` / ``.rs`` (used by structural symbol resolution).
- ``definition_patterns`` — the ``def {name}`` / ``fn {name}`` keyword prefixes.
- ``container_has_braces`` — whether a container body ends in ``}`` (Rust) or not
  (Python), used by the structural resolver's trailer logic.
- ``tree_sitter_language`` — deprecated; always returns ``None``. The abstract
  parser (:mod:`capybase.adapters.abstract_parser`) is the sole structural
  backend. Retained on the Protocol for API compatibility (see
  ``tests/test_language_adapter.py``).

The I/O-heavy behaviors (syntax_check / cargo check / LSP / clippy / shadow
tests) stay in their existing helpers — they're deeply interleaved with
repo/path context and the diagnostic-delta machinery, so they are not plumbed
through the registry.

A ``LanguageAdapterRegistry`` is keyed by the language string
:func:`conflict_extractor.detect_language` produces. Unsupported languages get
a ``NullAdapter`` (pure no-ops / safe defaults) so every caller can dispatch
unconditionally without a None-check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


# ---------------------------------------------------------------------------
# Language catalog — the single literal source of truth
# ---------------------------------------------------------------------------
#
# Every language capybase knows about lives here ONCE, as a ``LanguageSpec``.
# The extension map (``EXTENSION_TO_LANGUAGE``), the family map
# (``abstract_parser._LANG_FAMILY``), and the Family-A set
# (``string_lexer._FAMILY_A_LANGS``) are all DERIVED from this catalog — they
# used to be three hand-maintained literals that had drifted (``rs``/
# ``golang`` aliases were in the string-lexer set but not the family map, so
# ``detect_family("rs")`` returned None while ``_lang_uses_slash_comments("rs")``
# returned True). Deriving from one literal makes that class of drift
# impossible: adding a language is one entry here, and every view updates.
#
# ``family`` is ``"A"`` (brace-delimited, ``//`` comments) or ``"B"``
# (indentation-delimited, ``#`` comments). ``None`` marks non-source languages
# (text/config) that the extractor tags but the structural parser does not
# handle — they have no family and never participate in brace/comment dispatch.

@dataclass(frozen=True)
class LanguageSpec:
    language_id: str
    family: str | None
    aliases: frozenset[str] = field(default_factory=frozenset)
    extensions: frozenset[str] = field(default_factory=frozenset)


_LANGUAGE_CATALOG: tuple[LanguageSpec, ...] = (
    # Family B (indentation-delimited, ``#`` comments)
    LanguageSpec("python", "B", frozenset(), frozenset({".py"})),
    LanguageSpec("ruby", "B", frozenset(), frozenset({".rb"})),
    # Family A (brace-delimited, ``//`` comments)
    LanguageSpec("rust", "A", frozenset({"rs"}), frozenset({".rs"})),
    LanguageSpec(
        "javascript", "A",
        frozenset({"js", "jsx"}),
        frozenset({".js", ".mjs", ".cjs", ".jsx"}),
    ),
    LanguageSpec(
        "typescript", "A",
        frozenset({"ts", "tsx"}),
        frozenset({".ts", ".tsx"}),
    ),
    LanguageSpec("go", "A", frozenset({"golang"}), frozenset({".go"})),
    LanguageSpec("java", "A", frozenset(), frozenset({".java"})),
    LanguageSpec("c", "A", frozenset(), frozenset({".c", ".h"})),
    LanguageSpec(
        "cpp", "A", frozenset({"c++"}),
        frozenset({".cpp", ".cc", ".cxx", ".hpp", ".hh"}),
    ),
    LanguageSpec("csharp", "A", frozenset({"cs"}), frozenset({".cs"})),
    LanguageSpec("kotlin", "A", frozenset(), frozenset({".kt", ".kts"})),
    LanguageSpec("swift", "A", frozenset(), frozenset({".swift"})),
    LanguageSpec("scala", "A", frozenset(), frozenset({".scala"})),
    LanguageSpec("dart", "A", frozenset(), frozenset({".dart"})),
    LanguageSpec("php", "A", frozenset(), frozenset({".php"})),
    # Non-source (text/config) — tagged by the extractor, not structurally parsed
    LanguageSpec("shell", None, frozenset(), frozenset({".sh", ".bash"})),
    LanguageSpec("json", None, frozenset(), frozenset({".json"})),
    LanguageSpec("yaml", None, frozenset(), frozenset({".yaml", ".yml"})),
    LanguageSpec("toml", None, frozenset(), frozenset({".toml"})),
    LanguageSpec("markdown", None, frozenset(), frozenset({".md"})),
)


@dataclass(frozen=True)
class _DerivedViews:
    """Materialized lookup tables built once from ``_LANGUAGE_CATALOG``."""
    extension_to_language: dict[str, str]
    # language name OR alias → family. Includes both canonical names and aliases
    # as keys so ``detect_family("rs")`` resolves the same as
    # ``detect_family("rust")``.
    name_or_alias_to_family: dict[str, str]
    family_a_langs: frozenset[str]

    @classmethod
    def build(cls, catalog: tuple[LanguageSpec, ...]) -> "_DerivedViews":
        ext: dict[str, str] = {}
        family_map: dict[str, str] = {}
        family_a: set[str] = set()
        seen_aliases: set[str] = set()
        seen_exts: set[str] = set()
        for spec in catalog:
            # Canonical name as a key in every view.
            if spec.language_id in family_map:
                raise ValueError(
                    f"duplicate language_id {spec.language_id!r} in catalog"
                )
            if spec.family is not None:
                family_map[spec.language_id] = spec.family
                if spec.family == "A":
                    family_a.add(spec.language_id)
            # Aliases as additional keys.
            for alias in spec.aliases:
                if alias in seen_aliases or alias in family_map:
                    raise ValueError(
                        f"alias {alias!r} collides with another language or alias"
                    )
                seen_aliases.add(alias)
                if spec.family is not None:
                    family_map[alias] = spec.family
                    if spec.family == "A":
                        family_a.add(alias)
            # Extensions.
            for e in spec.extensions:
                if e in seen_exts:
                    raise ValueError(
                        f"extension {e!r} claimed by two languages in catalog"
                    )
                seen_exts.add(e)
                ext[e] = spec.language_id
        return cls(ext, family_map, frozenset(family_a))


_DERIVED = _DerivedViews.build(_LANGUAGE_CATALOG)


#: The single source of truth for file-extension → language-name mapping.
#: Derived from ``_LANGUAGE_CATALOG``; previously a hand-maintained literal that
#: could drift from the family maps. ``conflict_extractor.detect_language`` and
#: the abstract parser's family dispatch both read this.
EXTENSION_TO_LANGUAGE: dict[str, str] = _DERIVED.extension_to_language


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
            # NOTE: ``*`` is intentionally NOT a comment-line prefix. It was
            # meant to catch ``/* */`` block-comment continuation lines, but it
            # also matched valid pointer dereferences (``*p = 5;``) — silently
            # dropping code from normalized output / the model's context window.
            comment_line_prefixes=("//", "/*", "*/"),
            source_extension=".rs",
            _definition_patterns=(
                "fn {name}", "struct {name}", "enum {name}", "trait {name}",
                "mod {name}", "const {name}", "static {name}",
            ),
            container_has_braces=True,
        )


#: Per-language configuration for the brace-family adapters. Each
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
    """A brace-delimited (Family A) language adapter.

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
            # NOTE: ``*`` is intentionally NOT a comment-line prefix. It was
            # meant to catch ``/* */`` block-comment continuation lines
            # (`` * foo``), but it also matched valid pointer dereferences
            # (``*p = 5;``) and multi-line multiplications — silently dropping
            # code from normalized output / the model's context window. A bare
            # ``*``-leading line is far more often code than a comment.
            comment_line_prefixes=("//", "/*", "*/"),
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
        # Reuse-design P0: unknown text has NO assumed comment syntax —
        # treating # or // as comments can silently mask code as comments
        # (the proposal's NullAdapter finding). Callers use
        # startswith(prefixes); an empty tuple means no line is a comment.
        return ()

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
    matching adapter or :class:`NullAdapter` for unknown languages. Adding
    a language is :meth:`register` (or a new adapter class + a registration line),
    not edits scattered across the verifier.

    Short-form aliases (``js``, ``ts``, ``c++``, ``cs``, ...) are normalized to
    their canonical language before lookup, so a caller passing the short form
    gets the correct adapter (not NullAdapter with the wrong ``comment_prefix``).
    """

    #: Short-form aliases → canonical language name. Mirrors the aliases in
    #: ``abstract_parser._LANG_FAMILY`` so every parser-supported language
    #: resolves to a real adapter.
    _ALIASES: dict[str, str] = {
        "js": "javascript",
        "ts": "typescript",
        "jsx": "javascript",
        "tsx": "typescript",
        "c++": "cpp",
        "cs": "csharp",
    }

    def __init__(self) -> None:
        self._adapters: dict[str, LanguageAdapter] = {}
        self._null = NullAdapter()

    def register(self, adapter: LanguageAdapter) -> None:
        self._adapters[adapter.name] = adapter

    def _canonicalize(self, language: str) -> str:
        """Resolve a (possibly aliased) language name to its canonical form."""
        return self._ALIASES.get(language, language)

    def get(self, language: str | None) -> LanguageAdapter:
        """The adapter for ``language``, or the NullAdapter when unsupported/None."""
        if language is None:
            return self._null
        canonical = self._canonicalize(language)
        return self._adapters.get(canonical, self._null)

    @property
    def supported(self) -> tuple[str, ...]:
        """The registered (non-null) language names."""
        return tuple(self._adapters)


# The process-wide default registry. Built-ins are registered at import; tests
# and callers use :func:`adapter_for` for the common case. The I/O-heavy
# behaviors (syntax_check / LSP / clippy) dispatch through their existing
# helpers, not the registry.
#
# Adapters are registered for every language the abstract parser supports.
# Registering only python/rust would leave every other parser-supported language
# (JS/TS/Go/Java/C/C++/C#/Kotlin/Swift/Scala/Dart/PHP) falling through to
# NullAdapter, whose comment_prefix is '#' — wrong for all brace languages,
# which use '//'. That would silently break comment-line detection in consensus
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

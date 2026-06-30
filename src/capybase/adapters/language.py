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
- ``tree_sitter_language`` — lazy grammar loading (delegates to structural).

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
        """The tree-sitter ``Language`` for this adapter's grammar, or None when
        the grammar isn't installed. Lazily imported so capybase works without
        the ``structural`` extra."""
        ...


@dataclass(frozen=True)
class _BaseAdapter:
    """Shared base for the concrete adapters (frozen value objects)."""

    name: str
    comment_prefix: str
    comment_line_prefixes: tuple[str, ...]
    source_extension: str
    _definition_patterns: tuple[str, ...]
    container_has_braces: bool

    def definition_patterns(self) -> tuple[str, ...]:
        return self._definition_patterns


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

    def tree_sitter_language(self) -> Any:
        try:
            from tree_sitter import Language
            from tree_sitter_python import language as _py_lang
            return Language(_py_lang())
        except Exception:  # noqa: BLE001 - optional grammar
            return None


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

    def tree_sitter_language(self) -> Any:
        try:
            from tree_sitter import Language
            from tree_sitter_rust import language as _rust_lang
            return Language(_rust_lang())
        except Exception:  # noqa: BLE001 - optional grammar
            return None


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
_REGISTRY = LanguageAdapterRegistry()
_REGISTRY.register(PythonAdapter())
_REGISTRY.register(RustAdapter())


def adapter_for(language: str | None) -> LanguageAdapter:
    """The :class:`LanguageAdapter` for ``language`` (or NullAdapter)."""
    return _REGISTRY.get(language)


def registry() -> LanguageAdapterRegistry:
    """The process-wide registry (for registration / inspection)."""
    return _REGISTRY

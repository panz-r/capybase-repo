"""Tests for the pluggable language-adapter registry (#5).

The adapter gives language-specific behavior (comment syntax, source extension,
definition patterns, grammar, brace-delimited containers) a single home. These
cover the registry + the two built-in adapters + the NullAdapter fallback, and
that the three formerly-duplicated comment-syntax call sites (verification,
consensus, context_builder) now agree through the adapter.
"""

from __future__ import annotations

from capybase.adapters.language import (
    LanguageAdapterRegistry,
    NullAdapter,
    PythonAdapter,
    RustAdapter,
    adapter_for,
    registry,
)


# ---------------------------------------------------------------------------
# Registry + built-in adapters
# ---------------------------------------------------------------------------


def test_registry_has_python_and_rust():
    assert "python" in registry().supported
    assert "rust" in registry().supported


def test_adapter_for_returns_the_right_adapter():
    assert isinstance(adapter_for("python"), PythonAdapter)
    assert isinstance(adapter_for("rust"), RustAdapter)


def test_adapter_for_unknown_returns_null():
    """Unknown / None languages get the NullAdapter (safe defaults), not None."""
    assert isinstance(adapter_for("go"), NullAdapter)
    assert isinstance(adapter_for(None), NullAdapter)


def test_python_adapter_shape():
    py = adapter_for("python")
    assert py.comment_prefix == "#"
    assert py.comment_line_prefixes == ("#",)
    assert py.source_extension == ".py"
    assert py.container_has_braces is False
    assert "def {name}" in py.definition_patterns()
    assert "class {name}" in py.definition_patterns()


def test_rust_adapter_shape():
    rust = adapter_for("rust")
    assert rust.comment_prefix == "//"
    assert rust.comment_line_prefixes == ("//", "/*", "*", "*/")
    assert rust.source_extension == ".rs"
    assert rust.container_has_braces is True
    assert "fn {name}" in rust.definition_patterns()
    assert "struct {name}" in rust.definition_patterns()


def test_null_adapter_safe_defaults():
    null = adapter_for("text")
    assert null.comment_prefix == "#"
    assert null.definition_patterns() == ()
    assert null.source_extension == ""
    assert null.container_has_braces is False
    assert null.tree_sitter_language() is None


def test_tree_sitter_language_returns_none_after_migration():
    """tree-sitter is deprecated; the abstract parser is the sole backend.

    The method is retained on the Protocol for API compatibility but always
    returns None — no grammar loading, no optional dependency."""
    py = adapter_for("python")
    assert py.tree_sitter_language() is None
    rs = adapter_for("rust")
    assert rs.tree_sitter_language() is None


# ---------------------------------------------------------------------------
# Registration: adding a language is a new adapter, not verifier edits
# ---------------------------------------------------------------------------


def test_register_adds_a_new_language():
    """The registry extension point: a new adapter registers without touching
    the verifier/orchestrator (the #5 win)."""

    # A minimal stand-alone adapter (not inheriting _BaseAdapter) to prove the
    # registry accepts any object conforming to the protocol.
    class GoAdapter:
        name = "go"
        comment_prefix = "//"
        comment_line_prefixes = ("//", "/*")
        source_extension = ".go"
        container_has_braces = True

        def definition_patterns(self):
            return ("func {name}", "type {name}")

        def tree_sitter_language(self):
            return None

    reg = LanguageAdapterRegistry()
    reg.register(GoAdapter())
    assert "go" in reg.supported
    got = reg.get("go")
    assert got.comment_prefix == "//"
    assert got.definition_patterns() == ("func {name}", "type {name}")
    assert got.container_has_braces is True
    # And an unregistered language still falls back to Null.
    assert isinstance(reg.get("python"), NullAdapter)


# ---------------------------------------------------------------------------
# The three formerly-duplicated comment-syntax sites now agree
# ---------------------------------------------------------------------------


def test_consensus_comment_line_uses_adapter():
    """consensus._is_comment_line delegates to the adapter (// for rust)."""
    from capybase.consensus import _is_comment_line
    assert _is_comment_line("# a comment", "python") is True
    assert _is_comment_line("// a comment", "rust") is True
    assert _is_comment_line("* cont", "rust") is True  # rust block-comment cont
    assert _is_comment_line("x = 1", "python") is False


def test_context_builder_comment_uses_adapter():
    """context_builder._is_context_comment delegates to the adapter."""
    from capybase.context_builder import _is_context_comment
    assert _is_context_comment("# py", "python") is True
    assert _is_context_comment("*/ end", "rust") is True  # the superset prefix
    assert _is_context_comment("code = 1", "rust") is False


def test_verification_blank_markers_uses_adapter():
    """verification._blank_markers uses the adapter's comment prefix (// for rust
    so the blanked baseline parses; # for python)."""
    from capybase.verification import _blank_markers
    marked = "<<<<<<< H\nx\n=======\ny\n>>>>>>> b\n"
    py = _blank_markers(marked, "python")
    rust = _blank_markers(marked, "rust")
    assert "# conflict-marker" in py
    assert "// conflict-marker" in rust

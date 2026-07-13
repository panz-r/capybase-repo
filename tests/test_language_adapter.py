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
    """Unknown / None languages get the NullAdapter (safe defaults), not None.

    Fix #12 changed this: Go (and the other parser-supported brace languages)
    now have real adapters. Use a genuinely-unrecognized language to exercise
    the NullAdapter fallback."""
    assert isinstance(adapter_for("cobol"), NullAdapter)
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


# ---------------------------------------------------------------------------
# Fix #11 + #12 regression suite — consolidated language map and adapters for
# all parser-supported languages (not just python/rust).
# ---------------------------------------------------------------------------


def test_brace_languages_have_correct_comment_prefix():
    """Fix #12: every parser-supported brace language now has a real adapter
    with comment_prefix '//'. Before this, they all fell through to NullAdapter
    (comment_prefix '#') — wrong for every brace language, which uses '//'.
    That silently broke comment-line detection in consensus/context-building."""
    brace_langs = (
        "rust", "javascript", "typescript", "go", "java",
        "c", "cpp", "csharp", "kotlin", "swift", "scala", "dart", "php",
    )
    for lang in brace_langs:
        a = adapter_for(lang)
        assert a.comment_prefix == "//", (
            f"{lang} comment_prefix is {a.comment_prefix!r}, expected '//'"
        )
        assert a.container_has_braces is True
        # Not the NullAdapter fallback.
        assert not isinstance(a, NullAdapter), f"{lang} fell through to NullAdapter"


def test_go_definition_patterns():
    """Fix #12: the Go adapter's definition patterns find Go declarations
    (func/type/var/const), enabling _find_definition_span symbol search."""
    go = adapter_for("go")
    assert "func {name}" in go.definition_patterns()
    assert "type {name}" in go.definition_patterns()
    assert go.source_extension == ".go"


def test_java_and_cpp_adapters_shape():
    """Fix #12: Java and C++ adapters carry their keyword-specific definition
    patterns (class/void for Java; class/template/void for C++)."""
    java = adapter_for("java")
    assert "class {name}" in java.definition_patterns()
    assert java.source_extension == ".java"
    cpp = adapter_for("cpp")
    assert "class {name}" in cpp.definition_patterns()
    assert cpp.source_extension == ".cpp"


def test_ruby_still_uses_null_adapter():
    """Fix #12 regression guard: Ruby is NOT in the brace-language set (it's
    Family B, indentation-delimited), so it still gets the NullAdapter. Ruby
    support would need its own adapter (comment_prefix '#', no braces)."""
    assert isinstance(adapter_for("ruby"), NullAdapter)


def test_language_map_single_source_of_truth():
    """Fix #11: the extractor's map and the parser's map are now the SAME object
    (language.EXTENSION_TO_LANGUAGE), so an extension added in one place is
    recognized everywhere — previously .cc/.kt/.swift were parser-known but
    extractor-unknown, and .rb/.sh/.json were extractor-known but parser-unknown."""
    from capybase.adapters import abstract_parser as ap
    from capybase.adapters.language import EXTENSION_TO_LANGUAGE
    from capybase.conflict_extractor import detect_language

    assert ap._EXT_LANG is EXTENSION_TO_LANGUAGE  # same object
    # Extensions the old extractor map missed but the parser knew:
    assert detect_language("foo.cc") == "cpp"
    assert detect_language("foo.kt") == "kotlin"
    assert detect_language("foo.swift") == "swift"
    assert detect_language("foo.mjs") == "javascript"
    # Extensions the old parser map missed but the extractor knew:
    assert ap._EXT_LANG.get(".rb") == "ruby"
    assert ap._EXT_LANG.get(".toml") == "toml"


def test_all_brace_adapters_deprecated_tree_sitter():
    """Fix #12 regression guard: every newly-registered adapter inherits the
    deprecated tree_sitter_language → None (no grammar loading)."""
    for lang in ("go", "java", "cpp", "typescript", "kotlin"):
        assert adapter_for(lang).tree_sitter_language() is None


# ---------------------------------------------------------------------------
# Fourth-pass: alias languages (js/ts/jsx/tsx/c++/cs) must resolve to a real
# adapter, not NullAdapter. Latent today (extensions map to canonical names),
# but a caller passing the short form directly hit the wrong comment_prefix.
# ---------------------------------------------------------------------------


def test_alias_languages_get_real_adapter_not_null():
    """The 6 language aliases in ``_LANG_FAMILY`` (js, ts, jsx, tsx, c++, cs)
    fell through to NullAdapter (comment_prefix '#', empty definition
    patterns) because the registry only iterated canonical names. A caller
    passing the short form directly got wrong comment-line detection. After
    the fix each alias resolves to a brace-family adapter with comment_prefix
    '//' matching its canonical language."""
    for alias in ("js", "ts", "jsx", "tsx", "c++", "cs"):
        ad = adapter_for(alias)
        assert not isinstance(ad, NullAdapter), (
            f"alias {alias!r} must resolve to a real adapter, not NullAdapter"
        )
        assert ad.comment_prefix == "//", (
            f"alias {alias!r} must use '//' comments (brace language); "
            f"got comment_prefix={ad.comment_prefix!r}"
        )


def test_alias_adapter_matches_canonical_behavior():
    """An alias adapter must behave like its canonical language: same
    comment_prefix and (non-empty) definition patterns. ``js``↔``javascript``,
    ``c++``↔``cpp``, ``cs``↔``csharp``."""
    pairs = [("js", "javascript"), ("ts", "typescript"), ("c++", "cpp"), ("cs", "csharp")]
    for alias, canon in pairs:
        a = adapter_for(alias)
        c = adapter_for(canon)
        assert a.comment_prefix == c.comment_prefix, (
            f"{alias} comment_prefix {a.comment_prefix!r} != {canon} {c.comment_prefix!r}"
        )
        assert a.definition_patterns == c.definition_patterns, (
            f"{alias} definition_patterns != {canon}"
        )

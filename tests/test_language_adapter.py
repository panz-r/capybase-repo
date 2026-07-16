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


# --- H3 regression: multi-modifier method signatures ---


def test_h3_find_definition_span_java_multi_modifier():
    """Java/C#/C++ method signatures often stack modifiers before the name:
    ``public static void foo()``, ``protected abstract int compute()``. The
    definition patterns enumerate single-keyword prefixes (``void {name}``),
    so a modifier stack defeats the ``startswith`` match and the symbol is not
    found — silently breaking cross-file symbol resolution for the most common
    Java/C#/C++ signatures."""
    from capybase.adapters.structural import _find_definition_span
    # Java: public static void foo() — must find foo.
    src = "class C {\n    public static void foo() {\n        return;\n    }\n}\n"
    span = _find_definition_span(src, "foo", "java")
    assert span is not None, "Java 'public static void foo()' must be found"
    assert span[0] == 1, f"foo is on line 1; got span={span}"
    # C#: public static int Compute() — must find Compute.
    src_cs = "class C {\n    public static int Compute() { return 0; }\n}\n"
    span_cs = _find_definition_span(src_cs, "Compute", "csharp")
    assert span_cs is not None, "C# 'public static int Compute()' must be found"
    # C++: virtual void execute() — must find execute.
    src_cpp = "class C {\n    virtual void execute() { }\n}\n"
    span_cpp = _find_definition_span(src_cpp, "execute", "cpp")
    assert span_cpp is not None, "C++ 'virtual void execute()' must be found"
    # Regression: single-modifier signatures still work.
    src_simple = "class C {\n    void bar() { }\n}\n"
    assert _find_definition_span(src_simple, "bar", "java") is not None
    # Regression: non-matching name must NOT falsely match.
    assert _find_definition_span(src, "nonexistent", "java") is None


# --- H3 hardening: fallback must not false-positive on types/calls/Python ---


def test_h3_fallback_no_false_positive_on_param_type():
    """The stacked-modifier fallback must match the DEFINITION IDENTIFIER, not
    just any word on the line. A parameter type, return type, or throws-clause
    name must NOT be found as a definition."""
    from capybase.adapters.structural import _find_definition_span
    # StaticType is a parameter TYPE, not the def name (process is).
    src = "class C {\n    public void process(StaticType x) { }\n}\n"
    assert _find_definition_span(src, "StaticType", "java") is None, (
        "parameter type must not be matched as a definition"
    )
    # Logger is a RETURN TYPE, not the def name (getLogger is).
    src2 = "class C { public static Logger getLogger() { return null; } }\n"
    assert _find_definition_span(src2, "Logger", "java") is None, (
        "return type must not be matched as a definition"
    )
    # The actual def name still works.
    assert _find_definition_span(src, "process", "java") is not None


def test_h3_fallback_not_for_python():
    """The stacked-modifier fallback is for brace languages (Java/C#/C++).
    Python has no stacked modifiers, so it must NOT fire — ``async def foo()``
    is handled by the exact-prefix ``def {name}`` match (async is stripped by
    the parser, and the pattern matches ``def foo``)."""
    from capybase.adapters.structural import _find_definition_span
    # A call named 'foo' after a 'static' token — must NOT match for Python.
    src = "def bar():\n    static = foo()\n"
    assert _find_definition_span(src, "foo", "python") is None, (
        "fallback must not fire for Python (no stacked modifiers)"
    )
    # async def still works via the exact-prefix path.
    src2 = "async def greet():\n    pass\n"
    assert _find_definition_span(src2, "greet", "python") is not None, (
        "async def must be found via the prefix match"
    )


# --- Finding 3 (round 7): H3 hardening regressed typed-field detection ---


def test_r7_h3_typed_field_detection_java_csharp():
    """The H3 hardening's type/field branch stopped AT the type keyword
    (Java patterns only enumerate ``void`` as a type, so ``int`` ended the
    modifier-strip loop) — typed fields/properties like ``private final int
    count`` regressed to false negatives. The old word-search found them.

    A field's name is the LAST identifier before ``=`` / ``;`` / ``{``, not the
    first after the modifier run (which is the type)."""
    from capybase.adapters.structural import _find_definition_span
    cases = [
        ("class C {\n    private final int count = 0;\n}\n", "count", "java"),
        ("class C {\n    public int Count { get; set; }\n}\n", "Count", "csharp"),
        ("class C {\n    String name;\n}\n", "name", "java"),
        ("class C {\n    protected List<Item> items;\n}\n", "items", "csharp"),
    ]
    for src, name, lang in cases:
        span = _find_definition_span(src, name, lang)
        assert span is not None, f"{lang} field/property {name!r} must be found"
    # Negative: a parameter TYPE must still NOT match (the hardening's purpose).
    assert _find_definition_span("class C {\n    public void process(StaticType x) { }\n}\n", "StaticType", "java") is None


# --- B-5 (round 8): H3 gate misses lowercase-primitive fields ---


def test_r8_h3_lowercase_primitive_field():
    r"""A bare primitive-type field (``int count;``, ``long total;``, ``double
    pi;``) has no modifier prefix and a lowercase type — neither in
    decl_keywords nor Capitalized — so the H3 fallback gate rejected it. This
    is the single most common Java/C# field shape. Broaden the gate so the
    type/field branch fires when the line has the ``Type name`` shape (>=2
    identifier tokens before the terminator)."""
    from capybase.adapters.structural import _find_definition_span
    cases = [
        ("class C {\n    int count = 0;\n}\n", "count", "java"),
        ("class C {\n    long total;\n}\n", "total", "java"),
        ("class C {\n    double pi = 3.14;\n}\n", "pi", "csharp"),
        ("class C {\n    boolean flag;\n}\n", "flag", "java"),
    ]
    for src, name, lang in cases:
        span = _find_definition_span(src, name, lang)
        assert span is not None, f"{lang} primitive field {name!r} must be found"
    # False positives still rejected.
    assert _find_definition_span("class C {\n    public void process(StaticType x) { }\n}\n", "StaticType", "java") is None
    assert _find_definition_span("def bar():\n    static = foo()\n", "foo", "python") is None


# --- B-5 hardening (round 9): reject comments + statements ---


def test_r9_h3_no_false_positive_on_comment_or_statement():
    r"""The H3 fallback gate (broadened in round 8 to ``ident_count >= 2``)
    over-matched: a comment line, a ``return X;``, a ``throw new X();``, a
    ``new X();`` all have >= 2 identifier tokens and falsely matched as
    definitions. The gate must reject comment markers and statement keywords."""
    from capybase.adapters.structural import _find_definition_span
    # Comment line mentioning the name BEFORE the real def.
    src = "class C {\n    // remember to update the bar\n    void bar() {}\n}\n"
    span = st = _find_definition_span(src, "bar", "java")
    assert span is None or span[0] == 2, (
        f"comment line must not match before the real def; got {span}"
    )
    # Statement-keyword lines.
    for label, src, name in [
        ("return", "    return result;", "result"),
        ("throw new", "    throw new Exception();", "Exception"),
        ("new call", "    new Foo();", "Foo"),
        ("assert", "    assert condition;", "condition"),
    ]:
        span = _find_definition_span(src, name, "java")
        assert span is None, f"{label} statement must not match as a definition; got {span}"
    # True positives still work (the gate's purpose).
    assert _find_definition_span("class C {\n    int count = 0;\n}\n", "count", "java") is not None
    assert _find_definition_span("class C {\n    public static void foo() {}\n}\n", "foo", "java") is not None


# --- Finding 2 (round 14): exact-prefix false positives ---


def test_r14_find_definition_span_no_false_positive_prefix():
    r"""The exact-prefix path in _find_definition_span returns immediately on
    startswith(pat) without validating that {name} is the actual definition
    identifier. Two false-positive modes:
    - a return type matching a modifier pattern (public Bar getBar() — Bar is
      the return type, not the def name);
    - a prefix of a longer name (fn compute_other matches search for compute).
    Add a word-boundary check after the prefix match."""
    from capybase.adapters.structural import _find_definition_span
    # Return-type false positive (single-modifier pattern match).
    src = "class C {\n    public Bar getBar() { return new Bar(); }\n}\n"
    assert _find_definition_span(src, "Bar", "java") is None, (
        "return type must not match as a definition (public Bar is a prefix pattern)"
    )
    # Prefix-of-longer-name false positive.
    src2 = "fn compute_other() -> i32 { 2 }\n"
    assert _find_definition_span(src2, "compute", "rust") is None, (
        "prefix of a longer name must not match (fn compute_other != fn compute)"
    )
    # True positives still work.
    assert _find_definition_span("fn compute() -> i32 { 2 }\n", "compute", "rust") is not None
    assert _find_definition_span("class C {\n    public Bar getBar() { }\n}\n", "getBar", "java") is not None


def test_r32_find_definition_span_ignores_string_literals():
    """B-1 (HIGH): a name appearing ONLY inside a string literal was reported as
    a definition (the scan has no string-awareness), producing phantom definitions
    that corrupt cross-file symbol resolution."""
    from capybase.adapters.structural import _find_definition_span
    src = 'fn main() {\n    let s = "fn foo() { not real }";\n    println!("{}", s);\n}\n'
    assert _find_definition_span(src, "foo", "rust") is None, (
        "definition inside a string literal must be ignored"
    )
    # Java/C++ string form.
    src2 = 'class C {\n    String s = "void foo() {}";\n}\n'
    assert _find_definition_span(src2, "foo", "java") is None, (
        "definition inside a Java string literal must be ignored"
    )
    # Python string form.
    src3 = 'x = "def foo(): return 1"\n'
    assert _find_definition_span(src3, "foo", "python") is None, (
        "definition inside a Python string literal must be ignored"
    )


def test_r33_find_definition_span_not_over_rejected_by_body_strings():
    """F1 (HIGH, round-32 regression): the _line_content_is_string guard was too
    broad — it rejected ANY line with a quote before a decl keyword, silently
    missing real one-liner definitions whose body string contains common words
    like 'function', 'void', 'class'. Now uses string-blanking (not a heuristic)."""
    from capybase.adapters.structural import _find_definition_span
    # Real definitions whose body contains strings with decl-like words.
    assert _find_definition_span('def msg(): return "Error in function bar"', "msg", "python") is not None
    assert _find_definition_span('fn foo() -> &str { "hello fn bar()" }', "foo", "rust") is not None
    assert _find_definition_span('public String foo() { return "void bar"; }', "foo", "java") is not None


def test_r33_referenced_symbols_ignores_string_literals():
    """F4 (MEDIUM): referenced_symbols extracted identifiers from inside string
    literals, polluting the dependency-drop check. Now blanks strings first."""
    from capybase.adapters.structural import referenced_symbols
    refs = referenced_symbols('msg = "def foo(): return bar"', "python")
    assert "foo" not in refs, f"identifier inside string extracted; got {refs}"
    assert "bar" not in refs, f"identifier inside string extracted; got {refs}"
    # Real code references must survive.
    refs2 = referenced_symbols("x = compute(real_thing)", "python")
    assert "compute" in refs2
    assert "real_thing" in refs2

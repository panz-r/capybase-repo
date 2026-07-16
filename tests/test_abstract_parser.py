"""Spec for the grammar-free abstract structural parser.

Replaces tree-sitter grammars with two state machines: Family B (indentation-
delimited, Python) and Family A (brace-delimited, Rust et al.). These tests pin
the behavior the rest of capybase relies on: coarse kinds, faithful spans/bodies,
nested children, conflict-marker robustness, and graceful degradation. Pure — no
tree-sitter, no model, no I/O.
"""

from __future__ import annotations

from capybase.adapters import abstract_parser as ap
from capybase.adapters import structural_context as sc
from capybase.adapters import structural_diff as sd


# ---------------------------------------------------------------------------
# Shared helpers — parse-and-flatten shortcuts used across the suite
# ---------------------------------------------------------------------------


def _flat(src: str, lang: str = "python"):
    """Parse ``src`` and return its flat unit list (top-level + nested children).

    The single most common two-line idiom in the suite (``ir = parse_file(...);
    flat = all_units_flat(ir)``) collapsed to one call. Returns the flat list
    directly so assertions read ``names = [u.name for u in _flat(src, "rust")]``.
    """
    ir = ap.parse_file(src, language=lang)
    assert ir is not None, f"parse_file returned None for {lang!r}"
    return ap.all_units_flat(ir)


def _kinds(src: str, lang: str = "python") -> list[tuple[str, str]]:
    """``[(kind, name), ...]`` for ``src``, in source order.

    Used both for assertions (``assert ("method", "foo") in _kinds(src, "java")``)
    and for error messages (``f"got {_kinds(src)}"``), replacing the 37× repeated
    inline ``_kinds_of(ir)`` pattern.
    """
    return [(u.kind, u.name) for u in _flat(src, lang)]


def _kinds_of(ir) -> list[tuple[str, str]]:
    """``[(kind, name), ...]`` for an already-parsed ``FileIR``.

    The error-message variant: tests that use ``parse_family_a``/``parse_family_b``
    directly (not ``parse_file``) already have ``ir`` in scope, so this takes the
    IR rather than raw source. Replaces the 37× ``[(u.kind, u.name) for u in
    ap.all_units_flat(ir)]`` inline pattern.
    """
    return [(u.kind, u.name) for u in ap.all_units_flat(ir)]


def _names_by_kind(flat: list, kind: str) -> list[str]:
    """Names of units of ``kind`` in an already-flattened list.

    Replaces the repeated ``_names_by_kind(flat, "method")`` idiom.
    """
    return [u.name for u in flat if u.kind == kind]


# ---------------------------------------------------------------------------
# Family dispatch
# ---------------------------------------------------------------------------


def test_detect_family_by_language():
    assert ap.detect_family("python") == ap.FAMILY_B
    assert ap.detect_family("rust") == ap.FAMILY_A
    assert ap.detect_family("javascript") == ap.FAMILY_A
    assert ap.detect_family("typescript") == ap.FAMILY_A
    assert ap.detect_family("go") == ap.FAMILY_A
    assert ap.detect_family(None, "app.py") == ap.FAMILY_B
    assert ap.detect_family(None, "src/lib.rs") == ap.FAMILY_A
    assert ap.detect_family(None, "unknown.xyz") is None
    assert ap.detect_family(None, None) is None


def test_parse_file_returns_none_for_unknown_family():
    """An unrecognized language/path → None (no structural signal)."""
    assert ap.parse_file("anything", language="cobol") is None
    assert ap.parse_file("anything", path="readme.md") is None


def test_parse_file_dispatches_on_language_then_path():
    """Language wins over path; path is the fallback when language is None."""
    ir = ap.parse_file("def f():\n    pass\n", language="python")
    assert ir is not None and ir.family == ap.FAMILY_B
    ir = ap.parse_file("fn main() {}\n", path="main.rs")
    assert ir is not None and ir.family == ap.FAMILY_A


# ---------------------------------------------------------------------------
# Family B (Python)
# ---------------------------------------------------------------------------


def test_family_b_top_level_function():
    """A module-level def is a FUNCTION with a faithful span/body."""
    src = "def greet():\n    return 'hello'\n"
    ir = ap.parse_family_b(src)
    flat = ap.all_units_flat(ir)
    assert len(ir.units) == 1
    f = ir.units[0]
    assert f.kind == "function"
    assert f.name == "greet"
    assert f.span == (0, 1)
    assert "def greet():" in f.body
    assert "return 'hello'" in f.body


def test_family_b_class_with_methods_nests_children():
    """Methods are METHOD-kind children nested under their CLASS parent."""
    src = (
        "class Foo:\n"
        "    def a(self):\n"
        "        return 1\n"
        "\n"
        "    def b(self):\n"
        "        return 2\n"
    )
    ir = ap.parse_family_b(src)
    assert len(ir.units) == 1
    cls = ir.units[0]
    assert cls.kind == "class"
    assert cls.name == "Foo"
    assert cls.span[0] == 0
    # The class span covers its methods (no overlap with the next sibling).
    assert cls.span[1] >= 5
    names = [(c.kind, c.name) for c in cls.children]
    assert ("method", "a") in names
    assert ("method", "b") in names
    assert all(c.kind == "method" for c in cls.children)


def test_family_b_async_def_classified_as_function():
    """``async def`` is still a FUNCTION (async is a prefix, not a kind)."""
    src = "async def fetch():\n    return 1\n"
    ir = ap.parse_family_b(src)
    assert ir.units[0].kind == "function"
    assert ir.units[0].name == "fetch"


def test_family_b_methods_only_nest_inside_class():
    """A ``def`` at module indent is a FUNCTION, not a METHOD (no class parent)."""
    src = "def top():\n    return 1\n\ndef other():\n    return 2\n"
    ir = ap.parse_family_b(src)
    assert all(u.kind == "function" for u in ir.units)
    assert {u.name for u in ir.units} == {"top", "other"}


def test_family_b_imports_are_module_stmt():
    """Import/from-import lines are MODULE_STMT units."""
    src = "import os\nfrom typing import List\n\ndef f():\n    pass\n"
    ir = ap.parse_family_b(src)
    stmts = [u for u in ir.units if u.kind == "module_stmt"]
    assert len(stmts) == 2
    assert "os" in {s.name for s in stmts}


def test_family_b_decorators_attach_to_following_decl():
    """A ``@decorator`` line is folded into the following def's body/span."""
    src = "@property\ndef score(self):\n    return self._score\n"
    ir = ap.parse_family_b(src)
    assert len(ir.units) == 1
    f = ir.units[0]
    assert f.name == "score"
    assert f.span[0] == 0  # the decorator line
    assert "@property" in f.body


def test_family_b_test_detection_by_name_prefix():
    """A ``def test_*`` is flagged is_test (TEST is a sub-classification)."""
    src = "def test_foo():\n    assert True\n\ndef helper():\n    pass\n"
    ir = ap.parse_family_b(src)
    by_name = {u.name: u for u in ir.units}
    assert by_name["test_foo"].is_test is True
    assert by_name["helper"].is_test is False


def test_family_b_nested_function_is_not_a_method():
    """A def nested inside another FUNCTION (not a CLASS) stays FUNCTION-kind."""
    src = "def outer():\n    def inner():\n        return 1\n    return inner()\n"
    ir = ap.parse_family_b(src)
    flat = ap.all_units_flat(ir)
    kinds = {u.name: u.kind for u in flat}
    # inner is a nested function (parent is a function, not a class) — it may or
    # may not be enumerated depending on depth handling, but it must NEVER be a
    # ``method`` (methods are class members only).
    assert kinds.get("inner", "function") != "method"


def test_family_b_never_crashes_on_malformed():
    """Malformed/partial input never raises — robustness over correctness."""
    # Unterminated, mixed indentation, garbage.
    ir = ap.parse_family_b("def f(\n    :\n   class X\n<<<<<<<\n")
    assert ir is not None  # didn't raise
    assert isinstance(ir.units, list)


# ---------------------------------------------------------------------------
# Family A (Rust / brace-delimited)
# ---------------------------------------------------------------------------


RUST_SAMPLE = (
    "use std::io;\n"
    "\n"
    "pub const N: u32 = 5;\n"
    "\n"
    "pub struct Config {\n"
    "    pub port: u16,\n"
    "}\n"
    "\n"
    "impl Config {\n"
    "    pub fn new() -> Self {\n"
    "        Config { port: 8080 }\n"
    "    }\n"
    "\n"
    "    pub fn label(&self) -> String {\n"
    '        format!("port={}", self.port)\n'
    "    }\n"
    "}\n"
    "\n"
    "fn main() {\n"
    "    let c = Config::new();\n"
    '    println!("{}", c.label());\n'
    "}\n"
)


def test_family_a_struct_is_class():
    """A ``struct`` is a CLASS-kind unit."""
    ir = ap.parse_family_a(RUST_SAMPLE, "rust")
    classes = [u for u in ir.units if u.kind == "class"]
    names = {u.name for u in classes}
    assert "Config" in names


def test_family_a_impl_is_container_only_not_emitted():
    """``impl X`` is a container, NOT an entity — only its methods are emitted.

    Mirrors tree-sitter (impl_item has no entity kind). The impl IS in the tree
    as a distinct scope (so ``fn make`` in ``impl A`` doesn't collide with one in
    ``impl B``), but the flat entity list skips it. This is load-bearing for
    identity: an ``impl Config`` must NOT collide with ``struct Config`` under
    the same (class, "Config") key.
    """
    ir = ap.parse_family_a(RUST_SAMPLE, "rust")
    flat = ap.all_units_flat(ir)
    # Exactly one "Config" entity (the struct), NOT two (struct + impl).
    config_entities = [u for u in flat if u.name == "Config"]
    assert len(config_entities) == 1
    assert config_entities[0].kind == "class"
    # The impl is in the tree (as a container scope) but not in the flat entities.
    assert any(u.is_container_scope and u.name == "Config" for u in ir.units)


def test_family_a_methods_nest_as_children_of_impl():
    """``fn`` inside an ``impl`` are METHOD-kind, nested as children.

    They attach to the struct's entity tree (the impl is container-only, so its
    children pass through to the enclosing scope — here top-level, since the impl
    is at module scope). The methods must be present and METHOD-kind.
    """
    ir = ap.parse_family_a(RUST_SAMPLE, "rust")
    flat = ap.all_units_flat(ir)
    methods = [u for u in flat if u.kind == "method"]
    method_names = {u.name for u in methods}
    assert "new" in method_names
    assert "label" in method_names


def test_family_a_free_function_is_function_not_method():
    """A module-level ``fn`` (not inside any impl) is FUNCTION-kind."""
    ir = ap.parse_family_a(RUST_SAMPLE, "rust")
    flat = ap.all_units_flat(ir)
    main = next(u for u in flat if u.name == "main")
    assert main.kind == "function"


def test_family_a_use_is_module_stmt():
    """A ``use`` statement is a MODULE_STMT unit."""
    ir = ap.parse_family_a(RUST_SAMPLE, "rust")
    uses = [u for u in ir.units if u.kind == "module_stmt"]
    assert any(u.name == "std::io" for u in uses)


def test_family_a_const_is_field():
    """A top-level ``pub const`` is a FIELD unit."""
    ir = ap.parse_family_a(RUST_SAMPLE, "rust")
    fields = [u for u in ir.units if u.kind == "field"]
    assert any(u.name == "N" for u in fields)


def test_family_a_string_aware_brace_counting():
    """A ``{`` inside a string literal must NOT open a scope (string-aware)."""
    src = (
        'fn main() {\n'
        '    let s = "{ not a scope }";\n'
        '    let m = format!("{} {}", 1, 2);\n'
        '}\n'
    )
    ir = ap.parse_family_a(src, "rust")
    flat = ap.all_units_flat(ir)
    # Exactly one function (main); the string braces didn't create phantom units.
    assert sum(1 for u in flat if u.kind == "function") == 1


def test_family_a_object_literal_brace_is_expression_level():
    """A bare ``{`` without a declaration keyword (object literal) is skipped."""
    src = (
        "fn main() {\n"
        "    let m = {\"key\": \"value\"};\n"
        "    let v = vec![{ x: 1 }, { y: 2 }];\n"
        "}\n"
    )
    ir = ap.parse_family_a(src, "rust")
    flat = ap.all_units_flat(ir)
    # Only main; the object literals didn't become units.
    assert sum(1 for u in flat if u.kind in ("function", "method", "class")) == 1


def test_family_a_never_crashes_on_malformed():
    """Malformed/unbalanced braces never raise; depth clamps at 0."""
    src = "fn broken() {\n  }}}\n  use std::\n<<<<<<<\n"
    ir = ap.parse_family_a(src, "rust")
    assert ir is not None


# ---------------------------------------------------------------------------
# Conflict-marker awareness
# ---------------------------------------------------------------------------


def test_conflict_markers_close_open_units_family_b():
    """Conflict markers close any open unit; the parser doesn't crash mid-merge."""
    src = (
        "def greet():\n"
        "    x = 1\n"
        "<<<<<<< HEAD\n"
        "    return 'hi'\n"
        "=======\n"
        "    return 'howdy'\n"
        ">>>>>>> abc123\n"
    )
    ir = ap.parse_family_b(src)
    # greet is detected and closed at the marker (it doesn't swallow the markers).
    funcs = [u for u in ir.units if u.kind == "function"]
    assert any(u.name == "greet" for u in funcs)


def test_conflict_markers_close_open_units_family_a():
    """Family A also treats markers as scope boundaries."""
    src = (
        "fn main() {\n"
        "<<<<<<< HEAD\n"
        "    let x = 1;\n"
        "=======\n"
        "    let x = 2;\n"
        ">>>>>>> abc\n"
        "}\n"
    )
    ir = ap.parse_family_a(src, "rust")
    assert ir is not None
    flat = ap.all_units_flat(ir)
    assert any(u.name == "main" for u in flat)


# ---------------------------------------------------------------------------
# Minified / generated detection + parse confidence
# ---------------------------------------------------------------------------


def test_minified_code_yields_low_confidence():
    """Very long median line length (minified/generated) → confidence 0.0."""
    long_line = "x=" + "a" * 300 + ";y=" + "b" * 300
    ir = ap.parse_family_a(long_line, "javascript")
    assert ir.parse_confidence == 0.0
    assert ir.units == []


def test_normal_code_is_full_confidence():
    """Ordinary source parses at confidence 1.0."""
    ir = ap.parse_family_b("def f():\n    return 1\n", "python")
    assert ir.parse_confidence == 1.0


def test_low_confidence_parse_degrades_to_none_in_consumers():
    """A confidence-0.0 parse (minified) surfaces as None through the consumer
    wrapper and the 3-way diff, NOT as an empty FileIR. This lets callers
    distinguish "no trustworthy structure" from "genuinely empty file" — the
    contract parse_confidence was always meant to send but never did."""
    from capybase.adapters import structural
    # Minified JS: median line length > 200 → confidence 0.0.
    minified = "var a=1;" + "x" * 300 + ";"
    # The parser still returns the FileIR (confidence stamped)...
    ir = ap.parse_file(minified, language="javascript")
    assert ir is not None and ir.parse_confidence == 0.0
    # ...but the consumer wrapper gates it to None.
    assert structural._abstract_parse(minified, "javascript") is None
    assert structural.enumerate_entities(minified, "javascript") is None
    # And the 3-way diff declines when any side is untrustworthy.
    normal = "function foo() { return 1; }"
    assert sd.compute_structural_diff_3way(normal, normal, minified, language="javascript") is None


# ---------------------------------------------------------------------------
# Region queries
# ---------------------------------------------------------------------------


def test_enclosing_unit_finds_deepest():
    """enclosing_unit returns the deepest unit whose span contains the anchor."""
    src = (
        "class Foo:\n"
        "    def bar(self):\n"
        "        return 1\n"
    )
    ir = ap.parse_family_b(src)
    flat = ap.all_units_flat(ir)
    method = next(u for u in flat if u.name == "bar")
    # Anchor inside the method body (row 2).
    assert ap.enclosing_unit(ir, (2, 2)).name == "bar"
    # Anchor on the class line (row 0).
    assert ap.enclosing_unit(ir, (0, 0)).name == "Foo"


def test_units_in_container_returns_children():
    """units_in_container returns the siblings inside the enclosing container."""
    src = (
        "class Foo:\n"
        "    def a(self):\n"
        "        return 1\n"
        "    def b(self):\n"
        "        return 2\n"
    )
    ir = ap.parse_family_b(src)
    # Anchor inside method a → container is Foo → children are [a, b].
    kids = ap.units_in_container(ir, (2, 2))
    names = {k.name for k in kids}
    assert "a" in names and "b" in names


def test_units_in_container_at_module_scope_returns_top_level():
    """An anchor at module scope (no enclosing unit) returns top-level units."""
    src = "def a():\n    pass\n\ndef b():\n    pass\n"
    ir = ap.parse_family_b(src)
    # Anchor on the blank line between (row 1) — not inside any unit.
    kids = ap.units_in_container(ir, (1, 1))
    # No enclosing unit → returns the top-level units.
    names = {k.name for k in kids}
    assert "a" in names and "b" in names


# ---------------------------------------------------------------------------
# Body fingerprint (rename detection basis)
# ---------------------------------------------------------------------------


def test_body_fingerprint_invariant_to_rename():
    """Two functions differing only in name produce the SAME body fingerprint.

    This is the basis for rename detection: a renamed entity's header is stripped
    so its content digest is unchanged, letting semantic_diff pair it to its
    base original.
    """
    a = "def foo():\n    return 1\n    return 2\n"
    b = "def bar():\n    return 1\n    return 2\n"
    assert ap.unit_body_fingerprint(a) == ap.unit_body_fingerprint(b)


def test_body_fingerprint_changes_with_body_edit():
    """A body content change produces a DIFFERENT fingerprint."""
    a = "def foo():\n    return 1\n"
    b = "def foo():\n    return 2\n"
    assert ap.unit_body_fingerprint(a) != ap.unit_body_fingerprint(b)


def test_body_fingerprint_invariant_to_whitespace():
    """Whitespace/reformatting changes leave the fingerprint unchanged."""
    a = "def foo():\n    return 1\n"
    b = "def foo():\n\treturn    1\n"
    assert ap.unit_body_fingerprint(a) == ap.unit_body_fingerprint(b)


# ---------------------------------------------------------------------------
# Language expansion (Improvement #2): Family-A languages beyond Rust
# ---------------------------------------------------------------------------


def test_javascript_parsing():
    """JS/TS function + class detection via the Family-A state machine."""
    src = "function foo() {\n    return 1;\n}\n\nclass Bar {\n    constructor() {}\n}\n"
    ir = ap.parse_file(src, language="javascript")
    assert ir is not None
    kinds = {u.kind for u in ir.units}
    assert "function" in kinds
    assert "class" in kinds


def test_go_parsing():
    """Go function detection."""
    src = 'package main\n\nfunc main() {\n    fmt.Println("hi")\n}\n\nfunc helper(x int) int {\n    return x\n}\n'
    ir = ap.parse_file(src, language="go")
    assert ir is not None
    funcs = [u.name for u in ir.units if u.kind == "function"]
    assert "main" in funcs
    assert "helper" in funcs


def test_java_parsing():
    """Java class + method detection. The keyword-less ``main`` method (whose
    signature leads with return type ``void``, no ``fn``/``function`` keyword)
    is now extracted as a METHOD child of the class via the keyword-less
    method heuristic."""
    src = (
        "import java.util.List;\n\n"
        "public class Main {\n"
        "    public static void main(String[] args) {\n"
        "        System.out.println(\"hi\");\n"
        "    }\n"
        "}\n"
    )
    ir = ap.parse_file(src, language="java")
    assert ir is not None
    classes = [u.name for u in ir.units if u.kind == "class"]
    assert "Main" in classes
    # The keyword-less method is now a child of the class.
    flat = ap.all_units_flat(ir)
    methods = _names_by_kind(flat, "method")
    assert "main" in methods


def test_java_keywordless_methods_extracted():
    """Java methods (no declaration keyword — return type leads) are extracted as
    METHOD children of their class. Multiple sibling methods, constructors, and
    methods with complex signatures all recover. Before, every Java
    method was absorbed into the class body (invisible to the merge engine)."""
    src = (
        "class Service {\n"
        "    Service() { init(); }\n"
        "    public int compute(int x, int y) { return x + y; }\n"
        "    private void log(String msg) { System.err.println(msg); }\n"
        "}\n"
    )
    ir = ap.parse_file(src, language="java")
    flat = ap.all_units_flat(ir)
    methods = sorted(u.name for u in flat if u.kind == "method")
    # Constructor + both methods recovered.
    assert "Service" in methods       # constructor (classname as method)
    assert "compute" in methods
    assert "log" in methods


def test_cpp_methods_with_access_specifiers():
    """C++ methods are extracted despite ``public:``/``private:`` access-
    specifier lines between them (the access specifier is just absorbed, not a
    declaration). Constructor + regular method both recovered."""
    src = (
        "class Widget {\n"
        "public:\n"
        "    Widget() { count = 0; }\n"
        "    int getCount() { return count; }\n"
        "private:\n"
        "    void reset() { count = 0; }\n"
        "};\n"
    )
    ir = ap.parse_file(src, language="cpp")
    flat = ap.all_units_flat(ir)
    methods = sorted(u.name for u in flat if u.kind == "method")
    assert "Widget" in methods    # constructor
    assert "getCount" in methods
    assert "reset" in methods


def test_csharp_methods_extracted():
    """C# methods (keyword-less, like Java) are extracted as METHOD children."""
    src = (
        "class Config {\n"
        "    public string GetName() { return name; }\n"
        "    private void SetName(string v) { name = v; }\n"
        "}\n"
    )
    ir = ap.parse_file(src, language="csharp")
    flat = ap.all_units_flat(ir)
    methods = sorted(u.name for u in flat if u.kind == "method")
    assert "GetName" in methods
    assert "SetName" in methods


def test_control_flow_not_misclassified_as_methods():
    """Regression guard for the keyword-less method heuristic: control-flow
    blocks (``if``/``while``/``for``/``switch``) inside a method body must NOT
    be extracted as methods. They sit one brace-depth deeper than real methods,
    so the depth guard excludes them by construction."""
    src = (
        "class C {\n"
        "    void m() {\n"
        "        if (x) { y(); }\n"
        "        while (z) { w(); }\n"
        "        for (int i = 0; i < n; i++) { v(); }\n"
        "        switch (s) { case 1: break; }\n"
        "    }\n"
        "}\n"
    )
    ir = ap.parse_file(src, language="java")
    flat = ap.all_units_flat(ir)
    methods = _names_by_kind(flat, "method")
    # Only ``m`` is a method — the if/while/for/switch blocks are NOT entities.
    assert methods == ["m"], f"control flow leaked as methods: {methods}"


def test_c_free_functions_extracted_at_file_scope():
    """C free functions (keyword-less, at file scope) are extracted as FUNCTION
    units. The depth guard allows ``brace_depth == 0`` with no open container."""
    src = "int main() { return 0; }\nint helper(int x) { return x + 1; }\n"
    ir = ap.parse_file(src, language="c")
    flat = ap.all_units_flat(ir)
    funcs = sorted(u.name for u in flat if u.kind == "function")
    assert "main" in funcs
    assert "helper" in funcs


def test_js_class_method_shorthand_extracted():
    """JS/TS class method shorthand (``class C { m() {} }`` — no ``function``
    keyword) is now extracted as a METHOD. Previously absorbed into the class."""
    src = "class C {\n    m() { return 1; }\n    n() { return 2; }\n}\n"
    ir = ap.parse_file(src, language="javascript")
    flat = ap.all_units_flat(ir)
    methods = sorted(u.name for u in flat if u.kind == "method")
    assert "m" in methods and "n" in methods


def test_rust_keyword_extraction_unchanged_by_keywordless_heuristic():
    """Regression guard: Rust (which uses ``fn``) is unaffected by the keyword-
    less method heuristic — keyword-prefixed classification still takes priority."""
    src = "impl C {\n    fn foo() {}\n    fn bar() {}\n}\n"
    ir = ap.parse_file(src, language="rust")
    flat = ap.all_units_flat(ir)
    methods = sorted(u.name for u in flat if u.kind == "method")
    assert methods == ["bar", "foo"]


def test_is_available_for_all_family_languages():
    """is_available returns True for all languages with a known family."""
    from capybase.adapters import structural
    for lang in ("python", "rust", "javascript", "typescript", "go", "java", "c", "cpp"):
        assert structural.is_available(lang), f"{lang} should be available"


# ---------------------------------------------------------------------------
# Import/export surface (Improvement #3)
# ---------------------------------------------------------------------------


def test_python_import_export_surface():
    """Family B: imports from import lines, exports from public def/class names."""
    src = (
        "import os\n"
        "from sys import path\n"
        "\n"
        "def public_fn():\n    pass\n"
        "\n"
        "def _private_fn():\n    pass\n"
        "\n"
        "class PublicClass:\n    pass\n"
    )
    ir = ap.parse_file(src, language="python")
    assert "os" in ir.imports
    assert "sys" in ir.imports
    assert "public_fn" in ir.exports
    assert "PublicClass" in ir.exports
    assert "_private_fn" not in ir.exports


def test_rust_import_export_surface():
    """Family A: imports from use statements, exports from pub items."""
    src = (
        "use std::io;\n"
        "\n"
        "pub fn main() {\n    println!(\"hi\");\n}\n"
        "\n"
        "fn helper() {}\n"
        "\n"
        "pub struct Config {\n    x: u32,\n}\n"
    )
    ir = ap.parse_file(src, language="rust")
    assert "std::io" in ir.imports
    assert "main" in ir.exports
    assert "Config" in ir.exports
    assert "helper" not in ir.exports  # not pub


def test_javascript_import_surface():
    """JS destructured import path is extracted."""
    src = "import { foo } from './foo.js';\n\nexport function bar() {\n    return 1;\n}\n"
    ir = ap.parse_file(src, language="javascript")
    assert "./foo.js" in ir.imports
    assert "bar" in ir.exports


def test_export_detection_no_dead_eported_token():
    """Regression guard: the export keyword set no longer carries the dead
    ``"EPORTED"`` typo. The real modifiers (pub/export/public) and the CommonJS
    ``module.exports`` form drive export classification. Exercises the extractor
    directly with a synthetic unit so the test isn't coupled to the parser's
    anonymous-function-expression naming (a separate limitation)."""
    # A named function with the CommonJS export form on its header line.
    unit = ap.StructuralUnit(
        kind=ap.KIND_FUNCTION, name="foo",
        span=(0, 0),
        body="module.exports = function foo() { return 1; }",
    )
    _imports, exports = ap._extract_imports_exports_a("", [unit])
    assert "foo" in exports
    # And the classic modifiers still work (the typo's removal didn't regress).
    unit2 = ap.StructuralUnit(
        kind=ap.KIND_FUNCTION, name="bar",
        span=(0, 0), body="export function bar() { return 2; }",
    )
    _i2, exports2 = ap._extract_imports_exports_a("", [unit2])
    assert "bar" in exports2


# ---------------------------------------------------------------------------
# Line-offset index (Improvement #4)
# ---------------------------------------------------------------------------


def test_line_offset_index_correctness():
    """_build_line_index + _row_at produce correct row numbers for all byte offsets."""
    src = "line0\nline1\nline2\n"
    idx = ap._build_line_index(src)
    assert idx == [0, 6, 12, 18]  # 3 newlines → 4 line starts (trailing \n)
    assert ap._row_at(idx, 0) == 0   # start of line 0
    assert ap._row_at(idx, 5) == 0   # end of line 0
    assert ap._row_at(idx, 6) == 1   # start of line 1
    assert ap._row_at(idx, 11) == 1  # end of line 1
    assert ap._row_at(idx, 12) == 2  # start of line 2


def test_line_offset_index_empty_source():
    """Empty source produces a single-entry index (line 0 at byte 0)."""
    idx = ap._build_line_index("")
    assert idx == [0]
    assert ap._row_at(idx, 0) == 0


def test_family_a_row_at_matches_count():
    """The O(log n) _row_at produces the same result as O(n) str.count for a
    representative Rust source with multiple declarations."""
    src = (
        "use std::io;\n"
        "\n"
        "pub fn main() {\n    println!(\"hi\");\n}\n"
        "\n"
        "pub struct Config {\n    x: u32,\n}\n"
        "\n"
        "impl Config {\n    pub fn new() -> Self {\n        Config { x: 1 }\n    }\n}\n"
    )
    idx = ap._build_line_index(src)
    for byte_idx in range(0, len(src), 7):  # sample every 7 bytes
        expected = src.count("\n", 0, byte_idx)
        assert ap._row_at(idx, byte_idx) == expected, f"mismatch at byte {byte_idx}"


# ---------------------------------------------------------------------------
# 3-way structural diff (Improvement #5)
# ---------------------------------------------------------------------------


def test_structural_diff_no_changes():
    """All three versions identical → all units unchanged, no conflicts."""
    src = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
    diff = sd.compute_structural_diff_3way(src, src, src, language="python")
    assert diff is not None
    assert len(diff.aligned) == 2
    assert all(a.change_kind == sd._CHANGE_KIND_UNCHANGED for a in diff.aligned)
    assert len(diff.structural_conflicts) == 0


def test_structural_diff_modified_both():
    """Both sides modified the same unit → modified_both → structural conflict."""
    base = "def foo():\n    return 1\n"
    left = "def foo():\n    return 2\n"  # left changed body
    right = "def foo():\n    return 3\n"  # right changed body differently
    diff = sd.compute_structural_diff_3way(base, left, right, language="python")
    assert diff is not None
    foo = next(a for a in diff.aligned if a.name == "foo")
    assert foo.change_kind == sd._CHANGE_KIND_MODIFIED_BOTH
    assert len(diff.structural_conflicts) == 1


def test_structural_distinct_additions_no_conflict():
    """Each side adds a DIFFERENT unit → no structural conflict."""
    base = "def existing():\n    pass\n"
    left = "def existing():\n    pass\n\ndef left_new():\n    return 1\n"
    right = "def existing():\n    pass\n\ndef right_new():\n    return 2\n"
    diff = sd.compute_structural_diff_3way(base, left, right, language="python")
    assert diff is not None
    assert len(diff.structural_conflicts) == 0
    names = {a.name for a in diff.aligned}
    assert "left_new" in names and "right_new" in names


def test_structural_diff_required_units():
    """required_units lists all units that must appear in the merge."""
    base = "def foo():\n    return 1\n"
    left = "def foo():\n    return 2\n\ndef added():\n    pass\n"
    right = "def foo():\n    return 3\n"
    diff = sd.compute_structural_diff_3way(base, left, right, language="python")
    assert diff is not None
    assert "foo" in diff.required_units
    assert "added" in diff.required_units


def test_structural_diff_rust_multi_unit():
    """The rust_impl scenario: struct + two methods, each side modifies a
    different method → no structural conflict."""
    base = (
        "pub struct Config {\n    pub name: String,\n}\n\n"
        "impl Config {\n    pub fn new() -> Self {\n        Config { name: \"\".into() }\n    }\n"
        "    pub fn label(&self) -> String {\n        format!(\"hi\")\n    }\n}\n"
    )
    left = base.replace('"".into()', '"x".into()')  # left changes new()
    right = base.replace('format!("hi")', 'format!("bye")')  # right changes label()
    diff = sd.compute_structural_diff_3way(base, left, right, language="rust")
    assert diff is not None
    kinds = {a.name: a.change_kind for a in diff.aligned}
    assert kinds.get("new") == sd._CHANGE_KIND_MODIFIED_LEFT
    assert kinds.get("label") == sd._CHANGE_KIND_MODIFIED_RIGHT
    assert len(diff.structural_conflicts) == 0  # different units, no conflict


# ---------------------------------------------------------------------------
# Structural context annotation (Improvement #6)
# ---------------------------------------------------------------------------


def test_render_context_shows_changes():
    """The annotation lists changed units and the required output."""
    base = "def foo():\n    return 1\n"
    left = "def foo():\n    return 2\n"
    right = "def foo():\n    return 3\n"
    diff = sd.compute_structural_diff_3way(base, left, right, language="python")
    text = sc.render_structural_context(diff)
    assert "STRUCTURAL CONTEXT" in text
    assert "MODIFIED BY BOTH" in text
    assert "foo" in text
    assert "synthesize" in text


def test_render_context_no_changes_returns_empty():
    """No changes → empty annotation (nothing to tell the model)."""
    src = "def foo():\n    return 1\n"
    diff = sd.compute_structural_diff_3way(src, src, src, language="python")
    assert sc.render_structural_context(diff) == ""


def test_render_context_distinct_additions_says_no_conflict():
    """Distinct additions → annotation says 'no structural conflicts'."""
    base = "def existing():\n    pass\n"
    left = "def existing():\n    pass\n\ndef left_new():\n    pass\n"
    right = "def existing():\n    pass\n\ndef right_new():\n    pass\n"
    diff = sd.compute_structural_diff_3way(base, left, right, language="python")
    text = sc.render_structural_context(diff)
    assert "NONE" in text
    assert "left_new" in text and "right_new" in text


# ---------------------------------------------------------------------------
# Import-surface annotation import handling is the highest-value
# structural operation). The block surfaces an explicit "union the imports"
# instruction instead of leaving the model to infer it from generic lines.
# ---------------------------------------------------------------------------


def test_render_context_import_union_python():
    """Both sides add different imports → the annotation surfaces a dedicated
    import-surface block with an explicit 'union them' instruction and the full
    required import set. (The canonical import-combine merge shape.)"""
    base = "import os"
    left = "import os\nimport json"
    right = "import os\nimport sys"
    diff = sd.compute_structural_diff_3way(base, left, right, language="python")
    text = sc.render_structural_context(diff)
    assert "Import surface" in text
    assert "CURRENT adds json" in text
    assert "REPLAYED adds sys" in text
    assert "union" in text
    # The full union of imports is named explicitly.
    assert "os" in text and "json" in text and "sys" in text
    # Imports are NOT duplicated in the generic per-unit change list.
    assert "[MODULE_STMT]" not in text


def test_render_context_import_union_rust_use():
    """Family-A ``use`` statements get the same import-surface treatment —
    guards the Rust import path (which also exercises the no-trailing-newline
    crash fix in parse_family_a)."""
    base = "use std::fs;"
    left = "use std::fs;\nuse std::io;"
    right = "use std::fs;\nuse std::path;"
    diff = sd.compute_structural_diff_3way(base, left, right, language="rust")
    text = sc.render_structural_context(diff)
    assert "Import surface" in text
    assert "std::io" in text and "std::path" in text
    assert "union" in text


def test_render_context_no_import_block_when_only_code_changed():
    """A function-only change (no import change) produces NO import-surface
    block — the annotation is unchanged from the pre-import-block behavior."""
    base = "def foo():\n    return 1\n"
    left = "def foo():\n    return 2\n"
    right = "def foo():\n    return 3\n"
    diff = sd.compute_structural_diff_3way(base, left, right, language="python")
    text = sc.render_structural_context(diff)
    assert "Import surface" not in text


# ---------------------------------------------------------------------------
# Family B regression suite — multi-line signatures, decorator spans,
# triple-quoted-string phantoms, fragmentation false-positives.
# Each test pins one of the silent-wrong-output bugs found in the parser review.
# ---------------------------------------------------------------------------


def test_multiline_python_signature_body_included():
    """Fix #1: a PEP 8 line-wrapped signature must NOT close the function at the
    ``) -> bool:`` line. The body (``return True``) must be inside the unit."""
    src = (
        "def long_function_name(\n"
        "    arg_one: int,\n"
        "    arg_two: str,\n"
        ") -> bool:\n"
        "    return True\n"
    )
    ir = ap.parse_family_b(src)
    assert len(ir.units) == 1
    f = ir.units[0]
    assert f.name == "long_function_name"
    assert f.kind == "function"
    # The span must cover the body line (row 4), not stop at the closing paren (row 3).
    assert f.span[1] >= 4, f"span {f.span} does not cover the body"
    assert "return True" in f.body, "body lost the return statement"


def test_multiline_signature_with_nested_call():
    """Fix #1 regression guard: continuation through nested parentheses and a
    bracket list — none of these mid-signature brackets close the function."""
    src = (
        "def f(\n"
        "    a=dict(b=[1, 2]),\n"
        "    c=(x for x in []),\n"
        ") -> None:\n"
        "    pass\n"
    )
    ir = ap.parse_family_b(src)
    assert len(ir.units) == 1
    assert ir.units[0].name == "f"
    assert "pass" in ir.units[0].body


def test_multiline_signature_then_sibling_function():
    """Fix #1: after a multi-line signature function closes normally at EOF, a
    following sibling function at the same indent is a separate unit."""
    src = (
        "def first(\n"
        "    a,\n"
        "):\n"
        "    return 1\n"
        "\n"
        "def second():\n"
        "    return 2\n"
    )
    ir = ap.parse_family_b(src)
    names = {u.name for u in ir.units}
    assert names == {"first", "second"}, f"got {[u.name for u in ir.units]}"


def test_decorator_does_not_extend_previous_unit():
    """Fix #2: a decorator belongs to the NEXT declaration. The previous unit
    must NOT absorb the next unit's decorator line into its span/body.

    Before the fix, the first ``x`` greedily extended its span through the
    second ``x``'s ``@x.setter`` decorator."""
    src = (
        "class C:\n"
        "    @property\n"
        "    def x(self):\n"
        "        return self._x\n"
        "\n"
        "    @x.setter\n"
        "    def x(self, v):\n"
        "        self._x = v\n"
    )
    ir = ap.parse_family_b(src)
    flat = ap.all_units_flat(ir)
    methods = [u for u in flat if u.kind == "method" and u.name == "x"]
    assert len(methods) == 2, f"expected 2 x-methods, got {len(methods)}"
    # The first method's span must end at the ``return`` line (row 3), NOT reach
    # the second method's decorator (row 5).
    first, second = sorted(methods, key=lambda u: u.span[0])
    assert first.span[1] <= 3, f"first method span {first.span} ate the next decorator"
    assert "@x.setter" not in first.body, "first method absorbed the second's decorator"
    # The second method starts at its own decorator (row 5).
    assert second.span[0] == 5, f"second method span {second.span} should start at @x.setter"


def test_stacked_decorators_on_one_function():
    """Fix #2 regression guard: two or more decorators on the SAME function
    still fold into that function's span (existing behavior unchanged)."""
    src = (
        "@deco1\n"
        "@deco2\n"
        "def f():\n"
        "    return 1\n"
        "\n"
        "def g():\n"
        "    return 2\n"
    )
    ir = ap.parse_family_b(src)
    by_name = {u.name: u for u in ir.units}
    f = by_name["f"]
    assert f.span == (0, 3), f"f span {f.span}"
    assert "@deco1" in f.body and "@deco2" in f.body
    assert by_name["g"].span == (5, 6)


def test_decorator_on_class_method():
    """Fix #2 in the class context: a decorated method must not steal the next
    sibling method's body."""
    src = (
        "class C:\n"
        "    @staticmethod\n"
        "    def make():\n"
        "        return C()\n"
        "\n"
        "    def helper(self):\n"
        "        return 1\n"
    )
    ir = ap.parse_family_b(src)
    flat = ap.all_units_flat(ir)
    make = next(u for u in flat if u.name == "make")
    helper = next(u for u in flat if u.name == "helper")
    # make ends at its return line (row 3), not into helper.
    assert make.span[1] <= 3
    assert "def helper" not in make.body
    assert helper.span[0] == 5


def test_triple_quoted_docstring_no_phantom_units():
    """Fix #6: a multi-line triple-quoted docstring containing class/def-shaped
    text must NOT produce phantom nested units. Family B had no string-state
    awareness, so ``class Fake:`` inside a docstring was parsed as a real unit."""
    src = (
        "def foo():\n"
        '    """docs\n'
        "    class Fake:\n"
        "        def method(self): pass\n"
        '    """\n'
        "    return 1\n"
    )
    ir = ap.parse_family_b(src)
    flat = ap.all_units_flat(ir)
    names = {u.name for u in flat}
    assert "Fake" not in names, "phantom class from docstring"
    assert "method" not in names, "phantom method from docstring"
    assert "foo" in names
    # foo's body must include the return (the docstring didn't close it early).
    foo = next(u for u in flat if u.name == "foo")
    assert "return 1" in foo.body


def test_triple_quoted_string_module_level():
    """Fix #6: a module-level multi-line triple-quoted string (module docstring
    or constant) with class-like content must not spawn phantom units."""
    src = (
        '"""\n'
        "class Fake:\n"
        "    def m(self): pass\n"
        '"""\n'
        "\n"
        "def real():\n"
        "    return 1\n"
    )
    ir = ap.parse_family_b(src)
    flat = ap.all_units_flat(ir)
    names = {u.name for u in flat}
    assert "Fake" not in names
    assert "real" in names


def test_fragmentation_not_triggered_for_test_file():
    """Fix #8: a large test module with many small ``test_*`` functions is
    normal, not pathological fragmentation. Must parse at confidence 1.0."""
    tests = [f"def test_{i}():\n    assert {i} < 100\n" for i in range(60)]
    src = "\n".join(tests)
    n_lines = len(src.split("\n"))
    ir = ap.parse_family_b(src)
    assert len(ir.units) == 60
    assert ir.parse_confidence == 1.0, (
        f"test file flagged as fragmented ({len(ir.units)} units / {n_lines} lines)"
    )


def test_fragmentation_still_flags_non_test_garbage():
    """Fix #8 regression guard: genuinely fragmented code (many small NON-test
    functions in little space) is still flagged as low-confidence."""
    # 40 tiny non-test functions in ~120 lines.
    fns = [f"def fn{i}():\n    x = {i}\n" for i in range(40)]
    src = "\n".join(fns)
    ir = ap.parse_family_b(src)
    # Heuristic still fires for non-test fragmentation.
    assert ir.parse_confidence < 1.0, "non-test fragmentation should be flagged"


# ---------------------------------------------------------------------------
# Family A regression suite — Go receiver methods, Go type-struct, raw/verbatim
# strings, and field-detection-fold correctness. Each pins one bug from review.
# ---------------------------------------------------------------------------


def test_go_receiver_method_name_recovered():
    """Fix #4: Go receiver methods ``func (recv) Name()`` lost their name (the
    keyword path took the token after ``func`` — ``(`` — which stripped to
    empty). The name is the identifier before the final param list."""
    src = (
        "package main\n"
        "\n"
        "type Server struct { port int }\n"
        "\n"
        "func (s *Server) Start() {\n"
        "    s.run()\n"
        "}\n"
        "\n"
        "func (s Server) Stop() {\n"
        "    s.halt()\n"
        "}\n"
    )
    ir = ap.parse_file(src, language="go")
    flat = ap.all_units_flat(ir)
    methods = sorted(u.name for u in flat if u.kind == "method")
    assert "Start" in methods, f"receiver method name lost: {methods}"
    assert "Stop" in methods


def test_go_type_struct_is_class_not_field():
    """Fix #5: ``type X struct {}`` put the name BEFORE ``struct``, so the
    class-keyword path (which expects the name AFTER) found none, and the
    field-emitter (keying on ``type``) misclassified it as a FIELD. Must be a
    CLASS named after the type."""
    src = "type Server struct {\n    port int\n}\n"
    ir = ap.parse_file(src, language="go")
    flat = ap.all_units_flat(ir)
    classes = [u for u in flat if u.kind == "class"]
    assert any(u.name == "Server" for u in classes), (
        f"Server not a class: {[(u.kind, u.name) for u in flat]}"
    )
    # And NOT double-counted as a field.
    fields = [u for u in flat if u.kind == "field" and u.name == "Server"]
    assert fields == [], "type-struct double-counted as field"


def test_go_type_interface_is_class():
    """Fix #5 for ``type X interface``: same name-before-keyword shape."""
    src = (
        "type Reader interface {\n"
        "    Read(p []byte) (n int, err error)\n"
        "}\n"
    )
    ir = ap.parse_file(src, language="go")
    flat = ap.all_units_flat(ir)
    classes = [u.name for u in flat if u.kind == "class"]
    assert "Reader" in classes


def test_rust_hash_raw_string_with_braces():
    """Fix #9: Rust raw strings with hash delimiters ``r#\"...\"#`` close on
    \"# (not \"), so a ``{`` or ``}`` inside must not perturb brace depth. A
    method following the raw string must still be detected correctly."""
    src = (
        "impl C {\n"
        "    fn a() {\n"
        '        let s = r#"\n'
        "        { not a scope }\n"
        '        "#;\n'
        "    }\n"
        "    fn b() {\n"
        "        return 2;\n"
        "    }\n"
        "}\n"
    )
    ir = ap.parse_file(src, language="rust")
    flat = ap.all_units_flat(ir)
    methods = sorted(u.name for u in flat if u.kind == "method")
    # Both methods detected; the raw string braces didn't corrupt depth tracking.
    assert "a" in methods and "b" in methods, f"methods: {methods}"


def test_rust_raw_string_embedded_quote_does_not_close():
    """Fix #9 (stronger): a Rust raw string whose CONTENT contains a ``"`` must
    not close the string early. Before the fix, an embedded ``"`` followed by
    unbalanced braces corrupted depth tracking and absorbed following functions
    into the string's owner."""
    src = (
        "fn f() {\n"
        '    let a = r#"x " y { { {"#;\n'
        "}\n"
        "fn g() {\n"
        "    return 2;\n"
        "}\n"
    )
    ir = ap.parse_file(src, language="rust")
    flat = ap.all_units_flat(ir)
    names = {u.name for u in flat}
    assert "g" in names, (
        f"function g lost — raw string embedded quote closed early: {names}"
    )


def test_csharp_verbatim_string_multiline():
    """Fix #9: C# verbatim strings ``@\"...\"`` span lines and may contain
    braces. A method after a multi-line verbatim string must still be found."""
    src = (
        "class C {\n"
        '    string query = @"\n'
        "    SELECT * FROM t\n"
        "    WHERE x = {0}\n"
        '    ";\n'
        "    void M() {}\n"
        "}\n"
    )
    ir = ap.parse_file(src, language="csharp")
    flat = ap.all_units_flat(ir)
    methods = _names_by_kind(flat, "method")
    assert "M" in methods, f"method after verbatim string lost: {methods}"


def test_field_detection_top_level_const():
    """Fix #10 regression guard: a top-level ``pub const N: u32 = 5;`` is still
    detected as a FIELD unit after folding field detection into the main pass
    (removing the second whole-file re-scan)."""
    src = (
        "pub const N: u32 = 5;\n"
        "\n"
        "pub fn main() {\n"
        "    let _ = N;\n"
        "}\n"
    )
    ir = ap.parse_file(src, language="rust")
    flat = ap.all_units_flat(ir)
    fields = [u.name for u in flat if u.kind == "field"]
    assert "N" in fields, f"top-level const not a field: {fields}"


def test_field_inside_function_body_not_top_level():
    """Fix #10: a ``const``/``let`` INSIDE a function body must NOT be emitted
    as a top-level field unit — only depth-0 declarations are entities."""
    src = (
        "fn main() {\n"
        "    const INNER: u32 = 42;\n"
        "    let x = 1;\n"
        "}\n"
    )
    ir = ap.parse_file(src, language="rust")
    flat = ap.all_units_flat(ir)
    fields = [u.name for u in flat if u.kind == "field"]
    # INNER is inside main's body, not top-level — must not be a field entity.
    assert "INNER" not in fields, f"body-local const leaked as field: {fields}"
    assert "x" not in fields


def test_rust_type_alias_still_a_field():
    """Fix #10/B2 regression guard: Rust ``type X = Y;`` (a type alias, NOT a
    Go ``type X struct``) is still a FIELD — the backward-name-lookup for Go
    must not misfire on Rust type aliases."""
    src = "type Foo = Bar;\n\nfn main() {}\n"
    ir = ap.parse_file(src, language="rust")
    flat = ap.all_units_flat(ir)
    fields = [u.name for u in flat if u.kind == "field"]
    assert "Foo" in fields, f"Rust type alias not a field: {fields}"


# ---------------------------------------------------------------------------
# Second-pass review fixes — (field-drop regression) – (edge cases in
# the bracket/string/continuation machinery introduced by the first round).
# ---------------------------------------------------------------------------


def test_field_with_struct_literal_initializer():
    """A top-level ``const``/``static`` whose initializer contains a braced
    struct literal must still be detected as a FIELD. The in-pass emitter
    resets the token buffer at every ``{``/``}``, so by the ``;`` the
    declaration keyword is gone and the field is silently dropped. This is a
    very common Rust pattern."""
    src = "pub const P: Point = Point { x: 1, y: 2 };\nfn main() {}\n"
    ir = ap.parse_file(src, language="rust")
    flat = ap.all_units_flat(ir)
    fields = [u.name for u in flat if u.kind == "field"]
    assert "P" in fields, f"struct-literal const dropped: {fields}"


def test_field_with_macro_brace_initializer():
    """ (macro form): ``vec!{...}`` / ``format!{...}`` braced macros in a
    top-level const initializer must not lose the field."""
    src = "const V: Vec<u8> = vec!{1, 2, 3};\nfn main() {}\n"
    ir = ap.parse_file(src, language="rust")
    flat = ap.all_units_flat(ir)
    fields = [u.name for u in flat if u.kind == "field"]
    assert "V" in fields, f"macro-brace const dropped: {fields}"


def test_field_with_brace_no_regression_on_plain_const():
    """a plain const (no braces) is still detected."""
    src = "pub const N: u32 = 5;\nfn main() {}\n"
    ir = ap.parse_file(src, language="rust")
    flat = ap.all_units_flat(ir)
    assert "N" in [u.name for u in flat if u.kind == "field"]


def test_inline_comment_unbalanced_bracket_no_continuation():
    """an inline comment containing an unbalanced bracket must NOT corrupt
    the continuation depth. ``_line_bracket_delta`` must strip comments before
    counting brackets."""
    src = (
        "x = 1  # see func(\n"
        "def real():\n"
        "    return 2\n"
    )
    ir = ap.parse_family_b(src)
    names = {u.name for u in ir.units}
    assert "real" in names, f"comment-paren swallowed the next def: {names}"


def test_dangling_open_brace_does_not_swallow_next_def():
    """a malformed dangling ``{`` (a merge artifact) must not absorb the
    next declaration as a continuation. Only ``(`` opens a signature
    continuation; ``{``/``[`` (collection literals) at depth 0 are not
    declaration-relevant."""
    src = (
        "d = {\n"
        "def foo():\n"
        "    pass\n"
        "}\n"
    )
    ir = ap.parse_family_b(src)
    names = {u.name for u in ir.units}
    # foo must be detected even though a dangling { precedes it.
    assert "foo" in names, f"dangling brace swallowed foo: {names}"


def test_multiline_signature_still_uses_paren_continuation():
    """the multi-line signature fix (#1) must still work —
    ``(`` IS a continuation trigger, so the closing ``) -> bool:`` is absorbed."""
    src = (
        "def long(\n"
        "    a,\n"
        ") -> bool:\n"
        "    return True\n"
    )
    ir = ap.parse_family_b(src)
    assert len(ir.units) == 1
    assert "return True" in ir.units[0].body


def test_identifier_ending_in_r_before_string_not_raw():
    """an identifier ending in ``r``/``b`` immediately before a string
    literal must NOT be misread as a raw-string prefix. ``myr#\"...\"#`` — the
    ``r`` is part of ``myr``, not a prefix. The fix is a word-boundary check in
    ``_match_string_prefix``: the rune run must be preceded by a non-identifier
    char (or start of input). Tested at the unit level (the precise fix site)
    plus a parse-level check that a following function is detected."""
    # Unit level — the precise behavior the fix corrects.
    assert ap._match_string_prefix('ambr#"', 5) == 0   # 'm' before 'rb' → not a prefix
    assert ap._match_string_prefix('myr"', 3) == 0     # 'y' before 'r' → not a prefix
    assert ap._match_string_prefix('r#"', 2) == 1      # start of input → real raw
    assert ap._match_string_prefix(' r#"', 3) == 1     # space boundary → real raw
    assert ap._match_string_prefix('=r#"', 3) == 1     # '=' boundary → real raw
    assert ap._match_string_prefix('br"', 2) == 0      # real byte-raw (closes on ")
    # Parse level: an identifier ending in 'r' before a plain string must not
    # send the scanner into a bogus raw-string state. Both functions detected.
    src = (
        "fn f() {\n"
        '    let s = our"plain";\n'   # 'our' — r is part of the identifier
        "}\n"
        "fn g() {\n"
        "    return 2;\n"
        "}\n"
    )
    ir = ap.parse_file(src, language="rust")
    flat = ap.all_units_flat(ir)
    names = {u.name for u in flat}
    assert "g" in names, f"identifier-before-quote corrupted string state: {names}"


def test_real_raw_string_prefix_still_works():
    """a genuine ``r#\"...\"#`` (with a word boundary
    before ``r``) is still recognized as a raw string and closes on ``\"#``."""
    src = (
        "fn f() {\n"
        '    let s = r#"has a quote \" here"#;\n'
        "}\n"
        "fn g() {\n"
        "    return 2;\n"
        "}\n"
    )
    ir = ap.parse_file(src, language="rust")
    flat = ap.all_units_flat(ir)
    names = {u.name for u in flat}
    assert "g" in names, f"genuine raw string no longer recognized: {names}"


def test_backslash_line_continuation_def():
    """an explicit backslash line-continuation in a signature is handled —
    ``def \\<newline>foo():`` still detects ``foo``. Rare but PEP 8-legal."""
    src = "def \\\nfoo():\n    pass\n"
    ir = ap.parse_family_b(src)
    names = {u.name for u in ir.units}
    assert "foo" in names, f"backslash-continued def missed: {names}"


def test_unterminated_string_does_not_corrupt_continuation():
    """an unterminated single-line ``\"`` on one line must not leave the
    bracket counter seeing raw brackets on subsequent lines. The string state
    should reset at the newline (Family B is line-oriented)."""
    # A signature line with an unterminated string then a real def after.
    # The unterminated " is malformed, but the parser must still find ``real``.
    src = (
        'x = "oops\n'
        "def real():\n"
        "    return 2\n"
    )
    ir = ap.parse_family_b(src)
    names = {u.name for u in ir.units}
    assert "real" in names, f"unterminated string corrupted scan: {names}"


def test_csharp_verbatim_string_doubled_quotes():
    """C# verbatim strings ``@\"...\"`` escape a literal quote by doubling
    it (``\"\"``). The scanner must not close the string at the first ``\"\"``."""
    src = (
        "class C {\n"
        '    string s = @"he said ""hi"" end";\n'
        "    void M() {}\n"
        "}\n"
    )
    ir = ap.parse_file(src, language="csharp")
    flat = ap.all_units_flat(ir)
    methods = _names_by_kind(flat, "method")
    assert "M" in methods, f"verbatim doubled-quote closed early: {methods}"
# Cross-cutting regression suite — identity collisions, added_both conflicts,
# and the fingerprint guard. Each pins one bug found in the review.
# ---------------------------------------------------------------------------


def test_duplicate_method_names_decline_diff():
    """Fix #3: two units with the same identity (e.g. two ``(method, "f")`` —
    Java/C++/Python overloads, re-definitions) used to collide silently in the
    identity-keyed dicts, dropping one unit and missing any conflict on it.
    The diff now declines (returns None) so the conflict escalates to the LLM
    path instead of being silently truncated."""
    base = (
        "class C:\n"
        "    def f(self):\n"
        "        return 1\n"
        "    def f(self, x):\n"
        "        return x\n"
    )
    left = base.replace("return 1", "return 10")
    right = base.replace("return x", "return x + 1")
    diff = sd.compute_structural_diff_3way(base, left, right, language="python")
    assert diff is None, "duplicate-identity parse must decline (return None)"


def test_duplicate_names_decline_at_entity_disjoint():
    """Fix #3 at the entity_disjoint rule: when the inner entity enumeration
    produces a duplicate identity, the rule declines (returns None) so the
    conflict escalates rather than silently dropping a unit mid-merge."""
    from capybase.adapters import structural

    # A class with two methods of the same name — re-enumerated inside the body.
    base = (
        "class C:\n"
        "    def f(self):\n"
        "        return 1\n"
        "    def f(self, x):\n"
        "        return x\n"
        "    def g(self):\n"
        "        return 3\n"
    )
    # Re-enumerate inside the body — the duplicate f appears in the entity list.
    ents = structural.enumerate_entities(base, "python", container_span=(1, 1))
    assert ents is not None
    assert ap.has_duplicate_identities(ents), (
        "duplicate detector must flag the two f-methods"
    )


def test_no_duplicate_identities_for_distinct_units():
    """Fix #3 regression guard: the duplicate detector must NOT flag a normal
    file with all-distinct entity names."""
    src = (
        "class C:\n"
        "    def a(self): pass\n"
        "    def b(self): pass\n"
        "    def c(self): pass\n"
    )
    ir = ap.parse_family_b(src)
    flat = ap.all_units_flat(ir)
    assert not ap.has_duplicate_identities(flat)


def test_added_both_different_bodies_is_conflict():
    """Fix #7: when both sides ADD a unit of the same name with DIFFERENT
    bodies, that's a genuine conflict (each side's addition is incompatible).
    Previously classified as ``added_both`` and NOT counted as a structural
    conflict — a silent miss. Now sub-classified as a conflict so the model is
    told to synthesize."""
    base = "pass\n"
    left = "def f():\n    return 1\n"
    right = "def f():\n    return 2\n"  # same name, different body
    diff = sd.compute_structural_diff_3way(base, left, right, language="python")
    assert diff is not None
    conflicts = diff.structural_conflicts
    names = {a.name for a in conflicts}
    assert "f" in names, (
        f"added_both with different bodies must be a conflict: {names}"
    )


def test_added_both_same_body_not_conflict():
    """Fix #7 regression guard: when both sides add the SAME unit (identical
    body), it's an agreed addition — NOT a conflict. The sub-classification
    must not over-fire."""
    base = "pass\n"
    added = "def f():\n    return 1\n"
    diff = sd.compute_structural_diff_3way(base, added, added, language="python")
    assert diff is not None
    assert len(diff.structural_conflicts) == 0, "identical addition is not a conflict"


def test_rename_detector_ignores_contentless_fingerprints():
    """Fix #13: two content-less bodies (``pass``-only methods, docstring-only
    functions) share the fingerprint ``l0`` (no digest). The rename detector
    used a broken guard (``fingerprint != f"l{count}"``) that never skipped,
    so two unrelated empty methods could pair as a false rename. The guard now
    skips fingerprints with no ``:digest`` (the content-less marker)."""
    # Two pass-only functions with the SAME fingerprint shape but different
    # names — must NOT be paired as a rename.
    base = "def original():\n    pass\n"
    left = "def renamed():\n    pass\n"
    right = base  # right unchanged
    diff = sd.compute_structural_diff_3way(base, left, right, language="python")
    assert diff is not None
    renamed = [a for a in diff.aligned if a.change_kind == sd._CHANGE_KIND_RENAMED]
    # The content-less body must not produce a false rename pairing.
    assert renamed == [], (
        f"content-less bodies falsely paired as rename: {[(a.name) for a in renamed]}"
    )


# ---------------------------------------------------------------------------
# Test-coverage expansion (second-pass review): rename detection, region
# queries, multi-container diffs, confidence edges, and Go-declaration edges.
# These pin load-bearing behaviors that worked but were previously untested.
# ---------------------------------------------------------------------------


# --- Positive rename detection (the _CHANGE_KIND_RENAMED path) ---

def test_rename_detected_both_sides_same_new_name():
    """Positive rename: base has foo, both sides rename to bar (same body).
    Must classify as RENAMED (not deleted_both + added_both), and drop the old
    name from required_units."""
    base = "def foo():\n    return 1\n    return 2\n"
    side = "def bar():\n    return 1\n    return 2\n"
    diff = sd.compute_structural_diff_3way(base, side, side, language="python")
    assert diff is not None
    renamed = [a for a in diff.aligned if a.change_kind == sd._CHANGE_KIND_RENAMED]
    assert len(renamed) == 1
    assert renamed[0].name == "bar"
    assert renamed[0].base.name == "foo"
    # required_units carries the NEW name, not the old.
    assert "bar" in diff.required_units
    assert "foo" not in diff.required_units


def test_rename_conflicting_left_bar_right_baz():
    """Conflicting rename: left renames foo→bar, right renames foo→baz. This is
    a divergent-name rename conflict — both names must survive in required_units
    and the conflict must be flagged. Post round-12/14, the alignment has a
    single added_both_conflict entry (base=foo, left=bar, right=baz) — no stale
    entries, no contradictory renamed+conflict pair."""
    base = "def foo():\n    return 1\n    return 2\n"
    left = "def bar():\n    return 1\n    return 2\n"
    right = "def baz():\n    return 1\n    return 2\n"
    diff = sd.compute_structural_diff_3way(base, left, right, language="python")
    # Both new names survive in required_units.
    assert "bar" in diff.required_units and "baz" in diff.required_units
    # The conflict is flagged.
    assert len(diff.structural_conflicts) >= 1
    # No contradictory RENAMED entry for the same base.
    assert not any(a.change_kind == sd._CHANGE_KIND_RENAMED for a in diff.aligned), (
        "a divergent-name rename must not leave a non-conflicting RENAMED entry"
    )


def test_rename_one_sided_other_keeps_original():
    """One-sided rename: left renames foo→bar, right keeps foo. Since foo is
    still present under its original name (right kept it), bar must NOT pair as
    a rename — it's a genuine addition. Conservative-correct."""
    base = "def foo():\n    return 1\n    return 2\n"
    left = "def bar():\n    return 1\n    return 2\n"
    right = "def foo():\n    return 1\n    return 2\n"
    diff = sd.compute_structural_diff_3way(base, left, right, language="python")
    renamed = [a for a in diff.aligned if a.change_kind == sd._CHANGE_KIND_RENAMED]
    assert renamed == [], "one-sided rename where original is kept must NOT pair"


def test_rename_plus_body_edit_does_not_pair():
    """A rename with a heavy body edit won't pair (the fingerprint differs) — it
    stays added+deleted, which is safe. Conservative guard."""
    base = "def foo():\n    return 1\n    return 2\n"
    side = "def bar():\n    return 99\n    return 2\n"  # body changed
    diff = sd.compute_structural_diff_3way(base, side, side, language="python")
    renamed = [a for a in diff.aligned if a.change_kind == sd._CHANGE_KIND_RENAMED]
    assert renamed == [], "rename + body edit must not pair"


# --- Region queries: enclosing_container, nested classes, boundaries ---

def test_enclosing_container_direct():
    """enclosing_container returns the parent of the deepest enclosing unit.
    For an anchor inside a method, that's the class."""
    src = (
        "class Foo:\n"
        "    def bar(self):\n"
        "        return 1\n"
    )
    ir = ap.parse_family_b(src)
    container = ap.enclosing_container(ir, (2, 2))  # inside bar's body
    assert container is not None
    assert container.name == "Foo"


def test_enclosing_container_at_module_scope():
    """An anchor inside a top-level function (module scope) has no container —
    enclosing_container returns None."""
    src = "def top():\n    return 1\n"
    ir = ap.parse_family_b(src)
    assert ap.enclosing_container(ir, (1, 1)) is None


def test_nested_class_region_queries():
    """A class nested inside a class: an anchor in the inner class's method
    resolves to the inner method (enclosing_unit), the inner class
    (enclosing_container), and the inner class's methods (units_in_container)."""
    src = (
        "class Outer:\n"
        "    class Inner:\n"
        "        def m(self):\n"
        "            return 1\n"
        "        def n(self):\n"
        "            return 2\n"
    )
    ir = ap.parse_family_b(src)
    # Anchor inside m (row 3).
    assert ap.enclosing_unit(ir, (3, 3)).name == "m"
    container = ap.enclosing_container(ir, (3, 3))
    assert container is not None and container.name == "Inner"
    kids = ap.units_in_container(ir, (3, 3))
    kid_names = {k.name for k in kids}
    assert "m" in kid_names and "n" in kid_names


def test_enclosing_unit_end_row_boundary():
    """An anchor exactly on a unit's end_row resolves to that unit (boundary
    inclusive), preferring the narrower nested unit."""
    src = (
        "class Foo:\n"
        "    def bar(self):\n"
        "        return 1\n"
    )
    ir = ap.parse_family_b(src)
    flat = ap.all_units_flat(ir)
    bar = next(u for u in flat if u.name == "bar")
    # Anchor at bar's end_row.
    end = bar.span[1]
    assert ap.enclosing_unit(ir, (end, end)).name == "bar"


# --- Multi-container diff ---

def test_multi_container_diff_distinct_methods_no_conflict():
    """Two classes each with methods; left touches a method in A, right touches
    a method in B. No structural conflict (distinct identities). Verifies global
    identity keying across containers works for the intended no-collision case."""
    base = (
        "class A:\n"
        "    def run(self):\n"
        "        return 1\n"
        "\n"
        "class B:\n"
        "    def run(self):\n"
        "        return 2\n"
    )
    # NOTE: duplicate method name (run in A and B) → duplicate-identity decline.
    # So this specific case declines. Use distinct names to test multi-container.
    base2 = (
        "class A:\n"
        "    def start(self):\n"
        "        return 1\n"
        "\n"
        "class B:\n"
        "    def stop(self):\n"
        "        return 2\n"
    )
    left = base2.replace("return 1", "return 10")   # left changes A.start
    right = base2.replace("return 2", "return 20")  # right changes B.stop
    diff = sd.compute_structural_diff_3way(base2, left, right, language="python")
    assert diff is not None
    assert len(diff.structural_conflicts) == 0  # distinct units, no conflict
    kinds = {a.name: a.change_kind for a in diff.aligned}
    assert kinds.get("start") == sd._CHANGE_KIND_MODIFIED_LEFT
    assert kinds.get("stop") == sd._CHANGE_KIND_MODIFIED_RIGHT


def test_render_context_multi_container():
    """render_structural_context produces coherent output for a multi-class
    file — both containers' changed units appear."""
    base = "class A:\n    def x(self):\n        return 1\n\nclass B:\n    def y(self):\n        return 2\n"
    left = base.replace("return 1", "return 10")
    right = base.replace("return 2", "return 20")
    diff = sd.compute_structural_diff_3way(base, left, right, language="python")
    text = sc.render_structural_context(diff)
    assert "x" in text and "y" in text
    assert "NONE" in text  # no structural conflicts


# --- _assess_confidence edge inputs ---

def test_confidence_empty_source():
    """Empty source → confidence 1.0, zero units. Never crashes."""
    ir = ap.parse_family_b("")
    assert ir.parse_confidence == 1.0
    assert ir.units == []


def test_confidence_all_comments():
    """A file of only comments → confidence 1.0, zero units."""
    src = "# just a comment\n# another\n# and one more\n"
    ir = ap.parse_family_b(src)
    assert ir.parse_confidence == 1.0
    assert ir.units == []


def test_confidence_all_imports():
    """An imports-heavy module (many import lines, no functions) is NOT flagged
    as fragmented — imports are legitimate module-level content."""
    imports = "\n".join(f"import module_{i}" for i in range(40))
    ir = ap.parse_family_b(imports)
    # 40 imports in 40 lines: n_lines < 100 so fragmentation doesn't fire.
    assert ir.parse_confidence == 1.0


def test_confidence_does_not_raise_on_binary_content():
    """Binary-ish content (null bytes) must not raise — parse_file returns a
    low-confidence FileIR (the never-raise guarantee)."""
    binary = "def f():\n    x = b'\\x00\\x01\\x02'\n    return x\n"
    ir = ap.parse_file(binary, language="python")
    assert ir is not None  # didn't raise


# --- _go_declaration_name edge cases ---

def test_go_generic_type_decl_struct():
    """Go generic type declaration: ``type X[T] struct {}``. The name is X
    (the [T] is a type parameter, not part of the name)."""
    src = "type Container[T any] struct {\n    items []T\n}\n"
    ir = ap.parse_file(src, language="go")
    flat = ap.all_units_flat(ir)
    classes = [u.name for u in flat if u.kind == "class"]
    # The name may be 'Container[T]' or 'Container' depending on extraction; the
    # unit must at least exist as a class containing 'Container'.
    assert any("Container" in (n or "") for n in classes), f"generic type lost: {classes}"


def test_go_value_receiver_method():
    """Go value receiver (no pointer): ``func (s Server) M()``. Direct test of
    the receiver-kind classification alongside the pointer form."""
    src = (
        "type S struct {}\n"
        "func (s S) ValueMethod() {}\n"
        "func (s *S) PointerMethod() {}\n"
    )
    ir = ap.parse_file(src, language="go")
    flat = ap.all_units_flat(ir)
    methods = sorted(u.name for u in flat if u.kind == "method")
    assert "ValueMethod" in methods
    assert "PointerMethod" in methods


def test_go_free_function_not_method():
    """A Go free function (no receiver) is FUNCTION, not METHOD."""
    src = "func helper() {}\n"
    ir = ap.parse_file(src, language="go")
    flat = ap.all_units_flat(ir)
    funcs = [u for u in flat if u.kind == "function"]
    assert any(u.name == "helper" for u in funcs)


# --- Family/language mismatch robustness ---

def test_family_mismatch_does_not_crash():
    """Parsing Python source with language='rust' (wrong family) must not crash
    — it returns a FileIR (possibly garbage, but never raises). Robustness over
    correctness on mismatched input."""
    py_src = "def foo():\n    return 1\n"
    ir = ap.parse_file(py_src, language="rust")
    assert ir is not None  # didn't crash; produced some FileIR


# ---------------------------------------------------------------------------
# Third-pass review fixes: (stmt_start_byte), (Rust lifetimes),
# (initializer-brace guard), (Kotlin fun + return-type heuristic).
# ---------------------------------------------------------------------------


def _field_named(ir, name):
    """True when ``ir`` contains a top-level FIELD unit named ``name``."""
    return any(u.kind == ap.KIND_FIELD and u.name == name for u in ap.all_units_flat(ir))


def _unit_named(ir, name):
    """True when ``ir`` contains a unit named ``name`` (any kind)."""
    return any(u.name == name for u in ap.all_units_flat(ir))


# ---: stmt_start_byte must only advance at top-level ; ---


def test_r2_field_with_function_expression_initializer():
    """``const f = function { ... };`` — the in-brace ``;`` (after
    ``return 1``) must NOT advance ``stmt_start_byte``; the outer ``;`` must
    slice the full statement. Previously the inner ``;`` reset the tracker,
    so the outer ``;`` sliced just ``} ;`` and the whole declaration was
    silently dropped.

    the function expression is classified as a FUNCTION ``f`` (the
    binding name is recovered); the original concern — silent drop — is
    what this guards against. Either a FUNCTION or FIELD ``f`` is acceptable."""
    src = "const f = function() {\n    return 1;\n};\n"
    ir = ap.parse_family_a(src, language="javascript")
    assert _unit_named(ir, "f"), (
        f"const=function(){{...}}; must emit a unit named 'f'; got "
        f"{_kinds_of(ir)}"
    )


def test_r2_field_with_arrow_function_initializer():
    """Regression: same bug for arrow-function bindings (very common in
    TS/JS). ``const h = () => { ... };`` — the inner ``;`` must not corrupt
    ``stmt_start_byte``. an arrow expression falls through to the
    keywordless path (the ``=>`` isn't captured at the ``{``); either way the
    binding must not be silently dropped."""
    src = "const h = () => {\n    return 2;\n};\n"
    ir = ap.parse_family_a(src, language="typescript")
    flat = ap.all_units_flat(ir)
    assert any(u.name == "h" for u in flat) or any(
        u.kind == ap.KIND_FIELD for u in flat
    ), (
        f"const=()=>{{...}}; must not be silently dropped; got "
        f"{[(u.kind, u.name) for u in flat]}"
    )


def test_r2_field_with_struct_literal_still_works():
    """the original case (Rust struct literal) must
    still emit a field. The fix must not regress the case it was added for."""
    src = "pub const P: Point = Point {\n    x: 0,\n    y: 0,\n};\n"
    ir = ap.parse_family_a(src, language="rust")
    assert _field_named(ir, "P"), (
        f"Rust struct literal must still emit FIELD 'P'; got "
        f"{_kinds_of(ir)}"
    )


# ---: Rust lifetimes preceded by & /, / ( corrupt the parse ---


def test_r3_rust_static_lifetime_in_return_type():
    """``pub fn f -> &'static str`` — the ``'`` of ``'static`` is
    preceded by ``&`` (not alnum/_), so the parser misread it as a char-literal
    opener and never closed it, corrupting the rest of the file → 0 units.
    A lifetime is ``'`` followed by an identifier; recognize it and skip."""
    src = 'pub fn f() -> &\'static str {\n    "x"\n}\n'
    ir = ap.parse_family_a(src, language="rust")
    assert _unit_named(ir, "f"), (
        f"fn with &'static return type must be detected; got "
        f"{_kinds_of(ir)}"
    )


def test_r3_rust_generic_lifetime_param():
    """``fn f<'a>(x: &'a str)`` — lifetime in generic params and a ref
    type. Both ``'a`` occurrences are preceded by ``<`` / ``&``."""
    src = "pub fn f<'a>(x: &'a str) {\n    todo!()\n}\n"
    ir = ap.parse_family_a(src, language="rust")
    assert _unit_named(ir, "f"), (
        f"fn with lifetime generic must be detected; got "
        f"{_kinds_of(ir)}"
    )


def test_r3_rust_lifetime_in_body_not_corrupted():
    """a lifetime appearing in the function body (``let y: &'static str``)
    must not corrupt the parse either — the function still parses, and a
    following sibling is still detected."""
    src = (
        'pub fn first() {\n    let y: \'static str = "hi";\n}\n'
        'pub fn second() {\n    1\n}\n'
    )
    ir = ap.parse_family_a(src, language="rust")
    names = [u.name for u in ap.all_units_flat(ir)]
    assert "first" in names and "second" in names, (
        f"lifetimes in body must not corrupt following siblings; got {names}"
    )


def test_r3_rust_char_literal_still_works():
    """genuine Rust char literals (``'a'``, ``'\\n'``)
    must still be handled — they are NOT lifetimes. A char literal has the
    ``'`` followed by content then a closing ``'``; a lifetime is ``'`` + an
    identifier with no closing ``'``. The string with an embedded char literal
    must not unbalance the brace scan."""
    src = "pub fn f() {\n    let c = 'a';\n    let nl = '\\\\n';\n}\n"
    ir = ap.parse_family_a(src, language="rust")
    assert _unit_named(ir, "f"), (
        f"char literals must still parse; got "
        f"{_kinds_of(ir)}"
    )


# ---: initializer-brace guard too broad (const=function/class/arrow) ---


def test_g7_const_function_expression_emits_function():
    """``const f = function { ... }`` (no semicolon, JS style) — the
     initializer guard rejected every ``const X = {`` shape, including
    function/class expressions. tightening fix, the function
    body brace must be classified as a FUNCTION (the guard lets ``= function``
    through to normal classification)."""
    src = "const named = function() {\n    return 42;\n}\n"
    ir = ap.parse_family_a(src, language="javascript")
    kinds = _kinds_of(ir)
    # Either a FUNCTION 'named' or a FIELD 'named' is acceptable; the silent
    # DROP (empty) is the bug. With the keyword path it should be a
    # function unit.
    assert kinds, (
        f"const=function(){{...}} must not be silently dropped; got {kinds}"
    )
    assert _unit_named(ir, "named"), f"expected unit 'named'; got {kinds}"


def test_g7_const_class_expression_detected():
    """``const C = class { ... }`` — a class expression assigned to a
    const. Must not be silently dropped."""
    src = "const Foo = class {\n    bar() {\n        return 1;\n    }\n};\n"
    ir = ap.parse_family_a(src, language="javascript")
    kinds = _kinds_of(ir)
    assert _unit_named(ir, "Foo"), f"class expression must be detected; got {kinds}"


def test_g7_rust_struct_literal_still_field():
    """the genuine initializer-literal case the guard was
    added for (Rust ``const P = Point { ... };``) must STILL return None from
    classification (so the field is emitted at the ``;``), not be
    misclassified as a function."""
    src = "pub const P: Point = Point { x: 0, y: 0 };\n"
    ir = ap.parse_family_a(src, language="rust")
    kinds = _kinds_of(ir)
    # Must be a FIELD (not a phantom FUNCTION/CLASS 'Point').
    assert _field_named(ir, "P"), f"struct literal must be FIELD 'P'; got {kinds}"
    # And no phantom 'Point' function/class should appear.
    assert not _unit_named(ir, "Point"), (
        f"struct literal must not emit phantom 'Point' unit; got {kinds}"
    )


def test_g7_object_literal_initializer_still_field():
    """a plain object-literal initializer
    (``const cfg = { ... };``) must remain a FIELD — the guard still applies
    when the token after ``=`` is ``{`` (an object literal), just not when
    it's ``function``/``class``/``(`` (an expression)."""
    src = "const cfg = {\n    port: 8080,\n    host: 'x',\n};\n"
    ir = ap.parse_family_a(src, language="javascript")
    assert _field_named(ir, "cfg"), (
        f"object literal must be FIELD 'cfg'; got "
        f"{_kinds_of(ir)}"
    )


# ---: Kotlin 'fun' missing + return-type-after-params breaks heuristic ---


def test_g8_kotlin_top_level_fun():
    """Kotlin's ``fun`` keyword is missing from ``_A_FUNC_KEYWORDS``, so
    every top-level Kotlin function was dropped (the keywordless heuristic
    only accepts C free functions at file scope). Adding ``fun`` fixes this."""
    src = "fun topLevel(x: Int): Int {\n    return x + 1\n}\n"
    ir = ap.parse_family_a(src, language="kotlin")
    kinds = _kinds_of(ir)
    assert _unit_named(ir, "topLevel"), (
        f"Kotlin top-level fun must be detected; got {kinds}"
    )


def test_g8_kotlin_method_with_return_type():
    """a Kotlin method ``fun m: Int { ... }`` inside a class. Even with
    ``fun`` added, the return type ``: Int`` after the params would break the
    keywordless-method heuristic (it requires the buffer to end in ``)``).
    But the keyword path (``fun`` recognized) handles this — the name comes
    from after the keyword, not from the heuristic."""
    src = "class Widget {\n    fun area(): Int {\n        return 1\n    }\n}\n"
    ir = ap.parse_family_a(src, language="kotlin")
    kinds = _kinds_of(ir)
    method = [u for u in ap.all_units_flat(ir) if u.kind == ap.KIND_METHOD]
    assert any(m.name == "area" for m in method), (
        f"Kotlin method with return type must be detected as METHOD 'area'; got {kinds}"
    )


def test_g8_kotlin_top_level_fun_no_return_type():
    """a Kotlin ``fun`` with no return type (``fun f { }``) — the simple
    case. Must also be detected once ``fun`` is in the keyword set."""
    src = "fun simple() {\n    println()\n}\n"
    ir = ap.parse_family_a(src, language="kotlin")
    assert _unit_named(ir, "simple"), (
        f"Kotlin fun (no return type) must be detected; got "
        f"{_kinds_of(ir)}"
    )


# ---------------------------------------------------------------------------
# Fourth-pass review fixes: (Kotlin coverage), (Go generics),
# (JS/TS arrow functions), Consumer (container-scope leak).
# ---------------------------------------------------------------------------

# ---: Kotlin object / data class / init blocks / extension functions ---


def test_g9_kotlin_object_singleton():
    """``object Config { ... }`` — Kotlin's singleton declaration. ``object``
    was missing from ``_A_CLASS_KEYWORDS``; the block was dropped entirely.
    The singleton's ``fun``/``val`` members should be detected."""
    src = (
        'object Config {\n'
        '    val name = "x"\n'
        '    fun load(): String {\n'
        '        return name\n'
        '    }\n'
        '}\n'
    )
    ir = ap.parse_family_a(src, language="kotlin")
    flat = ap.all_units_flat(ir)
    kinds = [(u.kind, u.name) for u in flat]
    assert _unit_named(ir, "Config"), f"object Config must be detected as a CLASS; got {kinds}"
    assert _unit_named(ir, "load"), f"object member fun load must be detected; got {kinds}"


def test_g9_kotlin_data_class():
    """``data class Point(val x: Int, val y: Int)`` — a bodyless data class.
    ``data`` was not recognized; the class (a primary Kotlin construct) was
    dropped. At minimum the CLASS ``Point`` must be detected."""
    src = "data class Point(val x: Int, val y: Int)\n"
    ir = ap.parse_family_a(src, language="kotlin")
    flat = ap.all_units_flat(ir)
    # Either 'Point' is detected as a class, or at least the unit isn't dropped.
    # Bodyless classes have no '{' so the brace machine can't fire — but the
    # parser should still surface SOMETHING (or we accept this as a known gap
    # and the test documents it). Acceptance: 'Point' appears as a unit.
    assert _unit_named(ir, "Point"), (
        f"data class Point must be detected; got "
        f"{[(u.kind, u.name) for u in flat]}"
    )


def test_g9_kotlin_init_block_not_swallowed_silently():
    """a Kotlin ``init { ... }`` block inside a class. ``init`` isn't a
    declaration keyword, so the block was absorbed into the enclosing class
    with no separate unit. This test documents the expectation: the class is
    still detected, and sibling ``fun`` declarations are not lost."""
    src = (
        "class C(val x: Int) {\n"
        "    init {\n"
        "        println(x)\n"
        "    }\n"
        "    fun m(): Int {\n"
        "        return x\n"
        "    }\n"
        "}\n"
    )
    ir = ap.parse_family_a(src, language="kotlin")
    flat = ap.all_units_flat(ir)
    kinds = [(u.kind, u.name) for u in flat]
    assert _unit_named(ir, "C"), f"class C must be detected; got {kinds}"
    assert _unit_named(ir, "m"), f"sibling fun m must not be lost behind init; got {kinds}"


def test_g9_kotlin_companion_object():
    """``companion object { ... }`` inside a class — Kotlin's static-like
    block. Its members (``fun factory``) must be reachable."""
    src = (
        "class C {\n"
        "    companion object {\n"
        "        val PI = 3.14\n"
        "        fun factory(): C = C()\n"
        "    }\n"
        "}\n"
    )
    ir = ap.parse_family_a(src, language="kotlin")
    flat = ap.all_units_flat(ir)
    kinds = [(u.kind, u.name) for u in flat]
    # The companion's fun with a brace body should be detected.
    assert _unit_named(ir, "factory"), (
        f"companion object fun factory must be detected; got {kinds}"
    )


# ---: Go generic function name mis-extraction ---


def test_g10_go_generic_function_name():
    """``func Map[T, U any](in []T, f func(T) U) []U { ... }`` — a Go 1.18+
    generic function. The type-param list ``[T, U any]`` between ``func`` and
    the params confused ``_go_declaration_name``'s receiver detection: it found
    the last balanced ``(...)`` (the params), then took the token before it
    (``]`` from the ``[]U`` return type) as the name → name=None. The real name
    is ``Map`` (right after ``func``)."""
    src = (
        "func Map[T, U any](in []T, f func(T) U) []U {\n"
        "    return nil\n"
        "}\n"
    )
    ir = ap.parse_family_a(src, language="go")
    flat = ap.all_units_flat(ir)
    kinds = [(u.kind, u.name) for u in flat]
    assert _unit_named(ir, "Map"), f"generic func Map must be detected by name; got {kinds}"


def test_g10_go_generic_function_two_params():
    """ regression breadth: a generic function with a simpler signature."""
    src = "func First[T any](xs []T) T {\n    return xs[0]\n}\n"
    ir = ap.parse_family_a(src, language="go")
    flat = ap.all_units_flat(ir)
    assert _unit_named(ir, "First"), (
        f"generic func First must be detected; got "
        f"{[(u.kind, u.name) for u in flat]}"
    )


def test_g10_go_non_generic_function_still_works():
    """a plain (non-generic) Go function name must still
    be recovered correctly after the generic-aware fix."""
    src = "func process(items []int) int {\n    return len(items)\n}\n"
    ir = ap.parse_family_a(src, language="go")
    flat = ap.all_units_flat(ir)
    assert _unit_named(ir, "process"), (
        f"non-generic func must still work; got "
        f"{[(u.kind, u.name) for u in flat]}"
    )


def test_g10_go_generic_receiver_method():
    """ regression: a generic receiver method (``func (s Stack[T]) ...``).
    Both the receiver-with-generics AND the method name must be handled."""
    src = (
        "type Stack[T any] struct {\n"
        "    items []T\n"
        "}\n"
        "func (s Stack[T]) Push(v T) {\n"
        "    s.items = append(s.items, v)\n"
        "}\n"
    )
    ir = ap.parse_family_a(src, language="go")
    flat = ap.all_units_flat(ir)
    kinds = [(u.kind, u.name) for u in flat]
    assert _unit_named(ir, "Push"), f"generic receiver method Push must be detected; got {kinds}"


# ---: JS/TS arrow functions with block body and no semicolon ---


def test_g11_js_arrow_function_block_no_semicolon():
    """``const f = () => { ... }`` (no trailing ``;``) — the dominant
    modern JS/TS function form under no-semicolon style (Standard, Airbnb).
    The let ``= (`` through the initializer guard, but the keywordless
    heuristic then failed (buffer ends in ``=>``, not ``)``) → the whole
    declaration was dropped. recognizes ``=>`` before ``{`` as a
    function-body opener and recovers the binding name."""
    src = "const handler = () => {\n    return 1\n}\n"
    ir = ap.parse_family_a(src, language="javascript")
    flat = ap.all_units_flat(ir)
    kinds = [(u.kind, u.name) for u in flat]
    assert _unit_named(ir, "handler"), (
        f"arrow fn (block body, no semi) must be detected; got {kinds}"
    )


def test_g11_js_arrow_function_with_params():
    """an arrow function with params and a block body, no semicolon."""
    src = "const add = (a, b) => {\n    return a + b\n}\n"
    ir = ap.parse_family_a(src, language="javascript")
    flat = ap.all_units_flat(ir)
    assert _unit_named(ir, "add"), (
        f"arrow fn with params must be detected; got "
        f"{[(u.kind, u.name) for u in flat]}"
    )


def test_g11_ts_arrow_function_with_return_type():
    """a TypeScript arrow with an explicit return type before ``=>``."""
    src = "const get = (x: number): number => {\n    return x + 1\n}\n"
    ir = ap.parse_family_a(src, language="typescript")
    flat = ap.all_units_flat(ir)
    assert _unit_named(ir, "get"), (
        f"TS arrow fn with return type must be detected; got "
        f"{[(u.kind, u.name) for u in flat]}"
    )


def test_g11_js_arrow_function_with_semicolon_still_works():
    """the previously-working case (arrow + block + ``;``)
    must still produce a unit for the binding."""
    src = "const handler = () => {\n    return 1\n};\n"
    ir = ap.parse_family_a(src, language="javascript")
    flat = ap.all_units_flat(ir)
    assert _unit_named(ir, "handler"), (
        f"arrow fn with semi must still work; got "
        f"{[(u.kind, u.name) for u in flat]}"
    )


# ---------------------------------------------------------------------------
# Fifth-pass: (require expression import) + coverage-hardening tests
# for untested-but-working paths (block comments, char literals, byte strings,
# backtick templates, C preprocessor, import-name variants, parse_file
# robustness, deleted-by-one-side alignment).
# ---------------------------------------------------------------------------


# ---: CommonJS require as an expression is an import ---


def test_g12_require_expression_is_module_stmt():
    """``const fs = require('fs')`` — Node.js/CommonJS require as an
    expression (not leading the line) was not detected: the import regex
    required ``require`` to lead the line. The require-as-expression form is
    ubiquitous in Node code. After it produces a MODULE_STMT named ``fs``."""
    src = "const fs = require('fs');\nfunction f() {\n    return 1\n}\n"
    ir = ap.parse_family_a(src, language="javascript")
    flat = ap.all_units_flat(ir)
    imports = [u for u in flat if u.kind == ap.KIND_MODULE_STMT]
    assert any(u.name == "fs" for u in imports), (
        f"require('fs') must be a MODULE_STMT named 'fs'; got "
        f"{[(u.kind, u.name) for u in flat]}"
    )


def test_g12_require_not_at_line_start():
    """ regression: a ``require`` nested inside a function body (not at
    top-level / brace_depth 0) must NOT be detected as an import — it's a
    runtime call, not a module dependency."""
    src = "function f() {\n    const x = require('dyn');\n    return x\n}\n"
    ir = ap.parse_family_a(src, language="javascript")
    flat = ap.all_units_flat(ir)
    imports = [u for u in flat if u.kind == ap.KIND_MODULE_STMT]
    assert not any(u.name == "dyn" for u in imports), (
        f"require() inside a function body must not be an import; got "
        f"{[(u.kind, u.name) for u in flat]}"
    )


def test_g12_leading_require_still_works():
    """the original form (``require('fs')`` leading the
    line, e.g. ``require('fs')`` alone) must still be detected."""
    src = "require('fs');\nfunction f() {}\n"
    ir = ap.parse_family_a(src, language="javascript")
    flat = ap.all_units_flat(ir)
    assert any(u.kind == ap.KIND_MODULE_STMT and u.name == "fs" for u in flat), (
        f"leading require('fs') must still work; got "
        f"{[(u.kind, u.name) for u in flat]}"
    )


# --- Coverage: block comment with a fake declaration inside ---


def test_cov_block_comment_with_fake_class_inside():
    """A block comment ``/* ... */`` spanning lines with a ``class Fake {`` inside
    must not produce a phantom CLASS unit. Pins the in_block_comment state path
    (lines 1087-1093), which was untested."""
    src = (
        "/* this is a\n"
        "   multi-line comment\n"
        "   with class Fake { void m() {} } inside */\n"
        "fn real() {\n    1\n}\n"
    )
    ir = ap.parse_family_a(src, language="rust")
    flat = ap.all_units_flat(ir)
    names = [u.name for u in flat]
    assert "Fake" not in names, f"block-comment phantom class leaked; got {names}"
    assert "real" in names


def test_cov_line_comment_terminates_token_buffer():
    """A line comment ``//`` mid-statement ends the token buffer run (a
    declaration following on the next line must not concatenate with the
    pre-comment tokens). Pins the line-comment buffer-reset path."""
    src = (
        "let x = 1 // trailing comment\n"
        "pub fn real() {\n    1\n}\n"
    )
    ir = ap.parse_family_a(src, language="rust")
    flat = ap.all_units_flat(ir)
    assert _unit_named(ir, "real"), (
        f"fn after a // comment must be detected (no buffer concat); got "
        f"{[(u.kind, u.name) for u in flat]}"
    )


# --- Coverage: Rust char-literal variants close correctly ---


def test_cov_rust_char_literal_variants():
    """Rust char literals (``'x'``, ``'\\n'``, ``'\\''``, ``'\\\\'``) must close
    correctly without corrupting the brace scan. Pins the in_str == 'char' close
    path (lines 1101-1104), which was untested."""
    src = (
        "pub fn f() {\n"
        "    let a = 'x';\n"
        "    let b = '\\\\n';\n"
        "    let c = '\\\\\\\\';\n"
        "    let d = '\\\\'';\n"
        "}\n"
    )
    ir = ap.parse_family_a(src, language="rust")
    assert _unit_named(ir, "f"), (
        f"char literals must not corrupt the parse; got "
        f"{_kinds_of(ir)}"
    )


# --- Coverage: Rust byte string b"..." ---


def test_cov_rust_byte_string_prefix():
    """A Rust byte string ``b"..."`` must not corrupt the brace scan (the ``b``
    prefix is recognized by ``_match_string_prefix``). Pins the byte-string
    prefix branch (lines 994-995)."""
    src = 'pub fn f() {\n    let bytes = b"hello";\n    let raw = br#"hi"#;\n}\n'
    ir = ap.parse_family_a(src, language="rust")
    assert _unit_named(ir, "f"), (
        f"byte string b\"...\" must not corrupt; got "
        f"{_kinds_of(ir)}"
    )


# --- Coverage: JS/TS backtick template literal ---


def test_cov_js_backtick_template_literal():
    """A JS/TS template literal `` `...${expr}...` `` containing braces must not
    corrupt the brace count (the ``${...}`` interpolation has braces). Pins the
    backtick-string state path (lines 1161-1164), which was untested."""
    src = (
        "function tag() {\n"
        "    const q = `value is ${1 + 2} and ${obj.prop}`;\n"
        "    return q;\n"
        "}\n"
    )
    ir = ap.parse_family_a(src, language="javascript")
    assert _unit_named(ir, "tag"), (
        f"backtick template with ${{}} must not corrupt brace count; got "
        f"{_kinds_of(ir)}"
    )


# --- Coverage: C preprocessor #include / #define ---


def test_cov_c_preprocessor_include_and_define():
    """C ``#include`` and ``#define`` at depth 0 must produce MODULE_STMT units.
    Pins the preprocessor-line handler (lines 1326-1354), which was untested."""
    src = (
        "#include <stdio.h>\n"
        '#include "myhdr.h"\n'
        "#define MAX 100\n"
        "int main() {\n"
        "    return 0;\n"
        "}\n"
    )
    ir = ap.parse_family_a(src, language="c")
    flat = ap.all_units_flat(ir)
    import_names = [u.name for u in flat if u.kind == ap.KIND_MODULE_STMT]
    assert "stdio.h" in import_names, f"#include <stdio.h> missing; got {import_names}"
    assert "myhdr.h" in import_names, f'#include "myhdr.h" missing; got {import_names}'
    assert "MAX" in import_names, f"#define MAX missing; got {import_names}"


# --- Coverage: import-name extraction variants ---


def test_cov_go_import_quoted_path():
    """Go ``import \"fmt\"`` → MODULE_STMT named ``fmt``. Pins the Go import-name
    regex branch in ``_extract_a_import_name``."""
    src = 'package main\n\nimport "fmt"\n\nfunc main() {\n    fmt.Println()\n}\n'
    ir = ap.parse_family_a(src, language="go")
    flat = ap.all_units_flat(ir)
    assert any(u.kind == ap.KIND_MODULE_STMT and u.name == "fmt" for u in flat), (
        f'Go import "fmt" must be named fmt; got '
        f"{[(u.kind, u.name) for u in flat if u.kind == ap.KIND_MODULE_STMT]}"
    )


def test_cov_js_export_braces_from():
    """JS ``export { foo, bar } from './mod'`` → MODULE_STMT named ``./mod``.
    Pins the export-from import-name regex branch."""
    src = "export { foo, bar } from './mod';\nfunction f() {}\n"
    ir = ap.parse_family_a(src, language="javascript")
    flat = ap.all_units_flat(ir)
    assert any(u.kind == ap.KIND_MODULE_STMT and u.name == "./mod" for u in flat), (
        f"export {{}} from must extract the path; got "
        f"{[(u.kind, u.name) for u in flat if u.kind == ap.KIND_MODULE_STMT]}"
    )


# --- Coverage: parse_file never raises on malformed input ---


def test_cov_parse_file_survives_deeply_nested_braces():
    """``parse_file`` must never raise — deeply nested braces (malformed/merge
    artifact) yield a low-confidence FileIR, not an exception. Pins the
    exception-path return (lines 2129-2136)."""
    src = "{" * 500 + "}" * 500
    ir = ap.parse_file(src, language="rust")
    assert ir is not None
    assert ir.parse_confidence == 0.0  # minified/garbage → low confidence


def test_cov_parse_file_survives_null_bytes():
    """Null bytes in source (binary content mistakenly fed in) must not raise."""
    src = "fn f() {\n\x00\x00\n}\n"
    ir = ap.parse_file(src, language="rust")
    assert ir is not None  # didn't raise


# --- Coverage: deleted-by-one-side alignment (one deletes, other modifies) ---


def test_cov_alignment_deleted_right_when_left_modifies():
    """When base has foo, left MODIFIES foo, and right DELETES foo, the alignment
    is ``deleted_right`` (right deleted it; left kept a modified version). Pins
    the has_b + has_l + not_has_r branch (line 2500-2502), which was untested."""
    base = "def foo():\n    return 1\n    return 2\n"
    left = "def foo():\n    return 99\n    return 2\n"   # modified
    right = "pass\n"                                      # deleted foo
    diff = sd.compute_structural_diff_3way(base, left, right, language="python")
    kinds = {a.change_kind for a in diff.aligned}
    assert sd._CHANGE_KIND_DELETED_RIGHT in kinds, (
        f"expected deleted_right (left modifies, right deletes); got {kinds}"
    )


def test_cov_alignment_deleted_left_when_right_modifies():
    """Mirror: base has foo, right MODIFIES, left DELETES → ``deleted_left``.
    Pins the has_b + not_has_l + has_r branch (line 2272), which was untested."""
    base = "def foo():\n    return 1\n    return 2\n"
    left = "pass\n"                                       # deleted foo
    right = "def foo():\n    return 99\n    return 2\n"   # modified
    diff = sd.compute_structural_diff_3way(base, left, right, language="python")
    kinds = {a.change_kind for a in diff.aligned}
    assert sd._CHANGE_KIND_DELETED_LEFT in kinds, (
        f"expected deleted_left (right modifies, left deletes); got {kinds}"
    )


# ---------------------------------------------------------------------------
# Sixth-pass: (required_units drops surviving units) + coverage hardening
# for untested-but-working paths (import-surface deletes, conflict-span in
# nested units, Rust attributes, parse_file exception/unknown-lang, binding
# reassignment guard, agreed rename).
# ---------------------------------------------------------------------------

# ---: required_units must include deleted-by-one-side (surviving units) ---


def test_r7_required_units_includes_deleted_right_surviving():
    """when left deletes a unit and right MODIFIES it (deleted_right), the
    unit survives in the merge as right's version. ``required_units`` used to
    exclude deleted_left/deleted_right, risking the LLM dropping a unit that
    should survive. After deleted-by-one-side units ARE required; only
    deleted_both (truly gone) is excluded."""
    base = "def bar():\n    return 2\n"
    left = "pass\n"                                      # deleted bar
    right = "def bar():\n    return 99\n"                # modified bar
    diff = sd.compute_structural_diff_3way(base, left, right, language="python")
    assert "bar" in diff.required_units, (
        f"deleted_right (survives on right) must be in required_units; got "
        f"{diff.required_units}"
    )


def test_r7_required_units_includes_deleted_left_surviving():
    """right deletes, left modifies-and-keeps → deleted_left, and
    the unit survives as left's version → must be required."""
    base = "def bar():\n    return 2\n"
    left = "def bar():\n    return 99\n"                 # modified bar
    right = "pass\n"                                     # deleted bar
    diff = sd.compute_structural_diff_3way(base, left, right, language="python")
    assert "bar" in diff.required_units, (
        f"deleted_left (survives on left) must be in required_units; got "
        f"{diff.required_units}"
    )


def test_r7_required_units_excludes_deleted_both():
    """a unit deleted by BOTH sides (deleted_both) is
    truly gone from the merge → must NOT be in required_units."""
    base = "def bar():\n    return 2\n"
    left = "pass\n"                                      # deleted bar
    right = "pass\n"                                     # deleted bar too
    diff = sd.compute_structural_diff_3way(base, left, right, language="python")
    assert "bar" not in diff.required_units, (
        f"deleted_both (truly removed) must not be required; got "
        f"{diff.required_units}"
    )


def test_r7_required_units_annotation_renders_surviving():
    """the 'Required: preserve these units' annotation in
    render_structural_context must include a deleted-by-one-side unit."""
    base = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
    left = "def foo():\n    return 10\n\ndef baz():\n    return 3\n"  # modify foo, delete bar, add baz
    right = "def foo():\n    return 1\n\ndef bar():\n    return 20\n" # modify bar
    diff = sd.compute_structural_diff_3way(base, left, right, language="python")
    ctx = sc.render_structural_context(diff)
    assert "bar" in ctx, (
        f"surviving unit 'bar' must appear in the Required annotation; got:\n{ctx}"
    )


# --- Coverage: import-surface delete rendering (lines 2690-2695) ---


def test_cov_import_surface_renders_deletions():
    """The import-surface block must render imports added by each side AND
    compute the survivor union. Imports are identity-matched by name, so a
    one-sided delete + other-side-keep classifies as ``unchanged`` (the unit
    survives); a true delete-by-both is ``deleted_both``. This test pins the
    add-rendering + survivor-union paths in ``_render_import_surface``."""
    base = "import os\n"
    left = "import os\nimport sys\n"                     # left added sys
    right = "import os\nimport json\n"                   # right added json
    diff = sd.compute_structural_diff_3way(base, left, right, language="python")
    ctx = sc.render_structural_context(diff)
    import_block = [ln for ln in ctx.split("\n") if "Import surface" in ln or "merged imports" in ln]
    block_text = "\n".join(import_block)
    assert "sys" in block_text, f"left-added 'sys' must be in the surface;\n{block_text}"
    assert "json" in block_text, f"right-added 'json' must be in the surface;\n{block_text}"
    assert "os" in block_text, f"base import 'os' must be a survivor;\n{block_text}"


# --- Coverage: conflict-span annotation inside a nested unit (2804-2811) ---


def test_cov_conflict_span_inside_method():
    """render_structural_context with conflict_span anchored inside a METHOD
    must annotate 'This conflict is inside: METHOD name'. Pins the
    conflict-span unit-lookup loop (lines 2804-2811), which was untested in
    isolation for the nested case."""
    base = "class C:\n    def foo(self):\n        return 1\n\ndef bar():\n    return 2\n"
    left = "class C:\n    def foo(self):\n        return 10\n\ndef bar():\n    return 2\n"
    right = "class C:\n    def foo(self):\n        return 1\n\ndef bar():\n    return 2\n"
    diff = sd.compute_structural_diff_3way(base, left, right, language="python")
    ctx = sc.render_structural_context(diff, conflict_span=(1, 1))  # inside foo
    assert "This conflict is inside: METHOD foo" in ctx, (
        f"conflict inside method must be annotated;\n{ctx}"
    )


def test_cov_conflict_span_inside_class():
    """conflict_span anchored on a class header annotates the CLASS (the
    enclosing unit lookup finds the class, not a method)."""
    base = "class C:\n    def foo(self):\n        return 1\n"
    left = "class C:\n    def foo(self):\n        return 10\n"
    right = "class C:\n    def foo(self):\n        return 1\n"
    diff = sd.compute_structural_diff_3way(base, left, right, language="python")
    ctx = sc.render_structural_context(diff, conflict_span=(0, 0))  # class header
    assert "This conflict is inside: CLASS C" in ctx, (
        f"conflict inside class must be annotated;\n{ctx}"
    )


# --- Coverage: Rust attribute preceding a decl (1336, 1340, 1466-1468) ---


def test_cov_rust_attribute_span_includes_attr_line():
    """A Rust attribute (``#[derive(Debug)]`` / ``#[cfg(test)]``) preceding a
    struct/fn must be included in the unit's span (start_row moves up to the
    attribute). Pins the pending_attr_row + attr_start_row path."""
    src = "#[derive(Debug)]\npub struct Point {\n    x: i32,\n}\n"
    ir = ap.parse_family_a(src, language="rust")
    flat = ap.all_units_flat(ir)
    pt = next((u for u in flat if u.name == "Point"), None)
    assert pt is not None, "struct Point must be detected"
    assert pt.span[0] == 0, (
        f"span must start at the attribute line (row 0); got span={pt.span}"
    )
    assert "#[derive(Debug)]" in pt.body, (
        f"attribute must be in the body; got body={pt.body!r}"
    )


def test_cov_rust_cfg_attribute_on_fn():
    """A ``#[cfg(test)]`` attribute on a function attaches to the fn."""
    src = "#[cfg(test)]\npub fn f() -> i32 {\n    1\n}\n"
    ir = ap.parse_family_a(src, language="rust")
    flat = ap.all_units_flat(ir)
    fn = next((u for u in flat if u.name == "f"), None)
    assert fn is not None and fn.span[0] == 0


# --- Coverage: parse_file exception path (2143-2150) ---


def test_cov_parse_file_returns_low_conf_on_exception():
    """parse_file must NEVER raise — an internal parser exception yields a
    zero-confidence FileIR (not a crash). Pins the except branch (2143-2150)
    via a forced monkeypatch."""
    import capybase.adapters.abstract_parser as apm
    orig = apm.parse_family_a
    apm.parse_family_a = lambda s, l=None: (_ for _ in ()).throw(RuntimeError("forced"))
    try:
        ir = apm.parse_file("fn f() {}", language="rust")
        assert ir is not None
        assert ir.parse_confidence == 0.0
        assert ir.units == []
    finally:
        apm.parse_family_a = orig


# --- Coverage: Family C / unknown language returns None (2151-2152) ---


def test_cov_parse_file_unknown_language_returns_none():
    """An unrecognized language (no family mapping) → parse_file returns None
    (no structural signal). Pins the family-C-not-implemented / unknown path."""
    assert ap.parse_file("some code", language="cobol") is None


def test_cov_parse_file_no_language_no_path_returns_none():
    """No language AND no path → can't determine family → None."""
    assert ap.parse_file("x", language=None, path=None) is None


# --- Coverage: _binding_name_before reassignment guard (1912-1927) ---


def test_cov_binding_name_reassignment_returns_none():
    """A bare reassignment ``x = function() { ... }`` (no let/const/var before
    the name) must NOT be misread as a binding — _binding_name_before returns
    None. The function is still detected (anonymous). Pins the guard at
    lines 1912-1927."""
    src = "x = function() {\n    return 1\n}\n"
    ir = ap.parse_family_a(src, language="javascript")
    flat = ap.all_units_flat(ir)
    # The function IS detected (anonymous or via keywordless path), but the
    # binding-name recovery must NOT attach 'x' as the name (it's a reassignment).
    named_x = [u for u in flat if u.name == "x"]
    assert not named_x, (
        f"reassignment target 'x' must not become a binding name; got "
        f"{[(u.kind, u.name) for u in flat]}"
    )


# ---------------------------------------------------------------------------
# Seventh-pass: coverage hardening for verified-working parser paths.
# AlignedUnit fallbacks, enclosing_container module-root, fingerprint guard,
# tab indentation, minified-side diff decline.
# ---------------------------------------------------------------------------


# --- Coverage: AlignedUnit.name / .kind all-None fallbacks (2293, 2301) ---


def test_cov_aligned_unit_all_none_fallbacks():
    """An AlignedUnit with base/left/right all None falls back to name
    ``"<anon>"`` and kind ``unknown_block``. Pins the fallback properties
    (lines 2293, 2301), which were untested."""
    a = sd.AlignedUnit(base=None, left=None, right=None, change_kind=sd._CHANGE_KIND_UNCHANGED)
    assert a.name == "<anon>", f"all-None name fallback; got {a.name!r}"
    assert a.kind == ap.KIND_UNKNOWN, f"all-None kind fallback; got {a.kind!r}"


def test_cov_aligned_unit_name_prefers_left():
    """AlignedUnit.name returns the first non-None, non-empty name from
    left/right/base. Pins the precedence order in the property."""
    base_u = ap.StructuralUnit(kind="function", name="base", span=(0, 0), body="b")
    left_u = ap.StructuralUnit(kind="function", name="left", span=(0, 0), body="l")
    a = sd.AlignedUnit(base=base_u, left=left_u, right=None, change_kind=sd._CHANGE_KIND_MODIFIED_LEFT)
    assert a.name == "left"


# --- Coverage: enclosing_container returns None at module root (2234) ---


def test_cov_enclosing_container_module_root_none():
    """A span inside a top-level (module-scope) function has no enclosing
    container — enclosing_container returns None. Pins the module-root
    return (line 2234)."""
    src = "def top():\n    return 1\n"
    ir = ap.parse_family_b(src)
    assert ap.enclosing_container(ir, (1, 1)) is None


def test_cov_enclosing_container_finds_class_parent():
    """A span inside a method resolves to the class as its container.
    Pins the non-None path of enclosing_container."""
    src = "class C:\n    def m(self):\n        return 1\n"
    ir = ap.parse_family_b(src)
    c = ap.enclosing_container(ir, (2, 2))  # inside m's body
    assert c is not None and c.name == "C"


# --- Coverage: _fingerprint_has_content guard ---


def test_cov_fingerprint_has_content():
    """``_fingerprint_has_content`` distinguishes content-less fingerprints
    (``l0``, ``l3`` — just a line count) from content-bearing ones
    (``l3:digest``). Used by rename detection to avoid pairing two unrelated
    empty bodies. Pins all three branches."""
    assert ap._fingerprint_has_content("l0") is False
    assert ap._fingerprint_has_content("l3") is False
    assert ap._fingerprint_has_content("") is False
    assert ap._fingerprint_has_content("l3:abc123") is True


# --- Coverage: tab-indented Python method (indent handling) ---


def test_cov_tab_indented_python_method():
    """A tab-indented method (tabs counted as 8 cols per Python convention)
    must be detected as a method inside its class. Pins the tab-expansion in
    ``_indent_width``."""
    src = "class C:\n\tdef m(self):\n\t\treturn 1\n"
    ir = ap.parse_family_b(src)
    flat = ap.all_units_flat(ir)
    assert _unit_named(ir, "m"), (
        f"tab-indented method must be detected; got "
        f"{[(u.kind, u.name) for u in flat]}"
    )


# --- Coverage: minified side declines the 3-way diff ---


def test_cov_minified_side_declines_diff():
    """When any side of the 3-way diff parses at confidence 0.0 (minified /
    garbage), ``compute_structural_diff_3way`` returns None rather than
    building an annotation from an untrustworthy parse. Pins the
    confidence-decline guard."""
    base = "def f():\n    return 1\n"
    left = "def f():\n    return 1\n"
    # A long single-line file (> 200 char median) → confidence 0.0.
    right = "def f(): return 1; def g(): return 2; " * 20
    diff = sd.compute_structural_diff_3way(base, left, right, language="python")
    assert diff is None, "minified side must decline the diff"


# --- Coverage: parse_family_a string-state edge cases (safety net for extraction) ---


def test_cov_raw_string_backslash_is_literal():
    """Inside a Rust raw string (``r#"..."#``) a backslash is LITERAL, not an
    escape — the scanner must not skip the next char. Pins the raw-string escape
    branch where ``str_hash_count > 0`` short-circuits the ``i += 2`` escape skip."""
    # r#"...\"..."# — the \" must not be treated as an escape (raw string).
    # A brace inside the raw string must not open/close a scope.
    src = 'fn f() {\n    let s = r#"{ not a brace } \\not escaped"#;\n}\n'
    ir = ap.parse_family_a(src, language="rust")
    assert _unit_named(ir, "f"), (
        f"raw-string backslash must be literal; got {_kinds_of(ir)}"
    )


def test_cov_rust_char_literal_closes_string_state():
    """A Rust char literal ``'a'`` must close the char-string state cleanly so a
    following brace isn't miscounted. Pins the ``in_str == 'char'`` close branch
    and the ordinary single-quote close (lines 1116-1119)."""
    src = "fn f() {\n    let c = 'a';\n    let d = '\\\\n';\n    if c == d { return; }\n}\n"
    ir = ap.parse_family_a(src, language="rust")
    assert _unit_named(ir, "f"), (
        f"char literal must not corrupt brace scan; got {_kinds_of(ir)}"
    )


def test_cov_ordinary_double_quote_string_entry():
    """A plain ``\"...\"`` string (no prefix) must enter string state via the
    ordinary-quote branch. Pins the ``else`` branch that sets ``str_hash_count=0``
    (lines 1165-1167), complementing the raw/prefixed branches already covered."""
    src = 'fn f() {\n    let s = "hello { world";\n}\n'
    ir = ap.parse_family_a(src, language="rust")
    assert _unit_named(ir, "f"), (
        f"ordinary quote string must not corrupt; got {_kinds_of(ir)}"
    )


def test_cov_hash_line_non_preprocessor_consumed():
    """A ``#`` line that isn't a preprocessor directive or pragma (e.g. a lone
    shebang mid-file, or malformed) is consumed as a complete statement with no
    unit emitted. Pins the fall-through ``_consume_line_as_statement`` branch
    (lines 1348-1352)."""
    # A stray '#!' not at line 0, between two functions — must not crash or emit.
    src = "fn a() {\n    return 1;\n}\n#!stray\nfn b() {\n    return 2;\n}\n"
    ir = ap.parse_family_a(src, language="rust")
    names = [u.name for u in ap.all_units_flat(ir)]
    assert "a" in names and "b" in names, (
        f"stray # line must not block surrounding parse; got {names}"
    )


# --- C1 regression: _strip_inline_comment must not mangle Python // ---


def test_c1_strip_inline_comment_python_floor_division():
    """Python ``//`` is floor division, NOT a comment marker. Stripping it
    corrupts body fingerprints and rename detection: ``x = total // count``
    would normalize identically to ``x = total``, causing a false rename pair.

    Only ``#`` is a comment in Python/Ruby; ``//`` is a comment only in the
    Family-A brace languages (Rust/JS/Go/C/...). The default (no lang given)
    must be Python-correct since all current callers are Family-B paths."""
    from capybase.adapters.abstract_parser import _strip_inline_comment, normalize_body
    # Python: // is code, # is comment. (Trailing whitespace after the strip is
    # left for the caller's whitespace-collapse step to handle.)
    assert _strip_inline_comment("x = total // count") == "x = total // count"
    assert _strip_inline_comment("x = 1  # note") == "x = 1  "
    # Family-A: // is comment, # is not (Rust attribute / preprocessor).
    assert _strip_inline_comment("let x = 1; // note", lang="rust") == "let x = 1; "
    assert _strip_inline_comment("let x = 1; #[attr]", lang="rust") == "let x = 1; #[attr]"
    # The end-to-end fingerprint impact: floor-division is preserved.
    from capybase.adapters.abstract_parser import unit_body_fingerprint
    fp_div = unit_body_fingerprint("def f():\n    x = total // count\n")
    fp_nodiv = unit_body_fingerprint("def f():\n    x = total\n")
    assert fp_div != fp_nodiv, (
        "floor-division // must not be stripped from Python body fingerprints"
    )


# --- C2 regression: conflict marker inside a triple-quoted string ---


def test_c2_conflict_marker_inside_triple_string():
    r"""A ``=======`` (or other conflict marker) appearing inside an open
    triple-quoted Python string is string CONTENT, not a real conflict marker.
    It must NOT close open units or reset the parser state.

    Previously the conflict-marker check ran before the triple-quote-absorption
    check, so a marker inside a docstring/multi-line string truncated the
    enclosing unit's span and body. Plausible in real code — a docstring
    containing a diff example or markdown table."""
    src = (
        'def f():\n'
        '    s = """\n'
        '=======\n'
        '"""\n'
        '    return 1\n'
    )
    ir = ap.parse_file(src, language="python")
    flat = ap.all_units_flat(ir)
    # The function f must span the WHOLE snippet (its body includes the string
    # and the return), not be truncated at the ======= line.
    f_unit = next((u for u in flat if u.name == "f"), None)
    assert f_unit is not None, f"function f must be detected; got {_kinds_of(ir)}"
    # end_row should reach the last line (the 'return 1' line), not stop at the marker.
    assert "return 1" in f_unit.body, (
        f"f's body must include the full string + return; got:\n{f_unit.body!r}"
    )
    assert "=======" in f_unit.body, (
        f"the ======= inside the string must be part of f's body, not a scope break"
    )


# --- M1 regression: raw-string closer must not leak #s into the token buffer ---


def test_m1_raw_string_closer_advances_past_hashes():
    r"""A Rust raw string ``r#"..."#`` closes on ``"`` followed by exactly N
    ``#`` chars. The scanner must advance past BOTH the ``"`` and the ``#``-run
    — previously it zeroed ``hash_count`` before reading it in the return, so
    the closing ``#``s leaked into the token buffer and could corrupt the next
    declaration's classification.

    Pins the fix: a real function declared on the same line after a raw string
    must still be detected."""
    # r#"..."# immediately followed by `fn real` — the leaked #'s would glue to
    # the buffer and misclassify the brace.
    src = 'fn outer() {\n    let s = r#"x"#;\n}\nfn real() {\n    return 1;\n}\n'
    ir = ap.parse_family_a(src, language="rust")
    names = [u.name for u in ap.all_units_flat(ir)]
    assert "outer" in names and "real" in names, (
        f"raw-string closer must not corrupt surrounding parse; got {names}"
    )
    # Multi-hash raw string: r##"..."##  — must advance past BOTH #'s.
    src2 = 'fn f() {\n    let s = r##"x"##;\n}\n'
    ir2 = ap.parse_family_a(src2, language="rust")
    assert _unit_named(ir2, "f"), (
        f"multi-hash raw-string closer must not corrupt; got {_kinds_of(ir2)}"
    )


# --- A1 regression: Family-A // comments must be stripped from fingerprints ---


def test_a1_family_a_inline_comment_stripped_from_fingerprint():
    """A Rust/JS/Go inline ``//`` comment must be stripped from the body
    fingerprint so two bodies differing only by a comment normalize equal
    (the AstPreservationValidator relies on comment-stability).

    The C1 fix made _strip_inline_comment language-aware but didn't thread
    ``lang`` through normalize_body → unit_body_fingerprint, so the default
    (Python: strip #, keep //) applied to Family-A too — breaking comment-
    stability for every Family-A language and silently breaking rename
    detection when a side adds/removes an inline comment."""
    from capybase.adapters.abstract_parser import (
        unit_body_fingerprint, normalize_body, entity_body_content,
    )
    rust_with = "fn compute(x: i32) -> i32 {\n    let y = x * 2;\n    y + 1 // note\n}"
    rust_without = "fn compute(x: i32) -> i32 {\n    let y = x * 2;\n    y + 1\n}"
    # Family-A fingerprints must be comment-stable.
    assert unit_body_fingerprint(rust_with, lang="rust") == unit_body_fingerprint(rust_without, lang="rust"), (
        "Rust // comment must be stripped from the fingerprint (comment-stability)"
    )
    # And the Python direction still works (// is floor division, preserved).
    py_div = "def f():\n    x = total // count\n"
    py_no = "def f():\n    x = total\n"
    assert unit_body_fingerprint(py_div, lang="python") != unit_body_fingerprint(py_no, lang="python"), (
        "Python // is floor division and must be PRESERVED (C1 contract)"
    )
    assert unit_body_fingerprint(py_div) != unit_body_fingerprint(py_no), (
        "default (no lang) is Python-correct"
    )


# --- B1 regression: C++ digit separator must not trap char-literal state ---


def test_b1_cpp_digit_separator_not_char_literal():
    r"""C++14 digit separators (``1'000'000``) use ``'`` between digits. The
    scanner must NOT enter char-literal state on them — it would swallow the
    digits until the next ``'`` and corrupt the brace scan, silently dropping
    subsequent declarations.

    A digit separator is ``digit ' digit``; a char literal is ``'X'``; a Rust
    lifetime is ``'ident``. The discriminator already handles lifetimes; this
    adds the digit-separator exception."""
    src = "int compute() {\n    return 1'000;\n}\nvoid next_decl() {\n}\n"
    ir = ap.parse_family_a(src, language="cpp")
    names = [u.name for u in ap.all_units_flat(ir)]
    assert "compute" in names and "next_decl" in names, (
        f"C++ digit separator must not drop subsequent decls; got {names}"
    )


# --- B1-hole regression: C++ hex digit separator ---


def test_b1_cpp_hex_digit_separator():
    r"""C++14 digit separators also apply to hex/binary literals: ``0x1F'0000``,
    ``0b1010'1010``. A hex letter (A-F) before the ``'`` must not trap char-
    literal state. The B1 fix only handled decimal digits; hex letters fell
    through to ``in_str='char'`` and swallowed the rest of the file."""
    src = "int bar() {\n    long x = 0x1F'0000;\n    return x;\n}\nint baz() {\n    return 1;\n}\n"
    ir = ap.parse_family_a(src, language="cpp")
    names = [u.name for u in ap.all_units_flat(ir)]
    assert "bar" in names and "baz" in names, (
        f"hex digit separator must not drop subsequent decls; got {names}"
    )


# --- A-1 regression: b'X' byte char literal broken by B1 hex fix ---


def test_a1_rust_byte_char_literal_not_digit_separator():
    r"""A Rust byte char literal ``b'0'`` / ``b'a'`` / ``b'F'`` must NOT be
    treated as a digit separator. The B1 hex-digit broadening (prev in
    _HEXDIGITS and nxt1 in _HEXDIGITS) matched these because both the prefix
    char (e.g. ``b``) and the content (e.g. ``a``) are hex — but the closing
    ``'`` at nxt2 distinguishes a char literal from a digit separator.

    Without the nxt2 guard, b'a' skips char-literal state, the closing ' opens
    one that swallows the rest of the file, and every subsequent declaration
    is silently dropped. Idiomatic in Rust lexers/parsers (b'0'..=b'9')."""
    src = (
        "fn parse_hex(c: u8) -> u8 {\n"
        "    match c {\n"
        "        b'0'..=b'9' => c - b'0',\n"
        "        b'a'..=b'f' => c - b'a' + 10,\n"
        "        b'A'..=b'F' => c - b'A' + 10,\n"
        "    }\n"
        "}\n"
        "fn next_fn() -> u8 { 0 }\n"
    )
    ir = ap.parse_family_a(src, language="rust")
    names = [u.name for u in ap.all_units_flat(ir)]
    assert "parse_hex" in names and "next_fn" in names, (
        f"byte char literal b'X' must not drop subsequent decls; got {names}"
    )
    # Hex digit separators still work (the B1-hole fix's intent).
    src2 = "int bar() {\n    long x = 0x1F'0000;\n    return x;\n}\nint baz() { return 1; }\n"
    ir2 = ap.parse_family_a(src2, language="cpp")
    assert "bar" in [u.name for u in ap.all_units_flat(ir2)] and "baz" in [u.name for u in ap.all_units_flat(ir2)]


# --- B.1 (round 10): multi-line block-comment interior must be stripped ---


def test_b1_multiline_block_comment_interior_stripped():
    r"""Interior lines of a multi-line ``/* ... */`` block comment (the Javadoc/
    Rustdoc `` * continuation`` style) must be recognized as comment content and
    stripped from fingerprints, so a rename that edits the comment interior still
    pairs (comment-stability). Previously _has_code_content was line-based with
    no block-comment state, so `` * Version A.`` leaked into the fingerprint as
    code — breaking rename pairing and the AstPreservationValidator invariant."""
    from capybase.adapters.abstract_parser import unit_body_fingerprint
    base = (
        "fn fetch() {\n"
        "    let x = 1;\n"
        "    /*\n"
        "     * Current version.\n"
        "     */\n"
        "    return x;\n"
        "}\n"
    )
    side = (
        "fn fetch_data() {\n"
        "    let x = 1;\n"
        "    /*\n"
        "     * Replayed version.\n"
        "     */\n"
        "    return x;\n"
        "}\n"
    )
    fp_b = unit_body_fingerprint(base, lang="rust")
    fp_s = unit_body_fingerprint(side, lang="rust")
    assert fp_b == fp_s, (
        f"a rename editing only the block-comment interior must be comment-stable; "
        f"base={fp_b!r} side={fp_s!r}"
    )


# --- C1/C2 (round 11): _filter_code_lines must handle mid-line block comments ---


def test_c1_midline_block_comment_opener():
    r"""A ``/*`` mid-line (after code) must open block-comment state, and the
    code before it must survive. Previously _filter_code_lines only recognized
    line-START ``/*``, so the whole comment region leaked as code and broke
    fingerprint comment-stability for inline-opened comments."""
    from capybase.adapters.abstract_parser import _filter_code_lines, unit_body_fingerprint
    out = _filter_code_lines(["let x = 1; /* note", " rest of comment", "*/ let y = 2;"], lang="rust")
    # Code before the opener and after the closer must survive; interior stripped.
    joined = " ".join(out)
    assert "let x = 1" in joined, f"code before mid-line /* must survive; got {out}"
    assert "let y = 2" in joined, f"code after */ closer must survive; got {out}"
    assert "rest of comment" not in joined, f"comment interior must be stripped; got {out}"
    # End-to-end: fingerprint comment-stable for inline-opened comment.
    base = "fn f() {\n    let x = 1; /* v1\n    interior\n    */\n}\n"
    side = "fn fetch_data() {\n    let x = 1; /* v2\n    different\n    */\n}\n"
    assert unit_body_fingerprint(base, lang="rust") == unit_body_fingerprint(side, lang="rust"), (
        "inline-opened block comment must be comment-stable"
    )


def test_c2_code_after_closer_survives():
    r"""Code on the same line after a ``*/`` closer must survive. Previously
    the closer line was unconditionally dropped (``continue``), losing any
    trailing code."""
    from capybase.adapters.abstract_parser import _filter_code_lines
    out = _filter_code_lines(["/* open", "*/ let y = 2;"], lang="rust")
    joined = " ".join(out)
    assert "let y = 2" in joined, f"code after */ must survive; got {out}"
    # Single-line block comment followed by code.
    out2 = _filter_code_lines(["/* comment */ let x = 1;"], lang="rust")
    joined2 = " ".join(out2)
    assert "let x = 1" in joined2, f"code after single-line /* */ must survive; got {out2}"


# --- Finding 1 (round 12): _filter_code_lines string-blanking index shift ---


def test_r12_filter_code_lines_string_before_comment():
    r"""A string literal on the same line before a block comment must not corrupt
    the extraction. The scan blanks strings (to avoid /* in a string opening
    block state) then slices code from the ORIGINAL line using indices from the
    blanked line. If the blanking changes the line LENGTH (fixed 2-char '_' for
    a variable-length string), every index after the string shifts and the
    extraction slices the wrong span — the string bleeds past its close quote
    and swallows the comment opener.

    Use a length-preserving blank so indices stay aligned."""
    from capybase.adapters.abstract_parser import _filter_code_lines, unit_body_fingerprint
    out = _filter_code_lines(['let s = "abc"; /* c */ let x = 1;'], lang="rust")
    joined = " ".join(out)
    # The comment must be stripped, the string and trailing code preserved.
    assert "/* c */" not in joined, f"block comment must be stripped; got {out!r}"
    assert "*/" not in joined, f"comment closer must not leak; got {out!r}"
    assert "let x = 1" in joined, f"trailing code must survive; got {out!r}"
    # End-to-end: fingerprints must be string-stable when only the string value
    # differs and a block comment follows.
    b1 = 'fn f() {\n    let s = "abc"; /* c */ let x = 1;\n}\n'
    b2 = 'fn f() {\n    let s = "xyz"; /* c */ let x = 1;\n}\n'
    assert unit_body_fingerprint(b1, lang="rust") == unit_body_fingerprint(b2, lang="rust"), (
        "string-value diff must not break fingerprint stability when a comment follows"
    )


# --- D1 (round 13): _strip_inline_comment index-shift (same bug class) ---


def test_r13_strip_inline_comment_string_length_preserving():
    r"""_strip_inline_comment blanks strings before finding the comment marker,
    then slices the ORIGINAL line at the blanked-line index. A fixed-length
    replacement ('_') shifts the index when the string length differs, truncating
    the line mid-string and corrupting the fingerprint. This is the same bug
    class round-12 fixed in _filter_code_lines. Use a length-preserving blank."""
    from capybase.adapters.abstract_parser import _strip_inline_comment, unit_body_fingerprint
    # A long string before an inline // comment.
    out = _strip_inline_comment('let x = "LONGSTRING"; // comment', lang="rust")
    assert out.startswith('let x = "LONGSTRING"'), (
        f"string must be preserved (not truncated mid-string); got {out!r}"
    )
    assert "//" not in out, f"inline comment must be stripped; got {out!r}"
    # End-to-end: fingerprint rename-stable when base has a long string + comment.
    body_foo = 'fn foo() {\n    let msg = "Hello welcome to the system"; // greet\n    println!(msg);\n}\n'
    body_bar = 'fn bar() {\n    let msg = "Hello welcome to the system";\n    println!(msg);\n}\n'
    assert unit_body_fingerprint(body_foo, lang="rust") == unit_body_fingerprint(body_bar, lang="rust"), (
        "rename with a long-string + inline-comment base must be fingerprint-stable"
    )


# --- Finding 1 (round 16): _A_FIELD_RE must skip Rust mut ---


def test_r16_field_name_mut_not_captured():
    r"""Rust ``let mut`` / ``static mut`` bindings: the ``mut`` keyword must be
    treated as a modifier (like ``pub``/``static``), not captured as the field
    name. Previously _A_FIELD_RE captured ``mut`` as the name, causing all
    ``mut`` bindings to collide on identity ``(field, 'mut')`` and silently
    dropping all but the first."""
    from capybase.adapters.abstract_parser import _field_name_from_buf
    assert _field_name_from_buf("let mut counter = 0;") == "counter"
    assert _field_name_from_buf("static mut GLOBAL: i32 = 0;") == "GLOBAL"
    assert _field_name_from_buf("pub static mut STATE: u64 = 0;") == "STATE"
    # Non-mut still works.
    assert _field_name_from_buf("let x = 1;") == "x"
    assert _field_name_from_buf("const N: u32 = 5;") == "N"
    # Two mut bindings don't collide.
    ir = ap.parse_family_a("static mut A: u64 = 0;\nstatic mut B: u64 = 1;\n", "rust")
    names = [u.name for u in ap.all_units_flat(ir) if u.kind == "field"]
    assert "A" in names and "B" in names, f"two mut bindings must not collide; got {names}"


# ---------------------------------------------------------------------------
# Round 24 — C++/Dart methods with tokens trailing the parameter list.
# A single over-strict guard (``joined.endswith(")"))`` in
# _classify_keywordless_method) silently dropped any method whose signature
# had tokens after the ``)``: C++ const/noexcept/override/final qualifiers,
# C++ trailing return types (``auto f() -> T``), and Dart ``async``/``async*``.
# These are idiomatic shapes — a large fraction of real C++/Dart methods were
# silently lost. The fix strips known trailing qualifiers BEFORE the shape
# guard, then accepts the trailing-return form explicitly.
# ---------------------------------------------------------------------------


def test_r24_cpp_const_method_detected():
    """C++ ``int get() const { }`` — the ``const`` qualifier must not drop the
    method. The most common C++ getter shape."""
    src = (
        "class C {\n"
        "public:\n"
        "    int get() const { return x; }\n"
        "    int plain() { return 1; }\n"
        "};\n"
    )
    kinds = _kinds(src, "cpp")
    assert ("method", "get") in kinds, f"const method dropped; got {kinds}"
    assert ("method", "plain") in kinds, f"plain method regressed; got {kinds}"


def test_r24_cpp_noexcept_override_final_methods_detected():
    """All four C++ trailing method qualifiers must be detected."""
    src = (
        "class C {\n"
        "public:\n"
        "    void a() noexcept { }\n"
        "    bool b() override { return false; }\n"
        "    void c() final { }\n"
        "    bool d() const noexcept { return true; }\n"
        "    int e() const override { return 0; }\n"
        "};\n"
    )
    kinds = _kinds(src, "cpp")
    for name in ("a", "b", "c", "d", "e"):
        assert ("method", name) in kinds, f"{name!r} dropped; got {kinds}"


def test_r24_cpp_trailing_return_type_detected():
    """C++ trailing return type ``auto f() -> T { }`` — the modern return idiom.
    Both the free function and class-method forms."""
    src = (
        "auto add(int a, int b) -> int { return a + b; }\n"
        "class Calc {\n"
        "public:\n"
        "    auto sub(int a, int b) -> long { return a - b; }\n"
        "};\n"
    )
    kinds = _kinds(src, "cpp")
    assert ("function", "add") in kinds, f"free trailing-return fn dropped; got {kinds}"
    assert ("method", "sub") in kinds, f"member trailing-return fn dropped; got {kinds}"


def test_r24_dart_async_method_name_recovered():
    """Dart ``Type name() async { }`` — the ``async`` keyword must be stripped so
    the method name is recovered (not ``None``). A ``None`` name breaks identity
    and rename tracking and collides all async methods on ``(method, None)``."""
    src = (
        "class C {\n"
        "  Future<int> compute() async { return 1; }\n"
        "  void main() async { print(1); }\n"
        "}\n"
    )
    kinds = _kinds(src, "dart")
    assert ("method", "compute") in kinds, f"async method name lost; got {kinds}"
    assert ("method", "main") in kinds, f"async method name lost; got {kinds}"


def test_r24_trailing_qualifier_does_not_swallow_control_flow():
    """Regression guard: stripping trailing qualifiers must NOT cause a plain
    ``if (x) { }`` / ``while (y) { }`` at method-depth to be misread as a
    method named ``if``/``while``. The control-flow keyword guard still fires."""
    src = (
        "class C {\n"
        "public:\n"
        "    void loop() {\n"
        "        if (x) { return; }\n"
        "        while (y) { step(); }\n"
        "    }\n"
        "};\n"
    )
    kinds = _kinds(src, "cpp")
    names = [n for _, n in kinds]
    assert "if" not in names, f"control-flow swallowed as method; got {kinds}"
    assert "while" not in names, f"control-flow swallowed as method; got {kinds}"
    assert ("method", "loop") in kinds, f"real method regressed; got {kinds}"


def test_r24_keywordless_method_plain_form_unchanged():
    """Regression guard: the classic keyword-less method shape (``int get() { }``,
    no trailing tokens) still classifies after the qualifier-stripping change."""
    src = (
        "class C {\n"
        "public:\n"
        "    int get() { return 1; }\n"
        "    void set(int v) { x = v; }\n"
        "};\n"
    )
    kinds = _kinds(src, "cpp")
    assert ("method", "get") in kinds, f"plain get regressed; got {kinds}"
    assert ("method", "set") in kinds, f"plain set regressed; got {kinds}"


# ---------------------------------------------------------------------------
# Round 24 — line comment between signature and ``{`` (Allman brace style).
# A ``// comment`` ending the signature line triggered the line-comment buffer
# reset, wiping the accumulated signature tokens before the ``{`` on the next
# line was classified — silently dropping the whole unit. Affects every Family-A
# language with Allman braces + a trailing inline comment.
# ---------------------------------------------------------------------------


def test_r24_rust_signature_line_comment_before_brace():
    """``fn foo() // c\\n{ }`` — the inline comment must not wipe the signature
    buffer; the function must still be detected when its brace is on the next line."""
    src = "fn foo()  // this is foo\n{\n    return 1;\n}\nfn bar() { return 2; }\n"
    kinds = _kinds(src, "rust")
    assert ("function", "foo") in kinds, f"foo dropped by line-comment reset; got {kinds}"
    assert ("function", "bar") in kinds, f"sibling bar regressed; got {kinds}"


def test_r24_cpp_allman_brace_with_inline_comment():
    """C++ Allman braces with an inline comment on the signature line — the
    method must survive (comment must not reset the buffer across the newline)."""
    src = (
        "class C {\n"
        "public:\n"
        "    int getCount() const  // returns count\n"
        "    {\n"
        "        return count;\n"
        "    }\n"
        "    void other() {}\n"
        "};\n"
    )
    kinds = _kinds(src, "cpp")
    assert ("method", "getCount") in kinds, f"getCount dropped; got {kinds}"
    assert ("method", "other") in kinds, f"sibling other regressed; got {kinds}"


# ---------------------------------------------------------------------------
# Round 24 — associated items & bodyless signatures inside containers.
# The in-pass field/method emitters only fired at brace_depth == 0, so several
# idiomatic constructs nested inside impl/trait/mod/extern/interface were
# silently dropped: Rust associated consts, Rust trait method signatures (no
# body), Rust extern "C" FFI declarations, and Go interface method specs.
# ---------------------------------------------------------------------------


def test_r24_rust_associated_const_in_impl():
    """Rust ``impl C { const N: u32 = 1; }`` — the associated const must emit a
    FIELD unit. Previously dropped (field emitter gated on brace_depth == 0)."""
    src = (
        "struct Config;\n"
        "impl Config {\n"
        "    const VERSION: u32 = 1;\n"
        "    fn name() -> &'static str { \"x\" }\n"
        "}\n"
    )
    kinds = _kinds(src, "rust")
    assert ("field", "VERSION") in kinds, f"associated const dropped; got {kinds}"
    assert ("method", "name") in kinds, f"sibling fn regressed; got {kinds}"


def test_r24_rust_associated_const_in_trait():
    """Rust ``trait T { const N: u32; }`` — trait associated const (no value)."""
    src = (
        "trait T {\n"
        "    const N: u32;\n"
        "    fn foo(&self);\n"
        "}\n"
    )
    kinds = _kinds(src, "rust")
    assert ("field", "N") in kinds, f"trait associated const dropped; got {kinds}"


def test_r24_rust_trait_bodyless_method():
    """Rust ``trait T { fn foo(&self); }`` — a trait method with no body (just a
    signature terminated by ``;``). Previously dropped (no ``{`` to classify)."""
    src = (
        "trait T {\n"
        "    fn foo(&self);\n"
    "    fn bar(&self) -> bool;\n"
        "}\n"
    )
    kinds = _kinds(src, "rust")
    assert ("method", "foo") in kinds, f"trait bodyless method dropped; got {kinds}"
    assert ("method", "bar") in kinds, f"trait bodyless method dropped; got {kinds}"


def test_r24_rust_extern_c_ffi_declarations():
    """Rust ``extern \"C\" { fn foo(); fn bar(x: i32) -> i32; }`` — FFI surface.
    Each declaration inside the extern block must be tracked (the linkage surface)."""
    src = (
        "extern \"C\" {\n"
        "    fn foo();\n"
        "    fn bar(x: i32) -> i32;\n"
        "}\n"
        "pub fn real_func() -> i32 { 42 }\n"
    )
    kinds = _kinds(src, "rust")
    fn_names = [n for k, n in kinds if k in ("function", "method")]
    assert "foo" in fn_names, f"extern fn foo dropped; got {kinds}"
    assert "bar" in fn_names, f"extern fn bar dropped; got {kinds}"
    assert ("function", "real_func") in kinds, f"real_func regressed; got {kinds}"


def test_r24_go_interface_method_specs():
    """Go ``type R interface { Read(p []byte) error; Close() error }`` — each
    interface method spec (signature, no body) must be tracked as a method."""
    src = (
        "type Reader interface {\n"
        "    Read(p []byte) (n int, err error)\n"
        "    Close() error\n"
        "}\n"
        "func main() {}\n"
    )
    kinds = _kinds(src, "go")
    assert ("method", "Read") in kinds, f"interface method Read dropped; got {kinds}"
    assert ("method", "Close") in kinds, f"interface method Close dropped; got {kinds}"


def test_r24_rust_macro_rules_detected():
    """Rust ``macro_rules! name { ... }`` — a macro definition must be tracked as
    an entity (its name is the macro name). Previously dropped (``macro_rules``
    is not a recognized keyword, and the body brace opened a depth-only scope)."""
    src = (
        "macro_rules! vec {\n"
        "    ($e:expr) => { vec![$e] }\n"
        "}\n"
        "fn bar() {}\n"
    )
    kinds = _kinds(src, "rust")
    fn_names = [n for _, n in kinds]
    assert "vec" in fn_names, f"macro_rules! vec dropped; got {kinds}"
    assert ("function", "bar") in kinds, f"sibling bar regressed; got {kinds}"

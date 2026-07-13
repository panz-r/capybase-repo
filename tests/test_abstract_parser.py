"""Spec for the grammar-free abstract structural parser (Round 1).

Replaces tree-sitter grammars with two state machines: Family B (indentation-
delimited, Python) and Family A (brace-delimited, Rust et al.). These tests pin
the behavior the rest of capybase relies on: coarse kinds, faithful spans/bodies,
nested children, conflict-marker robustness, and graceful degradation. Pure — no
tree-sitter, no model, no I/O.
"""

from __future__ import annotations

from capybase.adapters import abstract_parser as ap


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
    flat = ap._all_units_flat(ir)
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
    flat = ap._all_units_flat(ir)
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
    flat = ap._all_units_flat(ir)
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
    flat = ap._all_units_flat(ir)
    methods = [u for u in flat if u.kind == "method"]
    method_names = {u.name for u in methods}
    assert "new" in method_names
    assert "label" in method_names


def test_family_a_free_function_is_function_not_method():
    """A module-level ``fn`` (not inside any impl) is FUNCTION-kind."""
    ir = ap.parse_family_a(RUST_SAMPLE, "rust")
    flat = ap._all_units_flat(ir)
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
    flat = ap._all_units_flat(ir)
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
    flat = ap._all_units_flat(ir)
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
    flat = ap._all_units_flat(ir)
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
    contract parse_confidence was always meant to send but never did (Round 4
    wired the gate)."""
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
    assert ap.compute_structural_diff_3way(normal, normal, minified, language="javascript") is None


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
    flat = ap._all_units_flat(ir)
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
    is now extracted as a METHOD child of the class (Round 4 keyword-less
    method heuristic)."""
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
    flat = ap._all_units_flat(ir)
    methods = [u.name for u in flat if u.kind == "method"]
    assert "main" in methods


def test_java_keywordless_methods_extracted():
    """Java methods (no declaration keyword — return type leads) are extracted as
    METHOD children of their class. Multiple sibling methods, constructors, and
    methods with complex signatures all recover. Before Round 4, every Java
    method was absorbed into the class body (invisible to the merge engine)."""
    src = (
        "class Service {\n"
        "    Service() { init(); }\n"
        "    public int compute(int x, int y) { return x + y; }\n"
        "    private void log(String msg) { System.err.println(msg); }\n"
        "}\n"
    )
    ir = ap.parse_file(src, language="java")
    flat = ap._all_units_flat(ir)
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
    flat = ap._all_units_flat(ir)
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
    flat = ap._all_units_flat(ir)
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
    flat = ap._all_units_flat(ir)
    methods = [u.name for u in flat if u.kind == "method"]
    # Only ``m`` is a method — the if/while/for/switch blocks are NOT entities.
    assert methods == ["m"], f"control flow leaked as methods: {methods}"


def test_c_free_functions_extracted_at_file_scope():
    """C free functions (keyword-less, at file scope) are extracted as FUNCTION
    units. The depth guard allows ``brace_depth == 0`` with no open container."""
    src = "int main() { return 0; }\nint helper(int x) { return x + 1; }\n"
    ir = ap.parse_file(src, language="c")
    flat = ap._all_units_flat(ir)
    funcs = sorted(u.name for u in flat if u.kind == "function")
    assert "main" in funcs
    assert "helper" in funcs


def test_js_class_method_shorthand_extracted():
    """JS/TS class method shorthand (``class C { m() {} }`` — no ``function``
    keyword) is now extracted as a METHOD. Previously absorbed into the class."""
    src = "class C {\n    m() { return 1; }\n    n() { return 2; }\n}\n"
    ir = ap.parse_file(src, language="javascript")
    flat = ap._all_units_flat(ir)
    methods = sorted(u.name for u in flat if u.kind == "method")
    assert "m" in methods and "n" in methods


def test_rust_keyword_extraction_unchanged_by_keywordless_heuristic():
    """Regression guard: Rust (which uses ``fn``) is unaffected by the keyword-
    less method heuristic — keyword-prefixed classification still takes priority."""
    src = "impl C {\n    fn foo() {}\n    fn bar() {}\n}\n"
    ir = ap.parse_file(src, language="rust")
    flat = ap._all_units_flat(ir)
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
    diff = ap.compute_structural_diff_3way(src, src, src, language="python")
    assert diff is not None
    assert len(diff.aligned) == 2
    assert all(a.change_kind == ap._CHANGE_KIND_UNCHANGED for a in diff.aligned)
    assert len(diff.structural_conflicts) == 0


def test_structural_diff_modified_both():
    """Both sides modified the same unit → modified_both → structural conflict."""
    base = "def foo():\n    return 1\n"
    left = "def foo():\n    return 2\n"  # left changed body
    right = "def foo():\n    return 3\n"  # right changed body differently
    diff = ap.compute_structural_diff_3way(base, left, right, language="python")
    assert diff is not None
    foo = next(a for a in diff.aligned if a.name == "foo")
    assert foo.change_kind == ap._CHANGE_KIND_MODIFIED_BOTH
    assert len(diff.structural_conflicts) == 1


def test_structural_distinct_additions_no_conflict():
    """Each side adds a DIFFERENT unit → no structural conflict."""
    base = "def existing():\n    pass\n"
    left = "def existing():\n    pass\n\ndef left_new():\n    return 1\n"
    right = "def existing():\n    pass\n\ndef right_new():\n    return 2\n"
    diff = ap.compute_structural_diff_3way(base, left, right, language="python")
    assert diff is not None
    assert len(diff.structural_conflicts) == 0
    names = {a.name for a in diff.aligned}
    assert "left_new" in names and "right_new" in names


def test_structural_diff_required_units():
    """required_units lists all units that must appear in the merge."""
    base = "def foo():\n    return 1\n"
    left = "def foo():\n    return 2\n\ndef added():\n    pass\n"
    right = "def foo():\n    return 3\n"
    diff = ap.compute_structural_diff_3way(base, left, right, language="python")
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
    diff = ap.compute_structural_diff_3way(base, left, right, language="rust")
    assert diff is not None
    kinds = {a.name: a.change_kind for a in diff.aligned}
    assert kinds.get("new") == ap._CHANGE_KIND_MODIFIED_LEFT
    assert kinds.get("label") == ap._CHANGE_KIND_MODIFIED_RIGHT
    assert len(diff.structural_conflicts) == 0  # different units, no conflict


# ---------------------------------------------------------------------------
# Structural context annotation (Improvement #6)
# ---------------------------------------------------------------------------


def test_render_context_shows_changes():
    """The annotation lists changed units and the required output."""
    base = "def foo():\n    return 1\n"
    left = "def foo():\n    return 2\n"
    right = "def foo():\n    return 3\n"
    diff = ap.compute_structural_diff_3way(base, left, right, language="python")
    text = ap.render_structural_context(diff)
    assert "STRUCTURAL CONTEXT" in text
    assert "MODIFIED BY BOTH" in text
    assert "foo" in text
    assert "synthesize" in text


def test_render_context_no_changes_returns_empty():
    """No changes → empty annotation (nothing to tell the model)."""
    src = "def foo():\n    return 1\n"
    diff = ap.compute_structural_diff_3way(src, src, src, language="python")
    assert ap.render_structural_context(diff) == ""


def test_render_context_distinct_additions_says_no_conflict():
    """Distinct additions → annotation says 'no structural conflicts'."""
    base = "def existing():\n    pass\n"
    left = "def existing():\n    pass\n\ndef left_new():\n    pass\n"
    right = "def existing():\n    pass\n\ndef right_new():\n    pass\n"
    diff = ap.compute_structural_diff_3way(base, left, right, language="python")
    text = ap.render_structural_context(diff)
    assert "NONE" in text
    assert "left_new" in text and "right_new" in text


# ---------------------------------------------------------------------------
# Import-surface annotation (survey: import handling is the highest-value
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
    diff = ap.compute_structural_diff_3way(base, left, right, language="python")
    text = ap.render_structural_context(diff)
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
    diff = ap.compute_structural_diff_3way(base, left, right, language="rust")
    text = ap.render_structural_context(diff)
    assert "Import surface" in text
    assert "std::io" in text and "std::path" in text
    assert "union" in text


def test_render_context_no_import_block_when_only_code_changed():
    """A function-only change (no import change) produces NO import-surface
    block — the annotation is unchanged from the pre-import-block behavior."""
    base = "def foo():\n    return 1\n"
    left = "def foo():\n    return 2\n"
    right = "def foo():\n    return 3\n"
    diff = ap.compute_structural_diff_3way(base, left, right, language="python")
    text = ap.render_structural_context(diff)
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
    flat = ap._all_units_flat(ir)
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
    flat = ap._all_units_flat(ir)
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
    flat = ap._all_units_flat(ir)
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
    flat = ap._all_units_flat(ir)
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
    flat = ap._all_units_flat(ir)
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
    flat = ap._all_units_flat(ir)
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
    flat = ap._all_units_flat(ir)
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
    flat = ap._all_units_flat(ir)
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
    flat = ap._all_units_flat(ir)
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
    flat = ap._all_units_flat(ir)
    methods = [u.name for u in flat if u.kind == "method"]
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
    flat = ap._all_units_flat(ir)
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
    flat = ap._all_units_flat(ir)
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
    flat = ap._all_units_flat(ir)
    fields = [u.name for u in flat if u.kind == "field"]
    assert "Foo" in fields, f"Rust type alias not a field: {fields}"


# ---------------------------------------------------------------------------
# Second-pass review fixes — R1 (field-drop regression) + G1–G6 (edge cases in
# the bracket/string/continuation machinery introduced by the first round).
# ---------------------------------------------------------------------------


def test_field_with_struct_literal_initializer():
    """R1: a top-level ``const``/``static`` whose initializer contains a braced
    struct literal must still be detected as a FIELD. The in-pass emitter (fix
    #10) reset the token buffer at every ``{``/``}``, so by the ``;`` the
    declaration keyword was gone and the field was silently dropped. This is a
    very common Rust pattern."""
    src = "pub const P: Point = Point { x: 1, y: 2 };\nfn main() {}\n"
    ir = ap.parse_file(src, language="rust")
    flat = ap._all_units_flat(ir)
    fields = [u.name for u in flat if u.kind == "field"]
    assert "P" in fields, f"struct-literal const dropped: {fields}"


def test_field_with_macro_brace_initializer():
    """R1 (macro form): ``vec!{...}`` / ``format!{...}`` braced macros in a
    top-level const initializer must not lose the field."""
    src = "const V: Vec<u8> = vec!{1, 2, 3};\nfn main() {}\n"
    ir = ap.parse_file(src, language="rust")
    flat = ap._all_units_flat(ir)
    fields = [u.name for u in flat if u.kind == "field"]
    assert "V" in fields, f"macro-brace const dropped: {fields}"


def test_field_with_brace_no_regression_on_plain_const():
    """R1 regression guard: a plain const (no braces) is still detected."""
    src = "pub const N: u32 = 5;\nfn main() {}\n"
    ir = ap.parse_file(src, language="rust")
    flat = ap._all_units_flat(ir)
    assert "N" in [u.name for u in flat if u.kind == "field"]


def test_inline_comment_unbalanced_bracket_no_continuation():
    """G1: an inline comment containing an unbalanced bracket must NOT corrupt
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
    """G2: a malformed dangling ``{`` (a merge artifact) must not absorb the
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
    """G2 regression guard: the multi-line signature fix (#1) must still work —
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
    """G3: an identifier ending in ``r``/``b`` immediately before a string
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
    flat = ap._all_units_flat(ir)
    names = {u.name for u in flat}
    assert "g" in names, f"identifier-before-quote corrupted string state: {names}"


def test_real_raw_string_prefix_still_works():
    """G3 regression guard: a genuine ``r#\"...\"#`` (with a word boundary
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
    flat = ap._all_units_flat(ir)
    names = {u.name for u in flat}
    assert "g" in names, f"genuine raw string no longer recognized: {names}"


def test_backslash_line_continuation_def():
    """G4: an explicit backslash line-continuation in a signature is handled —
    ``def \\<newline>foo():`` still detects ``foo``. Rare but PEP 8-legal."""
    src = "def \\\nfoo():\n    pass\n"
    ir = ap.parse_family_b(src)
    names = {u.name for u in ir.units}
    assert "foo" in names, f"backslash-continued def missed: {names}"


def test_unterminated_string_does_not_corrupt_continuation():
    """G5: an unterminated single-line ``\"`` on one line must not leave the
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
    """G6: C# verbatim strings ``@\"...\"`` escape a literal quote by doubling
    it (``\"\"``). The scanner must not close the string at the first ``\"\"``."""
    src = (
        "class C {\n"
        '    string s = @"he said ""hi"" end";\n'
        "    void M() {}\n"
        "}\n"
    )
    ir = ap.parse_file(src, language="csharp")
    flat = ap._all_units_flat(ir)
    methods = [u.name for u in flat if u.kind == "method"]
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
    diff = ap.compute_structural_diff_3way(base, left, right, language="python")
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
    assert ap._has_duplicate_identities(ents), (
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
    flat = ap._all_units_flat(ir)
    assert not ap._has_duplicate_identities(flat)


def test_added_both_different_bodies_is_conflict():
    """Fix #7: when both sides ADD a unit of the same name with DIFFERENT
    bodies, that's a genuine conflict (each side's addition is incompatible).
    Previously classified as ``added_both`` and NOT counted as a structural
    conflict — a silent miss. Now sub-classified as a conflict so the model is
    told to synthesize."""
    base = "pass\n"
    left = "def f():\n    return 1\n"
    right = "def f():\n    return 2\n"  # same name, different body
    diff = ap.compute_structural_diff_3way(base, left, right, language="python")
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
    diff = ap.compute_structural_diff_3way(base, added, added, language="python")
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
    diff = ap.compute_structural_diff_3way(base, left, right, language="python")
    assert diff is not None
    renamed = [a for a in diff.aligned if a.change_kind == ap._CHANGE_KIND_RENAMED]
    # The content-less body must not produce a false rename pairing.
    assert renamed == [], (
        f"content-less bodies falsely paired as rename: {[(a.name) for a in renamed]}"
    )

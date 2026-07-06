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

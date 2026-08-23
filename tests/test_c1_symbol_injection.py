"""C1 (sprint-22): deterministic missing-symbol repair — pure helpers.

The compiler names the missing symbol; the merge sides carry its
declaration outside the conflict unit. These tests pin the parse table
(both language families), the declaration finder's conservative
inclusion rules, and the language-correct injection placement + dedup.
"""

from capybase.verification import (
    find_symbol_declaration_lines,
    inject_symbol_declaration,
    parse_missing_symbols,
    symbol_injection_point,
)

# ---------------------------------------------------------------------------
# parse_missing_symbols — the unified cross-language signature table.
# ---------------------------------------------------------------------------

def test_parse_c_shapes():
    msg = (
        "src/t.c:10:5: error: 'cliSwitchProto' undeclared (first use in this function)\n"
        "src/t.c:20:1: error: unknown type name 'BtCursor'\n"
        "src/t.c:30:9: warning: implicit declaration of function 'foo' [-Wimplicit]"
    )
    assert parse_missing_symbols(msg, "c") == [
        "cliSwitchProto", "BtCursor", "foo"]


def test_parse_gxx_shapes():
    msg = "t.cc:5:7: error: 'item' does not name a type; 'widget' was not declared"
    assert parse_missing_symbols(msg, "cpp") == ["item", "widget"]


def test_parse_rust_shapes():
    msg = (
        "error[E0425]: cannot find value `anchor` in this scope\n"
        "error[E0433]: could not compile: prefix `item` is unknown\n"
        "error[E0432]: unresolved import `crate::entity::prelude`"
    )
    assert parse_missing_symbols(msg, "rust") == ["anchor", "item", "prelude"]


def test_parse_language_gating():
    # rust patterns must not fire for C diagnostics and vice versa
    assert parse_missing_symbols("'x' undeclared", "rust") == []
    assert parse_missing_symbols("cannot find value `x`", "c") == []


def test_parse_dedup_and_limit():
    msg = "'a' undeclared ... 'a' undeclared ... 'b' was not declared"
    assert parse_missing_symbols(msg, "c") == ["a", "b"]


# ---------------------------------------------------------------------------
# find_symbol_declaration_lines — only complete, injectable lines.
# ---------------------------------------------------------------------------

def test_find_rust_use_and_mod():
    sides = (
        "use std::fmt;\n"
        "use crate::entity::prelude::*;\n"
        "mod tests;\n"
        "fn main() { let x = 1; }\n"
    )
    assert "use crate::entity::prelude::*;" in find_symbol_declaration_lines(
        "prelude", "rust", sides)
    assert "mod tests;" in find_symbol_declaration_lines("tests", "rust", sides)


def test_find_rust_excludes_definitions_and_comments():
    sides = (
        "// use crate::x::Symbol;\n"
        "fn Symbol() -> u32 { 3 }\n"
        "let Symbol = 5;\n"
    )
    assert find_symbol_declaration_lines("Symbol", "rust", sides) == []


def test_find_c_prototype_typedef_forward():
    sides = (
        "#include <stdio.h>\n"
        "int cliSwitchProto(int argc, char **argv);\n"
        "typedef struct BtCursor BtCursor;\n"
        "struct sqlite3;\n"
        "int main(void) { return 0; }\n"
    )
    decls = find_symbol_declaration_lines("cliSwitchProto", "c", sides)
    assert decls == ["int cliSwitchProto(int argc, char **argv);"]
    assert find_symbol_declaration_lines("BtCursor", "c", sides) == [
        "typedef struct BtCursor BtCursor;"]
    assert find_symbol_declaration_lines("sqlite3", "c", sides) == [
        "struct sqlite3;"]


def test_find_c_excludes_bodies_and_assignments():
    sides = (
        "int broken(int x) { return x; }\n"
        "int assigned = symbol_fn;\n"
    )
    assert find_symbol_declaration_lines("broken", "c", sides) == []
    assert find_symbol_declaration_lines("symbol_fn", "c", sides) == []


# ---------------------------------------------------------------------------
# inject_symbol_declaration — placement and dedup.
# ---------------------------------------------------------------------------

def test_inject_rust_after_use_block():
    buf = "use std::fmt;\n\nfn main() { let _ = item::x; }\n"
    out = inject_symbol_declaration(buf, "use crate::item;", "rust")
    assert out is not None
    assert out.splitlines()[1] == "use crate::item;"
    assert out.splitlines()[0] == "use std::fmt;"


def test_inject_c_after_includes():
    buf = "#include <stdio.h>\n#include \"local.h\"\n\nint main(void) { return f(); }\n"
    out = inject_symbol_declaration(buf, "int f(void);", "c")
    assert out is not None
    lines = out.splitlines()
    assert lines.index("int f(void);") == 2  # right after the last #include


def test_inject_dedups_existing():
    buf = "use std::fmt;\nuse crate::item;\n\nfn main() {}\n"
    assert inject_symbol_declaration(
        buf, "use crate::item;", "rust") is None


def test_inject_preserves_trailing_newline():
    buf = "#include <stdio.h>\nint f(void);\n"
    out = inject_symbol_declaration(buf, "int g(void);", "c")
    assert out is not None and out.endswith("\n")


def test_injection_point_rust_leading_attrs():
    buf = "#![allow(dead_code)]\n\nuse std::fmt;\n\npub fn main() {}\n"
    assert symbol_injection_point(buf, "rust") == 3  # after `use std::fmt;`


def test_injection_point_fallback_top():
    assert symbol_injection_point("int f(void) { return 0; }\n", "c") == 0


# ---------------------------------------------------------------------------
# The composite behavior (what the orchestrator hook does).
# ---------------------------------------------------------------------------

def test_composite_c_missing_prototype():
    """The redis-0002 shape: merge drops a prototype; the side has it."""
    base = "#include <stdio.h>\nint f(void);\n\nint main(void) { return f(); }\n"
    merged = "#include <stdio.h>\n\nint main(void) { return f(); }\n"
    errors = "t.c:3:22: error: 'f' undeclared (first use in this function)"
    (sym,) = parse_missing_symbols(errors, "c")
    assert sym == "f"
    decls = find_symbol_declaration_lines(sym, "c", base)
    out = inject_symbol_declaration(merged, decls[0], "c")
    assert out is not None
    assert "int f(void);" in out
    assert out.index("int f(void);") < out.index("int main")


def test_composite_rust_missing_use():
    """The axum-0019 shape: merge drops a use; the side has it."""
    side = (
        "use std::fmt;\n"
        "use crate::extra::item;\n"
        "\n"
        "pub fn go() -> u32 { item::next() }\n"
    )
    merged = "use std::fmt;\n\npub fn go() -> u32 { item::next() }\n"
    errors = "error: prefix `item` is unknown"
    (sym,) = parse_missing_symbols(errors, "rust")
    assert sym == "item"
    decls = find_symbol_declaration_lines(sym, "rust", side)
    assert decls == ["use crate::extra::item;"]
    out = inject_symbol_declaration(merged, decls[0], "rust")
    assert out is not None
    assert "use crate::extra::item;" in out.splitlines()

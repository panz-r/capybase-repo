"""Tests for the C skeleton extractor and its prompt integration.

The skeleton extractor is a lightweight depth-tracking scanner that yields
top-level entity names (includes, macros, typedefs, structs, functions,
globals) without building a full parser. These tests cover:

- Core extraction (each entity kind)
- Masking (strings/comments don't fool the brace/paren counter)
- Macro robustness (function-like macros, line continuations)
- Safe degradation (X-macros, unbalanced braces)
- render() dedup + token budget truncation
- Prompt integration: the skeleton block appears for oversized C files only
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from capybase.adapters.c_skeleton import extract_skeleton  # noqa: E402


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------

def test_includes_and_basic_entities():
    src = """\
#include <stdio.h>
#include "sqliteInt.h"
#define SQLITE_OK 0
#define SQLITE_ERROR 1
typedef unsigned int u32;
int sqlite3BtreeOpen(const char *z, sqlite3 **pp);
int sqlite3Step(Vdbe *p) { return 0; }
int sqlite3_os_type;
"""
    sk = extract_skeleton(src)
    assert sk.includes == ["stdio.h", "sqliteInt.h"]
    assert sk.macros == ["SQLITE_OK", "SQLITE_ERROR"]
    assert sk.typedefs == ["u32"]
    # Function defined with a body is still captured by name.
    assert any(f.startswith("sqlite3BtreeOpen(") for f in sk.functions)
    assert any(f.startswith("sqlite3Step(") for f in sk.functions)
    assert "sqlite3_os_type" in sk.globals


def test_struct_union_enum_tags():
    src = """\
struct Btree { int x; };
union Tagged { int i; float f; };
enum Color { RED, GREEN, BLUE };
"""
    sk = extract_skeleton(src)
    assert "Btree" in sk.structs
    assert "Tagged" in sk.structs
    assert "Color" in sk.structs


def test_function_like_macro_recorded():
    src = """\
#define MAX(a,b) ((a)>(b)?(a):(b))
#define UNUSED(x) (void)(x)
"""
    sk = extract_skeleton(src)
    # Function-like macros should keep the parens so the model sees they
    # take arguments.
    assert "MAX(...)" in sk.macros
    assert "UNUSED(...)" in sk.macros


# ---------------------------------------------------------------------------
# Masking & robustness
# ---------------------------------------------------------------------------

def test_strings_and_comments_dont_affect_brace_depth():
    src = """\
int f(void) {
    /* comment with } brace */
    char *s = "string with { brace } and ( paren )";
    int n = strlen(s);
    return n;
}
int g(void) { return 1; }
"""
    sk = extract_skeleton(src)
    # If the comment/string masking failed, the first } in the comment or
    # string would prematurely close f()'s body and g() would be swallowed
    # into f()'s body. Both must be captured.
    names = {f.split("(", 1)[0] for f in sk.functions}
    assert "f" in names
    assert "g" in names


def test_line_continuation_joined():
    src = """\
#define LONG_MACRO(x) \\
    do { \\
        (x) *= 2; \\
    } while (0)
int after_macro(void);
"""
    sk = extract_skeleton(src)
    assert "LONG_MACRO(...)" in sk.macros
    assert any(f.startswith("after_macro(") for f in sk.functions)


def test_x_macro_does_not_crash():
    # X-macros deliberately produce unbalanced-looking token streams. The
    # extractor must not crash and should still extract what it can.
    src = """\
#define X(a, b) a##_INIT = b,
struct Config {
    int X(A, 1)
    int X(B, 2)
    int X(C, 3)
};
#undef X
int main(void);
"""
    sk = extract_skeleton(src)
    # Must terminate without raising; main() should be caught.
    assert any(f.startswith("main(") for f in sk.functions)


def test_empty_file():
    sk = extract_skeleton("")
    assert sk.entity_count == 0
    assert sk.render() == ""


# ---------------------------------------------------------------------------
# render()
# ---------------------------------------------------------------------------

def test_render_dedup():
    src = """\
#include <stdio.h>
#include <stdio.h>
#include <stdio.h>
#define FOO 1
#define FOO 2
int a(void);
int a(int);
"""
    sk = extract_skeleton(src)
    out = sk.render(max_tokens=400)
    # stdio.h collapsed to a single occurrence.
    assert out.count("stdio.h") == 1
    # FOO collapsed to one macro entry.
    assert out.count("FOO") == 1
    # Two distinct a(...) signatures survive (dedup is on name-before-paren
    # but both are rendered because we keep the first occurrence — wait,
    # actually the dedup key is name-before-paren, so a(void) and a(int)
    # collapse. This is the intended behavior: we report a is defined, not
    # every signature. Assert exactly one a(...) line.
    assert out.count("a(") == 1


def test_render_truncates_at_token_budget():
    src = "\n".join(
        f"int func_{i}(void) {{ return {i}; }}" for i in range(200)
    )
    sk = extract_skeleton(src)
    out = sk.render(max_tokens=60)  # very tight budget
    # The functions line should hit the char ceiling and stop, then the
    # renderer should not add a Globals line (budget exhausted).
    assert "File skeleton" in out
    # Roughly bounded by the token budget (4 chars/token * 60 + some slack).
    assert len(out) < 60 * 4 + 80


# ---------------------------------------------------------------------------
# Prompt integration
# ---------------------------------------------------------------------------

def test_skeleton_appears_in_prompt_for_oversized_c_file():
    """When the base is oversized and the language is C, the prompt should
    include the file skeleton block."""
    from capybase.resolution_engine import _fit_to_budget

    # Build a ConflictUnit stand-in with the minimum attributes the
    # skeleton path touches. _fit_to_budget reads unit.language,
    # unit.original_worktree_text, unit.base.text, and
    # unit.structural_metadata.
    class _FakeBase:
        text = ""

    class _FakeUnit:
        language = "c"
        original_worktree_text = (
            "#include <stdio.h>\n"
            "#define SQLITE_OK 0\n"
            "int sqlite3BtreeOpen(const char *z, sqlite3 **pp);\n"
            "int sqlite3_os_type;\n"
        ) * 400  # well over _SIDES_MAX_CHARS
        base = _FakeBase()
        structural_metadata: dict = {}

    out = _fit_to_budget(
        budget=None,
        intro="",
        contract="",
        rules="",
        sides_text="",
        structural_anchor="",
        siblings_block="",
        deps="",
        few_shot="",
        primary_text="",
        unit=_FakeUnit(),  # type: ignore[arg-type]
    )
    # 9-tuple: last element is the skeleton block.
    assert len(out) == 9
    skeleton_block = out[-1]
    assert "File skeleton" in skeleton_block
    assert "SQLITE_OK" in skeleton_block
    assert "sqlite3BtreeOpen" in skeleton_block


def test_skeleton_absent_for_small_files():
    """Small files (under _SIDES_MAX_CHARS) should not trigger the skeleton
    even if the language is C."""
    from capybase.resolution_engine import _fit_to_budget

    class _FakeBase:
        text = ""

    class _FakeUnit:
        language = "c"
        original_worktree_text = "int main(void) { return 0; }\n"  # tiny
        base = _FakeBase()
        structural_metadata: dict = {}

    out = _fit_to_budget(
        budget=None,
        intro="",
        contract="",
        rules="",
        sides_text="",
        structural_anchor="",
        siblings_block="",
        deps="",
        few_shot="",
        primary_text="",
        unit=_FakeUnit(),  # type: ignore[arg-type]
    )
    skeleton_block = out[-1]
    assert skeleton_block == ""


def test_skeleton_absent_for_non_c_languages():
    """Python / Rust files should not trigger the C skeleton extractor."""
    from capybase.resolution_engine import _fit_to_budget

    class _FakeBase:
        text = ""

    class _FakeUnit:
        language = "python"
        original_worktree_text = "x = 1\n" * 5000
        base = _FakeBase()
        structural_metadata: dict = {}

    out = _fit_to_budget(
        budget=None,
        intro="",
        contract="",
        rules="",
        sides_text="",
        structural_anchor="",
        siblings_block="",
        deps="",
        few_shot="",
        primary_text="",
        unit=_FakeUnit(),  # type: ignore[arg-type]
    )
    assert out[-1] == ""

"""Tests for the C/C++ compile floor (gcc/clang -fsyntax-only).

These exercise ``_compile_ccs`` directly and (in later-added test sections) the
``CcsSyntaxValidator`` per-unit check and the ``verify_file`` C/C++ branch. The
gcc/g++-backed tests skip when the compiler is absent (CI without a toolchain);
the wiring / graceful-degrade tests run unconditionally via monkeypatch.
"""

from __future__ import annotations

import shutil

import pytest

from capybase.verification import (
    ValidationConfig,
    VerificationEngine,
    _compile_ccs,
    _is_ccs_resolution_error,
)


GCC = shutil.which("gcc")
GXX = shutil.which("g++")
skip_no_gcc = pytest.mark.skipif(GCC is None, reason="gcc not installed")
skip_no_gxx = pytest.mark.skipif(GXX is None, reason="g++ not installed")


# ---------------------------------------------------------------------------
# _compile_ccs (real gcc/g++)
# ---------------------------------------------------------------------------


@skip_no_gcc
def test_compile_c_clean_source():
    ok, msg = _compile_ccs("int main(void) { return 0; }\n", cc_path="gcc",
                           std="c11", suffix=".c")
    assert ok is True
    assert msg == "cc ok"


@skip_no_gcc
def test_compile_c_detects_syntax_error():
    # Missing semicolon — a true parse error gcc must catch.
    src = "int main(void) { return 0 }\n"
    ok, msg = _compile_ccs(src, cc_path="gcc", std="c11", suffix=".c")
    assert ok is False
    # gcc format: "file:line:col: error: ..."; the message carries the error.
    assert "error" in msg
    assert "expected" in msg


@skip_no_gxx
def test_compile_cpp_clean_source():
    ok, msg = _compile_ccs("int main() { return 0; }\n", cc_path="g++",
                           std="c++17", suffix=".cpp")
    assert ok is True
    assert msg == "cc ok"


@skip_no_gxx
def test_compile_cpp_detects_syntax_error():
    # Unterminated string — a parse error.
    src = 'int main() { char *s = "unterminated; return 0; }\n'
    ok, msg = _compile_ccs(src, cc_path="g++", std="c++17", suffix=".cpp")
    assert ok is False
    assert "error" in msg


@skip_no_gcc
def test_compile_ccs_std_rejects_bogus():
    # An unrecognized -std= makes gcc emit a flag error (gcc: error: ...).
    ok, msg = _compile_ccs("int main(void){return 0;}\n", cc_path="gcc",
                           std="c999", suffix=".c")
    assert ok is False
    assert "error" in msg or "unrecognized" in msg


@skip_no_gcc
def test_compile_ccs_header_file_compiles_standalone():
    # Headers (.h) are valid translation units under -fsyntax-only (declarations
    # only); gcc needs no .c driver wrapper. This pins the header edge case.
    hdr = "#ifndef H\n#define H\nint add(int a, int b);\n#endif\n"
    ok, msg = _compile_ccs(hdr, cc_path="gcc", std="c11", suffix=".h")
    assert ok is True, msg


@skip_no_gcc
def test_compile_ccs_missing_binary_raises_file_not_found():
    # A missing compiler raises FileNotFoundError — the caller gates on this to
    # report "not checked" rather than a false failure.
    with pytest.raises(FileNotFoundError):
        _compile_ccs("int main(void){return 0;}\n",
                     cc_path="definitely-not-a-real-compiler-xyz",
                     std="c11", suffix=".c")


# ---------------------------------------------------------------------------
# _is_ccs_resolution_error (semantic vs parse classification; no toolchain)
# ---------------------------------------------------------------------------


def test_is_ccs_resolution_error_classifies_semantic():
    # Each semantic pattern → True (deferred to Phase B; not a per-unit defect).
    semantic = [
        "x.c:5:3: error: use of undeclared identifier 'foo'",
        "x.cpp:10:5: error: 'bar' was not declared in this scope",
        "x.c:3:2: error: 'T' has not been declared",
        "x.cpp:8:3: error: no matching function for call to 'f'",
        "x.cpp:9:3: error: cannot convert 'int' to 'char*'",
        "x.c:4:8: error: invalid use of incomplete type 'struct S'",
        "x.cpp:12:4: error: 'x' is not a member of 'Foo'",
        "x.cpp:1:1: error: 'Bar' does not name a type",
        "x.cpp:7:3: error: 'class Foo' has no member named 'baz'",
        "x.cpp:1: undefined reference to `symbol'",
    ]
    for msg in semantic:
        assert _is_ccs_resolution_error(msg), f"expected True for: {msg!r}"


def test_is_ccs_resolution_error_surfaces_parse_errors():
    # Parse errors don't match any semantic pattern → False (surfaced as defects).
    parse = [
        "x.c:5:3: error: expected ';' before '}' token",
        "x.c:1:1: error: expected '=', ',', ';', 'asm' or '__attribute__'",
        "x.c:2:5: error: stray '\\342' in program",
        "x.c:3:1: error: unterminated string literal",
        "x.c:4:8: error: missing terminating \" character",
        "x.c:5:3: error: expected expression before '}' token",
        "x.c:6:1: error: expected declaration specifiers or '...' before 'x'",
    ]
    for msg in parse:
        assert not _is_ccs_resolution_error(msg), f"expected False for: {msg!r}"


def test_is_ccs_resolution_error_empty_and_none():
    assert not _is_ccs_resolution_error("")
    assert not _is_ccs_resolution_error(None)  # type: ignore[arg-type]


def test_is_ccs_resolution_error_case_insensitive():
    # gcc/clang message case can vary by locale/version; matching is case-blind.
    assert _is_ccs_resolution_error("Error: Use of Undeclared Identifier 'x'")
    assert _is_ccs_resolution_error("X.C:1:1: ERROR: Cannot Convert 'int'")

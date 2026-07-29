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

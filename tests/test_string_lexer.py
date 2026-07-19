"""Spec for the canonical string/comment lexer (string_lexer.py).

This module is the single source of truth for blanking string-literal and
comment contents across the parser, resolver, verifier, and consensus layers.
These tests pin its contract directly so the migration of the 7 prior blanking
sites (rounds 38-48) can proceed against a stable target.

Coverage required (each must hold for the migration to be behavior-preserving):
- Regular strings (``"..."``, ``'...'``, escapes).
- Python triple-quotes (``\"\"\"...\"\"\"``, multi-line).
- Rust raw strings (``r"..."``, ``r#"..."#``, any hash count).
- C++ raw strings (``R"DELIM(...)DELIM"``, prefixed forms).
- Rust char literals vs lifetimes vs C++14 digit separators.
- Comments (``//`` line, ``/* */`` block for Family A; ``#`` for Family B).
- f-string interpolation preservation (the validator path).
- Length preservation (byte offsets align with the original).
"""

from __future__ import annotations

from capybase.adapters.string_lexer import (
    blank_strings_and_comments,
    blank_strings,
    blank_comments,
    blank_raw_strings,
)


# ---------------------------------------------------------------------------
# Regular strings
# ---------------------------------------------------------------------------


def test_regular_double_quoted_string_blanked():
    assert blank_strings_and_comments('x = "hello"', "python") == 'x = _______'


def test_regular_single_quoted_string_blanked():
    # 'hi' is 4 chars (quote, h, i, quote) → 4 blanks.
    assert blank_strings_and_comments("x = 'hi'", "python") == "x = ____"


def test_escape_sequence_blanked_length_preserving():
    # backslash + escaped char both blanked; length preserved.
    src = 'x = "a\\"b"'  # the string is a"b (3 content chars)
    out = blank_strings_and_comments(src, "python")
    assert len(out) == len(src)
    assert out.startswith("x = ")


def test_adjacent_strings_both_blanked():
    src = 'x = "a" + "b"'
    out = blank_strings_and_comments(src, "python")
    assert out == "x = ___ + ___"


# ---------------------------------------------------------------------------
# Python triple-quotes
# ---------------------------------------------------------------------------


def test_triple_double_quote_blanked_multiline():
    src = 'x = """\nline1\nline2\n"""'
    out = blank_strings_and_comments(src, "python")
    # The whole triple-quoted block is blanked; newlines preserved.
    assert '"' not in out.replace('x = ', "")
    assert "\n" in out  # newlines kept


def test_triple_single_quote_blanked():
    src = "x = '''hi'''"
    out = blank_strings_and_comments(src, "python")
    assert "hi" not in out


def test_string_inside_triple_quote_not_double_counted():
    # A ``"`` inside a triple-quoted string must NOT close early.
    src = 'x = """has "quote" inside"""'
    out = blank_strings_and_comments(src, "python")
    assert "quote" not in out
    assert "has" not in out


# ---------------------------------------------------------------------------
# Rust raw strings
# ---------------------------------------------------------------------------


def test_rust_raw_string_no_hash_blanked():
    src = 'let x = r"hello";'
    out = blank_strings_and_comments(src, "rust")
    assert "hello" not in out
    assert ";" in out


def test_rust_raw_string_hash_count_exact_match():
    # r##"..."## — the closer must have EXACTLY 2 hashes (Rust Reference).
    # An interior "### (3 hashes) must NOT close a 2-hash string (3 ≠ 2).
    src = 'let x = r##"content with "### hashes and more"##;'
    out = blank_strings_and_comments(src, "rust")
    # The 3-hash interior must NOT close the 2-hash string — "and more" is
    # string content, blanked; the real closer "## ends the string.
    assert "and more" not in out
    assert "hashes" not in out
    assert ";" in out


def test_rust_raw_string_embedded_quote():
    # An embedded ``"`` in a raw string must not close it.
    src = 'let x = r#"embed " quote"#;'
    out = blank_strings_and_comments(src, "rust")
    assert "embed" not in out
    assert "quote" not in out
    assert ";" in out


def test_rust_byte_raw_string():
    src = 'let x = br#"bytes"#;'
    out = blank_strings_and_comments(src, "rust")
    assert "bytes" not in out


# ---------------------------------------------------------------------------
# C++ raw strings
# ---------------------------------------------------------------------------


def test_cpp_raw_string_blanked():
    src = 'auto s = R"(has "quote" inside)";'
    out = blank_strings_and_comments(src, "cpp")
    assert "has" not in out
    assert "quote" not in out
    assert ";" in out


def test_cpp_raw_string_delimiter():
    src = 'auto s = R"DELIM(content "with" quotes)DELIM";'
    out = blank_strings_and_comments(src, "cpp")
    assert "content" not in out
    assert "DELIM" not in out or "_" in out  # delim blanked with the string


def test_cpp_raw_string_prefixed_forms():
    for pref in ("u8R", "LR", "uR", "UR"):
        src = f'{pref}"x(has "q" inside)x";'
        out = blank_strings_and_comments(src, "cpp")
        assert "inside" not in out, f"{pref}-prefixed raw string leaked: {out!r}"


# ---------------------------------------------------------------------------
# Char literals, lifetimes, digit separators
# ---------------------------------------------------------------------------


def test_rust_char_literal_blanked():
    src = "let c = 'x';"
    out = blank_strings_and_comments(src, "rust")
    assert "'x'" not in out


def test_rust_lifetime_not_blanked():
    # 'a is a lifetime, not a char literal — must stay as code.
    src = "fn f<'a>(x: &'a i32) {}"
    out = blank_strings_and_comments(src, "rust")
    assert "'a" in out  # lifetime preserved
    assert "{}" in out


def test_cpp14_digit_separator_not_blanked():
    src = "int n = 1'000'000;"
    out = blank_strings_and_comments(src, "cpp")
    assert "1'000'000" in out  # digit separators preserved


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


def test_family_a_line_comment_blanked():
    src = "x = 1  // a comment\ny = 2"
    out = blank_strings_and_comments(src, "rust")
    assert "comment" not in out
    assert "x = 1" in out
    assert "y = 2" in out


def test_family_a_block_comment_blanked():
    src = "x = /* block */ 1"
    out = blank_strings_and_comments(src, "rust")
    assert "block" not in out
    assert "1" in out


def test_family_b_hash_comment_blanked():
    src = "x = 1  # a comment\ny = 2"
    out = blank_strings_and_comments(src, "python")
    assert "comment" not in out
    assert "x = 1" in out
    assert "y = 2" in out


def test_hash_not_comment_in_family_a():
    # Rust #[attr] is NOT a comment.
    src = "#[derive(Debug)]\nstruct S;"
    out = blank_strings_and_comments(src, "rust")
    assert "derive" in out  # attribute preserved
    assert "Debug" in out


def test_slash_not_comment_in_family_b():
    # Python // is floor division, NOT a comment.
    src = "x = 10 // 3"
    out = blank_strings_and_comments(src, "python")
    assert "10 // 3" in out  # operator preserved


def test_comment_inside_string_not_stripped():
    # A ``//`` inside a string literal is string content, not a comment.
    src = 'x = "http://example.com"'
    out = blank_strings_and_comments(src, "rust")
    assert "example" not in out  # the URL is string content, blanked


# ---------------------------------------------------------------------------
# f-string interpolation preservation
# ---------------------------------------------------------------------------


def test_fstring_interpolation_preserved_when_requested():
    src = 'x = f"val={foo()}"'
    out = blank_strings_and_comments(
        src, "python", preserve_fstring_interpolation=True
    )
    # The ``foo()`` call inside the interpolation is preserved (as code).
    assert "foo()" in out


def test_fstring_interpolation_blanked_by_default():
    src = 'x = f"val={foo()}"'
    out = blank_strings_and_comments(src, "python")
    # By default the whole f-string (including the interpolation) is blanked.
    assert "foo()" not in out


# ---------------------------------------------------------------------------
# Length preservation
# ---------------------------------------------------------------------------


def test_length_preserved_on_complex_input():
    src = (
        'x = "str"  // comment\n'
        'y = r#"raw"#\n'
        "z = 'c'\n"
        "# line comment\n"
        "w = 1"
    )
    out = blank_strings_and_comments(src, "rust")
    assert len(out) == len(src), (
        f"length changed: {len(src)} -> {len(out)}\n{src!r}\n{out!r}"
    )


# ---------------------------------------------------------------------------
# Granular wrappers
# ---------------------------------------------------------------------------


def test_blank_strings_only_preserves_comments():
    src = 'x = "str"  // comment'
    out = blank_strings(src, "rust")
    assert "str" not in out  # string blanked
    assert "comment" in out  # comment preserved


def test_blank_comments_only_preserves_strings():
    src = 'x = "str"  // comment'
    out = blank_comments(src, "rust")
    assert "str" in out  # string preserved
    assert "comment" not in out  # comment blanked


def test_blank_raw_strings_isolates_raw():
    # Raw strings blanked, regular strings preserved (for the two-pass case).
    src = 'a = r#"raw"# b = "regular"'
    out = blank_raw_strings(src)
    assert "raw" not in out
    assert "regular" in out


# ---------------------------------------------------------------------------
# Edge cases / robustness
# ---------------------------------------------------------------------------


def test_empty_input():
    assert blank_strings_and_comments("", "python") == ""


def test_no_strings_or_comments():
    src = "x = 1 + 2"
    assert blank_strings_and_comments(src, "python") == src


def test_unterminated_string_no_crash():
    # Malformed input must not crash (best-effort).
    src = 'x = "never closed'
    out = blank_strings_and_comments(src, "python")
    assert isinstance(out, str)
    assert len(out) == len(src)


def test_unterminated_block_comment_no_crash():
    src = "x = /* never closed"
    out = blank_strings_and_comments(src, "rust")
    assert isinstance(out, str)
    assert len(out) == len(src)

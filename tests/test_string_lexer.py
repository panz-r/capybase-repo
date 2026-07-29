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


def test_c_char_literal_blanked():
    # C char literal 'x' is a string-literal form, must be blanked.
    src = "char c = 'x';"
    out = blank_strings_and_comments(src, "c")
    assert "'x'" not in out
    assert "char c =" in out


def test_cpp_char_literal_blanked():
    src = "char c = 'x';"
    out = blank_strings_and_comments(src, "cpp")
    assert "'x'" not in out


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


def test_c_block_comment_blanked():
    # Family-A block comments must be blanked for C — pinned (not assumed).
    src = "int x = /* hidden */ 1;"
    out = blank_strings_and_comments(src, "c")
    assert "hidden" not in out
    assert "int x =" in out
    assert "1;" in out


def test_cpp_block_comment_blanked():
    src = "int x = /* hidden */ 1;"
    out = blank_strings_and_comments(src, "cpp")
    assert "hidden" not in out
    assert "1;" in out


def test_cpp_multiline_block_comment_blanked():
    src = "int x = /* a\nmulti\nline\nblock */ 1;"
    out = blank_strings_and_comments(src, "cpp")
    assert "multi" not in out
    assert "block" not in out
    assert "1;" in out
    assert len(out) == len(src)


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


def test_length_preserved_on_complex_cpp_input():
    # Every tricky C/C++ construct at once: block comment, C++ raw string, char
    # literal, line comment, digit separator — all must be length-preserving.
    # The raw-string delimiter is ``DL`` so the closer ``)DL"`` cannot appear in
    # the body (a real C++ constraint: the delimiter must not occur in content).
    src = (
        'auto s = R"DL(raw { body } )DL";  /* block */\n'
        "char c = 'x';\n"
        "// line comment\n"
        "int n = 1'000'000;\n"
        "int w = 1;"
    )
    out = blank_strings_and_comments(src, "cpp")
    assert len(out) == len(src), (
        f"length changed: {len(src)} -> {len(out)}\n{src!r}\n{out!r}"
    )
    # Sanity: comment/string bodies are gone, code survives.
    assert "raw" not in out
    assert "block" not in out
    assert "line comment" not in out
    assert "int w = 1;" in out


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


# ---------------------------------------------------------------------------
# PHP comment-style bugfix + comment-blanker delegation parity
# ---------------------------------------------------------------------------


def test_php_blanks_both_hash_and_slash_comments():
    """Regression: PHP uses BOTH ``//`` and ``#`` as line comments, but
    ``blank_comments`` formerly treated PHP as slash-only (because
    ``_lang_uses_slash_comments('php')`` is True), leaving ``#`` comments
    unblanked. Both styles must now be blanked."""
    src = "# php hash\n// php slash\ncode"
    out = blank_comments(src, "php")
    assert "#" not in out.split("\n")[0], f"hash comment not blanked: {out!r}"
    assert "//" not in out.split("\n")[1], f"slash comment not blanked: {out!r}"
    assert "code" in out.split("\n")[2]


def test_structural_blank_comments_matches_canonical_lexer():
    """``structural._blank_comments`` now delegates to ``string_lexer.blank_comments``.
    Every call site runs ``_blank_text_strings`` FIRST (so strings are already
    blanked when the comment blanker sees the text). Assert the two agree
    byte-for-byte on post-string-blanked input across languages and comment
    styles — proves the delegation preserves behavior."""
    from capybase.adapters.structural import _blank_comments as struct_bc
    from capybase.adapters.structural import _blank_text_strings
    cases = [
        ("# hash comment\ncode", "python"),
        ("// slash comment\ncode", "rust"),
        ("/* block */ code", "rust"),
        ("/* multi\nline\nblock */ after", "rust"),
        ("# php hash\n// php slash\ncode", "php"),
        ("// has \"quote\" inside", "rust"),
        ("# has \"quote\" inside", "python"),
        ("let a = \"str\"; // comment\nlet b = 2;", "rust"),
        ("f\"{x}\" + \"plain\" # comment", "python"),
        ("plain text no comments here", "python"),
        # C/C++ — pin the Family-A delegation (// and /* */) under c/cpp.
        ("// slash comment\ncode", "c"),
        ("/* block */ code", "cpp"),
        ("/* multi\nline\nblock */ after", "c"),
        ("int a = \"str\"; // comment\nint b = 2;", "cpp"),
    ]
    for text, lang in cases:
        preblanked = _blank_text_strings(text)  # strings gone, as at call sites
        canonical = blank_comments(preblanked, lang)
        delegated = struct_bc(preblanked, lang)
        assert delegated == canonical, (
            f"_blank_comments divergence for [{lang}] {text!r}:\n"
            f"  structural: {delegated!r}\n  canonical:  {canonical!r}"
        )


# ---------------------------------------------------------------------------
# C/C++ line continuation (backslash-newline splices a ``//`` comment)
# ---------------------------------------------------------------------------


def test_c_line_continued_comment_continues_under_c():
    # A ``//`` comment ending in ``\`` runs onto the next physical line in C/C++.
    src = "int x = 1; // comment one \\\n still comment here \nint y = 2;\n"
    out = blank_strings_and_comments(src, "c")
    # The continued body must be blanked...
    assert "still comment here" not in out
    assert "comment one" not in out
    # ...and code after the (real) newline survives.
    assert "int y = 2;" in out
    assert "int x = 1;" in out
    # Length is preserved.
    assert len(out) == len(src)


def test_cpp_line_continued_comment_continues_under_cpp():
    src = "int x = 1; // comment one \\\n leaked body \nint y = 2;\n"
    out = blank_strings_and_comments(src, "cpp")
    assert "leaked body" not in out
    assert "int y = 2;" in out
    assert len(out) == len(src)


def test_c_line_continuation_chains_across_multiple_lines():
    # Each trailing ``\`` extends the comment one more physical line.
    src = "// a \\\n// b \\\n// c \nint x = 1;\n"
    out = blank_strings_and_comments(src, "c")
    # All three comment fragments blanked...
    assert " a " not in out
    assert " b " not in out
    assert " c " not in out
    # ...code after the final (un-continued) newline survives.
    assert "int x = 1;" in out


def test_line_continuation_does_not_apply_to_rust():
    # Rust does NOT splice backslash-newline — the comment ends at the newline
    # and the next line is ordinary code. This is the regression guard.
    src = "// comment one \\\n code on next line\n"
    out = blank_strings_and_comments(src, "rust")
    assert "comment one" not in out
    # The ``\`` itself was part of the comment (blanked), but the next line is
    # real code and must survive unblanked.
    assert "code on next line" in out


def test_c_line_continuation_macro_braces_not_skewed():
    # The motivating case: a ``//`` comment whose continued line contains braces
    # would, without continuation handling, leak those braces into brace-balance.
    src = (
        "int before = 0;\n"
        "// macro note: do { something } while(0) \\\n"
        "   and more { braces } here \n"
        "int after = 1;\n"
    )
    out = blank_strings_and_comments(src, "c")
    # The continued comment's braces are gone...
    assert "something" not in out
    assert "braces" not in out
    # ...so the only braces left are from real code (none here) — net zero.
    assert out.count("{") == 0
    assert out.count("}") == 0
    assert "int before = 0;" in out
    assert "int after = 1;" in out


def test_line_continuation_backslash_inside_string_unaffected():
    # A ``\`` before a newline INSIDE a string literal is a string escape, not a
    # comment continuation — must not change string-blanking behavior.
    src = 'char *s = "abc \\\n def"; // real comment\nint x = 1;\n'
    out = blank_strings_and_comments(src, "c")
    assert " def" not in out  # string body blanked
    assert "real comment" not in out
    assert "int x = 1;" in out
    assert len(out) == len(src)

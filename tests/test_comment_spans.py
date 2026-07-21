"""Tests for enumerate_comment_spans — the byte-exact comment-span extractor.

The canonical lexer's char-scan already tracks comment state (in_line_comment /
in_block_comment). enumerate_comment_spans reuses that state machine to emit
(start_byte, end_byte_exclusive, comment_text) tuples for every comment region,
without running the blanker. This is the foundation for the deferred-comment-
reconciliation system (classify → mask → reconcile).

Coverage:
- ``//`` line comments (Family A)
- ``/* */`` block comments (Family A, multi-line)
- ``#`` line comments (Family B / Python)
- Comments inside string literals are NOT counted (the string absorbs them)
- Nested block comments (Rust `/* /* */ */`)
- Doc comments (Rust `///`, `//!`) — counted as comments (classification is later)
- Attributes (Rust `#[...]`) are NOT comments (they're code)
- Byte offsets align with the original text (length-preserving invariant)
"""

from __future__ import annotations

from capybase.adapters.string_lexer import enumerate_comment_spans


def test_line_comment_family_a():
    """A ``//`` line comment in a Family-A language (Rust)."""
    text = "let x = 1; // a comment\nlet y = 2;\n"
    spans = enumerate_comment_spans(text, "rust")
    assert len(spans) == 1
    start, end, comment_text = spans[0]
    assert text[start:end] == "// a comment"
    assert "a comment" in comment_text


def test_block_comment_family_a():
    """A ``/* */`` block comment, multi-line."""
    text = "let x = 1; /* block\ncomment */ let y = 2;"
    spans = enumerate_comment_spans(text, "rust")
    assert len(spans) == 1
    start, end, comment_text = spans[0]
    assert text[start:end] == "/* block\ncomment */"
    assert "block" in comment_text


def test_line_comment_family_b():
    """A ``#`` line comment in a Family-B language (Python)."""
    text = "x = 1  # a comment\ny = 2\n"
    spans = enumerate_comment_spans(text, "python")
    assert len(spans) == 1
    start, end, comment_text = spans[0]
    assert text[start:end] == "# a comment"


def test_comment_inside_string_not_counted():
    """A ``//`` or ``#`` inside a string literal is string content, not a comment."""
    text = 'let url = "http://example.com"; // real comment'
    spans = enumerate_comment_spans(text, "rust")
    # Only the real comment, not the URL's //.
    assert len(spans) == 1
    assert "real comment" in spans[0][2]


def test_hash_inside_string_not_comment_in_family_a():
    """In Family A (Rust), ``#`` is an attribute, not a comment — but ``#`` inside
    a string is string content either way."""
    text = 'let s = "# not a comment";'
    spans = enumerate_comment_spans(text, "rust")
    assert spans == []


def test_doc_comments_counted_as_comments():
    """Rust ``///`` and ``//!`` are comments (doc comments); classification into
    DOCTEST vs DEFERRED happens later."""
    text = "/// Doc comment\n//! Inner doc\nfn foo() {}"
    spans = enumerate_comment_spans(text, "rust")
    assert len(spans) == 2
    assert "Doc comment" in spans[0][2]
    assert "Inner doc" in spans[1][2]


def test_attribute_not_a_comment():
    """Rust ``#[derive(Debug)]`` is an attribute (code), not a comment."""
    text = "#[derive(Debug)]\nstruct S;\n"
    spans = enumerate_comment_spans(text, "rust")
    assert spans == []


def test_multiple_comments():
    """Multiple comments in one text, each emitted as a separate span."""
    text = "// first\nlet x = 1; // second\n/* third */\n"
    spans = enumerate_comment_spans(text, "rust")
    assert len(spans) == 3
    assert "first" in spans[0][2]
    assert "second" in spans[1][2]
    assert "third" in spans[2][2]


def test_byte_offsets_align_with_original():
    """The spans' (start, end) must exactly slice the original text."""
    text = "let x = 1; /* comment */ let y = 2; // line\n"
    spans = enumerate_comment_spans(text, "rust")
    for start, end, comment_text in spans:
        assert text[start:end] == comment_text


def test_empty_input():
    assert enumerate_comment_spans("", "rust") == []


def test_no_comments():
    text = "let x = 1;\nlet y = 2;\n"
    assert enumerate_comment_spans(text, "rust") == []


def test_nested_block_comment_rust():
    """Rust supports nested block comments: /* outer /* inner */ still outer */."""
    text = "/* outer /* inner */ still outer */ let x = 1;"
    spans = enumerate_comment_spans(text, "rust")
    assert len(spans) == 1
    start, end, comment_text = spans[0]
    assert text[start:end] == "/* outer /* inner */ still outer */"


def test_comment_at_eof_no_newline():
    """A line comment at end-of-file with no trailing newline."""
    text = "let x = 1; // at end"
    spans = enumerate_comment_spans(text, "rust")
    assert len(spans) == 1
    assert "at end" in spans[0][2]


# ---------------------------------------------------------------------------
# Selective masking (B1) — mask_deferable_comments
# ---------------------------------------------------------------------------


def test_mask_deferable_comments_blanks_prose_only():
    """Only DEFERRED comments are blanked; MACHINE/LEGAL/GENERATED/DOCTEST
    survive verbatim. Length-preserving (offsets unchanged)."""
    from capybase.adapters.string_lexer import mask_deferable_comments
    text = (
        "// Copyright 2024 Acme.\n"           # LEGAL — survives
        "fn foo() {\n"
        "    // max retries\n"                 # DEFERRED — blanked
        "    let x = 1;\n"
        "}\n"
    )
    masked, deferred = mask_deferable_comments(text, "rust")
    assert len(masked) == len(text), "length must be preserved"
    # LEGAL comment survives.
    assert "Copyright 2024 Acme" in masked
    # DEFERRED comment is blanked (content → spaces).
    assert "max retries" not in masked
    # Code survives.
    assert "fn foo()" in masked
    assert "let x = 1;" in masked
    # Exactly 1 deferred span recorded.
    assert len(deferred) == 1
    assert "max retries" in deferred[0][2]  # the original text


def test_mask_deferable_comments_empty_when_no_prose():
    """When there are no DEFERRED comments, the masked text equals the original
    and the deferred list is empty."""
    from capybase.adapters.string_lexer import mask_deferable_comments
    text = "#[derive(Debug)]\nfn foo() { let x = 1; }\n"
    masked, deferred = mask_deferable_comments(text, "rust")
    assert masked == text
    assert deferred == []


def test_mask_deferable_comments_preserves_offsets():
    """The masked text's non-comment content must align byte-for-byte with the
    original (only comment chars are replaced, by spaces)."""
    from capybase.adapters.string_lexer import mask_deferable_comments
    text = "let x = 1; // a comment\n"
    masked, deferred = mask_deferable_comments(text, "rust")
    # The code part (before the comment) is unchanged.
    code_part = "let x = 1; "
    assert masked[:len(code_part)] == code_part
    # The comment part is all spaces (same length).
    comment_part = "// a comment"
    masked_comment = masked[len(code_part):len(code_part) + len(comment_part)]
    assert set(masked_comment) == {" "}


# ---------------------------------------------------------------------------
# K3 — Python docstring spans (triple-quoted strings as first statement)
# ---------------------------------------------------------------------------


def test_docstring_spans_finds_module_docstring():
    """A module-level docstring (first statement) is found."""
    from capybase.adapters.string_lexer import enumerate_docstring_spans
    text = '"""Module docstring."""\nx = 1\n'
    spans = enumerate_docstring_spans(text, "python")
    assert len(spans) == 1
    start, end, content = spans[0]
    assert "Module docstring" in content
    assert text[start:end] == content


def test_docstring_spans_finds_function_docstring():
    """A function's docstring (first statement in body) is found."""
    from capybase.adapters.string_lexer import enumerate_docstring_spans
    text = (
        "def foo():\n"
        '    """Does foo."""\n'
        "    return 1\n"
    )
    spans = enumerate_docstring_spans(text, "python")
    assert len(spans) == 1
    assert "Does foo" in spans[0][2]


def test_docstring_spans_finds_class_docstring():
    """A class's docstring is found."""
    from capybase.adapters.string_lexer import enumerate_docstring_spans
    text = (
        "class Foo:\n"
        '    """A class."""\n'
        "    pass\n"
    )
    spans = enumerate_docstring_spans(text, "python")
    assert len(spans) == 1
    assert "A class" in spans[0][2]


def test_docstring_spans_triple_single_quote():
    """Triple-single-quote docstrings are also found."""
    from capybase.adapters.string_lexer import enumerate_docstring_spans
    text = "'''Single-quote docstring.'''\nx = 1\n"
    spans = enumerate_docstring_spans(text, "python")
    assert len(spans) == 1
    assert "Single-quote docstring" in spans[0][2]


def test_docstring_spans_ignores_non_docstring_strings():
    """A string NOT in docstring position (not the first statement) is NOT found."""
    from capybase.adapters.string_lexer import enumerate_docstring_spans
    text = (
        "def foo():\n"
        "    x = 1\n"
        '    s = "not a docstring"\n'
        "    return s\n"
    )
    spans = enumerate_docstring_spans(text, "python")
    # No docstrings (the function's first statement is `x = 1`, not a string).
    assert spans == []


def test_docstring_spans_ignores_strings_in_expressions():
    """A bare string expression NOT as first statement is not a docstring."""
    from capybase.adapters.string_lexer import enumerate_docstring_spans
    text = (
        "def foo():\n"
        '    """real docstring"""\n'
        '    "not a docstring"\n'  # expression statement, not first
        "    return 1\n"
    )
    spans = enumerate_docstring_spans(text, "python")
    assert len(spans) == 1
    assert "real docstring" in spans[0][2]


def test_docstring_spans_multiline():
    """Multi-line docstrings are captured in full."""
    from capybase.adapters.string_lexer import enumerate_docstring_spans
    text = (
        "def foo():\n"
        '    """First line.\n'
        "    Second line.\n"
        '    """\n'
        "    return 1\n"
    )
    spans = enumerate_docstring_spans(text, "python")
    assert len(spans) == 1
    content = spans[0][2]
    assert "First line" in content
    assert "Second line" in content


def test_docstring_spans_returns_empty_for_non_python():
    """Non-Python languages yield no docstring spans (graceful degradation)."""
    from capybase.adapters.string_lexer import enumerate_docstring_spans
    text = 'fn foo() { 1 }\n'
    spans = enumerate_docstring_spans(text, "rust")
    assert spans == []


def test_docstring_spans_returns_empty_for_no_docstrings():
    """Code with no docstrings yields no spans."""
    from capybase.adapters.string_lexer import enumerate_docstring_spans
    text = "def foo():\n    return 1\n"
    spans = enumerate_docstring_spans(text, "python")
    assert spans == []

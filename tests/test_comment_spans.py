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

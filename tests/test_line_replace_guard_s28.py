"""S28-241.3 (queue item 7) — the line_replace attribution guards.

The 0127 regression: an era-mix member error whose message pointed at
an included file anchored the BUFFER's line 1, and the rung replaced
the file's first line with an unrelated function header — garbage the
compile gate then rejected, wasting the round. Three guards:

(a) the error's file:line:col must name THE CONFLICT FILE (stem
    match) — included-header/sibling-file errors skip the rung;
(b) the anchor is that error's own line;
(c) the replacement must share a token skeleton with the replaced
    line (the ratio floor alone admitted dissimilar lines).
"""

from __future__ import annotations

from capybase.verification import (
    _token_skeleton_ok,
    find_replacement_line,
)

# The 0127 shape, minimized: a 3-line buffer whose line 1 is an
# include; the error text names an INCLUDED header's line 1.
_BUFFER = (
    "#include <vector>\n"
    "int counter = 0;\n"
    "void f() {}\n"
)
_HEADER_ERROR = (
    "/tree/src/peg/autocomplete_core.cpp:209:31: error: 'class duckdb::"
    "shared_ptr<duckdb::CompiledGrammar>' has no member 'GetTokenizer';"
    " /tree/include/duckdb/format.h:1:1: note: expanded from here"
)


def test_guard_a_header_error_never_anchors_the_buffer():
    """(a): the only line-1 match names ANOTHER file — the rung declines
    instead of replacing the buffer's first line."""
    assert find_replacement_line(
        _BUFFER, _HEADER_ERROR, "cpp",
        "vector<AutoCompleteSuggestion> GenerateAutoCompleteSuggestions();\n",
        file_path="/tree/src/peg/autocomplete_core.cpp",
    ) is None


def test_guard_b_anchor_is_the_conflict_files_own_line():
    """(b): a conflict-file error anchors ITS line, not a first-match
    from another file's include chain."""
    buffer = "int a = 0;\nint counter = x y;\nint b = 1;\n"
    error = (
        "/tree/include/duckdb/format.h:36:5: note: here\n"
        "/tree/src/peg/thing.cpp:2:16: error: expected ';' before 'y'"
    )
    parent = "int counter = x + y;\n"
    got = find_replacement_line(
        buffer, error, "cpp", parent,
        file_path="/tree/src/peg/thing.cpp")
    assert got is not None
    idx, replacement = got
    assert idx == 1  # the error's own line (0-based)
    assert replacement == "int counter = x + y;"


def test_guard_c_token_skeleton_declines_dissimilar_pairs():
    """(c): the ratio floor alone passed a function header for a
    comment/include line; the skeleton floor declines it."""
    assert not _token_skeleton_ok(
        "#include <vector>",
        "vector<AutoCompleteSuggestion> GenerateAutoCompleteSuggestions();")


def test_token_skeleton_passes_true_counterparts():
    # a use-site swap shares the call symbol
    assert _token_skeleton_ok(
        "cache.GetTokenizer(behavior)",
        "grammar->GetTokenizer(behavior)")
    # a syntax fix shares nearly everything
    assert _token_skeleton_ok(
        "int counter = x y;",
        "int counter = x + y;")
    # empty-token lines never pass
    assert not _token_skeleton_ok("123 456", "789 012")


def test_legitimate_replacement_still_fires():
    """The guards must not kill the rung's real work: a conflict-file
    error whose counterpart line exists in a side still replaces."""
    buffer = "void f() {}\nint flags = SET_FLAGS(1);\nvoid g() {}\n"
    error = "/tree/src/util.cpp:2:17: error: type defaults to 'int'"
    parent = "static int flags = SET_FLAGS(1);\n"
    got = find_replacement_line(
        buffer, error, "c", parent,
        file_path="/tree/src/util.cpp")
    assert got == (1, "static int flags = SET_FLAGS(1);")

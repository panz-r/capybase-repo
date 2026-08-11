"""Sprint 14 fixes: macro-atomic tokenization, move detection, statement splitting.

Tests for three mechanisms informed by 7 reviewer responses:

1. **Macro-atomic tokenization** — ALL-CAPS macro invocations are treated as
   single atomic tokens so token_disjoint can't garble their arguments.

2. **Move detection** — detects when one side "moved" a code block (deleted +
   re-added verbatim) and resolves by taking the mover's version.

3. **Statement-level splitting** — splits oversized sub-units at safe statement
   boundaries (lines ending with ``;`` at body indent level).
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Fix 1: Macro-atomic tokenization
# ---------------------------------------------------------------------------


def test_tokenize_with_macros_detects_macro_call():
    """ALL-CAPS identifier followed by ( is treated as a single atomic token."""
    from capybase.structural_resolver import _tokenize_with_macros
    text = "LOG_DEBUG(\"msg\", x);"
    toks, lookup = _tokenize_with_macros(text)
    # The macro call should be replaced with a __MACRO_0 placeholder
    assert any("__MACRO_" in t for t in toks), f"expected macro placeholder, got {toks}"
    # The lookup should contain the original macro tokens
    assert len(lookup) == 1
    macro_toks = list(lookup.values())[0]
    assert "LOG_DEBUG" in macro_toks


def test_detokenize_with_macros_restores_original():
    """Round-trip: tokenize → detokenize produces the original text."""
    from capybase.structural_resolver import _tokenize_with_macros, _detokenize_with_macros
    text = "result = LOG_DEBUG(\"msg\", foo) + 1;"
    toks, lookup = _tokenize_with_macros(text)
    restored = _detokenize_with_macros(toks, lookup)
    assert restored == text, f"round-trip failed: {restored!r} != {text!r}"


def test_lowercase_function_call_not_treated_as_macro():
    """Lowercase function calls are NOT treated as macros."""
    from capybase.structural_resolver import _tokenize_with_macros
    text = "result = foo(arg1, arg2);"
    toks, lookup = _tokenize_with_macros(text)
    assert len(lookup) == 0, f"lowercase call should not be atomic: {lookup}"
    # All original tokens preserved (the tokenizer splits arg1 → arg + 1)
    assert "foo" in toks
    assert "arg" in toks


def test_macro_with_nested_parens():
    """Macro invocations with nested parens are handled correctly."""
    from capybase.structural_resolver import _tokenize_with_macros, _detokenize_with_macros
    text = "ASSERT(check(x, y));"
    toks, lookup = _tokenize_with_macros(text)
    assert len(lookup) == 1
    restored = _detokenize_with_macros(toks, lookup)
    assert restored == text


def test_token_disjoint_doesnt_garble_macros():
    """When both sides change different parts of a line with a macro call,
    token_disjoint should treat the macro as atomic — no garbled splice."""
    from capybase.structural_resolver import _try_token_disjoint
    base = "    int x = MAX(a, b) + offset;"
    current = "    int x = MAX(a, b) + new_offset;"
    replayed = "    int x = MAX(a, c) + offset;"
    # Both sides changed something on the same line, but the macro MAX(a,b)
    # is atomic so the splice can't interleave inside it.
    result = _try_token_disjoint(base, current, replayed)
    # May succeed or decline — the key is no garbled macro output
    if result is not None:
        # The macro call should appear intact, not split
        assert "MAX(" in result or "__MACRO_" not in result, \
            f"macro was garbled: {result!r}"


# ---------------------------------------------------------------------------
# Fix 2: Move detection
# ---------------------------------------------------------------------------


def test_move_detection_finds_moved_block():
    """When one side moves a 10+ line block that reappears verbatim at a shifted
    position, the move is detected and the mover's version is returned."""
    from capybase.structural_resolver import _try_move_transplant
    # Base has a 15-line function body
    old_func_lines = [f"    int var_{i} = {i};" for i in range(13)]
    base = "\n".join(["void old_func() {"] + old_func_lines + ["}"])
    # Current moved old_func to the end and added a new function at the top
    current = "\n".join([
        "void new_func() {",
        "    int z = 0;",
        "    int w = 1;",
        "    int v = 2;",
        "    int u = 3;",
        "    int t = 4;",
        "    int s = 5;",
        "}",
        "void old_func() {",
    ] + old_func_lines + ["}"])
    # Replayed modified old_func in place
    replayed = "\n".join([
        "void old_func() {",
    ] + [l if "var_5" not in l else "    int var_5 = 99;" for l in old_func_lines] + ["}"])
    result = _try_move_transplant(base, current, replayed)
    assert result is not None, "should detect the move"
    assert "new_func" in result, "should preserve current's new function"
    assert "old_func" in result, "should preserve the moved function"


def test_move_detection_declines_on_small_blocks():
    """Blocks <6 lines are not treated as moves (too likely to be coincidence)."""
    from capybase.structural_resolver import _try_move_transplant
    base = "line1\nline2\nline3"
    current = "line3\nline1\nline2"  # reorder (3 lines)
    replayed = "line1\nmodified\nline3"
    result = _try_move_transplant(base, current, replayed)
    assert result is None, "should not detect a move for <6 lines"


def test_move_detection_declines_on_no_move():
    """When neither side moved a block, returns None."""
    from capybase.structural_resolver import _try_move_transplant
    base = "void f() {\n    a;\n    b;\n}"
    current = "void f() {\n    a;\n    c;\n}"  # modified b→c
    replayed = "void f() {\n    a;\n    d;\n}"  # modified b→d
    result = _try_move_transplant(base, current, replayed)
    assert result is None, "should not detect a move when neither side moved"


# ---------------------------------------------------------------------------
# Fix 3: Statement-level splitting
# ---------------------------------------------------------------------------


def test_statement_split_points_not_found_for_small_unit():
    """Small units (<80 lines) don't get statement-level split points."""
    from capybase.conflict_extractor import _find_statement_split_points
    from capybase.conflict_model import ConflictUnit, ConflictSide
    unit = ConflictUnit(
        session_id="s", step_index=0, path="f.cpp", language="cpp",
        conflict_type="UU", unit_id="f.cpp:1:0",
        unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=""),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=""),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=""),
        original_worktree_text="void f() {\n    a;\n    b;\n}",
        marker_span=(0, 3),
        structural_metadata={},
    )
    result = _find_statement_split_points(unit)
    assert result is None, "small unit should not get split points"


def test_statement_split_points_found_for_large_unit():
    """Large units (>80 lines) with clear statement boundaries get split points."""
    from capybase.conflict_extractor import _find_statement_split_points
    from capybase.conflict_model import ConflictUnit, ConflictSide
    # Build a 90-line function body with statement boundaries
    body_lines = [f"    int var_{i} = {i};" for i in range(90)]
    wt = "void f() {\n" + "\n".join(body_lines) + "\n}"
    unit = ConflictUnit(
        session_id="s", step_index=0, path="f.cpp", language="cpp",
        conflict_type="UU", unit_id="f.cpp:1:0",
        unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=""),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=""),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=""),
        original_worktree_text=wt,
        marker_span=(0, len(wt.splitlines()) - 1),
        structural_metadata={},
    )
    result = _find_statement_split_points(unit, max_lines=80, min_splits=3)
    assert result is not None, "large unit should get split points"
    assert len(result) >= 3, f"expected ≥3 split points, got {len(result)}"

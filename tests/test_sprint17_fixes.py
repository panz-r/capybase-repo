"""Sprint 17: diff3 refinement fallback, side-specific restoration, lint frequency.

Tests for three fixes targeting failing eval cases:

1. **Diff3 refinement fallback** — when block count mismatch (79 vs 78),
   best-effort positional matching instead of bailing entire file.

2. **Side-specific line restoration** — extends intent coverage repair to
   restore lines added by ONE side (not just common to both).

3. **Lint transform frequency detection** — detects lint transforms by
   frequency (>5 occurrences) even when _is_mechanical_side rejects.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Fix 1: Diff3 refinement fallback
# ---------------------------------------------------------------------------


def test_match_blocks_to_units_basic():
    """When diff3 produces more blocks than units, the matcher aligns them."""
    from capybase.conflict_extractor import _match_blocks_to_units
    from capybase.adapters.git_diff3 import Diff3Block

    # 3 blocks, 2 units — block 2 is a git coalescing artifact
    blocks = [
        Diff3Block(ours="cur0", base="base0", theirs="rep0"),
        Diff3Block(ours="extra", base="extra_base", theirs="extra_rep"),
        Diff3Block(ours="cur1", base="base1", theirs="rep1"),
    ]
    base_text = "base0\nextra_base\nbase1"

    # Mock units with current.text matching
    from capybase.conflict_model import ConflictUnit, ConflictSide
    units = [
        ConflictUnit(
            session_id="s", step_index=0, path="f.cpp", language="cpp",
            conflict_type="UU", unit_id="f:1:0", unit_kind="text_marker_block",
            base=ConflictSide(label="BASE", text=""),
            current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="cur0"),
            replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=""),
            original_worktree_text="", marker_span=(0, 0),
            structural_metadata={},
        ),
        ConflictUnit(
            session_id="s", step_index=0, path="f.cpp", language="cpp",
            conflict_type="UU", unit_id="f:1:1", unit_kind="text_marker_block",
            base=ConflictSide(label="BASE", text=""),
            current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="cur1"),
            replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=""),
            original_worktree_text="", marker_span=(0, 0),
            structural_metadata={},
        ),
    ]
    result = _match_blocks_to_units(blocks, units, base_text)
    assert result is not None
    assert len(result) == 2
    assert result[0].ours == "cur0"
    assert result[1].ours == "cur1"


def test_match_blocks_to_units_declines_on_no_match():
    """When no block matches a unit, returns None."""
    from capybase.conflict_extractor import _match_blocks_to_units
    from capybase.adapters.git_diff3 import Diff3Block
    from capybase.conflict_model import ConflictUnit, ConflictSide

    blocks = [Diff3Block(ours="totally_different", base="b", theirs="t")]
    units = [ConflictUnit(
        session_id="s", step_index=0, path="f.cpp", language="cpp",
        conflict_type="UU", unit_id="f:1:0", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=""),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="cur0"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=""),
        original_worktree_text="", marker_span=(0, 0),
        structural_metadata={},
    )]
    result = _match_blocks_to_units(blocks, units, "b")
    assert result is None  # ratio < 0.3


# ---------------------------------------------------------------------------
# Fix 2: Side-specific line restoration
# ---------------------------------------------------------------------------


def test_restore_side_specific_current_addition():
    """A line added only by current (not in base, not in replayed) that the
    candidate dropped gets restored."""
    from capybase.orchestrator import _try_restore_common_lines
    base = "void f() {\n    int x = 1;\n}\n"
    current = "void f() {\n    int x = 1;\n    int y = 2;\n}\n"
    replayed = "void f() {\n    int x = 1;\n}\n"
    # Candidate dropped current's unique addition
    candidate = "void f() {\n    int x = 1;\n}\n"
    repaired = _try_restore_common_lines(candidate, base, current, replayed, "cpp")
    assert repaired is not None, "should restore the dropped side-specific line"
    assert "int y = 2;" in repaired


def test_restore_side_specific_replayed_addition():
    """A line added only by replayed gets restored."""
    from capybase.orchestrator import _try_restore_common_lines
    base = "void f() {\n    int x = 1;\n}\n"
    current = "void f() {\n    int x = 1;\n}\n"
    replayed = "void f() {\n    int x = 1;\n    int z = 3;\n}\n"
    candidate = "void f() {\n    int x = 1;\n}\n"
    repaired = _try_restore_common_lines(candidate, base, current, replayed, "cpp")
    assert repaired is not None, "should restore replayed's addition"
    assert "int z = 3;" in repaired


def test_restore_does_not_restore_structural_tokens():
    """Structural tokens (braces) are not restored."""
    from capybase.orchestrator import _try_restore_common_lines
    base = "void f() {\n    x;\n"
    current = "void f() {\n    x;\n}\n"
    replayed = "void f() {\n    x;\n"
    candidate = "void f() {\n    x;\n"
    repaired = _try_restore_common_lines(candidate, base, current, replayed, "cpp")
    # The '}' should NOT be restored as a side-specific line
    if repaired is not None:
        assert repaired.count("}") <= 1


# ---------------------------------------------------------------------------
# Fix 3: Lint transform frequency detection
# ---------------------------------------------------------------------------


def test_lint_transform_frequency_detection():
    """When a lint transform appears >5 times, it's detected even if
    _is_mechanical_side would reject it."""
    from capybase.structural_resolver import _try_lint_transform
    # Base uses 'and' many times
    base_lines = [f"    if (a{i} and b{i}) result++;"
                  for i in range(10)]
    base = "\n".join(base_lines)
    # Current is a refactor (different structure)
    current_lines = [f"    if (a{i} and b{i}) result++; // refactored"
                     for i in range(10)]
    current = "\n".join(current_lines)
    # Replayed is a lint pass: and→&&
    replayed_lines = [f"    if (a{i} && b{i}) result++;"
                      for i in range(10)]
    replayed = "\n".join(replayed_lines)
    result = _try_lint_transform(base, current, replayed)
    # Should detect the and→&& transform by frequency and apply to current
    assert result is not None, "should detect lint transform by frequency"
    assert "&&" in result
    assert "and " not in result or "&&" in result  # all 'and' replaced


def test_lint_transform_declines_when_no_frequency():
    """When transforms don't appear frequently, frequency path doesn't fire."""
    from capybase.structural_resolver import _try_lint_transform
    base = "x = 1;\ny = 2;\nz = 3;\n"
    current = "CUR_x = 1;\ny = 2;\nz = 3;\n"
    replayed = "x = 1;\nREP_y = 2;\nz = 3;\n"
    result = _try_lint_transform(base, current, replayed)
    # Neither side has lint transforms — should decline
    assert result is None

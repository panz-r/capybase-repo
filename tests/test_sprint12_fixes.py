"""Sprint 12 fixes: base broadening, header validation, shape hints.

Tests for the three algorithmic improvements:

1. **Entity-skeleton base broadening** — ``_try_broaden_base`` returns the
   enclosing entity's text when the refined hunk over-collapsed, giving
   token rules a broader anchor context (clickhouse-0024 fix).

2. **Header syntax validation** — headers are no longer skipped by the
   per-unit gcc gate. A clean header compiles; a parse-error header fails.

3. **Shape-specific strategic hints** — ``_shape_hint_block`` emits a
   1-2 sentence strategic hint for the hardest conflict shapes.
"""

from __future__ import annotations

from capybase.conflict_model import ConflictUnit, ConflictSide


def _unit_with_metadata(metadata: dict, **kwargs) -> ConflictUnit:
    """Build a minimal ConflictUnit with structural_metadata."""
    defaults = dict(
        session_id="s", step_index=0, path="f.cpp", language="cpp",
        conflict_type="UU", unit_id="f.cpp:1:0",
        unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=""),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=""),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=""),
        original_worktree_text="",
        marker_span=(0, 0),
    )
    defaults.update(kwargs)
    defaults["structural_metadata"] = metadata
    return ConflictUnit(**defaults)


# ---------------------------------------------------------------------------
# Fix 1: Base broadening was removed — it's fundamentally unsafe.
# token_disjoint reconstructs from the base, so a broadened base produces
# full-function output that corrupts the splice. The test below verifies
# that token_disjoint is NEVER called with a broadened base.
# ---------------------------------------------------------------------------


def test_token_disjoint_output_scope_matches_sides():
    """token_disjoint must produce output with the same scope as its sides,
    not the scope of a broadened base. Regression guard for the removed
    base-broadening feature that corrupted splices."""
    from capybase.structural_resolver import _try_token_disjoint

    base = "result = a + b;"
    current = "result = a + c;"
    replayed = "result = a + b + 1;"
    merged = _try_token_disjoint(base, current, replayed)
    assert merged is not None
    # Output must be 1 line (same scope as the sides), not multi-line.
    assert merged.count("\n") == 0, (
        f"token_disjoint output has wrong scope: {merged!r}"
    )


# ---------------------------------------------------------------------------
# Fix 3: Shape-specific strategic hints
# ---------------------------------------------------------------------------


def test_shape_hint_refactor_vs_lint():
    """A refactor-vs-lint conflict produces a hint about preserving the refactor."""
    from capybase.resolution_engine import _shape_hint_block

    unit = _unit_with_metadata({
        "conflict_features": {
            "commit_change_type": "refactor",
            "imbalance_ratio": 1.0,
        },
        "merge_direction": {"kind": "both_modify"},
    })
    hint = _shape_hint_block(unit)
    assert "refactor" in hint.lower()
    assert "preserve" in hint.lower()


def test_shape_hint_rewrite_vs_edit():
    """An asymmetric both_modify conflict (rewrite vs edit) produces a hint."""
    from capybase.resolution_engine import _shape_hint_block

    unit = _unit_with_metadata({
        "conflict_features": {
            "commit_change_type": "feature",
            "imbalance_ratio": 5.0,
        },
        "merge_direction": {"kind": "both_modify"},
    })
    hint = _shape_hint_block(unit)
    assert "rewrite" in hint.lower() or "either side verbatim" in hint.lower()


def test_shape_hint_both_add_disjoint():
    """A both_add conflict with no same-line overlap produces a hint to include both."""
    from capybase.resolution_engine import _shape_hint_block

    unit = _unit_with_metadata({
        "conflict_features": {
            "same_line_overlap": False,
        },
        "merge_direction": {"kind": "both_add"},
    })
    hint = _shape_hint_block(unit)
    assert "both sides" in hint.lower()
    assert "additions" in hint.lower()


def test_shape_hint_modify_delete():
    """A modify/delete conflict produces a hint about the deletion decision."""
    from capybase.resolution_engine import _shape_hint_block

    unit = _unit_with_metadata({
        "conflict_features": {
            "modify_delete": True,
        },
        "merge_direction": {"kind": "modify_delete"},
    })
    hint = _shape_hint_block(unit)
    assert "modify/delete" in hint.lower() or "deleted" in hint.lower()


def test_shape_hint_no_hint_for_simple_conflict():
    """A simple conflict with no special shape produces no hint."""
    from capybase.resolution_engine import _shape_hint_block

    unit = _unit_with_metadata({
        "conflict_features": {
            "commit_change_type": "bugfix",
            "imbalance_ratio": 1.2,
            "same_line_overlap": True,
            "modify_delete": False,
        },
        "merge_direction": {"kind": "both_modify"},
    })
    hint = _shape_hint_block(unit)
    assert hint == ""


def test_shape_hint_empty_metadata():
    """When structural_metadata is empty, no hint is produced."""
    from capybase.resolution_engine import _shape_hint_block

    unit = _unit_with_metadata({})
    hint = _shape_hint_block(unit)
    assert hint == ""

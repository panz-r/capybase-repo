"""Sprint 13 fixes: compile_commands verification, shape router, token equiv.

Tests for three mechanisms informed by 5 reviewer responses:

1. **compile_commands.json verification** — `_try_compile_commands` extracts
   the exact g++ command for a file and runs -fsyntax-only.

2. **Conflict-shape router** — `_classify_conflict_shape` classifies the
   conflict before the cascade and gates unsafe rules.

3. **Token equivalence classes** — `_norm_tok` maps NULL→nullptr, and→&&,
   etc. so the resolver recognizes them as identical.
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
# Fix 2: Conflict-shape router
# ---------------------------------------------------------------------------


def test_shape_router_rewrite_vs_edit():
    """High imbalance + both_modify → rewrite_vs_edit."""
    from capybase.structural_resolver import _classify_conflict_shape
    unit = _unit_with_metadata({
        "conflict_features": {"imbalance_ratio": 5.0, "same_line_overlap": True},
        "merge_direction": {"kind": "both_modify"},
    })
    assert _classify_conflict_shape(unit) == "rewrite_vs_edit"


def test_shape_router_pure_insertion():
    """Both_add without same-line overlap → pure_insertion."""
    from capybase.structural_resolver import _classify_conflict_shape
    unit = _unit_with_metadata({
        "conflict_features": {"same_line_overlap": False},
        "merge_direction": {"kind": "both_add"},
    })
    assert _classify_conflict_shape(unit) == "pure_insertion"


def test_shape_router_stable_token_edit():
    """Same-line overlap with low imbalance → stable_token_edit."""
    from capybase.structural_resolver import _classify_conflict_shape
    unit = _unit_with_metadata({
        "conflict_features": {"imbalance_ratio": 1.5, "same_line_overlap": True},
        "merge_direction": {"kind": "both_modify"},
    })
    assert _classify_conflict_shape(unit) == "stable_token_edit"


def test_shape_router_general():
    """Balanced both_modify without same-line overlap → general."""
    from capybase.structural_resolver import _classify_conflict_shape
    unit = _unit_with_metadata({
        "conflict_features": {"imbalance_ratio": 1.5, "same_line_overlap": False},
        "merge_direction": {"kind": "both_modify"},
    })
    assert _classify_conflict_shape(unit) == "general"


def test_shape_router_empty_metadata():
    """When structural_metadata is empty, defaults to general."""
    from capybase.structural_resolver import _classify_conflict_shape
    unit = _unit_with_metadata({})
    assert _classify_conflict_shape(unit) == "general"


def test_rewrite_vs_edit_skips_token_disjoint():
    """token_disjoint must NOT fire on rewrite_vs_edit shapes (garbled splice).
    The shape router gates it out."""
    from capybase.structural_resolver import resolve_structurally
    # rewrite_vs_edit: one side has 10 lines, other has 1 line
    unit = _unit_with_metadata({
        "conflict_features": {"imbalance_ratio": 10.0, "same_line_overlap": True},
        "merge_direction": {"kind": "both_modify"},
    },
        base=ConflictSide(label="BASE", text="x = a + b;"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE",
            text="\n".join(f"line_{i};" for i in range(10))),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="x = a + c;"),
    )
    result = resolve_structurally(unit)
    # Should NOT resolve via token_disjoint (shape router skips it)
    assert result.rule != "token_disjoint", (
        "token_disjoint must not fire on rewrite_vs_edit shape"
    )


# ---------------------------------------------------------------------------
# Fix 3: Token equivalence classes
# ---------------------------------------------------------------------------


def test_token_equiv_null_nullptr():
    """NULL and nullptr are treated as equivalent tokens."""
    from capybase.structural_resolver import _norm_tok
    assert _norm_tok("NULL") == _norm_tok("nullptr")
    assert _norm_tok("NULL") == "nullptr"


def test_token_equiv_and_operator():
    """C++ 'and' keyword and '&&' operator are equivalent."""
    from capybase.structural_resolver import _norm_tok
    assert _norm_tok("and") == _norm_tok("&&")
    assert _norm_tok("or") == _norm_tok("||")
    assert _norm_tok("not") == _norm_tok("!")


def test_token_equiv_boolean_macros():
    """TRUE/FALSE macros map to true/false."""
    from capybase.structural_resolver import _norm_tok
    assert _norm_tok("TRUE") == "true"
    assert _norm_tok("FALSE") == "false"


def test_token_equiv_unaffected_tokens():
    """Unmapped tokens pass through unchanged."""
    from capybase.structural_resolver import _norm_tok
    assert _norm_tok("foo") == "foo"
    assert _norm_tok("123") == "123"
    assert _norm_tok("int") == "int"


def test_token_change_ops_recognizes_equiv_as_equal():
    """When one side changes NULL→nullptr, _token_change_ops sees NO change."""
    from capybase.structural_resolver import _token_change_ops, _tokenize
    base_toks = _tokenize("return NULL;")
    side_toks = _tokenize("return nullptr;")
    ops = _token_change_ops(base_toks, side_toks)
    # NULL→nullptr is an equivalence → no change detected
    assert len(ops) == 0, f"NULL→nullptr should be equivalent, got ops: {ops}"


def test_token_change_ops_detects_real_change():
    """A genuine change (foo→bar) is still detected."""
    from capybase.structural_resolver import _token_change_ops, _tokenize
    base_toks = _tokenize("return foo;")
    side_toks = _tokenize("return bar;")
    ops = _token_change_ops(base_toks, side_toks)
    assert len(ops) > 0, "foo→bar should be a detected change"


# ---------------------------------------------------------------------------
# Fix 1: compile_commands.json (tested indirectly — requires a real repo)
# ---------------------------------------------------------------------------


def test_load_compile_commands_returns_none_for_missing():
    """When no compile_commands.json exists, _load_compile_commands returns None."""
    from capybase.verification import _load_compile_commands, _COMPILE_COMMANDS_CACHE
    # Clear cache to ensure fresh check
    _COMPILE_COMMANDS_CACHE.pop("/nonexistent", None)
    result = _load_compile_commands("/nonexistent")
    assert result is None


def test_try_compile_commands_returns_none_without_json():
    """_try_compile_commands returns None when no compile_commands.json."""
    from capybase.verification import _try_compile_commands, _COMPILE_COMMANDS_CACHE
    _COMPILE_COMMANDS_CACHE.clear()
    result = _try_compile_commands("/nonexistent", "src/foo.cpp", "int main() {}", "cpp")
    assert result is None

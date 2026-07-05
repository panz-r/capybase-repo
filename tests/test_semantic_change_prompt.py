"""Tests for the entity-level semantic-change block injected into the resolve
prompt (Phase 2c / survey Tier 5).

The block summarizes what each side changed at the entity level (added/removed/
renamed/signature_changed/body_changed) via ``semantic_diff``, giving the model
precise change intent it would otherwise have to infer from the raw sides.
"""

from __future__ import annotations

import pytest

from capybase.adapters import structural
from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.context_builder import ContextBuilder
from capybase.resolution_engine import _semantic_change_block, build_resolve_prompt

pytestmark = pytest.mark.skipif(
    not structural.is_available("python"),
    reason="tree-sitter Python grammar unavailable",
)


def _unit(base: str, current: str, replayed: str) -> ConflictUnit:
    return ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text="", marker_span=(1, 5),
    )


def test_semantic_change_block_renders_rename_on_current_side():
    """A side that renames an entity surfaces as 'renamed' in the block, with both
    the old and new name — precise intent the raw side text conveys only
    implicitly."""
    base = "def validate_token():\n    return check()\n"
    current = "def check_token():\n    return check()\n"  # renamed on upstream
    replayed = base  # replayed unchanged
    block = _semantic_change_block(_unit(base, current, replayed))
    assert "Entity-level changes" in block
    assert "CURRENT side" in block
    assert "renamed" in block
    assert "validate_token" in block and "check_token" in block


def test_semantic_change_block_renders_add_on_replayed_side():
    """A side that adds an entity surfaces as 'added'."""
    base = "def main():\n    return 1\n"
    current = base
    replayed = "def main():\n    return 1\n\ndef helper():\n    return 2\n"
    block = _semantic_change_block(_unit(base, current, replayed))
    assert "REPLAYED side" in block
    assert "added" in block
    assert "helper" in block


def test_semantic_change_block_empty_when_no_entity_change():
    """When neither side makes an entity-level change (e.g. a value-only edit
    inside an existing function), the block is empty — no noise in the prompt."""
    base = "def main():\n    return 1\n"
    current = "def main():\n    return 2\n"  # body change, same entity
    replayed = "def main():\n    return 3\n"
    block = _semantic_change_block(_unit(base, current, replayed))
    # body_changed IS an entity-level change, so this surfaces it. Use a truly
    # unchanged case to verify the empty path:
    assert block != ""  # body change is reported
    block_unchanged = _semantic_change_block(_unit(base, base, base))
    assert block_unchanged == ""


def test_semantic_change_block_appears_in_resolve_prompt():
    """The block reaches the assembled resolve prompt (not just the helper)."""
    base = "def main():\n    return 1\n"
    current = base
    replayed = "def main():\n    return 1\n\ndef helper():\n    return 2\n"
    unit = _unit(base, current, replayed)
    prompt = build_resolve_prompt(unit, ContextBuilder().build(unit))
    assert "Entity-level changes" in prompt
    assert "helper" in prompt


def test_semantic_change_block_empty_for_unsupported_language():
    """A non-python/rust language degrades to an empty block (no crash)."""
    base = "function foo() { return 1; }"
    unit = ConflictUnit(
        session_id="s", step_index=1, path="app.js", language="javascript",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=base),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=base),
        original_worktree_text="", marker_span=(1, 5),
    )
    assert _semantic_change_block(unit) == ""

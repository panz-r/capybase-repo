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


def test_semantic_change_block_surfaces_role_when_no_entity_change():
    """When neither side makes an entity-level change, the block still surfaces
    the commit ROLE (a value-only edit is config_update) — the role is
    informative even without entity changes. Only a truly-unclassifiable case
    (role=unknown) with no changes is empty."""
    base = "def main():\n    return 1\n"
    current = "def main():\n    return 2\n"  # body change, same entity
    replayed = "def main():\n    return 3\n"
    block = _semantic_change_block(_unit(base, current, replayed))
    # body_changed IS an entity-level change, so this surfaces it.
    assert block != ""  # body change is reported
    # Identical input → no entity changes, but the role is config_update (value-
    # only / no-code-change classification), so the block surfaces the role.
    block_identical = _semantic_change_block(_unit(base, base, base))
    assert "config_update" in block_identical


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


def test_semantic_change_block_surfaces_commit_role_for_bugfix():
    """A bugfix commit surfaces its role + correctness guidance even when there
    ARE entity changes (so the model knows 'preserve behavior'). The role line
    names the role and its guidance."""
    base = "def compute():\n    return a + b\n"
    current = base
    replayed = "def compute():\n    return a - b\n"  # body change → bugfix
    block = _semantic_change_block(_unit(base, current, replayed))
    assert "REPLAYED commit role: bugfix" in block
    assert "preserve" in block.lower() or "correctness" in block.lower()


def test_semantic_change_block_surfaces_role_even_without_entity_changes():
    """A config/value-only change has NO entity-level changes but a clear role
    (config_update). The block surfaces the role rather than being empty — the
    role is informative even when the entity diff is."""
    base = "PORT = 8080\n"
    current = base
    replayed = "PORT = 9090\n"  # value change, no entity change → config_update
    # Need a path that's not a config extension to exercise the value-only branch
    # (a .toml path would short-circuit at the extension check, which is fine too,
    # but here we test the no-entity-change path).
    from capybase.conflict_model import ConflictSide, ConflictUnit
    unit = ConflictUnit(
        session_id="s", step_index=1, path="settings.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text="", marker_span=(1, 5),
    )
    block = _semantic_change_block(unit)
    assert "config_update" in block
    assert "REPLAYED commit role" in block


def test_semantic_change_block_omits_unknown_role():
    """An 'unknown' role (couldn't classify) is omitted — no noise. Only
    informative roles surface the guidance line."""
    # Malformed input that degrades to unknown: a non-config, non-test path that
    # can't be parsed. The block should be empty (no changes, role unknown).
    from capybase.conflict_model import ConflictSide, ConflictUnit
    unit = ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="# same\n"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="# same\n"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="# same\n"),
        original_worktree_text="", marker_span=(1, 5),
    )
    block = _semantic_change_block(unit)
    assert "REPLAYED commit role: unknown" not in block

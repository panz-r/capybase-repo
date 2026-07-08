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
from capybase.context_builder import ContextBuilder, ContextBundle
from capybase.resolution_engine import (
    _semantic_change_block,
    _value_resolution_block,
    build_resolve_prompt,
)

pytestmark = pytest.mark.skipif(
    not structural.is_available("python"),
    reason="abstract parser unavailable for python",
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
    """A language the abstract parser has no family for (Family C / declarative,
    e.g. SQL) degrades to an empty block (no crash). Note: javascript IS
    supported by the Family-A parser and now produces a real block — see
    test_semantic_change_block_works_for_javascript."""
    base = "SELECT * FROM users;"
    unit = ConflictUnit(
        session_id="s", step_index=1, path="query.sql", language="sql",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=base),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=base),
        original_worktree_text="", marker_span=(1, 5),
    )
    assert _semantic_change_block(unit) == ""


def test_semantic_change_block_works_for_javascript():
    """A Family-A language (javascript) — supported by the abstract parser —
    now produces a real entity-change block, not empty. Regression guard for the
    old ``python/rust``-only gate that left 12 of 14 languages without the
    annotation."""
    base = "function foo() {\n    return 1;\n}\n"
    current = base
    replayed = "function foo() {\n    return 2;\n}\nfunction bar() {\n    return 3;\n}\n"
    unit = ConflictUnit(
        session_id="s", step_index=1, path="app.js", language="javascript",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text="", marker_span=(1, 5),
    )
    block = _semantic_change_block(unit)
    assert "Entity-level changes" in block
    assert "bar" in block  # the added function surfaces


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


# ---------------------------------------------------------------------------
# Value-resolution guidance block (pick a side OR write a combining expression)
# ---------------------------------------------------------------------------


def _unit_with_vr(vr: str) -> ConflictUnit:
    u = _unit("def greet():\n    return 'hello'\n", "    return 'hi'", "    return 'howdy'")
    u.structural_metadata["conflict_features"] = {"value_resolution": vr}
    return u


def test_value_resolution_block_surfaces_for_return_conflict():
    """A return value-resolution conflict surfaces guidance telling the model it
    may pick one side OR write a combining expression — so a reasoning model
    doesn't self-report needs_human on a resolvable value conflict."""
    block = _value_resolution_block(_unit_with_vr("return"))
    assert "VALUE-RESOLUTION" in block
    assert "pick one side" in block.lower()
    assert "new expression" in block.lower()
    assert "needs_human" in block  # the "don't report needs_human" steer


def test_value_resolution_block_names_target_for_assignment():
    """An assignment value resolution names the target so the model knows what
    base operation must be preserved."""
    block = _value_resolution_block(_unit_with_vr("assignment:a"))
    assert "assignment" in block
    assert "target `a`" in block


def test_value_resolution_block_empty_when_not_value_resolution():
    """When the conflict is not a value resolution (genuine distinct additions),
    no guidance is surfaced — the prompt is unchanged."""
    assert _value_resolution_block(_unit_with_vr("")) == ""
    # No conflict_features dict at all → also empty (never breaks the prompt).
    bare = _unit("x", "y", "z")
    assert _value_resolution_block(bare) == ""


def test_value_resolution_block_appears_in_resolve_prompt():
    """The guidance reaches the full resolve prompt for a value-resolution unit."""
    u = _unit_with_vr("return")
    prompt = build_resolve_prompt(u, ContextBundle(primary_text=""))
    assert "VALUE-RESOLUTION" in prompt


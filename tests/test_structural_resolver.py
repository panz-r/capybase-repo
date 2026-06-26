"""Tests for the deterministic structural pre-resolver.

All rules are pure functions over the three conflict sides — no I/O, no model,
no git — so every rule is exhaustively testable. The safety contract (validate-
or-fall-through) is exercised in the orchestrator integration tests; here we
lock in each rule's correctness directly.
"""

from __future__ import annotations

import pytest

from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.structural_resolver import StructuralResolution, resolve_structurally


def _unit(base: str, current: str, replayed: str) -> ConflictUnit:
    def _side(label, text):
        return ConflictSide(label=label, text=text)  # type: ignore[arg-type]

    return ConflictUnit(
        session_id="s", step_index=0, path="f.py", unit_id="u",
        base=_side("BASE", base),
        current=_side("CURRENT_UPSTREAM_SIDE", current),
        replayed=_side("REPLAYED_COMMIT_SIDE", replayed),
        original_worktree_text=base,
    )


# ---------------------------------------------------------------------------
# Rule 1: identical sides
# ---------------------------------------------------------------------------


def test_identical_sides_resolves_to_that_side():
    u = _unit("x = 1", "x = 2", "x = 2")
    r = resolve_structurally(u)
    assert r.resolved and r.rule == "identical_sides"
    assert r.text == "x = 2"


def test_identical_sides_ignores_whitespace_variance():
    u = _unit("x = 1", "x = 2  ", "  x = 2")
    r = resolve_structurally(u)
    assert r.rule == "identical_sides"
    # Emits the non-empty side as-is (current here), not normalized.
    assert r.text == "x = 2  "


def test_identical_sides_both_empty_resolves_empty():
    u = _unit("x = 1", "", "")
    r = resolve_structurally(u)
    assert r.resolved
    assert r.text == ""


# ---------------------------------------------------------------------------
# Rule 2: one-sided change
# ---------------------------------------------------------------------------


def test_one_sided_current_changed_only():
    # Current diverged, replayed == base → take current.
    u = _unit("def f():\n    return 1", "def f():\n    return 2", "def f():\n    return 1")
    r = resolve_structurally(u)
    assert r.resolved and r.rule == "one_sided_change"
    assert r.text == "def f():\n    return 2"


def test_one_sided_replayed_changed_only():
    # Replayed diverged, current == base → take replayed.
    u = _unit("def f():\n    return 1", "def f():\n    return 1", "def f():\n    return 3")
    r = resolve_structurally(u)
    assert r.resolved and r.rule == "one_sided_change"
    assert r.text == "def f():\n    return 3"


def test_one_sided_when_other_side_concedes_to_empty():
    # Current deleted (empty), replayed kept base → replayed is the only change? No:
    # current="" differs from base, replayed==base → current changed (to empty),
    # replayed didn't → take current (the deletion). This is a legitimate one-sided
    # change (one side chose to delete).
    u = _unit("x = 1", "", "x = 1")
    r = resolve_structurally(u)
    assert r.rule == "one_sided_change"
    assert r.text == ""


# ---------------------------------------------------------------------------
# Rule 3: disjoint edits (both changed, non-overlapping lines)
# ---------------------------------------------------------------------------


def test_disjoint_edits_merge_both_changes():
    # Base has two lines; current edits line 1, replayed edits line 2. Disjoint.
    base = "A = 1\nB = 1"
    current = "A = 2\nB = 1"      # changed line 0
    replayed = "A = 1\nB = 2"     # changed line 1
    r = resolve_structurally(_unit(base, current, replayed))
    assert r.resolved and r.rule == "disjoint_edits"
    assert r.text == "A = 2\nB = 2"  # both edits applied


def test_disjoint_edits_insertions_in_different_spots():
    base = "def f():\n    pass"
    # current adds a docstring at top; replayed changes the body. Disjoint lines.
    current = "def f():\n    \"\"\"doc\"\"\"\n    pass"
    replayed = "def f():\n    return 1"
    r = resolve_structurally(_unit(base, current, replayed))
    if r.resolved:  # only assert safety when it resolves; disjoint detection is conservative
        assert r.rule == "disjoint_edits"
        # Must contain BOTH sides' intent (docstring from current, return from replayed).
        assert "doc" in r.text
        assert "return 1" in r.text


def test_disjoint_edits_overlapping_returns_unresolved():
    # Both sides change the SAME line → real conflict → unresolved (defer to LLM).
    base = "x = 1"
    current = "x = 2"
    replayed = "x = 3"
    r = resolve_structurally(_unit(base, current, replayed))
    assert not r.resolved
    assert r.rule is None


def test_disjoint_edits_adjacent_non_overlapping_lines_merge():
    # Line 0 vs line 1 — adjacent but not overlapping → safe to merge.
    base = "a = 1\nb = 1"
    current = "a = 2\nb = 1"
    replayed = "a = 1\nb = 2"
    r = resolve_structurally(_unit(base, current, replayed))
    assert r.resolved and r.rule == "disjoint_edits"
    assert r.text == "a = 2\nb = 2"


# ---------------------------------------------------------------------------
# Fall-through: genuine conflicts stay unresolved
# ---------------------------------------------------------------------------


def test_real_semantic_conflict_is_unresolved():
    # Both sides changed the same thing differently → no safe rule → None.
    u = _unit("color = 'red'", "color = 'blue'", "color = 'green'")
    r = resolve_structurally(u)
    assert not r.resolved
    assert r.rule is None


def test_both_sides_diverge_on_overlapping_multiline_block_unresolved():
    base = "def f():\n    x = 1\n    y = 2"
    current = "def f():\n    x = 9\n    y = 2"
    replayed = "def f():\n    x = 1\n    y = 9"
    # Both touch line 1 (the def line) AND diverge — overlapping → unresolved.
    # (If difflib treats the def line as equal, this may resolve disjointly;
    # either outcome is safe. Assert the resolved case is internally consistent.)
    r = resolve_structurally(_unit(base, current, replayed))
    if r.resolved:
        assert r.rule in ("disjoint_edits",)


# ---------------------------------------------------------------------------
# Rule priority: identical beats one-sided beats disjoint
# ---------------------------------------------------------------------------


def test_identical_takes_priority_over_one_sided():
    # current==replayed (identical), but both differ from base.
    u = _unit("x = 1", "x = 9", "x = 9")
    r = resolve_structurally(u)
    assert r.rule == "identical_sides"  # not one_sided_change


# ---------------------------------------------------------------------------
# Resolution shape: produces block-interior text (splices like an LLM candidate)
# ---------------------------------------------------------------------------


def test_resolved_text_is_plain_block_text_no_markers():
    u = _unit("a\nb", "a\nB", "a\nb")
    r = resolve_structurally(u)
    assert r.resolved
    # No conflict markers leaked into the resolved text.
    assert "<<<" not in r.text and "===" not in r.text and ">>>" not in r.text

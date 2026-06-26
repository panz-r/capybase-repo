"""Tests for token-level disjoint resolution (survey §4.2 Summer, layer 3).

The line-granular rules (disjoint_edits, zealous_merge) decline whenever two
sides touch the SAME line — even if they changed DIFFERENT TOKENS on it. Token
granularity recognizes these as disjoint and splices both edits in. This is the
safe, disjoint subset of Summer's token-rewrite idea — no move rules, just
disjoint-token splicing with the same safety contract as disjoint_edits, one
granularity finer.

Covers: same-line different-token merges, overlap declines, the lossless
tokenize round-trip, the line-budget guard, and multi-line cases.
"""

from __future__ import annotations

import ast

import pytest

from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.structural_resolver import (
    TOKEN_DISJOINT_MAX_LINES,
    _detokenize,
    _token_change_ops,
    _tokenize,
    _try_token_disjoint,
    resolve_structurally,
)


def _unit(base, cur, rep, *, lang="python", path="app.py"):
    return ConflictUnit(
        session_id="s", step_index=1, path=path, language=lang,
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=cur),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=rep),
        original_worktree_text="", marker_span=None,
    )


# ---------------------------------------------------------------------------
# Tokenize / detokenize — lossless round-trip
# ---------------------------------------------------------------------------


def test_tokenize_splits_into_four_categories():
    toks = _tokenize("x = 3")
    assert toks == ["x", " ", "=", " ", "3"]


def test_tokenize_preserves_indentation():
    toks = _tokenize("    return 1")
    assert toks[0] == "    "


def test_detokenize_is_lossless():
    for src in ["MAX_RETRIES = 3", "    result = f(a, b)", "x=1\ny=2", "", "  "]:
        assert _detokenize(_tokenize(src)) == src


def test_tokenize_handles_punctuation():
    src = "d['key'] = (1, 2)"
    assert _detokenize(_tokenize(src)) == src


# ---------------------------------------------------------------------------
# _try_token_disjoint — the core merge
# ---------------------------------------------------------------------------


def test_value_bump_plus_rename_merges():
    """Side A bumps the value; side B renames the constant — different tokens."""
    r = _try_token_disjoint("MAX_RETRIES = 3", "MAX_RETRIES = 5", "MAX_TIMEOUT = 3")
    assert r == "MAX_TIMEOUT = 5"


def test_rename_plus_flag_toggle_merges():
    r = _try_token_disjoint(
        "    result = process(data, config, verbose=True)",
        "    result = process(data, config, verbose=False)",
        "    output = process(data, config, verbose=True)",
    )
    assert r == "    output = process(data, config, verbose=False)"


def test_overlapping_token_edits_decline():
    """Both sides change the same value token → genuine conflict → None."""
    assert _try_token_disjoint("x = 3", "x = 5", "x = 7") is None


def test_overlapping_name_edits_decline():
    """Both sides rename the same identifier differently → conflict → None."""
    assert _try_token_disjoint("def foo(): pass", "def bar(): pass", "def baz(): pass") is None


def test_one_side_unchanged_declines():
    """If a side made no token change, an earlier rule handles it."""
    assert _try_token_disjoint("x = 3", "x = 5", "x = 3") is None


def test_both_sides_same_change_handled_by_earlier_rule():
    """Identical changes on both sides: token rule sees overlap (correctly
    declines), but the full resolver's ``identical_sides`` rule handles it
    before token_disjoint is ever reached."""
    # In isolation, token rule declines (the token spans coincide):
    assert _try_token_disjoint("x = 3", "x = 5", "x = 5") is None
    # But through the full resolver, identical_sides fires first:
    r = resolve_structurally(_unit("x = 3", "x = 5", "x = 5"))
    assert r.rule == "identical_sides"
    assert r.text == "x = 5"


def test_disjoint_edits_on_different_positions_of_one_line():
    """Two distinct argument changes at different positions on one call."""
    r = _try_token_disjoint(
        "f(a, b, c)",
        "f(a, B, c)",   # changed b -> B
        "f(a, b, C)",   # changed c -> C
    )
    assert r == "f(a, B, C)"


def test_insertions_at_different_positions_merge():
    """Side A inserts a token early; side B inserts one late — disjoint."""
    r = _try_token_disjoint("x = 1", "x = 1 + 2", "y = 1")
    # cur inserts '+ 2' after '1'; rep inserts 'y' before 'x' (well, changes x->y)
    # These are disjoint token changes → merges.
    assert r is not None


# ---------------------------------------------------------------------------
# Budget guard
# ---------------------------------------------------------------------------


def test_declines_when_conflict_too_large():
    """Conflicts exceeding TOKEN_DISJOINT_MAX_LINES stay with the line/entity rules."""
    # Build a conflict just over the line budget.
    big_base = "\n".join(f"v{i} = {i}" for i in range(TOKEN_DISJOINT_MAX_LINES + 1))
    big_cur = big_base.replace("v0 = 0", "v0 = 99")
    big_rep = big_base.replace("v1 = 1", "v1 = 99")
    assert _try_token_disjoint(big_base, big_cur, big_rep) is None


def test_fires_within_line_budget():
    """A conflict within the budget still resolves.

    The budget counts non-blank lines across ALL three sides, so with
    TOKEN_DISJOINT_MAX_LINES=12 each side can have up to 4 lines."""
    base = "a = 1\nb = 2"   # 2 lines × 3 sides = 6 total, well within budget
    cur = "a = 9\nb = 2"
    rep = "a = 1\nb = 9"
    r = _try_token_disjoint(base, cur, rep)
    assert r is not None
    assert "a = 9" in r and "b = 9" in r


# ---------------------------------------------------------------------------
# resolve_structurally integration — the rule fires in the chain
# ---------------------------------------------------------------------------


def test_resolve_structurally_uses_token_disjoint():
    """The full resolver chain reaches token_disjoint for same-line diff-token edits."""
    r = resolve_structurally(_unit("MAX_RETRIES = 3", "MAX_RETRIES = 5", "MAX_TIMEOUT = 3"))
    assert r.rule == "token_disjoint"
    assert r.text == "MAX_TIMEOUT = 5"


def test_resolve_structurally_token_rule_valid_python():
    """The merged text parses cleanly."""
    r = resolve_structurally(
        _unit("    return a + b", "    return a + b + 1", "    return a * b")
    )
    if r.rule == "token_disjoint":
        # Wrap in a function and parse.
        ast.parse("def f(a, b):\n" + r.text)


def test_line_rules_take_precedence_for_whole_line_disjoint():
    """When edits are on entirely different lines, disjoint_edits (not token) fires."""
    base = "a = 1\nb = 2"
    cur = "a = 9\nb = 2"
    rep = "a = 1\nb = 9"
    r = resolve_structurally(_unit(base, cur, rep))
    assert r.rule == "disjoint_edits"


def test_token_rule_fires_when_line_rules_decline():
    """The whole point: token rule catches what line rules can't."""
    base = "timeout = 30"
    cur = "timeout = 60"           # bump value
    rep = "TIMEOUT_SECONDS = 30"   # rename
    r = resolve_structurally(_unit(base, cur, rep))
    assert r.rule == "token_disjoint"
    assert r.text == "TIMEOUT_SECONDS = 60"

"""Tests for the s27-extend-21 dead-rung repairs.

The whole-file repair beam's side-pick (both sites) and alternation
collapse silently NameError'd (undefined ``units``/``language``) inside
their best-effort excepts — zero engagement across every stored flight.
These tests pin the revived machinery and its new guards.
"""

from __future__ import annotations

from capybase.verification import (
    delimiter_failure_shape,
    splice_level_delimiter_repair,
)


# ---------------------------------------------------------------------------
# The shared P6b surgery (single implementation)
# ---------------------------------------------------------------------------


def test_delimiter_failure_shape_classification():
    assert delimiter_failure_shape(["SyntaxError: unmatched ')' at line 3"]) == "delim"
    assert delimiter_failure_shape(["error: unmatched ']'"]) == "delim"
    assert (
        delimiter_failure_shape(
            ["error: mismatched closing delimiter: `}`"])
        == "brace")
    assert (
        delimiter_failure_shape(["brace imbalance detected at line 12"])
        == "brace")
    assert delimiter_failure_shape(["unmatched '}' detected"]) == "brace"
    assert delimiter_failure_shape(["some other error"]) is None
    assert delimiter_failure_shape([]) is None


def test_splice_level_repair_fixes_unbalanced_close():
    """A resolution whose first line closes a construct opened BEFORE the
    marker span: internally balanced alone, unbalanced spliced."""
    worktree = (
        "def f():\n"
        "    x = (1 +\n"
        "        2)\n"
        "<<<<<<< H\n"
        "    pass\n"
        "=======\n"
        "    return\n"
        ">>>>>>> b\n"
       )
    # The conflict block's 0-based span: lines 3..7.
    # The resolution keeps real content AND closes a paren opened before
    # the span (the canonical P6b shape).
    resolved = "    pass\n)"
    out = splice_level_delimiter_repair(
        worktree, (3, 7), resolved,
        ["SyntaxError: unmatched ')' at line 8"], "python")
    assert out is not None
    region, form = out
    assert form == "delim"
    assert "pass" in region
    assert ")" not in region  # the stray close was repaired away


def test_splice_level_repair_declines_non_shape():
    worktree = "a\n<<<<<<< H\nb\n=======\nc\n>>>>>>> d\ne\n"
    assert splice_level_delimiter_repair(
        worktree, (1, 5), "x", ["some other error"], "python") is None


# ---------------------------------------------------------------------------
# The side-pick churn guard
# ---------------------------------------------------------------------------


def _orch_with_sides(monkeypatch, base, cur, rep):
    """An Orchestrator shell whose _micro_stage_sides returns fixed texts."""
    from capybase.orchestrator import Orchestrator

    orch = object.__new__(Orchestrator)
    monkeypatch.setattr(
        orch, "_micro_stage_sides",
        lambda _path: ({"current": cur, "replayed": rep}, base),
        raising=False,
    )
    return orch


def test_churn_guard_allows_asymmetric(tmp_path, monkeypatch):
    """sqlite-0040's shape: loser churn 2, winner 840 — side-pick allowed."""
    base = "line\n" * 900
    cur = "line\n" * 900                       # ≈ base (the loser)
    rep = "".join(f"changed {i}\n" for i in range(420)) + "line\n" * 480
    orch = _orch_with_sides(monkeypatch, base, cur, rep)
    assert orch._side_pick_churn_ok("f.py") is True


def test_churn_guard_declines_symmetric(tmp_path, monkeypatch):
    """Both sides made real, comparable changes — no side-landing (the
    multi-unit fixture's shape: ~8/~8)."""
    base = 'S = ["core"]\nFLAGS = {"a": "off"}\n'
    cur = 'S = ["core", "scheduler"]\nFLAGS = {"a": "on"}\n'
    rep = 'S = ["core", "reloader"]\nFLAGS = {"b": "on"}\n'
    orch = _orch_with_sides(monkeypatch, base, cur, rep)
    assert orch._side_pick_churn_ok("f.py") is False


def test_churn_guard_none_when_sides_missing(tmp_path, monkeypatch):
    orch = _orch_with_sides(monkeypatch, "", "", "")
    assert orch._side_pick_churn_ok("f.py") is None

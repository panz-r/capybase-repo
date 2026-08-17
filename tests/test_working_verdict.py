"""WORKING verdict — the both-features, oracle-divergent outcome.

jsonc-0004 class: the resolver produces a compiling, functioning merge that
preserves BOTH sides' changes, but the repo-derived oracle equals one side
verbatim (the human dropped the other side's working code for reasons
outside the merge inputs — project direction, planning). That is a
near-success, not a failure: these tests pin the preservation measurement
and the verdict routing that separates WORKING from NEAR_MATCH (imperfect
quality) and ORACLE_DIVERGENT (genuinely different). No model, no network.
"""

from __future__ import annotations

from types import SimpleNamespace

from scripts.live_eval_realworld import (
    PASS_THRESHOLD,
    WORKING_PRESERVATION_MIN,
    CaseResult,
    _is_working,
    _preservation_fields,
    _side_preservation,
)


# ---------------------------------------------------------------------------
# _side_preservation
# ---------------------------------------------------------------------------

BASE = "\n".join(f"base {i}" for i in range(10)) + "\n"


def test_preservation_full_superset():
    side = BASE.replace("base 3", "side 3") + "side new\n"
    output = side  # output carries the side's change verbatim
    assert _side_preservation(BASE, side, output) == 1.0


def test_preservation_counts_deletions_as_preserved_when_absent():
    side = BASE.replace("base 5\n", "")           # side deleted line 5
    out_keeps = BASE.replace("base 3", "side 3")  # output kept line 5
    out_honors = out_keeps.replace("base 5\n", "")
    assert _side_preservation(BASE, side, out_keeps) == 0.0
    assert _side_preservation(BASE, side, out_honors) == 1.0


def test_preservation_whitespace_insensitive():
    side = BASE.replace("base 3", "side 3")
    output = BASE.replace("base 3", "\t side 3  ")  # indentation drift
    assert _side_preservation(BASE, side, output) == 1.0


def test_preservation_none_when_side_equals_base():
    assert _side_preservation(BASE, BASE, "anything") is None


def test_preservation_partial():
    side = BASE.replace("base 3", "side 3").replace("base 7", "side 7") + "side new\n"
    output = BASE.replace("base 3", "side 3")  # kept one edit, dropped two
    assert 0.0 < _side_preservation(BASE, side, output) < WORKING_PRESERVATION_MIN


# ---------------------------------------------------------------------------
# _preservation_fields — loser/winner by churn
# ---------------------------------------------------------------------------

def _case(base, current, replayed):
    return SimpleNamespace(base=base, current=current, replayed=replayed)


def test_preservation_fields_rank_by_churn():
    # current rewrote heavily, replayed made the small change
    cur = "\n".join(f"cur {i}" for i in range(10)) + "\n"
    rep = BASE.replace("base 3", "rep 3")
    both = cur.replace("cur 3", "rep 3")  # output carries replayed's edit too
    loser, winner = _preservation_fields(_case(BASE, cur, rep), both)
    assert loser is not None and winner is not None
    assert loser == 1.0            # replayed's edit present
    # current's rewrite carried except the single colliding line, where the
    # output must pick one side's text (19/20 changed lines)
    assert winner == 0.95


def test_preservation_fields_empty_output():
    cur = BASE.replace("base 3", "cur 3")
    assert _preservation_fields(_case(BASE, cur, BASE), "") == (None, None)


# ---------------------------------------------------------------------------
# _is_working — verdict routing
# ---------------------------------------------------------------------------

def _res(**kw):
    r = CaseResult(id="x", language="c", dataset="d")
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def test_working_requires_both_sides_preserved():
    r = _res(marker_free=True, compiles=True,
             matches_oracle=PASS_THRESHOLD - 0.05,
             loser_preservation=1.0, winner_preservation=0.9)
    assert _is_working(r) is True


def test_dropped_loser_feature_is_not_working():
    # output kept the winner's rewrite but lost the loser's small fix —
    # that is a quality divergence (the shape the oracle itself has!), not
    # a both-features merge
    r = _res(marker_free=True, compiles=True,
             matches_oracle=PASS_THRESHOLD - 0.05,
             loser_preservation=0.1, winner_preservation=1.0)
    assert _is_working(r) is False


def test_dropped_winner_rewrite_is_not_working():
    r = _res(marker_free=True, compiles=True,
             matches_oracle=0.5,
             loser_preservation=1.0, winner_preservation=0.2)
    assert _is_working(r) is False


def test_escalated_or_markerful_or_noncompiling_never_working():
    common = dict(matches_oracle=0.5, loser_preservation=1.0,
                  winner_preservation=1.0)
    assert _is_working(_res(escalated=True, marker_free=True, compiles=True, **common)) is False
    assert _is_working(_res(escalated=False, marker_free=False, compiles=True, **common)) is False
    assert _is_working(_res(escalated=False, marker_free=True, compiles=False, **common)) is False


def test_pass_threshold_result_is_not_working():
    # sim at/above the PASS bar is PASS; WORKING only labels the sub-bar
    # near-successes
    r = _res(marker_free=True, compiles=True, matches_oracle=PASS_THRESHOLD,
             loser_preservation=1.0, winner_preservation=1.0)
    assert _is_working(r) is False


def test_missing_preservation_fields_not_working():
    r = _res(marker_free=True, compiles=True,
             matches_oracle=PASS_THRESHOLD - 0.05,
             loser_preservation=None, winner_preservation=None)
    assert _is_working(r) is False

"""S28-144/146 — oracle-calibrated verdict semantics (harness).

The oracle defines correctness. Three guards make the verdict chain
honest when the environment cannot judge:

- S28-146: a TEXTUAL check the runner applies (brace balance, markers,
  python compile) is inapplicable when the ORACLE fails it on the same
  file — the verdict follows sim (17 s28 rows at sim >= 0.95, 8 at
  1.000, read ORACLE_DIVERGENT on oracle-class brace anomalies).
- S28-144(1): an ESCALATED session whose oracle_builds probe FAILED is
  un-passable in this environment — GATE_UNAVAILABLE down to the
  NEAR_MATCH bar (duckdb-0126/0127 at 0.899/0.916 sat under the old
  0.95 door).
- S28-144(2): oracle_equivalent (eval-only flag): marker-free sim >= 0.99
  with oracle_builds False is PASS-equivalent by identity with an oracle
  that cannot build here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "live_eval_realworld_ocg",
        Path(__file__).resolve().parent.parent / "scripts" / "live_eval_realworld.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["live_eval_realworld_ocg"] = mod
    spec.loader.exec_module(mod)
    # S28-368: these tests unit-test the CALIBRATION guard —
    # pin the graduated fresh-gate read off (a different door).
    mod._SHIP_GATE_READ = False  # type: ignore[arg-type]
    return mod


_M = _load_module()


def _row(**kw):
    r = _M.CaseResult(id="x", language="cpp", dataset="d")
    r.escalated = False
    r.marker_free = True
    r.compiles = True
    r.matches_oracle = 1.0
    for k, v in kw.items():
        setattr(r, k, v)
    return r


# ---------------------------------------------------------------------------
# S28-146: the oracle-check-inapplicability helper
# ---------------------------------------------------------------------------

UNBALANCED = "int f() {\n    if (x) {\n        return 1;\n"  # missing closes
BALANCED = "int f() {\n    return 1;\n}\n"


def test_oracle_brace_failure_makes_check_inapplicable():
    assert _M._oracle_check_inapplicable(
        expected=UNBALANCED, language="cpp", marker_free=True,
        compiles=False, gate_applies=True, compiles_from_build=False)


def test_oracle_brace_ok_check_still_applies():
    assert not _M._oracle_check_inapplicable(
        expected=BALANCED, language="cpp", marker_free=True,
        compiles=False, gate_applies=True, compiles_from_build=False)


def test_build_derived_compiles_declines_the_guard():
    # the harness cannot re-run the tree build on the oracle text —
    # that calibration is oracle_builds (S28-144), not this guard
    assert not _M._oracle_check_inapplicable(
        expected=UNBALANCED, language="cpp", marker_free=True,
        compiles=False, gate_applies=True, compiles_from_build=True)


def test_marker_path():
    oracle_with_markers = "<<<<<<< HEAD\nint x;\n>>>>>>> replayed\n"
    assert _M._oracle_check_inapplicable(
        expected=oracle_with_markers, language="cpp", marker_free=False,
        compiles=True, gate_applies=True, compiles_from_build=False)
    assert not _M._oracle_check_inapplicable(
        expected="int x;\n", language="cpp", marker_free=False,
        compiles=True, gate_applies=True, compiles_from_build=False)


def test_python_path():
    assert _M._oracle_check_inapplicable(
        expected="def f(:\n    pass\n", language="python",
        marker_free=True, compiles=False, gate_applies=True,
        compiles_from_build=False)
    assert not _M._oracle_check_inapplicable(
        expected="def f():\n    pass\n", language="python",
        marker_free=True, compiles=False, gate_applies=True,
        compiles_from_build=False)


def test_healthy_or_no_gate_never_inapplicable():
    assert not _M._oracle_check_inapplicable(
        expected=UNBALANCED, language="cpp", marker_free=True,
        compiles=True, gate_applies=True, compiles_from_build=False)
    assert not _M._oracle_check_inapplicable(
        expected=UNBALANCED, language="cpp", marker_free=True,
        compiles=False, gate_applies=False, compiles_from_build=False)
    assert not _M._oracle_check_inapplicable(
        expected="", language="cpp", marker_free=False,
        compiles=False, gate_applies=True, compiles_from_build=False)


# ---------------------------------------------------------------------------
# S28-146: the verdict follows sim when the check is inapplicable
# ---------------------------------------------------------------------------

def test_inapplicable_check_sim_one_passes():
    r = _row(compiles=False, oracle_check_inapplicable=True, matches_oracle=1.0)
    assert _M._verdict_chain(r) == "PASS"


def test_inapplicable_check_sim_bands():
    r = _row(compiles=False, oracle_check_inapplicable=True,
             matches_oracle=0.88)  # just under PASS_THRESHOLD (0.90)
    assert _M._verdict_chain(r) == "NEAR_MATCH"
    r = _row(compiles=False, oracle_check_inapplicable=True,
             matches_oracle=0.5)
    assert _M._verdict_chain(r) == "ORACLE_DIVERGENT"


def test_check_failure_without_oracle_backup_still_divergent():
    # the S28-161 lesson holds: only the oracle-fails-too path flips —
    # an unbacked check failure keeps the honest verdict
    r = _row(compiles=False, oracle_check_inapplicable=False,
             matches_oracle=1.0)
    assert _M._verdict_chain(r) == "ORACLE_DIVERGENT"


def test_marker_failure_inapplicable_via_oracle_markers():
    r = _row(marker_free=False, compiles=True,
             oracle_check_inapplicable=True, matches_oracle=0.85)
    assert _M._verdict_chain(r) == "NEAR_MATCH"


# ---------------------------------------------------------------------------
# S28-144(1): the escalated + oracle_builds=False door widens to 0.80
# ---------------------------------------------------------------------------

def test_escalated_oracle_builds_false_near_bar():
    r = _row(escalated=True, oracle_builds=False, matches_oracle=0.9)
    assert _M._verdict_chain(r) == "GATE_UNAVAILABLE"


def test_escalated_oracle_builds_false_below_near_bar_stays():
    r = _row(escalated=True, oracle_builds=False, matches_oracle=0.7)
    assert _M._verdict_chain(r) == "ESCALATE"


def test_escalated_without_oracle_failure_stays_escalate():
    r = _row(escalated=True, oracle_builds=None, matches_oracle=0.9)
    assert _M._verdict_chain(r) == "ESCALATE"
    r = _row(escalated=True, oracle_builds=True, matches_oracle=0.9)
    assert _M._verdict_chain(r) == "ESCALATE"


def test_legacy_override_untouched():
    # the original >= 0.95 any-verdict override still holds
    r = _row(compiles=False, oracle_builds=False, matches_oracle=0.97)
    assert _M._verdict_chain(r) == "GATE_UNAVAILABLE"

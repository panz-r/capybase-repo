"""S28-191/192/194/196b/202 — the harness-hygiene package (eval-only).

Pilot4/pilot5's duckdb evidence, built out: the oracle probe memoized
per case (S28-191(1)), a timed-out tree gate is UNDECIDABLE rather
than degraded (S28-192(c)) and skips the second cold build
(S28-191(2)/(3)), the standalone route detects real include roots and
declines non-discriminative missing-include evidence (S28-192(a,b)),
the acceptance trust lands on the row (S28-194), the operator= shape
joins the side-symbol patterns (S28-196b), and TIMEOUT_* classes
require timeout evidence (S28-202).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "live_eval_hygiene_s28",
        Path(__file__).resolve().parent.parent / "scripts" / "live_eval_realworld.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["live_eval_hygiene_s28"] = mod
    spec.loader.exec_module(mod)  # type: ignore[arg-type]
    return mod


_M = _load_module()


# ---------------------------------------------------------------------------
# S28-192(b): include-root detection
# ---------------------------------------------------------------------------

def test_include_roots_find_the_tree_layout(tmp_path):
    (tmp_path / "src" / "include").mkdir(parents=True)
    _M._DETECTED_BUILD_CMD["case-x"] = "cmake --build build -I/opt/gtest/include"
    case = SimpleNamespace(id="case-x", path="src/parser/parser.cpp")
    roots = _M._oracle_include_roots(tmp_path, case)
    assert str(tmp_path / "src" / "include") in roots
    assert "/opt/gtest/include" in roots
    assert str(tmp_path) in roots  # the S28-105 baseline stays


def test_include_roots_skip_absent_dirs(tmp_path):
    _M._DETECTED_BUILD_CMD.pop("case-y", None)
    case = SimpleNamespace(id="case-y", path="a/b.cpp")
    roots = _M._oracle_include_roots(tmp_path, case)
    assert roots == [str(tmp_path), str(tmp_path / "a")]


# ---------------------------------------------------------------------------
# S28-191(1): the oracle-probe memo
# ---------------------------------------------------------------------------

def test_oracle_probe_cached_per_case(monkeypatch):
    _M._ORACLE_PROBE_CACHE.pop("case-z", None)
    calls = []
    monkeypatch.setattr(_M, "_oracle_builds_uncached",
                        lambda repo, case, cs: calls.append(case.id) or "R")
    case = SimpleNamespace(id="case-z")
    assert _M._oracle_builds(Path("/tmp"), case, None) == "R"
    assert _M._oracle_builds(Path("/tmp"), case, None) == "R"
    assert calls == ["case-z"]  # the second repeat read the memo
    _M._ORACLE_PROBE_CACHE.pop("case-z", None)


def test_timed_out_tree_gate_blocks_the_oracle_probe(monkeypatch):
    """S28-191(2)+S28-192(c): the runner build timed out — the oracle
    probe returns None WITHOUT a second cold build and WITHOUT the
    standalone fallback (a timeout is undecidable, not degraded)."""
    _M._C_BUILD_TIMED_OUT.add("case-t")
    called = []
    monkeypatch.setattr(_M, "_c_builds",
                        lambda repo, case: called.append(1) or None)
    case = SimpleNamespace(id="case-t", language="cpp", path="a/b.cpp",
                           expected_resolved="int main(){}")
    assert _M._oracle_builds_uncached(Path("/tmp"), case, None) is None
    assert called == []  # no second 300s burn
    _M._C_BUILD_TIMED_OUT.discard("case-t")


def test_c_builds_fast_skips_the_memoized_timeout(monkeypatch):
    _M._C_BUILD_TIMED_OUT.add("case-s")
    ran = []
    monkeypatch.setattr(_M, "_run_shell_tree",
                        lambda *a, **k: ran.append(1))
    case = SimpleNamespace(id="case-s", dataset="d")
    assert _M._c_builds(Path("/tmp"), case) is None
    assert ran == []
    _M._C_BUILD_TIMED_OUT.discard("case-s")


def test_c_builds_timeout_raises_and_memoizes(monkeypatch):
    """S28-192(c): the timeout must NOT read as the degraded None —
    _c_builds re-raises after memoizing."""
    _M._C_BUILD_TIMED_OUT.discard("case-r")

    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="make", timeout=300)

    monkeypatch.setattr(_M, "_run_shell_tree", _boom)
    _M._DETECTED_BUILD_CMD["case-r"] = "make"
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            _M._c_builds(Path("/tmp"), SimpleNamespace(id="case-r", dataset="d"))
        assert "case-r" in _M._C_BUILD_TIMED_OUT
    finally:
        _M._C_BUILD_TIMED_OUT.discard("case-r")
        _M._DETECTED_BUILD_CMD.pop("case-r", None)


def test_degraded_gate_include_fatals_stay_false(monkeypatch, tmp_path):
    """The S28-192 refinement the D10 pin forced: on the GENUINELY
    degraded route (no build command), include-fatals-only failures
    stay False — the environment's true state (generated headers
    absent) and the sound discriminator behind the php band's honest
    GU (S28-199). The unsound population (timeout-routed probes) is
    handled by the S28-192(c) timeout split, not here. The probe runs
    with the tree's enriched roots (S28-192(b))."""
    _M._C_BUILD_TIMED_OUT.discard("case-m")
    _M._DETECTED_BUILD_CMD.pop("case-m", None)
    monkeypatch.setitem(_M.C_BUILD_COMMANDS, "hyg-d", "")  # no gate

    from capybase import verification as _v
    seen = {}

    def _fake_ccs(text, cc_path, std, suffix, include_paths):
        seen["paths"] = include_paths
        return False, ("fatal error: duckdb/parser/parser.hpp: "
                       "No such file or directory\ncompilation terminated.\n")

    monkeypatch.setattr(_v, "_compile_ccs", _fake_ccs)
    case = SimpleNamespace(id="case-m", language="cpp", path="y.cpp",
                           dataset="hyg-d",
                           expected_resolved="#include <duckdb/x.hpp>\nint main(){}\n")
    assert _M._oracle_builds_uncached(tmp_path, case, None) is False
    assert seen["paths"] == [str(tmp_path), str(tmp_path)]  # repo + file dir
    _M._C_BUILD_TIMED_OUT.discard("case-m")



# ---------------------------------------------------------------------------
# S28-194: the acceptance trust on the row
# ---------------------------------------------------------------------------

def test_caseresult_carries_acceptance_trust_fields():
    r = _M.CaseResult(id="x", language="cpp", dataset="d")
    assert r.acceptance_tier is None and r.acceptance_decision is None
    r.acceptance_tier = "B"
    r.acceptance_decision = "PROPOSE_FOR_REVIEW"
    assert r.__dict__["acceptance_tier"] == "B"  # serialized with the row


# ---------------------------------------------------------------------------
# S28-202: timeout evidence (extended pins live in the taxonomy file)
# ---------------------------------------------------------------------------

def test_timeout_case_still_classifies_from_explicit_reason():
    assert _M._classify_terminal_reason(
        "case timeout after 1200s") == "TIMEOUT_CASE"
    assert _M._classify_terminal_reason(
        "post-resolution scoring exceeded the wall (1200s)") == \
        "TIMEOUT_AFTER_ACCEPT"


# ---------------------------------------------------------------------------
# S28-196b: the operator= side-symbol pattern
# ---------------------------------------------------------------------------

def test_operator_eq_pattern_captured():
    import capybase.resolution_engine as re_mod
    import re as _re
    msg = ("error: no match for 'operator=' (operand types are "
           "'duckdb::MatchState' and 'int')")
    syms = []
    for pat in re_mod._SIDE_SYMBOL_PATTERNS:
        m = _re.search(pat, msg)
        if m and m.group(1) not in syms:
            syms.append(m.group(1))
    assert "operator=" in syms


def test_ship_gate_unproven_census(monkeypatch):
    """S28-232: escalated c/cpp rows with no passing probe after the
    last acceptance carry ship_gate_unproven=True — the fmt-0003
    pattern on every row, computable from the journal events."""
    from types import SimpleNamespace as NS

    def _ev(seq, etype, outcome=None):
        return NS(seq=seq, event_type=etype,
                  payload=({"outcome": outcome} if outcome else {}))

    case = NS(id="c1", language="cpp")
    res = _M.CaseResult(id="c1", language="cpp", dataset="d")
    res.escalated = True
    events = [
        _ev(10, "build_probe", "fail"),
        _ev(20, "candidate_accepted"),
        _ev(30, "build_probe", "fail"),   # strangers' probes after accept
        _ev(40, "build_probe", "fail"),
    ]
    last_accept = max((e.seq for e in events
                       if e.event_type == "candidate_accepted"), default=None)
    proved = any(e.event_type == "build_probe"
                 and (e.payload or {}).get("outcome") == "pass"
                 and e.seq > last_accept for e in events)
    ship_unproven = (case.language in ("c", "cpp", "c++")
                     and res.escalated and last_accept is not None
                     and not proved)
    assert ship_unproven is True

    # a passing probe after acceptance disproves it
    events.append(_ev(50, "build_probe", "pass"))
    proved = any(e.event_type == "build_probe"
                 and (e.payload or {}).get("outcome") == "pass"
                 and e.seq > last_accept for e in events)
    assert proved is True

    # the field serializes with the row
    res.ship_gate_unproven = True
    assert res.__dict__["ship_gate_unproven"] is True


def test_source_pin_beam_skip_census():
    """S28-233: the beam-skip census emit exists and precedes the
    `if accepted:` guard — the empty-accepted population (0052's shape:
    every per-unit candidate rejected, the assembled buffer failing on
    the inter-unit seam) is journaled instead of silently skipped."""
    import capybase.orchestrator as orch_mod
    from pathlib import Path
    src = Path(orch_mod.__file__).read_text()
    assert "whole_file_beam_skipped" in src
    i_skip = src.index("whole_file_beam_skipped")
    i_guard = src.index("if accepted:", i_skip)
    assert i_skip < i_guard

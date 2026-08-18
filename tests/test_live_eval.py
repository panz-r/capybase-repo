"""Tests for the eval harness classifier (_classify_terminal_reason).

These tests prevent classification regressions like the SAFE_SKIP false-match
on 'per-unit gcc gate is skipped' (which classified a header-cap escalation
as a safe skip).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_classifier():
    """Import _classify_terminal_reason from scripts/live_eval_realworld.py.

    The script isn't a package module, so we load it by path."""
    spec = importlib.util.spec_from_file_location(
        "live_eval_realworld",
        Path(__file__).resolve().parent.parent / "scripts" / "live_eval_realworld.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["live_eval_realworld"] = mod
    spec.loader.exec_module(mod)  # type: ignore[arg-type]
    return mod._classify_terminal_reason


_classify = _load_classifier()


def test_safe_skip_matches_no_conflict():
    assert _classify("skipped (no conflict): git rebase resolved cleanly") == "SAFE_SKIP"


def test_safe_skip_does_not_match_gate_skipped():
    """Regression: 'skipped' in reason matched 'per-unit gcc gate is skipped',
    misclassifying a header-cap escalation as SAFE_SKIP."""
    assert _classify(
        "header file CEGIS cap reached (0 retry budget for headers; "
        "per-unit gcc gate is skipped)"
    ) != "SAFE_SKIP"


def test_safe_stop_matches_resurrection():
    assert _classify("suspected silent resurrection of deleted content") == "SAFE_STOP"


def test_timeout_throughput_matches_case_timeout_many_regions():
    assert _classify("case timeout after 1200s (endless CEGIS retries)") == "TIMEOUT_CASE"


def test_model_empty_matches_could_not_resolve():
    assert _classify("could not resolve include/nlohmann/json.hpp:1:0 (no specific reason)") == "MODEL_EMPTY"


def test_oversized_matches_too_large():
    assert _classify("oversized prompt: 18347t > 8192t window") == "OVERSIZED"


def test_other_is_fallback():
    assert _classify("some unrecognized reason") == "OTHER"


# ---------------------------------------------------------------------------
# _verdict_chain — the GATE_UNAVAILABLE classification (sprint-17 WS1c)
# ---------------------------------------------------------------------------

def _load_verdict_chain():
    spec = importlib.util.spec_from_file_location(
        "live_eval_realworld_vchain",
        Path(__file__).resolve().parent.parent / "scripts" / "live_eval_realworld.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["live_eval_realworld_vchain"] = mod
    spec.loader.exec_module(mod)  # type: ignore[arg-type]
    return mod._verdict_chain, mod.CaseResult, mod.PASS_THRESHOLD


_verdict, _CR, _PT = _load_verdict_chain()


def _rec(**kw):
    base = dict(id="x", language="rust", dataset="d", escalated=False,
                marker_free=True, compiles=True, matches_oracle=0.99,
                elapsed=1.0, reason="", verdict="")
    base.update(kw)
    return _CR(**base)


def test_gate_unavailable_when_oracle_fails_same_gate():
    # sim 0.999 merge, gate rejected, oracle_builds=False (the oracle fails
    # the same gate) → sandbox artifact, not a resolver failure.
    r = _rec(escalated=True, matches_oracle=0.999, oracle_builds=False)
    assert _verdict(r) == "GATE_UNAVAILABLE"


def test_gate_rejection_with_clean_oracle_stands():
    # oracle_builds=True → the gate CAN distinguish — the rejection is real.
    r = _rec(escalated=True, matches_oracle=0.999, oracle_builds=True)
    assert _verdict(r) == "ESCALATE"
    r = _rec(escalated=False, marker_free=True, compiles=False,
             matches_oracle=0.999, oracle_builds=True)
    assert _verdict(r) == "ORACLE_DIVERGENT"


def test_undecidable_probe_changes_nothing():
    # oracle_builds=None (probe didn't run / undecidable) → original verdict.
    r = _rec(escalated=True, matches_oracle=0.999, oracle_builds=None)
    assert _verdict(r) == "ESCALATE"


def test_gate_unavailable_requires_high_sim():
    # A divergent merge (sim < 0.95) never hides behind the classification.
    r = _rec(escalated=True, matches_oracle=0.70, oracle_builds=False)
    assert _verdict(r) == "ESCALATE"


def test_pass_never_overridden():
    r = _rec(matches_oracle=0.99, oracle_builds=False)
    assert _verdict(r) == "PASS"

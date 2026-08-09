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

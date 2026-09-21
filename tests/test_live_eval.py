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


def test_setup_failed_matches_infrastructure_failures():
    """s27-extend-27: git-lock/materializer setup failures are NOT resolver
    outcomes — clickhouse-0003 sat in the ESCALATE column for weeks on a
    git-lock write failure before this class existed."""
    assert _classify(
        "setup failed: RuntimeError: git ('add', '-A') failed: fatal: "
        "unable to write lock file"
    ) == "SETUP_FAILED"
    assert _classify(
        "setup failed: AttributeError: 'bool' object has no attribute 'stat'"
    ) == "SETUP_FAILED"


def test_setup_failed_not_matched_mid_reason():
    """Only the reason PREFIX counts — a resolver outcome that happens to
    mention 'setup' stays classified by its own class."""
    assert _classify("could not resolve after setup phase") != "SETUP_FAILED"


def test_harness_crashes_classify_setup_failed():
    """s27-72: 'orch raised: ...' / 'harness error: ...' are
    infrastructure, not resolver capability — SETUP_FAILED keeps them out
    of the real-conflict denominator (the clickhouse-0003 doctrine)."""
    from scripts.live_eval_realworld import _classify_terminal_reason
    assert _classify_terminal_reason(
        "orch raised: RuntimeError: disk full") == "SETUP_FAILED"
    assert _classify_terminal_reason(
        "harness error: TimeoutExpired") == "SETUP_FAILED"


# ---------------------------------------------------------------------------
# S28-137: the case-timeout watchdog must distinguish "engine accepted and
# completed; scoring builds blew the wall" from "endless CEGIS retries", and
# give a finished resolution a scoring grace before abandonment.
# ---------------------------------------------------------------------------

def _load_runner_module():
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location(
        "live_eval_realworld_s28137",
        Path(__file__).resolve().parent.parent / "scripts" / "live_eval_realworld.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["live_eval_realworld_s28137"] = mod
    spec.loader.exec_module(mod)  # type: ignore[arg-type]
    return mod


def test_timeout_after_accept_classified_distinctly():
    """The engine-accepted timeout reason must NOT fall into TIMEOUT_CASE/
    TIMEOUT_CAPABILITY — a completed resolution is not a capability failure
    (duckdb-0062/0106 were scored ESCALATE/TIMEOUT_CAPABILITY despite the
    journals showing candidate_accepted + session_completed)."""
    classify = _load_classifier()
    assert classify(
        "case timeout after 1200s (engine accepted; post-resolution scoring "
        "exceeded the wall)") == "TIMEOUT_AFTER_ACCEPT"
    # The legacy reason keeps its classification.
    assert classify("case timeout after 1200s (endless CEGIS retries)") == \
        "TIMEOUT_CASE"


def _write_journal(path, event_types):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(
        f'{{"event_type": "{e}"}}' for e in event_types) + "\n")


def test_engine_session_completed_detects_accepted_session(tmp_path):
    mod = _load_runner_module()
    j = tmp_path / "flights" / "c1" / "sessA" / "journal.jsonl"
    _write_journal(j, ["session_started", "conflict_detected",
                       "candidate_accepted", "session_completed"])
    assert mod._engine_session_completed(tmp_path, "c1") is True


def test_engine_session_completed_false_when_resolution_incomplete(tmp_path):
    """A journal WITHOUT session_completed means the engine is still resolving
    — the watchdog must not grant the scoring grace (old behavior)."""
    mod = _load_runner_module()
    j = tmp_path / "flights" / "c1" / "sessA" / "journal.jsonl"
    _write_journal(j, ["session_started", "resolution_attempt",
                       "candidate_rejected"])
    assert mod._engine_session_completed(tmp_path, "c1") is False


def test_engine_session_completed_prefers_newest_journal(tmp_path):
    """--preserve-flights accumulates session dirs across retries; only the
    NEWEST session's outcome counts (an older accepted session must not
    vouch for a current run that is still looping)."""
    import os
    mod = _load_runner_module()
    old = tmp_path / "flights" / "c1" / "old"
    new = tmp_path / "flights" / "c1" / "new"
    _write_journal(old / "journal.jsonl",
                   ["candidate_accepted", "session_completed"])
    _write_journal(new / "journal.jsonl", ["session_started"])
    os.utime(old / "journal.jsonl", (1_000_000_000, 1_000_000_000))
    os.utime(new / "journal.jsonl", (2_000_000_000, 2_000_000_000))
    assert mod._engine_session_completed(tmp_path, "c1") is False
    # And the reverse ordering — newest completed → True.
    os.utime(new / "journal.jsonl", (500_000_000, 500_000_000))
    os.utime(old / "journal.jsonl", (2_000_000_000, 2_000_000_000))
    assert mod._engine_session_completed(tmp_path, "c1") is True


def test_engine_session_completed_handles_missing_or_corrupt(tmp_path):
    mod = _load_runner_module()
    assert mod._engine_session_completed(None, "c1") is False
    assert mod._engine_session_completed(tmp_path, "c1") is False  # no journals
    j = tmp_path / "flights" / "c1" / "s" / "journal.jsonl"
    j.parent.mkdir(parents=True)
    j.write_text("{not json at all\n")
    assert mod._engine_session_completed(tmp_path, "c1") is False

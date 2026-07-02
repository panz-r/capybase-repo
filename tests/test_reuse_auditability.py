"""Tests for exact-reuse auditability (#idea 8).

The reuse mechanism now records WHY a match succeeded (matched_conditions) and
WHY near-misses were rejected (near_misses), and surfaces the source prior in the
accept report. A skip is no longer indistinguishable from an empty store.
"""

from __future__ import annotations

from capybase.conflict_model import HistoricalExample
from capybase.exact_reuse import ReuseCandidate, find_exact_reuse
from capybase.memory.shape import conflict_shape_hash
from capybase.memory.store import Experience, ExperienceStore


def _exp(base, current, replayed, resolved, *, region_kind="function",
         conflict_shape=None, language="python", outcome="accepted",
         validator_features=None):
    if conflict_shape is None:
        conflict_shape = conflict_shape_hash(
            base=base, current=current, replayed=replayed
        )
    return Experience(
        example=HistoricalExample(
            summary="cfg.py:u1", base=base, current=current,
            replayed=replayed, resolved=resolved, source="s",
        ),
        outcome=outcome, language=language, path="cfg.py",
        region_kind=region_kind, conflict_shape=conflict_shape,
        validator_features=validator_features or {},
    )


def _store(tmp_path, exps):
    store = ExperienceStore(tmp_path / "exp.jsonl")
    for e in exps:
        store.append(e)
    return store


def _unit(base, cur, rep, *, language="python"):
    from capybase.conflict_model import ConflictSide, ConflictUnit
    return ConflictUnit(
        session_id="s", step_index=0, path="cfg.py", language=language,
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=cur),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=rep),
        original_worktree_text=base, marker_span=(0, 0),
    )


# ---------------------------------------------------------------------------
# match conditions recorded
# ---------------------------------------------------------------------------


def test_match_records_which_conditions_matched(tmp_path):
    """A full match carries matched_conditions naming shape/language/region/evidence."""
    base, cur, rep = "def load():\n    return 1", "def load():\n    return 2", "def load():\n    return 3"
    store = _store(tmp_path, [_exp(base, cur, rep, "merged")])
    unit = _unit(base, cur, rep)
    reuse = find_exact_reuse(unit=unit, store=store, language="python",
                             region_kind="function")
    assert reuse is not None and not reuse.skip_reason
    assert reuse.matched_conditions
    # The conditions name the shape, language, region, outcome, evidence.
    cond_str = " ".join(reuse.matched_conditions)
    assert "shape=" in cond_str
    assert "language=python" in cond_str
    assert "region_kind=function" in cond_str
    assert "outcome=accepted" in cond_str
    assert "evidence=" in cond_str


# ---------------------------------------------------------------------------
# near-miss recording
# ---------------------------------------------------------------------------


def test_near_miss_wrong_language_recorded(tmp_path):
    """A same-shape prior with the wrong language is recorded as a near-miss."""
    base, cur, rep = "def load():\n    return 1", "def load():\n    return 2", "def load():\n    return 3"
    store = _store(tmp_path, [_exp(base, cur, rep, "merged", language="rust")])
    unit = _unit(base, cur, rep, language="python")
    reuse = find_exact_reuse(unit=unit, store=store, language="python",
                             region_kind="function")
    # No full match, but a near-miss skip sentinel with the reason.
    assert reuse is not None
    assert reuse.skip_reason
    assert any("wrong language" in nm for nm in reuse.near_misses)


def test_near_miss_multiple_rejections_recorded(tmp_path):
    """Multiple same-shape priors failing different conditions are all recorded."""
    base, cur, rep = "def load():\n    return 1", "def load():\n    return 2", "def load():\n    return 3"
    store = _store(tmp_path, [
        _exp(base, cur, rep, "merged", language="rust"),  # wrong language
        _exp(base, cur, rep, "merged", region_kind="class"),  # wrong region
    ])
    unit = _unit(base, cur, rep, language="python")
    reuse = find_exact_reuse(unit=unit, store=store, language="python",
                             region_kind="function")
    assert reuse is not None and reuse.skip_reason
    assert len(reuse.near_misses) == 2
    reasons = " ".join(reuse.near_misses)
    assert "wrong language" in reasons
    assert "wrong region kind" in reasons


def test_genuine_empty_store_returns_none(tmp_path):
    """A store with no same-shape priors returns None (not a skip sentinel)."""
    # The stored prior is an APPEND (adds a line); the query is a MODIFY (changes
    # a line) — genuinely different shapes, so no shape match → None.
    store = _store(tmp_path, [
        _exp("x = 1", "x = 1\ny = 2", "x = 1\nz = 3", "merged"),
    ])
    unit = _unit("def load():\n    return 1",
                 "def load():\n    return 2",
                 "def load():\n    return 3")
    reuse = find_exact_reuse(unit=unit, store=store, language="python",
                             region_kind="function")
    # No shape match at all → None (genuine empty, not a near-miss skip).
    assert reuse is None


# ---------------------------------------------------------------------------
# surfacing in accept reports
# ---------------------------------------------------------------------------


def test_accept_report_shows_reuse_source():
    """The accept report names the source prior when resolved via exact reuse."""
    from capybase.accept_report import build_accept_report
    from capybase.conflict_model import (
        CandidateResolution, ConflictSide, ConflictUnit, VerificationResult,
    )
    from capybase.orchestrator import UnitOutcome

    unit = ConflictUnit(
        session_id="s", step_index=0, path="cfg.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="a"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="b"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="c"),
        original_worktree_text="a", marker_span=(0, 0),
    )
    cand = CandidateResolution(
        candidate_id="u:exact_reuse", unit_id="u", model_name="exact-reuse",
        prompt_version="exact_history_reuse.v1", resolved_text="bc",
        explanation="verbatim replay of prior accepted resolution (from cfg.py:prior)",
        provenance="exact_history_reuse",
    )
    o = UnitOutcome(unit=unit)
    o.accepted = cand
    o.validation = VerificationResult(
        candidate_id="u:exact_reuse", unit_id="u", passed=True,
        features={"markers_remaining": 0, "syntax_passed": True},
    )
    report = build_accept_report([o], tests_passed=True, test_verdict="ok")
    assert "reuse source" in report
    assert "cfg.py:prior" in report

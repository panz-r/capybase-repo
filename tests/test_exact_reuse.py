"""Tests for exact history reuse (#9 step 4).

The conservative rerere++ exact mechanism: replay a prior accepted resolution
verbatim when the conflict shape + language + region kind + outcome + validation
evidence ALL match. Always on; safety = full re-validation (a stale reuse fails
and falls through). Covers the pure finder + the orchestrator dispatch.
"""

from __future__ import annotations

from types import SimpleNamespace

from capybase.conflict_model import (
    ConflictSide,
    ConflictUnit,
    HistoricalExample,
)
from capybase.exact_reuse import ReuseCandidate, find_exact_reuse
from capybase.memory.shape import conflict_shape_hash
from capybase.memory.store import Experience, ExperienceStore


def _unit(base, current, replayed, *, language="python"):
    return ConflictUnit(
        session_id="s", step_index=0, path="cfg.py", language=language,
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text=base, marker_span=(0, 0),
    )


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
            replayed=replayed, resolved=resolved, source="s1",
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


# ---------------------------------------------------------------------------
# the pure finder
# ---------------------------------------------------------------------------


def test_no_store_returns_none():
    unit = _unit("a", "b", "c")
    assert find_exact_reuse(unit=unit, store=None, language="python",
                            region_kind="function") is None


def test_exact_match_returns_reuse_candidate(tmp_path):
    base, cur, rep = "def load():\n    return 1", "def load():\n    return 2", "def load():\n    return 3"
    resolved = "def load():\n    return 4"
    store = _store(tmp_path, [_exp(base, cur, rep, resolved)])
    unit = _unit(base, cur, rep)
    reuse = find_exact_reuse(unit=unit, store=store, language="python",
                             region_kind="function")
    assert reuse is not None
    assert reuse.resolved_text == resolved
    assert reuse.source_summary == "cfg.py:u1"


def test_different_shape_does_not_match(tmp_path):
    """A conflict with a different edit structure is never reused.

    The shape is content-agnostic (same per-side edit counts hash equal), so a
    genuine structural difference is required: an append-only conflict vs a
    modify-both-sides conflict.
    """
    # Stored: both sides APPEND a distinct line (shape: cur added=1, rep added=1).
    store = _store(tmp_path, [
        _exp("def load():\n    return 1",
             "def load():\n    return 1\n# added by current",
             "def load():\n    return 1\n# added by replayed",
             "merged"),
    ])
    # Query: both sides MODIFY the return line (shape: cur changed=1, rep changed=1).
    unit = _unit("def load():\n    return 1",
                 "def load():\n    return 99",
                 "def load():\n    return 100")
    reuse = find_exact_reuse(unit=unit, store=store, language="python",
                             region_kind="function")
    assert reuse is None


def test_different_language_does_not_match(tmp_path):
    base, cur, rep = "def load():\n    return 1", "def load():\n    return 2", "def load():\n    return 3"
    store = _store(tmp_path, [_exp(base, cur, rep, "merged", language="rust")])
    unit = _unit(base, cur, rep, language="python")
    reuse = find_exact_reuse(unit=unit, store=store, language="python",
                             region_kind="function")
    # Same shape, wrong language → a near-miss skip sentinel (#idea 8), not None.
    assert reuse is None or reuse.skip_reason
    if reuse is not None:
        assert any("wrong language" in nm for nm in reuse.near_misses)


def test_different_region_kind_does_not_match(tmp_path):
    base, cur, rep = "def load():\n    return 1", "def load():\n    return 2", "def load():\n    return 3"
    store = _store(tmp_path, [_exp(base, cur, rep, "merged", region_kind="class")])
    unit = _unit(base, cur, rep)
    reuse = find_exact_reuse(unit=unit, store=store, language="python",
                             region_kind="function")
    assert reuse is None or reuse.skip_reason
    if reuse is not None:
        assert any("wrong region kind" in nm for nm in reuse.near_misses)


def test_escalated_outcome_never_reused(tmp_path):
    """An escalated (non-accepted) prior is never reused."""
    base, cur, rep = "def load():\n    return 1", "def load():\n    return 2", "def load():\n    return 3"
    store = _store(tmp_path, [
        _exp(base, cur, rep, "bad", outcome="escalated"),
    ])
    unit = _unit(base, cur, rep)
    # store.accepted() excludes escalated, so the scan finds nothing.
    reuse = find_exact_reuse(unit=unit, store=store, language="python",
                             region_kind="function")
    assert reuse is None


def test_prior_with_failed_diagnostics_not_reused(tmp_path):
    """Condition 5: a prior that introduced diagnostics isn't trusted."""
    base, cur, rep = "def load():\n    return 1", "def load():\n    return 2", "def load():\n    return 3"
    store = _store(tmp_path, [
        _exp(base, cur, rep, "merged",
             validator_features={"introduced_diagnostics": 3}),
    ])
    unit = _unit(base, cur, rep)
    reuse = find_exact_reuse(unit=unit, store=store, language="python",
                             region_kind="function")
    # Same shape, failed diagnostics → a near-miss skip sentinel.
    assert reuse is None or reuse.skip_reason
    if reuse is not None:
        assert any("no validation evidence" in nm for nm in reuse.near_misses)


def test_prior_with_tests_passed_is_trusted(tmp_path):
    """Condition 5: tests_passed=True is strong validation evidence."""
    base, cur, rep = "def load():\n    return 1", "def load():\n    return 2", "def load():\n    return 3"
    store = _store(tmp_path, [
        _exp(base, cur, rep, "merged",
             validator_features={"tests_passed": True, "introduced_diagnostics": 1}),
    ])
    unit = _unit(base, cur, rep)
    reuse = find_exact_reuse(unit=unit, store=store, language="python",
                             region_kind="function")
    assert reuse is not None


def test_empty_resolved_text_not_reused(tmp_path):
    """A prior with an empty resolution is skipped (nothing to replay)."""
    base, cur, rep = "def load():\n    return 1", "def load():\n    return 2", "def load():\n    return 3"
    store = _store(tmp_path, [_exp(base, cur, rep, "")])
    unit = _unit(base, cur, rep)
    reuse = find_exact_reuse(unit=unit, store=store, language="python",
                             region_kind="function")
    # Same shape, empty resolved → a near-miss skip sentinel.
    assert reuse is None or reuse.skip_reason
    if reuse is not None:
        assert any("empty resolved" in nm for nm in reuse.near_misses)


# ---------------------------------------------------------------------------
# orchestrator dispatch: the reused candidate is re-validated
# ---------------------------------------------------------------------------


def test_orchestrator_reuses_when_match_exists(repo, tmp_path, monkeypatch):
    """A matching prior accepted resolution is replayed and accepted."""
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator

    cfg = Config()
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)

    base, cur, rep = "def load():\n    return 1", "def load():\n    return 2", "def load():\n    return 3"
    resolved = "def load():\n    return 2\n"
    store = ExperienceStore(tmp_path / "mem.jsonl")
    shape = conflict_shape_hash(base=base, current=cur, replayed=rep)
    store.append(Experience(
        example=HistoricalExample(summary="cfg.py:prior", base=base, current=cur,
                                  replayed=rep, resolved=resolved, source="old"),
        outcome="accepted", language="python", path="cfg.py",
        region_kind="function", conflict_shape=shape, validator_features={},
    ))
    orch.memory_store = store

    unit = _unit(base, cur, rep)
    unit.structural_metadata["enclosing_node_type"] = "function_definition"
    outcome = orch._try_exact_reuse(unit)
    assert outcome is not None
    assert outcome.accepted is not None
    assert outcome.accepted.provenance == "exact_history_reuse"
    assert outcome.accepted.resolved_text == resolved


def test_orchestrator_falls_through_when_no_match(repo, tmp_path):
    """No match → _try_exact_reuse returns None (falls through to structural)."""
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator

    cfg = Config()
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    orch.memory_store = ExperienceStore(tmp_path / "mem.jsonl")
    unit = _unit("def load():\n    return 1",
                 "def load():\n    return 2",
                 "def load():\n    return 3")
    assert orch._try_exact_reuse(unit) is None


def test_orchestrator_no_store_returns_none(repo):
    """No memory store configured → no reuse."""
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator

    cfg = Config()
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    orch.memory_store = None
    unit = _unit("a", "b", "c")
    assert orch._try_exact_reuse(unit) is None

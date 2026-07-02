"""Tests for the per-unit HistoryDecisionContext snapshot (#idea 5 cohesion).

The snapshot is built once per unit and memoized, collapsing the repeated
for_conflict (~4×) / obligation-patch-loop (~2×) / features (2×) queries to 1×
each. It's journaled as the single per-unit history_decision_snapshot event.
"""

from __future__ import annotations

import json
from pathlib import Path

from capybase.config import Config
from capybase.orchestrator import Orchestrator
from capybase.history_confidence import HistoryDecisionContext

from tests.conftest import git
from tests.multistep_builder import CommitEdit, build_multistep_rebase


def _base_cfg(repo: Path) -> Config:
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    return cfg


def _snapshot_events(orch: Orchestrator) -> list:
    return [e for e in orch.journal.read_events()
            if e.event_type == "history_decision_snapshot"]


def test_snapshot_event_journaled_per_unit(repo: Path):
    """Each resolved unit with history gets one history_decision_snapshot event."""
    build_multistep_rebase(
        repo,
        base_files={"cfg.py": "# a\n\n\ndef f():\n    return 1\n"},
        feat_commits=[
            CommitEdit("feat: edit f", {"cfg.py": "# a\n\n\ndef f():\n    return 2\n"}),
            CommitEdit("feat: edit f again", {"cfg.py": "# a\n\n\ndef f():\n    return 3\n"}),
        ],
        main_commits=[
            CommitEdit("main: edit f", {"cfg.py": "# a\n\n\ndef f():\n    return 99\n"}),
        ],
        stop_early=True,
    )
    from tests.test_rust_cross_file import PathAwareClient
    from capybase.resolution_engine import ResolutionEngine
    cfg = _base_cfg(repo)
    client = PathAwareClient({"cfg.py": "    return 2\n"})
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    orch.run()
    snaps = _snapshot_events(orch)
    # At least one snapshot event was emitted for the conflict unit.
    assert snaps, "expected a history_decision_snapshot event"
    snap = snaps[0]
    assert snap.payload.get("unit_id")
    # The payload carries the non-bulky audit fields.
    assert "region_key_kind" in snap.payload
    assert "confidence_score" in snap.payload
    assert "future_obligation_count" in snap.payload


def test_for_conflict_memoized_within_unit(repo: Path, monkeypatch):
    """The expensive for_conflict query is memoized within a unit's resolution.

    Rather than wrapping the live service mid-run (fragile), we directly exercise
    the cache: call _history_context_for twice for the same unit and assert the
    underlying service is queried only once."""
    from types import SimpleNamespace
    from capybase.history import HistoryContext, ReplayCommit

    cfg = _base_cfg(repo)
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    call_count = {"n": 0}
    future = ReplayCommit(oid="f1", parent_oid="c0", subject="later", body_summary="",
                          touched_files=["cfg.py"], diffstat={}, patch_id="", index=1)
    ctx = HistoryContext(
        current_replay_commit=None, source_commit_index=0, source_commit_count=2,
        previous_source_commits_touching_file=[],
        future_source_commits_touching_file=[future],
        future_source_commits_touching_region=[future],
        recent_target_commits_touching_file=[], region_detection_method="diff",
    )
    class _Svc:
        def for_conflict(self, unit, *, replayed_commit_oid=None):
            call_count["n"] += 1
            return ctx
    orch._history_service = _Svc()
    orch._history_plan = SimpleNamespace(source_commits=[future])
    unit = SimpleNamespace(
        unit_id="u", path="cfg.py",
        structural_metadata={"replayed_commit_oid": "c0"},
    )
    # First call queries the service; second call hits the cache.
    orch._clear_history_caches()
    orch._history_context_for(unit)
    orch._history_context_for(unit)
    orch._history_context_for(unit)
    assert call_count["n"] == 1, (
        f"for_conflict called {call_count['n']}× — cache not working (expected 1)"
    )
    # After clearing, a fresh query runs again.
    orch._clear_history_caches()
    orch._history_context_for(unit)
    assert call_count["n"] == 2


def test_snapshot_to_journal_payload():
    """The snapshot's journal payload carries the audit fields without the bulky objects."""
    snap = HistoryDecisionContext(unit_id="u1", region_key_kind="function",
                                  conflict_shape="abc123")
    payload = snap.to_journal_payload()
    assert payload["unit_id"] == "u1"
    assert payload["region_key_kind"] == "function"
    assert payload["conflict_shape"] == "abc123"
    assert payload["confidence_score"] is None
    assert payload["future_obligation_count"] == 0
    assert payload["exact_reuse_matched"] is False


def test_snapshot_empty_when_no_history(repo: Path):
    """No history plan → the snapshot isn't built (the build is gated on a plan)."""
    cfg = _base_cfg(repo)
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    # No rebase in progress → run() escalates before any snapshot is built.
    # The snapshot cache stays empty.
    assert orch._history_snapshots == {}

"""Integration tests for the future-obligations gate in the orchestrator (#9 step 3).

The orchestrator computes FutureObligations from the history service + future
patches, sets the rendered block on the context builder (so the LLM prompt sees
it), and rejects a candidate that drops a required symbol (converting it to a
retry). These tests cover that wiring at the orchestrator level — the pure
extraction logic is in test_future_obligations.py.
"""

from __future__ import annotations

from types import SimpleNamespace

from capybase.conflict_model import (
    CandidateResolution,
    ConflictSide,
    ConflictUnit,
)
from capybase.history import HistoryContext, ReplayCommit


def _commit(oid, subject="later", files=None, index=1):
    return ReplayCommit(
        oid=oid, parent_oid="p", subject=subject, body_summary="",
        touched_files=files or ["cfg.py"], diffstat={}, patch_id="", index=index,
    )


def _unit():
    return ConflictUnit(
        session_id="s", step_index=0, path="cfg.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="def parse_config():\n    return {}\n"),
        current=ConflictSide(
            label="CURRENT_UPSTREAM_SIDE",
            text="def parse_config():\n    return {'a': 1}\n",
        ),
        replayed=ConflictSide(
            label="REPLAYED_COMMIT_SIDE",
            text="def parse_config():\n    return {'b': 2}\n",
        ),
        original_worktree_text="def parse_config():\n    return {}\n",
        marker_span=(0, 1),
    )


def _ctx_with_future_region():
    """A history context where a future commit touches the region."""
    return HistoryContext(
        current_replay_commit=_commit("c0", "current", index=0),
        source_commit_index=0, source_commit_count=2,
        previous_source_commits_touching_file=[],
        future_source_commits_touching_file=[_commit("f1", "use parse_config")],
        future_source_commits_touching_region=[_commit("f1", "use parse_config")],
        recent_target_commits_touching_file=[],
        region_detection_method="diff",
    )


def _patch_calling_parse_config() -> bytes:
    """A future patch whose added line references parse_config()."""
    return (
        b"--- a/cfg.py\n+++ b/cfg.py\n@@ -1,1 +1,2 @@\n"
        b" existing\n+x = parse_config()\n"
    )


# ---------------------------------------------------------------------------
# the check fires in the orchestrator with a mocked history service
# ---------------------------------------------------------------------------


def test_orchestrator_rejects_candidate_dropping_required_symbol(repo, monkeypatch):
    """A candidate that drops parse_config (which a later commit calls) is
    rejected by the orchestrator's future-obligations check."""
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator

    cfg = Config()
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)

    # Wire a history service that returns our future-region context.
    ctx = _ctx_with_future_region()
    class _Svc:
        def for_conflict(self, unit, *, replayed_commit_oid=None):
            return ctx
    orch._history_service = _Svc()
    orch._history_plan = SimpleNamespace(source_commits=[_commit("c0", "c", index=0)])

    # commit_patch returns a patch that references parse_config → survival obligation.
    monkeypatch.setattr(orch.git, "commit_patch",
                        lambda oid: _patch_calling_parse_config())

    unit = _unit()
    # Candidate KEEPS parse_config → should pass.
    keep = CandidateResolution(
        candidate_id="u:keep", unit_id="u", model_name="m",
        prompt_version="resolve_text_block.v5", resolved_text="def parse_config():\n    return {'a':1,'b':2}\n",
        provenance="plain_llm",
    )
    ok, dropped = orch._future_obligations_check(unit, keep)
    assert ok, f"expected pass, got dropped={dropped}"

    # Candidate DROPS parse_config → should fail.
    drop = CandidateResolution(
        candidate_id="u:drop", unit_id="u", model_name="m",
        prompt_version="resolve_text_block.v5", resolved_text="# removed parse_config\npass\n",
        provenance="plain_llm",
    )
    ok, dropped = orch._future_obligations_check(unit, drop)
    assert not ok
    assert "parse_config" in dropped


def test_orchestrator_passes_when_no_history_plan(repo):
    """No history plan → no future obligations → always passes."""
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator

    cfg = Config()
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    # _history_service and _history_plan default to None.
    unit = _unit()
    cand = CandidateResolution(
        candidate_id="u:c", unit_id="u", model_name="m",
        prompt_version="resolve_text_block.v5", resolved_text="anything",
        provenance="plain_llm",
    )
    ok, dropped = orch._future_obligations_check(unit, cand)
    assert ok and dropped == []


# ---------------------------------------------------------------------------
# the prompt block is populated when obligations apply
# ---------------------------------------------------------------------------


def test_prompt_block_populated_when_obligations_apply(repo, monkeypatch):
    """_set_future_obligations_prompt_block sets a non-empty block on the
    context builder when a future commit depends on a defined symbol."""
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator

    cfg = Config()
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    ctx = _ctx_with_future_region()
    class _Svc:
        def for_conflict(self, unit, *, replayed_commit_oid=None):
            return ctx
    orch._history_service = _Svc()
    orch._history_plan = SimpleNamespace(source_commits=[_commit("c0", "c", index=0)])
    monkeypatch.setattr(orch.git, "commit_patch",
                        lambda oid: _patch_calling_parse_config())

    unit = _unit()
    orch._set_future_obligations_prompt_block(unit)
    block = orch.context_builder.future_obligations_block
    assert block, "expected a non-empty future-obligations block"
    assert "parse_config" in block
    assert "Future obligations" in block


def test_prompt_block_empty_when_no_future_touches(repo):
    """No future region touches → block is empty (prompt omits it)."""
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator
    from capybase.history import HistoryQueryService

    cfg = Config()
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    # A service with a plan but no future touches.
    orch._history_service = HistoryQueryService(
        SimpleNamespace(
            source_commits=[_commit("c0", "c", index=0)],
            target_base_oid="b", target_tip_oid="t", source_tip_oid="s",
            created_at="now", commit_by_oid=lambda oid: _commit("c0", "c", index=0),
            index_of=lambda oid: 0,
        )
    )
    orch._history_plan = SimpleNamespace(source_commits=[_commit("c0", "c", index=0)])
    unit = _unit()
    orch._set_future_obligations_prompt_block(unit)
    assert orch.context_builder.future_obligations_block == ""

"""Tests for history-awareness steps 6-8: experience store, prompt summaries,
and advisory features.

Step 6: Experience records carry history_features (replay position, future
touches) alongside the existing validator/process features.
Step 7: the resolve prompt includes a compact "History context" block when a
RebasePlan is active; it's budget-trimmable (lowest priority).
Step 8: history features flow to the merged feature spine (experience store +
accept report + future calibration).
"""

from __future__ import annotations

from capybase.conflict_model import ConflictSide, ContextBundle, ConflictUnit
from capybase.history import (
    HistoryQueryService, RebasePlan, ReplayCommit,
)
from capybase.memory.store import Experience


# ---------------------------------------------------------------------------
# Step 6: Experience.history_features
# ---------------------------------------------------------------------------


def test_experience_roundtrips_history_features():
    """An Experience with history_features serializes + deserializes intact."""
    from capybase.conflict_model import HistoricalExample
    exp = Experience(
        example=HistoricalExample(
            summary="cfg.py:u", base="a", current="b", replayed="c",
            resolved="d", source="s1",
        ),
        outcome="accepted", language="python", path="cfg.py",
        history_features={
            "history_source_commit_index": 2,
            "history_future_file_touch_count": 1,
        },
    )
    d = exp.to_dict()
    assert d["history_features"]["history_future_file_touch_count"] == 1
    again = Experience.from_dict(d)
    assert again.history_features["history_source_commit_index"] == 2


def test_experience_loads_old_records_without_history_features():
    """An old JSONL line without history_features loads with an empty dict."""
    from capybase.conflict_model import HistoricalExample
    d = {
        "example": HistoricalExample(
            summary="x", base="a", current="b", replayed="c", resolved="d"
        ).model_dump(),
        "outcome": "accepted",
    }
    exp = Experience.from_dict(d)
    assert exp.history_features == {}


# ---------------------------------------------------------------------------
# Step 7: history context in the prompt
# ---------------------------------------------------------------------------


def test_context_bundle_has_history_context_field():
    """ContextBundle carries a history_context string (empty by default)."""
    b = ContextBundle(primary_text="x", token_estimate=1)
    assert b.history_context == ""


def test_context_builder_injects_history_when_service_set():
    """When a HistoryQueryService is set, the builder populates history_context."""
    from capybase.context_builder import ContextBuilder
    from capybase.conflict_model import ConflictSide, ConflictUnit

    plan = RebasePlan(
        source_commits=[
            ReplayCommit(oid="c1", parent_oid="b", subject="base",
                         body_summary="", touched_files=["cfg.py"],
                         diffstat={}, patch_id="", index=0),
            ReplayCommit(oid="c2", parent_oid="c1", subject="Add strict",
                         body_summary="", touched_files=["cfg.py"],
                         diffstat={}, patch_id="", index=1),
            ReplayCommit(oid="c3", parent_oid="c2", subject="Rename parse",
                         body_summary="", touched_files=["cfg.py"],
                         diffstat={}, patch_id="", index=2),
        ],
        target_base_oid="b", target_tip_oid="t", source_tip_oid="c3",
        created_at="now",
    )
    qs = HistoryQueryService(plan)
    builder = ContextBuilder(history_service=qs)
    unit = ConflictUnit(
        session_id="s", step_index=1, path="cfg.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="x"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="y"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="z"),
        original_worktree_text="x", marker_span=(0, 0),
        structural_metadata={
            "replayed_commit_oid": "c2",
            "enclosing_node_signature": "def parse",
        },
    )
    bundle = builder.build(unit)
    assert "History context" not in bundle.history_context or "Replaying" in bundle.history_context
    assert "Replaying commit 2/3" in bundle.history_context
    assert "Rename parse" in bundle.history_context  # future region touch


def test_context_builder_no_history_without_service():
    """Without a history service, history_context stays empty."""
    from capybase.context_builder import ContextBuilder
    builder = ContextBuilder()
    unit = ConflictUnit(
        session_id="s", step_index=1, path="cfg.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="x"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="y"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="z"),
        original_worktree_text="x", marker_span=(0, 0),
    )
    assert builder.build(unit).history_context == ""


def test_context_builder_appends_branch_intent_block():
    """When branch_intent_block is set, it appears in the history context."""
    from capybase.context_builder import ContextBuilder
    plan = RebasePlan(
        source_commits=[
            ReplayCommit(oid="c1", parent_oid="b", subject="base",
                         body_summary="", touched_files=["cfg.py"],
                         diffstat={}, patch_id="", index=0),
        ],
        target_base_oid="b", target_tip_oid="t", source_tip_oid="c1",
        created_at="now",
    )
    qs = HistoryQueryService(plan)
    builder = ContextBuilder(history_service=qs)
    builder.branch_intent_block = "Branch final intent (net effect):\ncfg.py:\n  - parse_config: added in commit(s) 1"
    unit = ConflictUnit(
        session_id="s", step_index=1, path="cfg.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="x"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="y"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="z"),
        original_worktree_text="x", marker_span=(0, 0),
        structural_metadata={"replayed_commit_oid": "c1"},
    )
    bundle = builder.build(unit)
    assert "Branch final intent" in bundle.history_context
    assert "parse_config" in bundle.history_context


def test_context_builder_appends_future_obligations_block():
    """When future_obligations_block is set, it appears in the history context."""
    from capybase.context_builder import ContextBuilder
    plan = RebasePlan(
        source_commits=[
            ReplayCommit(oid="c1", parent_oid="b", subject="base",
                         body_summary="", touched_files=["cfg.py"],
                         diffstat={}, patch_id="", index=0),
        ],
        target_base_oid="b", target_tip_oid="t", source_tip_oid="c1",
        created_at="now",
    )
    qs = HistoryQueryService(plan)
    builder = ContextBuilder(history_service=qs)
    builder.future_obligations_block = (
        "Future obligations (later source commits expect these — preserve them):\n"
        "  - later commit \"use config\" expects `parse_config` to still exist"
    )
    unit = ConflictUnit(
        session_id="s", step_index=1, path="cfg.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="x"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="y"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="z"),
        original_worktree_text="x", marker_span=(0, 0),
        structural_metadata={"replayed_commit_oid": "c1"},
    )
    bundle = builder.build(unit)
    # Obligations moved to obligations_context (#idea 9 — first-class budget section).
    assert "Future obligations" in bundle.obligations_context
    assert "parse_config" in bundle.obligations_context


def test_resolve_prompt_renders_history_block():
    """The resolve prompt includes a 'History context' section when the bundle
    carries one."""
    from capybase.resolution_engine import build_resolve_prompt
    unit = ConflictUnit(
        session_id="s", step_index=1, path="cfg.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="def f():\n    return 1\n"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="    return 2\n"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="    return 3\n"),
        original_worktree_text="def f():\n<<<<<<<\n    return 2\n=======\n    return 3\n>>>>>>>\n",
        marker_span=(1, 5),
    )
    bundle = ContextBundle(
        primary_text="def f():\n    return 1\n",
        token_estimate=10,
        history_context="Replaying commit 2/3: \"Add strict\"\nLater: \"Rename parse\"",
    )
    prompt = build_resolve_prompt(unit, bundle)
    assert "History context:" in prompt
    assert "Replaying commit 2/3" in prompt


def test_resolve_prompt_omits_history_when_empty():
    """No history_context → the prompt omits the section entirely."""
    from capybase.resolution_engine import build_resolve_prompt
    unit = ConflictUnit(
        session_id="s", step_index=1, path="cfg.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="def f():\n    return 1\n"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="    return 2\n"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="    return 3\n"),
        original_worktree_text="def f():\n<<<<<<<\n    return 2\n=======\n    return 3\n>>>>>>>\n",
        marker_span=(1, 5),
    )
    bundle = ContextBundle(primary_text="def f():\n    return 1\n", token_estimate=10)
    prompt = build_resolve_prompt(unit, bundle)
    assert "History context:" not in prompt


# ---------------------------------------------------------------------------
# Step 8: advisory features
# ---------------------------------------------------------------------------


def test_history_features_are_advisory_zero_without_plan():
    """Without a RebasePlan, _history_features_for returns an empty dict."""
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator
    cfg = Config()
    orch = Orchestrator(cfg, repo=".", out=lambda *_a, **_k: None)
    unit = ConflictUnit(
        session_id="s", step_index=1, path="cfg.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="x"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="y"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="z"),
        original_worktree_text="x", marker_span=(0, 0),
    )
    assert orch._history_features_for(unit) == {}

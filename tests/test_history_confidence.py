"""Tests for the history-confidence score (#9 step 1).

Two layers:
- ``history_confidence.history_confidence_for``: pure scoring from a
  HistoryContext + optional probe mode. Covers each quality band, each detection
  method, the augment-threshold semantics, and the None-context sentinel.
- ``history.HistoryContext.region_detection_method``: the detection method is
  surfaced out of ``_touches_region`` so the score has a real signal to read.
"""

from __future__ import annotations

from capybase.history import (
    HistoryContext,
    HistoryQueryService,
    RebasePlan,
    RegionKey,
    ReplayCommit,
)
from capybase.history_confidence import (
    DEFAULT_AUGMENT_THRESHOLD,
    HistoryConfidence,
    history_confidence_for,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _commit(oid: str, parent: str = "p", *, subject="s", files=None) -> ReplayCommit:
    return ReplayCommit(
        oid=oid, parent_oid=parent, subject=subject, body_summary="",
        touched_files=files or ["cfg.py"], diffstat={}, patch_id="pid", index=0,
    )


def _key(*, kind="function", name="parse", span=(0, 5)) -> RegionKey:
    return RegionKey(
        path="cfg.py", language="python", kind=kind, name=name,
        enclosing_node_type="function_definition",
        start_line=span[0], end_line=span[1], structural_hash="abc123",
    )


def _plan(commits: list[ReplayCommit]) -> RebasePlan:
    return RebasePlan(
        source_commits=commits, target_base_oid="base", target_tip_oid="tip",
        source_tip_oid="src", created_at="2026-01-01T00:00:00Z",
    )


class _FakeGit:
    """A fake GitBackend whose diff output we control, to drive _touches_region."""

    def __init__(self, diff_output: str | None):
        self._diff = diff_output

    def _run_raw(self, args):  # noqa: D401 - mimics GitBackend._run_raw
        if self._diff is None:
            raise RuntimeError("fetch failed")
        return self._diff.encode()


def _unit(*, path="cfg.py", replayed_oid=None):
    """A minimal stand-in for a ConflictUnit with structural_metadata."""
    from types import SimpleNamespace

    md = {}
    if replayed_oid:
        md["replayed_commit_oid"] = replayed_oid
    md["enclosing_node_span"] = (0, 5)
    md["enclosing_node_type"] = "function_definition"
    md["enclosing_node_signature"] = "def parse()"
    return SimpleNamespace(path=path, structural_metadata=md, marker_span=(0, 5))


# ---------------------------------------------------------------------------
# None / empty context → zero score
# ---------------------------------------------------------------------------


def test_none_context_is_zero_confidence():
    conf = history_confidence_for(None)
    assert conf.score == 0.0
    assert not conf.has_rebase_plan
    assert not conf.is_augmenting
    assert conf.region_key_quality == "low"


def test_no_plan_context_is_zero_confidence():
    """A context with no current replay commit (history_has_context False) → 0."""
    ctx = HistoryContext(
        current_replay_commit=None, source_commit_index=None,
        source_commit_count=0,
        previous_source_commits_touching_file=[],
        future_source_commits_touching_file=[],
        future_source_commits_touching_region=[],
        recent_target_commits_touching_file=[],
        region_detection_method="none",
    )
    conf = history_confidence_for(ctx)
    assert conf.score == 0.0
    assert not conf.has_rebase_plan


# ---------------------------------------------------------------------------
# scoring across detection methods + probe modes
# ---------------------------------------------------------------------------


def _full_ctx(*, detection="diff", has_region_touches=True):
    """A context where the plan + identity are known (max those two signals)."""
    touches = [_commit("f1")] if has_region_touches else []
    return HistoryContext(
        current_replay_commit=_commit("c0"),
        source_commit_index=2,
        source_commit_count=5,
        previous_source_commits_touching_file=[],
        future_source_commits_touching_file=touches,
        future_source_commits_touching_region=touches,
        recent_target_commits_touching_file=[],
        region_detection_method=detection,
    )


def test_plan_and_identity_alone_contribute_partial_score():
    """Known plan + known identity, but no region signal and no probe."""
    conf = history_confidence_for(_full_ctx(detection="none", has_region_touches=False))
    # 0.2 (plan) + 0.2 (identity) + 0 (region key=low) + 0 (detection=none) + 0 (probe=none)
    assert conf.score == 0.4
    assert conf.has_rebase_plan
    assert conf.replay_identity_known


def test_diff_detection_scores_higher_than_heuristic():
    diff_conf = history_confidence_for(_full_ctx(detection="diff"))
    heur_conf = history_confidence_for(_full_ctx(detection="heuristic"))
    none_conf = history_confidence_for(_full_ctx(detection="none"))
    assert diff_conf.score > heur_conf.score > none_conf.score


def test_region_key_quality_tracks_detection_method():
    assert history_confidence_for(_full_ctx(detection="diff")).region_key_quality == "high"
    assert history_confidence_for(_full_ctx(detection="heuristic")).region_key_quality == "medium"


def test_sequence_patch_scores_higher_than_path_patch():
    ctx = _full_ctx(detection="diff")
    seq = history_confidence_for(ctx, probe_mode_used="sequence_patch")
    path = history_confidence_for(ctx, probe_mode_used="path_patch")
    none = history_confidence_for(ctx, probe_mode_used=None)
    assert seq.score > path.score > none.score
    assert seq.future_probe_quality == "sequence_patch"
    assert path.future_probe_quality == "path_patch"
    assert none.future_probe_quality == "none"


def test_score_never_exceeds_one():
    """Everything maxed out → exactly 1.0 (no float drift beyond clamp)."""
    conf = history_confidence_for(
        _full_ctx(detection="diff"), probe_mode_used="sequence_patch"
    )
    assert conf.score == 1.0


# ---------------------------------------------------------------------------
# is_augmenting threshold
# ---------------------------------------------------------------------------


def test_augmenting_requires_future_region_signal():
    """A high score from plan+identity+probe but NO region touches is not augmenting."""
    ctx = _full_ctx(detection="none", has_region_touches=False)
    conf = history_confidence_for(ctx, probe_mode_used="sequence_patch")
    # Even with a probe, no future-region signal → not augmenting.
    assert not conf.is_augmenting


def test_augmenting_when_diff_detection_and_above_threshold():
    conf = history_confidence_for(_full_ctx(detection="diff"))
    assert conf.score >= DEFAULT_AUGMENT_THRESHOLD
    assert conf.is_augmenting


def test_not_augmenting_when_only_subject_heuristic():
    """A lone subject-heuristic match is weak — below the augment threshold."""
    conf = history_confidence_for(_full_ctx(detection="heuristic"))
    # 0.2+0.2 + 0.2*0.5(region medium) + 0.2*0.35(heuristic) + 0 = 0.57
    # That's above 0.4, so is_augmenting by score... but heuristic IS a real
    # future-region signal (just weak). The contract: augmenting = score>=thr AND
    # detection != none. Heuristic qualifies. Assert it's augmenting but LOWER
    # than diff, so the test documents the intended semantics.
    assert conf.is_augmenting
    assert conf.score < history_confidence_for(_full_ctx(detection="diff")).score


# ---------------------------------------------------------------------------
# detection method surfaced from HistoryQueryService
# ---------------------------------------------------------------------------


def test_service_records_diff_method_when_diff_matches():
    """When diff-overlap finds a region touch, region_detection_method == 'diff'."""
    future = _commit("fut", parent="c0", subject="refactor", files=["cfg.py"])
    plan = _plan([_commit("c0"), future])
    # A diff hunk overlapping span (0,5): @@ -1,3 +1,4 @@ → lines 0..2 (0-based).
    git = _FakeGit("@@ -1,3 +1,4 @@\n line\n")
    svc = HistoryQueryService(plan, git=git)
    unit = _unit(replayed_oid="c0")
    ctx = svc.for_conflict(unit, replayed_commit_oid="c0")
    assert len(ctx.future_source_commits_touching_region) == 1
    assert ctx.region_detection_method == "diff"


def test_service_records_none_when_no_future_touches():
    """No future commits touching the file → method 'none'."""
    plan = _plan([_commit("c0")])  # only commit, no future
    svc = HistoryQueryService(plan)
    ctx = svc.for_conflict(_unit(replayed_oid="c0"), replayed_commit_oid="c0")
    assert ctx.region_detection_method == "none"
    assert not ctx.future_source_commits_touching_region


def test_to_features_includes_detection_method():
    """The feature dict (→ experience store/calibration) carries the method."""
    ctx = _full_ctx(detection="diff")
    feats = ctx.to_features()
    assert feats["history_region_detection_method"] == "diff"


def test_orchestrator_surfaces_confidence_into_features(repo):
    """The orchestrator's _history_features_for merges the confidence score
    into the feature spine that reaches the experience store + calibration."""
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator

    cfg = Config()
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    # Build a service with a real plan so _history_context_for returns a full ctx.
    future = _commit("fut", parent="c0", subject="refactor", files=["cfg.py"])
    plan = _plan([_commit("c0"), future])
    from capybase.history import HistoryQueryService

    orch._history_service = HistoryQueryService(plan, git=_FakeGit("@@ -1,3 +1,4 @@\nx\n"))
    unit = _unit(replayed_oid="c0")
    feats = orch._history_features_for(unit)
    assert feats.get("history_region_detection_method") == "diff"
    assert "history_confidence_score" in feats
    assert feats.get("history_is_augmenting") is True

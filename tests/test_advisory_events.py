"""Tests for advisory-failure visibility (#idea 4).

The history layer deliberately catches exceptions and degrades to no-op (rebase
safety). This phase makes those silent failures OBSERVABLE: each advisory
subsystem emits a distinct journal event tagged ``{"advisory": True}``, surfaced
in the dry-run report + escalation review bundle (not the terminal). These tests
force each subsystem to fail and assert the distinct event appears.
"""

from __future__ import annotations

import json
from pathlib import Path

from capybase.config import Config
from capybase.orchestrator import Orchestrator

from tests.conftest import git


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _advisory_events(orch: Orchestrator) -> list:
    """All advisory journal events (payload tagged advisory=True)."""
    return [
        e for e in orch.journal.read_events()
        if getattr(e.payload, "get", lambda *_: None)("advisory")
    ]


def _advisory_types(orch: Orchestrator) -> set[str]:
    return {e.event_type for e in _advisory_events(orch)}


def _base_cfg(repo: Path) -> Config:
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    return cfg


# ---------------------------------------------------------------------------
# emit_advisory helper (the seam)
# ---------------------------------------------------------------------------


def test_emit_advisory_tags_payload(repo: Path):
    """emit_advisory produces a journal event tagged advisory=True with a reason."""
    cfg = _base_cfg(repo)
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    orch.journal.emit_advisory(
        "history_unavailable", "test reason", path="cfg.py", unit_id="u",
    )
    adv = _advisory_events(orch)
    assert len(adv) == 1
    assert adv[0].event_type == "history_unavailable"
    assert adv[0].payload["advisory"] is True
    assert adv[0].payload["reason"] == "test reason"
    assert adv[0].path == "cfg.py"
    assert adv[0].unit_id == "u"


# ---------------------------------------------------------------------------
# per-subsystem: a failure emits the distinct advisory event
# ---------------------------------------------------------------------------


def test_rebase_plan_build_failure_emits_history_unavailable(repo: Path, monkeypatch):
    """A failure inside _build_rebase_plan emits history_unavailable (was silent)."""
    cfg = _base_cfg(repo)
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    # merge_base must succeed (else the early-return at the top skips the try);
    # replayed_commit_sequence raises to trigger the advisory.
    monkeypatch.setattr(orch.git, "merge_base", lambda a, b: "fakebase")
    def boom(*a, **k):
        raise RuntimeError("synthetic plan-build failure")
    monkeypatch.setattr(orch.git, "replayed_commit_sequence", boom)
    orch._history_plan = orch._build_rebase_plan("aaa", "bbb")
    assert orch._history_plan is None  # degraded
    assert "history_unavailable" in _advisory_types(orch)


def test_future_obligations_failure_emits_advisory(repo: Path, monkeypatch):
    """A failure inside obligation extraction emits future_obligations_failed."""
    from capybase.conflict_model import ConflictSide, ConflictUnit
    from types import SimpleNamespace

    cfg = _base_cfg(repo)
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    # Wire a fake history service + plan so _future_obligations_for runs.
    unit = ConflictUnit(
        session_id="s", step_index=0, path="cfg.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="def f():\n    pass\n"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="def f():\n    return 1\n"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="def f():\n    return 2\n"),
        original_worktree_text="", marker_span=(0, 0),
        structural_metadata={"replayed_commit_oid": "c0"},
    )
    from capybase.history import HistoryContext, ReplayCommit

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
            return ctx
    orch._history_service = _Svc()
    orch._history_plan = SimpleNamespace(source_commits=[future])
    # Force the obligation EXTRACTION to raise (the outer try/except in
    # _future_obligations_for catches it). The per-commit patch fetch has its own
    # inner try/except that degrades to b"" (a separate, narrower concern).
    import capybase.future_obligations as fom
    monkeypatch.setattr(
        fom, "extract_future_obligations",
        lambda **k: (_ for _ in ()).throw(RuntimeError("synthetic extraction failure")),
    )
    orch._future_obligations_for(unit)
    assert "future_obligations_failed" in _advisory_types(orch)


def test_branch_intent_failure_emits_advisory(repo: Path, monkeypatch):
    """A failure inside branch-intent build emits branch_intent_failed."""
    from types import SimpleNamespace

    cfg = _base_cfg(repo)
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    plan = SimpleNamespace(source_commits=[
        SimpleNamespace(oid="c1", subject="s", touched_files=["cfg.py"])
    ])
    # The per-commit patch fetch degrades gracefully (inner try/except → b"");
    # force build_branch_intent ITSELF to raise (the outer try/except catches it).
    import capybase.branch_intent as bim
    monkeypatch.setattr(
        bim, "build_branch_intent",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("synthetic intent failure")),
    )
    orch._build_branch_intent(plan)
    assert "branch_intent_failed" in _advisory_types(orch)


def test_exact_reuse_failure_emits_advisory_not_mislabeled(repo: Path, tmp_path, monkeypatch):
    """An exception inside find_exact_reuse emits exact_reuse_failed (not the
    mislabeled 'no exact match' skip)."""
    from capybase.conflict_model import ConflictSide, ConflictUnit
    from capybase.memory.store import ExperienceStore

    cfg = _base_cfg(repo)
    cfg.memory.enabled = True
    cfg.future.enable_rag = True
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    store = ExperienceStore(tmp_path / "exp.jsonl")
    orch.memory_store = store
    unit = ConflictUnit(
        session_id="s", step_index=0, path="cfg.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="x"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="y"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="z"),
        original_worktree_text="x", marker_span=(0, 0),
    )
    # Break find_exact_reuse by making the shape comparison raise.
    import capybase.exact_reuse as erm
    monkeypatch.setattr(erm, "conflict_shape_hash",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("synthetic")))
    orch._try_exact_reuse(unit)
    types = _advisory_types(orch)
    assert "exact_reuse_failed" in types, (
        f"expected exact_reuse_failed for the internal exception; got {types}"
    )


def test_future_probe_unavailable_emits_on_read_failure(repo: Path, monkeypatch):
    """A failure reading the resolved file for the probe emits future_probe_unavailable."""
    from types import SimpleNamespace
    from capybase.orchestrator import StepResult
    from capybase.policy_strictness import StrictnessPolicy
    from capybase.history import HistoryContext, ReplayCommit

    cfg = _base_cfg(repo)
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    orch.strictness = StrictnessPolicy(mode="ci")
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
            return ctx
    orch._history_service = _Svc()
    orch._history_plan = SimpleNamespace(source_commits=[future])
    unit = SimpleNamespace(path="cfg.py", unit_id="u",
                           structural_metadata={"replayed_commit_oid": "c0"})
    outcome = SimpleNamespace(unit=unit, accepted=SimpleNamespace(provenance="plain_llm"),
                              validation=None)
    result = StepResult(step_index=0, units_by_path={}, skipped=[],
                        outcomes=[outcome], escalated=False, reason="",
                        tests_passed=None, continued=False)
    # read_worktree_file raises a non-FileNotFoundError → the probe-unavailable path.
    def boom(p):
        raise OSError("synthetic read failure")
    monkeypatch.setattr(orch.git, "read_worktree_file", boom)
    orch._run_future_apply_probe(result)
    assert "future_probe_unavailable" in _advisory_types(orch)


# ---------------------------------------------------------------------------
# dry-run surfaces advisory events
# ---------------------------------------------------------------------------


def test_dryrun_summary_history_includes_advisory_section():
    """summary_history() reports advisory event counts when present."""
    from capybase.dryrun import RehearsalReport, RehearsalStep

    report = RehearsalReport(
        would_succeed=True, target="main", head_before="aaa", head_after="bbb",
        session_id="s", history_active=True,
    )
    report.steps = [RehearsalStep(step=1, accepted=True)]
    report.advisory_counts = {"history_unavailable": 1, "future_obligations_failed": 2}
    out = report.summary_history()
    assert "3 advisory event(s)" in out
    assert "history_unavailable" in out
    assert "future_obligations_failed" in out


def test_dryrun_summarize_journal_counts_advisory_events(tmp_path):
    """_summarize_journal counts advisory-tagged events into advisory_counts."""
    from capybase.dryrun import RehearsalReport, _summarize_journal

    report = RehearsalReport()
    j = tmp_path / "j.jsonl"
    j.write_text("\n".join([
        json.dumps({"event_type": "step_started", "payload": {}, "step_index": 1}),
        json.dumps({"event_type": "history_unavailable",
                    "payload": {"advisory": True, "reason": "plan failed"},
                    "step_index": 1}),
        json.dumps({"event_type": "future_obligations_failed",
                    "payload": {"advisory": True, "reason": "extract failed"},
                    "step_index": 1}),
    ]) + "\n")
    _summarize_journal(j, report)
    assert report.advisory_counts.get("history_unavailable") == 1
    assert report.advisory_counts.get("future_obligations_failed") == 1


def test_review_bundle_renders_advisories_section(tmp_path):
    """write_review_bundle includes a ## advisories section when given advisories."""
    from capybase.escalation import write_review_bundle
    from capybase.session import SessionPaths

    paths = SessionPaths("test-bundle", repo_root=str(tmp_path))
    paths.mkdirs()
    out = write_review_bundle(
        paths, reason="escalated",
        advisories=["history_unavailable: plan failed",
                    "future_obligations_failed: extract failed"],
    )
    text = out.read_text()
    assert "## advisories" in text
    assert "history_unavailable" in text
    assert "future_obligations_failed" in text


def test_review_bundle_omits_advisories_when_none(tmp_path):
    """No advisories kwarg → no advisories section (the common healthy case)."""
    from capybase.escalation import write_review_bundle
    from capybase.session import SessionPaths

    paths = SessionPaths("test-bundle2", repo_root=str(tmp_path))
    paths.mkdirs()
    out = write_review_bundle(paths, reason="escalated")
    assert "## advisories" not in out.read_text()

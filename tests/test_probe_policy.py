"""Tests for the probe-mode policy (#9 step 2).

The orchestrator's ``_run_future_apply_probe`` now selects the probe mode by
strictness:
- strict modes (ci/unattended) → ``sequence_patch`` (applies intervening same-
  path commits first), and a failure escalates.
- non-strict modes (interactive/dry_run) → ``path_patch`` (advisory only).

This file covers the mode SELECTION + intervening-commit derivation + the
escalation gate. The probe's own mechanics are covered in test_future_apply_probe.
"""

from __future__ import annotations

from types import SimpleNamespace

from capybase.history import HistoryContext, ReplayCommit
from capybase.policy_strictness import StrictnessPolicy


def _commit(oid, parent, subject, files, index):
    return ReplayCommit(
        oid=oid, parent_oid=parent, subject=subject, body_summary="",
        touched_files=files, diffstat={}, patch_id="", index=index,
    )


def _ctx(region, file_commits):
    return HistoryContext(
        current_replay_commit=_commit("c0", "p", "current", ["cfg.py"], 0),
        source_commit_index=0, source_commit_count=3,
        previous_source_commits_touching_file=[],
        future_source_commits_touching_file=file_commits,
        future_source_commits_touching_region=region,
        recent_target_commits_touching_file=[],
        region_detection_method="diff",
    )


# ---------------------------------------------------------------------------
# mode selection driven by strictness
# ---------------------------------------------------------------------------


def _capture_probe_call(repo, *, policy_mode, monkeypatch):
    """Run _run_future_apply_probe against a fake StepResult and capture the
    mode + intervening_commits passed to future_apply_probe."""
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator, StepResult

    cfg = Config()
    cfg.tests.required = False
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    orch.strictness = StrictnessPolicy(mode=policy_mode)

    captured: dict = {}

    def fake_probe(git, *, resolved_path, resolved_content, future_commits,
                   max_probes=1, mode="path_patch", intervening_commits=None):
        from capybase.history import FutureApplyResult
        captured["mode"] = mode
        captured["intervening_count"] = len(intervening_commits or [])
        captured["future_count"] = len(future_commits)
        return FutureApplyResult(
            probed=True, applies=True,
            future_commit_subject=future_commits[0].subject if future_commits else "",
            reason="ok",
        )

    monkeypatch.setattr("capybase.history.future_apply_probe", fake_probe)

    # Build a StepResult with one accepted outcome whose unit triggers the probe.
    region = [_commit("fut2", "fut1", "rename", ["cfg.py"], 2)]
    file_commits = [
        _commit("fut1", "c0", "intermediate edit", ["cfg.py"], 1),
        region[0],
    ]
    ctx = _ctx(region, file_commits)
    unit = SimpleNamespace(
        path="cfg.py", unit_id="u",
        structural_metadata={"replayed_commit_oid": "c0"},
    )
    outcome = SimpleNamespace(
        unit=unit, accepted=SimpleNamespace(provenance="plain_llm"),
        validation=None,
    )
    result = StepResult(step_index=0, units_by_path={}, skipped=[],
                       outcomes=[outcome], escalated=False, reason="",
                       tests_passed=None, continued=False)

    # Wire the history service so _history_context_for returns our ctx.
    class _Svc:
        def for_conflict(self, unit, *, replayed_commit_oid=None):
            return ctx
    orch._history_service = _Svc()
    orch._history_plan = SimpleNamespace(source_commits=[_commit("c0", "p", "c", ["cfg.py"], 0)])

    # read_worktree_file must return some bytes.
    monkeypatch.setattr(orch.git, "read_worktree_file", lambda p: b"resolved")

    orch._run_future_apply_probe(result)
    return captured


def test_strict_mode_uses_sequence_patch(repo, monkeypatch):
    """ci/unattended modes select sequence_patch for the probe."""
    captured = _capture_probe_call(repo, policy_mode="ci", monkeypatch=monkeypatch)
    assert captured["mode"] == "sequence_patch"


def test_unattended_mode_uses_sequence_patch(repo, monkeypatch):
    captured = _capture_probe_call(repo, policy_mode="unattended", monkeypatch=monkeypatch)
    assert captured["mode"] == "sequence_patch"


def test_interactive_mode_uses_path_patch(repo, monkeypatch):
    """interactive/dry_run modes select the cheaper path_patch (advisory)."""
    captured = _capture_probe_call(repo, policy_mode="interactive", monkeypatch=monkeypatch)
    assert captured["mode"] == "path_patch"


def test_dry_run_mode_uses_path_patch(repo, monkeypatch):
    captured = _capture_probe_call(repo, policy_mode="dry_run", monkeypatch=monkeypatch)
    assert captured["mode"] == "path_patch"


# ---------------------------------------------------------------------------
# intervening-commit derivation
# ---------------------------------------------------------------------------


def test_intervening_commits_derived_for_sequence_mode(repo, monkeypatch):
    """In sequence mode, the same-path file-touching commits BEFORE the probed
    region commit are passed as intervening (so the probe state reflects them)."""
    captured = _capture_probe_call(repo, policy_mode="unattended", monkeypatch=monkeypatch)
    # file_commits = [fut1 (intermediate), fut2 (probed region)].
    # intervening = [fut1] (the one before fut2).
    assert captured["mode"] == "sequence_patch"
    assert captured["intervening_count"] == 1


def test_no_intervending_when_region_commit_is_first(repo, monkeypatch):
    """When the probed region commit is the first file-touching commit, there
    are no intervening commits to apply."""
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator, StepResult
    from capybase.policy_strictness import StrictnessPolicy

    cfg = Config()
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    orch.strictness = StrictnessPolicy(mode="unattended")

    captured: dict = {}
    def fake_probe(git, *, resolved_path, resolved_content, future_commits,
                   max_probes=1, mode="path_patch", intervening_commits=None):
        from capybase.history import FutureApplyResult
        captured["intervening_count"] = len(intervening_commits or [])
        return FutureApplyResult(probed=True, applies=True,
                                 future_commit_subject="x", reason="ok")
    monkeypatch.setattr("capybase.history.future_apply_probe", fake_probe)

    region = [_commit("fut1", "c0", "rename", ["cfg.py"], 1)]
    # No intermediate file commit before the region commit.
    ctx = _ctx(region, region)
    unit = SimpleNamespace(path="cfg.py", unit_id="u",
                           structural_metadata={"replayed_commit_oid": "c0"})
    outcome = SimpleNamespace(unit=unit, accepted=SimpleNamespace(provenance="plain_llm"),
                              validation=None)
    result = StepResult(step_index=0, units_by_path={}, skipped=[],
                       outcomes=[outcome], escalated=False, reason="",
                       tests_passed=None, continued=False)
    class _Svc:
        def for_conflict(self, unit, *, replayed_commit_oid=None):
            return ctx
    orch._history_service = _Svc()
    orch._history_plan = SimpleNamespace(source_commits=[_commit("c0", "p", "c", ["cfg.py"], 0)])
    monkeypatch.setattr(orch.git, "read_worktree_file", lambda p: b"resolved")
    orch._run_future_apply_probe(result)
    assert captured["intervening_count"] == 0


# ---------------------------------------------------------------------------
# escalation gate reflects the mode
# ---------------------------------------------------------------------------


def test_failed_probe_escalates_in_strict_mode_with_mode_in_reason(repo, monkeypatch):
    """A failed probe in ci/unattended escalates, and the reason names the mode."""
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator, StepResult
    from capybase.policy_strictness import StrictnessPolicy

    cfg = Config()
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    orch.strictness = StrictnessPolicy(mode="ci")

    def fail_probe(git, *, resolved_path, resolved_content, future_commits,
                   max_probes=1, mode="path_patch", intervening_commits=None):
        from capybase.history import FutureApplyResult
        return FutureApplyResult(probed=True, applies=False,
                                 future_commit_subject="rename",
                                 reason="patch context mismatch")
    monkeypatch.setattr("capybase.history.future_apply_probe", fail_probe)

    region = [_commit("fut1", "c0", "rename", ["cfg.py"], 1)]
    ctx = _ctx(region, region)
    unit = SimpleNamespace(path="cfg.py", unit_id="u",
                           structural_metadata={"replayed_commit_oid": "c0"})
    outcome = SimpleNamespace(unit=unit, accepted=SimpleNamespace(provenance="plain_llm"),
                              validation=None)
    result = StepResult(step_index=0, units_by_path={}, skipped=[],
                       outcomes=[outcome], escalated=False, reason="",
                       tests_passed=None, continued=False)
    class _Svc:
        def for_conflict(self, unit, *, replayed_commit_oid=None):
            return ctx
    orch._history_service = _Svc()
    orch._history_plan = SimpleNamespace(source_commits=[_commit("c0", "p", "c", ["cfg.py"], 0)])
    monkeypatch.setattr(orch.git, "read_worktree_file", lambda p: b"resolved")
    orch._run_future_apply_probe(result)
    assert result.escalated
    assert "sequence_patch" in result.reason


def test_failed_probe_does_not_escalate_in_interactive_mode(repo, monkeypatch):
    """A failed probe in interactive mode journals but does NOT escalate."""
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator, StepResult
    from capybase.policy_strictness import StrictnessPolicy

    cfg = Config()
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    orch.strictness = StrictnessPolicy(mode="interactive")

    def fail_probe(git, *, resolved_path, resolved_content, future_commits,
                   max_probes=1, mode="path_patch", intervening_commits=None):
        from capybase.history import FutureApplyResult
        return FutureApplyResult(probed=True, applies=False,
                                 future_commit_subject="rename",
                                 reason="patch context mismatch")
    monkeypatch.setattr("capybase.history.future_apply_probe", fail_probe)

    region = [_commit("fut1", "c0", "rename", ["cfg.py"], 1)]
    ctx = _ctx(region, region)
    unit = SimpleNamespace(path="cfg.py", unit_id="u",
                           structural_metadata={"replayed_commit_oid": "c0"})
    outcome = SimpleNamespace(unit=unit, accepted=SimpleNamespace(provenance="plain_llm"),
                              validation=None)
    result = StepResult(step_index=0, units_by_path={}, skipped=[],
                       outcomes=[outcome], escalated=False, reason="",
                       tests_passed=None, continued=False)
    class _Svc:
        def for_conflict(self, unit, *, replayed_commit_oid=None):
            return ctx
    orch._history_service = _Svc()
    orch._history_plan = SimpleNamespace(source_commits=[_commit("c0", "p", "c", ["cfg.py"], 0)])
    monkeypatch.setattr(orch.git, "read_worktree_file", lambda p: b"resolved")
    orch._run_future_apply_probe(result)
    assert not result.escalated

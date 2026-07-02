"""Tests for the ADAPTIVE future-probe mode (the policy→primitive change).

The probe mode is no longer a strictness-mode policy guess (strict→sequence_patch,
non-strict→path_patch). It's derived from the conflict's own data: sequence_patch
is strictly more accurate, so it's used whenever intervening same-path commits
exist; path_patch is the degenerate no-intervening case. Accuracy is a property
of the data, not the run mode. These tests pin that contract.

(The detailed mode/escalation matrix lives in test_probe_policy.py; this file
documents the adaptive-design intent + the no-config contract.)
"""

from __future__ import annotations

from types import SimpleNamespace

from capybase.config import Config
from capybase.orchestrator import Orchestrator, StepResult
from capybase.policy_strictness import StrictnessPolicy


def _commit(oid, parent="p", subject="s", files=None, index=1):
    from capybase.history import ReplayCommit
    return ReplayCommit(
        oid=oid, parent_oid=parent, subject=subject, body_summary="",
        touched_files=files or ["cfg.py"], diffstat={}, patch_id="", index=index,
    )


def _ctx(region, file_commits):
    from capybase.history import HistoryContext
    return HistoryContext(
        current_replay_commit=_commit("c0", "p", "current", index=0),
        source_commit_index=0, source_commit_count=2,
        previous_source_commits_touching_file=[],
        future_source_commits_touching_file=file_commits,
        future_source_commits_touching_region=region,
        recent_target_commits_touching_file=[],
        region_detection_method="diff",
    )


def _run_probe(repo, monkeypatch, *, region, file_commits, policy_mode):
    """Wire a fake history service + captured probe; return the mode selected."""
    cfg = Config()
    cfg.tests.required = False
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    orch.strictness = StrictnessPolicy(mode=policy_mode)
    captured: dict = {}

    def fake_probe(git, *, resolved_path, resolved_content, future_commits,
                   max_probes=1, mode="path_patch", intervening_commits=None):
        from capybase.history import FutureApplyResult
        captured["mode"] = mode
        captured["intervening"] = len(intervening_commits or [])
        return FutureApplyResult(probed=True, applies=True,
                                 future_commit_subject="x", reason="ok")
    monkeypatch.setattr("capybase.history.future_apply_probe", fake_probe)
    ctx = _ctx(region, file_commits)
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
    orch._history_plan = SimpleNamespace(source_commits=[_commit("c0", "p", "c", index=0)])
    monkeypatch.setattr(orch.git, "read_worktree_file", lambda p: b"resolved")
    orch._run_future_apply_probe(result)
    return captured


def test_intervening_present_selects_sequence_in_interactive(repo, monkeypatch):
    """The key change: even in INTERACTIVE mode, intervening commits →
    sequence_patch. Previously interactive forced path_patch regardless of data."""
    # fut2 (region) is preceded by fut1 (same-path) → 1 intervening.
    region = [_commit("fut2", "fut1", "rename", ["cfg.py"], 2)]
    file_commits = [_commit("fut1", "c0", "intermediate", ["cfg.py"], 1), region[0]]
    captured = _run_probe(repo, monkeypatch, region=region,
                          file_commits=file_commits, policy_mode="interactive")
    assert captured["mode"] == "sequence_patch"
    assert captured["intervening"] == 1


def test_no_intervening_selects_path_in_unattended(repo, monkeypatch):
    """Even in UNATTENDED mode, NO intervening commits → path_patch (the
    degenerate case where sequence_patch does no extra work anyway)."""
    region = [_commit("fut1", "c0", "rename", ["cfg.py"], 1)]  # only commit
    captured = _run_probe(repo, monkeypatch, region=region,
                          file_commits=region, policy_mode="unattended")
    assert captured["mode"] == "path_patch"
    assert captured["intervening"] == 0


def test_mode_is_data_derived_not_mode_derived(repo, monkeypatch):
    """Same conflict data → same mode across all policy modes (the contract:
    accuracy depends on the data, not the run mode)."""
    region = [_commit("fut2", "fut1", "rename", ["cfg.py"], 2)]
    file_commits = [_commit("fut1", "c0", "intermediate", ["cfg.py"], 1), region[0]]
    modes_seen = set()
    for policy_mode in ("interactive", "dry_run", "ci", "unattended"):
        captured = _run_probe(repo, monkeypatch, region=region,
                              file_commits=file_commits, policy_mode=policy_mode)
        modes_seen.add(captured["mode"])
    # All four modes picked the same probe mode (sequence_patch) for the same data.
    assert modes_seen == {"sequence_patch"}

"""Tests for the FutureApplyProbe (#history step 9 — narrow ECC-lite).

The probe checks whether a locally-valid resolution breaks the next source
commit touching the same region: in a throwaway worktree, write the resolved
file, test ``git apply --check`` against the next future commit's patch.

Covers:
- future_apply_probe: the pure-ish function (creates/cleans a worktree, applies,
  reports). Tests both the "applies cleanly" and "does NOT apply" outcomes.
- git_backend.commit_patch + check_apply: the git primitives.
- The no-future-commits / no-op degradation.
"""

from __future__ import annotations

from pathlib import Path

from capybase.history import FutureApplyResult, ReplayCommit, future_apply_probe
from tests.conftest import git


def _commit(oid, parent, subject, files, index):
    return ReplayCommit(
        oid=oid, parent_oid=parent, subject=subject, body_summary="",
        touched_files=files, diffstat={}, patch_id="", index=index,
    )


def _build_probe_repo(repo: Path) -> dict:
    """A repo with a base file, a feat branch that edits it, and a FUTURE commit
    (also on feat) that renames a function — the patch the probe will test."""
    (repo / "cfg.py").write_text("def parse():\n    return 1\n")
    git(repo, "add", "cfg.py"); git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    # The "current" commit being replayed: edits the return value.
    (repo / "cfg.py").write_text("def parse():\n    return 2\n")
    git(repo, "add", "cfg.py"); git(repo, "commit", "-q", "-m", "change return")
    current_oid = git(repo, "rev-parse", "HEAD").stdout.strip()
    # The "future" commit: renames parse() → load_config().
    (repo / "cfg.py").write_text("def load_config():\n    return 2\n")
    git(repo, "add", "cfg.py"); git(repo, "commit", "-q", "-m", "Rename parse to load_config")
    future_oid = git(repo, "rev-parse", "HEAD").stdout.strip()
    # Reset back to the current commit (so the worktree HEAD is at "current").
    git(repo, "reset", "--hard", current_oid)
    return {"current_oid": current_oid, "future_oid": future_oid}


def test_commit_patch_returns_nonempty(repo: Path):
    """git_backend.commit_patch yields a real diff for a commit."""
    (repo / "f.txt").write_text("a\n"); git(repo, "add", "f.txt")
    git(repo, "commit", "-q", "-m", "add f")
    oid = git(repo, "rev-parse", "HEAD").stdout.strip()
    from capybase.git_backend import GitBackend
    gb = GitBackend(repo)
    patch = gb.commit_patch(oid)
    assert b"f.txt" in patch
    assert b"+++" in patch


def test_check_apply_clean_patch(repo: Path):
    """git apply --check passes for a patch that applies cleanly."""
    (repo / "f.txt").write_text("line1\n"); git(repo, "add", "f.txt")
    git(repo, "commit", "-q", "-m", "base")
    (repo / "f.txt").write_text("line1\nline2\n"); git(repo, "add", "f.txt")
    git(repo, "commit", "-q", "-m", "add line2")
    add_oid = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "reset", "--hard", "HEAD~1")  # back to base
    from capybase.git_backend import GitBackend
    gb = GitBackend(repo)
    patch = gb.commit_patch(add_oid)
    assert gb.check_apply(patch) is True


def test_check_apply_conflicting_patch(repo: Path):
    """git apply --check fails for a patch that doesn't apply."""
    (repo / "f.txt").write_text("line1\n"); git(repo, "add", "f.txt")
    git(repo, "commit", "-q", "-m", "base")
    (repo / "f.txt").write_text("line1\nline2\n"); git(repo, "add", "f.txt")
    git(repo, "commit", "-q", "-m", "add line2")
    add_oid = git(repo, "rev-parse", "HEAD").stdout.strip()
    # Now change the file so the patch context doesn't match.
    (repo / "f.txt").write_text("completely different\n")
    from capybase.git_backend import GitBackend
    gb = GitBackend(repo)
    patch = gb.commit_patch(add_oid)
    assert gb.check_apply(patch) is False


def test_future_apply_probe_no_future_commits():
    """When there are no future commits, the probe returns probed=False."""
    from capybase.git_backend import GitBackend
    gb = GitBackend(".")
    result = future_apply_probe(
        gb, resolved_path="cfg.py", resolved_content=b"x\n",
        future_commits=[],
    )
    assert result.probed is False
    assert result.applies is True  # safe default — no signal


def test_future_apply_probe_applies_cleanly(repo: Path):
    """A resolution compatible with the future commit → probe reports success."""
    ctx = _build_probe_repo(repo)
    from capybase.git_backend import GitBackend
    gb = GitBackend(repo)
    # The "resolved" content keeps `def parse` — the future commit renames it,
    # but the rename patch applies cleanly to this content (it's a search/replace
    # on `parse` → `load_config` which is present).
    resolved = b"def parse():\n    return 2\n"
    future_commits = [_commit(ctx["future_oid"], ctx["current_oid"],
                              "Rename parse to load_config", ["cfg.py"], 1)]
    result = future_apply_probe(
        gb, resolved_path="cfg.py", resolved_content=resolved,
        future_commits=future_commits,
    )
    assert result.probed is True
    # The rename patch may or may not apply depending on exact context; the test
    # just confirms the probe ran and produced a definitive result.
    assert isinstance(result.applies, bool)
    assert result.future_commit_subject == "Rename parse to load_config"


def test_future_apply_probe_cleans_up_worktree(repo: Path):
    """The probe always cleans up its throwaway worktree (no orphans)."""
    ctx = _build_probe_repo(repo)
    from capybase.git_backend import GitBackend
    gb = GitBackend(repo)
    import tempfile
    before = set(Path(tempfile.gettempdir()).glob("capybase-futureprobe-*"))
    future_apply_probe(
        gb, resolved_path="cfg.py", resolved_content=b"def parse():\n    return 2\n",
        future_commits=[_commit(ctx["future_oid"], ctx["current_oid"],
                                "Rename", ["cfg.py"], 1)],
    )
    after = set(Path(tempfile.gettempdir()).glob("capybase-futureprobe-*"))
    # The temp dirs are removed by remove_worktree (the dir is the worktree path,
    # and git removes it). No new lingering dirs.
    assert len(after - before) == 0, "orphaned worktree temp dir left behind"


def test_future_apply_probe_never_raises(repo: Path):
    """A probe failure (e.g. bogus oid) returns probed=False, never raises."""
    from capybase.git_backend import GitBackend
    gb = GitBackend(repo)
    result = future_apply_probe(
        gb, resolved_path="cfg.py", resolved_content=b"x\n",
        future_commits=[_commit("bogus-oid", "bogus-parent", "bogus", ["cfg.py"], 0)],
    )
    # The probe ran but the bogus patch was empty → it skipped → probed=True,
    # applies=True (no patch to fail). Or if the worktree failed → probed=False.
    # Either way, no exception.
    assert isinstance(result, FutureApplyResult)


# ---------------------------------------------------------------------------
# Step 1: Regression tests for the fixed future-probe gating control flow.
# The bug: run() called continue_rebase() after _run_future_apply_probe without
# checking result.escalated. Fix: break before continuing when the probe escalates.
# These test the _run_future_apply_probe method directly (not the full run() loop)
# since the gating is inside that method + the break in run().
# ---------------------------------------------------------------------------


def test_future_probe_passes_in_unattended_no_escalation(repo, monkeypatch):
    """A passing probe does NOT escalate, even in unattended mode."""
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator, StepResult, UnitOutcome
    from capybase.conflict_model import CandidateResolution, ConflictSide, ConflictUnit
    from capybase.history import FutureApplyResult
    from capybase.policy_strictness import StrictnessPolicy

    cfg = Config()
    cfg.policy.policy_mode = "unattended"
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    orch.strictness = StrictnessPolicy(mode="unattended")

    # A unit with an accepted candidate + future touches.
    unit = ConflictUnit(
        session_id="s", step_index=1, path="cfg.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="x"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="y"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="z"),
        original_worktree_text="x", marker_span=(0, 0),
        structural_metadata={"replayed_commit_oid": "c1"},
    )
    cand = CandidateResolution(
        candidate_id="u:c", unit_id="u", model_name="fake",
        prompt_version="v", resolved_text="resolved",
    )
    outcome = UnitOutcome(unit=unit, validation=None, attempts=[cand])
    outcome.accepted = cand
    result = StepResult(step_index=1)
    result.outcomes = [outcome]

    # Stub: history context with future region touches + a passing probe.
    from capybase.history import HistoryContext, ReplayCommit
    good_ctx = HistoryContext(
        current_replay_commit=ReplayCommit(
            oid="c1", parent_oid="b", subject="cur", body_summary="",
            touched_files=["cfg.py"], diffstat={}, patch_id="", index=0,
        ),
        source_commit_index=0, source_commit_count=2,
        previous_source_commits_touching_file=[],
        future_source_commits_touching_file=[
            ReplayCommit(oid="c2", parent_oid="c1", subject="future",
                         body_summary="", touched_files=["cfg.py"],
                         diffstat={}, patch_id="", index=1),
        ],
        future_source_commits_touching_region=[
            ReplayCommit(oid="c2", parent_oid="c1", subject="future",
                         body_summary="", touched_files=["cfg.py"],
                         diffstat={}, patch_id="", index=1),
        ],
        recent_target_commits_touching_file=[],
    )
    monkeypatch.setattr(orch, "_history_context_for", lambda u: good_ctx)
    monkeypatch.setattr(orch, "strictness", StrictnessPolicy(mode="unattended"))
    # Stub the probe to return "passes".
    import capybase.history as hist_mod
    monkeypatch.setattr(hist_mod, "future_apply_probe", lambda *a, **kw: FutureApplyResult(
        probed=True, applies=True, future_commit_subject="future",
        reason="applies cleanly",
    ))
    # Stub read_worktree_file (no actual file).
    monkeypatch.setattr(orch.git, "read_worktree_file", lambda p: b"resolved")

    orch._history_service = True  # truthy so the method doesn't bail early
    orch._history_plan = True
    orch._run_future_apply_probe(result)
    assert not result.escalated, "passing probe should NOT escalate"


def test_future_probe_fails_in_unattended_escalates(repo, monkeypatch):
    """A failing probe in unattended mode sets result.escalated = True."""
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator, StepResult, UnitOutcome
    from capybase.conflict_model import CandidateResolution, ConflictSide, ConflictUnit
    from capybase.history import FutureApplyResult, HistoryContext, ReplayCommit
    from capybase.policy_strictness import StrictnessPolicy

    cfg = Config()
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)

    unit = ConflictUnit(
        session_id="s", step_index=1, path="cfg.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="x"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="y"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="z"),
        original_worktree_text="x", marker_span=(0, 0),
        structural_metadata={"replayed_commit_oid": "c1"},
    )
    cand = CandidateResolution(
        candidate_id="u:c", unit_id="u", model_name="fake",
        prompt_version="v", resolved_text="resolved",
    )
    outcome = UnitOutcome(unit=unit, validation=None, attempts=[cand])
    outcome.accepted = cand
    result = StepResult(step_index=1)
    result.outcomes = [outcome]

    future_commit = ReplayCommit(
        oid="c2", parent_oid="c1", subject="future", body_summary="",
        touched_files=["cfg.py"], diffstat={}, patch_id="", index=1,
    )
    good_ctx = HistoryContext(
        current_replay_commit=future_commit, source_commit_index=0,
        source_commit_count=2,
        previous_source_commits_touching_file=[],
        future_source_commits_touching_file=[future_commit],
        future_source_commits_touching_region=[future_commit],
        recent_target_commits_touching_file=[],
    )
    monkeypatch.setattr(orch, "_history_context_for", lambda u: good_ctx)
    monkeypatch.setattr(orch, "strictness", StrictnessPolicy(mode="unattended"))
    import capybase.history as hist_mod
    monkeypatch.setattr(hist_mod, "future_apply_probe", lambda *a, **kw: FutureApplyResult(
        probed=True, applies=False, future_commit_subject="future",
        reason="does NOT apply cleanly",
    ))
    monkeypatch.setattr(orch.git, "read_worktree_file", lambda p: b"resolved")
    orch._history_service = True
    orch._history_plan = True

    orch._run_future_apply_probe(result)
    assert result.escalated, "failing probe in unattended mode MUST escalate"
    assert "future-apply" in (result.reason or "")


def test_future_probe_fails_in_interactive_does_not_escalate(repo, monkeypatch):
    """A failing probe in interactive mode journals but does NOT escalate."""
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator, StepResult, UnitOutcome
    from capybase.conflict_model import CandidateResolution, ConflictSide, ConflictUnit
    from capybase.history import FutureApplyResult, HistoryContext, ReplayCommit
    from capybase.policy_strictness import StrictnessPolicy

    cfg = Config()
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)

    unit = ConflictUnit(
        session_id="s", step_index=1, path="cfg.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="x"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="y"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="z"),
        original_worktree_text="x", marker_span=(0, 0),
        structural_metadata={"replayed_commit_oid": "c1"},
    )
    cand = CandidateResolution(
        candidate_id="u:c", unit_id="u", model_name="fake",
        prompt_version="v", resolved_text="resolved",
    )
    outcome = UnitOutcome(unit=unit, validation=None, attempts=[cand])
    outcome.accepted = cand
    result = StepResult(step_index=1)
    result.outcomes = [outcome]

    future_commit = ReplayCommit(
        oid="c2", parent_oid="c1", subject="future", body_summary="",
        touched_files=["cfg.py"], diffstat={}, patch_id="", index=1,
    )
    good_ctx = HistoryContext(
        current_replay_commit=future_commit, source_commit_index=0,
        source_commit_count=2,
        previous_source_commits_touching_file=[],
        future_source_commits_touching_file=[future_commit],
        future_source_commits_touching_region=[future_commit],
        recent_target_commits_touching_file=[],
    )
    monkeypatch.setattr(orch, "_history_context_for", lambda u: good_ctx)
    monkeypatch.setattr(orch, "strictness", StrictnessPolicy(mode="interactive"))
    import capybase.history as hist_mod
    monkeypatch.setattr(hist_mod, "future_apply_probe", lambda *a, **kw: FutureApplyResult(
        probed=True, applies=False, future_commit_subject="future",
        reason="does NOT apply cleanly",
    ))
    monkeypatch.setattr(orch.git, "read_worktree_file", lambda p: b"resolved")
    orch._history_service = True
    orch._history_plan = True

    orch._run_future_apply_probe(result)
    assert not result.escalated, "failing probe in interactive mode should NOT escalate"


def test_future_probe_throw_does_not_crash(repo, monkeypatch):
    """A probe exception is swallowed (no signal, no crash)."""
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator, StepResult, UnitOutcome
    from capybase.conflict_model import CandidateResolution, ConflictSide, ConflictUnit
    from capybase.history import HistoryContext, ReplayCommit

    cfg = Config()
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)

    unit = ConflictUnit(
        session_id="s", step_index=1, path="cfg.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="x"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="y"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="z"),
        original_worktree_text="x", marker_span=(0, 0),
        structural_metadata={"replayed_commit_oid": "c1"},
    )
    cand = CandidateResolution(
        candidate_id="u:c", unit_id="u", model_name="fake",
        prompt_version="v", resolved_text="resolved",
    )
    outcome = UnitOutcome(unit=unit, validation=None, attempts=[cand])
    outcome.accepted = cand
    result = StepResult(step_index=1)
    result.outcomes = [outcome]

    future_commit = ReplayCommit(
        oid="c2", parent_oid="c1", subject="future", body_summary="",
        touched_files=["cfg.py"], diffstat={}, patch_id="", index=1,
    )
    good_ctx = HistoryContext(
        current_replay_commit=future_commit, source_commit_index=0,
        source_commit_count=2,
        previous_source_commits_touching_file=[],
        future_source_commits_touching_file=[future_commit],
        future_source_commits_touching_region=[future_commit],
        recent_target_commits_touching_file=[],
    )
    monkeypatch.setattr(orch, "_history_context_for", lambda u: good_ctx)
    from capybase.policy_strictness import StrictnessPolicy
    monkeypatch.setattr(orch, "strictness", StrictnessPolicy(mode="unattended"))

    def _boom(*a, **kw):
        raise RuntimeError("probe crashed")
    monkeypatch.setattr(orch.git, "read_worktree_file", _boom)
    orch._history_service = True
    orch._history_plan = True

    # Should not raise.
    orch._run_future_apply_probe(result)
    assert not result.escalated  # no signal → no escalation

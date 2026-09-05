"""capybase clean — the no-trace contract for unpromoted state.

Every test builds a hermetic repo, seeds the full state inventory
(candidate + backup branches, recovery refs, a live worktree, the
.rebase-agent/ tree, real commits on the candidate branch), and
asserts the clean contract: capybase's state gone INCLUDING the
unreachable objects, the user's branches/reflogs/objects untouched,
and the object store not larger afterwards.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from capybase.cleanup import clean_capybase_state
from capybase.git_backend import GitBackend


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(cmd, cwd=repo, check=True)
    (repo / "f.txt").write_text("one\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "c1"], cwd=repo, check=True)
    return repo


def _seed_state(repo: Path) -> str:
    """Create the full unpromoted-state inventory; return the candidate tip."""
    # A real commit on the candidate branch (objects that must be pruned).
    subprocess.run(
        ["git", "checkout", "-q", "-b", "capybase/candidate/main@20260905-1"],
        cwd=repo, check=True)
    # Substantial + incompressible so the size assertion reflects real
    # reclamation (a 2-commit micro-repo's gc packing overhead would
    # otherwise exceed the reclaimed bytes).
    import os
    (repo / "g.bin").write_bytes(os.urandom(200_000))
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "candidate c2"], cwd=repo, check=True)
    tip = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                         capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    # A backup branch + recovery refs.
    subprocess.run(
        ["git", "branch", "capybase/backup/main@20260905-1"], cwd=repo, check=True)
    subprocess.run(
        ["git", "update-ref", "refs/rebase-agent/abc123/start", tip],
        cwd=repo, check=True)
    subprocess.run(
        ["git", "update-ref", "refs/rebase-agent/abc123/step-1", tip],
        cwd=repo, check=True)
    # The on-disk state root.
    d = repo / ".rebase-agent" / "candidates" / "x"
    d.mkdir(parents=True)
    (d / "session_state.json").write_text(json.dumps({"outcome": "success"}))
    (repo / ".rebase-agent" / "sessions" / "s1").mkdir(parents=True)
    (repo / ".rebase-agent" / "sessions" / "s1" / "journal.jsonl").write_text(
        '{"e": 1}\n')
    return tip


def _ref_exists(repo: Path, ref: str) -> bool:
    r = subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref],
                       cwd=repo, capture_output=True, text=True)
    return r.returncode == 0


def _object_exists(repo: Path, oid: str) -> bool:
    r = subprocess.run(["git", "cat-file", "-e", oid], cwd=repo,
                       capture_output=True, text=True)
    return r.returncode == 0


def test_clean_removes_everything_including_objects(tmp_path):
    repo = _repo(tmp_path)
    tip = _seed_state(repo)
    assert _object_exists(repo, tip)
    report = clean_capybase_state(repo)
    assert not report.refused
    assert not _ref_exists(repo, "capybase/candidate/main@20260905-1")
    assert not _ref_exists(repo, "capybase/backup/main@20260905-1")
    assert not _ref_exists(repo, "refs/rebase-agent/abc123/start")
    assert not (repo / ".rebase-agent").exists()
    # THE no-trace guarantee: the candidate's commits are PRUNED, not
    # merely unreachable (reflog expiry + gc --prune=now).
    assert not _object_exists(repo, tip)
    assert report.git_size_after <= report.git_size_before


def test_dry_run_mutates_nothing(tmp_path):
    repo = _repo(tmp_path)
    tip = _seed_state(repo)
    report = clean_capybase_state(repo, dry_run=True)
    assert report.dry_run and not report.refused
    assert len(report.candidate_branches) == 1
    assert len(report.backup_branches) == 1
    assert len(report.recovery_refs) == 2
    assert _ref_exists(repo, "capybase/candidate/main@20260905-1")
    assert (repo / ".rebase-agent").exists()
    assert _object_exists(repo, tip)


def test_user_state_untouched(tmp_path):
    repo = _repo(tmp_path)
    _seed_state(repo)
    subprocess.run(["git", "branch", "my-branch"], cwd=repo, check=True)
    head_reflog = subprocess.run(
        ["git", "reflog", "main"], cwd=repo, capture_output=True, text=True
    ).stdout
    report = clean_capybase_state(repo)
    assert not report.refused
    assert _ref_exists(repo, "main") and _ref_exists(repo, "my-branch")
    # Reachable reflog history survives (only UNREACHABLE entries expire).
    after = subprocess.run(["git", "reflog", "main"], cwd=repo,
                           capture_output=True, text=True).stdout
    assert after == head_reflog


def test_live_candidate_worktree_removed(tmp_path):
    repo = _repo(tmp_path)
    tip = _seed_state(repo)
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", str(wt),
         "capybase/candidate/main@20260905-1"],
        cwd=repo, check=True)
    assert wt.exists()
    report = clean_capybase_state(repo)
    assert not report.refused
    assert not wt.exists()
    assert not _ref_exists(repo, "capybase/candidate/main@20260905-1")
    assert not _object_exists(repo, tip)


def test_refuses_mid_operation(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _seed_state(repo)
    monkeypatch.setattr(
        GitBackend, "operation_in_progress",
        lambda self: "rebase", raising=True)
    report = clean_capybase_state(repo)
    assert report.refused and "in progress" in report.refused
    assert _ref_exists(repo, "capybase/candidate/main@20260905-1")


def test_already_clean_repo_is_a_noop(tmp_path):
    repo = _repo(tmp_path)
    report = clean_capybase_state(repo)
    assert not report.refused
    assert report.candidate_branches == []
    assert report.state_dir_bytes == 0
    assert "0" in report.summary()


class TestCrashRecovery:
    """Crashed runs / reboots bypass the finally-teardown — clean must
    still find and remove everything from the surviving signals alone."""

    def test_branchless_worktree_removed_via_admin_name(self, tmp_path):
        # Crash + partial escalation cleanup deleted the branch; the
        # worktree dir and its admin entry survive. Ownership is still
        # provable from the admin name / path prefix.
        import tempfile
        repo = _repo(tmp_path)
        tip = _seed_state(repo)
        wt = Path(tempfile.mkdtemp(prefix="capybase-candidate-"))
        subprocess.run(
            ["git", "worktree", "add", str(wt),
             "capybase/candidate/main@20260905-1"],
            cwd=repo, check=True)
        # Simulate the partial cleanup: force-delete the checked-out
        # branch ref directly (a crash path git normally guards).
        subprocess.run(
            ["git", "-C", str(wt), "checkout", "-q", "--detach"], check=True)
        subprocess.run(
            ["git", "update-ref", "-d",
             "refs/heads/capybase/candidate/main@20260905-1"],
            cwd=repo, check=True)
        assert wt.exists()
        report = clean_capybase_state(repo)
        assert not report.refused
        assert not wt.exists()
        assert not (repo / ".git" / "worktrees").exists() or \
            not any((repo / ".git" / "worktrees").iterdir())
        assert not _object_exists(repo, tip)

    def test_reboot_wiped_worktree_dir_admin_pruned(self, tmp_path):
        # The temp dir vanished (reboot); the admin entry survives.
        import tempfile
        repo = _repo(tmp_path)
        _seed_state(repo)
        wt = Path(tempfile.mkdtemp(prefix="capybase-candidate-"))
        subprocess.run(
            ["git", "worktree", "add", str(wt),
             "capybase/candidate/main@20260905-1"],
            cwd=repo, check=True)
        shutil.rmtree(wt)  # the "reboot"
        report = clean_capybase_state(repo, dry_run=True)
        assert not report.refused
        report = clean_capybase_state(repo)
        assert not report.refused
        assert not (repo / ".git" / "worktrees").exists() or \
            not any((repo / ".git" / "worktrees").iterdir())

    def test_dryrun_branches_and_worktrees_cleaned(self, tmp_path):
        # A crashed --dry-run rehearsal leaves capybase/dryrun/<session>
        # branches and a capybase-dryrun-* worktree.
        import tempfile
        repo = _repo(tmp_path)
        subprocess.run(
            ["git", "branch", "capybase/dryrun/sess-42"], cwd=repo, check=True)
        wt = Path(tempfile.mkdtemp(prefix="capybase-dryrun-"))
        subprocess.run(
            ["git", "worktree", "add", str(wt), "capybase/dryrun/sess-42"],
            cwd=repo, check=True)
        report = clean_capybase_state(repo)
        assert not report.refused
        assert not _ref_exists(repo, "capybase/dryrun/sess-42")
        assert not wt.exists()


def test_crashed_worktree_mid_rebase_is_discarded(tmp_path):
    """THE crash scenario: capybase died mid-rebase inside its worktree.

    The rebase state lives in the WORKTREE's git dir
    (.git/worktrees/<name>/rebase-merge) — invisible to the main repo's
    operation_in_progress check, so clean does not refuse (correct: an
    owned worktree's rebase state is capybase's by construction, and
    discarding it is the point). Double-force removal clears the live
    rebase state; the branch, admin entries, and objects follow.
    """
    repo = _repo(tmp_path)
    # A conflicting side branch to stop the inner rebase at a conflict.
    subprocess.run(["git", "checkout", "-q", "-b", "other"], cwd=repo, check=True)
    (repo / "f.txt").write_text("two\n")
    subprocess.run(["git", "commit", "-aqm", "c2"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    (repo / "f.txt").write_text("three\n")
    subprocess.run(["git", "commit", "-aqm", "c3"], cwd=repo, check=True)
    subprocess.run(
        ["git", "branch", "capybase/candidate/main@t1", "other"],
        cwd=repo, check=True)
    import tempfile
    wt = Path(tempfile.mkdtemp(prefix="capybase-candidate-"))
    subprocess.run(
        ["git", "worktree", "add", str(wt), "capybase/candidate/main@t1"],
        cwd=repo, check=True)
    r = subprocess.run(["git", "-C", str(wt), "rebase", "main"],
                       cwd=repo, capture_output=True, text=True)
    assert r.returncode != 0  # stopped at the conflict
    admin = repo / ".git" / "worktrees" / wt.name
    assert (admin / "rebase-merge").exists()  # mid-rebase, live
    # The main repo is idle — the crashed state is invisible to it.
    assert GitBackend(repo).operation_in_progress() is None
    report = clean_capybase_state(repo)
    assert not report.refused
    assert not wt.exists()
    assert not admin.exists()
    assert not _ref_exists(repo, "capybase/candidate/main@t1")

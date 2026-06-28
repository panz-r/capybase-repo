"""Tests for the rebase pre-flight checks.

The preflight must refuse to touch the repo on a bad starting state: a
half-finished operation, a detached HEAD, an unknown target, a dirty tree, a
self-rebase. Each blocking failure short-circuits ``rebase()`` with a clear
GitError before any rebase is started. The fast-forward / up-to-date report is
informational (non-blocking).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from capybase.config import Config
from capybase.git_backend import GitBackend, GitError
from capybase.preflight import run_rebase_preflight

from tests.conftest import git


def _backend(repo: Path) -> GitBackend:
    return GitBackend(repo)


def _cfg() -> Config:
    return Config()


# ---------------------------------------------------------------------------
# Happy path: a clean repo on a branch passes every check.
# ---------------------------------------------------------------------------


def test_preflight_passes_clean_repo_on_branch(repo: Path):
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "branch", "upstream")
    (repo / "a.txt").write_text("b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "diverge")

    report = run_rebase_preflight(_backend(repo), _cfg(), "upstream", llm_ping=False)
    assert report.passed, [str(c) for c in report.checks]
    assert report.first_blocking_failure is None
    names = [c.name for c in report.checks]
    assert "git-repo" in names and "on-branch" in names and "target-resolves" in names


def test_preflight_git_version_check(repo: Path):
    g = _backend(repo)
    ver = g.git_version()
    assert ver >= (2, 0), ver  # we're definitely on modern git in CI


# ---------------------------------------------------------------------------
# Detached HEAD is blocked.
# ---------------------------------------------------------------------------


def test_preflight_blocks_detached_head(repo: Path):
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "branch", "upstream")
    git(repo, "checkout", "-q", "--detach", "HEAD")

    report = run_rebase_preflight(_backend(repo), _cfg(), "upstream", llm_ping=False)
    assert not report.passed
    fail = report.first_blocking_failure
    assert fail is not None and fail.name == "on-branch"
    assert "detached" in fail.detail.lower()


# ---------------------------------------------------------------------------
# An in-progress operation is blocked.
# ---------------------------------------------------------------------------


def test_preflight_blocks_in_progress_rebase(repo: Path):
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "branch", "feat")
    (repo / "a.txt").write_text("b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "main")
    git(repo, "checkout", "-q", "feat")
    (repo / "a.txt").write_text("c\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "feat")
    # Start a rebase that conflicts, leaving it in progress.
    git(repo, "rebase", "main", check=False)
    assert _backend(repo).rebase_in_progress()

    report = run_rebase_preflight(_backend(repo), _cfg(), "main", llm_ping=False)
    fail = report.first_blocking_failure
    assert fail is not None and fail.name == "no-op-in-progress"
    assert "rebase" in fail.detail

    # Clean up so the tmp repo is tidy.
    git(repo, "rebase", "--abort", check=False)


def test_preflight_blocks_in_progress_cherry_pick(repo: Path):
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "branch", "feat")
    (repo / "a.txt").write_text("b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "main2")
    git(repo, "checkout", "-q", "feat")
    feat_head = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "checkout", "-q", "main")
    # Cherry-pick feat onto main, which will conflict and leave CHERRY_PICK_HEAD.
    git(repo, "cherry-pick", feat_head, check=False)
    assert _backend(repo).operation_in_progress() == "cherry-pick"

    report = run_rebase_preflight(_backend(repo), _cfg(), "feat", llm_ping=False)
    fail = report.first_blocking_failure
    assert fail is not None and fail.name == "no-op-in-progress"
    assert "cherry-pick" in fail.detail

    git(repo, "cherry-pick", "--abort", check=False)


# ---------------------------------------------------------------------------
# Unknown target and self-rebase are blocked.
# ---------------------------------------------------------------------------


def test_preflight_blocks_unknown_target(repo: Path):
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")

    report = run_rebase_preflight(_backend(repo), _cfg(), "no-such-branch", llm_ping=False)
    fail = report.first_blocking_failure
    assert fail is not None and fail.name == "target-resolves"


def test_preflight_blocks_self_rebase(repo: Path):
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")

    report = run_rebase_preflight(_backend(repo), _cfg(), "HEAD", llm_ping=False)
    fail = report.first_blocking_failure
    assert fail is not None and fail.name == "not-self-rebase"


# ---------------------------------------------------------------------------
# Dirty worktree: blocked without --autostash, allowed (informational) with it.
# ---------------------------------------------------------------------------


def test_preflight_dirty_blocked_without_autostash(repo: Path):
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "branch", "upstream")
    (repo / "a.txt").write_text("uncommitted\n")  # dirty

    report = run_rebase_preflight(_backend(repo), _cfg(), "upstream", autostash=False, llm_ping=False)
    fail = report.first_blocking_failure
    assert fail is not None and fail.name == "clean-worktree"


def test_preflight_dirty_ok_with_autostash(repo: Path):
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "branch", "upstream")
    (repo / "a.txt").write_text("uncommitted\n")  # dirty

    report = run_rebase_preflight(_backend(repo), _cfg(), "upstream", autostash=True, llm_ping=False)
    # No blocking failure from the clean-worktree check.
    clean = [c for c in report.checks if c.name == "clean-worktree"]
    assert clean and clean[0].ok
    assert "autostash" in clean[0].detail.lower()


# ---------------------------------------------------------------------------
# Fast-forward / up-to-date report is non-blocking and informational.
# ---------------------------------------------------------------------------


def test_preflight_ff_report(repo: Path):
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "branch", "upstream")
    git(repo, "checkout", "-q", "upstream")
    (repo / "b.txt").write_text("b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "advance upstream")
    git(repo, "checkout", "-q", "main")

    # main is now BEHIND upstream → rebase onto upstream would fast-forward.
    report = run_rebase_preflight(_backend(repo), _cfg(), "upstream", llm_ping=False)
    shape = [c for c in report.checks if c.name == "rebase-shape"]
    assert shape, "expected a rebase-shape check"
    assert not shape[0].blocking  # informational
    assert "fast-forward" in shape[0].detail.lower()


def test_preflight_uptodate_report(repo: Path):
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "branch", "upstream")
    # main == upstream: nothing to replay. (target == HEAD here only because we
    # haven't diverged; check the shape message is informational either way.)
    report = run_rebase_preflight(_backend(repo), _cfg(), "upstream", llm_ping=False)
    shape = [c for c in report.checks if c.name == "rebase-shape"]
    assert shape and not shape[0].blocking

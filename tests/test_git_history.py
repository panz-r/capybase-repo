"""Tests for the history-query git plumbing behind silent-resurrection detection.

These build tiny repos with a deletion commit on the upstream/``onto`` branch
and a replaying branch, then exercise the read-only git_backend methods
(``merge_base``, ``files_changed_between``, ``blob_at``, the rebase-state
readers) that the resurrection scan consumes. The methods must never raise on
missing data — they're advisory inputs.
"""

from __future__ import annotations

from pathlib import Path

from capybase.git_backend import GitBackend

from tests.conftest import git


def test_merge_base_finds_common_ancestor(repo: Path):
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")

    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / "b.txt").write_text("b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "feat")

    git(repo, "checkout", "-q", "main")
    (repo / "c.txt").write_text("c\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "main")

    g = GitBackend(repo)
    mb = g.merge_base("main", "feat")
    # The merge base is the base commit; its tree has a.txt only.
    assert mb is not None
    assert g.blob_at(mb, "a.txt") == b"a\n"
    # b.txt and c.txt don't exist at the merge base.
    assert g.blob_at(mb, "b.txt") is None


def test_merge_base_none_on_unresolvable(repo: Path):
    g = GitBackend(repo)
    assert g.merge_base("does-not-exist", "HEAD") is None


def test_files_changed_between(repo: Path):
    (repo / "x.txt").write_text("1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "v1")
    v1 = git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "y.txt").write_text("2\n")
    (repo / "x.txt").write_text("changed\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "v2")
    v2 = git(repo, "rev-parse", "HEAD").stdout.strip()

    g = GitBackend(repo)
    changed = g.files_changed_between(v1, v2)
    assert set(changed) == {"x.txt", "y.txt"}


def test_blob_at_returns_none_for_missing_path(repo: Path):
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    g = GitBackend(repo)
    assert g.blob_at("HEAD", "nope.txt") is None
    assert g.blob_at("HEAD", "a.txt") == b"a\n"


def test_rebase_state_readers_during_rebase(repo: Path):
    """During a rebase, onto/orig-head/head-name are readable; None when idle."""
    (repo / "app.py").write_text("def f():\n    return 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")

    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text("def f():\n    return 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "feat change")

    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text("def f():\n    return 3\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "main change")

    git(repo, "checkout", "-q", "feat")
    git(repo, "rebase", "main", check=False)  # conflict

    g = GitBackend(repo)
    onto = g.rebase_onto_oid()
    orig = g.rebase_orig_head_oid()
    head_name = g.rebase_head_name()
    assert onto is not None
    assert orig is not None
    assert head_name is not None
    # onto resolves to main's tip; orig-head to the pre-rebase feat tip.
    assert onto == git(repo, "rev-parse", "main").stdout.strip()


def test_rebase_state_readers_none_when_idle(repo: Path):
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    g = GitBackend(repo)
    assert g.rebase_onto_oid() is None
    assert g.rebase_orig_head_oid() is None
    assert g.rebase_head_name() is None

"""Tests for user-visible backup branches and the namespace-guarded delete.

The backup branch (``capybase/backup/<branch>@<ts>``) is the safety net a real
user relies on to undo a bad rebase. These tests cover creation, listing,
deletion round-trip, slug sanitisation, and the critical safety rail: delete_ref
must refuse anything outside the backup namespace so a stray call can never
delete the user's real branches.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from capybase.git_backend import GitBackend, GitError

from tests.conftest import git


def _repo_with_commit(repo: Path) -> str:
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def test_create_backup_ref_points_at_source(repo: Path):
    oid = _repo_with_commit(repo)
    g = GitBackend(repo)
    ref = g.create_backup_ref(oid, label="main")
    assert ref.startswith("refs/heads/capybase/backup/")
    assert "main" in ref
    # The branch resolves to the source OID.
    assert git(repo, "rev-parse", ref).stdout.strip() == oid


def test_backup_label_slug_sanitises_slashes(repo: Path):
    oid = _repo_with_commit(repo)
    g = GitBackend(repo)
    ref = g.create_backup_ref(oid, label="feature/cool-thing")
    # A '/' in the label would create a nested ref; the slug flattens it.
    short = ref[len("refs/heads/capybase/backup/"):]
    assert "/" not in short.split("@")[0], short
    assert "feature-cool-thing" in short


def test_list_backup_refs_round_trip(repo: Path):
    oid = _repo_with_commit(repo)
    g = GitBackend(repo)
    assert g.list_backup_refs() == []
    r1 = g.create_backup_ref(oid, label="main")
    r2 = g.create_backup_ref(oid, label="feat")
    # create_* returns full refnames; list_* returns short names. Strip the
    # refs/heads/ prefix to compare them on equal footing.
    short = lambda r: r[len("refs/heads/"):]
    listed = g.list_backup_refs()
    assert set(listed) == {short(r1), short(r2)}


def test_delete_backup_ref_round_trip(repo: Path):
    oid = _repo_with_commit(repo)
    g = GitBackend(repo)
    ref = g.create_backup_ref(oid, label="main")
    short = ref[len("refs/heads/"):]
    assert short in g.list_backup_refs()
    g.delete_ref(ref)
    assert g.list_backup_refs() == []


def test_delete_ref_accepts_short_name(repo: Path):
    oid = _repo_with_commit(repo)
    g = GitBackend(repo)
    ref = g.create_backup_ref(oid, label="main")
    short = ref[len("refs/heads/"):]
    g.delete_ref(short)
    assert g.list_backup_refs() == []


@pytest.mark.parametrize("bad_ref", [
    "refs/heads/main",          # a real user branch
    "main",                     # short form of a real branch
    "refs/rebase-agent/x/start",  # capybase's internal audit ref
])
def test_delete_ref_refuses_non_backup_namespace(repo: Path, bad_ref: str):
    _repo_with_commit(repo)
    g = GitBackend(repo)
    # Also create a real branch for the "main" cases so the ref is plausible.
    git(repo, "branch", "topic")
    with pytest.raises(GitError, match="refuses to delete"):
        g.delete_ref(bad_ref)

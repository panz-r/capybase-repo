from capybase.git_backend import GitBackend


def test_clean_worktree(git_backend, repo):
    assert git_backend.worktree_is_clean()


def test_unmerged_paths(conflicted_repo):
    git = GitBackend(conflicted_repo["repo"])
    unmerged = git.list_unmerged_paths()
    assert len(unmerged) == 1
    entry = unmerged[0]
    assert entry.path == "app.py"
    assert entry.mode == "UU"
    # all three stages present
    assert {1, 2, 3} <= set(entry.stages)


def test_read_stage_blobs(conflicted_repo):
    git = GitBackend(conflicted_repo["repo"])
    assert git.read_stage_blob("app.py", 1).decode() == conflicted_repo["base"]
    assert git.read_stage_blob("app.py", 2).decode() == conflicted_repo["current"]
    assert git.read_stage_blob("app.py", 3).decode() == conflicted_repo["replayed"]


def test_rebase_in_progress(conflicted_repo):
    git = GitBackend(conflicted_repo["repo"])
    assert git.rebase_in_progress()


def test_worktree_file_has_markers(conflicted_repo):
    git = GitBackend(conflicted_repo["repo"])
    text = git.read_worktree_file("app.py").decode()
    assert "<<<<<<<" in text and "=======" in text and ">>>>>>>" in text


def test_rebase_progress_mid_rebase(conflicted_repo):
    """(msgnum, end) while a rebase is stopped — the dropped-empty-pick
    signal. A pick resolved to no-change gets DROPPED: HEAD does not move,
    but the rebase's position does (cython-0020: four CI picks dropped one
    per iteration; the head-only stuck-guard killed the healthy rebase)."""
    git = GitBackend(conflicted_repo["repo"])
    prog = git.rebase_progress()
    assert prog is not None
    # Exact: the fixture replays exactly one commit — a swapped tuple
    # must not pass (msgnum >= 1 and end >= msgnum are swap-blind).
    assert prog == (1, 1)


def test_rebase_progress_none_when_clean(repo):
    git = GitBackend(repo)
    assert git.rebase_progress() is None


def test_unmerged_paths_dedupes_stages(conflicted_repo):
    git = GitBackend(conflicted_repo["repo"])
    assert git.unmerged_paths() == ["app.py"]

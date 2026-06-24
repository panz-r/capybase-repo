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

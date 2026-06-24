"""Shared pytest fixtures: temp git repos with synthetic rebase conflicts.

These build real, tiny git repositories in tmp_path, then drive a rebase into a
``UU`` (both-modified) conflict so git_backend/orchestrator can be tested
against genuine unmerged index state — not mocks.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from capybase.git_backend import GitBackend


def git(repo: Path, *args: str, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "tester"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "t@example.com"
    env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = "2000-01-01T00:00:00"
    env["GIT_PAGER"] = "cat"
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env,
        capture_output=True,
        text=True,
        input=input_text,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {args} failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    return proc


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An initialized git repo with identity configured."""
    git(tmp_path, "init", "-q", "-b", "main")
    return tmp_path


@pytest.fixture
def conflicted_repo(repo: Path) -> dict:
    """A repo stopped at a UU rebase conflict over ``app.py``.

    Layout:
      main  : BASE content
      feat  : diverges from main (REPLAYED commit)
      main  also diverges (CURRENT_UPSTREAM side)

    Replaying ``feat`` onto ``main`` yields a both-modified conflict.
    Returns paths + the ConflictSide texts used.
    """
    base = "def greet():\n    return 'hello'\n"
    upstream = "def greet():\n    return 'hi'\n"          # CURRENT_UPSTREAM_SIDE
    replayed = "def greet():\n    return 'howdy'\n"        # REPLAYED_COMMIT_SIDE

    (repo / "app.py").write_text(base)
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "base")

    # feat branch from base, edit -> replayed.
    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text(replayed)
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "replayed change")

    # switch to main, edit -> upstream (current side).
    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text(upstream)
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "upstream change")

    # Rebase feat onto main -> conflict.
    git(repo, "checkout", "-q", "feat")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected a rebase conflict"
    return {
        "repo": repo,
        "path": "app.py",
        "base": base,
        "current": upstream,
        "replayed": replayed,
    }


@pytest.fixture
def multi_unit_conflicted_repo(repo: Path) -> dict:
    """A repo stopped at a UU rebase conflict with TWO hunks in one file.

    Layout (mirrors the live ``settings-uu`` fixture): a single ``cfg.py``
    with two well-separated conflict regions — a services list and a feature
    flags dict — both modified on both sides such that git emits two distinct
    ``<<<<<<< ... >>>>>>>`` blocks. Replaying ``feat`` onto ``main`` yields a
    multi-unit-per-file conflict.

    Returns paths + the expected merged texts for each hunk.
    """
    base = (
        'ENABLED_SERVICES = ["core", "cli"]\n'
        "\n\n"
        'class ServiceConfig:\n    name = "capybase"\n'
        "\n\n"
        'FEATURE_FLAGS = {\n    "cache": "off",\n    "metrics": "off",\n}\n'
    )
    upstream = (
        'ENABLED_SERVICES = ["core", "cli", "scheduler"]\n'
        "\n\n"
        'class ServiceConfig:\n    name = "capybase"\n'
        "\n\n"
        'FEATURE_FLAGS = {\n    "cache": "off",\n    "metrics": "on",\n}\n'
    )
    replayed = (
        'ENABLED_SERVICES = ["core", "cli", "reloader"]\n'
        "\n\n"
        'class ServiceConfig:\n    name = "capybase"\n'
        "\n\n"
        'FEATURE_FLAGS = {\n    "cache": "on",\n    "metrics": "off",\n}\n'
    )

    (repo / "cfg.py").write_text(base)
    git(repo, "add", "cfg.py")
    git(repo, "commit", "-q", "-m", "base")

    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / "cfg.py").write_text(replayed)
    git(repo, "add", "cfg.py")
    git(repo, "commit", "-q", "-m", "replayed changes")

    git(repo, "checkout", "-q", "main")
    (repo / "cfg.py").write_text(upstream)
    git(repo, "add", "cfg.py")
    git(repo, "commit", "-q", "-m", "upstream changes")

    git(repo, "checkout", "-q", "feat")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected a rebase conflict"
    return {
        "repo": repo,
        "path": "cfg.py",
        "base": base,
        "current": upstream,
        "replayed": replayed,
        # Sensible merges the model/human would produce (combine both sides).
        # These are the *block-interior* resolved texts — exactly what replaces
        # the marker span. The services conflict covers only the assignment
        # line; the flags conflict covers only the two dict-entry lines (the
        # surrounding ``FEATURE_FLAGS = {`` and ``}``` are outside the span).
        "services_merged": 'ENABLED_SERVICES = ["core", "cli", "scheduler", "reloader"]',
        "flags_merged": '    "cache": "on",\n    "metrics": "on"',
    }


@pytest.fixture
def git_backend(repo: Path) -> GitBackend:
    return GitBackend(repo)

"""The README terminology guard in hooks/pre-commit (2026-09-18).

Builds a temp git repo with the real hook installed, then asserts:
banned terms in staged README additions refuse the commit; the
'  # readme-guard: allow' marker permits; the same terms in non-README
files are untouched (the guard is README-scoped).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "hooks" / "pre-commit"


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(args), cwd=str(cwd), capture_output=True, text=True,
        env={"GIT_CONFIG_GLOBAL": "/dev/null",
             "GIT_CONFIG_SYSTEM": "/dev/null",
             "HOME": str(cwd),
             "PATH": "/usr/bin:/bin:/usr/local/bin"})


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _run(tmp_path, "git", "init", "-q")
    _run(tmp_path, "git", "config", "user.email", "t@example.com")
    _run(tmp_path, "git", "config", "user.name", "t")
    _run(tmp_path, "git", "config", "core.hooksPath", "hooks")
    (tmp_path / "hooks").mkdir()
    shutil.copy(HOOK, tmp_path / "hooks" / "pre-commit")
    _run(tmp_path, "chmod", "+x", str(tmp_path / "hooks" / "pre-commit"))
    (tmp_path / "README.md").write_text("# test\n")
    _run(tmp_path, "git", "add", "README.md")
    _run(tmp_path, "git", "commit", "-qm", "init")
    return tmp_path


def _stage_commit(repo: Path, filename: str, line: str):
    p = repo / filename
    with p.open("a") as f:
        f.write(line + "\n")
    _run(repo, "git", "add", filename)
    return _run(repo, "git", "commit", "-qm", "x")


@pytest.mark.parametrize("line,term", [
    ("The harvest ran.", "harvest"),
    ("harvesting the corpus", "harvest"),
    ("monster tier results", "monster"),
    ("see PLAN-LEDGER.md", "internal process-doc"),
    ("tracked as S28-99", "sprint worklog"),
])
def test_banned_terms_refuse(repo, line, term):
    r = _stage_commit(repo, "README.md", line)
    assert r.returncode != 0, line
    assert term in r.stderr


def test_allow_marker_permits(repo):
    r = _stage_commit(repo, "README.md",
                      "The harvest ran.  # readme-guard: allow")
    assert r.returncode == 0, r.stderr


def test_clean_readme_commits(repo):
    r = _stage_commit(repo, "README.md",
                      "The full run of the larger corpus.")
    assert r.returncode == 0, r.stderr


def test_non_readme_files_untouched(repo):
    # The banned vocabulary in a docs file is out of this guard's scope
    # (only the README carries the accepted-vocabulary contract).
    (repo / "docs").mkdir()
    r = _stage_commit(repo, "docs/notes.md",
                      "monster harvest S28-99 PLAN-LEDGER.md")
    assert r.returncode == 0, r.stderr

"""The corpus-source guard in hooks/pre-commit (2026-09-19).

Builds a temp git repo with the real hook installed, then asserts:
staged additions under the dataset landing dirs refuse (their .gitignore
placeholder passes), a >=2MB staged file refuses, a >50-file batch under
one directory refuses, and the '# corpus-guard: allow' marker permits the
first two. The guards exist so external repository sources — and test
cases derived from them — never enter tracking; scripts regenerate them
into gitignored dirs.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "hooks" / "pre-commit"
CAP = 2_000_000
BATCH = 50


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


def _stage_and_commit(repo: Path, files: dict[str, str]) -> subprocess.CompletedProcess:
    for name, content in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _run(repo, "git", "add", "-A", ".")
    return _run(repo, "git", "commit", "-qm", "x")


def test_dataset_landing_dir_refuses(repo):
    r = _stage_and_commit(repo, {"extracted-testdata/realworld/case.json": "{}"})
    assert r.returncode != 0
    assert "external/derived corpus data" in r.stderr


@pytest.mark.parametrize("landing", ["external-datasets", "extracted-testdata", "downloads"])
def test_all_landing_dirs_refuse(repo, landing):
    r = _stage_and_commit(repo, {f"{landing}/some-repo/file.c": "int main(){}\n"})
    assert r.returncode != 0, r.stderr


def test_landing_dir_gitignore_placeholder_passes(repo):
    r = _stage_and_commit(
        repo, {"downloads/.gitignore": "*\n!.gitignore\n"})
    assert r.returncode == 0, r.stderr


def test_landing_dir_allow_marker_passes(repo):
    r = _stage_and_commit(
        repo,
        {"extracted-testdata/realworld/case.json": '{}  # corpus-guard: allow\n'})
    assert r.returncode == 0, r.stderr


def test_oversize_file_refuses(repo):
    big = "x" * CAP  # exactly at the cap refuses; below passes
    r = _stage_and_commit(repo, {"docs/big.txt": big})
    assert r.returncode != 0
    assert "cap" in r.stderr


def test_oversize_file_allow_marker_passes(repo):
    content = "x" * (CAP - 40) + "\n# corpus-guard: allow\n"
    r = _stage_and_commit(repo, {"docs/big.txt": content})
    assert r.returncode == 0, r.stderr


def test_just_under_cap_passes(repo):
    r = _stage_and_commit(repo, {"docs/big.txt": "x" * (CAP - 1)})
    assert r.returncode == 0, r.stderr


def test_mass_addition_batch_refuses(repo):
    files = {f"vendor/tree/src/file{i:03d}.c": "int x;\n" for i in range(BATCH + 1)}
    r = _stage_and_commit(repo, files)
    assert r.returncode != 0
    assert "new files staged under" in r.stderr


def test_batch_at_limit_passes(repo):
    files = {f"vendor/tree/src/file{i:03d}.c": "int x;\n" for i in range(BATCH)}
    r = _stage_and_commit(repo, files)
    assert r.returncode == 0, r.stderr


def test_marker_exempt_files_do_not_count_toward_batch(repo):
    files = {
        f"vendor/tree/src/file{i:03d}.c": "int x;\n" for i in range(BATCH)}
    files["vendor/tree/src/extra.c"] = "int x;  # corpus-guard: allow\n"
    r = _stage_and_commit(repo, files)
    assert r.returncode == 0, r.stderr


def test_ordinary_multi_file_commit_passes(repo):
    files = {f"src/mod{i}/file.py": "x = 1\n" for i in range(10)}
    r = _stage_and_commit(repo, files)
    assert r.returncode == 0, r.stderr

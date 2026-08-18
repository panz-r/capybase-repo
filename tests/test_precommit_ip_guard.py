"""hooks/pre-commit — the no-endpoints-in-repo guard.

The hook blocks STAGED ADDITIONS carrying environment-specific endpoint
identifiers: non-loopback IPv4 literals, *.local mDNS hostnames, and
DESKTOP-* machine names. Loopback/any addresses are allowed (they identify
no infrastructure). These tests drive the real hook script against a temp
git repo with staged content.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "hooks" / "pre-commit"


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    return repo


def _stage(repo: Path, content: str) -> None:
    (repo / "f.txt").write_text(content)
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)


def _run_hook(repo: Path) -> subprocess.CompletedProcess:
    if not (HOOK.is_file() and shutil.os.access(HOOK, shutil.os.X_OK)):
        pytest.fail(f"hook missing or not executable: {HOOK}")
    return subprocess.run(
        ["bash", str(HOOK)], cwd=repo, capture_output=True, text=True,
    )


def test_clean_commit_passes(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _stage(repo, "url = http://localhost:8080/v1\nmodel = 'chat'\n")
    p = _run_hook(repo)
    assert p.returncode == 0, p.stderr


def test_loopback_and_any_ips_allowed(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _stage(repo, "a = 127.0.0.1\nb = 0.0.0.0\nc = http://127.0.0.1:8080/v1\n")
    p = _run_hook(repo)
    assert p.returncode == 0, p.stderr


def test_private_lan_ip_blocked(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _stage(repo, 'base_url = "http://192.168.50.235:8086/v1"\n')  # endpoint-guard: allow
    p = _run_hook(repo)
    assert p.returncode == 1
    assert "192.168.50.235" in p.stderr  # endpoint-guard: allow


def test_public_ip_blocked(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _stage(repo, "server = 8.8.8.8\n")  # endpoint-guard: allow
    p = _run_hook(repo)
    assert p.returncode == 1


def test_mdns_hostname_blocked(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _stage(repo, 'url = "http://mybox.local:8085/v1"\n')  # endpoint-guard: allow
    p = _run_hook(repo)
    assert p.returncode == 1
    assert "*.local" in p.stderr


def test_machine_name_blocked(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _stage(repo, "# served on DESKTOP-NOVA\n")  # endpoint-guard: allow
    p = _run_hook(repo)
    assert p.returncode == 1


def test_unstaged_ip_does_not_block(tmp_path: Path) -> None:
    # Only STAGED additions are inspected: a dirty worktree file that was
    # never added must not fail the commit.
    repo = _init_repo(tmp_path)
    _stage(repo, "clean = true\n")
    (repo / "dirty.txt").write_text("http://10.0.0.7:1/v1\n")  # endpoint-guard: allow
    p = _run_hook(repo)
    assert p.returncode == 0, p.stderr


def test_version_numbers_are_not_ips(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _stage(repo, "version = '1.2.3'\nseason = '3.4.5.6.x'\n")  # endpoint-guard: allow
    # 3.4.5.6 IS an IPv4-shaped literal and must be blocked; 1.2.3 is not.  # endpoint-guard: allow
    p = _run_hook(repo)
    assert p.returncode == 1
    assert "3.4.5.6" in p.stderr  # endpoint-guard: allow


def test_allow_marker_permits_intentional_exceptions(tmp_path: Path) -> None:
    # A line carrying the explicit marker is an auditable opt-out (the guard's
    # own test fixtures use it); the SAME line without the marker still blocks.
    repo = _init_repo(tmp_path)
    _stage(repo, "doc = see http://203.0.113.9:9000/v1  # endpoint-guard: allow\n")
    assert _run_hook(repo).returncode == 0

    repo2 = _init_repo(tmp_path / "second")
    (repo2 / "g.txt").write_text("doc = see http://203.0.113.9:9000/v1\n")  # endpoint-guard: allow
    subprocess.run(["git", "add", "g.txt"], cwd=repo2, check=True)
    assert _run_hook(repo2).returncode == 1

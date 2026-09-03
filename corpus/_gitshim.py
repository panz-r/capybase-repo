"""The corpus tests' git helper (copied from tests/conftest.py).

The corpus suite is deliberately OUTSIDE pytest (user directive: corpus
tests never run via pytest — they fetch gigabytes from GitHub and have
their own execution model), so it cannot import from tests/conftest.
This is the same helper, verbatim.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


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

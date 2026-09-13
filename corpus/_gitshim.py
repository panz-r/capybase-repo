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
    # s27-74: hermetic against the machine's global git config —
    # merge.conflictstyle=diff3 changes every fixture's marker shape,
    # rebase.backend=apply breaks rebase-merge detection, gpgsign
    # fails every commit.
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
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

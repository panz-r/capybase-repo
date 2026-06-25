"""Canonical 3-way conflict markers via ``git merge-file --diff3``.

Git's own merge algorithm sometimes resolves adjacent non-conflicting lines
that the worktree's raw ``<<<<<<<`` markers still show as part of the conflict
block. Running ``git merge-file --diff3`` on the three stage blobs produces the
*tightest* text boundaries git itself would use, with the ``|||||||`` base
section inline. This gives the model the minimal conflict region — less
distraction for a 3B model prone to "lost in the middle."

The module is a thin subprocess wrapper: it writes the three blobs to temp
files, runs ``git merge-file --diff3 -p``, and parses the result into
``Diff3Block`` objects (each a minimal conflict with base/ours/theirs sections).
Falls back to ``None`` on any error so the extractor keeps using the worktree
markers.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Diff3Block:
    """One minimal conflict block from ``git merge-file --diff3``.

    ``base`` is the common-ancestor section (between ``|||||||`` and ``=======``);
    ``ours`` is the CURRENT/upstream side; ``theirs`` is the REPLAYED side.
    """

    ours: str
    base: str
    theirs: str


def merge_file_diff3(
    base_text: str,
    ours_text: str,
    theirs_text: str,
) -> list[Diff3Block] | None:
    """Run ``git merge-file --diff3`` and parse the conflict blocks.

    Returns the list of minimal conflict blocks, or ``None`` if git is
    unavailable or the command fails. An empty list means git successfully
    merged the blobs with no conflicts (the sides are compatible) — in that
    case the caller should use the merged result directly.
    """
    try:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            base_p = tdp / "base"
            ours_p = tdp / "ours"
            theirs_p = tdp / "theirs"
            base_p.write_text(base_text, encoding="utf-8")
            ours_p.write_text(ours_text, encoding="utf-8")
            theirs_p.write_text(theirs_text, encoding="utf-8")
            # -p prints to stdout; --diff3 includes the base section; -q suppresses
            # the "CONFLICT" stderr message. Exit 0 = no conflict, 1 = conflict,
            # >1 = error.
            proc = subprocess.run(
                [
                    "git", "merge-file", "--diff3", "-p", "-q",
                    str(ours_p), str(base_p), str(theirs_p),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            # git merge-file exit codes: 0 = clean merge, >0 = number of
            # conflicts found, negative = error. So any non-negative exit is
            # valid output; we parse the stdout to determine the blocks.
            if proc.returncode < 0:
                return None
            if proc.returncode == 0:
                # No conflict — the sides merge cleanly.
                return []
            return _parse_diff3(proc.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _parse_diff3(merged: str) -> list[Diff3Block]:
    """Parse ``git merge-file --diff3`` output into conflict blocks.

    The format is::

        <<<<<<< file
        ours lines
        ||||||| file
        base lines
        =======
        theirs lines
        >>>>>>> file
    """
    blocks: list[Diff3Block] = []
    lines = merged.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("<<<<<<<"):
            # Collect until >>>>>>>
            ours_buf: list[str] = []
            base_buf: list[str] = []
            theirs_buf: list[str] = []
            i += 1
            section = "ours"
            while i < len(lines):
                l = lines[i]
                if l.startswith("|||||||"):
                    section = "base"
                elif l.startswith("======="):
                    section = "theirs"
                elif l.startswith(">>>>>>>"):
                    i += 1
                    break
                else:
                    if section == "ours":
                        ours_buf.append(l)
                    elif section == "base":
                        base_buf.append(l)
                    elif section == "theirs":
                        theirs_buf.append(l)
                i += 1
            blocks.append(
                Diff3Block(
                    ours="\n".join(ours_buf),
                    base="\n".join(base_buf),
                    theirs="\n".join(theirs_buf),
                )
            )
        else:
            i += 1
    return blocks


def is_available() -> bool:
    """True if the ``git`` binary is runnable (``merge-file`` ships with it)."""
    try:
        proc = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, timeout=5
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

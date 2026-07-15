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

Diff algorithm: ``git merge-file`` honors the ``diff.algorithm``
config, which selects the xdiff backend used to align base↔ours and base↔theirs
before the 3-way merge. **Histogram** is the default here — it anchors on rare
(low-frequency) lines, producing more stable, tighter conflict regions than
Myers on noisy code (prior findings a conflict-size reduction in ~10% of
merges that had conflicts). Patience/minimal/myers are selectable fallbacks.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# The four xdiff backends git accepts for ``diff.algorithm``. Histogram is the
# recommended default; the others are selectable fallbacks for
# pathological cases or minimal-edit-script diagnostics. Validated before use so
# an unknown value from config never reaches the subprocess.
DIFF_ALGORITHMS = ("myers", "patience", "histogram", "minimal")
DEFAULT_DIFF_ALGORITHM = "histogram"


def _validated_algorithm(algorithm: str | None) -> str:
    """Return a git-accepted ``diff.algorithm`` value, else the default.

    Rejects anything outside the allowlist (including the empty string) so a
    malformed config value can't inject flags into the subprocess. Falls back to
    the histogram default rather than erroring — marker refinement is advisory.
    """
    if algorithm in DIFF_ALGORITHMS:
        return algorithm
    return DEFAULT_DIFF_ALGORITHM


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
    *,
    diff_algorithm: str | None = None,
) -> list[Diff3Block] | None:
    """Run ``git merge-file --diff3`` and parse the conflict blocks.

    Returns the list of minimal conflict blocks, or ``None`` if git is
    unavailable or the command fails. An empty list means git successfully
    merged the blobs with no conflicts (the sides are compatible) — in that
    case the caller should use the merged result directly.

    ``diff_algorithm`` selects the xdiff backend: one of
    ``DIFF_ALGORITHMS`` (default histogram). It is passed via ``-c
    diff.algorithm=<alg>`` rather than a positional flag because
    ``merge-file`` predates the ``--diff-algorithm`` plumbing option on older
    gits; the ``-c`` form works on every version and is exactly how git's own
    config-driven selection behaves. An unknown value silently falls back to
    histogram — refinement is advisory and must never hard-fail the merge.
    """
    algorithm = _validated_algorithm(diff_algorithm)
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
            # >1 = error. ``-c diff.algorithm=<alg>`` selects the xdiff backend
            # used for the base↔ours/base↔theirs alignment. Note ``-c`` and its
            # value are separate argv elements (no shell) — combining them into
            # one arg makes git reject it as an "unknown option".
            proc = subprocess.run(
                [
                    "git",
                    "-c", f"diff.algorithm={algorithm}",
                    "merge-file", "--diff3", "-p", "-q",
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

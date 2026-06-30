"""Silent-resurrection detection: did a clean merge undo a deliberate deletion?

Git's 3-way merge can resolve *cleanly* (no conflict markers) while resurrecting
dead code the ``onto`` branch deliberately deleted — because the replayed branch
predates the cleanup. Git sees no conflict; capybase historically saw no conflict
either, and the cleanup was silently undone. This module finds that case.

The core logic (:func:`merge_intent.detect_resurrection`) is pure and git-free;
this module is the git layer that feeds it the right blobs:

- :func:`scan_resurrections` — the end-of-rebase scan. For every path the
  ``onto`` branch DELETED since the merge-base (the cleanup intent), fetch the
  base / onto / result blobs and check whether the result brought any of the
  deleted content back.
- :func:`scan_step` — the per-step inline scan, scoped to one replayed commit.

Both return :class:`ResurrectionFinding` records (path, the deleting commit's
subject, the resurrected blocks, similarity). They never raise — git errors are
swallowed and reported as an empty findings list, since resurrection detection
is advisory (it must never break a rebase that would otherwise succeed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from capybase.merge_intent import ResurrectedBlock, detect_resurrection

if TYPE_CHECKING:
    from capybase.git_backend import GitBackend


@dataclass
class ResurrectionFinding:
    """One path where the merge result resurrected deliberately-deleted content.

    ``deleting_commit`` is the subject of the upstream commit that removed the
    content (the cleanup), for the bundle/journal to report. ``blocks`` are the
    specific resurrected regions, largest-first. ``path`` is the repo-relative
    path. Empty ``blocks`` (shouldn't happen post-filter) means a path was a
    candidate but no block cleared the thresholds.
    """

    path: str
    deleting_commit: str = ""
    blocks: list[ResurrectedBlock] = field(default_factory=list)

    @property
    def resurrected_line_count(self) -> int:
        """Total lines of deliberately-deleted content that came back."""
        return sum(b.block_line_count for b in self.blocks)


def scan_resurrections(
    git: "GitBackend",
    *,
    base_oid: str,
    onto_oid: str,
    result_oid: str,
    min_block_lines: int = 3,
    min_coverage: float = 0.85,
    exclude_paths: set[str] | None = None,
) -> list[ResurrectionFinding]:
    """Find content ``onto`` deleted (vs ``base_oid``) that ``result`` resurrected.

    This is the end-of-rebase scan. ``base_oid`` is the merge-base of the
    original branch and ``onto`` (the common ancestor — before either side
    diverged). ``onto_oid`` is the upstream tip (the deletion intent lives in
    ``base_oid..onto_oid``). ``result_oid`` is the post-rebase HEAD.

    For each path ``onto`` changed since the merge-base (deleted OR modified — a
    cleanup can delete a block *within* a file, not just a whole file), we fetch
    the base/onto/result blobs and run the pure :func:`detect_resurrection`. The
    block-level detection (not just whole-file) is what catches the
    edit_file.rs-style case: the file still exists, but a block the upstream
    removed came back.

    ``exclude_paths`` are paths the caller ALREADY reviewed and deliberately
    kept — typically a modify/delete conflict resolved via block-capture's
    ``keep_block``. Such a keep IS the deliberate resurrection of content
    upstream deleted; it was judged explicitly (not silently), so flagging it
    here would double-report an already-reviewed decision. Pass those paths to
    suppress the silent-resurrection finding for them.

    Returns one :class:`ResurrectionFinding` per path with a hit, sorted by
    resurrected-line count (largest first). Empty when ``onto`` changed nothing
    or none of the deletions came back — the common, safe case. Never raises:
    git errors are swallowed (advisory detection must not break a rebase).
    """
    # The paths to inspect = paths onto CHANGED since the merge-base. A cleanup
    # can be a whole-file deletion OR an intra-file block deletion; both express
    # a deletion intent the pure detect_resurrection can find at block level.
    excluded = exclude_paths or set()
    candidate_paths = _changed_paths(git, base_oid, onto_oid)
    findings: list[ResurrectionFinding] = []
    for path in candidate_paths:
        if path in excluded:
            continue
        base_blob = _blob_text(git, base_oid, path)
        onto_blob = _blob_text(git, onto_oid, path)
        result_blob = _blob_text(git, result_oid, path)
        if base_blob is None or result_blob is None:
            continue  # can't be a resurrection of base content if base/result absent
        # onto_blob may be None (whole-file deletion) or present (block deletion
        # within a still-existing file). detect_resurrection takes onto's text;
        # a None blob means the file was wholly removed → empty "ours" text.
        blocks = detect_resurrection(
            base_blob,
            onto_blob or "",  # onto's content (empty if the whole file was removed)
            result_blob,
            min_block_lines=min_block_lines,
            min_coverage=min_coverage,
        )
        if blocks:
            subject = _deleting_commit_subject(git, base_oid, onto_oid, path)
            findings.append(
                ResurrectionFinding(
                    path=path, deleting_commit=subject, blocks=blocks
                )
            )
    findings.sort(key=lambda f: f.resurrected_line_count, reverse=True)
    return findings


def scan_step(
    git: "GitBackend",
    *,
    step_oid: str,
    base_oid: str,
    onto_oid: str,
    min_block_lines: int = 3,
    min_coverage: float = 0.85,
    exclude_paths: set[str] | None = None,
) -> list[ResurrectionFinding]:
    """Per-step resurrection scan: did replaying one commit resurrect a deletion?

    Scoped to a single replayed commit (``step_oid``), this checks whether that
    commit's result (the tree after the step was applied) brought back content
    ``onto`` deleted. ``base_oid`` is the merge-base bounding the window. Runs
    on the same deletion-paths logic as :func:`scan_resurrections` but with the
    step's tree as the ``result``. Returns findings sorted largest-first.
    """
    return scan_resurrections(
        git,
        base_oid=base_oid,
        onto_oid=onto_oid,
        result_oid=step_oid,
        min_block_lines=min_block_lines,
        min_coverage=min_coverage,
        exclude_paths=exclude_paths,
    )


# ---------------------------------------------------------------------------
# git helpers (never raise; advisory detection must not break a rebase)
# ---------------------------------------------------------------------------


def _changed_paths(git: "GitBackend", base_oid: str, onto_oid: str) -> list[str]:
    """Paths that differ between ``base_oid`` and ``onto_oid``.

    Uses ``git diff --name-only`` between the two revisions. We return ALL
    changed paths (not just deletions): a cleanup can delete a block *within* a
    still-existing file, and the pure :func:`detect_resurrection` finds those
    intra-file block deletions at block level. Pure additions (base absent) are
    filtered out upstream (``base_blob is None`` skip), since they can't be a
    resurrection of base content. Returns ``[]`` on any error.
    """
    return git.files_changed_between(base_oid, onto_oid)


def _blob_text(git: "GitBackend", rev: str, path: str) -> str | None:
    """Decoded content of ``path`` at ``rev``, or None if absent."""
    raw = git.blob_at(rev, path)
    if raw is None:
        return None
    return raw.decode("utf-8", errors="replace")


def _deleting_commit_subject(
    git: "GitBackend", base_oid: str, onto_oid: str, path: str
) -> str:
    """The subject of the commit in ``base_oid..onto_oid`` that removed ``path``.

    For the bundle/journal attribution ("removed by <commit>"). Advisory: empty
    string on any failure.
    """
    try:
        out = git._run_ok(  # noqa: SLF001
            ["log", "-1", "--format=%s", f"{base_oid}..{onto_oid}", "--", path],
            what="git log (deleting commit)",
        ).strip()
        return out
    except Exception:  # noqa: BLE001 - advisory attribution
        return ""

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

from capybase.merge_intent import (
    ResurrectedBlock,
    classify_deletion_stability,
    detect_resurrection,
)

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
    replayed_oid: str | None = None,
    min_block_lines: int = 3,
    min_coverage: float = 0.85,
    history_depth: int = 50,
    exclude_paths: set[str] | None = None,
) -> list[ResurrectionFinding]:
    """Find content either side deleted (vs ``base_oid``) that ``result`` resurrected.

    This is the end-of-rebase scan. ``base_oid`` is the merge-base of the
    original branch and ``onto`` (the common ancestor — before either side
    diverged). ``onto_oid`` is the upstream tip. ``result_oid`` is the
    post-rebase HEAD. ``replayed_oid`` (optional) is the original branch tip —
    when provided, BOTH sides' deletions are checked (the replayed branch may
    have its own cleanups the merge could undo).

    For each path a branch changed since the merge-base (deleted OR modified —
    a cleanup can delete a block *within* a file, not just a whole file), we
    fetch the base/side/result blobs and run the pure
    :func:`detect_resurrection` to find candidate resurrected blocks. Then,
    when ``history_depth > 0``, each candidate is verified against the deleting
    branch's bounded commit history via :func:`classify_deletion_stability`:
    only **stable** deletions (removed and never re-added on that branch) are
    flagged. Transient absences (deleted then re-added) are filtered out,
    reducing false positives.

    ``exclude_paths`` are paths the caller ALREADY reviewed and deliberately
    kept — typically a modify/delete conflict resolved via block-capture's
    ``keep_block``.

    Returns one :class:`ResurrectionFinding` per path with a hit, sorted by
    resurrected-line count (largest first). Empty when nothing was deleted or
    none of the deletions came back — the common, safe case. Never raises.
    """
    excluded = exclude_paths or set()
    findings: list[ResurrectionFinding] = []

    # Check both sides: onto (upstream) and replayed (feature branch).
    sides: list[tuple[str, str]] = [(onto_oid, "onto")]
    if replayed_oid and replayed_oid != onto_oid:
        sides.append((replayed_oid, "replayed"))

    for side_oid, side_label in sides:
        candidate_paths = _changed_paths(git, base_oid, side_oid)
        for path in candidate_paths:
            if path in excluded:
                continue
            base_blob = _blob_text(git, base_oid, path)
            side_blob = _blob_text(git, side_oid, path)
            result_blob = _blob_text(git, result_oid, path)
            if base_blob is None or result_blob is None:
                continue
            candidates = detect_resurrection(
                base_blob,
                side_blob or "",
                result_blob,
                min_block_lines=min_block_lines,
                min_coverage=min_coverage,
            )
            if not candidates:
                continue
            # Convergent-add filter: if the OTHER side independently ADDED the
            # candidate block's content (relative to base), both branches
            # converged on the same addition — not a resurrection. Skip it.
            #
            # Crucially, we compare against the other side's ADDED lines, not
            # its full blob. A resurrection's block came from base, so it also
            # appears verbatim in any side that didn't touch it — checking the
            # full blob would wrongly classify every genuine resurrection as a
            # "convergent addition" (the original bug: onto deleted dead(),
            # replayed never touched it, so replayed's blob still contained it).
            other_oid = replayed_oid if side_label == "onto" else onto_oid
            other_blob = _blob_text(git, other_oid, path) if other_oid else None
            other_added_lines = _added_lines(base_blob or "", other_blob or "")
            # History-walk stability verification: for each candidate block,
            # check whether the deletion was stable on this branch.
            blob_seq = None
            if history_depth > 0 and candidates:
                blob_seq = git.blob_sequence(
                    base_oid, side_oid, path, max_depth=history_depth,
                )
            stable_blocks: list[ResurrectedBlock] = []
            for blk in candidates:
                block_lines = blk.text.splitlines()
                # Convergent-add check: if ALL of the block's non-blank lines
                # were independently ADDED by the other side (not just carried
                # forward from base), both sides converged on the same addition
                # — not a resurrection. Requires 100% match (not 80%) to avoid
                # suppressing genuine resurrections whose block is dominated by
                # generic boilerplate that the other side independently added
                # elsewhere (false negative on the resurrection guard).
                block_nonblank = [ln for ln in block_lines if ln.strip()]
                if block_nonblank and other_added_lines:
                    in_other = sum(
                        1 for ln in block_nonblank if ln in other_added_lines
                    )
                    if in_other == len(block_nonblank):
                        continue  # convergent addition, not a resurrection
                if blob_seq:
                    stability = classify_deletion_stability(
                        block_lines, blob_seq, min_coverage=min_coverage,
                    )
                else:
                    stability = "stable"  # no history available; can't refine
                if stability == "stable":
                    # Enrich the block with stability info for the journal.
                    enriched = ResurrectedBlock(
                        text=blk.text,
                        base_span=blk.base_span,
                        coverage=blk.coverage,
                        result_line_count=blk.result_line_count,
                        block_line_count=blk.block_line_count,
                        extra={
                            **blk.extra,
                            "stability": stability,
                            "deleting_side": side_label,
                        },
                    )
                    stable_blocks.append(enriched)
            if stable_blocks:
                subject = _deleting_commit_subject(git, base_oid, side_oid, path)
                findings.append(
                    ResurrectionFinding(
                        path=path, deleting_commit=subject, blocks=stable_blocks
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
    replayed_oid: str | None = None,
    min_block_lines: int = 3,
    min_coverage: float = 0.85,
    history_depth: int = 50,
    exclude_paths: set[str] | None = None,
) -> list[ResurrectionFinding]:
    """Per-step resurrection scan: did replaying one commit resurrect a deletion?

    Scoped to a single replayed commit (``step_oid``), this checks whether that
    commit's result (the tree after the step was applied) brought back content
    either side deleted. ``base_oid`` is the merge-base bounding the window.
    Runs on the same deletion-paths + stability logic as
    :func:`scan_resurrections` but with the step's tree as the ``result``.
    Returns findings sorted largest-first.
    """
    return scan_resurrections(
        git,
        base_oid=base_oid,
        onto_oid=onto_oid,
        result_oid=step_oid,
        replayed_oid=replayed_oid,
        min_block_lines=min_block_lines,
        min_coverage=min_coverage,
        history_depth=history_depth,
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


def _added_lines(base: str, side: str) -> set[str]:
    """Non-blank lines ``side`` introduced relative to ``base``.

    Used by the convergent-add filter: a candidate block is only a convergent
    addition if the OTHER side actually ADDED those lines, not merely carried
    them forward unchanged from base. Uses difflib to isolate the inserted /
    replaced-into lines, so content present in base (and thus in any side that
    didn't edit it) is excluded — which is what distinguishes a genuine
    convergent addition from a plain resurrection.
    """
    import difflib

    if not side:
        return set()
    base_lines = base.splitlines()
    side_lines = side.splitlines()
    added: set[str] = set()
    matcher = difflib.SequenceMatcher(a=base_lines, b=side_lines, autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            for ln in side_lines[j1:j2]:
                if ln.strip():
                    added.add(ln)
    return added


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

"""Reusable multi-commit rebase fixture builder.

The single biggest history-aware test gap: **no test drives a real multi-commit
rebase** (conflicts at *different* commits in the replay sequence). Every conftest
fixture builds exactly one replayed commit onto one upstream change, so the core
"history-aware" scenario — "this conflict is at commit 2 of 5; commit 4 also
touches this region" — was impossible to construct end-to-end.

This module builds a real git history with N commits on a feature branch and M
commits on the target branch, then drives a genuine ``git rebase main`` so the
index/worktree reflect authentic unmerged state at *each* stop. A commit that
edits a region the corresponding upstream commit also edited produces a conflict
at that replay step.

All commits use pinned ``GIT_AUTHOR_DATE`` (via the conftest ``git()`` helper) so
the replayed OID sequence + the persisted ``rebase_plan.json`` are deterministic
and assertable across runs.

Pure helper (no pytest), importable by any test. The ``git()`` helper is imported
from conftest to keep one source of truth for deterministic git invocation.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class _GitLike(Protocol):
    """Matches conftest.git — pins identity/date for deterministic commits."""

    def __call__(
        self, repo: Path, *args: str, input_text: str | None = ..., check: bool = ...
    ) -> subprocess.CompletedProcess: ...


@dataclass(frozen=True)
class CommitEdit:
    """One commit's worth of file edits.

    ``files`` maps repo-relative path → full new content (the file is written
    wholesale). A commit editing a region the corresponding upstream commit also
    edited produces a conflict at that replay step. ``message`` is the commit
    subject (used by history features: region heuristics, branch intent).
    """

    message: str
    files: dict[str, str] = field(default_factory=dict)


@dataclass
class MultiStepRebase:
    """A built multi-commit rebase scenario.

    ``conflicts_at`` are the 1-based replay-step indices git reports as
    conflicted (the steps where a feat edit collided with a main edit).
    ``replayed_oids`` is the feat branch's commits in replay order (oldest-first),
    matching what ``RebasePlan.source_commits`` will hold.
    """

    repo: Path
    conflicts_at: list[int] = field(default_factory=list)
    base_tip: str = ""        # the merge-base commit OID
    feat_tip: str = ""        # source tip (pre-rebase HEAD)
    main_tip: str = ""        # target tip (``git rebase main`` target)
    replayed_oids: list[str] = field(default_factory=list)
    rebase_in_progress: bool = False  # True when left stopped mid-replay


def _apply_commit(repo: Path, git_fn: _GitLike, edit: CommitEdit) -> None:
    """Write the edit's files, stage, and commit."""
    for path, content in edit.files.items():
        full = repo / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
    git_fn(repo, "add", "-A")
    git_fn(repo, "commit", "-q", "-m", edit.message)


def _resolve_conflict_step(repo: Path, git_fn: _GitLike, edit: CommitEdit) -> None:
    """Resolve a conflict at the current rebase step by overwriting with the
    feat-side content and continuing.

    For the builder's own setup we resolve conflicts by taking the feat branch's
    version verbatim (we control both sides, so the feat edit is the "intended"
    replay). Tests that want to exercise capybase's resolution instead pass
    ``stop_early=True`` and hand the stopped rebase to the orchestrator.
    """
    for path, content in edit.files.items():
        full = repo / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
    git_fn(repo, "add", "-A")
    # Continue may hit the NEXT conflict; the caller loops.
    git_fn(repo, "rebase", "--continue", check=False)


def build_multistep_rebase(
    repo: Path,
    *,
    base_files: dict[str, str],
    feat_commits: list[CommitEdit],
    main_commits: list[CommitEdit],
    stop_early: bool = False,
    git_fn: _GitLike | None = None,
) -> MultiStepRebase:
    """Construct a real multi-commit rebase scenario.

    Layout produced:
    1. A base commit with ``base_files``.
    2. A ``main`` branch with ``main_commits`` applied (the target).
    3. A ``feat`` branch from base with ``feat_commits`` applied (the source).
    4. ``git checkout feat && git rebase main`` — replays the feat commits onto
       main, stopping at each conflict.

    Args:
        base_files: the shared starting content (written at the base commit).
        feat_commits: the replayed branch's commits, oldest-first.
        main_commits: the target branch's commits, oldest-first.
        stop_early: when True, leave the rebase stopped at the FIRST conflict
            (genuine ``rebase-merge/`` state — the precondition for the
            ``run()`` entry point + lazy history build). When False, the builder
            resolves each conflict by taking the feat side verbatim and continues
            until the rebase completes (clean repo, history recorded).
        git_fn: the conftest ``git()`` helper for deterministic commits. Defaults
            to importing it lazily so this module is usable without conftest
            loaded (tests pass it explicitly).

    Returns a :class:`MultiStepRebase` describing the scenario. The replayed OID
    sequence is captured from the feat branch BEFORE the rebase mutates it.
    """
    if git_fn is None:
        from tests.conftest import git as git_fn  # type: ignore[assignment]

    # 1. Base commit on main.
    _apply_commit(repo, git_fn, CommitEdit(message="base", files=dict(base_files)))
    base_tip = git_fn(repo, "rev-parse", "HEAD").stdout.strip()

    # 2. main branch: apply main_commits.
    for edit in main_commits:
        _apply_commit(repo, git_fn, edit)
    main_tip = git_fn(repo, "rev-parse", "HEAD").stdout.strip()

    # 3. feat branch from base: branch off, apply feat_commits.
    git_fn(repo, "checkout", "-q", "-b", "feat", base_tip)
    for edit in feat_commits:
        _apply_commit(repo, git_fn, edit)
    feat_tip = git_fn(repo, "rev-parse", "HEAD").stdout.strip()
    # Capture the feat commit OIDs in replay order (oldest-first), BEFORE the
    # rebase rewrites them. These match RebasePlan.source_commits post-rebase up
    # to OID rewriting (the subjects/order are stable).
    rev_list = git_fn(
        repo, "rev-list", "--reverse", f"{base_tip}..{feat_tip}"
    ).stdout.strip()
    replayed_oids = [o for o in rev_list.split("\n") if o]

    # 4. Rebase feat onto main.
    conflicts_at: list[int] = []
    git_fn(repo, "checkout", "-q", "feat")
    # The rebase replays feat commits one at a time. We drive it and record which
    # steps conflict by checking for unmerged paths after each potential stop.
    proc = git_fn(repo, "rebase", "main", check=False)
    step = 1
    while True:
        # Is there a conflict right now? Check for unmerged paths.
        status = git_fn(
            repo, "status", "--porcelain", check=False
        ).stdout
        has_conflict = any(
            line.startswith(("UU", "AA", "DD", "AU", "UA", "DU", "UD"))
            for line in status.splitlines()
        )
        if has_conflict:
            conflicts_at.append(step)
            if stop_early:
                # Leave it stopped at the first conflict — genuine rebase state.
                return MultiStepRebase(
                    repo=repo, conflicts_at=conflicts_at,
                    base_tip=base_tip, feat_tip=feat_tip, main_tip=main_tip,
                    replayed_oids=replayed_oids, rebase_in_progress=True,
                )
            # Resolve by taking the feat commit's intended content, then continue.
            # The feat commit at this step is replayed_oids[step-1]; we don't have
            # its tree directly post-rebase, but the feat-side content for this
            # step is feat_commits[step-1].files (what the replay intended).
            _resolve_conflict_step(repo, git_fn, feat_commits[step - 1])
            step += 1
            continue
        # No conflict: the rebase either finished or is mid-clean-replay. Check
        # whether a rebase is still in progress.
        rip = _rebase_in_progress(repo, git_fn)
        if not rip:
            break  # rebase completed
        # Mid-clean-replay: a feat commit applied cleanly, advance the step count.
        step += 1
        # Drive the next step (git rebase continues automatically on clean apply,
        # but if we're here a rebase IS in progress without a conflict — that's an
        # unusual state; nudge with --continue to be safe).
        git_fn(repo, "rebase", "--continue", check=False)

    return MultiStepRebase(
        repo=repo, conflicts_at=conflicts_at,
        base_tip=base_tip, feat_tip=feat_tip, main_tip=main_tip,
        replayed_oids=replayed_oids, rebase_in_progress=False,
    )


def _rebase_in_progress(repo: Path, git_fn: _GitLike) -> bool:
    """True when a rebase-merge/ state dir exists (a rebase is stopped)."""
    res = git_fn(
        repo, "rev-parse", "--git-path", "rebase-merge", check=False
    )
    if res.returncode != 0:
        return False
    path = res.stdout.strip()
    return (repo / path).is_dir()

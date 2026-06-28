"""Subprocess-only Git backend.

This is the *only* layer permitted to invoke git. The orchestrator depends on
its stable method surface so that git mechanics never leak into resolution,
verification, or risk logic.

Stage mapping (git's unmerged index entries)::

    stage 1 -> BASE                  (merge base / common ancestor)
    stage 2 -> CURRENT_UPSTREAM_SIDE (the branch being rebased onto)
    stage 3 -> REPLAYED_COMMIT_SIDE  (the commit being replayed)

We deliberately avoid "ours"/"theirs" terminology in high-level logs because
their meaning flips during rebase vs. merge and is a frequent source of
mistakes. The typed ``SideLabel`` vocabulary in conflict_model is authoritative.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    """A git command failed or returned unexpected output."""


@dataclass(frozen=True)
class UnmergedPath:
    """One entry from ``git ls-files -u``.

    ``mode`` is the raw unmerged mode string, e.g. ``UU`` for both-modified,
    ``AU``/``UA`` for added-by-us/them, etc. ``stages`` maps present stage
    numbers (1/2/3) to blob oids.
    """

    path: str
    mode: str
    stages: dict[int, str]  # stage -> blob oid


@dataclass(frozen=True)
class GitResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str

    def ensure_ok(self, action: str) -> None:
        if not self.ok:
            raise GitError(
                f"git {action} failed (rc={self.returncode}): {self.stderr.strip() or self.stdout.strip()}"
            )


STAGE_BASE = 1
STAGE_CURRENT = 2
STAGE_REPLAYED = 3


class GitBackend:
    def __init__(self, repo: str | Path = ".", *, check_git: bool = True) -> None:
        self.repo = Path(repo).resolve()
        if check_git:
            self._run_ok(["rev-parse", "--git-dir"], what="rev-parse")

    # ------------------------------------------------------------------ low level

    def _run(
        self,
        args: list[str],
        *,
        what: str = "",
        check: bool = False,
        input_bytes: bytes | None = None,
        env: dict[str, str] | None = None,
        capture: bool = True,
    ) -> GitResult:
        full_env = os.environ.copy()
        # Force a stable, parseable locale and disable any pager.
        full_env.setdefault("LC_ALL", "C")
        full_env["GIT_PAGER"] = "cat"
        full_env["PAGER"] = "cat"
        if env:
            full_env.update(env)
        cmd = ["git", "-C", str(self.repo), *args]
        proc = subprocess.run(
            cmd,
            input=input_bytes,
            env=full_env,
            capture_output=capture,
        )
        res = GitResult(
            ok=proc.returncode == 0,
            returncode=proc.returncode,
            stdout=proc.stdout.decode("utf-8", errors="replace") if capture else "",
            stderr=proc.stderr.decode("utf-8", errors="replace") if capture else "",
        )
        if check:
            res.ensure_ok(what or args[0])
        return res

    def _run_ok(self, args: list[str], *, what: str = "", input_bytes: bytes | None = None) -> str:
        res = self._run(args, what=what, check=True, input_bytes=input_bytes)
        return res.stdout

    # ------------------------------------------------------------------ queries

    def is_inside_worktree(self) -> bool:
        return self._run(["rev-parse", "--is-inside-work-tree"]).ok

    def head_oid(self) -> str:
        return self._run_ok(["rev-parse", "HEAD"], what="rev-parse HEAD").strip()

    def current_branch(self) -> str | None:
        """The checked-out branch name, or ``None`` if HEAD is detached.

        ``git symbolic-ref --quiet HEAD`` returns the branch ref
        (``refs/heads/<name>``) on success and exits non-zero on detached HEAD.
        """
        res = self._run(["symbolic-ref", "--quiet", "HEAD"])
        if not res.ok:
            return None
        ref = res.stdout.strip()
        # symbolic-ref prints the full refname; strip the heads/ prefix.
        prefix = "refs/heads/"
        return ref[len(prefix):] if ref.startswith(prefix) else ref

    def resolve_ref(self, ref: str) -> str | None:
        """Resolve ``ref`` to an object id, or ``None`` if it doesn't exist.

        Unlike :meth:`ref_exists` (bool), this returns the actual oid. Accepts
        anything ``git rev-parse`` does: branch names, tags, oids, ``HEAD``,
        ``HEAD~3``, etc.
        """
        res = self._run(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
        if not res.ok:
            return None
        oid = res.stdout.strip()
        return oid or None

    def is_ancestor(self, maybe_ancestor: str, descendant: str) -> bool:
        """True if ``maybe_ancestor`` is an ancestor of ``descendant``.

        Used for fast-forward / up-to-date detection. Wraps
        ``git merge-base --is-ancestor``. Never raises: an unresolvable ref is
        treated as "not an ancestor" (returns False), matching git's own exit.
        """
        res = self._run(["merge-base", "--is-ancestor", maybe_ancestor, descendant])
        return res.ok

    def operation_in_progress(self) -> str | None:
        """The kind of in-progress git operation, or ``None`` if the repo is idle.

        Broader than :meth:`rebase_in_progress` (rebase-only): detects an
        ongoing rebase, merge, cherry-pick, revert, or bisect so the rebase
        preflight can refuse to run on top of a half-finished operation.

        Returns a stable short label (``"rebase"``, ``"merge"``,
        ``"cherry-pick"``, ``"revert"``, ``"bisect"``) suitable for messaging.
        """
        # Each in-progress op leaves a sentinel file under .git. Resolve them via
        # rev-parse --git-path so a worktree-linked repo (common .git elsewhere)
        # is handled correctly.
        for label, sentinel in (
            ("rebase", "rebase-merge"),
            ("rebase", "rebase-apply"),
            ("merge", "MERGE_HEAD"),
            ("cherry-pick", "CHERRY_PICK_HEAD"),
            ("revert", "REVERT_HEAD"),
            ("bisect", "BISECT_LOG"),
        ):
            r = self._run(["rev-parse", "--git-path", sentinel])
            if not r.ok:
                continue
            rel = r.stdout.strip()
            if not rel:
                continue
            p = Path(rel) if Path(rel).is_absolute() else (self.repo / rel)
            if p.exists():
                return label
        return None

    def git_version(self) -> tuple[int, int]:
        """The installed git version as a ``(major, minor)`` tuple.

        ``(0, 0)`` if git can't be queried or its output can't be parsed — the
        preflight then warns rather than crashing.
        """
        res = self._run(["--version"])
        if not res.ok:
            return (0, 0)
        out = res.stdout.strip()
        # "git version 2.43.0" -> parse the first major.minor.
        for tok in out.split():
            if tok and tok[0].isdigit():
                parts = tok.split(".")
                try:
                    return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
                except ValueError:
                    continue
        return (0, 0)

    def worktree_is_clean(self) -> bool:
        """True if the worktree has no user changes.

        capybase's own artifact tree (``.rebase-agent/``) is excluded — it's
        capybase's bookkeeping (journal, session dir), not the developer's
        uncommitted work, and the Orchestrator writes it before ``rebase`` runs
        its clean check. A pathspec exclusion (not ``.gitignore``) is used so we
        don't assume the user has gitignored it.
        """
        out = self._run_ok(
            ["status", "--porcelain", "--", ":(exclude).rebase-agent"],
            what="status --porcelain",
        )
        return out.strip() == ""

    def require_clean_worktree(self) -> None:
        if not self.worktree_is_clean():
            raise GitError(
                "worktree is not clean; capybase requires a clean tree before "
                "starting (commit or stash your changes)"
            )

    def rebase_in_progress(self) -> bool:
        # During a rebase, .git/rebase-merge or .git/rebase-apply exists.
        # ``git rev-parse --git-path`` returns a path relative to the repo
        # root, so resolve it there (not against cwd).
        for kind in ("rebase-merge", "rebase-apply"):
            r = self._run(["rev-parse", "--git-path", kind])
            if not r.ok:
                continue
            rel = r.stdout.strip()
            if not rel:
                continue
            p = (self.repo / rel) if not Path(rel).is_absolute() else Path(rel)
            if p.exists():
                return True
        return False

    # ------------------------------------------------------------------ refs / backup

    def ref_exists(self, ref: str) -> bool:
        return self._run(["rev-parse", "--verify", "--quiet", ref]).ok

    def create_ref(self, ref: str, target: str) -> None:
        self._run_ok(
            ["update-ref", ref, target], what=f"update-ref {ref}"
        )

    def create_session_refs(self, session_id: str, start_oid: str) -> None:
        """Create refs/rebase-agent/<session>/start pointing at ``start_oid``."""
        ref = f"refs/rebase-agent/{session_id}/start"
        self.create_ref(ref, start_oid)

    def record_step_ref(self, session_id: str, step: int, oid: str) -> None:
        ref = f"refs/rebase-agent/{session_id}/step-{step}"
        self.create_ref(ref, oid)

    # ------------------------------------------------------------------ user-visible backup branches

    #: The namespace under which user-visible backup branches live. These are
    #: real branches (not refs/rebase-agent/...) so they show up in
    #: ``git branch`` and the user can ``git reset --hard`` or delete them with
    #: ordinary commands. The internal recovery refs (above) are capybase's
    #: own audit trail; this is the safety net the user is meant to see.
    BACKUP_NAMESPACE = "refs/heads/capybase/backup"

    def create_backup_ref(self, source_oid: str, label: str) -> str:
        """Create a user-visible backup branch at ``source_oid`` and return its ref.

        The branch is named ``capybase/backup/<label>@<timestamp>`` so it sorts
        by time and is self-describing in ``git branch`` output. ``label`` is
        sanitised to a branch-name-safe slug (typically the current branch).
        """
        import time

        slug = _branch_slug(label)
        ts = time.strftime("%Y%m%d-%H%M%S")
        ref = f"{self.BACKUP_NAMESPACE}/{slug}@{ts}"
        self._run_ok(["update-ref", ref, source_oid], what=f"create backup {ref}")
        return ref

    def list_backup_refs(self) -> list[str]:
        """All backup branch short-names (e.g. ``capybase/backup/main@20260101-000000``)."""
        out = self._run_ok(
            ["for-each-ref", "--format=%(refname:short)", self.BACKUP_NAMESPACE],
            what="for-each-ref backups",
        )
        return [r for r in out.splitlines() if r.strip()]

    def delete_ref(self, ref: str) -> None:
        """Delete a ref, restricted to the backup namespace as a safety rail.

        Refuses anything outside ``refs/heads/capybase/backup/`` (short or full
        form) so a stray call can never delete the user's real branches or
        capybase's recovery refs. Raises :class:`GitError` on a namespace
        violation.
        """
        short = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        full = ref if ref.startswith("refs/") else f"refs/heads/{short}"
        if not full.startswith(self.BACKUP_NAMESPACE + "/"):
            raise GitError(
                f"delete_ref refuses to delete {ref!r}: only refs under "
                f"{self.BACKUP_NAMESPACE}/ may be deleted via this method"
            )
        self._run_ok(["update-ref", "-d", full], what=f"delete ref {full}")

    # ------------------------------------------------------------------ worktrees (dry-run)

    def add_worktree(
        self, path: str | Path, *, new_branch: str | None = None, detach: bool = False
    ) -> GitResult:
        """Create a linked worktree at ``path``.

        Either create it on ``new_branch`` starting at the current HEAD (used by
        dry-run so the rehearsal has its own throwaway branch and never moves the
        user's branch) or ``--detach`` at HEAD. Shares the object store, so this
        is cheap even for large repos.
        """
        # Exactly one mode: a new throwaway branch, or a detached HEAD. Reject
        # both-set (ambiguous) and neither-set (would check out HEAD on the
        # current branch inside the worktree, which can't be what a dry-run wants).
        if (new_branch is not None) == detach:
            raise ValueError("add_worktree: pass exactly one of new_branch / detach")
        args = ["worktree", "add"]
        if new_branch is not None:
            args += ["-b", new_branch]
        elif detach:
            args += ["--detach"]
        args += [str(path), "HEAD"]
        return self._run(args, what="worktree add")

    def remove_worktree(self, path: str | Path, *, force: bool = True) -> GitResult:
        """Remove a linked worktree. ``force`` (default) discards a dirty tree."""
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(path))
        return self._run(args, what="worktree remove")

    def prune_worktrees(self) -> GitResult:
        """Prune stale worktree administrative entries (after a forced removal)."""
        return self._run(["worktree", "prune"], what="worktree prune")

    # ------------------------------------------------------------------ rebase control

    def start_rebase(self, target: str, *, autostash: bool = False) -> GitResult:
        # ``--no-autostash`` is the safe default: capybase must never silently
        # move a developer's uncommitted work into a stash it might lose. The
        # ``autostash`` opt-in (``capybase rebase --autostash``) mirrors ``git
        # rebase --autostash`` for users who explicitly accept the stash dance.
        args = ["rebase", "--autostash" if autostash else "--no-autostash", target]
        return self._run(args, what=f"rebase {target}")

    def continue_rebase(self) -> GitResult:
        return self._run(
            ["rebase", "--continue"],
            what="rebase --continue",
            # GIT_EDITOR=true makes git rebase --continue proceed without an
            # editor when the commit message is unchanged.
            env={"GIT_EDITOR": "true"},
        )

    def abort_rebase(self) -> GitResult:
        return self._run(["rebase", "--abort"], what="rebase --abort")

    # ------------------------------------------------------------------ unmerged state

    def list_unmerged_paths(self) -> list[UnmergedPath]:
        """List unmerged paths via ``git ls-files -u -z``.

        Output columns per record: ``<mode> <oid> <stage>\t<path>``. We group
        by path and synthesize a mode string from the set of stages present.
        """
        out = self._run_ok(["ls-files", "-u", "-z"], what="ls-files -u -z")
        # Output is NUL-separated records of the form
        # "<mode> <oid> <stage>\t<path>".
        by_path: dict[str, UnmergedPath] = {}
        for record in out.split("\0"):
            if not record.strip():
                continue
            meta, _, path = record.partition("\t")
            if not path:
                continue
            parts = meta.split()
            if len(parts) != 3:
                continue
            _mode_hex, oid, stage_str = parts
            try:
                stage = int(stage_str)
            except ValueError:
                continue
            existing = by_path.get(path)
            stages = dict(existing.stages) if existing else {}
            stages[stage] = oid
            by_path[path] = UnmergedPath(path=path, mode=_synthesize_mode(stages), stages=stages)
        return list(by_path.values())

    def read_stage_blob(self, path: str, stage: int) -> bytes:
        """Return the raw bytes of ``path`` at the given unmerged stage."""
        try:
            return self._run_raw(["show", f":{stage}:{path}"])
        except GitError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise GitError(f"could not read stage {stage} of {path!r}: {exc}") from exc

    def _run_raw(self, args: list[str]) -> bytes:
        cmd = ["git", "-C", str(self.repo), *args]
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            raise GitError(
                f"git {args[0]} failed (rc={proc.returncode}): "
                f"{proc.stderr.decode('utf-8', errors='replace').strip()}"
            )
        return proc.stdout

    def read_worktree_file(self, path: str) -> bytes:
        full = self.repo / path
        return full.read_bytes()

    def write_worktree_file(self, path: str, data: bytes) -> None:
        full = self.repo / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)

    def stage_paths(self, paths: list[str]) -> None:
        if not paths:
            return
        self._run_ok(["add", "--", *paths], what="git add")

    def staged_paths(self) -> list[str]:
        out = self._run_ok(
            ["diff", "--cached", "--name-only", "-z"],
            what="diff --cached --name-only",
        )
        return [p for p in out.split("\0") if p]

    def last_touch(self, path: str, *, ref: str = "HEAD") -> tuple[str, str]:
        """Return ``(commit_sha, commit_subject)`` of the commit at ``ref`` that
        last touched ``path``. Used for conflict provenance (survey §3.3):
        attributing each side of a conflict to the commit that introduced it.

        Returns ``("", "")`` when git has no history for the path (e.g. a
        brand-new untracked conflict) so callers never crash on missing provenance.
        Never raises — provenance is advisory metadata, not load-bearing.
        """
        try:
            out = self._run_ok(
                ["log", "-1", "--format=%H%x09%s", ref, "--", path],
                what="git log (last_touch)",
            ).strip()
        except Exception:  # noqa: BLE001 - provenance is advisory; never crash
            return "", ""
        if not out:
            return "", ""
        sha, _, subject = out.partition("\t")
        return sha, subject

    def last_touch_blob(self, blob_oid: str) -> tuple[str, str]:
        """Return ``(commit_sha, commit_subject)`` of a commit that introduced the
        blob ``blob_oid``. Searches all refs (``--find-object``) since during a
        conflicted rebase the side blobs live on different branches than HEAD.

        Used for per-side conflict provenance: each ConflictSide carries a
        ``blob_oid`` from the unmerged index, and this attributes it to a commit.
        Returns ``("", "")`` when the blob isn't found in any reachable history.
        Never raises — provenance is advisory.
        """
        if not blob_oid:
            return "", ""
        try:
            out = self._run_ok(
                ["log", "--all", "-1", "--format=%H%x09%s", "--find-object", blob_oid],
                what="git log (last_touch_blob)",
            ).strip()
        except Exception:  # noqa: BLE001 - provenance is advisory; never crash
            return "", ""
        if not out:
            return "", ""
        sha, _, subject = out.partition("\t")
        return sha, subject

    def has_unmerged_paths(self) -> bool:
        return any(True for _ in self._iter_unmerged_quick())

    def _iter_unmerged_quick(self):
        out = self._run(["ls-files", "-u", "-z"]).stdout
        for record in out.split("\0"):
            if record.strip():
                yield record

    # ------------------------------------------------------------------ rerere

    def rerere_enabled(self) -> bool:
        return self._run(["config", "--bool", "rerere.enabled"]).stdout.strip() == "true"


def _branch_slug(label: str) -> str:
    """Sanitise ``label`` to a branch-name-safe slug for backup branch names.

    Keeps alphanumerics, ``-`` and ``_``; replaces anything else (notably ``/``)
    with ``-`` so a feature branch like ``feature/foo`` becomes ``feature-foo``.
    Empty input becomes ``head``.
    """
    import re

    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", label or "").strip("-")
    return slug or "head"


def _synthesize_mode(stages: dict[int, str]) -> str:
    """Render a git-like unmerged mode from the stages present.

    This is informational only (e.g. ``UU``, ``AA``) for classification; it is
    not git's literal two-letter mode but a stable, sortable proxy derived from
    which stages exist.
    """
    has = lambda s: s in stages  # noqa: E731
    if has(2) and has(3):
        return "UU"
    if has(2) and not has(3):
        return "UA" if has(1) else "AA"
    if not has(2) and has(3):
        return "AU" if has(1) else "AA"
    return "??"


def default_backend(repo: str | Path = ".") -> GitBackend:
    return GitBackend(repo)

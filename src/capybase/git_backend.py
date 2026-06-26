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

    def worktree_is_clean(self) -> bool:
        """True if `git status --porcelain` is empty."""
        out = self._run_ok(
            ["status", "--porcelain"], what="status --porcelain"
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

    # ------------------------------------------------------------------ rebase control

    def start_rebase(self, target: str) -> GitResult:
        # --no-autostash: never silently move uncommitted work.
        return self._run(
            ["rebase", "--no-autostash", target], what=f"rebase {target}"
        )

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

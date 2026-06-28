"""Pre-flight checks before a rebase touches the user's repo.

The orchestrator's resolve/test/continue loop already protects *intra*-rebase
state (per-step refs, abort-on-escalation). This module guards everything
*before* ``git rebase`` runs: a half-finished operation, a detached HEAD, an
unknown target, a dirty tree. The principle is to fail fast with a clear,
actionable message rather than let git produce an opaque error or — worse —
leave the repo in a confusing intermediate state.

Two entry points:

- :func:`run_rebase_preflight` (``llm_ping=False``) — the git-only checks that
  gate :meth:`Orchestrator.rebase`. Fast; never touches the network.
- :func:`run_rebase_preflight` (``llm_ping=True``) — additionally pings the LLM
  endpoint (used by ``capybase check``). The rebase path itself stays git-only.

Each check is a :class:`PreflightCheck` with ``blocking`` semantics: a blocking
failure aborts the rebase (raises :class:`~capybase.git_backend.GitError` with
the detail); a non-blocking check is informational (e.g. "this will fast-forward")
and is journaled but never aborts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from capybase.config import Config
    from capybase.git_backend import GitBackend

# Minimum git version whose scripted-rebase plumbing (merge-base --is-ancestor,
# worktree semantics, rebase --autostash behaviour) we rely on. 2.30 is the
# modern baseline (released Dec 2020); below this we warn rather than trust
# subtle behaviour differences.
MIN_GIT_VERSION: tuple[int, int] = (2, 30)


@dataclass
class PreflightCheck:
    """The outcome of one pre-flight check.

    ``blocking=True`` means a failing check aborts the rebase. Non-blocking
    checks (like the fast-forward report) only inform. ``ok`` is the verdict;
    ``detail`` is a human-actionable message shown to the user and journaled.
    """

    name: str
    ok: bool
    blocking: bool
    detail: str

    def __str__(self) -> str:
        flag = "BLOCK" if (self.blocking and not self.ok) else ("ok  " if self.ok else "warn")
        return f"[{flag}] {self.name}: {self.detail}"


@dataclass
class PreflightReport:
    """The full set of checks for one invocation."""

    checks: list[PreflightCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True iff no blocking check failed."""
        return all(c.ok for c in self.checks if c.blocking)

    @property
    def first_blocking_failure(self) -> PreflightCheck | None:
        for c in self.checks:
            if c.blocking and not c.ok:
                return c
        return None

    def as_payload(self) -> list[dict]:
        return [
            {"name": c.name, "ok": c.ok, "blocking": c.blocking, "detail": c.detail}
            for c in self.checks
        ]


def _check_git_worktree(git: "GitBackend") -> PreflightCheck:
    ok = git.is_inside_worktree()
    return PreflightCheck(
        name="git-repo",
        ok=ok,
        blocking=True,
        detail="inside a git worktree" if ok else "not inside a git worktree",
    )


def _check_git_version(git: "GitBackend") -> PreflightCheck:
    ver = git.git_version()
    ok = ver >= MIN_GIT_VERSION
    detail = (
        f"git {ver[0]}.{ver[1]} >= {MIN_GIT_VERSION[0]}.{MIN_GIT_VERSION[1]}"
        if ok
        else f"git {ver[0]}.{ver[1]} is older than the supported "
        f"{MIN_GIT_VERSION[0]}.{MIN_GIT_VERSION[1]} baseline; please upgrade git"
    )
    return PreflightCheck(name="git-version", ok=ok, blocking=True, detail=detail)


def _check_no_op_in_progress(git: "GitBackend") -> PreflightCheck:
    op = git.operation_in_progress()
    if op is None:
        return PreflightCheck(
            name="no-op-in-progress", ok=True, blocking=True,
            detail="no git operation in progress",
        )
    return PreflightCheck(
        name="no-op-in-progress", ok=False, blocking=True,
        detail=(
            f"a {op} is already in progress; finish or abort it first "
            f"(e.g. `git {op} --abort` or complete the operation)"
        ),
    )


def _check_not_detached(git: "GitBackend") -> PreflightCheck:
    branch = git.current_branch()
    if branch is not None:
        return PreflightCheck(
            name="on-branch", ok=True, blocking=True,
            detail=f"on branch {branch!r}",
        )
    return PreflightCheck(
        name="on-branch", ok=False, blocking=True,
        detail=(
            "HEAD is detached; capybase rebases a branch (so it can back it up "
            "and move it). Check out the branch you want to rebase first, "
            "e.g. `git checkout my-branch`"
        ),
    )


def _check_clean_worktree(git: "GitBackend", *, autostash: bool) -> PreflightCheck:
    clean = git.worktree_is_clean()
    if clean:
        return PreflightCheck(
            name="clean-worktree", ok=True, blocking=True,
            detail="working tree is clean",
        )
    if autostash:
        return PreflightCheck(
            name="clean-worktree", ok=True, blocking=True,
            detail="working tree has changes; --autostash will stash and reapply them",
        )
    return PreflightCheck(
        name="clean-worktree", ok=False, blocking=True,
        detail="working tree is dirty; commit/stash your changes, or use --autostash",
    )


def _check_target_resolves(git: "GitBackend", target: str) -> PreflightCheck:
    oid = git.resolve_ref(target)
    if oid is not None:
        return PreflightCheck(
            name="target-resolves", ok=True, blocking=True,
            detail=f"target {target!r} resolves to {oid[:8]}",
        )
    return PreflightCheck(
        name="target-resolves", ok=False, blocking=True,
        detail=f"target {target!r} is not a known revision",
    )


def _check_not_self_rebase(git: "GitBackend", target: str) -> PreflightCheck:
    """Reject rebasing a branch onto itself (a no-op that confuses state)."""
    target_oid = git.resolve_ref(target)
    head_oid = git.resolve_ref("HEAD")
    if target_oid is None or head_oid is None:
        # Already reported by target-resolves / on-branch; be non-blocking here.
        return PreflightCheck(
            name="not-self-rebase", ok=True, blocking=True,
            detail="target/HEAD unresolvable (see earlier checks)",
        )
    if target_oid == head_oid:
        return PreflightCheck(
            name="not-self-rebase", ok=False, blocking=True,
            detail=f"target {target!r} is the current HEAD; rebasing onto itself is a no-op",
        )
    return PreflightCheck(
        name="not-self-rebase", ok=True, blocking=True,
        detail="target differs from current HEAD",
    )


def _check_ff_report(git: "GitBackend", target: str) -> PreflightCheck:
    """Informational (non-blocking): will this be a replay, fast-forward, or up-to-date?"""
    head_oid = git.resolve_ref("HEAD")
    target_oid = git.resolve_ref(target)
    if head_oid is None or target_oid is None:
        return PreflightCheck(
            name="rebase-shape", ok=True, blocking=False,
            detail="cannot determine (refs unresolvable)",
        )
    if target_oid == head_oid:
        return PreflightCheck(
            name="rebase-shape", ok=True, blocking=False,
            detail="already up to date with target",
        )
    if git.is_ancestor(target_oid, head_oid):
        return PreflightCheck(
            name="rebase-shape", ok=True, blocking=False,
            detail="HEAD already contains target; nothing to replay",
        )
    if git.is_ancestor(head_oid, target_oid):
        return PreflightCheck(
            name="rebase-shape", ok=True, blocking=False,
            detail="target is ahead of HEAD; this will fast-forward (no conflicts)",
        )
    return PreflightCheck(
        name="rebase-shape", ok=True, blocking=False,
        detail="diverged history; commits will be replayed onto target",
    )


def _check_llm_ping(config: "Config") -> PreflightCheck:
    """Ping the LLM endpoint. Only run when explicitly requested (capybase check)."""
    # Imported lazily so the rebase path (which never pings) has no network dep.
    from capybase.adapters.llm_openai import OpenAICompatibleClient
    from capybase.probes import probe_reachability

    try:
        client = OpenAICompatibleClient(config.model)
        result = probe_reachability(client, config.model)
    except Exception as exc:  # noqa: BLE001 - a ping reports, never raises
        return PreflightCheck(
            name="llm-reachable", ok=False, blocking=True,
            detail=f"LLM endpoint {config.model.base_url} unreachable: {exc}",
        )
    if result.ok:
        return PreflightCheck(
            name="llm-reachable", ok=True, blocking=True,
            detail=f"LLM endpoint {config.model.base_url} reachable ({result.detail})",
        )
    return PreflightCheck(
        name="llm-reachable", ok=False, blocking=True,
        detail=(
            f"LLM endpoint {config.model.base_url} not responding for model "
            f"{config.model.model!r}: {result.detail}"
        ),
    )


def run_rebase_preflight(
    git: "GitBackend",
    config: "Config",
    target: str,
    *,
    autostash: bool = False,
    llm_ping: bool = False,
) -> PreflightReport:
    """Run all rebase pre-flight checks and return a :class:`PreflightReport`.

    With ``llm_ping=False`` (the rebase path) only git-local checks run — no
    network, fast. With ``llm_ping=True`` (``capybase check``) the LLM endpoint
    is pinged as the final check.

    Does not raise: the caller inspects ``report.first_blocking_failure`` (or
    ``report.passed``) and decides. The rebase path turns a blocking failure
    into a :class:`~capybase.git_backend.GitError`.
    """
    report = PreflightReport()
    report.checks.append(_check_git_worktree(git))
    report.checks.append(_check_git_version(git))
    report.checks.append(_check_no_op_in_progress(git))
    report.checks.append(_check_not_detached(git))
    report.checks.append(_check_clean_worktree(git, autostash=autostash))
    report.checks.append(_check_target_resolves(git, target))
    report.checks.append(_check_not_self_rebase(git, target))
    report.checks.append(_check_ff_report(git, target))
    if llm_ping:
        report.checks.append(_check_llm_ping(config))
    return report

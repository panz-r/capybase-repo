"""``capybase clean`` — remove every trace of unpromoted rebase state.

The rebase machinery's no-trace contract: promoted work lives on the
source branch as ordinary commits (via the expected-OID CAS), and
everything else is scaffolding this module removes:

- ``refs/heads/capybase/*`` — candidate, dryrun, and backup branches
  (the whole capybase branch namespace)
- ``refs/rebase-agent/<session>/*`` — internal recovery/step refs
- linked worktrees OWNED by capybase — identified crash-proof by ANY of
  three signals (branch under ``refs/heads/capybase/*``, admin entry
  name, worktree path prefix), so a crash that deleted the branch or a
  reboot that wiped the temp dir still leaves a recognizable owner;
  stale registrations are pruned
- ``.rebase-agent/`` — audit bundles, sessions, journals, prompts,
  file snapshots (gitignored, but a trace nonetheless)
- the now-unreachable OBJECTS the deleted refs pointed at — reclaimed
  with ``git reflog expire --expire-unreachable=now --all`` followed
  by ``git gc --prune=now``, so the repository does not grow. Deleting
  a branch alone leaves its commits alive in the object store (kept
  reachable by HEAD's reflog); the expire+gc pair is what makes
  "clean" real.

Safety:
- Refuses to run while a rebase/merge/cherry-pick/revert is in progress.
- Only capybase's own namespaces are touched (``delete_ref``'s rail).
- The MAIN worktree is never removed; a user branch checked out there
  is left alone even if it somehow carries a capybase name.
- ``--dry-run`` lists everything and mutates nothing.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from capybase.git_backend import GitBackend

#: The on-disk state root (gitignored: audit bundles, sessions, journals).
STATE_DIR = ".rebase-agent"


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:  # pragma: no cover - racing deletes
            pass
    return total


#: Worktree ownership signals. Every worktree capybase creates carries
#: the prefix in three places: the admin entry name
#: (``.git/worktrees/<name>``), the on-disk path (an mkdtemp prefix),
#: and the checked-out branch. A crash can delete any ONE (partial
#: escalation cleanup removes the branch; a reboot wipes the temp dir;
#: the admin entry always survives) — matching on ANY signal is what
#: makes crashed-run cleanup safe and complete.
OWNED_ADMIN_PREFIXES = ("capybase-candidate-", "capybase-dryrun-")


@dataclass
class CleanReport:
    """What ``capybase clean`` found / removed."""
    dry_run: bool = False
    refused: str = ""            # non-empty ⇒ refused (e.g. op in progress)
    candidate_branches: list[str] = field(default_factory=list)
    dryrun_branches: list[str] = field(default_factory=list)
    backup_branches: list[str] = field(default_factory=list)
    recovery_refs: list[str] = field(default_factory=list)
    worktrees: list[str] = field(default_factory=list)
    state_dir_bytes: int = 0
    state_dir_removed: bool = False
    gc_ran: bool = False
    git_size_before: int = 0
    git_size_after: int = 0

    def summary(self) -> str:
        if self.refused:
            return f"CLEAN REFUSED: {self.refused}"
        mode = "DRY-RUN — nothing removed" if self.dry_run else "removed"
        lines = [
            f"capybase clean ({mode})",
            f"  candidate branches : {len(self.candidate_branches)}",
            f"  dryrun branches    : {len(self.dryrun_branches)}",
            f"  backup branches    : {len(self.backup_branches)}",
            f"  recovery refs      : {len(self.recovery_refs)}",
            f"  linked worktrees   : {len(self.worktrees)}",
            f"  {STATE_DIR}/          : "
            + (f"{self.state_dir_bytes} bytes" if self.state_dir_bytes
               else "absent"),
        ]
        if not self.dry_run:
            lines.append(
                f"  object store       : {self.git_size_before} → "
                f"{self.git_size_after} bytes"
                + (" (reclaimed)" if self.git_size_after < self.git_size_before
                   else " (unchanged)" if self.git_size_after == self.git_size_before
                   else " (grew — unreachable objects survived; "
                        "investigate reflogs)"))
        for b in self.candidate_branches:
            lines.append(f"    - {b}")
        for b in self.dryrun_branches:
            lines.append(f"    - {b}")
        for b in self.backup_branches:
            lines.append(f"    - {b}")
        return "\n".join(lines)


def _owned_worktrees(git: GitBackend, repo: Path) -> list[str]:
    """Linked worktrees (paths) owned by capybase — crash-proof.

    Ownership = ANY of: the checked-out branch is under
    ``refs/heads/capybase/*``, the admin entry name
    (``.git/worktrees/<name>``) carries a capybase prefix, or the
    worktree path carries one. The admin entries are enumerated
    directly (each contains a ``gitdir`` file naming the worktree path)
    because ``worktree list`` hides branchless leftovers a crash can
    leave behind. The MAIN worktree (the repo root itself) is never
    included.
    """
    main = Path(repo).resolve()
    owned: set[str] = set()

    def _is_owned(wt_path: str, branch: str, admin_name: str) -> bool:
        if branch.startswith("refs/heads/capybase/"):
            return True
        if admin_name.startswith(OWNED_ADMIN_PREFIXES):
            return True
        return any(p in wt_path for p in OWNED_ADMIN_PREFIXES)

    # Signal 1: live branches via worktree list.
    out = git._run_ok(["worktree", "list", "--porcelain"],
                      what="list worktrees")
    current: dict[str, str] = {}

    def _flush(cur: dict[str, str]) -> None:
        wt = cur.get("worktree", "")
        if not wt or Path(wt).resolve() == main:
            return
        if _is_owned(wt, cur.get("branch", ""), Path(wt).name):
            owned.add(str(Path(wt).resolve()))

    for line in out.splitlines():
        if not line.strip():
            _flush(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    _flush(current)

    # Signal 2: admin entries (survive branch deletion and reboots; the
    # gitdir file names the worktree path even when list shows nothing).
    admins_dir = repo / ".git" / "worktrees"
    if admins_dir.is_dir():
        for admin in admins_dir.iterdir():
            if not admin.is_dir():
                continue
            gitdir_file = admin / "gitdir"
            try:
                wt_path = gitdir_file.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if not wt_path:
                continue
            # gitdir holds the worktree's .git FILE path — strip it.
            wt = str(Path(wt_path).parent.resolve())
            if wt == str(main) or Path(wt) == main:
                continue
            if _is_owned(wt, "", admin.name):
                owned.add(wt)
    return sorted(owned)


def clean_capybase_state(
    repo: str | Path, *, dry_run: bool = False,
) -> CleanReport:
    """Remove all unpromoted capybase state; return what happened.

    See the module docstring for the inventory and the safety contract.
    Never raises on missing state (an already-clean repo is a no-op
    report); git failures propagate as :class:`GitError`.
    """
    repo = Path(repo).resolve()
    git = GitBackend(repo, check_git=True)
    report = CleanReport(dry_run=dry_run)

    op = git.operation_in_progress()
    if op:
        report.refused = (
            f"a git {op} is in progress — finish or abort it before cleaning "
            "(clean removes recovery state a resumable op may need)")
        return report

    report.git_size_before = _dir_size(repo / ".git")

    # Inventory: the WHOLE capybase branch namespace (candidate, dryrun,
    # backup — a crashed run can leave any of them).
    all_cb = git._run_ok(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads/capybase"],
        what="list capybase branches")
    for b in (x for x in all_cb.splitlines() if x.strip()):
        if b.startswith("capybase/dryrun/"):
            report.dryrun_branches.append(b)
        elif b.startswith("capybase/backup/"):
            report.backup_branches.append(b)
        else:  # candidate (and anything future under capybase/)
            report.candidate_branches.append(b)
    rec = git._run_ok(
        ["for-each-ref", "--format=%(refname)", "refs/rebase-agent"],
        what="list recovery refs")
    report.recovery_refs = [r for r in rec.splitlines() if r.strip()]
    report.worktrees = _owned_worktrees(git, repo)
    state_dir = repo / STATE_DIR
    report.state_dir_bytes = _dir_size(state_dir) if state_dir.exists() else 0

    if dry_run:
        return report

    # Worktrees first (git refuses -D on a branch checked out live).
    # Double-force survives a lock a crashed run may have left.
    for wt in report.worktrees:
        if Path(wt).exists():
            git._run_ok(["worktree", "remove", "--force", "--force", wt],
                        what=f"remove worktree {wt}")
    git.prune_worktrees()

    # Branches + recovery refs (delete_ref's namespace rail is the guard).
    for short in (report.candidate_branches + report.dryrun_branches
                  + report.backup_branches):
        git.delete_ref(short)
    for ref in report.recovery_refs:
        git.delete_ref(ref)

    # On-disk state root.
    if state_dir.exists():
        shutil.rmtree(state_dir, ignore_errors=True)
        report.state_dir_removed = not state_dir.exists()

    # Reclaim the now-unreachable objects so the repo does not grow.
    # Order matters: unreachable-only expiry first (user reflog entries
    # pointing at REACHABLE history stay), then prune-now gc.
    subprocess.run(
        ["git", "-C", str(repo), "reflog", "expire",
         "--expire-unreachable=now", "--all"],
        capture_output=True, text=True, timeout=600)
    subprocess.run(
        ["git", "-C", str(repo), "gc", "--prune=now", "--quiet"],
        capture_output=True, text=True, timeout=1800)
    report.gc_ran = True
    report.git_size_after = _dir_size(repo / ".git")
    return report


__all__ = ["clean_capybase_state", "CleanReport", "STATE_DIR"]

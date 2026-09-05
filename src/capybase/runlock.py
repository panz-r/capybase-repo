"""Run liveness lock — "is a capybase run alive on THIS repo right now?"

Git's in-progress sentinels say an operation is unfinished but nothing
about WHO started it or whether anything is still alive to finish it
(OOM kill, crash, reboot — or the repo dir was copied while the original
run continues elsewhere). Journals and recovery refs are attributable
only when the crash left them coherent.

The lock is written at the start of every mutating run and removed on
normal teardown; a crashed run's lock stays but reads as DEAD. Liveness
requires ALL of:

- the pid is alive,
- the process start time matches (pid reuse after a reboot can't fool it),
- the repo path recorded in the lock is THIS repo — a copied directory
  inherits the ORIGINAL's lock; the live process belongs to the original,
  not to the copy, so the copy is cleanable.

Pure stdlib, Linux-first: the authoritative start time is
``/proc/<pid>/stat`` field 22; without /proc the check degrades to a
bare pid-alive test (weaker, still useful).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

LOCK_NAME = "run.lock"


def lock_path(repo: str | Path) -> Path:
    return Path(repo) / ".rebase-agent" / LOCK_NAME


def _start_time(pid: int) -> str | None:
    """The process's start time (clock ticks) from /proc, or None."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # field 22, but comm (field 2) may contain spaces — parse after ')'
    tail = stat.rpartition(")")[2].split()
    try:
        return tail[19]  # 22nd field minus the first two already consumed
    except IndexError:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else


def write_lock(repo: str | Path) -> None:
    """Record this process as the live run for ``repo`` (best-effort).

    Overwrites a stale lock — a dead run's lock must not block the next.
    """
    p = lock_path(repo)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "pid": os.getpid(),
            "start_time": _start_time(os.getpid()),
            "repo": str(Path(repo).resolve()),
        }), encoding="utf-8")
    except OSError:
        pass  # locking is advisory; a run without a lock is still correct


def clear_lock(repo: str | Path) -> None:
    """Remove the lock on normal teardown (best-effort)."""
    try:
        lock_path(repo).unlink(missing_ok=True)
    except OSError:
        pass


def live_lock(repo: str | Path) -> dict | None:
    """The lock record if a run is LIVE for THIS repo, else None.

    A missing lock, a dead pid, a start-time mismatch (post-reboot pid
    reuse), or a lock whose recorded repo is a DIFFERENT path (the
    copied-directory case) all read as not-live.
    """
    p = lock_path(repo)
    try:
        record = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    pid = record.get("pid")
    if not isinstance(pid, int) or not _pid_alive(pid):
        return None
    recorded_start = record.get("start_time")
    if recorded_start is not None:
        actual = _start_time(pid)
        if actual is None or str(actual) != str(recorded_start):
            return None  # pid reused by another process
    if record.get("repo") != str(Path(repo).resolve()):
        return None  # the lock belongs to a different directory (a copy)
    return record


class run_lock_guard:
    """Context manager: hold the run lock for ``repo`` across a run.

    Crash-safe by design — the lock surviving is fine; ``live_lock``
    only reports runs whose process is actually alive.
    """

    def __init__(self, repo: str | Path) -> None:
        self.repo = repo

    def __enter__(self) -> "run_lock_guard":
        write_lock(self.repo)
        return self

    def __exit__(self, *exc) -> None:
        clear_lock(self.repo)


__all__ = ["write_lock", "clear_lock", "live_lock", "run_lock_guard",
           "lock_path"]

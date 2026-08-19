"""Process hygiene — sweep of stale build processes from prior runs.

Timed-out or killed eval/orchestrator runs can orphan their build trees:
``subprocess.run(shell=True, timeout=...)`` (and any kill that only
targets the direct child) leaves make/libtool/ccache descendants alive,
reparented to the session reaper, compiling inside /var/tmp/capy-rw-*
worktrees the harness has already deleted. Observed live (2026-08-19):
~274 such processes across seven leaked generations pinning the box at
load ~92 for hours.

The canonical fix is ``verification._run_shell_tree`` (own session +
process-group SIGKILL on timeout) at every spawn site; this sweep is the
DEFENSE-IN-DEPTH net, shared by every entry point (the live eval script
at startup/exit, the CLI at startup).

Matching is on BOTH cmdline markers and the process working directory:
the leaked population is mostly bare ``make`` / ``ccache g++`` / libtool
command lines that carry no marker string, but every one of them runs
with its cwd inside a /var/tmp/capy-rw-* eval worktree (which outlives
the worktree itself — the dir shows as deleted). Uses /proc scanning
(not pkill, which can hang on large process tables) with a hard 5s
budget. Best-effort — never raises, never blocks the caller.
"""

from __future__ import annotations

import os
import signal
import time


def kill_stale_build_processes() -> int:
    """SIGKILL stale compiler/ccache processes from previous runs.

    Returns the number of processes signalled. Never raises.
    """
    deadline = time.monotonic() + 5.0
    killed = 0
    try:
        pid_dirs = os.listdir("/proc")
    except OSError:
        return 0
    for pid_dir in pid_dirs:
        if not pid_dir.isdigit():
            continue
        if time.monotonic() > deadline:
            break
        try:
            with open(f"/proc/{pid_dir}/cmdline", "rb") as f:
                cmdline = f.read().decode("utf-8", errors="replace")
            cwd = os.readlink(f"/proc/{pid_dir}/cwd")
        except (OSError, ValueError):
            continue
        if (
            "capybase-ccache-shim" in cmdline
            or "capy-rw-" in cmdline
            or "ccache-tmp/cpp_stdout" in cmdline
            or str(cwd).startswith("/var/tmp/capy-rw-")
        ):
            try:
                os.kill(int(pid_dir), signal.SIGKILL)
                killed += 1
            except (OSError, ValueError):
                continue
    return killed

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
    me = os.getpid()
    my_parent = os.getppid()
    for pid_dir in pid_dirs:
        if not pid_dir.isdigit():
            continue
        if time.monotonic() > deadline:
            break
        pid = int(pid_dir)
        # s27-85: NEVER signal ourselves or our parent — `capybase status`
        # run from inside an eval worktree killed ITSELF at startup (the
        # cwd rule matched the caller's own shell).
        if pid == me or pid == my_parent:
            continue
        try:
            with open(f"/proc/{pid_dir}/cmdline", "rb") as f:
                argv = f.read().split(b"\0")
            cmdline = b" ".join(argv).decode("utf-8", errors="replace")
            cwd = os.readlink(f"/proc/{pid_dir}/cwd")
        except (OSError, ValueError):
            continue
        # s27-85: marker matching on whole ARGV TOKENS, not a substring of
        # the joined cmdline — `bash -c '<text mentioning capy-rw->'` (a
        # grep/log command) matched the substring form and was SIGKILLed.
        tokens = [t.decode("utf-8", errors="replace") for t in argv if t]
        marker_hit = any(
            "capybase-ccache-shim" in t or "capy-rw-" in t
            or "ccache-tmp/cpp_stdout" in t
            for t in tokens)
        cwd_hit = str(cwd).startswith(("/tmp/capy-rw-", "/var/tmp/capy-rw-"))
        if not (marker_hit or cwd_hit):
            continue
        # s27-85: the cwd rule requires the worktree dir to STILL EXIST —
        # a live eval's worktree is present; a user shell cd'd into a
        # since-deleted (or live!) tree must not die. Combined with a
        # recency guard: only processes started within the window a stale
        # generation can be (12h) are candidates.
        if cwd_hit and not os.path.isdir(str(cwd)):
            continue
        if cwd_hit:
            try:
                with open(f"/proc/{pid_dir}/stat") as f:
                    stat = f.read()
                start_ticks = int(stat[stat.rindex(")") + 2:].split()[19])
                uptime_s = float(open("/proc/uptime").read().split()[0])
                age_s = uptime_s - start_ticks / os.sysconf("SC_CLK_TCK")
                if age_s > 12 * 3600:
                    continue  # older than any stale generation window
            except (OSError, ValueError, IndexError):
                continue
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except (OSError, ValueError):
            continue
    return killed

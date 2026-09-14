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


_BUILD_TOOLS = frozenset({
    "make", "gmake", "ccache", "gcc", "g++", "cc1", "cc1plus", "ld",
    "libtool", "cargo", "rustc", "ninja",
})


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
            cwd = os.readlink(f"/proc/{pid_dir}/cwd")
        except (OSError, ValueError):
            continue
        # Strip the kernel's " (deleted)" suffix so the cwd prefix match
        # and the orphan test below see the original path (orphaned build
        # tools have a DELETED cwd — the worktree the harness removed).
        cwd = str(cwd).replace(" (deleted)", "")
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
        # s27-89: the cwd rule requires the directory to be DELETED — that
        # is the pure orphan signature (the worktree the harness removed is
        # gone). A LIVE tree means a live eval owns these processes; its own
        # watchdog handles them, and a second CLI invocation's startup sweep
        # must not kill a running eval's compilers.
        if cwd_hit and os.path.isdir(str(cwd)):
            continue
        # s27-85: recency bound for marker-only matches — a days-old `tail
        # -f` on a build log has the marker in argv and must not be killed.
        # (A deleted-cwd orphan is killed at any age: it is burning CPU with
        # no worktree to return to.)
        if not cwd_hit:
            argv0 = tokens[0].rsplit("/", 1)[-1] if tokens else ""
            if argv0 not in _BUILD_TOOLS:
                continue
            try:
                with open(f"/proc/{pid_dir}/stat") as f:
                    stat = f.read()
                start_ticks = int(stat[stat.rindex(")") + 2:].split()[19])
                uptime_s = float(open("/proc/uptime").read().split()[0])
                age_s = uptime_s - start_ticks / os.sysconf("SC_CLK_TCK")
                if age_s > 12 * 3600:
                    continue
            except (OSError, ValueError, IndexError):
                continue
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except (OSError, ValueError):
            continue
    return killed

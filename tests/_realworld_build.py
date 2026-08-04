"""Authentic build harness for real-world C merge-conflict cases.

The C analog of ``tests/_realworld_cargo.py``: each real-world C case is checked
out at its resolved merge commit ``M`` (or source tip) in a **disposable git
worktree** linked to the shared (blob-filtered) clone, then the **user-supplied
build command** runs against the whole tree at that commit. This is the only
honest compile signal for real-world C conflicts: standalone ``gcc -fsyntax-
only`` can't resolve ``#include`` of project-internal headers (``server.h``,
``sqliteInt.h``), exactly the limitation standalone ``rustc`` hits on
``crate::`` paths.

**Why a generic command, not a fixed one.** C builds are non-uniform — redis
uses ``make``, sqlite uses ``./configure && make``, curl uses ``./configure &&
make`` (autotools), cmake projects use ``cmake --build``. There is no single
command the way ``cargo check`` works for every Rust crate. So the build command
is **user-supplied** (per dataset); no auto-discovery (too much build-system
variety in practice). For the corpus test runs, the per-dataset defaults live in
``C_BUILD_COMMANDS`` below; in production, the user sets ``[tests]
pre_continue``/``final`` to the command for their repo.

**Isolation model** mirrors ``_realworld_cargo.py`` exactly: the shared clone is
read-only (a linked worktree is added per case and removed in ``finally``), so
the clone's HEAD is never touched. This makes the harness interrupt-safe and
xdist-safe. The worktree shares the clone's object store, so blobs fetched
during mining are reused.

**``shell=True``** is used (C build commands chain with ``&&``, e.g.
``./configure && make``). The production ``TestRunner`` uses ``shlex.split`` /
no-shell; this corpus harness is test-only and needs shell features.

**The verdict is honest, not asserted.** A real-world merge may not build
(missing build deps, platform-specific code, pre-existing repo errors), so
callers assert only the infrastructure invariant (``ran`` — the command engaged)
and record ``compiled``/errors informatively.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Reuse the proven worktree-cleanup helper from the cargo harness — it's fully
# generic (prunes orphaned worktrees in the shared clone).
from tests._realworld_cargo import cleanup_orphan_worktrees


# Per-dataset build commands for the C corpus. These are the sensible defaults
# for the test runs; the user can override per-repo. C builds are non-uniform
# (no auto-discovery — too much build-system variety). Add entries as new C
# repos enter the corpus. Empty/absent = the test skips that dataset's build
# verdict (the gcc syntax floor still runs).
C_BUILD_COMMANDS: dict[str, str] = {
    "redis-history": "make -j4",
    "jsonc-history": "cmake --build build",
    "sqlite-history": "./configure && make -j4",
    "nlohmann-json-history": "cmake --build build",
    "clickhouse-history": "cmake --build build",
}

# Build timeout. C builds at a specific commit can be slow (a full sqlite
# configure+make, a redis rebuild). Cap it so a hung build can't stall the suite
# indefinitely; aligns with the cargo harness's DEFAULT_TIMEOUT (300s) but
# allows headroom for configure steps.
DEFAULT_TIMEOUT = 600


@dataclass
class BuildVerdict:
    """The result of an authentic build at a commit.

    ``ran`` is the infrastructure invariant: did the command actually engage (vs
    being absent, the worktree failing to create, or a timeout). Callers assert
    ``ran``; ``compiled``/``errors`` are recorded informatively (a real-world
    merge may not build on this machine — missing deps, platform code — and that
    is an honest signal, not a test failure).
    """
    ran: bool = False
    compiled: bool = False
    timed_out: bool = False
    errors: list[str] = field(default_factory=list)
    tool: str = "build"
    command: str = ""
    worktree: str = ""  # set even on failure, for diagnostics

    @property
    def verdict(self) -> str:
        if self.timed_out:
            return "TIMEOUT"
        if not self.ran:
            return "DID NOT RUN"
        return "PASS" if self.compiled else f"FAIL ({len(self.errors)} error(s))"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a git command in ``repo`` (mirrors _realworld_cargo._git)."""
    return subprocess.run(
        ["git", "-C", str(Path(repo).resolve()), *args],
        capture_output=True, text=True,
    )


def run_command_at_worktree(
    clone: Path,
    sha: str,
    command: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    env: dict[str, str] | None = None,
    cwd_suffix: str | None = None,
) -> BuildVerdict:
    """Run ``command`` in a disposable worktree of ``clone`` checked out at ``sha``.

    Mirrors ``cargo_check_at_worktree``'s isolation model: the clone stays
    read-only, a linked worktree is added per call and removed in ``finally``.
    The command runs via ``shell=True`` (C builds chain with ``&&``).

    Args:
        clone: the shared (blob-filtered) clone directory (read-only).
        sha: the commit to check out in the worktree (merge commit M or source
            tip).
        command: the build command (e.g. ``"make -j4"``,
            ``"./configure && make"``). Run via ``shell=True``.
        timeout: seconds before the build is killed (returns ``timed_out``).
        env: optional environment overrides (merged over ``os.environ``).
        cwd_suffix: optional subdirectory of the worktree to run in (e.g.
            ``"build"`` for out-of-tree cmake builds).

    Returns a :class:`BuildVerdict`. Assert ``verdict.ran`` (infrastructure
    invariant); record ``verdict.compiled`` honestly.
    """
    td = Path(tempfile.mkdtemp(prefix="capybase-build-worktree-"))
    wt = td / "wt"
    verdict = BuildVerdict(ran=False, command=command, worktree=str(wt))
    run_env = {**os.environ, **(env or {})}
    try:
        # Materialize an isolated checkout at sha. The clone stays on whatever
        # HEAD it was on — worktree add never moves the main worktree.
        add = _git(clone, "worktree", "add", "--quiet", "--detach", str(wt), sha)
        if add.returncode != 0:
            verdict.errors = [add.stderr.strip() or "git worktree add failed"]
            return verdict
        verdict.worktree = str(wt)
        cwd = str(wt / cwd_suffix) if cwd_suffix else str(wt)
        if cwd_suffix:
            # Create the build subdir if the caller wants an out-of-tree build
            # in a fresh worktree (the checkout won't have it).
            (wt / cwd_suffix).mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run(
                command, shell=True,
                capture_output=True, text=True,
                timeout=timeout, cwd=cwd, env=run_env,
            )
        except subprocess.TimeoutExpired:
            verdict.ran = True
            verdict.timed_out = True
            return verdict
        except FileNotFoundError as exc:
            # The shell or command binary is absent — the command didn't engage.
            verdict.errors = [f"command not found: {exc}"]
            return verdict

        verdict.ran = True
        verdict.compiled = proc.returncode == 0
        # First 5 non-empty stderr lines as the error summary (C compilers and
        # make emit the actionable diagnostics on stderr). Keep it bounded so a
        # verbose build doesn't bloat the test output.
        if not verdict.compiled:
            err_lines = [
                ln for ln in (proc.stderr or "").splitlines() if ln.strip()
            ]
            verdict.errors = err_lines[:5]
        return verdict
    finally:
        # Remove the worktree (force: it may have untracked build artifacts)
        # and the temp dir holding it.
        _git(clone, "worktree", "remove", "--force", str(wt))
        shutil.rmtree(td, ignore_errors=True)

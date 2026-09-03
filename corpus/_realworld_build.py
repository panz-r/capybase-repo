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
from corpus._realworld_cargo import cleanup_orphan_worktrees


# Per-dataset build commands for the C corpus. These are the sensible defaults
# for the test runs; the user can override per-repo. C builds are non-uniform
# (no auto-discovery — too much build-system variety). Add entries as new C
# repos enter the corpus. Empty/absent = the test skips that dataset's build
# verdict (the gcc syntax floor still runs).
C_BUILD_COMMANDS: dict[str, str] = {
    # Sprint-26 C18 (era recovery): the CC wrapper fixes BOTH the -lm
    # link order (Ubuntu --as-needed drops libm placed before objects —
    # redis-0049's oracle fails on 'undefined reference to log') and
    # gcc 15's -fno-common default (hashDictType multiple definitions in
    # redis.h). VERIFIED offline: redis-server/cli/benchmark all build.
    "redis-history": "make -j4 CC='cc -std=gnu99 -fcommon -Wl,--no-as-needed' CFLAGS='-std=gnu99 -fcommon' MALLOC=libc FORCE_LIBC_MALLOC=yes",
    # jsonc: the era repos build with -Werror; modern gcc emits warnings the era
    # compilers did not (unused-value etc), promoting clean-era code to build
    # failures — a false era-positive. Verified: -Wno-error builds the era clean.
    "jsonc-history": 'cmake --build build -- CFLAGS="-Wno-error"',
    "sqlite-history": "./configure && make -j4",
    "nlohmann-json-history": "cmake --build build",
    "clickhouse-history": "cmake --build build",
    "protobuf-history": "cmake --build build",
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


#: Per-dataset TEST commands (sprint-25 decision 3: the corpus repo config
#: carries test commands where they exist — the same mechanism as the
#: production ``[tests]`` config). Used by the harness's post-hoc
#: output-tests probe: when the resolver's merge diverges from the oracle
#: but the project's OWN tests pass on the output tree, the case classifies
#: WORKING (sprint-25 decision 1: capybase only fixes conflicts — it never
#: writes or modifies tests — so tests passing is un-gameable evidence of
#: real-world merge value; oracle-convergence alone may be too strict).
#: Empty/absent = no test command (the probe stays None; the
#: preservation-based WORKING rule still applies). Bounded by the probe's
#: timeout; suites that cannot run in the sandbox fail honestly to None.
C_TEST_COMMANDS: dict[str, str] = {
    "jsonc-history": "ctest --test-dir build --output-on-failure",
    # D9 (s27): redis's TCL suite runs on system tclsh against the built
    # binaries — VERIFIED on a materialized tree (era flag build, then
    # './runtest --single unit/type/string' all-pass). The full suite is
    # the command; the eval's timeout records None (not False) on overrun.
    "redis-history": "./runtest",
    # D9 (s27): sqlite's quicktest via testfixture — VERIFIED end-to-end
    # on an oracle-resolved tree (testfixture builds against the extracted
    # tcl dev tree, quicktest rc=0). The initial "era-broken" diagnosis was
    # a testing artifact: quicktest on the raw CONFLICTED worktree fails on
    # markers, and tclConfig.sh bakes foreign paths (both fixed).
    "sqlite-history": "make quicktest CFLAGS='-std=gnu99 -O1' TCL_CONFIG_SH=" + __import__("os").environ.get("CAPYBASE_TCL_CONFIG_SH", ""),
    # Rust crates: the case worktree IS the crate; cargo test with the
    # probe's timeout. Big suites (tokio) partial-run to a timeout → the
    # probe records None, not False (a timeout is not a test failure).
    "axum-history": "cargo test --quiet",
    "sea-orm-history": "cargo test --quiet",
    "tokio-history": "cargo test --quiet --lib",
}

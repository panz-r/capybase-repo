"""Authentic cargo-check harness for real-world Rust merge-conflict cases.

Each real-world Rust case (e.g. a serde conflict mined from git history) is
checked out at its resolved merge commit ``M`` in a **disposable git worktree**
linked to the shared (blob-filtered) clone, then ``cargo check`` runs against
the whole crate at M. This is the only honest compile signal for real-world
Rust conflicts: standalone ``rustc`` (and ``verify_file`` on a bare tmp_path)
can't resolve ``crate::``/``super::`` paths (E0432), and ``verify_file``'s
baseline/new-error model degenerates at M (the file is already the marker-free
human merge). Checking the crate out at M and compiling it with real deps,
edition, and sibling files is the authentic option.

**Isolation model.** The shared clone is treated as **read-only** by this
harness: a worktree is ``git worktree add``ed per case and removed afterward, so
the clone's HEAD/branch is never touched. This makes the harness:

- **Interrupt-safe.** A Ctrl-C may orphan a worktree, but it can't drift the
  clone — the clone was never checked out. Orphaned worktrees are pruned by
  :func:`cleanup_orphan_worktrees` at the next session start (idempotent).
- **xdist-safe.** Each worker creates its own disposable worktree; nothing
  mutates the shared clone, so concurrent workers can't race it. (Compare the
  older in-place checkout model, which mutated one clone under a
  ``threading.Lock`` and was xdist-unsafe.)

**Error detection** reuses the production cargo JSON path
(:func:`capybase.adapters.lsp._parse_cargo_messages`) plus the stderr fallback
(:func:`._first_error_line`) for fatal pre-JSON failures — the same logic the
live verifier's ``_check_cargo`` uses, not a weaker grep. A worktree shares the
clone's object store, so blobs already fetched during mining are reused (missing
ones come via the clone's promisor); no re-clone of history.

**The verdict is honest, not asserted.** A real-world merge may not compile on
our toolchain (newer edition, platform-specific deps, pre-existing repo errors),
so callers assert only the infrastructure invariant (``ran`` — cargo engaged)
and record ``compiled``/errors informatively. See
``test_realworld_conflicts.py``.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


# The cargo invocation timeout. The live verifier uses 120s for cargo check
# (RustAnalyzerRunner) and 180s for clippy/shadow tests; this harness builds a
# real crate at a specific commit, so we allow more headroom but cap it (a hung
# build must not stall the suite indefinitely). Aligns with the broader
# TestRunner default (300s).
DEFAULT_TIMEOUT = 300


@dataclass
class CargoVerdict:
    """The result of an authentic cargo check at a merge commit.

    ``ran`` is the infrastructure invariant: did cargo actually engage (vs being
    absent, timing out, or the worktree failing to materialize)? Callers assert
    this — a False here is an infrastructure regression, never a real finding
    about the merge.

    ``compiled`` / ``errors`` are the honest signal, recorded not asserted: a
    real-world merge that compiles on our toolchain is a strong pass signal;
    one that doesn't is an informative flag (the original repo may carry
    pre-existing errors, a newer edition, or platform-specific code).
    """

    ran: bool
    compiled: bool = False
    timed_out: bool = False
    errors: list[str] = field(default_factory=list)
    tool: str = "cargo"
    # The worktree path (set even on failure, for diagnostics). Empty if the
    # worktree never materialized.
    worktree: str = ""

    @property
    def verdict(self) -> str:
        """One-word human verdict for the test output line."""
        if self.timed_out:
            return "TIMEOUT"
        return "PASS" if self.compiled else f"FAIL ({len(self.errors)} error(s))"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a git command in ``repo``, returning the CompletedProcess.

    ``check=False``: callers interpret per-command exit codes (a worktree
    already absent during cleanup is fine, for instance). ``repo`` is resolved
    to an absolute path so the command works regardless of the caller's cwd
    (git records worktree paths in the clone's metadata; a relative path there
    would break later lookups from a different cwd).
    """
    return subprocess.run(
        ["git", "-C", str(Path(repo).resolve()), *args],
        capture_output=True, text=True,
    )


def cleanup_orphan_worktrees(clone: Path) -> int:
    """Remove worktrees left orphaned by an interrupted previous run.

    A Ctrl-C between ``worktree add`` and the ``finally`` removal leaves a
    worktree registered in the clone's metadata. Left unkempt these accumulate
    (each holds a checkout + build artifacts on disk). This prunes every
    registered worktree except the main clone itself, and runs the lock/prune
    sweep so stale administrative files clear too.

    Idempotent and safe to call any time: an already-clean clone is a no-op.
    Returns the count of worktrees removed (for logging).
    """
    # Resolve to absolute: ``git worktree list --porcelain`` reports the main
    # worktree's path as absolute, so a relative ``clone`` would fail the
    # equality check below and the main clone would be misclassified as an
    # orphan and DELETED. Comparing both as absolute (``samefile``) is the
    # safety rail that must never break.
    clone_abs = Path(clone).resolve()
    listing = _git(clone, "worktree", "list", "--porcelain")
    if listing.returncode != 0:
        return 0
    removed = 0
    for line in listing.stdout.splitlines():
        # ``worktree <path>`` lines name each worktree; the main clone's own
        # entry also appears (as the absolute clone path). Skip it — we only
        # remove LINKED (per-case) worktrees, never the main clone.
        if not line.startswith("worktree "):
            continue
        wt = Path(line.split(" ", 1)[1].strip())
        # Never touch the main clone. ``samefile`` tolerates relative/absolute
        # and symlink differences; a plain ``==`` on unequal-normalization paths
        # is exactly the bug that could delete the clone.
        if not wt.exists() or wt.samefile(clone_abs):
            continue
        res = _git(clone, "worktree", "remove", "--force", str(wt))
        if res.returncode == 0:
            removed += 1
        else:
            # Administrative entry without files: prune the metadata instead.
            shutil.rmtree(wt, ignore_errors=True)
    # Clear stale administrative/lock state regardless.
    _git(clone, "worktree", "prune")
    return removed


def cargo_check_at_worktree(
    clone: Path, merge_sha: str, *, timeout: int = DEFAULT_TIMEOUT
) -> CargoVerdict:
    """Check out ``merge_sha`` in a disposable worktree and run ``cargo check``.

    Creates a linked worktree of ``clone`` at ``merge_sha`` in a temp dir, runs
    ``cargo check --message-format=json`` there (with a per-worktree
    ``CARGO_TARGET_DIR`` so each build is independent and deterministic — no
    stale incremental cache can mask a failure), then removes the worktree in a
    ``finally``. The shared clone's HEAD/branch is never touched.

    Reuses the production cargo JSON parser
    (:func:`capybase.adapters.lsp._parse_cargo_messages`) and the stderr-fallback
    (:func:`._first_error_line`) for fatal pre-JSON failures — identical error
    detection to the live verifier's ``_check_cargo``.

    Returns a :class:`CargoVerdict`. ``ran=False`` means cargo couldn't engage
    (absent / timed out / worktree materialization failed) — the infrastructure
    invariant. ``compiled``/``errors`` are the honest, recorded-not-asserted
    signal.
    """
    cargo = shutil.which("cargo")
    if cargo is None:
        return CargoVerdict(ran=False)

    # Late import: the adapter is a runtime dependency but this test helper is
    # only imported by the real-world module, which already gates on cargo.
    from capybase.adapters.lsp import Diagnostic, _first_error_line, _parse_cargo_messages

    td = Path(tempfile.mkdtemp(prefix="capybase-worktree-"))
    wt = td / "wt"
    verdict = CargoVerdict(ran=False, worktree=str(wt))
    try:
        # Materialize an isolated checkout at M. The clone stays on whatever
        # HEAD it was on — ``worktree add`` never moves the main worktree.
        add = _git(clone, "worktree", "add", "--quiet", "--detach", str(wt), merge_sha)
        if add.returncode != 0:
            # Unknown sha, or a stale worktree lock. Not a real finding; surface
            # it so the caller's infra-invariant assertion fires informatively.
            verdict.errors = [add.stderr.strip() or "git worktree add failed"]
            return verdict
        verdict.worktree = str(wt)
        try:
            proc = subprocess.run(
                [cargo, "check", "--quiet", "--message-format=json"],
                capture_output=True, text=True,
                timeout=timeout, cwd=str(wt),
                # Per-worktree target dir: each build is independent and is
                # disposed with the worktree. The global cargo registry
                # (~/.cargo) is still shared, so deps don't re-fetch.
                env={**__import__("os").environ, "CARGO_TARGET_DIR": str(td / "target")},
            )
        except subprocess.TimeoutExpired:
            verdict.ran = True
            verdict.timed_out = True
            return verdict

        # Production-path error detection: parse the JSON stream, then fall back
        # to stderr for fatal failures that short-circuit before any JSON
        # (mirrors _check_cargo at lsp.py:190-202 exactly).
        diags = _parse_cargo_messages(proc.stdout or "", "")
        if proc.returncode != 0 and not diags:
            err_line = _first_error_line(proc.stderr or "")
            if err_line:
                diags.append(Diagnostic(severity="error", message=err_line))
        errors = [d.message for d in diags if d.severity == "error"]
        verdict.ran = True
        verdict.compiled = len(errors) == 0
        verdict.errors = errors
        return verdict
    finally:
        # Remove the worktree and the temp dir holding it + its target dir.
        # Best-effort: an interrupted removal is recovered by the orphan
        # cleanup at the next session start, and the clone is unaffected either
        # way (it was never checked out).
        if wt.exists():
            _git(clone, "worktree", "remove", "--force", str(wt))
        shutil.rmtree(td, ignore_errors=True)

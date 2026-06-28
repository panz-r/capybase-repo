"""Real-world merge-conflict cases (Method D) — the first external-data test set.

Drives real GitHub merge conflicts (downloaded + processed by
``scripts/fetch_mergeconflict_datasets.py`` into ``extracted-testdata/realworld/``)
through the verifier, using the human-authored merge (M) as the known-correct
resolution oracle. This anchors the synthetic catalog in realism: real conflicts
are messier (multi-hunk, pre-existing crate errors, unconventional formatting)
than the curated rows, and this is where overfitting to synthetic patterns shows
up.

**This is the first dependency on external data.** The 325MB+ datasets are
gitignored (too large for the repo; licenses require attribution not
redistribution), so a fresh clone has NO data and this module SKIPS entirely. To
populate it:

    .venv/bin/python scripts/fetch_mergeconflict_datasets.py --language python --limit 50

The script's DATASETS registry selects the language and caps the case count
(a dataset's thousands of conflicts would explode test parametrization). The
zenodo-hdiff dataset has 4,298 Python conflicts (and JS/Java/Clojure/Lua/Shell)
but no Rust; the Python cases run here against the always-on py_compile floor
(no toolchain gate). The serde-history dataset mines real Rust merge conflicts
from serde's git history; those Rust cases run an AUTHENTIC cargo check — the
cloned repo is checked out at the merge commit M and ``cargo check`` runs the
whole crate with real deps/edition/sibling files.

Why not ``verify_file`` for Rust? Standalone ``rustc`` (and ``verify_file``
against a bare tmp_path) can't resolve ``crate::``/``super::`` paths, so they
FALSE-POSITIVE on virtually every serde file (E0432). And ``verify_file``'s
baseline/new-error model degenerates at M: the file is already the marker-free
human merge, so baseline == after and it reports ``syntax_passed=True``
regardless. The only honest check is the whole crate at the committed resolved
state, which is what checking out M gives us.

Oracle policy: real-world M is the human merge, but it may NOT compile under our
floor if the original repo had pre-existing errors or used a different toolchain.
So we do NOT force ``passed``; we assert the case was *checked* (markers parse,
the merge splices marker-free, the floor engaged) and record the verifier's
verdict honestly — the value is "does capybase accept the human merge", not
"every real merge passes". A high accept rate validates the floor; a low rate
flags real-world rough edges.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from capybase.adapters.parsers import (
    contains_markers,
    parse_marker_blocks,
)
from capybase.verification import ValidationConfig, VerificationEngine

from tests.realworld_loader import (
    RealWorldCase,
    git_history_repo_path,
    load_realworld_cases,
)

# Module-level skip: no external data → the whole set is inert. The reason names
# the script so a contributor knows how to populate it.
pytestmark = pytest.mark.skipif(
    not load_realworld_cases(),
    reason=(
        "no real-world test data downloaded; run "
        "scripts/fetch_mergeconflict_datasets.py"
    ),
)

CARGO = shutil.which("cargo")

# Load once at import (cheap — a directory scan). Parametrization is stable.
CASES = load_realworld_cases()

# Serialize clone worktree mutations across parametrized Rust cases. Each Rust
# case checks the shared serde clone out at its own merge commit; two cases
# checking out different commits concurrently would race on the one worktree.
# This lock makes the cases safe under threads / ``pytest -p no:xdist``. They
# are NOT safe under ``pytest-xdist`` (separate processes don't share the lock)
# — run this module without xdist, or set ``-p no:cacheprovider`` isolation.
# That's acceptable: these cases are few (the miner caps conflicts) and each
# checkout+check is I/O-bound, not CPU-bound, so parallelism wouldn't help.
_CLONE_LOCK = threading.Lock()


def _cargo_check_at_merge(
    clone: Path, merge_sha: str, *, timeout: int = 600
) -> tuple[bool, list[str]]:
    """Check out ``merge_sha`` in ``clone`` and run ``cargo check``.

    This is the authentic compile signal for a real-world Rust conflict: at the
    resolved merge commit M, the file IS the human merge, so checking the crate
    out at M and running ``cargo check`` validates the committed resolution with
    real deps, the real edition, and real sibling files — exactly the context
    standalone ``rustc`` (and ``verify_file`` on a bare tmp_path) lack.

    Restores the clone to its prior branch/HEAD in a ``finally`` so the shared
    clone is left clean for the next case. Returns ``(ran, error_messages)``:
    ``ran`` is False iff ``cargo`` couldn't engage (absent/timeout/crash) — the
    infrastructure-invariant the caller asserts. ``error_messages`` (the cargo
    error lines) are returned for honest reporting, NOT asserted: a real-world
    merge that doesn't compile on our toolchain is an informative flag, not a
    test failure (the original repo may carry pre-existing errors, a newer
    edition, or platform-specific code).
    """
    cargo = shutil.which("cargo")
    if cargo is None:
        return False, []

    def _git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(clone), *args],
            capture_output=True, text=True,
        )

    # Remember where the clone was so we can restore it (it may be on a branch
    # or detached-HEAD from a prior case).
    head_ref = _git("symbolic-ref", "-q", "HEAD")
    prior = head_ref.stdout.strip() if head_ref.returncode == 0 else ""
    if not prior:
        # Detached HEAD: record the commit so we can return to it.
        prior = _git("rev-parse", "HEAD").stdout.strip()

    try:
        # Check out the merge commit M (detached). The worktree now reflects the
        # committed resolved state — the human merge is already on disk.
        co = _git("checkout", "--quiet", merge_sha)
        if co.returncode != 0:
            return False, [co.stderr.strip()]
        try:
            proc = subprocess.run(
                [cargo, "check", "--quiet", "--message-format=short"],
                capture_output=True, text=True,
                timeout=timeout, cwd=str(clone),
            )
        except subprocess.TimeoutExpired:
            return False, [f"cargo check timed out after {timeout}s"]
        ran = True
        # ``--message-format=short`` emits ``error: ...`` lines on stderr.
        errors = [
            line for line in (proc.stderr or "").splitlines()
            if line.lstrip().startswith("error")
        ]
        return ran, errors
    finally:
        if prior.startswith("refs/heads/"):
            _git("checkout", "--quiet", prior[len("refs/heads/"):])
        elif prior:
            _git("checkout", "--quiet", prior)


# ---------------------------------------------------------------------------
# Structural integrity: the generated cases are well-formed (no toolchain).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_realworld_marker_parses(case: RealWorldCase):
    """The git-generated markers parse to at least one conflict block."""
    blocks = parse_marker_blocks(case.marker_original)
    assert len(blocks) >= 1, f"{case.id}: no conflict blocks in marker_original"


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_realworld_human_merge_is_marker_free(case: RealWorldCase):
    """The human resolution (M) contains no leftover conflict markers."""
    assert not contains_markers(case.expected_resolved), (
        f"{case.id}: the human merge M still contains conflict markers"
    )


# ---------------------------------------------------------------------------
# Verifier verdict: does capybase accept the human merge?
# ---------------------------------------------------------------------------
#
# Two paths, one per language family:
#
# - **Python**: ``verify_file`` with the always-on ``py_compile`` floor. No
#   toolchain gate (py_compile is always available). M is the whole resolved
#   file, verified directly (no splice).
# - **Rust**: ``cargo check`` checked out at the merge commit M in the cloned
#   repo. This is the authentic signal: standalone ``rustc`` (and
#   ``verify_file`` against a bare tmp_path) can't resolve ``crate::``/``super::``
#   paths, so they FALSE-POSITIVE on virtually every serde file (E0432). Worse,
#   ``verify_file``'s baseline/new-error model degenerates at M — the file is
#   already the marker-free human merge, so baseline == after and it reports
#   ``syntax_passed=True`` regardless. The only honest check is the whole crate
#   at the committed resolved state, which is what checking out M gives us.
#
# Neither path forces ``passed``. The oracle policy: a real-world merge that
# compiles is a pass signal; one that doesn't is an informative flag (the
# original repo may carry pre-existing errors, a newer edition, or
# platform-specific code). We assert only the infrastructure invariant — that
# the floor ENGAGED — and record the verdict honestly.


def _rust_clone_or_skip(case: RealWorldCase):
    """Skip a Rust case unless the cloned repo + cargo + merge_sha are present.

    Rust cases need the real crate checked out at M, so they gate on (1) cargo,
    (2) the git-history clone existing, and (3) the case carrying a ``merge_sha``
    (older cases without it can't be checked out). Same inert-when-absent
    contract as the module-level "no data" skip: a fresh clone with no serde
    checkout skips cleanly.
    """
    if CARGO is None:
        pytest.skip("cargo not installed")
    clone = git_history_repo_path(case.dataset)
    if not (clone / ".git").exists():
        pytest.skip(f"{case.dataset} clone not present ({clone}); run the fetch script")
    if not case.merge_sha:
        pytest.skip(
            f"{case.id} has no merge_sha (re-run the fetch script to regenerate)"
        )


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_realworld_python_merge_verifier_verdict(case: RealWorldCase, tmp_path):
    """Python: does ``py_compile`` accept the human merge? (record honestly)."""
    if case.language != "python":
        pytest.skip("Python-only verifier verdict (Rust is cargo-gated separately)")
    eng = VerificationEngine.default(ValidationConfig())
    # M is the whole resolved file: verify it directly (no splicing).
    res = eng.verify_file(
        case.path, case.language, case.expected_resolved, [],
        repo_root=str(tmp_path),
    )
    # The floor must have engaged (py_compile is always on). syntax_checked=False
    # would mean the infrastructure regressed, not a real finding.
    assert res.features.get("syntax_checked") is True, (
        f"{case.id}: py_compile floor did not engage (syntax_checked=False) — "
        f"infrastructure regression. features={res.features}"
    )
    # Record the verdict (not asserted): a human merge that doesn't compile is an
    # honest real-world signal, recorded not hard-failed.
    if not res.passed:
        msgs = [f.message[:80] for f in res.hard_failures[:2]]
        print(f"  {case.id}: human merge did not pass py_compile: {msgs}")


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_realworld_rust_merge_cargo_verdict(case: RealWorldCase):
    """Rust: does the crate at merge commit M compile? (record honestly).

    Checks out the cloned repo at M and runs ``cargo check`` — the authentic
    whole-crate signal. ``verify_file``/standalone ``rustc`` can't do this: they
    can't resolve ``crate::`` paths (E0432), and ``verify_file``'s baseline model
    degenerates at M (the file is already marker-free). See the section comment.

    Asserts only that cargo ENGAGED (``ran``); the compile verdict is recorded,
    not asserted — a real-world merge that doesn't build on our toolchain is an
    informative flag, not a failure.
    """
    if case.language != "rust":
        pytest.skip("Rust-only cargo verdict (Python uses py_compile separately)")
    _rust_clone_or_skip(case)
    clone = git_history_repo_path(case.dataset)
    # One worktree, many cases: serialize the checkout/check/restore so cases
    # don't race on the clone's working tree.
    with _CLONE_LOCK:
        ran, errors = _cargo_check_at_merge(clone, case.merge_sha)
    # Infrastructure invariant: cargo must have engaged. ``ran=False`` means it
    # couldn't run (absent/timeout/crash) — that's an infra regression, not a
    # real finding about the merge.
    assert ran, (
        f"{case.id}: cargo check did not engage at {case.merge_sha[:12]} "
        f"(checkout failed, timeout, or cargo absent) — infrastructure "
        f"regression, not a real finding."
    )
    # Record the verdict honestly (not asserted). A serde merge that compiles on
    # our toolchain is a strong pass signal; one that doesn't is an honest flag
    # (newer edition, platform-specific deps, pre-existing errors).
    verdict = "PASS" if not errors else f"FAIL ({len(errors)} error(s))"
    print(f"  {case.id}: cargo check at {case.merge_sha[:12]} ({case.conflict_path}): {verdict}")
    for e in errors[:3]:
        print(f"    {e.strip()[:120]}")
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
from serde's git history; those Rust cases run an AUTHENTIC cargo check — M is
checked out in a disposable git worktree of the cloned repo and ``cargo check``
runs the whole crate with real deps/edition/sibling files. The shared clone is
read-only (never checked out), so the Rust cases are interrupt- and xdist-safe
(orphaned worktrees from a Ctrl-C'd run are pruned by the session fixture).

Why not ``verify_file`` for Rust? Standalone ``rustc`` (and ``verify_file``
against a bare tmp_path) can't resolve ``crate::``/``super::`` paths, so they
FALSE-POSITIVE on virtually every serde file (E0432). And ``verify_file``'s
baseline/new-error model degenerates at M: the file is already the marker-free
human merge, so baseline == after and it reports ``syntax_passed=True``
regardless. The only honest check is the whole crate at the committed resolved
state, which is what checking out M in a worktree gives us. See
``tests/_realworld_cargo.py`` for the worktree harness.

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

import pytest

from capybase.adapters.parsers import (
    contains_markers,
    parse_marker_blocks,
)
from capybase.verification import ValidationConfig, VerificationEngine

from tests._realworld_cargo import (
    cargo_check_at_worktree,
    cleanup_orphan_worktrees,
)
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


# ---------------------------------------------------------------------------
# Session setup: prune any worktrees orphaned by an interrupted previous run.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="session")
def _prune_orphan_worktrees():
    """Remove git worktrees left behind by a Ctrl-C'd previous run.

    Each Rust case checks out its merge commit in a disposable worktree of the
    shared clone (see :func:`tests._realworld_cargo.cargo_check_at_worktree`).
    The worktree is removed in a ``finally``, but an interrupt between creation
    and cleanup orphans it. This fixture prunes those once per session before
    the cases run, so leftover worktrees (and their build artifacts) don't
    accumulate. Idempotent and safe: an already-clean clone is a no-op, and the
    main clone itself is never touched.
    """
    # Only the datasets actually present have a clone to clean; missing clones
    # (no data downloaded) are skipped — nothing to prune.
    seen: set[str] = set()
    for case in CASES:
        if case.language != "rust" or case.dataset in seen:
            continue
        seen.add(case.dataset)
        clone = git_history_repo_path(case.dataset)
        if (clone / ".git").exists():
            cleanup_orphan_worktrees(clone)
    yield


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
# - **Rust**: ``cargo check`` run in a disposable git worktree checked out at
#   the merge commit M. This is the authentic signal: standalone ``rustc`` (and
#   ``verify_file`` against a bare tmp_path) can't resolve ``crate::``/``super::``
#   paths, so they FALSE-POSITIVE on virtually every serde file (E0432). Worse,
#   ``verify_file``'s baseline/new-error model degenerates at M — the file is
#   already the marker-free human merge, so baseline == after and it reports
#   ``syntax_passed=True`` regardless. The only honest check is the whole crate
#   at the committed resolved state, which is what checking out M (in a
#   read-only-clone worktree) gives us. The worktree is removed afterward, so
#   the shared clone is never mutated — interrupt- and xdist-safe.
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

    Checks out M in a disposable worktree of the cloned repo and runs
    ``cargo check`` — the authentic whole-crate signal. The shared clone is
    read-only (never checked out), so this is interrupt- and xdist-safe: each
    case builds in its own worktree that's removed afterward.
    ``verify_file``/standalone ``rustc`` can't do this: they can't resolve
    ``crate::`` paths (E0432), and ``verify_file``'s baseline model degenerates
    at M (the file is already marker-free). See the section comment.

    Asserts only that cargo ENGAGED (``verdict.ran``); the compile verdict is
    recorded, not asserted — a real-world merge that doesn't build on our
    toolchain is an informative flag, not a failure.
    """
    if case.language != "rust":
        pytest.skip("Rust-only cargo verdict (Python uses py_compile separately)")
    _rust_clone_or_skip(case)
    clone = git_history_repo_path(case.dataset)
    # A disposable worktree at M: the clone stays read-only, so no lock is
    # needed and concurrent cases/workers can't race it.
    verdict = cargo_check_at_worktree(clone, case.merge_sha)
    # Infrastructure invariant: cargo must have engaged. ``ran=False`` means it
    # couldn't run (absent/timeout/worktree failed) — that's an infra regression,
    # not a real finding about the merge.
    assert verdict.ran, (
        f"{case.id}: cargo check did not engage at {case.merge_sha[:12]} "
        f"(worktree materialization failed, timeout, or cargo absent) — "
        f"infrastructure regression, not a real finding. errors={verdict.errors}"
    )
    # Record the verdict honestly (not asserted). A serde merge that compiles on
    # our toolchain is a strong pass signal; one that doesn't is an honest flag
    # (newer edition, platform-specific deps, pre-existing errors).
    print(
        f"  {case.id}: cargo check at {case.merge_sha[:12]} "
        f"({case.conflict_path}): {verdict.verdict}"
    )
    for e in verdict.errors[:3]:
        print(f"    {e.strip()[:120]}")
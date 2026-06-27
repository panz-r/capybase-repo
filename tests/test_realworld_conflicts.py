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
(no toolchain gate). Rust cases, when a Rust-bearing dataset is added, will run
against the cargo floor (cargo-gated per case).

Oracle policy: real-world M is the human merge, but it may NOT compile under our
floor if the original repo had pre-existing errors or used a different toolchain.
So we do NOT force ``passed``; we assert the case was *checked* (markers parse,
the merge splices marker-free) and record the verifier's verdict honestly — the
value is "does capybase accept the human merge", not "every real merge passes".
A high accept rate validates the floor; a low rate flags real-world rough edges.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from capybase.adapters.parsers import (
    contains_markers,
    parse_marker_blocks,
)
from capybase.verification import ValidationConfig, VerificationEngine

from tests.realworld_loader import RealWorldCase, load_realworld_cases

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


def _needs_toolchain(case: RealWorldCase) -> bool:
    """Rust cases need cargo (loose-file rustc or cargo floor); Python's
    py_compile floor is always available. So only Rust cases are cargo-gated."""
    return case.language == "rust"


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_realworld_human_merge_verifier_verdict(case: RealWorldCase, tmp_path):
    """The verifier runs against the human merge; record the verdict honestly.

    The human resolution M is the whole merged file. We pass it as the file to
    verify (``original=M`` with no resolutions → the verifier checks M directly).
    Real-world M may NOT pass our floor: the original repo could have
    pre-existing errors, an unsupported toolchain, or M resolves all hunks while
    a single-span splice wouldn't. So this test does NOT force ``passed`` — it
    asserts the verifier RAN (``syntax_checked`` reflects whether the floor
    engaged) and records the verdict. The value is "does capybase accept the
    human merge", an honest real-world signal.

    Python's py_compile floor runs everywhere (no toolchain gate); Rust needs
    cargo, so Rust cases skip when cargo is absent.
    """
    if _needs_toolchain(case) and CARGO is None:
        pytest.skip("cargo not installed")
    eng = VerificationEngine.default(ValidationConfig())
    # M is the whole resolved file: verify it directly (no splicing).
    res = eng.verify_file(
        case.path, case.language, case.expected_resolved, [],
        repo_root=str(tmp_path),
    )
    # The verifier must have engaged. Python's py_compile is always on; Rust's
    # floor needs a toolchain (gated above). syntax_checked=False would mean the
    # infrastructure regressed (a supported floor failed to run).
    assert res.features.get("syntax_checked") is True, (
        f"{case.id}: the compile floor did not engage on the human merge "
        f"(syntax_checked=False) — infrastructure regression, not a real "
        f"finding. features={res.features}"
    )
    # Record the verdict in the test output (not asserted): a real-world merge
    # that compiles is a pass signal; one that doesn't is an informative flag.
    # We only hard-fail on the verifier not engaging (above) — a real-world merge
    # that trips the floor is an honest finding, recorded not asserted.
    if not res.passed:
        msgs = [f.message[:80] for f in res.hard_failures[:2]]
        print(f"  {case.id}: human merge did not pass the floor: {msgs}")
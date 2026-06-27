"""Parametrized tests over the synthetic Rust conflict catalog.

Drives every :class:`RustConflict` in ``tests/rust_conflict_catalog`` through the
verifier's compile floor in both directions:

1. **Accept**: ``verify_file`` with ``expected_resolved`` passes and
   ``syntax_passed is True`` (the known-good merge compiles).
2. **Reject**: ``verify_file`` with ``broken_resolved`` fails with a ``syntax``
   hard failure (the broken merge is caught).
3. **Well-formed**: the catalog's authentic ``original`` (from ``git merge-file``)
   round-trips through ``parse_marker_blocks`` → ``splice_all_resolutions`` to a
   marker-free file.

Cargo-backed cases skip on CI without cargo; the lone loose-file case skips on
CI without rustc. This is the living taxonomy coverage (see
``tests/rust_corpus_coverage.md``).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from capybase.adapters.parsers import (
    contains_markers,
    parse_marker_blocks,
    splice_all_resolutions,
)
from capybase.verification import ValidationConfig, VerificationEngine

from tests.rust_conflict_catalog import (
    RUST_CONFLICTS,
    RustConflict,
    build_markers,
)

CARGO = shutil.which("cargo")
RUSTC = shutil.which("rustc")


def _span(original: str) -> tuple[int, int]:
    blocks = parse_marker_blocks(original)
    assert len(blocks) == 1, f"expected exactly one conflict block, got {len(blocks)}"
    return blocks[0].span


def _write_scaffold(repo_root: Path, conflict: RustConflict) -> None:
    """Write the catalog case's scaffold files under repo_root."""
    for rel, content in conflict.scaffold.items():
        p = repo_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    # Ensure the conflicted file's directory exists.
    (repo_root / conflict.path).parent.mkdir(parents=True, exist_ok=True)


def _verify(conflict: RustConflict, resolved: str, tmp_path: Path):
    """Run verify_file on a catalog case with a given resolved span text."""
    original = build_markers(conflict.base, conflict.current, conflict.replayed)
    eng = VerificationEngine.default(ValidationConfig())
    # The Cargo.toml case uses its own sibling crate scaffold (handled in the
    # test); here write the generic scaffold.
    _write_scaffold(tmp_path, conflict)
    # For the Cargo.toml dependency case, provide the sibling crate the manifest
    # references so the resolved manifest resolves offline.
    if conflict.id == "cargo_dep_version":
        sibling = tmp_path.parent / "sibling"
        sibling.mkdir(exist_ok=True)
        (sibling / "Cargo.toml").write_text(
            '[package]\nname = "sibling"\nversion = "2.0.0"\nedition = "2021"\n'
        )
        (sibling / "src").mkdir(exist_ok=True)
        (sibling / "src" / "lib.rs").write_text("pub fn sib() -> u32 { 2 }\n")
    return eng.verify_file(
        conflict.path, conflict.language, original,
        [(_span(original), resolved)], repo_root=str(tmp_path),
    )


# ---------------------------------------------------------------------------
# Accept: the known-good merge compiles.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "conflict", [c for c in RUST_CONFLICTS if c.needs_cargo],
    ids=[c.id for c in RUST_CONFLICTS if c.needs_cargo],
)
def test_cargo_conflict_expected_resolved_passes(conflict: RustConflict, tmp_path):
    """The catalog's known-good merge passes the cargo compile floor."""
    if CARGO is None:
        pytest.skip("cargo not installed")
    res = _verify(conflict, conflict.expected_resolved, tmp_path)
    assert res.features["syntax_checked"] is True, (
        f"{conflict.id}: syntax not checked — {res.features}"
    )
    assert res.passed, (
        f"{conflict.id}: expected merge FAILED the compile floor: "
        f"{[f.message for f in res.hard_failures]}"
    )


def test_loose_conflict_expected_resolved_passes(tmp_path):
    """The lone loose-file case passes the standalone-rustc floor."""
    if RUSTC is None:
        pytest.skip("rustc not installed")
    conflict = next(c for c in RUST_CONFLICTS if not c.needs_cargo)
    res = _verify(conflict, conflict.expected_resolved, tmp_path)
    assert res.features["syntax_checked"] is True
    assert res.passed, (
        f"{conflict.id}: expected merge FAILED: "
        f"{[f.message for f in res.hard_failures]}"
    )


# ---------------------------------------------------------------------------
# Reject: the known-broken merge is caught.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "conflict", [c for c in RUST_CONFLICTS if c.needs_cargo],
    ids=[c.id for c in RUST_CONFLICTS if c.needs_cargo],
)
def test_cargo_conflict_broken_resolved_fails(conflict: RustConflict, tmp_path):
    """The catalog's known-broken merge is caught as a syntax failure."""
    if CARGO is None:
        pytest.skip("cargo not installed")
    res = _verify(conflict, conflict.broken_resolved, tmp_path)
    assert res.features["syntax_checked"] is True, (
        f"{conflict.id}: syntax not checked — {res.features}"
    )
    assert not res.passed, (
        f"{conflict.id}: broken merge was ACCEPTED (should have failed): "
        f"{[f.message for f in res.hard_failures]}"
    )
    syntax_fails = [f for f in res.hard_failures if f.validator == "syntax"]
    assert syntax_fails, (
        f"{conflict.id}: broken merge failed but not via a syntax failure: "
        f"{[f.validator for f in res.hard_failures]}"
    )


def test_loose_conflict_broken_resolved_fails(tmp_path):
    """The lone loose-file case's broken merge is caught by standalone rustc."""
    if RUSTC is None:
        pytest.skip("rustc not installed")
    conflict = next(c for c in RUST_CONFLICTS if not c.needs_cargo)
    res = _verify(conflict, conflict.broken_resolved, tmp_path)
    assert not res.passed
    assert any(f.validator == "syntax" for f in res.hard_failures)


# ---------------------------------------------------------------------------
# Well-formed: the catalog's authentic markers round-trip through splice.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "conflict", RUST_CONFLICTS, ids=[c.id for c in RUST_CONFLICTS],
)
def test_catalog_markers_round_trip(conflict: RustConflict):
    """The git-generated markers parse to one block and splice marker-free.

    This runs WITHOUT any toolchain (it only needs git) and guards the corpus's
    structural integrity: every case produces exactly one conflict block, and
    splicing the expected resolution leaves no markers behind.
    """
    original = build_markers(conflict.base, conflict.current, conflict.replayed)
    blocks = parse_marker_blocks(original)
    assert len(blocks) == 1, f"{conflict.id}: expected 1 block, got {len(blocks)}"
    spliced = splice_all_resolutions(
        original, [(blocks[0].span, conflict.expected_resolved)]
    )
    assert not contains_markers(spliced), (
        f"{conflict.id}: markers leaked after splicing the expected resolution"
    )

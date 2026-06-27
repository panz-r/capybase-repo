"""Property tests for verifier robustness via the catalog-mutation generator.

Round 3 (Method C, honest form). The generator (``tests/rust_mutation_generator``)
applies structure-preserving mutations to the curated catalog cases and these
tests assert verifier invariants the hand-authored rows cannot state:

1. **No-crash**: ``verify_file`` returns a result (never raises) on the mutated
   expected/broken splices.
2. **Verdict-invariance**: a structure-preserving mutation applied consistently
   across all three git sides PRESERVES the case's accept/reject verdict. The
   expected merge stays ``passed``; the broken merge stays ``not passed``. A flip
   is a real bug signal (the verifier is sensitive to something it shouldn't be)
   and fails the assertion with the mutation label.

These run through the same ``verify_file`` harness as the catalog tests, so they
skip on CI without cargo (the mutations need crate context to resolve). The
generator's own unit tests (does each mutator apply / skip correctly) run with no
toolchain.
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

from tests.rust_conflict_catalog import RUST_CONFLICTS, build_markers
from tests.rust_mutation_generator import (
    ALL_MUTATIONS,
    MUTATIONS_BY_CASE,
    Mutation,
    bump_numeric_literal,
    generate_mutations,
    rename_identifier,
)

CARGO = shutil.which("cargo")
RUSTC = shutil.which("rustc")


# ---------------------------------------------------------------------------
# Helpers (mirror tests/test_rust_conflict_catalog.py's _verify).
# ---------------------------------------------------------------------------


def _span(original: str) -> tuple[int, int]:
    blocks = parse_marker_blocks(original)
    assert len(blocks) == 1, f"expected one conflict block, got {len(blocks)}"
    return blocks[0].span


def _write_scaffold(repo_root: Path, conflict) -> None:
    from tests.rust_conflict_catalog import RustConflict

    for rel, content in conflict.scaffold.items():
        p = repo_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    (repo_root / conflict.path).parent.mkdir(parents=True, exist_ok=True)


def _verify_mutation(mutation: Mutation, case, resolved: str, tmp_path: Path):
    """Run verify_file on a mutated case with a given resolved span text."""
    original = build_markers(mutation.base, mutation.current, mutation.replayed)
    eng = VerificationEngine.default(ValidationConfig(enable_shadow_tests=case.shadow_test))
    _write_scaffold(tmp_path, case)
    return eng.verify_file(
        case.path, case.language, original,
        [(_span(original), resolved)], repo_root=str(tmp_path),
    )


# ---------------------------------------------------------------------------
# Generator unit tests (no toolchain needed).
# ---------------------------------------------------------------------------


def test_generator_produces_mutations():
    """The generator yields at least one mutation across the catalog."""
    assert len(ALL_MUTATIONS) > 0
    # rename_ident should apply broadly (most cases have a local identifier).
    labels = {m.label for _, m in ALL_MUTATIONS}
    assert "rename_ident" in labels


@pytest.mark.parametrize("case_id", [c.id for c in RUST_CONFLICTS])
def test_each_mutation_yields_a_genuine_conflict(case_id):
    """Every generated mutation produces exactly one conflict block (no clean merge).

    This is the structural integrity guard: build_markers raised RuntimeError
    for clean merges (filtered in generate_mutations), and the surviving mutation
    must splice marker-free. Runs without cargo.
    """
    case = next(c for c in RUST_CONFLICTS if c.id == case_id)
    for m in MUTATIONS_BY_CASE[case_id]:
        original = build_markers(m.base, m.current, m.replayed)
        blocks = parse_marker_blocks(original)
        assert len(blocks) == 1, f"{case_id}/{m.label}: expected 1 block"
        spliced = splice_all_resolutions(
            original, [(blocks[0].span, m.expected_resolved)]
        )
        assert not contains_markers(spliced), (
            f"{case_id}/{m.label}: markers leaked after splice"
        )


def test_bump_literal_skips_when_no_common_literal():
    """bump_numeric_literal returns None when no integer literal is common."""
    from tests.rust_conflict_catalog import RustConflict

    c = RustConflict(
        id="x", path="s.rs", language="rust",
        base="pub fn a() {}\n", current="pub fn b() {}\n",
        replayed="pub fn c() {}\n",
        expected_resolved="pub fn b() {}", broken_resolved="pub fn b(",
        taxonomy=(), needs_cargo=False,
    )
    assert bump_numeric_literal(c) is None


def test_rename_skips_non_rust():
    """rename_identifier is Rust-only (skips Cargo.toml / loose text cases)."""
    from tests.rust_conflict_catalog import RustConflict

    c = RustConflict(
        id="x", path="Cargo.toml", language="toml",
        base='[package]\nname = "x"\n', current='[package]\nname = "y"\n',
        replayed='[package]\nname = "z"\n',
        expected_resolved='name = "y"', broken_resolved='name = "y',
        taxonomy=(), needs_cargo=True,
    )
    assert rename_identifier(c) is None


# ---------------------------------------------------------------------------
# Property test 1: no-crash. verify_file never raises on mutated splices.
# ---------------------------------------------------------------------------


_NO_CRASH_CASES = [
    (cid, m) for cid, m in ALL_MUTATIONS
    if next(c for c in RUST_CONFLICTS if c.id == cid).needs_cargo
]


@pytest.mark.skipif(CARGO is None, reason="cargo not installed")
@pytest.mark.parametrize(
    "case_id, mutation", _NO_CRASH_CASES,
    ids=[f"{cid}-{m.label}" for cid, m in _NO_CRASH_CASES],
)
def test_mutation_does_not_crash_verifier(case_id, mutation, tmp_path):
    """verify_file returns a result (never raises) on the mutated splices."""
    case = next(c for c in RUST_CONFLICTS if c.id == case_id)
    # Both the expected and broken resolved texts must not raise.
    for resolved in (mutation.expected_resolved, mutation.broken_resolved):
        res = _verify_mutation(mutation, case, resolved, tmp_path)
        assert res is not None  # it returned a result rather than raising


# ---------------------------------------------------------------------------
# Property test 2: verdict-invariance. A cosmetic mutation preserves the verdict.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(CARGO is None, reason="cargo not installed")
@pytest.mark.parametrize(
    "case_id, mutation", _NO_CRASH_CASES,
    ids=[f"{cid}-{m.label}" for cid, m in _NO_CRASH_CASES],
)
def test_mutation_preserves_verdict(case_id, mutation, tmp_path):
    """A structure-preserving mutation preserves the case's accept/reject verdict.

    The expected merge stays ``passed``; the broken merge stays ``not passed``.
    A flip means the verifier is sensitive to a cosmetic change it shouldn't be.
    """
    case = next(c for c in RUST_CONFLICTS if c.id == case_id)

    res_expected = _verify_mutation(mutation, case, mutation.expected_resolved, tmp_path)
    res_broken = _verify_mutation(mutation, case, mutation.broken_resolved, tmp_path)

    assert res_expected.passed, (
        f"{case_id}/{mutation.label}: expected merge FLIPPED to reject after a "
        f"cosmetic mutation (was accept). hard_failures: "
        f"{[f.message for f in res_expected.hard_failures]}"
    )
    # The broken merge must remain rejected. (For shadow_test cases the broken
    # merge compiles but fails the test — still "not passed".)
    assert not res_broken.passed, (
        f"{case_id}/{mutation.label}: broken merge FLIPPED to accept after a "
        f"cosmetic mutation (was reject). This means the mutation accidentally "
        f"fixed the failure — re-check the mutator or the case."
    )

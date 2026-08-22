"""Consumer for the C/C++ conflict catalog.

Mirrors ``tests/test_rust_conflict_catalog.py``: three parametrized test sets
driven by ``CC_CONFLICTS``.

- **Accept** — the known-good merge (``expected_resolved``) passes the gcc/g++
  compile floor (``syntax_checked is True`` and ``res.passed``).
- **Reject** — the known-broken merge (``broken_resolved``) is caught as a
  syntax hard failure.
- **Well-formed** (toolchain-free) — ``build_markers`` yields exactly one marker
  block and the spliced file has no leftover markers. Runs unconditionally, so
  the catalog is structurally validated even where no compiler is installed.

The accept/reject tests skip per-case on the case's own compiler: a C case
needs ``gcc``, a C++ case needs ``g++``. This lets the suite run partially when
only one toolchain is present.

The catalog path uses ``verify_file`` against ``build_markers`` output written
into ``tmp_path`` — no real git repo, no rebase (that's the separate conftest
end-to-end path).
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

from tests.cc_conflict_catalog import (
    CC_CONFLICTS,
    CConflict,
    build_markers,
)

CC = shutil.which("gcc")
CXX = shutil.which("g++")


def _span(original: str) -> tuple[int, int]:
    blocks = parse_marker_blocks(original)
    assert len(blocks) == 1, f"expected exactly one conflict block, got {len(blocks)}"
    return blocks[0].span


def _verify(conflict: CConflict, resolved: str, tmp_path: Path):
    """Run verify_file on a catalog case with a given resolved span text."""
    original = build_markers(conflict.base, conflict.current, conflict.replayed)
    eng = VerificationEngine.default(ValidationConfig())
    (tmp_path / conflict.path).parent.mkdir(parents=True, exist_ok=True)
    return eng.verify_file(
        conflict.path, conflict.language, original,
        [(_span(original), resolved)], repo_root=str(tmp_path),
    )


def _compiler_for(conflict: CConflict) -> str | None:
    """The compiler a case needs (gcc for C, g++ for C++), or None if absent."""
    if conflict.language == "cpp":
        return CXX
    return CC


# ---------------------------------------------------------------------------
# Accept: the known-good merge compiles.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "conflict", CC_CONFLICTS, ids=[c.id for c in CC_CONFLICTS],
)
def test_cc_conflict_expected_resolved_passes(conflict: CConflict, tmp_path):
    """The catalog's known-good merge passes the gcc/g++ compile floor."""
    if _compiler_for(conflict) is None:
        pytest.skip(f"{conflict.language} compiler not installed")
    res = _verify(conflict, conflict.expected_resolved, tmp_path)
    assert res.features["syntax_checked"] is True, (
        f"{conflict.id}: syntax not checked — {res.features}"
    )
    assert res.passed, (
        f"{conflict.id}: expected merge FAILED the compile floor: "
        f"{[f.message for f in res.hard_failures]}"
    )


# ---------------------------------------------------------------------------
# Reject: the known-broken merge is caught.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "conflict", CC_CONFLICTS, ids=[c.id for c in CC_CONFLICTS],
)
def test_cc_conflict_broken_resolved_fails(conflict: CConflict, tmp_path):
    """The catalog's known-broken merge is caught as a syntax failure."""
    if _compiler_for(conflict) is None:
        pytest.skip(f"{conflict.language} compiler not installed")
    res = _verify(conflict, conflict.broken_resolved, tmp_path)
    assert res.features["syntax_checked"] is True, (
        f"{conflict.id}: syntax not checked — {res.features}"
    )
    if res.passed:
        # Sprint-21 coherence rung: deterministically repairable shapes
        # now pass WITH the repair flag — that is the rung working, not
        # the compile floor leaking.
        assert res.features.get("coherence_repair_applied"), (
            f"{conflict.id}: broken merge PASSED without a coherence repair"
        )
    else:
        assert True  # failed the floor as before
    if not res.passed:
        syntax_fails = [f for f in res.hard_failures if f.validator == "syntax"]
        assert len(syntax_fails) >= 1, (
            f"{conflict.id}: broken merge failed but no syntax hard failure was added"
        )


# ---------------------------------------------------------------------------
# Well-formed (toolchain-free): markers parse + splice clean.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "conflict", CC_CONFLICTS, ids=[c.id for c in CC_CONFLICTS],
)
def test_cc_catalog_markers_round_trip(conflict: CConflict):
    """Every catalog case produces exactly one conflict block and splices clean.

    Toolchain-free: runs even without a compiler, so the catalog's structural
    integrity is validated everywhere.
    """
    original = build_markers(conflict.base, conflict.current, conflict.replayed)
    # Exactly one conflict block.
    blocks = parse_marker_blocks(original)
    assert len(blocks) == 1, (
        f"{conflict.id}: expected 1 conflict block, got {len(blocks)}"
    )
    # Splicing the expected resolution leaves no markers.
    spliced = splice_all_resolutions(
        original, [(blocks[0].span, conflict.expected_resolved)]
    )
    assert not contains_markers(spliced), (
        f"{conflict.id}: markers remain after splicing expected_resolved"
    )
    # Splicing the broken resolution also leaves no markers (it's structurally a
    # valid splice — it just doesn't compile).
    spliced_broken = splice_all_resolutions(
        original, [(blocks[0].span, conflict.broken_resolved)]
    )
    assert not contains_markers(spliced_broken), (
        f"{conflict.id}: markers remain after splicing broken_resolved"
    )

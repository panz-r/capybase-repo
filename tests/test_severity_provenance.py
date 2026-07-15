"""Tests for conflict severity grading and per-side provenance.

Severity is a pure function of a ConflictUnit (no I/O, no model); provenance is
populated by the extractor via git_backend and verified against a real temp repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from capybase.conflict_extractor import compute_severity
from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.git_backend import GitBackend

from tests.conftest import git


def _unit(base: str, current: str, replayed: str, **kw) -> ConflictUnit:
    def _side(label, text):
        return ConflictSide(label=label, text=text)  # type: ignore[arg-type]

    return ConflictUnit(
        session_id="s", step_index=0, path="f.py", unit_id="u",
        base=_side("BASE", base),
        current=_side("CURRENT_UPSTREAM_SIDE", current),
        replayed=_side("REPLAYED_COMMIT_SIDE", replayed),
        original_worktree_text=base,
        **kw,
    )


# ---------------------------------------------------------------------------
# severity grading — pure function
# ---------------------------------------------------------------------------


def test_severity_low_for_small_disjoint_conflict():
    # Tiny, no definition, no same-line overlap → low.
    u = _unit("a = 1\nb = 1", "a = 2\nb = 1", "a = 1\nb = 2")
    assert compute_severity(u) == "low"


def test_severity_medium_for_same_line_overlap():
    # Both sides change the SAME line (real conflict) → medium (small, not def).
    u = _unit("x = 1", "x = 2", "x = 3")
    assert compute_severity(u) == "medium"


def test_severity_high_for_large_definition_touching():
    # Large hunk (>=30 non-empty lines) touching a definition → high.
    big = "\n".join(f"    line_{i} = {i}" for i in range(20))  # 20 body lines
    base = f"def f():\n{big}\n"
    current = f"def f():\n{big}\n    extra = 1\n"
    replayed = f"def f():\n{big}\n    extra = 2\n"
    u = _unit(base, current, replayed, enclosing_symbol="f")
    assert compute_severity(u) == "high"


def test_severity_low_when_one_side_only_changed():
    # Only one side changed (disjoint from the other) → low (small, no overlap).
    u = _unit("x = 1", "x = 2", "x = 1")
    assert compute_severity(u) == "low"


def test_severity_uses_enclosing_node_metadata_as_definition_signal():
    # No enclosing_symbol, but structural_metadata has enclosing_node_text → def.
    big = "\n".join(f"l{i} = {i}" for i in range(25))
    u = _unit(f"{big}\n", f"{big}\nX=1\n", f"{big}\nX=2\n",
              structural_metadata={"enclosing_node_text": "def f(): ..."})
    assert compute_severity(u) == "high"


def test_severity_medium_default_for_moderate_overlap():
    # Moderate size, same-line overlap, no definition → medium.
    lines = "\n".join(f"l{i} = {i}" for i in range(8))
    u = _unit(lines, lines.replace("l0 = 0", "l0 = 9"), lines.replace("l0 = 0", "l0 = 8"))
    assert compute_severity(u) == "medium"


# ---------------------------------------------------------------------------
# ConflictUnit.severity default + extraction wiring
# ---------------------------------------------------------------------------


def test_conflict_unit_severity_default_is_medium():
    u = _unit("x", "y", "z")
    assert u.severity == "medium"


def _extract(gb, path="app.py"):
    """Helper: extract units the way the real pipeline does, with real OIDs."""
    from capybase.conflict_extractor import ConflictExtractor
    from capybase.config import StructuralConfig

    extractor = ConflictExtractor(gb, structural_config=StructuralConfig(enabled=False))
    unmerged = next(
        (u for u in gb.list_unmerged_paths() if u.path == path), None
    )
    return extractor.extract_file_units(
        step_index=0, session_id="s", path=path, unmerged=unmerged,
    )


def test_extractor_assigns_severity(conflicted_repo):
    """The extractor must populate unit.severity for every extracted unit."""
    repo = conflicted_repo["repo"]
    gb = GitBackend(str(repo))
    units = _extract(gb)
    assert units, "expected at least one conflict unit"
    for u in units:
        assert u.severity in ("low", "medium", "high")


# ---------------------------------------------------------------------------
# provenance — populated by extractor via git_backend, against a real repo
# ---------------------------------------------------------------------------


def test_last_touch_blob_resolves_introducing_commit(repo: Path):
    """git_backend.last_touch_blob attributes a blob OID to its commit."""
    (repo / "f.txt").write_text("hello\n")
    git(repo, "add", "f.txt")
    git(repo, "commit", "-q", "-m", "add file")
    # Get the blob OID of f.txt at HEAD.
    oid = git(repo, "rev-parse", "HEAD:f.txt").stdout.strip()
    gb = GitBackend(str(repo))
    sha, subject = gb.last_touch_blob(oid)
    assert sha != ""
    assert subject == "add file"


def test_last_touch_blob_missing_returns_empty(repo: Path):
    gb = GitBackend(str(repo))
    sha, subject = gb.last_touch_blob("0" * 40)  # nonexistent OID
    assert sha == "" and subject == ""


def test_extractor_populates_provenance(conflicted_repo):
    """Each extracted unit carries per-side provenance in structural_metadata."""
    repo = conflicted_repo["repo"]
    gb = GitBackend(str(repo))
    units = _extract(gb)
    assert units
    prov = units[0].structural_metadata.get("provenance")
    assert prov is not None, "provenance must be populated"
    assert "current" in prov and "replayed" in prov and "base" in prov
    # The replayed side should attribute to the "replayed change" commit.
    assert prov["replayed"]["subject"] == "replayed change"
    assert prov["current"]["subject"] == "upstream change"


# ---------------------------------------------------------------------------
# risk engine consumes severity
# ---------------------------------------------------------------------------


def test_risk_score_incorporates_severity():
    from capybase.risk import _risk_score

    base = {"syntax_passed": True}
    low = _risk_score({**base, "conflict_severity": 0.0})
    high = _risk_score({**base, "conflict_severity": 2.0})
    assert high > low, "high-severity conflict must raise the risk score"
    assert high - low == pytest.approx(0.2)

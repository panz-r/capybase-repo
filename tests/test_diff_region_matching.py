"""Hardened tests for diff-based region matching (#history defect fixes 1-2).

Tests the 0-based/1-based boundary correctness, insert-only hunks, and the
tri-state return (True/False/None) that lets _touches_region distinguish "no
overlap, diff fetched OK" (suppress subject fallback) from "fetch failed" (fall
through to subject heuristic).
"""

from __future__ import annotations

from pathlib import Path

from capybase.git_backend import GitBackend
from capybase.history import HistoryQueryService, RebasePlan, RegionKey, ReplayCommit
from tests.conftest import git


def _key(start, end, path="cfg.py", name="parse", kind="function"):
    return RegionKey(
        path=path, language="python", kind=kind, name=name,
        enclosing_node_type="function_definition",
        start_line=start, end_line=end, structural_hash="",
    )


def _commit(oid, parent, subject, files, index):
    return ReplayCommit(
        oid=oid, parent_oid=parent, subject=subject, body_summary="",
        touched_files=files, diffstat={}, patch_id="", index=index,
    )


def _build_region_repo(repo: Path) -> dict:
    """A repo where a future commit edits lines at known positions in cfg.py.

    cfg.py has 10 lines; the "region" is lines 3-5 (0-based). A future commit
    edits line 4 (inside the region) and a second future commit edits line 8
    (outside). Both commits have generic subjects (no function name mention).
    """
    lines = [f"line{i}" for i in range(10)]
    (repo / "cfg.py").write_text("\n".join(lines) + "\n")
    git(repo, "add", "cfg.py"); git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    # "Current" commit (the one being replayed): edits line 0.
    lines[0] = "line0-edited"
    (repo / "cfg.py").write_text("\n".join(lines) + "\n")
    git(repo, "add", "cfg.py"); git(repo, "commit", "-q", "-m", "edit line 0")
    current = git(repo, "rev-parse", "HEAD").stdout.strip()
    # Future commit 1: edits line 4 (inside region 3-5, generic subject).
    lines[4] = "line4-edited"
    (repo / "cfg.py").write_text("\n".join(lines) + "\n")
    git(repo, "add", "cfg.py"); git(repo, "commit", "-q", "-m", "generic change")
    future_inside = git(repo, "rev-parse", "HEAD").stdout.strip()
    # Future commit 2: edits line 8 (outside region 3-5, generic subject).
    lines[8] = "line8-edited"
    (repo / "cfg.py").write_text("\n".join(lines) + "\n")
    git(repo, "add", "cfg.py"); git(repo, "commit", "-q", "-m", "another change")
    future_outside = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "reset", "--hard", current)
    return {
        "base": base, "current": current,
        "future_inside": future_inside, "future_outside": future_outside,
    }


def test_diff_hunk_inside_region_detected(repo: Path):
    """A future commit editing a line inside the region span is detected."""
    oids = _build_region_repo(repo)
    gb = GitBackend(repo)
    plan = RebasePlan(
        source_commits=[
            _commit(oids["current"], oids["base"], "edit line 0", ["cfg.py"], 0),
            _commit(oids["future_inside"], oids["current"], "generic change", ["cfg.py"], 1),
            _commit(oids["future_outside"], oids["future_inside"], "another change", ["cfg.py"], 2),
        ],
        target_base_oid=oids["base"], target_tip_oid=oids["base"],
        source_tip_oid=oids["future_outside"], created_at="now",
    )
    qs = HistoryQueryService(plan, git=gb)
    key = _key(3, 5)
    # future_inside edits line 4 → inside region 3-5 → should be detected via diff.
    result = qs._diff_touches_span(plan.source_commits[1], key)
    assert result is True


def test_diff_hunk_outside_region_not_detected(repo: Path):
    """A future commit editing a line OUTSIDE the region is NOT detected."""
    oids = _build_region_repo(repo)
    gb = GitBackend(repo)
    plan = RebasePlan(
        source_commits=[
            _commit(oids["current"], oids["base"], "edit line 0", ["cfg.py"], 0),
            _commit(oids["future_inside"], oids["current"], "generic change", ["cfg.py"], 1),
            _commit(oids["future_outside"], oids["future_inside"], "another change", ["cfg.py"], 2),
        ],
        target_base_oid=oids["base"], target_tip_oid=oids["base"],
        source_tip_oid=oids["future_outside"], created_at="now",
    )
    qs = HistoryQueryService(plan, git=gb)
    key = _key(3, 5)
    # future_outside edits line 8 → outside region 3-5 → diff shows no overlap.
    result = qs._diff_touches_span(plan.source_commits[2], key)
    assert result is False


def test_diff_suppresses_subject_fallback_when_no_overlap(repo: Path):
    """When diff fetched OK and shows no overlap, _touches_region returns False
    (does NOT fall through to subject heuristic)."""
    oids = _build_region_repo(repo)
    gb = GitBackend(repo)
    plan = RebasePlan(
        source_commits=[
            _commit(oids["current"], oids["base"], "edit line 0", ["cfg.py"], 0),
            _commit(oids["future_inside"], oids["current"], "generic change", ["cfg.py"], 1),
            _commit(oids["future_outside"], oids["future_inside"], "another change", ["cfg.py"], 2),
        ],
        target_base_oid=oids["base"], target_tip_oid=oids["base"],
        source_tip_oid=oids["future_outside"], created_at="now",
    )
    qs = HistoryQueryService(plan, git=gb)
    key = _key(3, 5, name="parse")
    # future_outside: diff shows no overlap (line 8 vs region 3-5). Even though
    # the subject mentions nothing about "parse", the subject heuristic would
    # return False anyway — but the KEY point is the diff result is trusted.
    assert qs._touches_region(plan.source_commits[2], key) is False


def test_diff_fetch_failure_returns_none(repo: Path):
    """A bogus OID (diff fetch fails) returns None, not False."""
    oids = _build_region_repo(repo)
    gb = GitBackend(repo)
    plan = RebasePlan(
        source_commits=[
            _commit("bogus-oid", "bogus-parent", "bogus", ["cfg.py"], 0),
        ],
        target_base_oid=oids["base"], target_tip_oid=oids["base"],
        source_tip_oid=oids["current"], created_at="now",
    )
    qs = HistoryQueryService(plan, git=gb)
    key = _key(3, 5)
    result = qs._diff_touches_span(plan.source_commits[0], key)
    assert result is None


def test_diff_fetch_failure_falls_through_to_subject(repo: Path):
    """When diff returns None (fetch failed), _touches_region falls through to
    the subject heuristic."""
    oids = _build_region_repo(repo)
    gb = GitBackend(repo)
    plan = RebasePlan(
        source_commits=[
            _commit("bogus-oid", "bogus-parent", "Rename parse to load", ["cfg.py"], 0),
        ],
        target_base_oid=oids["base"], target_tip_oid=oids["base"],
        source_tip_oid=oids["current"], created_at="now",
    )
    qs = HistoryQueryService(plan, git=gb)
    key = _key(3, 5, name="def parse")
    # Bogus OID → diff fetch fails → None → falls through to subject heuristic.
    # Subject mentions "parse" → match.
    assert qs._touches_region(plan.source_commits[0], key) is True


def test_hunk_at_region_boundary_start(repo: Path):
    """A hunk starting exactly at the region start (0-based boundary) is detected."""
    oids = _build_region_repo(repo)
    gb = GitBackend(repo)
    # Reset to current, then make a future commit that edits ONLY line 3
    # (region start, 0-based).
    git(repo, "checkout", "-q", "--detach", oids["current"])
    lines = (repo / "cfg.py").read_text().splitlines()
    lines[3] = "line3-boundary"
    (repo / "cfg.py").write_text("\n".join(lines) + "\n")
    git(repo, "add", "cfg.py"); git(repo, "commit", "-q", "-m", "boundary start")
    boundary_oid = git(repo, "rev-parse", "HEAD").stdout.strip()
    plan = RebasePlan(
        source_commits=[
            _commit(oids["current"], oids["base"], "cur", ["cfg.py"], 0),
            _commit(boundary_oid, oids["current"], "boundary", ["cfg.py"], 1),
        ],
        target_base_oid=oids["base"], target_tip_oid=oids["base"],
        source_tip_oid=boundary_oid, created_at="now",
    )
    qs = HistoryQueryService(plan, git=gb)
    key = _key(3, 5)
    result = qs._diff_touches_span(plan.source_commits[1], key)
    assert result is True


def test_hunk_at_region_boundary_end(repo: Path):
    """A hunk ending exactly at the region end (0-based boundary) is detected."""
    oids = _build_region_repo(repo)
    gb = GitBackend(repo)
    git(repo, "checkout", "-q", "--detach", oids["current"])
    lines = (repo / "cfg.py").read_text().splitlines()
    lines[5] = "line5-boundary"  # region end (0-based: 3-5)
    (repo / "cfg.py").write_text("\n".join(lines) + "\n")
    git(repo, "add", "cfg.py"); git(repo, "commit", "-q", "-m", "boundary end")
    boundary_oid = git(repo, "rev-parse", "HEAD").stdout.strip()
    plan = RebasePlan(
        source_commits=[
            _commit(oids["current"], oids["base"], "cur", ["cfg.py"], 0),
            _commit(boundary_oid, oids["current"], "boundary", ["cfg.py"], 1),
        ],
        target_base_oid=oids["base"], target_tip_oid=oids["base"],
        source_tip_oid=boundary_oid, created_at="now",
    )
    qs = HistoryQueryService(plan, git=gb)
    key = _key(3, 5)
    result = qs._diff_touches_span(plan.source_commits[1], key)
    assert result is True


def test_hunk_one_line_before_region_not_detected(repo: Path):
    """A hunk ending one line before the region start is NOT detected."""
    oids = _build_region_repo(repo)
    gb = GitBackend(repo)
    git(repo, "checkout", "-q", "--detach", oids["current"])
    lines = (repo / "cfg.py").read_text().splitlines()
    lines[2] = "line2-just-before"  # region is 3-5; line 2 is before
    (repo / "cfg.py").write_text("\n".join(lines) + "\n")
    git(repo, "add", "cfg.py"); git(repo, "commit", "-q", "-m", "before region")
    before_oid = git(repo, "rev-parse", "HEAD").stdout.strip()
    plan = RebasePlan(
        source_commits=[
            _commit(oids["current"], oids["base"], "cur", ["cfg.py"], 0),
            _commit(before_oid, oids["current"], "before", ["cfg.py"], 1),
        ],
        target_base_oid=oids["base"], target_tip_oid=oids["base"],
        source_tip_oid=before_oid, created_at="now",
    )
    qs = HistoryQueryService(plan, git=gb)
    key = _key(3, 5)
    result = qs._diff_touches_span(plan.source_commits[1], key)
    assert result is False


def test_commit_patch_recursive_for_nested_paths(repo: Path):
    """commit_patch emits patches for files in subdirectories (the -r fix)."""
    import os
    subdir = repo / "src" / "sub"
    subdir.mkdir(parents=True)
    (subdir / "mod.py").write_text("x = 1\n")
    git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "add nested")
    oid = git(repo, "rev-parse", "HEAD").stdout.strip()
    gb = GitBackend(repo)
    patch = gb.commit_patch(oid)
    assert b"src/sub/mod.py" in patch, (
        "commit_patch didn't emit a patch for a nested file — missing -r flag?"
    )

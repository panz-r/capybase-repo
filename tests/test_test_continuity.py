"""Tests for the test-continuity invariant (survey §2.1a).

A pre-rebase passing-test baseline is captured, then diffed against the
post-merge passing set: a baseline-passing test that now fails is a behavioral
regression the merge introduced — a high-signal counterexample the syntactic/
intent validators can't catch (a merge can preserve structure + intent-units yet
still break behavior).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from capybase.config import Config
from capybase.orchestrator import Orchestrator
from capybase.test_output import parse_passing_node_ids

from tests.conftest import git

# Use the running interpreter's pytest (the venv that has it installed). Bare
# ``pytest`` isn't on PATH in every environment; ``<python> -m pytest`` is.
_PYTEST = f"{sys.executable} -m pytest -v"


def _orch(repo: Path, *, enable_continuity: bool = True) -> Orchestrator:
    cfg = Config()
    # A real pytest suite, run with -v so per-test node-IDs are parseable.
    cfg.tests.pre_continue = _PYTEST
    cfg.tests.final = _PYTEST
    cfg.tests.required = True
    cfg.tests.enable_test_continuity = enable_continuity
    return Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)


def _repo_with_passing_test(repo: Path) -> None:
    """A repo with ONE passing pytest test in tests/test_app.py."""
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "__init__.py").write_text("")
    (repo / "tests" / "test_app.py").write_text(
        "def test_greet():\n    assert True\n"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "add passing test")


# ---------------------------------------------------------------------------
# parse_passing_node_ids is covered in test_output_parser.py; here we exercise
# the orchestrator-level baseline capture + regression diff.
# ---------------------------------------------------------------------------


def test_baseline_capture_records_passing_tests(repo):
    """_capture_test_continuity_baseline runs the suite pre-rebase and stashes
    the passing node-ID set."""
    _repo_with_passing_test(repo)
    orch = _orch(repo)
    orch._capture_test_continuity_baseline()
    assert orch._test_continuity_baseline is not None
    assert any("test_greet" in n for n in orch._test_continuity_baseline), (
        orch._test_continuity_baseline
    )


def test_continuity_detects_regression(repo):
    """A baseline-passing test that no longer passes post-merge is a regression."""
    _repo_with_passing_test(repo)
    orch = _orch(repo)
    orch._capture_test_continuity_baseline()
    assert orch._test_continuity_baseline  # baseline captured
    # Simulate a post-merge run where the baseline test now FAILS (not in the
    # post-merge passing set). The diff must surface it as a regression.
    postmerge_out = (
        "tests/test_app.py::test_greet FAILED\n"
        "========================= 1 failed in 0.1s =========================\n"
    )
    regressed = orch._test_continuity_regressions(postmerge_out, "pytest -v")
    assert any("test_greet" in r for r in regressed), regressed


def test_continuity_no_regression_when_still_passing(repo):
    """A baseline-passing test that STILL passes post-merge → no regression."""
    _repo_with_passing_test(repo)
    orch = _orch(repo)
    orch._capture_test_continuity_baseline()
    postmerge_out = (
        "tests/test_app.py::test_greet PASSED\n"
        "========================= 1 passed in 0.1s =========================\n"
    )
    assert orch._test_continuity_regressions(postmerge_out, "pytest -v") == []


def test_continuity_preexisting_failure_not_a_regression(repo):
    """A test that FAILED pre-rebase is not in the baseline, so failing again
    post-merge is NOT flagged as a regression (it's pre-existing)."""
    # A repo with one failing test pre-rebase.
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "__init__.py").write_text("")
    (repo / "tests" / "test_app.py").write_text(
        "def test_broken():\n    assert False\n"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "add failing test")
    orch = _orch(repo)
    orch._capture_test_continuity_baseline()
    # The baseline only contains PASSING tests → test_broken is not in it (or
    # the baseline is empty/None when nothing passed — both mean inert here).
    baseline = orch._test_continuity_baseline or set()
    assert all("test_broken" not in n for n in baseline)
    # Failing again post-merge → not a regression (wasn't passing before).
    postmerge_out = "tests/test_app.py::test_broken FAILED\n"
    assert orch._test_continuity_regressions(postmerge_out, "pytest -v") == []


def test_continuity_inert_when_disabled(repo):
    """enable_test_continuity=False → no baseline captured, no diff."""
    _repo_with_passing_test(repo)
    orch = _orch(repo, enable_continuity=False)
    orch._capture_test_continuity_baseline()
    assert orch._test_continuity_baseline is None
    # No baseline → diff is always empty (inert).
    assert orch._test_continuity_regressions("anything", "pytest -v") == []


def test_continuity_inert_when_no_tests(repo):
    """An empty repo with no test files → the suite collects nothing → no
    baseline → the invariant is inert (no false positives)."""
    orch = _orch(repo)
    # No tests directory, no tests collected → empty passing set → baseline None.
    # (pytest rc=5 "no tests collected"; parse_passing_node_ids returns empty.)
    orch._capture_test_continuity_baseline()
    # Either None (no passing tests parsed) or empty — both mean inert.
    assert not orch._test_continuity_baseline

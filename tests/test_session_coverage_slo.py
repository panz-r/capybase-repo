"""Tests for the session-level coverage SLO (Phase 4 / survey §3.3).

Aggregates per-unit intent preservation coverage across the whole rebase window
into one ratio, surfaced at completion as observability for detecting
regressions across orchestrator changes. Advisory only — never blocks.
"""

from __future__ import annotations

from pathlib import Path

from capybase.config import Config
from capybase.orchestrator import Orchestrator, StepResult
from capybase.session import SessionPaths

from tests.conftest import git


def _orch(repo: Path, *, slo: float = 0.0, messages: list | None = None) -> Orchestrator:
    cfg = Config()
    cfg.validation.session_coverage_slo = slo
    if messages is None:
        messages = []
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *a, **k: messages.append(a[0]) if a else None)
    orch.paths = SessionPaths("t", repo_root=repo)
    orch.paths.root.mkdir(parents=True, exist_ok=True)
    return orch


def _bootstrap_repo(tmp_path: Path) -> Path:
    repo = tmp_path
    git(repo, "init", "-q", "-b", "main")
    git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    return repo


def test_rollup_aggregates_preserved_and_total(tmp_path):
    """The rollup sums per-unit (preserved, total) across all samples into one
    ratio — the window-level preservation metric."""
    repo = _bootstrap_repo(tmp_path)
    orch = _orch(repo)
    orch._session_coverage_samples = [
        ("a.py", 3, 4),  # 3/4 preserved
        ("b.py", 5, 6),  # 5/6 preserved
    ]
    rollup = orch._session_coverage_rollup()
    assert rollup is not None
    ratio, preserved, total = rollup
    assert preserved == 8
    assert total == 10
    assert ratio == 0.8


def test_rollup_none_when_no_samples(tmp_path):
    """A clean rebase with no measured units → None (nothing to report)."""
    repo = _bootstrap_repo(tmp_path)
    orch = _orch(repo)
    orch._session_coverage_samples = []
    assert orch._session_coverage_rollup() is None


def test_rollup_none_when_total_zero(tmp_path):
    """Samples with zero total (units that added nothing) don't crash; rollup is
    None rather than a division by zero."""
    repo = _bootstrap_repo(tmp_path)
    orch = _orch(repo)
    orch._session_coverage_samples = [("a.py", 0, 0)]
    assert orch._session_coverage_rollup() is None


def test_report_surfaces_ratio_at_completion(tmp_path):
    """The completion report emits a session-coverage line with the aggregate ratio."""
    repo = _bootstrap_repo(tmp_path)
    messages: list[str] = []
    orch = _orch(repo, messages=messages)
    orch._session_coverage_samples = [("a.py", 9, 10)]
    orch._report_session_coverage_slo()
    joined = "".join(messages)
    assert "session intent coverage" in joined
    assert "90.0%" in joined
    assert "9/10" in joined


def test_report_emits_advisory_when_below_slo(tmp_path):
    """When the ratio is below the configured SLO, an advisory warning is emitted
    (still advisory only — does not block)."""
    repo = _bootstrap_repo(tmp_path)
    messages: list[str] = []
    orch = _orch(repo, slo=0.95, messages=messages)
    orch._session_coverage_samples = [("a.py", 8, 10)]  # 80% < 95% SLO
    orch._report_session_coverage_slo()
    joined = "".join(messages)
    assert "below the configured SLO" in joined


def test_report_no_advisory_when_above_slo(tmp_path):
    """When the ratio meets/exceeds the SLO, no advisory warning."""
    repo = _bootstrap_repo(tmp_path)
    messages: list[str] = []
    orch = _orch(repo, slo=0.80, messages=messages)
    orch._session_coverage_samples = [("a.py", 9, 10)]  # 90% >= 80% SLO
    orch._report_session_coverage_slo()
    joined = "".join(messages)
    assert "below the configured SLO" not in joined


def test_report_noop_when_disabled_or_no_data(tmp_path):
    """With no samples (clean rebase), the report is a silent no-op (no crash)."""
    repo = _bootstrap_repo(tmp_path)
    messages: list[str] = []
    orch = _orch(repo, slo=0.95, messages=messages)
    orch._session_coverage_samples = []
    orch._report_session_coverage_slo()
    assert messages == []


def test_accumulate_extracts_coverage_from_validation(tmp_path):
    """_accumulate_coverage_samples reads the intent_coverage detail from each
    accepted unit's validation and records (path, preserved, total)."""
    from capybase.conflict_model import (
        CandidateResolution, ConflictSide, ConflictUnit,
        VerificationResult, VerificationWarning,
    )
    from capybase.orchestrator import UnitOutcome

    repo = _bootstrap_repo(tmp_path)
    orch = _orch(repo)
    unit = ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=""),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=""),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=""),
        original_worktree_text="", marker_span=(1, 5),
    )
    validation = VerificationResult(
        candidate_id="c", unit_id="u", passed=True,
        warnings=[VerificationWarning(
            validator="intent_coverage",
            message="coverage above floor",
            detail={"current_preserved": 2, "current_total": 2,
                    "replayed_preserved": 1, "replayed_total": 3},
        )],
    )
    result = StepResult(step_index=1)
    result.outcomes = [UnitOutcome(
        unit=unit,
        accepted=CandidateResolution(
            candidate_id="c", unit_id="u", model_name="m", prompt_version="v",
            resolved_text="x",
        ),
        validation=validation,
    )]
    orch._accumulate_coverage_samples(result)
    assert orch._session_coverage_samples == [("app.py", 3, 5)]  # 2+1 preserved, 2+3 total


def test_accumulate_skips_unaccepted_units(tmp_path):
    """Escalated units (no accepted candidate) don't contribute to the SLO."""
    from capybase.conflict_model import ConflictSide, ConflictUnit
    from capybase.orchestrator import UnitOutcome

    repo = _bootstrap_repo(tmp_path)
    orch = _orch(repo)
    unit = ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=""),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=""),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=""),
        original_worktree_text="", marker_span=(1, 5),
    )
    result = StepResult(step_index=1)
    result.outcomes = [UnitOutcome(unit=unit, accepted=None)]  # escalated
    orch._accumulate_coverage_samples(result)
    assert orch._session_coverage_samples == []

"""Tests for semantic post-merge accept reports (#4).

Two layers:
- :func:`build_accept_report` (pure): composes per-unit obligations/validation/
  classification with the step-level test verdict into a markdown "why we
  accepted" summary.
- The orchestrator wiring: a clean step writes ``final/accept-report.md`` with
  a section per accepted unit; an escalation step writes nothing.

The report reuses #3's obligations (preserved edits), #2's classification band,
and the verification result (markers/syntax) — it composes, never recomputes.
"""

from __future__ import annotations

import json

from capybase.accept_report import build_accept_report
from capybase.classifier import ConflictClassification
from capybase.conflict_model import (
    CandidateResolution,
    ConflictSide,
    ConflictUnit,
    VerificationResult,
)
from capybase.orchestrator import UnitOutcome


def _unit(base="a = 1", current="a = 1\nb = 2", replayed="a = 1\nc = 3") -> ConflictUnit:
    return ConflictUnit(
        session_id="s", step_index=1, path="cfg.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text=base, marker_span=(0, 0),
    )


def _cand(resolved: str, *, prompt_version="resolve_text_block.v5",
          model_name="fake") -> CandidateResolution:
    return CandidateResolution(
        candidate_id="u:c", unit_id="u", model_name=model_name,
        prompt_version=prompt_version, resolved_text=resolved,
    )


def _outcome(unit, cand, *, validation=None, classification=None) -> UnitOutcome:
    o = UnitOutcome(unit=unit)
    o.accepted = cand
    o.validation = validation
    o.classification = classification
    return o


# ---------------------------------------------------------------------------
# build_accept_report (pure)
# ---------------------------------------------------------------------------


def test_report_lists_preserved_obligations_and_test_verdict():
    """The report names each side's preserved edits and the test verdict."""
    unit = _unit()
    cand = _cand("a = 1\nb = 2\nc = 3")
    validation = VerificationResult(
        candidate_id="u:c", unit_id="u", passed=True,
        features={"markers_remaining": 0, "syntax_passed": True},
    )
    report = build_accept_report([_outcome(unit, cand, validation=validation)],
                                 tests_passed=True, test_verdict="3 passed")
    assert "cfg.py" in report
    assert "preserved CURRENT" in report and "b = 2" in report
    assert "preserved REPLAYED" in report and "c = 3" in report
    assert "no conflict markers" in report
    assert "syntax passed" in report
    assert "tests: passed" in report and "3 passed" in report


def test_report_includes_classification_band_when_present():
    """The classification band (#2) appears when routing ran."""
    unit = _unit()
    cand = _cand("a = 1\nb = 2\nc = 3", prompt_version="resolve_text_block.v5")
    cls = ConflictClassification(
        difficulty="simple", band="trivial",
        reasons=["disjoint edits"], features={},
    )
    report = build_accept_report([_outcome(unit, cand, classification=cls)],
                                 tests_passed=None)
    assert "difficulty: trivial" in report


def test_report_omits_classification_when_absent():
    """A non-LLM accept (structural/etc.) has no classification → no band line."""
    unit = _unit()
    cand = _cand("a = 1\nb = 2\nc = 3", prompt_version="structural.insertion_union")
    report = build_accept_report([_outcome(unit, cand)], tests_passed=None)
    assert "difficulty:" not in report
    # But the via-label records the deterministic path.
    assert "deterministic (insertion_union)" in report


def test_report_empty_when_no_accepted_units():
    """An escalation step (no accepts) yields an empty report body."""
    unit = _unit()
    o = UnitOutcome(unit=unit)  # accepted is None
    assert build_accept_report([o], tests_passed=False) == ""


def test_report_test_verdict_none_means_skipped():
    report = build_accept_report(
        [_outcome(_unit(), _cand("a = 1\nb = 2\nc = 3"))],
        tests_passed=None,
    )
    assert "tests: skipped" in report


def test_report_marks_failed_syntax_and_tests():
    """A failed syntax check and failed tests are surfaced, not hidden."""
    unit = _unit()
    cand = _cand("a = 1\nb = 2\nc = 3")
    validation = VerificationResult(
        candidate_id="u:c", unit_id="u", passed=False,
        features={"markers_remaining": 0, "syntax_passed": False},
    )
    report = build_accept_report([_outcome(unit, cand, validation=validation)],
                                 tests_passed=False, test_verdict="1 failed")
    assert "syntax failed" in report
    assert "tests: FAILED" in report


# ---------------------------------------------------------------------------
# Orchestrator wiring: the report file is written on a clean step
# ---------------------------------------------------------------------------


def test_clean_step_writes_accept_report_file(conflicted_repo):
    """A rebase that resolves cleanly writes final/accept-report.md naming the
    resolved unit and the test verdict."""
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator
    from capybase.resolution_engine import ResolutionEngine

    repo = conflicted_repo["repo"]
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"  # always passes
    cfg.tests.final = "true"
    payload = json.dumps({"resolved_text": "    return 'hi' + 'howdy'"})
    engine = ResolutionEngine(cfg.model, client=__import__(
        "tests.test_orchestrator", fromlist=["CyclingClient"]).CyclingClient([payload]))
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert not result.escalated, result.reason
    report = orch.paths.final / "accept-report.md"
    assert report.exists(), "no accept-report.md written on a clean step"
    text = report.read_text()
    assert "app.py" in text  # the resolved unit
    assert "tests:" in text


def test_escalation_does_not_write_accept_report(conflicted_repo):
    """A step that escalates (no accepted candidate) writes no report section."""
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator
    from capybase.resolution_engine import ResolutionEngine

    repo = conflicted_repo["repo"]
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    # A client that returns leaked markers → forces escalation.
    bad = json.dumps({"resolved_text": "    x\n<<<<<<< still\n"})
    engine = ResolutionEngine(cfg.model, client=__import__(
        "tests.test_orchestrator", fromlist=["CyclingClient"]).CyclingClient([bad]))
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    orch.run()
    report = orch.paths.final / "accept-report.md"
    # No accepted unit → no accept report written (the escalation review bundle
    # is a separate file).
    assert not report.exists()

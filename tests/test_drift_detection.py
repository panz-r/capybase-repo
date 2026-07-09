"""Session-level drift detection (behavioral-regression, mechanism-gated).

The first-gen detector embedded a prose anchor and cosine-compared it to merged
code. An external review (see ``docs/drift-detector-review.md``) established
that cross-modal comparison has no operating point — the distance floor for a
correct merge overlaps the drifted-merge distribution — so the embedding
monitor was scrapped. The replacement follows the review's three immediate
actions:

1. **Gate on resolution mechanism** — deterministic resolutions (exact reuse,
   structural union, brace repair) emit NO drift signal; drift is impossible by
   construction. Only LLM resolutions can fire.
2. **No embeddings, no anchor, no threshold** — there is nothing to calibrate.
3. **Test regression is the primary signal** — a baseline-passing test that now
   fails is a high-confidence drift indicator (0% FPR per the SAM literature).

These tests cover the mechanism gate, the regression signal, the coverage-note
surfacing, the accumulator semantics, and the summary rendering. No embedder is
needed (the monitor takes none).
"""

from __future__ import annotations

from capybase.drift import DriftMonitor, DriftReport


# ---------------------------------------------------------------------------
# is_drift: the mechanism gate (review immediate action #1)
# ---------------------------------------------------------------------------


def test_deterministic_step_never_drifts_even_with_regressions():
    """The headline fix: a deterministic exact-reuse step that shows a test
    regression does NOT fire drift — the resolution is a verbatim replay of a
    validated state, so the regression was pre-existing, not model-induced.
    This eliminates the false positives the review flagged on py_simple,
    py_multi_unit, and rust_port_test (all deterministic exact-reuse).
    """
    report = DriftReport(
        commit_index=1, mechanism="deterministic",
        regressed_tests=("tests/test_x.py::test_foo",),
        coverage_note="test coverage for modified files: 5 baseline test(s) active",
    )
    assert report.is_drift is False


def test_llm_step_with_regression_is_drift():
    report = DriftReport(
        commit_index=2, mechanism="llm",
        regressed_tests=("tests/test_x.py::test_foo",),
        coverage_note="test coverage for modified files: 5 baseline test(s) active",
    )
    assert report.is_drift is True


def test_mixed_step_with_regression_is_drift():
    """A step with both LLM and deterministic resolutions still can drift —
    the LLM resolution is the drift-risk source."""
    report = DriftReport(
        commit_index=3, mechanism="mixed",
        regressed_tests=("tests/test_x.py::test_bar",),
        coverage_note="test coverage for modified files: 3 baseline test(s) active",
    )
    assert report.is_drift is True


def test_llm_step_with_no_regression_is_not_drift():
    """The healthy case: an LLM resolution that introduces no test regression
    is not drift — the behavioral signal (the 0%-FPR check) found nothing."""
    report = DriftReport(
        commit_index=4, mechanism="llm",
        regressed_tests=(),
        coverage_note="test coverage for modified files: 5 baseline test(s) active",
    )
    assert report.is_drift is False


def test_deterministic_step_with_no_regression_is_not_drift():
    report = DriftReport(
        commit_index=5, mechanism="deterministic",
        regressed_tests=(),
        coverage_note="test coverage for modified files: 5 baseline test(s) active",
    )
    assert report.is_drift is False


def test_deterministic_step_with_no_baseline_is_not_drift():
    """Even without a test baseline (signal inactive), a deterministic step
    cannot drift — the mechanism gate is independent of the signal."""
    report = DriftReport(
        commit_index=6, mechanism="deterministic",
        regressed_tests=(),
        coverage_note="no test baseline captured — behavioral drift signal inactive",
    )
    assert report.is_drift is False


# ---------------------------------------------------------------------------
# DriftMonitor.observe: accumulator semantics
# ---------------------------------------------------------------------------


def test_observe_returns_report_with_passed_values():
    monitor = DriftMonitor()
    report = monitor.observe(
        commit_index=1, mechanism="llm",
        regressed_tests=["tests/test_a.py::test_one"],
        coverage_note="5 baseline test(s) active",
    )
    assert report is not None
    assert report.commit_index == 1
    assert report.mechanism == "llm"
    assert report.regressed_tests == ("tests/test_a.py::test_one",)
    assert "5 baseline" in report.coverage_note


def test_observe_appends_to_history():
    monitor = DriftMonitor()
    monitor.observe(commit_index=1, mechanism="deterministic",
                    regressed_tests=[], coverage_note="ok")
    monitor.observe(commit_index=2, mechanism="llm",
                    regressed_tests=["t1"], coverage_note="ok")
    assert len(monitor.history) == 2
    assert monitor.history[0].commit_index == 1
    assert monitor.history[1].commit_index == 2


def test_observe_normalizes_list_to_tuple():
    """observe accepts a list (what the orchestrator passes) and stores a
    tuple (DriftReport is frozen)."""
    monitor = DriftMonitor()
    report = monitor.observe(
        commit_index=1, mechanism="llm",
        regressed_tests=["a", "b"], coverage_note="ok",
    )
    assert isinstance(report.regressed_tests, tuple)
    assert report.regressed_tests == ("a", "b")


def test_observe_defaults_coverage_note_when_empty():
    """An empty coverage_note falls back to the no-baseline message so the
    report always explains why the signal was/wasn't active."""
    monitor = DriftMonitor()
    report = monitor.observe(
        commit_index=1, mechanism="llm", regressed_tests=[], coverage_note="",
    )
    assert "no test baseline" in report.coverage_note


def test_total_regressions_excludes_deterministic_steps():
    """A regression observed after a deterministic replay was pre-existing
    (not caused by the replay), so it does not count toward LLM-induced drift."""
    monitor = DriftMonitor()
    monitor.observe(commit_index=1, mechanism="deterministic",
                    regressed_tests=["pre_existing"], coverage_note="ok")
    monitor.observe(commit_index=2, mechanism="llm",
                    regressed_tests=["model_caused"], coverage_note="ok")
    assert monitor.total_regressions == 1


def test_total_regressions_sums_across_llm_steps():
    monitor = DriftMonitor()
    monitor.observe(commit_index=1, mechanism="llm",
                    regressed_tests=["a", "b"], coverage_note="ok")
    monitor.observe(commit_index=2, mechanism="mixed",
                    regressed_tests=["c"], coverage_note="ok")
    assert monitor.total_regressions == 3


def test_drift_steps_filters_to_actual_drift():
    monitor = DriftMonitor()
    monitor.observe(commit_index=1, mechanism="deterministic",
                    regressed_tests=["x"], coverage_note="ok")  # not drift
    monitor.observe(commit_index=2, mechanism="llm",
                    regressed_tests=[], coverage_note="ok")      # not drift
    monitor.observe(commit_index=3, mechanism="llm",
                    regressed_tests=["y"], coverage_note="ok")  # drift
    drift = monitor.drift_steps
    assert len(drift) == 1
    assert drift[0].commit_index == 3


# ---------------------------------------------------------------------------
# DriftReport.render: the advisory text
# ---------------------------------------------------------------------------


def test_render_drift_lists_regressions_and_mechanism():
    report = DriftReport(
        commit_index=7, mechanism="llm",
        regressed_tests=("tests/test_x.py::test_foo", "tests/test_x.py::test_bar"),
        coverage_note="5 baseline test(s) active",
    )
    text = report.render()
    assert "behavioral drift @ commit 7" in text
    assert "2 regression(s)" in text
    assert "llm" in text
    assert "test_foo" in text


def test_render_drift_truncates_long_regression_list():
    many = tuple(f"tests/test_x.py::test_{i}" for i in range(10))
    report = DriftReport(
        commit_index=1, mechanism="llm",
        regressed_tests=many, coverage_note="ok",
    )
    text = report.render()
    assert "..." in text
    assert "10 regression(s)" in text
    assert "test_4" in text
    assert "test_9" not in text  # only first 5 shown


def test_render_no_drift_deterministic_explains_gate():
    report = DriftReport(
        commit_index=1, mechanism="deterministic",
        regressed_tests=("t1",), coverage_note="5 baseline test(s) active",
    )
    text = report.render()
    assert "no drift" in text
    assert "deterministic" in text
    assert "impossible by construction" in text


def test_render_no_drift_llm_no_regression_healthy():
    report = DriftReport(
        commit_index=1, mechanism="llm",
        regressed_tests=(), coverage_note="5 baseline test(s) active",
    )
    text = report.render()
    assert "no drift" in text
    assert "0 regressions" in text


# ---------------------------------------------------------------------------
# DriftMonitor.summary: the post-session headline
# ---------------------------------------------------------------------------


def test_summary_empty_when_no_observations():
    assert DriftMonitor().summary() == ""


def test_summary_no_drift_clean_session():
    monitor = DriftMonitor()
    monitor.observe(commit_index=1, mechanism="llm",
                    regressed_tests=[], coverage_note="ok")
    monitor.observe(commit_index=2, mechanism="deterministic",
                    regressed_tests=[], coverage_note="ok")
    s = monitor.summary()
    assert "2-commit window" in s
    assert "0 regression" in s
    assert "no drift" in s


def test_summary_reports_pre_existing_deterministic_regressions():
    """A session where the only regressions appeared under deterministic replays
    — those are pre-existing, not model-induced. The summary distinguishes them
    so the operator knows there was no actionable drift."""
    monitor = DriftMonitor()
    monitor.observe(commit_index=1, mechanism="deterministic",
                    regressed_tests=["pre_existing"], coverage_note="ok")
    s = monitor.summary()
    assert "0 LLM-induced regression" in s
    assert "1 pre-existing under deterministic" in s
    assert "no model-induced drift" in s


def test_summary_reports_llm_induced_drift():
    monitor = DriftMonitor()
    monitor.observe(commit_index=1, mechanism="deterministic",
                    regressed_tests=["pre"], coverage_note="ok")
    monitor.observe(commit_index=2, mechanism="llm",
                    regressed_tests=["model_caused1", "model_caused2"],
                    coverage_note="ok")
    monitor.observe(commit_index=3, mechanism="mixed",
                    regressed_tests=["model_caused3"], coverage_note="ok")
    s = monitor.summary()
    assert "3-commit window" in s
    assert "3 LLM-induced regression" in s
    assert "2 step" in s  # commit 2 + 3 both drifted

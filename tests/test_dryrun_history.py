"""Tests for the history-aware dry-run report (#9 step 10).

The dry-run report now breaks resolutions down by mechanism, surfaces future
probes + conflict chains, and emits a rule-based recommended action. Falls back
to the terse summary when no history plan was active.
"""

from __future__ import annotations

from capybase.dryrun import RehearsalReport, RehearsalStep, _summarize_journal, _via_to_provenance


def _report(*, history_active=True, would_succeed=True) -> RehearsalReport:
    return RehearsalReport(
        would_succeed=would_succeed, target="main",
        head_before="aaa", head_after="bbb", session_id="s",
        history_active=history_active,
    )


# ---------------------------------------------------------------------------
# summary_history
# ---------------------------------------------------------------------------


def test_summary_history_falls_back_when_no_history():
    """No history plan → the terse summary (history can't add value)."""
    report = _report(history_active=False)
    report.steps = [RehearsalStep(step=1, accepted=True, files=["cfg.py"])]
    out = report.summary_history()
    assert "DRY RUN" in out  # the terse header


def test_summary_history_lists_commit_and_conflict_counts():
    report = _report()
    report.steps = [
        RehearsalStep(step=1, accepted=True, files=["cfg.py"]),
        RehearsalStep(step=2, files=["util.py"]),
        RehearsalStep(step=3),  # no conflicts
    ]
    out = report.summary_history()
    assert "3 commit(s) replayed" in out
    assert "2 conflict(s) encountered" in out


def test_summary_history_breaks_down_by_mechanism():
    report = _report()
    report.steps = [
        RehearsalStep(step=1, accepted=True, mechanisms=["deterministic_structural"]),
        RehearsalStep(step=2, accepted=True, mechanisms=["history_augmented_llm"]),
    ]
    report.mechanism_counts = {"deterministic_structural": 1, "history_augmented_llm": 1}
    out = report.summary_history()
    assert "deterministic" in out.lower()
    assert "history-augmented" in out.lower()


def test_summary_history_surfaces_future_probes():
    report = _report()
    report.steps = [
        RehearsalStep(step=1, accepted=True,
                      future_probes_passed=2, future_probes_failed=1),
    ]
    out = report.summary_history()
    assert "2 future probe(s) passed" in out
    assert "1 failed" in out


def test_summary_history_lists_conflict_chains():
    report = _report()
    report.conflict_chains = ["3 conflicts in cfg.py :: function > parse across commits 2, 4, 5"]
    out = report.summary_history()
    assert "conflict chain" in out
    assert "parse" in out


def test_summary_history_recommends_action_on_chain():
    report = _report(would_succeed=False)
    report.conflict_chains = ["2 conflicts in cfg.py :: function > parse"]
    out = report.summary_history()
    assert "recommended action" in out
    assert "squash" in out


def test_summary_history_no_action_when_clean():
    """A clean successful rebase with no chains → no recommended action."""
    report = _report(would_succeed=True)
    report.steps = [RehearsalStep(step=1, accepted=True)]
    out = report.summary_history()
    assert "recommended action" not in out


def test_summary_history_counts_escalations():
    report = _report(would_succeed=False)
    report.steps = [
        RehearsalStep(step=1, accepted=True),
        RehearsalStep(step=2, escalated=True, detail="boom"),
    ]
    out = report.summary_history()
    assert "1 escalated" in out


# ---------------------------------------------------------------------------
# _summarize_journal folds the new event types
# ---------------------------------------------------------------------------


def _journal_event(event_type, payload=None, step_index=1):
    import json
    return json.dumps({
        "event_type": event_type,
        "payload": payload or {},
        "step_index": step_index,
    })


def test_journal_records_mechanism_from_candidate_accepted(tmp_path):
    """candidate_accepted with provenance populates mechanism_counts."""
    report = _report()
    j = tmp_path / "j.jsonl"
    j.write_text("\n".join([
        _journal_event("step_started", {}, 1),
        _journal_event("conflict_detected", {"paths": ["cfg.py"]}, 1),
        _journal_event("candidate_accepted", {"provenance": "plain_llm"}, 1),
    ]) + "\n")
    _summarize_journal(j, report)
    assert report.mechanism_counts.get("plain_llm") == 1
    assert report.steps[0].mechanisms == ["plain_llm"]


def test_journal_maps_via_label_for_pre_llm_mechanisms(tmp_path):
    """A structural accept (no provenance, via='structural') maps to the enum."""
    report = _report()
    j = tmp_path / "j.jsonl"
    j.write_text("\n".join([
        _journal_event("step_started", {}, 1),
        _journal_event("candidate_accepted", {"via": "structural"}, 1),
    ]) + "\n")
    _summarize_journal(j, report)
    assert report.mechanism_counts.get("deterministic_structural") == 1


def test_journal_records_exact_reuse(tmp_path):
    """exact_reuse_applied counts toward the mechanism breakdown."""
    report = _report()
    j = tmp_path / "j.jsonl"
    j.write_text("\n".join([
        _journal_event("step_started", {}, 1),
        _journal_event("exact_reuse_applied", {"source": "cfg.py:prior"}, 1),
    ]) + "\n")
    _summarize_journal(j, report)
    assert report.mechanism_counts.get("exact_history_reuse") == 1
    assert report.steps[0].accepted is True


def test_journal_records_future_probe_results(tmp_path):
    """future_apply_probe events increment passed/failed counters."""
    report = _report()
    j = tmp_path / "j.jsonl"
    j.write_text("\n".join([
        _journal_event("step_started", {}, 1),
        _journal_event("future_apply_probe", {"probed": True, "applies": True}, 1),
        _journal_event("future_apply_probe", {"probed": True, "applies": False}, 1),
    ]) + "\n")
    _summarize_journal(j, report)
    assert report.steps[0].future_probes_passed == 1
    assert report.steps[0].future_probes_failed == 1


def test_via_to_provenance_mapping():
    assert _via_to_provenance("structural") == "deterministic_structural"
    assert _via_to_provenance("sbcr") == "combination_search"
    assert _via_to_provenance("block_capture") == "block_capture"
    assert _via_to_provenance("exact_reuse") == "exact_history_reuse"
    # Unknown via → returned as-is (visible, not dropped).
    assert _via_to_provenance("future_mech") == "future_mech"

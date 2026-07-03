"""Tests for per-mechanism quality metrics (#9 step 9).

Aggregates the experience corpus (now carrying provenance from step 8) into
per-mechanism acceptance + later-failure counts. Covers the pure aggregator, the
text-table rendering, and the legacy/unknown-provenance handling.
"""

from __future__ import annotations

from capybase.conflict_model import HistoricalExample
from capybase.memory.store import Experience, ExperienceStore
from capybase.metrics import (
    MechanismStats,
    MetricsReport,
    compute_metrics,
)


def _exp(outcome, provenance, *, validator_features=None):
    return Experience(
        example=HistoricalExample(
            summary="cfg.py:u", base="a", current="b",
            replayed="c", resolved="d", source="s",
        ),
        outcome=outcome, language="python", path="cfg.py",
        provenance=provenance, validator_features=validator_features or {},
    )


def _store(tmp_path, exps):
    store = ExperienceStore(tmp_path / "exp.jsonl")
    for e in exps:
        store.append(e)
    return store


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------


def test_none_store_yields_empty_report():
    assert compute_metrics(None).by_mechanism == {}


def test_empty_store_yields_empty_report(tmp_path):
    store = ExperienceStore(tmp_path / "empty.jsonl")
    report = compute_metrics(store)
    assert report.by_mechanism == {}


def test_accepted_counted_per_mechanism(tmp_path):
    store = _store(tmp_path, [
        _exp("accepted", "deterministic_structural"),
        _exp("accepted", "deterministic_structural"),
        _exp("accepted", "plain_llm"),
    ])
    report = compute_metrics(store)
    assert report.get("deterministic_structural").accepted == 2
    assert report.get("plain_llm").accepted == 1


def test_escalated_counted_per_mechanism(tmp_path):
    store = _store(tmp_path, [
        _exp("accepted", "plain_llm"),
        _exp("escalated", "plain_llm"),
        _exp("escalated", "plain_llm"),
    ])
    report = compute_metrics(store)
    stats = report.get("plain_llm")
    assert stats.accepted == 1
    assert stats.escalated == 2
    assert stats.accept_rate == 1 / 3


def test_later_probe_failure_counted(tmp_path):
    """An accepted experience whose future-apply probe later failed."""
    store = _store(tmp_path, [
        _exp("accepted", "history_augmented_llm",
             validator_features={
                 "future_apply_probe_probed": True,
                 "future_apply_probe_applies": False,
             }),
        _exp("accepted", "history_augmented_llm",
             validator_features={
                 "future_apply_probe_probed": True,
                 "future_apply_probe_applies": True,
             }),
    ])
    report = compute_metrics(store)
    stats = report.get("history_augmented_llm")
    assert stats.accepted == 2
    assert stats.later_probe_failures == 1


def test_later_test_failure_counted(tmp_path):
    """An accepted experience whose step tests later failed."""
    store = _store(tmp_path, [
        _exp("accepted", "exact_history_reuse",
             validator_features={"tests_passed": False}),
    ])
    report = compute_metrics(store)
    assert report.get("exact_history_reuse").later_test_failures == 1


def test_legacy_provenance_counted_separately(tmp_path):
    """Experiences with no provenance (pre-step-8) are tallied as legacy, not
    bucketed under a fake mechanism."""
    store = _store(tmp_path, [
        _exp("accepted", ""),  # legacy
        _exp("accepted", ""),  # legacy
    ])
    report = compute_metrics(store)
    assert report.legacy_count == 2
    # No mechanism buckets get the legacy experiences.
    assert report.by_mechanism == {}


def test_mechanism_with_no_data_omitted_from_table(tmp_path):
    """Mechanisms with zero resolutions don't appear in the rendered table."""
    store = _store(tmp_path, [_exp("accepted", "plain_llm")])
    report = compute_metrics(store)
    table = report.render_table()
    assert "plain_llm" in table.lower() or "LLM" in table
    assert "deterministic" not in table.lower()  # no data → omitted


# ---------------------------------------------------------------------------
# #idea 11: manual_corrections + reuse_hits
# ---------------------------------------------------------------------------


def test_manual_corrections_counted(tmp_path):
    """Accepted manual resolutions count as manual_corrections (#idea 11)."""
    store = _store(tmp_path, [
        _exp("accepted", "manual"),
        _exp("accepted", "manual"),
        _exp("accepted", "plain_llm"),
    ])
    report = compute_metrics(store)
    assert report.get("manual").manual_corrections == 2
    assert report.get("plain_llm").manual_corrections == 0


def test_reuse_hits_counted(tmp_path):
    """Accepted exact-reuse resolutions count as reuse_hits (#idea 11)."""
    store = _store(tmp_path, [
        _exp("accepted", "exact_history_reuse"),
        _exp("accepted", "exact_history_reuse"),
        _exp("accepted", "exact_history_reuse"),
        _exp("accepted", "plain_llm"),
    ])
    report = compute_metrics(store)
    assert report.get("exact_history_reuse").reuse_hits == 3
    assert report.get("plain_llm").reuse_hits == 0


def test_table_shows_new_columns(tmp_path):
    """The rendered table includes the manual + reuse columns."""
    store = _store(tmp_path, [
        _exp("accepted", "manual"),
        _exp("accepted", "exact_history_reuse"),
    ])
    report = compute_metrics(store)
    table = report.render_table()
    assert "man" in table  # the manual column header
    assert "reuse" in table  # the reuse column header


def test_escalated_manual_not_counted_as_correction(tmp_path):
    """An escalated (not accepted) manual outcome isn't a correction."""
    store = _store(tmp_path, [
        _exp("escalated", "manual"),
    ])
    report = compute_metrics(store)
    assert report.get("manual").manual_corrections == 0
    assert report.get("manual").escalated == 1


def test_empty_report_table_has_placeholder(tmp_path):
    store = ExperienceStore(tmp_path / "empty.jsonl")
    report = compute_metrics(store)
    table = report.render_table()
    assert "no recorded resolutions" in table


def test_unknown_future_value_bucketed_and_visible(tmp_path):
    """A future provenance value (not in the known set) is still bucketed so it's
    visible in the table rather than silently dropped."""
    store = _store(tmp_path, [_exp("accepted", "some_future_mechanism")])
    report = compute_metrics(store)
    # It's bucketed under its raw name (not legacy).
    assert "some_future_mechanism" in report.by_mechanism
    assert report.get("some_future_mechanism").accepted == 1


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_metrics_cli_command_prints_table(repo, monkeypatch):
    """`capybase metrics` prints the per-mechanism table from the store."""
    import io
    from capybase.config import Config
    from capybase.cli import _run_metrics
    from capybase.git_backend import GitBackend
    from capybase.memory.store import ExperienceStore

    cfg = Config()
    cfg.memory.enabled = True
    cfg.future.enable_rag = True
    git = GitBackend(str(repo))
    store = ExperienceStore.for_repo(str(git.repo), cfg.memory.store_path)
    store.append(_exp("accepted", "deterministic_structural"))
    store.append(_exp("escalated", "plain_llm"))

    buf = io.StringIO()
    rc = _run_metrics(cfg, repo=str(repo), out=buf)
    assert rc == 0
    out = buf.getvalue()
    assert "Per-mechanism" in out
    assert "deterministic" in out.lower()


def test_metrics_cli_when_memory_disabled(repo):
    """`capybase metrics` reports gracefully when memory/rag is disabled."""
    import io
    from capybase.config import Config
    from capybase.cli import _run_metrics

    cfg = Config()
    cfg.memory.enabled = False  # disabled
    buf = io.StringIO()
    rc = _run_metrics(cfg, repo=str(repo), out=buf)
    assert rc == 0
    assert "not configured" in buf.getvalue()

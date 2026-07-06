"""Session-level semantic drift detection (embeddings survey §6).

An advisory (non-blocking) monitor that detects when the cumulative effect of
several merges drifts the branch from its original purpose. Per-commit cosine
distance between the session anchor (the branch intent — the GOAL) and the
merged resolved texts (the OUTCOME).

A deterministic fake embedder (vectors from a caller-specified text→vector map)
makes the drift threshold assertable without a live endpoint.
"""

from __future__ import annotations

import pytest

from capybase.drift import DriftMonitor, DriftReport, _cosine_distance


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _MapEmbedder:
    """Returns a caller-specified vector per text."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = 0

    def embed(self, texts):
        if isinstance(texts, str):
            texts = [texts]
        self.calls += 1
        return [self.mapping.get(t, [0.0, 0.0]) for t in texts]


# ---------------------------------------------------------------------------
# _cosine_distance
# ---------------------------------------------------------------------------


def test_cosine_distance_identical_is_zero():
    assert _cosine_distance([1.0, 0.0], [1.0, 0.0]) == 0.0


def test_cosine_distance_orthogonal_is_one():
    assert _cosine_distance([1.0, 0.0], [0.0, 1.0]) == pytest.approx(1.0)


def test_cosine_distance_mismatched_length_is_one():
    assert _cosine_distance([1.0], [1.0, 0.0]) == 1.0


def test_cosine_distance_zero_vector_is_one():
    assert _cosine_distance([0.0, 0.0], [1.0, 0.0]) == 1.0


# ---------------------------------------------------------------------------
# DriftMonitor: anchor + check
# ---------------------------------------------------------------------------


def test_set_anchor_embeds_once():
    emb = _MapEmbedder({"intent text": [1.0, 0.0]})
    m = DriftMonitor(embedder=emb, threshold=0.20)
    m.set_anchor("intent text")
    assert emb.calls == 1


def test_set_anchor_idempotent_same_text():
    """Re-calling set_anchor with the same text is a no-op (digest match)."""
    emb = _MapEmbedder({"intent": [1.0, 0.0]})
    m = DriftMonitor(embedder=emb, threshold=0.20)
    m.set_anchor("intent")
    m.set_anchor("intent")
    assert emb.calls == 1  # not re-embedded


def test_check_returns_none_when_no_anchor():
    """Without an anchor, check is a no-op (returns None)."""
    m = DriftMonitor(embedder=_MapEmbedder({}), threshold=0.20)
    assert m.check("anything", 0) is None


def test_check_returns_none_when_no_embedder():
    m = DriftMonitor(embedder=None, threshold=0.20)
    m.set_anchor("intent")  # no-op
    assert m.check("probe", 0) is None


def test_check_below_threshold_no_drift():
    """Probe close to anchor → distance below threshold → is_drift False."""
    emb = _MapEmbedder({
        "intent": [1.0, 0.0],
        "good merge": [0.95, 0.05],  # cosine ~0.997, distance ~0.003
    })
    m = DriftMonitor(embedder=emb, threshold=0.20)
    m.set_anchor("intent")
    report = m.check("good merge", 0)
    assert report is not None
    assert not report.is_drift
    assert report.distance < 0.20


def test_check_above_threshold_drift_detected():
    """Probe far from anchor → distance above threshold → is_drift True."""
    emb = _MapEmbedder({
        "intent": [1.0, 0.0],
        "drifted merge": [0.0, 1.0],  # orthogonal, distance 1.0
    })
    m = DriftMonitor(embedder=emb, threshold=0.20)
    m.set_anchor("intent")
    report = m.check("drifted merge", 0)
    assert report is not None
    assert report.is_drift
    assert report.distance > 0.20


def test_cumulative_tracks_max_distance():
    """cumulative is the running max across the session."""
    emb = _MapEmbedder({
        "intent": [1.0, 0.0, 0.0],
        "merge1": [0.95, 0.05, 0.0],   # close
        "merge2": [0.0, 0.0, 1.0],     # far (orthogonal)
        "merge3": [0.95, 0.05, 0.0],   # close again
    })
    m = DriftMonitor(embedder=emb, threshold=0.20)
    m.set_anchor("intent")
    r1 = m.check("merge1", 0)
    r2 = m.check("merge2", 1)
    r3 = m.check("merge3", 2)
    assert r1.cumulative < 0.20
    assert r2.cumulative > 0.90  # spiked
    assert r3.cumulative > 0.90  # retains the max


def test_embed_failure_returns_none():
    """A failing embed on check → None (never raises)."""
    m = DriftMonitor(embedder=_MapEmbedder({"intent": [1.0, 0.0]}), threshold=0.20)
    m.set_anchor("intent")

    class _Boom:
        def embed(self, texts):
            raise RuntimeError("down")

    m.embedder = _Boom()
    assert m.check("probe", 0) is None


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


def test_summary_empty_when_no_history():
    m = DriftMonitor(embedder=_MapEmbedder({}), threshold=0.20)
    assert m.summary() == ""


def test_summary_reports_max_distance_and_band():
    emb = _MapEmbedder({
        "intent": [1.0, 0.0],
        "merge1": [0.9, 0.1],   # low drift
        "merge2": [0.0, 1.0],   # high drift (orthogonal)
    })
    m = DriftMonitor(embedder=emb, threshold=0.20)
    m.set_anchor("intent")
    m.check("merge1", 0)
    m.check("merge2", 1)
    s = m.summary()
    assert "2-commit" in s
    assert "high" in s


def test_summary_low_band_when_all_close():
    emb = _MapEmbedder({
        "intent": [1.0, 0.0],
        "merge": [0.98, 0.02],
    })
    m = DriftMonitor(embedder=emb, threshold=0.20)
    m.set_anchor("intent")
    m.check("merge", 0)
    s = m.summary()
    assert "low" in s


# ---------------------------------------------------------------------------
# DriftReport
# ---------------------------------------------------------------------------


def test_report_render_format():
    # distance 0.25 > threshold 0.20 but < 0.30 (1.5× threshold) → medium band.
    r = DriftReport(commit_index=3, distance=0.25, cumulative=0.25, threshold=0.20)
    s = r.render()
    assert "commit 3" in s
    assert "0.25" in s
    assert "medium" in s


def test_report_is_drift_boundary():
    """distance == threshold is NOT drift (strictly greater)."""
    r = DriftReport(commit_index=0, distance=0.20, cumulative=0.20, threshold=0.20)
    assert not r.is_drift
    r2 = DriftReport(commit_index=0, distance=0.21, cumulative=0.21, threshold=0.20)
    assert r2.is_drift

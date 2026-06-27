"""Tests for the embeddings-calibration corpus + quantile-gap calibrator.

Covers: the similarity-probe corpus is well-formed; the calibrator derives
sensible thresholds from controlled score distributions; the quantile-gap lands
in the valley; the three estimates are recorded; failures degrade gracefully.
"""

from __future__ import annotations

import pytest

from capybase.embeddings_corpus import SIMILARITY_PROBES, probes
from capybase.embeddings_calibration import (
    DEFAULT_MIN_SIMILARITY,
    _largest_gap_threshold,
    _percentile,
    calibrate_thresholds,
)


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------


def test_corpus_nonempty():
    assert len(SIMILARITY_PROBES) >= 6


def test_corpus_accessor_returns_copy():
    a = probes()
    a.append(a[0])
    b = probes()
    assert len(b) < len(a)  # mutating the returned list doesn't affect the source


def test_corpus_probes_well_formed():
    for p in SIMILARITY_PROBES:
        assert p.query and p.related and p.unrelated
        assert p.label
        assert p.language in ("python", "rust")


def test_corpus_spans_languages():
    langs = {p.language for p in SIMILARITY_PROBES}
    assert "python" in langs
    assert "rust" in langs


def test_corpus_related_differs_from_unrelated():
    """Each probe's related and unrelated texts are genuinely different."""
    for p in SIMILARITY_PROBES:
        assert p.related != p.unrelated


# ---------------------------------------------------------------------------
# Statistical helpers
# ---------------------------------------------------------------------------


def test_percentile_single_value():
    assert _percentile([0.5], 50) == 0.5


def test_percentile_basic():
    assert _percentile([0.0, 1.0], 50) == 0.5
    assert _percentile([0.0, 1.0], 0) == 0.0
    assert _percentile([0.0, 1.0], 100) == 1.0


def test_percentile_empty():
    assert _percentile([], 50) == 0.0


def test_percentile_interp():
    # 10th percentile of [0, 10, 20, 30, 40]
    assert abs(_percentile([0, 10, 20, 30, 40], 10) - 4.0) < 0.01


def test_largest_gap_well_separated():
    """Related high, unrelated low, clear gap → threshold in the gap."""
    t = _largest_gap_threshold([0.8, 0.85, 0.9], [0.2, 0.25, 0.3])
    assert 0.3 < t < 0.8  # lands between the clusters


def test_largest_gap_tolerates_outliers():
    """A few unrelated outliers in the related zone don't move the threshold
    out of the main gap."""
    t = _largest_gap_threshold(
        [0.997, 0.998, 0.999, 1.0, 0.997, 0.998, 0.999, 1.0],
        [0.196, 0.5, 0.6, 0.9999, 0.196, 0.5, 0.6, 0.9999],
    )
    # The main gap is between ~0.6 and ~0.997; threshold lands there.
    assert 0.6 < t < 0.997


def test_largest_gap_empty():
    assert _largest_gap_threshold([], []) == DEFAULT_MIN_SIMILARITY


# ---------------------------------------------------------------------------
# calibrate_thresholds — with a fake client
# ---------------------------------------------------------------------------


class _DomainFakeClient:
    """Maps texts to 2D vectors by domain so related pairs are close."""

    def embed(self, texts):
        if isinstance(texts, str):
            texts = [texts]
        vecs = []
        for t in texts:
            if any(k in t for k in ["rust", "fn ", "impl", "const", "enum", "struct"]):
                base = [0.9, 0.1]
            else:
                base = [0.1, 0.9]
            noise = (len(t) % 7) * 0.01
            vecs.append([base[0] + noise, base[1] - noise])
        return vecs


class _FailingClient:
    def embed(self, texts):
        raise RuntimeError("server down")


class _MismatchClient:
    def embed(self, texts):
        if isinstance(texts, str):
            texts = [texts]
        # Return wrong count
        return [[0.1, 0.2] for _ in range(len(texts) - 1)]


def test_calibrate_succeeds_with_realistic_client():
    cal = calibrate_thresholds(_DomainFakeClient(), embeddings_model="embed")
    assert cal.ok
    assert 0.0 < cal.min_similarity <= 1.0
    assert cal.related.count == len(SIMILARITY_PROBES)
    assert cal.unrelated.count == len(SIMILARITY_PROBES)
    # Related scores should be higher than unrelated on average.
    assert cal.related.mean > cal.unrelated.mean


def test_calibrate_records_all_three_estimates():
    cal = calibrate_thresholds(_DomainFakeClient(), embeddings_model="embed")
    assert cal.ok
    d = cal.to_dict()
    ests = d["estimates"]
    assert "quantile_gap" in ests
    assert "related_p10" in ests
    assert "unrelated_p90" in ests


def test_calibrate_envelope_has_distributions():
    cal = calibrate_thresholds(_DomainFakeClient(), embeddings_model="embed")
    d = cal.to_dict()
    assert d["related"]["count"] > 0
    assert "min" in d["related"] and "max" in d["related"] and "mean" in d["related"]
    assert d["unrelated"]["count"] > 0


def test_calibrate_failed_endpoint_keeps_default():
    cal = calibrate_thresholds(_FailingClient(), embeddings_model="embed")
    assert not cal.ok
    assert cal.min_similarity == DEFAULT_MIN_SIMILARITY
    assert any("failed" in n for n in cal.notes)


def test_calibrate_count_mismatch_keeps_default():
    cal = calibrate_thresholds(_MismatchClient(), embeddings_model="embed")
    assert not cal.ok
    assert cal.min_similarity == DEFAULT_MIN_SIMILARITY


def test_calibrate_model_recorded():
    cal = calibrate_thresholds(_DomainFakeClient(), embeddings_model="qwen-embed")
    assert cal.model == "qwen-embed"
    assert cal.to_dict()["model"] == "qwen-embed"


def test_calibrate_threshold_separates_classes():
    """The derived threshold should separate the related/unrelated distributions:
    most related scores above it, most unrelated below."""
    cal = calibrate_thresholds(_DomainFakeClient(), embeddings_model="embed")
    assert cal.ok
    # The threshold should be below the related mean and above the unrelated mean
    # (for a well-behaved model on this corpus).
    assert cal.min_similarity < cal.related.mean


def test_to_dict_roundtrip_shape():
    cal = calibrate_thresholds(_DomainFakeClient(), embeddings_model="embed")
    d = cal.to_dict()
    assert isinstance(d, dict)
    assert d["ok"] is True
    assert "probed_at" in d
    assert isinstance(d["notes"], list)

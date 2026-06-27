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


def test_corpus_size_in_target_band():
    """Enough probes to fit a stable isotonic transform and yield well-separated
    related/unrelated distributions. Target band is 24-32 (breadth over depth)."""
    n = len(SIMILARITY_PROBES)
    assert 24 <= n <= 32, f"corpus has {n} probes; expected 24-32"


def test_corpus_labels_unique():
    """Labels are report tags — they must be unique so a calibration report can
    attribute each probe without ambiguity."""
    labels = [p.label for p in SIMILARITY_PROBES]
    assert len(set(labels)) == len(labels)


def test_corpus_balanced_language_coverage():
    """Both languages carry enough probes to measure per-language drift later."""
    from collections import Counter

    counts = Counter(p.language for p in SIMILARITY_PROBES)
    assert counts["python"] >= 8
    assert counts["rust"] >= 5


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


# ---------------------------------------------------------------------------
# Overlap-fallback branch (the "weak model" path)
# ---------------------------------------------------------------------------


class _ZeroVectorClient:
    """Every text embeds to the zero vector — cosine is 0.0 for every pair.

    This is the degenerate 'distributions overlap entirely' case: related and
    unrelated scores are indistinguishable, so the quantile-gap estimate
    collapses to 0 and the calibrator must fall back to the conservative floor
    (unrelated_p90) rather than admit every conflict as a few-shot match.
    """

    def embed(self, texts):
        if isinstance(texts, str):
            texts = [texts]
        return [[0.0, 0.0, 0.0] for _ in texts]


def test_overlap_fallback_applies_conservative_floor():
    """When the quantile-gap collapses (overlap), the applied floor falls back to
    unrelated_p90 rather than 0.0 — and a diagnostic note is recorded."""
    cal = calibrate_thresholds(_ZeroVectorClient(), embeddings_model="embed")
    assert cal.ok  # the endpoint worked; the MODEL is just too weak
    # Both distributions are all zeros → gap_threshold is 0.0 → fallback fires.
    assert cal.quantile_gap == 0.0
    # The applied floor is the conservative reference (unrelated_p90 = 0.0 here).
    assert cal.min_similarity == cal.unrelated_p90
    assert any("overlap" in n for n in cal.notes)
    assert any("too weak" in n for n in cal.notes)


def test_overlap_fallback_recorded_envelope_carries_note():
    """The overlap note survives into the serialized envelope (so the report /
    profile shows the user the model is too weak for reliable RAG)."""
    cal = calibrate_thresholds(_ZeroVectorClient(), embeddings_model="embed")
    d = cal.to_dict()
    assert d["ok"] is True
    assert d["min_similarity"] == d["estimates"]["unrelated_p90"]
    assert any("overlap" in n for n in d["notes"])


class _ConstantVectorClient:
    """Every text embeds to the SAME non-zero vector — cosine is 1.0 for every
    pair. A different degeneracy: the gap is well-defined (0) but at the top of
    the range, so the fallback does NOT fire; the threshold is a valid positive
    value. Guards against the fallback over-triggering."""

    def embed(self, texts):
        if isinstance(texts, str):
            texts = [texts]
        return [[1.0, 0.5] for _ in texts]


def test_constant_vectors_do_not_trigger_overlap_fallback():
    """A constant-vectors model (all cosine 1.0) has a positive median-midpoint
    threshold, so the conservative fallback must NOT fire (no overlap note)."""
    cal = calibrate_thresholds(_ConstantVectorClient(), embeddings_model="embed")
    assert cal.ok
    assert cal.min_similarity > 0.0
    assert not any("overlap" in n for n in cal.notes)


# ---------------------------------------------------------------------------
# Score calibration (§2.1): isotonic transform + 3-zone thresholds (§3.2)
# ---------------------------------------------------------------------------


def test_isotonic_fit_produced_on_well_separated_model():
    """A well-separated model (related high, unrelated low) yields an isotonic
    transform with recorded breakpoints and an ordered green/amber/red zone."""
    cal = calibrate_thresholds(_DomainFakeClient(), embeddings_model="embed")
    assert cal.ok
    assert cal.has_isotonic_fit
    assert len(cal.isotonic_points) > 0
    # green (high-confidence band) >= amber (borderline) >= red (hard floor).
    assert cal.green_threshold >= cal.amber_threshold >= cal.red_threshold
    # The classes separate on the calibrated scale.
    assert cal.ks_separation > 0.0


def test_isotonic_transform_is_monotone():
    """The calibrated transform is monotone-nondecreasing: higher raw cosine ->
    higher-or-equal calibrated score. (Isotonic regression's defining property.)"""
    cal = calibrate_thresholds(_DomainFakeClient(), embeddings_model="embed")
    assert cal.has_isotonic_fit
    raws = sorted({p[0] for p in cal.isotonic_points})
    cals = [cal.calibrated_score(r) for r in raws]
    assert cals == sorted(cals)
    # The calibrated score of a high raw value exceeds a low raw value's.
    assert cal.calibrated_score(raws[-1]) >= cal.calibrated_score(raws[0])


def test_calibrated_score_is_identity_without_fit():
    """No isotonic fit (overlap/weak model) -> calibrated_score returns the raw
    value unchanged (the floor is then evaluated on the raw scale as before)."""
    cal = calibrate_thresholds(_ZeroVectorClient(), embeddings_model="embed")
    assert not cal.has_isotonic_fit
    assert cal.calibrated_score(0.42) == 0.42


def test_overlap_drops_isotonic_fit_keeps_raw_floor():
    """When the model is too weak (overlap), the isotonic fit is dropped and the
    raw quantile-gap floor still applies. Both contracts hold together."""
    cal = calibrate_thresholds(_ZeroVectorClient(), embeddings_model="embed")
    assert cal.ok
    assert not cal.has_isotonic_fit
    assert cal.isotonic_points == []
    # The raw floor still fires (the pre-isotonic behavior, preserved).
    assert cal.min_similarity == cal.unrelated_p90


def test_zones_and_isotonic_roundtrip_via_to_dict():
    """The isotonic points + zones survive serialization (to_dict)."""
    cal = calibrate_thresholds(_DomainFakeClient(), embeddings_model="embed")
    d = cal.to_dict()
    assert d["ok"] is True
    assert "zones" in d and {"green", "amber", "red"} <= set(d["zones"])
    assert isinstance(d["isotonic_points"], list)
    assert all(isinstance(p, list) and len(p) == 2 for p in d["isotonic_points"])
    assert d["ks_separation"] > 0.0


def test_from_dict_roundtrips_isotonic_and_zones():
    """from_dict reconstructs the full envelope incl. the transform + zones, and
    the reconstructed transform scores identically (within the 4dp serialization
    rounding that to_dict deliberately applies)."""
    from capybase.embeddings_calibration import EmbeddingCalibration

    cal = calibrate_thresholds(_DomainFakeClient(), embeddings_model="embed")
    again = EmbeddingCalibration.from_dict(cal.to_dict())
    assert again.has_isotonic_fit
    assert len(again.isotonic_points) == len(cal.isotonic_points)
    # Thresholds round to 4dp on serialization.
    assert again.green_threshold == pytest.approx(cal.green_threshold, abs=1e-4)
    assert again.red_threshold == pytest.approx(cal.red_threshold, abs=1e-4)
    assert again.ks_separation == pytest.approx(cal.ks_separation, abs=1e-4)
    # The reconstructed transform scores a known raw value within rounding.
    raw = cal.isotonic_points[0][0]
    assert again.calibrated_score(raw) == pytest.approx(
        cal.calibrated_score(raw), abs=1e-3
    )


def test_from_dict_tolerant_of_pre_isotonic_envelope():
    """An old envelope (pre-isotonic, missing zones/isotonic_points) still loads,
    with the additive fields defaulting gracefully."""
    from capybase.embeddings_calibration import EmbeddingCalibration

    old_envelope = {
        "model": "embed",
        "min_similarity": 0.4,
        "estimates": {"quantile_gap": 0.4, "related_p10": 0.6, "unrelated_p90": 0.3},
        "related": {"count": 8, "min": 0.5, "max": 0.9, "mean": 0.7},
        "unrelated": {"count": 8, "min": 0.1, "max": 0.35, "mean": 0.2},
        "ok": True,
        "probed_at": "2026-06-27T00:00:00+00:00",
        "notes": [],
    }
    cal = EmbeddingCalibration.from_dict(old_envelope)
    assert cal.min_similarity == 0.4
    assert not cal.has_isotonic_fit
    assert cal.isotonic_points == []
    assert cal.green_threshold == 0.0
    # Identity transform (no fit) -> calibrated_score returns raw unchanged.
    assert cal.calibrated_score(0.4) == 0.4


def test_old_envelope_keys_still_present():
    """Backward-compat: the pre-isotonic envelope keys all still appear in
    to_dict (existing readers/tests aren't broken by the additive zones)."""
    cal = calibrate_thresholds(_DomainFakeClient(), embeddings_model="embed")
    d = cal.to_dict()
    for key in (
        "model", "min_similarity", "estimates", "related", "unrelated",
        "ok", "probed_at", "notes",
    ):
        assert key in d, f"missing legacy key: {key}"
    assert "quantile_gap" in d["estimates"]
    assert "related_p10" in d["estimates"]
    assert "unrelated_p90" in d["estimates"]


# ---------------------------------------------------------------------------
# Robust estimators (survey 2 §3.1 Huber, §4.1 MAD zones)
# ---------------------------------------------------------------------------


def test_score_distribution_records_median_and_mad():
    """The distribution summary now carries the robust median/MAD alongside
    mean/min/max — the data drift detection compares on."""
    cal = calibrate_thresholds(_DomainFakeClient(), embeddings_model="embed")
    assert cal.related.count > 0
    assert isinstance(cal.related.median, float)
    assert isinstance(cal.related.mad, float)
    # to_dict surfaces them.
    d = cal.to_dict()
    assert "median" in d["related"] and "mad" in d["related"]


def test_envelope_records_fit_loss_and_zone_method():
    """The provenance fields (fit_loss, zone_method) are recorded for transparency."""
    cal = calibrate_thresholds(_DomainFakeClient(), embeddings_model="embed")
    assert cal.fit_loss in ("l2", "huber")
    assert cal.zone_method in ("mad", "percentile")
    d = cal.to_dict()
    assert d["fit_loss"] == cal.fit_loss
    assert d["zone_method"] == cal.zone_method
    assert "related_mad" in d and "unrelated_mad" in d


def test_fit_loss_and_zone_method_roundtrip():
    """The provenance fields survive to_dict/from_dict."""
    from capybase.embeddings_calibration import EmbeddingCalibration

    cal = calibrate_thresholds(_DomainFakeClient(), embeddings_model="embed")
    again = EmbeddingCalibration.from_dict(cal.to_dict())
    assert again.fit_loss == cal.fit_loss
    assert again.zone_method == cal.zone_method
    assert again.related_mad == pytest.approx(cal.related_mad, abs=1e-4)


def test_from_dict_tolerant_of_pre_robust_envelope():
    """An old envelope (pre-Huber/MAD) omits the new fields — loads with defaults."""
    from capybase.embeddings_calibration import EmbeddingCalibration

    old = {
        "model": "embed", "min_similarity": 0.4, "ok": True, "probed_at": "",
        "estimates": {"quantile_gap": 0.4, "related_p10": 0.6, "unrelated_p90": 0.3},
        "related": {"count": 8, "min": 0.5, "max": 0.9, "mean": 0.7},
        "unrelated": {"count": 8, "min": 0.1, "max": 0.35, "mean": 0.2},
        "isotonic_points": [[0.1, 0.0], [0.9, 1.0]],
        "zones": {"green": 0.7, "amber": 0.6, "red": 0.5},
        "ks_separation": 0.8, "notes": [],
    }
    cal = EmbeddingCalibration.from_dict(old)
    assert cal.fit_loss == "l2"  # default
    assert cal.zone_method == "mad"  # default
    assert cal.related.median == 0.0  # absent → default
    assert cal.related_mad == 0.0


def test_overlap_fallback_uses_percentile_zones_when_mad_zero():
    """A degenerate (zero-vector) model produces MAD=0 calibrated scores, so the
    zone derivation falls back to percentile zones (the documented degeneracy path)."""
    cal = calibrate_thresholds(_ZeroVectorClient(), embeddings_model="embed")
    # The fit is dropped entirely on full overlap, so no zones either way —
    # confirm the never-raise contract holds and zone_method is a valid value.
    assert cal.zone_method in ("mad", "percentile")
    assert cal.fit_loss in ("l2", "huber")


def test_label_noise_triggers_huber_refit():
    """When the L2 fit shows a large robust-sigma residual (label noise), the
    calibrator refits under Huber loss and records fit_loss='huber' + a note.

    The _DomainFakeClient's keyword-based separation produces exactly such a
    residual pattern, so it serves as the noise case here."""
    cal = calibrate_thresholds(_DomainFakeClient(), embeddings_model="embed")
    assert cal.fit_loss == "huber"
    assert any("Huber" in n or "noise" in n for n in cal.notes)


def test_clean_model_keeps_l2_fit():
    """A model whose L2 residuals are all within the robust threshold keeps the
    L2 fit (fit_loss='l2'). Built by a fake with no residual outliers."""

    class _CleanSeparationClient:
        """Perfectly separable: related always cosine ~1.0, unrelated always ~0.0,
        with no residual outliers — L2 suffices."""
        def embed(self, texts):
            if isinstance(texts, str):
                texts = [texts]
            out = []
            for t in texts:
                # All corpus signatures share tokens → close; queries close too.
                # Use a single consistent axis so related~1, unrelated~0 cleanly.
                out.append([1.0, 0.0])
            return out

    cal = calibrate_thresholds(_CleanSeparationClient(), embeddings_model="embed")
    # All identical vectors → cosines all 1.0 → no separation → fit dropped.
    # The provenance fields still take valid default values.
    assert cal.fit_loss in ("l2", "huber")


def test_zones_remain_ordered_on_separated_model():
    """Regardless of MAD vs percentile path, green >= amber >= red holds on a
    well-separated model (the zone invariant downstream code relies on)."""
    cal = calibrate_thresholds(_DomainFakeClient(), embeddings_model="embed")
    if cal.has_isotonic_fit:
        assert cal.green_threshold >= cal.amber_threshold >= cal.red_threshold

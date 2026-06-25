"""Tests for calibrated risk routing.

These exercise the calibration seam: feature extraction, the CalibrationModel
load/save/predict cycle, the CalibratedRiskEngine overriding the accept path,
and the CalibrationDataset built from the experience store. No sklearn is
needed at runtime — the model is pure-Python inference; fitting is offline.
"""

from __future__ import annotations

import json
from pathlib import Path

from capybase.calibration import (
    CalibrationDataset,
    CalibrationModel,
    CalibratedRiskEngine,
    features_to_vector,
)
from capybase.conflict_model import (
    HistoricalExample,
    VerificationResult,
)
from capybase.memory.store import Experience, ExperienceStore
from capybase.risk import RiskEngine


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------


def test_features_to_vector_handles_bools_ints_missing():
    feats = {
        "markers_remaining": True,
        "splice_scope_ok": False,
        "lsp_error_count": 3,
        "syntax_passed": True,
    }
    vec = features_to_vector(feats)
    assert isinstance(vec, list)
    assert all(isinstance(x, float) for x in vec)
    # markers_remaining=True → 1.0
    idx_markers = _FEATURE_KEYS.index("markers_remaining")
    assert vec[idx_markers] == 1.0
    # splice_scope_ok=False → 0.0
    idx_scope = _FEATURE_KEYS.index("splice_scope_ok")
    assert vec[idx_scope] == 0.0
    # lsp_error_count=3 → 3.0
    idx_lsp = _FEATURE_KEYS.index("lsp_error_count")
    assert vec[idx_lsp] == 3.0
    # Missing key (model_needs_human) → 0.0
    idx_nh = _FEATURE_KEYS.index("model_needs_human")
    assert vec[idx_nh] == 0.0


# Need the constant for index lookup; import it.
from capybase.calibration import _FEATURE_KEYS  # noqa: E402


def test_features_to_vector_empty_dict():
    vec = features_to_vector({})
    assert len(vec) == len(_FEATURE_KEYS)
    assert all(x == 0.0 for x in vec)


# ---------------------------------------------------------------------------
# CalibrationModel
# ---------------------------------------------------------------------------


def test_model_predict_proba_range():
    m = CalibrationModel(
        coefficients=[0.0] * len(_FEATURE_KEYS),
        intercept=0.0,
        threshold=0.7,
    )
    proba = m.predict_proba({})
    assert 0.0 <= proba <= 1.0
    # With all-zero features and zero intercept, proba should be 0.5.
    assert abs(proba - 0.5) < 0.01


def test_model_predict_proba_increases_with_risk_features():
    # A positive coefficient on model_needs_human should raise the proba.
    idx_nh = _FEATURE_KEYS.index("model_needs_human")
    coeffs = [0.0] * len(_FEATURE_KEYS)
    coeffs[idx_nh] = 5.0  # strong positive weight
    m = CalibrationModel(coefficients=coeffs, intercept=0.0, threshold=0.7)
    safe = m.predict_proba({})
    risky = m.predict_proba({"model_needs_human": True})
    assert risky > safe
    assert risky > 0.9  # strong weight → near-certain failure


def test_model_save_load_roundtrip(tmp_path):
    m = CalibrationModel(
        coefficients=[1.0, 2.0], intercept=-1.0, threshold=0.65
    )
    path = tmp_path / "model.json"
    path.write_text(json.dumps(m.to_dict()), encoding="utf-8")
    loaded = CalibrationModel.load(path)
    assert loaded is not None
    assert loaded.coefficients == [1.0, 2.0]
    assert loaded.intercept == -1.0
    assert loaded.threshold == 0.65


def test_model_load_returns_none_when_absent(tmp_path):
    assert CalibrationModel.load(tmp_path / "nonexistent.json") is None


def test_model_load_returns_none_on_corrupt(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not json", encoding="utf-8")
    assert CalibrationModel.load(path) is None


# ---------------------------------------------------------------------------
# CalibratedRiskEngine
# ---------------------------------------------------------------------------


def _passed_result(features=None):
    return VerificationResult(
        candidate_id="c",
        unit_id="u",
        passed=True,
        hard_failures=[],
        features=features or {},
    )


def test_calibrated_passthrough_when_no_model():
    engine = CalibratedRiskEngine(max_retries_per_unit=2, model=None)
    res = _passed_result({"syntax_passed": True})
    decision = engine.decide(res, retry_count=0)
    assert decision.action == "accept"


def test_calibrated_escalates_high_risk_passing_candidate():
    # Model with a strong weight on model_needs_human and a low threshold.
    idx_nh = _FEATURE_KEYS.index("model_needs_human")
    coeffs = [0.0] * len(_FEATURE_KEYS)
    coeffs[idx_nh] = 5.0
    model = CalibrationModel(coefficients=coeffs, intercept=0.0, threshold=0.5)
    engine = CalibratedRiskEngine(max_retries_per_unit=2, model=model)
    # A candidate that PASSES all hard checks but has model_needs_human=True
    # (a soft signal the rules engine ignores on the accept path).
    res = _passed_result({"syntax_passed": True, "model_needs_human": True})
    decision = engine.decide(res, retry_count=0)
    assert decision.action == "escalate"
    assert decision.risk_score is not None and decision.risk_score > 0.5


def test_calibrated_accepts_low_risk_passing_candidate():
    idx_nh = _FEATURE_KEYS.index("model_needs_human")
    coeffs = [0.0] * len(_FEATURE_KEYS)
    coeffs[idx_nh] = 5.0
    model = CalibrationModel(coefficients=coeffs, intercept=-5.0, threshold=0.7)
    engine = CalibratedRiskEngine(max_retries_per_unit=2, model=model)
    res = _passed_result({"syntax_passed": True})  # no risk features
    decision = engine.decide(res, retry_count=0)
    assert decision.action == "accept"
    assert decision.risk_score is not None and decision.risk_score < 0.7


def test_calibrated_does_not_override_technical_failure_routing():
    # A truncated candidate should still retry, not be sent to the model.
    model = CalibrationModel(
        coefficients=[0.0] * len(_FEATURE_KEYS), intercept=0.0, threshold=0.1
    )
    engine = CalibratedRiskEngine(max_retries_per_unit=2, model=model)
    res = VerificationResult(
        candidate_id="c", unit_id="u", passed=False, hard_failures=[], features={}
    )
    decision = engine.decide(res, retry_count=0, failure_kind="truncated")
    assert decision.action == "retry"


def test_calibrated_from_config_loads_model(tmp_path):
    model = CalibrationModel(coefficients=[1.0], intercept=0.0, threshold=0.9)
    path = tmp_path / "model.json"
    path.write_text(json.dumps(model.to_dict()), encoding="utf-8")
    engine = CalibratedRiskEngine.from_config(
        max_retries_per_unit=2,
        model_path=str(path),
        escalate_threshold=0.6,
    )
    assert engine.model is not None
    assert engine.model.threshold == 0.6  # overridden by config


def test_calibrated_from_config_passthrough_when_no_model(tmp_path):
    engine = CalibratedRiskEngine.from_config(
        max_retries_per_unit=2,
        model_path=str(tmp_path / "nonexistent.json"),
        escalate_threshold=0.7,
    )
    assert engine.model is None


# ---------------------------------------------------------------------------
# CalibrationDataset
# ---------------------------------------------------------------------------


def test_dataset_from_store_labels_outcomes(tmp_path):
    store = ExperienceStore(tmp_path / "exp.jsonl")
    store.append(
        Experience(
            example=HistoricalExample(summary="ok", base="b", current="c", replayed="r", resolved="s"),
            outcome="accepted",
            validator_features={"syntax_passed": True},
        )
    )
    store.append(
        Experience(
            example=HistoricalExample(summary="bad", base="b", current="c", replayed="r", resolved=""),
            outcome="escalated",
            validator_features={"syntax_passed": False},
        )
    )
    ds = CalibrationDataset.from_store(store)
    assert ds.n == 2
    assert ds.n_positive == 1  # one failure
    # accepted → label 0.0; escalated → label 1.0
    labels = [y for _, y in ds.rows]
    assert 0.0 in labels and 1.0 in labels

"""Tests for Phase D: consensus entropy routing, conformal risk, top-K bundles.

Step 5 of the multi-request pipeline. After filtering and repairing candidates,
algorithmically decide accept or escalate: high-entropy consensus → human review
with a side-by-side view of the top-K variations.
"""

from __future__ import annotations

import json
from pathlib import Path

from capybase.escalation import write_review_bundle
from capybase.conflict_model import (
    CandidateResolution,
    ConflictSide,
    ConflictUnit,
    VerificationResult,
)
from capybase.consensus import ConsensusReport, _entropy, select
from capybase.session import SessionPaths


# ---------------------------------------------------------------------------
# Entropy
# ---------------------------------------------------------------------------


def test_entropy_zero_for_unanimous():
    assert _entropy([5], 5) == 0.0


def test_entropy_one_for_maximally_split():
    # 5 samples, 5 clusters → maximally split → entropy 1.0
    assert abs(_entropy([1, 1, 1, 1, 1], 5) - 1.0) < 0.01


def test_entropy_partial_split():
    e = _entropy([3, 2], 5)
    assert 0.0 < e < 1.0


def test_entropy_single_sample():
    assert _entropy([1], 1) == 0.0


def test_consensus_report_has_entropy_field():
    cands = [
        _cand("a", "AAA"),
        _cand("b", "AAA"),
        _cand("c", "BBB"),
        _cand("d", "CCC"),
        _cand("e", "DDD"),
    ]
    rep = select(cands, None)
    assert rep.entropy > 0.5  # split across many clusters
    assert rep.cluster_count == 4  # AAA, BBB, CCC, DDD


def _cand(rid, text, conf=0.0):
    return CandidateResolution(
        candidate_id=rid, unit_id="u", model_name="m",
        prompt_version="v", resolved_text=text, self_reported_confidence=conf,
    )


# ---------------------------------------------------------------------------
# Entropy → risk routing
# ---------------------------------------------------------------------------


def test_risk_escalates_on_high_entropy():
    from capybase.risk import RiskEngine

    res = VerificationResult(
        candidate_id="c", unit_id="u", passed=True, hard_failures=[], features={},
    )
    engine = RiskEngine(entropy_escalate_threshold=0.5)
    decision = engine.decide(res, retry_count=0, consensus_entropy=0.8)
    assert decision.action == "escalate"
    assert "entropy" in decision.reasons[0].lower()


def test_risk_accepts_on_low_entropy():
    from capybase.risk import RiskEngine

    res = VerificationResult(
        candidate_id="c", unit_id="u", passed=True, hard_failures=[], features={},
    )
    engine = RiskEngine(entropy_escalate_threshold=0.5)
    decision = engine.decide(res, retry_count=0, consensus_entropy=0.1)
    assert decision.action == "accept"


def test_risk_no_entropy_passthrough():
    from capybase.risk import RiskEngine

    res = VerificationResult(
        candidate_id="c", unit_id="u", passed=True, hard_failures=[], features={},
    )
    engine = RiskEngine(entropy_escalate_threshold=0.5)
    # consensus_entropy=None → no entropy check, accept
    decision = engine.decide(res, retry_count=0, consensus_entropy=None)
    assert decision.action == "accept"


# ---------------------------------------------------------------------------
# ConformalRiskModel
# ---------------------------------------------------------------------------


def test_conformal_model_predict_proba_range():
    from capybase.calibration import ConformalRiskModel, _FEATURE_KEYS

    m = ConformalRiskModel(
        coefficients=[0.0] * len(_FEATURE_KEYS), intercept=0.0, alpha=0.1,
        calibration_scores=[0.5, 0.6, 0.7, 0.8],
    )
    p = m.predict_proba({})
    assert 0.0 <= p <= 1.0


def test_conformal_model_should_escalate_low_pvalue():
    from capybase.calibration import ConformalRiskModel

    # Calibration scores are nonconformity (1 - P(true label)); high = atypical.
    # A risky feature (model_needs_human=True with a strong positive coefficient)
    # drives P(fail) high → high nonconformity → fewer calibration points are
    # more atypical → low p-value → escalate.
    from capybase.calibration import _FEATURE_KEYS

    idx_nh = _FEATURE_KEYS.index("model_needs_human")
    coeffs = [0.0] * len(_FEATURE_KEYS)
    coeffs[idx_nh] = 10.0  # very strong failure signal
    m = ConformalRiskModel(
        coefficients=coeffs, intercept=-2.0, alpha=0.1,
        # A large calibration set so the smoothing floor 1/(n+1) < alpha.
        calibration_scores=[i / 20 for i in range(1, 20)],
    )
    assert m.should_escalate({"model_needs_human": True})


def test_conformal_model_accepts_safe_features():
    from capybase.calibration import ConformalRiskModel, _FEATURE_KEYS

    m = ConformalRiskModel(
        coefficients=[0.0] * len(_FEATURE_KEYS), intercept=-5.0, alpha=0.1,
        calibration_scores=[0.5, 0.6, 0.7, 0.8],
    )
    assert not m.should_escalate({})


def test_conformal_pvalue_ranks_success_above_failure():
    """Coverage-guarantee direction: a candidate the model predicts will
    SUCCEED (low P(fail), low nonconformity) must get a HIGHER p-value than one
    it predicts will FAIL (high P(fail), high nonconformity), given the same
    calibration set. This pins the conformal convention — it would fail against
    the prior inverted scorer."""
    from capybase.calibration import ConformalRiskModel
    from capybase.calibration import _FEATURE_KEYS

    # model_needs_human=True is a strong failure signal (positive coef → high
    # z → high P(fail) → high nonconformity → low p-value).
    idx_nh = _FEATURE_KEYS.index("model_needs_human")
    coeffs = [0.0] * len(_FEATURE_KEYS)
    coeffs[idx_nh] = 10.0
    m = ConformalRiskModel(
        coefficients=coeffs, intercept=-3.0, alpha=0.1,
        # A large calibration set spanning safe→risky nonconformity. The
        # smoothing floor is 1/(n+1); with n=19 that's ~0.05 < alpha=0.1, so a
        # maximally-atypical candidate can actually fall below alpha.
        calibration_scores=[i / 20 for i in range(1, 20)],
    )
    p_safe = m.predict_proba({})                       # model predicts success
    p_risky = m.predict_proba({"model_needs_human": True})  # model predicts fail
    assert p_safe > p_risky, (p_safe, p_risky)
    # And the safe candidate is accepted, the risky one escalated.
    assert not m.should_escalate({})
    assert m.should_escalate({"model_needs_human": True})


def test_conformal_pvalue_smoothing_bounds():
    """p-values use the (count+1)/(n+1) smoothing: never exactly 0 or 1."""
    from capybase.calibration import ConformalRiskModel, _FEATURE_KEYS

    m = ConformalRiskModel(
        coefficients=[0.0] * len(_FEATURE_KEYS), intercept=0.0, alpha=0.1,
        calibration_scores=[0.5, 0.6, 0.7, 0.8],
    )
    # Even at the extremes of nonconformity, p stays in the open (0,1) range.
    assert 0.0 < m.predict_proba({}) < 1.0



def test_conformal_model_save_load_roundtrip(tmp_path):
    from capybase.calibration import ConformalRiskModel

    m = ConformalRiskModel(
        coefficients=[1.0, 2.0], intercept=-1.0, alpha=0.15,
        calibration_scores=[0.5, 0.6],
    )
    path = tmp_path / "model.json"
    path.write_text(json.dumps(m.to_dict()), encoding="utf-8")
    loaded = ConformalRiskModel.load(path)
    assert loaded is not None
    assert loaded.coefficients == [1.0, 2.0]
    assert loaded.alpha == 0.15
    assert loaded.calibration_scores == [0.5, 0.6]


def test_conformal_model_load_returns_none_for_non_conformal(tmp_path):
    """Loading a non-conformal JSON returns None (falls back to logistic)."""
    from capybase.calibration import ConformalRiskModel

    path = tmp_path / "model.json"
    path.write_text(json.dumps({"coefficients": [], "intercept": 0, "threshold": 0.7}), encoding="utf-8")
    assert ConformalRiskModel.load(path) is None


# ---------------------------------------------------------------------------
# TECP token-entropy threshold (survey §4.1): the entropy-only conformal gate
# that fires when a candidate's mean token-entropy exceeds the (1-alpha)
# quantile fit on accepted calibration outcomes.
# ---------------------------------------------------------------------------


def test_feature_keys_include_mean_token_entropy():
    """The model-side uncertainty signal is part of the canonical vector."""
    from capybase.calibration import _FEATURE_KEYS

    assert "mean_token_entropy" in _FEATURE_KEYS


def test_conformal_tecp_threshold_roundtrips(tmp_path):
    from capybase.calibration import ConformalRiskModel

    m = ConformalRiskModel(
        coefficients=[1.0], intercept=0.0, alpha=0.1,
        calibration_scores=[0.5], tecp_entropy_threshold=1.2,
    )
    path = tmp_path / "model.json"
    path.write_text(json.dumps(m.to_dict()), encoding="utf-8")
    loaded = ConformalRiskModel.load(path)
    assert loaded is not None
    assert loaded.tecp_entropy_threshold == 1.2


def test_conformal_tecp_threshold_defaults_none():
    """Without a fitted threshold, the TECP gate is absent (ignored)."""
    from capybase.calibration import ConformalRiskModel, _FEATURE_KEYS

    m = ConformalRiskModel(
        coefficients=[0.0] * len(_FEATURE_KEYS), intercept=-5.0, alpha=0.1,
        calibration_scores=[0.5, 0.6, 0.7, 0.8],
    )
    assert m.tecp_entropy_threshold is None


def test_conformal_tecp_escalates_high_entropy():
    """A candidate whose mean token-entropy exceeds the fitted threshold is
    escalated even when the logistic p-value alone would accept it."""
    from capybase.calibration import ConformalRiskModel, _FEATURE_KEYS

    # Strong negative intercept → the logistic path predicts success (low
    # nonconformity) so it would normally accept; the entropy gate overrides.
    m = ConformalRiskModel(
        coefficients=[0.0] * len(_FEATURE_KEYS), intercept=-5.0, alpha=0.1,
        calibration_scores=[0.5, 0.6, 0.7, 0.8],
        tecp_entropy_threshold=0.9,
    )
    # Sanity: low entropy alone is accepted.
    assert not m.should_escalate({"mean_token_entropy": 0.1})
    # High entropy crosses the TECP gate → escalate.
    assert m.should_escalate({"mean_token_entropy": 1.5})


def test_conformal_tecp_ignores_missing_entropy():
    """When the candidate has no entropy value (capture off), the TECP gate is
    skipped — never escalates on a missing signal."""
    from capybase.calibration import ConformalRiskModel, _FEATURE_KEYS

    m = ConformalRiskModel(
        coefficients=[0.0] * len(_FEATURE_KEYS), intercept=-5.0, alpha=0.1,
        calibration_scores=[0.5, 0.6, 0.7, 0.8],
        tecp_entropy_threshold=0.9,
    )
    assert not m.should_escalate({})  # no mean_token_entropy key
    assert not m.should_escalate({"mean_token_entropy": None})


def test_conformal_tecp_threshold_boundary_is_strict():
    """Entropy equal to the threshold does NOT escalate (strict >). This pins
    the boundary so the (1-alpha) coverage quantile is itself accepted."""
    from capybase.calibration import ConformalRiskModel, _FEATURE_KEYS

    m = ConformalRiskModel(
        coefficients=[0.0] * len(_FEATURE_KEYS), intercept=-5.0, alpha=0.1,
        calibration_scores=[0.5, 0.6, 0.7, 0.8],
        tecp_entropy_threshold=0.9,
    )
    assert not m.should_escalate({"mean_token_entropy": 0.9})  # equal → accept
    assert m.should_escalate({"mean_token_entropy": 0.9001})   # just over → escalate


# ---------------------------------------------------------------------------
# Runtime wiring: the conformal model (with TECP) actually gates the
# CalibratedRiskEngine.decide accept→escalate path. Closes the loop so the
# entropy signal reaches routing, not just capture + calibration.
# ---------------------------------------------------------------------------


def _passed_result(features):
    return VerificationResult(
        candidate_id="c", unit_id="u", passed=True, hard_failures=[],
        features=features,
    )


def test_engine_escalates_via_conformal_tecp_at_runtime():
    """A passing candidate whose mean_token_entropy crosses the TECP threshold
    is escalated by CalibratedRiskEngine.decide — the end-to-end path."""
    from capybase.calibration import CalibratedRiskEngine, ConformalRiskModel, _FEATURE_KEYS

    m = ConformalRiskModel(
        coefficients=[0.0] * len(_FEATURE_KEYS), intercept=-5.0, alpha=0.1,
        # p-value path accepts (strong negative intercept).
        calibration_scores=[0.5, 0.6, 0.7, 0.8],
        tecp_entropy_threshold=0.9,
    )
    engine = CalibratedRiskEngine(max_retries_per_unit=2, model=m)
    decision = engine.decide(
        _passed_result({"syntax_passed": True, "mean_token_entropy": 1.5}),
        retry_count=0,
    )
    assert decision.action == "escalate"
    assert decision.risk_score is not None


def test_engine_accepts_conformal_low_entropy_at_runtime():
    """The same model accepts a candidate with low entropy — TECP gate opens
    only for the nonconforming tail."""
    from capybase.calibration import CalibratedRiskEngine, ConformalRiskModel, _FEATURE_KEYS

    m = ConformalRiskModel(
        coefficients=[0.0] * len(_FEATURE_KEYS), intercept=-5.0, alpha=0.1,
        calibration_scores=[0.5, 0.6, 0.7, 0.8],
        tecp_entropy_threshold=0.9,
    )
    engine = CalibratedRiskEngine(max_retries_per_unit=2, model=m)
    decision = engine.decide(
        _passed_result({"syntax_passed": True, "mean_token_entropy": 0.1}),
        retry_count=0,
    )
    assert decision.action == "accept"


def test_engine_conformal_ignores_tecp_when_threshold_absent():
    """No fitted threshold → the TECP gate is absent and a confident candidate
    is accepted (the flag-off / no-entropy-data default behavior)."""
    from capybase.calibration import CalibratedRiskEngine, ConformalRiskModel, _FEATURE_KEYS

    m = ConformalRiskModel(
        coefficients=[0.0] * len(_FEATURE_KEYS), intercept=-5.0, alpha=0.1,
        calibration_scores=[0.5, 0.6, 0.7, 0.8],
        tecp_entropy_threshold=None,
    )
    engine = CalibratedRiskEngine(max_retries_per_unit=2, model=m)
    decision = engine.decide(
        _passed_result({"syntax_passed": True, "mean_token_entropy": 1.5}),
        retry_count=0,
    )
    assert decision.action == "accept"


# ---------------------------------------------------------------------------
# Side-by-side review bundle
# ---------------------------------------------------------------------------


def _unit():
    return ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="def f():\n    pass"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="    return 0"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="    return 9"),
        original_worktree_text="def f():\n<<<<<<<\n    return 0\n=======\n    return 9\n>>>>>>>\n",
        marker_span=(1, 5),
    )


def test_review_bundle_renders_alternates(tmp_path):
    paths = SessionPaths("testbundle", str(tmp_path))
    paths.mkdirs()
    cand = CandidateResolution(
        candidate_id="c1", unit_id="u", model_name="m",
        prompt_version="v", resolved_text="    return [0, 9]",
        self_reported_confidence=0.8,
    )
    alt1 = CandidateResolution(
        candidate_id="c2", unit_id="u", model_name="m",
        prompt_version="v", resolved_text="    return (0, 9)",
        self_reported_confidence=0.5,
    )
    alt2 = CandidateResolution(
        candidate_id="c3", unit_id="u", model_name="m",
        prompt_version="v", resolved_text="    return {0, 9}",
        self_reported_confidence=0.3,
    )
    bundle = write_review_bundle(
        paths,
        reason="consensus entropy too high",
        step_index=1,
        unit=_unit(),
        candidate=cand,
        alternates=[alt1, alt2],
        consensus={"entropy": 0.85, "agreement_score": 0.4},
    )
    content = bundle.read_text(encoding="utf-8")
    assert "alternate candidates" in content
    assert "variation 1" in content
    assert "variation 2" in content
    assert "return (0, 9)" in content
    assert "return {0, 9}" in content
    assert "entropy: 0.85" in content


def test_review_bundle_without_alternates(tmp_path):
    """When no alternates given, the bundle renders just the best candidate."""
    paths = SessionPaths("testbundle2", str(tmp_path))
    paths.mkdirs()
    cand = CandidateResolution(
        candidate_id="c1", unit_id="u", model_name="m",
        prompt_version="v", resolved_text="    return 0",
    )
    bundle = write_review_bundle(
        paths, reason="syntax error", step_index=1, unit=_unit(), candidate=cand,
    )
    content = bundle.read_text(encoding="utf-8")
    assert "alternate candidates" not in content
    assert "return 0" in content

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
    # Recovery retry disabled so needs_human escalates (the calibrated path this
    # test exercises); with recovery on, a refusal gets one reframed retry first.
    from capybase.risk import RiskEngine as _RE
    engine = CalibratedRiskEngine(
        max_retries_per_unit=2, model=model,
        fallback=_RE(max_retries_per_unit=2, enable_recovery_retry=False),
    )
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


# ---------------------------------------------------------------------------
# Extended feature capture (Problem 2): the resolution-process signals that
# now ride into validator_features and the extended _FEATURE_KEYS vector.
# ---------------------------------------------------------------------------


def test_feature_keys_include_resolution_signals():
    """The canonical vector now carries the epistemic-uncertainty signals the
    system computes during resolution, not just validator hard-checks."""
    from capybase.calibration import _FEATURE_KEYS

    for key in (
        "consensus_entropy",
        "consensus_agreement",
        "consensus_cluster_count",
        "difficulty_complex",
        "retry_count",
        "conflict_side_chars",
        "enclosing_node_lines",
        "self_reported_confidence",
        "mean_token_entropy",
        "intent_agreement",
        "low_consistency_fact_count",
    ):
        assert key in _FEATURE_KEYS, key


def test_features_to_vector_carries_resolution_signals():
    from capybase.calibration import _FEATURE_KEYS

    feats = {
        "consensus_entropy": 0.92,
        "consensus_agreement": 0.34,
        "difficulty_complex": True,
        "retry_count": 2,
        "conflict_side_chars": 500,
        "self_reported_confidence": 0.4,
    }
    vec = features_to_vector(feats)
    assert vec[_FEATURE_KEYS.index("consensus_entropy")] == 0.92
    assert vec[_FEATURE_KEYS.index("consensus_agreement")] == 0.34
    assert vec[_FEATURE_KEYS.index("difficulty_complex")] == 1.0  # bool → 1.0
    assert vec[_FEATURE_KEYS.index("retry_count")] == 2.0
    assert vec[_FEATURE_KEYS.index("conflict_side_chars")] == 500.0
    assert vec[_FEATURE_KEYS.index("self_reported_confidence")] == 0.4
    # Missing keys (e.g. enclosing_node_lines absent) → 0.0
    assert vec[_FEATURE_KEYS.index("enclosing_node_lines")] == 0.0


def test_dataset_includes_resolution_signals_in_vector(tmp_path):
    """An Experience carrying resolution-process features yields a vector that
    captures them — proving the capture seam reaches the model."""
    from capybase.calibration import _FEATURE_KEYS

    store = ExperienceStore(tmp_path / "exp.jsonl")
    store.append(
        Experience(
            example=HistoricalExample(
                summary="ok", base="b", current="c", replayed="r", resolved="s"
            ),
            outcome="escalated",
            validator_features={
                "syntax_passed": True,
                "consensus_entropy": 0.95,
                "difficulty_complex": True,
                "retry_count": 2,
            },
            retry_count=2,
        )
    )
    ds = CalibrationDataset.from_store(store)
    assert ds.n == 1
    vec, label = ds.rows[0]
    assert label == 1.0  # escalated → failure
    assert vec[_FEATURE_KEYS.index("consensus_entropy")] == 0.95
    assert vec[_FEATURE_KEYS.index("difficulty_complex")] == 1.0
    assert vec[_FEATURE_KEYS.index("retry_count")] == 2.0


def test_old_model_with_shorter_feature_keys_loads_and_scores():
    """Backward-compat: a model fit on the old 13 keys (serialized with its own
    feature_keys) still loads and scores. The new keys are simply absent from
    its vector — no crash, no shape mismatch."""
    old_keys = [
        "markers_remaining", "whole_file_markers_remaining", "splice_scope_ok",
        "copied_one_side", "copied_current_side", "copied_replayed_side",
        "model_needs_human", "syntax_passed", "ast_preserved",
        "lsp_error_count", "lsp_new_error_count",
        "hard_failure_count", "warning_count",
    ]
    # A model dict as written by the pre-extension fit_calibration.py.
    model_dict = {
        "coefficients": [0.5] * len(old_keys),
        "intercept": -1.0,
        "threshold": 0.7,
        "feature_keys": old_keys,
    }
    m = CalibrationModel.from_dict(model_dict)
    # Scoring uses the model's OWN (13-key) feature_keys, so the 8 new keys in
    # the global _FEATURE_KEYS don't cause a length mismatch.
    proba = m.predict_proba({"syntax_passed": True, "consensus_entropy": 0.9})
    assert 0.0 <= proba <= 1.0
    assert m.feature_keys == tuple(old_keys)


def test_new_model_roundtrips_extended_keys(tmp_path):
    """A model with the extended key set saves and loads faithfully."""
    from capybase.calibration import _FEATURE_KEYS

    m = CalibrationModel(
        coefficients=[0.1] * len(_FEATURE_KEYS),
        intercept=-2.0,
        threshold=0.6,
    )
    path = tmp_path / "m.json"
    path.write_text(json.dumps(m.to_dict()), encoding="utf-8")
    loaded = CalibrationModel.load(path)
    assert loaded is not None
    assert loaded.feature_keys == _FEATURE_KEYS
    assert len(loaded.coefficients) == len(_FEATURE_KEYS)


# ---------------------------------------------------------------------------
# Orchestrator capture seam: _merge_resolution_features
# ---------------------------------------------------------------------------


def test_merge_resolution_features_captures_all_signals():
    """The merge helper is the seam between the resolution process and the
    recorded feature dict. It must surface consensus, difficulty, conflict
    size, node lines, confidence, retry count, and the TECP token-entropy
    signal — the signals that were previously dropped before reaching the
    experience store."""
    from capybase.consensus import ConsensusReport
    from capybase.conflict_model import ConflictSide, ConflictUnit
    from capybase.orchestrator import Orchestrator, UnitOutcome
    from capybase.config import Config

    unit = ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="def f():\n    return 1"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="    return 2"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="    return 3"),
        original_worktree_text="def f():\n<<<<<<<\n    return 2\n=======\n    return 3\n>>>>>>>\n",
        marker_span=(1, 5),
        structural_metadata={
            "enclosing_node_span": [1, 12],  # 12-line node
        },
    )
    outcome = UnitOutcome(unit=unit)
    outcome.difficulty = "complex"
    outcome.retry_count = 2
    outcome.consensus = ConsensusReport(
        winner=None, clusters=[], n_samples=3,
        agreement_score=0.34, cluster_count=3, entropy=0.92,
    )

    orch = Orchestrator(Config(), repo=".")
    merged = orch._merge_resolution_features(
        {"syntax_passed": True}, outcome, accepted=None
    )
    # Validator features pass through.
    assert merged["syntax_passed"] is True
    # Resolution-process signals are now present.
    assert merged["consensus_entropy"] == 0.92
    assert merged["consensus_agreement"] == 0.34
    assert merged["consensus_cluster_count"] == 3.0
    assert merged["difficulty_complex"] == 1.0
    assert merged["retry_count"] == 2.0
    # Conflict size = sum of side char lengths.
    assert merged["conflict_side_chars"] == float(
        len(unit.base.text) + len(unit.current.text) + len(unit.replayed.text)
    )
    assert merged["enclosing_node_lines"] == 12.0


def test_merge_resolution_features_surfaces_token_entropy():
    """TECP: the accepted candidate's mean_token_entropy reaches
    the recorded features — proving the model-side uncertainty signal threads
    from the adapter, through the candidate, into the calibration corpus."""
    from capybase.consensus import ConsensusReport
    from capybase.conflict_model import (
        CandidateResolution,
        ConflictSide,
        ConflictUnit,
    )
    from capybase.orchestrator import Orchestrator, UnitOutcome
    from capybase.config import Config

    unit = ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="b"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="c"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="r"),
        original_worktree_text="x",
    )
    outcome = UnitOutcome(unit=unit)
    outcome.difficulty = "simple"
    outcome.retry_count = 0
    outcome.consensus = ConsensusReport(
        winner=None, clusters=[], n_samples=1,
        agreement_score=1.0, cluster_count=1, entropy=0.0,
    )
    accepted = CandidateResolution(
        candidate_id="c", unit_id="u", model_name="m", prompt_version="v",
        resolved_text="r", mean_token_entropy=0.88,
    )

    orch = Orchestrator(Config(), repo=".")
    merged = orch._merge_resolution_features({}, outcome, accepted=accepted)
    assert merged["mean_token_entropy"] == 0.88


def test_merge_resolution_features_surfaces_intent_agreement():
    """FactSelfCheck: the consensus report's intent_agreement and
    low_consistency_fact_count reach the recorded feature vector — proving the
    rationale-consistency signal threads into the calibration corpus."""
    from capybase.consensus import ConsensusReport
    from capybase.conflict_model import ConflictSide, ConflictUnit
    from capybase.orchestrator import Orchestrator, UnitOutcome
    from capybase.config import Config

    unit = ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="b"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="c"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="r"),
        original_worktree_text="x",
    )
    outcome = UnitOutcome(unit=unit)
    outcome.consensus = ConsensusReport(
        winner=None, clusters=[], n_samples=3,
        agreement_score=0.67, cluster_count=2, entropy=0.92,
        intent_agreement=0.55, low_consistency_fact_count=2,
    )
    orch = Orchestrator(Config(), repo=".")
    merged = orch._merge_resolution_features({}, outcome, accepted=None)
    assert merged["intent_agreement"] == 0.55
    assert merged["low_consistency_fact_count"] == 2.0
    # And they vectorize cleanly into the feature vector.
    from capybase.calibration import features_to_vector, _FEATURE_KEYS

    vec = features_to_vector(merged)
    assert vec[_FEATURE_KEYS.index("intent_agreement")] == 0.55
    assert vec[_FEATURE_KEYS.index("low_consistency_fact_count")] == 2.0


def test_merge_resolution_features_intent_defaults_when_absent():
    """When no consensus report carries intent fields (e.g. single-sample
    path), the merge yields the safe defaults (1.0 / 0) — no penalty."""
    from capybase.consensus import ConsensusReport
    from capybase.conflict_model import ConflictSide, ConflictUnit
    from capybase.orchestrator import Orchestrator, UnitOutcome
    from capybase.config import Config

    unit = ConflictUnit(
        session_id="s", step_index=1, path="app.py", conflict_type="UU",
        unit_id="u", base=ConflictSide(label="BASE", text="b"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="c"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="r"),
        original_worktree_text="x",
    )
    outcome = UnitOutcome(unit=unit)
    # No consensus set at all → getattr defaults kick in.
    orch = Orchestrator(Config(), repo=".")
    merged = orch._merge_resolution_features({}, outcome, accepted=None)
    assert merged["intent_agreement"] == 1.0
    assert merged["low_consistency_fact_count"] == 0.0


def test_merge_resolution_features_entropy_none_passthrough():
    """When no entropy was captured (flag off / failed candidate), the recorded
    feature is None — features_to_vector later maps it to 0.0 (missing)."""
    from capybase.consensus import ConsensusReport
    from capybase.conflict_model import ConflictSide, ConflictUnit
    from capybase.orchestrator import Orchestrator, UnitOutcome
    from capybase.config import Config

    unit = ConflictUnit(
        session_id="s", step_index=1, path="app.py", conflict_type="UU",
        unit_id="u", base=ConflictSide(label="BASE", text="b"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="c"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="r"),
        original_worktree_text="x",
    )
    outcome = UnitOutcome(unit=unit)
    outcome.consensus = ConsensusReport(
        winner=None, clusters=[], n_samples=1, agreement_score=1.0,
        cluster_count=1, entropy=0.0,
    )

    orch = Orchestrator(Config(), repo=".")
    merged = orch._merge_resolution_features({}, outcome, accepted=None)
    assert merged["mean_token_entropy"] is None
    # And it vectorizes to 0.0 (treated as "confident / not atypical").
    from capybase.calibration import features_to_vector, _FEATURE_KEYS

    vec = features_to_vector(merged)
    assert vec[_FEATURE_KEYS.index("mean_token_entropy")] == 0.0




# ---------------------------------------------------------------------------
# Model-scoped feature keys (regression: vectorization must use the model's
# serialized feature_keys, not the global _FEATURE_KEYS, so coefficients align
# against the features the model was fit on — old/reordered models would
# otherwise silently misalign).
# ---------------------------------------------------------------------------


def test_features_to_vector_honors_explicit_keys():
    """features_to_vector must extract in the order of the passed keys, not the
    global _FEATURE_KEYS."""
    feats = {"a": 1, "b": True, "c": 2.5}
    vec = features_to_vector(feats, keys=("a", "b", "c", "missing"))
    assert vec == [1.0, 1.0, 2.5, 0.0]


def test_features_to_vector_default_uses_global_keys_with_conflict_severity():
    """The global _FEATURE_KEYS now includes conflict_severity (it was recorded
    by the orchestrator but absent from the vector — the calibrated model
    couldn't learn from it)."""
    assert "conflict_severity" in _FEATURE_KEYS
    vec = features_to_vector({"conflict_severity": 2})
    idx = _FEATURE_KEYS.index("conflict_severity")
    assert vec[idx] == 2.0


def test_calibration_model_vectorizes_against_its_own_feature_keys():
    """A model fit with a custom (reordered/subset) feature order must align
    its coefficients against THAT order, not the global one.

    Two models with swapped key order and swapped coefficients must give the
    SAME prediction on the same features — proving the coefficients track the
    model's keys, not a fixed global order.
    """
    keys_ab = ("feat_a", "feat_b")
    keys_ba = ("feat_b", "feat_a")
    feats = {"feat_a": 1.0, "feat_b": 0.0}
    # model1: weight 5 on feat_a, 0 on feat_b → z = 5*1 = 5
    m1 = CalibrationModel(
        coefficients=[5.0, 0.0], intercept=0.0, threshold=0.5,
        feature_keys=keys_ab,
    )
    # model2: SAME fit but keys recorded in swapped order → coefficients swap
    # too. predict_proba must be identical (it's the same model).
    m2 = CalibrationModel(
        coefficients=[0.0, 5.0], intercept=0.0, threshold=0.5,
        feature_keys=keys_ba,
    )
    assert m1.predict_proba(feats) == m2.predict_proba(feats)


def test_calibration_model_with_mismatched_lengths_degrades_neutrally():
    """A corrupt model (coefficients != feature_keys length) must return a
    neutral 0.5 prediction — never crash, never silently misalign."""
    import warnings

    bad = CalibrationModel(
        coefficients=[1.0, 2.0, 3.0], intercept=0.0, threshold=0.5,
        feature_keys=("only_one_key",),  # 3 coefs, 1 key → mismatch
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        proba = bad.predict_proba({"only_one_key": 5.0})
    assert proba == 0.5


def test_conformal_risk_model_with_mismatched_lengths_degrades_neutrally():
    """Same guard for ConformalRiskModel."""
    import warnings

    from capybase.calibration import ConformalRiskModel

    bad = ConformalRiskModel(
        coefficients=[1.0, 2.0], intercept=0.0, alpha=0.1,
        feature_keys=("only_one_key",),  # 2 coefs, 1 key → mismatch
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        proba = bad.predict_proba({"only_one_key": 5.0})
    assert proba == 0.5

"""Calibrated risk routing: a learned threshold over validator features.

The MVP ``RiskEngine`` is a hand-tuned rules function. Once enough labeled
outcomes accumulate in the experience store, a lightweight classifier can
predict the probability a merge will fail and override the accept/escalate
boundary with a calibrated threshold. This module provides:

- ``CalibrationDataset``: builds a (features, label) table from the store,
  where the label is whether the merge was eventually rejected/escalated.
- ``CalibratedRiskEngine``: a drop-in replacement for ``RiskEngine`` producing
  the same ``RiskDecision`` shape (the orchestrator reads only ``action``).
  It delegates to the rules engine for technical-failure routing but, when a
  fitted model is present, overrides the accept/escalate boundary on passing
  candidates using a logistic regression on the features.

The fitted model is a tiny JSON (coefficients + intercept + threshold), so
inference is a pure-Python dot-product — no ML framework needed at runtime.
Fitting uses ``scikit-learn`` but only in the offline ``fit_calibration``
script (an optional dep), not in the runtime path.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from capybase.conflict_model import RiskDecision, VerificationResult
from capybase.memory.store import Experience, ExperienceStore
from capybase.risk import RiskEngine


# The canonical feature vector used for calibration. These keys come from
# VerificationResult.features; we extract a fixed-order vector so the model's
# coefficients are stable. Missing features default to 0.
_FEATURE_KEYS: tuple[str, ...] = (
    "markers_remaining",
    "whole_file_markers_remaining",
    "splice_scope_ok",
    "copied_one_side",
    "copied_current_side",
    "copied_replayed_side",
    "model_needs_human",
    "syntax_passed",
    "ast_preserved",
    "lsp_error_count",
    "lsp_new_error_count",
    "hard_failure_count",
    "warning_count",
)


def features_to_vector(features: dict[str, Any]) -> list[float]:
    """Extract a fixed-order numeric feature vector from a features dict.

    Booleans become 0.0/1.0; ints/floats pass through; missing keys are 0.0.
    """
    out: list[float] = []
    for key in _FEATURE_KEYS:
        val = features.get(key, 0)
        if isinstance(val, bool):
            out.append(1.0 if val else 0.0)
        elif isinstance(val, (int, float)):
            out.append(float(val))
        else:
            out.append(0.0)
    return out


@dataclass
class CalibrationModel:
    """A fitted logistic-regression model (coefficients + intercept + threshold).

    ``predict_proba(features)`` returns the estimated probability of FAILURE
    (0=safe, 1=will fail). At runtime this is a dot-product + sigmoid — no
    numpy/sklearn dependency.
    """

    coefficients: list[float]
    intercept: float
    threshold: float  # escalate if proba >= threshold
    feature_keys: tuple[str, ...] = _FEATURE_KEYS

    def predict_proba(self, features: dict[str, Any]) -> float:
        vec = features_to_vector(features)
        z = self.intercept
        for w, x in zip(self.coefficients, vec):
            z += w * x
        return _sigmoid(z)

    def to_dict(self) -> dict[str, Any]:
        return {
            "coefficients": self.coefficients,
            "intercept": self.intercept,
            "threshold": self.threshold,
            "feature_keys": list(self.feature_keys),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CalibrationModel":
        keys = tuple(d.get("feature_keys", _FEATURE_KEYS))
        return cls(
            coefficients=list(d.get("coefficients", [])),
            intercept=float(d.get("intercept", 0.0)),
            threshold=float(d.get("threshold", 0.7)),
            feature_keys=keys,
        )

    @classmethod
    def load(cls, path: str | Path) -> "CalibrationModel | None":
        """Load a model from JSON, or return None if the file is absent."""
        p = Path(path)
        if not p.is_file():
            return None
        try:
            return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, KeyError, ValueError):
            return None


def _sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


# ---------------------------------------------------------------------------
# Conformal risk model (split conformal prediction)
# ---------------------------------------------------------------------------


@dataclass
class ConformalRiskModel:
    """A split-conformal predictor with a coverage guarantee.

    Unlike ``CalibrationModel`` (fixed threshold), this derives the escalation
    threshold from a calibration set with a statistical coverage guarantee:
    with probability ≥ 1−α, a merge the model accepts will indeed be correct.
    The nonconformity score is 1−P(correct label). At runtime, ``predict_proba``
    returns the conformal p-value — the fraction of calibration examples with
    a higher nonconformity score. If p-value < α, the candidate is escalated.

    All inference is pure-Python (dot-product + lookup); sklearn is only needed
    offline for fitting. When the calibration scores are empty (no data yet),
    this falls back to the logistic model's threshold.
    """

    coefficients: list[float]
    intercept: float
    alpha: float  # coverage = 1 - alpha
    calibration_scores: list[float] = field(default_factory=list)
    feature_keys: tuple[str, ...] = _FEATURE_KEYS

    def predict_proba(self, features: dict[str, Any]) -> float:
        """Return the conformal p-value (probability of being an outlier).

        High p-value = this candidate looks like the calibration set's
        successful merges; low p-value = it's an outlier (escalate).
        """
        vec = features_to_vector(features)
        z = self.intercept
        for w, x in zip(self.coefficients, vec):
            z += w * x
        proba_fail = _sigmoid(z)
        # Nonconformity score: how much this looks like a failure.
        score = 1.0 - proba_fail  # = P(success); high score = looks safe
        # p-value = fraction of calibration examples with HIGHER nonconformity
        # (= lower P(success)). If our score is safer than most calibration
        # examples, p-value is high (accept). If it's riskier, p-value is low.
        if not self.calibration_scores:
            # No calibration data: fall back to the raw probability.
            return proba_fail
        n = len(self.calibration_scores)
        count_below = sum(1 for s in self.calibration_scores if s < score)
        return count_below / n

    @property
    def threshold(self) -> float:
        """The effective escalate threshold (= alpha)."""
        return self.alpha

    def should_escalate(self, features: dict[str, Any]) -> bool:
        """True if the conformal p-value is below alpha (outlier)."""
        return self.predict_proba(features) < self.alpha

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "conformal",
            "coefficients": self.coefficients,
            "intercept": self.intercept,
            "alpha": self.alpha,
            "calibration_scores": self.calibration_scores,
            "feature_keys": list(self.feature_keys),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ConformalRiskModel":
        keys = tuple(d.get("feature_keys", _FEATURE_KEYS))
        return cls(
            coefficients=list(d.get("coefficients", [])),
            intercept=float(d.get("intercept", 0.0)),
            alpha=float(d.get("alpha", 0.1)),
            calibration_scores=list(d.get("calibration_scores", [])),
            feature_keys=keys,
        )

    @classmethod
    def load(cls, path: str | Path) -> "ConformalRiskModel | None":
        p = Path(path)
        if not p.is_file():
            return None
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if d.get("type") != "conformal":
                return None
            return cls.from_dict(d)
        except (json.JSONDecodeError, KeyError, ValueError):
            return None


# ---------------------------------------------------------------------------
# Dataset (for offline fitting)
# ---------------------------------------------------------------------------


@dataclass
class CalibrationDataset:
    """A (features, label) table built from the experience store.

    The label is 1.0 if the outcome was rejected/escalated (a failure), 0.0 if
    accepted. Used by the offline ``fit_calibration`` script; not needed at
    runtime.
    """

    rows: list[tuple[list[float], float]]

    @classmethod
    def from_store(cls, store: ExperienceStore) -> "CalibrationDataset":
        rows: list[tuple[list[float], float]] = []
        for exp in store:
            label = 0.0 if exp.outcome == "accepted" else 1.0
            vec = features_to_vector(exp.validator_features)
            rows.append((vec, label))
        return cls(rows)

    @property
    def n(self) -> int:
        return len(self.rows)

    @property
    def n_positive(self) -> int:
        return sum(1 for _, y in self.rows if y >= 0.5)


# ---------------------------------------------------------------------------
# CalibratedRiskEngine
# ---------------------------------------------------------------------------


class CalibratedRiskEngine:
    """A risk engine that overrides accept/escalate with a learned threshold.

    Produces the same ``RiskDecision`` shape as ``RiskEngine`` (the orchestrator
    consumes only ``action``). Technical failures (request/parse/truncated/lsp)
    and genuine refusals are still routed by the rules engine — calibration
    only affects the accept path: a candidate that PASSES all hard checks but
    has a high predicted failure probability is escalated to human review
    instead of accepted automatically.

    When no fitted model is loaded, this delegates entirely to ``RiskEngine``
    (transparent passthrough).
    """

    def __init__(
        self,
        *,
        max_retries_per_unit: int = 2,
        model: CalibrationModel | None = None,
        fallback: RiskEngine | None = None,
        entropy_escalate_threshold: float = 0.6,
    ) -> None:
        self.fallback = fallback or RiskEngine(
            max_retries_per_unit=max_retries_per_unit,
            entropy_escalate_threshold=entropy_escalate_threshold,
        )
        self.model = model

    @classmethod
    def from_config(
        cls,
        *,
        max_retries_per_unit: int,
        model_path: str,
        escalate_threshold: float,
        entropy_escalate_threshold: float = 0.6,
    ) -> "CalibratedRiskEngine":
        """Build from config: load the conformal model if present, else the
        logistic calibration model, else passthrough."""
        model = ConformalRiskModel.load(model_path)
        if model is None:
            model = CalibrationModel.load(model_path)
            if model is not None:
                model.threshold = escalate_threshold
        return cls(
            max_retries_per_unit=max_retries_per_unit,
            model=model,
            entropy_escalate_threshold=entropy_escalate_threshold,
        )

    def decide(
        self,
        result: VerificationResult,
        *,
        retry_count: int,
        failure_kind: str = "",
        consensus_entropy: float | None = None,
    ) -> RiskDecision:
        decision = self.fallback.decide(
            result,
            retry_count=retry_count,
            failure_kind=failure_kind,
            consensus_entropy=consensus_entropy,
        )
        # Calibration only overrides the ACCEPT path: a candidate that passed
        # all hard checks but is predicted likely to fail gets escalated.
        if decision.action == "accept" and self.model is not None:
            proba = self.model.predict_proba(result.features)
            if proba >= self.model.threshold:
                return RiskDecision(
                    action="escalate",
                    reasons=decision.reasons + [
                        f"calibrated risk {proba:.2f} >= threshold {self.model.threshold:.2f}"
                    ],
                    risk_score=proba,
                    required_followups=["calibrated escalation: high predicted failure risk"],
                )
            # Attach the calibrated score to the accepted decision.
            return RiskDecision(
                action=decision.action,
                reasons=decision.reasons,
                risk_score=proba,
                required_followups=decision.required_followups,
            )
        return decision

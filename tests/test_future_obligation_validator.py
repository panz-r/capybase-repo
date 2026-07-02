"""Tests for the FutureObligationValidator (#idea 7 — obligations as evidence).

A candidate that fails future obligations now flows through the verification
pipeline like any other validator: it produces a warning named "future_obligation"
+ feature keys (future_obligation_count etc.) that reach risk, accept reports,
dry-run, and calibration uniformly. The inline orchestrator gate is removed.
"""

from __future__ import annotations

from capybase.conflict_model import (
    CandidateResolution,
    ConflictSide,
    ConflictUnit,
)
from capybase.verification import (
    FutureObligationValidator,
    ValidationConfig,
    VerificationEngine,
)


def _unit(base="def helper():\n    return 1\n", current="def helper():\n    return 2\n",
          replayed="def helper():\n    return 3\n"):
    return ConflictUnit(
        session_id="s", step_index=0, path="cfg.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text=base, marker_span=(0, 0),
    )


def _cand(resolved):
    return CandidateResolution(
        candidate_id="u:c", unit_id="u", model_name="m",
        prompt_version="resolve_text_block.v5", resolved_text=resolved,
        provenance="plain_llm",
    )


def _obligations():
    """Build a FutureObligations requiring helper to survive."""
    from capybase.future_obligations import FutureObligation, FutureObligations
    return FutureObligations(obligations=[
        FutureObligation(kind="symbol_survival", symbol="helper",
                         commit_subject="use helper", required=True),
    ])


def _engine():
    return VerificationEngine.default(ValidationConfig(), extra_validators=[])


# ---------------------------------------------------------------------------
# the validator
# ---------------------------------------------------------------------------


def test_no_obligations_is_a_pass():
    """When no obligations are set, the validator passes (no-op)."""
    v = FutureObligationValidator()
    v.set_obligations(None)
    # Verify against any candidate — should pass with zero counts.
    from types import SimpleNamespace
    ctx = SimpleNamespace(
        unit=_unit(), candidate=_cand("def helper():\n    return 2\n"),
        config=ValidationConfig(),
    )
    result = v.verify(ctx)
    assert result.passed
    assert result.features["future_obligation_count"] == 0


def test_candidate_keeping_symbol_passes():
    """A candidate that keeps helper passes the future-obligation check."""
    v = FutureObligationValidator()
    v.set_obligations(_obligations())
    from types import SimpleNamespace
    ctx = SimpleNamespace(
        unit=_unit(), candidate=_cand("def helper():\n    return 2\n"),
        config=ValidationConfig(),
    )
    result = v.verify(ctx)
    assert result.passed
    assert result.features["future_obligation_count"] == 1
    assert result.features["future_obligation_dropped_count"] == 0


def test_candidate_dropping_symbol_warns_with_features():
    """A candidate that drops helper produces a warning + the feature keys."""
    v = FutureObligationValidator()
    v.set_obligations(_obligations())
    from types import SimpleNamespace
    ctx = SimpleNamespace(
        unit=_unit(), candidate=_cand("# helper removed\npass\n"),
        config=ValidationConfig(),
    )
    result = v.verify(ctx)
    assert not result.passed
    assert result.severity == "warning"
    assert result.name == "future_obligation"
    assert result.features["future_obligation_count"] == 1
    assert result.features["future_obligation_dropped_count"] == 1
    assert "helper" in result.features["future_obligation_dropped_symbols"]


def test_validator_integrates_into_verify_pipeline():
    """The FutureObligationValidator runs as part of verify() and its warning
    reaches the VerificationResult.warnings list (so the risk engine sees it)."""
    v = FutureObligationValidator()
    v.set_obligations(_obligations())
    engine = _engine()
    engine.register(v)
    # A candidate dropping helper.
    result = engine.verify(_unit(), _cand("# helper removed\npass\n"))
    warning_names = {w.validator for w in result.warnings}
    assert "future_obligation" in warning_names
    assert result.features.get("future_obligation_dropped_count") == 1


def test_risk_engine_retries_on_future_obligation_warning():
    """The risk engine retries on the future_obligation warning (not just the
    orchestrator's removed inline gate)."""
    from capybase.risk import RiskEngine
    from capybase.conflict_model import VerificationResult, VerificationWarning

    result = VerificationResult(
        candidate_id="u:c", unit_id="u", passed=True, hard_failures=[],
        warnings=[VerificationWarning(validator="future_obligation",
                                       message="dropped helper")],
        features={"future_obligation_dropped_count": 1},
    )
    engine = RiskEngine(max_retries_per_unit=3)
    decision = engine.decide(result, retry_count=0)
    assert decision.action == "retry"


def test_calibration_sees_future_obligation_features():
    """The future_obligation feature keys are in _FEATURE_KEYS so calibration
    can learn from them."""
    from capybase.calibration import _FEATURE_KEYS
    assert "future_obligation_count" in _FEATURE_KEYS
    assert "future_obligation_dropped_count" in _FEATURE_KEYS

"""Tests for the offline fitter's TECP entropy-threshold helper.

The full fitter needs scikit-learn/numpy (offline-only, not installed in this
env), but ``_tecp_entropy_threshold`` is pure-Python and is the part the TECP
(survey §4.1) work added — so it is unit-tested directly via importlib.

The helper computes the (1-alpha) quantile of ``mean_token_entropy`` over
ACCEPTED (correct) calibration outcomes; failures (rejected/escalated) and rows
without entropy are skipped.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# scripts/ is not on the package path; load the module by file path.
_FITTER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "fit_calibration.py"
_spec = importlib.util.spec_from_file_location("fit_calibration", _FITTER_PATH)
assert _spec is not None and _spec.loader is not None
fit_calibration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fit_calibration)
_tecp = fit_calibration._tecp_entropy_threshold


class _Exp:
    """Minimal stand-in for memory.store.Experience — only the fields _tecp reads."""

    def __init__(self, outcome, mean_token_entropy):
        self.outcome = outcome
        self.validator_features = (
            {} if mean_token_entropy is None
            else {"mean_token_entropy": mean_token_entropy}
        )


def _store(exps):
    return list(exps)


def test_tecp_quantile_over_accepted_outcomes():
    # Accepted entropies: [0.1, 0.2, 0.3, 0.4, 0.5]. alpha=0.2 → 0.8 quantile.
    # ceil(0.8 * 4) = ceil(3.2) = 4 → index 4 → 0.5.
    store = _store([_Exp("accepted", v) for v in [0.1, 0.2, 0.3, 0.4, 0.5]])
    q = _tecp(store, 0.2)
    assert q == pytest.approx(0.5)


def test_tecp_ignores_failure_outcomes():
    # Only the two accepted rows count; the escalated ones are skipped entirely.
    store = _store([
        _Exp("accepted", 0.2),
        _Exp("accepted", 0.8),
        _Exp("escalated", 0.99),
        _Exp("rejected", 0.95),
    ])
    # alpha=0.1 → ceil(0.9 * 1) = ceil(0.9) = 1 → index 1 of [0.2, 0.8] → 0.8.
    q = _tecp(store, 0.1)
    assert q == pytest.approx(0.8)


def test_tecp_skips_rows_without_entropy():
    # Rows where entropy was never captured (None) don't contribute.
    store = _store([
        _Exp("accepted", 0.1),
        _Exp("accepted", None),
        _Exp("accepted", 0.9),
    ])
    # alpha=0.5 → ceil(0.5 * 1) = 1 → index 1 of [0.1, 0.9] → 0.9.
    q = _tecp(store, 0.5)
    assert q == pytest.approx(0.9)


def test_tecp_returns_none_when_no_entropy_observed():
    store = _store([
        _Exp("accepted", None),
        _Exp("escalated", 0.9),  # failure, and skipped anyway
    ])
    assert _tecp(store, 0.1) is None


def test_tecp_single_observation():
    store = _store([_Exp("accepted", 0.42)])
    # n=1 → pos = ceil((1-alpha)*0) = 0 → index 0 → 0.42.
    assert _tecp(store, 0.1) == pytest.approx(0.42)


def test_tecp_alpha_zero_takes_max():
    """alpha→0 means (1-alpha)=1.0 → the maximum accepted entropy (the
    strictest, full-coverage threshold)."""
    store = _store([_Exp("accepted", v) for v in [0.1, 0.2, 0.3, 0.4, 0.5]])
    # ceil(1.0 * 4) = 4 → index 4 → 0.5.
    assert _tecp(store, 0.0) == pytest.approx(0.5)

"""Calibrated confidence — historical per-class priors (design P3's
final piece). Priors ANNOTATE review decisions; they never flip a tier
(evidence decides — a prior alone promoting would be the
resolver-decides-safety mistake in statistical dress)."""

from __future__ import annotations

from capybase.acceptance import decide
from capybase.calibration_priors import (
    derive_priors,
    load_priors,
    prior_for,
    prior_reason,
    save_priors,
)


def test_derive_priors_excludes_denominator_classes():
    records = (
        [{"language": "python", "verdict": "PASS"}] * 70
        + [{"language": "python", "verdict": "WORKING"}] * 10
        + [{"language": "python", "verdict": "ESCALATE"}] * 20
        # excluded: no resolution outcome
        + [{"language": "python", "verdict": "ESCALATE",
            "terminal_reason": "SAFE_SKIP"}] * 5
        + [{"language": "c", "verdict": "ESCALATE"}] * 2
    )
    priors = derive_priors(records)
    # python: n=100, pass=70 — WORKING is a graded success, NOT a pass.
    assert priors["python"] == {"n": 100, "pass_rate": 0.7}
    # tiny classes are still derived (the MINIMUM applies at lookup).
    assert priors["c"]["n"] == 2


def test_prior_for_requires_meaningful_sample():
    priors = {"python": {"n": 100, "pass_rate": 0.7},
              "c": {"n": 2, "pass_rate": 0.5}}
    assert prior_for(priors, "python") == {"n": 100, "pass_rate": 0.7}
    assert prior_for(priors, "c") is None       # below the minimum (20)
    assert prior_for(priors, "rust") is None    # absent class
    assert prior_for(None, "python") is None    # priors disabled


def test_roundtrip(tmp_path):
    priors = {"python": {"n": 214, "pass_rate": 0.92}}
    save_priors(priors, tmp_path / "p" / "priors.json")
    assert load_priors(tmp_path / "p" / "priors.json") == priors
    assert load_priors(tmp_path / "absent.json") is None


class _U:
    unit_id = "u1"


class _V:
    features = {"syntax_passed": True}
    warnings = []


class _C:
    provenance = "plain_llm"
    suspected_validator_error = False


class _O:
    unit = _U(); unit_id = "u1"; validation = _V(); accepted = _C()


def test_prior_annotates_but_never_flips_tier():
    prior = {"n": 214, "pass_rate": 0.92}
    with_p = decide([_O()], True, class_prior=prior)
    without = decide([_O()], True)
    # Same tier/decision — the prior only informs the review reasons.
    assert (with_p.tier, with_p.decision) == (without.tier, without.decision)
    assert prior_reason(prior) in with_p.reasons
    assert not any("calibration" in r for r in without.reasons)

    weak = {"n": 40, "pass_rate": 0.35}
    with_weak = decide([_O()], True, class_prior=weak)
    assert (with_weak.tier, with_weak.decision) == ("B", "PROPOSE_FOR_REVIEW")
    assert prior_reason(weak) in with_weak.reasons

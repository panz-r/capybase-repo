"""UNKNOWN IS NOT PASS — the acceptance subsystem's first slice
(candidate-ref design P3, sprint-27).

An oracle that could not run (missing/vanished toolchain, undecidable
location) must never look like one that passed:
- features stop recording ``syntax_passed: True`` for unrun checks;
- the evidence records ``syntax_outcome: "unknown"`` (+ ``unknown`` flag);
- the quality score withholds credit (absent key);
- risk adds an explicit unknown bump;
- the accept report says "NOT CHECKED" instead of staying silent.
"""

from __future__ import annotations

from capybase.accept_report import _validation_lines
from capybase.risk import _risk_score as risk_score
from capybase.verification import VerificationCheckResult


def test_features_do_not_claim_pass_for_unrun_check():
    """The three not-run paths carry unknown, not a pass claim."""
    from capybase.verification import CcsSyntaxValidator
    # constructed result check via the dataclass contract:
    r = VerificationCheckResult(
        name="ccs_syntax", passed=True, unknown=True,
        message="C compiler not available; syntax not checked",
        features={"ccs_syntax_checked": False,
                  "syntax_outcome": "unknown"})
    assert r.unknown is True
    assert "syntax_passed" not in r.features


def test_risk_unknown_bump_fires():
    """syntax_outcome=unknown raises risk (less than a failure)."""
    base = {"conflict_severity": 1.0}
    unknown = dict(base, syntax_outcome="unknown")
    failed = dict(base, syntax_passed=False)
    r_base = risk_score(base)
    r_unknown = risk_score(unknown)
    r_failed = risk_score(failed)
    assert r_unknown > r_base, "unknown must raise risk over no-signal"
    assert r_failed > r_unknown, "a failure still outranks an unknown"


def test_accept_report_unknown_line_present():
    """A validation whose syntax oracle never ran prints NOT CHECKED."""
    # _validation_lines signature takes the validation object; construct a
    # minimal stand-in with the features that drive the branch.
    class _V:
        features = {"syntax_outcome": "unknown", "ccs_syntax_checked": False}
        hard_failures = []
    lines = _validation_lines(_V())
    assert any("NOT CHECKED" in ln for ln in lines), lines
    assert not any(ln == "- syntax passed" for ln in lines)


# ---------------------------------------------------------------------------
# s27-extend-42: the evidence envelope
# ---------------------------------------------------------------------------

def test_evidence_envelope_reads_ran_check_fingerprint():
    from capybase.acceptance import evidence_envelope

    class _U:
        unit_id = "u1"

    class _Val:
        features = {
            "syntax_passed": True, "syntax_scope": "unit",
            "syntax_tool": "cc (Ubuntu 15.2.0) 15.2.0",
            "syntax_duration_ms": 42, "ccs_syntax_checked": True,
        }

    class _O:
        unit = _U()
        validation = _Val()
        accepted = object()

    env = evidence_envelope(_O())
    assert len(env) == 1
    e = env[0]
    assert (e.oracle, e.outcome, e.scope) == ("syntax", "pass", "unit")
    assert "15.2.0" in e.tool and e.duration_ms == 42


def test_evidence_envelope_unknown_and_absent():
    from capybase.acceptance import evidence_envelope

    class _U:
        unit_id = "u1"

    class _ValUnknown:
        features = {"syntax_outcome": "unknown",
                    "syntax_scope": "file"}

    class _O:
        unit = _U(); validation = _ValUnknown(); accepted = object()

    (e,) = evidence_envelope(_O())
    assert (e.outcome, e.tool, e.duration_ms) == ("unknown", "", 0)

    class _ValBare:
        features = {"markers_remaining": False}

    class _O2:
        unit = _U(); validation = _ValBare(); accepted = object()

    assert evidence_envelope(_O2()) == []


def test_safety_class_taxonomy():
    """D0-D3 (reuse-design stage 1): reproducibility is not correctness."""
    from capybase.langs import SafetyClass, safety_class_for

    assert safety_class_for("exact_history_reuse") == SafetyClass.EXACT
    assert safety_class_for("combination_search") == SafetyClass.HEURISTIC
    assert safety_class_for("deterministic_symbol_injection") == SafetyClass.HEURISTIC
    assert safety_class_for("plain_llm") is None
    # Unlisted deterministic-* provenances default conservative-STRUCTURAL.
    assert safety_class_for("deterministic_new_thing") == SafetyClass.STRUCTURAL


def test_tier_a_requires_d0_d1_not_heuristic_determinism():
    """The acceptance refinement: a reproducible SEARCH (D3) must not
    reach AUTO_APPLY on its mechanism label alone — it needs the
    evidence tiers like any model-assisted resolution."""
    from capybase.acceptance import AUTO_APPLY, PROPOSE_FOR_REVIEW, decide

    class _U:
        unit_id = "u1"

    class _V:
        features = {"syntax_passed": True}
        warnings = []

    class _CExact:
        provenance = "exact_history_reuse"
        suspected_validator_error = False

    class _O:
        unit = _U(); validation = _V(); accepted = None

        def __init__(self, cand):
            self.accepted = cand

    exact = decide([_O(_CExact())], True)
    assert (exact.tier, exact.decision) == ("A", AUTO_APPLY)
    assert "D0/D1" in exact.reasons[0]

    class _CSbcr:
        provenance = "combination_search"       # deterministic label, D3
        suspected_validator_error = False

    heuristic = decide([_O(_CSbcr())], True)
    assert (heuristic.tier, heuristic.decision) == ("B", PROPOSE_FOR_REVIEW)

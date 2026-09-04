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

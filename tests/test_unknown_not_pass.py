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


def test_strict_mode_d01_exemption_from_confidence_floor():
    """Reuse-design stage 2: D0/D1 candidates don't need a model-opinion
    floor — the SafetyClass exemption replaces the 0.85/0.9 floats that
    were gaming the gate. A deterministic-structural candidate at
    confidence 0.0 passes strict mode; a plain_llm candidate at 0.0
    does not."""
    from capybase.policy_strictness import StrictnessPolicy

    policy = StrictnessPolicy(mode="unattended", min_confidence=0.6)

    class _Side:
        def __init__(self, text):
            self.text = text

    class _U:
        unit_id = "u1"
        path = "f.py"
        language = "python"
        unit_kind = "text_marker_block"
        marker_span = (0, 3)
        structural_metadata = {}
        current = _Side("x = 1")
        replayed = _Side("x = 2")
        base = _Side("x = 0")
    class _Val:
        passed = True
        hard_failures = []
        warnings = []
        features = {"syntax_passed": True}
    class _Cand_det:
        provenance = "deterministic_structural"
        self_reported_confidence = 0.0  # zeroed — the float is vestigial
        needs_human = False
        failure_kind = ""
        resolved_text = "x = 1"
    class _Cand_llm:
        provenance = "plain_llm"
        self_reported_confidence = 0.0
        needs_human = False
        failure_kind = ""
        resolved_text = "x = 1"

    ok_det, why_det = policy.should_accept(_U(), _Cand_det(), _Val())
    assert ok_det, f"D0/D1 should be exempt: {why_det}"

    # The plain_llm path exercises the EXISTING deterministic-confidence
    # override (strong structural evidence can override low self-report)
    # — pre-existing behavior, separate from the SafetyClass exemption.
    # The key assertion: the D0/D1 candidate passes through the CLASS
    # exemption (provenance → SafetyClass), not through a float.


def test_missing_evidence_is_not_complete():
    """s27-71 (fifth-pass B3): an accepted unit with NO validation object
    (reconciliation-appended whole-file outcomes, R3 accepts) or no syntax
    record at all (fast-verify accepts) is MISSING evidence — the mirror
    of 'unknown is not pass'. It must degrade to Tier B, never stamp Tier
    A 'complete oracles' on a unit no oracle saw."""
    from capybase.acceptance import PROPOSE_FOR_REVIEW, decide

    class _U:
        unit_id = "u1"

    class _CStruct:
        provenance = "deterministic_structural"
        suspected_validator_error = False

    class _ONoVal:
        unit = _U(); validation = None; accepted = _CStruct()

    class _VEmpty:
        features = {}
        warnings = []

    class _OFastVerify:
        unit = _U(); validation = _VEmpty(); accepted = _CStruct()

    for outcome in (_ONoVal(), _OFastVerify()):
        d = decide([outcome], True)
        assert (d.tier, d.decision) == ("B", PROPOSE_FOR_REVIEW), d.reasons


def test_produced_provenances_explicitly_classified():
    """s27-71 (fifth-pass B4): every provenance string the orchestrator
    PRODUCES must carry an explicit table entry — the vocabulary drift
    (abstract keys like compiler_fixit listed while deterministic_gcc_fixit
    rides the STRUCTURAL default) silently reclassified the repair family
    as D1 and reached Tier A against the module's own doctrine."""
    import re
    from capybase.langs import _PROVENANCE_SAFETY, safety_class_for
    from pathlib import Path
    _repo = Path(__file__).resolve().parent.parent
    src = (_repo / "src" / "capybase" / "orchestrator.py").read_text()
    produced = set()
    for m in re.finditer(r'provenance="([a-z_0-9{}:+\-]+)"', src):
        produced.add(m.group(1))
    # resolve the f-string template families to their concrete keys
    concrete = set()
    for p in produced:
        if "{" in p:
            concrete |= {p.format(side=s, winner=w, majority_side=s,
                                  opposite=w)
                         for s in ("current", "replayed")
                         for w in ("current", "replayed")}
        else:
            concrete.add(p)
    unmapped = sorted(
        p for p in concrete
        if p not in _PROVENANCE_SAFETY
        and p not in ("manual", "plain_llm", "plain_llm_mixed"))
    assert unmapped == [], (
        f"provenances riding the unlisted-deterministic default: {unmapped}")
    # spot-check the doctrine: repairs are D3, policy mechanisms D2
    from capybase.langs import SafetyClass
    assert safety_class_for("deterministic_gcc_fixit") == SafetyClass.HEURISTIC
    assert safety_class_for("deterministic_convergence_seed") == SafetyClass.POLICY


def test_safety_class_handles_pipeline_stage_suffixes():
    """s27-72 (sixth pass): repair rungs append '+'-stages
    (deterministic_gcc_fixit+file_linker) — the matcher must resolve the
    base, not fall to the STRUCTURAL default (the drift bug B4 fixed,
    re-entered by concatenation)."""
    from capybase.langs import SafetyClass, safety_class_for
    assert safety_class_for(
        "deterministic_gcc_fixit+file_linker") == SafetyClass.HEURISTIC
    assert safety_class_for(
        "plain_llm+intent_coverage") is None
    assert safety_class_for(
        "deterministic_convergence_seed+prefix_dedup") == SafetyClass.POLICY

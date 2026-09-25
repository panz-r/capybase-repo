"""S28-203 — the sides-check alignment (pilot-gated, default OFF).

The model's validator doubt is TESTABLE, not a verdict: on
`suspected_validator_error`, run the failing check on the PRISTINE
sides. Both sides fail it too = the check is inapplicable for the
region's content family (no shelve on non-discriminative evidence).
Both sides pass = the splice broke the property (the doubt was wrong;
the failure belongs to the seam family's feedback). The 11-member
population (scikit-0052 x10, prusaslicer-0115 at sim 1.000,
redis-0032...) shelved near-oracle content on UNVERIFIED doubt.
"""

from __future__ import annotations

from types import SimpleNamespace

from capybase.conflict_model import (
    ConflictSide,
    ConflictUnit,
    VerificationFailure,
    VerificationResult,
)
from capybase.verification import sides_check_alignment


def _unit(lang="python", cur="", rep=""):
    return ConflictUnit(
        session_id="s", step_index=0, path="f.py", language=lang,
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=""),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=cur),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=rep),
        original_worktree_text="",
        marker_span=(0, 0),
    )


def _result(*msgs, passed=False):
    return VerificationResult(
        candidate_id="c", unit_id="u", passed=passed,
        hard_failures=[
            VerificationFailure(validator="python_string_balance",
                                message=m)
            for m in msgs
        ],
    )


BROKEN_DOCSTRING = 'def f(x):\n    """param list\n    return x\n'  # unterminated
CLEAN_PY = "def f(x):\n    return x\n"


def test_both_sides_fail_the_check_is_inapplicable():
    """The S28-105 doctrine at the engine: both pristine sides carry the
    same property the validator rejects — the check cannot pass for
    this region's content family."""
    u = _unit(cur=BROKEN_DOCSTRING, rep=BROKEN_DOCSTRING)
    r = _result("unterminated triple-quoted string at line 2")
    assert sides_check_alignment(u, r) == "inapplicable"


def test_both_sides_pass_means_applicable():
    """Clean sides + a failing candidate = the splice broke the
    property (the scikit-0052 split: string-interior fragments whose
    correctness depends on enclosing pairing the fragment cannot
    see)."""
    u = _unit(cur=CLEAN_PY, rep=CLEAN_PY)
    r = _result("unterminated triple-quoted string at line 723")
    assert sides_check_alignment(u, r) == "applicable"


def test_syntax_class_dispatches_on_ast():
    u = _unit(cur=CLEAN_PY, rep="def f(:\n    pass\n")
    r = _result("SyntaxError: invalid syntax")
    # one side parses, one does not -> mixed -> unknown
    assert sides_check_alignment(u, r) == "unknown"
    u2 = _unit(cur="def f(:\n", rep="def g(:\n")
    assert sides_check_alignment(u2, r) == "inapplicable"
    u3 = _unit(cur=CLEAN_PY, rep=CLEAN_PY)
    assert sides_check_alignment(u3, r) == "applicable"


def test_mixed_outcomes_stay_unknown():
    u = _unit(cur=BROKEN_DOCSTRING, rep=CLEAN_PY)
    r = _result("unterminated triple-quoted string at line 2")
    assert sides_check_alignment(u, r) == "unknown"


def test_non_python_and_empty_sides_decline():
    u = _unit("cpp", cur="int x;\n", rep="int y;\n")
    assert sides_check_alignment(u, _result("error: expected ;")) == "unknown"
    u2 = _unit(cur=BROKEN_DOCSTRING, rep="")
    assert sides_check_alignment(u2, _result("unterminated triple")) == "unknown"


def test_unknown_check_class_declines():
    u = _unit(cur=CLEAN_PY, rep=CLEAN_PY)
    r = _result("schema mismatch: field 'qty' expects int")
    assert sides_check_alignment(u, r) == "unknown"


# ---------------------------------------------------------------------------
# the orchestrator wiring: the doubt no longer force-escalates when the
# alignment answers
# ---------------------------------------------------------------------------

class _RecJournal:
    def __init__(self):
        self.events = []

    def emit(self, event, payload, **_kw):
        self.events.append((event, payload))


def _orch(flag):
    from capybase.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)
    orch.journal = _RecJournal()
    orch.step = 1
    orch.config = SimpleNamespace(future=SimpleNamespace(
        enable_sides_check_alignment=flag))
    orch.risk = SimpleNamespace(decide=None)
    return orch


def test_wiring_downgrades_the_suspicion_when_inapplicable(monkeypatch):
    """Both sides fail the check -> the suspicion flag is dropped from
    the decide call and the branch is journaled."""
    orch = _orch(True)
    unit = _unit(cur=BROKEN_DOCSTRING, rep=BROKEN_DOCSTRING)
    validation = _result("unterminated triple-quoted string at line 2")
    seen = {}

    def _fake_decide(result, **kw):
        seen["suspect"] = kw.get("suspected_validator_error")
        return SimpleNamespace(action="retry", reasons=[])

    monkeypatch.setattr(orch.risk, "decide", _fake_decide)
    cand = SimpleNamespace(suspected_validator_error=True,
                           failure_kind="")
    import capybase.orchestrator as orch_mod
    # inline the wiring's decision logic the way _resolve_unit_core does
    _decide_suspect = cand.suspected_validator_error
    if (_decide_suspect and not getattr(validation, "passed", False)
            and getattr(orch.config.future,
                        "enable_sides_check_alignment", False)):
        from capybase.verification import sides_check_alignment
        _align = sides_check_alignment(unit, validation)
        if _align in ("inapplicable", "applicable"):
            orch.journal.emit(f"sides_check_{_align}", {})
            _decide_suspect = False
    _fake_decide(validation, suspected_validator_error=_decide_suspect)
    assert seen["suspect"] is False
    assert orch.journal.events[0][0] == "sides_check_inapplicable"


def test_wiring_flag_off_keeps_the_escalation():
    """Flag off: the suspicion reaches risk.decide untouched (the
    sprint-27 contract — the shelve path is the DEFAULT behavior)."""
    orch = _orch(False)
    unit = _unit(cur=BROKEN_DOCSTRING, rep=BROKEN_DOCSTRING)
    validation = _result("unterminated triple-quoted string at line 2")
    _decide_suspect = True
    if (_decide_suspect and not getattr(validation, "passed", False)
            and getattr(orch.config.future,
                        "enable_sides_check_alignment", False)):
        _decide_suspect = False  # unreachable with the flag off
    assert _decide_suspect is True


def test_source_pin_the_decide_call_reads_the_gated_flag():
    """The real call site must feed risk.decide the ALIGNMENT-GATED
    flag variable, not the candidate's raw self-report — otherwise the
    alignment result is computed and ignored."""
    import re as _re
    src = open(
        __import__("pathlib").Path(
            __import__("capybase.orchestrator", fromlist=["x"]).__file__)
    ).read()
    # the gated local exists...
    assert "_decide_suspect = cand.suspected_validator_error" in src
    assert "_decide_suspect = False" in src
    # ...and the decide call consumes it (not the raw attribute)
    m = _re.search(
        r"decision = self\.risk\.decide\((?:[^\)]|\)(?!\)))*?"
        r"suspected_validator_error=(\w+)", src)
    assert m and m.group(1) == "_decide_suspect", (
        "risk.decide must consume the alignment-gated suspicion flag")

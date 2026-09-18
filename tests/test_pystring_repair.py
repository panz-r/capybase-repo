"""S28-79: the python string-balance repair arm — the whole-file repair
ladder's deterministic termination of unterminated triple-quoted strings.

The census's dominant python failure signature (~60 validation hits
across the harvest's python tail) is ``unterminated (triple-quoted)
string literal``: the model merges docstring blocks and drops a closer,
the string swallows the rest of the file, and every CEGIS retry
reproduces the same imbalance (scikit-0052: 15 candidates, one
signature). Same doctrine as the C brace repair — one clean edit,
re-validated, anti-repeat table bounds attempts.
"""

from __future__ import annotations

from capybase.conflict_model import (
    CandidateResolution, ConflictSide, ConflictUnit, VerificationFailure,
)
from capybase.orchestrator import _try_deterministic_pystring_repair
from capybase.verification import (
    _py_string_imbalance, _try_close_unterminated_string,
)

_DQ = '"""'


def _unit(language="python", kind="whole_file"):
    return ConflictUnit(
        session_id="s", step_index=0, path="m.py", language=language,
        unit_id="m.py:0", unit_kind=kind,
        base=ConflictSide(label="BASE", text=""),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=""),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=""),
        original_worktree_text="", marker_span=None,
    )


def _cand(text=""):
    return CandidateResolution(
        candidate_id="c", unit_id="m.py:0", model_name="m",
        prompt_version="t", resolved_text=text,
    )


def _fail(msg):
    return VerificationFailure(validator="syntax", severity="error",
                               message=msg)


BROKEN = "def f():\n    s = " + _DQ + "doc\n    return 1\n"  # opener, no closer


def test_imbalance_scan_finds_opener_and_flavor():
    assert _py_string_imbalance(BROKEN) == (2, _DQ)
    balanced = BROKEN + _DQ + "\n"
    assert _py_string_imbalance(balanced) is None
    sq = "x = " + "'''" + "doc\n"  # single-quote-triple flavor
    assert _py_string_imbalance(sq) == (1, "'''")


def test_close_at_detected_line_and_at_eof():
    # detected line 3 ("    return 1") -> closer inserted before it
    fixed = _try_close_unterminated_string(BROKEN, detected_line=3)
    assert fixed is not None
    lines = fixed.splitlines()
    assert lines[2] == _DQ
    assert _py_string_imbalance(fixed) is None
    # detected line past EOF -> closer appended
    fixed2 = _try_close_unterminated_string(BROKEN, detected_line=99)
    assert fixed2 is not None and fixed2.splitlines()[-1] == _DQ
    # balanced text declines
    assert _try_close_unterminated_string("x = 1\n") is None


def test_repair_arm_produces_whole_file_candidate():
    unit = _unit()
    failures = [_fail(
        'SyntaxError: unterminated triple-quoted string literal '
        '(detected at line 3)')]
    det, diag = _try_deterministic_pystring_repair(
        failures, BROKEN, [(unit, _cand(BROKEN))], 0)
    assert diag == "repaired"
    (wu, wc), = det
    assert wu.unit_kind == "whole_file"
    assert wc.provenance == "deterministic_pystring_repair"
    assert _py_string_imbalance(wc.resolved_text) is None
    assert '"""' in wc.resolved_text


def test_repair_arm_declines_outside_its_shape():
    unit = _unit()
    # non-matching failure kind
    det, diag = _try_deterministic_pystring_repair(
        [_fail("IndentationError: expected an indented block")],
        BROKEN, [(unit, _cand(BROKEN))], 0)
    assert det is None and diag == "not_string_failure"
    # non-python language
    det, diag = _try_deterministic_pystring_repair(
        [_fail("unterminated string literal (detected at line 2)")],
        BROKEN, [(_unit(language="cpp"), _cand(BROKEN))], 0)
    assert det is None and diag == "not_python"
    # balanced buffer
    ok = BROKEN + _DQ + "\n"
    det, diag = _try_deterministic_pystring_repair(
        [_fail("unterminated string literal (detected at line 2)")],
        ok, [(unit, _cand(ok))], 0)
    assert det is None and diag == "no_imbalance"
    # rust string content in a python file is still python: the arm runs
    det, diag = _try_deterministic_pystring_repair(
        [_fail("unterminated string literal (detected at line 2)")],
        BROKEN, [(unit, _cand(BROKEN))], 0)
    assert diag == "repaired"

"""S28-365 D2 — the python literal-family rungs, built from the DATA.

The design pass pulled the scikit family's REAL failure texts (26
throttled events): the shapes are the MISMATCHED closer (closing
parenthesis does not match opening square bracket) and the
UNTERMINATED triple-quote — NOT the unmatched-closer message the D2
predecessor awaited (which is why three trials never fired it). The
rungs: one splice at the named position, full re-gate, conservative
declines.
"""

from __future__ import annotations

from capybase.orchestrator import (
    _python_mismatch_closer_fix,
    _python_triple_quote_fix,
)


def test_mismatch_closer_substitutes_the_right_char():
    assert _python_mismatch_closer_fix("a = [1, 2)\n", 1) == "a = [1, 2]\n"


def test_mismatch_closer_scans_the_bracket_stack():
    # the func-closer case: the mismatch is the FIRST ")" (it closes the
    # "["), not the line's rightmost paren
    assert _python_mismatch_closer_fix(
        "x = func([1, 2))\n", 1) == "x = func([1, 2])\n"
    # the one-short case: the substitution alone leaves the line
    # unbalanced — the helper substitutes and the GATE declines it
    # (the conservative doctrine; no insertion guesswork)
    out = _python_mismatch_closer_fix("x = func(a, [b, c)\n", 1)
    assert out == "x = func(a, [b, c]\n"


def test_mismatch_closer_declines_without_a_paren():
    assert _python_mismatch_closer_fix("plain line\n", 1) is None
    assert _python_mismatch_closer_fix("a = [1, 2)\n", 9) is None


def test_mismatch_closer_noop_declines():
    assert _python_mismatch_closer_fix("a = [1, 2]\n", 1) is None


def test_triple_quote_closes_the_nearest_opener():
    text = "x = 1\ns = " + chr(39) * 3 + " literal\ny = 2\n"
    out = _python_triple_quote_fix(text, 3)
    assert out is not None
    # the closer lands at the END of the line before the detection line
    assert out.split("\n")[1].endswith(chr(39) * 3)


def test_triple_quote_prefers_the_last_opener():
    lines = ["a = " + chr(39) * 3 + " first",
             "b = " + chr(34) * 3 + " second",
             "c = 3",
             "d = 4"]
    text = "\n".join(lines)
    out = _python_triple_quote_fix(text, 4)
    assert out is not None
    # the rightmost opener (the triple-DOUBLE on line 2) is the open one
    assert out.split("\n")[2].endswith(chr(34) * 3)


def test_triple_quote_declines_without_an_opener():
    assert _python_triple_quote_fix("x = 1\ny = 2\n", 2) is None
    assert _python_triple_quote_fix("x = 1\n", 1) is None

"""Sprint-23 Batch B unit tests: iterated brace, delimiter, C1b REPLACE."""
from capybase.verification import (
    _delimiter_imbalance_line,
    _try_balance_braces_iterated,
    _try_repair_delimiter,
    derive_prototype,
    find_replacement_line,
)


# ---------------------------------------------------------------------------
# Iterated brace repair (sqlite-0019: 2-gap, sqlite-0029: 4-gap)
# ---------------------------------------------------------------------------

def test_iterated_double_gap():
    text = "def a():\n    if x:\n        y = 1\n\ndef b():\n    pass\n"
    # Simulate a splice that drops TWO closing braces
    broken = "def a():\n    if x:\n        y = 1\n    \ndef b():\n    pass"
    # The iterated repair may not fix indentation-based Python; test with C
    broken_c = "int f() {\n    if (x) {\n        return 1;\n\nint g() {\n    return 2;\n}"
    out = _try_balance_braces_iterated(broken_c, "c")
    assert out is not None, "2-gap should repair iteratively"
    from capybase.verification import _brace_imbalance_line
    assert _brace_imbalance_line(out, "c") is None


def test_iterated_quadruple_gap():
    broken = (
        "int a() {\n    int b() {\n        int c() {\n            int d() {\n"
        "                return 0;\n"
        "}\n"
    )
    out = _try_balance_braces_iterated(broken, "c")
    assert out is not None, "4-gap should repair iteratively"
    from capybase.verification import _brace_imbalance_line
    assert _brace_imbalance_line(out, "c") is None


def test_iterated_no_gap_untouched():
    clean = "int f() {\n    return 0;\n}\n"
    assert _try_balance_braces_iterated(clean, "c") is None


# ---------------------------------------------------------------------------
# Delimiter repair (zenodo-0085: unmatched ')')
# ---------------------------------------------------------------------------

def test_unmatched_close_paren_detected():
    text = "foo(bar)\nprint(x))\n"
    imb = _delimiter_imbalance_line(text, "python")
    assert imb is not None and imb[0] == 1  # line 2, the stray )


def test_stray_close_repaired():
    text = "result = compute(a, b))\n"
    out = _try_repair_delimiter(text, "python")
    assert out is not None
    assert _delimiter_imbalance_line(out, "python") is None


def test_balanced_delimiters_untouched():
    text = "print(compute(a, b))\n"
    assert _delimiter_imbalance_line(text, "python") is None


def test_nested_brackets():
    text = "d[key[0]]\n"
    assert _delimiter_imbalance_line(text, "python") is None


# ---------------------------------------------------------------------------
# C1b: line replacement (redis-0014 wait3, redis-0040 output_help)
# ---------------------------------------------------------------------------

def test_wait3_replacement_found():
    buffer = 'if ((pid = wait3(&stat, WNOHANG, (void**)0)) != 0) {\n'
    parent = 'if ((pid = wait3(&statloc, WNOHANG, NULL)) != 0) {\n'
    error = "t.c:1:22: error: passing argument 2 of 'wait3' from incompatible pointer type"
    result = find_replacement_line(buffer, error, "c", parent)
    assert result is not None
    idx, repl = result
    assert idx == 0
    assert "statloc" in repl and "NULL" in repl


def test_no_replacement_when_identical():
    buffer = "int x = 1;\n"
    error = "t.c:1:5: error: something"
    result = find_replacement_line(buffer, error, "c", buffer)
    assert result is None  # same text → no replacement


# ---------------------------------------------------------------------------
# C1b: derived prototype (redis-0013 cliSwitchProto)
# ---------------------------------------------------------------------------

def test_derive_prototype_from_definition():
    definition = "static int cliSwitchProto(void) {"
    proto = derive_prototype(definition)
    assert proto == "static int cliSwitchProto(void);"


def test_derive_no_brace_declines():
    assert derive_prototype("int foo(void);") is None
    assert derive_prototype("int foo") is None

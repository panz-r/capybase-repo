"""Spec for value-resolution conflict detection.

A value resolution is a conflict where both sides preserve the SAME statement
shape (a return, an assignment to the same target) and only a value/expression
diverges — picking either side is the correct merge, not a dropped intent. These
tests pin classify_value_resolution for Python (ast) and Family-A (regex).
Pure — no I/O, no model.
"""

from __future__ import annotations

from capybase.value_resolution import classify_value_resolution


# ---------------------------------------------------------------------------
# Python: return value resolution
# ---------------------------------------------------------------------------


def test_python_return_value_conflict_indented():
    """The py_simple case: indented return statements with different values."""
    r = classify_value_resolution(
        "return 'hello'", "    return 'hi'", "    return 'howdy'", "python"
    )
    assert r is not None
    assert r.kind == "return"
    assert r.target == ""


def test_python_return_value_conflict_all_indented():
    r = classify_value_resolution(
        "    return 1", "    return 2", "    return 3", "python"
    )
    assert r is not None and r.kind == "return"


def test_python_return_bare():
    r = classify_value_resolution("return a", "return b", "return c", "python")
    assert r is not None and r.kind == "return"


# ---------------------------------------------------------------------------
# Python: assignment value resolution
# ---------------------------------------------------------------------------


def test_python_assignment_same_target_divergent_rhs():
    """Both sides assign to `a` with different RHS → assignment value resolution."""
    r = classify_value_resolution("a = 1", "a = 5", "a = f(x) - 2", "python")
    assert r is not None
    assert r.kind == "assignment"
    assert r.target == "a"


def test_python_assignment_indented():
    r = classify_value_resolution("    a = 1", "    a = 5", "    a = 9", "python")
    assert r is not None and r.kind == "assignment" and r.target == "a"


def test_python_augassign_same_target_and_op():
    """a += 1 / a += 2 / a += 3 → augassign value resolution."""
    r = classify_value_resolution("a += 1", "a += 2", "a += 3", "python")
    assert r is not None
    assert r.kind == "augassign"
    assert r.target == "a"


def test_python_augassign_different_op_not_value_resolution():
    """a += 1 vs a -= 2 differ in operator → not a clean value resolution."""
    r = classify_value_resolution("a += 1", "a += 2", "a -= 3", "python")
    assert r is None


# ---------------------------------------------------------------------------
# Python: NOT a value resolution (genuine distinct additions / mismatches)
# ---------------------------------------------------------------------------


def test_python_different_assignment_targets():
    """x = 5 vs y = 6 → different targets, not a value resolution."""
    assert classify_value_resolution("x = 1", "x = 5", "y = 6", "python") is None


def test_python_distinct_additions_function_vs_import():
    """A function def on one side and an import on the other → distinct additions."""
    assert classify_value_resolution(
        "pass", "def foo():\n    return 1", "import os", "python"
    ) is None


def test_python_malformed_fragment_returns_none():
    """Malformed input never raises; returns None."""
    assert classify_value_resolution("garbage{{{", "more}}}", "junk", "python") is None


def test_python_empty_sides_returns_none():
    assert classify_value_resolution("", "", "", "python") is None


def test_python_different_statement_types():
    """A return on one side and an assignment on the other → not a value resolution."""
    assert classify_value_resolution("return 1", "return 2", "x = 3", "python") is None


def test_python_conditional_with_different_guard_not_value_resolution():
    """When the deepest last statement is reached by descending through a
    conditional (``if``), the GUARD must match across all three sides. ``if flag:
    y = 1`` vs ``if not flag: y = 3`` have the same assignment target ``y`` but
    DIFFERENT conditions — a one-sided merge would drop one branch's condition
    logic entirely (silent wrong merge). The descent into the ``if`` body must
    verify the condition is identical before treating the inner assignment as a
    pure value resolution."""
    r = classify_value_resolution(
        "if flag:\n    y = 1",
        "if flag:\n    y = 2",
        "if not flag:\n    y = 3",  # different guard
        "python",
    )
    assert r is None, (
        f"conditionally-guarded assignment with differing guards wrongly "
        f"classified as value resolution: {r}"
    )


def test_python_conditional_with_same_guard_is_value_resolution():
    """Regression guard: when the guard IS identical across all three sides, the
    inner assignment IS a value resolution (the guard is preserved by taking
    either side)."""
    r = classify_value_resolution(
        "if flag:\n    y = 1",
        "if flag:\n    y = 2",
        "if flag:\n    y = 3",  # same guard
        "python",
    )
    assert r is not None and r.kind == "assignment" and r.target == "y"


def test_python_return_in_different_branches_not_value_resolution():
    """``if x: return 1`` vs ``if z: return 3`` — same statement type (return)
    but different guards. A one-sided merge drops one branch's condition; this
    is NOT a value resolution."""
    r = classify_value_resolution(
        "if x:\n    return 1",
        "if x:\n    return 2",
        "if z:\n    return 3",  # different guard
        "python",
    )
    assert r is None, (
        f"return in differently-guarded branches wrongly classified: {r}"
    )


# ---------------------------------------------------------------------------
# Family A (Rust / brace-delimited): regex-based
# ---------------------------------------------------------------------------


def test_rust_return_value_conflict():
    r = classify_value_resolution("return 1", "return 2", "return 3", "rust")
    assert r is not None and r.kind == "return"


def test_rust_let_assignment_same_target():
    r = classify_value_resolution("let x = 1;", "let x = 5;", "let x = 6;", "rust")
    assert r is not None
    assert r.kind == "assignment"
    assert r.target == "x"


def test_rust_bare_assignment_same_target():
    """A bare `x = ...` assignment (no let) with the same target."""
    r = classify_value_resolution("x = 1;", "x = 5;", "x = 6;", "rust")
    assert r is not None and r.kind == "assignment" and r.target == "x"


def test_rust_different_targets_not_value_resolution():
    assert classify_value_resolution(
        "let x = 5;", "let x = 6;", "let y = 7;", "rust"
    ) is None


def test_rust_mutability_difference_not_value_resolution():
    """``let mut x = 1`` vs ``let x = 2`` — the ``mut`` keyword is semantically
    significant in Rust (affects the borrow checker; a non-mut binding can't be
    re-assigned). The Family-A regex swallowed ``mut`` as an optional modifier
    and classified by target ``x`` only, so a one-sided merge could pick the
    non-mut side and silently change (or break) the binding's mutability. The
    full binding signature (``let mut x`` vs ``let x``) must match across all
    three sides for a pure value resolution."""
    r = classify_value_resolution(
        "let mut x = 1;",
        "let x = 2;",      # cur dropped mut
        "let mut x = 3;",
        "rust",
    )
    assert r is None, (
        f"mutability difference (let mut vs let) wrongly classified: {r}"
    )


def test_rust_same_mutability_is_value_resolution():
    """Regression guard: when all three sides use the SAME mutability, the
    assignment IS a value resolution."""
    r1 = classify_value_resolution(
        "let mut x = 1;", "let mut x = 2;", "let mut x = 3;", "rust"
    )
    assert r1 is not None and r1.kind == "assignment" and r1.target == "x"
    r2 = classify_value_resolution(
        "let x = 1;", "let x = 2;", "let x = 3;", "rust"
    )
    assert r2 is not None and r2.kind == "assignment" and r2.target == "x"


def test_javascript_return_value_conflict():
    r = classify_value_resolution(
        "return 'hi'", "return 'howdy'", "return 'hello'", "javascript"
    )
    assert r is not None and r.kind == "return"


def test_family_a_malformed_returns_none():
    assert classify_value_resolution("{{{", "}}}", "junk", "rust") is None


# ---------------------------------------------------------------------------
# Dispatch + unknown language
# ---------------------------------------------------------------------------


def test_unknown_language_returns_none():
    assert classify_value_resolution("return 1", "return 2", "return 3", "cobol") is None
    assert classify_value_resolution("return 1", "return 2", "return 3", None) is None


def test_as_feature_format():
    """The compact feature string for the conflict-features spine."""
    from capybase.value_resolution import ValueResolution

    assert ValueResolution("return").as_feature() == "return"
    assert ValueResolution("assignment", "a").as_feature() == "assignment:a"
    assert ValueResolution("augassign", "count").as_feature() == "augassign:count"

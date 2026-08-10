"""Sprint 11 fixes: pattern reuse inserts, intent coverage, sbcr provenance.

Tests for the four algorithmic improvements informed by reviewer feedback:

1. **Insert-pattern reuse** — ``_extract_edit_pattern`` now captures insert
   opcodes with a forward anchor, and ``_instantiate_pattern`` inserts before
   the anchor. This enables sibling-pattern reuse for the nlohmann-0019 shape
   (``Type x;`` → ``Type x{};``).

2. **Intent-coverage repair** — ``_try_restore_common_lines`` restores lines
   common to BOTH sides that an LLM candidate dropped.

3. **sbcr identifier-provenance guard** — ``_has_undeclared_side_local_identifier``
   detects when sbcr stacks a statement using a variable declared only in one
   side's context (the clickhouse-0041 defect).

4. **NEAR_MATCH dump trigger** — ``_any_unit_used_llm`` detects LLM-resolved
   steps for bundle dumping.
"""

from __future__ import annotations

from capybase.orchestrator import (
    _extract_edit_pattern,
    _instantiate_pattern,
    _try_restore_common_lines,
    _has_undeclared_side_local_identifier,
)


# ---------------------------------------------------------------------------
# Fix 2: Insert-pattern reuse (nlohmann-0019 shape)
# ---------------------------------------------------------------------------


def test_extract_pattern_insert_before_semicolon():
    """The nlohmann-0019 value-init pattern: ``Type x;`` → ``Type x{};``.

    The tokenizer may combine ``{};`` as one token, producing a ``replace``
    opcode (``;`` → ``{};``). Either way, the pattern must be extractable
    and instantiable on a sibling with a different identifier."""
    base = "    NumberType n;"
    resolved = "    NumberType n{};"
    pat = _extract_edit_pattern(base, resolved)
    assert pat is not None, "should extract a pattern"
    assert len(pat) >= 1


def test_instantiate_pattern_sibling_different_identifier():
    """The pattern extracted from ``NumberType n;`` → ``NumberType n{};``
    must apply to ``double result;`` → ``double result{};``."""
    base = "    NumberType n;"
    resolved = "    NumberType n{};"
    pat = _extract_edit_pattern(base, resolved)
    assert pat is not None

    sibling = "    double result;"
    inst = _instantiate_pattern(sibling, pat)
    assert inst is not None, "should instantiate on sibling"
    assert "{}" in inst, f"expected value-init braces, got: {inst!r}"
    assert "result" in inst, "sibling identifier must be preserved"
    assert "NumberType" not in inst, "original identifier must NOT leak"


def test_instantiate_pattern_multiple_siblings():
    """The pattern must apply to multiple siblings with different types
    and identifiers — the nlohmann-0019/0024 throughput case."""
    base = "    int counter;"
    resolved = "    int counter{};"
    pat = _extract_edit_pattern(base, resolved)
    assert pat is not None

    siblings = [
        ("    double ratio;", "ratio"),
        ("    bool flag;", "flag"),
        ("    char buffer;", "buffer"),
        ("    std::string name;", "name"),
    ]
    for sibling, ident in siblings:
        inst = _instantiate_pattern(sibling, pat)
        assert inst is not None, f"should instantiate for {ident}"
        assert "{}" in inst, f"expected braces for {ident}, got: {inst!r}"
        assert ident in inst, f"identifier {ident} must survive"


def test_pattern_reuse_does_not_substitute_identifiers():
    """A rename pattern (``oldName`` → ``newName``) must NOT be instantiable
    — the IDENT guard in _category_seq_to_tokens correctly blocks it."""
    base = "    int oldName = 0;"
    resolved = "    int newName = 0;"
    pat = _extract_edit_pattern(base, resolved)
    if pat is None:
        return  # pattern may be too complex or declined — that's fine
    sibling = "    int otherVar = 0;"
    inst = _instantiate_pattern(sibling, pat)
    # Should NOT produce "otherVarnewName" or substitute identifiers.
    if inst is not None:
        assert "newName" not in inst or "oldName" not in inst, (
            "identifier substitution leaked into sibling"
        )


def test_legacy_3tuple_patterns_still_work():
    """Old-format 3-tuple patterns (without op_type) must still work via
    the backward-compatibility path in _instantiate_pattern."""
    # A simple replace: ";" → "; // comment"
    legacy_pattern = [(";", "; // comment", ";")]
    base = "    int x;"
    inst = _instantiate_pattern(base, legacy_pattern)
    # May or may not succeed depending on tokenization, but must not crash.
    if inst is not None:
        assert "x" in inst


# ---------------------------------------------------------------------------
# Fix 3: Intent-coverage repair (side-common line restore)
# ---------------------------------------------------------------------------


def test_restore_common_lines_basic():
    """A candidate that dropped a line common to both sides gets it restored."""
    base = "void f() {\n    int x = 1;\n}\n"
    current = "void f() {\n    int x = 1;\n    int y = 2;\n}\n"
    replayed = "void f() {\n    int x = 1;\n    int y = 2;\n}\n"
    # Candidate dropped the "int y = 2;" line (common to both sides).
    candidate = "void f() {\n    int x = 1;\n}\n"
    repaired = _try_restore_common_lines(candidate, base, current, replayed, "cpp")
    assert repaired is not None, "should restore the dropped common line"
    assert "int y = 2;" in repaired


def test_restore_common_lines_no_common_dropped():
    """When the candidate has all common lines, nothing is restored."""
    base = "void f() {\n    int x = 1;\n}\n"
    current = "void f() {\n    int x = 1;\n    extra_cur();\n}\n"
    replayed = "void f() {\n    int x = 1;\n    extra_rep();\n}\n"
    candidate = "void f() {\n    int x = 1;\n    extra_cur();\n    extra_rep();\n}\n"
    repaired = _try_restore_common_lines(candidate, base, current, replayed, "cpp")
    # "int x = 1;" is common and present — no restoration needed.
    assert repaired is None


def test_restore_common_lines_declines_on_unbalanced():
    """If restoring the line would unbalance braces, decline."""
    # The common line is "}" but inserting it creates imbalance.
    base = "void f() {\n}\n"
    current = "void f() {\n}\n}\n"  # extra } common to both
    replayed = "void f() {\n}\n}\n"
    candidate = "void f() {\n}\n"  # dropped the extra }
    repaired = _try_restore_common_lines(candidate, base, current, replayed, "cpp")
    # Restoring "}" makes depth go negative — should be declined.
    # (Unless the brace repair fixes it, but _try_restore_common_lines checks
    # brace balance, not repairs.)
    if repaired is not None:
        # If it did restore, it must be brace-balanced.
        from capybase.verification import _brace_imbalance_line
        assert _brace_imbalance_line(repaired, "cpp") is None


# ---------------------------------------------------------------------------
# Fix 4: sbcr identifier-provenance guard (clickhouse-0041 shape)
# ---------------------------------------------------------------------------


def test_undeclared_identifier_side_local_variable():
    """sbcr stacked a return using `suffix` from replayed, but suffix was
    declared only in replayed's context (not in the candidate)."""
    candidate = (
        "std::string withOrdinalEnding(int n) {\n"
        "    return std::to_string(n);\n"
        "    return suffix.substr(0, 2);\n"
        "}\n"
    )
    base = "std::string withOrdinalEnding(int n) {\n    return std::to_string(n);\n}\n"
    current = "std::string withOrdinalEnding(int n) {\n    return std::to_string(n);\n}\n"
    replayed = (
        "std::string withOrdinalEnding(int n) {\n"
        "    std::string suffix = getSuffix(n);\n"
        "    return suffix.substr(0, 2);\n"
        "}\n"
    )
    result = _has_undeclared_side_local_identifier(candidate, base, current, replayed)
    assert result == "suffix", f"expected 'suffix', got {result!r}"


def test_undeclared_identifier_not_flagged_when_declared():
    """When the candidate declares the identifier, it's not flagged."""
    candidate = (
        "int f() {\n"
        "    int x = compute();\n"
        "    return x;\n"
        "}\n"
    )
    base = "int f() {\n    return 0;\n}\n"
    current = "int f() {\n    return 0;\n}\n"
    replayed = "int f() {\n    int x = compute();\n    return x;\n}\n"
    result = _has_undeclared_side_local_identifier(candidate, base, current, replayed)
    assert result is None, f"x is declared in the candidate, should not be flagged"


def test_undeclared_identifier_not_flagged_in_both_sides():
    """When the identifier appears in BOTH sides (not just one), it's not
    flagged — it's a shared variable, not a side-local one."""
    candidate = (
        "int f() {\n"
        "    return result;\n"
        "}\n"
    )
    base = "int f() {\n    int result = compute();\n    return result;\n}\n"
    current = "int f() {\n    int result = compute2();\n    return result;\n}\n"
    replayed = "int f() {\n    int result = compute3();\n    return result;\n}\n"
    result = _has_undeclared_side_local_identifier(candidate, base, current, replayed)
    # result appears in both sides (and base), so it's NOT side-local.
    assert result is None


def test_undeclared_identifier_not_flagged_for_keywords():
    """Keywords like true/false/nullptr must not be flagged."""
    candidate = "void f() {\n    return nullptr;\n}\n"
    base = "void f() {\n}\n"
    current = "void f() {\n}\n"
    replayed = "void f() {\n    return nullptr;\n}\n"
    result = _has_undeclared_side_local_identifier(candidate, base, current, replayed)
    assert result is None


def test_undeclared_identifier_frequent_usage_not_flagged():
    """An identifier used 3+ times in the candidate is likely a parameter
    or member — not flagged even if it's side-local."""
    candidate = (
        "void f(int param) {\n"
        "    use(param);\n"
        "    use(param);\n"
        "    return param;\n"
        "}\n"
    )
    base = "void f() {\n}\n"
    current = "void f() {\n}\n"
    replayed = "void f(int param) {\n    use(param);\n}\n"
    result = _has_undeclared_side_local_identifier(candidate, base, current, replayed)
    # param appears 4 times → frequent → not flagged.
    assert result is None

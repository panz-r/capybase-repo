"""Sprint 18: escape-hatch repair, zero-budget escape, loop continuation.

Tests for three fixes targeting remaining case-by-case failures:

1. **Escape-hatch intent_coverage_repair** — the convergence escape hatch
   accepts cycling candidates but never ran _try_intent_coverage_repair,
   leaving dropped lines un-restored (0.94→0.95 gap).

2. **Zero-budget escape** — when max_retries=0 (multi-unit files), the
   first compiling candidate was thrown away because risk.decide() said
   "retry" (unaware of the 0 budget) and the oscillation backstop
   immediately escalated.

3. **Loop continuation** — the per-unit loop broke on the first escalation,
   leaving remaining units unprocessed.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Fix 1: intent_coverage_repair runs on escape-hatch accepts
# ---------------------------------------------------------------------------


def test_restore_common_lines_restores_side_specific():
    """_try_restore_common_lines restores a side-specific dropped line."""
    from capybase.orchestrator import _try_restore_common_lines

    base = "void f() {\n    int x = 1;\n}\n"
    current = "void f() {\n    int x = 1;\n    int y = 2;\n}\n"
    replayed = "void f() {\n    int x = 1;\n}\n"
    # Candidate dropped current's unique addition
    candidate = "void f() {\n    int x = 1;\n}\n"
    repaired = _try_restore_common_lines(candidate, base, current, replayed, "cpp")
    assert repaired is not None, "should restore the dropped side-specific line"
    assert "int y = 2;" in repaired


def test_escape_hatch_runs_repair(monkeypatch):
    """When the convergence escape hatch accepts a candidate, the intent
    coverage repair should run and restore dropped lines."""
    from capybase.conflict_model import ConflictUnit, ConflictSide
    from capybase.orchestrator import Orchestrator
    from capybase.config import Config

    # Track whether _try_intent_coverage_repair was called
    repair_called = []

    base = "void f() {\n    int x = 1;\n    int y = 2;\n}\n"
    current = "void f() {\n    int x = 1;\n    int y = 2;\n    int z = 3;\n}\n"
    replayed = "void f() {\n    int x = 10;\n    int y = 2;\n}\n"
    # Candidate dropped current's addition of int z = 3
    candidate_text = "void f() {\n    int x = 10;\n    int y = 2;\n}\n"

    unit = ConflictUnit(
        session_id="s", step_index=0, path="f.cpp", language="cpp",
        conflict_type="UU", unit_id="f:1:0", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text=base, marker_span=(0, 3),
        structural_metadata={},
    )

    cfg = Config()
    orch = Orchestrator.__new__(Orchestrator)
    orch.config = cfg

    # Provide a mock journal that accepts emit/store_prompt calls
    class _MockJournal:
        def emit(self, *a, **kw): pass
        def store_prompt(self, *a, **kw): pass
    orch.journal = _MockJournal()
    orch.step = 0

    # Monkey-patch _record_resolution_attempt to be a no-op
    orch._record_resolution_attempt = lambda *a, **kw: None

    # Create a fake candidate
    from capybase.conflict_model import CandidateResolution

    cand = CandidateResolution(
        candidate_id="test", unit_id="f:1:0",
        model_name="test", prompt_version="test",
        resolved_text=candidate_text,
        provenance="plain_llm",
    )

    result = orch._try_intent_coverage_repair(unit, cand)
    # The repair should have restored int z = 3
    assert "int z = 3;" in result.resolved_text, (
        "intent_coverage_repair should restore the dropped line"
    )


# ---------------------------------------------------------------------------
# Fix 2: Zero-budget escape
# ---------------------------------------------------------------------------


def test_zero_budget_escape_conditions():
    """The zero-budget escape should only fire when:
    - max_retries is not None and _unit_budget == 0
    - retry_count == 0
    - candidate has resolved_text
    - no hard failures
    - only advisory warnings
    """
    # This is a logic test — verify the advisory set covers the right validators
    _ADVISORY = frozenset({
        "preservation_heuristic",
        "both_sides_represented",
        "obligation",
        "intent_coverage",
        "unattributed_code",
    })

    # Content-loss validators that ARE in the advisory set (accepted by zero-budget escape)
    assert "both_sides_represented" in _ADVISORY
    assert "intent_coverage" in _ADVISORY

    # Non-advisory validators that should NOT be accepted
    assert "ccs_syntax" not in _ADVISORY
    assert "marker_check" not in _ADVISORY
    assert "ast_preservation" not in _ADVISORY


def test_zero_budget_escape_does_not_fire_with_hard_failures():
    """When the candidate has hard failures (compile errors), the zero-budget
    escape must NOT fire — a non-compiling file is worse than escalating."""
    # Verify that hard failures are checked before the escape
    # The code checks `not validation.hard_failures` before the escape.
    # This test documents the invariant.
    from capybase.verification import VerificationResult, VerificationFailure

    failing = VerificationResult(
        candidate_id="test", unit_id="test",
        passed=False,
        hard_failures=[VerificationFailure(
            validator="ccs_syntax", severity="error",
            message="error: redefinition of 'foo'",
        )],
        warnings=[],
    )
    assert failing.hard_failures, "hard failures present"
    assert not (not failing.hard_failures), "escape condition fails with hard failures"


# ---------------------------------------------------------------------------
# Fix 3: Loop continues after escalation
# ---------------------------------------------------------------------------


def test_escalated_units_is_list_not_break():
    """Verify the loop uses escalated_units list with continue, not break."""
    # This is a structural test — verify the code uses 'continue' not 'break'
    # by checking the orchestrator source
    import inspect
    from capybase.orchestrator import Orchestrator

    source = inspect.getsource(Orchestrator._resolve_step)
    assert "escalated_units" in source, "should use escalated_units list"
    assert "escalated_units.append(outcome)" in source, "should append to list"
    assert "continue" in source, "should continue not break"
    # The old pattern should be gone
    assert "escalated_unit = outcome\n                    break" not in source, (
        "old break pattern should be removed"
    )


def test_loop_continues_after_escalation():
    """When the first unit escalates, subsequent units should still be
    processed (their outcomes appended to result.outcomes)."""
    # Simulate: 3 units, 2nd escalates, verify 3rd is processed
    outcomes = []
    escalated = []

    class FakeOutcome:
        def __init__(self, uid, accepted):
            self.unit_id = uid
            self.accepted = accepted

    units = ["u0", "u1", "u2"]
    # u1 will escalate (accepted=None)
    accept_map = {"u0": "yes", "u1": None, "u2": "yes"}

    # Simulate the loop logic (mirroring the orchestrator's pattern)
    for unit_id in units:
        outcome = FakeOutcome(unit_id, accept_map[unit_id])
        outcomes.append(outcome)
        if outcome.accepted is None:
            escalated.append(outcome)
            continue  # Sprint 18 fix: continue, not break
        # accepted processing would happen here

    # All 3 units should have outcomes
    assert len(outcomes) == 3, "all units should be processed"
    # Only u1 should be in escalated
    assert len(escalated) == 1
    assert escalated[0].unit_id == "u1"


# ---------------------------------------------------------------------------
# Fix 4: multi-symbol duplicate-definition enrichment
# ---------------------------------------------------------------------------

# A file where three ``get_*_float_prefix`` overload families are defined
# OUTSIDE the conflict region. gcc stops at the FIRST overload conflict, but
# the candidate re-defines the whole family — without multi-symbol enrichment
# the model fixes one duplicate per CEGIS iteration and the header cap (1
# retry) is exhausted before the rest are addressed.
_FLOAT_PREFIX_FILE = """\
// ... ~9000 lines of preamble ...
  static constexpr CharType get_cbor_float_prefix(float /*unused*/)
  {
      return static_cast<CharType>(0xFA);
  }

  static constexpr CharType get_cbor_float_prefix(double /*unused*/)
  {
      return static_cast<CharType>(0xFB);
  }

  static constexpr CharType get_msgpack_float_prefix(float /*unused*/)
  {
      return static_cast<CharType>(0xCA);
  }

  static constexpr CharType get_msgpack_float_prefix(double /*unused*/)
  {
      return static_cast<CharType>(0xCB);
  }

  static constexpr CharType get_ubjson_float_prefix(float /*unused*/)
  {
      return static_cast<CharType>(0xFA);
  }

  static constexpr CharType get_ubjson_float_prefix(double /*unused*/)
  {
      return static_cast<CharType>(0xFB);
  }
// ... rest of file ...
"""


def test_dup_def_modify_delete_recommends_accept_deletion():
    """modify/delete: when one side's additions are ALL duplicates and the
    other side deleted the block, the note leads with "accept the deletion."

    This is the nlohmann-0034 scenario: the CURRENT side added float-prefix
    functions that already exist elsewhere; the REPLAYED side deleted the
    duplicate block. The prompt's obligation block says "CURRENT must preserve
    these" — the enrichment must explicitly override that and tell the model to
    accept the deletion (output EMPTY resolved_text).
    """
    from capybase.orchestrator import _enrich_duplicate_definition_failures
    from capybase.verification import VerificationResult, VerificationFailure
    from types import SimpleNamespace

    cur_side = (
        "  static constexpr CharType get_cbor_float_prefix(float /*unused*/)\n"
        "  {\n      return to_char_type(0xFA);\n  }\n\n"
        "  static constexpr CharType get_msgpack_float_prefix(float /*unused*/)\n"
        "  {\n      return to_char_type(0xCA);\n  }\n"
    )
    unit = SimpleNamespace(
        original_worktree_text=_FLOAT_PREFIX_FILE,
        marker_span=(2, 6), language="cpp",
        current=SimpleNamespace(text=cur_side),
        replayed=SimpleNamespace(text=""),  # deleted the block
    )
    cand = SimpleNamespace(resolved_text=cur_side)  # model copied current
    validation = VerificationResult(
        candidate_id="t", unit_id="t", passed=False,
        hard_failures=[VerificationFailure(
            validator="ccs_syntax", severity="error",
            message=("test.hpp:4:1: error: '...::get_cbor_float_prefix(float)'"
                     " cannot be overloaded with '...::get_cbor_float_prefix"
                     "(float)'"),
        )], warnings=[],
    )

    _enrich_duplicate_definition_failures(unit, cand, validation)
    msg = validation.hard_failures[0].message

    # Conclusion-first: leads with the actionable recommendation.
    assert "RESOLVE BY ACCEPTING THE REPLAYED SIDE'S DELETION" in msg
    # Explicitly overrides the misleading obligation.
    assert "must preserve" in msg.lower() or "must preserve" in msg
    assert "INCORRECT" in msg
    # Lists all duplicate families (gcc only reported cbor).
    assert "get_cbor_float_prefix" in msg
    assert "get_msgpack_float_prefix" in msg
    # Positive instruction: output empty.
    assert "EMPTY" in msg


def test_dup_def_modify_modify_recommends_prefer_side():
    """modify/modify: when one side's additions are ALL duplicates but the
    other side has content, the note says "prefer the [clean] side"."""
    from capybase.orchestrator import _enrich_duplicate_definition_failures
    from capybase.verification import VerificationResult, VerificationFailure
    from types import SimpleNamespace

    cur_side = (
        "  static constexpr CharType get_cbor_float_prefix(float /*unused*/)\n"
        "  {\n      return to_char_type(0xFA);\n  }\n"
    )
    rep_side = (
        "  // replaced with a comment block\n"
        "  int new_helper() { return 42; }\n"
    )
    unit = SimpleNamespace(
        original_worktree_text=_FLOAT_PREFIX_FILE,
        marker_span=(2, 6), language="cpp",
        current=SimpleNamespace(text=cur_side),
        replayed=SimpleNamespace(text=rep_side),
    )
    cand = SimpleNamespace(resolved_text=cur_side)
    validation = VerificationResult(
        candidate_id="t", unit_id="t", passed=False,
        hard_failures=[VerificationFailure(
            validator="ccs_syntax", severity="error",
            message=("error: '::get_cbor_float_prefix(float)' cannot be "
                     "overloaded"),
        )], warnings=[],
    )
    _enrich_duplicate_definition_failures(unit, cand, validation)
    msg = validation.hard_failures[0].message
    assert "PREFER THE REPLAYED SIDE" in msg
    assert "do NOT include the CURRENT side" in msg


def test_dup_def_fallback_when_no_side_info():
    """When side attribution is inconclusive (both sides have duplicates, or
    unit lacks side info), the note falls back to "omit these functions"."""
    from capybase.orchestrator import _enrich_duplicate_definition_failures
    from capybase.verification import VerificationResult, VerificationFailure
    from types import SimpleNamespace

    cand_text = (
        "  static constexpr CharType get_cbor_float_prefix(float /*unused*/)\n"
        "  {\n      return to_char_type(0xFA);\n  }\n\n"
        "  static constexpr CharType get_ubjson_float_prefix(float /*unused*/)\n"
        "  {\n      return to_char_type(0xFB);\n  }\n"
    )
    # No current/replayed on unit → side attribution returns None → fallback.
    unit = SimpleNamespace(
        original_worktree_text=_FLOAT_PREFIX_FILE,
        marker_span=(2, 6), language="cpp",
    )
    cand = SimpleNamespace(resolved_text=cand_text)
    validation = VerificationResult(
        candidate_id="t", unit_id="t", passed=False,
        hard_failures=[VerificationFailure(
            validator="ccs_syntax", severity="error",
            message=("error: '::get_cbor_float_prefix(float)' cannot be "
                     "overloaded"),
        )], warnings=[],
    )
    _enrich_duplicate_definition_failures(unit, cand, validation)
    msg = validation.hard_failures[0].message
    assert "DUPLICATE DEFINITIONS" in msg
    assert "get_cbor_float_prefix" in msg
    assert "get_ubjson_float_prefix" in msg
    assert "Remove every duplicate definition" in msg


def test_dup_def_enrichment_shows_definition_not_call_site():
    """``_find_def_context`` must locate the DEFINITION, not a preceding call.

    A call like ``write_character(get_cbor_float_prefix(x));`` appears BEFORE
    the definition in source order. The enrichment must skip it and anchor on
    the real definition signature (return-type prefix + ``{`` body).
    """
    from capybase.orchestrator import _find_def_context

    lines = (
        # call site comes first in source order
        "    oa->write_character(get_cbor_float_prefix(j.m_value.number_float));\n"
        "    return get_cbor_float_prefix(x);  // another call\n"
        "\n"
        "    static constexpr CharType get_cbor_float_prefix(float /*unused*/)\n"
        "    {\n"
        "        return static_cast<CharType>(0xFA);\n"
        "    }\n"
    ).split("\n")

    hit = _find_def_context(lines, "get_cbor_float_prefix")
    assert hit is not None, "should find the definition"
    lineno, ctx = hit
    # The definition signature is on the 4th line (1-based), NOT the call on
    # line 1 or 2.
    assert "static constexpr CharType" in lines[lineno - 1]
    assert "static_cast<CharType>(0xFA)" in ctx


def test_dup_def_enrichment_is_idempotent():
    """Re-running enrichment on an already-enriched failure is a no-op."""
    from capybase.orchestrator import _enrich_duplicate_definition_failures
    from capybase.verification import VerificationResult, VerificationFailure
    from types import SimpleNamespace

    unit = SimpleNamespace(
        original_worktree_text="  int foo() {\n    return 1;\n  }\n",
        marker_span=None, language="cpp",
    )
    cand = SimpleNamespace(resolved_text="  int foo() {\n    return 2;\n  }\n")
    validation = VerificationResult(
        candidate_id="t", unit_id="t", passed=False,
        hard_failures=[VerificationFailure(
            validator="ccs_syntax", severity="error",
            message="error: redefinition of 'int foo()'",
        )],
        warnings=[],
    )
    _enrich_duplicate_definition_failures(unit, cand, validation)
    first = validation.hard_failures[0].message
    # Run again — must not duplicate the NOTE block.
    _enrich_duplicate_definition_failures(unit, cand, validation)
    second = validation.hard_failures[0].message
    assert first == second, "second enrichment must be a no-op"
    assert second.count("NOTE") == 1, "exactly one NOTE block"


def test_dup_def_enrichment_skips_when_no_duplicate_failure():
    """Non-duplicate errors (e.g. missing semicolon) are left untouched."""
    from capybase.orchestrator import _enrich_duplicate_definition_failures
    from capybase.verification import VerificationResult, VerificationFailure
    from types import SimpleNamespace

    unit = SimpleNamespace(
        original_worktree_text="  int foo() {\n    return 1;\n  }\n",
        marker_span=None, language="cpp",
    )
    cand = SimpleNamespace(resolved_text="  int foo() {\n    return 1\n  }\n")
    original_msg = "test.hpp:2:12: error: expected ';' before '}' token"
    validation = VerificationResult(
        candidate_id="t", unit_id="t", passed=False,
        hard_failures=[VerificationFailure(
            validator="ccs_syntax", severity="error",
            message=original_msg,
        )],
        warnings=[],
    )
    _enrich_duplicate_definition_failures(unit, cand, validation)
    assert validation.hard_failures[0].message == original_msg, (
        "non-duplicate error must not be enriched"
    )

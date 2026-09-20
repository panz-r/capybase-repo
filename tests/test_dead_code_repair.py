"""The dead-statement repair arm (S28-102 TASK 3, built S28-133).

A stacked-return merge — a small model emits both sides' return statements
one after the other — hard-fails the whole-file ``unreachable_code``
validator (Phase 2 verify_file; the per-unit gate never sees it). The
repair arm deletes the flagged dead statements mechanically — zero model
calls — instead of burning CEGIS retries on a failure the model must
infer. Pins:

- the spans core reports whole-statement line ranges (multi-line calls);
- the core deletes exactly the flagged statements, refuses a deletion
  that would empty the file, and must clear the detector completely;
- the whole-file repair beam rung lands the repaired candidate;
- the RiskEngine path is out of scope here (the ceiling tests in
  test_routing.py cover the sampling interplay).
"""

from __future__ import annotations

from capybase.conflict_model import (
    CandidateResolution, ConflictSide, ConflictUnit, VerificationFailure,
)
from capybase.verification import (
    _py_unreachable_code, _py_unreachable_spans, whole_file_dead_code_repair,
)


# ---------------------------------------------------------------------------
# Spans core + wrapper compatibility
# ---------------------------------------------------------------------------


def test_spans_report_whole_statement_ranges():
    source = (
        "def f():\n"
        "    return 'hi'\n"
        "    x = call(\n"
        "        1, 2,\n"
        "    )\n"
        "    return 'howdy'\n"
    )
    # BOTH trailing statements are dead, each reported as a whole-statement
    # span (the multi-line call is one span, not three fragments).
    assert _py_unreachable_spans(source) == [
        ("f", "return", 3, 5), ("f", "return", 6, 6)]


def test_wrapper_keeps_3tuple_shape():
    source = "def f():\n    return 'hi'\n    x = 1\n"
    assert _py_unreachable_code(source) == [("f", "return", 3)]
    assert _py_unreachable_code("def f():\n    pass\n") == []


# ---------------------------------------------------------------------------
# The whole-file core
# ---------------------------------------------------------------------------


def _failure(line: int) -> VerificationFailure:
    return VerificationFailure(
        validator="unreachable_code", severity="error",
        message=f"line {line}: unreachable code after return",
        detail={"line": line},
    )


def test_core_deletes_the_dead_statement():
    merged = "def f():\n    return 'hi'\n    return 'howdy'\n"
    repaired, deleted = whole_file_dead_code_repair(
        merged, [_failure(3)])
    assert repaired == "def f():\n    return 'hi'\n"
    assert deleted == 1


def test_core_deletes_multi_line_dead_statements_whole():
    merged = (
        "def f():\n"
        "    return 'hi'\n"
        "    x = other(\n"
        "        1, 2,\n"
        "    )\n"
    )
    repaired, deleted = whole_file_dead_code_repair(
        merged, [_failure(3)])
    assert repaired == "def f():\n    return 'hi'\n"
    assert deleted == 3  # one statement (3 lines) — deleted whole, not split


def test_core_requires_the_detector_to_clear():
    """The core recomputes the spans itself, so a partial repair is
    impossible — two stacked returns after a terminator are BOTH deleted
    and the detector clears."""
    merged = "def f():\n    return 'hi'\n    return 'mid'\n    return 'howdy'\n"
    repaired, _deleted = whole_file_dead_code_repair(
        merged, [_failure(3)])
    assert "return 'mid'" not in repaired
    assert "return 'howdy'" not in repaired
    assert _py_unreachable_spans(repaired) == []


def test_core_declines_other_validators():
    merged = "def f():\n    return 'hi'\n    return 'howdy'\n"
    assert whole_file_dead_code_repair(
        merged, [_failure(3).__class__(validator="syntax", severity="error",
                                       message="broken")]) is None


def test_core_refuses_a_file_emptying_deletion(monkeypatch):
    """A span covering the whole file: the deletion would empty it — the
    refusal branch (defensive; real spans only cover function bodies)."""
    merged = "def f():\n    return 1\n"
    monkeypatch.setattr(
        "capybase.verification._py_unreachable_spans",
        lambda source: [("f", "return", 1, 2)])
    failures = [_failure(1)]
    assert whole_file_dead_code_repair(merged, failures) is None


# ---------------------------------------------------------------------------
# The whole-file repair beam rung
# ---------------------------------------------------------------------------


def _whole_file_unit(resolved: str) -> ConflictUnit:
    return ConflictUnit(
        session_id="s", step_index=0, path="app.py", language="python",
        conflict_type="UU", unit_id="app.py", unit_kind="whole_file",
        base=ConflictSide(label="BASE", text=""),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=""),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=""),
        original_worktree_text=resolved, marker_span=None,
    )


def test_beam_rung_lands_the_dead_code_repair():
    from capybase.orchestrator import _try_deterministic_dead_code_repair

    merged = "def greet():\n    return 'hi'\n    return 'howdy'\n"
    unit = _whole_file_unit(merged)
    cand = CandidateResolution(
        candidate_id="c1", unit_id=unit.unit_id, model_name="m",
        prompt_version="v", resolved_text=merged)
    accepted = [(unit, cand)]
    failures = [_failure(3)]

    out = _try_deterministic_dead_code_repair(failures, accepted, 0)
    assert out is not None
    _unit_new, cand_new = out[0]
    assert cand_new.provenance == "deterministic_dead_code_repair"
    assert cand_new.resolved_text == "def greet():\n    return 'hi'\n"
    # the original accepted pair is untouched (immutability contract)
    assert accepted[0][1].resolved_text == merged


def test_beam_rung_declines_non_dead_code_shapes():
    from capybase.orchestrator import _try_deterministic_dead_code_repair

    merged = "def greet():\n    return 'hi'\n"
    unit = _whole_file_unit(merged)
    cand = CandidateResolution(
        candidate_id="c1", unit_id=unit.unit_id, model_name="m",
        prompt_version="v", resolved_text=merged)
    out = _try_deterministic_dead_code_repair(
        [_failure(3)], [(unit, cand)], 0)
    assert out is None  # no unreachable_code failure → defer to the LLM path


def test_beam_rung_fault_idx_bounds():
    from capybase.orchestrator import _try_deterministic_dead_code_repair

    unit = _whole_file_unit("def greet():\n    return 'hi'\n")
    cand = CandidateResolution(
        candidate_id="c1", unit_id=unit.unit_id, model_name="m",
        prompt_version="v", resolved_text="def greet():\n    return 'hi'\n")
    # fault_idx beyond the accepted list → None (the beam's own guard)
    assert _try_deterministic_dead_code_repair(
        [_failure(3)], [(unit, cand)], 5) is None

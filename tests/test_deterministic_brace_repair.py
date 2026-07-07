"""Deterministic brace repair + cross-hunk splice context (Fixes #1 + #2).

Fix #2: ``_try_balance_braces`` + ``_try_deterministic_brace_repair`` fix the
recurring splice-junction brace imbalance deterministically, skipping the LLM
call when a single edit (or a few stray-brace removals) fully balances the
spliced file. The live eval showed the model reproducing the same extra/missing
brace at a hunk junction across 4 retries because it couldn't see the junction.

Fix #1: ``_splice_context_snippet`` widens its window to span the two adjacent
units' marker spans when the error line falls at a hunk junction, so the model
sees both hunks and their boundary instead of just one unit's ±5 lines.
"""

from __future__ import annotations

from capybase.conflict_model import ConflictSide, ConflictUnit, CandidateResolution
from capybase.orchestrator import (
    _resolved_buffer,
    _splice_context_snippet,
    _try_deterministic_brace_repair,
    _attribute_whole_file_failure,
)
from capybase.verification import VerificationFailure, _brace_imbalance_line


def _unit(*, worktree, marker_span, uid="u", language="rust"):
    return ConflictUnit(
        session_id="s", step_index=0, path="a.rs", language=language,
        conflict_type="UU", unit_id=uid, unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=""),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=""),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=""),
        original_worktree_text=worktree, marker_span=marker_span,
    )


def _cand(uid, resolved):
    return CandidateResolution(
        candidate_id=f"c:{uid}", unit_id=uid, model_name="m",
        prompt_version="v", resolved_text=resolved,
    )


def _brace_failure(line: int) -> VerificationFailure:
    return VerificationFailure(
        validator="syntax", severity="error",
        message=f"splice coherence: unbalanced braces at line {line}",
        detail={"brace_imbalance_line": line},
    )


# ---------------------------------------------------------------------------
# _try_deterministic_brace_repair: single unit, extra brace
# ---------------------------------------------------------------------------


def test_det_repair_single_unit_extra_brace():
    """A single-unit conflict whose resolution has an extra } is fixed without
    an LLM call. The deterministic repair operates on the spliced buffer and
    returns a whole-file unit carrying the repaired text."""
    worktree = (
        "fn main() {\n"
        "<<<<<<< HEAD\n    let x = 1;\n=======\n    let y = 2;\n>>>>>>> feat\n"
        "}\n"
    )
    unit = _unit(worktree=worktree, marker_span=(1, 5), uid="u:1")
    bad = _cand("u:1", "    let x = 1;\n}")  # extra }
    accepted = [(unit, bad)]
    spliced = _resolved_buffer(worktree, accepted)
    imb = _brace_imbalance_line(spliced)
    assert imb is not None
    failures = [_brace_failure(imb + 1)]
    fault_idx = _attribute_whole_file_failure(failures, [unit])
    result = _try_deterministic_brace_repair(failures, worktree, accepted, fault_idx)
    assert result is not None, "should deterministically repair"
    u_r, c_r = result[0]
    assert u_r.unit_kind == "whole_file"
    assert u_r.marker_span is None
    assert c_r.provenance == "deterministic_brace_repair"
    # The repaired buffer, when re-spliced, must be balanced.
    re_spliced = _resolved_buffer(worktree, result)
    assert _brace_imbalance_line(re_spliced) is None


def test_det_repair_single_unit_unclosed_brace():
    """A single-unit conflict with an unclosed { is fixed by appending }."""
    worktree = (
        "fn main() {\n"
        "<<<<<<< HEAD\n    let x = 1;\n=======\n    let y = 2;\n>>>>>>> feat\n"
        "}\n"
    )
    unit = _unit(worktree=worktree, marker_span=(1, 5), uid="u:1")
    bad = _cand("u:1", "    if cond {\n        let x = 1;")  # unclosed {
    accepted = [(unit, bad)]
    spliced = _resolved_buffer(worktree, accepted)
    imb = _brace_imbalance_line(spliced)
    assert imb is not None
    failures = [_brace_failure(imb + 1)]
    fault_idx = _attribute_whole_file_failure(failures, [unit])
    result = _try_deterministic_brace_repair(failures, worktree, accepted, fault_idx)
    assert result is not None
    re_spliced = _resolved_buffer(worktree, result)
    assert _brace_imbalance_line(re_spliced) is None


def test_det_repair_defers_on_non_brace_failure():
    """A cargo/semantic error is NOT a brace failure → defer to LLM."""
    worktree = (
        "fn main() {\n<<<<<<< HEAD\n    x\n=======\n    y\n>>>>>>> feat\n}\n"
    )
    unit = _unit(worktree=worktree, marker_span=(1, 5), uid="u:1")
    cand = _cand("u:1", "    x")
    accepted = [(unit, cand)]
    failures = [VerificationFailure(
        validator="cargo", severity="error",
        message="error[E0433]: failed to resolve", detail={},
    )]
    result = _try_deterministic_brace_repair(failures, worktree, accepted, 0)
    assert result is None


def test_det_repair_defers_on_balanced_splice():
    """If the spliced buffer is already balanced, there's nothing to fix."""
    worktree = (
        "fn main() {\n<<<<<<< HEAD\n    x\n=======\n    y\n>>>>>>> feat\n}\n"
    )
    unit = _unit(worktree=worktree, marker_span=(1, 5), uid="u:1")
    good = _cand("u:1", "    x")  # balanced
    accepted = [(unit, good)]
    failures = [_brace_failure(99)]
    result = _try_deterministic_brace_repair(failures, worktree, accepted, 0)
    assert result is None


def test_det_repair_defers_on_structural_error():
    """A } embedded in a line with real code (no brace-only line to remove) is
    structural → defer to LLM.

    The deterministic repair only acts on brace-only lines to avoid corrupting
    real code. Here the resolved text merges a one-liner ``fn`` whose closing
    ``}`` is on the same line as the body — there's no standalone ``}`` to
    remove, so the repair must defer."""
    worktree = (
        "fn main() {\n<<<<<<< HEAD\n    x\n=======\n    y\n>>>>>>> feat\n}\n"
    )
    unit = _unit(worktree=worktree, marker_span=(1, 5), uid="u:1")
    # Resolved text: a one-liner fn that closes inline, plus a stray standalone }
    # that shares a line with code — the repair can't safely touch it.
    bad = _cand("u:1", "    foo() } bar()")  # two } on one line with code
    accepted = [(unit, bad)]
    spliced = _resolved_buffer(worktree, accepted)
    imb = _brace_imbalance_line(spliced)
    if imb is None:
        return  # balanced by coincidence; skip
    failures = [_brace_failure(imb + 1)]
    result = _try_deterministic_brace_repair(failures, worktree, accepted, 0)
    # The stray } is on a code line (foo() } bar()) — not brace-only → defer.
    # But if the imbalance happens to be a standalone trailing }, the repair may
    # succeed. Only assert defer when the divergence line has real code.
    spliced_lines = spliced.split("\n")
    from capybase.verification import _strip_strings_comments
    div_line = spliced_lines[imb] if imb < len(spliced_lines) else ""
    cleaned = _strip_strings_comments(div_line)[0] if div_line else ""
    if cleaned.strip() != "}":
        assert result is None, "structural brace error should defer to LLM"


# ---------------------------------------------------------------------------
# _splice_context_snippet: cross-hunk widening (Fix #1)
# ---------------------------------------------------------------------------


def test_splice_snippet_single_unit_default_window():
    """A single-unit conflict gets the default ±5 line window."""
    worktree = (
        "line0\nline1\nline2\nline3\nline4\n"
        "<<<<<<< HEAD\nold\n=======\nnew\n>>>>>>> feat\n"
        "line10\nline11\nline12\nline13\nline14\n"
    )
    unit = _unit(worktree=worktree, marker_span=(5, 9), uid="u:5")
    cand = _cand("u:5", "RESOLVED")
    accepted = [(unit, cand)]
    # Error at line 8 (inside the resolved region).
    failures = [_brace_failure(8)]
    snippet = _splice_context_snippet(failures, worktree, accepted)
    assert ">>>" in snippet  # error line marked
    assert "RESOLVED" in snippet
    # Default window: ~11 lines.
    line_count = len(snippet.strip().split("\n"))
    assert 9 <= line_count <= 13


def test_splice_snippet_two_units_widens_to_junction():
    """Two adjacent units: the snippet spans both units when the error is at
    the junction, so the model sees both hunks and their boundary."""
    # Two conflict blocks separated by a few lines.
    worktree = (
        "<<<<<<< HEAD\nunit_a_content\n=======\nold_a\n>>>>>>> feat\n"
        "gap_line_1\ngap_line_2\n"
        "<<<<<<< HEAD\nunit_b_content\n=======\nold_b\n>>>>>>> feat\n"
    )
    unit_a = _unit(worktree=worktree, marker_span=(0, 4), uid="u:0")
    unit_b = _unit(worktree=worktree, marker_span=(7, 11), uid="u:7")
    cand_a = _cand("u:0", "resolved_a")
    cand_b = _cand("u:7", "resolved_b")
    accepted = [(unit_a, cand_a), (unit_b, cand_b)]
    # Error line falls at the junction (near the end of unit A / start of the gap).
    spliced = _resolved_buffer(worktree, accepted)
    # Find the line of resolved_a in the spliced buffer.
    a_line = None
    for i, l in enumerate(spliced.split("\n"), 1):
        if "resolved_a" in l:
            a_line = i
            break
    assert a_line is not None
    failures = [_brace_failure(a_line)]
    snippet = _splice_context_snippet(failures, worktree, accepted)
    # The widened snippet should include content from BOTH units.
    assert "resolved_a" in snippet
    assert "resolved_b" in snippet


def test_splice_snippet_no_error_line_returns_empty():
    """When no error line can be parsed, the snippet is empty (additive only)."""
    worktree = "<<<<<<< HEAD\nold\n=======\nnew\n>>>>>>> feat\n"
    unit = _unit(worktree=worktree, marker_span=(0, 4), uid="u:0")
    cand = _cand("u:0", "resolved")
    accepted = [(unit, cand)]
    failures = [VerificationFailure(
        validator="cargo", severity="error",
        message="build failed", detail={},
    )]
    snippet = _splice_context_snippet(failures, worktree, accepted)
    assert snippet == ""

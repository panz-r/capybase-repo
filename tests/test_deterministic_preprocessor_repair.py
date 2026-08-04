"""Deterministic preprocessor repair + cross-unit attribution (Layer 1 + Layer 2).

Layer 1: ``_try_balance_preprocessor`` + ``_try_deterministic_preprocessor_repair``
fix a whole-file ``#if/#endif`` imbalance deterministically, skipping the LLM
call when a single edit (remove a stray bare ``#endif``, append a missing
``#endif``) fully balances the spliced file. This is the cross-unit analogue of
the brace repair — the entity-splitting + splice pipeline can leave a
preprocessor imbalance that no single sub-unit owns (a conflict region sliced
mid-file whose matching directive is upstream/downstream of the marker block).

Layer 2: ``_attribute_whole_file_failure`` now reads the
``preprocessor_imbalance_line`` detail key (previously only salvaged via the
message regex), and ``_splice_context_snippet`` widens its window to the
enclosing ``#if/#endif`` region so the model sees the conditional context.
"""

from __future__ import annotations

from capybase.conflict_model import ConflictSide, ConflictUnit, CandidateResolution
from capybase.orchestrator import (
    _attribute_whole_file_failure,
    _resolved_buffer,
    _splice_context_snippet,
    _try_deterministic_preprocessor_repair,
)
from capybase.verification import (
    VerificationFailure,
    _preprocessor_imbalance_line,
    _try_balance_preprocessor,
)


def _unit(*, worktree, marker_span, uid="u", language="c"):
    return ConflictUnit(
        session_id="s", step_index=0, path="a.c", language=language,
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


def _pp_failure(line: int) -> VerificationFailure:
    return VerificationFailure(
        validator="syntax", severity="error",
        message=(
            f"splice coherence: unbalanced preprocessor directives "
            f"at line {line} (missing #endif or extra #endif)"
        ),
        detail={"preprocessor_imbalance_line": line},
    )


# ---------------------------------------------------------------------------
# _try_balance_preprocessor: direct unit tests of the balancer
# ---------------------------------------------------------------------------

def test_balance_removes_stray_endif():
    """An extra bare #endif (depth goes negative) is removed in one edit.

    The stray #endif must NOT be the last directive (a trailing stray #endif
    is the truncated-slice signature, which defers to the model). Here real
    content follows the stray, so removal is unambiguous."""
    text = "\n".join([
        "#ifdef X",
        "int a(void){return 0;}",
        "#endif",
        "#endif",  # stray — depth goes -1
        "int after(void){return 1;}",  # real content after the stray
    ])
    assert _preprocessor_imbalance_line(text) is not None
    result = _try_balance_preprocessor(text)
    assert result is not None
    assert _preprocessor_imbalance_line(result) is None
    # The stray #endif line was removed.
    assert result.count("#endif") == 1


def test_balance_appends_missing_endif():
    """An unclosed #if (depth ends positive) is closed by appending #endif."""
    text = "\n".join([
        "int before(void){return 0;}",
        "#ifdef SQLITE_TEST",
        "int test_only(void){return 1;}",
        # no #endif — depth ends at +1
    ])
    assert _preprocessor_imbalance_line(text) is not None
    result = _try_balance_preprocessor(text)
    assert result is not None
    assert _preprocessor_imbalance_line(result) is None
    # A #endif was appended.
    assert result.rstrip().endswith("#endif")


def test_balance_appends_multiple_missing_endifs():
    """Multiple unclosed #if directives get their deficit of #endif appended."""
    text = "\n".join([
        "#if defined(A)",
        "#if defined(B)",
        "int nested(void){return 0;}",
        # two unclosed #ifs — depth ends at +2
    ])
    assert _preprocessor_imbalance_line(text) is not None
    result = _try_balance_preprocessor(text)
    assert result is not None
    assert _preprocessor_imbalance_line(result) is None


def test_balance_defers_on_directive_with_code():
    """A stray #endif sharing a line with real code is structural → defer.

    The balancer only acts on directive-only lines to avoid corrupting real
    code. Here the extra #endif is glued to a statement, so removal would
    drop code → return None."""
    text = "\n".join([
        "#ifdef X",
        "int a(void){return 0;}",
        "#endif int stray_code;",  # not a bare #endif
        "int after(void){return 1;}",  # content after, so not a truncated slice
    ])
    result = _try_balance_preprocessor(text)
    assert result is None


def test_balance_defers_on_truncated_slice():
    """A stray #endif at the END of the file (nothing after it) is the signature
    of a truncated mid-file slice — the text is missing the content that would
    have rebalanced the count. Removing the trailing #endif would balance the
    depth count but is semantically WRONG (the real fix is missing content the
    model must generate). Defer to the LLM path.

    This is the sqlite-0040 case: the current side had 143 preprocessor
    directives vs the oracle's 240 — a truncated slice where the trailing
    #endif is the last line."""
    text = "\n".join([
        "#ifdef X",
        "int a(void){return 0;}",
        "#endif",
        "#endif /* TCLSH */",  # stray, at EOF — truncated slice
    ])
    assert _preprocessor_imbalance_line(text) is not None
    result = _try_balance_preprocessor(text)
    assert result is None  # defer — the model must generate the missing content


def test_balance_defers_on_balanced_text():
    """Already-balanced text returns None (nothing to fix)."""
    text = "\n".join([
        "#ifdef X",
        "int a(void){return 0;}",
        "#endif",
    ])
    assert _preprocessor_imbalance_line(text) is None
    assert _try_balance_preprocessor(text) is None


def test_balance_defers_on_empty():
    assert _try_balance_preprocessor("") is None


# ---------------------------------------------------------------------------
# _try_deterministic_preprocessor_repair: orchestrator primitive (Layer 1)
# ---------------------------------------------------------------------------

def test_det_repair_single_unit_stray_endif():
    """A single-unit C conflict whose resolution has a stray #endif is fixed
    without an LLM call. The deterministic repair operates on the spliced
    buffer and returns a whole-file unit carrying the repaired text."""
    worktree = "\n".join([
        "int head(void){return 0;}",       # 0
        "<<<<<<<",                         # 1
        "#ifdef X",                        # 2
        "int a(void){return 1;}",          # 3
        "#endif",                          # 4
        "=======",                         # 5
        "#ifdef X",                        # 6
        "int a(void){return 2;}",          # 7
        "#endif",                          # 8
        ">>>>>>>",                         # 9
        "int tail(void){return 3;}",       # 10
    ])
    unit = _unit(worktree=worktree, marker_span=(1, 9), uid="u:1")
    bad = _cand("u:1", "#ifdef X\nint a(void){return 2;}\n#endif\n#endif")  # stray
    accepted = [(unit, bad)]
    spliced = _resolved_buffer(worktree, accepted)
    imb = _preprocessor_imbalance_line(spliced)
    assert imb is not None
    failures = [_pp_failure(imb + 1)]
    fault_idx = _attribute_whole_file_failure(failures, [unit])
    result = _try_deterministic_preprocessor_repair(failures, worktree, accepted, fault_idx)
    assert result is not None, "should deterministically repair"
    u_r, c_r = result[0]
    assert u_r.unit_kind == "whole_file"
    assert u_r.marker_span is None
    assert c_r.provenance == "deterministic_preprocessor_repair"
    re_spliced = _resolved_buffer(worktree, result)
    assert _preprocessor_imbalance_line(re_spliced) is None


def test_det_repair_single_unit_unclosed_if():
    """A single-unit C conflict with an unclosed #if is fixed by appending #endif."""
    worktree = "\n".join([
        "<<<<<<<",
        "#ifdef X",
        "int a(void){return 1;}",
        "#endif",
        "=======",
        "#ifdef X",
        "int a(void){return 2;}",
        ">>>>>>>",   # replayed drops the #endif
    ])
    unit = _unit(worktree=worktree, marker_span=(0, 7), uid="u:1")
    bad = _cand("u:1", "#ifdef X\nint a(void){return 2;}")
    accepted = [(unit, bad)]
    spliced = _resolved_buffer(worktree, accepted)
    imb = _preprocessor_imbalance_line(spliced)
    assert imb is not None
    failures = [_pp_failure(imb + 1)]
    result = _try_deterministic_preprocessor_repair(failures, worktree, accepted, 0)
    assert result is not None
    re_spliced = _resolved_buffer(worktree, result)
    assert _preprocessor_imbalance_line(re_spliced) is None


def test_det_repair_defers_on_non_preprocessor_failure():
    """A brace/cargo error is NOT a preprocessor failure → defer."""
    worktree = "<<<<<<<\nint x;\n=======\nint y;\n>>>>>>>"
    unit = _unit(worktree=worktree, marker_span=(0, 4), uid="u:1")
    cand = _cand("u:1", "int x;")
    accepted = [(unit, cand)]
    failures = [VerificationFailure(
        validator="cargo", severity="error",
        message="error[E0433]: failed to resolve", detail={},
    )]
    result = _try_deterministic_preprocessor_repair(failures, worktree, accepted, 0)
    assert result is None


def test_det_repair_defers_on_non_c_language():
    """Preprocessor repair is C/C++ only — a Rust file defers."""
    worktree = "<<<<<<<\nfn x(){}\n=======\nfn y(){}\n>>>>>>>"
    unit = _unit(worktree=worktree, marker_span=(0, 4), uid="u:1", language="rust")
    cand = _cand("u:1", "fn x(){}")
    accepted = [(unit, cand)]
    failures = [_pp_failure(1)]
    result = _try_deterministic_preprocessor_repair(failures, worktree, accepted, 0)
    assert result is None


def test_det_repair_defers_on_balanced_splice():
    """If the spliced buffer is already balanced, there's nothing to fix."""
    worktree = "<<<<<<<\n#ifdef X\nint a;\n#endif\n=======\n#ifdef X\nint b;\n#endif\n>>>>>>>"
    unit = _unit(worktree=worktree, marker_span=(0, 8), uid="u:1")
    good = _cand("u:1", "#ifdef X\nint b;\n#endif")
    accepted = [(unit, good)]
    failures = [_pp_failure(99)]
    result = _try_deterministic_preprocessor_repair(failures, worktree, accepted, 0)
    assert result is None


# ---------------------------------------------------------------------------
# Layer 2: attribution detail-key read
# ---------------------------------------------------------------------------

def test_attribution_reads_preprocessor_detail_key():
    """_attribute_whole_file_failure reads the preprocessor_imbalance_line
    detail key directly (not just the message regex)."""
    worktree = "\n".join([
        "<<<<<<<",            # 0
        "#ifdef X",           # 1
        "int a;",             # 2
        "#endif",             # 3
        "=======",            # 4
        "#ifdef X",           # 5
        "int b;",             # 6
        ">>>>>>>",            # 7
    ])
    unit = _unit(worktree=worktree, marker_span=(0, 7), uid="u:1")
    # Error at line 3 (1-based) is inside the unit's span [0,7].
    failures = [_pp_failure(3)]
    idx = _attribute_whole_file_failure(failures, [unit])
    assert idx == 0


def test_attribution_returns_neg1_for_outside_span_preprocessor():
    """When the preprocessor imbalance line is outside ALL unit spans,
    attribution returns -1 (the cross-unit case). The caller's nearest-
    preceding-unit fallback handles this in _whole_file_repair."""
    worktree = "\n".join([
        "<<<<<<<",     # 0  span (0,2)
        "int a;",      # 1
        "=======",     # 2  (oops, tiny span for the test)
        "int b;",      # 3
        ">>>>>>>",     # 4
        "#endif",      # 5  stray, OUTSIDE the span
    ])
    unit = _unit(worktree=worktree, marker_span=(0, 2), uid="u:1")
    failures = [_pp_failure(6)]  # line 6 is outside span [0,2]
    idx = _attribute_whole_file_failure(failures, [unit])
    assert idx == -1


# ---------------------------------------------------------------------------
# Layer 2: splice-context snippet preprocessor widening
# ---------------------------------------------------------------------------

def test_snippet_widens_to_enclosing_conditional():
    """For a preprocessor imbalance, the splice-context snippet widens to
    include the enclosing #if/#endif region (not just ±5 lines)."""
    # Build a spliced buffer where the #if is far before the error line.
    lines = ["int header(void){return 0;}"]  # 1
    lines += [""] * 10                        # 2-11 (padding beyond ±5)
    lines += ["#ifdef X"]                     # 12  (the enclosing #if)
    lines += ["int guarded(void){return 1;}"] # 13
    lines += ["#endif"]                       # 14
    worktree = "\n".join(lines)
    unit = _unit(worktree=worktree, marker_span=(0, 0), uid="u:1")
    cand = _cand("u:1", lines[0])
    accepted = [(unit, cand)]
    # Pretend the imbalance is at line 13 (inside the conditional).
    failures = [_pp_failure(13)]
    snippet = _splice_context_snippet(failures, worktree, accepted)
    assert snippet != ""
    # The widening must reach back to the #ifdef (line 12), which a ±5 window
    # from line 13 would NOT include (12 is within ±5 here, so also check it
    # includes the directive explicitly).
    assert "#ifdef X" in snippet


def test_snippet_includes_endif_after_error():
    """The widening reaches forward to the #endif that closes the open conditional."""
    lines = ["#ifdef X"]                      # 1
    lines += ["int guarded(void){return 1;}"] # 2
    lines += [""] * 12                        # 3-14 (padding beyond ±5)
    lines += ["#endif"]                       # 15
    worktree = "\n".join(lines)
    unit = _unit(worktree=worktree, marker_span=(0, 0), uid="u:1")
    cand = _cand("u:1", lines[0])
    accepted = [(unit, cand)]
    failures = [_pp_failure(2)]  # error near the #if; #endif is far below
    snippet = _splice_context_snippet(failures, worktree, accepted)
    assert "#endif" in snippet

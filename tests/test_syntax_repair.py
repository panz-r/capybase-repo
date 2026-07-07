"""Per-unit syntax checks + targeted repair retries (CEGIS loop hardening).

PythonSyntaxValidator + RustSyntaxValidator catch code syntax errors on the
FIRST candidate (before Phase B), surfacing them as hard failures that seed
PROMPT_REPAIR — the model sees the broken candidate + the compile diagnostic and
gets up to max_retries_per_unit (default 2) targeted fix attempts.
"""

from __future__ import annotations

import pytest

from capybase.conflict_model import (
    ConflictSide,
    ConflictUnit,
    CandidateResolution,
)
from capybase.verification import (
    VerificationContext,
    PythonSyntaxValidator,
    RustSyntaxValidator,
    _braces_balanced,
    _is_rust_resolution_error,
)
from capybase.config import ValidationConfig


def _unit(*, base="", current="", replayed="", worktree=None, language="python",
          marker_span=(0, 0)):
    wt = worktree if worktree is not None else base
    return ConflictUnit(
        session_id="s", step_index=0, path="a.py" if language == "python" else "a.rs",
        language=language, conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text=wt, marker_span=marker_span,
    )


def _candidate(resolved=""):
    return CandidateResolution(
        candidate_id="c", unit_id="u", model_name="m",
        prompt_version="v", resolved_text=resolved,
    )


def _verify(validator, unit, candidate):
    ctx = VerificationContext(unit=unit, candidate=candidate, config=ValidationConfig())
    return validator.verify(ctx)


# ---------------------------------------------------------------------------
# _braces_balanced
# ---------------------------------------------------------------------------


def test_braces_balanced_valid():
    assert _braces_balanced("pub fn f() {\n    let x = { 1 };\n}\n") is True


def test_braces_balanced_unbalanced():
    assert _braces_balanced("Config {\n    x: 1,\n") is False  # missing closing }


def test_braces_balanced_ignores_strings_and_comments():
    # Brace inside a string literal and a comment should not count.
    text = 'let s = "{ unbalanced }"; // } extra\nfn f() {}\n'
    assert _braces_balanced(text) is True


def test_braces_balanced_empty():
    assert _braces_balanced("") is True


# ---------------------------------------------------------------------------
# _is_rust_resolution_error (semantic filter)
# ---------------------------------------------------------------------------


def test_semantic_error_detected():
    assert _is_rust_resolution_error("error[E0433]: failed to resolve") is True
    assert _is_rust_resolution_error("error[E0063]: missing field") is True


def test_syntax_error_not_semantic():
    assert _is_rust_resolution_error("error: expected `;`, found `format`") is False
    assert _is_rust_resolution_error("error: unterminated string literal") is False


# ---------------------------------------------------------------------------
# PythonSyntaxValidator
# ---------------------------------------------------------------------------


def test_python_syntax_catches_unclosed_bracket():
    """A candidate with an unclosed bracket → hard failure."""
    v = PythonSyntaxValidator()
    unit = _unit(
        base="def f():\n    return 1\n",
        current="def f():\n    return 1\n",
        replayed="def f():\n    return 1\n",
        worktree="def f():\n<<<<<<<\n    return 1\n=======\n    return 2\n>>>>>>>\n",
        marker_span=(1, 5),
    )
    cand = _candidate(resolved="    return [1, 2,")  # unclosed bracket
    res = _verify(v, unit, cand)
    assert not res.passed
    assert res.features["python_syntax_checked"] is True
    assert res.features["syntax_passed"] is False


def test_python_syntax_passes_valid_code():
    v = PythonSyntaxValidator()
    unit = _unit(
        base="def f():\n    return 1\n",
        current="def f():\n    return 1\n",
        replayed="def f():\n    return 1\n",
        worktree="def f():\n<<<<<<<\n    return 1\n=======\n    return 2\n>>>>>>>\n",
        marker_span=(1, 5),
    )
    cand = _candidate(resolved="    return [1, 2]")
    res = _verify(v, unit, cand)
    assert res.passed
    assert res.features["syntax_passed"] is True


def test_python_syntax_skips_non_python():
    v = PythonSyntaxValidator()
    unit = _unit(language="rust", base="fn f() {}", marker_span=(0, 0))
    cand = _candidate(resolved="fn f() {}")
    res = _verify(v, unit, cand)
    assert res.passed
    assert res.features["python_syntax_checked"] is False


def test_python_syntax_skips_empty_resolved():
    v = PythonSyntaxValidator()
    unit = _unit(marker_span=(0, 0))
    cand = _candidate(resolved="")
    res = _verify(v, unit, cand)
    assert res.passed
    assert res.features["python_syntax_checked"] is False


def test_python_syntax_multi_unit_safe():
    """Sibling conflict markers are blanked so the parse isn't corrupted."""
    v = PythonSyntaxValidator()
    # A multi-hunk file: the candidate resolves hunk 1, hunk 2's raw markers remain.
    worktree = (
        "def f():\n<<<<<<<\n    return 1\n=======\n    return 2\n>>>>>>>\n\n"
        "def g():\n<<<<<<<\n    return 3\n=======\n    return 4\n>>>>>>>\n"
    )
    unit = _unit(
        base="def f():\n    return 1\n\ndef g():\n    return 3\n",
        worktree=worktree, marker_span=(1, 5),
    )
    cand = _candidate(resolved="    return 2")
    res = _verify(v, unit, cand)
    # The sibling markers are blanked to comments, so the candidate's valid code
    # parses fine (not false-failed by the raw markers).
    assert res.passed


# ---------------------------------------------------------------------------
# RustSyntaxValidator
# ---------------------------------------------------------------------------


rustc = pytest.importorskip("shutil").which("rustc")
skip_no_rustc = pytest.mark.skipif(rustc is None, reason="rustc not installed")


@skip_no_rustc
def test_rust_syntax_catches_malformed_format():
    """The exact live-eval failure: a newline inside a format! string literal."""
    v = RustSyntaxValidator()
    worktree = (
        'pub fn label(&self) -> String {\n'
        '<<<<<<<\n    format!("x")\n=======\n    format!("y")\n>>>>>>>\n'
        '}\n'
    )
    unit = _unit(
        language="rust",
        base='pub fn label(&self) -> String {\n    format!("x")\n}\n',
        worktree=worktree, marker_span=(1, 5),
    )
    # Malformed: newline inside the string literal
    cand = _candidate(resolved='    format!("{}\n    (retries={})", a, b)')
    res = _verify(v, unit, cand)
    assert not res.passed
    assert res.features["rust_syntax_checked"] is True


@skip_no_rustc
def test_rust_syntax_passes_valid_code():
    v = RustSyntaxValidator()
    # A free function (no &self) so standalone rustc accepts it without an impl.
    worktree = (
        'pub fn label() -> String {\n'
        '<<<<<<<\n    format!("x")\n=======\n    format!("y")\n>>>>>>>\n'
        '}\n'
    )
    unit = _unit(
        language="rust",
        base='pub fn label() -> String {\n    format!("x")\n}\n',
        worktree=worktree, marker_span=(1, 5),
    )
    cand = _candidate(resolved='    format!("[{}] (retries={})", "name", 5)')
    res = _verify(v, unit, cand)
    assert res.passed, res.message


@skip_no_rustc
def test_rust_syntax_brace_guard_skips_partial_context():
    """A candidate that produces unbalanced braces when spliced → skip (no false fail)."""
    v = RustSyntaxValidator()
    # The candidate is just struct-init lines; spliced without the surrounding
    # impl/fn, the braces are unbalanced → the guard skips the compile.
    worktree = (
        "impl Config {\n    pub fn new() -> Self {\n        Config {\n"
        "<<<<<<<\n            x: 1,\n=======\n            y: 2,\n>>>>>>>\n"
        "        }\n    }\n}\n"
    )
    unit = _unit(
        language="rust",
        base="impl Config {\n    pub fn new() -> Self {\n        Config {\n            x: 1,\n        }\n    }\n}\n",
        worktree=worktree, marker_span=(3, 5),
    )
    cand = _candidate(resolved="            x: 1,\n            y: 2,")
    res = _verify(v, unit, cand)
    # The brace guard should skip (the full-file splice IS balanced, but if the
    # marker span produces an unbalanced fragment, it defers). This test confirms
    # the guard doesn't false-fail a correct merge.
    assert res.passed  # either skipped or passed — never a false failure


@skip_no_rustc
def test_rust_syntax_semantic_filter_defers_resolution_errors():
    """An E0xxx (semantic) error is NOT a syntax defect → defer to Phase B."""
    v = RustSyntaxValidator()
    # A struct with a duplicate field (E0062) — semantic, not syntax.
    worktree = (
        "struct S {\n    a: i32,\n<<<<<<<\n    b: i32,\n=======\n    b: i32,\n>>>>>>>\n}\n"
    )
    unit = _unit(
        language="rust",
        base="struct S {\n    a: i32,\n    b: i32,\n}\n",
        worktree=worktree, marker_span=(1, 5),
    )
    cand = _candidate(resolved="    b: i32,\n    b: i32,")  # duplicate field
    res = _verify(v, unit, cand)
    # E0062 is semantic → filtered → passed (deferred to Phase B cargo check)
    assert res.passed


def test_rust_syntax_skips_non_rust():
    v = RustSyntaxValidator()
    unit = _unit(language="python", marker_span=(0, 0))
    cand = _candidate(resolved="x = 1")
    res = _verify(v, unit, cand)
    assert res.passed
    assert res.features["rust_syntax_checked"] is False

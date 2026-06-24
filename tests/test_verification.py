from capybase.conflict_model import (
    CandidateResolution,
    ConflictSide,
    ConflictUnit,
)
from capybase.verification import ValidationConfig, VerificationEngine


def _unit(base, current, replayed, worktree, span=(1, 5)):
    return ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text=worktree, marker_span=span,
    )


def _candidate(resolved, needs_human=False):
    return CandidateResolution(
        candidate_id="c", unit_id="u", model_name="m", prompt_version="resolve_text_block.v2",
        resolved_text=resolved, needs_human=needs_human,
    )


def _engine():
    return VerificationEngine.default(ValidationConfig())


def test_passes_clean_resolution():
    worktree = "def f():\n<<<<<<< H\n    return 1\n=======\n    return 2\n>>>>>>> b\n"
    unit = _unit("def f():\n    pass", "    return 1", "    return 2", worktree)
    cand = _candidate("    return 1 + 2")
    res = _engine().verify(unit, cand)
    assert res.passed, res.hard_failures
    assert not res.features["markers_remaining"]


def test_fails_on_remaining_markers():
    worktree = "def f():\n<<<<<<< H\n    return 1\n=======\n    return 2\n>>>>>>> b\n"
    unit = _unit("def f():\n    pass", "    return 1", "    return 2", worktree)
    cand = _candidate("    x\n<<<<<<< still here\n")
    res = _engine().verify(unit, cand)
    assert not res.passed


def test_fails_on_needs_human():
    worktree = "x\n<<<<<<< H\na\n=======\nb\n>>>>>>> c\n"
    unit = _unit("x", "a", "b", worktree)
    cand = _candidate("merged", needs_human=True)
    res = _engine().verify(unit, cand)
    assert not res.passed
    assert any(f.validator == "needs_human" for f in res.hard_failures)


def test_flags_copying_one_side():
    worktree = "x\n<<<<<<< H\ncur\n=======\nrep\n>>>>>>> b\n"
    unit = _unit("x", "cur", "rep", worktree)
    # resolved == current side verbatim
    cand = _candidate("cur")
    res = _engine().verify(unit, cand)
    # warning-level, not hard failure
    assert any(w.validator == "preservation_heuristic" for w in res.warnings)


def test_exact_splice_scope_rejects_outside_edits():
    worktree = "l1\nl2\n<<<<<<< H\na\n=======\nb\n>>>>>>> b\nl5\nl6\n"
    unit = _unit("x", "a", "b", worktree, span=(2, 6))
    # A valid merged text keeps outside lines intact.
    cand = _candidate("merged")
    res = _engine().verify(unit, cand)
    assert res.passed


def test_syntax_check_python():
    # Syntax checking now lives in Phase B (verify_file), not per-unit, so it
    # runs against the fully-spliced file. Per-unit verify no longer sets
    # syntax_checked (that feature moved to verify_file).
    worktree = "def f():\n<<<<<<< H\n    return 1\n=======\n    return 2\n>>>>>>> b\n"
    unit = _unit("def f():\n    pass", "    return 1", "    return 2", worktree)
    cand = _candidate("    return 3")
    # Per-unit result: valid, but syntax_checked is absent (Phase A).
    res = _engine().verify(unit, cand)
    assert res.passed
    assert "syntax_checked" not in res.features

    # Phase B: whole-file syntax on the spliced result.
    fres = _engine().verify_file(
        unit.path, unit.language, unit.original_worktree_text,
        [(unit.marker_span, cand.resolved_text)],
    )
    assert fres.features.get("syntax_checked") is True
    assert fres.passed, fres.hard_failures


def test_verify_file_clean_multi_unit():
    """A two-hunk Python file, both resolved: whole file compiles, no markers."""
    # Two functions each with a one-line conflict, separated by a blank line.
    worktree = (
        "def a():\n<<<<<<< H\n    return 1\n=======\n    return 2\n>>>>>>> b\n"
        "\n"
        "def c():\n<<<<<<< H\n    return 3\n=======\n    return 4\n>>>>>>> b\n"
    )
    # spans: block1 = (1,5), block2 = (8,12)
    fres = _engine().verify_file(
        "app.py", "python", worktree,
        [((1, 5), "    return 1 + 2"), ((8, 12), "    return 3 + 4")],
    )
    assert fres.passed, fres.hard_failures
    assert fres.features.get("syntax_checked") is True
    assert fres.features.get("syntax_passed") is True
    assert fres.features.get("whole_file_markers_remaining") == 0


def test_verify_file_catches_cross_unit_syntax_error():
    """The core Phase B win: two resolutions that are valid Python each, but
    produce invalid code when juxtaposed. Per-unit validation could never
    catch this because each only saw one block spliced into a file whose
    other block was still raw markers."""
    # Two adjacent ``return`` statements at module top level (no enclosing def)
    # are individually fine lines, but together are a SyntaxError.
    worktree = (
        "<<<<<<< H\nreturn 1\n=======\nreturn 2\n>>>>>>> b\n"
        "<<<<<<< H\nreturn 3\n=======\nreturn 4\n>>>>>>> b\n"
    )
    fres = _engine().verify_file(
        "app.py", "python", worktree,
        [((0, 4), "return 1"), ((5, 9), "return 3")],
    )
    assert not fres.passed
    assert any(f.validator == "syntax" for f in fres.hard_failures)


def test_verify_file_catches_leaked_markers():
    """A resolution that itself smuggles in markers is caught at file level."""
    worktree = "<<<<<<< H\na\n=======\nb\n>>>>>>> b\n"
    fres = _engine().verify_file(
        "app.py", "python", worktree,
        [((0, 4), "x\n<<<<<<< sneaky\n")],
    )
    assert not fres.passed
    assert any(f.validator == "whole_file_markers" for f in fres.hard_failures)

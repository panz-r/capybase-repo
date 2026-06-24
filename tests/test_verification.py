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
    worktree = "def f():\n<<<<<<< H\n    return 1\n=======\n    return 2\n>>>>>>> b\n"
    unit = _unit("def f():\n    pass", "    return 1", "    return 2", worktree)
    # valid python merge
    cand = _candidate("    return 3")
    res = _engine().verify(unit, cand)
    assert res.features.get("syntax_checked") is True
    assert res.passed

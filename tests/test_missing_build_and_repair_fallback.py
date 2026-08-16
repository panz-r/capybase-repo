"""Missing-build-system classification + repair side fallback.

protobuf-0043's oscillation (PASS 1.0000 ↔ ESCALATE 0.9424): Phase 2's
build invocation ran where no Makefile exists (a rebase worktree carries
tracked sources, not generated build artifacts) — ``make: *** No targets
specified and no makefile found``. Treated as a hard failure it poisoned
every whole-file candidate AND the repair feedback; the model sensibly
declined the meaningless feedback three times and the rebase escalated.

Two deterministic fixes, pinned here:
- ``_is_missing_build_system``: the build check is UNAVAILABLE, not failed.
- ``_empty_repair_side_fallback``: when a repair re-resolution dies (empty
  model output), fall back to the file's majority side instead of
  escalating the whole rebase.
"""

from __future__ import annotations

from capybase.conflict_model import CandidateResolution, ConflictSide, ConflictUnit
from capybase.orchestrator import _empty_repair_side_fallback
from capybase.verification import _is_missing_build_system


def test_missing_build_system_detected():
    assert _is_missing_build_system(
        "make: *** No targets specified and no makefile found.  Stop.")
    assert _is_missing_build_system("some prefix; no Makefile found. Stop.")
    assert _is_missing_build_system("CMake Error: can't find cmake cache")


def test_real_build_failures_not_classified_as_missing():
    # A build that RAN and reported compile errors is a real signal.
    assert not _is_missing_build_system(
        "foo.cc:12:5: error: expected ';' after expression")
    assert not _is_missing_build_system(
        "make: *** [Makefile:512: foo.o] Error 1")
    assert not _is_missing_build_system("")


# ---------------------------------------------------------------------------
# _empty_repair_side_fallback
# ---------------------------------------------------------------------------

def _unit(uid, cur_text, rep_text):
    return ConflictUnit(
        session_id="s", step_index=0, path="f.cc", language="cpp",
        unit_id=uid, unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=""),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=cur_text),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=rep_text),
        original_worktree_text="x", marker_span=(0, 0),
    )


def _cand(uid, text, provenance="llm"):
    return CandidateResolution(
        candidate_id=f"{uid}:c", unit_id=uid, model_name="t",
        prompt_version="t", resolved_text=text, provenance=provenance)


def test_fallback_takes_majority_side():
    units = [
        _unit("u0", "cur0", "rep0"),
        _unit("u1", "cur1", "rep1"),
        _unit("u2", "cur2", "rep2"),
    ]
    accepted = [
        (units[0], _cand("u0", "cur0", "deterministic_source_current_only")),
        (units[1], _cand("u1", "cur1", "deterministic_source_current_only")),
        # The escalated unit's LLM text differs from both sides.
        (units[2], _cand("u2", "mixed junk")),
    ]
    out = _empty_repair_side_fallback(accepted)
    assert out is not None
    assert [c.resolved_text for _, c in out] == ["cur0", "cur1", "cur2"]
    # Only the swapped unit gets fallback provenance; units already on the
    # majority side keep their original candidates untouched.
    assert "current_only_fallback" in out[2][1].provenance
    assert out[0][1] is accepted[0][1]
    assert out[1][1] is accepted[1][1]


def test_fallback_tie_breaks_to_current():
    units = [_unit("u0", "cur0", "rep0"), _unit("u1", "cur1", "rep1")]
    accepted = [
        (units[0], _cand("u0", "junk", "deterministic_source_current_only")),
        (units[1], _cand("u1", "junk", "deterministic_source_replayed_only")),
    ]
    out = _empty_repair_side_fallback(accepted)
    assert out is not None
    assert [c.resolved_text for _, c in out] == ["cur0", "cur1"]


def test_fallback_declines_when_already_that_side():
    units = [_unit("u0", "cur0", "rep0")]
    accepted = [(units[0], _cand("u0", "cur0"))]
    assert _empty_repair_side_fallback(accepted) is None

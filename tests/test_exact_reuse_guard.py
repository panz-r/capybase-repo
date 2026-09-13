"""s27-72 (sixth pass): exact_reuse must never replay a whole-file
record into a marker region (the convergence-seed region/file inversion)."""
from __future__ import annotations

from capybase.conflict_model import ConflictSide, ConflictUnit


def _unit(cur: str, rep: str) -> ConflictUnit:
    return ConflictUnit(
        session_id="s", step_index=1, path="a.py", language="python",
        conflict_type="UU", unit_id="a.py:1:0", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="x = 1"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=cur),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=rep),
        original_worktree_text="", marker_span=(0, 2),
    )


def test_exact_reuse_rejects_whole_file_on_region(tmp_path):
    """s27-72: a stored Experience whose resolved text is whole-file-scaled
    vs the query region is a region/file inversion record (the
    convergence-seed poison) — near-miss, never a replay candidate."""
    from capybase.exact_reuse import find_exact_reuse
    from capybase.memory.store import Experience, ExperienceStore
    from capybase.memory.shape import conflict_shape_hash
    from capybase.conflict_model import HistoricalExample

    big = "\n".join(f"line_{i}" for i in range(500))
    shape = conflict_shape_hash(
        base="x = 1", current="added line", replayed="other line")
    exp = Experience(
        example=HistoricalExample(
            summary="seeded changelog", base="x = 1",
            current="added line", replayed="other line", resolved=big),
        outcome="accepted",
        path="CHANGELOG.md",
        region_kind="text",
        conflict_shape=shape,
        validator_features={"introduced_diagnostics": 0},
    )
    store = ExperienceStore(tmp_path / "experiences.jsonl")
    store.append(exp)

    unit = _unit("added line", "other line")
    got = find_exact_reuse(
        unit=unit, store=store, language=None, region_kind="text",
        path="CHANGELOG.md")
    # either no candidate or a skip sentinel — never the 500-line replay
    assert got is None or not got.resolved_text, got
    assert got is not None and ">> region" in " ".join(got.near_misses)

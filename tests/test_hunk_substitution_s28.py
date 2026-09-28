"""S28-275(b) hunk-level substitution — the error-site mechanics.

The whole-file side-pick needs the ENTIRE other side's splice to verify —
structurally impossible when the other side carries its own era errors
(0127: the replayed splice fails its own Tokenizer ctor). The hunk rung
swaps ONLY the candidate's base-aligned region at the gate failure's
line for the other side's aligned region: 0127's two-edit gap class
(the receiver rename + the deleted ProgramMatcher block) is local
surgery through the sides' diff alignment.

The tests exercise the pure mechanics on the fixture's shapes: the
deletion (base had a block, the other side removed it, the candidate
kept it — the substitution removes it) and the rename (the other side's
aligned region names a different receiver). Plus the conservative
declines: insert-only regions, no-ops, oversized regions, and the
buffer-line → unit-local mapping.
"""

from __future__ import annotations

from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.orchestrator import (
    _hunk_substitute_at_line,
    _unit_local_line,
)


class _C:
    def __init__(self, text):
        self.resolved_text = text


def _unit(span, frag):
    return ConflictUnit(
        session_id="s", step_index=0, path="src/lexer.cpp", language="cpp",
        conflict_type="UU", unit_id="u1", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="x\n"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="x\n"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="x\n"),
        original_worktree_text="x\n", marker_span=span,
    ), _C(frag)


# ---------------------------------------------------------------------------
# the deletion shape (0127's ProgramMatcher block): the fragment carries
# the block plus surrounding candidate content — the rung is LOCAL surgery
# (the 60%-of-fragment cap declines whole-fragment swaps by design)
# ---------------------------------------------------------------------------

BASE = "\n".join(
    ["head;"] + [f"matcher_line_{i};" for i in range(1, 6)] + ["tail;"])
OTHER = "head;\ntail;\n"  # the other side DELETED the block
FRAG = "\n".join(
    ["head;"]
    + [f"matcher_line_{i};" for i in range(1, 6)]
    + ["tail;"]
    + [f"candidate_extra_{i};" for i in range(1, 11)])  # 15 lines total


def test_deleted_block_is_removed_at_the_error_site():
    out = _hunk_substitute_at_line(FRAG, BASE, OTHER, local_line=3)
    assert out is not None
    new, f0, f1 = out
    assert "matcher_line_3;" not in new
    assert "candidate_extra_1;" in new
    lines = new.split("\n")
    assert lines[0] == "head;" and lines[1] == "tail;"
    # the region covered exactly the block (frag lines 1..6 of the split)
    assert f0 == 1 and f1 == 6


def test_rename_shape_swaps_the_aligned_region():
    base = "\n".join(
        ["head;", "Parser p;", "p.state.field;", "tail;"]
        + [f"more_{i};" for i in range(10)])
    other = "\n".join(
        ["head;", "Parser p;", "p.cache.field;", "tail;"]
        + [f"more_{i};" for i in range(10)])
    frag = base
    out = _hunk_substitute_at_line(frag, base, other, local_line=2)
    assert out is not None
    new, _, _ = out
    assert "p.cache.field;" in new
    assert "p.state.field;" not in new


# ---------------------------------------------------------------------------
# the conservative declines
# ---------------------------------------------------------------------------

def test_fragment_only_insert_declines():
    # the error line sits in frag-only lines (no base alignment)
    base = "a;\nb;\n"
    other = "a;\nOTHER;\nb;\n"
    frag = "a;\nb;\nINVENTED_1;\nINVENTED_2;\n"
    assert _hunk_substitute_at_line(frag, base, other, local_line=2) is None


def test_other_side_agrees_at_the_error_site_declines():
    # the other side's only edit is elsewhere: no run contains the
    # error line's base position
    base = "a;\nb;\nc;\n"
    other = "a;\nb;\nC2;\n"
    frag = base
    assert _hunk_substitute_at_line(frag, base, other, local_line=0) is None


def test_noop_substitution_declines():
    base = "a;\nb;\n"
    other = "a;\nb;\n"  # identical to base: substitution is a no-op
    frag = "a;\nb;\n"
    assert _hunk_substitute_at_line(frag, base, other, local_line=0) is None


def test_oversized_region_declines():
    # the aligned region swallows >60% of the fragment: the side-pick's
    # territory, already declined
    base = "\n".join(f"l{i};" for i in range(10))
    other = "replaced_whole;\n"
    frag = "\n".join(f"l{i};" for i in range(10))
    assert _hunk_substitute_at_line(frag, base, other, local_line=4) is None


def test_out_of_range_line_declines():
    assert _hunk_substitute_at_line("a;\n", "a;\n", "b;\n", 5) is None
    assert _hunk_substitute_at_line("", "", "", 0) is None


# ---------------------------------------------------------------------------
# the buffer-line -> unit-local mapping
# ---------------------------------------------------------------------------

def test_map_single_unit():
    u, c = _unit((1, 2), "head;\nbody;\n")
    accepted = [(u, c)]
    # original: "l0\nSPAN0\nSPAN1\nSPAN2\ntail\n"; buffer = l0 + frag + tail
    assert _unit_local_line(accepted, 0) is None  # original's own line
    hit = _unit_local_line(accepted, 1)
    assert hit is not None and hit[2] == 0
    hit = _unit_local_line(accepted, 2)
    assert hit is not None and hit[2] == 1
    assert _unit_local_line(accepted, 4) is None  # the tail line


def test_map_multi_unit_orders_by_span():
    u1, c1 = _unit((0, 0), "A0;\n")
    u2, c2 = _unit((2, 3), "B0;\nB1;\n")
    accepted = [(u1, c1), (u2, c2)]
    # real splice semantics: a resolved text's trailing \n contributes an
    # empty final frag line — buffer: A0; '' mid B0; B1; ''
    assert _unit_local_line(accepted, 0)[2] == 0
    assert _unit_local_line(accepted, 1)[0] is u1  # the frag's own line
    assert _unit_local_line(accepted, 2) is None  # the original's mid line
    assert _unit_local_line(accepted, 3) == (u2, c2, 0)
    assert _unit_local_line(accepted, 4) == (u2, c2, 1)
    assert _unit_local_line(accepted, 5) == (u2, c2, 2)  # trailing empty
    assert _unit_local_line(accepted, 6) is None  # past the buffer


def test_none_line_declines():
    assert _unit_local_line([_unit((0, 0), "a;")], None) is None
    assert _unit_local_line([], 0) is None

"""Sprint-20 S20.8 — move-and-edit shape detection (journal-only stage).

One side moved a base block (>=70% verbatim line match at a different
position) while the other side edited the same base block in place.
Today's _try_move_transplant takes the mover's text wholesale — the
editor's delta is dropped. The detector measures the shape for the
enabling decision; no behavioral change.
"""

from __future__ import annotations

from capybase.structural_resolver import _detect_move_edit_shape


def _mk(n=12, tag=""):
    return [f"{tag}line{i} // {'x' * 20}" for i in range(n)]


# base: [A(20), X(8), B(20)] — X is the block that gets moved+edited.
# The stable surroundings are LONGER than X so the line diff anchors on
# them and expresses X's relocation as delete+insert (when the identical
# moved copy is the longest match, difflib anchors ON it and the move
# becomes invisible — the detector's documented boundary).
_A, _X, _B = _mk(20, "a"), _mk(8, "x"), _mk(20, "c")
BASE = _A + _X + _B


def test_moved_and_edited_block_detected():
    # current: X relocated to the END with new junk left in its place (the
    # relocation-visible shape — a pure order swap is ambiguous to a line
    # diff, see the detector's documented boundary); replayed: edited X
    # in place.
    cur = _A + _mk(9, "j") + _B + _X  # X moved to EOF, junk behind
    rep_lines = list(_X)
    rep_lines[3] = "xline3 EDITED-BY-REPLAYED"
    rep_lines[7] = "xline7 ALSO-EDITED"
    rep = _A + rep_lines + _B
    out = _detect_move_edit_shape(
        "\n".join(BASE), "\n".join(cur), "\n".join(rep))
    assert out is not None
    cand = out["candidates"][0]
    assert cand["mover"] == "current"
    assert cand["verbatim_ratio"] >= 0.70
    assert cand["editor_delta_lines"] >= 2
    assert cand["block_lines"] == 8


def test_no_editor_delta_not_a_candidate():
    # The block moved but nobody edited it — plain move, not move-and-edit.
    cur = _A + _mk(9, "j") + _B + _X
    out = _detect_move_edit_shape(
        "\n".join(BASE), "\n".join(cur), "\n".join(BASE))
    assert out is None


def test_no_move_not_a_candidate():
    # Both sides edit X in place; nothing relocated.
    cur_lines = list(_X)
    cur_lines[2] = "xline2 CUR-EDIT"
    rep_lines = list(_X)
    rep_lines[5] = "xline5 REP-EDIT"
    out = _detect_move_edit_shape(
        "\n".join(BASE),
        "\n".join(_A + cur_lines + _B),
        "\n".join(_A + rep_lines + _B))
    assert out is None


def test_reverse_direction_detected():
    # replayed is the mover, current the editor.
    rep = _A + _mk(9, "j") + _B + _X
    cur_lines = list(_X)
    cur_lines[4] = "xline4 CUR-EDIT"
    out = _detect_move_edit_shape(
        "\n".join(BASE), "\n".join(_A + cur_lines + _B), "\n".join(rep))
    assert out is not None
    assert out["candidates"][0]["mover"] == "replayed"


def test_small_blocks_ignored():
    lines = _mk(4, "b") + _mk(8, "z")  # block under min_block_lines
    cur = _mk(8, "z") + _mk(4, "b")
    rep_lines = _mk(4, "b")
    rep_lines[1] = "edited"
    out = _detect_move_edit_shape(
        "\n".join(lines), "\n".join(cur), "\n".join(rep_lines + _mk(8, "z")))
    assert out is None

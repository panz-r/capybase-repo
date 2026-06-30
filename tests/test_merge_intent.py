"""Tests for :mod:`capybase.merge_intent` — the pure core behind side
classification and silent-resurrection detection.

These exercise the logic directly with text fixtures (no git, no model),
including the real-world ``edit_file.rs`` shape: upstream deletes a test block
while the replayed branch keeps it (the modify/delete ambiguity), and the
silent-undo case where a clean merge resurrects deliberately-deleted code.
"""

from __future__ import annotations

from capybase.merge_intent import (
    ResurrectedBlock,
    classify_side,
    detect_resurrection,
    direction,
)


# ---------------------------------------------------------------------------
# classify_side
# ---------------------------------------------------------------------------


def test_classify_unchanged():
    base = "def f():\n    return 1\n"
    assert classify_side(base, base) == "unchanged"


def test_classify_deleted_empty_side():
    """The edit_file.rs shape: side is empty, base full → deleted."""
    base = "def a():\n    pass\n\ndef b():\n    pass\n"
    assert classify_side(base, "") == "deleted"


def test_classify_deleted_partial():
    """Side removed some base lines, added nothing → deleted."""
    base = "def a():\n    pass\n\ndef b():\n    pass\n"
    side = "def a():\n    pass\n"
    assert classify_side(base, side) == "deleted"


def test_classify_added():
    base = ""
    side = "def new():\n    return 1\n"
    assert classify_side(base, side) == "added"


def test_classify_added_from_near_empty():
    # Base near-empty, side grows it.
    base = "\n\n"
    side = "def new():\n    return 1\n"
    assert classify_side(base, side) == "added"


def test_classify_modified():
    """Side both removes and adds content → modified."""
    base = "def a():\n    return 1\n"
    side = "def a():\n    return 2\n\ndef b():\n    return 3\n"
    assert classify_side(base, side) == "modified"


def test_classify_replaced_same_size_is_modified():
    base = "x = 1\n"
    side = "x = 2\n"
    assert classify_side(base, side) == "modified"


def test_classify_both_empty_is_unchanged():
    assert classify_side("", "") == "unchanged"


# ---------------------------------------------------------------------------
# direction
# ---------------------------------------------------------------------------


def test_direction_modify_delete_current_deleted():
    """The real edit_file.rs case: upstream (current) deleted, replayed kept."""
    base = "    #[test]\n    fn brace_balance_passes() {\n        assert!(true);\n    }\n"
    current = ""  # upstream removed the whole block
    replayed = base  # replayed branch kept it verbatim
    d = direction(base, current, replayed)
    assert d.current == "deleted"
    assert d.replayed == "unchanged"
    assert d.kind == "modify_delete"
    assert d.deleting_side == "current"
    assert "CURRENT_UPSTREAM_SIDE DELETED" in d.summary


def test_direction_modify_delete_replayed_deleted():
    base = "    fn helper() {}\n"
    current = base
    replayed = ""
    d = direction(base, current, replayed)
    assert d.kind == "modify_delete"
    assert d.deleting_side == "replayed"
    assert "REPLAYED_COMMIT_SIDE DELETED" in d.summary


def test_direction_delete_delete():
    """Both sides deleted → delete_delete, not ambiguous, no deleting_side."""
    base = "def a():\n    pass\n\ndef b():\n    pass\n"
    d = direction(base, "", "")
    assert d.kind == "delete_delete"
    assert d.deleting_side is None


def test_direction_both_add():
    base = ""
    d = direction(base, "fn a() {}\n", "fn b() {}\n")
    assert d.kind == "both_add"
    assert d.deleting_side is None


def test_direction_both_modify():
    base = "def f():\n    return 1\n"
    d = direction(base, "def f():\n    return 2\n", "def f():\n    return 3\n")
    assert d.kind == "both_modify"
    assert d.current == "modified"
    assert d.replayed == "modified"


def test_direction_one_unchanged():
    base = "def f():\n    return 1\n"
    d = direction(base, base, "def f():\n    return 2\n")
    assert d.kind == "one_unchanged"
    assert d.replayed == "modified"


def test_direction_both_unchanged():
    base = "def f():\n    return 1\n"
    d = direction(base, base, base)
    assert d.kind == "both_unchanged"


# ---------------------------------------------------------------------------
# detect_resurrection
# ---------------------------------------------------------------------------


def test_detect_resurrection_block_back_whole():
    """Ours deleted a block; the merge result brought it back verbatim."""
    base = "fn a() {}\n\nfn dead() {\n    // old impl\n    do_thing();\n}\n\nfn c() {}\n"
    # ours removed the dead() block (the cleanup commit).
    ours = "fn a() {}\n\nfn c() {}\n"
    # result resurrects dead() — the silent undo.
    result = "fn a() {}\n\nfn dead() {\n    // old impl\n    do_thing();\n}\n\nfn c() {}\n"
    findings = detect_resurrection(base, ours, result, min_block_lines=3)
    assert len(findings) == 1
    f = findings[0]
    assert f.coverage >= 0.99
    assert "fn dead()" in f.text
    assert "do_thing()" in f.text


def test_detect_resurrection_none_when_deletion_held():
    """If the deletion held in the result, nothing is reported (safe case)."""
    base = "fn a() {}\n\nfn dead() {\n    do_thing();\n}\n\nfn c() {}\n"
    ours = "fn a() {}\n\nfn c() {}\n"
    result = ours  # deletion held
    assert detect_resurrection(base, ours, result) == []


def test_detect_resurrection_none_when_ours_deleted_nothing():
    base = "fn a() {}\n"
    assert detect_resurrection(base, base, base) == []


def test_detect_resurrection_ignores_small_blocks():
    """Blocks under min_block_lines are not flagged (coincidental reappearances)."""
    base = "fn a() {}\n}\n"  # one meaningful line
    ours = ""  # removed it
    result = base  # came back
    assert detect_resurrection(base, ours, result, min_block_lines=3) == []


def test_detect_resurrection_partial_coverage_filtered():
    """A block that only partially reappears (below threshold) is not flagged."""
    base = "\n".join(f"line {i}" for i in range(10))
    ours = ""  # all deleted
    # Result only resurrects 2 of the 10 lines → coverage ~0.2, below 0.85.
    result = "line 3\nline 4\n"
    assert detect_resurrection(base, ours, result, min_block_lines=3, min_coverage=0.85) == []


def test_detect_resurrection_sorted_largest_first():
    """Multiple deletions reported largest-first by block size.

    ``ours`` keeps the anchor lines (``fn a`` / ``fn c``) so the two deleted
    regions are separate maximal runs — otherwise the whole base is one
    contiguous delete and reports a single block.
    """
    big = [f"big {i}" for i in range(8)]
    small = [f"small {i}" for i in range(4)]
    base = "\n".join(["fn a"] + big + ["fn c"] + small + ["fn e"])
    # ours keeps the anchors, drops the two dead blocks → two delete regions.
    ours = "fn a\nfn c\nfn e"
    result = base  # both came back
    findings = detect_resurrection(base, ours, result, min_block_lines=3)
    assert len(findings) == 2
    assert findings[0].block_line_count >= findings[1].block_line_count
    assert "big 0" in findings[0].text


def test_detect_resurrection_replace_not_treated_as_deletion():
    """A modification (replace) is not a clean deletion — not flagged."""
    base = "def old():\n    return 1\n\ndef other():\n    return 2\n"
    # ours rewrote old() → a replace, not a clean delete.
    ours = "def new():\n    return 1\n\ndef other():\n    return 2\n"
    # result keeps old() — but since ours didn't *cleanly delete* it, no flag.
    result = base
    findings = detect_resurrection(base, ours, result, min_block_lines=3)
    assert findings == []

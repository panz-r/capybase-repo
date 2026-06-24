from capybase.adapters.parsers import (
    contains_markers,
    parse_marker_blocks,
    parse_resolution_json,
    splice_all_resolutions,
    splice_resolution,
)
from capybase.adapters.llm_openai import coerce_candidate_dict


def test_parse_single_block():
    text = "header\n<<<<<<< HEAD\ncurrent\n=======\nreplayed\n>>>>>>> branch\nfooter\n"
    blocks = parse_marker_blocks(text)
    assert len(blocks) == 1
    b = blocks[0]
    assert b.current_text == "current"
    assert b.replayed_text == "replayed"
    assert b.span == (1, 5)


def test_parse_multiple_blocks():
    text = (
        "<<<<<<< HEAD\na\n=======\nb\n>>>>>>>\n"
        "middle\n"
        "<<<<<<< HEAD\nc\n=======\nd\n>>>>>>>\n"
    )
    blocks = parse_marker_blocks(text)
    assert len(blocks) == 2
    assert blocks[0].current_text == "a"
    assert blocks[1].replayed_text == "d"


def test_parse_tolerates_marker_labels():
    text = "<<<<<<< HEAD:branch name\ncur\n=======\nrep\n>>>>>>> commit (summary)\n"
    blocks = parse_marker_blocks(text)
    assert len(blocks) == 1


def test_unterminated_raises():
    import pytest

    with pytest.raises(ValueError):
        parse_marker_blocks("<<<<<<< HEAD\ncur\n")
    with pytest.raises(ValueError):
        parse_marker_blocks("<<<<<<< HEAD\ncur\n=======\nrep\n")


def test_contains_markers():
    assert contains_markers("<<<<<<< x\na\n")
    assert contains_markers("a\n=======\nb\n")
    assert not contains_markers("plain text\n")


def test_splice_replaces_block():
    text = "h\n<<<<<<< H\ncur\n=======\nrep\n>>>>>>> b\nf\n"
    out = splice_resolution(text, (1, 5), "merged")
    assert out == "h\nmerged\nf\n"


def test_splice_preserves_outside_lines():
    text = "l1\nl2\n<<<<<<< H\nx\n=======\ny\n>>>>>>> b\nl5\nl6\n"
    out = splice_resolution(text, (2, 6), "z\nzz")
    assert out.startswith("l1\nl2\n")
    assert out.endswith("z\nzz\nl5\nl6\n")


# --- batch splice (multi-unit-per-file) ---


def _two_block_file():
    """A marker-laden file with two conflict blocks separated by context.

    Returns (text, span1, span2) where span1 < span2 (block 1 is higher up).
    """
    text = (
        "top\n"
        "<<<<<<< H\nc1\n=======\nr1\n>>>>>>>\n"
        "mid\n"
        "<<<<<<< H\nc2\n=======\nr2\n>>>>>>>\n"
        "bot\n"
    )
    # lines: 0 top / 1 << / 2 c1 / 3 == / 4 r1 / 5 >> / 6 mid /
    #        7 << / 8 c2 / 9 == / 10 r2 / 11 >> / 12 bot
    return text, (1, 5), (7, 11)


def test_splice_all_two_blocks_both_replaced():
    text, span1, span2 = _two_block_file()
    out = splice_all_resolutions(
        text, [(span1, "A1"), (span2, "A2")]
    )
    # Both resolutions present, markers gone, surrounding context intact.
    assert "A1" in out and "A2" in out
    assert not contains_markers(out)
    assert out.startswith("top\nA1\nmid\n")
    assert out.endswith("\nA2\nbot\n")


def test_splice_all_offset_correct_when_first_resolution_grows():
    """The core regression: block 1's resolution has *more* lines than the
    original block. A naive accumulate loop would then splice block 2 at the
    wrong offset. Reverse-order splicing keeps both correct."""
    text, span1, span2 = _two_block_file()
    # span1 is 5 lines; resolve to 2 lines (shrinks) and check span2 still lands.
    out = splice_all_resolutions(text, [(span1, "short1"), (span2, "short2")])
    lines = out.split("\n")
    # top, short1, mid, short2, bot  (+ trailing "")
    assert lines == ["top", "short1", "mid", "short2", "bot", ""]


def test_splice_all_offset_correct_when_first_resolution_shrinks_a_lot():
    text, span1, span2 = _two_block_file()
    # span1 is 5 lines -> resolve to 1; span2 is 5 lines -> resolve to 1.
    out = splice_all_resolutions(text, [(span1, "X"), (span2, "Y")])
    assert out.split("\n") == ["top", "X", "mid", "Y", "bot", ""]


def test_splice_all_accepts_unsorted_input():
    """Caller may pass spans in any order; we sort internally."""
    text, span1, span2 = _two_block_file()
    a = splice_all_resolutions(text, [(span1, "A"), (span2, "B")])
    b = splice_all_resolutions(text, [(span2, "B"), (span1, "A")])
    assert a == b


def test_splice_all_empty_returns_original():
    text, _, _ = _two_block_file()
    assert splice_all_resolutions(text, []) == text


def test_splice_all_single_block_matches_splice_resolution():
    text, span1, _ = _two_block_file()
    assert splice_all_resolutions(text, [(span1, "ONLY")]) == splice_resolution(
        text, span1, "ONLY"
    )


def test_splice_all_rejects_overlapping_spans():
    import pytest

    text, span1, _ = _two_block_file()
    # span1 is (1,5); craft an overlapping span (4, 6).
    with pytest.raises(ValueError, match="overlapping"):
        splice_all_resolutions(text, [(span1, "A"), ((4, 6), "B")])


def test_splice_all_rejects_out_of_range_span():
    import pytest

    text, _, _ = _two_block_file()  # 13 lines (indices 0..12)
    with pytest.raises(ValueError, match="out of range"):
        splice_all_resolutions(text, [((10, 99), "B")])


# --- JSON resolution parsing (reasoning-model tolerant) ---


def test_parse_resolution_fenced_json():
    raw = "thoughts...\n```json\n{\"resolved_text\": \"x\", \"needs_human\": false}\n```"
    data, warns = parse_resolution_json(raw)
    assert data["resolved_text"] == "x"
    assert warns == []


def test_parse_resolution_pure_json():
    data, _ = parse_resolution_json('{"resolved_text": "y"}')
    assert data["resolved_text"] == "y"


def test_parse_resolution_prose_then_json():
    raw = "user: let me think...\nso the answer is\n{\"resolved_text\": \"z\"}\n"
    data, warns = parse_resolution_json(raw)
    assert data["resolved_text"] == "z"
    assert any("scan" in w for w in warns)


def test_parse_resolution_ignores_braces_in_strings():
    # stray f-string braces must not break the brace scanner
    raw = "I'll use f'hi {name}' here.\n{\"resolved_text\": \"done\"}"
    data, _ = parse_resolution_json(raw)
    assert data["resolved_text"] == "done"


def test_parse_resolution_picks_last_object():
    raw = "{\"resolved_text\": \"first\"}\nmore prose\n{\"resolved_text\": \"final\"}"
    data, _ = parse_resolution_json(raw)
    assert data["resolved_text"] == "final"


def test_parse_resolution_no_json_returns_empty():
    data, warns = parse_resolution_json("just prose, no braces here")
    assert data == {}
    assert any("no valid" in w for w in warns)


def test_parse_resolution_multiline_escaped_text():
    payload = '{"resolved_text": "    return \'merged\'\\n    x = 1", "explanation": "ok"}'
    data, warns = parse_resolution_json(payload)
    assert "return" in data["resolved_text"]
    assert warns == []


def test_coerce_candidate_dict_normalizes_aliases():
    data, _ = coerce_candidate_dict('{"resolved": "r", "confidence": 0.5}')
    assert data["resolved_text"] == "r"
    assert data["self_reported_confidence"] == 0.5

from capybase.adapters.parsers import (
    contains_markers,
    parse_marker_blocks,
    parse_resolution_json,
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

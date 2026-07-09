"""Tests for the markdown-code output-layout parser.

Under the ``markdown_code`` output layout the model emits the merged code as a
RAW fenced code block (no JSON escaping of newlines/quotes) followed by a small
JSON metadata object. These tests pin the parser's extraction of that shape:

1. The code block becomes ``resolved_text`` verbatim — embedded quotes and
   newlines survive unescaped (the whole point of the layout).
2. The metadata JSON is merged on top (needs_human, explanation, intents).
3. Graceful fallback: a model that ignored the layout (emitted only JSON) still
   parses via the legacy path; a response with no usable structure degrades to
   an empty dict + warnings rather than crashing.
"""

from __future__ import annotations

from capybase.adapters.parsers import (
    _extract_markdown_code_block,
    parse_resolution_json,
)


def test_extract_code_block_returns_last_non_json_fence():
    raw = (
        "Here is the merge:\n"
        "```python\n"
        "x = 1\n"
        "```\n"
        "```json\n"
        '{"needs_human": false}\n'
        "```\n"
    )
    code = _extract_markdown_code_block(raw)
    assert code == "x = 1"


def test_extract_code_block_bare_fence_counts_as_code():
    """A fence with no language hint is the code (not the metadata)."""
    raw = "```\ndef f():\n    return 0\n```\n"
    assert _extract_markdown_code_block(raw) == "def f():\n    return 0"


def test_extract_code_block_none_when_only_json_fence():
    raw = '```json\n{"resolved_text": "x"}\n```\n'
    assert _extract_markdown_code_block(raw) is None


def test_markdown_layout_preserves_embedded_quotes_and_newlines():
    """The JSON-escaping failure mode: code with embedded \" and newlines."""
    raw = (
        "```python\n"
        "def f():\n"
        '    return {"key": "value", "n": 9}\n'
        "```\n"
        "```json\n"
        '{"explanation": "combined both sides"}\n'
        "```\n"
    )
    data, warns = parse_resolution_json(raw, layout="markdown_code")
    assert (
        data["resolved_text"]
        == 'def f():\n    return {"key": "value", "n": 9}'
    )
    assert data["explanation"] == "combined both sides"
    assert warns == []


def test_markdown_layout_captures_metadata_fields():
    raw = (
        "```python\n"
        "    return 0\n"
        "```\n"
        "```json\n"
        "{\n"
        '  "needs_human": false,\n'
        '  "current_side_intent": ["return 0"],\n'
        '  "replayed_commit_intent": ["return 9"],\n'
        '  "explanation": "kept current"\n'
        "}\n"
        "```\n"
    )
    data, _ = parse_resolution_json(raw, layout="markdown_code")
    assert data["resolved_text"] == "    return 0"
    assert data["needs_human"] is False
    assert data["current_side_intent"] == ["return 0"]
    assert data["explanation"] == "kept current"


def test_markdown_layout_code_block_without_metadata_warns():
    """A code block but no JSON metadata → resolved_text set, warning emitted."""
    raw = "```python\nx = 1\n```\n(no json after)"
    data, warns = parse_resolution_json(raw, layout="markdown_code")
    assert data["resolved_text"] == "x = 1"
    assert any("no metadata" in w for w in warns)


def test_markdown_layout_falls_back_to_json_when_no_code_block():
    """A model that ignored the layout and emitted only JSON still parses."""
    raw = '```json\n{"resolved_text": "y = 2", "explanation": "e"}\n```\n'
    data, warns = parse_resolution_json(raw, layout="markdown_code")
    assert data["resolved_text"] == "y = 2"
    assert any("fall" in w for w in warns)  # the fallback warning


def test_markdown_layout_handles_indented_code():
    """Leading indentation (the PRESERVE rule) is preserved exactly."""
    raw = (
        "```python\n"
        "    if True:\n"
        "        print('hi')\n"
        "```\n"
        "```json\n{}\n```\n"
    )
    data, _ = parse_resolution_json(raw, layout="markdown_code")
    assert data["resolved_text"] == "    if True:\n        print('hi')"


def test_markdown_layout_empty_code_block():
    raw = "```\n```\n```json\n{}\n```\n"
    data, warns = parse_resolution_json(raw, layout="markdown_code")
    assert data["resolved_text"] == ""


def test_layout_none_is_legacy_json_path():
    """layout=None (default) never extracts a code block."""
    raw = (
        "```python\nx = 1\n```\n"
        '```json\n{"resolved_text": "from json"}\n```\n'
    )
    data, _ = parse_resolution_json(raw, layout=None)
    # Legacy path: resolved_text comes from the JSON object, not the code block.
    assert data["resolved_text"] == "from json"


def test_markdown_layout_unparseable_response_degrades_gracefully():
    """No code block AND no JSON → empty dict, no crash."""
    data, warns = parse_resolution_json("just prose, nothing useful", layout="markdown_code")
    assert data == {}
    assert warns


def test_markdown_layout_tolerant_metadata_repair():
    """The metadata JSON is salvaged via the lenient repair pass (trailing comma)."""
    raw = (
        "```python\nx = 1\n```\n"
        "```json\n"
        '{"needs_human": false, "explanation": "ok",}\n'  # trailing comma
        "```\n"
    )
    data, warns = parse_resolution_json(raw, layout="markdown_code")
    assert data["resolved_text"] == "x = 1"
    assert data["explanation"] == "ok"

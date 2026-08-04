"""Shared brace-matching utilities for deterministic merge primitives.

These text-surgical helpers find the matching closing delimiter for an opening
``{``, ``(``, or ``[`` by tracking bracket depth. They are string-level scans
(no AST), used by the keyed-item and named-field primitives to locate insertion
points (before a container's closing brace).

The scans are comment/string-aware: brackets inside comments and string
literals are not counted, so a ``// comment with a }`` or a ``"hello {"`` does
not confuse the depth tracker.
"""

from __future__ import annotations


def _mask_strings_and_comments(text: str, language: str | None = None) -> str:
    """Replace string/char-literal and comment content with spaces (preserve length).

    This lets a brace-depth scan ignore brackets that appear inside strings or
    comments. Language-aware: Rust uses ``//`` and ``/* */``; Python uses ``#``
    and triple-quotes. Falls back to C-family comment syntax.

    Thin delegate to the canonical ``adapters.string_lexer.blank_strings_and_comments``.
    The prior hand-rolled char scanner was abandoned upstream (it leaked raw-
    string content, corrupting the brace count when a raw string contained
    ``{``/``}``); ``string_lexer`` handles every string form correctly.
    ``string_char=" "`` preserves this function's historical space-replacement
    output so callers' behavior is byte-identical.
    """
    from capybase.adapters.string_lexer import blank_strings_and_comments
    return blank_strings_and_comments(text, language, string_char=" ")


def find_closing_brace(
    text: str, open_idx: int, *, language: str | None = None,
) -> int | None:
    """Find the index of the ``}`` matching the ``{`` at ``open_idx``.

    Returns the character index of the matching closing brace, or None when:
    - ``text[open_idx]`` is not ``{``
    - the braces are unbalanced (no matching close found)

    Tracks brace depth, skipping brackets inside strings and comments (via
    :func:`_mask_strings_and_comments`). Only ``{``/``}`` are tracked (not
    parens/brackets) — the caller is responsible for passing the index of a
    genuine ``{``.
    """
    if open_idx < 0 or open_idx >= len(text) or text[open_idx] != "{":
        return None
    masked = _mask_strings_and_comments(text, language)
    depth = 0
    for i in range(open_idx, len(masked)):
        ch = masked[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def find_container_close_line(
    lines: list[str], header_line_idx: int, *, language: str | None = None,
) -> int | None:
    """Find the LINE index of the closing ``}`` for a brace block opened on or
    after ``header_line_idx``.

    Scans from ``header_line_idx`` for the first ``{``, then tracks brace depth
    across subsequent lines to find the matching ``}``. Returns the line index,
    or None when the block is unbalanced.
    """
    full_text = "\n".join(lines)
    # Find the first { at or after the header line's start.
    char_offset = sum(len(lines[j]) + 1 for j in range(header_line_idx))
    # Search for { starting from the header line.
    open_idx = full_text.find("{", char_offset)
    if open_idx == -1:
        return None
    close_idx = find_closing_brace(full_text, open_idx, language=language)
    if close_idx is None:
        return None
    # Convert char index back to line index.
    line_idx = full_text[:close_idx].count("\n")
    return line_idx


__all__ = [
    "find_closing_brace",
    "find_container_close_line",
]

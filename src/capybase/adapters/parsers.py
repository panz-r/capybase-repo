"""Pure text parsers: conflict-marker blocks and LLM JSON responses.

No git, no IO — these are pure functions so they are trivially testable and
reusable by both the extractor and the resolution engine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass
class MarkerBlock:
    """One ``<<<<<<< ... >>>>>>>`` conflict marker block found in a file.

    Line numbers are 0-based and inclusive. ``start`` is the line holding
    ``<<<<<<<``; ``current_start..current_end`` spans the CURRENT side
    (between ``<<<<<<<`` and ``=======``); ``replayed_start..replayed_end``
    spans the REPLAYED side (between ``=======`` and ``>>>>>>>``).
    """

    start: int  # line index of <<<<<<<
    divider: int  # line index of =======
    end: int  # line index of >>>>>>>

    current_text: str  # text between <<<<<<< and =======
    replayed_text: str  # text between ======= and >>>>>>>

    @property
    def span(self) -> tuple[int, int]:
        """Inclusive [start, end] line span of the whole marker block."""
        return (self.start, self.end)


# The three canonical markers. We match prefixes so trailing labels (branch
# names, commit summaries) on the ``<<<<<<<``/``>>>>>>>`` lines are tolerated.
_MARK_CURRENT = "<<<<<<<"
_MARK_DIVIDER = "======="
_MARK_REPLAYED = ">>>>>>>"


def parse_marker_blocks(text: str) -> list[MarkerBlock]:
    """Parse all conflict-marker blocks in ``text``.

    Lines outside any block are ignored. Malformed nesting is reported by
    raising ``ValueError`` with the offending line number so callers can
    escalate rather than silently splice.
    """
    lines = text.split("\n")
    blocks: list[MarkerBlock] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith(_MARK_CURRENT):
            start = i
            current_lines: list[str] = []
            j = i + 1
            while j < n and not lines[j].startswith(_MARK_DIVIDER):
                current_lines.append(lines[j])
                j += 1
            if j >= n:
                raise ValueError(
                    f"unterminated conflict block: '<<<<<<<' at line {start} "
                    f"with no matching '======='"
                )
            divider = j
            replayed_lines: list[str] = []
            k = j + 1
            while k < n and not lines[k].startswith(_MARK_REPLAYED):
                replayed_lines.append(lines[k])
                k += 1
            if k >= n:
                raise ValueError(
                    f"unterminated conflict block: '<<<<<<<' at line {start} "
                    f"with no matching '>>>>>>>'"
                )
            end = k
            blocks.append(
                MarkerBlock(
                    start=start,
                    divider=divider,
                    end=end,
                    current_text="\n".join(current_lines),
                    replayed_text="\n".join(replayed_lines),
                )
            )
            i = end + 1
        else:
            i += 1
    return blocks


def contains_markers(text: str) -> bool:
    """True if ``text`` contains any conflict marker prefix."""
    for line in text.split("\n"):
        if (
            line.startswith(_MARK_CURRENT)
            or line.startswith(_MARK_DIVIDER)
            or line.startswith(_MARK_REPLAYED)
        ):
            return True
    return False


def splice_resolution(
    worktree_text: str, marker_span: tuple[int, int], resolved_text: str
) -> str:
    """Replace the inclusive marker-block line span with ``resolved_text``.

    The block occupies whole lines ``[start, end]`` inclusive. We reconstruct
    by splicing on the newline-separated line list so offsets stay exact.
    """
    lines = worktree_text.split("\n")
    start, end = marker_span
    if start < 0 or end >= len(lines) or start > end:
        raise ValueError(
            f"marker_span {marker_span} out of range for {len(lines)} lines"
        )
    # Preserve whether the original block included a trailing newline by
    # joining resolved text lines into the same list shape.
    resolved_lines = resolved_text.split("\n") if resolved_text != "" else []
    new_lines = lines[:start] + resolved_lines + lines[end + 1 :]
    return "\n".join(new_lines)


# ---------------------------------------------------------------------------
# LLM response parsing
# ---------------------------------------------------------------------------


def parse_resolution_json(raw: str) -> tuple[dict, list[str]]:
    """Parse a model response into a resolution dict + parse warnings.

    Tolerates the common failure modes of small/reasoning models:

    1. A fenced ```json ... ``` block (preferred).
    2. Raw JSON at top level.
    3. Chain-of-thought prose *followed by* JSON — we scan for a balanced
       top-level ``{ ... }`` object, preferring the last one (the final
       answer), and we ignore braces that appear inside string literals so
       constructs like ``f'hi {name}'`` don't fool the scanner.
    """
    warnings: list[str] = []

    # 1. Prefer a fenced ```json (or ```) block.
    fenced = _extract_fenced(raw)
    if fenced is not None:
        data, ok = _try_json(fenced)
        if ok:
            return _as_dict(data, warnings)
        warnings.append("fenced block was not valid JSON; falling back to scan")

    # 2. Direct parse of the whole response.
    data, ok = _try_json(raw.strip())
    if ok:
        return _as_dict(data, warnings)

    # 3. Scan for balanced top-level objects; keep the last parseable one.
    warnings.append("strict JSON parse failed; scanning for embedded object")
    candidates = _find_balanced_objects(raw)
    for cand in reversed(candidates):
        data, ok = _try_json(cand)
        if ok:
            return _as_dict(data, warnings)
    warnings.append("no valid JSON object found in response")
    return {}, warnings


def _try_json(text: str) -> tuple[object, bool]:
    try:
        return json.loads(text), True
    except json.JSONDecodeError:
        return None, False


def _as_dict(data: object, warnings: list[str]) -> tuple[dict, list[str]]:
    if not isinstance(data, dict):
        warnings.append("parsed JSON is not an object")
        return {}, warnings
    return data, warnings


def _extract_fenced(raw: str) -> str | None:
    """Return the contents of the last ```json or ``` fenced block, or None."""
    blocks: list[str] = []
    lines = raw.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            # opening fence (```json or ```)
            buf: list[str] = []
            j = i + 1
            closed = False
            while j < n:
                if lines[j].strip().startswith("```"):
                    closed = True
                    break
                buf.append(lines[j])
                j += 1
            if closed:
                blocks.append("\n".join(buf))
            i = j + 1
        else:
            i += 1
    return blocks[-1] if blocks else None


def _find_balanced_objects(text: str) -> list[str]:
    """Return every top-level balanced ``{...}`` substring.

    Braces inside JSON string literals (double-quoted) are ignored so that
    values like ``"f'hi {name}'"`` do not break scanning. Single quotes are
    *not* treated as string delimiters — they appear in prose apostrophes
    (``I'll``) and Python f-strings inside values, and honoring them would
    swallow real JSON.
    """
    objects: list[str] = []
    depth = 0
    start = -1
    in_str = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2  # skip escaped char
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            i += 1
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    objects.append(text[start : i + 1])
                    start = -1
        i += 1
    return objects

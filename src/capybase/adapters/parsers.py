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

    ``base_text`` holds the merged-base content from a diff3/zdiff3 style
    conflict (the ``|||||||`` section). Empty for the default (merge) style.
    """

    start: int  # line index of <<<<<<<
    divider: int  # line index of =======
    end: int  # line index of >>>>>>>

    current_text: str  # text between <<<<<<< and (||||||| or =======)
    replayed_text: str  # text between ======= and >>>>>>>
    base_text: str = ""  # text between ||||||| and ======= (diff3/zdiff3 only)

    @property
    def span(self) -> tuple[int, int]:
        """Inclusive [start, end] line span of the whole marker block."""
        return (self.start, self.end)


# The conflict markers. We match prefixes so trailing labels (branch names,
# commit summaries) on the ``<<<<<<<``/``>>>>>>>`` lines are tolerated.
_MARK_CURRENT = "<<<<<<<"
_MARK_BASE = "|||||||"  # diff3/zdiff3 base section (optional)
_MARK_DIVIDER = "======="
_MARK_REPLAYED = ">>>>>>>"


def _is_marker(line: str) -> str | None:
    """Return which marker a line is (column-0 prefix), or None.

    Strips a trailing ``\\r`` first so CRLF files match cleanly (a marker line
    ``<<<<<<< HEAD\\r`` would otherwise miss ``startswith`` and leak ``\\r`` into
    parsed text). The returned marker is the canonical prefix, not the raw line.
    """
    stripped = line[:-1] if line.endswith("\r") else line
    if stripped.startswith(_MARK_CURRENT):
        return _MARK_CURRENT
    if stripped.startswith(_MARK_BASE):
        return _MARK_BASE
    if stripped.startswith(_MARK_DIVIDER):
        return _MARK_DIVIDER
    if stripped.startswith(_MARK_REPLAYED):
        return _MARK_REPLAYED
    return None


def parse_marker_blocks(text: str) -> list[MarkerBlock]:
    """Parse all conflict-marker blocks in ``text``.

    Handles both the default (merge) style::

        <<<<<<< label
        current
        =======
        replayed
        >>>>>>> label

    and the diff3/zdiff3 style, which adds an optional merged-base section::

        <<<<<<< label
        current
        ||||||| label
        base
        =======
        replayed
        >>>>>>> label

    Without diff3-aware parsing, the ``||||||| base`` section would be silently
    appended to ``current_text`` — corrupting the model's input. CRLF line
    endings are normalized (trailing ``\\r`` stripped) so parsed text is clean.

    Lines outside any block are ignored. Malformed nesting is reported by
    raising ``ValueError`` with the offending line number so callers can
    escalate rather than silently splice.
    """
    lines = text.split("\n")
    blocks: list[MarkerBlock] = []
    i = 0
    n = len(lines)
    while i < n:
        if _is_marker(lines[i]) != _MARK_CURRENT:
            i += 1
            continue
        start = i
        # Collect the CURRENT side until we hit ||||||| (diff3 base) or =======.
        current_lines: list[str] = []
        j = i + 1
        base_marker_line: int | None = None
        while j < n:
            m = _is_marker(lines[j])
            if m == _MARK_BASE:
                base_marker_line = j
                break
            if m == _MARK_DIVIDER:
                break
            current_lines.append(lines[j])
            j += 1
        # If a diff3 base section was found, collect base lines until =======.
        base_lines: list[str] = []
        if base_marker_line is not None:
            j = base_marker_line + 1
            while j < n and _is_marker(lines[j]) != _MARK_DIVIDER:
                base_lines.append(lines[j])
                j += 1
        if j >= n or _is_marker(lines[j]) != _MARK_DIVIDER:
            raise ValueError(
                f"unterminated conflict block: '<<<<<<<' at line {start} "
                f"with no matching '======='"
            )
        divider = j
        replayed_lines: list[str] = []
        k = j + 1
        while k < n and _is_marker(lines[k]) != _MARK_REPLAYED:
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
                current_text=_normalize(current_lines),
                replayed_text=_normalize(replayed_lines),
                base_text=_normalize(base_lines),
            )
        )
        i = end + 1
    return blocks


def _normalize(lines: list[str]) -> str:
    """Join lines, stripping trailing ``\\r`` (CRLF normalization).

    Git marker lines on Windows-style files carry ``\\r``; without stripping,
    the parsed text would carry ``\\r`` at every line end, polluting the model
    input and breaking splice comparisons.
    """
    return "\n".join(ln[:-1] if ln.endswith("\r") else ln for ln in lines)


def contains_markers(text: str) -> bool:
    """True if ``text`` contains any conflict marker prefix (column-0).

    Uses line-start matching so ``// =====`` comment banners, indented rules,
    and marker-shaped strings inside content are NOT flagged. Handles CRLF.
    """
    for line in text.split("\n"):
        if _is_marker(line) is not None:
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


def splice_all_resolutions(
    original: str,
    spans_and_texts: list[tuple[tuple[int, int], str]],
) -> str:
    """Apply a batch of resolutions to one original file, offset-correctly.

    Each entry is ``(marker_span, resolved_text)`` where ``marker_span`` is an
    inclusive 0-based ``[start, end]`` line range into ``original`` (the full
    marker-laden worktree text). All spans are interpreted against the same
    ``original``; resolutions are applied in *reverse line order* (highest
    ``start`` first) so that replacing a span never shifts the line numbers of
    any not-yet-applied span. This is what makes multi-hunk splicing correct:
    a naive accumulate-into-buffer loop would invalidate later spans' offsets
    as soon as an earlier resolution changes the line count.

    Spans must be non-overlapping and in range; otherwise ``ValueError`` is
    raised so the caller escalates rather than silently writing a bad file.
    An empty list returns ``original`` unchanged.
    """
    if not spans_and_texts:
        return original
    lines = original.split("\n")
    n = len(lines)
    # Validate ranges and non-overlap up front (fail loudly, not mid-splice).
    spans: list[tuple[int, int, str]] = []
    for (start, end), text in spans_and_texts:
        if start < 0 or end >= n or start > end:
            raise ValueError(
                f"marker_span ({start}, {end}) out of range for {n} lines"
            )
        spans.append((start, end, text))
    # Sort by start descending; detect overlap against the next-lower span.
    spans.sort(key=lambda t: t[0], reverse=True)
    prev_start: int | None = None
    for start, end, _ in spans:
        if prev_start is not None and end >= prev_start:
            raise ValueError(
                f"overlapping spans: ({start},{end}) overlaps a span starting at {prev_start}"
            )
        prev_start = start
    # Apply bottom-to-top against the same accumulating buffer. Because we go
    # from highest span down, each splice only touches lines at or below the
    # spans we haven't processed yet, so their absolute indices stay valid.
    buffer = original
    for start, end, text in spans:
        buffer = splice_resolution(buffer, (start, end), text)
    return buffer


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

"""S28-139: ordered splice selection — block-capture generalized from a
binary decision to an ordered splice.

For conflicts whose sides are ADDITIVE (both sides contribute blocks; the open
question is their ORDER, not their content), the model returns a tiny
selection/order answer over the existing blocks and capybase materializes the
result verbatim from the conflict sides. Copied code is byte-identical by
construction; the answer costs tens of tokens instead of a full regeneration.

The gate (fitness in the SBCR ambiguity band) lives in the orchestrator; this
module is the pure protocol: decomposition, prompt, strict parse, and
materialization. Any protocol violation latches the unit to normal generation
(the S28-136 factoring latch pattern).
"""

from __future__ import annotations

import json
import re
from typing import Any

PROMPT_SPLICE_SELECTION = "PROMPT_SPLICE#v1"

_CURRENT_TAG = "A"
_REPLAYED_TAG = "R"

# Strict answer shape: {"order": ["A1", "R1", ...], "glue": [{"after": "A1",
# "lines": ["..."]}]} — "after" may also be "start" (before everything) or
# "end" (after everything).
_ORDER_KEY = "order"
_GLUE_KEY = "glue"
_MAX_GLUE_INSERTIONS = 4
_MAX_GLUE_LINES = 3
_MAX_GLUE_LINE_CHARS = 120


def decompose_chunks(
    current_text: str, replayed_text: str, *, max_chunks: int = 16,
) -> list[dict[str, Any]] | None:
    """Split each conflict side into blocks: maximal runs of consecutive
    non-blank lines, numbered A1..An (current) and R1..Rm (replayed) in
    document order. Blank separators between same-side blocks are recorded so
    the materializer can restore them. Returns None when the shape doesn't
    fit the protocol (no blocks, or more than ``max_chunks`` total — too
    fragmented to order reliably).
    """
    chunks: list[dict[str, Any]] = []

    def _side(tag: str, text: str) -> int:
        lines = (text or "").split("\n")
        # Strip leading/trailing blanks of the conflict region.
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        n = 0
        run: list[str] = []
        blanks_before = 0
        for line in lines + ["\n"]:  # sentinel flushes the last run
            if line.strip():
                run.append(line)
            else:
                if run:
                    n += 1
                    chunks.append({
                        "id": f"{tag}{n}",
                        "side": "current" if tag == _CURRENT_TAG else "replayed",
                        "index": n,
                        "lines": list(run),
                        "blanks_before": blanks_before,
                    })
                    run = []
                    blanks_before = 1
                else:
                    blanks_before += 1
        return n

    na = _side(_CURRENT_TAG, current_text)
    nr = _side(_REPLAYED_TAG, replayed_text)
    if na == 0 or nr == 0:
        return None  # not an additive shape
    if na + nr > max_chunks:
        return None  # too fragmented
    return chunks


def render_chunk_list(chunks: list[dict[str, Any]]) -> str:
    """Compact identities for the prompt: id, line count, first line verbatim."""
    lines: list[str] = []
    for c in chunks:
        first = c["lines"][0].strip()
        if len(first) > 90:
            first = first[:87] + "..."
        extra = f" (+{len(c['lines']) - 1} more lines)" if len(c["lines"]) > 1 else ""
        lines.append(f'  {c["id"]}: {first}{extra}')
    return "\n".join(lines)


def build_splice_prompt(
    current_label: str,
    replayed_label: str,
    chunks: list[dict[str, Any]],
) -> str:
    """The ordered-splice prompt: block identities + the strict answer
    contract. The model never regenerates block content — it orders it."""
    ids = ", ".join(c["id"] for c in chunks)
    return (
        "This merge conflict is ADDITIVE: both sides contribute code blocks "
        "and the open question is their correct ORDER relative to each other.\n\n"
        f"{current_label} blocks:\n{render_chunk_list([c for c in chunks if c['side'] == 'current'])}\n\n"
        f"{replayed_label} blocks:\n{render_chunk_list([c for c in chunks if c['side'] == 'replayed'])}\n\n"
        "Decide the order the blocks should appear in the merged result. "
        "Reason in 1-3 sentences, then answer with ONLY a JSON object:\n"
        '{"order": ["A1", "R1", "A2", "R2"], "glue": [{"after": "A1", "lines": ["short line"]}]}'
        "\n\nRules:\n"
        f"- \"order\" must contain EVERY block id exactly once (any of: {ids}).\n"
        f"- \"glue\" is optional; at most {_MAX_GLUE_INSERTIONS} insertions, "
        f"each at most {_MAX_GLUE_LINES} short literal lines, each keyed by "
        '"after": <block id> or "start" or "end". Omit "glue" entirely if no '
        "new code is needed.\n"
        "- Never write block code in the answer — the blocks are inserted "
        "verbatim by the tool."
    )


def parse_splice_answer(
    text: str, chunks: list[dict[str, Any]], *,
    max_glue_insertions: int = _MAX_GLUE_INSERTIONS,
    max_glue_lines: int = _MAX_GLUE_LINES,
) -> tuple[list[str], list[dict[str, Any]]] | None:
    """Strict parse of the model's answer.

    Returns (order_ids, glue_insertions) or None on ANY violation: unparseable
    JSON, unknown id, missing id, duplicated id, oversized glue. The caller
    latches the unit to normal generation on None (factoring pattern).
    """
    if not text or not text.strip():
        return None
    # The answer follows a short reasoning preamble; take the last {...} block.
    m = None
    for m in re.finditer(r"\{.*\}", text or "", re.DOTALL):
        pass
    if m is None:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    order = obj.get(_ORDER_KEY)
    if not isinstance(order, list) or not order:
        return None
    known = {c["id"] for c in chunks}
    seen: set[str] = set()
    for cid in order:
        if not isinstance(cid, str) or cid not in known or cid in seen:
            return None
        seen.add(cid)
    if seen != known:
        return None  # every block must appear exactly once (pure ordering)
    raw_glue = obj.get(_GLUE_KEY, [])
    if raw_glue is None:
        raw_glue = []
    if not isinstance(raw_glue, list) or len(raw_glue) > max_glue_insertions:
        return None
    glue: list[dict[str, Any]] = []
    for g in raw_glue:
        if not isinstance(g, dict):
            return None
        anchor = g.get("after")
        lines = g.get("lines")
        if anchor not in known and anchor not in ("start", "end"):
            return None
        if (not isinstance(lines, list) or not lines
                or len(lines) > max_glue_lines
                or any(not isinstance(l, str) or len(l) > _MAX_GLUE_LINE_CHARS
                       for l in lines)):
            return None
        glue.append({"after": anchor, "lines": [l for l in lines]})
    return [str(c) for c in order], glue


def _blocks_by_id(chunks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in chunks}


def materialize(
    chunks: list[dict[str, Any]],
    order: list[str],
    glue: list[dict[str, Any]],
) -> str:
    """Materialize the merged text: blocks joined verbatim in the chosen
    order. Two consecutive blocks from the SAME side that were adjacent in
    that side restore the blank separator the decomposition removed; cross-
    side joins butt directly. Glue insertions land at their anchors.
    """
    by_id = {c["id"]: c for c in chunks}
    out: list[str] = []
    emitted: list[dict[str, Any]] = []

    def emit(cid: str) -> None:
        c = by_id[cid]
        if emitted:
            prev = emitted[-1]
            if prev["side"] == c["side"] and c["index"] == prev["index"] + 1:
                # The join contributes one newline on each side of this
                # piece, so an empty piece already yields one blank line;
                # extra blanks go here verbatim.
                out.append("\n" * (c["blanks_before"] - 1))
        out.append("\n".join(c["lines"]))
        emitted.append(c)

    for g in glue:
        if g["after"] == "start":
            out.append("\n".join(g["lines"]))
    for cid in order:
        for g in glue:
            if g["after"] == cid:
                out.append("\n".join(g["lines"]))
        emit(cid)
    for g in glue:
        if g["after"] == "end":
            out.append("\n".join(g["lines"]))
    return "\n".join(out)

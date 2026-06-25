"""Context building for the resolver.

MVP: the conflict block, ±N surrounding lines, the file path, the inferred
language, and a best-effort enclosing symbol. The ``ContextBundle`` shape is
richer than this (related snippets, retrieved examples, structural view) so
program slicing, RAG, and AST views can be added later without changing the
resolver signature.
"""

from __future__ import annotations

from capybase.conflict_model import ContextBundle, ConflictUnit, TokenBudget


class ContextBuilder:
    def __init__(self, context_lines: int = 15) -> None:
        self.context_lines = context_lines

    def build(self, unit: ConflictUnit, budget: TokenBudget | None = None) -> ContextBundle:
        budget = budget or TokenBudget()
        text = unit.original_worktree_text
        lines = text.split("\n")
        # Sibling marker blocks in this file (if any). Their spans are absolute
        # line ranges in ``original_worktree_text``. We use them to *confine*
        # the context window so it doesn't bleed across a sibling conflict
        # block: showing the model another block's raw ``<<<<<<< ... >>>>>>>``
        # markers as ordinary context is misleading and can cause it to merge
        # across block boundaries. The window is clamped to stop at the nearest
        # sibling boundary on each side.
        siblings = _sibling_spans(unit)
        if unit.marker_span is not None:
            start, end = unit.marker_span
            lo = max(0, start - self.context_lines)
            hi = min(len(lines) - 1, end + self.context_lines)
            lo = _clamp_low(lo, start, siblings)
            hi = _clamp_high(hi, end, siblings, len(lines) - 1)
            primary_lines = lines[lo : hi + 1]
        else:
            primary_lines = lines
        primary = "\n".join(primary_lines)
        # Rough token estimate (~4 chars/token). Good enough for budgeting;
        # a real tokenizer can be swapped in later without interface change.
        est = max(1, len(primary) // 4)
        side_summaries = {
            "base": _head(unit.base.text),
            "current": _head(unit.current.text),
            "replayed": _head(unit.replayed.text),
        }
        structural_view: dict[str, object] = {}
        if siblings:
            structural_view["sibling_conflict_count"] = len(siblings)
            structural_view["sibling_spans"] = [list(s) for s in siblings]
        # Surface the tree-sitter enclosing node (if the extractor populated
        # one) as a semantic anchor in the structural view. This gives the
        # prompt builder a way to tell the model "you are merging inside def
        # greet()" — far sharper context than the raw line window alone. The
        # enclosing node text is NOT substituted for primary_text (the model
        # still needs the exact marker lines), but is provided alongside.
        meta = unit.structural_metadata
        if meta.get("enclosing_node_type"):
            structural_view["enclosing_node_type"] = meta["enclosing_node_type"]
            if meta.get("enclosing_node_signature"):
                structural_view["enclosing_node_signature"] = meta[
                    "enclosing_node_signature"
                ]
            if meta.get("enclosing_node_text"):
                structural_view["enclosing_node_text"] = meta["enclosing_node_text"]
            structural_view["unit_kind"] = unit.unit_kind
        return ContextBundle(
            primary_text=primary,
            side_summaries=side_summaries,
            token_estimate=est,
            structural_view=structural_view,
        )


def _sibling_spans(unit: ConflictUnit) -> list[tuple[int, int]]:
    """The marker spans of the *other* conflict units in this file, if any."""
    raw = unit.structural_metadata.get("sibling_units")
    if not raw:
        return []
    out: list[tuple[int, int]] = []
    for sib in raw:
        if sib.get("unit_id") == unit.unit_id:
            continue
        span = sib.get("marker_span")
        if isinstance(span, list) and len(span) == 2:
            out.append((int(span[0]), int(span[1])))
    return out


def _clamp_low(lo: int, block_start: int, siblings: list[tuple[int, int]]) -> int:
    """Raise ``lo`` so it doesn't enter a sibling block that ends just above."""
    for s_start, s_end in siblings:
        if s_end < block_start and s_end >= lo:
            # sibling block occupies [s_start, s_end]; stop just after it.
            lo = max(lo, s_end + 1)
    return lo


def _clamp_high(
    hi: int, block_end: int, siblings: list[tuple[int, int]], last_line: int
) -> int:
    """Lower ``hi`` so it doesn't enter a sibling block that starts just below."""
    for s_start, s_end in siblings:
        if s_start > block_end and s_start <= hi:
            hi = min(hi, s_start - 1)
    return hi


def _head(text: str, n: int = 200) -> str:
    text = text.strip()
    return text if len(text) <= n else text[:n] + " …"

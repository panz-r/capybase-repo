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
        if unit.marker_span is not None:
            start, end = unit.marker_span
            lo = max(0, start - self.context_lines)
            hi = min(len(lines) - 1, end + self.context_lines)
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
        return ContextBundle(
            primary_text=primary,
            side_summaries=side_summaries,
            token_estimate=est,
        )


def _head(text: str, n: int = 200) -> str:
    text = text.strip()
    return text if len(text) <= n else text[:n] + " …"

"""Context building for the resolver.

MVP: the conflict block, ±N surrounding lines, the file path, the inferred
language, and a best-effort enclosing symbol. The ``ContextBundle`` shape is
richer than this (related snippets, retrieved examples, structural view) so
program slicing, RAG, and AST views can be added later without changing the
resolver signature.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from capybase.conflict_model import ContextBundle, ConflictUnit, TokenBudget

if TYPE_CHECKING:
    from capybase.memory.retriever import Retriever


class ContextBuilder:
    def __init__(
        self,
        context_lines: int = 15,
        *,
        retriever: "Retriever | None" = None,
        retriever_k: int = 3,
        min_examples: int = 3,
        use_enclosing_as_primary: bool = False,
        canonicalize_context: bool = False,
    ) -> None:
        self.context_lines = context_lines
        self.retriever = retriever
        self.retriever_k = retriever_k
        self.min_examples = min_examples
        self.use_enclosing_as_primary = use_enclosing_as_primary
        self.canonicalize_context = canonicalize_context

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
        side_summaries = {
            "base": _head(unit.base.text),
            "current": _head(unit.current.text),
            "replayed": _head(unit.replayed.text),
        }
        structural_view: dict[str, object] = {}
        if siblings:
            structural_view["sibling_conflict_count"] = len(siblings)
            structural_view["sibling_spans"] = [list(s) for s in siblings]
        # Structural deconstruction: when tree-sitter resolved the enclosing
        # definition node and it fits the size budget, use it as primary_text
        # instead of the line window. The model sees the full logical block
        # (def/impl) it is merging inside — sharper than an arbitrary text
        # slice that may truncate mid-function. The line window remains the
        # fallback when the node is absent or too large.
        meta = unit.structural_metadata
        if meta.get("enclosing_node_type"):
            structural_view["enclosing_node_type"] = meta["enclosing_node_type"]
            if meta.get("enclosing_node_signature"):
                structural_view["enclosing_node_signature"] = meta[
                    "enclosing_node_signature"
                ]
            if meta.get("enclosing_node_text"):
                structural_view["enclosing_node_text"] = meta["enclosing_node_text"]
                if self.use_enclosing_as_primary:
                    primary = meta["enclosing_node_text"]
            structural_view["unit_kind"] = unit.unit_kind
        # Token canonicalization: strip comment lines, docstrings, and blank
        # runs from the context shown to the model. This reduces noise for a
        # 3B model prone to "lost in the middle" — the model focuses on the
        # functional code. Does NOT alter resolved_text (the model still emits
        # exact indentation); only the context window is cleaned.
        if self.canonicalize_context:
            primary = canonicalize_context(primary, unit.language)
        # Rough token estimate (~4 chars/token). Good enough for budgeting;
        # a real tokenizer can be swapped in later without interface change.
        est = max(1, len(primary) // 4)
        # RAG few-shot: retrieve similar past merges from the experience store
        # and inject them as dynamic demonstrations. The query is the conflict
        # "signature" (the three sides concatenated). Skipped when the retriever
        # is absent or the corpus is too small to be meaningful.
        retrieved: list = []
        if self.retriever is not None:
            query = " ".join([unit.base.text, unit.current.text, unit.replayed.text])
            try:
                candidates = self.retriever.retrieve(
                    query, k=self.retriever_k, language=unit.language
                )
                if len(candidates) >= self.min_examples or candidates:
                    retrieved = candidates
            except Exception:  # noqa: BLE001 - retrieval is best-effort
                pass
        return ContextBundle(
            primary_text=primary,
            side_summaries=side_summaries,
            retrieved_examples=retrieved,
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


def canonicalize_context(text: str, language: str | None = None) -> str:
    """Strip noise from the context window shown to the model.

    Removes standalone comment lines, collapses blank-line runs, and trims
    trailing whitespace — keeping the model focused on functional code rather
    than docstrings, license headers, or decorative comments. Indentation is
    PRESERVED (it is structurally significant). The conflict-marker lines
    (``<<<<<<<``, ``=======``, ``>>>>>>>``) are always kept — the model needs
    to see the exact block boundaries.
    """
    if not text:
        return text
    lines: list[str] = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        # Never strip conflict-marker lines — the model needs exact boundaries.
        if stripped.startswith(("<<<<<<<", "=======", ">>>>>>>", "|||||||")):
            lines.append(line.rstrip())
            continue
        # Drop full comment lines.
        if _is_context_comment(stripped, language):
            continue
        lines.append(line.rstrip())
    out = "\n".join(lines)
    # Collapse runs of blank lines to a single blank.
    while "\n\n\n" in out:
        out = out.replace("\n\n\n", "\n\n")
    return out


def _is_context_comment(stripped: str, language: str | None) -> bool:
    """True if a stripped line is entirely a comment (not code)."""
    if not stripped:
        return False
    if language == "python":
        return stripped.startswith("#")
    if language == "rust":
        return stripped.startswith(("//", "/*", "*", "*/"))
    return stripped.startswith(("#", "//"))

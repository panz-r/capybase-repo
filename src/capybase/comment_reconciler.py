"""Comment ledger + frontier selection for the deferred-comment-reconciliation system.

Parts C1 + C2 of the MVP. The ledger groups comment variants across the three
git versions (base/current/replayed) and the resolved buffer, keyed by lineage.
The frontier selects which comments actually need reconciliation (those affected
by the conflict — overlapping the conflict region, attached to changed code, or
edited by either side).

The ledger is the reconciler's input model: it carries the provenance (which
version each comment came from) the §8 prompt needs, and the byte spans the CST
editor needs for deterministic plan application.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from capybase.adapters.string_lexer import enumerate_comment_spans
from capybase.adapters.comment_classifier import (
    classify_spans, CommentClass, ClassifiedComment, NON_DEFERABLE,
)


@dataclass
class LedgerEntry:
    """One comment in one version (base/current/replayed/resolved).

    ``lineage_id`` groups variants of the same logical comment across versions.
    """
    lineage_id: str
    version: str           # "base" | "current" | "replayed" | "resolved"
    text: str              # the comment text
    cls: CommentClass      # the classification
    start: int             # byte offset in this version's text
    end: int               # byte offset (exclusive) in this version's text
    anchor_symbol: str = ""  # the enclosing entity's (kind, name), e.g. "function:foo"
    line: int = 0          # the 1-based line number (for display)
    # Whether the code attached to this comment's anchor changed across versions.
    changed_with_code: bool = False


def _anchor_for_span(
    text: str, start: int, lang: str, entities: list | None = None,
) -> str:
    """The enclosing entity's (kind:name) for a comment at byte offset ``start``.

    Uses the abstract parser's entity list (if provided) to find the lowest
    enclosing entity. Falls back to "" when no entity encloses the span.
    """
    if not entities:
        return ""
    # Convert byte offset to line number.
    line = text[:start].count("\n")
    for ent in entities:
        ent_span = getattr(ent, "span", None)
        if ent_span and line >= ent_span[0] and line <= ent_span[1]:
            kind = getattr(ent, "kind", "")
            name = getattr(ent, "name", "") or ""
            return f"{kind}:{name}"
    return ""


def _line_of(text: str, offset: int) -> int:
    """1-based line number for a byte offset."""
    return text[:offset].count("\n") + 1


def _token_jaccard(a: str, b: str) -> float:
    """Token-set Jaccard similarity (for comment correspondence)."""
    ta, tb = set(a.lower().split()), set(b.lower().split())
    if not ta and not tb:
        return 1.0
    u = ta | tb
    return len(ta & tb) / len(u) if u else 0.0


def build_comment_ledger(
    base: str,
    current: str,
    replayed: str,
    resolved: str,
    lang: str,
    *,
    base_entities: list | None = None,
    current_entities: list | None = None,
    replayed_entities: list | None = None,
    resolved_entities: list | None = None,
) -> list[LedgerEntry]:
    """Build the comment ledger from the four versions.

    Enumerates + classifies comments in each version, attaches anchor symbols,
    and assigns lineage_ids (grouping the same logical comment across versions
    by anchor + text similarity).

    Only DEFERRED comments are included in the ledger — non-deferable comments
    (MACHINE/LEGAL/GENERATED/DOCTEST) are preserved verbatim and don't need
    reconciliation.
    """
    versions = [
        ("base", base, base_entities),
        ("current", current, current_entities),
        ("replayed", replayed, replayed_entities),
        ("resolved", resolved, resolved_entities),
    ]
    entries: list[LedgerEntry] = []
    # Collect all deferred comments per version.
    raw_by_version: dict[str, list[ClassifiedComment]] = {}
    for vname, vtext, _vents in versions:
        spans = enumerate_comment_spans(vtext, lang)
        classified = classify_spans(spans, vtext, lang)
        raw_by_version[vname] = [c for c in classified if c.cls == CommentClass.DEFERRED]

    # Assign lineage_ids: group across versions by (anchor_symbol, text similarity).
    # Strategy: for each version's comments, try to match against already-seen
    # comments from OTHER versions at the same anchor. If no match, new lineage.
    lineage_counter = 0
    # Track (anchor_symbol → list of (lineage_id, text)) across all versions.
    anchor_lineages: dict[str, list[tuple[str, str]]] = {}

    for vname, vtext, vents in versions:
        ents = vents or []
        for cc in raw_by_version[vname]:
            anchor = _anchor_for_span(vtext, cc.start, lang, ents)
            line = _line_of(vtext, cc.start)
            # Try to match an existing lineage at the same anchor with similar text.
            best_lineage = None
            best_sim = 0.0
            for lid, ltext in anchor_lineages.get(anchor, []):
                sim = _token_jaccard(cc.text, ltext)
                if sim > best_sim:
                    best_sim = sim
                    best_lineage = lid
            if best_lineage is not None and best_sim >= 0.3:
                lineage_id = best_lineage
            else:
                lineage_counter += 1
                lineage_id = f"LC{lineage_counter}"
            # Register this comment under its anchor for future matching.
            anchor_lineages.setdefault(anchor, []).append((lineage_id, cc.text))
            entries.append(LedgerEntry(
                lineage_id=lineage_id,
                version=vname,
                text=cc.text,
                cls=cc.cls,
                start=cc.start,
                end=cc.end,
                anchor_symbol=anchor,
                line=line,
            ))

    return entries


def select_comment_frontier(
    ledger: list[LedgerEntry],
    *,
    conflict_byte_ranges: list[tuple[int, int]] | None = None,
) -> list[LedgerEntry]:
    """The subset of ledger entries that need reconciliation.

    A comment is in the frontier when ANY of:
    - It's in the RESOLVED version (the code we're about to write — its comments
      might be stale from the merge).
    - It overlaps a conflict region (byte-range intersection).
    - Its text differs across versions (both sides edited it, or one side changed it).
    - Its anchor's code changed (the entity it documents was modified).

    Non-frontier comments are reattached verbatim (the fast path). For the MVP,
    we use a conservative frontier: include all RESOLVED-version comments (since
    that's the file we're writing) whose lineage has variants that differ, OR
    that overlap the conflict region.
    """
    if not ledger:
        return []
    # Group by lineage_id.
    by_lineage: dict[str, list[LedgerEntry]] = {}
    for e in ledger:
        by_lineage.setdefault(e.lineage_id, []).append(e)

    # A lineage needs reconciliation if its variants differ across versions.
    def _lineage_differs(entries: list[LedgerEntry]) -> bool:
        texts = {e.text.strip() for e in entries}
        if len(texts) > 1:
            return True  # different text across versions
        versions_seen = {e.version for e in entries}
        # If a lineage appears in only SOME versions (added/deleted by one side).
        if not ({"base", "current", "replayed"} <= versions_seen or versions_seen == {"resolved"}):
            return True
        return False

    # Check conflict-region overlap.
    def _overlaps_conflict(entry: LedgerEntry) -> bool:
        if not conflict_byte_ranges:
            return False
        for cs, ce in conflict_byte_ranges:
            if entry.start < ce and entry.end > cs:
                return True
        return False

    frontier: list[LedgerEntry] = []
    for lid, entries in by_lineage.items():
        differs = _lineage_differs(entries)
        overlaps = any(_overlaps_conflict(e) for e in entries)
        if differs or overlaps:
            # Include the RESOLVED version's entry (the one we'll rewrite). If
            # there's no resolved entry (the comment was deleted), include the
            # base/current/replayed entry for the reconciler to disposition.
            resolved_entries = [e for e in entries if e.version == "resolved"]
            if resolved_entries:
                frontier.extend(resolved_entries)
            else:
                frontier.extend(entries)
    return frontier


__all__ = [
    "LedgerEntry",
    "build_comment_ledger",
    "select_comment_frontier",
]

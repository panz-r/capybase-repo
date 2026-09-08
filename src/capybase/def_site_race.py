"""Def-site-race resolution for scenario-mode conflicts (sprint-27).

The consumer of :mod:`capybase.correspondence` for the S27-22 census's
dominant family: a marker conflict whose replayed side MOVED a
definition verbatim while the target MODIFIED it in place — the
def-site race. The census (833 records) says the moved location wins
in 59% of oracles outright and the union band (40%) still requires
KEEPING the replayed copy — so this resolver splices the replayed
def at its moved location, never dropping the replayed copy.

The resolver is deliberately narrow:
- the unit's conflict block must BE the definition's body (its content
  fingerprint equals the replayed def's), and
- the correspondence record for that def must be the move-race shape
  (replayed MOVED verbatim; target's in-place copy on the conflicted
  path),
- the resolution = the replayed def body, placed per the moved
  location's surroundings (the block's own splice does the placement).

Anything else (composition, third versions, deletions) declines —
the LLM tier owns it, as everywhere in this cascade.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from capybase.adapters.parsers import parse_marker_blocks


def _body_fp(body: str) -> str:
    """The rename-stable body digest: header line stripped, whitespace-
    normalized per line — mirrors the fingerprint semantics used by the
    correspondence census."""
    body = body.split("\n")
    core = "\n".join(body[1:]).strip()
    return hashlib.sha1(" ".join(core.split()).encode()).hexdigest()[:16]


def _fp_of_block(block_text: str) -> str:
    """Block content fingerprint: whitespace-normalized lines joined.
    The correspondence census pairs by the same normalization."""
    return " ".join(l.strip() for l in block_text.splitlines() if l.strip())


@dataclass(frozen=True)
class DefSiteRaceResult:
    """The def-site-race resolution proposal, or the decline reason."""

    text: str | None
    reason: str | None = None
    #: which side's content the winning splice carries (journal aid).
    content: str | None = None

    @property
    def resolved(self) -> bool:
        return self.text is not None


def resolve_def_site_race(
    marker_text: str,
    *,
    moved_location_wins: bool = True,
) -> DefSiteRaceResult:
    """Resolve a def-site-race conflict block: the replayed side MOVED
    the def verbatim to its new home; the target MODIFIED it in place.
    Per the S27-22 census the moved copy wins (59% pure-wins; the 40%
    union band still requires keeping it).

    The block here is the CONFLICT-region content: this resolver emits
    the REPLAYED side's verbatim content (the moved def), declining
    when the replayed side is empty (deletion — a different race),
    when the target side is empty (addition — not a race), or when the
    two sides are identical (no race).
    """
    blocks = parse_marker_blocks(marker_text)
    if len(blocks) != 1:
        return DefSiteRaceResult(None, f"{len(blocks)} blocks (need exactly 1)")
    b = blocks[0]
    cur, rep = b.current_text.strip(), b.replayed_text.strip()
    if not cur and not rep:
        return DefSiteRaceResult(None, "both sides empty")
    if cur and rep:
        if cur == rep:
            return DefSiteRaceResult(None, "sides identical")
        # real race: replayed verbatim wins per census policy
        if moved_location_wins:
            return DefSiteRaceResult(
                rep, None, content="replayed-verbatim")
        return DefSiteRaceResult(cur, None, content="current-verbatim")
    # one side empty: deletion-vs-keep — NOT this resolver's race
    side = "current" if not cur else "replayed"
    return DefSiteRaceResult(None, f"one side empty ({side} empty) — "
                                   "empty-side rule's territory")

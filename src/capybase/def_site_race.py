"""Deterministic resolver for the move-race conflict family (s27-24).

The move-race shape: the replayed commit MOVED a definition and the
target branch edited it in place — git surfaces an edit/edit conflict at
the old location whose replayed side is the moved definition verbatim.

EVIDENCE GATE (the actual contract): this module's resolver alone is
NOT evidence — any two-sided conflict would resolve. The evidence is
registered by the scenario harness (``orch._race_step_paths``): a path
is registered only when the conflict block's content is a SUBSET of the
source tip's tree at another home (the moved def's new home), i.e.
cross-file move evidence computed from the repo. The orchestrator
fires this resolver only on registered paths
(``future.enable_def_site_race`` + evidence present).

Census (s27-22, 833 moved-race records): oracle = the moved
(replayed-side) content verbatim in 59% of records; the remaining 41%
still keep the replayed copy. The resolver therefore proposes the
replayed side verbatim and relies on the standard validation pipeline
(nothing bypasses it).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from capybase.adapters.parsers import parse_marker_blocks


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

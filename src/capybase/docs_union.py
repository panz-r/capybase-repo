"""Deterministic docs-union for changelog-shaped conflicts (sprint-27).

Changelogs, whats-new files, and release notes are append-only logs:
both sides' entries are additive, and the human merge is almost always
the UNION of both sides' additions (corpus-measured: 48/51 = 94% of
changelog conflicts with both sides adding resolve to the union; the
other 3 pick one side — see PLAN-LEDGER S27-08). This module detects
changelog-shaped conflicts and proposes the ordered union as a
deterministic candidate.

Shape detection is conservative on two axes: the path must name a
changelog (whats_new/changelog/changes/news/history/release-notes +
.rst/.md/.txt), and the content must show heading/underline or
entry-list structure. A random .rst how-to is NOT an append-only log.

The union: the current side is the canonical sequence (the trunk's
file); each replayed-side addition is spliced after the last
occurrence of its ANCHOR — the nearest preceding base line in the
replayed file — so entries land in their section. Additions with no
base anchor (new sections) append at the end. Duplicates (entries both
sides added) are skipped. The result runs the standard validation
pipeline; nothing bypasses it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: Path-shape gate: filename says changelog + docs extension.
_CHANGELOG_PATH = re.compile(
    r"(whats_?new|change_?log|changes|news|history|release[_-]?notes)",
    re.IGNORECASE,
)
_DOCS_EXT = (".rst", ".md", ".txt")

#: Content-shape gate: RST section underline, Markdown ATX heading, or
#: a bulleted/numbered/directive entry line.
_RST_UNDERLINE = re.compile(r"^[=\-~^\"']{3,}\s*$")
_MD_HEADING = re.compile(r"^#{1,6}\s+\S")
_ENTRY_LINE = re.compile(r"^\s*(?:[-*+]\s+\S|\d+\.\s+\S|\.\. \S)")


def is_changelog_path(path: str) -> bool:
    """True when the path names a changelog-ish docs file."""
    p = (path or "").lower()
    return bool(_CHANGELOG_PATH.search(p)) and p.endswith(_DOCS_EXT)


def _looks_like_changelog(text: str) -> bool:
    """Content gate: heading/underline AND entry-list structure present."""
    lines = text.splitlines()
    headings = entries = 0
    for i, ln in enumerate(lines):
        if _MD_HEADING.match(ln):
            headings += 1
        elif _RST_UNDERLINE.match(ln) and i > 0 and lines[i - 1].strip():
            headings += 1
        elif _ENTRY_LINE.match(ln):
            entries += 1
    return headings >= 1 and entries >= 1


def _norm(line: str) -> str:
    return " ".join(line.split())


@dataclass(frozen=True)
class DocsUnionResult:
    """The deterministic docs-union proposal, or the decline reason.

    ``text`` is None on decline (``reason`` explains); non-None text is a
    candidate for the standard validation pipeline — accepted only if it
    passes (markers/syntax/splice checks apply as usual).
    """

    text: str | None
    reason: str | None = None
    #: entries taken from each side (for journaling).
    current_entries: int = 0
    replayed_entries: int = 0

    @property
    def resolved(self) -> bool:
        return self.text is not None


def propose_docs_union(
    path: str,
    base: str,
    current: str,
    replayed: str,
) -> DocsUnionResult:
    """Propose the ordered union for one changelog-shaped conflict.

    Declines (text=None) when: the path isn't changelog-shaped; the
    content doesn't look like an append-only log; either side removed
    existing entries (a changelog edit that rewrites history is not a
    union); or a side adds nothing (the union degenerates to the other
    side — the source portfolio's job, not ours).
    """
    if not is_changelog_path(path):
        return DocsUnionResult(None, "path is not changelog-shaped")
    if not (_looks_like_changelog(current) or _looks_like_changelog(replayed)):
        return DocsUnionResult(None, "content is not changelog-shaped")

    base_lines = base.splitlines()
    cur_lines = current.splitlines()
    rep_lines = replayed.splitlines()

    base_set = {_norm(l) for l in base_lines if _norm(l)}
    cur_set = {_norm(l) for l in cur_lines if _norm(l)}
    rep_set = {_norm(l) for l in rep_lines if _norm(l)}

    # A side that REMOVES base entries is rewriting, not appending.
    removed_by_cur = base_set - cur_set
    removed_by_rep = base_set - rep_set
    if removed_by_cur or removed_by_rep:
        return DocsUnionResult(
            None,
            f"side removes existing entries "
            f"(cur -{len(removed_by_cur)}, rep -{len(removed_by_rep)})",
        )

    # Additions per side (order-preserving), keyed by normalized text.
    rep_norm = [_norm(l) for l in rep_lines]
    cur_adds = [l for l in cur_lines if _norm(l) and _norm(l) not in base_set]
    rep_adds = [
        (i, l) for i, l in enumerate(rep_lines)
        if rep_norm[i] and rep_norm[i] not in base_set
    ]
    if not cur_adds and not rep_adds:
        return DocsUnionResult(None, "no additions on either side")
    if not cur_adds or not rep_adds:
        return DocsUnionResult(None, "one-sided additions (source portfolio's job)")

    out = list(cur_lines)
    out_norm = [_norm(l) for l in out]
    inserted = skipped_dup = 0
    for pos, add in rep_adds:
        add_n = rep_norm[pos]
        if add_n in out_norm:
            skipped_dup += 1
            continue
        # Anchor: nearest preceding BASE line in the replayed file.
        anchor = None
        for j in range(pos - 1, -1, -1):
            if rep_norm[j] and rep_norm[j] in base_set:
                anchor = rep_norm[j]
                break
        placed = False
        if anchor is not None and anchor in out_norm:
            # Insert after the LAST occurrence of the anchor, extended
            # past any contiguous non-base lines already following it
            # (groups a side's entries under one anchor).
            i = len(out_norm) - 1 - out_norm[::-1].index(anchor)
            k = i + 1
            while k < len(out_norm) and out_norm[k] not in base_set:
                k += 1
            out.insert(k, add)
            out_norm.insert(k, add_n)
            placed = True
        if not placed:
            # No anchor in the output (a brand-new section): append.
            out.append(add)
            out_norm.append(add_n)
        inserted += 1

    return DocsUnionResult(
        "\n".join(out) + ("\n" if current.endswith("\n") else ""),
        None,
        current_entries=len(cur_adds),
        replayed_entries=inserted,
    )

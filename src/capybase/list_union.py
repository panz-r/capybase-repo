"""Deterministic list-union for sorted-name-list conflicts (sprint-27).

AUTHORS, .mailmap, CONTRIBUTORS, CREDITS — files that are one entry per
line, maintained by appending names, usually kept sorted. When both
sides of a conflict touch the list, the human merge is the CURRENT
side's lines plus the REPLAYED side's ADDITIONS (corpus-measured,
S27-48, across all 141 libuv list-file conflict blocks: 104 blocks
reproduce the oracle line-set exactly, 37 more differ by a couple of
target-side lines the human merge dropped — ~0.977 token-jaccard, still
far above the 0.90 verdict bar; zero blocks drop a replayed addition).
The current side's own removals are RESPECTED, not unioned back: the
target's dedups/cleanups survive the merge (the naive base∪adds union
invented lines in 17 blocks and its removal-decline skipped 101 more).

Ordering does not affect the verdict metric (token-jaccard is
set-based), but the mechanism emits the most plausible order: the
sorted union when both sides are individually sorted (the AUTHORS
case), else the current side's order with the replayed-only lines
appended.

Declines when the replayed side adds nothing (nothing to union — the
source portfolio's job). The proposed text runs the standard validation
pipeline; nothing bypasses it.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Basenames (lowercased, optional .txt/.md/.alist extensions) that name
#: a one-entry-per-line person list. README is deliberately absent —
#: prose, not a list.
_LIST_BASENAMES = {
    "authors", "contributors", "credits", ".mailmap", "mailmap",
    "acknowledgements", "acknowledgments", "thanksto",
}
_LIST_EXTS = ("", ".txt", ".md", ".alist")


def is_list_file_path(path: str) -> bool:
    """True when the basename names a one-entry-per-line list file."""
    base = (path or "").lower().rsplit("/", 1)[-1]
    if base.startswith("."):
        stem, found_ext = base, ""
    else:
        stem, _, found_ext = base.partition(".") if "." in base else (base, "", "")
    if found_ext and f".{found_ext}" not in _LIST_EXTS:
        return False
    return stem in _LIST_BASENAMES or base in _LIST_BASENAMES


def _norm(line: str) -> str:
    return " ".join(line.split())


def _is_sorted(lines: list[str]) -> bool:
    keyed = [l.casefold() for l in lines if l.strip()]
    return keyed == sorted(keyed)


@dataclass(frozen=True)
class ListUnionResult:
    """The deterministic list-union proposal, or the decline reason."""

    text: str | None
    reason: str | None = None
    #: entries taken from each side (for journaling).
    current_entries: int = 0
    replayed_entries: int = 0

    @property
    def resolved(self) -> bool:
        return self.text is not None


def propose_list_union(
    path: str,
    base: str,
    current: str,
    replayed: str,
) -> ListUnionResult:
    """Propose the current-side lines + replayed additions for a list
    conflict.

    Declines (text=None) when the path isn't a list file or the
    replayed side adds nothing.
    """
    if not is_list_file_path(path):
        return ListUnionResult(None, "path is not a list file")

    base_lines = base.splitlines()
    cur_lines = current.splitlines()
    rep_lines = replayed.splitlines()

    base_set = {_norm(l) for l in base_lines if _norm(l)}
    cur_set = {_norm(l) for l in cur_lines if _norm(l)}
    rep_adds = [l for l in rep_lines if _norm(l) and _norm(l) not in base_set]
    rep_adds = [l for l in rep_adds if _norm(l) not in cur_set]
    if not rep_adds:
        return ListUnionResult(None, "replayed side adds nothing")

    if _is_sorted(cur_lines) and _is_sorted(rep_lines):
        out = sorted(cur_lines + rep_adds, key=lambda l: l.casefold())
    else:
        out = cur_lines + rep_adds
    # Dedupe by normalized text, keeping each line's first occurrence's
    # original formatting (a name both sides added, differing only in
    # whitespace, is one entry).
    seen: set[str] = set()
    union: list[str] = []
    for l in out:
        n = _norm(l)
        if n and n not in seen:
            union.append(l)
            seen.add(n)

    return ListUnionResult(
        "\n".join(union) + ("\n" if current.endswith("\n") else ""),
        None,
        current_entries=len(cur_set),
        replayed_entries=len(rep_adds),
    )

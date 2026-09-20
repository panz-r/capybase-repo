"""Lossless common-span factoring for large conflict sides (S28-136).

When the three conflict sides (base / current / replayed interiors of the
marker block) share large identical line runs, the resolve prompt renders
each side as its DIFFERING segments plus ordered shared-span references
(``@A1`` ... ``@An``), and the model's resolution may reference the same
spans; capybase re-expands them verbatim into the final resolved text.

Why this is lossless: a run identical in ALL THREE versions is unchanged
material by diff3 semantics — any merge preserves it. The model never
needs its content; it needs only where the differences sit relative to the
shared anchors. Capybase owns the span text and the expansion is a
mechanical splice — nothing is summarized, nothing can be silently
altered (the design principle: prove identity, make preservation
mechanical).

Protocol (strict):
  - a reference is a FULL line matching ``@A<digits>`` exactly;
  - the resolution must reference A1..An exactly once each, in ascending
    order, with no unknown indices;
  - literal lines pass through verbatim.
Any violation → the caller treats the attempt as failed and retries
WITHOUT factoring (the full sides; the oversize guards then behave
exactly as today).
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

_REF_RE = re.compile(r"^@A(\d+)\b")

#: Smallest run (lines) eligible for factoring — shorter runs stay inline
#: (the reference line itself costs ~1 token, so tiny runs are net losses).
DEFAULT_MIN_RUN = 4

#: The factoring gate: don't engage the protocol unless the shared material
#: is a meaningful fraction of the largest side AND absolutely large.
DEFAULT_MIN_SHARED_FRAC = 0.35
DEFAULT_MIN_SHARED_LINES = 60


def _matching_blocks(a: list[str], b: list[str]):
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    return matcher.get_matching_blocks()


def _qualifying_common_ranges(
    base_lines: list[str],
    cur_lines: list[str],
    rep_lines: list[str],
    *,
    min_run: int,
) -> list[tuple[int, int, int, int]]:
    """Base-coordinate ranges identical in ALL THREE side texts.

    A range qualifies only when it is fully contained in ONE base↔current
    matching block AND ONE base↔replayed matching block: inside a matching
    block the side is verbatim-identical to base, so containment proves the
    range reads the same in base, current, and replayed.

    Returns disjoint ``(lo, hi, cur_shift, rep_shift)`` tuples — the 0-based
    base-coordinate range plus the per-side shift (``side_index = base_index
    + shift``) proven by that range's blocks — each of length ≥ ``min_run``,
    sorted by start.
    """
    cur_blocks = [
        (m.a, m.b, m.size) for m in _matching_blocks(base_lines, cur_lines)[:-1]
    ]
    rep_blocks = [
        (m.a, m.b, m.size) for m in _matching_blocks(base_lines, rep_lines)[:-1]
    ]

    tagged: list[tuple[int, int, int, int]] = []
    for ci, (ca, cb, csize) in enumerate(cur_blocks):
        for ri, (ra, rb, rsize) in enumerate(rep_blocks):
            lo, hi = max(ca, ra), min(ca + csize, ra + rsize)
            if hi - lo >= min_run:
                tagged.append((lo, hi, ci, ri))
    tagged.sort()

    # Merge ranges proven by the SAME block pair when they overlap or touch
    # (the union is still contained in both blocks). Each range carries its
    # side shift: side_index = base_index + shift.
    runs: list[list[int]] = []  # [lo, hi, cur_shift, rep_shift]
    for lo, hi, ci, ri in tagged:
        c_shift = cur_blocks[ci][1] - cur_blocks[ci][0]
        r_shift = rep_blocks[ri][1] - rep_blocks[ri][0]
        if (
            runs
            and c_shift == runs[-1][2]
            and r_shift == runs[-1][3]
            and lo <= runs[-1][1]
        ):
            runs[-1][1] = max(runs[-1][1], hi)
        else:
            runs.append([lo, hi, c_shift, r_shift])

    # Disjointify across different shifts: later ranges are clipped to start
    # after the previous range (their overlap was already covered).
    disjoint: list[list[int]] = []
    prev_hi = -1
    for lo, hi, c_shift, r_shift in runs:
        if lo < prev_hi:
            continue  # already covered by an earlier range
        disjoint.append([lo, hi, c_shift, r_shift])
        prev_hi = hi
    return disjoint


def factor_common_spans(
    base: str,
    current: str,
    replayed: str,
    *,
    min_run: int = DEFAULT_MIN_RUN,
    min_shared_frac: float = DEFAULT_MIN_SHARED_FRAC,
    min_shared_lines: int = DEFAULT_MIN_SHARED_LINES,
) -> dict | None:
    """Factor line runs identical across ALL THREE sides into shared spans.

    A shared span is a base-coordinate range fully contained in one
    base↔current matching block AND one base↔replayed matching block —
    i.e. provably identical (line-for-line) in base, current, AND replayed.
    Adjacent proofs merge. Returns ``None`` when the shared material is
    below the gate — the caller keeps the standard (unfactored) prompt
    byte-identical. The returned dict:

      rendered_base / rendered_cur / rendered_rep — the factored side texts
        (differing segments + ``@Ak (N shared lines elided)`` reference lines)
      spans        — shared-span texts, document order (for expansion)
      shared_lines — total shared line count
      max_side     — the largest side's line count
    """
    base_lines = base.split("\n")
    cur_lines = current.split("\n")
    rep_lines = replayed.split("\n")

    ranges = _qualifying_common_ranges(
        base_lines, cur_lines, rep_lines, min_run=min_run)
    if not ranges:
        return None

    shared = sum(hi - lo for lo, hi, *_ in ranges)
    max_side = max(len(base_lines), len(cur_lines), len(rep_lines))
    if shared < min_shared_lines or (
        max_side and shared / max_side < min_shared_frac
    ):
        return None
    spans = ["\n".join(base_lines[lo:hi]) for lo, hi, *_ in ranges]

    def _render(side_lines: list[str], shift_for) -> str:
        """Render one side: differing segments inline, ``@Ak`` reference
        lines at each shared span. ``shift_for(k)`` maps the k-th range's
        base coordinates to this side's coordinates (identity for base)."""
        parts: list[str] = []
        prev = 0  # 0-based side line cursor
        for k, (lo, hi, _cs, _rs) in enumerate(ranges):
            shift = shift_for(k)
            s_lo, s_hi = lo + shift, hi + shift
            if s_lo < prev:
                return None  # overlapping side images — not renderable
            parts.extend(side_lines[prev:s_lo])
            parts.append(f"@A{k + 1} ({hi - lo} shared lines elided)")
            prev = s_hi
        parts.extend(side_lines[prev:])
        return "\n".join(parts)

    rendered_base = _render(base_lines, lambda k: 0)
    rendered_cur = _render(cur_lines, lambda k: ranges[k][2])
    rendered_rep = _render(rep_lines, lambda k: ranges[k][3])
    return {
        "spans": spans,
        "rendered_base": rendered_base,
        "rendered_cur": rendered_cur,
        "rendered_rep": rendered_rep,
        "shared_lines": shared,
        "max_side": max_side,
    }


def ref_index(line: str) -> int | None:
    """The span index of a reference line, or None.

    A reference line starts with ``@A<digits>`` (after stripping); the
    renderer emits it with a trailing count annotation — ``@A1 (6 shared
    lines elided)`` — and models may copy that annotated form back, so
    trailing text is tolerated. A reference NOT at line start (e.g. inside
    a code line) stays literal.
    """
    stripped = line.strip()
    m = _REF_RE.match(stripped)
    return int(m.group(1)) - 1 if m else None


def expand_factored_resolution(
    resolved_text: str, spans: list[str]
) -> str | None:
    """Re-expand a factored resolution into the full resolved text.

    Strict protocol: every reference line must be ``@A<index+1>``, each
    referenced exactly ONCE, in ascending order, no unknown indices, and
    the referenced spans must lie in range. Any violation → None (the
    caller retries unfactored). On success the references are replaced by
    their span texts verbatim.
    """
    spans_n = len(spans)
    seen: list[int] = []
    out: list[str] = []
    for line in resolved_text.split("\n"):
        idx = ref_index(line)
        if idx is None:
            out.append(line)
            continue
        if idx < 0 or idx >= spans_n:
            return None  # unknown index → protocol violation
        if seen and idx <= seen[-1]:
            return None  # out of order or repeated
        seen.append(idx)
        out.append(spans[idx])
    if sorted(seen) != list(range(spans_n)):
        # Not every shared span was referenced → the reconstruction would
        # silently drop common material → protocol violation.
        return None
    return "\n".join(out)

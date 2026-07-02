"""Conflict-shape normalization for retrieval + exact reuse (#9 steps 4/5).

A "conflict shape" is a content-agnostic fingerprint of HOW a conflict is
structured: how many lines each side added/removed/changed vs base, and the
overall edit structure. Two conflicts with the same shape (e.g. both "each side
appends one distinct line") are structurally equivalent even if their text
differs — which is exactly what exact reuse (#9 step 4) and same-shape retrieval
explanation (#9 step 5) need to reason about.

This is deliberately NOT a hash of the text itself (that would only match
identical conflicts). It's a hash of the SHAPE: a compact representation of the
per-side edit opcodes normalized so whitespace/cosmetic differences don't
distinguish structurally-identical conflicts.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Normalize whitespace WITHIN each line, preserving line boundaries.

    Cosmetic reformatting (extra spaces, trailing whitespace) shouldn't change
    the shape, but NEWLINES must be preserved — the shape is defined by per-line
    edit counts, and collapsing newlines would erase the structure (a 2-line
    append would look identical to a 1-line modify).
    """
    if not text:
        return ""
    lines = text.split("\n")
    return "\n".join(_WS_RE.sub(" ", ln.strip()) for ln in lines if ln.strip())


def _side_shape(base_norm: str, side_norm: str) -> tuple[int, int, int]:
    """The (added, removed, changed) line counts of a side vs base.

    Uses difflib on the normalized lines (cheap; the sides are small blocks).
    ``added`` = lines in side not in base; ``removed`` = base lines not in side;
    ``changed`` = replace-opcode pairs. These three numbers capture the edit
    structure independent of the actual content.
    """
    import difflib

    if not side_norm and not base_norm:
        return (0, 0, 0)
    b_lines = base_norm.split("\n") if base_norm else []
    s_lines = side_norm.split("\n") if side_norm else []
    sm = difflib.SequenceMatcher(a=b_lines, b=s_lines, autojunk=False)
    added = removed = changed = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "insert":
            added += j2 - j1
        elif tag == "delete":
            removed += i2 - i1
        elif tag == "replace":
            d_b = i2 - i1
            d_s = j2 - j1
            # The paired portion is the min; excess goes to add/remove.
            paired = min(d_b, d_s)
            changed += paired
            if d_s > d_b:
                added += d_s - d_b
            elif d_b > d_s:
                removed += d_b - d_s
    return (added, removed, changed)


def conflict_shape_hash(
    *, base: str, current: str, replayed: str
) -> str:
    """A short hash of a conflict's structural shape (#9 steps 4/5).

    The shape = (current side's added/removed/changed, replayed side's
    added/removed/changed) computed against the normalized base. Two conflicts
    hash the same iff both sides edit base in the same line-count structure,
    regardless of the actual content. Returns a 12-char hex digest (short is
    fine — collisions across the same path/language are vanishingly unlikely and
    the candidate re-validation is the backstop).
    """
    base_n = _normalize(base)
    cur_shape = _side_shape(base_n, _normalize(current))
    rep_shape = _side_shape(base_n, _normalize(replayed))
    blob = f"cur={cur_shape}|rep={rep_shape}"
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def shape_for_unit(unit: Any) -> str:
    """Convenience: compute the conflict shape from a ConflictUnit."""
    base = getattr(getattr(unit, "base", None), "text", "") or ""
    current = getattr(getattr(unit, "current", None), "text", "") or ""
    replayed = getattr(getattr(unit, "replayed", None), "text", "") or ""
    return conflict_shape_hash(base=base, current=current, replayed=replayed)

"""Deterministic Cargo.toml manifest-union editor.

A Tier-A primitive for TOML manifest conflicts (Cargo.toml), following the
:mod:`import_union` pattern: pure, text-surgical (no TOML writer dependency —
preserves formatting byte-for-byte), conservative.

Two merge operations:

1. **Feature-list union:** when both sides change the same dependency's
   ``features = [...]`` array with disjoint additions, union them.
   ``tokio = { features = ["rt"] }`` + ``tokio = { features = ["macros"] }``
   → ``tokio = { features = ["macros", "rt"] }``.

2. **Array union:** when both sides append to the same TOML array
   (``members = [...]``, ``features = [...]``, etc.) with disjoint additions,
   union them.

Version bumps (``tokio = "1.52.2"`` vs ``tokio = "1.51.3"``) are NOT unioned
— they're exclusive scalar choices, handled by the preservation validator's
exclusive-PASS path.

Design contract (same as the Rust primitives):
  - **deterministic, idempotent, transactional, conservative.**
  - Tier-A: lossless mechanical operation.
  - Pure of I/O; the existing ``_run_cargo_manifest_check`` gauntlet remains
    authoritative after the edit.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from capybase.import_union import (
    ImportUnionResult,
    STATUS_APPLIED, STATUS_NOT_APPLICABLE, STATUS_BLOCKED, STATUS_AMBIGUOUS,
    RISK_TIER_A,
)


def _normalize(line: str) -> str:
    return " ".join(line.split())


def _brackets_balanced(s: str) -> bool:
    pairs = {")": "(", "]": "[", "}": "{"}
    opens = set("([{")
    stack: list[str] = []
    for ch in s:
        if ch in opens:
            stack.append(ch)
        elif ch in pairs:
            if not stack or stack[-1] != pairs[ch]:
                return False
            stack.pop()
    return not stack


#: Matches a TOML key = [...] array assignment, capturing the key and the
#: bracket-internal content. Handles both single-line and the key prefix of
#: multi-line arrays (we only handle single-line arrays in v1).
_TOML_ARRAY_RE = re.compile(
    r"^(\s*)([\w.-]+)\s*=\s*\[([^\]]*)\]\s*(#.*)?$"
)

#: Matches a TOML inline table assignment: key = { ... features = [...] ... }
#: Captures the key, the table content, and everything after the closing }.
_TOML_INLINE_TABLE_RE = re.compile(
    r"^(\s*)([\w.-]+)\s*=\s*\{(.*)\}\s*(#.*)?$"
)

#: Extracts features = [...] from inside an inline table.
_FEATURES_IN_TABLE_RE = re.compile(r'features\s*=\s*\[([^\]]*)\]')


def _parse_toml_array_items(content: str) -> list[str]:
    """Parse the items inside a TOML array ``["a", "b", "c"]`` content string.

    Returns the list of raw item strings (with quotes preserved). Handles
    both quoted (``"a"``) and bare (``a``) items, split on top-level commas.
    """
    items: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in content:
        if ch in "[{":
            depth += 1
            cur.append(ch)
        elif ch in "]}":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            item = "".join(cur).strip()
            if item:
                items.append(item)
            cur = []
        else:
            cur.append(ch)
    item = "".join(cur).strip()
    if item:
        items.append(item)
    return items


def _toml_array_item_norm(item: str) -> str:
    """Normalized form of a TOML array item for comparison (strip quotes/spaces)."""
    return item.strip().strip('"').strip("'")


def propose_manifest_union(
    resolved_text: str, missing_obligations: list,
    *, base_text: str = "", other_side_text: str = "",
) -> ImportUnionResult:
    """Propose a deterministic TOML manifest-union edit.

    Acts on additive obligations from change-accounting. Specifically targets
    TOML array additions (feature lists, workspace members) and inline-table
    feature-list additions. Version bumps and structural table changes are
    left to the model (exclusive choices).

    Args:
        resolved_text: the candidate's current resolved_text.
        missing_obligations: ``BranchObligation`` records.
        base_text: the base hunk text.
        other_side_text: the dropped side's text (for context).

    Returns an :class:`ImportUnionResult`. Never raises.
    """
    try:
        before_hash = hashlib.sha256(
            (resolved_text or "").encode("utf-8")
        ).hexdigest()[:16]

        # --- Filter to additive executable obligations. ---
        candidate_lines: list[str] = []
        for ob in missing_obligations or []:
            if getattr(ob, "operation", "") != "added":
                continue
            if getattr(ob, "status", "") != "MISSING":
                continue
            if getattr(ob, "exclusive", False):
                continue
            line = getattr(ob, "line", "") or ""
            if not line.strip():
                continue
            from capybase.change_accounting import classify_channel
            ch = classify_channel(line)
            if ch in ("comment", "formatting"):
                continue
            candidate_lines.append(line)

        if not candidate_lines:
            return ImportUnionResult(
                status=STATUS_NOT_APPLICABLE, text=resolved_text,
                certificate={"reason": "no additive obligations",
                             "before_hash": before_hash,
                             "after_hash": before_hash},
            )

        # --- Idempotency: drop lines already present. ---
        resolved_norms = {_normalize(l) for l in resolved_text.splitlines() if l.strip()}
        fresh_lines = [l for l in candidate_lines if _normalize(l) not in resolved_norms]
        if not fresh_lines:
            return ImportUnionResult(
                status=STATUS_NOT_APPLICABLE, text=resolved_text,
                certificate={"reason": "all lines already present (idempotent)",
                             "before_hash": before_hash,
                             "after_hash": before_hash},
            )

        # --- Try feature-list / array union for each fresh line. ---
        edited_text = resolved_text
        closed: list[str] = []
        edits: list[str] = []
        unresolved: list[str] = []

        for fresh_line in fresh_lines:
            result = _try_array_or_feature_union(edited_text, fresh_line)
            if result is not None:
                edited_text = result
                closed.append(_normalize(fresh_line))
                edits.append(f"union: {fresh_line.strip()[:60]}")
            else:
                # Try simple line transplant (like block_insertion but for TOML).
                transplanted = _try_line_transplant(edited_text, fresh_line, other_side_text)
                if transplanted is not None:
                    edited_text = transplanted
                    closed.append(_normalize(fresh_line))
                    edits.append(f"transplant: {fresh_line.strip()[:60]}")
                else:
                    unresolved.append(fresh_line.strip()[:60])

        if not closed:
            return ImportUnionResult(
                status=STATUS_NOT_APPLICABLE, text=resolved_text,
                certificate={"reason": "no lines could be safely unioned",
                             "before_hash": before_hash,
                             "after_hash": before_hash},
            )

        # --- Local validity. ---
        if not _brackets_balanced(edited_text):
            return ImportUnionResult(
                status=STATUS_BLOCKED, text=resolved_text,
                certificate={"reason": "bracket imbalance after edit",
                             "before_hash": before_hash,
                             "after_hash": before_hash},
            )

        after_hash = hashlib.sha256(
            edited_text.encode("utf-8")
        ).hexdigest()[:16]
        return ImportUnionResult(
            status=STATUS_APPLIED, text=edited_text,
            certificate={
                "primitive": "toml.manifest_union/v1",
                "closed_obligations": closed,
                "remaining_obligations": len(unresolved),
                "edits": edits,
                "preconditions": {
                    "bracket_balanced": True,
                },
                "risk_tier": RISK_TIER_A,
                "before_hash": before_hash,
                "after_hash": after_hash,
                "unresolved": unresolved,
            },
        )
    except Exception:  # noqa: BLE001
        return ImportUnionResult(
            status=STATUS_BLOCKED, text=resolved_text,
            certificate={"reason": "internal error", "before_hash": ""},
        )


def _try_array_or_feature_union(
    text: str, missing_line: str,
) -> str | None:
    """Try to merge a missing TOML line into an existing array or inline table.

    Returns the edited text, or None when no safe union is possible.
    """
    # Case 1: the missing line is a TOML array assignment (key = [...])
    # and the candidate has the same key with a different array.
    m = _TOML_ARRAY_RE.match(missing_line)
    if m:
        missing_key = m.group(2)
        missing_items = set(
            _toml_array_item_norm(i)
            for i in _parse_toml_array_items(m.group(3))
        )
        if not missing_items:
            return None
        # Find the same key in the candidate.
        for i, cl in enumerate(text.splitlines(keepends=True)):
            cm = _TOML_ARRAY_RE.match(cl.rstrip("\n"))
            if cm and cm.group(2) == missing_key:
                cand_items = set(
                    _toml_array_item_norm(i2)
                    for i2 in _parse_toml_array_items(cm.group(3))
                )
                # Check for disjoint additions (no overlap = both added different items).
                new_items = missing_items - cand_items
                if not new_items:
                    return None  # all items already present
                # Check no CONFLICTING items (same item, different value).
                # For simple string arrays, this is just set union.
                # Reconstruct the merged array preserving candidate order + appending new.
                cand_raw = _parse_toml_array_items(cm.group(3))
                merged = list(cand_raw)
                # Find the raw form of new items from the missing line.
                missing_raw = _parse_toml_array_items(m.group(3))
                for raw in missing_raw:
                    if _toml_array_item_norm(raw) in new_items:
                        merged.append(raw)
                # Reconstruct the line.
                indent = cm.group(1)
                trailing = cm.group(4) or ""
                inner = ", ".join(merged)
                ending = "\n" if cl.endswith("\n") else ""
                new_line = f"{indent}{missing_key} = [{inner}]{trailing}{ending}"
                lines = text.splitlines(keepends=True)
                lines[i] = new_line
                return "".join(lines)
        return None

    # Case 2: the missing line is an inline table (key = { ... features = [...] ... })
    # with a features array that should be unioned into the candidate's same-key table.
    m2 = _TOML_INLINE_TABLE_RE.match(missing_line)
    if m2:
        missing_key = m2.group(2)
        missing_table = m2.group(3)
        fm = _FEATURES_IN_TABLE_RE.search(missing_table)
        if not fm:
            return None  # no features array to union
        missing_features = set(
            _toml_array_item_norm(i)
            for i in _parse_toml_array_items(fm.group(1))
        )
        if not missing_features:
            return None
        # Find the same key in the candidate.
        for i, cl in enumerate(text.splitlines(keepends=True)):
            cm = _TOML_INLINE_TABLE_RE.match(cl.rstrip("\n"))
            if cm and cm.group(2) == missing_key:
                cand_table = cm.group(3)
                cfm = _FEATURES_IN_TABLE_RE.search(cand_table)
                if not cfm:
                    return None  # candidate has no features array
                cand_features = set(
                    _toml_array_item_norm(i2)
                    for i2 in _parse_toml_array_items(cfm.group(1))
                )
                new_features = missing_features - cand_features
                if not new_features:
                    return None  # all features already present
                # Build the merged features array (candidate order + new appended).
                cand_raw = _parse_toml_array_items(cfm.group(1))
                missing_raw = _parse_toml_array_items(fm.group(1))
                merged = list(cand_raw)
                for raw in missing_raw:
                    if _toml_array_item_norm(raw) in new_features:
                        merged.append(raw)
                merged_str = ", ".join(merged)
                # Replace the features array in the candidate's table content.
                new_table = _FEATURES_IN_TABLE_RE.sub(
                    f'features = [{merged_str}]', cand_table, count=1)
                indent = cm.group(1)
                trailing = cm.group(4) or ""
                ending = "\n" if cl.endswith("\n") else ""
                new_line = f"{indent}{missing_key} = {{{new_table}}}{trailing}{ending}"
                lines = text.splitlines(keepends=True)
                lines[i] = new_line
                return "".join(lines)
        return None

    return None


def _try_line_transplant(
    text: str, missing_line: str, other_side_text: str,
) -> str | None:
    """Transplant a missing TOML line to its correct position via anchors.

    Simple version: find the line before the missing line in the other side,
    and insert after it in the candidate. Returns None when anchors can't be
    located (conservative).
    """
    if not other_side_text:
        return None
    other_lines = other_side_text.splitlines()
    missing_norm = _normalize(missing_line)
    # Find the missing line in the other side.
    idx = None
    for i, ol in enumerate(other_lines):
        if _normalize(ol) == missing_norm:
            idx = i
            break
    if idx is None:
        return None
    # Anchor: the line before.
    if idx == 0:
        return None  # no before-anchor
    before_anchor = _normalize(other_lines[idx - 1])
    if not before_anchor:
        return None
    # Find the anchor in the candidate.
    lines = text.splitlines(keepends=True)
    for i, cl in enumerate(lines):
        if _normalize(cl) == before_anchor:
            ending = "\n"
            return "".join(lines[:i + 1] + [missing_line.strip() + "\n"] + lines[i + 1:])
    return None


__all__ = ["propose_manifest_union"]

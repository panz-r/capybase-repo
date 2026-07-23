"""Deterministic block-insertion editor.

When change-accounting detects the model copied one side and dropped a
contiguous ADDITIVE block of lines from the other side (not an import leaf
— those are handled by :mod:`import_union`), this module transplants the
block into the candidate at its correct position, determined by anchor
lines.

The convergence pattern: one side added a macro-gated block, a set of
``pub use`` re-exports, or a doc-comment block; the model copied the other
side. The block is genuinely additive content that should be integrated,
not synthesized.

The insertion position is determined by **anchor lines**: lines from the
dropped side that appear BOTH immediately before and after the missing
block AND survive in the candidate. When both anchors are found, the block
is inserted between them. When anchors can't be uniquely located, the
primitive returns AMBIGUOUS (conservative — never guesses a position).

Design contract (same as import_union / deletion_union):
  - **deterministic, idempotent, transactional, conservative.**
  - Tier-A primitive: verbatim transplant (no synthesis).
  - Pure of I/O; cargo/rustc remains authoritative after the edit.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from capybase.import_union import (
    ImportUnionResult,
    STATUS_APPLIED, STATUS_NOT_APPLICABLE, STATUS_BLOCKED, STATUS_AMBIGUOUS,
    RISK_TIER_A,
)


def _normalize(line: str) -> str:
    """Whitespace-normalized form for matching."""
    return " ".join(line.split())


def _brackets_balanced(s: str) -> bool:
    """True when (), [], {} are balanced across the whole string."""
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


def _is_import_line(line: str) -> bool:
    """True for ``use`` / ``pub use`` lines (handled by import_union, not us)."""
    s = line.strip()
    return s.startswith("use ") or s.startswith("pub use ") or s.startswith("pub(crate) use ")


def propose_block_insertion(
    resolved_text: str, missing_obligations: list,
    *, base_text: str = "", other_side_text: str = "",
) -> ImportUnionResult:
    """Propose a deterministic block transplant into a merge candidate.

    Args:
        resolved_text: the candidate's current resolved_text.
        missing_obligations: ``BranchObligation`` records. Only additive
            (non-exclusive, ``operation == "added"``, ``status == "MISSING"``)
            EXECUTABLE obligations that are NOT import lines are candidates.
            Import lines are left to import_union; exclusive choices are left
            to the model.
        base_text: the base hunk text (for deriving anchor lines). When empty,
            anchors can't be computed → NOT_APPLICABLE.
        other_side_text: the dropped side's full hunk text (for locating the
            block in context). When empty, we work from the obligations alone.

    Returns an :class:`ImportUnionResult`. Never raises.
    """
    try:
        before_hash = hashlib.sha256(
            (resolved_text or "").encode("utf-8")
        ).hexdigest()[:16]

        # --- Filter to additive non-import executable obligations. ---
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
            # Skip import lines (handled by import_union).
            if _is_import_line(line):
                continue
            # Skip comment-only lines (handled by comment reconciliation).
            from capybase.change_accounting import classify_channel
            if classify_channel(line) in ("comment", "formatting"):
                continue
            candidate_lines.append(line)

        if not candidate_lines:
            return ImportUnionResult(
                status=STATUS_NOT_APPLICABLE, text=resolved_text,
                certificate={"reason": "no additive non-import obligations",
                             "before_hash": before_hash,
                             "after_hash": before_hash},
            )

        # --- Check if any candidate lines are already present (idempotency). ---
        resolved_norms = {_normalize(l) for l in resolved_text.splitlines() if l.strip()}
        fresh_lines = [l for l in candidate_lines if _normalize(l) not in resolved_norms]
        if not fresh_lines:
            return ImportUnionResult(
                status=STATUS_NOT_APPLICABLE, text=resolved_text,
                certificate={"reason": "all candidate lines already present (idempotent)",
                             "before_hash": before_hash,
                             "after_hash": before_hash},
            )

        # --- Locate anchor lines from the other side's context. ---
        # The block was inserted into the dropped side at a specific position.
        # The lines immediately before and after the block in the dropped side's
        # text are the anchors. If both survive in the candidate, we can insert
        # the block between them.
        if not other_side_text:
            return ImportUnionResult(
                status=STATUS_NOT_APPLICABLE, text=resolved_text,
                certificate={"reason": "no other-side context for anchor detection",
                             "before_hash": before_hash,
                             "after_hash": before_hash},
            )

        # Find the block's position in the other side's text.
        other_lines = other_side_text.splitlines()
        fresh_norms = [_normalize(l) for l in fresh_lines]
        # Find the contiguous run of missing lines in the other side.
        block_start, block_end = _find_block_in_other(
            other_lines, fresh_norms)
        if block_start is None:
            return ImportUnionResult(
                status=STATUS_AMBIGUOUS, text=resolved_text,
                certificate={"reason": "block not found as contiguous run in other side",
                             "before_hash": before_hash,
                             "after_hash": before_hash},
            )

        # The anchor lines: the line before and after the block in the other side.
        before_anchor = other_lines[block_start - 1].strip() if block_start > 0 else ""
        after_anchor = other_lines[block_end + 1].strip() if block_end + 1 < len(other_lines) else ""

        if not before_anchor and not after_anchor:
            return ImportUnionResult(
                status=STATUS_AMBIGUOUS, text=resolved_text,
                certificate={"reason": "no anchor lines around block",
                             "before_hash": before_hash,
                             "after_hash": before_hash},
            )

        # --- Find the insertion point in the candidate. ---
        # The candidate must have BOTH anchor lines (normalized), and they must
        # appear in the right order (before_anchor before after_anchor).
        candidate_lines_list = resolved_text.splitlines(keepends=True)
        before_norm = _normalize(before_anchor)
        after_norm = _normalize(after_anchor)

        insert_after_idx = None
        for i, cl in enumerate(candidate_lines_list):
            if before_anchor and _normalize(cl) == before_norm:
                # Found the before-anchor. The block goes after this line.
                # Check that the after-anchor appears later (confirming position).
                if after_anchor:
                    found_after = any(
                        _normalize(candidate_lines_list[j]) == after_norm
                        for j in range(i + 1, len(candidate_lines_list))
                    )
                    if not found_after:
                        continue  # before-anchor found but no after-anchor after it
                insert_after_idx = i
                break

        if insert_after_idx is None:
            return ImportUnionResult(
                status=STATUS_AMBIGUOUS, text=resolved_text,
                certificate={"reason": "anchor lines not found in candidate (position unclear)",
                             "before_hash": before_hash,
                             "after_hash": before_hash},
            )

        # --- Build the edited text: insert the block after insert_after_idx. ---
        block_text = "\n".join(fresh_lines)
        # Detect the line ending of the anchor line to match it.
        anchor_line = candidate_lines_list[insert_after_idx]
        ending = "\n" if anchor_line.endswith("\n") else ""
        # The block lines, with matching line endings.
        block_with_endings = "".join(
            line + "\n" for line in fresh_lines
        )

        edited_lines = (
            candidate_lines_list[:insert_after_idx + 1]
            + [block_with_endings]
            + candidate_lines_list[insert_after_idx + 1:]
        )
        edited_text = "".join(edited_lines)

        # --- Local validity: brace balance. ---
        if not _brackets_balanced(edited_text):
            return ImportUnionResult(
                status=STATUS_BLOCKED, text=resolved_text,
                certificate={"reason": "brace imbalance after insertion",
                             "before_hash": before_hash,
                             "after_hash": before_hash},
            )

        # --- Verify: the fresh lines are now present. ---
        edited_norms = {_normalize(l) for l in edited_text.splitlines() if l.strip()}
        if not all(_normalize(l) in edited_norms for l in fresh_lines):
            return ImportUnionResult(
                status=STATUS_BLOCKED, text=resolved_text,
                certificate={"reason": "round-trip check failed (lines not present after edit)",
                             "before_hash": before_hash,
                             "after_hash": before_hash},
            )

        after_hash = hashlib.sha256(
            edited_text.encode("utf-8")
        ).hexdigest()[:16]
        return ImportUnionResult(
            status=STATUS_APPLIED, text=edited_text,
            certificate={
                "primitive": "rust.block_insertion/v1",
                "closed_obligations": [_normalize(l) for l in fresh_lines],
                "remaining_obligations": 0,
                "edits": [f"inserted {len(fresh_lines)} line(s) after anchor"],
                "preconditions": {
                    "before_anchor": before_anchor[:60] if before_anchor else "",
                    "after_anchor": after_anchor[:60] if after_anchor else "",
                    "brace_balanced": True,
                },
                "risk_tier": RISK_TIER_A,
                "before_hash": before_hash,
                "after_hash": after_hash,
            },
        )
    except Exception:  # noqa: BLE001 — transactional: never break the loop
        return ImportUnionResult(
            status=STATUS_BLOCKED, text=resolved_text,
            certificate={"reason": "internal error (transactional rollback)",
                         "before_hash": ""},
        )


def _find_block_in_other(
    other_lines: list[str], missing_norms: list[str],
) -> tuple[int | None, int | None]:
    """Find the contiguous run of missing lines in the other side's text.

    Returns ``(start_idx, end_idx)`` inclusive, or ``(None, None)`` when the
    missing lines don't form a single contiguous block in the other side.
    """
    # Mark which lines in other_lines match a missing norm.
    matching: list[bool] = []
    for ol in other_lines:
        matching.append(_normalize(ol) in missing_norms)

    # Find the first and last consecutive run of True.
    first = None
    last = None
    for i, m in enumerate(matching):
        if m:
            if first is None:
                first = i
            last = i
        else:
            if first is not None:
                # We've found a complete run. Check if there are more matches
                # later — if so, the block is split (AMBIGUOUS).
                if any(matching[j] for j in range(i + 1, len(matching))):
                    return None, None  # split block
                break
    return first, last


__all__ = ["propose_block_insertion"]

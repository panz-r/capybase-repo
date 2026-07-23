"""Deterministic deletion-application editor.

Symmetric to :mod:`import_union`: when change-accounting detects the model
copied a side that KEPT lines the other side intended to DELETE (a
``DROPPED_DELETION`` obligation), this module removes those lines
mechanically — no second model call.

The convergence pattern: one side deleted an import / unused variable /
dead code block; the model copied the other side (which kept it). The
preservation heuristic flags the dropped deletion; the deletion editor
applies it.

Design contract (same as import_union):
  - **deterministic, idempotent, transactional, conservative.**
  - Tier-A primitive: lossless mechanical operation (line removal).
  - Pure of I/O; the existing cargo/rustc gauntlet remains authoritative
    after the edit.
  - Result vocabulary: ``APPLIED`` / ``NOT_APPLICABLE`` / ``BLOCKED`` /
    ``AMBIGUOUS`` (reuses import_union's constants for consistency).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from capybase.import_union import (
    ImportUnionResult,
    STATUS_APPLIED, STATUS_NOT_APPLICABLE, STATUS_BLOCKED, STATUS_AMBIGUOUS,
    RISK_TIER_A,
)


# ---------------------------------------------------------------------------
# Local validity (reuse import_union's brace-balance check)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# The deletion proposer
# ---------------------------------------------------------------------------


def propose_deletion_application(
    resolved_text: str, missing_obligations: list,
) -> ImportUnionResult:
    """Propose a deterministic deletion of dropped-deletion lines.

    Args:
        resolved_text: the candidate's current resolved_text.
        missing_obligations: ``BranchObligation`` records. Only those with
            ``status == "DROPPED_DELETION"`` are acted on; everything else
            is ignored (additions are handled by import_union / the model).

    Returns an :class:`ImportUnionResult`. Never raises — internal errors
    map to BLOCKED (transactional rollback).
    """
    try:
        before_hash = hashlib.sha256(
            (resolved_text or "").encode("utf-8")
        ).hexdigest()[:16]

        # --- Filter to DROPPED_DELETION obligations only. ---
        to_delete: list[str] = []
        for ob in missing_obligations or []:
            if getattr(ob, "status", "") != "DROPPED_DELETION":
                continue
            line = getattr(ob, "line", "") or ""
            if not line.strip():
                continue
            to_delete.append(line.strip())

        if not to_delete:
            return ImportUnionResult(
                status=STATUS_NOT_APPLICABLE, text=resolved_text,
                certificate={"reason": "no dropped-deletion obligations",
                             "before_hash": before_hash,
                             "after_hash": before_hash},
            )

        # --- Match each deletion line to candidate lines (exact + normalized). ---
        # A deletion line must match a candidate line EXACTLY (whitespace-
        # normalized) before we remove it. We never remove a line that
        # doesn't match — that would be guessing.
        candidate_lines = (resolved_text or "").splitlines(keepends=True)
        delete_norms = {_normalize(line) for line in to_delete}

        # Track which candidate lines to remove (by index).
        remove_indices: set[int] = set()
        for i, cl in enumerate(candidate_lines):
            if _normalize(cl) in delete_norms:
                remove_indices.add(i)

        if not remove_indices:
            # None of the deletion lines are present in the candidate.
            return ImportUnionResult(
                status=STATUS_NOT_APPLICABLE, text=resolved_text,
                certificate={"reason": "deletion lines not found in candidate",
                             "before_hash": before_hash,
                             "after_hash": before_hash},
            )

        # --- Safety gate: don't remove a line if it would orphan a delimiter. ---
        # We check brace balance AFTER removal. If removing the lines would
        # unbalance braces, refuse (BLOCKED) — the deletion might be removing
        # a line that's structurally load-bearing in this context.
        surviving = [
            cl for i, cl in enumerate(candidate_lines) if i not in remove_indices
        ]
        edited_text = "".join(surviving)

        if not _brackets_balanced(edited_text):
            return ImportUnionResult(
                status=STATUS_BLOCKED, text=resolved_text,
                certificate={"reason": "brace imbalance after deletion",
                             "before_hash": before_hash,
                             "after_hash": before_hash},
            )

        # --- Idempotency: if the edit didn't change anything, NOT_APPLICABLE. ---
        after_hash = hashlib.sha256(
            edited_text.encode("utf-8")
        ).hexdigest()[:16]
        if before_hash == after_hash:
            return ImportUnionResult(
                status=STATUS_NOT_APPLICABLE, text=resolved_text,
                certificate={"reason": "no change (idempotent)",
                             "before_hash": before_hash,
                             "after_hash": after_hash},
            )

        closed = [_normalize(line) for line in to_delete
                  if any(_normalize(cl) == _normalize(line)
                         for i, cl in enumerate(candidate_lines)
                         if i in remove_indices)]
        return ImportUnionResult(
            status=STATUS_APPLIED, text=edited_text,
            certificate={
                "primitive": "rust.deletion_application/v1",
                "closed_obligations": closed,
                "remaining_obligations": 0,
                "edits": [f"removed {len(remove_indices)} line(s)"],
                "preconditions": {
                    "exact_match": True,
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


def _normalize(line: str) -> str:
    """Whitespace-normalized form for matching (re-indented lines match)."""
    return " ".join(line.split())


__all__ = ["propose_deletion_application"]

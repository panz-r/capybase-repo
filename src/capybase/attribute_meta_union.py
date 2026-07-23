"""Deterministic attribute/meta-list union editor.

Merges set-like Rust attribute lists through an explicit policy registry:
``#[derive(Debug, Clone)]`` + ``#[derive(Debug, Serialize)]`` →
``#[derive(Debug, Clone, Serialize)]``. Also handles ``#[allow(...)]`` and
``#[warn(...)]`` lint lists (same lint level only).

Policy-driven, not generic: each attribute has its own merge semantics. Some
attributes are opaque (``#[cfg(...)]``, ``#[repr(...)]``, ``#[serde(...)]``)
and are NEVER unioned. ``#[deny(...)]`` and ``#[forbid(...)]`` are NEVER
unioned (they cannot be overridden downstream).

Design contract (same as all Tier-A primitives):
  - **deterministic, idempotent, transactional, conservative.**
  - Tier-A for built-in derives; Tier-B risk_flags for external derives.
  - Pure of I/O; cargo/rustc remains authoritative after the edit.
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
from capybase.change_accounting import _derive_trait_set, _is_derive_attr


#: Built-in derives that are always safe to union (ordinary marker/ordering traits).
_SAFE_BUILTIN_DERIVES = frozenset({
    "Clone", "Copy", "Debug", "Default", "Eq", "PartialEq",
    "Ord", "PartialOrd", "Hash",
})

#: Matches #[allow(...)] / #[warn(...)] / #[deny(...)] / #[forbid(...)]
_LINT_ATTR_RE = re.compile(
    r"^(\s*)#(?:\!?)\[(allow|warn|deny|forbid)\s*\(([^)]*)\)\]\s*(#.*)?$"
)

#: Matches #[derive(...)] (outer or inner).
#: Reuses the pattern from change_accounting._DERIVE_RE.
_DERIVE_OUTER_RE = re.compile(
    r"^(\s*)#!?\[derive\s*\(([^)]*)\)\]\s*(#.*)?$"
)


def _normalize(line: str) -> str:
    return " ".join(line.split())


def _split_meta_items(content: str) -> list[str]:
    """Split a meta-item list ``A, B, C`` into individual items."""
    return [i.strip() for i in content.split(",") if i.strip()]


def propose_attribute_meta_union(
    resolved_text: str, missing_obligations: list,
) -> ImportUnionResult:
    """Propose a deterministic attribute/meta-list union edit.

    Acts on additive directive-channel obligations (``#[derive(...)]`` and
    ``#[allow(...)]`` / ``#[warn(...)]`` lines). Version bumps, cfg, repr,
    serde, deny, forbid are never unioned.

    Returns an :class:`ImportUnionResult`. Never raises.
    """
    try:
        before_hash = hashlib.sha256(
            (resolved_text or "").encode("utf-8")
        ).hexdigest()[:16]

        # --- Filter to additive directive obligations. ---
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
            if classify_channel(line) != "directive":
                continue
            candidate_lines.append(line)

        if not candidate_lines:
            return ImportUnionResult(
                status=STATUS_NOT_APPLICABLE, text=resolved_text,
                certificate={"reason": "no additive directive obligations",
                             "before_hash": before_hash,
                             "after_hash": before_hash},
            )

        # --- Idempotency: drop lines already present. ---
        resolved_norms = {_normalize(l) for l in resolved_text.splitlines() if l.strip()}
        fresh = [l for l in candidate_lines if _normalize(l) not in resolved_norms]
        if not fresh:
            return ImportUnionResult(
                status=STATUS_NOT_APPLICABLE, text=resolved_text,
                certificate={"reason": "all directive lines already present",
                             "before_hash": before_hash,
                             "after_hash": before_hash},
            )

        edited_lines = resolved_text.splitlines(keepends=True)
        closed: list[str] = []
        edits: list[str] = []
        risk_flags: list[str] = []
        unresolved: list[str] = []

        for fresh_line in fresh:
            result = _try_union_one_attribute(
                edited_lines, fresh_line, risk_flags)
            if result is not None:
                edited_lines = result
                closed.append(_normalize(fresh_line))
                edits.append(f"union: {fresh_line.strip()[:60]}")
            else:
                unresolved.append(fresh_line.strip()[:60])

        if not closed:
            return ImportUnionResult(
                status=STATUS_NOT_APPLICABLE, text=resolved_text,
                certificate={"reason": "no attributes could be safely unioned",
                             "before_hash": before_hash,
                             "after_hash": before_hash},
            )

        edited_text = "".join(edited_lines)
        after_hash = hashlib.sha256(
            edited_text.encode("utf-8")
        ).hexdigest()[:16]
        cert = {
            "primitive": "rust.attribute_meta_union/v1",
            "closed_obligations": closed,
            "remaining_obligations": len(unresolved),
            "edits": edits,
            "preconditions": {"same_attribute": True},
            "risk_tier": RISK_TIER_A,
            "before_hash": before_hash,
            "after_hash": after_hash,
            "unresolved": unresolved,
        }
        if risk_flags:
            cert["risk_flags"] = risk_flags
        return ImportUnionResult(
            status=STATUS_APPLIED, text=edited_text,
            certificate=cert,
        )
    except Exception:  # noqa: BLE001
        return ImportUnionResult(
            status=STATUS_BLOCKED, text=resolved_text,
            certificate={"reason": "internal error", "before_hash": ""},
        )


def _try_union_one_attribute(
    lines: list[str], missing_line: str, risk_flags: list[str],
) -> list[str] | None:
    """Try to union one missing attribute line into the existing lines.

    Returns the edited lines list, or None when no safe union is possible.
    Appends to ``risk_flags`` in-place when external derives are involved.
    """
    # Case 1: #[derive(...)] union.
    if _is_derive_attr(missing_line.strip()):
        missing_traits = _derive_trait_set(missing_line.strip())
        if not missing_traits:
            return None
        for i, cl in enumerate(lines):
            if not _is_derive_attr(cl.rstrip("\n")):
                continue
            cand_traits = _derive_trait_set(cl.rstrip("\n"))
            if not cand_traits:
                continue
            new_traits = missing_traits - cand_traits
            if not new_traits:
                return None  # all traits already present
            # Build merged list: candidate order + new appended.
            merged = [t for t in _split_meta_items(
                _DERIVE_OUTER_RE.match(cl.rstrip("\n")).group(2)
            )]
            # Check for external derives → Tier-B risk flag.
            external = [t for t in new_traits if t not in _SAFE_BUILTIN_DERIVES]
            if external:
                risk_flags.append(
                    f"external_derive_union:{','.join(external)}")
            # Append new traits.
            missing_raw = _split_meta_items(
                _DERIVE_OUTER_RE.match(missing_line.strip()).group(2)
            )
            for raw in missing_raw:
                if raw.strip() in new_traits:
                    merged.append(raw.strip())
            # Reconstruct the line.
            m = _DERIVE_OUTER_RE.match(cl.rstrip("\n"))
            indent = m.group(1)
            trailing = m.group(3) or ""
            inner = ", ".join(merged)
            ending = "\n" if cl.endswith("\n") else ""
            lines[i] = f"{indent}#[derive({inner})]{trailing}{ending}"
            return lines
        return None

    # Case 2: #[allow(...)] / #[warn(...)] lint union (same level only).
    lm = _LINT_ATTR_RE.match(missing_line.strip())
    if lm and lm.group(2) in ("allow", "warn"):
        missing_level = lm.group(2)
        missing_items = set(_split_meta_items(lm.group(3)))
        if not missing_items:
            return None
        for i, cl in enumerate(lines):
            cm = _LINT_ATTR_RE.match(cl.rstrip("\n"))
            if not cm or cm.group(2) != missing_level:
                continue
            cand_items = set(_split_meta_items(cm.group(3)))
            new_items = missing_items - cand_items
            if not new_items:
                return None  # all items already present
            merged = [t for t in _split_meta_items(cm.group(3))]
            missing_raw = _split_meta_items(lm.group(3))
            for raw in missing_raw:
                if raw.strip() in new_items:
                    merged.append(raw.strip())
            indent = cm.group(1)
            trailing = cm.group(4) or ""
            inner = ", ".join(merged)
            ending = "\n" if cl.endswith("\n") else ""
            lines[i] = f"{indent}#[{missing_level}({inner})]{trailing}{ending}"
            return lines
        return None

    # deny, forbid, cfg, repr, serde, etc. → never unioned.
    return None


__all__ = ["propose_attribute_meta_union"]

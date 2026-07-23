"""Deterministic named-field union editor.

Inserts struct fields that the model dropped by copying one side. When
CURRENT adds ``timeout: Duration`` and REPLAYED adds ``_marker: PhantomData``
to the same struct, the union preserves both additions.

Design contract (same as all Tier-A primitives):
  - **deterministic, idempotent, transactional, conservative.**
  - Uses :func:`brace_utils.find_container_close_line` to locate insertion point.
  - Tuple structs → AMBIGUOUS (fields are positional, not named).
  - Same field name → exclusive (left to the model).
  - ``repr(C)``/``repr(packed)``/serialization attrs → Tier-B risk_flags.
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
from capybase.brace_utils import find_container_close_line


def _normalize(line: str) -> str:
    return " ".join(line.split())


#: Matches a struct field declaration: [pub] name: Type
#: Captures the field name and the full field text.
_FIELD_RE = re.compile(
    r"^(\s*)(?:pub(?:\s*\([^)]*\))?\s+)?([A-Za-z_]\w*)\s*:\s*(.+?)\s*,?\s*$"
)

#: Matches a struct header: [pub] struct Name[<generics>] {
_STRUCT_HEADER_RE = re.compile(
    r"^(\s*)(?:pub(?:\s*\([^)]*\))?\s+)?struct\s+([A-Za-z_]\w*)"
)

#: Detects repr/serialization attributes (order-sensitive → Tier-B)
_ORDER_SENSITIVE_ATTR_RE = re.compile(
    r"#\s*\[repr\s*\(|#\s*\[serde\s*\("
)


def _field_name(line: str) -> str | None:
    """Extract the field name from a struct field line.

    ``    timeout: Duration,`` → ``timeout``
    ``    pub stream: S,`` → ``stream``
    Returns None for non-field lines (comments, blank, braces, attributes).
    """
    m = _FIELD_RE.match(line)
    return m.group(2) if m else None


def _is_tuple_struct(header: str) -> bool:
    """True for ``struct Foo(`` — tuple structs have positional fields."""
    return "struct " in header and "(" in header.split("struct")[1].split("{")[0] \
        if "struct" in header else False


def propose_named_field_union(
    resolved_text: str, missing_obligations: list,
    *, other_side_text: str = "",
) -> ImportUnionResult:
    """Propose a deterministic struct-field union.

    For each additive obligation that looks like a struct field (``name: Type``),
    find the destination struct in the candidate by matching the struct name.
    If the field name doesn't collide, transplant the field before the struct's
    closing brace.

    Returns an :class:`ImportUnionResult`. Never raises.
    """
    try:
        before_hash = hashlib.sha256(
            (resolved_text or "").encode("utf-8")
        ).hexdigest()[:16]

        # --- Filter to additive obligations that look like struct fields. ---
        candidate_fields: list[str] = []
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
            if classify_channel(line) in ("comment", "formatting", "directive"):
                continue
            # Must look like a struct field: name: Type
            if _field_name(line) is None:
                continue
            candidate_fields.append(line)

        if not candidate_fields:
            return ImportUnionResult(
                status=STATUS_NOT_APPLICABLE, text=resolved_text,
                certificate={"reason": "no additive field obligations",
                             "before_hash": before_hash,
                             "after_hash": before_hash},
            )

        # --- Idempotency: drop fields whose name already exists. ---
        existing_field_names = set()
        for cl in resolved_text.splitlines():
            name = _field_name(cl)
            if name:
                existing_field_names.add(name)
        fresh = [
            line for line in candidate_fields
            if _field_name(line) not in existing_field_names
        ]
        if not fresh:
            return ImportUnionResult(
                status=STATUS_NOT_APPLICABLE, text=resolved_text,
                certificate={"reason": "all fields already present",
                             "before_hash": before_hash,
                             "after_hash": before_hash},
            )

        edited_text = resolved_text
        closed: list[str] = []
        edits: list[str] = []
        risk_flags: list[str] = []
        unresolved: list[str] = []

        for field_line in fresh:
            result = _try_insert_field(
                edited_text, field_line, other_side_text, risk_flags)
            if result is not None:
                edited_text = result
                closed.append(_normalize(field_line))
                edits.append(f"insert field {_field_name(field_line)}")
            else:
                unresolved.append(field_line.strip()[:60])

        if not closed:
            return ImportUnionResult(
                status=STATUS_NOT_APPLICABLE, text=resolved_text,
                certificate={"reason": "no fields could be inserted",
                             "before_hash": before_hash,
                             "after_hash": before_hash},
            )

        after_hash = hashlib.sha256(
            edited_text.encode("utf-8")
        ).hexdigest()[:16]
        cert = {
            "primitive": "rust.named_field_union/v1",
            "closed_obligations": closed,
            "remaining_obligations": len(unresolved),
            "edits": edits,
            "preconditions": {"no_name_collision": True},
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


def _try_insert_field(
    text: str, field_line: str, other_side_text: str,
    risk_flags: list[str],
) -> str | None:
    """Try to insert one struct field into the candidate.

    Returns the edited text, or None when no safe insertion is possible.
    """
    field_name = _field_name(field_line)
    if field_name is None:
        return None

    # Find the destination struct by looking at the other side.
    if not other_side_text:
        return None
    other_lines = other_side_text.splitlines()
    field_line_idx = None
    for i, ol in enumerate(other_lines):
        if _field_name(ol) == field_name:
            field_line_idx = i
            break
    if field_line_idx is None:
        return None

    # Walk backwards to find the struct header.
    struct_name = None
    struct_header_line = None
    for j in range(field_line_idx - 1, -1, -1):
        stripped = other_lines[j].strip()
        m = _STRUCT_HEADER_RE.match(stripped)
        if m:
            struct_name = m.group(2)
            struct_header_line = stripped
            break
    if struct_name is None:
        return None

    # Refuse tuple structs (fields are positional, not named).
    if _is_tuple_struct(struct_header_line):
        return None

    # Find the same struct header in the candidate.
    # Use splitlines() (no keepends) for the brace scan, then re-split with
    # keepends for insertion. find_container_close_line joins lines with \n,
    # so keepends lines (which already have \n) would produce double-newline
    # offsets.
    cand_lines_plain = text.splitlines()
    header_idx = None
    for i, cl in enumerate(cand_lines_plain):
        if _STRUCT_HEADER_RE.match(cl.strip()) and struct_name in cl:
            header_idx = i
            break
    if header_idx is None:
        return None

    # Check for repr/serialization attrs → Tier-B risk flag.
    for k in range(max(0, header_idx - 5), header_idx):
        if _ORDER_SENSITIVE_ATTR_RE.search(cand_lines_plain[k]):
            risk_flags.append("order_sensitive_attribute")
            break

    # Find the struct's closing brace.
    close_line = find_container_close_line(cand_lines_plain, header_idx, language="rust")
    if close_line is None:
        return None

    # Detect indentation from existing fields.
    indent = ""
    for k in range(header_idx + 1, close_line):
        fn = _field_name(cand_lines_plain[k])
        if fn:
            stripped = cand_lines_plain[k].lstrip()
            indent = cand_lines_plain[k][:len(cand_lines_plain[k]) - len(stripped)]
            break

    # Extract the full field text from the obligation line (with trailing comma).
    field_text = field_line.rstrip()
    if not field_text.endswith(","):
        field_text += ","
    if indent:
        field_text = indent + field_text.strip()

    # Insert before the closing brace (use keepends for the actual splice).
    cand_lines = text.splitlines(keepends=True)
    cand_lines.insert(close_line, field_text + "\n")
    return "".join(cand_lines)


__all__ = ["propose_named_field_union"]

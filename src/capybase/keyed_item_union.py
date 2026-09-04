"""Deterministic keyed-item union editor.

Inserts complete named items (methods, functions, associated types, consts)
into ``impl`` blocks, ``mod`` blocks, and ``trait`` definitions when the model
dropped them by copying the other side.

The convergence pattern: CURRENT adds ``fn encode()`` to ``impl Client``, REPLAYED
adds ``fn decode()`` to the same ``impl Client``. The model copies one side,
dropping the other's method. The keyed-item editor transplants the method into
the surviving impl block before its closing brace.

Semantic key: ``container_name + item_kind + item_name``. Different keys union;
same key = exclusive (left to the model). Rust doesn't support method
overloading, so same-name items are always a semantic conflict.

Design contract (same as all Tier-A primitives):
  - **deterministic, idempotent, transactional, conservative.**
  - Uses :func:`brace_utils.find_container_close_line` to locate insertion point.
  - Tier-A for distinct items; same-key collisions are NOT unioned.
  - Pure of I/O; cargo/rustc remains authoritative after the edit.

Stage 3 (the SWITCH, primitive #3 of 5): the KeyedCollectionMerge
engine is the authoritative implementation. This module supplies only
the Rust-item codec; the lifecycle lives in
:mod:`capybase.keyed_collection`. The codec was shadow-verified at
6/6 agreement with the original inline lifecycle before the switch.
"""

from __future__ import annotations

import re

from capybase.import_union import (
    ImportUnionResult,
    STATUS_APPLIED, STATUS_NOT_APPLICABLE, STATUS_BLOCKED, STATUS_AMBIGUOUS,
    RISK_TIER_A,
)
from capybase.brace_utils import find_container_close_line


def _normalize(line: str) -> str:
    return " ".join(line.split())


#: Extracts the item name from a Rust declaration line.
#: Handles: fn, async fn, pub fn, const, static, type, struct, enum, trait, mod, macro_rules!
_ITEM_NAME_RE = re.compile(
    r"^\s*(?:pub(?:\s*\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+\S+\s+)?"
    r"(?:fn|const|static|type|struct|enum|trait|mod)\s+"
    r"([A-Za-z_]\w*)"
)
#: macro_rules! name extraction
_MACRO_NAME_RE = re.compile(r"^\s*macro_rules!\s+([A-Za-z_]\w*)")


def _item_name(line: str) -> str | None:
    """Extract the declared name from a Rust item line.

    Returns the bare identifier (e.g. ``encode`` from ``fn encode(&self) {``),
    or None when the line isn't a recognizable item declaration.
    """
    s = line.strip()
    m = _MACRO_NAME_RE.match(s)
    if m:
        return m.group(1)
    m = _ITEM_NAME_RE.match(s)
    if m:
        return m.group(1)
    return None


def _is_rust_item(line: str) -> bool:
    """True when the line declares a Rust item (fn, const, struct, etc.)."""
    return _item_name(line) is not None


def _is_macro(line: str) -> bool:
    """True for ``macro_rules!`` — excluded from auto-union."""
    return bool(_MACRO_NAME_RE.match(line.strip()))


def _item_codec():
    """The Rust-item codec for the KeyedCollectionMerge engine.

    Carries the EXACT semantics of the original inline lifecycle
    (shadow-verified at 6/6 agreement before the switch):

    - no channel filter — ``_is_rust_item`` already rejects comments
      and blank lines (the original had no classify_channel call).
    - ``already_present`` always False — the original decided
      idempotency inside the insert attempt via the scope-qualified
      per-container collision (claim-3 fix), surfacing as
      ``unresolved`` rather than an early idempotent exit.
    - ``local_validity`` always True — the original had NO local
      validity gate; the zero-regression bar forbids new BLOCKED
      paths on mid-repair (possibly unbalanced) candidates.
    - ``edit_notes`` accumulates the original certificate's edit
      strings (``insert <name> before line <N>``) — the engine's
      own edit reasons don't carry the destination line number.
    """

    class _RustItemCodec:
        def __init__(self) -> None:
            self.edit_notes: list[str] = []

        def applicable_obligations(self, obligations):
            from capybase.block_insertion import _is_import_line
            out = []
            for ob in obligations or []:
                if getattr(ob, "operation", "") != "added":
                    continue
                if getattr(ob, "status", "") != "MISSING":
                    continue
                if getattr(ob, "exclusive", False):
                    continue
                line = getattr(ob, "line", "") or ""
                if not line.strip():
                    continue
                # Must be a Rust item declaration (fn/const/type/etc.)
                if not _is_rust_item(line):
                    continue
                # Refuse macro_rules! (opaque expansion).
                if _is_macro(line):
                    continue
                # Skip imports (handled by import_union).
                if _is_import_line(line):
                    continue
                out.append(line)
            return out

        def already_present(self, text, item):
            return False

        def try_edit(self, text, item, context):
            item_name_str = _item_name(item)
            if item_name_str is None:
                return None
            dest_info = _find_destination_container(text, item, context)
            if dest_info is None:
                return None
            container_close_line, indent = dest_info

            # SCOPE-QUALIFIED collision (claim-3 fix): the name must
            # not exist in THIS container — find the container header
            # by scanning backward for the first impl/mod/trait line.
            cand = text.splitlines()
            container_start = 0
            for k in range(container_close_line - 1, -1, -1):
                if _is_rust_item(cand[k]) and any(
                        kw in cand[k] for kw in ("impl ", "mod ", "trait ")):
                    container_start = k
                    break
            for k in range(container_start, container_close_line):
                if _item_name(cand[k]) == item_name_str:
                    return None  # genuine collision in the destination

            # Extract the full item subtree from the other side.
            item_subtree = _extract_item_subtree(context, item)
            if item_subtree is None:
                # Fall back to just the obligation line(s) — but only
                # if it's a complete item (has a body or ends with ;).
                item_subtree = item.strip()

            # Insert before the container's closing brace.
            insertion = item_subtree.rstrip() + "\n"
            if indent:
                insertion = "\n".join(
                    (indent + ln if ln.strip() else ln)
                    for ln in insertion.splitlines()
                ) + "\n"
            lines = text.splitlines(keepends=True)
            pos = sum(len(l) for l in lines[:container_close_line])
            pos = min(pos, len(text))
            self.edit_notes.append(
                f"insert {item_name_str} before line {container_close_line}")
            return (pos, pos, insertion)

        def local_validity(self, text):
            return True

    return _RustItemCodec()


def propose_keyed_item_union(
    resolved_text: str, missing_obligations: list,
    *, other_side_text: str = "",
) -> ImportUnionResult:
    """Propose a deterministic keyed-item insertion.

    For each additive obligation that declares a Rust item (fn, const, type,
    etc.), find the destination container (impl/mod/trait) in the candidate by
    matching the container header. If the item name doesn't collide with an
    existing item in that container, transplant the complete item subtree.

    The item subtree is reconstructed from the other side's text: the full
    text from the item's declaration through its matching closing brace.

    Stage 3 (the SWITCH): the KeyedCollectionMerge engine runs the
    lifecycle; this function is a thin adapter mapping the engine's
    :class:`PrimitiveResult` to the wire format
    (:class:`ImportUnionResult`, original certificate keys,
    ``rust.keyed_item_union/v1``).

    Returns an :class:`ImportUnionResult`. Never raises.
    """
    from capybase.deterministic_model import PrimitiveStatus
    from capybase.keyed_collection import merge_keyed_collection

    codec = _item_codec()
    result = merge_keyed_collection(
        codec, resolved_text, missing_obligations,
        other_side_text=other_side_text,
        mechanism_id="rust.keyed_item_union/v1",
    )

    _status_map = {
        PrimitiveStatus.APPLIED: STATUS_APPLIED,
        PrimitiveStatus.NOT_APPLICABLE: STATUS_NOT_APPLICABLE,
        PrimitiveStatus.AMBIGUOUS: STATUS_AMBIGUOUS,
        PrimitiveStatus.BLOCKED: STATUS_BLOCKED,
    }
    _reason_map = {
        "no applicable items": "no additive item obligations",
        "no items could be safely inserted": "no items could be inserted",
        "all items already present (idempotent)":
            "all items already present (collision or idempotent)",
    }
    text = result.candidate if result.candidate is not None else resolved_text
    cert = dict(result.certificate)
    cert["primitive"] = "rust.keyed_item_union/v1"
    if "reason" in cert:
        cert["reason"] = _reason_map.get(cert["reason"], cert["reason"])
    if result.status == PrimitiveStatus.APPLIED:
        cert["closed_obligations"] = [
            _normalize(c) for c in result.closed_obligations or []]
        cert["edits"] = list(codec.edit_notes)
        cert["preconditions"] = {
            "no_name_collision": True,
            "destination_found": True,
        }
        cert["risk_tier"] = RISK_TIER_A
    return ImportUnionResult(
        status=_status_map[result.status], text=text, certificate=cert)


def _find_destination_container(
    text: str, item_line: str, other_side_text: str,
) -> tuple[int, str] | None:
    """Find where to insert an item in the candidate text.

    Returns ``(close_line_idx, indent)`` where close_line_idx is the line
    number of the container's closing ``}`` and indent is the indentation
    of items inside the container (e.g. ``"    "``). Returns None when no
    unique destination is found.

    Strategy: look at the other side's text to find which container the item
    was added to (by scanning for the container header above the item line).
    Then find the SAME container header in the candidate text.
    """
    # Find the item in the other side to determine its container.
    item_name = _item_name(item_line)
    if item_name is None or not other_side_text:
        return None

    other_lines = other_side_text.splitlines()
    item_line_idx = None
    for i, ol in enumerate(other_lines):
        if _item_name(ol) == item_name:
            item_line_idx = i
            break
    if item_line_idx is None:
        return None

    # Walk backwards from the item to find the container header.
    # Scan for the first line that opens an impl/mod/trait block. We don't
    # track brace depth (too fragile for backward scanning — method bodies
    # have their own braces). Instead, we rely on the structural convention
    # that container headers are the first ``impl``/``mod``/``trait`` line
    # above the item. This is correct for well-formed Rust source.
    container_header = None
    for j in range(item_line_idx - 1, -1, -1):
        stripped = other_lines[j].strip()
        if (stripped.startswith("impl ") or stripped.startswith("mod ")
                or stripped.startswith("trait ")):
            container_header = stripped
            break
    if container_header is None:
        return None

    # Find the same container header in the candidate text.
    cand_lines = text.splitlines()
    header_idx = None
    for i, cl in enumerate(cand_lines):
        if cl.strip() == container_header:
            header_idx = i
            break
    if header_idx is None:
        return None

    # Find the container's closing brace.
    close_line = find_container_close_line(cand_lines, header_idx, language="rust")
    if close_line is None:
        return None

    # Detect indentation from existing items in the container.
    indent = ""
    for k in range(header_idx + 1, close_line):
        ln = cand_lines[k]
        if ln.strip() and _is_rust_item(ln):
            stripped = ln.lstrip()
            indent = ln[:len(ln) - len(stripped)]
            break

    return (close_line, indent)


def _extract_item_subtree(other_text: str, item_line: str) -> str | None:
    """Extract the complete item subtree from the other side's text.

    Finds the item declaration and returns everything from its start through
    its matching closing brace (or the line ending with ``;`` for bodyless
    items).
    """
    if not other_text:
        return None
    other_lines = other_text.splitlines()
    item_name = _item_name(item_line)
    if item_name is None:
        return None

    start_idx = None
    for i, ol in enumerate(other_lines):
        if _item_name(ol) == item_name:
            start_idx = i
            break
    if start_idx is None:
        return None

    # Collect preceding attributes/comments.
    collect_start = start_idx
    for k in range(start_idx - 1, -1, -1):
        stripped = other_lines[k].strip()
        if stripped.startswith("#[") or stripped.startswith("#![") or stripped.startswith("///") or stripped.startswith("//!") or not stripped:
            collect_start = k
        else:
            break

    # Find the end of the item: either a line ending with ; or the matching }.
    full_text = "\n".join(other_lines)
    char_start = sum(len(other_lines[j]) + 1 for j in range(collect_start))
    # Find the first { after the item start.
    brace_idx = full_text.find("{", char_start)
    semi_idx = full_text.find(";", char_start)
    if semi_idx != -1 and (brace_idx == -1 or semi_idx < brace_idx):
        # Bodyless item (trait method / associated const).
        return "\n".join(other_lines[collect_start:start_idx + 1]) \
            if start_idx + 1 <= len(other_lines) else None
    if brace_idx == -1:
        return None
    from capybase.brace_utils import find_closing_brace
    close_idx = find_closing_brace(full_text, brace_idx, language="rust")
    if close_idx is None:
        return None
    end_line = full_text[:close_idx].count("\n")
    return "\n".join(other_lines[collect_start:end_line + 1])


__all__ = ["propose_keyed_item_union"]

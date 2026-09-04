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

Stage 3 (the SWITCH, primitive #2 of 5): the KeyedCollectionMerge
engine is the authoritative implementation. This module supplies only
the struct-field codec (the language/construct half); the lifecycle
(filter → idempotency → sequential transactional edits → local
validity → certificate) lives in :mod:`capybase.keyed_collection`.
The codec was shadow-verified at 6/6 agreement with the original
inline lifecycle before the switch.
"""

from __future__ import annotations

import re

from capybase.import_union import ImportUnionResult, RISK_TIER_A
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


def _field_codec():
    """The struct-field codec for the KeyedCollectionMerge engine.

    Carries the EXACT semantics of the original inline lifecycle
    (shadow-verified at 6/6 agreement before the switch):

    - ``already_present`` always False — the original code decided
      idempotency inside the insert attempt: the scope-qualified
      collision check (per-destination-struct, claim-3 fix) returns
      "no safe insertion" for an existing field, which surfaces as
      ``unresolved`` rather than an early idempotent exit. The
      engine's pre-check must not front-run that decision.
    - ``local_validity`` always True — the original had NO local
      validity gate; candidate text mid-repair may be legitimately
      unbalanced, and the zero-regression bar for the switch forbids
      new BLOCKED paths.
    - ``risk_flags`` accumulates on the codec instance (one
      ``order_sensitive_attribute`` per insert attempt into a struct
      carrying repr/serde attributes — duplicates preserved, matching
      the original's per-field append).
    """

    class _StructFieldCodec:
        def __init__(self) -> None:
            self.risk_flags: list[str] = []

        def applicable_obligations(self, obligations):
            from capybase.change_accounting import classify_channel
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
                if classify_channel(line) in (
                        "comment", "formatting", "directive"):
                    continue
                # Must look like a struct field: name: Type
                if _field_name(line) is None:
                    continue
                out.append(line)
            return out

        def already_present(self, text, item):
            return False

        def try_edit(self, text, item, context):
            field_name = _field_name(item)
            if field_name is None:
                return None
            if not context:
                return None
            other_lines = context.splitlines()
            field_line_idx = None
            for i, ol in enumerate(other_lines):
                if _field_name(ol) == field_name:
                    field_line_idx = i
                    break
            if field_line_idx is None:
                return None
            # Walk backwards to the struct header in the other side.
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
            # Tuple structs: fields are positional, not named.
            if _is_tuple_struct(struct_header_line):
                return None
            # Find the same struct header in the candidate.
            cand_lines_plain = text.splitlines()
            header_idx = None
            for i, cl in enumerate(cand_lines_plain):
                if _STRUCT_HEADER_RE.match(cl.strip()) and struct_name in cl:
                    header_idx = i
                    break
            if header_idx is None:
                return None
            # repr/serialization attrs → order-sensitive risk flag.
            for k in range(max(0, header_idx - 5), header_idx):
                if _ORDER_SENSITIVE_ATTR_RE.search(cand_lines_plain[k]):
                    self.risk_flags.append("order_sensitive_attribute")
                    break
            close_line = find_container_close_line(
                cand_lines_plain, header_idx, language="rust")
            if close_line is None:
                return None
            # SCOPE-QUALIFIED collision (claim 3): the name must not
            # exist in THIS struct — not anywhere in the file.
            for k in range(header_idx + 1, close_line):
                if _field_name(cand_lines_plain[k]) == field_name:
                    return None
            # Detect indentation from existing fields.
            indent = ""
            for k in range(header_idx + 1, close_line):
                if _field_name(cand_lines_plain[k]):
                    stripped = cand_lines_plain[k].lstrip()
                    indent = cand_lines_plain[k][
                        :len(cand_lines_plain[k]) - len(stripped)]
                    break
            field_text = item.rstrip()
            if not field_text.endswith(","):
                field_text += ","
            if indent:
                field_text = indent + field_text.strip()
            # Insert before the closing brace (keepends offsets are
            # the exact string positions).
            cand_lines = text.splitlines(keepends=True)
            pos = sum(len(l) for l in cand_lines[:close_line])
            pos = min(pos, len(text))
            return (pos, pos, field_text + "\n")

        def local_validity(self, text):
            return True

    return _StructFieldCodec()


def propose_named_field_union(
    resolved_text: str, missing_obligations: list,
    *, other_side_text: str = "",
) -> ImportUnionResult:
    """Propose a deterministic struct-field union.

    For each additive obligation that looks like a struct field (``name: Type``),
    find the destination struct in the candidate by matching the struct name.
    If the field name doesn't collide, transplant the field before the struct's
    closing brace.

    Stage 3 (the SWITCH): the KeyedCollectionMerge engine runs the
    lifecycle; this function is a thin adapter mapping the engine's
    :class:`PrimitiveResult` to the wire format
    (:class:`ImportUnionResult`, original certificate keys,
    ``rust.named_field_union/v1``).

    Returns an :class:`ImportUnionResult`. Never raises.
    """
    from capybase.keyed_collection import merge_keyed_collection, to_wire_result

    codec = _field_codec()
    result = merge_keyed_collection(
        codec, resolved_text, missing_obligations,
        other_side_text=other_side_text,
        mechanism_id="rust.named_field_union/v1",
    )

    def _applied_cert(r):
        # Certificate keys of the original inline lifecycle.
        cert = {
            "closed_obligations": [
                _normalize(c) for c in r.closed_obligations or []],
            "edits": [
                f"insert field {_field_name(c)}"
                for c in r.closed_obligations or []],
            "preconditions": {"no_name_collision": True},
            "risk_tier": RISK_TIER_A,
        }
        if codec.risk_flags:
            cert["risk_flags"] = codec.risk_flags
        return cert

    return to_wire_result(
        result, resolved_text,
        mechanism_id="rust.named_field_union/v1",
        reason_map={
            "no applicable items": "no additive field obligations",
            "no items could be safely inserted": "no fields could be inserted",
            "all items already present (idempotent)":
                "all fields already present",
        },
        applied_cert=_applied_cert)


__all__ = ["propose_named_field_union"]

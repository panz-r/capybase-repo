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

Stage 3 (the SWITCH, primitive #4 of 5): the KeyedCollectionMerge
engine is the authoritative implementation. This module supplies only
the attribute/meta codec; the lifecycle lives in
:mod:`capybase.keyed_collection`. The codec was shadow-verified at
11/11 agreement with the original inline lifecycle before the switch.
"""

from __future__ import annotations

import re

from capybase.import_union import ImportUnionResult, RISK_TIER_A
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


def _attribute_codec():
    """The attribute/meta codec for the KeyedCollectionMerge engine.

    Carries the EXACT semantics of the original inline lifecycle
    (shadow-verified at 11/11 agreement before the switch):

    - ``already_present`` is a REAL pre-check (unlike the field/item
      codecs): normalized-line membership in the resolved text — the
      original dropped exact-duplicate directive lines before the
      per-attribute union attempts.
    - ``try_edit`` returns a LINE-REPLACEMENT span (the union rewrites
      one existing attribute line in place), not a pure insertion.
      context (other side) is unused — the union partner is found in
      the candidate itself.
    - ``local_validity`` always True — the original had NO local
      validity gate.
    - ``risk_flags`` accumulates external-derive flags on the codec
      instance for the adapter to merge into the certificate.
    """

    class _AttributeCodec:
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
                if classify_channel(line) != "directive":
                    continue
                out.append(line)
            return out

        def already_present(self, text, item):
            norms = {self._normalize(l)
                     for l in text.splitlines() if l.strip()}
            return self._normalize(item) in norms

        def try_edit(self, text, item, context):
            lines = text.splitlines(keepends=True)
            before = list(lines)  # _try_union_one_attribute mutates in place
            edited = _try_union_one_attribute(lines, item, self.risk_flags)
            if edited is None:
                return None
            # Locate the changed line (exactly one is rewritten in place).
            for i, (b, a) in enumerate(zip(before, edited)):
                if b != a:
                    start = sum(len(l) for l in before[:i])
                    return (start, start + len(b), a)
            return None

        def local_validity(self, text):
            return True

        @staticmethod
        def _normalize(line):
            return " ".join(line.split())

    return _AttributeCodec()


def propose_attribute_meta_union(
    resolved_text: str, missing_obligations: list,
) -> ImportUnionResult:
    """Propose a deterministic attribute/meta-list union edit.

    Acts on additive directive-channel obligations (``#[derive(...)]`` and
    ``#[allow(...)]`` / ``#[warn(...)]`` lines). Version bumps, cfg, repr,
    serde, deny, forbid are never unioned.

    Stage 3 (the SWITCH): the KeyedCollectionMerge engine runs the
    lifecycle; this function is a thin adapter mapping the engine's
    :class:`PrimitiveResult` to the wire format
    (:class:`ImportUnionResult`, original certificate keys,
    ``rust.attribute_meta_union/v1``).

    Returns an :class:`ImportUnionResult`. Never raises.
    """
    from capybase.keyed_collection import merge_keyed_collection, to_wire_result

    codec = _attribute_codec()
    result = merge_keyed_collection(
        codec, resolved_text, missing_obligations,
        other_side_text="",
        mechanism_id="rust.attribute_meta_union/v1",
    )

    def _applied_cert(r):
        cert = {
            "closed_obligations": [
                _normalize(c) for c in r.closed_obligations or []],
            "edits": [
                f"union: {c.strip()[:60]}"
                for c in r.closed_obligations or []],
            "preconditions": {"same_attribute": True},
            "risk_tier": RISK_TIER_A,
        }
        if codec.risk_flags:
            cert["risk_flags"] = codec.risk_flags
        return cert

    return to_wire_result(
        result, resolved_text,
        mechanism_id="rust.attribute_meta_union/v1",
        reason_map={
            "no applicable items": "no additive directive obligations",
            "no items could be safely inserted":
                "no attributes could be safely unioned",
            "all items already present (idempotent)":
                "all directive lines already present",
        },
        applied_cert=_applied_cert)


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

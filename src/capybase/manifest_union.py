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

import re
from dataclasses import dataclass

from capybase.import_union import ImportUnionResult, RISK_TIER_A




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


def _manifest_codec():
    """The manifest-array codec (stage 3: the SWITCH — the engine is now
    the authoritative implementation; this codec was shadow-verified at
    6/6 agreement with the original inline lifecycle)."""
    import re as _re
    from capybase.change_accounting import classify_channel as _cc

    class _ManifestCodec:
        """CollectionCodec for TOML manifest arrays (shadow-verified)."""

        def applicable_obligations(self, obligations):
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
                if _cc(line) in ("comment", "formatting"):
                    continue
                out.append(line)
            return out

        def already_present(self, text, item):
            item_feats = _re.findall(r'"([^"]+)"', item)
            if item_feats and "features" in item:
                return all(f'"{f}"' in text for f in item_feats)
            return item.strip() in text

        def try_edit(self, text, item, context):
            item_feats = _re.findall(r'"([^"]+)"', item)
            if not item_feats:
                return None
            item_key = _re.match(r"\s*(\w+)", item.strip())
            item_key = item_key.group(1) if item_key else ""

            for line in text.splitlines():
                m_old = _re.match(rf"^\s*{item_key}\s*=\s*\[(.*)\]\s*$", line)
                if m_old:
                    old_items = m_old.group(1)
                    old_vals = _re.findall(r'"([^"]+)"', old_items)
                    merged = sorted(set(old_vals) | set(item_feats))
                    merged_str = ", ".join(f'"{v}"' for v in merged)
                    arr_start = text.index(line) + line.index("[")
                    arr_end = arr_start + len(old_items) + 2
                    return (arr_start, arr_end, f"[{merged_str}]")

            m_feat = _re.search(
                rf"{item_key}\s*=\s*\{{[^}}]*?features\s*=\s*\[([^\]]*)\]",
                text)
            if m_feat:
                old_items = m_feat.group(1)
                old_vals = _re.findall(r'"([^"]+)"', old_items)
                merged = sorted(set(old_vals) | set(item_feats))
                merged_str = ", ".join(f'"{v}"' for v in merged)
                return (m_feat.start(1), m_feat.end(1), merged_str)
            return None

        def local_validity(self, text):
            return text.count("[") == text.count("]")

    # Extend try_edit with the transplant fallback (matching the old
    # primitive's _try_line_transplant — insert after the anchor line).
    _orig_try_edit = _ManifestCodec.try_edit

    def _try_edit_with_transplant(self, text, item, context):
        result = _orig_try_edit(self, text, item, context)
        if result is not None:
            return result
        # Transplant fallback: find the anchor (the line before the
        # missing line in the other side), insert after it.
        if not context:
            return None
        other_lines = context.splitlines()
        item_stripped = item.strip()
        anchor_idx = None
        for i, ol in enumerate(other_lines):
            if ol.strip() == item_stripped:
                # Found the item; the anchor is the line before it.
                if i > 0 and other_lines[i - 1].strip():
                    anchor_idx = i - 1
                break
        if anchor_idx is None:
            return None
        anchor = other_lines[anchor_idx].strip()
        # Find the anchor in the candidate text.
        for j, cl in enumerate(text.splitlines()):
            if cl.strip() == anchor:
                lines = text.splitlines(keepends=True)
                pos = sum(len(l) for l in lines[:j + 1])
                pos = min(pos, len(text))
                return (pos, pos, item_stripped + "\n")
        return None

    _ManifestCodec.try_edit = _try_edit_with_transplant
    return _ManifestCodec()


def propose_manifest_union(
    resolved_text: str, missing_obligations: list,
    *, base_text: str = "", other_side_text: str = "",
) -> ImportUnionResult:
    """Propose a deterministic TOML manifest-union edit.

    Acts on additive obligations from change-accounting. Specifically targets
    TOML array additions (feature lists, workspace members) and inline-table
    feature-list additions. Version bumps and structural table changes are
    left to the model (exclusive choices).

    **Stage 3 (the SWITCH)**: the KeyedCollectionMerge engine is now the
    authoritative implementation. The codec was shadow-verified at 6/6
    agreement with the original inline lifecycle (every shape from the
    test suite: feature-list union, workspace members, idempotent,
    version-bump exclusion, multi-feature, simple array). This function
    is a thin adapter: engine result → ImportUnionResult.

    Args:
        resolved_text: the candidate's current resolved_text.
        missing_obligations: ``BranchObligation`` records.
        base_text: the base hunk text.
        other_side_text: the dropped side's text (for context).

    Returns an :class:`ImportUnionResult`. Never raises.
    """
    from capybase.keyed_collection import merge_keyed_collection, to_wire_result

    result = merge_keyed_collection(
        _manifest_codec(), resolved_text, missing_obligations,
        other_side_text=other_side_text,
        mechanism_id="toml.manifest_union/v1",
    )

    return to_wire_result(
        result, resolved_text,
        mechanism_id="toml.manifest_union/v1",
        # Certificate keys of the original inline lifecycle (the original
        # set risk_tier/preconditions only on the APPLIED certificate).
        applied_cert=lambda r: {
            "risk_tier": RISK_TIER_A,
            "preconditions": {"bracket_balanced": True},
        })




__all__ = ["propose_manifest_union"]

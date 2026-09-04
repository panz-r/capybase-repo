"""Manifest-array shadow-mode port (reuse-design stage 2, item 4).

The existing `propose_manifest_union` stays authoritative; the engine
(`merge_keyed_collection` + a manifest codec) runs alongside on the
SAME inputs. `shadow_compare` records divergences; the test asserts
they agree on every shape from the existing test suite.
"""

from __future__ import annotations

import pytest

from capybase.change_accounting import classify_channel
from capybase.deterministic_model import PrimitiveStatus
from capybase.import_union import (
    STATUS_APPLIED,
    STATUS_NOT_APPLICABLE,
)
from capybase.keyed_collection import merge_keyed_collection, shadow_compare
from capybase.manifest_union import propose_manifest_union


class _ob:
    """Mirrors the manifest test suite's obligation factory."""
    def __init__(self, line, exclusive=False):
        self.line = line
        self.channel = classify_channel(line)
        self.status = "MISSING"
        self.side = "replayed"
        self.operation = "added"
        self.exclusive = exclusive


class ManifestArrayCodec:
    """The manifest-array codec for the keyed-collection engine.

    Implements the SAME edit shapes as the existing primitive's
    `_try_array_or_feature_union` + `_try_line_transplant`, but through
    the CollectionCodec protocol (span+replacement, not text-in-text-out).
    """

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
            ch = classify_channel(line)
            if ch in ("comment", "formatting"):
                continue
            out.append(line)
        return out

    def already_present(self, text, item):
        """Idempotency: the item's CONTENT is already in the text."""
        # Feature-list: check each new feature value is present.
        import re
        new_feats = re.findall(r'"([^"]+)"', item)
        if new_feats and "features" in item:
            return all(f'"{f}"' in text for f in new_feats)
        return item.strip() in text

    def try_edit(self, text, item, context):
        """The same array-union edit the existing primitive does."""
        import re
        # Extract the item's features (the values to union).
        item_feats = re.findall(r'"([^"]+)"', item)
        if not item_feats:
            return None
        item_key = re.match(r'\s*(\w+)', item.strip())
        item_key = item_key.group(1) if item_key else ""

        # Case 1: plain array in the text (key = [...]).
        for line in text.splitlines():
            m_old = re.match(rf'^\s*{item_key}\s*=\s*\[(.*)\]\s*$', line)
            if m_old:
                old_items = m_old.group(1)
                old_vals = re.findall(r'"([^"]+)"', old_items)
                merged = sorted(set(old_vals) | set(item_feats))
                merged_str = ", ".join(f'"{v}"' for v in merged)
                arr_start = text.index(line) + line.index("[")
                arr_end = arr_start + len(old_items) + 2
                return (arr_start, arr_end, f"[{merged_str}]")

        # Case 2: inline table feature list (key = { ..., features = [...] }).
        m_feat = re.search(
            rf'{item_key}\s*=\s*\{{[^}}]*?features\s*=\s*\[([^\]]*)\]',
            text)
        if m_feat:
            old_items = m_feat.group(1)
            old_vals = re.findall(r'"([^"]+)"', old_items)
            merged = sorted(set(old_vals) | set(item_feats))
            merged_str = ", ".join(f'"{v}"' for v in merged)
            return (m_feat.start(1), m_feat.end(1), merged_str)
        return None

    def local_validity(self, text):
        return text.count("[") == text.count("]")


# The shapes from the existing test suite — the shadow must agree on all.
SHADOW_CASES = [
    ("feature_list", 'tokio = { version = "1.0", features = ["rt"] }\n',
     [_ob('tokio = { version = "1.0", features = ["macros"] }')], ""),
    ("members", 'members = ["crate-a", "crate-b"]\n',
     [_ob('members = ["crate-c"]')], ""),
    ("idempotent", 'tokio = { version = "1.0", features = ["rt", "macros"] }\n',
     [_ob('tokio = { version = "1.0", features = ["macros"] }')], ""),
    ("version_bump", 'tokio = { version = "1.52.2" }\n',
     [_ob('tokio = { version = "1.51.3" }', exclusive=True)], ""),
    ("multi_features", 'tokio = { version = "1.0", features = ["rt"] }\n',
     [_ob('tokio = { version = "1.0", features = ["macros", "net"] }')], ""),
    ("simple_array", 'keywords = ["rust", "async"]\n',
     [_ob('keywords = ["runtime"]')], ""),
]


@pytest.mark.parametrize("name,resolved,missing,other", SHADOW_CASES)
def test_shadow_manifest_agrees(name, resolved, missing, other):
    """Old (authoritative) and new (engine) must agree on every shape."""
    old = propose_manifest_union(resolved, missing, other_side_text=other)
    new = merge_keyed_collection(
        ManifestArrayCodec(), resolved, missing, other_side_text=other,
        mechanism_id="toml.manifest_engine/v0")

    divergences = shadow_compare("manifest_union", old, new)
    # Status agreement is the minimum bar for the port.
    old_status = str(old.status).lower()
    new_status = new.status.value
    assert old_status == new_status, (
        f"{name}: old={old_status} new={new_status} "
        f"divergences={[(d.item, d.old_status, d.new_status) for d in divergences]}")
    # Text agreement when both applied.
    if old_status == STATUS_APPLIED:
        assert old.text == new.candidate, (
            f"{name}: applied texts differ:\n"
            f"  old: {old.text[:120]}\n  new: {new.candidate[:120]}")

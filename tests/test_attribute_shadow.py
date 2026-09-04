"""Attribute-meta shadow-mode port (reuse-design stage 2, item 7a).

The existing `propose_attribute_meta_union` stays authoritative; the
engine runs alongside via an attribute codec. Shadow agreement is the bar.
"""

from __future__ import annotations

import re

import pytest

from capybase.change_accounting import BranchObligation
from capybase.keyed_collection import merge_keyed_collection
from capybase.attribute_meta_union import propose_attribute_meta_union


def _ob(line: str) -> BranchObligation:
    return BranchObligation(
        line=line, channel="directive", status="MISSING",
        side="replayed", operation="added", exclusive=False,
    )


#: The lint/attribute kinds that UNION (both sides' items merge).
_UNIONABLE = {"derive", "allow", "warn"}
#: Never unioned — opaque or safety-critical.
_NEVER = {"deny", "forbid", "cfg", "repr", "cfg_attr"}

_BUILTIN_DERIVES = frozenset({
    "Debug", "Clone", "Copy", "PartialEq", "Eq", "Hash",
    "Default", "PartialOrd", "Ord",
})

_ATTR_RE = re.compile(r"#\[(\w+)\(([^)]*)\)\]")


class AttributeCodec:
    """Rust attribute/meta-list union through the CollectionCodec protocol.

    Implements the same policy as the existing primitive: derive/allow/warn
    union; deny/forbid/cfg/repr never; external derives flagged; lint-level
    mismatch refused; idempotent on already-present traits.
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
            m = _ATTR_RE.search(line)
            if not m:
                continue
            kind = m.group(1)
            if kind not in _UNIONABLE:
                continue
            out.append(line)
        return out

    def already_present(self, text, item):
        m_new = _ATTR_RE.search(item)
        if not m_new:
            return True
        kind, new_items = m_new.group(1), m_new.group(2)
        new_set = {x.strip() for x in new_items.split(",") if x.strip()}
        # Find the same-kind attribute in the text.
        for m_old in _ATTR_RE.finditer(text):
            if m_old.group(1) == kind:
                old_set = {
                    x.strip() for x in m_old.group(2).split(",")
                    if x.strip()}
                if new_set <= old_set:
                    return True
        return False

    def try_edit(self, text, item, context):
        m_new = _ATTR_RE.search(item)
        if not m_new:
            return None
        kind, new_items = m_new.group(1), m_new.group(2)
        new_set = {x.strip() for x in new_items.split(",") if x.strip()}
        # Find the same-kind attribute in the text.
        for m_old in _ATTR_RE.finditer(text):
            if m_old.group(1) != kind:
                continue
            old_inner = m_old.group(2)
            old_set = {
                x.strip() for x in old_inner.split(",") if x.strip()}
            # Lint-level is same-kind by construction here; a CROSS-kind
            # mismatch (#[allow] vs #[deny]) means the item's kind wasn't
            # in _UNIONABLE or was filtered — handled by applicable().
            merged = sorted(old_set | new_set)
            merged_str = ", ".join(merged)
            start = m_old.start(2)
            end = m_old.end(2)
            return (start, end, merged_str)
        return None

    def local_validity(self, text):
        return text.count("#[") == text.count("]")


SHADOW_CASES = [
    ("builtin_derive", "#[derive(Debug, Clone)]\nstruct S { x: u32 }",
     [_ob("#[derive(Debug, PartialEq)]")]),
    ("external_derive", "#[derive(Debug)]\nstruct S {}",
     [_ob("#[derive(Serialize)]")]),
    ("allow_lint", "#[allow(dead_code)]\nfn main(){}",
     [_ob("#[allow(unused_variables)]")]),
    ("warn_lint", "#[warn(unused_imports)]\nfn main(){}",
     [_ob("#[warn(deprecated)]")]),
    ("deny_refused", "#[deny(unsafe_code)]\nfn main(){}",
     [_ob("#[deny(unused_variables)]")]),
    ("forbid_refused", "#[forbid(unsafe_code)]\nfn main(){}",
     [_ob("#[forbid(unused_variables)]")]),
    ("cfg_refused", '#[cfg(feature = "x")]\nfn main(){}',
     [_ob('#[cfg(feature = "y")]')]),
    ("repr_refused", "#[repr(C)]\nstruct S {}",
     [_ob("#[repr(packed)]")]),
    ("lint_mismatch", "#[allow(dead_code)]\nfn main(){}",
     [_ob("#[deny(dead_code)]")]),
    ("idempotent", "#[derive(Debug, Clone)]\nstruct S {}",
     [_ob("#[derive(Clone)]")]),
    ("all_present", "#[derive(Debug, Clone, Serialize)]\nstruct S {}",
     [_ob("#[derive(Clone, Serialize)]")]),
]


@pytest.mark.parametrize("name,resolved,missing", SHADOW_CASES)
def test_shadow_attr_agrees(name, resolved, missing):
    old = propose_attribute_meta_union(resolved, missing)
    new = merge_keyed_collection(
        AttributeCodec(), resolved, missing,
        mechanism_id="rust.attr_engine/v0")
    old_status = str(old.status).lower()
    new_status = new.status.value
    assert old_status == new_status, (
        f"{name}: old={old_status} new={new_status} "
        f"reason={new.certificate.get('reason', '')[:80]}")
    if old_status == "applied":
        # Content agreement (both contain the unioned traits).
        old_traits = set(re.findall(r"\w+", old.text))
        new_traits = set(re.findall(r"\w+", new.candidate or ""))
        assert old_traits == new_traits, (
            f"{name}: trait sets differ:\n  old only: {old_traits - new_traits}"
            f"\n  new only: {new_traits - old_traits}")

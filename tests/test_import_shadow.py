"""Import-union shadow-mode port (reuse-design stage 2, item 8).

The existing `propose_import_union` stays authoritative. The engine
runs alongside via an ImportCodec that DELEGATES to the existing
`parse_use_leaves` + `_merge_into_group_line` machinery (the genuinely
language-specific tree parsing stays where it is) while running
through the engine's lifecycle (filter → idempotency → transactional
edits → certificate).

This is the thinnest codec: an adapter, not a reimplementation.
"""

from __future__ import annotations

import pytest

from capybase.change_accounting import BranchObligation, classify_channel
from capybase.import_union import (
    ImportLeaf,
    _add_separate_use_line,
    _merge_into_group_line,
    parse_use_leaves,
    propose_import_union,
)
from capybase.keyed_collection import merge_keyed_collection


def _ob(line: str, exclusive=False, operation="added") -> BranchObligation:
    return BranchObligation(
        line=line, channel=classify_channel(line), status="MISSING",
        side="replayed", operation=operation, exclusive=exclusive,
    )


class ImportCodec:
    """Rust import union through the CollectionCodec protocol.

    The use-tree parsing and group-merging DELEGATE to the existing
    `parse_use_leaves` / `_merge_into_group_line` (genuinely
    language-specific machinery); the engine provides the lifecycle.
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
            # Must be a use statement parseable by the existing machinery.
            if parse_use_leaves(line) is None:
                continue
            out.append(line)
        return out

    def already_present(self, text, item):
        to_add = parse_use_leaves(item)
        if not to_add:
            return True
        # §2 contract: keyed on the FULL PATH (not binding) — a
        # same-binding-different-path leaf is a collision, not presence.
        existing_paths = set()
        for line in text.splitlines():
            existing = parse_use_leaves(line)
            if existing:
                for l in existing:
                    existing_paths.add(l.path)
        return all(l.path in existing_paths for l in to_add)

    def try_edit(self, text, item, context):
        to_add = parse_use_leaves(item)
        if to_add is None:
            return None
        # Try merging into each existing use line.
        for line in text.splitlines():
            dest_leaves = parse_use_leaves(line)
            if dest_leaves is None:
                continue
            merged = _merge_into_group_line(line, dest_leaves, to_add)
            if merged is not None and merged != line:
                start = text.index(line)
                end = start + len(line)
                return (start, end, merged)
        # Separate-line fallback: insert as new use line(s) adjacent to
        # the last use line (delegates to the primitive's own helper).
        use_lines = [l for l in text.splitlines()
                     if parse_use_leaves(l) is not None]
        if use_lines:
            result = _add_separate_use_line(use_lines[-1], to_add, text)
            if result is not None:
                new_text, _added = result
                # One insertion span: end of the anchor line.
                lines = text.splitlines(keepends=True)
                anchor = use_lines[-1].rstrip("\n")
                for i, ln in enumerate(lines):
                    if ln.rstrip("\n") == anchor:
                        pos = sum(len(l) for l in lines[:i + 1])
                        pos = min(pos, len(text))
                        return (pos, pos, new_text[pos:pos + (len(new_text) - len(text))])
        return None

    def local_validity(self, text):
        return text.count("{") == text.count("}")


SHADOW_CASES = [
    ("grouped_add",
     "use std::collections::{HashMap, BTreeMap};\nfn main(){}",
     [_ob("use std::collections::HashSet;")]),
    ("separate_import",
     "use std::fmt;\nfn main(){}",
     [_ob("use std::io;")]),
    ("rename",
     "use std::fmt::Debug;\nfn main(){}",
     [_ob("use std::fmt::Display as Disp;")]),
    ("exclusive_ignored",
     "use a::Client;\nfn main(){}",
     [_ob("use b::Client;", exclusive=True)]),
    ("non_additive_ignored",
     "use a::Client;\nfn main(){}",
     [_ob("use a::Client;", operation="removed")]),
    ("no_destination",
     "fn main(){}",
     [_ob("use util::X;")]),
    ("idempotent",
     "use std::collections::{HashMap, HashSet};\nfn main(){}",
     [_ob("use std::collections::HashSet;")]),
    ("not_a_use",
     "let x = 1;\nfn main(){}",
     [_ob("let y = 2;")]),
]


@pytest.mark.parametrize("name,resolved,missing", SHADOW_CASES)
def test_shadow_import_agrees(name, resolved, missing):
    old = propose_import_union(resolved, missing)
    new = merge_keyed_collection(
        ImportCodec(), resolved, missing,
        mechanism_id="rust.import_engine/v0")
    old_status = str(old.status).lower()
    new_status = new.status.value
    assert old_status == new_status, (
        f"{name}: old={old_status} new={new_status} "
        f"reason={new.certificate.get('reason', '')[:80]}")
    if old_status == "applied":
        assert old.text == new.candidate, (
            f"{name}: texts differ:\n  old: {old.text[:120]}\n"
            f"  new: {(new.candidate or '')[:120]}")

"""JavaScript/TypeScript import codec (reuse-design: second new language).

The design's leverage order: Python (done) → JS/TS → Go → Java.
JS/TS imports have richer shapes than Python:

- ``import X from 'module'``           — default import
- ``import { A, B } from 'module'``    — named imports
- ``import * as NS from 'module'``     — namespace import
- ``import type { T } from 'module'``  — TypeScript type-only
- ``export { X } from 'module'``       — re-export

Semantic identity: (module, kind, imported-name) — a default import of
module M is distinct from a named import {A} from M, which is distinct
from a namespace import of M.
"""

from __future__ import annotations

import re

import pytest

from capybase.change_accounting import BranchObligation, classify_channel
from capybase.deterministic_model import PrimitiveStatus
from capybase.keyed_collection import merge_keyed_collection


def _ob(line: str) -> BranchObligation:
    return BranchObligation(
        line=line, channel=classify_channel(line), status="MISSING",
        side="replayed", operation="added", exclusive=False,
    )


_JS_IMPORT = re.compile(
    r"^(?:import|export)\s+"
    r"(?:(type)\s+)?"                                      # TS type-only
    r"(?:(\w+)\s*,?\s*)?"                                   # default import
    r"(?:\{([^}]*)\}\s*)?"                                  # named imports
    r"(?:(\*\s+as\s+\w+)\s*)?"                              # namespace
    r"from\s+['\"]([^'\"]+)['\"]"
)


def _import_identity(line: str) -> dict | None:
    """The semantic identity of one JS/TS import line."""
    m = _JS_IMPORT.match(line.strip())
    if not m:
        return None
    type_only = bool(m.group(1))
    default = m.group(2)
    named = frozenset(
        n.strip().split(" as ")[0].strip()
        for n in (m.group(3) or "").split(",") if n.strip()
    )
    namespace = m.group(4)
    module = m.group(5)
    return {
        "module": module,
        "type_only": type_only,
        "default": default or "",
        "named": named,
        "namespace": namespace or "",
    }


class JSImportCodec:
    """JS/TS import union through the CollectionCodec protocol."""

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
            if _import_identity(line) is None:
                continue
            out.append(line)
        return out

    def already_present(self, text, item):
        item_id = _import_identity(item)
        if item_id is None:
            return True
        for line in text.splitlines():
            existing = _import_identity(line)
            if existing is None:
                continue
            if existing["module"] != item_id["module"]:
                continue
            # Same module: check if all item names are already imported.
            if item_id["default"] and existing["default"] != item_id["default"]:
                continue  # different default name — could be a rename
            if item_id["named"] and not (item_id["named"] <= existing["named"]):
                continue  # some names not yet imported
            if item_id["namespace"] and existing["namespace"] != item_id["namespace"]:
                continue
            # This line covers the item's needs.
            if (not item_id["default"] or existing["default"] == item_id["default"]) and \
               (not item_id["named"] or item_id["named"] <= existing["named"]) and \
               (not item_id["namespace"] or existing["namespace"] == item_id["namespace"]):
                return True
        return False

    def try_edit(self, text, item, context):
        lines = text.splitlines(keepends=True)
        # Find the last import line.
        last_import_idx = None
        for i, l in enumerate(lines):
            stripped = l.strip()
            if stripped.startswith(("import ", "export {")) and "from" in stripped:
                last_import_idx = i
        if last_import_idx is None:
            return None  # no existing imports to anchor against
        # Try to MERGE into an existing import from the same module.
        item_id = _import_identity(item)
        if item_id and item_id["named"]:
            for i, l in enumerate(lines):
                existing = _import_identity(l)
                if existing is None or existing["module"] != item_id["module"]:
                    continue
                if existing["named"]:
                    # Merge named imports: union the name sets.
                    merged = sorted(existing["named"] | item_id["named"])
                    names_str = ", ".join(merged)
                    new_line = re.sub(
                        r"\{[^}]*\}", f"{{{names_str}}}", l.rstrip())
                    start = sum(len(ln) for ln in lines[:i])
                    end = start + len(l)
                    return (start, end, new_line + "\n")
        # Separate-line fallback: insert after the last import.
        pos = sum(len(l) for l in lines[:last_import_idx + 1])
        pos = min(pos, len(text))
        return (pos, pos, item.strip() + "\n")

    def local_validity(self, text):
        return text.count("{") == text.count("}")


class TestJSImportCodec:

    def test_named_import_insertion(self):
        text = "import { useState } from 'react';\n\nfunction App() {}\n"
        result = merge_keyed_collection(
            JSImportCodec(), text, [_ob("import { useEffect } from 'react';")],
            mechanism_id="js.import_engine/v0")
        assert result.status is PrimitiveStatus.APPLIED
        assert "useEffect" in result.candidate
        assert "useState" in result.candidate

    def test_named_import_merge_same_module(self):
        """Two named imports from the same module merge into one line."""
        text = "import { useState } from 'react';\n"
        result = merge_keyed_collection(
            JSImportCodec(), text, [_ob("import { useEffect, useRef } from 'react';")],
            mechanism_id="js.import_engine/v0")
        assert result.status is PrimitiveStatus.APPLIED
        # All three names in one import line.
        for name in ("useState", "useEffect", "useRef"):
            assert name in result.candidate
        # Only one react import (merged, not duplicated).
        assert result.candidate.count("from 'react'") == 1

    def test_default_import_separate_line(self):
        """A default import from a new module → separate line."""
        text = "import { useState } from 'react';\n"
        result = merge_keyed_collection(
            JSImportCodec(), text, [_ob("import axios from 'axios';")],
            mechanism_id="js.import_engine/v0")
        assert result.status is PrimitiveStatus.APPLIED
        assert "axios" in result.candidate
        assert result.candidate.count("from 'react'") == 1

    def test_idempotent(self):
        text = "import { useState, useEffect } from 'react';\n"
        result = merge_keyed_collection(
            JSImportCodec(), text, [_ob("import { useEffect } from 'react';")],
            mechanism_id="js.import_engine/v0")
        assert result.status is PrimitiveStatus.NOT_APPLICABLE

    def test_namespace_import(self):
        text = "import { useState } from 'react';\n"
        result = merge_keyed_collection(
            JSImportCodec(), text, [_ob("import * as React from 'react';")],
            mechanism_id="js.import_engine/v0")
        assert result.status is PrimitiveStatus.APPLIED
        assert "* as React" in (result.candidate or "")

    def test_ts_type_only(self):
        text = "import { component } from './utils';\n"
        result = merge_keyed_collection(
            JSImportCodec(), text, [_ob("import type { Props } from './types';")],
            mechanism_id="js.import_engine/v0")
        assert result.status is PrimitiveStatus.APPLIED
        assert "type { Props }" in (result.candidate or "")

    def test_different_module_not_merged(self):
        """Imports from different modules are separate lines."""
        text = "import { a } from './a';\n"
        result = merge_keyed_collection(
            JSImportCodec(), text, [_ob("import { b } from './b';")],
            mechanism_id="js.import_engine/v0")
        assert result.status is PrimitiveStatus.APPLIED
        assert "from './a'" in result.candidate
        assert "from './b'" in result.candidate

    def test_non_import_declined(self):
        text = "import { a } from './a';\nconst x = 1;\n"
        result = merge_keyed_collection(
            JSImportCodec(), text, [_ob("const y = 2;")],
            mechanism_id="js.import_engine/v0")
        assert result.status is PrimitiveStatus.NOT_APPLICABLE

    def test_engine_never_raises(self):
        result = merge_keyed_collection(
            JSImportCodec(), None, [],
            mechanism_id="js.import_engine/v0")
        assert result is not None

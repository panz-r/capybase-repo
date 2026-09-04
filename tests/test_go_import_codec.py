"""Go import codec (reuse-design: third new language).

Go imports are the simplest shape: one import block, one path per line,
optionally aliased. The codec demonstrates the engine handles Go's
block-structured import syntax through the same protocol.

Shapes:
- ``import "fmt"``                       — single import
- ``import (\n    "fmt"\n    "os"\n)``   — import block
- ``import myalias "example.com/pkg"``   — aliased import
- ``import _ "embed"``                   — blank import (side effects)
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


_GO_PATH = re.compile(r'"([^"]+)"')


def _import_path(line: str) -> str | None:
    """The import path from a Go import line (or the line inside a block)."""
    stripped = line.strip()
    m = _GO_PATH.search(stripped)
    if not m:
        return None
    # Accept: import lines, bare paths (inside blocks), aliased, blank.
    if (stripped.startswith("import ") or
            stripped.startswith("_ ") or
            re.match(r"^\w+\s+\"", stripped) or
            stripped.startswith('"')):
        return m.group(1)
    return None


class GoImportCodec:
    """Go import union through the CollectionCodec protocol."""

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
            if _import_path(line) is None:
                continue
            out.append(line)
        return out

    def already_present(self, text, item):
        path = _import_path(item)
        if path is None:
            return True
        return f'"{path}"' in text

    def try_edit(self, text, item, context):
        lines = text.splitlines(keepends=True)
        path = _import_path(item)
        if path is None:
            return None
        # Find the import block's closing paren.
        in_import_block = False
        close_paren_idx = None
        single_import_idx = None
        for i, l in enumerate(lines):
            stripped = l.strip()
            if stripped == "import (":
                in_import_block = True
            elif in_import_block and stripped == ")":
                close_paren_idx = i
                break
            elif stripped.startswith("import ") and _import_path(l):
                single_import_idx = i
        if close_paren_idx is not None:
            # Insert before the closing paren.
            pos = sum(len(l) for l in lines[:close_paren_idx])
            pos = min(pos, len(text))
            # Detect indentation from existing imports.
            indent = "\t"
            for l in lines:
                stripped = l.strip()
                if stripped.startswith('"') and not stripped.startswith("import"):
                    ws = l[:len(l) - len(l.lstrip())]
                    indent = ws
                    break
            return (pos, pos, f"{indent}{item.strip()}\n")
        if single_import_idx is not None:
            # Convert single import to a block, or add after.
            pos = sum(len(l) for l in lines[:single_import_idx + 1])
            pos = min(pos, len(text))
            return (pos, pos, item.strip() + "\n")
        return None

    def local_validity(self, text):
        return text.count("(") == text.count(")")


class TestGoImportCodec:

    def test_insert_into_block(self):
        text = 'import (\n\t"fmt"\n\t"os"\n)\n'
        result = merge_keyed_collection(
            GoImportCodec(), text, [_ob('\t"strings"')],
            mechanism_id="go.import_engine/v0")
        assert result.status is PrimitiveStatus.APPLIED
        assert '"strings"' in result.candidate
        assert '"fmt"' in result.candidate
        assert '"os"' in result.candidate

    def test_idempotent(self):
        text = 'import (\n\t"fmt"\n)\n'
        result = merge_keyed_collection(
            GoImportCodec(), text, [_ob('\t"fmt"')],
            mechanism_id="go.import_engine/v0")
        assert result.status is PrimitiveStatus.NOT_APPLICABLE

    def test_aliased_import(self):
        text = 'import (\n\t"fmt"\n)\n'
        result = merge_keyed_collection(
            GoImportCodec(), text, [_ob('\tmyalias "example.com/pkg"')],
            mechanism_id="go.import_engine/v0")
        assert result.status is PrimitiveStatus.APPLIED
        assert 'myalias "example.com/pkg"' in result.candidate

    def test_blank_import(self):
        text = 'import (\n\t"fmt"\n)\n'
        result = merge_keyed_collection(
            GoImportCodec(), text, [_ob('\t_ "embed"')],
            mechanism_id="go.import_engine/v0")
        assert result.status is PrimitiveStatus.APPLIED
        assert '_ "embed"' in result.candidate

    def test_non_import_declined(self):
        text = 'import (\n\t"fmt"\n)\nfunc main() {}\n'
        result = merge_keyed_collection(
            GoImportCodec(), text, [_ob("x := 1")],
            mechanism_id="go.import_engine/v0")
        assert result.status is PrimitiveStatus.NOT_APPLICABLE

    def test_engine_never_raises(self):
        result = merge_keyed_collection(
            GoImportCodec(), None, [],
            mechanism_id="go.import_engine/v0")
        assert result is not None

"""Python import codec (reuse-design: first new language codec).

The design's leverage order puts Python imports first among new codecs:
the shapes are simple (import X / from X import Y / from X import a, b /
relative imports), the language is in the corpus, and the codec
demonstrates that the KeyedCollectionMerge engine works identically for
a new language through the same protocol.
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


#: Python import patterns:
#:   import os
#:   import os.path
#:   from typing import List, Dict
#:   from . import sibling
#:   from .relative import thing
#:   from __future__ import annotations
_PY_IMPORT = re.compile(r"^import\s+([\w.]+)")
_PY_FROM = re.compile(r"^from\s+([\w.]+)\s+import\s+(.+)")


def _import_identity(line: str) -> tuple[str, frozenset[str]] | None:
    """(module, names) — the semantic identity of a Python import line."""
    line = line.strip()
    m = _PY_FROM.match(line)
    if m:
        module = m.group(1)
        names = frozenset(
            n.strip() for n in m.group(2).split(",") if n.strip())
        return module, names
    m = _PY_IMPORT.match(line)
    if m:
        return m.group(1), frozenset()
    return None


class PythonImportCodec:
    """Python import union through the CollectionCodec protocol.

    Implements the same lifecycle contracts as the Rust import codec:
    filter to additive import obligations, idempotency by full
    (module, names) identity, insertion adjacent to existing imports.
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
            if _import_identity(line) is None:
                continue
            out.append(line)
        return out

    def already_present(self, text, item):
        item_id = _import_identity(item)
        if item_id is None:
            return True
        item_module, item_names = item_id
        for line in text.splitlines():
            existing_id = _import_identity(line)
            if existing_id is None:
                continue
            ex_module, ex_names = existing_id
            if ex_module == item_module:
                if not item_names:
                    return True  # `import os` — module presence suffices
                if item_names <= ex_names:
                    return True  # all names already imported
        return False

    def try_edit(self, text, item, context):
        # Find the last import line to insert after.
        lines = text.splitlines(keepends=True)
        last_import_idx = None
        for i, l in enumerate(lines):
            stripped = l.strip()
            if stripped.startswith(("import ", "from ")):
                last_import_idx = i
        if last_import_idx is None:
            # No existing imports — insert at the top (after any docstring
            # or __future__ imports, but we keep it simple: after the first
            # non-comment, non-docstring line).
            insert_at = 0
            for i, l in enumerate(lines):
                stripped = l.strip()
                if stripped.startswith(("#", '"""', "'''")) or not stripped:
                    insert_at = i + 1
                else:
                    break
            pos = sum(len(l) for l in lines[:insert_at])
            pos = min(pos, len(text))
            return (pos, pos, item.strip() + "\n")
        # Insert after the last import.
        pos = sum(len(l) for l in lines[:last_import_idx + 1])
        pos = min(pos, len(text))
        return (pos, pos, item.strip() + "\n")

    def local_validity(self, text):
        # Python doesn't have brace balance; just check non-empty.
        return bool(text.strip())


class TestPythonImportCodec:
    """The codec through the engine — the design's cross-language proof
    extended to Python (the first new language beyond Rust/TOML)."""

    def test_simple_import_insertion(self):
        text = "import os\nimport sys\n\ndef main():\n    pass\n"
        result = merge_keyed_collection(
            PythonImportCodec(), text, [_ob("import json")],
            mechanism_id="python.import_engine/v0")
        assert result.status is PrimitiveStatus.APPLIED
        assert "import json" in result.candidate
        assert "import os" in result.candidate
        # Inserted after the last import (sys), not at the top.
        assert result.candidate.index("json") > result.candidate.index("sys")

    def test_from_import_insertion(self):
        text = "import os\n\ndef main():\n    pass\n"
        result = merge_keyed_collection(
            PythonImportCodec(), text, [_ob("from typing import List")],
            mechanism_id="python.import_engine/v0")
        assert result.status is PrimitiveStatus.APPLIED
        assert "from typing import List" in result.candidate

    def test_idempotent_already_present(self):
        text = "import os\nimport json\nfrom typing import List\n"
        result = merge_keyed_collection(
            PythonImportCodec(), text, [_ob("import os")],
            mechanism_id="python.import_engine/v0")
        assert result.status is PrimitiveStatus.NOT_APPLICABLE

    def test_from_import_partial_idempotent(self):
        """`from typing import List` present; `from typing import List, Dict`
        → the Dict part is still additive (subset check)."""
        text = "from typing import List\n"
        result = merge_keyed_collection(
            PythonImportCodec(), text, [_ob("from typing import Dict")],
            mechanism_id="python.import_engine/v0")
        assert result.status is PrimitiveStatus.APPLIED
        assert "Dict" in result.candidate

    def test_no_existing_imports_inserts_at_top(self):
        text = "# comment\n\ndef main():\n    pass\n"
        result = merge_keyed_collection(
            PythonImportCodec(), text, [_ob("import os")],
            mechanism_id="python.import_engine/v0")
        assert result.status is PrimitiveStatus.APPLIED
        assert "import os" in result.candidate
        # Inserted after the comment, before the function.
        assert result.candidate.index("import os") < result.candidate.index("def")

    def test_relative_import(self):
        text = "import os\n"
        result = merge_keyed_collection(
            PythonImportCodec(), text, [_ob("from . import sibling")],
            mechanism_id="python.import_engine/v0")
        assert result.status is PrimitiveStatus.APPLIED
        assert "from . import sibling" in result.candidate

    def test_non_import_declined(self):
        text = "import os\nx = 1\n"
        result = merge_keyed_collection(
            PythonImportCodec(), text, [_ob("x = 2")],
            mechanism_id="python.import_engine/v0")
        assert result.status is PrimitiveStatus.NOT_APPLICABLE

    def test_engine_never_raises(self):
        result = merge_keyed_collection(
            PythonImportCodec(), None, [],
            mechanism_id="python.import_engine/v0")
        assert result is not None

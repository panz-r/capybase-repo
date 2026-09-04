"""Named-field shadow-mode port (reuse-design stage 2, item 5a).

The existing `propose_named_field_union` stays authoritative; the
engine (`merge_keyed_collection` + a struct-field codec) runs alongside
on the SAME inputs. Shadow agreement is the bar.
"""

from __future__ import annotations

import pytest

from capybase.change_accounting import BranchObligation, classify_channel
from capybase.deterministic_model import PrimitiveStatus
from capybase.keyed_collection import merge_keyed_collection, shadow_compare
from capybase.named_field_union import propose_named_field_union


def _ob(line: str) -> BranchObligation:
    return BranchObligation(
        line=line, channel=classify_channel(line), status="MISSING",
        side="replayed", operation="added", exclusive=False,
    )


class StructFieldCodec:
    """Rust struct-field insertion through the CollectionCodec protocol.

    Implements the SAME edit shape as `_try_insert_field`: find the
    destination struct by walking the other side back from the field,
    locate the same struct in the candidate, insert before the closing
    brace. Scope-qualified collision (claim-3 fix).
    """

    #: Matches ``struct Name<...> {`` or ``struct Name {``.
    _HEADER = __import__("re").compile(
        r"^\s*(?:pub\s+)?struct\s+(\w+)")
    _TUPLE = __import__("re").compile(r"\(.*\)\s*;?\s*$")

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
            if ch in ("comment", "formatting", "directive"):
                continue
            # Must look like a struct field: name: Type,
            if not __import__("re").match(r"^\s*\w+\s*:\s*[^,]+,", line):
                continue
            out.append(line)
        return out

    def already_present(self, text, item):
        """Scope-qualified (claim-3 fix): the field name must exist in
        the DESTINATION struct, not anywhere in the file."""
        field_name = item.strip().split(":")[0].strip()
        struct_name = self._find_struct(item, text)
        if struct_name is None:
            return False  # can't determine — let try_edit decide
        lines = text.splitlines()
        header_idx = self._find_header(lines, struct_name)
        if header_idx is None:
            return False
        close = self._find_close(lines, header_idx)
        if close is None:
            return False
        for k in range(header_idx + 1, close):
            fn = lines[k].strip().split(":")[0].strip()
            if fn == field_name:
                return True
        return False

    def try_edit(self, text, item, context):
        """Insert the field before the destination struct's closing brace."""
        import re
        field_name = item.strip().split(":")[0].strip()
        if not context:
            return None
        other_lines = context.splitlines()
        # Find the field in the other side, then walk back to the struct.
        field_idx = None
        for i, ol in enumerate(other_lines):
            if ol.strip().split(":")[0].strip() == field_name:
                field_idx = i
                break
        if field_idx is None:
            return None
        struct_name = None
        for j in range(field_idx - 1, -1, -1):
            m = self._HEADER.match(other_lines[j])
            if m:
                struct_name = m.group(1)
                header = other_lines[j]
                break
        if struct_name is None:
            return None
        if self._TUPLE.search(header or ""):
            return None  # tuple struct — positional, not named
        # Find the same struct in the candidate.
        cand_lines = text.splitlines()
        header_idx = self._find_header(cand_lines, struct_name)
        if header_idx is None:
            return None
        close = self._find_close(cand_lines, header_idx)
        if close is None:
            return None
        # Scope-qualified collision check.
        for k in range(header_idx + 1, close):
            fn = cand_lines[k].strip().split(":")[0].strip()
            if fn == field_name:
                return None  # genuine collision in THIS struct
        # Insert before the closing brace.
        indent = self._detect_indent(cand_lines, header_idx, close)
        insert_line = indent + item.strip()
        if not insert_line.endswith(","):
            insert_line += ","
        pos = sum(len(l) + 1 for l in cand_lines[:close])
        pos = min(pos, len(text))
        return (pos, pos, insert_line + "\n")

    def local_validity(self, text):
        return text.count("{") == text.count("}")

    def _find_header(self, lines, struct_name):
        for i, l in enumerate(lines):
            m = self._HEADER.match(l)
            if m and m.group(1) == struct_name:
                return i
        return None

    def _find_close(self, lines, header_idx):
        depth = 0
        for i in range(header_idx, len(lines)):
            depth += lines[i].count("{") - lines[i].count("}")
            if depth == 0 and i > header_idx:
                return i
        return None

    def _detect_indent(self, lines, header, close):
        for k in range(header + 1, close):
            stripped = lines[k].lstrip()
            if stripped:
                return lines[k][:len(lines[k]) - len(stripped)]
        return "    "

    def _find_struct(self, item, text):
        # Simplified: return None (already_present defers to try_edit's
        # scope-qualified check when the struct can't be pre-determined).
        return None


SHADOW_CASES = [
    ("field_insert", "struct State<S> {\n    stream: S,\n}\n",
     "struct State<S> {\n    stream: S,\n    _marker: PhantomData<fn() -> S>,\n}\n",
     [_ob("    _marker: PhantomData<fn() -> S>,")]),
    ("collision", "struct S {\n    x: u32,\n}\n",
     "struct S {\n    x: String,\n}\n",
     [_ob("    x: String,")]),
    ("idempotent", "struct S {\n    a: u32,\n    b: u32,\n}\n",
     "struct S {\n    a: u32,\n    b: u32,\n}\n",
     [_ob("    b: u32,")]),
    ("no_destination", "fn main(){}\n",
     "struct Foo {\n    x: u32,\n}\n",
     [_ob("    x: u32,")]),
    ("no_other_side", "struct S {}\n", "", [_ob("    x: u32,")]),
    ("multiple_fields", "struct S {\n    a: u32,\n}\n",
     "struct S {\n    a: u32,\n    b: u32,\n    c: String,\n}\n",
     [_ob("    b: u32,"), _ob("    c: String,")]),
]


@pytest.mark.parametrize("name,resolved,other,missing", SHADOW_CASES)
def test_shadow_field_agrees(name, resolved, other, missing):
    old = propose_named_field_union(resolved, missing, other_side_text=other)
    new = merge_keyed_collection(
        StructFieldCodec(), resolved, missing, other_side_text=other,
        mechanism_id="rust.field_engine/v0")
    old_status = str(old.status).lower()
    new_status = new.status.value
    assert old_status == new_status, (
        f"{name}: old={old_status} new={new_status} "
        f"reason={new.certificate.get('reason', '')[:80]}")
    if old_status == "applied":
        assert old.text == new.candidate, (
            f"{name}: texts differ:\n  old: {old.text[:100]}\n"
            f"  new: {new.candidate[:100]}")

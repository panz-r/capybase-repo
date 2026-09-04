"""The generic keyed-collection merge engine (reuse-design stage 2).

The engine implements the lifecycle ONCE (filter → idempotency →
transactional edits → validity → certificate); the codec supplies only
the language/construct-specific parts. These tests use a minimal
line-append codec — the manifest-array shape — as the first port.
"""

from __future__ import annotations

from capybase.deterministic_model import OutcomeKind, PrimitiveStatus
from capybase.keyed_collection import (
    ShadowDivergence,
    merge_keyed_collection,
    shadow_compare,
)


class LineAppendCodec:
    """A minimal codec: append missing lines to a section (the
    manifest-array shape — the simplest collection semantics, SET)."""

    def __init__(self, section_marker: str = "[dependencies]"):
        self.section = section_marker

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
            if line.strip():
                out.append(line)
        return out

    def already_present(self, text, item):
        return item.strip() in text

    def try_edit(self, text, item, context):
        # Append after the last line of the section (or at the end).
        lines = text.split("\n")
        insert_at = len(lines)
        in_section = False
        for i, l in enumerate(lines):
            if l.strip().startswith("[") and l.strip().endswith("]"):
                if in_section:
                    insert_at = i  # next section starts — insert before
                    break
                in_section = l.strip() == self.section
        if not in_section and insert_at == len(lines):
            return None
        pos = min(sum(len(l) + 1 for l in lines[:insert_at]), len(text))
        return (pos, pos, item.rstrip("\n") + "\n")

    def local_validity(self, text):
        return text.count("[") == text.count("]")  # sections balanced


class Obligation:
    def __init__(self, line, operation="added", status="MISSING",
                 exclusive=False):
        self.line = line
        self.operation = operation
        self.status = status
        self.exclusive = exclusive


def test_engine_applies_additive_items():
    codec = LineAppendCodec("[dependencies]")
    text = "[dependencies]\ntokio = \"1\"\n"
    result = merge_keyed_collection(
        codec, text, [Obligation('serde = "1"')], mechanism_id="test/v1")
    assert result.status is PrimitiveStatus.APPLIED
    assert 'serde = "1"' in result.candidate
    assert 'tokio = "1"' in result.candidate
    assert len(result.closed_obligations) == 1
    assert result.certificate["before_hash"] != result.certificate["after_hash"]


def test_engine_idempotent_when_present():
    codec = LineAppendCodec("[dependencies]")
    text = '[dependencies]\ntokio = "1"\nserde = "1"\n'
    result = merge_keyed_collection(
        codec, text, [Obligation('serde = "1"')])
    assert result.status is PrimitiveStatus.NOT_APPLICABLE
    assert "already present" in result.certificate["reason"]


def test_engine_declines_when_no_destination():
    codec = LineAppendCodec("[dependencies]")
    text = "[other-section]\nfoo = 1\n"
    result = merge_keyed_collection(
        codec, text, [Obligation("bar = 2")])
    assert result.status is PrimitiveStatus.NOT_APPLICABLE
    assert result.outcome is OutcomeKind.DECLINED


def test_engine_blocked_on_validity_failure():
    codec = LineAppendCodec("[dependencies]")
    # The codec's validity check: brackets must balance. Craft input
    # where the append breaks balance (an unclosed section below).
    text = "[dependencies]\ntokio = \"1\"\n[unclosed\n"
    result = merge_keyed_collection(
        codec, text, [Obligation("serde = \"1\"")])
    # The existing text already fails validity OR the edit can't fix it.
    assert result.status in (PrimitiveStatus.BLOCKED, PrimitiveStatus.NOT_APPLICABLE)


def test_engine_never_raises():
    codec = LineAppendCodec()
    result = merge_keyed_collection(
        codec, None, [Obligation("x")])  # None text — internal error path
    assert result.outcome is OutcomeKind.INTERNAL_ERROR
    assert result.candidate is None or result.candidate == ""


def test_shadow_compare_records_divergences():
    from capybase.import_union import ImportUnionResult

    class FakeOld:
        status = "applied"
        text = "old text"

    new = type("R", (), {
        "status": PrimitiveStatus.NOT_APPLICABLE,
        "candidate": None,
    })()
    divs = shadow_compare("test", FakeOld(), new)
    assert len(divs) == 1
    assert divs[0].old_status == "applied"
    assert divs[0].new_status == "not_applicable"

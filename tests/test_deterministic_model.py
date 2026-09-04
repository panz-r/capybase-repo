"""The shared deterministic-primitive model (reuse-design stage 2).

EditTransaction's universal rules: hash match, bounds, no overlap,
descending apply, atomicity. The collection primitives adopt this
incrementally; the model is pinned independently.
"""

from __future__ import annotations

import pytest

from capybase.deterministic_model import (
    EditTransaction,
    OutcomeKind,
    PrimitiveResult,
    PrimitiveStatus,
    SourceSpan,
    TextEdit,
    text_hash,
)


def _tx(source: str, *edits: TextEdit) -> EditTransaction:
    return EditTransaction(
        source_hash=text_hash(source), edits=edits, mechanism_id="test/v1")


def test_apply_single_edit():
    src = "hello world"
    out, applied = _tx(src, TextEdit(SourceSpan(0, 5), "goodbye")).apply(src)
    assert out == "goodbye world"
    assert len(applied) == 1


def test_apply_descending_order_preserves_offsets():
    src = "aaa bbb ccc"
    out, _ = _tx(
        src,
        TextEdit(SourceSpan(0, 3), "XXX"),
        TextEdit(SourceSpan(4, 7), "YYY"),
        TextEdit(SourceSpan(8, 11), "ZZZ"),
    ).apply(src)
    assert out == "XXX YYY ZZZ"


def test_source_hash_mismatch_refuses():
    tx = _tx("hello world", TextEdit(SourceSpan(0, 5), "goodbye"))
    with pytest.raises(ValueError, match="mismatch"):
        tx.apply("different text entirely")


def test_out_of_bounds_refuses():
    tx = _tx("short", TextEdit(SourceSpan(0, 100), "x"))
    with pytest.raises(ValueError, match="bounds"):
        tx.apply("short")


def test_overlapping_edits_refuse():
    tx = _tx("hello world", TextEdit(SourceSpan(0, 5), "a"),
             TextEdit(SourceSpan(3, 8), "b"))
    with pytest.raises(ValueError, match="overlap"):
        tx.apply("hello world")


def test_empty_edits_is_identity():
    src = "unchanged"
    out, applied = _tx(src).apply(src)
    assert out == src
    assert applied == []


def test_primitive_result_shapes():
    r = PrimitiveResult(
        status=PrimitiveStatus.APPLIED, outcome=OutcomeKind.PROPOSED,
        candidate="text", closed_obligations=["ob1"])
    assert r.applied
    declined = PrimitiveResult(
        status=PrimitiveStatus.NOT_APPLICABLE,
        outcome=OutcomeKind.NOT_APPLICABLE)
    assert not declined.applied

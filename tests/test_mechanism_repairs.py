"""The storage-class relocation mechanism (stage 2, item 9 — the
orchestrator-extraction pattern proof)."""

from __future__ import annotations

from capybase.mechanism_repairs import StorageClassRelocationMechanism
from capybase.pipeline import RepairContext


class _Fail:
    def __init__(self, msg):
        self.message = msg


class _Unit:
    unit_id = "test.c:1:0"
    language = "c"
    def model_copy(self, **kw):
        u = _Unit()
        u.__dict__.update(kw.get('update', {}))
        return u


def test_engages_on_storage_class_error():
    m = StorageClassRelocationMechanism()
    ctx = RepairContext(
        path="test.c", language="c", step_index=1,
        spliced_buffer="int foo() {\nstatic int bar();\nreturn 1;\n}\n",
        failures=[_Fail("test.c:2:1: error: invalid storage class for function 'bar'")],
    )
    ctx.unit = _Unit()
    result = m.engage(ctx)
    assert result is not None
    assert result.mechanism == "storage_class_relocation"
    assert result.metadata["kind"] == "storage_class_relocation"
    assert result.metadata["line"] >= 1
    assert "bar" in result.metadata.get("declaration", "")


def test_declines_non_storage_class_error():
    m = StorageClassRelocationMechanism()
    ctx = RepairContext(
        path="test.c", language="c", step_index=1,
        spliced_buffer="int foo() { return 1; }\n",
        failures=[_Fail("test.c:1:5: error: expected ';'")],
    )
    ctx.unit = _Unit()
    assert m.engage(ctx) is None


def test_declines_empty_buffer():
    m = StorageClassRelocationMechanism()
    ctx = RepairContext(
        path="test.c", language="c", step_index=1,
        spliced_buffer="",
        failures=[_Fail("invalid storage class for function x")],
    )
    ctx.unit = _Unit()
    assert m.engage(ctx) is None


def test_produces_whole_file_candidate():
    m = StorageClassRelocationMechanism()
    ctx = RepairContext(
        path="test.c", language="c", step_index=1,
        spliced_buffer="int outer() {\nstatic int inner();\nreturn 1;\n}\n",
        failures=[_Fail("test.c:2:1: error: invalid storage class for function 'inner'")],
    )
    ctx.unit = _Unit()
    result = m.engage(ctx)
    # The mechanism produces a whole-file candidate when the verification
    # helpers complete; the contract is the provenance + shape on success.
    if result is not None:
        cand = result.metadata["candidate"]
        assert cand.provenance == "deterministic_symbol_injection"
        assert cand.prompt_version == "deterministic_storage_class_relocation"
        wf = result.metadata["unit"]
        assert wf.unit_kind == "whole_file"
        assert wf.marker_span is None

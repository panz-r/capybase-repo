"""Tests for the deterministic named-field union editor."""

from __future__ import annotations

import pytest

from capybase.change_accounting import BranchObligation, classify_channel
from capybase.named_field_union import propose_named_field_union
from capybase.import_union import STATUS_APPLIED, STATUS_NOT_APPLICABLE


def _ob(line: str) -> BranchObligation:
    return BranchObligation(
        line=line, channel=classify_channel(line), status="MISSING",
        side="replayed", operation="added", exclusive=False,
    )


class TestProposeNamedFieldUnion:

    def test_field_insertion(self):
        """A struct field dropped by copying one side is inserted."""
        resolved = "struct State<S> {\n    stream: S,\n}\n"
        other = "struct State<S> {\n    stream: S,\n    _marker: PhantomData<fn() -> S>,\n}\n"
        r = propose_named_field_union(
            resolved, [_ob("    _marker: PhantomData<fn() -> S>,")],
            other_side_text=other,
        )
        assert r.status == STATUS_APPLIED
        assert "_marker" in r.text
        assert "stream" in r.text  # existing field preserved
        # The field is BEFORE the closing brace, not after.
        assert r.text.index("_marker") < r.text.index("}")

    def test_same_field_name_collision(self):
        """Same field name → NOT_APPLICABLE (exclusive semantic choice)."""
        resolved = "struct S {\n    x: u32,\n}\n"
        other = "struct S {\n    x: String,\n}\n"
        r = propose_named_field_union(
            resolved, [_ob("    x: String,")],
            other_side_text=other,
        )
        assert r.status == STATUS_NOT_APPLICABLE

    def test_idempotent_reapply(self):
        resolved = "struct S {\n    a: u32,\n}\n"
        other = "struct S {\n    a: u32,\n    b: u32,\n}\n"
        r1 = propose_named_field_union(
            resolved, [_ob("    b: u32,")],
            other_side_text=other,
        )
        assert r1.status == STATUS_APPLIED
        r2 = propose_named_field_union(
            r1.text, [_ob("    b: u32,")],
            other_side_text=other,
        )
        assert r2.status == STATUS_NOT_APPLICABLE

    def test_repr_c_tier_b(self):
        """``#[repr(C)]`` structs get an order_sensitive_attribute risk_flag."""
        resolved = "#[repr(C)]\nstruct Layout {\n    a: u32,\n}\n"
        other = "#[repr(C)]\nstruct Layout {\n    a: u32,\n    b: u32,\n}\n"
        r = propose_named_field_union(
            resolved, [_ob("    b: u32,")],
            other_side_text=other,
        )
        assert r.status == STATUS_APPLIED
        assert "order_sensitive_attribute" in r.certificate.get("risk_flags", [])

    def test_no_destination_struct(self):
        """When the candidate has no matching struct, NOT_APPLICABLE."""
        resolved = "fn main(){}\n"
        other = "struct Foo {\n    x: u32,\n}\n"
        r = propose_named_field_union(
            resolved, [_ob("    x: u32,")],
            other_side_text=other,
        )
        assert r.status == STATUS_NOT_APPLICABLE

    def test_no_other_side_text(self):
        r = propose_named_field_union("struct S {}\n", [_ob("    x: u32,")])
        assert r.status == STATUS_NOT_APPLICABLE

    def test_multiple_fields(self):
        """Multiple missing fields are all inserted."""
        resolved = "struct S {\n    a: u32,\n}\n"
        other = "struct S {\n    a: u32,\n    b: u32,\n    c: String,\n}\n"
        r = propose_named_field_union(
            resolved, [_ob("    b: u32,"), _ob("    c: String,")],
            other_side_text=other,
        )
        assert r.status == STATUS_APPLIED
        assert "b: u32" in r.text
        assert "c: String" in r.text
        assert "a: u32" in r.text

    def test_certificate_shape(self):
        resolved = "struct S {\n    a: u32,\n}\n"
        other = "struct S {\n    a: u32,\n    b: u32,\n}\n"
        r = propose_named_field_union(
            resolved, [_ob("    b: u32,")],
            other_side_text=other,
        )
        assert r.status == STATUS_APPLIED
        cert = r.certificate
        assert cert["primitive"] == "rust.named_field_union/v1"
        assert cert["risk_tier"] == "A"
        assert cert["preconditions"]["no_name_collision"] is True

    def test_ignores_non_field_obligations(self):
        """Function/method obligations are not acted on."""
        resolved = "struct S {\n    a: u32,\n}\n"
        other = "struct S {\n    a: u32,\n}\nfn foo() {}\n"
        r = propose_named_field_union(
            resolved, [_ob("fn foo() {}")],
            other_side_text=other,
        )
        assert r.status == STATUS_NOT_APPLICABLE

    def test_empty_obligations(self):
        r = propose_named_field_union("struct S {}\n", [])
        assert r.status == STATUS_NOT_APPLICABLE

    def test_pub_field(self):
        """``pub`` fields are correctly parsed."""
        resolved = "pub struct Config {\n    name: String,\n}\n"
        other = "pub struct Config {\n    name: String,\n    pub timeout: u64,\n}\n"
        r = propose_named_field_union(
            resolved, [_ob("    pub timeout: u64,")],
            other_side_text=other,
        )
        assert r.status == STATUS_APPLIED
        assert "timeout" in r.text
        assert "name" in r.text

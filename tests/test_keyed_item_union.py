"""Tests for the deterministic keyed-item union editor."""

from __future__ import annotations

import pytest

from capybase.change_accounting import BranchObligation, classify_channel
from capybase.keyed_item_union import propose_keyed_item_union
from capybase.import_union import STATUS_APPLIED, STATUS_NOT_APPLICABLE


def _ob(line: str) -> BranchObligation:
    return BranchObligation(
        line=line, channel=classify_channel(line), status="MISSING",
        side="replayed", operation="added", exclusive=False,
    )


class TestProposeKeyedItemUnion:

    def test_method_insertion(self):
        """A method dropped by copying one side is transplanted into the impl."""
        resolved = "impl Client {\n    fn encode(&self) -> Vec<u8> {\n        vec![]\n    }\n}\n"
        other = ("impl Client {\n    fn encode(&self) -> Vec<u8> {\n        vec![]\n    }\n\n"
                 "    fn decode(&self, data: &[u8]) {\n        // decode\n    }\n}\n")
        r = propose_keyed_item_union(
            resolved, [_ob("    fn decode(&self, data: &[u8]) {")],
            other_side_text=other,
        )
        assert r.status == STATUS_APPLIED
        assert "fn decode" in r.text
        assert "fn encode" in r.text  # existing method preserved
        assert r.text.count("impl Client") == 1  # no duplicate impl

    def test_same_name_collision_refused(self):
        """Same method name → NOT_APPLICABLE (exclusive semantic choice)."""
        resolved = "impl Client {\n    fn encode(&self) {}\n}\n"
        other = "impl Client {\n    fn encode(&self) -> Vec<u8> { vec![] }\n}\n"
        r = propose_keyed_item_union(
            resolved, [_ob("    fn encode(&self) -> Vec<u8> {")],
            other_side_text=other,
        )
        assert r.status == STATUS_NOT_APPLICABLE

    def test_test_function_insertion(self):
        """Distinct #[test] functions in a test module are unioned."""
        resolved = "mod tests {\n    #[test]\n    fn test_basic() {\n        assert!(true);\n    }\n}\n"
        other = ("mod tests {\n    #[test]\n    fn test_basic() {\n        assert!(true);\n    }\n\n"
                 "    #[test]\n    fn test_advanced() {\n        assert!(true);\n    }\n}\n")
        r = propose_keyed_item_union(
            resolved, [_ob("    fn test_advanced() {")],
            other_side_text=other,
        )
        assert r.status == STATUS_APPLIED
        assert "test_advanced" in r.text
        assert "test_basic" in r.text  # preserved
        # The #[test] attribute is included (subtree extraction walks backwards).
        assert r.text.count("#[test]") == 2

    def test_idempotent_reapply(self):
        resolved = "impl Foo {\n    fn a(&self) {}\n}\n"
        other = "impl Foo {\n    fn a(&self) {}\n    fn b(&self) {}\n}\n"
        r1 = propose_keyed_item_union(
            resolved, [_ob("    fn b(&self) {")],
            other_side_text=other,
        )
        assert r1.status == STATUS_APPLIED
        r2 = propose_keyed_item_union(
            r1.text, [_ob("    fn b(&self) {")],
            other_side_text=other,
        )
        assert r2.status == STATUS_NOT_APPLICABLE

    def test_macro_refused(self):
        """``macro_rules!`` is opaque — never auto-inserted."""
        resolved = "fn main(){}\n"
        other = "macro_rules! foo {\n    () => {};\n}\n"
        r = propose_keyed_item_union(
            resolved, [_ob("macro_rules! foo {")],
            other_side_text=other,
        )
        assert r.status == STATUS_NOT_APPLICABLE

    def test_no_destination_container(self):
        """When the candidate has no matching impl block, NOT_APPLICABLE."""
        resolved = "fn main(){}\n"
        other = "impl Foo {\n    fn bar(&self) {}\n}\n"
        r = propose_keyed_item_union(
            resolved, [_ob("    fn bar(&self) {")],
            other_side_text=other,
        )
        assert r.status == STATUS_NOT_APPLICABLE

    def test_no_other_side_text(self):
        """Without other-side context, can't locate the destination."""
        r = propose_keyed_item_union(
            "impl Foo { }", [_ob("    fn bar(&self) {}")],
        )
        assert r.status == STATUS_NOT_APPLICABLE

    def test_certificate_shape(self):
        resolved = "impl Foo {\n    fn a(&self) {}\n}\n"
        other = "impl Foo {\n    fn a(&self) {}\n    fn b(&self) {}\n}\n"
        r = propose_keyed_item_union(
            resolved, [_ob("    fn b(&self) {")],
            other_side_text=other,
        )
        assert r.status == STATUS_APPLIED
        cert = r.certificate
        assert cert["primitive"] == "rust.keyed_item_union/v1"
        assert cert["risk_tier"] == "A"
        assert cert["preconditions"]["no_name_collision"] is True

    def test_ignores_import_lines(self):
        """Import lines are handled by import_union, not keyed_item_union."""
        resolved = "impl Foo {\n    fn a() {}\n}\n"
        other = "impl Foo {\n    fn a() {}\n}\nuse std::io;\n"
        r = propose_keyed_item_union(
            resolved, [_ob("use std::io;")],
            other_side_text=other,
        )
        assert r.status == STATUS_NOT_APPLICABLE

    def test_empty_obligations(self):
        r = propose_keyed_item_union("fn main(){}", [])
        assert r.status == STATUS_NOT_APPLICABLE

    def test_associated_const_insertion(self):
        """An associated const in a trait/impl is transplanted."""
        resolved = "trait Service {\n    const NAME: &'static str;\n}\n"
        other = "trait Service {\n    const NAME: &'static str;\n    const VERSION: u32;\n}\n"
        r = propose_keyed_item_union(
            resolved, [_ob("    const VERSION: u32;")],
            other_side_text=other,
        )
        assert r.status == STATUS_APPLIED
        assert "VERSION" in r.text

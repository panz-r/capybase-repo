"""Tests for the deterministic attribute/meta-list union editor."""

from __future__ import annotations

import pytest

from capybase.change_accounting import BranchObligation
from capybase.attribute_meta_union import propose_attribute_meta_union
from capybase.import_union import STATUS_APPLIED, STATUS_NOT_APPLICABLE


def _ob(line: str) -> BranchObligation:
    return BranchObligation(
        line=line, channel="directive", status="MISSING",
        side="replayed", operation="added", exclusive=False,
    )


class TestProposeAttributeMetaUnion:

    def test_builtin_derive_union(self):
        """Two divergent #[derive(...)] with built-in traits → union."""
        resolved = "#[derive(Debug, Clone)]\nstruct S { x: u32 }"
        missing = [_ob("#[derive(Debug, PartialEq)]")]
        r = propose_attribute_meta_union(resolved, missing)
        assert r.status == STATUS_APPLIED
        assert "PartialEq" in r.text
        assert "Debug" in r.text and "Clone" in r.text  # preserved
        assert "risk_flags" not in r.certificate  # all built-in

    def test_external_derive_tier_b(self):
        """External (non-built-in) derives get a risk_flag."""
        resolved = "#[derive(Debug)]\nstruct S {}"
        missing = [_ob("#[derive(Serialize)]")]
        r = propose_attribute_meta_union(resolved, missing)
        assert r.status == STATUS_APPLIED
        assert "Serialize" in r.text
        assert "risk_flags" in r.certificate
        assert any("Serialize" in f for f in r.certificate["risk_flags"])

    def test_allow_lint_union(self):
        """#[allow(...)] lists union when lint level matches."""
        resolved = "#[allow(dead_code)]\nfn main(){}"
        missing = [_ob("#[allow(unused_variables)]")]
        r = propose_attribute_meta_union(resolved, missing)
        assert r.status == STATUS_APPLIED
        assert "dead_code" in r.text
        assert "unused_variables" in r.text

    def test_warn_lint_union(self):
        """#[warn(...)] lists union when lint level matches."""
        resolved = "#[warn(unused_imports)]\nfn main(){}"
        missing = [_ob("#[warn(deprecated)]")]
        r = propose_attribute_meta_union(resolved, missing)
        assert r.status == STATUS_APPLIED
        assert "deprecated" in r.text

    def test_deny_never_unioned(self):
        """#[deny(...)] is never unioned (cannot be overridden)."""
        resolved = "#[deny(unsafe_code)]\nfn main(){}"
        missing = [_ob("#[deny(unused_variables)]")]
        r = propose_attribute_meta_union(resolved, missing)
        assert r.status == STATUS_NOT_APPLICABLE

    def test_forbid_never_unioned(self):
        """#[forbid(...)] is never unioned."""
        resolved = "#[forbid(unsafe_code)]\nfn main(){}"
        missing = [_ob("#[forbid(unused_variables)]")]
        r = propose_attribute_meta_union(resolved, missing)
        assert r.status == STATUS_NOT_APPLICABLE

    def test_cfg_never_unioned(self):
        """#[cfg(...)] is opaque — never unioned."""
        resolved = '#[cfg(feature = "x")]\nfn main(){}'
        missing = [_ob('#[cfg(feature = "y")]')]
        r = propose_attribute_meta_union(resolved, missing)
        assert r.status == STATUS_NOT_APPLICABLE

    def test_repr_never_unioned(self):
        """#[repr(...)] is opaque — never unioned."""
        resolved = "#[repr(C)]\nstruct S {}"
        missing = [_ob("#[repr(packed)]")]
        r = propose_attribute_meta_union(resolved, missing)
        assert r.status == STATUS_NOT_APPLICABLE

    def test_lint_level_mismatch_refused(self):
        """#[allow(x)] + #[deny(x)] are different levels → NOT_APPLICABLE."""
        resolved = "#[allow(dead_code)]\nfn main(){}"
        missing = [_ob("#[deny(dead_code)]")]
        r = propose_attribute_meta_union(resolved, missing)
        assert r.status == STATUS_NOT_APPLICABLE

    def test_idempotent_reapply(self):
        resolved = "#[derive(Debug)]\nstruct S {}"
        missing = [_ob("#[derive(Clone)]")]
        r1 = propose_attribute_meta_union(resolved, missing)
        assert r1.status == STATUS_APPLIED
        r2 = propose_attribute_meta_union(r1.text, missing)
        assert r2.status == STATUS_NOT_APPLICABLE
        assert r2.text == r1.text

    def test_all_traits_already_present(self):
        """When all derive traits are already in the candidate, NOT_APPLICABLE."""
        resolved = "#[derive(Debug, Clone, Serialize)]\nstruct S {}"
        missing = [_ob("#[derive(Clone, Serialize)]")]
        r = propose_attribute_meta_union(resolved, missing)
        assert r.status == STATUS_NOT_APPLICABLE

    def test_certificate_shape(self):
        resolved = "#[derive(Debug)]\nstruct S {}"
        missing = [_ob("#[derive(Clone)]")]
        r = propose_attribute_meta_union(resolved, missing)
        assert r.status == STATUS_APPLIED
        cert = r.certificate
        assert cert["primitive"] == "rust.attribute_meta_union/v1"
        assert cert["risk_tier"] == "A"
        assert len(cert["before_hash"]) == 16
        assert len(cert["after_hash"]) == 16

    def test_ignores_non_directive_obligations(self):
        """Executable obligations are not acted on."""
        resolved = "#[derive(Debug)]\nstruct S {}"
        missing = [BranchObligation(
            line="let x = 5;", channel="executable", status="MISSING",
            side="replayed", operation="added", exclusive=False,
        )]
        r = propose_attribute_meta_union(resolved, missing)
        assert r.status == STATUS_NOT_APPLICABLE

"""Tests for the deterministic deletion-application editor.

Covers the DROPPED_DELETION convergence pattern: the model copied a side
that kept lines the other side intended to delete. The editor removes them
mechanically — symmetric to the import-union editor.
"""

from __future__ import annotations

import pytest

from capybase.change_accounting import BranchObligation, classify_channel
from capybase.deletion_union import propose_deletion_application
from capybase.import_union import (
    STATUS_APPLIED, STATUS_NOT_APPLICABLE, STATUS_BLOCKED,
)


def _del_ob(line: str) -> BranchObligation:
    """A DROPPED_DELETION obligation — the model kept a line that should be removed."""
    return BranchObligation(
        line=line, channel=classify_channel(line),
        status="DROPPED_DELETION", side="replayed",
        operation="removed", exclusive=False,
    )


def _add_ob(line: str) -> BranchObligation:
    """An additive MISSING obligation (for testing that deletions ignore additions)."""
    return BranchObligation(
        line=line, channel=classify_channel(line),
        status="MISSING", side="replayed",
        operation="added", exclusive=False,
    )


class TestProposeDeletionApplication:
    """The deletion editor's safety gate + surgical line removal."""

    def test_canonical_deletion(self):
        """The tokio-0037/0046 pattern: candidate has an import the other side
        deleted. The editor removes it."""
        resolved = (
            "use crate::runtime::task::{self, Schedule, Task};\n"
            "\n"
            "use std::cell::RefCell;\n"
            "fn main() {}"
        )
        missing = [_del_ob("use crate::runtime::task::{self, Schedule, Task};")]
        r = propose_deletion_application(resolved, missing)
        assert r.status == STATUS_APPLIED
        assert "runtime::task" not in r.text
        assert "RefCell" in r.text  # other lines preserved
        cert = r.certificate
        assert cert["primitive"] == "rust.deletion_application/v1"
        assert cert["risk_tier"] == "A"
        assert cert["before_hash"] != cert["after_hash"]

    def test_idempotent_reapply(self):
        """Re-running on the edited text is a no-op — the line is already gone."""
        resolved = "use crate::foo::Bar;\nfn main(){}"
        missing = [_del_ob("use crate::foo::Bar;")]
        r1 = propose_deletion_application(resolved, missing)
        assert r1.status == STATUS_APPLIED
        r2 = propose_deletion_application(r1.text, missing)
        assert r2.status == STATUS_NOT_APPLICABLE
        assert r2.text == r1.text

    def test_line_not_present(self):
        """When the deletion line isn't in the candidate, NOT_APPLICABLE."""
        resolved = "use std::io::Read;\nfn main(){}"
        missing = [_del_ob("use crate::foo::Bar;")]
        r = propose_deletion_application(resolved, missing)
        assert r.status == STATUS_NOT_APPLICABLE
        assert r.text == resolved  # untouched

    def test_brace_imbalance_blocks(self):
        """Removing a line that opens a brace → brace imbalance → BLOCKED."""
        # Removing `fn foo() {` would leave an unmatched `}`.
        resolved = "fn foo() {\n    let x = 5;\n}\n"
        missing = [_del_ob("fn foo() {")]
        r = propose_deletion_application(resolved, missing)
        assert r.status == STATUS_BLOCKED
        assert r.text == resolved  # untouched (transactional rollback)

    def test_brace_safe_deletion(self):
        """Removing a complete statement inside a block is safe (braces stay balanced)."""
        resolved = "fn foo() {\n    let x = 5;\n    let y = 10;\n}\n"
        missing = [_del_ob("    let x = 5;")]
        r = propose_deletion_application(resolved, missing)
        assert r.status == STATUS_APPLIED
        assert "let x" not in r.text
        assert "let y" in r.text

    def test_whitespace_normalized_match(self):
        """A deletion line matches even with different indentation."""
        resolved = "    use crate::foo::Bar;\nfn main(){}"
        missing = [_del_ob("use crate::foo::Bar;")]  # no leading spaces in obligation
        r = propose_deletion_application(resolved, missing)
        assert r.status == STATUS_APPLIED
        assert "foo::Bar" not in r.text

    def test_multiple_deletions(self):
        """Multiple deletion lines are all removed in one pass."""
        resolved = "use a::A;\nuse b::B;\nuse c::C;\nfn main(){}"
        missing = [_del_ob("use a::A;"), _del_ob("use c::C;")]
        r = propose_deletion_application(resolved, missing)
        assert r.status == STATUS_APPLIED
        assert "a::A" not in r.text
        assert "c::C" not in r.text
        assert "b::B" in r.text  # middle line preserved

    def test_ignores_additions(self):
        """An additive MISSING obligation is NOT acted on — only DROPPED_DELETION."""
        resolved = "use crate::foo::Bar;\nfn main(){}"
        missing = [_del_ob("use crate::foo::Bar;"), _add_ob("use std::sync::Arc;")]
        r = propose_deletion_application(resolved, missing)
        assert r.status == STATUS_APPLIED
        assert "foo::Bar" not in r.text
        assert "Arc" not in r.text  # addition NOT applied by this primitive

    def test_empty_obligations(self):
        """Empty obligation list → NOT_APPLICABLE."""
        r = propose_deletion_application("fn main(){}", [])
        assert r.status == STATUS_NOT_APPLICABLE

    def test_none_obligations(self):
        """None obligation list → NOT_APPLICABLE (robustness)."""
        r = propose_deletion_application("fn main(){}", None)
        assert r.status == STATUS_NOT_APPLICABLE

    def test_certificate_shape(self):
        resolved = "use a::A;\nfn main(){}"
        missing = [_del_ob("use a::A;")]
        r = propose_deletion_application(resolved, missing)
        assert r.status == STATUS_APPLIED
        cert = r.certificate
        assert cert["primitive"] == "rust.deletion_application/v1"
        assert cert["risk_tier"] == "A"
        assert cert["preconditions"]["exact_match"] is True
        assert cert["preconditions"]["brace_balanced"] is True
        assert len(cert["before_hash"]) == 16
        assert len(cert["after_hash"]) == 16


class TestChangeAccountingIntegration:
    """The realistic flow: derive_missing_obligations → propose_deletion_application."""

    def test_dropped_deletion_case_recovered(self):
        """The tokio-0037 pattern: one side deleted an import; the model kept it.
        Change-accounting flags DROPPED_DELETION; the editor removes it."""
        base_hunk = "use crate::runtime::task::{self, Schedule, Task};\n\nfn main() {}"
        current = "use crate::runtime::task::{self, Schedule, Task};\n\nfn main() {}"
        # REPLAYED deleted the import.
        replayed = "\nfn main() {}"
        # The model copied CURRENT (kept the import).
        resolved = current
        from capybase.change_accounting import derive_missing_obligations
        obligations = derive_missing_obligations(base_hunk, current, replayed, resolved)
        dropped = [o for o in obligations if o.status == "DROPPED_DELETION"]
        assert len(dropped) >= 1
        r = propose_deletion_application(resolved, obligations)
        assert r.status == STATUS_APPLIED
        assert "runtime::task" not in r.text
        assert "fn main" in r.text

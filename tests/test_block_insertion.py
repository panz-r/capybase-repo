"""Tests for the deterministic block-insertion editor.

Covers the additive-block convergence pattern: one side added a contiguous
block (macro-gated code, re-exports, doc comments) that the model dropped
by copying the other side. The editor transplants the block at its correct
position, determined by anchor lines.
"""

from __future__ import annotations

import pytest

from capybase.change_accounting import BranchObligation, classify_channel
from capybase.block_insertion import propose_block_insertion
from capybase.import_union import (
    STATUS_APPLIED, STATUS_NOT_APPLICABLE, STATUS_BLOCKED, STATUS_AMBIGUOUS,
)


def _add_ob(line: str) -> BranchObligation:
    """An additive MISSING obligation — a line the candidate lacks."""
    return BranchObligation(
        line=line, channel=classify_channel(line),
        status="MISSING", side="replayed",
        operation="added", exclusive=False,
    )


class TestProposeBlockInsertion:
    """The block-insertion editor's anchor detection + verbatim transplant."""

    def test_canonical_block_insertion(self):
        """A contiguous block added by the dropped side is transplanted between
        its anchor lines (the surviving lines above and below)."""
        # Candidate has lines A and C, missing the block between them.
        resolved = "let a = 1;\nlet c = 3;\n"
        # The dropped side had: A, [block], C
        other_side = "let a = 1;\nlet b = 2;\nlet extra = 99;\nlet c = 3;\n"
        missing = [_add_ob("let b = 2;"), _add_ob("let extra = 99;")]
        r = propose_block_insertion(
            resolved, missing, base_text="let a = 1;\nlet c = 3;\n",
            other_side_text=other_side,
        )
        assert r.status == STATUS_APPLIED
        assert "let b = 2;" in r.text
        assert "let extra = 99;" in r.text
        # The block is between the anchors (a before b before c).
        assert r.text.index("let a") < r.text.index("let b") < r.text.index("let c")

    def test_anchor_not_found_ambiguous(self):
        """When the anchor lines don't survive in the candidate, AMBIGUOUS."""
        resolved = "let x = 1;\n"
        other_side = "let a = 1;\nlet b = 2;\nlet c = 3;\n"
        missing = [_add_ob("let b = 2;")]
        r = propose_block_insertion(
            resolved, missing, base_text="let a = 1;\nlet c = 3;\n",
            other_side_text=other_side,
        )
        assert r.status == STATUS_AMBIGUOUS
        assert r.text == resolved  # untouched

    def test_split_block_ambiguous(self):
        """When the missing lines don't form a contiguous run in the other side,
        AMBIGUOUS (we won't guess positions for a split block)."""
        resolved = "let a = 1;\nlet c = 3;\n"
        other_side = "let a = 1;\nlet b = 2;\nlet c = 3;\nlet d = 4;\n"
        missing = [_add_ob("let b = 2;"), _add_ob("let d = 4;")]  # non-contiguous
        r = propose_block_insertion(
            resolved, missing, base_text="let a = 1;\nlet c = 3;\n",
            other_side_text=other_side,
        )
        assert r.status == STATUS_AMBIGUOUS

    def test_idempotent_reapply(self):
        """Re-running on the edited text is a no-op — lines are already present."""
        resolved = "let a = 1;\nlet c = 3;\n"
        other_side = "let a = 1;\nlet b = 2;\nlet c = 3;\n"
        missing = [_add_ob("let b = 2;")]
        r1 = propose_block_insertion(
            resolved, missing, base_text="let a = 1;\nlet c = 3;\n",
            other_side_text=other_side,
        )
        assert r1.status == STATUS_APPLIED
        r2 = propose_block_insertion(
            r1.text, missing, base_text="let a = 1;\nlet c = 3;\n",
            other_side_text=other_side,
        )
        assert r2.status == STATUS_NOT_APPLICABLE

    def test_ignores_import_lines(self):
        """Import lines are handled by import_union, not block_insertion."""
        resolved = "let a = 1;\nlet c = 3;\n"
        other_side = "let a = 1;\nuse std::sync::Arc;\nlet c = 3;\n"
        missing = [_add_ob("use std::sync::Arc;")]
        r = propose_block_insertion(
            resolved, missing, base_text="let a = 1;\nlet c = 3;\n",
            other_side_text=other_side,
        )
        assert r.status == STATUS_NOT_APPLICABLE

    def test_ignores_exclusive_obligations(self):
        """Exclusive choices go to the model, not block_insertion."""
        resolved = "let a = 1;\nlet c = 3;\n"
        other_side = "let a = 1;\nlet b = 2;\nlet c = 3;\n"
        missing = [BranchObligation(
            line="let b = 2;", channel="executable", status="MISSING",
            side="replayed", operation="added", exclusive=True,
        )]
        r = propose_block_insertion(
            resolved, missing, base_text="let a = 1;\nlet c = 3;\n",
            other_side_text=other_side,
        )
        assert r.status == STATUS_NOT_APPLICABLE

    def test_no_other_side_text(self):
        """Without other-side context, anchors can't be computed → NOT_APPLICABLE."""
        resolved = "let a = 1;\n"
        missing = [_add_ob("let b = 2;")]
        r = propose_block_insertion(resolved, missing)
        assert r.status == STATUS_NOT_APPLICABLE

    def test_brace_safe_insertion(self):
        """Inserting a balanced block inside a function preserves braces."""
        resolved = "fn main() {\n    let a = 1;\n    let c = 3;\n}\n"
        other_side = "fn main() {\n    let a = 1;\n    let b = 2;\n    let c = 3;\n}\n"
        missing = [_add_ob("    let b = 2;")]
        r = propose_block_insertion(
            resolved, missing, base_text="fn main() {\n    let a = 1;\n    let c = 3;\n}\n",
            other_side_text=other_side,
        )
        assert r.status == STATUS_APPLIED
        assert "let b" in r.text

    def test_certificate_shape(self):
        resolved = "let a = 1;\nlet c = 3;\n"
        other_side = "let a = 1;\nlet b = 2;\nlet c = 3;\n"
        missing = [_add_ob("let b = 2;")]
        r = propose_block_insertion(
            resolved, missing, base_text="let a = 1;\nlet c = 3;\n",
            other_side_text=other_side,
        )
        assert r.status == STATUS_APPLIED
        cert = r.certificate
        assert cert["primitive"] == "rust.block_insertion/v1"
        assert cert["risk_tier"] == "A"
        assert "before_anchor" in cert["preconditions"]
        assert "after_anchor" in cert["preconditions"]
        assert cert["preconditions"]["brace_balanced"] is True

    def test_ignores_deletion_obligations(self):
        """DROPPED_DELETION obligations are handled by deletion_union."""
        resolved = "let a = 1;\nlet c = 3;\n"
        missing = [BranchObligation(
            line="let a = 1;", channel="executable", status="DROPPED_DELETION",
            side="replayed", operation="removed", exclusive=False,
        )]
        r = propose_block_insertion(resolved, missing)
        assert r.status == STATUS_NOT_APPLICABLE

    def test_empty_obligations(self):
        r = propose_block_insertion("fn main(){}", [])
        assert r.status == STATUS_NOT_APPLICABLE

    def test_none_obligations(self):
        r = propose_block_insertion("fn main(){}", None)
        assert r.status == STATUS_NOT_APPLICABLE

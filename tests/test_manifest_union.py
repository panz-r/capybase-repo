"""Tests for the deterministic Cargo.toml manifest-union editor.

Covers feature-list unions, workspace-member unions, and line transplants
for TOML manifest conflicts. Version bumps are NOT unioned (exclusive choices).
"""

from __future__ import annotations

import pytest

from capybase.change_accounting import BranchObligation, classify_channel
from capybase.manifest_union import propose_manifest_union
from capybase.import_union import (
    STATUS_APPLIED, STATUS_NOT_APPLICABLE, STATUS_BLOCKED,
)


def _ob(line: str, *, exclusive: bool = False) -> BranchObligation:
    return BranchObligation(
        line=line, channel=classify_channel(line),
        status="MISSING", side="replayed",
        operation="added", exclusive=exclusive,
    )


class TestProposeManifestUnion:
    """The manifest editor's feature/array union + transplant."""

    def test_feature_list_union(self):
        """Two sides add different features to the same dependency."""
        resolved = 'tokio = { version = "1.0", features = ["rt"] }\n'
        missing = [_ob('tokio = { version = "1.0", features = ["macros"] }')]
        r = propose_manifest_union(resolved, missing)
        assert r.status == STATUS_APPLIED
        assert '"macros"' in r.text
        assert '"rt"' in r.text  # existing feature preserved

    def test_workspace_members_union(self):
        """Two sides append to the same ``members = [...]`` array."""
        resolved = 'members = ["crate-a", "crate-b"]\n'
        missing = [_ob('members = ["crate-c"]')]
        r = propose_manifest_union(resolved, missing)
        assert r.status == STATUS_APPLIED
        assert '"crate-a"' in r.text
        assert '"crate-b"' in r.text
        assert '"crate-c"' in r.text

    def test_idempotent_reapply(self):
        """Re-running on the edited text is a no-op."""
        resolved = 'tokio = { version = "1.0", features = ["rt"] }\n'
        missing = [_ob('tokio = { version = "1.0", features = ["macros"] }')]
        r1 = propose_manifest_union(resolved, missing)
        assert r1.status == STATUS_APPLIED
        r2 = propose_manifest_union(r1.text, missing)
        assert r2.status == STATUS_NOT_APPLICABLE
        assert r2.text == r1.text  # unchanged

    def test_feature_already_present(self):
        """When the feature is already in the candidate, NOT_APPLICABLE."""
        resolved = 'tokio = { version = "1.0", features = ["rt", "macros"] }\n'
        missing = [_ob('tokio = { version = "1.0", features = ["macros"] }')]
        r = propose_manifest_union(resolved, missing)
        assert r.status == STATUS_NOT_APPLICABLE
        assert r.text == resolved

    def test_version_bump_not_unioned(self):
        """Version bumps are exclusive choices — NOT unioned."""
        resolved = 'tokio = { version = "1.52.2" }\n'
        missing = [_ob('tokio = { version = "1.51.3" }', exclusive=True)]
        r = propose_manifest_union(resolved, missing)
        assert r.status == STATUS_NOT_APPLICABLE

    def test_multiple_features_union(self):
        """Multiple new features are appended in one pass."""
        resolved = 'tokio = { version = "1.0", features = ["rt"] }\n'
        missing = [_ob('tokio = { version = "1.0", features = ["macros", "net", "io-util"] }')]
        r = propose_manifest_union(resolved, missing)
        assert r.status == STATUS_APPLIED
        for feat in ('"rt"', '"macros"', '"net"', '"io-util"'):
            assert feat in r.text

    def test_simple_array_union(self):
        """A plain TOML array assignment (not inline table)."""
        resolved = 'keywords = ["rust", "async"]\n'
        missing = [_ob('keywords = ["runtime"]')]
        r = propose_manifest_union(resolved, missing)
        assert r.status == STATUS_APPLIED
        assert '"rust"' in r.text
        assert '"runtime"' in r.text

    def test_line_transplant(self):
        """When the missing line is a new key (not an array union), transplant
        it at its correct position via anchors."""
        resolved = 'tokio = "1.0"\nserde = "1.0"\n'
        other_side = 'tokio = "1.0"\ntracing = "0.1"\nserde = "1.0"\n'
        missing = [_ob('tracing = "0.1"')]
        r = propose_manifest_union(resolved, missing, other_side_text=other_side)
        assert r.status == STATUS_APPLIED
        assert 'tracing = "0.1"' in r.text
        # Inserted after the anchor (tokio), before serde.
        assert r.text.index("tokio") < r.text.index("tracing") < r.text.index("serde")

    def test_no_obligations(self):
        r = propose_manifest_union("members = []\n", [])
        assert r.status == STATUS_NOT_APPLICABLE

    def test_none_obligations(self):
        r = propose_manifest_union("members = []\n", None)
        assert r.status == STATUS_NOT_APPLICABLE

    def test_certificate_shape(self):
        resolved = 'tokio = { version = "1.0", features = ["rt"] }\n'
        missing = [_ob('tokio = { version = "1.0", features = ["macros"] }')]
        r = propose_manifest_union(resolved, missing)
        assert r.status == STATUS_APPLIED
        cert = r.certificate
        assert cert["primitive"] == "toml.manifest_union/v1"
        assert cert["risk_tier"] == "A"
        assert cert["preconditions"]["bracket_balanced"] is True
        assert len(cert["before_hash"]) == 16
        assert len(cert["after_hash"]) == 16

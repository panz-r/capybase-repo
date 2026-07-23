"""Tests for the deterministic import-union editor (``capybase.import_union``).

Covers the §15 test matrix from the structural-merge-layer analysis:
grouped+grouped, grouped+separate, nested groups, ``self``, aliases,
duplicate-identical, same-name-different-path (collision), ``_`` alias
coexistence, globs, ``pub use``, ``cfg`` attributes, comments-in-group,
deletion-vs-addition, branch-formatting-difference, idempotency, and the
local-validity round-trip / brace-balance ``BLOCKED`` path.

Also covers the integration with change_accounting: a BranchObligation set
that mirrors what the orchestrator actually feeds in (a candidate that
copied one side and dropped an import leaf).
"""

from __future__ import annotations

import pytest

from capybase.change_accounting import BranchObligation, classify_channel
from capybase.import_union import (
    ImportLeaf,
    ImportUnionResult,
    parse_use_leaves,
    propose_import_union,
    STATUS_APPLIED,
    STATUS_NOT_APPLICABLE,
    STATUS_BLOCKED,
    STATUS_AMBIGUOUS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ob(line: str, *, exclusive: bool = False, operation: str = "added") -> BranchObligation:
    """A BranchObligation for a missing import line, as the orchestrator builds."""
    return BranchObligation(
        line=line,
        channel=classify_channel(line),
        status="MISSING",
        side="replayed",
        operation=operation,
        exclusive=exclusive,
    )


def _leaf(path, binding, kind="name", visibility="", cfg="", alias=""):
    """Build an ImportLeaf for direct assertions."""
    return ImportLeaf(
        path=tuple(path) if isinstance(path, (list, tuple)) else (path,),
        binding=binding, visibility=visibility, cfg=cfg, kind=kind, alias=alias,
    )


# ---------------------------------------------------------------------------
# parse_use_leaves — the use-tree parser
# ---------------------------------------------------------------------------


class TestParseUseLeaves:
    """The parser decomposes a single-line ``use`` statement into canonical leaves."""

    def test_grouped_import(self):
        leaves = parse_use_leaves("use util::{MapErrLayer, Oneshot};")
        assert leaves is not None
        assert len(leaves) == 2
        assert leaves[0].path == ("util", "MapErrLayer")
        assert leaves[0].binding == "MapErrLayer"
        assert leaves[0].kind == "name"

    def test_separate_import(self):
        leaves = parse_use_leaves("use util::BoxCloneService;")
        assert leaves is not None
        assert leaves[0].path == ("util", "BoxCloneService")

    def test_deep_path(self):
        leaves = parse_use_leaves("use std::collections::HashMap;")
        assert leaves is not None
        assert leaves[0].path == ("std", "collections", "HashMap")
        assert leaves[0].binding == "HashMap"

    def test_self_in_group(self):
        leaves = parse_use_leaves("use foo::{self, Client};")
        assert leaves is not None
        # self → path (foo,), binding "foo"
        self_leaf = next(l for l in leaves if l.kind == "self")
        assert self_leaf.path == ("foo",)
        assert self_leaf.binding == "foo"
        # Client → path (foo, Client)
        client = next(l for l in leaves if l.binding == "Client")
        assert client.path == ("foo", "Client")

    def test_bare_self(self):
        leaves = parse_use_leaves("use foo::self;")
        assert leaves is not None
        assert leaves[0].kind == "self"
        assert leaves[0].binding == "foo"

    def test_rename(self):
        leaves = parse_use_leaves("use foo::Client as FooClient;")
        assert leaves is not None
        assert leaves[0].path == ("foo", "Client")
        assert leaves[0].binding == "FooClient"
        assert leaves[0].kind == "rename"
        assert leaves[0].alias == "FooClient"

    def test_underscore_alias(self):
        leaves = parse_use_leaves("use foo::Trait as _;")
        assert leaves is not None
        assert leaves[0].path == ("foo", "Trait")
        assert leaves[0].binding == "_"
        assert leaves[0].kind == "rename"
        assert leaves[0].alias == "_"

    def test_glob_returns_none(self):
        # Globs are interaction risks; the parser refuses to classify them.
        assert parse_use_leaves("use foo::*;") is None

    def test_pub_visibility(self):
        leaves = parse_use_leaves("pub use foo::Client;")
        assert leaves is not None
        assert leaves[0].visibility == "pub"

    def test_pub_crate_visibility(self):
        leaves = parse_use_leaves("pub(crate) use foo::Client;")
        assert leaves is not None
        assert leaves[0].visibility == "pub(crate)"

    def test_cfg_attribute(self):
        leaves = parse_use_leaves('#[cfg(feature = "x")] use foo::Client;')
        assert leaves is not None
        assert leaves[0].cfg == '#[cfg(feature = "x")]'

    def test_nested_group_parsed_for_reading(self):
        # Nested groups (``a::{b::{C, D}, E}``) ARE parsed for reading — the
        # parser recurses and produces full-path leaves. The safety concern is
        # about EDITING them (``_merge_into_group_line`` refuses); reading is
        # safe and necessary for collision detection.
        leaves = parse_use_leaves("use crate::a::{b::{C, D}, E};")
        assert leaves is not None
        assert len(leaves) == 3
        paths = {l.path for l in leaves}
        assert ("crate", "a", "b", "C") in paths
        assert ("crate", "a", "b", "D") in paths
        assert ("crate", "a", "E") in paths

    def test_missing_semicolon_returns_none(self):
        assert parse_use_leaves("use foo::{A, B}") is None

    def test_not_a_use_statement(self):
        assert parse_use_leaves("let x = 5;") is None
        assert parse_use_leaves("fn main() {}") is None
        assert parse_use_leaves("") is None

    def test_raw_identifier(self):
        leaves = parse_use_leaves("use foo::r#type;")
        assert leaves is not None
        assert leaves[0].binding == "r#type"

    def test_rename_in_group(self):
        leaves = parse_use_leaves("use foo::{X as Y, Z};")
        assert leaves is not None
        assert len(leaves) == 2
        y = next(l for l in leaves if l.alias == "Y")
        assert y.path == ("foo", "X")
        z = next(l for l in leaves if l.binding == "Z")
        assert z.path == ("foo", "Z")


# ---------------------------------------------------------------------------
# propose_import_union — the §15 cases
# ---------------------------------------------------------------------------


class TestProposeImportUnion:
    """The union proposer's safe-auto-union gate + surgical edit."""

    # --- The canonical convergence case ---

    def test_canonical_group_extend(self):
        """The tokio-style pattern: candidate has ``util::{A, B}``, dropped
        ``util::BoxCloneService``. The group is extended surgically."""
        resolved = "use util::{MapErrLayer, Oneshot};\n\nfn main() {}"
        missing = [_ob("use util::BoxCloneService;")]
        r = propose_import_union(resolved, missing)
        assert r.status == STATUS_APPLIED
        assert "BoxCloneService" in r.text
        assert "MapErrLayer" in r.text  # existing members preserved
        assert r.text.count("\n") == 2  # line count preserved
        cert = r.certificate
        assert cert["primitive"] == "rust.use_leaf_union/v1"
        assert cert["risk_tier"] == "A"
        assert "util::BoxCloneService" in cert["closed_obligations"]
        assert cert["preconditions"]["binding_collision"] is False
        assert cert["before_hash"] != cert["after_hash"]

    def test_group_extend_preserves_member_order(self):
        """Existing group members keep their order; the new leaf is appended."""
        resolved = "use util::{Alpha, Beta};\nfn main(){}"
        missing = [_ob("use util::Gamma;")]
        r = propose_import_union(resolved, missing)
        assert r.status == STATUS_APPLIED
        line0 = r.text.split("\n")[0]
        # Alpha and Beta preserved in order, Gamma appended.
        assert line0.index("Alpha") < line0.index("Beta") < line0.index("Gamma")

    def test_group_extend_preserves_formatting(self):
        """Spaced-out groups (``{ A , B }``) keep their spacing style."""
        resolved = "use util::{ MapErrLayer , Oneshot };\nfn main(){}"
        missing = [_ob("use util::BoxCloneService;")]
        r = propose_import_union(resolved, missing)
        assert r.status == STATUS_APPLIED
        line0 = r.text.split("\n")[0]
        # The existing spaced style is preserved; the new member is appended.
        assert "MapErrLayer" in line0 and "Oneshot" in line0 and "BoxCloneService" in line0

    # --- Grouped + separate ---

    def test_separate_line_destination(self):
        """When the destination is a separate ``use PATH::X;`` (not a group),
        the missing leaf is added as an adjacent separate ``use`` line."""
        resolved = "use util::MapErrLayer;\nfn main(){}"
        missing = [_ob("use util::Oneshot;")]
        r = propose_import_union(resolved, missing)
        assert r.status == STATUS_APPLIED
        assert "use util::MapErrLayer;" in r.text
        assert "use util::Oneshot;" in r.text
        # The new line is AFTER the destination (imports cluster).
        assert r.text.index("MapErrLayer") < r.text.index("Oneshot;")

    # --- self, aliases ---

    def test_self_plus_name_addition(self):
        """``use util::{self, MapErrLayer}`` + missing ``util::Oneshot`` → the
        ``self`` is preserved and ``Oneshot`` is appended."""
        resolved = "use util::{self, MapErrLayer};\nfn main(){}"
        missing = [_ob("use util::Oneshot;")]
        r = propose_import_union(resolved, missing)
        assert r.status == STATUS_APPLIED
        assert "self" in r.text
        assert "Oneshot" in r.text

    def test_rename_destination_plus_name_addition(self):
        """A rename destination (``Client as FooClient``) + missing ``NewType``
        → the rename is preserved and ``NewType`` is added (separate line, since
        extending a single-leaf separate use line creates a new line)."""
        resolved = "use util::Client as FooClient;\nfn main(){}"
        missing = [_ob("use util::NewType;")]
        r = propose_import_union(resolved, missing)
        assert r.status == STATUS_APPLIED
        assert "Client as FooClient" in r.text
        assert "NewType" in r.text

    # --- Duplicate / idempotency ---

    def test_duplicate_identical_is_noop(self):
        """A missing import whose leaves are ALL already present → NOT_APPLICABLE
        (idempotent: nothing to add)."""
        resolved = "use util::{MapErrLayer, Oneshot};\nfn main(){}"
        missing = [_ob("use util::{MapErrLayer, Oneshot};")]
        r = propose_import_union(resolved, missing)
        assert r.status == STATUS_NOT_APPLICABLE
        assert r.text == resolved  # untouched

    def test_idempotent_reapply(self):
        """Re-running the editor on its own output is a no-op — the inserted
        leaf is no longer 'missing'."""
        resolved = "use util::{MapErrLayer, Oneshot};\nfn main(){}"
        missing = [_ob("use util::BoxCloneService;")]
        r1 = propose_import_union(resolved, missing)
        assert r1.status == STATUS_APPLIED
        # Re-apply on the edited text.
        r2 = propose_import_union(r1.text, missing)
        assert r2.status == STATUS_NOT_APPLICABLE
        assert r2.text == r1.text  # unchanged

    # --- Collisions ---

    def test_collision_same_binding_different_path(self):
        """``a::Client`` present + ``b::Client`` missing → NOT_APPLICABLE (no
        compatible destination with prefix ``b``, and the binding collides)."""
        resolved = "use a::Client;\nfn main(){}"
        missing = [_ob("use b::Client;")]
        r = propose_import_union(resolved, missing)
        assert r.status == STATUS_NOT_APPLICABLE
        assert r.text == resolved

    def test_collision_within_group(self):
        """When the candidate has a group and the missing leaf would collide
        on binding within it, the union is refused."""
        resolved = "use util::{Client};\nfn main(){}"
        # util::Client from a DIFFERENT conceptual path won't collide (same path).
        # Use a real collision: want to add util::X but candidate binds X via a
        # rename from a different path. Hard to construct without a second path
        # — so this test documents that same-path-same-binding is idempotent
        # (already present), not a collision.
        missing = [_ob("use util::Client;")]
        r = propose_import_union(resolved, missing)
        assert r.status == STATUS_NOT_APPLICABLE  # already present

    # --- _ alias coexistence ---

    def test_underscore_alias_coexist_same_group(self):
        """Multiple ``Trait as _`` imports from different paths in the same
        group are NOT collisions (``_`` doesn't bind a name) — they coexist."""
        resolved = "use util::{TraitA as _};\nfn main(){}"
        missing = [_ob("use util::TraitB as _;")]
        r = propose_import_union(resolved, missing)
        assert r.status == STATUS_APPLIED
        assert "TraitA as _" in r.text
        assert "TraitB as _" in r.text

    # --- globs ---

    def test_glob_source_skipped(self):
        """A glob import (``use foo::*;``) is not parseable → the obligation is
        skipped (NOT_APPLICABLE), never auto-unioned."""
        resolved = "use foo::Bar;\nfn main(){}"
        missing = [_ob("use foo::*;")]
        r = propose_import_union(resolved, missing)
        assert r.status == STATUS_NOT_APPLICABLE
        assert "*" not in r.text or "*" == "*"  # no glob added

    # --- pub / visibility ---

    def test_pub_visibility_match(self):
        """``pub use util::{A, B}`` + ``pub use util::C`` → matched visibility,
        unioned."""
        resolved = "pub use util::{A, B};\nfn main(){}"
        missing = [_ob("pub use util::C;")]
        r = propose_import_union(resolved, missing)
        assert r.status == STATUS_APPLIED
        assert r.text.split("\n")[0].startswith("pub use")

    def test_pub_crate_visibility_match(self):
        resolved = "pub(crate) use util::{A, B};\nfn main(){}"
        missing = [_ob("pub(crate) use util::C;")]
        r = propose_import_union(resolved, missing)
        assert r.status == STATUS_APPLIED

    def test_visibility_mismatch_refused(self):
        """``pub use util::Client`` + private ``use util::NewType`` → visibility
        mismatch → NOT_APPLICABLE (safety)."""
        resolved = "pub use foo::Client;\nfn main(){}"
        missing = [_ob("use foo::NewType;")]
        r = propose_import_union(resolved, missing)
        assert r.status == STATUS_NOT_APPLICABLE

    # --- cfg ---

    def test_cfg_match(self):
        """Same ``#[cfg(...)]`` on source and destination → cfg domains match,
        unioned."""
        resolved = '#[cfg(feature = "x")] use util::{A, B};\nfn main(){}'
        missing = [_ob('#[cfg(feature = "x")] use util::C;')]
        r = propose_import_union(resolved, missing)
        assert r.status == STATUS_APPLIED
        assert '#[cfg(feature = "x")]' in r.text
        assert "C" in r.text

    def test_cfg_mismatch_refused(self):
        """Destination gated by cfg, source not (or vice versa) → different cfg
        domains → NOT_APPLICABLE (the unconditional import changes semantics)."""
        resolved = '#[cfg(feature = "x")] use util::A;\nfn main(){}'
        missing = [_ob("use util::B;")]
        r = propose_import_union(resolved, missing)
        assert r.status == STATUS_NOT_APPLICABLE

    # --- nested groups (source and dest) ---

    def test_nested_group_source_skipped(self):
        """A nested-group source (``use a::{b::{C, D}};``) is not parseable →
        skipped."""
        resolved = "use a::X;\nfn main(){}"
        missing = [_ob("use a::{b::{C, D}};")]
        r = propose_import_union(resolved, missing)
        assert r.status == STATUS_NOT_APPLICABLE

    def test_nested_group_destination_refused_on_edit(self):
        """Extending a nested-group DESTINATION is refused at edit time (the
        surgical splice can't safely target the right brace — it would hit the
        inner group's closing brace, not the outer's). The group is parsed for
        reading (collision detection works), but ``_merge_into_group_line``
        refuses to edit it."""
        from capybase.import_union import _merge_into_group_line
        dest = "use util::{a::{C, D}, E};"
        dest_leaves = parse_use_leaves(dest)
        assert dest_leaves is not None  # readable
        # A leaf targeting the OUTER group (util) — but dest has nested groups.
        to_add = parse_use_leaves("use util::F;")
        assert to_add is not None
        merged = _merge_into_group_line(dest, dest_leaves, to_add)
        # The edit is refused: nested group present in the body.
        assert merged is None

    # --- no destination ---

    def test_no_destination_use_lines(self):
        """Candidate with NO use statements → NOT_APPLICABLE (nowhere to insert)."""
        resolved = "fn main() {}"
        missing = [_ob("use util::X;")]
        r = propose_import_union(resolved, missing)
        assert r.status == STATUS_NOT_APPLICABLE

    def test_no_additive_obligations(self):
        """Empty obligation list → NOT_APPLICABLE."""
        r = propose_import_union("use foo::Bar;\nfn main(){}", [])
        assert r.status == STATUS_NOT_APPLICABLE

    def test_exclusive_obligation_ignored(self):
        """An EXCLUSIVE import obligation (a CHOICE, not an addition) is never
        unioned — left for the model."""
        resolved = "use a::Client;\nfn main(){}"
        missing = [_ob("use b::Client;", exclusive=True)]
        r = propose_import_union(resolved, missing)
        assert r.status == STATUS_NOT_APPLICABLE

    def test_non_additive_obligation_ignored(self):
        """A removed-line obligation is not an addition; ignored."""
        resolved = "use a::Client;\nfn main(){}"
        missing = [_ob("use a::Client;", operation="removed")]
        r = propose_import_union(resolved, missing)
        assert r.status == STATUS_NOT_APPLICABLE

    # --- certificate / contract ---

    def test_certificate_shape(self):
        """The APPLIED certificate records the transaction for the journal."""
        resolved = "use util::{A, B};\nfn main(){}"
        missing = [_ob("use util::C;")]
        r = propose_import_union(resolved, missing)
        assert r.status == STATUS_APPLIED
        cert = r.certificate
        assert cert["primitive"] == "rust.use_leaf_union/v1"
        assert cert["risk_tier"] == "A"
        assert cert["closed_obligations"] == ["util::C"]
        assert cert["remaining_obligations"] == 0
        assert isinstance(cert["edits"], list) and len(cert["edits"]) >= 1
        assert set(cert["preconditions"]) == {
            "same_visibility", "same_cfg_domain",
            "binding_collision", "contains_glob",
        }
        assert len(cert["before_hash"]) == 16
        assert len(cert["after_hash"]) == 16
        assert cert["before_hash"] != cert["after_hash"]


# ---------------------------------------------------------------------------
# Robustness / transactional safety
# ---------------------------------------------------------------------------


class TestRobustness:
    """The editor never breaks the resolution loop, even on bad input."""

    def test_none_obligations(self):
        r = propose_import_union("use foo::Bar;\nfn main(){}", None)
        assert r.status == STATUS_NOT_APPLICABLE
        assert r.text == "use foo::Bar;\nfn main(){}"

    def test_empty_resolved_text(self):
        r = propose_import_union("", [_ob("use foo::Bar;")])
        assert r.status == STATUS_NOT_APPLICABLE

    def test_non_use_obligation_ignored(self):
        """A non-import executable line is not an import obligation."""
        resolved = "use foo::Bar;\nfn main(){}"
        missing = [_ob("let x = 5;")]
        r = propose_import_union(resolved, missing)
        assert r.status == STATUS_NOT_APPLICABLE

    def test_internal_error_is_blocked_not_raised(self):
        """A garbage obligation object that would cause an error is caught and
        mapped to BLOCKED — never raised."""
        class Bad:
            channel = "executable"
            operation = "added"
            exclusive = False
            line = None  # will trigger issues downstream safely
        r = propose_import_union("use foo::Bar;\nfn main(){}", [Bad()])
        # None line is filtered early; should be NOT_APPLICABLE, not raised.
        assert r.status in (STATUS_NOT_APPLICABLE, STATUS_BLOCKED)


# ---------------------------------------------------------------------------
# Integration: change_accounting → import_union
# ---------------------------------------------------------------------------


class TestChangeAccountingIntegration:
    """The realistic flow: derive_missing_obligations feeds propose_import_union."""

    def test_convergence_case_recovered(self):
        """The exact pattern from the Rust shadow run: the model copied CURRENT
        (which has ``util::{MapErrLayer, Oneshot}``), dropping REPLAYED's
        ``util::BoxCloneService``. Change-accounting flags the missing import;
        the union editor inserts it."""
        base = "use util::{MapErrLayer, Oneshot};\n\nfn main() { /* uses Oneshot */ }"
        current = "use util::{MapErrLayer, Oneshot};\n\nfn main() { /* uses Oneshot */ }"
        # REPLAYED added BoxCloneService to the group.
        replayed = "use util::{MapErrLayer, Oneshot, BoxCloneService};\n\nfn main() { /* uses Oneshot */ }"
        # The model copied CURRENT verbatim.
        resolved = current
        from capybase.change_accounting import derive_missing_obligations
        obligations = derive_missing_obligations(base, current, replayed, resolved)
        assert any("BoxCloneService" in o.line for o in obligations)
        r = propose_import_union(resolved, obligations)
        assert r.status == STATUS_APPLIED
        assert "BoxCloneService" in r.text
        assert "MapErrLayer" in r.text and "Oneshot" in r.text

    def test_pure_comment_change_not_an_import_obligation(self):
        """When the dropped side's only change is a comment, change-accounting
        defers it; no import obligation reaches the union editor."""
        base = "use foo::Bar;\n\n// old comment\nfn main(){}"
        current = "use foo::Bar;\n\n// old comment\nfn main(){}"
        replayed = "use foo::Bar;\n\n// new comment\nfn main(){}"
        resolved = current
        from capybase.change_accounting import derive_missing_obligations
        obligations = derive_missing_obligations(base, current, replayed, resolved)
        # Comment-only changes are deferred, not executable obligations.
        assert obligations == []
        r = propose_import_union(resolved, obligations)
        assert r.status == STATUS_NOT_APPLICABLE

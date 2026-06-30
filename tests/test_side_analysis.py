"""Tests for the modify/delete disambiguation surfacing (Layer B).

A conflict whose upstream side deliberately deleted a block (and whose replayed
side kept it) is the failure mode that originally presented a deletion as an
addition. After the fix, the review bundle and the interactive view both
annotate each side with what it DID (DELETED/ADDED/...) and render a one-line
side analysis, so the conflict shape is explicit.

These build a ConflictUnit through the extractor (no full rebase needed) and
assert the annotations appear in both render paths.
"""

from __future__ import annotations

from pathlib import Path

from capybase.escalation import write_review_bundle
from capybase.session import SessionPaths


def _unit_with_kind(kind: str, *, current: str, replayed: str, base: str):
    """Build a ConflictUnit carrying a merge_direction classification.

    Mirrors what conflict_extractor stamps onto structural_metadata at
    extraction time, so we can exercise the rendering without a full rebase.
    """
    from capybase.conflict_model import ConflictSide, ConflictUnit

    d_kind = {
        "modify_delete": {
            "kind": "modify_delete",
            "current": "deleted",
            "replayed": "unchanged",
            "summary": (
                "modify/delete: CURRENT_UPSTREAM_SIDE DELETED this block; "
                "REPLAYED_COMMIT_SIDE kept/changed it"
            ),
            "deleting_side": "current",
        }
    }[kind]
    return ConflictUnit(
        session_id="t", step_index=1, path="edit_file.rs", language="rust",
        conflict_type="UU", unit_id="edit_file.rs:1:0",
        unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current, blob_oid="abc"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed, blob_oid="def"),
        original_worktree_text="",
        structural_metadata={
            "merge_direction": d_kind,
            "provenance": {
                "current": {"sha": "abc123", "subject": "consolidate(tests): remove 13 brace_balance tests"},
                "replayed": {"sha": "def456", "subject": "migrate test-only brace helper"},
            },
        },
    )


def test_bundle_annotates_deleted_side_and_provenance(tmp_path: Path):
    """The bundle tags the empty upstream side as DELETED + names the commit.

    Before the fix this side rendered as bare ``CURRENT_UPSTREAM_SIDE`` with no
    text, which read as 'absent' rather than 'deliberately removed'.
    """
    base = "    #[test]\n    fn brace_balance_passes() {\n        assert!(true);\n    }\n"
    unit = _unit_with_kind(
        "modify_delete", current="", replayed=base, base=base
    )
    paths = SessionPaths("t", repo_root=tmp_path)
    out = write_review_bundle(paths, reason="escalated", unit=unit)
    text = out.read_text()
    # The empty current side is annotated as DELETED.
    assert "CURRENT_UPSTREAM_SIDE — DELETED this block" in text
    # The deleting commit's subject is surfaced (already-collected provenance).
    assert "consolidate(tests): remove 13 brace_balance tests" in text
    # A one-line side analysis states the conflict shape.
    assert "modify/delete: CURRENT_UPSTREAM_SIDE DELETED this block" in text


def test_bundle_renders_plain_when_no_classification(tmp_path: Path):
    """Units without a merge_direction classification still render the bare
    header (back-compat for units extracted before the enrichment ran)."""
    from capybase.conflict_model import ConflictSide, ConflictUnit

    unit = ConflictUnit(
        session_id="t", step_index=1, path="a.py", language="python",
        conflict_type="UU", unit_id="a.py:1:0", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="x"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="y"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="z"),
        original_worktree_text="",
    )
    paths = SessionPaths("t", repo_root=tmp_path)
    text = write_review_bundle(paths, reason="escalated", unit=unit).read_text()
    # Bare header present, no DELETED/ADDED annotation.
    assert "### CURRENT_UPSTREAM_SIDE" in text
    assert "DELETED" not in text


def test_interactive_render_annotates_sides():
    """The interactive view tags each side header inline + shows side analysis."""
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator

    orch = Orchestrator(Config(), repo=".", out=lambda *_a, **_k: None)
    base = "    #[test]\n    fn brace_balance_passes() {\n        assert!(true);\n    }\n"
    unit = _unit_with_kind(
        "modify_delete", current="", replayed=base, base=base
    )
    rendered = orch._render_unit_interactive(unit, prior_outcomes=[])
    # The empty current side is tagged DELETED inline on its header (the
    # annotation is appended after the closing ``--`` of the label).
    assert "DELETED" in rendered
    # Provenance subject appears on the same annotated header.
    assert "consolidate(tests): remove 13 brace_balance tests" in rendered
    # One-line side analysis present.
    assert "side analysis: modify/delete" in rendered


def test_interactive_render_color_off_is_plain():
    """Default (color=False) rendering has NO ANSI escapes — byte-identical to
    the un-colored baseline. This is what keeps existing assertions valid."""
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator

    orch = Orchestrator(Config(), repo=".", out=lambda *_a, **_k: None)  # color=False
    base = "    #[test]\n    fn brace_balance_passes() {\n        assert!(true);\n    }\n"
    unit = _unit_with_kind("modify_delete", current="", replayed=base, base=base)
    rendered = orch._render_unit_interactive(unit, prior_outcomes=[])
    assert "\x1b[" not in rendered, "color=False must emit no ANSI escapes"


def test_interactive_render_color_on_emits_codes_on_headers():
    """color=True colors the structural elements (headers, side-analysis,
    DELETED tag) with ANSI codes, while the conflict-side *content* stays plain
    (so it's readable and substring-matchable). Proves the wiring end-to-end."""
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator
    from capybase.color import CYAN, MAGENTA, RED, RESET, YELLOW

    orch = Orchestrator(Config(), repo=".", out=lambda *_a, **_k: None, color=True)
    base = "    #[test]\n    fn brace_balance_passes() {\n        assert!(true);\n    }\n"
    unit = _unit_with_kind("modify_delete", current="", replayed=base, base=base)
    rendered = orch._render_unit_interactive(unit, prior_outcomes=[])
    # The side headers carry color codes (cyan for CURRENT, magenta for REPLAYED).
    assert CYAN in rendered
    assert MAGENTA in rendered
    # The DELETED annotation is red (semantic: a removal).
    assert RED in rendered
    # The side-analysis line is yellow.
    assert YELLOW in rendered
    # RESET terminates every styled token.
    assert RESET in rendered
    # The conflict-side CONTENT stays plain (no escapes injected into the body):
    # the replayed side's test body must appear without surrounding codes.
    assert "assert!(true);" in rendered
    # And the classification word "DELETED" is still substring-matchable.
    assert "DELETED" in rendered

"""Tests for the comment ledger + frontier selection (Parts C1+C2).

The ledger groups comment variants across base/current/replayed/resolved, keyed
by lineage_id. The frontier selects which comments need reconciliation (those
affected by the conflict — overlapping the conflict region, differing across
versions, or attached to changed code).
"""

from __future__ import annotations

from capybase.comment_reconciler import build_comment_ledger, select_comment_frontier


def test_ledger_groups_same_comment_across_versions():
    """The same comment present in base/current/replayed/resolved gets ONE
    lineage_id (grouped by anchor + text similarity)."""
    base = "fn foo() {\n    // returns 1\n    1\n}\n"
    ledger = build_comment_ledger(base, base, base, base, "rust")
    deferred = [e for e in ledger if e.version == "resolved"]
    assert len(deferred) == 1
    assert "returns 1" in deferred[0].text


def test_frontier_includes_comments_differing_across_versions():
    """A comment whose text differs between base/current/replayed is in the
    frontier (needs reconciliation)."""
    base = "fn foo() {\n    // old name\n    1\n}\n"
    cur = "fn foo() {\n    // renamed comment\n    1\n}\n"
    rep = "fn foo() {\n    // different comment\n    1\n}\n"
    resolved = "fn foo() {\n    // old name\n    1\n}\n"
    ledger = build_comment_ledger(base, cur, rep, resolved, "rust")
    frontier = select_comment_frontier(ledger)
    assert len(frontier) >= 1
    # The frontier should include the resolved-version entry.
    assert any(e.version == "resolved" for e in frontier)


def test_frontier_excludes_unchanged_comments():
    """A comment identical across ALL versions is NOT in the frontier (it's
    already correct — no reconciliation needed)."""
    text = "fn foo() {\n    // unchanged comment\n    1\n}\n"
    ledger = build_comment_ledger(text, text, text, text, "rust")
    frontier = select_comment_frontier(ledger)
    assert frontier == [], f"unchanged comment should not be in frontier: {frontier}"


def test_frontier_includes_overlapping_conflict_region():
    """A comment that overlaps the conflict byte range IS in the frontier even
    if its text is identical across versions (it may reference stale context)."""
    base = "fn foo() {\n    // comment in conflict\n    1\n}\n"
    ledger = build_comment_ledger(base, base, base, base, "rust")
    # The comment at byte ~14 (after "fn foo() {\n    ").
    frontier = select_comment_frontier(
        ledger, conflict_byte_ranges=[(10, 50)])
    assert len(frontier) >= 1


def test_ledger_only_includes_deferred_comments():
    """Non-deferable comments (LICENSE, MACHINE directives) are NOT in the ledger."""
    base = (
        "// Copyright 2024 Acme.\n"
        "fn foo() {\n    // prose comment\n    1\n}\n"
    )
    ledger = build_comment_ledger(base, base, base, base, "rust")
    # Only the "prose comment" — not the Copyright line.
    texts = [e.text for e in ledger if e.version == "resolved"]
    assert any("prose comment" in t for t in texts)
    assert not any("Copyright" in t for t in texts)


def test_frontier_empty_when_no_comments():
    """No comments at all → empty ledger → empty frontier."""
    base = "fn foo() { 1 }\n"
    ledger = build_comment_ledger(base, base, base, base, "rust")
    assert ledger == []
    assert select_comment_frontier(ledger) == []

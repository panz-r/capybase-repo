"""Tests for the deterministic comment-plan verifiers (Part G1).

These are the §9 counterexamples the reconciliation CEGIS loop feeds back to
the model. Each verifier is pure (no LLM) and produces a concrete
:class:`CommentFailure` naming the offending lineage + the specific defect.
"""

from __future__ import annotations

from capybase.comment_reconciler import (
    build_comment_ledger, select_comment_frontier,
    CommentPlan, CommentAction, LedgerEntry,
)
from capybase.comment_verifiers import (
    CommentFailure, verify_comment_plan,
    STALE_IDENTIFIER, INVALID_ANCHOR, UNACCOUNTED_COMMENT,
    DUPLICATE_COMMENT, DIRECTIVE_CHANGED,
)


def _frontier_with_resolved(base, cur, rep, resolved, lang="rust"):
    ledger = build_comment_ledger(base, cur, rep, resolved, lang)
    return select_comment_frontier(ledger)


def _resolved_lineage_id(frontier, substring=""):
    """The lineage_id of the resolved-version entry whose text contains substring."""
    for e in frontier:
        if e.version == "resolved" and (not substring or substring in e.text):
            return e.lineage_id
    raise AssertionError(f"no resolved entry with substring {substring!r} in {frontier}")


# ---------------------------------------------------------------------------
# Clean plan → no failures
# ---------------------------------------------------------------------------


def test_clean_plan_has_no_failures():
    """A plan that dispositions every frontier lineage and references only
    identifiers present in the resolved code produces zero failures."""
    base = "fn foo() {\n    // uses MAX_RETRIES\n    let x = MAX_RETRIES;\n}\n"
    # The comment differs across versions (so it's in the frontier) but the
    # rewrite references MAX_RETRIES, which IS in the resolved code.
    rep = "fn foo() {\n    // uses MAX_RETRIES (updated)\n    let x = MAX_RETRIES;\n}\n"
    resolved = base
    frontier = _frontier_with_resolved(base, base, rep, resolved)
    lid = _resolved_lineage_id(frontier, "MAX_RETRIES")
    plan = CommentPlan(actions=[
        CommentAction(lineage_id=lid, operation="rewrite",
                      text="uses MAX_RETRIES (still valid)"),
    ])
    failures = verify_comment_plan(plan, frontier, resolved, "rust")
    assert failures == [], f"expected no failures, got: {failures}"


# ---------------------------------------------------------------------------
# STALE_IDENTIFIER
# ---------------------------------------------------------------------------


def test_stale_identifier_detected():
    """A rewrite that references an identifier NOT in the resolved code is
    flagged STALE_IDENTIFIER (the model wrote a comment about removed code)."""
    base = "fn foo() {\n    // uses MAX_RETRIES\n    let x = 1;\n}\n"
    rep = "fn foo() {\n    // uses RETRY_COUNT\n    let x = 1;\n}\n"
    resolved = "fn foo() {\n    // uses MAX_RETRIES\n    let x = 1;\n}\n"
    frontier = _frontier_with_resolved(base, base, rep, resolved)
    lid = _resolved_lineage_id(frontier, "MAX_RETRIES")
    # The model rewrites to mention RETRY_COUNT — but that name isn't in `resolved`.
    plan = CommentPlan(actions=[
        CommentAction(lineage_id=lid, operation="rewrite",
                      text="uses RETRY_COUNT for the loop"),
    ])
    failures = verify_comment_plan(plan, frontier, resolved, "rust")
    stale = [f for f in failures if f.kind == STALE_IDENTIFIER]
    assert len(stale) == 1
    assert "RETRY_COUNT" in stale[0].message
    assert stale[0].lineage_id == lid


def test_stale_identifier_case_sensitive_real_token():
    """A reference to a real token (e.g. a Rust keyword or identifier in the
    code) is NOT stale. The check keys on actual identifier tokens, not
    arbitrary words inside a comment."""
    base = "fn foo() {\n    // returns x\n    let x = 1;\n}\n"
    rep = "fn foo() {\n    // returns x (updated)\n    let x = 1;\n}\n"
    resolved = base
    frontier = _frontier_with_resolved(base, base, rep, resolved)
    lid = _resolved_lineage_id(frontier, "returns")
    plan = CommentPlan(actions=[
        CommentAction(lineage_id=lid, operation="rewrite", text="returns x"),
    ])
    failures = verify_comment_plan(plan, frontier, resolved, "rust")
    assert all(f.kind != STALE_IDENTIFIER for f in failures), failures


# ---------------------------------------------------------------------------
# INVALID_ANCHOR
# ---------------------------------------------------------------------------


def test_invalid_anchor_detected():
    """An action whose lineage_id is NOT in the frontier is flagged
    INVALID_ANCHOR (the model invented or mis-spelled a lineage id)."""
    base = "fn foo() {\n    // real comment\n    1\n}\n"
    rep = "fn foo() {\n    // updated comment\n    1\n}\n"
    resolved = base
    frontier = _frontier_with_resolved(base, base, rep, resolved)
    plan = CommentPlan(actions=[
        CommentAction(lineage_id="LC999", operation="rewrite", text="bogus"),
    ])
    failures = verify_comment_plan(plan, frontier, resolved, "rust")
    invalid = [f for f in failures if f.kind == INVALID_ANCHOR]
    assert len(invalid) == 1
    assert invalid[0].lineage_id == "LC999"


# ---------------------------------------------------------------------------
# UNACCOUNTED_COMMENT
# ---------------------------------------------------------------------------


def test_unaccounted_comment_detected():
    """A frontier lineage that received NO disposition is flagged
    UNACCOUNTED_COMMENT (every frontier comment must get an explicit op)."""
    # Both comments differ across versions (so both are in the frontier) and
    # use disjoint vocabularies so the lineage matcher treats them as separate
    # lineages even without anchor-symbol info.
    base = (
        "fn foo() {\n    // alpha omega\n    1\n}\n"
        "fn bar() {\n    // beta gamma delta\n    2\n}\n"
    )
    rep = (
        "fn foo() {\n    // alpha prime\n    1\n}\n"
        "fn bar() {\n    // beta gamma prime\n    2\n}\n"
    )
    cur = (
        "fn foo() {\n    // alpha omega\n    1\n}\n"
        "fn bar() {\n    // beta gamma delta\n    2\n}\n"
    )
    resolved = (
        "fn foo() {\n    // alpha omega\n    1\n}\n"
        "fn bar() {\n    // beta gamma delta\n    2\n}\n"
    )
    frontier = _frontier_with_resolved(base, cur, rep, resolved)
    lids = {e.lineage_id for e in frontier if e.version == "resolved"}
    assert len(lids) >= 2, frontier
    first_lid = sorted(lids)[0]
    plan = CommentPlan(actions=[
        CommentAction(lineage_id=first_lid, operation="keep"),
    ])
    failures = verify_comment_plan(plan, frontier, resolved, "rust")
    unaccounted = [f for f in failures if f.kind == UNACCOUNTED_COMMENT]
    assert len(unaccounted) >= 1
    unaccounted_ids = {f.lineage_id for f in unaccounted}
    assert first_lid not in unaccounted_ids
    assert unaccounted_ids.issubset(lids)


# ---------------------------------------------------------------------------
# DUPLICATE_COMMENT
# ---------------------------------------------------------------------------


def test_duplicate_comment_detected():
    """One lineage receiving >1 disposition is flagged DUPLICATE_COMMENT."""
    base = "fn foo() {\n    // only comment\n    1\n}\n"
    rep = "fn foo() {\n    // changed comment\n    1\n}\n"
    resolved = base
    frontier = _frontier_with_resolved(base, base, rep, resolved)
    lid = _resolved_lineage_id(frontier, "only")
    plan = CommentPlan(actions=[
        CommentAction(lineage_id=lid, operation="rewrite", text="first rewrite"),
        CommentAction(lineage_id=lid, operation="keep"),
    ])
    failures = verify_comment_plan(plan, frontier, resolved, "rust")
    dups = [f for f in failures if f.kind == DUPLICATE_COMMENT]
    assert len(dups) == 1
    assert dups[0].lineage_id == lid


# ---------------------------------------------------------------------------
# DIRECTIVE_CHANGED (defensive — ledger only carries DEFERRED, but a classifier
# regression could leak a MACHINE comment through)
# ---------------------------------------------------------------------------


def test_directive_changed_detected_on_non_deferable_target():
    """If a frontier entry's classification is non-deferable (MACHINE/LEGAL/
    GENERATED/DOCTEST) but the plan rewrites it, that's DIRECTIVE_CHANGED.

    The ledger normally filters these out, but verify_comment_plan guards
    against a classifier regression leaking a directive through as DEFERRED.
    """
    base = "fn foo() { 1 }\n"
    resolved = base
    frontier = _frontier_with_resolved(base, base, base, resolved)
    if not frontier:
        # No deferred comments — synthesize a frontier entry that's a directive
        # misclassified as DEFERRED (simulating a classifier regression).
        from capybase.adapters.comment_classifier import CommentClass
        frontier = [LedgerEntry(
            lineage_id="LC1", version="resolved",
            text="// clippy::all", cls=CommentClass.MACHINE,
            start=0, end=14, anchor_symbol="function:foo",
        )]
    lid = frontier[0].lineage_id
    plan = CommentPlan(actions=[
        CommentAction(lineage_id=lid, operation="rewrite", text="clippy::warn"),
    ])
    failures = verify_comment_plan(plan, frontier, resolved, "rust")
    # The failure is present only when the entry's cls is actually non-deferable.
    from capybase.adapters.comment_classifier import NON_DEFERABLE
    if frontier[0].cls in NON_DEFERABLE:
        dc = [f for f in failures if f.kind == DIRECTIVE_CHANGED]
        assert len(dc) == 1
        assert dc[0].lineage_id == lid


# ---------------------------------------------------------------------------
# Keep / preserve_verbatim don't run STALE_IDENTIFIER
# ---------------------------------------------------------------------------


def test_keep_does_not_trigger_stale_check():
    """A ``keep`` action leaves the comment as-is — STALE_IDENTIFIER should not
    fire even if the existing comment mentions a now-removed identifier (the
    model didn't introduce the staleness; it chose to keep it)."""
    base = "fn foo() {\n    // uses MAX_RETRIES\n    let x = 1;\n}\n"
    rep = "fn foo() {\n    // uses RETRY_COUNT\n    let x = 1;\n}\n"
    resolved = "fn foo() {\n    // uses MAX_RETRIES\n    let x = 1;\n}\n"
    frontier = _frontier_with_resolved(base, base, rep, resolved)
    lid = _resolved_lineage_id(frontier, "MAX_RETRIES")
    plan = CommentPlan(actions=[
        CommentAction(lineage_id=lid, operation="keep"),
    ])
    failures = verify_comment_plan(plan, frontier, resolved, "rust")
    # No failures (keep is valid; MAX_RETRIES is in the code).
    assert failures == [], failures


# ---------------------------------------------------------------------------
# Q — STYLE_VIOLATION verifier (§9)
# ---------------------------------------------------------------------------


def test_style_violation_rambling_rewrite():
    """A rewrite >5× the longest source variant → STYLE_VIOLATION (rambling)."""
    from capybase.comment_verifiers import verify_comment_plan, STYLE_VIOLATION
    base = "fn foo() {\n    // short\n    1\n}\n"
    rep = "fn foo() {\n    // also short\n    1\n}\n"
    resolved = base
    frontier = _frontier_with_resolved(base, base, rep, resolved)
    lid = _resolved_lineage_id(frontier, "short")
    rambling = "this is a very long comment that goes on and on far beyond " * 10
    plan = CommentPlan(actions=[
        CommentAction(lineage_id=lid, operation="rewrite", text=rambling),
    ])
    failures = verify_comment_plan(plan, frontier, resolved, "rust")
    style = [f for f in failures if f.kind == STYLE_VIOLATION]
    assert len(style) == 1
    assert lid in style[0].lineage_id or style[0].lineage_id == lid


def test_style_violation_comment_syntax_leakage():
    """A rewrite containing // or # mid-line (a comment-within-a-comment) →
    STYLE_VIOLATION."""
    from capybase.comment_verifiers import verify_comment_plan, STYLE_VIOLATION
    base = "fn foo() {\n    // docs\n    1\n}\n"
    rep = "fn foo() {\n    // also docs\n    1\n}\n"
    resolved = base
    frontier = _frontier_with_resolved(base, base, rep, resolved)
    lid = _resolved_lineage_id(frontier, "docs")
    plan = CommentPlan(actions=[
        CommentAction(lineage_id=lid, operation="rewrite",
                      text="docs // with nested comment"),
    ])
    failures = verify_comment_plan(plan, frontier, resolved, "rust")
    style = [f for f in failures if f.kind == STYLE_VIOLATION]
    assert len(style) >= 1


def test_style_violation_clean_rewrite_passes():
    """A clean, reasonably-sized rewrite → no STYLE_VIOLATION."""
    from capybase.comment_verifiers import verify_comment_plan, STYLE_VIOLATION
    base = "fn foo() {\n    // uses MAX_RETRIES\n    let x = MAX_RETRIES;\n}\n"
    rep = "fn foo() {\n    // uses MAX_RETRIES_NEW\n    let x = MAX_RETRIES;\n}\n"
    resolved = base
    frontier = _frontier_with_resolved(base, base, rep, resolved)
    lid = _resolved_lineage_id(frontier, "MAX_RETRIES")
    plan = CommentPlan(actions=[
        CommentAction(lineage_id=lid, operation="rewrite",
                      text="uses MAX_RETRIES for retry"),
    ])
    failures = verify_comment_plan(plan, frontier, resolved, "rust")
    assert all(f.kind != STYLE_VIOLATION for f in failures), failures


def test_style_violation_skipped_for_keep():
    """keep/preserve_verbatim don't run the style check (no new text)."""
    from capybase.comment_verifiers import verify_comment_plan, STYLE_VIOLATION
    base = "fn foo() {\n    // docs\n    1\n}\n"
    rep = "fn foo() {\n    // changed docs\n    1\n}\n"
    resolved = base
    frontier = _frontier_with_resolved(base, base, rep, resolved)
    lid = _resolved_lineage_id(frontier, "docs")
    plan = CommentPlan(actions=[
        CommentAction(lineage_id=lid, operation="keep"),
    ])
    failures = verify_comment_plan(plan, frontier, resolved, "rust")
    assert all(f.kind != STYLE_VIOLATION for f in failures), failures

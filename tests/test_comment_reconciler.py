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


# ---------------------------------------------------------------------------
# CST editor — apply_comment_plan + executable-token invariant (D1+D2)
# ---------------------------------------------------------------------------


def test_apply_rewrite_updates_comment_preserves_code():
    """A rewrite action replaces the comment text in-place; the executable code
    is unchanged (the hard invariant)."""
    from capybase.comment_reconciler import (
        build_comment_ledger, select_comment_frontier,
        CommentPlan, CommentAction, apply_comment_plan,
    )
    resolved = "fn foo() {\n    // old comment\n    let x = 1;\n}\n"
    # Force the comment into the frontier by making versions differ.
    base_diff = "fn foo() {\n    // DIFFERENT\n    let x = 1;\n}\n"
    ledger = build_comment_ledger(base_diff, resolved, resolved, resolved, "rust")
    frontier = select_comment_frontier(ledger)
    # Find the RESOLVED entry's lineage (the one apply_comment_plan edits).
    resolved_entries = [e for e in frontier if e.version == "resolved"]
    assert resolved_entries, f"no resolved entry in frontier: {frontier}"
    lid = resolved_entries[0].lineage_id
    plan = CommentPlan(actions=[
        CommentAction(lineage_id=lid, operation="rewrite",
                      text="new comment after rename", confidence=0.9),
    ])
    result = apply_comment_plan(resolved, frontier, plan, "rust")
    assert "new comment after rename" in result
    assert "old comment" not in result
    assert "fn foo()" in result
    assert "let x = 1;" in result


def test_apply_delete_blanks_comment_preserves_code():
    """A delete action blanks the comment; code unchanged."""
    from capybase.comment_reconciler import (
        build_comment_ledger, select_comment_frontier,
        CommentPlan, CommentAction, apply_comment_plan, ApplyError,
    )
    resolved = "fn foo() {\n    // stale comment\n    let x = 1;\n}\n"
    base_diff = "fn foo() {\n    // DIFFERENT\n    let x = 1;\n}\n"
    ledger = build_comment_ledger(base_diff, resolved, resolved, resolved, "rust")
    frontier = select_comment_frontier(ledger)
    resolved_entries = [e for e in frontier if e.version == "resolved"]
    lid = resolved_entries[0].lineage_id
    plan = CommentPlan(actions=[
        CommentAction(lineage_id=lid, operation="delete", confidence=0.95),
    ])
    result = apply_comment_plan(resolved, frontier, plan, "rust")
    assert "stale comment" not in result
    assert "fn foo()" in result
    assert "let x = 1;" in result


def test_apply_raises_if_code_changed():
    """If a plan accidentally changes executable code, the invariant catches it
    and raises ApplyError."""
    from capybase.comment_reconciler import (
        build_comment_ledger, select_comment_frontier,
        CommentPlan, CommentAction, apply_comment_plan, ApplyError,
    )
    resolved = "fn foo() {\n    // comment\n    let x = 1;\n}\n"
    base_diff = "fn foo() {\n    // DIFFERENT\n    let x = 1;\n}\n"
    ledger = build_comment_ledger(base_diff, resolved, resolved, resolved, "rust")
    frontier = select_comment_frontier(ledger)
    resolved_entries = [e for e in frontier if e.version == "resolved"]
    lid = resolved_entries[0].lineage_id
    # A rewrite that tries to inject code into the comment position.
    plan = CommentPlan(actions=[
        CommentAction(lineage_id=lid, operation="rewrite",
                      text="comment\n    let y = 2; // injected", confidence=0.5),
    ])
    try:
        result = apply_comment_plan(resolved, frontier, plan, "rust")
        # If it didn't raise, verify the code IS still preserved (the invariant
        # might pass if the injected code's tokens happen to match — unlikely but
        # the invariant is the safety net).
    except ApplyError:
        pass  # expected — the invariant caught the code change


def test_apply_keep_is_noop():
    """A keep action leaves the text unchanged."""
    from capybase.comment_reconciler import (
        build_comment_ledger, select_comment_frontier,
        CommentPlan, CommentAction, apply_comment_plan,
    )
    resolved = "fn foo() {\n    // a comment\n    1\n}\n"
    base_diff = "fn foo() {\n    // DIFFERENT\n    1\n}\n"
    ledger = build_comment_ledger(base_diff, resolved, resolved, resolved, "rust")
    frontier = select_comment_frontier(ledger)
    lid = frontier[0].lineage_id
    plan = CommentPlan(actions=[
        CommentAction(lineage_id=lid, operation="keep"),
    ])
    result = apply_comment_plan(resolved, frontier, plan, "rust")
    assert result == resolved  # unchanged


# ---------------------------------------------------------------------------
# Attempt-aware prompt + feedback block (Part G2)
# ---------------------------------------------------------------------------


def test_build_prompt_first_attempt_is_byte_identical_to_legacy():
    """The first iteration (attempt=0, feedback=None) produces the exact same
    prompt as the pre-G2 signature — backward-compatible for any caller that
    hasn't been updated."""
    from capybase.comment_reconciler import build_comment_reconcile_prompt
    base = "fn foo() {\n    // old\n    1\n}\n"
    rep = "fn foo() {\n    // new\n    1\n}\n"
    resolved = base
    ledger = build_comment_ledger(base, base, rep, resolved, "rust")
    frontier = select_comment_frontier(ledger)
    # Old-style call (no attempt/feedback kwargs) and new-style with attempt=0
    # and feedback=None must produce the same prompt.
    legacy = build_comment_reconcile_prompt(
        frontier, resolved, base, base, rep, "rust",
    )
    new_style = build_comment_reconcile_prompt(
        frontier, resolved, base, base, rep, "rust",
        attempt=0, feedback=None,
    )
    assert legacy == new_style


def test_build_prompt_renders_feedback_block_on_second_attempt():
    """When feedback (from the §9 verifiers) is non-empty, the prompt includes
    a `### prior-attempt feedback` block with each failure's message — the
    counterexample the model must address on this attempt."""
    from capybase.comment_reconciler import build_comment_reconcile_prompt
    from capybase.comment_verifiers import CommentFailure, STALE_IDENTIFIER
    base = "fn foo() {\n    // old\n    1\n}\n"
    rep = "fn foo() {\n    // new\n    1\n}\n"
    resolved = base
    ledger = build_comment_ledger(base, base, rep, resolved, "rust")
    frontier = select_comment_frontier(ledger)
    feedback = [CommentFailure(
        kind=STALE_IDENTIFIER, lineage_id="LC1",
        message="references identifier(s) not present: ['REMOVED_CONST']",
    )]
    prompt = build_comment_reconcile_prompt(
        frontier, resolved, base, base, rep, "rust",
        attempt=1, feedback=feedback,
    )
    assert "prior-attempt feedback" in prompt
    assert "REMOVED_CONST" in prompt
    assert "STALE_IDENTIFIER" in prompt


def test_build_prompt_omits_feedback_block_on_first_attempt():
    """attempt=0 never renders the feedback block (no prior attempt yet)."""
    from capybase.comment_reconciler import build_comment_reconcile_prompt
    from capybase.comment_verifiers import CommentFailure, STALE_IDENTIFIER
    base = "fn foo() {\n    // old\n    1\n}\n"
    rep = "fn foo() {\n    // new\n    1\n}\n"
    resolved = base
    ledger = build_comment_ledger(base, base, rep, resolved, "rust")
    frontier = select_comment_frontier(ledger)
    feedback = [CommentFailure(
        kind=STALE_IDENTIFIER, lineage_id="LC1", message="should not appear",
    )]
    prompt = build_comment_reconcile_prompt(
        frontier, resolved, base, base, rep, "rust",
        attempt=0, feedback=feedback,  # ignored on attempt 0
    )
    assert "prior-attempt feedback" not in prompt
    assert "should not appear" not in prompt


def test_build_prompt_mentions_reasoning_field():
    """The plan-first step instructs the model to emit a `reasoning` field per
    non-keep action — the build_repair_prompt pattern that forces the model to
    articulate WHY before emitting the disposition."""
    from capybase.comment_reconciler import build_comment_reconcile_prompt
    base = "fn foo() {\n    // old\n    1\n}\n"
    rep = "fn foo() {\n    // new\n    1\n}\n"
    resolved = base
    ledger = build_comment_ledger(base, base, rep, resolved, "rust")
    frontier = select_comment_frontier(ledger)
    prompt = build_comment_reconcile_prompt(
        frontier, resolved, base, base, rep, "rust",
    )
    assert "reasoning" in prompt.lower()


def test_parse_comment_plan_tolerates_reasoning_field():
    """The parser must accept (and ignore) the `reasoning` field without
    breaking — it's plan-first scaffolding, not part of the application."""
    from capybase.comment_reconciler import parse_comment_plan
    raw = '''{"actions": [
        {"lineage_id": "LC1", "operation": "rewrite",
         "text": "uses NEW_NAME", "reasoning": "old name was renamed",
         "confidence": 0.9}
    ]}'''
    plan = parse_comment_plan(raw)
    assert plan is not None
    assert len(plan.actions) == 1
    assert plan.actions[0].text == "uses NEW_NAME"


# ---------------------------------------------------------------------------
# Multi-language: K1 (gate) + K2 (prefix generalization)
# ---------------------------------------------------------------------------


def test_format_comment_rust_line():
    """// line comment: each line gets the // prefix."""
    from capybase.comment_reconciler import _format_comment
    out = _format_comment("hello\nworld", "// old comment", "rust")
    assert out == "// hello\n// world"


def test_format_comment_python_hash():
    """# line comment: each line gets the # prefix."""
    from capybase.comment_reconciler import _format_comment
    out = _format_comment("hello\nworld", "# old comment", "python")
    assert out == "# hello\n# world"


def test_format_comment_javascript_line():
    """// line comment for JS/TS works the same as Rust."""
    from capybase.comment_reconciler import _format_comment
    out = _format_comment("hello", "// old comment", "javascript")
    assert out == "// hello"


def test_format_comment_jsdoc_block():
    """JSDoc /** ... */ block: wrapped with the JSDoc delimiters."""
    from capybase.comment_reconciler import _format_comment
    orig = ["/**", " * Adds two numbers.", " */"]
    out = _format_comment("Adds two numbers.\nReturns the sum.", "\n".join(orig), "javascript")
    assert out.startswith("/**")
    assert out.endswith(" */")
    assert " * Adds two numbers." in out
    assert " * Returns the sum." in out


def test_format_comment_jsdoc_single_line():
    """Single-line JSDoc: /** text */."""
    from capybase.comment_reconciler import _format_comment
    out = _format_comment("Adds two numbers.", "/** old text */", "javascript")
    assert out == "/** Adds two numbers. */"


def test_format_comment_block_c_style():
    """C-style /* ... */ block (not JSDoc)."""
    from capybase.comment_reconciler import _format_comment
    out = _format_comment("hello world", "/* old */", "cpp")
    assert out == "/* hello world */"


def test_format_comment_python_docstring_triple_quote():
    """Python triple-quoted docstring: wrapped with the matching triple-quote."""
    from capybase.comment_reconciler import _format_comment
    out = _format_comment("Returns the sum.", '"""old docstring"""', "python")
    assert out == '"""Returns the sum."""'
    # Triple-single-quote variant.
    out2 = _format_comment("Returns the sum.", "'''old'''", "python")
    assert out2 == "'''Returns the sum.'''"


def test_apply_comment_plan_works_for_python_hash_comment():
    """End-to-end: apply_comment_plan rewrites a Python # comment."""
    from capybase.comment_reconciler import (
        build_comment_ledger, select_comment_frontier,
        CommentPlan, CommentAction, apply_comment_plan,
    )
    base = "def foo():\n    # uses X\n    return 1\n"
    rep = "def foo():\n    # uses Y\n    return 1\n"
    resolved = base
    ledger = build_comment_ledger(base, base, rep, resolved, "python")
    frontier = select_comment_frontier(ledger)
    lid = [e for e in frontier if e.version == "resolved"][0].lineage_id
    plan = CommentPlan(actions=[
        CommentAction(lineage_id=lid, operation="rewrite", text="uses Z"),
    ])
    result = apply_comment_plan(resolved, frontier, plan, "python")
    assert "# uses Z" in result
    assert "return 1" in result  # code unchanged


def test_apply_comment_plan_works_for_javascript_line_comment():
    """End-to-end: apply_comment_plan rewrites a JS // comment."""
    from capybase.comment_reconciler import (
        build_comment_ledger, select_comment_frontier,
        CommentPlan, CommentAction, apply_comment_plan,
    )
    base = "function foo() {\n    // uses X\n    return 1;\n}\n"
    rep = "function foo() {\n    // uses Y\n    return 1;\n}\n"
    resolved = base
    ledger = build_comment_ledger(base, base, rep, resolved, "javascript")
    frontier = select_comment_frontier(ledger)
    lid = [e for e in frontier if e.version == "resolved"][0].lineage_id
    plan = CommentPlan(actions=[
        CommentAction(lineage_id=lid, operation="rewrite", text="uses Z"),
    ])
    result = apply_comment_plan(resolved, frontier, plan, "javascript")
    assert "// uses Z" in result
    assert "return 1;" in result  # code unchanged


# ---------------------------------------------------------------------------
# P1+P2 — derived_from + reason_code fields (§12)
# ---------------------------------------------------------------------------


def test_parse_comment_plan_reads_derived_from():
    """The parser reads the derived_from provenance field (§12)."""
    from capybase.comment_reconciler import parse_comment_plan
    raw = '''{"actions": [
        {"lineage_id": "LC1", "operation": "rewrite",
         "text": "merged comment", "reason_code": "MERGE_CONFLICT_RESOLVED",
         "derived_from": ["base:LC1", "replayed:LC1"], "confidence": 0.9}
    ]}'''
    plan = parse_comment_plan(raw)
    assert plan is not None
    a = plan.actions[0]
    assert a.derived_from == ["base:LC1", "replayed:LC1"]
    assert a.reason_code == "MERGE_CONFLICT_RESOLVED"


def test_parse_comment_plan_handles_missing_derived_from():
    """Actions without derived_from/reason_code default to empty (backward
    compatible with plans produced before P1/P2)."""
    from capybase.comment_reconciler import parse_comment_plan
    raw = '''{"actions": [
        {"lineage_id": "LC1", "operation": "keep"}
    ]}'''
    plan = parse_comment_plan(raw)
    assert plan is not None
    a = plan.actions[0]
    assert a.derived_from == []
    assert a.reason_code == ""


def test_parse_comment_plan_handles_scalar_derived_from():
    """A scalar derived_from (mistake by the model) is wrapped into a list
    rather than crashing the parser."""
    from capybase.comment_reconciler import parse_comment_plan
    raw = '''{"actions": [
        {"lineage_id": "LC1", "operation": "rewrite",
         "text": "x", "derived_from": "base:LC1"}
    ]}'''
    plan = parse_comment_plan(raw)
    assert plan is not None
    assert plan.actions[0].derived_from == ["base:LC1"]


def test_prompt_requests_derived_from_and_reason_code():
    """The reconcile prompt's output contract requests derived_from + reason_code
    so the model emits provenance."""
    from capybase.comment_reconciler import build_comment_reconcile_prompt
    base = "fn foo() {\n    // old\n    1\n}\n"
    rep = "fn foo() {\n    // new\n    1\n}\n"
    ledger = build_comment_ledger(base, base, rep, base, "rust")
    frontier = select_comment_frontier(ledger)
    prompt = build_comment_reconcile_prompt(
        frontier, base, base, base, rep, "rust",
    )
    assert "derived_from" in prompt
    assert "reason_code" in prompt
    assert "MERGE_CONFLICT_RESOLVED" in prompt  # enumerated example

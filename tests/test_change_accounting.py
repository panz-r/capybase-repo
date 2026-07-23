"""Tests for the change-accounting module (``capybase.change_accounting``).

These cover the analysis's recommended test cases for the obligation-based
preservation check: channel classification, MISSING vs PRESENT detection,
comment deferral, directive handling, and the one-sided / equivalent /
genuine-drop scenarios that drive the convergence fix.
"""

from __future__ import annotations

import pytest

from capybase.change_accounting import (
    BranchObligation,
    classify_channel,
    derive_missing_obligations,
    derive_deferred_comments,
)


# ---------------------------------------------------------------------------
# Channel classification
# ---------------------------------------------------------------------------


class TestClassifyChannel:
    @pytest.mark.parametrize("line,expected", [
        ("/// doc comment", "comment"),
        ("//! inner doc", "comment"),
        ("// plain comment", "comment"),
        ("# python comment", "comment"),
        ("/* block open", "comment"),
        (" * block continuation", "comment"),
        ("#[derive(Debug)]", "directive"),
        ("#[cfg(test)]", "directive"),
        ("#![allow(dead_code)]", "directive"),
        ("@decorator(arg)", "directive"),
        ("fn foo() {", "executable"),
        ("    return 42;", "executable"),
        ("    }", "formatting"),
        ("    ", "formatting"),
        ("", "formatting"),
        ("};", "formatting"),
    ])
    def test_classification(self, line, expected):
        assert classify_channel(line) == expected

    def test_directive_beats_comment_for_hash_bracket(self):
        """``#[...]`` starts with ``#`` but is a directive, not a comment."""
        assert classify_channel("#[derive(Debug)]") == "directive"
        assert classify_channel("# plain comment") == "comment"


# ---------------------------------------------------------------------------
# derive_missing_obligations — the core scenarios
# ---------------------------------------------------------------------------


class TestDeriveMissingObligations:
    def test_one_sided_copy_passes(self):
        """Analysis test 1: one-sided conflict — copying the changed side has no
        missing obligations (the other side didn't change)."""
        # only current changed; replayed == base
        obls = derive_missing_obligations("base", "changed", "base", "changed")
        assert obls == []

    def test_equivalent_both_added_same_passes(self):
        """Analysis test 4: both branches independently made the same semantic
        change — exact-side copy passes as EQUIVALENT."""
        base = "fn a() { 1 }"
        cur = "fn a() { 1 }\nfn b() { 2 }"
        rep = "fn a() { 1 }\nfn b() { 2 }"  # same addition
        obls = derive_missing_obligations(base, cur, rep, cur)
        assert obls == []  # fn b already present → no obligation

    def test_genuine_dropped_delta_is_missing(self):
        """Analysis test 6: a true dropped executable delta produces a MISSING
        obligation with the specific line."""
        base = "fn a() { 1 }"
        cur = "fn a() { 1 }"
        rep = "fn a() { 1 }\nfn b() { 2 }"  # replayed added fn b
        obls = derive_missing_obligations(base, cur, rep, cur)
        assert len(obls) == 1
        assert "fn b() { 2 }" in obls[0].line
        assert obls[0].channel == "executable"
        assert obls[0].status == "MISSING"
        assert obls[0].operation == "added"

    def test_comment_only_change_is_deferred_not_obligation(self):
        """Analysis test 2/5: a comment-only change on the dropped side does NOT
        create an executable obligation (it's deferred to the comment pass)."""
        base = "fn a() { 1 }"
        cur = "fn a() { 1 }"
        rep = "/// docs\nfn a() { 1 }"  # replayed only added a doc comment
        obls = derive_missing_obligations(base, cur, rep, cur)
        assert obls == []  # comment-only → no executable obligation
        # but it IS a deferred comment
        deferred = derive_deferred_comments(base, cur, rep, cur)
        assert len(deferred) == 1
        assert "/// docs" in deferred[0].line

    def test_formatting_only_change_is_ignored(self):
        """Analysis test 10: a formatting-only branch delta creates no
        preservation obligation."""
        base = "fn a() {\n    1\n}"
        cur = "fn a() {\n    1\n}"
        rep = "fn a() {\n        1\n}"  # only re-indented
        obls = derive_missing_obligations(base, cur, rep, cur)
        assert obls == []

    def test_directive_change_is_obligation(self):
        """A ``#[derive(...)]`` / attribute change is executable-significant —
        it creates an obligation, not a deferred comment."""
        base = "struct S {\n    x: u32,\n}"
        cur = "struct S {\n    x: u32,\n}"
        rep = "#[derive(Debug)]\nstruct S {\n    x: u32,\n}"
        obls = derive_missing_obligations(base, cur, rep, cur)
        assert len(obls) == 1
        assert obls[0].channel == "directive"
        assert "#[derive(Debug)]" in obls[0].line

    def test_present_reindented_addition_not_missing(self):
        """A re-indented addition in the candidate counts as PRESENT (whitespace-
        normalized comparison), not MISSING."""
        base = "fn a() { 1 }"
        cur = "    fn b() { 2 }\nfn a() { 1 }"  # current added fn b (indented)
        rep = "fn b() { 2 }\nfn a() { 1 }"      # replayed added same (no indent)
        obls = derive_missing_obligations(base, cur, rep, cur)
        assert obls == []  # fn b present (modulo whitespace) → no obligation

    def test_copied_replayed_side(self):
        """When the model copies REPLAYED, the obligations come from CURRENT."""
        base = "fn a() { 1 }"
        cur = "fn a() { 1 }\nfn b() { 2 }"  # current added fn b
        rep = "fn a() { 1 }"
        obls = derive_missing_obligations(base, cur, rep, rep)  # copied replayed
        assert len(obls) == 1
        assert "fn b() { 2 }" in obls[0].line
        assert obls[0].side == "current"

    def test_not_an_exact_copy_returns_empty(self):
        """When the candidate differs from both sides, change accounting doesn't
        apply (return [] — the preservation check handles it differently)."""
        base = "fn a() { 1 }"
        cur = "fn a() { 2 }"
        rep = "fn a() { 3 }"
        res = "fn a() { 4 }"  # synthesized, not equal to either side
        assert derive_missing_obligations(base, cur, rep, res) == []

    def test_removed_lines_are_not_obligations(self):
        """A line the dropped side REMOVED (vs base) is not an obligation to
        integrate — the branch intended to delete it. Only ADDED lines that are
        absent are actionable."""
        base = "fn a() { 1 }\nfn b() { 2 }"
        cur = "fn a() { 1 }\nfn b() { 2 }"
        rep = "fn a() { 1 }"  # replayed deleted fn b
        obls = derive_missing_obligations(base, cur, rep, cur)
        assert obls == []  # the deletion isn't an obligation to integrate


# ---------------------------------------------------------------------------
# _render_failure integration (the delta-completion counterexample)
# ---------------------------------------------------------------------------


class TestRenderFailureIntegration:
    def test_missing_lines_render_as_delta_completion(self):
        """The repair prompt renders missing_lines as a constructive
        'integrate THESE lines' instruction, not a generic key-value dump."""
        from capybase.resolution_engine import _render_failure
        from capybase.conflict_model import VerificationFailure
        f = VerificationFailure(
            validator="preservation_heuristic",
            severity="warning",
            message="resolved text copies CURRENT verbatim, but REPLAYED "
                    "introduced executable changes not accounted for",
            detail={
                "copied_side": "current",
                "missing_lines": ["fn b() { 2 }", "use crate::x;"],
                "deferred_comments": 1,
                "missing_count": 2,
            },
        )
        rendered = _render_failure(f)
        assert "identical to CURRENT" in rendered
        assert "integrate them" in rendered
        assert "+ fn b() { 2 }" in rendered
        assert "+ use crate::x;" in rendered
        assert "deferred to the comment pass" in rendered

    def test_non_preservation_failure_renders_normally(self):
        """A syntax failure (no missing_lines) renders via the standard
        key-value path, unchanged."""
        from capybase.resolution_engine import _render_failure
        from capybase.conflict_model import VerificationFailure
        f = VerificationFailure(
            validator="syntax", severity="error",
            message="unclosed delimiter",
            detail={"line": 5, "col": 12},
        )
        rendered = _render_failure(f)
        assert "[syntax] unclosed delimiter" in rendered
        assert "line: 5" in rendered
        assert "col: 12" in rendered

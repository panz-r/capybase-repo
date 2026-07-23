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


class TestExclusiveConflicts:
    """The distinction that fixes the convergence loop: EXCLUSIVE conflicts
    (mutually-exclusive alternatives — choose, don't integrate) vs ADDITIVE
    (genuinely new content — integrate). Telling a small model to 'integrate'
    an exclusive conflict asks for the impossible."""

    def test_field_type_alternative_is_exclusive(self):
        """Two different type signatures for the SAME field are exclusive —
        the model should CHOOSE one, not combine them."""
        base = "    _marker: PhantomData<S>,"
        cur = "    _marker: PhantomData<fn(B) -> S>,"
        rep = "    _marker: PhantomData<fn() -> S>,"
        obls = derive_missing_obligations(base, cur, rep, cur)
        assert len(obls) == 1
        assert obls[0].exclusive is True

    def test_additive_import_is_not_exclusive(self):
        """A new ``use crate::b;`` is an ADDITION (coexists with
        ``use crate::a;``), not an exclusive alternative."""
        base = "use crate::a;\nfn main() {}"
        cur = "use crate::a;\nfn main() {}"
        rep = "use crate::a;\nuse crate::b;\nfn main() {}"
        obls = derive_missing_obligations(base, cur, rep, cur)
        assert len(obls) == 1
        assert obls[0].exclusive is False

    def test_new_function_is_not_exclusive(self):
        """A new ``fn b()`` is an addition, not exclusive with ``fn a()``."""
        base = "fn a() { 1 }"
        cur = "fn a() { 1 }"
        rep = "fn a() { 1 }\nfn b() { 2 }"
        obls = derive_missing_obligations(base, cur, rep, cur)
        assert len(obls) == 1
        assert obls[0].exclusive is False

    def test_assignment_target_alternative_is_exclusive(self):
        """Two different values for the SAME assignment target are exclusive."""
        base = "    let x = 1;"
        cur = "    let x = 2;"
        rep = "    let x = 3;"
        obls = derive_missing_obligations(base, cur, rep, cur)
        # `let x` — the anchor is `x`? Actually `let` captures `x` via the
        # identifier regex. Both sides have `let x` → exclusive.
        assert len(obls) == 1
        assert obls[0].exclusive is True

    def test_no_duplicate_obligations(self):
        """A line modified in multiple contexts (e.g. a field in the struct
        definition + its constructor) appears once, not duplicated."""
        base = "struct S { _marker: PhantomData<S> }\nimpl S { fn new() -> S { S { _marker: PhantomData } } }"
        cur = "struct S { _marker: PhantomData<fn(B) -> S> }\nimpl S { fn new() -> S { S { _marker: PhantomData } } }"
        rep = "struct S { _marker: PhantomData<fn() -> S> }\nimpl S { fn new() -> S { S { _marker: PhantomData } } }"
        obls = derive_missing_obligations(base, cur, rep, cur)
        # The _marker line appears once (deduped), not twice.
        marker_obls = [o for o in obls if "_marker" in o.line]
        assert len(marker_obls) <= 1


# ---------------------------------------------------------------------------
# _render_failure integration (the delta-completion counterexample)
# ---------------------------------------------------------------------------


class TestRenderFailureIntegration:
    def test_missing_lines_render_as_delta_completion(self):
        """The repair prompt renders missing_lines with the conflict-type-aware
        action instruction, not a generic key-value dump."""
        from capybase.resolution_engine import _render_failure
        from capybase.conflict_model import VerificationFailure
        f = VerificationFailure(
            validator="preservation_heuristic",
            severity="warning",
            message="resolved text copies CURRENT verbatim, but REPLAYED "
                    "has unaccounted changes (additive)",
            detail={
                "copied_side": "current",
                "missing_lines": ["fn b() { 2 }", "use crate::x;"],
                "conflict_type": "additive",
                "action": "integrate them into the candidate",
                "deferred_comments": 1,
                "missing_count": 2,
            },
        )
        rendered = _render_failure(f)
        assert "identical to CURRENT" in rendered
        assert "conflict type: additive" in rendered
        assert "DELTA-COMPLETION TASK" in rendered
        assert "ADD the above line(s)" in rendered
        assert "+ fn b() { 2 }" in rendered
        assert "+ use crate::x;" in rendered
        assert "deferred to the comment pass" in rendered

    def test_exclusive_conflict_renders_choose_instruction(self):
        """An exclusive conflict renders 'keep your selection OR switch' —
        NOT 'integrate' (which is impossible for mutually-exclusive
        alternatives)."""
        from capybase.resolution_engine import _render_failure
        from capybase.conflict_model import VerificationFailure
        f = VerificationFailure(
            validator="preservation_heuristic",
            severity="warning",
            message="resolved text copies CURRENT verbatim, but REPLAYED "
                    "has unaccounted changes (exclusive)",
            detail={
                "copied_side": "current",
                "missing_lines": ["_marker: PhantomData<fn() -> S>,"],
                "conflict_type": "exclusive",
                "action": "These are mutually-exclusive alternatives at the "
                          "same position — keep your selection OR switch to "
                          "the other side's value; both are valid.",
                "deferred_comments": 0,
                "missing_count": 1,
            },
        )
        rendered = _render_failure(f)
        assert "conflict type: exclusive" in rendered
        assert "keep your selection OR switch" in rendered
        assert "integrate" not in rendered  # NOT an integration task

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

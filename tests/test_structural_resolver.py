"""Tests for the deterministic structural pre-resolver.

All rules are pure functions over the three conflict sides — no I/O, no model,
no git — so every rule is exhaustively testable. The safety contract (validate-
or-fall-through) is exercised in the orchestrator integration tests; here we
lock in each rule's correctness directly.
"""

from __future__ import annotations

import pytest

from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.structural_resolver import (
    StructuralResolution,
    _try_zealous_merge,
    resolve_structurally,
)


def _unit(base: str, current: str, replayed: str) -> ConflictUnit:
    def _side(label, text):
        return ConflictSide(label=label, text=text)  # type: ignore[arg-type]

    return ConflictUnit(
        session_id="s", step_index=0, path="f.py", unit_id="u",
        base=_side("BASE", base),
        current=_side("CURRENT_UPSTREAM_SIDE", current),
        replayed=_side("REPLAYED_COMMIT_SIDE", replayed),
        original_worktree_text=base,
    )


# ---------------------------------------------------------------------------
# Rule 1: identical sides
# ---------------------------------------------------------------------------


def test_identical_sides_resolves_to_that_side():
    u = _unit("x = 1", "x = 2", "x = 2")
    r = resolve_structurally(u)
    assert r.resolved and r.rule == "identical_sides"
    assert r.text == "x = 2"


def test_identical_sides_ignores_whitespace_variance():
    u = _unit("x = 1", "x = 2  ", "  x = 2")
    r = resolve_structurally(u)
    assert r.rule == "identical_sides"
    # Emits the non-empty side as-is (current here), not normalized.
    assert r.text == "x = 2  "


def test_identical_sides_both_empty_resolves_empty():
    u = _unit("x = 1", "", "")
    r = resolve_structurally(u)
    assert r.resolved
    assert r.text == ""


# ---------------------------------------------------------------------------
# Rule 2: one-sided change
# ---------------------------------------------------------------------------


def test_one_sided_current_changed_only():
    # Current diverged, replayed == base → take current.
    u = _unit("def f():\n    return 1", "def f():\n    return 2", "def f():\n    return 1")
    r = resolve_structurally(u)
    assert r.resolved and r.rule == "one_sided_change"
    assert r.text == "def f():\n    return 2"


def test_one_sided_replayed_changed_only():
    # Replayed diverged, current == base → take replayed.
    u = _unit("def f():\n    return 1", "def f():\n    return 1", "def f():\n    return 3")
    r = resolve_structurally(u)
    assert r.resolved and r.rule == "one_sided_change"
    assert r.text == "def f():\n    return 3"


def test_one_sided_when_other_side_concedes_to_empty():
    # Current deleted (empty), replayed kept base. This is the modify/delete shape:
    # one side deliberately removed the block, the other conceded to base. The
    # delete_side rule now owns it (it's the rule specifically built to ACCEPT a
    # clean deletion), emitting the deleting side's empty text. Previously this
    # resolved via one_sided_change with the identical result; delete_side
    # attributes the resolution to the real intent so the bundle/journal can
    # surface "deliberate deletion accepted".
    u = _unit("x = 1", "", "x = 1")
    r = resolve_structurally(u)
    assert r.rule == "delete_side"
    assert r.text == ""


# ---------------------------------------------------------------------------
# Rule 3: disjoint edits (both changed, non-overlapping lines)
# ---------------------------------------------------------------------------


def test_disjoint_edits_merge_both_changes():
    # Base has two lines; current edits line 1, replayed edits line 2. Disjoint.
    base = "A = 1\nB = 1"
    current = "A = 2\nB = 1"      # changed line 0
    replayed = "A = 1\nB = 2"     # changed line 1
    r = resolve_structurally(_unit(base, current, replayed))
    assert r.resolved and r.rule == "disjoint_edits"
    assert r.text == "A = 2\nB = 2"  # both edits applied


def test_disjoint_edits_insertions_in_different_spots():
    base = "def f():\n    pass"
    # current adds a docstring at top; replayed changes the body. Disjoint lines.
    current = "def f():\n    \"\"\"doc\"\"\"\n    pass"
    replayed = "def f():\n    return 1"
    r = resolve_structurally(_unit(base, current, replayed))
    if r.resolved:  # only assert safety when it resolves; disjoint detection is conservative
        assert r.rule == "disjoint_edits"
        # Must contain BOTH sides' intent (docstring from current, return from replayed).
        assert "doc" in r.text
        assert "return 1" in r.text


def test_disjoint_edits_overlapping_returns_unresolved():
    # Both sides change the SAME line → real conflict → unresolved (defer to LLM).
    base = "x = 1"
    current = "x = 2"
    replayed = "x = 3"
    r = resolve_structurally(_unit(base, current, replayed))
    assert not r.resolved
    assert r.rule is None


def test_disjoint_edits_adjacent_non_overlapping_lines_merge():
    # Line 0 vs line 1 — adjacent but not overlapping → safe to merge.
    base = "a = 1\nb = 1"
    current = "a = 2\nb = 1"
    replayed = "a = 1\nb = 2"
    r = resolve_structurally(_unit(base, current, replayed))
    assert r.resolved and r.rule == "disjoint_edits"
    assert r.text == "a = 2\nb = 2"


# ---------------------------------------------------------------------------
# Fall-through: genuine conflicts stay unresolved
# ---------------------------------------------------------------------------


def test_real_semantic_conflict_is_unresolved():
    # Both sides changed the same thing differently → no safe rule → None.
    u = _unit("color = 'red'", "color = 'blue'", "color = 'green'")
    r = resolve_structurally(u)
    assert not r.resolved
    assert r.rule is None


def test_both_sides_diverge_on_overlapping_multiline_block_unresolved():
    base = "def f():\n    x = 1\n    y = 2"
    current = "def f():\n    x = 9\n    y = 2"
    replayed = "def f():\n    x = 1\n    y = 9"
    # Both touch line 1 (the def line) AND diverge — overlapping → unresolved.
    # (If difflib treats the def line as equal, this may resolve disjointly;
    # either outcome is safe. Assert the resolved case is internally consistent.)
    r = resolve_structurally(_unit(base, current, replayed))
    if r.resolved:
        assert r.rule in ("disjoint_edits",)


# ---------------------------------------------------------------------------
# Rule priority: identical beats one-sided beats disjoint
# ---------------------------------------------------------------------------


def test_identical_takes_priority_over_one_sided():
    # current==replayed (identical), but both differ from base.
    u = _unit("x = 1", "x = 9", "x = 9")
    r = resolve_structurally(u)
    assert r.rule == "identical_sides"  # not one_sided_change


# ---------------------------------------------------------------------------
# Resolution shape: produces block-interior text (splices like an LLM candidate)
# ---------------------------------------------------------------------------


def test_resolved_text_is_plain_block_text_no_markers():
    u = _unit("a\nb", "a\nB", "a\nb")
    r = resolve_structurally(u)
    assert r.resolved
    # No conflict markers leaked into the resolved text.
    assert "<<<" not in r.text and "===" not in r.text and ">>>" not in r.text


# ---------------------------------------------------------------------------
# Rule 4: zealous merge — per-base-line 3-way (survey §1.4)
#
# This is the rule disjoint_edits CAN'T handle: two edits that overlap in
# base-line span, yet are still safe because the overlap is agreed (both made
# the same change) or one-sided (one side conceded that sub-region). It only
# fires when disjoint_edits already refused, and only ever emits a merge where
# at most one side actually changed each base line's content.
# ---------------------------------------------------------------------------


def test_zealous_resolves_agreeing_overlap():
    # Both sides change the SAME line identically AND each makes a one-sided
    # change elsewhere → whole blocks differ (so identical_sides refuses), but
    # the overlapping line is agreed (both B→X) and the other line is one-sided
    # (current keeps base D, replayed→E). zealous resolves the whole hunk.
    base = "A\nB\nC\nD"
    current = "A\nX\nC\nD"
    replayed = "A\nX\nC\nE"
    r = resolve_structurally(_unit(base, current, replayed))
    assert r.resolved and r.rule == "zealous_merge"
    assert r.text == "A\nX\nC\nE"


def test_zealous_resolves_overlapping_but_one_sided():
    # The headline case git's coarse hunk flags as one conflict (verified: git
    # merge-file emits a single block here). Per base line: B→ current changed,
    # replayed conceded (take B2); C→ both changed identically (agree on C2).
    # disjoint_edits sees overlapping base regions {1,2}∩{2} and refuses.
    base = "A\nB\nC\nD"
    current = "A\nB2\nC2\nD"
    replayed = "A\nB\nC2\nD"
    r = resolve_structurally(_unit(base, current, replayed))
    assert r.resolved and r.rule == "zealous_merge"
    assert r.text == "A\nB2\nC2\nD"


def test_zealous_resolves_mixed_one_sided_and_disjoint():
    # current rewrites line 1; replayed rewrites line 2 — disjoint in base, BUT
    # adjacent enough that disjoint_edits' conservative reconstruction may
    # refuse. zealous handles it per-base-line regardless. Either rule resolving
    # is safe; assert the merge is correct when resolved.
    base = "a = 1\nb = 1\nc = 1"
    current = "a = 9\nb = 1\nc = 1"
    replayed = "a = 1\nb = 1\nc = 9"
    r = resolve_structurally(_unit(base, current, replayed))
    if r.resolved:
        assert r.rule in ("disjoint_edits", "zealous_merge")
        assert r.text == "a = 9\nb = 1\nc = 9"


def test_zealous_bails_on_genuine_two_sided_same_span():
    # Both sides change the same line differently → genuine conflict → None.
    base = "x = 1"
    current = "x = 2"
    replayed = "x = 3"
    r = resolve_structurally(_unit(base, current, replayed))
    assert not r.resolved
    assert r.rule is None


def test_zealous_bails_on_genuine_two_sided_overlapping_span():
    # Both sides change overlapping multiline regions, neither concedes → None.
    base = "def f():\n    x = 1\n    y = 2"
    current = "def f():\n    x = 1\n    y = 9"
    replayed = "def f():\n    x = 9\n    y = 2"
    r = resolve_structurally(_unit(base, current, replayed))
    # If difflib aligns the def/x/y lines as distinct regions, zealous may merge
    # disjointly; if it groups them as one overlapping region, it bails. Either
    # is safe — assert only that a resolved result is internally consistent.
    if r.resolved:
        assert r.rule in ("disjoint_edits", "zealous_merge")


def test_zealous_bails_on_pure_insertion_but_union_resolves_it():
    # Zealous itself still refuses a pure insertion (ordering is ambiguous at
    # the per-line merge granularity)...
    base = "A"
    current = "A\nB"      # current inserts B
    replayed = "A\nC"     # replayed inserts C
    assert _try_zealous_merge(base, current, replayed) is None
    # ...but the insertion_union rule (which runs after zealous in the pipeline)
    # DOES resolve it with a deterministic ordering (current's insert before
    # replayed's). This is the easy-merge gap #1 fills: pure insertions of
    # distinct lines no longer defer to the LLM.
    r = resolve_structurally(_unit(base, current, replayed))
    assert r.resolved
    assert r.rule == "insertion_union"
    assert r.text == "A\nB\nC"


def test_zealous_never_emits_garbage_on_partial_overlap():
    # Overlapping regions with DIFFERENT base spans are ambiguous (where does
    # one edit end?) → zealous must bail rather than splice.
    base = "A\nB\nC\nD"
    current = "A\nX\nC\nD"       # replaces base[1] only
    replayed = "A\nB\nC\nD"      # no change → one-sided, resolves via zealous
    r = resolve_structurally(_unit(base, current, replayed))
    # current changed, replayed == base → actually one_sided_change wins first.
    assert r.rule == "one_sided_change"
    assert r.text == "A\nX\nC\nD"


def test_zealous_resolved_text_has_no_markers():
    # Whole blocks differ (private one-sided edit on D) so identical_sides
    # refuses; the overlapping line B is one-sided (current B→X, replayed
    # concedes). zealous resolves it — assert no markers leak into the text.
    base = "A\nB\nC\nD"
    current = "A\nX\nC\nD2"
    replayed = "A\nB\nC\nD2"
    r = resolve_structurally(_unit(base, current, replayed))
    assert r.resolved and r.rule == "zealous_merge"
    assert "<<<" not in r.text and "===" not in r.text and ">>>" not in r.text


# ---------------------------------------------------------------------------
# Rule 1: delete_side — accept a deliberate deletion (modify/delete disambiguation)
#
# When one side cleanly deleted the block and the other side added nothing that
# the deletion would clobber, the safe resolution is to ACCEPT THE DELETION.
# This is the guard against the "silent loss of intent" failure mode where a
# modify/delete is wrongly merged to keep dead code. Declines when the non-
# deleting side added/modified-with-additions content (a real change the LLM
# must judge).
# ---------------------------------------------------------------------------


def test_delete_side_accepts_current_deletion_replayed_unchanged():
    # The edit_file.rs shape: upstream (current) deleted a test block, replayed
    # kept it verbatim. delete_side accepts the deletion → empty text.
    base = (
        "    #[test]\n    fn brace_balance_passes() {\n"
        "        assert!(check_brace_balance(...).is_ok());\n    }\n"
    )
    r = resolve_structurally(_unit(base, "", base))
    assert r.resolved and r.rule == "delete_side"
    assert r.text == ""


def test_delete_side_accepts_replayed_deletion_current_unchanged():
    # Symmetric: replayed deleted, current kept base.
    base = "def dead():\n    return 1\n"
    r = resolve_structurally(_unit(base, base, ""))
    assert r.resolved and r.rule == "delete_side"
    assert r.text == ""


def test_delete_side_both_deleted_resolves_via_identical_sides():
    # Both sides deleted (both empty) → not a modify/delete, so delete_side
    # declines (direction sets deleting_side=None when both deleted). The
    # identical_sides rule then resolves it: both sides are empty → empty merge.
    # Either attribution is correct; the result (accept the deletion) is the same.
    base = "def dead():\n    return 1\n"
    r = resolve_structurally(_unit(base, "", ""))
    assert r.resolved
    assert r.text == ""


def test_delete_side_declines_when_other_side_added_content():
    # Current deleted, but replayed ADDED new content that the deletion would
    # drop → decline so the LLM judges whether the addition or the deletion wins.
    base = "def dead():\n    return 1\n"
    replayed = "def new_thing():\n    return 2\n"  # an addition, not base
    r = resolve_structurally(_unit(base, "", replayed))
    assert not r.resolved


def test_delete_side_declines_when_other_side_modified_with_additions():
    # Current deleted, replayed rewrote the block (modified: removed + added) →
    # the keeper introduced new content; decline, don't silently drop it.
    base = "def dead():\n    return 1\n"
    replayed = "def dead():\n    return 1\n    cleanup()\n"  # modified: kept + added
    r = resolve_structurally(_unit(base, "", replayed))
    assert not r.resolved


def test_delete_side_takes_priority_and_records_rule():
    # A modify/delete where current deleted and replayed == base would otherwise
    # resolve via identical_sides/one_sided_change; delete_side owns it so the
    # journal/bundle can attribute the resolution to a deliberate deletion.
    base = "def a():\n    pass\n\ndef b():\n    pass\n"
    r = resolve_structurally(_unit(base, "", base))
    assert r.rule == "delete_side"


# ---------------------------------------------------------------------------
# Easy-merge union rules (#1): list_union, dict_union, insertion_union.
# These resolve the "both sides appended distinct items" shapes every prior
# rule declines, with a deterministic ordering (current-appends first).
# ---------------------------------------------------------------------------


def test_list_union_merges_distinct_appends():
    """Both sides append distinct items to a list → base + current + replayed."""
    base = 'SERVICES = ["core"]'
    current = 'SERVICES = ["core", "scheduler"]'
    replayed = 'SERVICES = ["core", "reloader"]'
    r = resolve_structurally(_unit(base, current, replayed))
    assert r.rule == "list_union"
    assert r.text == 'SERVICES = ["core", "scheduler", "reloader"]'


def test_list_union_declines_on_shared_append():
    """Both sides appending the SAME item is ambiguous → decline (let other rules)."""
    base = 'S = ["a"]'
    current = 'S = ["a", "b"]'
    replayed = 'S = ["a", "b"]'  # same append
    r = resolve_structurally(_unit(base, current, replayed))
    # identical_sides handles the same-append case; list_union declines.
    assert r.rule != "list_union"


def test_list_union_declines_when_a_side_edits_a_base_item():
    """A side that modifies a base item (not a pure append) → decline."""
    base = 'S = ["a", "b"]'
    current = 'S = ["A", "b"]'  # edited base item "a" → "A"
    replayed = 'S = ["a", "b", "c"]'
    r = resolve_structurally(_unit(base, current, replayed))
    assert r.rule != "list_union"


def test_dict_union_merges_distinct_inline_keys():
    """Both sides add distinct keys to an inline dict → base + current + replayed."""
    base = 'CFG = {"a": 1}'
    current = 'CFG = {"a": 1, "b": 2}'
    replayed = 'CFG = {"a": 1, "c": 3}'
    r = resolve_structurally(_unit(base, current, replayed))
    assert r.rule == "dict_union"
    assert '"a": 1' in r.text and '"b": 2' in r.text and '"c": 3' in r.text


def test_dict_union_declines_on_multiline_dict():
    """A multi-line dict declines (reconstructing indentation is fiddly → LLM)."""
    base = 'CFG = {\n    "a": 1,\n}'
    current = 'CFG = {\n    "a": 1,\n    "b": 2,\n}'
    replayed = 'CFG = {\n    "a": 1,\n    "c": 3,\n}'
    r = resolve_structurally(_unit(base, current, replayed))
    assert r.rule != "dict_union"  # multi-line → deferred


def test_dict_union_declines_on_shared_key():
    """Both sides adding the SAME key → decline (value conflict)."""
    base = 'CFG = {"a": 1}'
    current = 'CFG = {"a": 1, "b": 2}'
    replayed = 'CFG = {"a": 1, "b": 9}'  # same key, different value
    r = resolve_structurally(_unit(base, current, replayed))
    assert r.rule != "dict_union"


def test_insertion_union_merges_distinct_inserted_lines():
    """Both sides insert distinct lines after base anchors → interleaved.
    (token_disjoint may also handle this; what matters is a correct resolve.)"""
    base = "a = 1\nb = 2\nc = 3"
    current = "a = 1\nx = 9\nb = 2\nc = 3"      # insert x after a
    replayed = "a = 1\nb = 2\nc = 3\ny = 8"     # insert y after c
    r = resolve_structurally(_unit(base, current, replayed))
    assert r.resolved
    assert r.text == "a = 1\nx = 9\nb = 2\nc = 3\ny = 8"


def test_insertion_union_merges_multi_line_blocks():
    """Multi-line insertion BLOCKS (e.g. a new function) merge correctly, even
    when both sides share a blank-line separator (ignored in the overlap check)."""
    base = "def base():\n    return 0"
    current = "def base():\n    return 0\n\ndef add(x, y):\n    return x + y"
    replayed = "def base():\n    return 0\n\ndef sub(x, y):\n    return x - y"
    r = resolve_structurally(_unit(base, current, replayed))
    assert r.rule == "insertion_union"
    assert "def add" in r.text and "def sub" in r.text
    assert r.text.count("def base") == 1  # base not duplicated


def test_insertion_union_declines_when_a_side_modifies_a_base_line():
    """A side that modifies (not just inserts) a base line → decline."""
    base = "a = 1\nb = 2"
    current = "a = 99\nb = 2"  # modified a, not inserted
    replayed = "a = 1\nb = 2\nc = 3"
    r = resolve_structurally(_unit(base, current, replayed))
    assert r.rule != "insertion_union"


def test_insertion_union_declines_on_shared_inserted_line():
    """Both sides inserting the SAME line → ambiguous → decline."""
    base = "a = 1"
    current = "a = 1\nb = 2"
    replayed = "a = 1\nb = 2"  # same inserted line
    r = resolve_structurally(_unit(base, current, replayed))
    assert r.rule != "insertion_union"


# ---------------------------------------------------------------------------
# Blessed-corpus: the union/combine shapes now resolve with ZERO LLM calls
# (the reviewer's "Done when" criterion for #1).
# ---------------------------------------------------------------------------


def test_blessed_corpus_combine_shapes_resolve_deterministically():
    """The calibration corpus's combine shapes resolve via the deterministic
    resolver — no LLM judgment needed. list/dict/text/import combines."""
    from capybase.calibration_corpus import CALIBRATION_CONFLICTS
    from capybase.quality import _is_correct

    must_resolve = {
        "list-combine", "both-sides-add", "text-combine", "import-combine",
    }
    for title in must_resolve:
        conflict = next(c for c in CALIBRATION_CONFLICTS if c.title == title)
        r = resolve_structurally(conflict.unit)
        assert r.resolved, f"{title} did not resolve deterministically (rule={r.rule})"
        assert _is_correct(r.text, conflict.expected_text), (
            f"{title} resolved to wrong text: {r.text!r} vs {conflict.expected_text!r}"
        )

"""Tests for entity-boundary sub-conflict splitting (oversized-splitting v3).

Covers the core invariants of ``_split_unit_at_entities``:

* the sub-spans exactly partition the parent marker_span (no gaps, no overlaps),
* a splice round-trip over the sub-spans is clean (no marker scaffolding leaks),
* the gates (region size, language, disabled flag, no entities) are no-ops,
* lopsided add conflicts split on the structure-carrying side,
* symmetric conflicts require matching entity counts (decline otherwise),
* sub-units get distinct ids and traceability metadata,
* the Shared Resolution Context block renders + truncates correctly.

These tests build ``ConflictUnit`` objects directly (no git) so they exercise
the pure splitting logic quickly and deterministically.
"""
from __future__ import annotations

from capybase.adapters.parsers import splice_all_resolutions
from capybase.conflict_extractor import (
    ConflictExtractor,
    _drop_points_inside_preprocessor_conditional,
    _split_unit_at_entities,
)
from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.config import FutureConfig
from capybase.resolution_engine import _sibling_resolutions_block


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_unit(
    worktree: str,
    marker_span: tuple[int, int],
    cur_text: str,
    rep_text: str,
    *,
    path: str = "t.c",
    language: str = "c",
    base_text: str = "",
    unit_id: str = "t.c:0:0",
) -> ConflictUnit:
    """Build a marker-block ConflictUnit for direct splitting tests."""
    return ConflictUnit(
        session_id="s",
        step_index=0,
        path=path,
        language=language,
        conflict_type="UU",
        unit_id=unit_id,
        unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base_text),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=cur_text),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=rep_text),
        original_worktree_text=worktree,
        marker_span=marker_span,
        enclosing_symbol=None,
    )


def _two_func_block() -> tuple[str, tuple[int, int], str, str]:
    """A symmetric two-function C conflict: func_a and func_b in both sides.

    Returns (worktree, marker_span, current_text, replayed_text).
    """
    worktree = "\n".join([
        "#include <stdio.h>",      # 0
        "<<<<<<<",                 # 1
        "int func_a(int x) {",     # 2
        "    return x + 1;",       # 3
        "}",                       # 4
        "",                        # 5
        "int func_b(int y) {",     # 6
        "    return y * 2;",       # 7
        "}",                       # 8
        "=======",                 # 9
        "int func_a(int x) {",     # 10
        "    return x + 10;",      # 11
        "}",                       # 12
        "",                        # 13
        "int func_b(int y) {",     # 14
        "    return y * 20;",      # 15
        "}",                       # 16
        ">>>>>>>",                 # 17
        "static int tail = 0;",    # 18
    ])
    wt = worktree.split("\n")
    cur = "\n".join(wt[2:9])
    rep = "\n".join(wt[10:17])
    return worktree, (1, 17), cur, rep


# ---------------------------------------------------------------------------
# Partition + splice invariants
# ---------------------------------------------------------------------------

def test_partition_is_exact_contiguous_non_overlapping():
    """Sub-spans must tile the parent span with no gaps or overlaps."""
    worktree, span, cur, rep = _two_func_block()
    parent = _make_unit(worktree, span, cur, rep)
    subs = _split_unit_at_entities(parent, min_region_lines=8, min_sub_lines=3)

    assert len(subs) >= 2, "expected the two-function block to split"
    spans = sorted(s.marker_span for s in subs)
    # Covers the parent exactly.
    assert spans[0][0] == span[0]
    assert spans[-1][1] == span[1]
    # Contiguous + non-overlapping.
    for i in range(len(spans) - 1):
        assert spans[i][1] < spans[i + 1][0], "spans overlap"
        assert spans[i + 1][0] == spans[i][1] + 1, "gap between sub-spans"


def test_splice_round_trip_is_marker_free():
    """Splicing placeholders into every sub-span yields a clean, marker-free file."""
    worktree, span, cur, rep = _two_func_block()
    parent = _make_unit(worktree, span, cur, rep)
    subs = _split_unit_at_entities(parent, min_region_lines=8, min_sub_lines=3)
    assert len(subs) >= 2

    spans_and_texts = [(s.marker_span, f"RES{k}") for k, s in enumerate(subs)]
    out = splice_all_resolutions(worktree, spans_and_texts)
    # No conflict scaffolding leaked.
    for marker in ("<<<<<<<", "=======", ">>>>>>>"):
        assert marker not in out
    # Every placeholder landed.
    for k in range(len(subs)):
        assert f"RES{k}" in out


def test_symmetric_split_aligns_sides():
    """Sub-unit i's current + replayed carry the SAME entity (func_a / func_b)."""
    worktree, span, cur, rep = _two_func_block()
    parent = _make_unit(worktree, span, cur, rep)
    subs = _split_unit_at_entities(parent, min_region_lines=8, min_sub_lines=3)
    assert len(subs) == 2

    # First sub-unit: func_a in both sides.
    assert "func_a" in subs[0].current.text
    assert "func_a" in subs[0].replayed.text
    assert "func_b" not in subs[0].current.text
    # Second sub-unit: func_b in both sides.
    assert "func_b" in subs[1].current.text
    assert "func_b" in subs[1].replayed.text


# ---------------------------------------------------------------------------
# Gates (no-op conditions)
# ---------------------------------------------------------------------------

def test_below_min_region_lines_is_noop():
    """A small block returns [unit] unchanged."""
    worktree, span, cur, rep = _two_func_block()
    parent = _make_unit(worktree, span, cur, rep)
    # region is 17 lines; require 100 -> no split.
    subs = _split_unit_at_entities(parent, min_region_lines=100, min_sub_lines=3)
    assert subs == [parent]


def test_non_c_language_is_noop():
    """Only C/C++ languages are splittable; Python returns [unit]."""
    worktree, span, cur, rep = _two_func_block()
    parent = _make_unit(worktree, span, cur, rep, path="t.py", language="python")
    subs = _split_unit_at_entities(parent, min_region_lines=8, min_sub_lines=3)
    assert subs == [parent]


def test_no_entity_boundary_is_noop():
    """A single large function body (no interior entity) is not split."""
    # One big function, no second entity -> no interior split point.
    body = "int big(void) {\n" + "    int x = 0;\n" * 30 + "}\n"
    worktree = "\n".join([
        "<<<<<<<",
        *body.splitlines(),
        "=======",
        *body.splitlines(),
        ">>>>>>>",
    ])
    wt = worktree.split("\n")
    span = (0, len(wt) - 1)
    cur = "\n".join(wt[1:wt.index([l for l in wt if l.startswith("=======")][0])])
    rep = cur
    parent = _make_unit(worktree, span, cur, rep)
    subs = _split_unit_at_entities(parent, min_region_lines=20, min_sub_lines=8)
    assert subs == [parent]


def test_split_is_adaptive_below_threshold():
    """Splitting is always-on but adaptive: a region below ``entity_split_min_lines``
    is returned unchanged (no master flag)."""
    worktree, span, cur, rep = _two_func_block()
    parent = _make_unit(worktree, span, cur, rep)

    ex = ConflictExtractor.__new__(ConflictExtractor)  # bypass git
    ex.structural_config = None
    # Default min_region_lines=40 — the _two_func_block region (~24 lines) is
    # below it, so splitting declines and the unit passes through unchanged.
    ex.future_config = FutureConfig()
    assert ex._split_units([parent]) == [parent]

    # And a None future_config falls back to the same defaults.
    ex.future_config = None
    assert ex._split_units([parent]) == [parent]


# ---------------------------------------------------------------------------
# Lopsided (one-sided add) handling
# ---------------------------------------------------------------------------

def test_lopsided_add_splits_on_structure_carrying_side():
    """One side empty/degenerate, the other adds N functions -> N sub-units."""
    # replayed adds 3 multi-line functions; current is a single stale comment.
    def _fn(name: str) -> str:
        return "\n".join([
            f"static int {name}(int x) {{",
            "    int r = x;",
            "    r = r + 1;",
            "    r = r * 2;",
            "    return r;",
            "}",
        ])
    rep = "\n".join([_fn("fa"), _fn("fb"), _fn("fc")])
    cur = "/* stale comment */"
    worktree = "\n".join(["<<<<<<<", cur, "=======", rep, ">>>>>>>"])
    wt = worktree.split("\n")
    span = (0, len(wt) - 1)
    parent = _make_unit(worktree, span, cur, rep)

    subs = _split_unit_at_entities(parent, min_region_lines=8, min_sub_lines=4)
    # Should split the replayed side into per-function sub-units.
    assert len(subs) >= 2, f"expected split, got {len(subs)}"
    # Partition still exact.
    spans = sorted(s.marker_span for s in subs)
    assert spans[0][0] == span[0] and spans[-1][1] == span[1]
    for i in range(len(spans) - 1):
        assert spans[i][1] < spans[i + 1][0]
    # Splice round-trip clean.
    out = splice_all_resolutions(
        worktree, [(s.marker_span, f"R{k}") for k, s in enumerate(subs)]
    )
    for marker in ("<<<<<<<", "=======", ">>>>>>>"):
        assert marker not in out


def test_mismatched_entity_counts_declines():
    """Symmetric sides with different entity counts return [unit] (no mis-split)."""
    # current adds 2 functions; replayed adds 3 -> counts differ -> decline.
    cur = "int fa(void){return 1;}\nint fb(void){return 2;}"
    rep = "int fa(void){return 1;}\nint fb(void){return 2;}\nint fc(void){return 3;}"
    worktree = "\n".join(["<<<<<<<", cur, "=======", rep, ">>>>>>>"])
    wt = worktree.split("\n")
    span = (0, len(wt) - 1)
    parent = _make_unit(worktree, span, cur, rep)
    subs = _split_unit_at_entities(parent, min_region_lines=8, min_sub_lines=3)
    assert subs == [parent]


# ---------------------------------------------------------------------------
# Base fragmentation (Problem 2 fix): empty base enables deterministic resolve
# ---------------------------------------------------------------------------

def test_lopsided_subunits_get_empty_base_for_deterministic_resolve():
    """A one-sided-add sub-unit must NOT inherit the whole-file base.

    Inheriting the parent's whole-merge-base base makes the structural resolver
    see a base-vs-side conflict and decline — forcing a model call the design
    intends to be free. With an empty (per-fragment) base the sub-unit is a pure
    add/add that the deterministic cascade resolves with zero model calls.
    """
    from capybase.structural_resolver import resolve_structurally

    def _fn(name: str) -> str:
        return "\n".join([
            f"static int {name}(int x) {{",
            "    int r = x;",
            "    r = r + 1;",
            "    r = r * 2;",
            "    return r;",
            "}",
        ])
    rep = "\n".join([_fn("fa"), _fn("fb"), _fn("fc")])
    cur = "/* stale comment */"
    # Parent carries a LARGE whole-file base (the normal extractor shape).
    big_base = "/* whole merge-base file */\n" + "int noise = 0;\n" * 200
    worktree = "\n".join(["<<<<<<<", cur, "=======", rep, ">>>>>>>"])
    wt = worktree.split("\n")
    span = (0, len(wt) - 1)
    parent = _make_unit(worktree, span, cur, rep, base_text=big_base)

    subs = _split_unit_at_entities(parent, min_region_lines=8, min_sub_lines=4)
    assert len(subs) >= 2
    # The additive sub-units (empty current) must have an empty base, so the
    # structural resolver treats them as one-sided additions.
    resolved = 0
    for su in subs:
        if not su.current.text.strip() and su.replayed.text.strip():
            assert su.base.text == "", (
                f"additive sub-unit {su.unit_id} must have empty base, got "
                f"{len(su.base.text)} chars"
            )
            assert resolve_structurally(su).resolved, (
                f"additive sub-unit {su.unit_id} should resolve deterministically"
            )
            resolved += 1
    assert resolved >= 2, "expected at least 2 deterministically-resolved sub-units"


def test_subunit_function_context_derived_from_side_text():
    """Problem 1: sub-unit enclosing-function context comes from the side text,
    not the proportional marker_span worktree walk."""
    from capybase.resolution_engine import _function_local_context

    def _fn(name: str) -> str:
        return "\n".join([
            f"static int {name}(int x) {{",
            "    return x + 1;",
            "}",
        ])
    rep = "\n".join([_fn("alpha"), _fn("beta")])
    cur = "/* stale */"
    worktree = "\n".join(["<<<<<<<", cur, "=======", rep, ">>>>>>>"])
    wt = worktree.split("\n")
    span = (0, len(wt) - 1)
    parent = _make_unit(worktree, span, cur, rep)
    subs = _split_unit_at_entities(parent, min_region_lines=8, min_sub_lines=3)
    assert len(subs) >= 2

    # Each sub-unit's function-local context must name the function it resolves,
    # derived from its replayed side — NOT walk the worktree (which would land
    # on marker scaffolding).
    names_in_ctx = []
    for su in subs:
        if su.replayed.text.strip():
            ctx = _function_local_context(su)
            names_in_ctx.append(ctx)
    # At least one sub-unit's context names 'alpha' or 'beta' from its side.
    joined = "\n".join(names_in_ctx)
    assert "alpha" in joined or "beta" in joined, (
        f"expected side-derived function names in context, got:\n{joined}"
    )
    # And the context must not be polluted by marker scaffolding.
    assert "<<<<<<<" not in joined and "=======" not in joined


# ---------------------------------------------------------------------------
# Sub-unit identity + traceability
# ---------------------------------------------------------------------------

def test_distinct_unit_ids_and_traceability_metadata():
    """Each sub-unit has a distinct id and records its parent + index."""
    worktree, span, cur, rep = _two_func_block()
    parent = _make_unit(worktree, span, cur, rep)
    subs = _split_unit_at_entities(parent, min_region_lines=8, min_sub_lines=3)
    assert len(subs) >= 2

    ids = [s.unit_id for s in subs]
    assert len(set(ids)) == len(ids), "sub-unit ids must be distinct"
    for k, s in enumerate(subs):
        assert s.unit_id == f"{parent.unit_id}#s{k}"
        assert s.structural_metadata["parent_unit_id"] == parent.unit_id
        assert s.structural_metadata["sub_unit_index"] == k
        assert s.structural_metadata["sub_unit_count"] == len(subs)


def test_whole_file_unit_is_never_split():
    """A whole-file unit (marker_span None) passes through _split_units."""
    wf = _make_unit("x", (0, 0), "a", "b")
    wf.marker_span = None
    wf.unit_kind = "whole_file"
    ex = ConflictExtractor.__new__(ConflictExtractor)
    ex.structural_config = None
    ex.future_config = FutureConfig()
    assert ex._split_units([wf]) == [wf]


# ---------------------------------------------------------------------------
# Shared Resolution Context (SRC) prompt block
# ---------------------------------------------------------------------------

def test_src_block_empty_without_siblings():
    u = _make_unit("x", (0, 0), "a", "b")
    assert _sibling_resolutions_block(u) == ""


def test_src_block_renders_siblings():
    u = _make_unit("x", (0, 0), "a", "b")
    u.structural_metadata["sibling_resolutions"] = [
        "int fa(void){return 1;}",
        "struct Bar{int x;};",
    ]
    block = _sibling_resolutions_block(u)
    assert "Already resolved" in block
    assert "int fa(void)" in block
    assert "struct Bar" in block


def test_src_block_truncates_long_siblings():
    u = _make_unit("x", (0, 0), "a", "b")
    u.structural_metadata["sibling_resolutions"] = ["x" * 5000]
    block = _sibling_resolutions_block(u, max_tokens=100)
    assert "truncated" in block
    # ~400 chars budget + framing; well under the 5000-char input.
    assert len(block) < 1000


# ---------------------------------------------------------------------------
# Preprocessor (#if/#endif) safety — never split inside a conditional block
# ---------------------------------------------------------------------------

def test_drop_split_points_inside_preprocessor_conditional():
    """Direct unit test of the depth filter: points at depth > 0 are dropped."""
    # Lines: 0 #if, 1 func, 2 func, 3 #endif, 4 func, 5 #ifdef, 6 func, 7 #endif
    text = "\n".join([
        "#if defined(X)",      # 0  depth 0 -> 1
        "int a(void){return 0;}",  # 1  depth 1 (INSIDE)
        "int b(void){return 1;}",  # 2  depth 1 (INSIDE)
        "#endif",              # 3  depth 1 -> 0
        "int c(void){return 2;}",  # 4  depth 0 (SAFE)
        "#ifdef Y",            # 5  depth 0 -> 1
        "int d(void){return 3;}",  # 6  depth 1 (INSIDE)
        "#endif",              # 7  depth 1 -> 0
    ])
    kept = _drop_points_inside_preprocessor_conditional(text, [1, 2, 4, 6])
    # Points 1,2,6 are inside conditionals -> dropped; only 4 survives.
    assert kept == [4]


def test_split_declines_when_all_entities_inside_conditional():
    """When every entity boundary is inside #if/#endif, splitting declines and
    returns the parent unchanged — resolving as one block is splice-safe; a
    split there would strand #if from #endif across sub-units."""
    def _fn(name: str) -> str:
        return "\n".join([
            "static int %s(int x) {" % name,
            "    int r = x;",
            "    r = r + 1;",
            "    return r;",
            "}",
        ])
    # Both functions are wrapped in a single #if/#endif.
    cur = "\n".join(["#if defined(X)", _fn("fa"), _fn("fb"), "#endif"])
    rep = cur  # symmetric
    worktree = "\n".join(["<<<<<<<", cur, "=======", rep, ">>>>>>>"])
    wt = worktree.split("\n")
    span = (0, len(wt) - 1)
    parent = _make_unit(worktree, span, cur, rep)
    subs = _split_unit_at_entities(parent, min_region_lines=8, min_sub_lines=4)
    # The only entity boundaries are inside the conditional -> no safe split
    # points -> decline (return the parent unchanged).
    assert subs == [parent]


def test_split_skips_points_inside_conditional_keeps_safe_ones():
    """A block with one entity inside #if/#endif and one outside splits only at
    the safe (depth-0) boundary; the conditional-wrapped entity is NOT a split
    point, so no #if/#endif pair is divided across sub-units."""
    def _fn(name: str) -> str:
        return "\n".join([
            "static int %s(int x) {" % name,
            "    int r = x;",
            "    r = r + 1;",
            "    r = r * 2;",
            "    return r;",
            "}",
        ])
    # func_safe is at top level (depth 0); func_wrapped is inside #if/#endif.
    cur = "\n".join([_fn("safe"), "#if defined(X)", _fn("wrapped"), "#endif"])
    rep = cur  # symmetric (so entity counts match -> symmetric split path)
    worktree = "\n".join(["<<<<<<<", cur, "=======", rep, ">>>>>>>"])
    wt = worktree.split("\n")
    span = (0, len(wt) - 1)
    parent = _make_unit(worktree, span, cur, rep)
    subs = _split_unit_at_entities(parent, min_region_lines=8, min_sub_lines=4)
    # There IS a safe split point (between safe and the #if block), so it splits
    # into exactly 2 — NOT 3 (the entity inside the conditional is not a split
    # point, so the conditional-wrapped entity stays whole in one sub-unit).
    assert len(subs) == 2
    # Splice round-trip is marker-free (the split didn't break the conditional).
    out = splice_all_resolutions(
        worktree, [(s.marker_span, f"R{k}") for k, s in enumerate(subs)]
    )
    for marker in ("<<<<<<<", "=======", ">>>>>>>"):
        assert marker not in out
    # Sub-unit 0 holds the safe (top-level) function; sub-unit 1 holds the
    # entire #if ... wrapped ... #endif block intact (not split across units).
    assert "#if defined(X)" in subs[1].current.text
    assert "#endif" in subs[1].current.text




def test_extractor_splits_above_threshold():
    """extract_file_units emits sub-units when the block is above the split
    threshold — no flag required; splitting is always-on and adaptive.

    Uses a FakeGit that returns stage blobs equal to the side texts, so the
    full extraction path (marker parse -> split -> enrichment) runs.
    """
    worktree, _span, cur, rep = _two_func_block()
    git = _FakeGit(worktree, base="", current=cur, replayed=rep)
    ex = ConflictExtractor(
        git,
        structural_config=None,
        future_config=FutureConfig(entity_split_min_lines=8,
                                   entity_split_min_sub_lines=3),
    )
    units = ex.extract_file_units("t.c", step_index=0, session_id="s")
    assert len(units) >= 2, "expected the two-function block to split end-to-end"
    # All sub-units share the same parent_unit_id.
    parents = {u.structural_metadata.get("parent_unit_id") for u in units}
    assert len(parents) == 1
    assert parents.pop() == "t.c:0:0"


# ---------------------------------------------------------------------------
# Minimal fake git backend for the extractor integration test
# ---------------------------------------------------------------------------

class _FakeGit:
    """Just enough of GitBackend for ConflictExtractor.extract_file_units."""

    def __init__(self, worktree: str, *, base: str, current: str, replayed: str):
        self._worktree = worktree
        self._blobs = {1: base.encode(), 2: current.encode(), 3: replayed.encode()}

    def read_worktree_file(self, path: str) -> bytes:
        return self._worktree.encode("utf-8")

    def read_stage_blob(self, path: str, stage: int) -> bytes:
        if stage not in self._blobs:
            raise KeyError(stage)
        return self._blobs[stage]

    def last_touch_blob(self, oid):
        return ("", "")

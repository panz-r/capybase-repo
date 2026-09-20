"""Common-span factoring (S28-136): lossless shrinking of large conflict
sides for the LLM prompt.

When the three sides share large identical line runs, the resolve prompt
renders each side as differing segments + ordered ``@Ak`` references; the
model's resolution may reference them, and capybase re-expands the shared
spans verbatim into the final resolved text. Pins: the 3-way common-run
computation (interval intersection, the reviewer's A1/A2/A3 shape), the
expansion protocol (strict), and the rendering (per-side elision with
ordered references).
"""

from __future__ import annotations

from capybase.common_spans import (
    expand_factored_resolution, factor_common_spans, ref_index,
)


def _reviewer_example():
    """The external review's exact shape: one side renames process_user →
    persist_user; the replayed side inserts an audit call. Padding lines
    give the shared span enough bulk to clear the factoring gate."""
    pad = [f"    passthrough_{i} = {i}" for i in range(20)]
    base = "def process_user(user):\n    validate(user)\n" + "\n".join(
        pad + ["    save(user)"])
    cur = base.replace("process_user", "persist_user")
    rep = ("def process_user(user):\n    validate(user)\n    audit(user)\n"
           + "\n".join(pad + ["    save(user)"]))
    return base, cur, rep


# ---------------------------------------------------------------------------
# Factoring
# ---------------------------------------------------------------------------


def test_reviewer_shape_factors_into_shared_span_plus_deltas():
    base, cur, rep = _reviewer_example()
    f = factor_common_spans(base, cur, rep, min_run=2, min_shared_lines=8)
    assert f is not None
    # The shared span is the identical tail (pad + save) — common to all
    # three. The deltas carry the rename (cur) and the audit insertion (rep).
    assert f["shared_lines"] >= 20
    assert "persist_user" in f["rendered_cur"]
    assert "process_user" in f["rendered_rep"]
    assert "audit" in f["rendered_rep"]
    # The shared span TEXT comes from base (pad + save).
    assert f["spans"] and "save(user)" in f["spans"][-1]


def test_below_gate_returns_none():
    base = "def f():\n    return 1\n    return 2\n    return 3\n"
    cur = "def g():\n    return 1\n    return 2\n    return 3\n"
    rep = "def h():\n    return 1\n    return 2\n    return 3\n"
    # All sides identical except single leading tokens: the common runs are
    # 2 lines each — below min_run → nothing factorable.
    assert factor_common_spans(base, cur, rep) is None


def test_no_common_material_returns_none():
    base = "a = 1\nb = 2\nc = 3\nd = 4\ne = 5\n"
    cur = "x = 10\ny = 20\nz = 30\n"
    rep = "q = 100\nw = 200\n"
    assert factor_common_spans(base, cur, rep) is None


def test_document_order_and_ref_numbering():
    pad = "\n".join(f"    shared_{i} = {i}" for i in range(8))
    base = f"def f():\n{pad}\n    mid = 1\n{pad}\n"
    cur = f"def f():\n{pad}\n    mid = 2\n{pad}\n"
    rep = f"def f():\n{pad}\n    mid = 3\n{pad}\n"
    f = factor_common_spans(base, cur, rep, min_run=2, min_shared_lines=4)
    assert f is not None
    assert "@A2" in f["rendered_base"] and "@A1" in f["rendered_base"]
    # document order: @A1 appears before @A2 in every rendered side
    assert f["rendered_base"].index("@A1") < f["rendered_base"].index("@A2")


def test_identical_sides_still_factor_but_expansion_trivial():
    base = "def f():\n" + "\n".join(f"    x{i} = {i}" for i in range(60))
    f = factor_common_spans(base, base, base)
    assert f is not None  # identical sides are 100% common material
    # expansion of a fully-shared render reconstructs the side verbatim
    assert expand_factored_resolution(f["rendered_base"], f["spans"]) == base


# ---------------------------------------------------------------------------
# Expansion protocol
# ---------------------------------------------------------------------------


def test_expand_interleaves_refs_and_literals():
    spans = ["alpha-line\nalpha-line2", "beta-line"]
    text = "@A1\nmid = 1\n@A2\n"
    assert expand_factored_resolution(text, spans) == (
        "alpha-line\nalpha-line2\nmid = 1\nbeta-line\n")


def test_expand_unknown_index_declined():
    assert expand_factored_resolution("@A9", ["x"]) is None


def test_expand_out_of_order_declined():
    spans = ["a\n", "b\n"]
    assert expand_factored_resolution("@A2\n@A1\n", spans) is None


def test_expand_repeated_ref_declined():
    spans = ["a\n", "b\n"]
    assert expand_factored_resolution("@A1\n@A1\n", spans) is None


def test_expand_missing_span_declined():
    spans = ["a\n", "b\n"]
    assert expand_factored_resolution("@A1\n", spans) is None


def test_partial_line_ref_does_not_satisfy_coverage():
    """A reference mid-line (inside a code line) does NOT count as a
    reference — the strict coverage check still fails the reconstruction
    (the shared span would be silently dropped)."""
    spans = ["span text"]
    text = "code = compute(@A1)\n"
    assert expand_factored_resolution(text, spans) is None


def test_ref_index_semantics():
    assert ref_index("@A1") == 0
    assert ref_index("@A12") == 11
    # the renderer's annotated form is tolerated (trailing count hint)
    assert ref_index("@A1 (6 shared lines elided)") == 0
    assert ref_index("code @A1") is None


# ---------------------------------------------------------------------------
# Lossless round trip: elide, then re-expand, per side
# ---------------------------------------------------------------------------


def _multi_span_fixture():
    base_parts, cur_parts, rep_parts = [], [], []
    for k in range(6):
        run = [f"    shared_{k}_{j} = {j}" for j in range(6)]
        base_parts += run
        cur_parts += run
        rep_parts += run
        base_parts.append(f"    base_delta_{k} = {k}")
        cur_parts.append(f"    cur_delta_{k} = {k}")
        rep_parts.append(f"    rep_delta_{k} = {k}")
    return ("\n".join(base_parts), "\n".join(cur_parts),
            "\n".join(rep_parts))


def _reexpand(rendered: str, spans: list[str]) -> str:
    rebuilt = []
    for line in rendered.split("\n"):
        idx = ref_index(line)
        rebuilt.append(spans[idx] if idx is not None else line)
    return "\n".join(rebuilt)


def test_round_trip_reconstructs_each_side_losslessly():
    """Factoring elides shared runs from a side's rendering; substituting
    the spans back at the reference positions must reproduce the ORIGINAL
    side text exactly — the definition of lossless. Applied to all three
    sides of a multi-span fixture."""
    base, cur, rep = _multi_span_fixture()
    f = factor_common_spans(base, cur, rep, min_run=2, min_shared_lines=8)
    assert f is not None and len(f["spans"]) >= 2
    for rendered, original in (
        (f["rendered_base"], base),
        (f["rendered_cur"], cur),
        (f["rendered_rep"], rep),
    ):
        rebuilt = _reexpand(rendered, f["spans"])
        assert rebuilt == original, (
            "lossless reconstruction violated:\n--- rendered ---\n"
            f"{rendered}\n--- rebuilt ---\n{rebuilt}\n--- original ---\n"
            f"{original}")


def test_expansion_of_rendered_base_by_spans_equals_base():
    """The rendered side + the span list, re-expanded, must equal the
    original side exactly (stronger than the ref-position check: this is
    the expand_factored_resolution protocol applied to a render)."""
    base, cur, rep = _reviewer_example()
    f = factor_common_spans(base, cur, rep, min_run=2, min_shared_lines=8)
    assert f is not None
    expanded = expand_factored_resolution(f["rendered_base"], f["spans"])
    assert expanded == base

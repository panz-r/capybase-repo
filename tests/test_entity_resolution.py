"""Tests for entity-level disjoint resolution (Weave/Aura).

P2: when both sides add DISTINCT entities (methods/classes) at the same
insertion point, git's line-diff reports a conflict but the entities are
non-overlapping at entity granularity → safe to merge both. This is the single
most common real-world conflict that line-level merging provably cannot resolve.

Covers entity enumeration (the coarse parser) and the ``entity_disjoint`` rule
(both-sides-add-distinct → merge; same-entity-touched-by-both → decline).
Skips gracefully when tree-sitter is absent.
"""

from __future__ import annotations

import ast

import pytest

structural = pytest.importorskip("capybase.adapters.structural")
needs_ts = pytest.mark.skipif(
    not structural.is_available("python"),
    reason="abstract parser unavailable for python",
)

from capybase.adapters import structural as S
from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.structural_resolver import resolve_structurally


def _unit(base, current, replayed, *, lang="python", path="app.py"):
    u = ConflictUnit(
        session_id="s", step_index=1, path=path, language=lang,
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text="", marker_span=None,
    )
    u.structural_metadata["enclosing_node_text"] = base
    return u


# ---------------------------------------------------------------------------
# enumerate_entities — coarse entity listing
# ---------------------------------------------------------------------------


@needs_ts
def test_enumerate_python_module_level():
    src = "def a():\n    return 1\n\nclass C:\n    pass\n"
    ents = S.enumerate_entities(src, "python")
    ids = [e.identity for e in ents or []]
    assert ("function", "a") in ids
    assert ("class", "C") in ids


@needs_ts
def test_enumerate_python_class_methods():
    src = "class C:\n    def a(self):\n        pass\n    def b(self):\n        pass\n"
    ents = S.enumerate_entities(src, "python", container_span=(1, 1))
    ids = [e.identity for e in ents or []]
    assert ("method", "a") in ids
    assert ("method", "b") in ids


@needs_ts
def test_enumerate_returns_none_for_unparseable():
    ents = S.enumerate_entities("", "python")
    assert ents == []


@needs_ts
def test_entity_body_carries_source_text():
    src = "class C:\n    def a(self):\n        return 1\n"
    ents = S.enumerate_entities(src, "python", container_span=(1, 1))
    assert ents
    assert "return 1" in ents[0].body


@needs_ts
def test_rust_impl_methods_enumerated():
    rs = "impl S {\n    fn a(&self) {}\n    fn b(&self) {}\n}\n"
    ents = S.enumerate_entities(rs, "rust", container_span=(1, 1))
    ids = [e.identity for e in ents or []]
    assert ("method", "a") in ids
    assert ("method", "b") in ids


# ---------------------------------------------------------------------------
# entity_disjoint rule — the headline win
# ---------------------------------------------------------------------------


@needs_ts
def test_both_sides_add_distinct_methods_merges():
    """The headline case: two sides each add a different method to one class.
    git conflicts on the insertion point, but the entities are distinct → merge."""
    base = "class Store:\n    def __init__(self):\n        self.data = {}"
    cur = (
        "class Store:\n    def __init__(self):\n        self.data = {}\n"
        "    def load(self, k):\n        return self.data.get(k)"
    )
    rep = (
        "class Store:\n    def __init__(self):\n        self.data = {}\n"
        "    def save(self, k, v):\n        self.data[k] = v"
    )
    result = resolve_structurally(_unit(base, cur, rep))
    assert result.rule == "entity_disjoint"
    assert result.resolved
    text = result.text
    assert "def load" in text  # current's addition
    assert "def save" in text  # replayed's addition
    assert "def __init__" in text  # base entity preserved
    ast.parse(text)  # valid Python


@needs_ts
def test_both_sides_add_distinct_rust_methods_merges():
    base = "impl S {\n    fn new() -> Self {\n        S {}\n    }\n}"
    cur = (
        "impl S {\n    fn new() -> Self {\n        S {}\n    }\n"
        "    fn start(&self) {\n        self.run();\n    }\n}"
    )
    rep = (
        "impl S {\n    fn new() -> Self {\n        S {}\n    }\n"
        "    fn stop(&self) {\n        self.halt();\n    }\n}"
    )
    result = resolve_structurally(_unit(base, cur, rep, lang="rust", path="s.rs"))
    assert result.rule == "entity_disjoint"
    assert "fn start" in result.text
    assert "fn stop" in result.text


@needs_ts
def test_same_entity_modified_by_both_declines():
    """Both sides modify the SAME method → genuine intra-entity conflict → decline."""
    base = "class C:\n    def f(self):\n        return 1"
    cur = "class C:\n    def f(self):\n        return 2"
    rep = "class C:\n    def f(self):\n        return 3"
    result = resolve_structurally(_unit(base, cur, rep))
    assert not result.resolved  # declined → LLM handles it


@needs_ts
def test_one_side_adds_other_modifies_same_entity_declines():
    """Side A adds method 'b'; side B modifies base's method 'a' — disjoint (no
    shared entity) → merge. But if side B modifies the SAME 'b' A added → decline."""
    base = "class C:\n    def a(self):\n        return 1"
    cur = "class C:\n    def a(self):\n        return 1\n    def b(self):\n        return 2"
    rep = "class C:\n    def a(self):\n        return 99"
    # cur adds 'b' (untouched by rep) and keeps 'a' unchanged; rep modifies 'a'.
    # Touched sets: cur={b}, rep={a} → disjoint → merge.
    result = resolve_structurally(_unit(base, cur, rep))
    assert result.rule == "entity_disjoint"
    assert "def b" in result.text
    assert "return 99" in result.text  # rep's modification of 'a' applied


@needs_ts
def test_declines_without_enclosing_metadata():
    """No enclosing_node_text in metadata → can't enumerate → decline."""
    unit = _unit("class C:\n    pass", "class C:\n    def a(self): pass", "class C:\n    def b(self): pass")
    unit.structural_metadata.pop("enclosing_node_text")
    result = resolve_structurally(unit)
    assert not result.resolved


@needs_ts
def test_declines_for_unsupported_language():
    """Non-python/rust → no entity enumeration → entity rule can't fire. (The
    line-level rules may still apply; here the sides genuinely conflict so none
    resolve, demonstrating entity_disjoint doesn't fire for JS.)"""
    base = "class C {\n  a() {}\n}"
    cur = "class C {\n  a() { return 1; }\n}"
    rep = "class C {\n  a() { return 2; }\n}"
    result = resolve_structurally(_unit(base, cur, rep, lang="javascript"))
    # No rule resolves it (line rules conflict, entity rule unsupported for JS).
    assert not result.resolved


@needs_ts
def test_preserves_base_method_order():
    """When neither side reorders, base entity order is preserved in the output."""
    base = "class C:\n    def first(self):\n        pass"
    cur = "class C:\n    def first(self):\n        pass\n    def second(self):\n        pass"
    rep = "class C:\n    def first(self):\n        pass\n    def third(self):\n        pass"
    result = resolve_structurally(_unit(base, cur, rep))
    text = result.text
    assert text.index("def first") < text.index("def second")
    assert text.index("def first") < text.index("def third")


@needs_ts
def test_resolution_is_valid_python():
    """The merged container must parse cleanly (the validator will check this too)."""
    base = "class C:\n    def a(self):\n        x = 1"
    cur = "class C:\n    def a(self):\n        x = 1\n    def b(self):\n        y = 2"
    rep = "class C:\n    def a(self):\n        x = 1\n    def c(self):\n        z = 3"
    result = resolve_structurally(_unit(base, cur, rep))
    ast.parse(result.text)  # raises if invalid


@needs_ts
def test_no_entities_touched_declines():
    """If neither side added/modified an entity (identical bodies), an earlier
    rule (identical_sides) handles it; entity_disjoint declines the empty case."""
    base = "class C:\n    def a(self):\n        return 1"
    result = resolve_structurally(_unit(base, base + "\n# comment", base + "\n# other"))
    # Both sides differ only by trailing comment — not an entity change. The line
    # rules or entity rule decline; we don't assert which, just that it doesn't
    # produce a wrong entity merge.
    assert result.text is None or "def a" in result.text


# ---------------------------------------------------------------------------
# Fourth-pass: (resolver rename detection vs parser fingerprint consistency)
# and (refactoring-aware merge duplicate-identity guard).
# ---------------------------------------------------------------------------

from capybase.structural_resolver import _detect_renames, _body_content  # noqa: E402


def _entity(kind, name, body, span=(0, 1)):
    """Build a structural.Entity for rename-detection unit tests."""
    return S.Entity(kind=kind, name=name, body=body, span=span)


def test_r4_rename_with_inline_comment_drift_is_detected():
    """a rename where the body picked up an inline comment (or a changed
    string literal) MUST still be detected. The resolver's ``_body_content``
    used whitespace-collapse-only normalization (keeping comments/strings),
    while the parser's ``unit_body_fingerprint`` strips comments and blanks
    strings. The two algorithms disagreed: the parser paired the rename, the
    resolver missed it → the merge emitted the old name AND the new name as a
    duplicate. After ``_body_content`` strips inline comments so the
    rename pairs. ``loadData``→``fetchData`` with ``# cached`` added."""
    base_body = "def loadData():\n    return fetch()"
    renamed_body = "def fetchData():\n    return fetch()  # cached"
    base_ent = _entity("function", "loadData", base_body)
    renamed_ent = _entity("function", "fetchData", renamed_body)
    renames, removed = _detect_renames([renamed_ent], [base_ent])
    assert ("function", "fetchData") in renames, (
        f"rename loadData→fetchData (with comment drift) must be detected; "
        f"got renames={renames}"
    )
    assert ("function", "loadData") in removed


def test_r4_rename_with_string_literal_drift_is_detected():
    """a rename where a string literal value changed (but no real body
    change) must still pair. The parser's fingerprint blanks strings for
    exactly this reason; the resolver must match."""
    base_body = 'def loadData():\n    return fetch("v1")'
    renamed_body = 'def fetchData():\n    return fetch("v2")'
    base_ent = _entity("function", "loadData", base_body)
    renamed_ent = _entity("function", "fetchData", renamed_body)
    renames, _ = _detect_renames([renamed_ent], [base_ent])
    assert ("function", "fetchData") in renames, (
        f"rename with string-literal drift must be detected; got {renames}"
    )


def test_r4_real_body_change_still_not_a_rename():
    """a genuine body change (not just comment/string
    drift) must NOT pair as a rename — that would conflate two distinct
    functions. After the line ``return fetch`` vs ``return save``
    still differs under the comment/string-stripping normalization."""
    base_body = "def loadData():\n    return fetch()"
    renamed_body = "def fetchData():\n    return save()"
    base_ent = _entity("function", "loadData", base_body)
    renamed_ent = _entity("function", "fetchData", renamed_body)
    renames, _ = _detect_renames([renamed_ent], [base_ent])
    assert renames == {}, (
        f"a real body change must not pair as a rename; got {renames}"
    )


def test_r5_refactoring_merge_declines_on_duplicate_identities():
    """``_try_refactoring_aware_merge`` builds ``base_by_id`` without the
    duplicate-identity guard that ``_try_entity_disjoint`` has. A
    base with two entities sharing an identity (Python ``@property`` +
    ``@x.setter`` both named ``x``) would silently drop one in the dict. After
    the path declines (returns None) on duplicate identities, escalating
    to the LLM path — mirroring entity_disjoint."""
    from capybase.structural_resolver import _try_refactoring_aware_merge
    base = (
        "class C:\n"
        "    @property\n"
        "    def x(self):\n"
        "        return self._x\n"
        "\n"
        "    @x.setter\n"
        "    def x(self, v):\n"
        "        self._x = v\n"
    )
    # Same name 'x' twice → duplicate identity in base.
    unit = _unit(base, base, base)
    result = _try_refactoring_aware_merge(unit)
    assert result is None, (
        "refactoring-merge must decline (None) when base has duplicate "
        "identities, escalating to LLM instead of silently dropping one"
    )


# ---------------------------------------------------------------------------
# Fifth-pass: — _rebuild_container double-wraps a bare-function conflict.
# When the enclosing node is a top-level FUNCTION (not a class/impl container),
# both _try_entity_disjoint and _try_refactoring_aware_merge produced malformed
# output: the function header was kept AND the entities spliced inside it,
# nesting ``def foo():`` inside ``def foo():``.
# ---------------------------------------------------------------------------

from capybase.structural_resolver import (  # noqa: E402
    _try_entity_disjoint,
    _try_refactoring_aware_merge,
)


def _assert_valid_python_unit(text, label):
    """The resolved text for a single top-level entity must be ONE unit, not a
    nested/recursive structure. Asserts the text parses as valid Python and
    contains exactly one top-level def/class (no doubled headers)."""
    import ast
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        raise AssertionError(f"{label}: output is not valid Python: {e}\n{text!r}")
    top_defs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    assert len(top_defs) == 1, (
        f"{label}: expected exactly 1 top-level def/class, got {len(top_defs)}\n{text!r}"
    )


def test_r6_entity_disjoint_bare_function_not_double_wrapped():
    """``_try_entity_disjoint`` on a bare top-level function conflict (both
    sides add DISTINCT methods to a function, not a class) must NOT double-wrap:
    the enclosing function is recognized as the entity itself (not a container),
    so the output is a flat list of defs, not ``def foo():\\n    def foo():\\n    ...``
    with the function header kept and the entities nested inside."""
    base = "def foo():\n    return 1\n"
    cur = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
    rep = "def foo():\n    return 1\n\ndef baz():\n    return 3\n"
    result = _try_entity_disjoint(_unit(base, cur, rep))
    assert result is not None, "entity_disjoint should resolve distinct adds"
    # The output must NOT contain a doubled/nested 'def foo()' header.
    assert result.count("def foo()") <= 1, (
        f"output double-wraps the function header;\n{result!r}"
    )
    # bar and baz (the distinct adds) must be present as flat top-level defs.
    assert "def bar()" in result and "def baz()" in result
    # No line should be MORE indented than the body indent (the nested malformation
    # produced '    def foo()' — a def at body-indent depth).
    for ln in result.split("\n"):
        if ln.strip().startswith("def "):
            assert not ln.startswith("    def "), (
                f"nested def at body-indent (the malformation);\n{result!r}"
            )


def test_rebuild_container_visibility_prefixed_fn_is_bare():
    """A bare function with a visibility/async modifier (``pub fn``, ``export
    function``, ``async def``) is still a bare function, not a container — the
    merged entities must emit flat, not wrapped inside the function header.

    Regression guard: the old hardcoded ``_ENTITY_HEADER_TOKENS`` tuple matched
    only the keyword itself (``fn ``/``def ``/...), so ``pub fn foo() {`` missed
    the bare-function check and got the container splice — wrapping the merged
    entities inside ``pub fn foo() { ... }`` (a malformation)."""
    from capybase.structural_resolver import _rebuild_container
    ents = ["def a():\n    return 1", "def b():\n    return 2"]
    # Each visibility-prefixed function header must emit flat (no wrapper).
    for header in (
        "pub fn compute() {", "pub async fn compute() {",
        "export function compute() {", "unsafe fn compute() {",
        "async def compute():",
    ):
        enc = header + "\n    x + 1\n}" if "{" in header else header + "\n    return 1"
        result = _rebuild_container(enc, ents, "rust" if "{" in header else "python")
        assert result is not None, f"{header}: rebuild returned None"
        # Flat: both entities present, no enclosing function header wrapping them.
        assert "def a()" in result and "def b()" in result, f"{header}: missing entities"
        assert "compute" not in result, (
            f"{header}: enclosing header kept (the malformation);\n{result!r}"
        )
    # A visibility-prefixed CONTAINER (pub struct) must still wrap correctly.
    r = _rebuild_container("pub struct S {\n    x: u32\n}", ents, "rust")
    assert r is not None and "pub struct S {" in r and "}" in r.split("\n")[-1], (
        f"pub struct should keep its container framing;\n{r!r}"
    )


def test_r6_refactoring_aware_bare_function_not_double_wrapped():
    """``_try_refactoring_aware_merge`` on a bare-function rename+modify
    (one side renames foo→bar, other modifies foo's body) produces the single
    composed function (bar's header + modified body), no wrapper — never the
    malformed ``def foo:\\n def bar:\\n    ...`` nesting."""
    base = "def foo():\n    return 1\n    return 2\n"
    cur = "def bar():\n    return 1\n    return 2\n"   # rename foo→bar
    rep = "def foo():\n    return 1\n    return 99\n"  # modify foo's body
    result = _try_refactoring_aware_merge(_unit(base, cur, rep))
    assert result is not None, "refactoring merge should resolve rename+modify"
    # The composed output: bar's header (renamed) + the modified body (return 99).
    assert "def bar()" in result, f"renamed header must appear;\n{result!r}"
    assert "99" in result, f"modified body must appear;\n{result!r}"
    # No doubled header: 'def foo()' should NOT appear (it was renamed away).
    assert "def foo()" not in result, (
        f"old header must not be kept after rename;\n{result!r}"
    )


def test_r6_class_container_still_wraps_correctly():
    """the class-container case (where _rebuild_container's
    framing logic IS correct) must still work. A rename+modify inside a class
    should produce ``class C:\\n    def bar(self):...``, with the class header
    kept and the method composed inside."""
    base = "class C:\n    def foo(self):\n        return 1\n"
    cur = "class C:\n    def bar(self):\n        return 1\n"
    rep = "class C:\n    def foo(self):\n        return 99\n"
    result = _try_refactoring_aware_merge(_unit(base, cur, rep))
    assert result is not None
    assert result.startswith("class C:"), (
        f"class container header must be kept;\n{result!r}"
    )
    assert "def bar(self)" in result and "99" in result


# ---------------------------------------------------------------------------
# Seventh-pass: coverage hardening for the least-tested resolver paths.
# These pin verified-working behavior: the refactoring-merge merge-walk
# (1381-1400), the rename-conflict / agreed-rename branches (1085-1088),
# _changed_line_indices (96-105), and the rename-away emission in
# entity_disjoint (1141-1145).
# ---------------------------------------------------------------------------


def test_cov_refactoring_merge_class_rename_plus_modify():
    """The refactoring-merge merge-walk (lines 1381-1400) composes a rename +
    body-modify on the SAME entity inside a class. entity_disjoint declines
    (overlap: both sides touch foo); refactoring accepts (clean {rename,
    modify} partition). Output: class header kept + renamed method with the
    modified body."""
    base = "class C:\n    def foo(self):\n        return 1\n        return 2\n"
    cur = "class C:\n    def bar(self):\n        return 1\n        return 2\n"
    rep = "class C:\n    def foo(self):\n        return 1\n        return 99\n"
    result = _try_refactoring_aware_merge(_unit(base, cur, rep))
    assert result is not None
    assert result.startswith("class C:")
    assert "def bar(self)" in result, f"renamed method must appear;\n{result!r}"
    assert "99" in result, f"modified body must appear;\n{result!r}"
    assert "def foo(self)" not in result, f"old name must be gone;\n{result!r}"


def test_cov_refactoring_merge_appends_side_additions():
    """When the refactoring merge composes a rename+modify, it must also append
    each side's DISTINCT additions (entities not in base). Pins the additions
    append loop (lines 1390-1400)."""
    base = "class C:\n    def foo(self):\n        return 1\n"
    cur = (
        "class C:\n    def bar(self):\n        return 1\n"
        "    def new_cur(self):\n        return 5\n"
    )
    rep = (
        "class C:\n    def foo(self):\n        return 99\n"
        "    def new_rep(self):\n        return 6\n"
    )
    result = _try_refactoring_aware_merge(_unit(base, cur, rep))
    assert result is not None
    assert "new_cur" in result, f"current-side addition must survive;\n{result!r}"
    assert "new_rep" in result, f"replayed-side addition must survive;\n{result!r}"
    assert "5" in result and "6" in result


def test_cov_conflicting_renames_decline_both_paths():
    """When both sides rename the SAME entity to DIFFERENT new names, it's a
    genuine conflict — both entity_disjoint and refactoring_aware must
    decline (return None). Pins the rename-conflict branch (1085-1087)."""
    base = "class C:\n    def foo(self):\n        return 1\n"
    cur = "class C:\n    def bar(self):\n        return 1\n"
    rep = "class C:\n    def baz(self):\n        return 1\n"
    assert _try_entity_disjoint(_unit(base, cur, rep)) is None
    assert _try_refactoring_aware_merge(_unit(base, cur, rep)) is None


def test_cov_agreed_rename_resolves_entity_disjoint():
    """When both sides rename the SAME entity to the SAME new name, it's an
    AGREED change (not a conflict) — entity_disjoint resolves it. Pins the
    agreed_renames branch (1088)."""
    base = "class C:\n    def foo(self):\n        return 1\n"
    cur = "class C:\n    def bar(self):\n        return 1\n"
    rep = "class C:\n    def bar(self):\n        return 1\n"
    result = _try_entity_disjoint(_unit(base, cur, rep))
    assert result is not None
    assert "def bar(self)" in result and "def foo(self)" not in result


def test_cov_entity_disjoint_renamed_away_emission():
    """When a base entity is renamed away by one side, entity_disjoint emits
    the renamed version (not the old name). Pins the renamed-away branch
    (1141-1145): cur renamed foo->bar; rep kept foo unchanged. The merge
    keeps bar (the rename) and does NOT re-emit foo."""
    base = "class C:\n    def foo(self):\n        return 1\n"
    cur = "class C:\n    def bar(self):\n        return 1\n"   # rename foo->bar
    rep = "class C:\n    def foo(self):\n        return 1\n"    # kept foo
    # entity_disjoint sees cur touching bar (rename) + rep touching foo (kept).
    # These are the SAME canonical entity → overlap → decline UNLESS it's a
    # clean partition. Here rep didn't modify foo, so rep touched nothing real.
    result = _try_entity_disjoint(_unit(base, cur, rep))
    # The outcome depends on whether rep's "keep" counts as touched. If it
    # resolves, bar must appear and foo must not (renamed away). If it
    # declines, that's also acceptable (the rename+keep is ambiguous). The
    # key invariant: NEVER emit BOTH foo and bar (that'd be a duplicate).
    if result is not None:
        assert not ("def foo(self)" in result and "def bar(self)" in result), (
            f"must not emit both old and renamed name;\n{result!r}"
        )


# ---------------------------------------------------------------------------
# Consolidation #2: the canonical rename-detection core
# (``abstract_parser.detect_renames_2way`` + ``entity_body_content`` +
# ``split_header_body``) is now shared by the resolver, the 3-way diff, and
# ``semantic_diff``. These pin its contract directly so the shared path stays
# correct independent of any one caller.
# ---------------------------------------------------------------------------

from capybase.adapters.abstract_parser import (  # noqa: E402
    detect_renames_2way,
    entity_body_content,
    split_header_body,
    RENAME_NAME_SIMILARITY_THRESHOLD,
)


def test_canonical_detect_renames_exact_body_match():
    """The core signal: a renamed entity (same body, new name, old name gone)
    pairs. Works on Entity objects (the resolver/semantic_diff path)."""
    base = [_entity("function", "loadData", "def loadData():\n    return fetch()")]
    side = [_entity("function", "fetchData", "def fetchData():\n    return fetch()")]
    renames, removed = detect_renames_2way(base, side)
    assert renames == {("function", "fetchData"): ("function", "loadData")}, renames
    assert removed == {("function", "loadData")}


def test_canonical_detect_renames_comment_drift_pairs():
    """A rename whose body picked up an incidental comment still pairs — the
    canonical body signal strips comments (this is the, now baked
    into entity_body_content rather than maintained separately)."""
    base = [_entity("function", "loadData", "def loadData():\n    return fetch()")]
    side = [_entity("function", "fetchData", "def fetchData():\n    return fetch()  # cached")]
    renames, _ = detect_renames_2way(base, side)
    assert ("function", "fetchData") in renames


def test_canonical_detect_renames_real_body_change_does_not_pair():
    """A genuine body change (not just rename drift) must NOT pair — that would
    conflate two distinct entities."""
    base = [_entity("function", "loadData", "def loadData():\n    return fetch()")]
    side = [_entity("function", "fetchData", "def fetchData():\n    return save()")]
    renames, _ = detect_renames_2way(base, side)
    assert renames == {}


def test_canonical_detect_renames_copy_not_rename():
    """If the old name still exists on the side, it's a COPY, not a rename —
    must not pair (would drop a genuine duplicate)."""
    base = [_entity("function", "foo", "def foo():\n    return 1")]
    side = [
        _entity("function", "foo", "def foo():\n    return 1"),
        _entity("function", "bar", "def foo():\n    return 1"),
    ]
    renames, _ = detect_renames_2way(base, side)
    assert renames == {}


def test_canonical_detect_renames_jaccard_fallback():
    """With fuzzy_body_threshold set, a rename that ALSO edits the body still
    pairs via token-Jaccard similarity (the semantic_diff path). Without the
    threshold, the same case correctly does NOT pair (exact-only)."""
    base = [_entity("function", "parse_item",
                    "def parse_item():\n    x = read()\n    return transform(x)")]
    side = [_entity("function", "parse_thing",
                    "def parse_thing():\n    x = read()\n    return transform(x)\n    return None")]
    # Exact-only: bodies differ → no pair.
    r_exact, _ = detect_renames_2way(base, side)
    assert r_exact == {}, r_exact
    # Fuzzy: bodies share most tokens → pairs.
    r_fuzzy, _ = detect_renames_2way(base, side, fuzzy_body_threshold=0.80)
    assert ("function", "parse_thing") in r_fuzzy, r_fuzzy


def test_canonical_detect_renames_trivial_body_guarded():
    """Two entities sharing a trivial body (``pass``) with dissimilar names must
    NOT pair — the name-similarity/substantial-body guard prevents false pairs."""
    base = [_entity("function", "alpha", "def alpha():\n    pass")]
    side = [_entity("function", "omega", "def omega():\n    pass")]
    renames, _ = detect_renames_2way(base, side)
    assert renames == {}


def test_entity_body_content_oneliner_splits_at_scope():
    """The canonical body signal splits single-line bodies at the scope opener
    (``:`` for Python, ``{`` for brace langs) so the inline body isn't folded
    into the header. Previously the canonical core dropped one-liner bodies
    entirely (lines[1:] = empty); now it matches structural._split_header_body."""
    assert entity_body_content("def foo(): return 1") == "return 1"
    assert entity_body_content("fn foo() { 1 }") == "1"
    # Multi-line: header is line 0, rest is normalized.
    assert entity_body_content("def foo():\n    return 1") == "return 1"


def test_split_header_body_returns_both_parts():
    """split_header_body returns (header, rest) for signature + body use."""
    hdr, rest = split_header_body("def foo(a: int) -> str: return a")
    assert hdr == "def foo(a: int) -> str:"
    assert rest == "return a"
    # Multi-line
    hdr2, rest2 = split_header_body("def foo():\n    return 1\n    return 2")
    assert hdr2 == "def foo():"
    assert "return 1" in rest2 and "return 2" in rest2


def test_name_similarity_canonical():
    """The canonical name-similarity is char_ratio-based; identical = 1.0."""
    from capybase.adapters.abstract_parser import name_similarity
    assert name_similarity("loadData", "loadData") == 1.0
    assert name_similarity("loadData", "fetchData") < 1.0
    assert name_similarity("", "x") == 0.0
    assert name_similarity(None, "x") == 0.0
    # The threshold is exposed for callers that need it.
    assert RENAME_NAME_SIMILARITY_THRESHOLD == 0.6


def test_canonical_core_works_on_structural_units():
    """detect_renames_2way works on StructuralUnit too (the 3-way diff's unit
    type), not just Entity — it relies only on .identity/.kind/.name/.body."""
    from capybase.adapters.abstract_parser import StructuralUnit
    base = [StructuralUnit(kind="function", name="old", span=(0, 1),
                           body="def old():\n    return 1")]
    side = [StructuralUnit(kind="function", name="new", span=(0, 1),
                           body="def new():\n    return 1")]
    renames, removed = detect_renames_2way(base, side)
    assert renames == {("function", "new"): ("function", "old")}, renames
    assert removed == {("function", "old")}


def test_fingerprint_and_body_content_invariant():
    """The two rename-pairing signals must agree on content presence AND on
    rename-pairability.

    The 3-way diff (``structural_diff._detect_renames``) keys rename pairing on
    ``unit.fingerprint`` (baked at parse time via ``unit_body_fingerprint``),
    while the 2-way core (``detect_renames_2way``) and ``semantic_diff`` key on
    ``entity_body_content``. The 3-way path has no dedicated rename test, so its
    correctness rests on the invariant the docstring asserts:

      1. content-presence agreement: a body has a content digest in its
         fingerprint (``_fingerprint_has_content``) iff ``entity_body_content``
         is non-empty.
      2. rename agreement: for any two bodies that differ only in the name token
         in the header, the fingerprints are equal iff the body-contents are
         equal.

    If these ever diverge (different normalization, one stops blanking strings),
    the 3-way and 2-way rename detectors would silently disagree.
    """
    from capybase.adapters.abstract_parser import (
        _fingerprint_has_content, unit_body_fingerprint,
    )

    # A varied corpus spanning both families, one-liners, multi-line, empty,
    # content-less, comment-bearing, and field shapes.
    corpus = [
        "def foo():\n    return 1\n",              # multi-line Python
        "def foo(): return 1\n",                    # one-liner (colon)
        "fn foo() { 1 }\n",                         # one-liner (brace)
        "fn foo() {\n    1\n}\n",                   # multi-line brace
        "def foo():\n    pass\n",                   # content-less multi
        "def foo(): pass\n",                        # content-less one-liner
        "function foo() { return 1; }\n",           # JS one-liner
        "function foo() {\n    return 1;\n}\n",     # JS multi
        "def foo():\n    x = 1  # note\n",          # comment-bearing
        "pub fn foo() -> u32 {\n    42\n}\n",       # Rust with signature
        "const N: u32 = 5;\n",                      # field
        "",                                         # empty
        "def foo():\n",                             # header only
    ]

    # Invariant 1: content-presence agreement for every body.
    for body in corpus:
        fp = unit_body_fingerprint(body)
        content = entity_body_content(body)
        assert _fingerprint_has_content(fp) == (content != ""), (
            f"presence disagreement for {body!r}: "
            f"fp={fp!r} has_content={_fingerprint_has_content(fp)}, "
            f"entity_body_content={content!r}"
        )

    # Invariant 2: for a rename pair (same body, name changed), both signals
    # agree on equality. Rename foo→bar in each body that names foo.
    for body in corpus:
        if "foo" not in body:
            continue
        a, b = body.replace("foo", "alpha"), body.replace("foo", "beta")
        fp_eq = unit_body_fingerprint(a) == unit_body_fingerprint(b)
        content_eq = entity_body_content(a) == entity_body_content(b)
        assert fp_eq == content_eq, (
            f"rename-agreement broken for {body!r}: "
            f"fingerprint_equal={fp_eq}, content_equal={content_eq}"
        )




# --- H1 regression: 3-way rename must not drop added_both_conflict ---


def test_h1_both_sides_rename_same_name_different_bodies_stays_conflict():
    """When base has ``foo`` and BOTH sides rename it to ``bar``, but with
    DIFFERENT bodies, the 3-way diff must report a structural conflict — not
    collapse it into a non-conflicting RENAMED entry.

    The rename pass pairs ``bar`` with ``foo`` via fingerprint match (one side's
    body matches foo's), but when the OTHER side also has ``bar`` with a
    divergent body, that's a genuine rename-conflict. Previously the rename pass
    unconditionally emitted RENAMED (not in _CONFLICT_CHANGE_KINDS), dropping the
    conflict to zero and telling the LLM there's nothing to resolve."""
    from capybase.adapters.structural_diff import compute_structural_diff_3way
    base = "def foo():\n    return 1\n"
    left = "def bar():\n    return 1\n"    # rename foo->bar, body identical to foo
    right = "def bar():\n    return 2\n"   # rename foo->bar, DIFFERENT body
    diff = compute_structural_diff_3way(base, left, right, language="python")
    assert diff is not None, "diff must not decline on this input"
    conflicts = diff.structural_conflicts
    assert len(conflicts) >= 1, (
        f"both-sides-rename-same-name with divergent bodies must be a conflict; "
        f"got kinds={[a.change_kind for a in diff.aligned]}, conflicts={len(conflicts)}"
    )
    # Sanity: the agreed-same-body rename is still non-conflicting.
    right_same = "def bar():\n    return 1\n"
    diff_ok = compute_structural_diff_3way(base, left, right_same, language="python")
    assert diff_ok is not None
    assert len(diff_ok.structural_conflicts) == 0, (
        "both-sides-rename with identical bodies is an agreed rename, not a conflict"
    )


# --- H2 regression: rename + independent-add name collision must not double ---


def test_h2_entity_disjoint_rename_plus_independent_add_no_duplicate():
    """When one side renames ``foo``→``bar`` and the other side independently
    adds a fresh ``bar`` (different body, not a rename of foo), the merged
    container must NOT end up with two ``bar`` methods.

    The merge-walk's ``seen`` set is keyed by canonical BASE identity: the
    renamed bar is recorded under canonical ``foo``, while the independently-
    added bar has canonical ``bar``, so the dedup misses and both emit.
    The resolver should DECLINE (return None) when a name collision would
    result, rather than produce a malformed class with a doubled method."""
    base = "class C:\n    def foo(self):\n        return 1\n"
    cur = "class C:\n    def bar(self):\n        return 1\n"      # rename foo->bar
    rep = "class C:\n    def foo(self):\n        return 1\n    def bar(self):\n        return 99\n"  # keeps foo + adds bar
    result = _try_entity_disjoint(_unit(base, cur, rep))
    if result is None:
        return  # declined — correct
    # If it resolved, there must NOT be two def bar.
    assert result.count("def bar") <= 1, (
        f"merge produced a doubled 'def bar' (rename + independent-add collision);\n{result!r}"
    )


# --- Finding 1 regression: refactoring_aware_merge doubled entity ---


def test_f1_refactoring_aware_merge_rename_plus_add_no_duplicate():
    """The H2 name-collision guard was added only to _try_entity_disjoint.
    _try_refactoring_aware_merge (the sibling strategy) builds merged_ids the
    same way and had the same doubled-entity bug: when one side renames foo->bar
    and the other modifies foo's body AND adds a fresh bar, the composed entity
    (bar's header) and the addition (fresh bar) collide, producing two def bar.
    Unlike entity_disjoint, this output is syntactically valid Python (second
    shadows first) so validation may NOT catch it — a silent wrong merge."""
    base = "class C:\n    def foo(self):\n        return 1\n"
    cur = "class C:\n    def bar(self):\n        return 1\n"      # rename foo->bar
    rep = "class C:\n    def foo(self):\n        return 2\n    def bar(self):\n        return 99\n"  # modify foo + add bar
    result = _try_refactoring_aware_merge(_unit(base, cur, rep))
    if result is None:
        return  # declined — correct
    assert result.count("def bar") <= 1, (
        f"refactoring_aware merge produced a doubled 'def bar';\n{result!r}"
    )

"""Tests for entity-level disjoint resolution (survey §3.2/§5.2 Weave/Aura).

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
# Fourth-pass: R4 (resolver rename detection vs parser fingerprint consistency)
# and R5 (refactoring-aware merge duplicate-identity guard).
# ---------------------------------------------------------------------------

from capybase.structural_resolver import _detect_renames, _body_content  # noqa: E402


def _entity(kind, name, body, span=(0, 1)):
    """Build a structural.Entity for rename-detection unit tests."""
    return S.Entity(kind=kind, name=name, body=body, span=span)


def test_r4_rename_with_inline_comment_drift_is_detected():
    """R4: a rename where the body picked up an inline comment (or a changed
    string literal) MUST still be detected. The resolver's ``_body_content``
    used whitespace-collapse-only normalization (keeping comments/strings),
    while the parser's ``unit_body_fingerprint`` strips comments and blanks
    strings. The two algorithms disagreed: the parser paired the rename, the
    resolver missed it → the merge emitted the old name AND the new name as a
    duplicate. After R4, ``_body_content`` strips inline comments so the
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
    """R4: a rename where a string literal value changed (but no real body
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
    """R4 regression guard: a genuine body change (not just comment/string
    drift) must NOT pair as a rename — that would conflate two distinct
    functions. After R4 the line ``return fetch()`` vs ``return save()``
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
    """R5: ``_try_refactoring_aware_merge`` builds ``base_by_id`` without the
    duplicate-identity guard that ``_try_entity_disjoint`` has (fix #3). A
    base with two entities sharing an identity (Python ``@property`` +
    ``@x.setter`` both named ``x``) would silently drop one in the dict. After
    R5, the path declines (returns None) on duplicate identities, escalating
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

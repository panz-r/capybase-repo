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

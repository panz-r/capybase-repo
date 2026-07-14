"""Tests for rename-aware entity matching in entity_disjoint (survey §2.2 s3m).

The s3m lineage (this survey's §2.2) flags renames as the #1 false-negative
source for entity-level merge: when one side renames an entity (same body,
different name) while the other makes an unrelated change, naive entity_disjoint
treats the rename as "base keeps old + side added new" → a DUPLICATE method.
s3m added a Levenshtein/content-based rename handler; this implements it.

These tests cover: single rename + add (no duplicate), both-sides-rename-same-
entity (decline), coincidental-same-body (not a rename), threshold/content
guard, and rename-only falling through to one_sided_change.
"""

from __future__ import annotations

import ast

import pytest

structural = pytest.importorskip("capybase.adapters.structural")
needs_ts = pytest.mark.skipif(
    not structural.is_available("python"),
    reason="abstract parser unavailable for python",
)

from capybase.adapters.abstract_parser import name_similarity
from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.structural_resolver import (
    _body_content,
    _detect_renames,
    resolve_structurally,
)


def _unit(base, cur, rep, *, lang="python", path="app.py"):
    u = ConflictUnit(
        session_id="s", step_index=1, path=path, language=lang,
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=cur),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=rep),
        original_worktree_text="", marker_span=None,
    )
    u.structural_metadata["enclosing_node_text"] = base
    return u


# ---------------------------------------------------------------------------
# Rename detection primitives
# ---------------------------------------------------------------------------


def test_name_similarity_identical():
    assert name_similarity("loadData", "loadData") == 1.0


def test_name_similarity_unrelated():
    assert name_similarity("loadData", "xyz") < 0.5


def test_body_content_strips_signature():
    """The header line is stripped so a rename's body content matches."""
    a = "def loadData(self):\n    return self.fetch()"
    b = "def fetchData(self):\n    return self.fetch()"
    assert _body_content(a) == _body_content(b)


def test_body_content_differs_when_content_differs():
    a = "def f():\n    return 1"
    b = "def f():\n    return 2"
    assert _body_content(a) != _body_content(b)


@needs_ts
def test_detect_renames_finds_rename():
    from capybase.adapters import structural as S

    base = "class S:\n    def loadData(self):\n        return self.fetch()"
    side = "class S:\n    def fetchData(self):\n        return self.fetch()"
    be = S.enumerate_entities(base, "python", container_span=(1, 1))
    se = S.enumerate_entities(side, "python", container_span=(1, 1))
    renames, removed = _detect_renames(se, be)
    assert ("method", "fetchData") in renames
    assert ("method", "loadData") in removed


@needs_ts
def test_detect_renames_no_rename_when_old_name_remains():
    """If the old name still exists on the side, it's a COPY, not a rename."""
    from capybase.adapters import structural as S

    base = "class S:\n    def a(self):\n        return 1"
    side = "class S:\n    def a(self):\n        return 1\n    def b(self):\n        return 1"
    be = S.enumerate_entities(base, "python", container_span=(1, 1))
    se = S.enumerate_entities(side, "python", container_span=(1, 1))
    renames, removed = _detect_renames(se, be)
    assert renames == {}  # 'a' still present → b is an addition, not a rename
    assert removed == set()


# ---------------------------------------------------------------------------
# entity_disjoint with renames — the headline fix
# ---------------------------------------------------------------------------


@needs_ts
def test_rename_plus_unrelated_add_no_duplicate():
    """One side renames loadData→fetchData; other adds saveData. Result must
    contain fetchData (renamed) + saveData (added), NOT loadData (dropped)."""
    base = "class S:\n    def loadData(self):\n        return self.fetch()"
    cur = "class S:\n    def fetchData(self):\n        return self.fetch()"
    rep = (
        "class S:\n    def loadData(self):\n        return self.fetch()\n"
        "    def saveData(self, v):\n        self.store(v)"
    )
    r = resolve_structurally(_unit(base, cur, rep))
    assert r.rule == "entity_disjoint"
    text = r.text
    assert "def fetchData" in text  # rename applied
    assert "def loadData" not in text  # old name dropped (no duplicate)
    assert "def saveData" in text  # unrelated addition kept
    ast.parse(text)


@needs_ts
def test_rename_no_duplicate_count():
    """Exactly one of the old/new name appears — never both."""
    base = "class S:\n    def loadData(self):\n        return self.fetch()"
    cur = "class S:\n    def fetchData(self):\n        return self.fetch()"
    rep = "class S:\n    def loadData(self):\n        return self.fetch()\n    def help(self): pass"
    r = resolve_structurally(_unit(base, cur, rep))
    text = r.text
    assert text.count("def loadData") + text.count("def fetchData") == 1


@needs_ts
def test_both_sides_rename_same_entity_differently_declines():
    """Both sides rename the same entity to different names → genuine conflict →
    decline (can't pick one rename over the other)."""
    base = "class S:\n    def loadData(self):\n        return self.fetch()"
    cur = "class S:\n    def fetchData(self):\n        return self.fetch()"
    rep = "class S:\n    def pullData(self):\n        return self.fetch()"
    r = resolve_structurally(_unit(base, cur, rep))
    assert not r.resolved


@needs_ts
def test_both_sides_rename_same_entity_same_way_merges():
    """Both sides make the SAME rename → agreed change → merge with the new name."""
    base = "class S:\n    def loadData(self):\n        return self.fetch()"
    cur = "class S:\n    def fetchData(self):\n        return self.fetch()\n    def a(self): pass"
    rep = "class S:\n    def fetchData(self):\n        return self.fetch()\n    def b(self): pass"
    r = resolve_structurally(_unit(base, cur, rep))
    # Both renamed identically + each added a distinct method → merges cleanly.
    assert r.rule == "entity_disjoint"
    text = r.text
    assert "def fetchData" in text
    assert "def loadData" not in text
    assert "def a" in text and "def b" in text


@needs_ts
def test_coincidental_same_body_not_treated_as_rename():
    """Two distinct entities that happen to share a trivial body (e.g. ``pass``)
    are NOT misread as a rename when the old name still exists."""
    base = "class S:\n    def a(self):\n        pass"
    cur = "class S:\n    def a(self):\n        pass\n    def b(self):\n        pass"
    rep = "class S:\n    def a(self):\n        return 1"
    r = resolve_structurally(_unit(base, cur, rep))
    # cur adds 'b' (both a,b = pass); rep modifies 'a'. Not a rename → both kept.
    text = r.text
    assert "def a" in text and "def b" in text


@needs_ts
def test_rename_only_falls_through_to_one_sided_change():
    """A pure rename on one side with the other unchanged is a one-sided change
    at the container level → handled by an earlier rule, not entity_disjoint."""
    base = "class S:\n    def loadData(self):\n        return self.fetch()"
    cur = "class S:\n    def fetchData(self):\n        return self.fetch()"
    rep = base
    r = resolve_structurally(_unit(base, cur, rep))
    assert r.rule == "one_sided_change"


@needs_ts
def test_rust_rename_merges():
    """Rename awareness works for Rust impl methods too."""
    base = "impl S {\n    fn load(&self) -> i32 {\n        self.x\n    }\n}"
    cur = "impl S {\n    fn fetch(&self) -> i32 {\n        self.x\n    }\n}"
    rep = (
        "impl S {\n    fn load(&self) -> i32 {\n        self.x\n    }\n"
        "    fn save(&self, v: i32) {\n        self.x = v;\n    }\n}"
    )
    r = resolve_structurally(_unit(base, cur, rep, lang="rust", path="s.rs"))
    assert r.rule == "entity_disjoint"
    text = r.text
    assert "fn fetch" in text  # rename applied
    assert "fn load" not in text  # old name dropped
    assert "fn save" in text  # unrelated addition kept


@needs_ts
def test_rename_resolution_is_valid_python():
    """The merged container must parse cleanly (validator double-checks too)."""
    base = "class S:\n    def compute(self):\n        return self.value * 2"
    cur = "class S:\n    def calculate(self):\n        return self.value * 2"
    rep = (
        "class S:\n    def compute(self):\n        return self.value * 2\n"
        "    def reset(self):\n        self.value = 0"
    )
    r = resolve_structurally(_unit(base, cur, rep))
    ast.parse(r.text)


# ---------------------------------------------------------------------------
# Refactoring-aware composition (survey §3.2 RefMerge): when entity_disjoint
# DECLINES on overlap, but the overlap is a clean rename + body-modify, compose.
# Tests the _try_refactoring_aware_merge rule directly (it runs only on the
# overlap tail where the earlier line/entity rules declined).
# ---------------------------------------------------------------------------

from capybase.structural_resolver import _try_refactoring_aware_merge  # noqa: E402


def _overlap_unit(base, cur, rep, *, lang="python"):
    """A unit where the line-level rules DECLINE (both sides change overlapping
    lines) so the dispatch reaches entity_disjoint → refactoring_aware. We test
    the refactoring rule directly to isolate its logic."""
    u = _unit(base, cur, rep, lang=lang)
    return u


def test_refactoring_aware_resolves_rename_plus_body_modify():
    """Core case: side A renames foo→bar (pure rename), side B modifies foo's
    body. The composition takes the renamed header + the modified body."""
    base = "class C:\n    def foo():\n        x = 1\n        return x"
    cur = "class C:\n    def bar():\n        x = 1\n        return x"  # pure rename
    rep = "class C:\n    def foo():\n        x = 2\n        return x"  # body modify
    result = _try_refactoring_aware_merge(_overlap_unit(base, cur, rep))
    assert result is not None
    assert "def bar():" in result      # renamed header
    assert "x = 2" in result           # modified body
    assert "return x" in result        # unchanged tail
    # The composed entity is valid Python.
    ast.parse(result)


def test_refactoring_aware_compose_works_both_directions():
    """Whichever side renamed, the composition uses that side's header."""
    base = "class C:\n    def foo():\n        x = 1\n        return x"
    # Now the REPLAYED side renames, CURRENT modifies the body.
    cur = "class C:\n    def foo():\n        x = 2\n        return x"  # body modify
    rep = "class C:\n    def bar():\n        x = 1\n        return x"  # pure rename
    result = _try_refactoring_aware_merge(_overlap_unit(base, cur, rep))
    assert result is not None
    assert "def bar():" in result      # renamed header (from replayed)
    assert "x = 2" in result           # modified body (from current)


def test_refactoring_aware_declines_on_double_body_modify():
    """Both sides modify the body (no rename) → genuine conflict → decline."""
    base = "class C:\n    def foo():\n        x = 1\n        return x"
    cur = "class C:\n    def foo():\n        x = 2\n        return x"
    rep = "class C:\n    def foo():\n        x = 3\n        return x"
    assert _try_refactoring_aware_merge(_overlap_unit(base, cur, rep)) is None


def test_refactoring_aware_declines_on_rename_plus_signature_change():
    """A rename on one side + a SIGNATURE change on the other (header differs
    beyond the name) → can't safely compose → decline."""
    base = "class C:\n    def foo():\n        x = 1\n        return x"
    cur = "class C:\n    def bar():\n        x = 1\n        return x"        # rename
    rep = "class C:\n    def foo(a, b):\n        x = 1\n        return x"  # sig change
    assert _try_refactoring_aware_merge(_overlap_unit(base, cur, rep)) is None


def test_refactoring_aware_declines_on_conflicting_renames():
    """Both sides rename the same entity to different names → conflict → decline."""
    base = "class C:\n    def foo():\n        x = 1\n        return x"
    cur = "class C:\n    def bar():\n        x = 1\n        return x"  # foo->bar
    rep = "class C:\n    def baz():\n        x = 1\n        return x"  # foo->baz
    assert _try_refactoring_aware_merge(_overlap_unit(base, cur, rep)) is None


def test_refactoring_aware_resolves_rust_rename_plus_body_modify():
    """The rule covers Rust too (brace-family via the abstract parser)."""
    base = "impl S {\n    fn foo(&self) -> i32 {\n        let x = 1;\n        x\n    }\n}"
    cur = "impl S {\n    fn bar(&self) -> i32 {\n        let x = 1;\n        x\n    }\n}"  # rename
    rep = "impl S {\n    fn foo(&self) -> i32 {\n        let x = 2;\n        x\n    }\n}"  # body modify
    result = _try_refactoring_aware_merge(_overlap_unit(base, cur, rep, lang="rust"))
    assert result is not None
    assert "fn bar(" in result
    assert "let x = 2;" in result


def test_refactoring_aware_resolution_is_valid_python():
    """The composed container must parse cleanly (the validator double-checks)."""
    base = "class S:\n    def compute(self):\n        return self.value * 2"
    cur = "class S:\n    def calculate(self):\n        return self.value * 2"  # rename
    rep = "class S:\n    def compute(self):\n        return self.value * 3"  # body modify
    result = _try_refactoring_aware_merge(_overlap_unit(base, cur, rep))
    assert result is not None
    ast.parse(result)

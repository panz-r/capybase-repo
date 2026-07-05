"""Tests for the per-entity semantic diff (survey §5 foundational layer).

``semantic_diff`` classifies entity-level changes between two snapshots — the
deterministic input the resolve prompt, the critic, and the cross-commit
guardian consume. It pairs entities across names (a rename) via body-fingerprint
equality + a Jaccard fallback, so legitimate renames are recognized rather than
read as a spurious add+remove pair.
"""

from __future__ import annotations

import pytest

from capybase.adapters import structural


# Skip the whole module when tree-sitter isn't installed (the primitives
# degrade to None, and there's nothing to assert about the classification).
pytestmark = pytest.mark.skipif(
    not structural.is_available("python"),
    reason="tree-sitter Python grammar unavailable",
)


def _changes(old: str, new: str):
    return structural.semantic_diff(old, new, "python")


def test_no_change_yields_empty_diff():
    src = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
    assert _changes(src, src) == []


def test_added_function_detected():
    old = "def foo():\n    return 1\n"
    new = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
    out = _changes(old, new)
    assert len(out) == 1
    assert out[0].change_type == "added"
    assert out[0].name == "bar"
    assert out[0].kind == "function"


def test_removed_function_detected():
    old = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
    new = "def foo():\n    return 1\n"
    out = _changes(old, new)
    assert len(out) == 1
    assert out[0].change_type == "removed"
    assert out[0].name == "bar"


def test_rename_paired_by_identical_body():
    """A pure rename (same body, different name) is ONE rename change, not an
    add+remove pair. This is the core signal the name-based analyzers miss."""
    old = "def loadData():\n    return fetch()\n"
    new = "def fetchData():\n    return fetch()\n"
    out = _changes(old, new)
    assert len(out) == 1
    assert out[0].change_type == "renamed"
    assert out[0].old_name == "loadData"
    assert out[0].new_name == "fetchData"


def test_rename_with_small_body_edit_paired_by_jaccard():
    """A rename that ALSO edits the body slightly is still recognized as a rename
    via the Jaccard fallback (not a spurious add+remove). The bodies share most
    tokens, so they pair above the threshold."""
    old = "def parse_item():\n    x = read()\n    return transform(x)\n"
    new = "def parse_thing():\n    x = read()\n    return transform(x)\n    return None\n"
    out = _changes(old, new)
    renames = [c for c in out if c.change_type == "renamed"]
    assert len(renames) == 1
    assert renames[0].old_name == "parse_item"
    assert renames[0].new_name == "parse_thing"


def test_rename_not_conflated_with_unrelated_add():
    """If the old name still exists in new (the entity was copied, not renamed),
    the new-name entity is an ADD, not a rename."""
    old = "def foo():\n    return 1\n"
    new = "def foo():\n    return 1\n\ndef bar():\n    return 1\n"
    out = _changes(old, new)
    # bar has foo's body, but foo is still present → bar is a copy/add.
    assert len(out) == 1
    assert out[0].change_type == "added"
    assert out[0].name == "bar"


def test_signature_change_detected():
    old = "def foo(a, b):\n    return a + b\n"
    new = "def foo(a, b, c):\n    return a + b + c\n"
    out = _changes(old, new)
    assert len(out) == 1
    assert out[0].change_type == "signature_changed"
    assert out[0].name == "foo"


def test_body_change_detected():
    """Same signature, different body → body_changed (not signature_changed)."""
    old = "def foo(a, b):\n    return a + b\n"
    new = "def foo(a, b):\n    return a * b\n"
    out = _changes(old, new)
    assert len(out) == 1
    assert out[0].change_type == "body_changed"
    assert out[0].name == "foo"


def test_class_entity_classified():
    old = "class Foo:\n    pass\n"
    new = "class Foo:\n    pass\n\nclass Bar:\n    pass\n"
    out = _changes(old, new)
    assert len(out) == 1
    assert out[0].change_type == "added"
    assert out[0].kind == "class"
    assert out[0].name == "Bar"


def test_entity_body_fingerprint_invariant_to_whitespace():
    """The body fingerprint is whitespace/formatting-stable but rename-sensitive
    in the header (which is stripped). Two fns differing only in name + spacing
    produce equal body fingerprints → that's what powers rename pairing."""
    a = "def foo():\n    return 1\n"
    b = "def  bar ( ) :\n    return   1\n"
    ea = structural.enumerate_entities(a, "python")[0]
    eb = structural.enumerate_entities(b, "python")[0]
    assert structural.entity_body_fingerprint(ea, "python") == \
        structural.entity_body_fingerprint(eb, "python")


def test_render_lines_human_readable():
    c = structural.EntityChange(
        kind="function", name="x", change_type="renamed",
        old_name="a", new_name="b",
    )
    assert "renamed" in c.render() and "a" in c.render() and "b" in c.render()


def test_rust_semantic_diff_added():
    if not structural.is_available("rust"):
        pytest.skip("tree-sitter Rust grammar unavailable")
    old = "pub fn foo() -> i32 {\n    1\n}\n"
    new = "pub fn foo() -> i32 {\n    1\n}\n\npub fn bar() -> i32 {\n    2\n}\n"
    out = structural.semantic_diff(old, new, "rust")
    assert len(out) == 1
    assert out[0].change_type == "added"
    assert out[0].name == "bar"


def test_syntax_error_does_not_crash():
    """tree-sitter's error recovery best-effort-parses syntax errors (same as
    ``enumerate_entities``), so ``semantic_diff`` returns a classification rather
    than raising. It must never crash on malformed input — callers rely on the
    graceful-degradation contract (``None`` only when tree-sitter is absent)."""
    out = _changes("def broken(:\n", "def ok():\n    return 1\n")
    # No exception, and a list result (tree-sitter recovered the partial defs).
    assert isinstance(out, list)


# ---------------------------------------------------------------------------
# Cross-file movement detection (Phase 2d)
# ---------------------------------------------------------------------------


def test_detect_cross_file_move_simple():
    """An entity removed from its old file and present (by body) in a new file
    is detected as a move, not a deletion."""
    src = "def authenticate():\n    token = read_token()\n    return validate(token)\n"
    old = {"auth.py": src}
    new = {"auth.py": "", "auth/core.py": src}  # moved file
    moves = structural.detect_cross_file_moves(old, new, "python")
    assert moves is not None
    assert len(moves) == 1
    m = moves[0]
    assert m.name == "authenticate"
    assert m.old_path == "auth.py"
    assert m.new_path == "auth/core.py"
    assert "auth.py" in m.render() and "auth/core.py" in m.render()


def test_detect_cross_file_move_with_rename():
    """A move that coincides with a rename pairs by body fingerprint and records
    the new name."""
    old = {"a.py": "def process_data():\n    x = load()\n    return transform(x)\n"}
    new = {"a.py": "", "b.py": "def process_input():\n    x = load()\n    return transform(x)\n"}
    moves = structural.detect_cross_file_moves(old, new, "python")
    assert len(moves) == 1
    assert moves[0].name == "process_data"
    assert moves[0].new_name == "process_input"


def test_detect_no_move_when_entity_stays_in_file():
    """An entity still present in its original file is NOT a move."""
    src = "def foo():\n    return 1\n"
    moves = structural.detect_cross_file_moves({"a.py": src}, {"a.py": src}, "python")
    assert moves == []


def test_detect_no_move_when_entity_genuinely_removed():
    """An entity removed with no body match anywhere is NOT a move (genuinely
    deleted) — it does not appear in the moves list."""
    old = {"a.py": "def foo():\n    return 1\n"}
    new = {"a.py": ""}  # removed, no body match elsewhere
    moves = structural.detect_cross_file_moves(old, new, "python")
    assert moves == []


def test_detect_no_move_to_same_file():
    """A body match in the SAME path is not a move (guard against false positive
    when an entity is removed and re-added under a rename in the same file —
    that's an in-file rename, handled by semantic_diff)."""
    old = {"a.py": "def foo():\n    return compute()\n"}
    new = {"a.py": "def bar():\n    return compute()\n"}
    moves = structural.detect_cross_file_moves(old, new, "python")
    assert moves == []


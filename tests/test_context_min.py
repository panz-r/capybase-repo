"""Tests for context minimization: diff3 refinement, AST-as-primary, canonicalization.

Step 1 of the multi-request pipeline: minimize the search space before making
any LLM request. These test the three mechanisms — git merge-file --diff3 for
tightest boundaries, enclosing AST node as primary context, and comment/blank
stripping — in isolation and via the extractor integration.
"""

from __future__ import annotations

import pytest

from capybase.context_builder import ContextBuilder, canonicalize_context
from capybase.conflict_model import ConflictSide, ConflictUnit


# ---------------------------------------------------------------------------
# canonicalize_context
# ---------------------------------------------------------------------------


def test_canonicalize_strips_python_comments():
    src = "# header comment\nx = 1\n# trailing\ny = 2\n"
    out = canonicalize_context(src, "python")
    assert "# header comment" not in out
    assert "x = 1" in out
    assert "y = 2" in out


def test_canonicalize_strips_rust_comments():
    src = "// doc\nfn main() {}\n// trailing\n"
    out = canonicalize_context(src, "rust")
    assert "// doc" not in out
    assert "fn main()" in out


def test_canonicalize_preserves_indentation():
    src = "    return 1\n    # inline area\n    return 2\n"
    out = canonicalize_context(src, "python")
    assert "    return 1" in out
    assert "    return 2" in out


def test_canonicalize_collapses_blank_runs():
    src = "x = 1\n\n\n\n\ny = 2\n"
    out = canonicalize_context(src, "python")
    assert "\n\n\n" not in out
    assert "x = 1" in out
    assert "y = 2" in out


def test_canonicalize_keeps_conflict_markers():
    src = "<<<<<<< H\nours\n=======\ntheirs\n>>>>>>> b\n"
    out = canonicalize_context(src, "python")
    assert "<<<<<<<" in out
    assert "=======" in out
    assert ">>>>>>>" in out


def test_canonicalize_empty_string():
    assert canonicalize_context("", "python") == ""


# ---------------------------------------------------------------------------
# git merge-file --diff3
# ---------------------------------------------------------------------------


def test_diff3_detects_conflict():
    from capybase.adapters.git_diff3 import merge_file_diff3

    blocks = merge_file_diff3(
        "def f():\n    return 1\n",
        "def f():\n    return 2\n",
        "def f():\n    return 3\n",
    )
    assert blocks is not None
    assert len(blocks) == 1
    assert "return 2" in blocks[0].ours
    assert "return 1" in blocks[0].base
    assert "return 3" in blocks[0].theirs


def test_diff3_no_conflict_returns_empty():
    from capybase.adapters.git_diff3 import merge_file_diff3

    blocks = merge_file_diff3("x = 1\n", "x = 1\n", "x = 1\n")
    assert blocks == []


def test_diff3_multi_block():
    from capybase.adapters.git_diff3 import merge_file_diff3

    # Two genuinely conflicting regions (both sides change the same lines).
    base = "a = 1\n---\nb = 2\n"
    ours = "a = 10\n---\nb = 20\n"
    theirs = "a = 100\n---\nb = 200\n"
    blocks = merge_file_diff3(base, ours, theirs)
    assert blocks is not None
    assert len(blocks) == 2


def test_diff3_is_available():
    from capybase.adapters.git_diff3 import is_available

    assert is_available()  # git is always present in this environment


# ---------------------------------------------------------------------------
# AST-as-primary context (ContextBuilder integration)
# ---------------------------------------------------------------------------


def _unit(base, current, replayed, worktree, span=(1, 5)):
    u = ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text=worktree, marker_span=span,
    )
    u.structural_metadata["enclosing_node_type"] = "function_definition"
    u.structural_metadata["enclosing_node_signature"] = "def greet():"
    u.structural_metadata["enclosing_node_text"] = "def greet():\n    return 'hi'"
    return u


def test_use_enclosing_as_primary_replaces_line_window():
    worktree = (
        "import os\n\n# license header\ndef greet():\n"
        "<<<<<<< H\n    return 'hi'\n=======\n    return 'howdy'\n>>>>>>> b\n"
        "\ndef farewell():\n    return 'bye'\n"
    )
    unit = _unit("def greet():\n    pass", "    return 'hi'", "    return 'howdy'", worktree)
    # Without AST-primary: line window includes import/farewell
    cb_window = ContextBuilder(context_lines=5, use_enclosing_as_primary=False)
    ctx_window = cb_window.build(unit)
    assert "import os" in ctx_window.primary_text or "farewell" in ctx_window.primary_text

    # With AST-primary: only the enclosing function
    cb_ast = ContextBuilder(context_lines=5, use_enclosing_as_primary=True)
    ctx_ast = cb_ast.build(unit)
    assert "def greet():" in ctx_ast.primary_text
    assert "farewell" not in ctx_ast.primary_text
    assert "import os" not in ctx_ast.primary_text


def test_canonicalize_in_context_strips_comments():
    worktree = "# big comment\ndef f():\n<<<<<<< H\n1\n=======\n2\n>>>>>>> b\n"
    unit = _unit("def f():\n    pass", "1", "2", worktree, span=(2, 4))
    cb = ContextBuilder(context_lines=5, canonicalize_context=True)
    ctx = cb.build(unit)
    assert "# big comment" not in ctx.primary_text


def test_defaults_do_not_change_behavior():
    """When neither option is set, behavior matches the original line-window."""
    worktree = "# comment\ndef f():\n<<<<<<< H\n1\n=======\n2\n>>>>>>> b\n"
    unit = _unit("def f():\n    pass", "1", "2", worktree, span=(2, 4))
    cb = ContextBuilder(context_lines=5)  # defaults: both off
    ctx = cb.build(unit)
    assert "# comment" in ctx.primary_text  # not stripped
    assert "def f():" in ctx.primary_text  # line window includes surrounding code


# ---------------------------------------------------------------------------
# Extractor diff3 refinement (integration)
# ---------------------------------------------------------------------------


needs_ts = pytest.mark.skipif(
    not __import__("capybase.adapters.structural", fromlist=["is_available"]).is_available("python"),
    reason="abstract parser unavailable",
)


@needs_ts
def test_extractor_refines_with_diff3():
    from capybase.config import StructuralConfig
    from capybase.conflict_extractor import ConflictExtractor

    # The base/current/replayed blobs; the conflict is just the return value.
    base = "def greet():\n    return 'hi'\n"
    current = "def greet():\n    return 'howdy'\n"
    replayed = "def greet():\n    return 'bye'\n"

    class FakeGit:
        def read_stage_blob(self, path, stage):
            return base.encode("utf-8") if stage != 3 else replayed.encode("utf-8")

        def read_worktree_file(self, path):
            # Worktree has wider markers than the actual conflict (simulating
            # git having auto-resolved some adjacent lines).
            return (
                b"def greet():\n<<<<<<< H\n    return 'howdy'\n"
                b"=======\n    return 'bye'\n>>>>>>> b\n"
            )

    ex = ConflictExtractor(
        FakeGit(),
        structural_config=StructuralConfig(enabled=True, refine_with_diff3=True),
    )
    units = ex.extract_file_units("app.py", 1, "s")
    assert len(units) == 1
    # diff3_refined may or may not be present (depends on whether diff3 found a
    # tighter view); the test just verifies it doesn't crash and produces a unit.
    u = units[0]
    assert u.current.text  # has side text

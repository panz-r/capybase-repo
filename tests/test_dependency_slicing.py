"""Tests for cross-file dependency slicing (survey §5.3 Rover-style context).

P1: the context builder resolves definitions of symbols the conflict code
references across the repo and surfaces them as ``related_snippets`` in the
context bundle. The resolve prompt then renders a "Definitions this conflict
depends on" block so a small model merges consistently with those deps instead
of guessing.

The slicer reuses the existing ``referenced_symbols`` /
``find_symbol_definitions`` (grep+parse) — these tests cover the ContextBuilder
wiring, the prompt rendering, and the no-op default behavior, not the slicer
internals (those are covered by test_structural.py).
"""

from __future__ import annotations

import os

import pytest

from capybase.conflict_model import ConflictSide, ConflictUnit, RelatedSnippet
from capybase.context_builder import ContextBuilder, _enclosing_name, _slice_dependencies
from capybase.resolution_engine import build_resolve_prompt


def _unit(base, current, replayed, worktree=None, *, path="app.py", lang="python"):
    worktree = worktree or f"<<<<<<< H\n{current}\n=======\n{replayed}\n>>>>>>> b\n"
    return ConflictUnit(
        session_id="s", step_index=1, path=path, language=lang,
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text=worktree, marker_span=(0, 4),
    )


# ---------------------------------------------------------------------------
# _slice_dependencies wiring
# ---------------------------------------------------------------------------


def test_slicing_disabled_by_default():
    """Without cross_file_slice, related_snippets is empty — no behavior change."""
    unit = _unit("x = compute()", "x = compute()", "x = compute()")
    cb = ContextBuilder()  # cross_file_slice defaults to False
    ctx = cb.build(unit)
    assert ctx.related_snippets == []


def test_slicing_resolves_referenced_definition(tmp_path):
    """A symbol referenced by the edited side resolves to its repo definition."""
    # Write a helper the conflict code calls, in another file.
    helper = tmp_path / "helpers.py"
    helper.write_text("def compute_tax(rate):\n    return rate * 0.1\n")
    unit = _unit("x = compute_tax(r)", "x = compute_tax(r)", "x = compute_tax(r)")
    cb = ContextBuilder(
        cross_file_slice=True,
        slice_search_globs=[str(tmp_path / "*.py")],
    )
    ctx = cb.build(unit)
    found = [s for s in ctx.related_snippets if s.reason == "compute_tax"]
    assert found, "expected compute_tax definition to be sliced"
    assert "def compute_tax" in found[0].text


def test_slicing_excludes_enclosing_node_name(tmp_path):
    """The enclosing block's own name isn't re-sliced (it's already primary_text)."""
    (tmp_path / "m.py").write_text("def greet():\n    return 'hi'\n")
    unit = _unit("    return greet()", "    return greet()", "    return greet()")
    unit.structural_metadata["enclosing_node_signature"] = "def greet():"
    cb = ContextBuilder(
        cross_file_slice=True,
        slice_search_globs=[str(tmp_path / "*.py")],
    )
    ctx = cb.build(unit)
    reasons = {s.reason for s in ctx.related_snippets}
    assert "greet" not in reasons


def test_slicing_caps_snippet_count(tmp_path):
    """max_related_snippets caps how many definitions are surfaced."""
    for i in range(5):
        (tmp_path / f"h{i}.py").write_text(f"def helper_{i}():\n    return {i}\n")
    body = "\n".join(f"    helper_{i}()" for i in range(5))
    unit = _unit(body, body, body)
    cb = ContextBuilder(
        cross_file_slice=True,
        slice_search_globs=[str(tmp_path / "*.py")],
        max_related_snippets=2,
    )
    ctx = cb.build(unit)
    assert len(ctx.related_snippets) <= 2


def test_slicing_truncates_long_definitions(tmp_path):
    """A huge definition is truncated to the per-snippet char budget."""
    long_body = "\n".join(f"    x{i} = {i}" for i in range(200))
    (tmp_path / "big.py").write_text(f"def giant():\n{long_body}\n")
    unit = _unit("    giant()", "    giant()", "    giant()")
    cb = ContextBuilder(
        cross_file_slice=True,
        slice_search_globs=[str(tmp_path / "*.py")],
        max_snippet_chars=120,
    )
    ctx = cb.build(unit)
    giant = [s for s in ctx.related_snippets if s.reason == "giant"]
    assert giant
    assert len(giant[0].text) <= 200  # truncated well below the full def


def test_slicing_skips_unsupported_language(tmp_path):
    """Non-python/rust languages yield no snippets (slicer has no grammar)."""
    unit = _unit("x = 1", "x = 1", "x = 1", lang="javascript")
    cb = ContextBuilder(cross_file_slice=True)
    ctx = cb.build(unit)
    assert ctx.related_snippets == []


def test_slicing_no_refs_returns_empty(tmp_path):
    """A side with no resolvable symbols produces no snippets."""
    unit = _unit("    1", "    1", "    1")  # bare literal, no identifiers
    cb = ContextBuilder(
        cross_file_slice=True,
        slice_search_globs=[str(tmp_path / "*.py")],
    )
    ctx = cb.build(unit)
    assert ctx.related_snippets == []


# ---------------------------------------------------------------------------
# _enclosing_name helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sig,expected",
    [
        ("def greet():", "greet"),
        ("async def fetch_all():", "fetch_all"),
        ("class Config:", "Config"),
        ("fn compute() -> u32", "compute"),
        ("struct Foo", "Foo"),
    ],
)
def test_enclosing_name_extracts(sig, expected):
    unit = _unit("x", "x", "x")
    unit.structural_metadata["enclosing_node_signature"] = sig
    assert _enclosing_name(unit) == expected


def test_enclosing_name_none_when_absent():
    unit = _unit("x", "x", "x")
    assert _enclosing_name(unit) is None


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def test_resolve_prompt_renders_dependency_block():
    """related_snippets surface as 'Definitions this conflict depends on'."""
    unit = _unit("    x = compute()", "    x = compute()", "    x = compute()")
    ctx = ContextBuilder().build(unit)
    # Inject a related snippet directly to test the prompt builder contract.
    ctx = ctx.model_copy(
        update={
            "related_snippets": [
                RelatedSnippet(path="lib/tax.py", text="def compute():\n    return 0", reason="compute")
            ]
        }
    )
    prompt = build_resolve_prompt(unit, ctx)
    assert "Definitions this conflict depends on" in prompt
    assert "lib/tax.py" in prompt
    assert "def compute():" in prompt


def test_resolve_prompt_omits_dependency_block_when_empty():
    """No related_snippets → no dependency section (unchanged prompt shape)."""
    unit = _unit("x = 1", "x = 1", "x = 1")
    ctx = ContextBuilder().build(unit)
    assert ctx.related_snippets == []
    prompt = build_resolve_prompt(unit, ctx)
    assert "Definitions this conflict depends on" not in prompt


def test_dependency_block_appears_before_sides():
    """The dependency neighborhood precedes the three sides so the model reads
    the definitions before interpreting the conflict bodies."""
    unit = _unit("    x = compute()", "    x = compute()", "    x = compute()")
    ctx = ContextBuilder().build(unit).model_copy(
        update={
            "related_snippets": [
                RelatedSnippet(path="h.py", text="def compute():\n    return 1", reason="compute")
            ]
        }
    )
    prompt = build_resolve_prompt(unit, ctx)
    deps_pos = prompt.find("Definitions this conflict depends on")
    sides_pos = prompt.find("CURRENT_UPSTREAM_SIDE body")
    assert deps_pos != -1 and sides_pos != -1
    assert deps_pos < sides_pos


# ---------------------------------------------------------------------------
# Orchestrator wiring
# ---------------------------------------------------------------------------


def test_orchestrator_threads_cross_file_slice():
    """The orchestrator passes structural.cross_file_slice + repo root to the
    context builder (smoke test: config flag flows through construction)."""
    from capybase.config import Config, PolicyConfig, StructuralConfig

    cfg = Config(policy=PolicyConfig(), structural=StructuralConfig(cross_file_slice=True))
    assert cfg.structural.cross_file_slice is True
    # The builder would be constructed with cross_file_slice=True; verify the
    # field exists on StructuralConfig and is the source the orchestrator reads.
    # (Full orchestrator integration is covered by test_structural_orchestrator.)


needs_ts = pytest.mark.skipif(
    not __import__("capybase.adapters.structural", fromlist=["is_available"]).is_available("python"),
    reason="tree-sitter not installed",
)

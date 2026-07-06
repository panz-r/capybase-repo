"""Tests for the tree-sitter structural adapter and AST preservation validator.

These exercise the structural seam end-to-end: parsing, enclosing-node
resolution, fingerprint stability, extractor enrichment, context surfacing,
and the AstPreservationValidator catching a structural corruption that the
line-level ExactSpliceScope check misses.

Skips gracefully when tree-sitter is not installed (the structural extra is
optional). The CI/dev venv installs it; a minimal install should still pass.
"""

from __future__ import annotations

import pytest

structural = pytest.importorskip("capybase.adapters.structural")
needs_ts = pytest.mark.skipif(
    not structural.is_available("python"),
    reason="tree-sitter python grammar not installed",
)

from capybase.adapters import structural as S
from capybase.adapters.parsers import splice_resolution
from capybase.conflict_model import (
    CandidateResolution,
    ConflictSide,
    ConflictUnit,
)
from capybase.verification import ValidationConfig, VerificationEngine


# ---------------------------------------------------------------------------
# Adapter: parse / enclosing_node / fingerprint
# ---------------------------------------------------------------------------


PY_SOURCE = """def greet():
    return 'hi'

def farewell():
    return 'bye'

class Config:
    value = 1
"""


@needs_ts
def test_enclosing_node_resolves_function():
    node = S.enclosing_node(PY_SOURCE, (1, 1), "python")
    assert node is not None
    assert node.node_type == "function"
    assert node.signature == "def greet():"
    assert "return 'hi'" in node.text


@needs_ts
def test_enclosing_node_resolves_class():
    node = S.enclosing_node(PY_SOURCE, (7, 7), "python")
    assert node is not None
    assert node.node_type == "class"
    assert node.signature == "class Config:"


@needs_ts
def test_enclosing_node_anchors_on_span_start():
    # A span that extends past a definition's body still resolves to that
    # definition, because we anchor on the span START line (robust to the
    # marker-block being wider than the base content it replaces).
    node = S.enclosing_node(PY_SOURCE, (1, 5), "python")
    assert node is not None
    assert node.node_type == "function"


@needs_ts
def test_fingerprint_stable_under_whitespace_and_comments():
    a = S.ast_fingerprint("def f():\n    return 1\n", "python")
    b = S.ast_fingerprint("def f():\n    # comment\n    return 1\n\n\n", "python")
    assert a is not None and b is not None
    assert a == b


@needs_ts
def test_fingerprint_differs_for_different_structure():
    a = S.ast_fingerprint("def f():\n    return 1\n", "python")
    b = S.ast_fingerprint("def f():\n    return 1\n    print(1)\n", "python")
    assert a != b


@needs_ts
def test_fingerprint_region_partitions_nodes():
    outside, inside = S.fingerprint_region(PY_SOURCE, "python", (0, 1))
    assert outside is not None and inside is not None
    # The outside region must mention farewell and Config (unchanged units).
    # The abstract fingerprint uses coarse kinds: function/class.
    assert outside.count("function") >= 1
    assert "class:Config" in outside


@needs_ts
def test_fingerprint_region_detects_outside_deletion():
    full = "def greet():\n    return 1\n\ndef farewell():\n    return 2\n"
    drop = "def greet():\n    return 1\n"  # farewell removed
    out_full, _ = S.fingerprint_region(full, "python", (1, 1))
    out_drop, _ = S.fingerprint_region(drop, "python", (1, 1))
    assert out_full != out_drop


@needs_ts
def test_rust_enclosing_node_impl():
    rs = """impl Counter {
    fn next(&self) -> u32 {
        self.count + 1
    }
}
"""
    assert S.is_available("rust")
    node = S.enclosing_node(rs, (2, 2), "rust")
    assert node is not None
    # The impl is a container-only scope; a span inside fn next resolves to the
    # method (kind "method"), not the impl. Mirrors tree-sitter, where impl_item
    # is a container whose body is enumerated, not an entity itself.
    assert node.node_type == "method"
    assert node.signature is not None and "fn next" in node.signature


def test_is_available_false_for_unsupported_language():
    assert S.is_available("cobol") is False


def test_enclosing_node_returns_none_for_unknown_language():
    assert S.enclosing_node("x = 1", (0, 0), "cobol") is None


def test_referenced_symbols_extracts_identifiers():
    names = S.referenced_symbols("    return greet(x) + Farewell.VALUE", "python")
    assert "greet" in names
    assert "Farewell" in names
    assert "VALUE" in names
    assert "return" not in names  # keywords excluded


# ---------------------------------------------------------------------------
# AstPreservationValidator
# ---------------------------------------------------------------------------


def _unit(base, current, replayed, worktree, span, with_fingerprint=True):
    u = ConflictUnit(
        session_id="s",
        step_index=1,
        path="app.py",
        language="python",
        conflict_type="UU",
        unit_id="u",
        unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text=worktree,
        marker_span=span,
    )
    # The extractor computes the base fingerprint on the clean BASE blob;
    # mirror that here so the validator has a consistent baseline.
    if with_fingerprint and S.is_available("python"):
        outside, _ = S.fingerprint_region(base, "python", span)
        if outside is not None:
            u.structural_metadata["ast_fingerprint_base_outside"] = outside
    return u


def _candidate(resolved):
    return CandidateResolution(
        candidate_id="c",
        unit_id="u",
        model_name="m",
        prompt_version="resolve_text_block.v4",
        resolved_text=resolved,
    )


def _engine():
    return VerificationEngine.default(ValidationConfig())


@needs_ts
def test_ast_validator_passes_preserving_resolution():
    base = "def greet():\n    return 'hi'\n\ndef farewell():\n    return 'bye'\n"
    worktree = (
        "def greet():\n"
        "<<<<<<< H\n    return 'hi'\n"
        "=======\n    return 'howdy'\n"
        ">>>>>>> b\n"
        "\n"
        "def farewell():\n    return 'bye'\n"
    )
    unit = _unit(base, "    return 'hi'", "    return 'howdy'", worktree, (1, 5))
    cand = _candidate("    return ('hi', 'howdy')")
    res = _engine().verify(unit, cand)
    assert res.passed, [f.message for f in res.hard_failures]
    assert res.features.get("ast_preserved") is True
    assert res.features.get("ast_checked") is True


@needs_ts
def test_ast_validator_catches_injected_definition():
    # A model that injects a new top-level def inside what was greet's body.
    # This changes the structural fingerprint of nodes outside the span
    # (the file now has an extra function_definition), which the line-level
    # splice-scope check cannot detect.
    base = "def greet():\n    return 'hi'\n\ndef farewell():\n    return 'bye'\n"
    worktree = (
        "def greet():\n"
        "<<<<<<< H\n    return 'hi'\n"
        "=======\n    return 'howdy'\n"
        ">>>>>>> b\n"
        "\n"
        "def farewell():\n    return 'bye'\n"
    )
    unit = _unit(base, "    return 'hi'", "    return 'howdy'", worktree, (1, 5))
    cand = _candidate("    return ('hi', 'howdy')\ndef injected():\n    pass")
    res = _engine().verify(unit, cand)
    assert not res.passed
    assert any(f.validator == "ast_preservation" for f in res.hard_failures)
    assert res.features.get("ast_preserved") is False


@needs_ts
def test_ast_validator_inert_without_base_fingerprint():
    worktree = "def f():\n<<<<<<< H\n    return 1\n=======\n    return 2\n>>>>>>> b\n"
    unit = _unit(
        "def f():\n    pass",
        "    return 1",
        "    return 2",
        worktree,
        (1, 5),
        with_fingerprint=False,
    )
    cand = _candidate("    return 3")
    res = _engine().verify(unit, cand)
    assert res.features.get("ast_checked") is False
    assert res.features.get("ast_preserved") is True


# ---------------------------------------------------------------------------
# Extractor enrichment (integration)
# ---------------------------------------------------------------------------


class _FakeGit:
    """Yields a clean BASE and a marker-laden worktree for one unit."""

    def __init__(self, base, worktree):
        self._base = base
        self._worktree = worktree

    def read_stage_blob(self, path, stage):
        return self._base.encode("utf-8")

    def read_worktree_file(self, path):
        return self._worktree.encode("utf-8")


@needs_ts
def test_extractor_populates_structural_metadata():
    from capybase.config import StructuralConfig
    from capybase.conflict_extractor import ConflictExtractor

    base = "def greet():\n    return 'hi'\n"
    worktree = (
        "def greet():\n<<<<<<< H\n    return 'hi'\n"
        "=======\n    return 'howdy'\n>>>>>>> b\n"
    )
    ex = ConflictExtractor(
        _FakeGit(base, worktree), structural_config=StructuralConfig(enabled=True)
    )
    units = ex.extract_file_units("app.py", 1, "s")
    assert len(units) == 1
    u = units[0]
    assert u.unit_kind == "ast_region"
    assert u.structural_metadata.get("enclosing_node_type") == "function"
    assert u.structural_metadata.get("enclosing_node_signature") == "def greet():"
    assert "ast_fingerprint_base_outside" in u.structural_metadata
    assert u.enclosing_symbol == "def greet():"


@needs_ts
def test_extractor_inert_when_structural_disabled():
    from capybase.conflict_extractor import ConflictExtractor

    base = "def greet():\n    return 'hi'\n"
    worktree = (
        "def greet():\n<<<<<<< H\n    return 'hi'\n"
        "=======\n    return 'howdy'\n>>>>>>> b\n"
    )
    ex = ConflictExtractor(_FakeGit(base, worktree))  # no structural_config
    units = ex.extract_file_units("app.py", 1, "s")
    u = units[0]
    assert u.unit_kind == "text_marker_block"
    assert "enclosing_node_type" not in u.structural_metadata


# ---------------------------------------------------------------------------
# Context builder surfacing
# ---------------------------------------------------------------------------


@needs_ts
def test_context_builder_surfaces_enclosing_node():
    from capybase.config import StructuralConfig
    from capybase.conflict_extractor import ConflictExtractor
    from capybase.context_builder import ContextBuilder

    base = "def greet():\n    return 'hi'\n\ndef farewell():\n    return 'bye'\n"
    worktree = (
        "def greet():\n<<<<<<< H\n    return 'hi'\n"
        "=======\n    return 'howdy'\n>>>>>>> b\n"
        "\ndef farewell():\n    return 'bye'\n"
    )
    # Use the extractor to build a properly enriched unit (the context builder
    # reads enclosing_node_type from structural_metadata, which only the
    # extractor populates — not the bare _unit helper).
    ex = ConflictExtractor(
        _FakeGit(base, worktree), structural_config=StructuralConfig(enabled=True)
    )
    unit = ex.extract_file_units("app.py", 1, "s")[0]
    ctx = ContextBuilder(context_lines=5).build(unit)
    assert ctx.structural_view.get("enclosing_node_type") == "function"
    assert ctx.structural_view.get("enclosing_node_signature") == "def greet():"

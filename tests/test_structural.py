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
    reason="abstract parser unavailable for python",
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


@pytest.mark.parametrize("lang,leaked_keyword", [
    ("go", "func"), ("go", "package"), ("go", "chan"), ("go", "defer"),
    ("rust", "fn"), ("rust", "crate"), ("rust", "unsafe"), ("rust", "let"),
    ("javascript", "typeof"), ("javascript", "await"), ("javascript", "undefined"),
    ("java", "synchronized"), ("java", "throws"), ("java", "instanceof"),
    ("csharp", "namespace"), ("kotlin", "fun"), ("swift", "guard"),
])
def test_referenced_symbols_filters_per_language_keywords(lang, leaked_keyword):
    """Each language's reserved keywords are filtered out of the symbol list —
    not just Python's. Previously ``referenced_symbols`` used Python's
    ``keyword.iskeyword`` for ALL languages, so Go ``func``/Rust ``crate``/JS
    ``typeof``/Java ``synchronized`` leaked into the cross-commit ``uses`` set
    and the dependency-drop check (where they could form spurious edges or
    trigger false 'dropped symbol' reports)."""
    syms = S.referenced_symbols(f"{leaked_keyword} real_symbol()", lang)
    assert leaked_keyword not in syms
    assert "real_symbol" in syms  # the actual reference survives


def test_referenced_symbols_unknown_language_degrades_gracefully():
    """An unrecognized language yields no keyword filtering (empty set), not
    Python's list — so symbols are extracted without false keyword matches.
    Better to over-extract (miss is safe) than to apply the wrong language's
    keyword list (the prior Python-only bug)."""
    syms = S.referenced_symbols("SELECT col FROM tbl", "sql")
    assert "col" in syms and "tbl" in syms


def test_referenced_symbols_keeps_types_and_values_for_real_code():
    """Realistic multi-language snippets: keywords filtered, real call targets
    and identifiers kept. Guards against an over-broad keyword set suppressing
    genuine symbol references."""
    # Go: helper() is a real dependency; func/package/int filtered.
    go = S.referenced_symbols(
        "package main\n\nfunc main() int {\n    return helper()\n}", "go")
    assert "helper" in go and "main" in go
    assert "func" not in go and "package" not in go and "int" not in go
    # Rust: compute() kept; fn/let/u32 filtered.
    rust = S.referenced_symbols(
        "fn run() -> u32 {\n    let x = compute();\n    x\n}", "rust")
    assert "compute" in rust and "run" in rust
    assert "fn" not in rust and "let" not in rust and "u32" not in rust


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


# ---------------------------------------------------------------------------
# Fourth-pass: Consumer-side container-scope leak in enumerate_entities.
# ---------------------------------------------------------------------------


@needs_ts
def test_enumerate_entities_skips_container_scope_at_module_level():
    """The whole-module path (``container_span=None``) of enumerate_entities
    used to emit top-level ``impl``/``mod``/``namespace`` container-scope units
    as entities — whereas ``_all_flat_entities`` and ``duplicate_definitions``
    both skip them. Same container block, three different answers. After the
    fix, the whole-module path also skips container-scope units (an impl is a
    distinct scope, not an entity). The whole-module path returns top-level
    units only (children are surfaced via container_span queries), so we assert
    the impl is absent and the top-level struct is present."""
    from capybase.adapters import abstract_parser
    src = (
        "impl Config {\n"
        "    fn new() -> Self { Self {} }\n"
        "    fn load(&self) -> i32 { 0 }\n"
        "}\n"
        "\n"
        "pub struct Config {\n"
        "    name: String,\n"
        "}\n"
    )
    ents = S.enumerate_entities(src, "rust", container_span=None)
    assert ents is not None
    names = [e.name for e in ents]
    kinds = [e.kind for e in ents]
    # The impl block (container-scope) must NOT appear as an entity — neither
    # under a name nor as an anonymous None entity.
    assert "impl Config" not in names and None not in names, (
        f"container-scope impl must not be emitted as an entity; got names={names} kinds={kinds}"
    )
    # The top-level struct Config IS an entity.
    assert "Config" in names, f"struct Config must be an entity; got {names}"


# --- D-1 (round 8): match_entities must agree with canonical rename core ---


def test_r8_match_entities_rust_rename_with_comment():
    r"""match_entities (used by dropped_entities / preservation_coverage /
    unattributed_entities — the validator-facing rename path) must agree with
    the canonical detect_renames_2way: a Rust rename that also drops a ``//``
    comment must PAIR. Previously match_entities used the comment-PRESERVING
    entity_body_fingerprint, so the comment difference broke pairing — causing
    a false 'dropped entity' flag while the resolver/3-way-diff correctly
    recognized the rename."""
    from capybase.adapters.structural import Entity, match_entities
    b = Entity(kind="function", name="loadData",
               body="fn loadData() {\n    let x = 1; // note\n    x\n}", span=(0, 3))
    s = Entity(kind="function", name="fetchData",
               body="fn fetchData() {\n    let x = 1;\n    x\n}", span=(0, 3))
    matches = match_entities([b], [s], lang="rust")
    kinds = [m.kind for m in matches]
    assert "renamed" in kinds or "possibly_renamed" in kinds, (
        f"a Rust rename dropping a // comment must pair in match_entities; got {kinds}"
    )

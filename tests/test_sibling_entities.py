"""Tests for entity-neighborhood context (survey §4.1/§5.4 Rover).

P3: the model sees the OTHER methods/fields co-located in the same container as
the conflict — the entity neighborhood it must stay consistent with. This is
the survey's finding that *some* structured organization of context lifts a
small LLM's output, at near-zero cost (signatures only, no bodies). Distinct
from the cross-file callee definitions surfaced elsewhere.

Covers sibling_signatures, the extraction enrichment, context surfacing, and
prompt rendering. Skips gracefully when tree-sitter is absent.
"""

from __future__ import annotations

import pytest

structural = pytest.importorskip("capybase.adapters.structural")
needs_ts = pytest.mark.skipif(
    not structural.is_available("python"),
    reason="tree-sitter python grammar not installed",
)

from capybase.adapters import structural as S
from capybase.conflict_model import ConflictSide, ContextBundle, ConflictUnit
from capybase.context_builder import ContextBuilder
from capybase.resolution_engine import build_resolve_prompt


PY_CLASS = """class Service:
    def __init__(self):
        self.data = {}
    def load(self, k):
        return self.data.get(k)
    def save(self, k, v):
        self.data[k] = v
    def close(self):
        self.fd.close()
"""


def _unit(base="def save(self, k, v):\n        self.data[k] = v", **meta):
    u = ConflictUnit(
        session_id="s", step_index=1, path="svc.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=base),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=base),
        original_worktree_text="", marker_span=(3, 4),
    )
    for k, v in meta.items():
        u.structural_metadata[k] = v
    return u


# ---------------------------------------------------------------------------
# sibling_signatures — the coarse listing
# ---------------------------------------------------------------------------


@needs_ts
def test_sibling_signatures_lists_other_methods():
    """A conflict inside 'save' → siblings are __init__, load, close."""
    sigs = S.sibling_signatures(PY_CLASS, "python", container_span=(4, 4), exclude="save")
    assert sigs is not None
    assert "def __init__(self):" in sigs
    assert "def load(self, k):" in sigs
    assert "def close(self):" in sigs
    assert "def save" not in sigs  # excluded


@needs_ts
def test_sibling_signatures_excludes_enclosing_entity():
    sigs = S.sibling_signatures(PY_CLASS, "python", container_span=(2, 2), exclude="load")
    assert "def load" not in (sigs or [])


@needs_ts
def test_sibling_signatures_without_exclude_keeps_all():
    sigs = S.sibling_signatures(PY_CLASS, "python", container_span=(4, 4))
    assert sigs is not None
    assert "def save(self, k, v):" in sigs  # nothing excluded


@needs_ts
def test_sibling_signatures_capped_by_limit():
    """A large class is capped so the prompt doesn't bloat."""
    big = "class C:\n" + "\n".join(f"    def m{i}(self): pass" for i in range(20))
    sigs = S.sibling_signatures(big, "python", container_span=(1, 1), exclude="m0", limit=5)
    assert sigs is not None
    assert len(sigs) <= 5


@needs_ts
def test_sibling_signatures_empty_when_no_others():
    """A single-method class has no siblings."""
    src = "class C:\n    def only(self):\n        pass\n"
    sigs = S.sibling_signatures(src, "python", container_span=(1, 1), exclude="only")
    assert sigs == []


@needs_ts
def test_sibling_signatures_rust_impl():
    rs = "impl S {\n    fn a(&self) {}\n    fn b(&self) {}\n    fn c(&self) {}\n}\n"
    sigs = S.sibling_signatures(rs, "rust", container_span=(1, 1), exclude="b")
    assert sigs is not None
    assert "fn a(&self) {}" in sigs
    assert "fn c(&self) {}" in sigs
    assert "fn b" not in sigs


# ---------------------------------------------------------------------------
# Extraction enrichment: sibling_entities populated
# ---------------------------------------------------------------------------


@needs_ts
def test_enricher_populates_sibling_entities():
    """The structural enricher records sibling_entities from the base blob."""
    from capybase.config import StructuralConfig
    from capybase.conflict_extractor import ConflictExtractor

    base = PY_CLASS
    # Conflict is in 'save' (lines 4-5); worktree has markers there.
    worktree = (
        "class Service:\n"
        "    def __init__(self):\n        self.data = {}\n"
        "    def load(self, k):\n        return self.data.get(k)\n"
        "    def save(self, k, v):\n"
        "<<<<<<< H\n        self.data[k] = v\n=======\n        self._set(k, v)\n>>>>>>> b\n"
        "    def close(self):\n        self.fd.close()\n"
    )

    class FakeGit:
        def read_stage_blob(self, path, stage):
            return base.encode("utf-8")

        def read_worktree_file(self, path):
            return worktree.encode("utf-8")

    ex = ConflictExtractor(FakeGit(), structural_config=StructuralConfig(enabled=True))
    units = ex.extract_file_units("svc.py", 1, "s")
    assert len(units) == 1
    sibs = units[0].structural_metadata.get("sibling_entities")
    assert sibs is not None
    assert "def __init__(self):" in sibs
    assert "def close(self):" in sibs
    assert "def save" not in sibs  # enclosing entity excluded


# ---------------------------------------------------------------------------
# Context surfacing: sibling_entities → structural_view
# ---------------------------------------------------------------------------


@needs_ts
def test_context_builder_surfaces_siblings():
    """ContextBundle.structural_view carries sibling_entities when present."""
    unit = _unit()
    unit.structural_metadata["sibling_entities"] = ["def load(self, k):", "def close(self):"]
    ctx = ContextBuilder().build(unit)
    assert "sibling_entities" in ctx.structural_view
    assert "def load(self, k):" in ctx.structural_view["sibling_entities"]


def test_context_builder_omits_siblings_when_absent():
    """No sibling_entities → key absent from structural_view (unchanged shape)."""
    unit = _unit()
    ctx = ContextBuilder().build(unit)
    assert "sibling_entities" not in ctx.structural_view


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def test_prompt_renders_sibling_block():
    unit = _unit()
    ctx = ContextBundle(
        primary_text="ctx",
        structural_view={
            "enclosing_node_signature": "def save(self, k, v):",
            "enclosing_node_text": "def save...",
            "sibling_entities": ["def load(self, k):", "def close(self):"],
        },
    )
    prompt = build_resolve_prompt(unit, ctx)
    assert "Other entities in this container" in prompt
    assert "def load(self, k):" in prompt
    assert "def close(self):" in prompt


def test_prompt_omits_sibling_block_when_empty():
    unit = _unit()
    ctx = ContextBundle(primary_text="ctx", structural_view={})
    prompt = build_resolve_prompt(unit, ctx)
    assert "Other entities in this container" not in prompt


def test_sibling_block_after_anchor_before_sides():
    """Ordering: structural anchor → siblings → three sides."""
    unit = _unit()
    ctx = ContextBundle(
        primary_text="ctx",
        structural_view={
            "enclosing_node_signature": "def save(self, k, v):",
            "enclosing_node_text": "def save(self, k, v):\n    self.data[k] = v",
            "sibling_entities": ["def load(self, k):"],
        },
    )
    prompt = build_resolve_prompt(unit, ctx)
    anchor_pos = prompt.find("Logical block you are merging inside")
    sib_pos = prompt.find("Other entities in this container")
    sides_pos = prompt.find("CURRENT_UPSTREAM_SIDE body")
    assert -1 < anchor_pos < sib_pos < sides_pos


def test_sibling_block_included_in_prompt_variants():
    """All prompt variants carry the sibling block (variant invariance)."""
    from capybase.resolution_engine import build_resolve_prompt_variants

    unit = _unit()
    ctx = ContextBundle(
        primary_text="ctx",
        structural_view={
            "enclosing_node_signature": "def save(self, k, v):",
            "enclosing_node_text": "def save...",
            "sibling_entities": ["def load(self, k):"],
        },
    )
    for prompt, _suffix in build_resolve_prompt_variants(unit, ctx, k=3):
        assert "Other entities in this container" in prompt
        assert "def load(self, k):" in prompt

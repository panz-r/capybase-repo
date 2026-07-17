"""Semantic entity matching — embedding rename tier.

The 4th tier of ``match_entities``: when name + body-fingerprint + Jaccard all
fail to pair a renamed entity (because the body was edited beyond the 0.80
Jaccard floor), embeddings catch the rename by semantic similarity. This closes
the false-positive class where a renamed+edited function fires as both
``dropped_entities`` (old name gone) and ``unattributed_entities`` (new name
novel).

A deterministic fake embedder (vectors derived from a controllable text→vector
map) makes the cosine thresholds assertable without a live endpoint.
"""

from __future__ import annotations

import pytest

from capybase.adapters import structural
from capybase.adapters.structural import (
    Entity,
    MATCH_POSSIBLY_RENAMED,
    MATCH_RENAMED,
    MATCH_SAME_NAME,
    MATCH_UNMATCHED,
    dropped_entities,
    match_entities,
    preservation_coverage,
    set_entity_embedder,
    unattributed_entities,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _MapEmbedder:
    """Returns a caller-specified vector per text; records call count.

    Lets tests make two bodies embed to near-identical vectors (a rename) or
    orthogonal vectors (distinct), controlling the cosine outcome directly.
    """

    def __init__(self, mapping: dict[str, list[float]]):
        self.mapping = mapping
        self.calls = 0

    def embed(self, texts):
        if isinstance(texts, str):
            texts = [texts]
        self.calls += 1
        return [self.mapping.get(t, [0.0, 0.0, 0.0, 0.0]) for t in texts]


def _fn(name: str, body: str) -> Entity:
    """A function Entity with the given name and body text."""
    return Entity(kind="function", name=name, body=f"def {name}():\n{body}", span=(0, 0))


# Two vectors that are very similar (cosine ~0.99) — a rename.
_VEC_OLD = [1.0, 0.0, 0.1, 0.0]
_VEC_NEW = [0.99, 0.0, 0.1, 0.0]
# An orthogonal vector — a distinct function.
_VEC_DIFFERENT = [0.0, 1.0, 0.0, 0.0]


# ---------------------------------------------------------------------------
# match_entities: embedding tier
# ---------------------------------------------------------------------------


def test_embedding_tier_matches_renamed_heavily_edited_body():
    """A rename whose body edit dropped it below the Jaccard floor is caught.

    ``validate_token`` renamed to ``check_token`` with a body edit that makes
    the Jaccard fall below 0.80 — the name/body-fp/Jaccard tiers all miss it,
    but the embedding tier (cosine ~0.99 ≥ 0.85) pairs it as a rename.
    """
    from capybase.memory.embeddings import normalize_body_for_embedding

    src = _fn("validate_token", "    token = read()\n    return token is not None")
    tgt = _fn("check_token", "    tok = get()\n    return tok is not None")  # paraphrased
    # Map the normalized bodies to near-identical vectors.
    emb = _MapEmbedder({
        normalize_body_for_embedding(structural.entity_body_fingerprint(src, "") or ""): _VEC_OLD,
        normalize_body_for_embedding(structural.entity_body_fingerprint(tgt, "") or ""): _VEC_NEW,
    })
    matches = match_entities([src], [tgt], embedder=emb)
    assert len(matches) == 1
    assert matches[0].kind == MATCH_RENAMED
    assert matches[0].target is not None
    assert matches[0].target.name == "check_token"


def test_embedding_tier_possibly_renamed_in_mid_band():
    """Cosine 0.70–0.85 with a corroborating signal → possibly_renamed.

    The conjunction rule: a mid-band embedding match is accepted
    only with a corroborating Jaccard ≥ 0.80 OR name-similarity ≥ 0.6. Here the
    bodies share enough tokens for Jaccard ≥ 0.80 but a single token differs
    (so they don't pair by exact body-fp), and the embedding cosine is mid-band.
    """
    from capybase.memory.embeddings import normalize_body_for_embedding

    # Bodies that differ by one token (Jaccard high but not exact).
    src = _fn("compute_total", "    base = read_count()\n    extra = load_extra()\n    return base + extra + TAX")
    tgt = _fn("calc_total", "    base = read_count()\n    extra = load_extra()\n    return base + extra + FEE")
    # cos([1,0,0,0], [0.8, 0.6, 0, 0]) = 0.8 (mid-band).
    v_lo = [1.0, 0.0, 0.0, 0.0]
    v_mid = [0.8, 0.6, 0.0, 0.0]
    emb = _MapEmbedder({
        normalize_body_for_embedding(structural.entity_body_fingerprint(src, "") or ""): v_lo,
        normalize_body_for_embedding(structural.entity_body_fingerprint(tgt, "") or ""): v_mid,
    })
    matches = match_entities([src], [tgt], embedder=emb)
    assert len(matches) == 1
    assert matches[0].kind == MATCH_POSSIBLY_RENAMED


def test_embedding_tier_orthogonal_bodies_stay_unmatched():
    """Genuinely-distinct functions (cosine < 0.70) stay unmatched."""
    from capybase.memory.embeddings import normalize_body_for_embedding

    src = _fn("foo", "    return 1")
    tgt = _fn("bar", "    print('hello world')")
    emb = _MapEmbedder({
        normalize_body_for_embedding(structural.entity_body_fingerprint(src, "") or ""): _VEC_OLD,
        normalize_body_for_embedding(structural.entity_body_fingerprint(tgt, "") or ""): _VEC_DIFFERENT,
    })
    matches = match_entities([src], [tgt], embedder=emb)
    assert matches[0].kind == MATCH_UNMATCHED


def test_r39_embedding_tier_skips_empty_norm_target():
    """r39 (MEDIUM): the embedding-tier candidate loop ``break``ed (instead of
    ``continue``-ing) when a target's normalized body was empty (e.g. a
    comment-only body). Such a target has no vector, so it was excluded from
    the embed call — but the ``break`` aborted the whole loop, skipping every
    SUBSEQUENT candidate. A legitimate rename was missed (order-dependently)
    when an empty-norm target happened to precede the true match."""
    from capybase.memory.embeddings import normalize_body_for_embedding

    src = _fn("old_op", "    d=load_cache(); r=transform_data(d); log_event(r); persist(r); return r")
    # An empty-norm target (comment-only body) that sorts FIRST, then the true match.
    tgt_empty = _fn("aaa_co", "    # only comment")
    tgt_good = _fn("zzz_op", "    d=load_cache(); r=transform_data(d); return r")
    src_norm = normalize_body_for_embedding(structural.entity_body_fingerprint(src, "") or "")
    good_norm = normalize_body_for_embedding(structural.entity_body_fingerprint(tgt_good, "") or "")
    emb = _MapEmbedder({src_norm: _VEC_OLD, good_norm: _VEC_NEW})
    # Empty-norm target FIRST — the loop must skip it and still reach the good one.
    matches = match_entities([src], [tgt_empty, tgt_good], embedder=emb)
    assert matches[0].kind in (MATCH_RENAMED, MATCH_POSSIBLY_RENAMED), (
        f"empty-norm target broke the loop, missing the rename; got {matches[0].kind}"
    )
    assert matches[0].target.name == "zzz_op"


def test_copy_is_not_a_rename():
    """If the source name still exists in targets, it's not a rename (a copy)."""
    from capybase.memory.embeddings import normalize_body_for_embedding

    src = _fn("foo", "    return 1")
    # 'foo' exists in targets (a copy) + a similar 'bar' — neither is a rename.
    tgt_foo = _fn("foo", "    return 1")
    tgt_bar = _fn("bar", "    return 1")
    emb = _MapEmbedder({})
    matches = match_entities([src], [tgt_foo, tgt_bar], embedder=emb)
    # src 'foo' matches tgt 'foo' by exact name (tier 1) — never reaches embeddings.
    assert matches[0].kind == MATCH_SAME_NAME


def test_embedder_none_is_byte_identical_to_pre_embedding():
    """Passing embedder=None explicitly disables the tier (regression guard).

    Two functions with identical bodies (different names) pair by exact body-
    fingerprint (tier 2) without any embedding. Passing embedder=None must
    produce the same result as omitting it when the singleton is also None.
    """
    body = "    return compute_value(x)\n    log('done')"
    src = _fn("foo", body)
    tgt = _fn("bar", body)
    set_entity_embedder(None)  # ensure singleton doesn't interfere
    none_matches = match_entities([src], [tgt], embedder=None)
    # Exact body-fingerprint → MATCH_RENAMED (without any embedding).
    assert none_matches[0].kind == MATCH_RENAMED


def test_embedding_tier_never_raises_on_embed_failure():
    """A failing embedder leaves the entity unmatched (never raises)."""
    src = _fn("foo", "    return compute_value(x)")
    tgt = _fn("bar", "    return compute_value(y)")

    class _BoomEmbedder:
        def embed(self, texts):
            raise RuntimeError("endpoint down")

    matches = match_entities([src], [tgt], embedder=_BoomEmbedder())
    assert matches[0].kind == MATCH_UNMATCHED  # degraded, not a crash


def test_embedding_tier_only_called_on_unmatched():
    """The embed call should NOT happen for entities matched by cheaper tiers."""
    from capybase.memory.embeddings import normalize_body_for_embedding

    src = _fn("foo", "    return 1")
    tgt = _fn("foo", "    return 2")  # same name → tier 1 match
    emb = _MapEmbedder({})
    match_entities([src], [tgt], embedder=emb)
    assert emb.calls == 0  # never embedded — matched by name


# ---------------------------------------------------------------------------
# Dropped / unattributed suppression
# ---------------------------------------------------------------------------


def test_dropped_entities_suppresses_semantic_rename(tmp_path):
    """A renamed entity doesn't surface as dropped when embedder is present."""
    from capybase.memory.embeddings import normalize_body_for_embedding

    base = "def helper():\n    return 1\n"
    # Side adds validate_token; resolution has it as check_token (renamed).
    side = "def validate_token():\n    token = read()\n    return token is not None\n"
    resolved = "def check_token():\n    tok = get()\n    return tok is not None\n"

    def _fp_for(text, name):
        ents = structural.enumerate_entities(text, "python") or []
        for e in ents:
            if e.name == name:
                return normalize_body_for_embedding(structural.entity_body_fingerprint(e, "") or "")
        return ""

    emb = _MapEmbedder({
        _fp_for(side, "validate_token"): _VEC_OLD,
        _fp_for(resolved, "check_token"): _VEC_NEW,
    })
    dropped = dropped_entities(base, side, resolved, "python", embedder=emb)
    assert dropped == []  # renamed — not dropped


def test_unattributed_entities_suppresses_semantic_rename():
    """A renamed entity doesn't surface as unattributed when embedder is present."""
    from capybase.memory.embeddings import normalize_body_for_embedding

    resolved = "def check_token():\n    tok = get()\n    return tok is not None\n"
    current = "def validate_token():\n    token = read()\n    return token is not None\n"

    def _fp_for(text, name):
        ents = structural.enumerate_entities(text, "python") or []
        for e in ents:
            if e.name == name:
                return normalize_body_for_embedding(structural.entity_body_fingerprint(e, "") or "")
        return ""

    emb = _MapEmbedder({
        _fp_for(resolved, "check_token"): _VEC_NEW,
        _fp_for(current, "validate_token"): _VEC_OLD,
    })
    unattrib = unattributed_entities("", current, "", resolved, "python", embedder=emb)
    assert unattrib == []  # attributed via semantic rename


# ---------------------------------------------------------------------------
# Module-level singleton wiring
# ---------------------------------------------------------------------------


def test_singleton_embedder_picked_up_when_arg_omitted():
    """match_entities consults set_entity_embedder when no explicit embedder."""
    from capybase.memory.embeddings import normalize_body_for_embedding

    src = _fn("validate_token", "    token = read()\n    return token is not None")
    tgt = _fn("check_token", "    tok = get()\n    return tok is not None")
    emb = _MapEmbedder({
        normalize_body_for_embedding(structural.entity_body_fingerprint(src, "") or ""): _VEC_OLD,
        normalize_body_with_fp(tgt): _VEC_NEW,
    })
    set_entity_embedder(emb)
    try:
        # No explicit embedder arg → uses the singleton.
        matches = match_entities([src], [tgt])
        assert matches[0].kind == MATCH_RENAMED
    finally:
        set_entity_embedder(None)  # always restore


def normalize_body_with_fp(entity):
    from capybase.memory.embeddings import normalize_body_for_embedding

    return normalize_body_for_embedding(structural.entity_body_fingerprint(entity, "") or "")


def test_singleton_none_restores_pure_deterministic():
    """set_entity_embedder(None) makes matching byte-identical to pre-embedding.

    Two functions with identical bodies (different names) pair by exact body-
    fingerprint (tier 2) — no embedding involved. This confirms the singleton=None
    path is pure-deterministic and unaffected by the embedding tier.
    """
    set_entity_embedder(None)
    # Identical bodies (only the def header differs) → body-fp matches.
    body = "    return compute_value(x)\n    log('done')"
    src = _fn("foo", body)
    tgt = _fn("bar", body)
    matches = match_entities([src], [tgt])  # no embedder arg, singleton None
    # Exact body-fingerprint → MATCH_RENAMED (header-stripped bodies equal).
    assert matches[0].kind == MATCH_RENAMED


def test_preservation_coverage_counts_semantic_rename_as_preserved():
    """A semantic rename doesn't lower the coverage ratio."""
    from capybase.memory.embeddings import normalize_body_for_embedding

    base = "def helper():\n    return 1\n"
    side = "def new_fn():\n    token = read()\n    return token is not None\n"
    resolved = "def renamed_fn():\n    tok = get()\n    return tok is not None\n"

    def _fp(text, name):
        ents = structural.enumerate_entities(text, "python") or []
        for e in ents:
            if e.name == name:
                return normalize_body_for_embedding(structural.entity_body_fingerprint(e, "") or "")
        return ""

    emb = _MapEmbedder({
        _fp(side, "new_fn"): _VEC_OLD,
        _fp(resolved, "renamed_fn"): _VEC_NEW,
    })
    cov = preservation_coverage(base, side, resolved, "python", embedder=emb)
    assert cov is not None
    assert cov.added == 1
    assert cov.dropped == []  # rename preserved
    assert cov.ratio == 1.0

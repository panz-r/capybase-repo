"""Tests for HybridRetriever (BM25 + dense fusion).

Exercises the fusion semantics with controlled stores + fake embedding clients:
- RRF combines a lexical-only hit with an embedding-only hit (both surface).
- Embedding failure degrades to lexical ranking (never raises).
- DBSF uses score-magnitude normalization (distinct from RRF's rank-only).
- retrieve() (no scores) matches retrieve_scored() ordering.
- Fused scores flow through the context builder unchanged.
"""

from __future__ import annotations

import pytest

from capybase.conflict_model import HistoricalExample
from capybase.context_builder import ContextBuilder
from capybase.memory.retriever import (
    EmbeddingRetriever,
    HybridRetriever,
    LexicalRetriever,
)
from capybase.memory.store import Experience, ExperienceStore


def _exp(base, current, replayed, resolved, *, language="python"):
    return Experience(
        example=HistoricalExample(
            summary="s", base=base, current=current, replayed=replayed, resolved=resolved
        ),
        outcome="accepted",
        language=language,
    )


class _ConstEmbClient:
    """Every text embeds to a constant vector → cosine 1.0 for everything.

    So the embedding retriever surfaces EVERY accepted example (cosine clears the
    default 0.35 floor), independent of token overlap. Used to give the embedding
    retriever a deterministic, full-corpus contribution.
    """

    def __init__(self, vec=None):
        self._vec = vec or [0.4, 0.4]

    def embed(self, texts):
        if isinstance(texts, str):
            texts = [texts]
        return [list(self._vec) for _ in texts]


class _EmptyEmbClient:
    """Embedding endpoint always fails → embedding retriever returns []."""

    def embed(self, texts):
        raise RuntimeError("endpoint down")


class _OrthogonalEmbClient:
    """Corpus signatures embed to [1,0]; a bare query embeds to [0,1].

    So cosine between query and corpus is 0.0 → the embedding retriever returns
    [] (nothing clears the floor), isolating the lexical contribution.
    """

    def embed(self, texts):
        if isinstance(texts, str):
            texts = [texts]
        return [[1.0, 0.0] if "=" in t else [0.0, 1.0] for t in texts]


# ---------------------------------------------------------------------------
# RRF — combines complementary retrievers
# ---------------------------------------------------------------------------


def test_rrf_surfaces_results_from_both_retrievers(tmp_path):
    """RRF merges the two rankings: a result only lexical noticed still surfaces,
    and one only embedding noticed still surfaces — neither dominates."""
    store = ExperienceStore(tmp_path / "exp.jsonl")
    # Two accepted examples; both embed identically (so embedding "matches" both),
    # but only the first shares tokens with a typical query.
    store.append(_exp("alpha config", "alpha config two", "alpha config", "alpha config"))
    store.append(_exp("beta settings", "beta settings two", "beta settings", "beta settings"))
    lex = LexicalRetriever(store)
    emb = EmbeddingRetriever(store, _ConstEmbClient())
    hyb = HybridRetriever(lex, emb, fusion="rrf")

    scored = hyb.retrieve_scored("alpha config", k=5)
    # Both examples surface (each retriever contributed).
    assert len(scored) == 2
    # The alpha example (lexical rank 1 + embedding contribution) wins over beta.
    assert scored[0][1].base == "alpha config"


def test_rrf_embedding_failure_degrades_to_lexical(tmp_path):
    """When the embedding endpoint fails, the hybrid falls back to lexical-only
    RRF (one term) — identical ordering to BM25, never raises."""
    store = ExperienceStore(tmp_path / "exp.jsonl")
    store.append(_exp("shared token", "shared token two", "shared token", "shared token"))
    store.append(_exp("zzz unrelated", "zzz unrelated two", "zzz unrelated", "zzz unrelated"))
    lex = LexicalRetriever(store)
    emb = EmbeddingRetriever(store, _EmptyEmbClient())
    hyb = HybridRetriever(lex, emb, fusion="rrf")

    scored = hyb.retrieve_scored("shared token", k=5)
    # Only the lexical match surfaces; it's the top (and only) result.
    assert len(scored) == 1
    assert scored[0][1].base == "shared token"


def test_rrf_lexical_failure_degrades_to_embedding(tmp_path):
    """Symmetric: a lexical miss (no token overlap) + embedding hit → embedding
    result surfaces alone."""
    store = ExperienceStore(tmp_path / "exp.jsonl")
    store.append(_exp("compute value", "compute value two", "compute value", "compute value"))
    lex = LexicalRetriever(store)
    # Embedding matches everything (cosine 1.0); query has zero shared tokens.
    emb = EmbeddingRetriever(store, _ConstEmbClient())
    hyb = HybridRetriever(lex, emb, fusion="rrf")

    scored = hyb.retrieve_scored("totally different words", k=5)
    # Lexical returns [] (no overlap); embedding surfaces the example.
    assert len(scored) == 1
    assert scored[0][1].base == "compute value"


def test_rrf_score_is_summed_reciprocal_rank(tmp_path):
    """An example both retrievers rank #1 scores higher than one only one ranks
    #1 — RRF rewards consensus."""
    store = ExperienceStore(tmp_path / "exp.jsonl")
    store.append(_exp("both match", "both match two", "both match", "both match"))
    store.append(_exp("partial only", "partial only two", "partial only", "partial only"))
    lex = LexicalRetriever(store)
    emb = EmbeddingRetriever(store, _ConstEmbClient())
    hyb = HybridRetriever(lex, emb, fusion="rrf")

    scored = hyb.retrieve_scored("both match", k=5)
    by_base = {ex.base: s for s, ex in scored}
    # "both match" is lexical rank 1 AND embedding rank 1 (consensus) → highest.
    assert max(by_base.values()) == by_base["both match"]


# ---------------------------------------------------------------------------
# DBSF — score-magnitude normalization
# ---------------------------------------------------------------------------


def test_dbsf_normalizes_disparate_scales(tmp_path):
    """DBSF robustly normalizes each retriever's scores to [0,1] (median+MAD
    z-score) before summing. A corpus where BM25 scores vary but embeddings are
    constant still yields a usable fused ranking (no NaN, all in range)."""
    store = ExperienceStore(tmp_path / "exp.jsonl")
    store.append(_exp("match strong", "match strong two", "match strong", "match strong"))
    store.append(_exp("match weak", "match weak two", "match weak", "match weak"))
    lex = LexicalRetriever(store)
    emb = EmbeddingRetriever(store, _ConstEmbClient())
    hyb = HybridRetriever(lex, emb, fusion="dbsf")

    scored = hyb.retrieve_scored("match strong", k=5)
    assert len(scored) >= 1
    for s, _ in scored:
        assert isinstance(s, float) and s >= 0.0


def test_dbsf_robust_to_a_single_extreme_score(tmp_path):
    """Median+MAD normalization (50% breakdown): one extreme BM25 outlier among
    several results does NOT skew the whole fusion. Under min-max it would have
    crushed every other normalized score toward 0; under robust z-score the
    outlier is clipped at +3σ and the others keep their relative spacing.

    We assert the unit property directly: _dbsf_scores maps a clear outlier to
    the clip ceiling while preserving meaningful spread among the rest.
    """
    from capybase.memory.retriever import _dbsf_scores, _example_key

    # Four "normal" results clustered around 1.0–2.0, plus one extreme at 1000.0.
    from capybase.conflict_model import HistoricalExample

    def _ex(base):
        return HistoricalExample(
            summary="s", base=base, current="c", replayed="r", resolved="x"
        )

    ranked = [
        (1.0, _ex("a")), (1.5, _ex("b")), (2.0, _ex("c")), (1.2, _ex("d")),
        (1000.0, _ex("outlier")),
    ]
    norm = _dbsf_scores(ranked)
    # The outlier clips to the ceiling (the top of the [0,1] band).
    assert norm[_example_key(_ex("outlier"))] == pytest.approx(1.0)
    # The clustered results are NOT all crushed to ~0 (as min-max would): they
    # retain distinct, mid-band normalized values.
    clustered = [norm[_example_key(_ex(b))] for b in ("a", "b", "c", "d")]
    assert all(0.0 < v < 1.0 for v in clustered)
    assert len(set(round(v, 3) for v in clustered)) >= 2  # not all identical


def test_dbsf_equal_scores_get_neutral_weight(tmp_path):
    """MAD=0 (all scores equal) → every result gets the same neutral weight, not
    a division-by-zero (the documented degeneracy path)."""
    from capybase.memory.retriever import _dbsf_scores, _example_key
    from capybase.conflict_model import HistoricalExample

    def _ex(base):
        return HistoricalExample(summary="s", base=base, current="c", replayed="r", resolved="x")

    ranked = [(5.0, _ex("a")), (5.0, _ex("b")), (5.0, _ex("c"))]
    norm = _dbsf_scores(ranked)
    assert all(v == 1.0 for v in norm.values())


def test_unknown_fusion_falls_back_to_rrf(tmp_path):
    """An unrecognized fusion method degrades to RRF (the scale-agnostic default)."""
    store = ExperienceStore(tmp_path / "exp.jsonl")
    store.append(_exp("x token", "x token two", "x token", "x token"))
    lex = LexicalRetriever(store)
    emb = EmbeddingRetriever(store, _ConstEmbClient())
    hyb = HybridRetriever(lex, emb, fusion="bogus")
    assert hyb.fusion == "rrf"
    # Still produces results.
    assert len(hyb.retrieve_scored("x token", k=5)) == 1


# ---------------------------------------------------------------------------
# retrieve() parity + refresh
# ---------------------------------------------------------------------------


def test_retrieve_drops_scores_matching_order(tmp_path):
    """retrieve() delegates to retrieve_scored() and preserves ordering."""
    store = ExperienceStore(tmp_path / "exp.jsonl")
    store.append(_exp("aaa token", "aaa token two", "aaa token", "aaa token"))
    store.append(_exp("bbb token", "bbb token two", "bbb token", "bbb token"))
    lex = LexicalRetriever(store)
    emb = EmbeddingRetriever(store, _ConstEmbClient())
    hyb = HybridRetriever(lex, emb)

    scored = hyb.retrieve_scored("aaa token", k=5)
    retrieved = hyb.retrieve("aaa token", k=5)
    assert [ex for _, ex in scored] == retrieved


def test_refresh_propagates_to_both_subretrievers(tmp_path):
    """refresh() rebuilds both indexes so newly-appended examples surface."""
    store = ExperienceStore(tmp_path / "exp.jsonl")
    store.append(_exp("initial token", "initial token two", "initial token", "initial token"))
    lex = LexicalRetriever(store)
    emb = EmbeddingRetriever(store, _ConstEmbClient())
    hyb = HybridRetriever(lex, emb)

    # Append after construction; without refresh, the lexical index is stale.
    store.append(_exp("new token", "new token two", "new token", "new token"))
    hyb.refresh()
    scored = hyb.retrieve_scored("new token", k=5)
    assert any(ex.base == "new token" for _, ex in scored)


# ---------------------------------------------------------------------------
# Integration: fused scores flow through the context builder unchanged
# ---------------------------------------------------------------------------


def test_hybrid_scores_flow_into_context_bundle(tmp_path):
    """The fused scores journal into ContextBundle.retrieval_scores, parallel to
    the retrieved examples — same contract as a single retriever."""
    from capybase.conflict_model import ConflictSide, ConflictUnit

    store = ExperienceStore(tmp_path / "exp.jsonl")
    store.append(_exp("greet fn", "greet fn two", "greet fn", "greet fn"))
    store.append(_exp("farewell fn", "farewell fn two", "farewell fn", "farewell fn"))
    lex = LexicalRetriever(store)
    emb = EmbeddingRetriever(store, _ConstEmbClient())
    hyb = HybridRetriever(lex, emb)

    cb = ContextBuilder(context_lines=5, retriever=hyb, retriever_k=2, min_examples=0)
    unit = ConflictUnit(
        session_id="t", step_index=0, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="greet fn"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="greet fn two"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="greet fn"),
        original_worktree_text="greet fn\n<<<<<<< H\ngreet fn two\n=======\ngreet fn\n>>>>>>> b\n",
        marker_span=(1, 5),
    )
    ctx = cb.build(unit)
    assert len(ctx.retrieval_scores) == len(ctx.retrieved_examples)
    assert all(isinstance(s, float) for s in ctx.retrieval_scores)
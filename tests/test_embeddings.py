"""Tests for embedding-based RAG: the embeddings client, capability detection,
and the EmbeddingRetriever (cosine ranking + graceful fallback).

All via a fake embeddings client — no network. The retriever is tested behind
the same Protocol as LexicalRetriever, with controllable vectors so cosine
similarity is deterministic.
"""

from __future__ import annotations

import pytest

from capybase.conflict_model import HistoricalExample
from capybase.memory.embeddings import (
    EmbeddingsNotSupportedError,
    probe_embeddings_support,
)
from capybase.memory.retriever import EmbeddingRetriever, _cosine
from capybase.memory.store import Experience, ExperienceStore


# ---------------------------------------------------------------------------
# cosine helper
# ---------------------------------------------------------------------------


def test_cosine_identical_vectors_is_one():
    assert _cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_is_zero():
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_zero_vector_is_zero():
    assert _cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_opposite_is_minus_one():
    assert _cosine([1.0, 1.0], [-1.0, -1.0]) == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# capability detection
# ---------------------------------------------------------------------------


class FakeEmbeddingsClient:
    """Returns deterministic vectors. Optionally simulates unsupported/failure."""

    def __init__(self, *, supported: bool = True, fail: bool = False,
                 vector_for=None):
        self.supported = supported
        self.fail = fail
        self._vector_for = vector_for or (lambda _t: [1.0, 0.0, 0.0])
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        if self.fail:
            raise RuntimeError("embeddings endpoint exploded")
        if not self.supported:
            raise EmbeddingsNotSupportedError("not supported")
        inputs = [texts] if isinstance(texts, str) else list(texts)
        return [list(self._vector_for(t)) for t in inputs]


def test_probe_embeddings_support_true_for_working_endpoint():
    client = FakeEmbeddingsClient(supported=True)
    assert probe_embeddings_support(client) is True


def test_probe_embeddings_support_false_when_unsupported():
    client = FakeEmbeddingsClient(supported=False)
    assert probe_embeddings_support(client) is False


def test_probe_embeddings_support_false_on_failure():
    client = FakeEmbeddingsClient(fail=True)
    assert probe_embeddings_support(client) is False


def test_probe_embeddings_support_false_on_empty_vector():
    client = FakeEmbeddingsClient(vector_for=lambda _t: [])
    assert probe_embeddings_support(client) is False


# ---------------------------------------------------------------------------
# EmbeddingRetriever — cosine ranking over the corpus
# ---------------------------------------------------------------------------


def _example(base: str, cur: str, rep: str, resolved: str = "OK") -> HistoricalExample:
    return HistoricalExample(summary="s", base=base, current=cur,
                             replayed=rep, resolved=resolved)


def _store_with(*examples: HistoricalExample, tmp_path) -> ExperienceStore:
    store = ExperienceStore(tmp_path / "exp.jsonl")
    for ex in examples:
        store.append(Experience(example=ex, outcome="accepted", language="python"))
    return store


def test_embedding_retriever_ranks_most_similar_first(tmp_path):
    # Three examples; query is semantically closest to the second.
    store = _store_with(
        _example("color = red", "color = blue", "color = red", "color = blue"),
        _example("shape = square", "shape = circle", "shape = square", "shape = circle"),
        _example("size = 1", "size = 2", "size = 1", "size = 2"),
        tmp_path=tmp_path,
    )
    # Map each example's signature to a distinct basis vector; query matches #2.
    basis = {0: [1.0, 0, 0], 1: [0, 1.0, 0], 2: [0, 0, 1.0]}
    def vec_for(text):
        for i, b in enumerate(basis.values()):
            # The build embeds signatures; the query embeds the new conflict text.
            return b if any(needle in text for needle in ("color", "shape", "size")) else [0, 0, 0]
    # Simpler: drive similarity by which keyword appears.
    def vec_kw(text):
        if "shape" in text:
            return [0, 1.0, 0]
        if "color" in text:
            return [1.0, 0, 0]
        if "size" in text:
            return [0, 0, 1.0]
        return [0, 0, 0]
    client = FakeEmbeddingsClient(vector_for=vec_kw)
    r = EmbeddingRetriever(store, client)
    results = r.retrieve("shape = x", k=3)
    assert results, "expected at least one match above the floor"
    # The shape example must rank first.
    assert "shape" in results[0].resolved


def test_embedding_retriever_filters_below_similarity_floor(tmp_path):
    store = _store_with(
        _example("aaa", "aaa", "aaa", "resolved-aaa"),
        tmp_path=tmp_path,
    )
    # Query vector orthogonal to the example vector → below floor → no results.
    client = FakeEmbeddingsClient(vector_for=lambda t: [1.0, 0.0] if "aaa" in t else [0.0, 1.0])
    r = EmbeddingRetriever(store, client)
    # Query "zzz" embeds to [0,1], orthogonal to the corpus [1,0] → cosine 0.
    assert r.retrieve("zzz", k=3) == []


def test_embedding_retriever_empty_corpus_returns_empty(tmp_path):
    store = _store_with(tmp_path=tmp_path)  # no examples
    client = FakeEmbeddingsClient()
    r = EmbeddingRetriever(store, client)
    assert r.retrieve("anything", k=3) == []


def test_embedding_retriever_falls_back_on_embed_failure(tmp_path):
    """If embedding fails at query time, retrieve returns [] (no few-shot),
    exactly as when the corpus is too small — never raises."""
    store = _store_with(_example("a", "b", "c"), tmp_path=tmp_path)
    # Build succeeds (supported), but the query embed fails.
    client = FakeEmbeddingsClient(supported=True)
    r = EmbeddingRetriever(store, client)
    r._build()  # populate cache
    # Now make query-time embed fail.
    client.fail = True
    assert r.retrieve("query", k=3) == []


def test_embedding_retriever_falls_back_when_build_embed_fails(tmp_path):
    """If the corpus embedding fails entirely, retrieve returns [] gracefully."""
    store = _store_with(_example("a", "b", "c"), tmp_path=tmp_path)
    client = FakeEmbeddingsClient(fail=True)
    r = EmbeddingRetriever(store, client)
    assert r.retrieve("query", k=3) == []


def test_embedding_retriever_language_filter(tmp_path):
    store = _store_with(_example("a", "b", "c"), tmp_path=tmp_path)
    client = FakeEmbeddingsClient(vector_for=lambda t: [1.0, 0.0])
    r = EmbeddingRetriever(store, client)
    r._build()
    # The stored experience is language="python"; asking for rust excludes it.
    assert r.retrieve("query", k=3, language="rust") == []
    assert r.retrieve("query", k=3, language="python") != []


def test_embedding_retriever_refresh_rebuilds_cache(tmp_path):
    store = _store_with(_example("a", "b", "c"), tmp_path=tmp_path)
    client = FakeEmbeddingsClient()
    r = EmbeddingRetriever(store, client)
    r._build()
    assert r._vectors is not None
    r.refresh()
    assert r._accepted is None and r._vectors is None


# ---------------------------------------------------------------------------
# probe_embeddings (calibrate integration)
# ---------------------------------------------------------------------------


def test_probe_embeddings_reports_unsupported_for_501(monkeypatch):
    """When the endpoint returns 501, probe_embeddings reports not-supported."""
    from capybase.config import ModelConfig
    from capybase.probes import probe_embeddings

    def fake_init(self, config, **kw):
        self.config = config
    monkeypatch.setattr(
        "capybase.memory.embeddings.OpenAIEmbeddingsClient.__init__", fake_init
    )
    monkeypatch.setattr(
        "capybase.memory.embeddings.OpenAIEmbeddingsClient.embed",
        lambda self, t: (_ for _ in ()).throw(EmbeddingsNotSupportedError("501")),
    )
    cfg = ModelConfig(base_url="http://x/v1", model="m")
    result = probe_embeddings(cfg)
    assert result.ok is False
    assert "embeddings" in result.detail.lower()


def test_probe_embeddings_reports_supported_when_working(monkeypatch):
    from capybase.config import ModelConfig
    from capybase.probes import probe_embeddings

    monkeypatch.setattr(
        "capybase.memory.embeddings.OpenAIEmbeddingsClient.__init__",
        lambda self, config, **kw: setattr(self, "config", config) or None,
    )
    monkeypatch.setattr(
        "capybase.memory.embeddings.OpenAIEmbeddingsClient.embed",
        lambda self, t: [[0.1, 0.2, 0.3]],
    )
    cfg = ModelConfig(base_url="http://x/v1", model="m")
    result = probe_embeddings(cfg)
    assert result.ok is True

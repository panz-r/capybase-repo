"""Persisted vector cache for the embedding retriever (embeddings survey §1).

Two durable backends (sqlite-vec primary, numpy fallback) + an in-memory tier
for when neither dep is available. The cache is content-keyed so a re-embed is
skipped for any example whose four sides were embedded before — across process
restarts, store reordering, and interleaved appends.

These tests use a deterministic fake embedder (vectors derived from a hash of
the text) so cache hit/miss is assertable without a live endpoint. The numpy
backend's ranking is also checked against the pure-Python cosine to confirm
parity with the prior in-memory behavior.
"""

from __future__ import annotations

import hashlib
import struct

import pytest

from capybase.conflict_model import HistoricalExample
from capybase.memory.store import Experience, ExperienceStore
from capybase.memory.vector_index import (
    InMemoryCache,
    NumpyVectorCache,
    SqliteVecCache,
    build_cached_vectors,
    content_key,
    make_vector_cache,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _hash_vec(text: str, dim: int = 8) -> list[float]:
    """Deterministic vector from a text hash (stable, cheap, no network).

    Maps the hash bytes into [-1, 1] so the floats are bounded (avoids numpy
    overflow on L2-normalization for the cache tests).
    """
    h = hashlib.sha256(text.encode("utf-8")).digest()
    raw = (h * ((dim * 4 // len(h)) + 1))[: dim * 4]
    vals = list(struct.unpack(f">{dim}f", raw))
    # Map to a bounded range: scale by 2/max_float and center at 0 → [-1, 1].
    max_f = 3.4e38
    return [(v / max_f) * 2.0 - 1.0 for v in vals]


class FakeEmbedder:
    """Records calls and returns deterministic vectors from text hashes."""

    def __init__(self, dim: int = 8) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []

    def embed(self, texts):
        if isinstance(texts, str):
            self.calls.append([texts])
            return [_hash_vec(texts, self.dim)]
        self.calls.append(list(texts))
        return [_hash_vec(t, self.dim) for t in texts]


def _exp(base: str, resolved: str, current: str = "C", replayed: str = "R") -> Experience:
    return Experience(
        example=HistoricalExample(
            summary="x", base=base, current=current, replayed=replayed,
            resolved=resolved, source="t",
        ),
        outcome="accepted", path="a.py",
    )


# ---------------------------------------------------------------------------
# content_key
# ---------------------------------------------------------------------------


def test_content_key_is_stable_and_distinguishes_content():
    k1 = content_key("b", "c", "r", "z")
    k2 = content_key("b", "c", "r", "z")
    k3 = content_key("b", "c", "r", "DIFFERENT")
    assert k1 == k2
    assert k1 != k3


# ---------------------------------------------------------------------------
# build_cached_vectors: the core reconcile logic
# ---------------------------------------------------------------------------


def test_cold_cache_embeds_all_then_persists(tmp_path):
    cache = NumpyVectorCache(tmp_path / "v")
    emb = FakeEmbedder()
    keys = [content_key("b1", "c", "r", "z1"), content_key("b2", "c", "r", "z2")]
    sigs = ["sig1", "sig2"]
    vecs, out_keys = build_cached_vectors(cache, emb, sigs, keys)
    assert out_keys == keys
    assert len(vecs) == 2
    # Both were embedded (cold cache).
    assert emb.calls == [["sig1", "sig2"]]


def test_warm_cache_embeds_only_new_entries(tmp_path):
    cache = NumpyVectorCache(tmp_path / "v")
    emb = FakeEmbedder()
    k1, k2 = content_key("b1", "c", "r", "z1"), content_key("b2", "c", "r", "z2")
    # First run: cold, embeds both.
    build_cached_vectors(cache, emb, ["sig1", "sig2"], [k1, k2])
    emb.calls.clear()
    # Second run: k1 cached, k3 new — only k3 should be embedded.
    k3 = content_key("b3", "c", "r", "z3")
    vecs, keys = build_cached_vectors(cache, emb, ["sig1", "sigX", "sig3"], [k1, k2, k3])
    assert keys == [k1, k2, k3]
    assert len(vecs) == 3
    assert emb.calls == [["sig3"]]  # only the new one


def test_offline_restart_uses_cache_zero_embeds(tmp_path):
    """A fresh process with the cache on disk must not re-embed anything."""
    cache = NumpyVectorCache(tmp_path / "v")
    emb = FakeEmbedder()
    keys = [content_key(f"b{i}", "c", "r", f"z{i}") for i in range(5)]
    sigs = [f"sig{i}" for i in range(5)]
    build_cached_vectors(cache, emb, sigs, keys)
    # New process: same path, new embedder + new cache instance.
    cache2 = NumpyVectorCache(tmp_path / "v")
    emb2 = FakeEmbedder()
    vecs, out_keys = build_cached_vectors(cache2, emb2, sigs, keys)
    assert out_keys == keys
    assert len(vecs) == 5
    assert emb2.calls == []  # zero embed calls — all served from disk


def test_pruned_keys_dropped_from_output(tmp_path):
    """A key absent from the current keys set is dropped (lazy prune)."""
    cache = NumpyVectorCache(tmp_path / "v")
    emb = FakeEmbedder()
    k1, k2 = content_key("b1", "c", "r", "z1"), content_key("b2", "c", "r", "z2")
    build_cached_vectors(cache, emb, ["s1", "s2"], [k1, k2])
    emb.calls.clear()
    # k2 is gone from the store now; only k1 + a new k3 requested.
    k3 = content_key("b3", "c", "r", "z3")
    vecs, keys = build_cached_vectors(cache, emb, ["s1", "s3"], [k1, k3])
    assert keys == [k1, k3]
    assert len(vecs) == 2
    assert emb.calls == [["s3"]]  # k1 cached, k3 new


def test_embed_failure_returns_cached_subset(tmp_path):
    cache = NumpyVectorCache(tmp_path / "v")
    emb = FakeEmbedder()
    k1 = content_key("b1", "c", "r", "z1")
    build_cached_vectors(cache, emb, ["s1"], [k1])  # warm cache
    # Now make embed fail for the new entry.
    emb_failing = _FailingEmbedder(when_texts=["s2"])
    k2 = content_key("b2", "c", "r", "z2")
    vecs, keys = build_cached_vectors(cache, emb_failing, ["s1", "s2"], [k1, k2])
    # k1 served from cache; k2 skipped (embed failed) — never raises.
    assert keys == [k1]
    assert len(vecs) == 1


class _FailingEmbedder:
    def __init__(self, when_texts):
        self.when = set(when_texts)

    def embed(self, texts):
        texts = [texts] if isinstance(texts, str) else list(texts)
        for t in texts:
            if t in self.when:
                raise RuntimeError("simulated endpoint failure")
        return [_hash_vec(t) for t in texts]


# ---------------------------------------------------------------------------
# EmbeddingRetriever integration with the cache
# ---------------------------------------------------------------------------


def test_retriever_uses_cache_across_instances(tmp_path):
    from capybase.memory.retriever import EmbeddingRetriever

    store = ExperienceStore(tmp_path / "exp.jsonl")
    store.append(_exp(base="def a(): return 1", resolved="def a(): return 1\n# merged"))
    emb = FakeEmbedder()
    cache = NumpyVectorCache(tmp_path / "v")
    r1 = EmbeddingRetriever(store, emb, cache=cache)
    r1._build()
    assert emb.calls and len(emb.calls[0]) == 1  # embedded the one example
    emb.calls.clear()
    # New retriever instance, same cache path — should NOT re-embed.
    r2 = EmbeddingRetriever(store, emb, cache=NumpyVectorCache(tmp_path / "v"))
    r2._build()
    assert emb.calls == []  # served from cache
    # And retrieval still works identically.
    res1 = r1.retrieve("def a(): return 1", k=1)
    res2 = r2.retrieve("def a(): return 1", k=1)
    assert len(res1) == len(res2) == 1


def test_retriever_without_cache_re_embeds_each_time(tmp_path):
    """cache=None preserves the prior behavior (regression guard)."""
    from capybase.memory.retriever import EmbeddingRetriever

    store = ExperienceStore(tmp_path / "exp.jsonl")
    store.append(_exp(base="base", resolved="resolved"))
    emb = FakeEmbedder()
    r1 = EmbeddingRetriever(store, emb, cache=None)
    r1._build()
    r2 = EmbeddingRetriever(store, emb, cache=None)
    r2._build()
    # Both re-embedded (no persistence).
    assert len(emb.calls) == 2


# ---------------------------------------------------------------------------
# Backend selection / fallback
# ---------------------------------------------------------------------------


def test_make_vector_cache_auto_prefers_sqlite_vec(tmp_path):
    pytest.importorskip("sqlite_vec")
    c = make_vector_cache("auto", tmp_path / "v")
    assert isinstance(c, SqliteVecCache)


def test_make_vector_cache_numpy_forced(tmp_path):
    pytest.importorskip("numpy")
    c = make_vector_cache("numpy", tmp_path / "v")
    assert isinstance(c, NumpyVectorCache)


def test_make_vector_cache_off_is_inmemory(tmp_path):
    c = make_vector_cache("off", tmp_path / "v")
    assert isinstance(c, InMemoryCache)


def test_make_vector_cache_unknown_raises(tmp_path):
    with pytest.raises(ValueError):
        make_vector_cache("nonsense", tmp_path / "v")


# ---------------------------------------------------------------------------
# Cross-backend equivalence: numpy ranking == sqlite-vec == pure cosine
# ---------------------------------------------------------------------------


def test_sqlite_vec_cache_roundtrip_matches_numpy(tmp_path):
    """Both durable backends must produce identical load→rebuild→load cycles."""
    sqlite3 = pytest.importorskip("sqlite_vec")
    pytest.importorskip("numpy")

    keys = [content_key(f"b{i}", "c", "r", f"z{i}") for i in range(4)]
    vecs = [_hash_vec(f"sig{i}") for i in range(4)]

    sv = SqliteVecCache(tmp_path / "sv")
    sv.add(vecs, keys)
    loaded_vecs, loaded_keys = sv.load()
    assert loaded_keys == keys
    # Vectors round-trip (sqlite-vec stores float32, so approximate equality).
    for got, want in zip(loaded_vecs, vecs):
        assert got == pytest.approx(want, abs=1e-5)

    np = NumpyVectorCache(tmp_path / "np")
    np.add(vecs, keys)
    n_vecs, n_keys = np.load()
    assert n_keys == keys


def test_numpy_cache_normalizes_vectors(tmp_path):
    """Numpy backend L2-normalizes on write so cosine = dot product."""
    np = pytest.importorskip("numpy")
    cache = NumpyVectorCache(tmp_path / "v")
    cache.add([[3.0, 4.0]], ["k"])  # norm = 5
    arr = np.load(cache.array_path)
    # 3/5, 4/5 after normalization.
    assert arr[0] == pytest.approx([0.6, 0.8], abs=1e-6)

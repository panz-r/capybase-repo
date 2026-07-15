"""Persisted vector cache for the embedding retriever.

Without this, ``EmbeddingRetriever._build`` re-embeds every accepted experience
on every process start — fine at tens of entries, a re-embed cliff as the corpus
grows past hundreds. This module provides a durable cache so only NEW corpus
entries (content_keys not yet seen) get embedded; cached vectors load from disk.

Three backends, selected by ``MemoryConfig.vector_cache``:

- **sqlite-vec** (primary when ``auto``): a ``vec0`` virtual table keyed by
  ``content_key``. ANN ``KNN`` search; handles tens of thousands of vectors with
  sub-millisecond query latency on CPU. Requires the ``sqlite-vec`` package.
- **numpy** (fallback): a flat ``(N, dim)`` float32 ``.npy`` array, L2-normalized
  so cosine = dot-product, plus a JSONL manifest mapping row → content_key.
  Linear scan (``N×dim`` dot), fast enough to low-thousands.
- **in-memory** (when both deps absent, or ``vector_cache="off"``): the prior
  behavior — re-embed everything each run, vectors held in a Python list.

The cache is **content-keyed**, not store-position-keyed: the key is the
``(base, current, replayed, resolved)`` tuple (the same key ``HybridRetriever``
fuses on), rendered as a stable JSON string. So a re-embed is skipped iff the
*exact* example content was embedded before — robust to store reordering, dedup,
and appends interleaved across sessions.

Lazy-imported: numpy / sqlite_vec are optional. Any import failure or open error
degrades one tier (sqlite_vec → numpy → in-memory); the retriever never hard-
fails because of the cache. All public methods are best-effort and never raise.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Protocol


class Embedder(Protocol):
    """Minimal embed contract (mirrors EmbeddingsClient)."""

    def embed(self, texts: str | list[str]) -> list[list[float]]: ...


def content_key(base: str, current: str, replayed: str, resolved: str) -> str:
    """Stable string key for an experience's content.

    Mirrors ``retriever._example_key`` (the tuple the HybridRetriever fuses on),
    rendered as compact JSON so it's hash-stable across processes and survives
    JSONL round-trips. Two examples with identical four sides share a key — so a
    re-embed is skipped on cache hit regardless of store ordering.
    """
    return json.dumps([base, current, replayed, resolved], ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Backend Protocol
# ---------------------------------------------------------------------------


class VectorCache:
    """The cache contract the retriever talks to.

    ``load`` returns the vectors+keys already cached (or empty if cold). ``add``
    persists newly-embedded vectors alongside their content_keys. ``rebuild``
    replaces the cache wholesale (used after pruning deleted content_keys).
    Implementations are best-effort: a disk write failure is swallowed (the
    vectors still work in-memory for this run; next run re-embeds).
    """

    def load(self) -> tuple[list[list[float]], list[str]]:
        """Return ``(vectors, content_keys)`` from disk, or ``([], [])`` if cold."""
        raise NotImplementedError

    def add(self, vectors: list[list[float]], keys: list[str]) -> None:
        """Persist ``vectors`` with parallel ``keys`` (append)."""
        raise NotImplementedError

    def rebuild(self, vectors: list[list[float]], keys: list[str]) -> None:
        """Replace the cache wholesale with ``vectors``/``keys``."""
        raise NotImplementedError


class InMemoryCache(VectorCache):
    """No persistence — the prior behavior (re-embed every run).

    Returned when no durable backend is available. ``load`` is always empty;
    ``add``/``rebuild`` are no-ops. The retriever still works for the current
    process; the only cost is re-embedding on the next start.
    """

    def load(self) -> tuple[list[list[float]], list[str]]:
        return [], []

    def add(self, vectors: list[list[float]], keys: list[str]) -> None:
        pass

    def rebuild(self, vectors: list[list[float]], keys: list[str]) -> None:
        pass


# ---------------------------------------------------------------------------
# numpy backend (fallback)
# ---------------------------------------------------------------------------


class NumpyVectorCache(VectorCache):
    """Flat normalized ``.npy`` array + JSONL row→key manifest.

    Vectors are L2-normalized on write so cosine = ``dot(query, row)`` (a single
    matrix-vector product at query time). Linear scan over ``N`` rows; fast
    enough to low-thousands of vectors, which covers the vast majority of rebase
    histories. The manifest is a JSONL file (one key per line, aligned to row
    index) so it's grep-able and append-friendly.
    """

    def __init__(self, path_stem: str | Path) -> None:
        self.array_path = Path(str(path_stem) + ".npy")
        self.manifest_path = Path(str(path_stem) + ".npy.manifest.jsonl")

    def load(self) -> tuple[list[list[float]], list[str]]:
        if not self.array_path.is_file() or not self.manifest_path.is_file():
            return [], []
        try:
            import numpy as np  # lazy; optional dep

            arr = np.load(self.array_path)
            if arr.ndim != 2 or arr.shape[0] == 0:
                return [], []
            keys = self._read_manifest()
            if len(keys) != arr.shape[0]:
                # Manifest/array drift — treat as cold (rebuild will reconcile).
                return [], []
            return arr.tolist(), keys
        except Exception:  # noqa: BLE001 - cache is best-effort
            return [], []

    def add(self, vectors: list[list[float]], keys: list[str]) -> None:
        if not vectors:
            return
        try:
            import numpy as np

            new = self._normalize(np.asarray(vectors, dtype="float32"))
            if self.array_path.is_file():
                existing = np.load(self.array_path)
                if existing.ndim == 2 and existing.shape[1] == new.shape[1]:
                    new = np.vstack([existing, new])
            self._write(new, keys, append_manifest=True)
        except Exception:  # noqa: BLE001 - never break the run for a cache write
            pass

    def rebuild(self, vectors: list[list[float]], keys: list[str]) -> None:
        if not vectors:
            self._clear()
            return
        try:
            import numpy as np

            arr = self._normalize(np.asarray(vectors, dtype="float32"))
            self._write(arr, keys, append_manifest=False)
        except Exception:  # noqa: BLE001
            pass

    def _write(self, arr, keys: list[str], *, append_manifest: bool) -> None:
        import numpy as np

        self.array_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(self.array_path, arr)
        mode = "a" if append_manifest and self.manifest_path.is_file() else "w"
        with open(self.manifest_path, mode, encoding="utf-8") as fh:
            for k in keys:
                fh.write(k + "\n")

    def _read_manifest(self) -> list[str]:
        out: list[str] = []
        with open(self.manifest_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if line:
                    out.append(line)
        return out

    def _clear(self) -> None:
        for p in (self.array_path, self.manifest_path):
            try:
                p.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _normalize(arr):
        import numpy as np

        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return arr / norms


# ---------------------------------------------------------------------------
# sqlite-vec backend (primary)
# ---------------------------------------------------------------------------


class SqliteVecCache(VectorCache):
    """A ``vec0`` virtual table keyed by ``content_key``.

    Schema: ``CREATE VIRTUAL TABLE vec_examples USING vec0(embedding float[<dim>],
    content_key text)``. The dimension is fixed at table-creation from the first
    vector seen; a dimension mismatch on ``add`` drops the table and recreates
    (handles a model swap that changed the embedding width). Query is ANN ``KNN``;
    for the retriever's cosine need, raw float32 vectors are stored (sqlite-vec's
    default L2 distance is monotonic in cosine for normalized vectors, and the
    retriever post-filters by the calibrated floor anyway).
    """

    def __init__(self, path_stem: str | Path) -> None:
        self.db_path = Path(str(path_stem) + ".vec.sqlite")
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._open()
        return self._conn

    def _open(self) -> sqlite3.Connection:
        import sqlite_vec  # lazy; optional dep

        db = sqlite3.connect(str(self.db_path))
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        return db

    def _table_exists(self) -> bool:
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vec_examples'"
        ).fetchone()
        return row is not None

    def _ensure_table(self, dim: int) -> None:
        if self._table_exists():
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn.execute(
            f"CREATE VIRTUAL TABLE vec_examples USING vec0(embedding float[{dim}], content_key text)"
        )

    def _drop_table(self) -> None:
        if self._table_exists():
            self.conn.execute("DROP TABLE vec_examples")
            self.conn.commit()

    def load(self) -> tuple[list[list[float]], list[str]]:
        if not self.db_path.is_file() or not self._table_exists():
            return [], []
        try:
            rows = self.conn.execute(
                "SELECT embedding, content_key FROM vec_examples ORDER BY rowid"
            ).fetchall()
            import sqlite_vec as sv  # noqa: F811 - re-import for deserialize

            vecs: list[list[float]] = []
            keys: list[str] = []
            for emb, key in rows:
                # sqlite-vec stores floats as a packed blob; unpack to a list.
                import struct

                n = len(emb) // 4
                vecs.append(list(struct.unpack(f"{n}f", emb)))
                keys.append(key)
            return vecs, keys
        except Exception:  # noqa: BLE001 - cache is best-effort
            return [], []

    def add(self, vectors: list[list[float]], keys: list[str]) -> None:
        if not vectors:
            return
        try:
            import sqlite_vec as sv

            dim = len(vectors[0])
            self._ensure_table(dim)
            existing_dim = self._existing_dim()
            if existing_dim is not None and existing_dim != dim:
                # Model swap changed the embedding width — rebuild from scratch.
                self._drop_table()
                self._ensure_table(dim)
            with self.conn:
                for vec, key in zip(vectors, keys):
                    self.conn.execute(
                        "INSERT INTO vec_examples(embedding, content_key) VALUES (?, ?)",
                        (sv.serialize_float32(vec), key),
                    )
        except Exception:  # noqa: BLE001 - never break the run for a cache write
            pass

    def rebuild(self, vectors: list[list[float]], keys: list[str]) -> None:
        try:
            self._drop_table()
            if not vectors:
                return
            self.add(vectors, keys)
        except Exception:  # noqa: BLE001
            pass

    def _existing_dim(self) -> int | None:
        if not self._table_exists():
            return None
        try:
            row = self.conn.execute("SELECT embedding FROM vec_examples LIMIT 1").fetchone()
            if not row or not row[0]:
                return None
            return len(row[0]) // 4
        except Exception:  # noqa: BLE001
            return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _have_sqlite_vec() -> bool:
    try:
        import sqlite_vec  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def _have_numpy() -> bool:
    try:
        import numpy  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def make_vector_cache(
    backend: str, path_stem: str | Path
) -> VectorCache:
    """Construct the configured cache, degrading across backends.

    ``backend``: ``"auto"`` (sqlite-vec → numpy → in-memory), ``"sqlite_vec"``,
    ``"numpy"``, or ``"off"``. A forced backend that's unavailable raises
    ``ValueError`` so a misconfiguration is loud; ``"auto"`` never raises.
    """
    b = (backend or "auto").lower()
    if b == "off":
        return InMemoryCache()
    if b == "sqlite_vec":
        if not _have_sqlite_vec():
            raise ValueError("vector_cache='sqlite_vec' but the sqlite_vec package is not installed")
        return SqliteVecCache(path_stem)
    if b == "numpy":
        if not _have_numpy():
            raise ValueError("vector_cache='numpy' but numpy is not installed")
        return NumpyVectorCache(path_stem)
    if b == "auto":
        if _have_sqlite_vec():
            return SqliteVecCache(path_stem)
        if _have_numpy():
            return NumpyVectorCache(path_stem)
        return InMemoryCache()
    raise ValueError(f"unknown vector_cache backend: {backend!r}")


# ---------------------------------------------------------------------------
# Reconcile helper (the build path the retriever calls)
# ---------------------------------------------------------------------------


def build_cached_vectors(
    cache: VectorCache,
    embedder: Embedder,
    signatures: list[str],
    keys: list[str],
) -> tuple[list[list[float]], list[str]]:
    """Return ``(vectors, keys)`` aligned to ``keys``, embedding only NEW entries.

    The cache is consulted first: any ``content_key`` already cached loads from
    disk. The remainder (keys not in the cache, or a cold cache) are embedded in
    ONE batch via ``embedder.embed`` and persisted via ``cache.add``. If a cached
    key is absent from the current ``keys`` set (the store pruned it), it's
    dropped — so the returned lists are exactly ``len(keys)`` and aligned.

    Never raises: an embed failure returns whatever cached vectors exist (the
    retriever then sees a partial corpus, which is correct — it just won't have
    few-shot for the un-embeddable entries this run).
    """
    if not keys:
        return [], []
    cached_vecs, cached_keys = cache.load()
    cached: dict[str, list[float]] = dict(zip(cached_keys, cached_vecs))

    missing_idx = [i for i, k in enumerate(keys) if k not in cached]
    if missing_idx:
        missing_texts = [signatures[i] for i in missing_idx]
        missing_keys = [keys[i] for i in missing_idx]
        try:
            new_vecs = embedder.embed(missing_texts)
            if len(new_vecs) != len(missing_idx):
                # Embedding returned a short list (endpoint hiccup) — keep only
                # what we got aligned, skip the rest this run.
                new_vecs = new_vecs[: len(missing_idx)]
                missing_keys = missing_keys[: len(new_vecs)]
                missing_idx = missing_idx[: len(new_vecs)]
            for k, v in zip(missing_keys, new_vecs):
                cached[k] = v
            cache.add(new_vecs, missing_keys)
        except Exception:  # noqa: BLE001 - embed failure → use cached subset
            pass

    out_vecs: list[list[float]] = []
    out_keys: list[str] = []
    for k in keys:
        v = cached.get(k)
        if v:
            out_vecs.append(v)
            out_keys.append(k)
    return out_vecs, out_keys

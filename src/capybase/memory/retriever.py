"""Retrieval over the experience store for RAG few-shot demonstrations.

Given a new conflict, the retriever finds the most similar past merges in the
experience corpus and returns them as ``HistoricalExample`` objects. These flow
into ``ContextBundle.retrieved_examples`` (the existing contract seam) and are
rendered into the prompt as dynamic few-shot — "here is how a similar conflict
was resolved before."

Two retrievers implement the same Protocol:

- ``LexicalRetriever`` (default): dependency-free BM25 over tokenized code. Splits
  identifiers (camelCase, snake_case), drops stopwords/punctuation, ranks by BM25.
- ``EmbeddingRetriever``: semantic retrieval via the llama-server ``/v1/embeddings``
  endpoint (survey §4.2, LLMinus pattern). Catches "same intent, different
  identifiers" that lexical matching misses. Used only when the endpoint supports
  embeddings (``capybase calibrate`` detects this); otherwise falls back to BM25.
- ``HybridRetriever``: fuses the two (survey §4) via RRF (default) or DBSF so BM25's
  exact-identifier strength and embeddings' semantic strength combine — degrading
  to lexical ranking when the embedding endpoint is unavailable.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Protocol

from capybase.conflict_model import HistoricalExample
from capybase.memory.store import Experience, ExperienceStore


class Retriever(Protocol):
    """Retrieve similar past merges for a new conflict."""

    def retrieve(
        self, query: str, *, k: int = 3, language: str | None = None
    ) -> list[HistoricalExample]: ...


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------

# Split camelCase / PascalCase / snake_case into lowercase terms.
_SPLIT_IDENT = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|[0-9]+")
_STOPWORDS = frozenset(
    {
        "def", "class", "return", "import", "from", "self", "cls", "the",
        "a", "an", "is", "are", "was", "were", "be", "been", "to", "of", "in",
        "on", "for", "with", "and", "or", "not", "if", "else", "elif", "while",
        "for", "try", "except", "finally", "with", "as", "pass", "break",
        "continue", "lambda", "yield", "global", "nonlocal", "assert", "del",
        "raise", "True", "False", "None", "fn", "let", "mut", "pub", "impl",
        "struct", "enum", "trait", "mod", "use", "match", "where", "ref",
        "move", "const", "static", "type", "async", "await", "box", "dyn",
    }
)


def tokenize(text: str) -> list[str]:
    """Tokenize source/text into lowercase terms for BM25.

    Splits camelCase and snake_case identifiers, drops stopwords, numbers, and
    single characters. Designed for code: ``getUserName`` → ``[get, user, name]``.
    """
    if not text:
        return []
    # First split on non-alphanumeric boundaries, then sub-split identifiers.
    raw_tokens: list[str] = []
    for chunk in re.split(r"[^A-Za-z0-9_]+", text):
        if not chunk:
            continue
        parts = _SPLIT_IDENT.findall(chunk)
        if parts:
            raw_tokens.extend(p.lower() for p in parts)
        elif chunk.isalpha():
            raw_tokens.append(chunk.lower())
    return [
        t
        for t in raw_tokens
        if len(t) > 1 and not t.isdigit() and t not in _STOPWORDS
    ]


# ---------------------------------------------------------------------------
# BM25
# ---------------------------------------------------------------------------


class _BM25Index:
    """A minimal in-memory BM25 index over a set of documents."""

    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.docs = docs
        self.n = len(docs)
        self.doc_len = [len(d) for d in docs]
        self.avgdl = sum(self.doc_len) / self.n if self.n else 0.0
        # Term frequency per doc.
        self.tf: list[Counter] = [Counter(d) for d in docs]
        # Document frequency per term.
        df: Counter = Counter()
        for c in self.tf:
            for term in c:
                df[term] += 1
        self.df = df
        # Inverse document frequency.
        self.idf = {
            term: math.log(1 + (self.n - dft + 0.5) / (dft + 0.5))
            for term, dft in df.items()
        }

    def score(self, query: list[str]) -> list[float]:
        """Return BM25 scores for each document against ``query``."""
        scores = [0.0] * self.n
        for i in range(self.n):
            tf_i = self.tf[i]
            dl = self.doc_len[i] or 1
            denom_norm = self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
            s = 0.0
            for term in query:
                if term not in tf_i:
                    continue
                idf = self.idf.get(term, 0.0)
                f = tf_i[term]
                s += idf * f * (self.k1 + 1) / (f + denom_norm)
            scores[i] = s
        return scores


# ---------------------------------------------------------------------------
# LexicalRetriever
# ---------------------------------------------------------------------------


class LexicalRetriever:
    """BM25 retrieval over accepted experiences in the store.

    Builds an index lazily from the store's accepted examples, tokenizing the
    concatenation of base/current/replayed (the conflict's "signature"). For a
    new conflict, tokenizes the query the same way and returns the top-k
    HistoricalExamples by BM25 score.
    """

    def __init__(self, store: ExperienceStore) -> None:
        self.store = store
        self._index: _BM25Index | None = None
        self._accepted: list[Experience] = []

    @property
    def _examples(self) -> list[HistoricalExample]:
        return [e.example for e in self._accepted]

    def _build(self) -> None:
        """Build the BM25 index from accepted experiences."""
        self._accepted = self.store.accepted()
        docs = [
            tokenize(
                " ".join([e.example.base, e.example.current, e.example.replayed])
            )
            for e in self._accepted
        ]
        self._index = _BM25Index(docs) if docs else _BM25Index([])

    def retrieve_scored(
        self, query: str, *, k: int = 3, language: str | None = None
    ) -> list[tuple[float, HistoricalExample]]:
        """Return ``(bm25_score, example)`` pairs for the top-k matches.

        Same ranking as :meth:`retrieve` but keeps the score, so the retrieval-
        score diagnostic can observe lexical-retrieval confidence too.
        """
        if self._index is None:
            self._build()
        assert self._index is not None
        if not self._accepted:
            return []
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        scores = self._index.score(q_tokens)
        ranked = sorted(
            (
                (s, exp)
                for s, exp in zip(scores, self._accepted)
                if s > 0 and (language is None or exp.language == language)
            ),
            key=lambda t: -t[0],
        )
        return [(s, exp.example) for s, exp in ranked[:k]]

    def retrieve(
        self, query: str, *, k: int = 3, language: str | None = None
    ) -> list[HistoricalExample]:
        """Return the top-k most similar past merges for ``query``.

        ``query`` is typically the concatenation of the new conflict's sides.
        ``language`` filters examples to the same language when given. Returns
        an empty list if the corpus is too small or no matches score above 0.
        Delegates to :meth:`retrieve_scored` and drops the scores.
        """
        return [ex for _, ex in self.retrieve_scored(query, k=k, language=language)]

    def refresh(self) -> None:
        """Force a rebuild of the index (after new experiences are appended)."""
        self._index = None
        self._accepted = []


# ---------------------------------------------------------------------------
# EmbeddingRetriever (survey §4.2, LLMinus-style semantic RAG)
# ---------------------------------------------------------------------------


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors. Returns 0 for zero vectors."""
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


class EmbeddingRetriever:
    """Semantic retrieval over accepted experiences via vector embeddings.

    Embeds each accepted example's signature (base+current+replayed concatenated,
    same as the lexical retriever's document text) ONCE and caches the vectors.
    For a new conflict, embeds the query and cosine-ranks the corpus. Returns the
    top-k above a small similarity floor (so unrelated conflicts aren't force-fit
    as few-shot).

    Falls back gracefully: if embedding fails (endpoint down/unsupported, empty
    vectors), ``retrieve`` returns an empty list — the context builder then gets
    no few-shot, exactly as when the corpus is too small. The caller (orchestrator)
    selects this retriever only when ``probe_embeddings_support`` confirmed the
    endpoint works, so in practice the fallback is for transient mid-run failures.
    """

    # Minimum cosine similarity to surface an example as a few-shot. Embeddings on
    # a small local model are noisier than OpenAI-scale ones; a modest floor keeps
    # genuinely-unrelated conflicts out of the prompt. This is the DEFAULT; the
    # applied value comes from a calibrated profile (``calibrate-embeddings``) and
    # is injected via the ``min_similarity`` constructor parameter.
    MIN_SIMILARITY = 0.35

    def __init__(
        self, store: ExperienceStore, client: object, *, min_similarity: float = MIN_SIMILARITY
    ) -> None:
        self.store = store
        self.client = client  # EmbeddingsClient (Protocol); typed loose to avoid import cycle
        self.min_similarity = float(min_similarity)
        self._accepted: list[Experience] | None = None
        self._vectors: list[list[float]] | None = None

    def _signature(self, exp: Experience) -> str:
        ex = exp.example
        return " ".join([ex.base, ex.current, ex.replayed])

    def _build(self) -> None:
        """Embed every accepted example. Cached until ``refresh``."""
        accepted = self.store.accepted()
        self._accepted = accepted
        if not accepted:
            self._vectors = []
            return
        try:
            sigs = [self._signature(e) for e in accepted]
            self._vectors = self.client.embed(sigs)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - degrade to no few-shot on any embed failure
            self._accepted = []
            self._vectors = []

    def retrieve_scored(
        self, query: str, *, k: int = 3, language: str | None = None
    ) -> list[tuple[float, HistoricalExample]]:
        """Return ``(cosine_score, example)`` pairs for the top-k matches.

        Same ranking/filtering as :meth:`retrieve`, but keeps the score so callers
        (the retrieval-score diagnostic, embeddings calibration) can observe the
        confidence of each retrieved example. Pairs are sorted by descending
        score; the score is the raw cosine, before the ``min_similarity`` filter
        is applied — so a caller can see what was filtered out and why.
        """
        if self._accepted is None:
            self._build()
        assert self._accepted is not None and self._vectors is not None
        if not self._accepted or not self._vectors:
            return []
        try:
            q_vec = self.client.embed(query)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - transient embed failure → no few-shot
            return []
        if not q_vec:
            return []
        q = q_vec[0]
        scored = [
            (_cosine(q, vec), exp)
            for vec, exp in zip(self._vectors, self._accepted)
            if language is None or exp.language == language
        ]
        scored.sort(key=lambda t: -t[0])
        # Return the top-k scored pairs (above the floor) — scores preserved.
        return [
            (s, exp.example) for s, exp in scored[:k] if s >= self.min_similarity
        ]

    def retrieve(
        self, query: str, *, k: int = 3, language: str | None = None
    ) -> list[HistoricalExample]:
        """Return the top-k semantically-similar past merges for ``query``.

        Embeds the query, cosine-ranks the cached corpus, filters by language and
        the similarity floor, and returns the top-k. Returns [] if the corpus is
        empty, embedding fails, or nothing clears the floor. Delegates to
        :meth:`retrieve_scored` and drops the scores.
        """
        return [
            ex for _, ex in self.retrieve_scored(query, k=k, language=language)
        ]

    def refresh(self) -> None:
        """Force a rebuild of the vector cache (after new experiences appended)."""
        self._accepted = None
        self._vectors = None


# ---------------------------------------------------------------------------
# HybridRetriever (survey §4: BM25 + dense fusion)
# ---------------------------------------------------------------------------

# RRF constant. The literature default (k=60) smooths the rank contribution so a
# single retriever's rank-1 doesn't dominate; it's scale-robust across models.
_RRF_K = 60


def _example_key(ex: HistoricalExample) -> tuple[str, ...]:
    """A stable content-based key for a HistoricalExample.

    The two retrievers call ``store.accepted()`` independently and get distinct
    Python objects for the same logical example, so fusing by ``id()`` would
    double-count it. Keying on the example's content (the three conflict sides +
    the resolved text) merges them correctly.
    """
    return (ex.base, ex.current, ex.replayed, ex.resolved)


def _rrf_scores(ranked: list[tuple[float, HistoricalExample]]) -> dict[tuple[str, ...], float]:
    """Reciprocal Rank Fusion scores for one retriever's ranked results.

    Maps each example (by content key) to ``1 / (k + rank)`` where rank is
    0-indexed. RRF is scale-agnostic: it uses only rank position, so BM25's
    unbounded scores and cosine's bounded scores contribute comparably without
    normalization.
    """
    return {_example_key(ex): 1.0 / (_RRF_K + r) for r, (_, ex) in enumerate(ranked)}


def _dbsf_scores(
    ranked: list[tuple[float, HistoricalExample]],
) -> dict[tuple[str, ...], float]:
    """Distribution-Based Score Fusion: min-max normalize one retriever's scores
    to [0,1] so disparate score scales (BM25 unbounded, cosine in [-1,1]) combine
    additively. Returns content-key -> normalized score.
    """
    if not ranked:
        return {}
    vals = [s for s, _ in ranked]
    lo, hi = min(vals), max(vals)
    span = hi - lo
    if span <= 0:
        # All scores equal — give every result the same neutral weight.
        return {_example_key(ex): 1.0 for _, ex in ranked}
    return {_example_key(ex): (s - lo) / span for s, ex in ranked}


class HybridRetriever:
    """Fuses lexical (BM25) and semantic (embedding) retrieval (survey §4).

    BM25 and embeddings catch complementary failures: BM25 nails exact-identifier
    matches (``getUserName`` vs ``get_user_name``) that a semantic model may rank
    as paraphrases; embeddings catch same-intent-different-identifiers (``fetch``
    vs ``retrieve``) that BM25 misses entirely. Combining them is strictly better
    when both work, and degrades to lexical ranking when the embedding endpoint is
    unavailable (the embedding retriever returns []).

    Two fusion methods:

    - ``"rrf"`` (default): Reciprocal Rank Fusion. Uses only rank position
      (``score = Σ 1/(k+rank)``), so it needs no labeled data and is robust to the
      incompatible score scales. The survey's "no-tuning baseline" (§4.2).
    - ``"dbsf"``: Distribution-Based Score Fusion. Min-max normalizes each
      retriever's raw scores to [0,1] then sums them. Better when the score
      magnitudes carry signal beyond rank (§4.1); pairs with the calibrated score
      scale from the isotonic transform when available.

    Implements the same ``retrieve_scored`` / ``retrieve`` / ``refresh`` shape as
    the single retrievers so it drops into the context builder unchanged. Never
    raises: an embedding failure just drops that retriever's contribution.
    """

    def __init__(
        self,
        lexical: LexicalRetriever,
        embedding: EmbeddingRetriever,
        *,
        fusion: str = "rrf",
    ) -> None:
        self.lexical = lexical
        self.embedding = embedding
        self.fusion = fusion if fusion in ("rrf", "dbsf") else "rrf"

    def retrieve_scored(
        self, query: str, *, k: int = 3, language: str | None = None
    ) -> list[tuple[float, HistoricalExample]]:
        """Return ``(fused_score, example)`` pairs for the top-k matches.

        Asks each retriever for its own top-k (so a result only one retriever
        notices can still surface), fuses the two rankings by the configured
        method, and returns the top-k by fused score. The score is the FUSED
        value (RRF weight or summed normalized score), not either retriever's raw
        score — so it's comparable across examples but not on a raw-cosine scale.
        """
        # Each retriever contributes its own top-k. Retrieve failures degrade to
        # [] (the existing per-retriever contract), never raise.
        try:
            lex_ranked = self.lexical.retrieve_scored(query, k=k, language=language)
        except Exception:  # noqa: BLE001 - best-effort fusion
            lex_ranked = []
        try:
            emb_ranked = self.embedding.retrieve_scored(query, k=k, language=language)
        except Exception:  # noqa: BLE001 - best-effort fusion
            emb_ranked = []

        if self.fusion == "dbsf":
            lex_scores = _dbsf_scores(lex_ranked)
            emb_scores = _dbsf_scores(emb_ranked)
        else:  # "rrf"
            lex_scores = _rrf_scores(lex_ranked)
            emb_scores = _rrf_scores(emb_ranked)

        # Merge by example CONTENT key (not id() — the two retrievers return
        # distinct objects for the same logical example). Keep the example from
        # whichever retriever surfaced it first.
        by_key: dict[tuple[str, ...], HistoricalExample] = {}
        for _, ex in lex_ranked + emb_ranked:
            by_key.setdefault(_example_key(ex), ex)

        # Sum the two retrievers' contributions per example (missing = 0).
        keys = set(lex_scores) | set(emb_scores)
        fused: list[tuple[float, HistoricalExample]] = []
        for key in keys:
            ex = by_key.get(key)
            if ex is None:
                continue
            total = lex_scores.get(key, 0.0) + emb_scores.get(key, 0.0)
            fused.append((total, ex))
        fused.sort(key=lambda t: -t[0])
        return fused[:k]

    def retrieve(
        self, query: str, *, k: int = 3, language: str | None = None
    ) -> list[HistoricalExample]:
        """Top-k past merges by fused rank. Delegates to :meth:`retrieve_scored`."""
        return [ex for _, ex in self.retrieve_scored(query, k=k, language=language)]

    def refresh(self) -> None:
        """Force both sub-retrievers to rebuild their indexes/caches."""
        self.lexical.refresh()
        self.embedding.refresh()

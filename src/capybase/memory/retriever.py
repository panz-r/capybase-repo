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

    def retrieve(
        self, query: str, *, k: int = 3, language: str | None = None
    ) -> list[HistoricalExample]:
        """Return the top-k most similar past merges for ``query``.

        ``query`` is typically the concatenation of the new conflict's sides.
        ``language`` filters examples to the same language when given. Returns
        an empty list if the corpus is too small or no matches score above 0.
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
        # Pair (score, experience), filter zero scores and language, take top-k.
        ranked = sorted(
            (
                (s, exp)
                for s, exp in zip(scores, self._accepted)
                if s > 0 and (language is None or exp.language == language)
            ),
            key=lambda t: -t[0],
        )
        return [exp.example for _, exp in ranked[:k]]

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
    # genuinely-unrelated conflicts out of the prompt.
    MIN_SIMILARITY = 0.35

    def __init__(self, store: ExperienceStore, client: object) -> None:
        self.store = store
        self.client = client  # EmbeddingsClient (Protocol); typed loose to avoid import cycle
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

    def retrieve(
        self, query: str, *, k: int = 3, language: str | None = None
    ) -> list[HistoricalExample]:
        """Return the top-k semantically-similar past merges for ``query``.

        Embeds the query, cosine-ranks the cached corpus, filters by language and
        the similarity floor, and returns the top-k. Returns [] if the corpus is
        empty, embedding fails, or nothing clears the floor.
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
        scored = [(s, e) for s, e in scored if s >= self.MIN_SIMILARITY]
        scored.sort(key=lambda t: -t[0])
        return [exp.example for _, exp in scored[:k]]

    def refresh(self) -> None:
        """Force a rebuild of the vector cache (after new experiences appended)."""
        self._accepted = None
        self._vectors = None

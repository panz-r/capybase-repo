"""Retrieval over the experience store for RAG few-shot demonstrations.

Given a new conflict, the retriever finds the most similar past merges in the
experience corpus and returns them as ``HistoricalExample`` objects. These flow
into ``ContextBundle.retrieved_examples`` (the existing contract seam) and are
rendered into the prompt as dynamic few-shot — "here is how a similar conflict
was resolved before."

The default ``LexicalRetriever`` is a dependency-free BM25 over tokenized
code. It splits identifiers (camelCase, snake_case), drops stopwords and
punctuation, and ranks by BM25 score. An ``EmbeddingRetriever`` (using the
llama-server ``/v1/embeddings`` endpoint) can slot in behind the same
Protocol later.
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

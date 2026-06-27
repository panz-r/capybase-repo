"""Statistical calibration of the embedding-retrieval similarity floor.

Companion to :mod:`capybase.probes` (which probes whether the embeddings
*endpoint works*) and :mod:`capybase.calibration_profile` (which stores the
result). This module derives a model-specific ``min_similarity`` threshold from
the measured score distribution, replacing the 0.35 class-constant guess with a
statistically-grounded value.

Method (per the design: quantile-gap):
- Embed the corpus's ``(query, related, unrelated)`` triples.
- Compute cosine similarity for each related pair and each unrelated pair → two
  score distributions.
- The applied threshold is the midpoint of the LARGEST GAP between the related
  and unrelated sorted-score arrays — the natural "valley" separating the two
  classes. Robust to model scale (different embedding models produce different
  cosine magnitudes) and to corpus bias (it adapts to wherever the model places
  the separation, not an absolute number).
- Two reference estimates are also computed for the report:
  ``related_p10`` (10th percentile of related — conservative-keep) and
  ``unrelated_p90`` (90th percentile of unrelated — conservative-reject).

Never raises: a failed/unavailable endpoint yields a calibration with ``ok=False``
so the caller keeps the default floor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from capybase.embeddings_corpus import SimilarityProbe, probes
from capybase.memory.retriever import _cosine
from capybase.stats import percentile as _percentile


# The default floor the EmbeddingRetriever ships with — used as the fallback
# when calibration can't run, and as the baseline the report compares against.
DEFAULT_MIN_SIMILARITY = 0.35


@dataclass(frozen=True)
class ScoreDistribution:
    """Summary statistics of one class's measured similarity scores."""

    count: int
    minimum: float
    maximum: float
    mean: float

    def to_dict(self) -> dict:
        return {
            "count": self.count,
            "min": round(self.minimum, 4),
            "max": round(self.maximum, 4),
            "mean": round(self.mean, 4),
        }


@dataclass(frozen=True)
class EmbeddingCalibration:
    """The result of a calibration run.

    ``min_similarity`` is the applied threshold (what the retriever should use).
    ``quantile_gap`` / ``related_p10`` / ``unrelated_p90`` are the three
    estimates; the report shows all three for transparency. ``related`` and
    ``unrelated`` are the measured score distributions. ``ok`` is False when the
    endpoint was unreachable or produced no valid scores (caller keeps the
    default floor).
    """

    model: str
    min_similarity: float
    quantile_gap: float
    related_p10: float
    unrelated_p90: float
    related: ScoreDistribution
    unrelated: ScoreDistribution
    ok: bool
    probed_at: str
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "min_similarity": round(self.min_similarity, 4),
            "estimates": {
                "quantile_gap": round(self.quantile_gap, 4),
                "related_p10": round(self.related_p10, 4),
                "unrelated_p90": round(self.unrelated_p90, 4),
            },
            "related": self.related.to_dict(),
            "unrelated": self.unrelated.to_dict(),
            "ok": self.ok,
            "probed_at": self.probed_at,
            "notes": list(self.notes),
        }


# ``_percentile`` is imported from :mod:`capybase.stats` (shared numerics).


def _largest_gap_threshold(related: list[float], unrelated: list[float]) -> float:
    """The midpoint of the largest separation between the two score distributions.

    A good embedding model places related pairs high and unrelated pairs low,
    ideally with empty space between them. This finds the largest gap in the
    merged sorted score sequence that separates the two classes — the natural
    decision boundary. Robust to scale and to a few outliers (it looks for the
    widest *class-switching* gap, not a single adjacency).

    Algorithm: merge and sort all scores with their class tag. Walk the sequence;
    whenever the class changes (r→u or u→r), the gap between the two adjacent
    values is a candidate boundary. The LARGEST such gap is where the two
    distributions are most separated — the threshold goes at its midpoint. If the
    classes are well-separated, this is the empty space between them; if they
    overlap heavily, the gaps shrink (correctly signaling a weak model).
    """
    if not related or not unrelated:
        if related:
            return _percentile(sorted(related), 50)
        if unrelated:
            return _percentile(sorted(unrelated), 50)
        return DEFAULT_MIN_SIMILARITY
    merged = [(s, "r") for s in related] + [(s, "u") for s in unrelated]
    merged.sort(key=lambda t: t[0])
    best_gap = 0.0
    # Default: midpoint of the two medians (a reasonable cut when no switch-gap).
    best_mid = (_percentile(sorted(related), 50) + _percentile(sorted(unrelated), 50)) / 2.0
    for i in range(len(merged) - 1):
        lo_val, lo_tag = merged[i]
        hi_val, hi_tag = merged[i + 1]
        if lo_tag != hi_tag:  # a class-switching boundary
            gap = hi_val - lo_val
            if gap > best_gap:
                best_gap = gap
                best_mid = (lo_val + hi_val) / 2.0
    return best_mid


def _distribution(scores: list[float]) -> ScoreDistribution:
    if not scores:
        return ScoreDistribution(0, 0.0, 0.0, 0.0)
    return ScoreDistribution(
        count=len(scores),
        minimum=min(scores),
        maximum=max(scores),
        mean=sum(scores) / len(scores),
    )


def calibrate_thresholds(
    client: object,
    embeddings_model: str = "",
) -> EmbeddingCalibration:
    """Derive a model-specific ``min_similarity`` from the corpus score distribution.

    ``client`` is an :class:`~capybase.memory.embeddings.EmbeddingsClient` (the
    ``embed`` method); ``embeddings_model`` is recorded in the envelope for
    traceability. Never raises — a failed endpoint yields ``ok=False`` with the
    default floor so the caller keeps working.
    """
    corpus = probes()
    notes: list[str] = []
    # Collect all texts to embed in one batch (queries + related + unrelated).
    texts: list[str] = []
    index_map: list[tuple[int, str]] = []  # (corpus_index, role)
    for i, p in enumerate(corpus):
        texts.append(p.query)
        index_map.append((i, "query"))
        texts.append(p.related)
        index_map.append((i, "related"))
        texts.append(p.unrelated)
        index_map.append((i, "unrelated"))

    try:
        vectors = client.embed(texts)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - unreachable endpoint → keep default
        notes.append(f"embeddings request failed: {exc}")
        return _failed(embeddings_model, notes)

    if not vectors or len(vectors) != len(texts):
        notes.append(
            f"embeddings count mismatch: requested {len(texts)}, got "
            f"{len(vectors) if vectors else 0}"
        )
        return _failed(embeddings_model, notes)

    # Group vectors by (corpus_index, role).
    vec_by_role: dict[tuple[int, str], list[float]] = {}
    for vec, (i, role) in zip(vectors, index_map):
        vec_by_role[(i, role)] = vec

    # Compute per-probe cosine similarities.
    related_scores: list[float] = []
    unrelated_scores: list[float] = []
    for i, _ in enumerate(corpus):
        q = vec_by_role.get((i, "query"))
        r = vec_by_role.get((i, "related"))
        u = vec_by_role.get((i, "unrelated"))
        if q is None or r is None or u is None:
            continue
        related_scores.append(_cosine(q, r))
        unrelated_scores.append(_cosine(q, u))

    if not related_scores or not unrelated_scores:
        notes.append("no valid similarity pairs produced from the corpus")
        return _failed(embeddings_model, notes)

    related_sorted = sorted(related_scores)
    unrelated_sorted = sorted(unrelated_scores)
    gap_threshold = _largest_gap_threshold(related_scores, unrelated_scores)
    related_p10 = _percentile(related_sorted, 10)
    unrelated_p90 = _percentile(unrelated_sorted, 90)

    # The applied threshold is the quantile-gap estimate. Clamp to [0, 1]; if the
    # distributions badly overlap (gap < 0), fall back to the more conservative
    # of the two reference estimates so we don't admit noise.
    applied = max(0.0, min(1.0, gap_threshold))
    if applied <= 0.0:
        # Distributions overlap entirely — use the stricter of the two references
        # (highest unrelated score is the safe cut when there's no clear gap).
        applied = max(unrelated_p90, 0.0)
        notes.append(
            "related/unrelated distributions overlap; using unrelated_p90 as a "
            "conservative floor (this model may be too weak for reliable RAG)"
        )

    return EmbeddingCalibration(
        model=embeddings_model,
        min_similarity=applied,
        quantile_gap=gap_threshold,
        related_p10=related_p10,
        unrelated_p90=unrelated_p90,
        related=_distribution(related_scores),
        unrelated=_distribution(unrelated_scores),
        ok=True,
        probed_at=datetime.now(timezone.utc).isoformat(),
        notes=notes,
    )


def _failed(model: str, notes: list[str]) -> EmbeddingCalibration:
    """A calibration result for an unreachable/failed endpoint (keeps the default)."""
    empty = ScoreDistribution(0, 0.0, 0.0, 0.0)
    return EmbeddingCalibration(
        model=model,
        min_similarity=DEFAULT_MIN_SIMILARITY,
        quantile_gap=DEFAULT_MIN_SIMILARITY,
        related_p10=DEFAULT_MIN_SIMILARITY,
        unrelated_p90=DEFAULT_MIN_SIMILARITY,
        related=empty,
        unrelated=empty,
        ok=False,
        probed_at=datetime.now(timezone.utc).isoformat(),
        notes=notes,
    )

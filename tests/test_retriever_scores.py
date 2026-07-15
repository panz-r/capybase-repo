"""Tests for retrieval-score exposure + tunable min_similarity (F1).

The retrievers now expose ``retrieve_scored`` (keeps the score) and the
EmbeddingRetriever accepts a ``min_similarity`` constructor parameter replacing
the 0.35 class constant. This is the foundation for the embeddings-calibration
diagnostic: without exposed scores, the calibrated threshold can't be validated.
"""

from __future__ import annotations

from capybase.conflict_model import HistoricalExample
from capybase.memory.retriever import EmbeddingRetriever, LexicalRetriever
from capybase.memory.store import Experience, ExperienceStore


def _exp(base, current, replayed, resolved, *, language="python", outcome="accepted"):
    return Experience(
        example=HistoricalExample(
            summary="s", base=base, current=current, replayed=replayed, resolved=resolved
        ),
        outcome=outcome,
        language=language,
    )


class _FakeEmbClient:
    """Returns deterministic 2D vectors so cosine similarity is controllable."""

    def __init__(self, vectors):
        self._vectors = vectors  # list[list[float]]

    def embed(self, texts):
        if isinstance(texts, str):
            texts = [texts]
        # Return one vector per input text, cycling through the provided set.
        return [self._vectors[i % len(self._vectors)] for i in range(len(texts))]


# ---------------------------------------------------------------------------
# EmbeddingRetriever.retrieve_scored
# ---------------------------------------------------------------------------


def test_embedding_retrieve_scored_returns_scores(tmp_path):
    store = ExperienceStore(tmp_path / "exp.jsonl")
    store.append(_exp("a=1", "a=2", "a=3", "a=23"))
    store.append(_exp("b=1", "b=2", "b=3", "b=23"))
    # Two corpus vectors: one aligned with the query, one orthogonal.
    client = _FakeEmbClient([[1.0, 0.0], [0.0, 1.0]])
    r = EmbeddingRetriever(store, client)
    scored = r.retrieve_scored("a=1 a=2 a=3", k=5)  # query embeds to [1,0]
    assert len(scored) >= 1
    assert all(isinstance(s, float) for s, _ in scored)
    # The first corpus vector [1,0] has cosine 1.0 with the query [1,0].
    assert scored[0][0] == 1.0


def test_embedding_retrieve_drops_scores(tmp_path):
    """The plain retrieve() delegates to retrieve_scored and drops the score."""
    store = ExperienceStore(tmp_path / "exp.jsonl")
    store.append(_exp("a=1", "a=2", "a=3", "a=23"))
    client = _FakeEmbClient([[1.0, 0.0]])
    r = EmbeddingRetriever(store, client)
    examples = r.retrieve("a=1 a=2 a=3", k=5)
    assert all(isinstance(e, HistoricalExample) for e in examples)
    assert len(examples) >= 1


class _OrthogonalClient:
    """Corpus signatures embed to [1,0]; a bare query embeds to [0,1].

    The retriever builds the corpus signature by joining the three sides
    (``base current replayed``), so a stored example embeds to [1,0] while a
    free-form query string embeds to [0,1] — cosine 0.0 between them, which is
    what makes the ``min_similarity`` floor observable.
    """

    def embed(self, texts):
        if isinstance(texts, str):
            texts = [texts]
        return [[1.0, 0.0] if "=" in t else [0.0, 1.0] for t in texts]


def test_embedding_min_similarity_filters(tmp_path):
    """A high min_similarity filters out low-scoring matches.

    The corpus example's signature embeds to [1,0] but the query "query" embeds
    to [0,1] → cosine 0.0. The default floor (0.35) drops it; lowering the floor
    to 0.0 admits it."""
    store = ExperienceStore(tmp_path / "exp.jsonl")
    store.append(_exp("a=1", "a=2", "a=3", "a=23"))
    client = _OrthogonalClient()
    # Default floor (0.35): a cosine of 0.0 filters it out already.
    r_default = EmbeddingRetriever(store, client)
    assert r_default.retrieve_scored("query", k=5) == []
    # Lower floor: the low-scoring match is admitted (0.0 >= 0.0).
    r_low = EmbeddingRetriever(store, client, min_similarity=0.0)
    scored = r_low.retrieve_scored("query", k=5)
    assert len(scored) == 1


def test_embedding_min_similarity_default_is_class_constant(tmp_path):
    store = ExperienceStore(tmp_path / "exp.jsonl")
    client = _FakeEmbClient([[1.0, 0.0]])
    r = EmbeddingRetriever(store, client)
    assert r.min_similarity == EmbeddingRetriever.MIN_SIMILARITY


def test_embedding_min_similarity_custom(tmp_path):
    store = ExperienceStore(tmp_path / "exp.jsonl")
    client = _FakeEmbClient([[1.0, 0.0]])
    r = EmbeddingRetriever(store, client, min_similarity=0.71)
    assert r.min_similarity == 0.71


# ---------------------------------------------------------------------------
# LexicalRetriever.retrieve_scored
# ---------------------------------------------------------------------------


def test_lexical_retrieve_scored_returns_scores(tmp_path):
    store = ExperienceStore(tmp_path / "exp.jsonl")
    store.append(_exp("compute value", "compute result", "compute output", "compute"))
    store.append(_exp("unrelated thing", "other stuff", "more text", "merged"))
    r = LexicalRetriever(store)
    scored = r.retrieve_scored("compute value", k=5)
    assert len(scored) >= 1
    assert all(isinstance(s, float) for s, _ in scored)
    # The 'compute' example should score higher than 'unrelated'.
    assert scored[0][1].base == "compute value"


def test_lexical_retrieve_drops_scores(tmp_path):
    store = ExperienceStore(tmp_path / "exp.jsonl")
    store.append(_exp("compute value", "compute result", "compute output", "compute"))
    r = LexicalRetriever(store)
    examples = r.retrieve("compute value", k=5)
    assert all(isinstance(e, HistoricalExample) for e in examples)


def test_lexical_retrieve_scored_empty_corpus(tmp_path):
    store = ExperienceStore(tmp_path / "exp.jsonl")
    r = LexicalRetriever(store)
    assert r.retrieve_scored("anything", k=5) == []


# ---------------------------------------------------------------------------
# ContextBundle carries retrieval_scores (F4)
# ---------------------------------------------------------------------------


def test_context_bundle_has_retrieval_scores_field():
    from capybase.conflict_model import ContextBundle

    cb = ContextBundle(primary_text="x", retrieval_scores=[0.71, 0.43])
    assert cb.retrieval_scores == [0.71, 0.43]


def test_context_bundle_retrieval_scores_default_empty():
    from capybase.conflict_model import ContextBundle

    cb = ContextBundle(primary_text="x")
    assert cb.retrieval_scores == []


# ---------------------------------------------------------------------------
# EmbeddingRetriever consumes the calibration transform (A4,
# ---------------------------------------------------------------------------


def _calibration(iso_points, red_threshold):
    """Build a minimal EmbeddingCalibration carrying a fitted transform."""
    from capybase.embeddings_calibration import EmbeddingCalibration, ScoreDistribution

    return EmbeddingCalibration(
        model="embed", min_similarity=red_threshold,
        quantile_gap=0.0, related_p10=0.0, unrelated_p90=0.0,
        related=ScoreDistribution(0, 0.0, 0.0, 0.0),
        unrelated=ScoreDistribution(0, 0.0, 0.0, 0.0),
        ok=True, probed_at="",
        isotonic_points=iso_points,
        green_threshold=red_threshold, amber_threshold=red_threshold,
        red_threshold=red_threshold, ks_separation=0.0,
    )


def test_calibration_transform_applied_to_scores(tmp_path):
    """With an attached calibration carrying a fit, the returned score is the
    CALIBRATED value (raw passed through the transform), not the raw cosine."""
    store = ExperienceStore(tmp_path / "exp.jsonl")
    store.append(_exp("a=1", "a=2", "a=3", "a=23"))
    client = _FakeEmbClient([[1.0, 0.0]])  # corpus + query both [1,0] → cosine 1.0
    # A transform mapping raw 1.0 -> calibrated 0.5; floor 0.0 admits it.
    cal = _calibration([(0.0, 0.0), (1.0, 0.5)], red_threshold=0.0)
    r = EmbeddingRetriever(store, client, calibration=cal)
    scored = r.retrieve_scored("query", k=5)
    assert len(scored) == 1
    # The journaled score is the calibrated value (0.5), not the raw cosine (1.0).
    assert scored[0][0] == 0.5


def test_calibration_floor_filters_on_calibrated_scale(tmp_path):
    """The floor filter uses the calibration's red_threshold on the calibrated
    scale. A match whose RAW cosine is high but CALIBRATED score is below the
    floor is rejected — the whole point of calibrating the scale."""
    store = ExperienceStore(tmp_path / "exp.jsonl")
    store.append(_exp("a=1", "a=2", "a=3", "a=23"))
    client = _FakeEmbClient([[1.0, 0.0]])  # raw cosine 1.0
    # Transform maps raw 1.0 -> 0.4 calibrated; floor red_threshold=0.5 rejects it.
    cal = _calibration([(0.0, 0.0), (1.0, 0.4)], red_threshold=0.5)
    r = EmbeddingRetriever(store, client, calibration=cal)
    assert r.retrieve_scored("query", k=5) == []  # calibrated 0.4 < floor 0.5
    # Lowering the floor to 0.3 admits it (0.4 >= 0.3).
    cal_low = _calibration([(0.0, 0.0), (1.0, 0.4)], red_threshold=0.3)
    r_low = EmbeddingRetriever(store, client, calibration=cal_low)
    assert len(r_low.retrieve_scored("query", k=5)) == 1


def test_calibration_without_fit_is_identical_to_no_calibration(tmp_path):
    """A calibration object WITHOUT an isotonic fit degrades to the raw-cosine
    path — byte-identical to passing no calibration at all."""
    from capybase.embeddings_calibration import EmbeddingCalibration, ScoreDistribution

    store = ExperienceStore(tmp_path / "exp.jsonl")
    store.append(_exp("a=1", "a=2", "a=3", "a=23"))
    client = _FakeEmbClient([[1.0, 0.0]])
    no_cal = EmbeddingRetriever(store, client, min_similarity=0.35)
    empty_cal = EmbeddingRetriever(
        store, client, min_similarity=0.35,
        calibration=EmbeddingCalibration(
            model="embed", min_similarity=0.35, quantile_gap=0.35,
            related_p10=0.0, unrelated_p90=0.0,
            related=ScoreDistribution(0, 0.0, 0.0, 0.0),
            unrelated=ScoreDistribution(0, 0.0, 0.0, 0.0),
            ok=True, probed_at="",  # no isotonic_points -> no fit
        ),
    )
    assert no_cal.retrieve_scored("query", k=5) == empty_cal.retrieve_scored("query", k=5)


def test_no_calibration_is_raw_cosine_path(tmp_path):
    """No calibration object at all → raw cosine scores + min_similarity floor,
    exactly the pre-calibration behavior."""
    store = ExperienceStore(tmp_path / "exp.jsonl")
    store.append(_exp("a=1", "a=2", "a=3", "a=23"))
    client = _FakeEmbClient([[1.0, 0.0]])
    r = EmbeddingRetriever(store, client, min_similarity=0.35)
    assert r.calibration is None
    scored = r.retrieve_scored("query", k=5)
    assert len(scored) == 1
    assert scored[0][0] == 1.0  # raw cosine, unchanged

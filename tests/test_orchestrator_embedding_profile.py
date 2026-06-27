"""Orchestrator integration tests for the calibrated embedding-similarity floor.

These verify the full ``profile → config.memory.embedding_min_similarity →
EmbeddingRetriever`` path (F3): a model profile that carries a calibrated
``embedding_min_similarity`` reaches the retriever the orchestrator actually
builds — not just ``self.config``. The pure transform is covered by
``tests/test_calibration_profile.py``; the retriever constructor floor is
covered by ``tests/test_retriever_scores.py``. This is the integration seam.

The embeddings client is monkeypatched so no network is touched and the
EmbeddingRetriever is constructible.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from capybase.calibration_profile import ModelProfile
from capybase.config import Config
from capybase.memory.retriever import EmbeddingRetriever, LexicalRetriever
from capybase.orchestrator import Orchestrator

from tests.conftest import real_profile_loader  # noqa: F401 (opt-in fixture)


@pytest.fixture(autouse=True)
def _exercise_overlay(real_profile_loader) -> None:
    """This module exercises the profile overlay, so opt back into the real loader."""


def _profile(**over) -> ModelProfile:
    base = dict(
        model="vibethink",
        max_tokens=4096,
        json_mode=True,
        capture_token_entropy=False,
        generation_timeout_seconds=60,
    )
    base.update(over)
    return ModelProfile(**base)


def _profile_path(repo: Path) -> Path:
    return repo / ".rebase-agent" / "memory" / "model_profile.json"


def _cfg(repo: Path, *, model: str = "vibethink", retriever: str = "lexical") -> Config:
    cfg = Config()
    cfg.model.model = model
    cfg.calibration.model_profile_path = str(_profile_path(repo))
    # Enable RAG so the orchestrator builds a retriever at all.
    cfg.memory.enabled = True
    cfg.future.enable_rag = True
    cfg.memory.retriever = retriever
    return cfg


def _patch_embeddings_client(monkeypatch, *, vectors=None):
    """Make OpenAIEmbeddingsClient constructible + embed without a network.

    ``vectors`` (if given) is returned per-batch by index; default returns a
    constant vector so retrieval is deterministic. Mirrors the monkeypatch style
    in ``tests/test_embeddings.py``.
    """
    from capybase.memory import embeddings as emb_mod

    monkeypatch.setattr(
        "capybase.memory.embeddings.OpenAIEmbeddingsClient.__init__",
        lambda self, config, **kw: setattr(self, "config", config) or None,
    )
    if vectors is None:
        monkeypatch.setattr(
            "capybase.memory.embeddings.OpenAIEmbeddingsClient.embed",
            lambda self, t: [[0.5, 0.5]] * (1 if isinstance(t, str) else len(t)),
        )
    else:
        monkeypatch.setattr(
            "capybase.memory.embeddings.OpenAIEmbeddingsClient.embed",
            lambda self, t: vectors,
        )


# ---------------------------------------------------------------------------
# The calibrated floor reaches the built retriever
# ---------------------------------------------------------------------------


def test_profile_embedding_min_similarity_reaches_retriever(repo: Path, monkeypatch):
    """A matching profile's ``embedding_min_similarity`` overrides the config
    default AND is the floor the built EmbeddingRetriever uses."""
    _profile(embedding_min_similarity=0.71).save(_profile_path(repo))
    _patch_embeddings_client(monkeypatch)
    cfg = _cfg(repo, retriever="embedding")

    orch = Orchestrator(cfg, repo=str(repo))

    # The floor propagated to config...
    assert orch.config.memory.embedding_min_similarity == 0.71
    # ...and to the retriever the context builder actually holds.
    retriever = orch.context_builder.retriever
    assert isinstance(retriever, EmbeddingRetriever)
    assert retriever.min_similarity == 0.71


def test_default_floor_when_no_profile(repo: Path, monkeypatch):
    """Without a profile, the retriever uses the conservative 0.35 default."""
    _patch_embeddings_client(monkeypatch)
    cfg = _cfg(repo, retriever="embedding")

    orch = Orchestrator(cfg, repo=str(repo))

    retriever = orch.context_builder.retriever
    assert isinstance(retriever, EmbeddingRetriever)
    assert retriever.min_similarity == 0.35  # MemoryConfig default


def test_capability_flag_flips_retriever_to_embedding(repo: Path, monkeypatch):
    """``enable_embedding_rag`` in a matching profile flips the retriever from
    lexical to embedding at orchestrator init."""
    _profile(enable_embedding_rag=True, embedding_min_similarity=0.6).save(
        _profile_path(repo)
    )
    _patch_embeddings_client(monkeypatch)
    cfg = _cfg(repo, retriever="lexical")  # user has lexical configured

    orch = Orchestrator(cfg, repo=str(repo))

    retriever = orch.context_builder.retriever
    assert isinstance(retriever, EmbeddingRetriever)
    assert retriever.min_similarity == 0.6  # calibrated floor applied too


def test_mismatched_model_does_not_apply_floor(repo: Path, monkeypatch):
    """A profile for a different model is ignored entirely — the floor stays at
    the config default and the retriever keeps lexical mode, AND the user is
    nudged to recalibrate."""
    import warnings

    _profile(model="other-model", enable_embedding_rag=True,
             embedding_min_similarity=0.9).save(_profile_path(repo))
    _patch_embeddings_client(monkeypatch)
    cfg = _cfg(repo, model="vibethink", retriever="lexical")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        orch = Orchestrator(cfg, repo=str(repo))

    retriever = orch.context_builder.retriever
    assert isinstance(retriever, LexicalRetriever)  # flag NOT applied
    assert orch.config.memory.embedding_min_similarity == 0.35  # default
    assert any("recalibrate" in str(w.message) for w in caught)


def test_explicit_config_floor_used_without_profile(repo: Path, monkeypatch):
    """When the user sets ``embedding_min_similarity`` in config directly (no
    profile), that value reaches the retriever."""
    _patch_embeddings_client(monkeypatch)
    cfg = _cfg(repo, retriever="embedding")
    cfg.memory.embedding_min_similarity = 0.42

    orch = Orchestrator(cfg, repo=str(repo))

    retriever = orch.context_builder.retriever
    assert isinstance(retriever, EmbeddingRetriever)
    assert retriever.min_similarity == 0.42


# ---------------------------------------------------------------------------
# A5: the calibrated envelope reaches the retriever (isotonic transform applied)
# ---------------------------------------------------------------------------


def _envelope_with_fit(*, red: float = 0.6) -> dict:
    """A calibration envelope as calibrate-embeddings would write it (with a
    fitted isotonic transform + zones)."""
    return {
        "model": "vibethink",
        "min_similarity": red,
        "estimates": {"quantile_gap": red, "related_p10": 0.9, "unrelated_p90": 0.4},
        "related": {"count": 24, "min": 0.7, "max": 0.99, "mean": 0.88},
        "unrelated": {"count": 24, "min": 0.05, "max": 0.41, "mean": 0.22},
        "ok": True,
        "probed_at": "2026-06-27T00:00:00+00:00",
        "notes": [],
        "isotonic_points": [[0.1, 0.0], [0.9, 1.0]],
        "zones": {"green": 0.7, "amber": 0.65, "red": red},
        "ks_separation": 0.85,
    }


def test_profile_calibration_envelope_reaches_retriever(repo: Path, monkeypatch):
    """A matching profile's full calibration envelope (isotonic transform + zones)
    is reconstructed and attached to the built EmbeddingRetriever, so the isotonic
    score transform applies at retrieval time."""
    _profile(
        enable_embedding_rag=True,
        embedding_min_similarity=0.6,
        embedding_calibration=_envelope_with_fit(),
    ).save(_profile_path(repo))
    _patch_embeddings_client(monkeypatch)
    cfg = _cfg(repo, retriever="lexical")  # profile flips it to embedding

    orch = Orchestrator(cfg, repo=str(repo))

    retriever = orch.context_builder.retriever
    assert isinstance(retriever, EmbeddingRetriever)
    # The calibration was reconstructed and attached.
    cal = retriever.calibration
    assert cal is not None
    assert getattr(cal, "has_isotonic_fit", False)
    assert getattr(cal, "red_threshold", 0.0) == 0.6


def test_calibration_envelope_empty_without_profile(repo: Path, monkeypatch):
    """No profile → no calibration envelope in config, and the retriever's
    calibration is None (raw-cosine path)."""
    _patch_embeddings_client(monkeypatch)
    cfg = _cfg(repo, retriever="embedding")

    orch = Orchestrator(cfg, repo=str(repo))

    retriever = orch.context_builder.retriever
    assert isinstance(retriever, EmbeddingRetriever)
    assert retriever.calibration is None
    assert orch.config.memory.embedding_calibration == {}


# ---------------------------------------------------------------------------
# B2: hybrid retriever wiring (survey §4)
# ---------------------------------------------------------------------------


def test_hybrid_retriever_built_from_config(repo: Path, monkeypatch):
    """``retriever == "hybrid"`` builds a HybridRetriever wrapping both lexical
    and embedding retrievers, with the calibrated floor + envelope applied to the
    embedding half."""
    _profile(
        enable_embedding_rag=True,
        embedding_min_similarity=0.6,
        embedding_calibration=_envelope_with_fit(),
    ).save(_profile_path(repo))
    _patch_embeddings_client(monkeypatch)
    cfg = _cfg(repo, retriever="hybrid")

    orch = Orchestrator(cfg, repo=str(repo))

    from capybase.memory.retriever import HybridRetriever

    retriever = orch.context_builder.retriever
    assert isinstance(retriever, HybridRetriever)
    # The embedding half carries the calibrated floor + transform.
    assert isinstance(retriever.embedding, EmbeddingRetriever)
    assert retriever.embedding.min_similarity == 0.6
    assert retriever.embedding.calibration is not None
    # Default fusion is RRF.
    assert retriever.fusion == "rrf"


def test_hybrid_fusion_method_from_profile(repo: Path, monkeypatch):
    """The profile's fusion_method reaches the HybridRetriever."""
    _profile(
        enable_embedding_rag=True,
        embedding_min_similarity=0.6,
        fusion_method="dbsf",
    ).save(_profile_path(repo))
    _patch_embeddings_client(monkeypatch)
    cfg = _cfg(repo, retriever="hybrid")

    orch = Orchestrator(cfg, repo=str(repo))

    from capybase.memory.retriever import HybridRetriever

    retriever = orch.context_builder.retriever
    assert isinstance(retriever, HybridRetriever)
    assert retriever.fusion == "dbsf"
    assert orch.config.memory.fusion_method == "dbsf"


def test_hybrid_degrades_to_lexical_when_embedding_unavailable(repo: Path, monkeypatch):
    """If the embedding endpoint can't be constructed, hybrid falls back to plain
    lexical (no crash). The HybridRetriever is NOT built — lexical-only wins."""
    # Make the embeddings client construction raise.
    def _boom(_cfg, *_a, **_k):
        raise RuntimeError("no embeddings endpoint")

    monkeypatch.setattr(
        "capybase.memory.embeddings.OpenAIEmbeddingsClient.__init__", _boom
    )
    cfg = _cfg(repo, retriever="hybrid")

    orch = Orchestrator(cfg, repo=str(repo))

    from capybase.memory.retriever import HybridRetriever, LexicalRetriever

    retriever = orch.context_builder.retriever
    # Embedding construction failed → plain lexical (not a lexical-only hybrid).
    assert isinstance(retriever, LexicalRetriever)
    assert not isinstance(retriever, HybridRetriever)


def test_hybrid_with_lexical_config_stays_lexical(repo: Path, monkeypatch):
    """Without ``retriever == "hybrid"`` set, no HybridRetriever is built even if
    the profile enables embedding RAG (it flips to "embedding" instead)."""
    _profile(enable_embedding_rag=True, embedding_min_similarity=0.6).save(
        _profile_path(repo)
    )
    _patch_embeddings_client(monkeypatch)
    cfg = _cfg(repo, retriever="lexical")

    orch = Orchestrator(cfg, repo=str(repo))

    from capybase.memory.retriever import HybridRetriever

    retriever = orch.context_builder.retriever
    assert not isinstance(retriever, HybridRetriever)  # flipped to embedding, not hybrid

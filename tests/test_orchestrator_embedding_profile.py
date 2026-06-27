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

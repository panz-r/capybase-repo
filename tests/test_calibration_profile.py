"""Tests for the model profile: round-trip persistence and the runtime overlay.

These cover the "Profile wins" contract without touching a network endpoint:
the profile is plain data, and ``apply_profile`` is a pure config transform.
Probe-side behavior (binary search, capability detection) lives in
``tests/test_probes.py``.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest

from capybase.calibration_profile import (
    PROFILE_KNOBS,
    ModelProfile,
    apply_profile,
    resolve_profile_path,
)

from tests.conftest import real_profile_loader  # noqa: F401


@pytest.fixture(autouse=True)
def _exercise_profile_io(real_profile_loader) -> None:
    """This module tests ModelProfile.load/save directly, so opt back into the
    real loader (the suite-wide conftest fixture otherwise disables it)."""
from capybase.config import ModelConfig


def _profile(**over) -> ModelProfile:
    base = dict(
        model="vibethink",
        max_tokens=16384,
        json_mode=False,
        capture_token_entropy=True,
        generation_timeout_seconds=240,
        samples=3,
        two_pass=True,
        plan_search=False,
        prompt_variants=True,
        diverse_sampling=False,
        enable_self_consistency=True,
        avg_latency_ms=4500.0,
        probed_at="2026-06-26T00:00:00Z",
        capybase_version="0.1.0",
        notes=["raised max_tokens", "server lacks response_format"],
    )
    base.update(over)
    return ModelProfile(**base)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_to_dict_from_dict_roundtrip():
    p = _profile()
    d = p.to_dict()
    assert d["model"] == "vibethink"
    assert d["max_tokens"] == 16384
    assert d["notes"] == ["raised max_tokens", "server lacks response_format"]
    again = ModelProfile.from_dict(d)
    assert again == p


def test_from_dict_tolerates_missing_optional_fields():
    # A minimal/older profile blob should still load with sane defaults.
    p = ModelProfile.from_dict({"model": "x", "max_tokens": 4096})
    assert p.model == "x"
    assert p.max_tokens == 4096
    assert p.json_mode is True  # default
    assert p.capture_token_entropy is False
    assert p.notes == []
    assert isinstance(p.avg_latency_ms, float)


def test_from_dict_coerces_non_list_notes_to_single_item():
    p = ModelProfile.from_dict({"model": "x", "max_tokens": 1, "notes": "oops"})
    assert p.notes == ["oops"]


def test_from_dict_backward_compatible_without_mechanism_fields():
    """An older profile (pre-mechanism-calibration) omits samples/two_pass/etc.
    It must still load, with mechanism fields defaulting to current behavior
    (samples=1, all mechanisms off) — no crash, no behavior change."""
    p = ModelProfile.from_dict(
        {"model": "vibethink", "max_tokens": 4096, "json_mode": True,
         "capture_token_entropy": False, "generation_timeout_seconds": 60}
    )
    assert p.max_tokens == 4096
    assert p.samples == 1  # default
    assert p.two_pass is False
    assert p.enable_self_consistency is False


def test_save_load_roundtrip(tmp_path: Path):
    p = _profile()
    path = tmp_path / ".rebase-agent" / "memory" / "model_profile.json"
    p.save(path)
    assert path.is_file()
    loaded = ModelProfile.load(path)
    assert loaded == p


def test_load_returns_none_when_absent(tmp_path: Path):
    assert ModelProfile.load(tmp_path / "nope.json") is None


@pytest.mark.parametrize("bad", ["not json", "{"])
def test_load_returns_none_when_malformed_json(tmp_path: Path, bad: str):
    path = tmp_path / "bad.json"
    path.write_text(bad, encoding="utf-8")
    # Resolution must never crash on an unreadable artifact file.
    assert ModelProfile.load(path) is None


def test_load_returns_none_when_not_an_object(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("[]", encoding="utf-8")
    # A JSON array isn't a profile object: indexing ``d.get(...)`` fails.
    assert ModelProfile.load(path) is None


def test_load_partial_blob_fills_defaults(tmp_path: Path):
    """A valid-but-minimal blob is NOT corrupt — it loads with defaults."""
    path = tmp_path / "minimal.json"
    path.write_text('{"model": "x", "max_tokens": 4096}', encoding="utf-8")
    p = ModelProfile.load(path)
    assert p is not None
    assert p.model == "x"
    assert p.max_tokens == 4096
    assert p.json_mode is True  # filled from default


def test_save_creates_parent_dirs(tmp_path: Path):
    p = _profile()
    path = tmp_path / "a" / "b" / "c" / "profile.json"
    p.save(path)
    assert json.loads(path.read_text(encoding="utf-8"))["model"] == "vibethink"


# ---------------------------------------------------------------------------
# apply_profile — the "Profile wins" overlay
# ---------------------------------------------------------------------------


def test_apply_profile_overlays_tuned_knobs_when_names_match():
    cfg = ModelConfig(model="vibethink")  # defaults: max_tokens=8192, json_mode=True
    new_cfg, overridden = apply_profile(cfg, _profile())
    assert new_cfg.max_tokens == 16384
    assert new_cfg.json_mode is False
    assert new_cfg.capture_token_entropy is True
    assert new_cfg.generation_timeout_seconds == 240
    # Mechanism choices overlaid too.
    assert new_cfg.samples == 3
    assert new_cfg.two_pass is True
    assert new_cfg.prompt_variants is True
    assert new_cfg.enable_self_consistency is True
    # The knobs that differ from ModelConfig defaults are overlaid. plan_search
    # and diverse_sampling are False in this profile (matching defaults), so they
    # are NOT in the overridden set — "overridden" means "value changed".
    expected_changed = set(PROFILE_KNOBS) - {"plan_search", "diverse_sampling"}
    assert set(overridden) == expected_changed


def test_apply_profile_preserves_untouched_knobs():
    cfg = ModelConfig(model="vibethink", temperature=0.35, sampling_temperature=0.9)
    new_cfg, overridden = apply_profile(cfg, _profile())
    assert new_cfg.temperature == 0.35  # never a profile knob
    assert new_cfg.sampling_temperature == 0.9  # never a profile knob
    assert "temperature" not in overridden
    assert "sampling_temperature" not in overridden


def test_apply_profile_does_not_mutate_original():
    cfg = ModelConfig(model="vibethink")
    original_max = cfg.max_tokens
    apply_profile(cfg, _profile())
    assert cfg.max_tokens == original_max  # immutable: caller's cfg unchanged


def test_apply_profile_with_none_is_noop():
    cfg = ModelConfig(model="vibethink")
    new_cfg, overridden = apply_profile(cfg, None)
    assert new_cfg is cfg
    assert overridden == []


def test_apply_profile_model_mismatch_is_ignored_and_warns():
    cfg = ModelConfig(model="qwen-coder")
    profile = _profile(model="vibethink")  # for a different model
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        new_cfg, overridden = apply_profile(cfg, profile)
    assert new_cfg.max_tokens == cfg.max_tokens  # unchanged
    assert overridden == []
    assert any("recalibrate" in str(w.message) for w in caught)


def test_apply_profile_reports_only_changed_knobs():
    # A profile whose values all equal the config defaults changes nothing.
    cfg = ModelConfig(model="vibethink")
    matching = _profile(
        max_tokens=cfg.max_tokens,
        json_mode=cfg.json_mode,
        capture_token_entropy=cfg.capture_token_entropy,
        generation_timeout_seconds=cfg.generation_timeout_seconds,
        samples=cfg.samples,
        two_pass=cfg.two_pass,
        plan_search=cfg.plan_search,
        prompt_variants=cfg.prompt_variants,
        diverse_sampling=cfg.diverse_sampling,
        enable_self_consistency=cfg.enable_self_consistency,
    )
    _, overridden = apply_profile(cfg, matching)
    assert overridden == []


def test_resolve_profile_path_relative_to_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    p = resolve_profile_path(str(repo), ".rebase-agent/memory/model_profile.json")
    assert p == repo / ".rebase-agent" / "memory" / "model_profile.json"


def test_resolve_profile_path_absolute_passthrough(tmp_path: Path):
    abs_path = tmp_path / "abs.json"
    assert resolve_profile_path("/any/repo", str(abs_path)) == abs_path


# ---------------------------------------------------------------------------
# Embeddings-calibration fields (F2): embedding_min_similarity + embedding_calibration
# ---------------------------------------------------------------------------

# A representative calibration envelope as ``calibrate-embeddings`` would write
# it (a subset of EmbeddingCalibration.to_dict); used to exercise persistence.
_CALIBRATION_ENV = {
    "model": "qwen-embed",
    "min_similarity": 0.71,
    "estimates": {"quantile_gap": 0.71, "related_p10": 0.83, "unrelated_p90": 0.40},
    "related": {"count": 8, "min": 0.7, "max": 0.99, "mean": 0.88},
    "unrelated": {"count": 8, "min": 0.05, "max": 0.41, "mean": 0.22},
    "ok": True,
    "probed_at": "2026-06-27T00:00:00+00:00",
    "notes": [],
}


def test_profile_roundtrip_preserves_embedding_fields():
    """The calibrated floor and its calibration envelope survive to_dict/from_dict."""
    p = _profile(embedding_min_similarity=0.71, embedding_calibration=_CALIBRATION_ENV)
    d = p.to_dict()
    assert d["embedding_min_similarity"] == 0.71
    assert d["embedding_calibration"] == _CALIBRATION_ENV
    again = ModelProfile.from_dict(d)
    assert again == p
    assert again.embedding_min_similarity == 0.71
    assert again.embedding_calibration == _CALIBRATION_ENV


def test_profile_embedding_min_similarity_default():
    """Without calibration, the field is the conservative 0.35 guess."""
    p = _profile()
    assert p.embedding_min_similarity == 0.35
    assert p.embedding_calibration == {}


def test_profile_fusion_method_roundtrip():
    """The hybrid fusion method (survey §4) round-trips through to_dict/from_dict."""
    p = _profile(fusion_method="dbsf")
    d = p.to_dict()
    assert d["fusion_method"] == "dbsf"
    again = ModelProfile.from_dict(d)
    assert again.fusion_method == "dbsf"
    assert again == p


def test_profile_fusion_method_default_empty():
    """Default fusion_method is "" — the orchestrator reads "" as "rrf" at runtime."""
    p = _profile()
    assert p.fusion_method == ""


def test_from_dict_backward_compatible_without_fusion_method():
    """An older profile (pre-hybrid) omits fusion_method; it loads as "" (→ rrf)."""
    p = ModelProfile.from_dict({"model": "vibethink", "max_tokens": 4096})
    assert p.fusion_method == ""


def test_from_dict_backward_compatible_without_embedding_fields():
    """An older profile (pre-calibrate-embeddings) omits the embedding fields.
    It must still load, defaulting the floor to 0.35 and the envelope to {}."""
    p = ModelProfile.from_dict({"model": "vibethink", "max_tokens": 4096})
    assert p.embedding_min_similarity == 0.35
    assert p.embedding_calibration == {}


def test_from_dict_coerces_non_dict_calibration_to_empty():
    """A corrupt calibration envelope (non-dict) degrades to {} — the profile's
    never-crash-on-load contract."""
    p = ModelProfile.from_dict(
        {"model": "x", "max_tokens": 1, "embedding_calibration": ["not", "a", "dict"]}
    )
    assert p.embedding_calibration == {}


def test_to_dict_coerces_non_dict_calibration_to_empty():
    """``to_dict`` defensively coerces a non-dict envelope so the serialized form
    is always a clean dict (a hand-mangled profile object must still serialize)."""
    p = _profile()
    object.__setattr__(p, "embedding_calibration", "oops")  # bypass dataclass typing
    d = p.to_dict()
    assert d["embedding_calibration"] == {}


def test_save_load_preserves_calibration_envelope(tmp_path: Path):
    """The full envelope (nested estimates + distributions) round-trips via JSON."""
    p = _profile(embedding_min_similarity=0.66, embedding_calibration=_CALIBRATION_ENV)
    path = tmp_path / "profile.json"
    p.save(path)
    loaded = ModelProfile.load(path)
    assert loaded is not None
    assert loaded.embedding_min_similarity == 0.66
    assert loaded.embedding_calibration["estimates"]["quantile_gap"] == 0.71
    assert loaded.embedding_calibration["related"]["mean"] == 0.88

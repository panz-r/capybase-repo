"""Provider config: the canonical endpoint mechanism for live runs.

Covers the contract in src/capybase/provider_config.py:

- provider JSONs live OUTSIDE the repo (<config_dir>/providers/<name>.json)
  and bundle host+model for the LLM, an optional SEPARATE embeddings
  host+model, and the REQUIRED calibration profile reference;
- resolution precedence per field: CLI flag → env var → provider file;
- no fallbacks: a run with no endpoint source, or without a calibration
  profile, is an ERROR — profiles are never auto-created or substituted;
- calibration profiles carry no host info and may be REUSED against a
  different host or model (apply_profile force semantics).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from capybase.calibration_profile import ModelProfile, apply_profile
from capybase.config import Config
from capybase.provider_config import (
    ProviderError,
    apply_to_config,
    profile_path_for,
    providers_dir,
    resolve_provider,
)


def _write_profile(config_dir: Path, name: str, model: str = "test-model") -> Path:
    p = profile_path_for(name, config_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    ModelProfile(model=model, max_tokens=1234, json_mode=True,
                 generation_timeout_seconds=99).save(p)
    return p


def _write_provider(config_dir: Path, name: str, **overrides) -> Path:
    d = providers_dir(config_dir)
    d.mkdir(parents=True, exist_ok=True)
    doc = {
        "profile": "e2b",
        "llm": {"base_url": "http://server-a.example:8086/v1", "model": "chat"},
    }
    doc.update(overrides)
    p = d / f"{name}.json"
    p.write_text(json.dumps(doc))
    return p


def _no_env() -> dict[str, str]:
    # Strip the ambient environment so tests never inherit a real endpoint.
    return {k: "" for k in (
        "CAPYBASE_PROVIDER", "CAPYBASE_BASE_URL", "CAPYBASE_MODEL",
        "CAPYBASE_API_KEY", "CAPYBASE_PROFILE",
    )}


# --- file loading ---------------------------------------------------------

def test_provider_file_nested_and_flat_forms(tmp_path: Path, real_profile_loader) -> None:
    _write_profile(tmp_path, "e2b")
    nested = _write_provider(tmp_path, "nested")
    flat = providers_dir(tmp_path) / "flat.json"
    flat.write_text(json.dumps({
        "profile": "e2b",
        "base_url": "http://server-b.example:9000/v1",
        "model": "m",
    }))

    r = resolve_provider(provider=str(nested), environ={}, config_dir=tmp_path)
    assert r.provider.base_url == "http://server-a.example:8086/v1"
    assert r.provider.model == "chat"
    assert r.provider.profile == "e2b"

    r2 = resolve_provider(provider="flat", environ={}, config_dir=tmp_path)
    assert r2.provider.base_url == "http://server-b.example:9000/v1"
    assert r2.provider.model == "m"


def test_provider_by_name_and_by_path(tmp_path: Path, real_profile_loader) -> None:
    _write_profile(tmp_path, "e2b")
    p = _write_provider(tmp_path, "acme")
    by_name = resolve_provider(provider="acme", environ={}, config_dir=tmp_path)
    by_path = resolve_provider(provider=str(p), environ={}, config_dir=tmp_path)
    assert by_name.provider.base_url == by_path.provider.base_url
    assert str(by_name.profile_path) == str(by_path.profile_path)


# --- precedence: CLI > env > file ------------------------------------------

def test_cli_overrides_env_overrides_file(tmp_path: Path, real_profile_loader) -> None:
    _write_profile(tmp_path, "e2b")
    _write_provider(tmp_path, "acme")
    env = {**_no_env(), "CAPYBASE_BASE_URL": "http://env.example/v1",
           "CAPYBASE_MODEL": "env-model"}

    r_env = resolve_provider(provider="acme", environ=env, config_dir=tmp_path)
    assert r_env.provider.base_url == "http://env.example/v1"
    assert r_env.provider.model == "env-model"
    assert r_env.provider.profile == "e2b"  # file value stands
    assert r_env.provider.provenance["base_url"] == "env"

    r_cli = resolve_provider(
        provider="acme", base_url="http://cli.example/v1", model="cli-model",
        environ=env, config_dir=tmp_path,
    )
    assert r_cli.provider.base_url == "http://cli.example/v1"
    assert r_cli.provider.model == "cli-model"
    assert r_cli.provider.provenance["base_url"] == "cli"


# --- hard refusals (the no-fallback contract) ------------------------------

def test_no_endpoint_configured_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ProviderError, match="no model endpoint configured"):
        resolve_provider(environ=_no_env(), config_dir=tmp_path)


def test_missing_provider_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ProviderError, match="provider config not found"):
        resolve_provider(provider="nope", environ=_no_env(), config_dir=tmp_path)


def test_running_without_a_profile_is_an_error(tmp_path: Path) -> None:
    (providers_dir(tmp_path)).mkdir(parents=True, exist_ok=True)
    (providers_dir(tmp_path) / "acme.json").write_text(json.dumps({
        "llm": {"base_url": "http://x.example/v1", "model": "m"},
    }))
    with pytest.raises(ProviderError, match="profile is REQUIRED"):
        resolve_provider(provider="acme", environ=_no_env(), config_dir=tmp_path)


def test_missing_profile_file_is_never_auto_created(tmp_path: Path) -> None:
    _write_provider(tmp_path, "acme")  # references profile "e2b" — absent
    with pytest.raises(ProviderError, match="never auto-created"):
        resolve_provider(provider="acme", environ=_no_env(), config_dir=tmp_path)
    assert not profile_path_for("e2b", tmp_path).exists()


def test_incomplete_provider_is_an_error(tmp_path: Path) -> None:
    _write_profile(tmp_path, "e2b")
    (providers_dir(tmp_path)).mkdir(parents=True, exist_ok=True)
    (providers_dir(tmp_path) / "acme.json").write_text(json.dumps({
        "profile": "e2b", "llm": {"base_url": "http://x.example/v1"},
    }))
    with pytest.raises(ProviderError, match="llm.model is empty"):
        resolve_provider(provider="acme", environ=_no_env(), config_dir=tmp_path)


# --- embeddings on a different host ----------------------------------------

def test_embeddings_endpoint_passthrough(tmp_path: Path, real_profile_loader) -> None:
    _write_profile(tmp_path, "e2b")
    _write_provider(tmp_path, "acme", embeddings={
        "base_url": "http://embed.example:8085/v1", "model": "embed",
    })
    r = resolve_provider(provider="acme", environ=_no_env(), config_dir=tmp_path)
    cfg, _ = apply_to_config(Config(), r)
    assert cfg.memory.embeddings_base_url == "http://embed.example:8085/v1"
    assert cfg.memory.embeddings_model == "embed"
    assert cfg.model.base_url == "http://server-a.example:8086/v1"


# --- profile reuse across models (force semantics) --------------------------

def test_apply_profile_force_allows_model_reuse() -> None:
    prof = ModelProfile(model="gemma-old", max_tokens=777,
                        generation_timeout_seconds=42)
    cfg = Config()
    cfg.model.model = "gemma-new"

    # Ambient (orchestrator) path: name mismatch → ignored.
    new_cfg, overridden = apply_profile(cfg.model, prof)
    assert overridden == [] and new_cfg.max_tokens != 777

    # Explicit (provider) path: mismatch is intentional reuse → applied.
    new_cfg2, overridden2 = apply_profile(cfg.model, prof, force=True)
    assert new_cfg2.max_tokens == 777
    assert "max_tokens" in overridden2


def test_apply_to_config_sets_endpoint_and_profile_knobs(tmp_path: Path, real_profile_loader) -> None:
    _write_profile(tmp_path, "e2b")
    _write_provider(tmp_path, "acme")
    r = resolve_provider(provider="acme", environ=_no_env(), config_dir=tmp_path)
    cfg, knobs = apply_to_config(Config(), r)
    assert cfg.model.base_url == "http://server-a.example:8086/v1"
    assert cfg.model.model == "chat"
    assert cfg.model.max_tokens == 1234  # from the profile (forced)
    assert cfg.model.generation_timeout_seconds == 99
    assert "max_tokens" in knobs


def test_api_key_env_indirection(tmp_path: Path, real_profile_loader) -> None:
    _write_profile(tmp_path, "e2b")
    _write_provider(tmp_path, "acme")
    (providers_dir(tmp_path) / "acme.json").write_text(json.dumps({
        "profile": "e2b",
        "llm": {"base_url": "http://server-a.example:8086/v1",
                "model": "chat", "api_key_env": "ACME_KEY"},
    }))
    r = resolve_provider(
        provider="acme", environ={**_no_env(), "ACME_KEY": "sekrit"},
        config_dir=tmp_path,
    )
    assert r.provider.api_key == "sekrit"
    assert r.provider.provenance["api_key"] == "env:ACME_KEY"


def test_describe_mentions_endpoint_and_profile(tmp_path: Path, real_profile_loader) -> None:
    _write_profile(tmp_path, "e2b")
    _write_provider(tmp_path, "acme", embeddings={
        "base_url": "http://embed.example:8085/v1", "model": "embed",
    })
    r = resolve_provider(provider="acme", environ=_no_env(), config_dir=tmp_path)
    d = r.provider.describe()
    assert "endpoint:" in d and "profile=e2b" in d and "embeddings=embed@" in d

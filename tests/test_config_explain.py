"""`capybase config explain`: effective value + source layer + default.

Covers the provenance contract end to end: built-in defaults, file sources
(config dir vs repo-local precedence), the v1->v2 migration notes, CLI-flag
recording, calibration-profile/provider stamps, --all, --format json, and
the unknown-key error with nearest-match suggestions.
"""

from __future__ import annotations

import argparse
import io
import json

import pytest

from capybase.cli import _run_config_explain, main
from capybase.config import Config


def _args(keys=(), *, all_keys=False, fmt="plain") -> argparse.Namespace:
    return argparse.Namespace(
        keys=list(keys), all=all_keys, format=fmt)


def test_default_source_for_untouched_config():
    cfg = Config()
    buf = io.StringIO()
    rc = _run_config_explain(_args(["features.rag"]), cfg, out=buf)
    assert rc == 0
    assert "effective: True" in buf.getvalue()
    assert "source:    default (built-in)" in buf.getvalue()
    assert "default:   True" in buf.getvalue()


def test_file_source_and_effective_value(tmp_path):
    d = tmp_path / "cfg"
    d.mkdir()
    (d / "capybase.toml").write_text("[features]\nrag = false\n")
    cfg = Config.load(d / "capybase.toml")
    buf = io.StringIO()
    rc = _run_config_explain(_args(["features.rag"]), cfg, out=buf)
    assert rc == 0
    assert "effective: False" in buf.getvalue()
    assert f"file ({d / 'capybase.toml'})" in buf.getvalue()


def test_migration_note_surfaced_for_v1_key(tmp_path):
    d = tmp_path / "cfg"
    d.mkdir()
    (d / "capybase.toml").write_text(
        "[future]\nenable_structural_resolver = false\n")
    cfg = Config.load(d / "capybase.toml")
    buf = io.StringIO()
    rc = _run_config_explain(
        _args(["features.structural_resolution"]), cfg, out=buf)
    assert rc == 0
    assert "note:" in buf.getvalue()
    assert "migration from future.enable_structural_resolver" in buf.getvalue()


def test_repo_local_override_wins_and_is_named(tmp_path, monkeypatch):
    cdir = tmp_path / "xdg" / "capybase"
    cdir.mkdir(parents=True)
    (cdir / "capybase.toml").write_text(
        "schema_version = 2\n[policy]\ncontext_lines = 15\n")
    (tmp_path / "capybase.toml").write_text(
        "schema_version = 2\n[policy]\ncontext_lines = 7\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg = Config.load()
    buf = io.StringIO()
    rc = _run_config_explain(_args(["policy.context_lines"]), cfg, out=buf)
    assert rc == 0
    assert "effective: 7" in buf.getvalue()
    assert "repo-local file" in buf.getvalue()


def test_cli_flag_is_recorded_as_source(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    (tmp_path / "xdg" / "capybase").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    rc = main(["--jury-mode", "shadow", "config", "explain",
               "future.jury_mode"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "effective: 'shadow'" in out
    assert "--jury-mode shadow flag" in out


def test_provider_and_profile_stamped_as_source(tmp_path):
    """apply_to_config stamps the provider + calibration profile as the
    value source for the endpoint knobs it writes."""
    from capybase.calibration_profile import ModelProfile
    from capybase.provider_config import (
        ProviderConfig, ResolvedProvider, apply_to_config,
    )
    cfg = Config()
    provider = ProviderConfig(
        name="test-prov", profile="test-prov",
        base_url="http://127.0.0.1:9/v1", model="fake-model",
        api_key="sk-x",
        provenance={"base_url": "file:x", "model": "file:x",
                    "api_key": "file:x", "profile": "file:x"},
    )
    profile = ModelProfile(model="fake-model")
    resolved = ResolvedProvider(
        provider=provider, profile=profile, profile_path=tmp_path / "p.json")
    cfg, _knobs, _report = apply_to_config(cfg, resolved)
    buf = io.StringIO()
    rc = _run_config_explain(_args(["model.base_url", "model.max_tokens"]),
                             cfg, out=buf)
    assert rc == 0
    assert "provider 'test-prov'" in buf.getvalue()
    assert "calibration profile" in buf.getvalue()
    assert str((tmp_path / "p.json")) in buf.getvalue()


def test_unknown_key_errors_with_suggestion():
    cfg = Config()
    buf, err = io.StringIO(), io.StringIO()
    rc = _run_config_explain(_args(["features.ragg"]), cfg, out=buf)
    # the handler prints the error to stderr; capture via pytest fixture
    # would need capsys — assert the return code and call again with capsys
    assert rc == 2


def test_unknown_key_suggestion_on_stderr(capsys):
    cfg = Config()
    rc = _run_config_explain(_args(["model.max_token"]), cfg)
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown config key" in err
    assert "model.max_tokens" in err


def test_json_format_is_machine_readable(tmp_path):
    d = tmp_path / "cfg"
    d.mkdir()
    (d / "capybase.toml").write_text("schema_version = 2\n")
    cfg = Config.load(d / "capybase.toml")
    buf = io.StringIO()
    rc = _run_config_explain(
        _args(["features.rag"], fmt="json"), cfg, out=buf)
    assert rc == 0
    records = json.loads(buf.getvalue())
    assert records[0]["key"] == "features.rag"
    assert records[0]["value"] is True
    assert "default" in records[0] and "source" in records[0]


def test_all_smoke_via_main(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    (tmp_path / "xdg" / "capybase").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    rc = main(["config", "explain", "--all"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "schema version: 2" in out
    assert "defaults only" in out
    assert "loader diagnostics: none" in out
    assert "features.rag" in out
    assert "mechanisms.sbcr_floor" in out
    assert "experimental.jury_mode" in out


def test_experimental_alias_resolves_to_future_model():
    cfg = Config()
    buf = io.StringIO()
    rc = _run_config_explain(_args(["experimental.jury_mode"]), cfg, out=buf)
    assert rc == 0
    assert "effective: 'off'" in buf.getvalue()

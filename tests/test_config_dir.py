"""Config-dir resolution: config + calibration read from a shared directory.

capybase reads ``capybase.toml`` and the calibration artifacts
(``model_profile.json``, ``calibration.json``) from a config dir (default
``~/.config/capybase``), so the user repo need not carry any capybase config.
These tests pin the precedence and path-relocation contract of ``Config.load``.

Resolution order (highest precedence first):
  1. explicit ``path`` file (direct/test use)
  2. repo-local ``./capybase.toml`` (cwd)
  3. ``<config_dir>/capybase.toml``
  4. built-in defaults

The calibration paths are relocated to the config dir (machine/user-specific,
shared across repos) UNLESS the toml sets an explicit path (a deliberate
override). The RAG experience store stays repo-relative.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from capybase.config import Config, default_config_dir


def test_config_dir_toml_loaded_when_no_repo_toml(tmp_path, monkeypatch):
    """A config-dir ``capybase.toml`` is loaded when the repo (cwd) has none."""
    cdir = tmp_path / "cfg"
    cdir.mkdir()
    (cdir / "capybase.toml").write_text('[model]\nmodel = "from-dir"\n')
    # cwd is a repo with no toml at all
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    cfg = Config.load(config_dir=cdir)
    assert cfg.model.model == "from-dir"
    assert cfg.source_path == str(cdir / "capybase.toml")


def test_repo_toml_beats_config_dir(tmp_path, monkeypatch):
    """A repo-local ``./capybase.toml`` (cwd) wins over the config dir's."""
    cdir = tmp_path / "cfg"
    cdir.mkdir()
    (cdir / "capybase.toml").write_text('[model]\nmodel = "from-cfgdir"\n')
    # cwd has its own toml — the per-repo override.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "capybase.toml").write_text('[model]\nmodel = "from-repo"\n')
    monkeypatch.chdir(repo)
    cfg = Config.load(config_dir=cdir)
    assert cfg.model.model == "from-repo"
    assert cfg.source_path == str(repo / "capybase.toml")


def test_calibration_paths_resolve_to_config_dir(tmp_path, monkeypatch):
    """model_profile_path + calibration_path relocate to the config dir.

    These are the defaults (``.rebase-agent/memory/...``), which capybase
    rewrites to live in the config dir so the user repo need not duplicate them.
    """
    cdir = tmp_path / "cfg"
    cdir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    cfg = Config.load(config_dir=cdir)
    assert cfg.calibration.model_profile_path == str(cdir / "model_profile.json")
    assert cfg.calibration.model_path == str(cdir / "calibration.json")


def test_explicit_toml_path_respected(tmp_path, monkeypatch):
    """An explicit calibration path in the toml is NOT rewritten to the config dir.

    A user who sets ``model_profile_path`` explicitly is making a deliberate
    choice (a custom location); capybase must respect it rather than clobber it.
    """
    cdir = tmp_path / "cfg"
    cdir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    toml = repo / "capybase.toml"
    toml.write_text(
        '[calibration]\n'
        'model_profile_path = "/abs/custom-profile.json"\n'
        'model_path = "/abs/custom-calib.json"\n'
    )
    cfg = Config.load(config_dir=cdir)
    assert cfg.calibration.model_profile_path == "/abs/custom-profile.json"
    assert cfg.calibration.model_path == "/abs/custom-calib.json"


def test_explicit_path_file_still_works(tmp_path, monkeypatch):
    """The explicit ``path`` arg (a file) is preserved for direct/test use.

    ``Config.load(some_file)`` keeps working — the config-dir machinery only
    engages when ``path`` is None. This guards the internal API used by tests
    and any programmatic callers.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)  # ensure no repo-local toml interferes
    toml = tmp_path / "direct.toml"
    toml.write_text('[model]\nmodel = "direct"\nsamples = 7\n')
    cfg = Config.load(toml)
    assert cfg.model.model == "direct"
    assert cfg.model.samples == 7
    assert cfg.source_path == str(toml)
    # Calibration paths still relocate to the default config dir even when a
    # file path is given directly (the relocation is independent of how the toml
    # was found).
    assert cfg.calibration.model_profile_path == str(
        default_config_dir() / "model_profile.json"
    )


def test_xdg_config_home_respected(tmp_path, monkeypatch):
    """``XDG_CONFIG_HOME`` sets the default config dir, per the XDG spec."""
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert default_config_dir() == xdg / "capybase"
    # And a toml there is picked up when no --config and no repo toml.
    (xdg / "capybase").mkdir(parents=True)
    (xdg / "capybase" / "capybase.toml").write_text('[model]\nmodel = "xdg"\n')
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    cfg = Config.load()  # no config_dir → default_config_dir() → XDG
    assert cfg.model.model == "xdg"


def test_defaults_when_nothing_present(tmp_path, monkeypatch):
    """No repo toml, no config-dir toml → built-in defaults; paths still relocate."""
    cdir = tmp_path / "cfg"
    cdir.mkdir()  # exists but empty
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    cfg = Config.load(config_dir=cdir)
    assert cfg.model.model == "vibethink"  # built-in default
    assert cfg.source_path is None
    # Calibration paths still point at the config dir (relocated defaults).
    assert cfg.calibration.model_profile_path == str(cdir / "model_profile.json")


def test_config_dir_relative_path_expanded(tmp_path, monkeypatch):
    """A ``--config`` value with ``~`` is expanded (user dir)."""
    # default_config_dir() handles the no-arg case; here we verify Config.load
    # expands a ~ in config_dir. Use monkeypatch to make ~ deterministic.
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    # Pass a ~-relative dir; Config.load must expand it.
    cfg = Config.load(config_dir="~/myconfig")
    assert cfg.calibration.model_profile_path == str(
        (tmp_path / "myconfig" / "model_profile.json")
    )


# ---------------------------------------------------------------------------
# Repo-local override merged ONTO config-dir config (not replacing it)
# ---------------------------------------------------------------------------


def test_repo_local_override_merges_onto_config_dir(tmp_path, monkeypatch):
    """A repo-local override that sets ONLY one section must NOT drop the other
    sections from the config-dir toml.

    Regression: a repo with only ``[tests]`` in ``capybase.local.toml`` used to
    shadow the entire config-dir toml, falling back to the built-in [model]
    defaults (wrong endpoint/model). The override is now deep-merged onto the
    config-dir config so unspecified sections inherit from it.
    """
    cdir = tmp_path / "cfg"
    cdir.mkdir()
    (cdir / "capybase.toml").write_text(
        '[model]\nmodel = "chat"\nbase_url = "http://desktop:8085/v1"\n'
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    # The repo override sets ONLY [tests]; it must not clobber [model].
    (repo / "capybase.local.toml").write_text(
        '[tests]\npre_continue = "true"\nrequired = false\n'
    )
    monkeypatch.chdir(repo)
    cfg = Config.load(config_dir=cdir)
    # [tests] from the override wins.
    assert cfg.tests.pre_continue == "true"
    assert cfg.tests.required is False
    # [model] is INHERITED from the config-dir toml (NOT the built-in default).
    assert cfg.model.model == "chat"
    assert cfg.model.base_url == "http://desktop:8085/v1"
    # source_path is the override (the file that was loaded last / wins).
    assert cfg.source_path == str((repo / "capybase.local.toml").resolve())


def test_repo_local_override_deep_merges_nested_section(tmp_path, monkeypatch):
    """A nested-table override merges field-by-field, not section-by-section."""
    cdir = tmp_path / "cfg"
    cdir.mkdir()
    (cdir / "capybase.toml").write_text(
        '[model]\nmodel = "chat"\ntemperature = 0.2\nmax_tokens = 8192\n'
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    # Override only `temperature` within [model]; `model` and `max_tokens` inherit.
    (repo / "capybase.local.toml").write_text('[model]\ntemperature = 0.5\n')
    monkeypatch.chdir(repo)
    cfg = Config.load(config_dir=cdir)
    assert cfg.model.temperature == 0.5  # override wins
    assert cfg.model.model == "chat"      # inherited
    assert cfg.model.max_tokens == 8192   # inherited

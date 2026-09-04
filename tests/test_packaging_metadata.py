"""Packaging-metadata drift guards.

The repo is Apache-2.0 (LICENSE + NOTICE + README) but pyproject.toml
briefly declared MIT — incorrect package metadata and downstream
ambiguity. These tests pin the metadata to the license files so the
contradiction cannot reappear, and pin the dev extra to the documented
test command (README: `pytest tests/ -n 6` requires pytest-xdist).
"""

from __future__ import annotations

from pathlib import Path

import tomllib

_REPO = Path(__file__).resolve().parent.parent


def _pyproject() -> dict:
    with (_REPO / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)


def test_license_metadata_matches_license_file():
    """pyproject's SPDX expression agrees with the LICENSE file (Apache-2.0)."""
    proj = _pyproject()["project"]
    assert proj["license"] == "Apache-2.0", (
        f"pyproject declares {proj['license']!r}; LICENSE/README are Apache-2.0"
    )
    license_text = (_REPO / "LICENSE").read_text()[:400].lower()
    assert "apache license" in license_text
    assert "version 2" in license_text


def test_license_files_declared_and_present():
    proj = _pyproject()["project"]
    assert proj["license-files"] == ["LICENSE", "NOTICE"]
    for name in proj["license-files"]:
        assert (_REPO / name).is_file(), f"declared license file {name} missing"


def test_dev_extra_supports_documented_test_command():
    """README documents `pytest tests/ -n 6` — the dev extra must install
    pytest-xdist (a fresh contributor following the docs gets the documented
    command)."""
    dev = _pyproject()["project"]["optional-dependencies"]["dev"]
    assert any("pytest-xdist" in d for d in dev), (
        "pytest-xdist missing from dev: the documented -n 6 command would fail"
    )
    assert any("pytest>=" in d or d.startswith("pytest") for d in dev)


def test_no_empty_extras():
    """An empty extra installs nothing while implying functionality — the
    `structural = []` placeholder was removed; the grammar-free abstract
    parser is built in. Keep extras meaningful or absent."""
    extras = _pyproject()["project"]["optional-dependencies"]
    for name, deps in extras.items():
        assert deps, f"optional-dependency {name!r} is empty — remove it"


def test_config_file_path_fails_loudly(tmp_path, monkeypatch):
    """--config naming a FILE must refuse, not silently default (the live
    tier-B smoke lost its overrides this way; s27-extend-41)."""
    from capybase.config import Config

    f = tmp_path / "capybase.toml"
    f.write_text("[tests]\npre_continue = 'echo hi'\n")
    try:
        Config.load(config_dir=f)
    except NotADirectoryError as exc:
        assert "DIRECTORY" in str(exc)
        assert str(f) in str(exc)
    else:
        raise AssertionError("file-path --config must raise")

    # The correct contract still works: the directory. chdir to tmp so
    # no repo-local ./capybase.toml overrides it (repo-local precedence)
    # — and drop the file written above, which would BE that override.
    f.unlink()
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "cfgdir"
    d.mkdir()
    (d / "capybase.toml").write_text(
        "[tests]\npre_continue = 'echo hi'\n")
    cfg = Config.load(config_dir=d)
    assert cfg.tests.pre_continue == "echo hi"

    # An absent dir stays "no config" (first-run semantics unchanged).
    cfg2 = Config.load(config_dir=tmp_path / "does-not-exist")
    assert cfg2.tests.pre_continue != "echo hi"

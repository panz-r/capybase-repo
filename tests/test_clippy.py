"""Tests for the opt-in Clippy lint check (cargo clippy) in Phase B.

Clippy is a quality check (not a compile check — the cargo floor already
proved the merge compiles). These verify: a merge that INTRODUCES a clippy
finding is flagged; the baseline excludes pre-existing findings; error vs
warning severity; the disabled state; and the loose-file/no-cargo no-op.
Cargo-backed tests skip on CI without cargo.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from capybase.verification import ValidationConfig, VerificationEngine

CARGO = shutil.which("cargo")
skip_no_cargo = pytest.mark.skipif(CARGO is None, reason="cargo not installed")

_CRATE_LIB = "pub mod config;\n"
_CRATE_CONFIG = (
    "pub struct Config { pub port: u16 }\n"
    "impl Config { pub fn new() -> Self { Config { port: 8080 } } }\n"
)


@pytest.fixture
def clippy_crate(tmp_path: Path) -> Path:
    """A minimal cargo crate for clippy tests."""
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "clippytest"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text(_CRATE_LIB)
    (src / "config.rs").write_text(_CRATE_CONFIG)
    return tmp_path


def _span(original: str) -> tuple[int, int]:
    lines = original.split("\n")
    start = next(i for i, l in enumerate(lines) if l.startswith("<<<<<<<"))
    end = next(i for i, l in enumerate(lines) if l.startswith(">>>>>>>"))
    return (start, end)


# A conflict where the correct merge compiles cleanly, but a merge that adds
# `+ 0` introduces a clippy identity_op finding.
def _config_conflict() -> str:
    return (
        "pub struct Config { pub port: u16 }\n"
        "impl Config {\n"
        "<<<<<<< H\n"
        "    pub fn new() -> Self { Config { port: 8080 } }\n"
        "=======\n"
        "    pub fn new() -> Self { Config { port: 9090 } }\n"
        ">>>>>>> b\n"
        "}\n"
    )


@skip_no_cargo
def test_clippy_flags_new_finding_at_warning(clippy_crate):
    """A merge introducing a clippy finding is flagged but not hard-rejected."""
    finding = "    pub fn new() -> Self { Config { port: 8080 + 0 } }"
    eng = VerificationEngine.default(
        ValidationConfig(enable_clippy=True, clippy_severity="warning")
    )
    res = eng.verify_file(
        "src/config.rs", "rust", _config_conflict(),
        [(_span(_config_conflict()), finding)], repo_root=str(clippy_crate),
    )
    # Compiles fine (cargo floor passed)...
    assert res.features["syntax_passed"] is True
    # ...but clippy found the introduced identity_op.
    assert res.features["clippy_checked"] is True
    assert res.features["clippy_new_finding_count"] >= 1
    # Warning severity → no hard failure (the merge compiles).
    assert not any(f.validator == "clippy" for f in res.hard_failures)


@skip_no_cargo
def test_clippy_error_severity_hard_rejects(clippy_crate):
    """At error severity, a lint-introducing merge is hard-rejected."""
    finding = "    pub fn new() -> Self { Config { port: 8080 + 0 } }"
    eng = VerificationEngine.default(
        ValidationConfig(enable_clippy=True, clippy_severity="error")
    )
    res = eng.verify_file(
        "src/config.rs", "rust", _config_conflict(),
        [(_span(_config_conflict()), finding)], repo_root=str(clippy_crate),
    )
    assert not res.passed
    assert any(f.validator == "clippy" for f in res.hard_failures)


@skip_no_cargo
def test_clippy_clean_merge_no_findings(clippy_crate):
    """A merge that introduces no new clippy finding passes cleanly."""
    clean = "    pub fn new() -> Self { Config { port: 9090 } }"
    eng = VerificationEngine.default(
        ValidationConfig(enable_clippy=True, clippy_severity="error")
    )
    res = eng.verify_file(
        "src/config.rs", "rust", _config_conflict(),
        [(_span(_config_conflict()), clean)], repo_root=str(clippy_crate),
    )
    assert res.features["clippy_checked"] is True
    assert res.features["clippy_new_finding_count"] == 0
    assert res.passed, [f.message for f in res.hard_failures]


def test_clippy_disabled_is_not_checked(clippy_crate):
    """When enable_clippy is off, clippy is not checked (default state)."""
    finding = "    pub fn new() -> Self { Config { port: 8080 + 0 } }"
    eng = VerificationEngine.default(ValidationConfig())  # clippy off
    res = eng.verify_file(
        "src/config.rs", "rust", _config_conflict(),
        [(_span(_config_conflict()), finding)], repo_root=str(clippy_crate),
    )
    assert res.features["clippy_checked"] is False
    assert res.features["clippy_new_finding_count"] == 0
    assert not any(f.validator == "clippy" for f in res.hard_failures)


@skip_no_cargo
def test_clippy_no_cargo_manifest_is_noop(tmp_path):
    """A loose .rs with no Cargo.toml → clippy can't run → not checked."""
    # No Cargo.toml written.
    eng = VerificationEngine.default(ValidationConfig(enable_clippy=True))
    res = eng.verify_file(
        "cfg.rs", "rust", "pub fn x() {}\n", [], repo_root=str(tmp_path)
    )
    assert res.features["clippy_checked"] is False


def test_clippy_not_run_for_python(tmp_path):
    """Clippy is a Rust-only check; Python files never invoke it."""
    (tmp_path / "Cargo.toml").write_text('[package]\nname="x"\nversion="0.1.0"\n')
    eng = VerificationEngine.default(ValidationConfig(enable_clippy=True))
    res = eng.verify_file(
        "app.py", "python", "def f():\n    return 1\n", [], repo_root=str(tmp_path)
    )
    # The clippy gate is `if language == 'rust'`, so for Python the check is
    # never invoked — the key is absent (not checked, not a failure).
    assert not res.features.get("clippy_checked")
    assert not any(f.validator == "clippy" for f in res.hard_failures)

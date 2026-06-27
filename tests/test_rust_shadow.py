"""Tests for Rust shadow-test discovery and the cargo-test runner.

Exercises ``_locate_shadow_test`` (the new ``(target, language)`` contract)
and ``_run_rust_shadow_test`` (real cargo, skipped when absent). The dispatch
into ``_run_shadow_tests`` is covered via a stubbed runner.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from capybase.verification import (
    ValidationConfig,
    VerificationEngine,
    _locate_shadow_test,
    _run_rust_shadow_test,
)

CARGO = shutil.which("cargo")
skip_no_cargo = pytest.mark.skipif(CARGO is None, reason="cargo not installed")


# ---------------------------------------------------------------------------
# _locate_shadow_test dispatch
# ---------------------------------------------------------------------------


def test_locate_python_returns_tuple(tmp_path):
    (tmp_path / "tests").mkdir()
    tf = tmp_path / "tests" / "test_app.py"
    tf.write_text("def test_x(): pass\n")
    assert _locate_shadow_test("app.py", str(tmp_path)) == (str(tf), "python")


def test_locate_python_none_when_absent(tmp_path):
    assert _locate_shadow_test("missing.py", str(tmp_path)) is None


def test_locate_rust_none_without_cargo_toml(tmp_path):
    # A .rs file in a non-cargo project → nothing to run.
    assert _locate_shadow_test("src/config.rs", str(tmp_path)) is None


def test_locate_rust_returns_empty_target_with_cargo(tmp_path):
    # Rust runs the whole cargo test suite (no per-module filter) — the
    # reliable choice given crate-structure-dependent test paths.
    (tmp_path / "Cargo.toml").write_text('[package]\nname="x"\nversion="0"\n')
    assert _locate_shadow_test("src/config.rs", str(tmp_path)) == ("", "rust")


def test_locate_unknown_extension_none(tmp_path):
    assert _locate_shadow_test("README.md", str(tmp_path)) is None


# ---------------------------------------------------------------------------
# _run_rust_shadow_test (real cargo)
# ---------------------------------------------------------------------------


def _make_cargo_crate(root: Path, *, failing: bool = False) -> None:
    """Write a minimal cargo crate with one passing (or failing) test."""
    (root / "Cargo.toml").write_text(
        '[package]\nname = "shadowtest"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    (root / "src").mkdir(parents=True)
    assertion = "assert!(false)" if failing else "assert!(true)"
    (root / "src" / "lib.rs").write_text(
        "pub struct Config { pub retries: u32 }\n"
        "impl Config { pub fn new() -> Self { Config { retries: 3 } } }\n"
        "\n"
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    use super::*;\n"
        f"    #[test]\n    fn it_works() {{ let _c = Config::new(); {assertion}; }}\n"
        "}\n"
    )


@skip_no_cargo
def test_run_rust_shadow_passing(tmp_path):
    _make_cargo_crate(tmp_path)
    passed, rc, target = _run_rust_shadow_test("", str(tmp_path))
    assert passed is True
    assert rc == 0
    assert target == ""


@skip_no_cargo
def test_run_rust_shadow_failing(tmp_path):
    _make_cargo_crate(tmp_path, failing=True)
    passed, rc, _ = _run_rust_shadow_test("", str(tmp_path))
    assert passed is False
    assert rc != 0


def test_run_rust_shadow_missing_cargo_none(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    passed, rc, _ = _run_rust_shadow_test("", str(tmp_path))
    assert passed is None  # cargo absent → "not run", not a failure
    assert rc == -1


# ---------------------------------------------------------------------------
# _run_shadow_tests dispatch (engine integration, no real subprocess)
# ---------------------------------------------------------------------------


def test_shadow_tests_rust_dispatch_calls_rust_runner(tmp_path, monkeypatch):
    """When a Rust test target is located, the engine dispatches to
    _run_rust_shadow_test and records the outcome in features."""
    (tmp_path / "Cargo.toml").write_text('[package]\nname="x"\nversion="0"\n')
    calls: list[str] = []

    def fake_runner(target, repo_root, *, timeout=180):
        calls.append(target)
        return (False, 1, target)  # simulate a failing test

    monkeypatch.setattr(
        "capybase.verification._run_rust_shadow_test", fake_runner
    )
    cfg = ValidationConfig(enable_shadow_tests=True)
    eng = VerificationEngine.default(cfg)
    features: dict = {}
    hard: list = []
    eng._run_shadow_tests("src/config.rs", "pub fn x() {}", str(tmp_path), hard, features)
    assert calls == [""]
    assert features["shadow_tests_run"] is True
    assert features["shadow_tests_passed"] is False
    assert any(f.validator == "shadow_tests" for f in hard)


def test_shadow_tests_rust_none_when_no_cargo_project(tmp_path):
    # No Cargo.toml → _locate_shadow_test returns None → no run recorded.
    cfg = ValidationConfig(enable_shadow_tests=True)
    eng = VerificationEngine.default(cfg)
    features: dict = {}
    hard: list = []
    eng._run_shadow_tests("src/config.rs", "pub fn x() {}", str(tmp_path), hard, features)
    assert features.get("shadow_tests_run") is not True
    assert hard == []


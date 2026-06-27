"""Tests for the cargo-driven Rust compile check (the default for cargo projects).

Round 1 used standalone ``rustc --emit=metadata`` on the resolved file in
isolation, which FALSE-POSITIVES on any leaf file using ``crate::`` / ``super::``
(standalone rustc can't resolve crate-relative paths → E0432). These tests
verify the fix: in a cargo project, ``cargo check`` (crate-aware) is the default
syntax check, so a valid ``crate::``-using leaf file passes, while standalone
rustc is reserved for loose ``.rs`` files with no Cargo.toml.

The cargo-backed tests skip when cargo is absent (CI without a toolchain).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from capybase.verification import ValidationConfig, VerificationEngine

CARGO = shutil.which("cargo")
RUSTC = shutil.which("rustc")
skip_no_cargo = pytest.mark.skipif(CARGO is None, reason="cargo not installed")
skip_no_rustc = pytest.mark.skipif(RUSTC is None, reason="rustc not installed")


# A two-file crate where the leaf (server.rs) uses crate:: — the exact pattern
# standalone rustc cannot check (it false-positives with E0432).
_CRATE_LIB = "pub mod config;\npub mod server;\n"
_CRATE_CONFIG = (
    "pub struct Config { pub port: u16 }\n"
    "impl Config { pub fn new() -> Self { Config { port: 8080 } } }\n"
)


@pytest.fixture
def cargo_crate(tmp_path: Path) -> Path:
    """A minimal two-module cargo crate for crate-aware verification tests."""
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "verifytest"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text(_CRATE_LIB)
    (src / "config.rs").write_text(_CRATE_CONFIG)
    # server.rs is written per-test by the test (the file under resolution).
    return tmp_path


def _write_server(crate_root: Path, body: str) -> None:
    (crate_root / "src" / "server.rs").write_text(body)


# A conflict in the leaf server.rs. Both sides are simple let-bindings; the
# correct merge keeps both. Crucially the file uses ``use crate::config::Config``
# — the pattern standalone rustc rejects.
def _server_conflict() -> str:
    return (
        "use crate::config::Config;\n"
        "pub fn serve(c: Config) {\n"
        "<<<<<<< H\n"
        "    let a = 1;\n"
        "=======\n"
        "    let b = 2;\n"
        ">>>>>>> b\n"
        "    let _ = (c, a, b);\n"
        "}\n"
    )


def _span(original: str) -> tuple[int, int]:
    lines = original.split("\n")
    start = next(i for i, l in enumerate(lines) if l.startswith("<<<<<<<"))
    end = next(i for i, l in enumerate(lines) if l.startswith(">>>>>>>"))
    return (start, end)


# ---------------------------------------------------------------------------
# The false-positive regression (the core fix)
# ---------------------------------------------------------------------------


@skip_no_cargo
def test_valid_crate_path_leaf_passes(cargo_crate):
    """A valid merge of a leaf file using ``crate::`` must PASS.

    This is the round-1 false-positive: standalone rustc fails this with
    E0432 (unresolved import ``crate::config``), but the merge is correct.
    Cargo check (crate-aware) accepts it.
    """
    _write_server(cargo_crate, _server_conflict())
    correct = "    let a = 1;\n    let b = 2;"
    eng = VerificationEngine.default(ValidationConfig())
    res = eng.verify_file(
        "src/server.rs", "rust", _server_conflict(), [(_span(_server_conflict()), correct)],
        repo_root=str(cargo_crate),
    )
    assert res.passed, [f.message for f in res.hard_failures]
    assert res.features["syntax_checked"] is True
    assert res.features["syntax_passed"] is True
    assert res.features["syntax_tool"] == "cargo"


@skip_no_cargo
def test_cargo_catches_introduced_error(cargo_crate):
    """A merge that introduces a real error (undefined name) is caught by cargo."""
    _write_server(cargo_crate, _server_conflict())
    broken = "    let a = 1;\n    let b = zzz;"  # zzz undefined
    eng = VerificationEngine.default(ValidationConfig())
    res = eng.verify_file(
        "src/server.rs", "rust", _server_conflict(), [(_span(_server_conflict()), broken)],
        repo_root=str(cargo_crate),
    )
    assert not res.passed
    syntax_fails = [f for f in res.hard_failures if f.validator == "syntax"]
    assert len(syntax_fails) == 1
    assert "zzz" in syntax_fails[0].message or "cannot find" in syntax_fails[0].message


@skip_no_cargo
def test_cargo_ignores_preexisting_errors(cargo_crate):
    """Pre-existing crate errors (not introduced by the merge) don't fail it.

    A repo that already doesn't compile is the developer's problem, not the
    merge's. The baseline comparison excludes errors present before the merge.
    """
    # Introduce a pre-existing error in config.rs (unrelated to the merge).
    _write_server(cargo_crate, _server_conflict())
    (cargo_crate / "src" / "config.rs").write_text(
        _CRATE_CONFIG + "\npub fn _unused() -> u32 { undefined_symbol }\n"
    )
    correct = "    let a = 1;\n    let b = 2;"
    eng = VerificationEngine.default(ValidationConfig())
    res = eng.verify_file(
        "src/server.rs", "rust", _server_conflict(), [(_span(_server_conflict()), correct)],
        repo_root=str(cargo_crate),
    )
    # The server.rs merge itself is valid; the pre-existing config.rs error is
    # in the baseline and excluded. So no NEW syntax failure from the merge.
    syntax_fails = [f for f in res.hard_failures if f.validator == "syntax"]
    assert syntax_fails == [], [f.message for f in syntax_fails]


# ---------------------------------------------------------------------------
# Loose-file fallback (no Cargo.toml → standalone rustc)
# ---------------------------------------------------------------------------


@skip_no_rustc
def test_loose_rust_file_uses_standalone_rustc(tmp_path):
    """A loose ``.rs`` with no Cargo.toml falls back to standalone rustc.

    The rust-uu fixture and single-file scripts have no crate context, so
    standalone rustc is the correct (and only) check here.
    """
    # No Cargo.toml written → loose file.
    conflict = (
        "pub fn greet(name: &str) -> String {\n"
        "<<<<<<< H\n"
        '    format!("hi {}", name)\n'
        "=======\n"
        '    format!("howdy {}", name)\n'
        ">>>>>>> b\n"
        "}\n"
    )
    correct = '    format!("hi and howdy {}", name)'
    eng = VerificationEngine.default(ValidationConfig())
    res = eng.verify_file(
        "cfg.rs", "rust", conflict, [(_span(conflict), correct)],
        repo_root=str(tmp_path),
    )
    assert res.passed, [f.message for f in res.hard_failures]
    assert res.features["syntax_checked"] is True
    # Loose file → no cargo; standalone rustc ran (no syntax_tool=cargo key).
    assert res.features.get("syntax_tool") != "cargo"


@skip_no_rustc
def test_loose_rust_file_rejects_noncompiling(tmp_path):
    conflict = (
        "pub fn greet() {\n"
        "<<<<<<< H\n"
        '    println!("hi")\n'
        "=======\n"
        '    println!("howdy")\n'
        ">>>>>>> b\n"
        "}\n"
    )
    broken = '    println!("hi"'  # unclosed macro
    eng = VerificationEngine.default(ValidationConfig())
    res = eng.verify_file(
        "cfg.rs", "rust", conflict, [(_span(conflict), broken)],
        repo_root=str(tmp_path),
    )
    assert not res.passed
    assert any(f.validator == "syntax" for f in res.hard_failures)


# ---------------------------------------------------------------------------
# Graceful degrade: no toolchain at all
# ---------------------------------------------------------------------------


def test_no_toolchain_is_not_checked(monkeypatch, tmp_path):
    """When neither cargo nor rustc is available, syntax is not checked."""
    import capybase.adapters.lsp as lsp_mod

    monkeypatch.setattr(lsp_mod, "_resolve", lambda cmd: None)
    monkeypatch.setattr(lsp_mod, "_has_cargo_manifest", lambda root: False)
    eng = VerificationEngine.default(ValidationConfig())
    res = eng.verify_file(
        "cfg.rs", "rust", "pub fn x() {}\n", [], repo_root=str(tmp_path)
    )
    assert res.features["syntax_checked"] is False
    assert not any(f.validator == "syntax" for f in res.hard_failures)


def test_cargo_project_without_cargo_binary_uses_rustc(monkeypatch, tmp_path):
    """A cargo project where the cargo binary is missing falls back to rustc.

    This keeps verification working (via standalone rustc) even when cargo
    isn't installed but rustc is — at the cost of the crate-aware check. The
    fallback is correct for the crate-root file but may miss crate-context
    issues on leaves; it's the best available when cargo is absent.
    """
    (tmp_path / "Cargo.toml").write_text('[package]\nname="x"\nversion="0"\n')
    import capybase.adapters.lsp as lsp_mod

    # cargo resolves to None (absent); rustc resolves to a sentinel truthy.
    real_resolve = lsp_mod._resolve

    def fake_resolve(cmd):
        if "cargo" in cmd:
            return None
        return real_resolve(cmd)

    monkeypatch.setattr(lsp_mod, "_resolve", fake_resolve)
    # The _run_cargo_syntax_check also constructs its own runner; neuter cargo
    # there too by making _has_cargo_manifest's gate pass but the runner report
    # checked=False (cargo absent). Simplest: stub the runner.
    monkeypatch.setattr(
        "capybase.adapters.lsp.RustAnalyzerRunner.check",
        lambda self, source, *, path, repo_root: lsp_mod.Diagnostics(checked=False, tool="cargo"),
    )
    conflict = (
        "pub fn x() -> u32 {\n<<<<<<< H\n    1\n=======\n    2\n>>>>>>> b\n}\n"
    )
    eng = VerificationEngine.default(ValidationConfig())
    res = eng.verify_file(
        "lib.rs", "rust", conflict, [(_span(conflict), "    3")],
        repo_root=str(tmp_path),
    )
    # cargo didn't run (checked=False) → standalone rustc fallback engaged.
    assert res.features["syntax_checked"] is True

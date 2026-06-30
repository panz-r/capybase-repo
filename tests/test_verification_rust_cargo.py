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
# Workspace layout: no root Cargo.toml, the crate lives in a subdirectory
# (the di-rac-rebase-test shape). Without nearest-manifest detection this falls
# back to standalone rustc, which false-positives on ``crate::`` paths.
# ---------------------------------------------------------------------------


@skip_no_cargo
def test_workspace_subdir_crate_uses_cargo_not_rustc(tmp_path):
    """A crate in a subdirectory (no root Cargo.toml) still uses cargo check.

    Regression for the workspace false-positive: ``_has_cargo_manifest(repo_root)``
    alone returns False when the Cargo.toml is in a member crate's subdir, so the
    cargo check was skipped and standalone rustc rejected every ``crate::``-using
    leaf with E0433. The fix walks to the nearest manifest from the file's path.
    """
    from capybase.adapters.lsp import (
        _has_cargo_manifest,
        nearest_cargo_manifest_dir,
    )

    # Repo root has NO Cargo.toml; the crate lives in member/.
    member = tmp_path / "member"
    (member / "src").mkdir(parents=True)
    (member / "Cargo.toml").write_text(
        '[package]\nname = "wsmember"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    (member / "src" / "lib.rs").write_text("pub mod config;\npub mod leaf;\n")
    (member / "src" / "config.rs").write_text(_CRATE_CONFIG)
    leaf_path = "member/src/leaf.rs"

    # Root has no manifest — the old check would miss this and use rustc.
    assert not _has_cargo_manifest(str(tmp_path))
    # nearest-manifest walk finds member/Cargo.toml.
    assert nearest_cargo_manifest_dir(str(tmp_path), leaf_path) == member.resolve()

    # A valid leaf using crate::config — standalone rustc would reject this.
    correct = (
        "use crate::config::Config;\n"
        "pub fn leaf(c: Config) -> u16 { c.port }\n"
    )
    eng = VerificationEngine.default(ValidationConfig())
    res = eng.verify_file(
        leaf_path, "rust", correct, [],  # no conflict; whole file is the resolved text
        repo_root=str(tmp_path),
    )
    assert res.passed, [f.message for f in res.hard_failures]
    # Cargo ran (crate-aware), not standalone rustc.
    assert res.features.get("syntax_tool") == "cargo"
    assert res.features.get("syntax_checked") is True


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


# ---------------------------------------------------------------------------
# Cargo.toml manifest verification (closes the manifest-verification gap)
# ---------------------------------------------------------------------------
#
# Cargo.toml is classified ``"toml"`` by detect_language (not ``"rust"``), so a
# dependency/manifest conflict never reached the rust syntax branch and was
# previously text-only verified. The new ``language == "toml"`` branch in
# verify_file runs a crate-aware manifest check. These tests drive a real
# Cargo.toml conflict through verify_file with language="toml".


def _manifest_crate(tmp_path: Path) -> Path:
    """A minimal cargo crate: a Cargo.toml with one dependency + a src/lib.rs."""
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "lib.rs").write_text("pub fn ping() -> u32 { 1 }\n")
    return tmp_path


def _manifest_conflict() -> str:
    """A dependency-version conflict in Cargo.toml.

    Both sides edit the same ``version = "..."`` line (a genuine conflict),
    using a path dependency on a sibling dir so the resolved manifest resolves
    offline (no registry/network). The correct merge keeps the higher version.
    """
    return (
        '[package]\n'
        'name = "manifesttest"\n'
        'version = "0.1.0"\n'
        'edition = "2021"\n'
        '\n'
        '[dependencies]\n'
        '<<<<<<< H\n'
        'sibling = { path = "../sibling", version = "1.0.0" }\n'
        '=======\n'
        'sibling = { path = "../sibling", version = "2.0.0" }\n'
        '>>>>>>> b\n'
    )


@skip_no_cargo
def test_cargo_toml_valid_manifest_passes(tmp_path):
    """A valid resolved Cargo.toml conflict passes the manifest check.

    The merge resolves the dependency to a single coherent version; cargo sees
    a well-formed manifest → syntax_passed is True via the cargo tool.
    """
    crate = _manifest_crate(tmp_path)
    # Provide the sibling dependency the manifest references (offline resolve).
    sibling = tmp_path.parent / "sibling"
    sibling.mkdir(exist_ok=True)
    (sibling / "Cargo.toml").write_text(
        '[package]\nname = "sibling"\nversion = "2.0.0"\nedition = "2021"\n'
    )
    (sibling / "src").mkdir(exist_ok=True)
    (sibling / "src" / "lib.rs").write_text("pub fn sib() -> u32 { 2 }\n")
    correct = 'sibling = { path = "../sibling", version = "2.0.0" }'
    eng = VerificationEngine.default(ValidationConfig())
    res = eng.verify_file(
        "Cargo.toml", "toml", _manifest_conflict(),
        [(_span(_manifest_conflict()), correct)], repo_root=str(crate),
    )
    assert res.features["syntax_checked"] is True, res.features
    assert res.features["syntax_tool"] == "cargo"
    assert res.passed, [f.message for f in res.hard_failures]


@skip_no_cargo
def test_cargo_toml_malformed_manifest_caught(tmp_path):
    """A resolved manifest that's invalid TOML (a botched merge) is caught.

    The merge drops a closing quote → malformed TOML. cargo fails to parse it
    synchronously (offline, deterministic). This is the correctness gap the
    manifest check closes: previously this was accepted as text-only.
    """
    crate = _manifest_crate(tmp_path)
    # A botched merge: missing closing quote on the version value.
    broken = 'sibling = { path = "../sibling", version = "2.0.0 }'
    eng = VerificationEngine.default(ValidationConfig())
    res = eng.verify_file(
        "Cargo.toml", "toml", _manifest_conflict(),
        [(_span(_manifest_conflict()), broken)], repo_root=str(crate),
    )
    assert res.features["syntax_checked"] is True
    assert res.features["syntax_tool"] == "cargo"
    assert not res.passed
    syntax_fails = [f for f in res.hard_failures if f.validator == "syntax"]
    assert len(syntax_fails) == 1, [f.message for f in syntax_fails]


def test_cargo_toml_no_cargo_is_text_only(tmp_path, monkeypatch):
    """With cargo absent, a Cargo.toml conflict stays text-only — no false fail.

    The manifest check fires only when cargo resolves. Without a toolchain the
    manifest can't be validated, so it's reported as not-checked (consistent
    with the rustc-absent graceful-degrade path) — never a false failure.
    """
    import capybase.adapters.lsp as lsp_mod

    monkeypatch.setattr(lsp_mod, "_resolve", lambda cmd: None)
    (tmp_path / "Cargo.toml").write_text(
        '<<<<<<< H\nversion = "1"\n=======\nversion = "2"\n>>>>>>> b\n'
    )
    conflict = (
        '<<<<<<< H\nversion = "1"\n=======\nversion = "2"\n>>>>>>> b\n'
    )
    eng = VerificationEngine.default(ValidationConfig())
    res = eng.verify_file(
        "Cargo.toml", "toml", conflict, [(_span(conflict), 'version = "2"')],
        repo_root=str(tmp_path),
    )
    # No cargo → manifest check skipped → not checked.
    assert res.features.get("syntax_checked") is not True
    assert not any(f.validator == "syntax" for f in res.hard_failures)


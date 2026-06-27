"""Tests for the Rust compile floor (rustc --emit=metadata) in Phase B.

These exercise ``_compile_rust``, ``_infer_rust_edition``, and the
``verify_file`` Rust syntax branch. The rustc-backed tests skip when rustc
is absent (CI without a toolchain); the edition-inference and wiring tests
run unconditionally.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from capybase.verification import (
    ValidationConfig,
    VerificationEngine,
    _compile_rust,
    _infer_rust_edition,
)


RUSTC = shutil.which("rustc")
skip_no_rustc = pytest.mark.skipif(RUSTC is None, reason="rustc not installed")


# ---------------------------------------------------------------------------
# _compile_rust (real rustc)
# ---------------------------------------------------------------------------


@skip_no_rustc
def test_compile_rust_clean_source():
    ok, msg = _compile_rust("pub fn x() -> u32 { 1 }\n", edition="2021")
    assert ok is True
    assert msg == "rustc ok"


@skip_no_rustc
def test_compile_rust_detects_syntax_error():
    # Missing comma in a macro call + a syntax error.
    src = 'pub fn bad() { println!("{}" 1) }\n'
    ok, msg = _compile_rust(src, edition="2021")
    assert ok is False
    # The returned message is the actionable error line, not the "aborting"
    # summary.
    assert msg.startswith("error")


@skip_no_rustc
def test_compile_rust_detects_missing_field():
    # A struct initializer missing a field — a semantic error rustc catches.
    src = (
        "pub struct C { pub a: u32, pub b: u32 }\n"
        "pub fn make() -> C { C { a: 1 } }\n"
    )
    ok, msg = _compile_rust(src, edition="2021")
    assert ok is False
    assert "missing field" in msg or "error" in msg


@skip_no_rustc
def test_compile_rust_edition_2015_accepted():
    ok, _ = _compile_rust("pub fn x() -> u32 { 1 }\n", edition="2015")
    assert ok is True


@skip_no_rustc
def test_compile_rust_missing_binary_raises_file_not_found():
    # A non-existent rustc path raises FileNotFoundError (the caller gates on
    # _resolve first, so this never reaches a false syntax failure in practice).
    with pytest.raises(FileNotFoundError):
        _compile_rust("pub fn x() {}\n", rustc_path="definitely-not-rustc-xyz")


# ---------------------------------------------------------------------------
# _infer_rust_edition (no toolchain needed)
# ---------------------------------------------------------------------------


def test_infer_edition_default_when_no_cargo(tmp_path):
    # No Cargo.toml anywhere → modern default.
    assert _infer_rust_edition(str(tmp_path), str(tmp_path / "src" / "x.rs")) == "2021"


def test_infer_edition_from_cargo_toml(tmp_path):
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "x"\nversion = "0.1.0"\nedition = "2018"\n'
    )
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    assert _infer_rust_edition(str(tmp_path), str(src_dir / "lib.rs")) == "2018"


def test_infer_edition_walks_up_to_nearest_manifest(tmp_path):
    # Cargo.toml at repo root, source in a nested dir.
    (tmp_path / "Cargo.toml").write_text('edition = "2015"\n')
    nested = tmp_path / "src" / "net"
    nested.mkdir(parents=True)
    assert _infer_rust_edition(str(tmp_path), str(nested / "conn.rs")) == "2015"


def test_infer_edition_ignores_commented_edition_line(tmp_path):
    (tmp_path / "Cargo.toml").write_text(
        '[package]\n# edition = "2015"\nedition = "2021"\n'
    )
    assert _infer_rust_edition(str(tmp_path), str(tmp_path / "x.rs")) == "2021"


def test_infer_edition_handles_single_quotes(tmp_path):
    (tmp_path / "Cargo.toml").write_text("edition = '2018'\n")
    assert _infer_rust_edition(str(tmp_path), str(tmp_path / "x.rs")) == "2018"


def test_infer_edition_unknown_value_falls_back(tmp_path):
    # A bogus edition value falls back to the default rather than passing a
    # bad flag to rustc.
    (tmp_path / "Cargo.toml").write_text('edition = "2099"\n')
    assert _infer_rust_edition(str(tmp_path), str(tmp_path / "x.rs")) == "2021"


def test_infer_edition_does_not_escape_repo_root(tmp_path):
    # A manifest outside the repo_root chain is not consulted.
    (tmp_path / "Cargo.toml").write_text('edition = "2015"\n')
    # path inside tmp but repo_root a subdir without a manifest
    nested = tmp_path / "inner"
    nested.mkdir()
    (tmp_path / "src").mkdir()
    assert _infer_rust_edition(str(nested), str(tmp_path / "src" / "x.rs")) == "2021"


# ---------------------------------------------------------------------------
# verify_file Rust syntax branch
# ---------------------------------------------------------------------------


# A small, self-contained Rust conflict for the wiring tests. The block sits
# inside a valid impl so a correct merge compiles.
_RUST_CONFLICT = (
    "pub struct Cfg {\n"
    '    pub name: String,\n'
    "}\n"
    "\n"
    "impl Cfg {\n"
    "    pub fn greet(&self) -> String {\n"
    "<<<<<<< H\n"
    '        format!("hi {}", self.name)\n'
    "=======\n"
    '        format!("howdy {}", self.name)\n'
    ">>>>>>> b\n"
    "    }\n"
    "}\n"
)
# A correct merge that combines both greetings (differs from each side).
_RUST_CORRECT = '        format!("hi and howdy {}", self.name)'
# A broken merge with an unclosed delimiter.
_RUST_BROKEN = '        format!("hi {}", self.name'


def _span_of_markers(original: str) -> tuple[int, int]:
    """Return the (start, end) marker span of the only conflict block."""
    lines = original.split("\n")
    start = next(i for i, l in enumerate(lines) if l.startswith("<<<<<<<"))
    end = next(i for i, l in enumerate(lines) if l.startswith(">>>>>>>"))
    return (start, end)


@skip_no_rustc
def test_verify_file_rust_accepts_compiling_merge(tmp_path):
    span = _span_of_markers(_RUST_CONFLICT)
    eng = VerificationEngine.default(ValidationConfig())
    res = eng.verify_file(
        "src/cfg.rs", "rust", _RUST_CONFLICT, [(span, _RUST_CORRECT)],
        repo_root=str(tmp_path),
    )
    assert res.passed, [f.message for f in res.hard_failures]
    assert res.features["syntax_checked"] is True
    assert res.features["syntax_passed"] is True


@skip_no_rustc
def test_verify_file_rust_rejects_noncompiling_merge(tmp_path):
    span = _span_of_markers(_RUST_CONFLICT)
    eng = VerificationEngine.default(ValidationConfig())
    res = eng.verify_file(
        "src/cfg.rs", "rust", _RUST_CONFLICT, [(span, _RUST_BROKEN)],
        repo_root=str(tmp_path),
    )
    assert not res.passed
    syntax_fails = [f for f in res.hard_failures if f.validator == "syntax"]
    assert len(syntax_fails) == 1
    assert syntax_fails[0].message.startswith("error")


@skip_no_rustc
def test_verify_file_rust_respects_edition_override(tmp_path):
    # An explicit edition override is honored. config.rust_edition set to 2021
    # with a source valid in 2021.
    span = _span_of_markers(_RUST_CONFLICT)
    cfg = ValidationConfig(rust_edition="2021")
    eng = VerificationEngine.default(cfg)
    res = eng.verify_file(
        "src/cfg.rs", "rust", _RUST_CONFLICT, [(span, _RUST_CORRECT)],
        repo_root=str(tmp_path),
    )
    assert res.features["syntax_checked"] is True
    assert res.passed


@skip_no_rustc
def test_verify_file_rust_inference_uses_cargo_toml(tmp_path):
    # When no explicit edition is set, inference reads Cargo.toml.
    (tmp_path / "Cargo.toml").write_text('edition = "2021"\n')
    span = _span_of_markers(_RUST_CONFLICT)
    eng = VerificationEngine.default(ValidationConfig())
    res = eng.verify_file(
        "src/cfg.rs", "rust", _RUST_CONFLICT, [(span, _RUST_CORRECT)],
        repo_root=str(tmp_path),
    )
    assert res.passed


def test_verify_file_rust_missing_rustc_is_not_checked(monkeypatch, tmp_path):
    # When rustc is absent, syntax is reported as not-checked and never fails.
    import capybase.adapters.lsp as lsp_mod

    monkeypatch.setattr(lsp_mod, "_resolve", lambda cmd: None)
    span = _span_of_markers(_RUST_CONFLICT)
    eng = VerificationEngine.default(ValidationConfig())
    res = eng.verify_file(
        "src/cfg.rs", "rust", _RUST_CONFLICT, [(span, _RUST_BROKEN)],
        repo_root=str(tmp_path),
    )
    assert res.features["syntax_checked"] is False
    # No syntax failure is added (the broken code wasn't checked).
    assert not any(f.validator == "syntax" for f in res.hard_failures)


@skip_no_rustc
def test_verify_file_rust_disabled_when_require_syntax_off(tmp_path):
    # With require_syntax_if_supported off, a broken merge is checked but the
    # failure is NOT a hard error (it's recorded in features only).
    span = _span_of_markers(_RUST_CONFLICT)
    cfg = ValidationConfig(require_syntax_if_supported=False)
    eng = VerificationEngine.default(cfg)
    res = eng.verify_file(
        "src/cfg.rs", "rust", _RUST_CONFLICT, [(span, _RUST_BROKEN)],
        repo_root=str(tmp_path),
    )
    assert res.features["syntax_checked"] is True
    assert res.features["syntax_passed"] is False
    assert not any(f.validator == "syntax" for f in res.hard_failures)

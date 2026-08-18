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
def test_compile_rust_edition_2024_accepted():
    # Edition 2024 stabilized in Rust 1.85 (Feb 2025); the default for new
    # crates. rustc 1.85+ accepts --edition 2024.
    ok, _ = _compile_rust("pub fn x() -> u32 { 1 }\n", edition="2024")
    assert ok is True


@skip_no_rustc
def test_compile_rust_edition_2024_rejects_bogus():
    # An edition rustc doesn't recognize must surface as a failure (caught),
    # not silently pass. 2099 is not a valid edition.
    ok, msg = _compile_rust("pub fn x() -> u32 { 1 }\n", edition="2099")
    assert ok is False
    assert "edition" in msg.lower() or "error" in msg.lower()


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


def test_infer_edition_2024_from_cargo_toml(tmp_path):
    # Edition 2024 (the default for cargo new since Rust 1.85) is recognized.
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "x"\nversion = "0.1.0"\nedition = "2024"\n'
    )
    assert _infer_rust_edition(str(tmp_path), str(tmp_path / "src" / "lib.rs")) == "2024"


def test_rust_editions_constant_includes_2024():
    from capybase.verification import _RUST_EDITIONS

    assert "2024" in _RUST_EDITIONS
    assert "2021" in _RUST_EDITIONS


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
def test_verify_file_rust_suppresses_crate_path_errors_standalone(tmp_path):
    """A correct Rust merge that uses ``crate::`` paths passes whole-file
    validation even under standalone rustc (no Cargo.toml context), because
    E0432/E0433 (unresolved crate paths) are undecidable standalone and are
    suppressed per ``rust_suppress_codes``. Without this suppression, correct
    merges cycle: rustc false-positives on ``use crate::...``, the candidate is
    rejected + repaired identically, and it converges. Surfaced by the
    gemma-4-e4b Rust run (6 E0433 escalation cases at sim 0.89-1.0)."""
    # A conflict whose correct merge references crate:: paths (unresolvable
    # standalone → E0433). Standalone rustc fails; the suppression must drop it.
    conflict = (
        "use crate::config::Setting;\n"
        "\n"
        "pub fn label(s: &Setting) -> String {\n"
        "<<<<<<< H\n"
        '    format!("[{}]", s.name)\n'
        "=======\n"
        '    format!("({})", s.name)\n'
        ">>>>>>> b\n"
        "}\n"
    )
    span = _span_of_markers(conflict)
    correct = '    format!("[{}] {}", s.name, s.name)'
    cfg = ValidationConfig(rust_suppress_codes=["E0432", "E0433"])
    eng = VerificationEngine.default(cfg)
    res = eng.verify_file(
        "src/cfg.rs", "rust", conflict, [(span, correct)],
        repo_root=str(tmp_path),  # no Cargo.toml → standalone rustc path
    )
    # The crate-path E0433 is suppressed → the merge passes.
    assert res.passed, (
        f"crate-path error should be suppressed standalone; got: "
        f"{[f.message for f in res.hard_failures]}")
    assert res.features["syntax_checked"] is True


@skip_no_rustc
def test_verify_file_rust_does_not_suppress_real_syntax_errors(tmp_path):
    """A genuine syntax error (unclosed delimiter) is NOT suppressed even with
    rust_suppress_codes set — only crate-path resolution errors are."""
    span = _span_of_markers(_RUST_CONFLICT)
    cfg = ValidationConfig(rust_suppress_codes=["E0432", "E0433"])
    eng = VerificationEngine.default(cfg)
    res = eng.verify_file(
        "src/cfg.rs", "rust", _RUST_CONFLICT, [(span, _RUST_BROKEN)],
        repo_root=str(tmp_path),
    )
    assert not res.passed  # the broken merge (unclosed delim) still fails


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


# ---------------------------------------------------------------------------
# Unchecked-baseline abstention (tokio-0110, sprint-17 WS1b)
# ---------------------------------------------------------------------------

class _SeqRunner:
    """Fake RustAnalyzerRunner returning a scripted sequence of Diagnostics.

    The baseline check runs first, the candidate check second — the live
    ordering inside _run_cargo_syntax_check."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    def check(self, source, *, path, repo_root):
        self.calls.append(source)
        return self._results.pop(0)


def _diags(checked, messages=()):
    from capybase.adapters.lsp import Diagnostic, Diagnostics
    return Diagnostics(
        checked=checked, tool="cargo",
        diagnostics=[Diagnostic(severity="error", message=m, line=1, column=1,
                                code="E0599", source="cargo") for m in messages])


def _syntax_engine():
    return VerificationEngine.default(ValidationConfig())


def test_unchecked_baseline_abstains_never_fails():
    # tokio-0110: the baseline's cold compile blew the subprocess timeout —
    # Diagnostics(checked=False) with an EMPTY error list. Deltaing against
    # it counted the candidate's (pre-existing) errors as "new" and rejected
    # a sim-0.999 merge whose oracle carries the same errors. Undecidable
    # delta must abstain.
    import capybase.adapters.lsp as lsp_mod
    eng = _syntax_engine()
    fake = _SeqRunner([
        _diags(False),                      # baseline: timed out, unchecked
        _diags(True, ["no method named `remove` found"]),
    ])
    from unittest.mock import patch
    with patch.object(lsp_mod, "RustAnalyzerRunner", return_value=fake):
        hard = []
        features: dict = {}
        ran = eng._run_cargo_syntax_check(
            "f.rs", "<<<<<<<\na\n=======\nb\n>>>>>>>\n", "fn a() {}\n",
            "/tmp/r", hard, features)
    assert ran is False                      # abstained — cargo "didn't run"
    assert features["syntax_checked"] is False
    assert features["syntax_passed"] is True
    assert hard == []                        # no failure recorded


def test_checked_baseline_still_fails_new_errors():
    # The true-positive path is unchanged: a checked baseline with no such
    # error + a candidate introducing it remains a hard failure.
    from unittest.mock import patch
    import capybase.adapters.lsp as lsp_mod
    eng = _syntax_engine()
    fake = _SeqRunner([
        _diags(True),                        # baseline clean
        _diags(True, ["no method named `remove` found"]),
    ])
    with patch.object(lsp_mod, "RustAnalyzerRunner", return_value=fake):
        hard = []
        features: dict = {}
        eng._run_cargo_syntax_check(
            "f.rs", "<<<<<<<\na\n=======\nb\n>>>>>>>\n", "fn a() {}\n",
            "/tmp/r", hard, features)
    assert features["syntax_checked"] is True
    assert features["syntax_passed"] is False
    assert hard and "remove" in hard[0].message


def test_preexisting_errors_in_baseline_are_not_new():
    # The delta still cancels errors the baseline already had (the
    # no-worse-than-before contract).
    from unittest.mock import patch
    import capybase.adapters.lsp as lsp_mod
    eng = _syntax_engine()
    fake = _SeqRunner([
        _diags(True, ["no method named `remove` found"]),
        _diags(True, ["no method named `remove` found"]),
    ])
    with patch.object(lsp_mod, "RustAnalyzerRunner", return_value=fake):
        hard = []
        features: dict = {}
        eng._run_cargo_syntax_check(
            "f.rs", "<<<<<<<\na\n=======\nb\n>>>>>>>\n", "fn a() {}\n",
            "/tmp/r", hard, features)
    assert features["syntax_checked"] is True
    assert features["syntax_passed"] is True
    assert hard == []

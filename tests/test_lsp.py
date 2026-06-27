"""Tests for the LSP diagnostics validator and shadow tests (Phase B).

These exercise the LSP runner protocol and the verify_file integration. The
PyrightRunner test uses the real pyright binary when installed (it's in the
dev venv); otherwise it skips. The validator logic tests use a fake runner so
they run without any external toolchain.
"""

from __future__ import annotations

import shutil

import pytest

from capybase.adapters.lsp import (
    Diagnostic,
    Diagnostics,
    LspConfig,
    LspRunner,
    PyrightRunner,
    runner_for,
)
from capybase.verification import ValidationConfig, VerificationEngine


# ---------------------------------------------------------------------------
# LspRunner protocol + dispatch
# ---------------------------------------------------------------------------


class _FakeRunner:
    """Returns canned diagnostics, for deterministic validator tests."""

    def __init__(self, baseline: Diagnostics, after: Diagnostics):
        self._baseline = baseline
        self._after = after
        self.calls = 0

    def check(self, source, *, path, repo_root):
        self.calls += 1
        # First call is the baseline (on the marker-blanked original); second
        # is the resolved file. Distinguish by call order.
        if self.calls == 1:
            return self._baseline
        return self._after


def test_runner_for_python():
    r = runner_for("python", config=LspConfig(pyright_path="pyright"))
    assert isinstance(r, PyrightRunner)


def test_runner_for_rust():
    r = runner_for("rust")
    assert r is not None


def test_runner_for_unknown_language():
    assert runner_for("brainfuck") is None


# ---------------------------------------------------------------------------
# PyrightRunner (real binary, skipped if absent)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("pyright") is None and not __import__("os").path.exists(
        "/w/capybase/.venv/bin/pyright"
    ),
    reason="pyright not installed",
)
def test_pyright_detects_undefined_name():
    r = PyrightRunner("/w/capybase/.venv/bin/pyright")
    d = r.check("def f():\n    return undefined_var\n", path="x.py", repo_root="/tmp")
    assert d.checked
    assert d.error_count >= 1
    assert any("undefined_var" in diag.message for diag in d.errors)


@pytest.mark.skipif(
    shutil.which("pyright") is None and not __import__("os").path.exists(
        "/w/capybase/.venv/bin/pyright"
    ),
    reason="pyright not installed",
)
def test_pyright_clean_source_no_errors():
    r = PyrightRunner("/w/capybase/.venv/bin/pyright")
    d = r.check("def f():\n    return 1\n", path="x.py", repo_root="/tmp")
    assert d.checked
    assert d.error_count == 0


def test_pyright_missing_binary_reports_unchecked():
    r = PyrightRunner("definitely-not-a-real-binary-xyz")
    d = r.check("x = 1", path="x.py", repo_root="/tmp")
    assert not d.checked


# ---------------------------------------------------------------------------
# verify_file LSP integration (fake runner)
# ---------------------------------------------------------------------------


def _config_with_lsp():
    return ValidationConfig(
        enable_lsp_diagnostics=True,
        pyright_path="pyright",
    )


def test_verify_file_lsp_rejects_new_errors(monkeypatch):
    """A candidate introducing a new type error must fail Phase B."""
    original = (
        "def greet():\n<<<<<<< H\n    return 'hi'\n"
        "=======\n    return 'howdy'\n>>>>>>> b\n"
    )
    resolutions = [((1, 5), "    return undefined_thing")]
    baseline = Diagnostics(checked=True, tool="pyright")  # no errors
    after = Diagnostics(
        checked=True,
        tool="pyright",
        diagnostics=[
            Diagnostic(severity="error", message="'undefined_thing' is not defined", line=1)
        ],
    )
    fake = _FakeRunner(baseline, after)
    # Monkeypatch runner_for to return our fake.
    import capybase.adapters.lsp as lsp_mod

    monkeypatch.setattr(lsp_mod, "runner_for", lambda lang, config=None: fake)
    engine = VerificationEngine.default(_config_with_lsp())
    res = engine.verify_file("app.py", "python", original, resolutions, repo_root="/tmp")
    assert not res.passed
    assert any(f.validator == "lsp_diagnostics" for f in res.hard_failures)
    assert res.features["lsp_checked"] is True
    assert res.features["lsp_new_error_count"] == 1


def test_verify_file_lsp_allows_preexisting_errors(monkeypatch):
    """Pre-existing errors (in the baseline) must NOT fail the merge."""
    original = (
        "def greet():\n<<<<<<< H\n    return 'hi'\n"
        "=======\n    return 'howdy'\n>>>>>>> b\n"
    )
    resolutions = [((1, 5), "    return ('hi', 'howdy')")]
    # Baseline has a pre-existing error; after has the SAME error (no new ones).
    preexisting = Diagnostic(severity="error", message="old problem", line=10)
    baseline = Diagnostics(checked=True, tool="pyright", diagnostics=[preexisting])
    after = Diagnostics(checked=True, tool="pyright", diagnostics=[preexisting])
    fake = _FakeRunner(baseline, after)
    import capybase.adapters.lsp as lsp_mod

    monkeypatch.setattr(lsp_mod, "runner_for", lambda lang, config=None: fake)
    engine = VerificationEngine.default(_config_with_lsp())
    res = engine.verify_file("app.py", "python", original, resolutions, repo_root="/tmp")
    assert res.passed, [f.message for f in res.hard_failures]
    assert res.features["lsp_new_error_count"] == 0


def test_verify_file_lsp_inert_when_disabled():
    """When LSP is off, features report not-checked and no failures."""
    original = "def f():\n    return 1\n"
    engine = VerificationEngine.default(ValidationConfig())  # lsp off
    res = engine.verify_file("app.py", "python", original, [], repo_root="/tmp")
    assert res.features["lsp_checked"] is False
    assert res.passed


def test_verify_file_lsp_inert_when_tool_absent(monkeypatch):
    """When the tool is absent (checked=False), no failure is added."""
    original = "def f():\n<<<<<<< H\n1\n=======\n2\n>>>>>>> b\n"
    baseline = Diagnostics(checked=False, tool="pyright")
    after = Diagnostics(checked=False, tool="pyright")
    fake = _FakeRunner(baseline, after)
    import capybase.adapters.lsp as lsp_mod

    monkeypatch.setattr(lsp_mod, "runner_for", lambda lang, config=None: fake)
    engine = VerificationEngine.default(_config_with_lsp())
    res = engine.verify_file("app.py", "python", original, [((1, 3), "3")], repo_root="/tmp")
    assert res.features["lsp_checked"] is False


# ---------------------------------------------------------------------------
# Shadow tests
# ---------------------------------------------------------------------------


def test_shadow_tests_inert_when_disabled():
    engine = VerificationEngine.default(ValidationConfig())
    res = engine.verify_file("app.py", "python", "x = 1\n", [], repo_root="/tmp")
    assert res.features.get("shadow_tests_run") is False


def test_locate_shadow_test_finds_conventional_path(tmp_path):
    from capybase.verification import _locate_shadow_test

    (tmp_path / "tests").mkdir()
    test_file = tmp_path / "tests" / "test_app.py"
    test_file.write_text("def test_x(): pass\n")
    found = _locate_shadow_test("app.py", str(tmp_path))
    assert found == (str(test_file), "python")


def test_locate_shadow_test_returns_none_when_absent(tmp_path):
    from capybase.verification import _locate_shadow_test

    assert _locate_shadow_test("missing.py", str(tmp_path)) is None


def test_locate_shadow_test_ignores_non_python():
    from capybase.verification import _locate_shadow_test

    # Rust file in a directory with no Cargo.toml → no cargo project → None.
    assert _locate_shadow_test("config.rs", "/tmp") is None


# ---------------------------------------------------------------------------
# Risk routing for lsp_failed
# ---------------------------------------------------------------------------


def test_risk_retries_lsp_failed():
    from capybase.conflict_model import VerificationResult
    from capybase.risk import RiskEngine

    res = VerificationResult(
        candidate_id="c", unit_id="u", passed=False,
        hard_failures=[], features={},
    )
    decision = RiskEngine(max_retries_per_unit=2).decide(
        res, retry_count=0, failure_kind="lsp_failed"
    )
    assert decision.action == "retry"


def test_risk_escalates_lsp_failed_after_max():
    from capybase.conflict_model import VerificationResult
    from capybase.risk import RiskEngine

    res = VerificationResult(
        candidate_id="c", unit_id="u", passed=False,
        hard_failures=[], features={},
    )
    decision = RiskEngine(max_retries_per_unit=2).decide(
        res, retry_count=2, failure_kind="lsp_failed"
    )
    assert decision.action == "escalate"


# ---------------------------------------------------------------------------
# Rust: cargo check + rust-analyzer runner
# ---------------------------------------------------------------------------


def test_rust_runner_returned_for_rust():
    from capybase.adapters.lsp import RustAnalyzerRunner, runner_for

    r = runner_for("rust")
    assert isinstance(r, RustAnalyzerRunner)


def test_parse_cargo_messages_extracts_errors():
    from capybase.adapters.lsp import _parse_cargo_messages

    # Two compiler-message lines: one error, one warning.
    stdout = "\n".join([
        '{"reason":"compiler-message","message":{"level":"error",'
        '"message":"cannot find value `X`","spans":[{"is_primary":true,'
        '"line_start":10,"column_start":5}],"code":{"code":"E0425"}}}',
        '{"reason":"compiler-message","message":{"level":"warning",'
        '"message":"unused variable","spans":[{"is_primary":true,'
        '"line_start":3,"column_start":9}]}}',
        '{"reason":"compiler-artifact"}',  # ignored line
    ])
    diags = _parse_cargo_messages(stdout, "src/lib.rs")
    assert len(diags) == 2
    err = next(d for d in diags if d.severity == "error")
    assert "cannot find value" in err.message
    assert err.line == 9  # cargo is 1-based → 0-based
    assert err.column == 4
    assert err.code == "E0425"
    assert err.source == "cargo"


def test_parse_cargo_messages_tolerates_garbage():
    from capybase.adapters.lsp import _parse_cargo_messages

    # Non-JSON lines and non-compiler-message reasons are skipped, never crash.
    stdout = "not json\n{}\n" + '{"reason":"compiler-artifact"}\n'
    assert _parse_cargo_messages(stdout, "src/lib.rs") == []


def test_rust_analyzer_runner_missing_cargo_reports_unchecked(tmp_path, monkeypatch):
    # When cargo is absent (and no rust-analyzer fallback), the runner reports
    # checked=False rather than raising.
    from capybase.adapters.lsp import RustAnalyzerRunner

    monkeypatch.setattr("capybase.adapters.lsp._resolve", lambda cmd: None)
    monkeypatch.setattr(
        "capybase.adapters.lsp._has_cargo_manifest", lambda root: True
    )
    r = RustAnalyzerRunner()
    d = r.check("pub fn x() {}\n", path="src/lib.rs", repo_root=str(tmp_path))
    assert d.checked is False


def test_has_cargo_manifest_detection(tmp_path):
    from capybase.adapters.lsp import _has_cargo_manifest

    assert _has_cargo_manifest(str(tmp_path)) is False
    (tmp_path / "Cargo.toml").write_text('[package]\nname="x"\n')
    assert _has_cargo_manifest(str(tmp_path)) is True


"""Sprint-20 S20.2 — toolchain-era preflight probe (ESCALATE_TOOLCHAIN).

tokio-0109 class: historical code where BOTH pristine sides AND the
oracle fail the gate with identical compile errors under the eval's
newer toolchain. The preflight classifies such cases in one cached
probe triple instead of burning full majority-of-3 budgets, and must
NEVER fire on passable cases (strict: all three fail, real compile
errors, identical side signatures; python/crateless-rust/degraded-gate
skip entirely — standalone rustc on one file fails on `use crate::`
paths for era-independent reasons).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "live_eval_realworld_toolchain",
        Path(__file__).resolve().parent.parent / "scripts" / "live_eval_realworld.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["live_eval_realworld_toolchain"] = mod
    spec.loader.exec_module(mod)  # type: ignore[arg-type]
    return mod


_M = _load_module()


def _case(**kw):
    base = {
        "id": "tokio-history-0109", "path": "src/lib.rs", "language": "rust",
        "base": "fn a() {}\n", "current": "fn a() { 1 }\n",
        "replayed": "fn a() { 2 }\n", "expected_resolved": "fn a() { 3 }\n",
        "marker_original": "<<<<<<<\n=======\n>>>>>>>\n",
        "dataset": "tokio-history",
    }
    base.update(kw)
    return _M.Case(**base)


class TestSignature:
    def test_rust_lines_kept_whole(self):
        out = ("warning: unused import\n"
               "error[E0658]: `#[deprecated]` is experimental\n"
               "  --> tokio/src/x.rs:10:5\n"
               "error: `ManuallyDrop` cannot be dropped\n"
               "error: could not compile `tokio` (lib) due to 6 previous "
               "errors; 71 warnings emitted\n")
        assert _M._compile_error_signature(out, "rust") == [
            "error: `ManuallyDrop` cannot be dropped",
            "error[E0658]: `#[deprecated]` is experimental",
        ]

    def test_rust_summary_counts_do_not_break_equality(self):
        # tokio-0109 live: the sides differ by ONE warning, and cargo's
        # summary line carried "71" vs "70" — the only signature
        # difference. Summaries are excluded, so the sides compare equal.
        a = ("error: `#[deprecated]` attribute cannot be used on trait impl "
             "blocks\nerror: could not compile `tokio` (lib) due to 6 "
             "previous errors; 71 warnings emitted\n")
        b = ("error: `#[deprecated]` attribute cannot be used on trait impl "
             "blocks\nerror: could not compile `tokio` (lib) due to 6 "
             "previous errors; 70 warnings emitted\n")
        assert _M._compile_error_signature(a, "rust") == \
            _M._compile_error_signature(b, "rust")

    def test_c_locations_stripped(self):
        out = ("src/foo.c:30:2: error: 'tokenizer_' does not name a type\n"
               "src/foo.c:31:2: error: expected ';' before '}'\n"
               "make: *** [Makefile:200: foo.o] Error 1\n")
        assert _M._compile_error_signature(out, "c") == [
            "error: 'tokenizer_' does not name a type",
            "error: expected ';' before '}'",
        ]

    def test_driver_noise_yields_empty_signature(self):
        # The cf50f4b broken-gate class: make usage text, no compile errors.
        out = ("Usage: make [options] [target] ...\n"
               "make: invalid option -- 'j'\n")
        assert _M._compile_error_signature(out, "c") == []
        assert _M._compile_error_signature(out, "cpp") == []


class TestVerdictChain:
    def test_toolchain_dead_short_circuits(self):
        r = _M.CaseResult(id="x", language="rust", dataset="d")
        r.escalated = True
        r.toolchain_dead = True
        assert _M._verdict_chain(r) == "ESCALATE_TOOLCHAIN"

    def test_plain_escalate_unchanged(self):
        r = _M.CaseResult(id="x", language="rust", dataset="d")
        r.escalated = True
        assert _M._verdict_chain(r) == "ESCALATE"


class TestTerminalReason:
    def test_toolchain_era_category(self):
        assert _M._classify_terminal_reason(
            "toolchain-era: both pristine sides and the oracle fail the gate "
            "with identical compile errors (cargo check)"
        ) == "TOOLCHAIN_ERA"


def _fake_gate(responses):
    """responses: list of (rc, output) consumed per call."""
    calls = []

    def fake(cmd, **kw):
        calls.append({"cmd": cmd, "cwd": kw.get("cwd")})
        rc, out = responses[len(calls) - 1] if len(calls) <= len(responses) \
            else responses[-1]
        return SimpleNamespace(returncode=rc, stdout="", stderr=out)

    fake.calls = calls
    return fake


_ERA_ERR = "error[E0658]: `#[deprecated]` is experimental\n"


class TestProbe:
    def _repo(self, tmp_path, conflicted=b"<<<<<<<\nx\n=======\nyn>>>>>>>\n"):
        repo = tmp_path / "r"
        (repo / "src").mkdir(parents=True)
        target = repo / "src" / "lib.rs"
        target.write_bytes(conflicted)
        return repo, target, conflicted

    def test_classifies_identical_failures_and_restores_file(
            self, tmp_path, monkeypatch):
        repo, target, conflicted = self._repo(tmp_path)
        fake = _fake_gate([(1, _ERA_ERR), (1, _ERA_ERR), (1, _ERA_ERR)])
        monkeypatch.setattr(_M, "_run_shell_tree", fake)
        probe = _M._toolchain_era_probe(
            repo, _case(), has_crate=True)
        assert probe is not None and probe["toolchain_dead"] is True
        assert probe["gate"] == "cargo check"
        assert len(fake.calls) == 3  # current, replayed, oracle
        assert target.read_bytes() == conflicted  # byte-exact restore

    def test_oracle_compiles_declines(self, tmp_path, monkeypatch):
        repo, target, _ = self._repo(tmp_path)
        monkeypatch.setattr(_M, "_run_shell_tree",
                            _fake_gate([(1, _ERA_ERR), (1, _ERA_ERR), (0, "")]))
        probe = _M._toolchain_era_probe(repo, _case(), has_crate=True)
        assert probe is not None and probe["toolchain_dead"] is False

    def test_differing_side_signatures_decline(self, tmp_path, monkeypatch):
        repo, target, _ = self._repo(tmp_path)
        monkeypatch.setattr(_M, "_run_shell_tree", _fake_gate([
            (1, "error[E0658]: era one\n"),
            (1, "error: something else entirely\n"),
            (1, "error[E0658]: era one\n"),
        ]))
        probe = _M._toolchain_era_probe(repo, _case(), has_crate=True)
        assert probe is not None and probe["toolchain_dead"] is False

    def test_no_real_errors_declines(self, tmp_path, monkeypatch):
        # Broken gate (usage text, rc=2) on every probe: never classifies.
        repo, target, _ = self._repo(tmp_path)
        monkeypatch.setattr(_M, "_run_shell_tree", _fake_gate([
            (2, "Usage: make [options] [target] ...\n")] * 3))
        c = _case(language="cpp", path="src/foo.cc")
        (repo / "src" / "foo.cc").write_text("int f();\n")
        monkeypatch.setattr(_M, "_DETECTED_BUILD_CMD", {c.id: "make -j4"})
        probe = _M._toolchain_era_probe(repo, c, has_crate=False)
        assert probe is not None and probe["toolchain_dead"] is False

    def test_skips_python_crateless_rust_and_true_gate(
            self, tmp_path, monkeypatch):
        repo, target, _ = self._repo(tmp_path)
        fake = _fake_gate([(1, _ERA_ERR)] * 3)
        monkeypatch.setattr(_M, "_run_shell_tree", fake)
        assert _M._toolchain_era_probe(repo, _case(language="python"),
                                       has_crate=False) is None
        assert _M._toolchain_era_probe(repo, _case(), has_crate=False) is None
        c = _case(language="c", path="src/foo.c")
        (repo / "src" / "foo.c").write_text("int f(void);\n")
        monkeypatch.setattr(_M, "_DETECTED_BUILD_CMD", {c.id: "true"})
        assert _M._toolchain_era_probe(repo, c, has_crate=False) is None
        assert not fake.calls  # nothing ran — all skips are pre-gate


class TestCensus:
    def test_census_classifies_toolchain(self, tmp_path, capsys):
        # Drive the REAL census classifier (nested in _print_census) with a
        # fixture results file — defect review pass 3: the prior version
        # re-asserted the input contract instead of the classifier.
        import json as _json
        recs = [
            {"id": "era", "escalated": True, "verdict": "ESCALATE_TOOLCHAIN",
             "reason": "toolchain-era: both pristine sides and the oracle "
                       "fail the gate with identical compile errors",
             "terminal_reason": "TOOLCHAIN_ERA", "toolchain_dead": True},
            {"id": "fine", "escalated": False, "verdict": "PASS",
             "reason": "", "terminal_reason": ""},
        ]
        f = tmp_path / "r.json"
        f.write_text(_json.dumps(recs))
        try:
            _M._print_census(str(f))
        except Exception:
            pass  # printing may expect more fields; the category is the assert
        out = capsys.readouterr().out
        assert "toolchain_era" in out, out[-400:]


def test_environmental_failures_never_classify(tmp_path, monkeypatch):
    """E2 post-reboot regression: a dependency-fetch failure is identical
    across all three probe texts BY CONSTRUCTION — without the
    environmental blocklist it trivially satisfies the strict identical-
    signature condition (six sea-orm cases misclassified era-dead)."""
    repo = tmp_path / "r"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "lib.rs").write_text("fn a() {}\n")
    fetch_err = ("error: failed to get `sea-query` as a dependency of "
                 "package `sea-orm v0.3.1`\n")
    monkeypatch.setattr(_M, "_run_shell_tree",
                        _fake_gate([(101, fetch_err)] * 3))
    c = _case()
    probe = _M._toolchain_era_probe(repo, c, has_crate=True)
    assert probe is not None
    assert probe["toolchain_dead"] is False
    assert probe["environmental"] is True


def test_mixed_signatures_still_classify(tmp_path, monkeypatch):
    """S21.1: environmental lines co-occurring with genuine era compile
    errors must NOT block classification (the 8-case fold-back lesson:
    any-pattern decline over-triggers)."""
    repo = tmp_path / "r"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "lib.rs").write_text("fn a() {}\n")
    mixed = ("error: failed to get `sea-query` as a dependency\n"
             "error[E0658]: `#[deprecated]` is experimental\n")
    monkeypatch.setattr(_M, "_run_shell_tree", _fake_gate([(101, mixed)] * 3))
    probe = _M._toolchain_era_probe(repo, _case(), has_crate=True)
    assert probe is not None and probe["toolchain_dead"] is True
    assert probe["environmental"] is False

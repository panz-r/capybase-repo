"""End-to-end test: the full orchestrator resolving a real Rust rebase conflict.

Drives the ``rust_conflicted_repo`` fixture (mirrors the live ``rust-uu``
fixture) through the complete M3 loop — extract → propose → verify → risk →
splice → compile-check → stage → continue — with a fake LLM client returning
the correct merge. This is the integration proof that the whole Rust pipeline
(Phase-B rustc compile floor + multi-unit splice + validation) works together
on real Rust code, and that a non-compiling Rust merge is rejected.

Requires rustc (skipped on CI without a toolchain).
"""

from __future__ import annotations

import json
import shutil

import pytest

from capybase.adapters.llm_openai import LLMResponse
from capybase.config import Config
from capybase.orchestrator import Orchestrator
from capybase.resolution_engine import ResolutionEngine

from tests.conftest import git

rustc = shutil.which("rustc")
skip_no_rustc = pytest.mark.skipif(rustc is None, reason="rustc not installed")


class CyclingClient:
    """Returns canned responses in order, then repeats the last."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        if len(self.responses) > 1:
            return LLMResponse(text=self.responses.pop(0))
        return LLMResponse(text=self.responses[0])


def _config(repo) -> Config:
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False  # no pytest/cargo in the fixture repo
    cfg.tests.pre_continue = None
    cfg.tests.final = None
    return cfg


def _payload(text: str) -> str:
    return json.dumps(
        {"resolved_text": text, "explanation": "merge", "self_reported_confidence": 0.8}
    )


@skip_no_rustc
def test_rust_rebase_resolves_and_compiles(rust_conflicted_repo):
    """A correct merge of the rust-uu fixture resolves, compiles, and continues.

    The fixture produces two conflict hunks inside one ``impl Config``: the
    ``new()`` initializer (retries value + new timeout_ms field) and the
    ``label()`` format string. Git auto-merges the non-conflicting struct-field
    addition. The correct merge keeps max_retries=5, adds timeout_ms=10000, and
    combines both format-string changes — and it must compile under the new
    Phase-B rustc floor to be accepted.
    """
    repo = rust_conflicted_repo["repo"]
    # The two block-interior merges (in hunk order: new() then label()).
    r_new = (
        "            max_retries: 5,\n"
        "            timeout_ms: 10000,"
    )
    r_label = (
        '        format!("[{}] (retries={}, timeout={})", self.name, '
        "self.max_retries, self.timeout_ms)"
    )
    engine = ResolutionEngine(
        _config(repo).model, client=CyclingClient([_payload(r_new), _payload(r_label)])
    )
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert not result.escalated, result.reason
    text = (repo / "src" / "config.rs").read_text()
    assert "<<<<<<<" not in text
    # The merge is semantically correct: both sides' intent preserved.
    assert "max_retries: 5" in text          # upstream's value
    assert "timeout_ms: 10000" in text       # replayed's field + init
    assert "[{}] (retries={}, timeout={})" in text  # combined format string
    # The rebase continued to completion (the resolved file was committed).
    git(repo, "rebase", "--abort", check=False)  # clean state for the log read
    log = git(repo, "log", "--oneline").stdout
    assert "replayed" in log


@skip_no_rustc
def test_rust_rebase_rejects_noncompiling_merge(rust_conflicted_repo):
    """A merge that breaks compilation (dropped field) is rejected at Phase B.

    The structural resolver / Phase-A validators can't catch a struct field
    added but never initialized — only the rustc compile floor (Phase B) sees
    it. This is the exact correctness gap the compile floor closes.
    """
    repo = rust_conflicted_repo["repo"]
    # "Broken" merge: struct gets timeout_ms (auto-merged by git) but new()
    # drops the timeout_ms initializer → E0063 missing field.
    r_new_broken = "            max_retries: 5,"
    r_label = (
        '        format!("[{}] (retries={}, timeout={})", self.name, '
        "self.max_retries, self.timeout_ms)"
    )
    engine = ResolutionEngine(
        _config(repo).model,
        client=CyclingClient([_payload(r_new_broken), _payload(r_label)]),
    )
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    # Either retried then escalated (the broken merge can't be fixed), OR the
    # structural resolver declined and the model's broken merge failed Phase B.
    # In both cases the rebase must NOT have silently applied a non-compiling
    # file — escalate is the correct, safe outcome.
    assert result.escalated
    assert "<<<<<<<" in (repo / "src" / "config.rs").read_text() or (
        "timeout_ms: 10000" not in (repo / "src" / "config.rs").read_text()
    )


@skip_no_rustc
def test_rust_rebase_inspect_extracts_units(rust_conflicted_repo):
    """Inspect detects the Rust conflict and extracts units without mutating."""
    repo = rust_conflicted_repo["repo"]
    before = (repo / "src" / "config.rs").read_text()
    orch = Orchestrator(_config(repo), repo=str(repo))
    result = orch.inspect()
    assert not result.escalated
    # worktree untouched
    assert (repo / "src" / "config.rs").read_text() == before
    # the file's units were extracted
    assert "src/config.rs" in result.units_by_path
    units = result.units_by_path["src/config.rs"]
    assert len(units) >= 2  # at least the two conflict hunks
    # language inferred from the .rs extension
    assert all(u.language == "rust" for u in units)

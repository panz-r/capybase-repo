"""Integration tests for the orchestrator against real temp git repos.

A fake LLM client (no network) returns a pre-baked merged resolution so the
full M3 loop — extract → propose → verify → risk → splice → stage → continue
— can be exercised end to end without a live model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from capybase.adapters.llm_openai import LLMResponse
from capybase.config import Config
from capybase.orchestrator import Orchestrator
from capybase.resolution_engine import ResolutionEngine

from tests.conftest import git


class FakeClient:
    """Returns canned JSON responses in order; repeats the last one forever."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        if self.responses:
            r = self.responses.pop(0)
        else:
            raise RuntimeError("no more fake responses")
        return LLMResponse(text=r)


class CyclingClient:
    """Like FakeClient but repeats the final response indefinitely.

    Used where the orchestrator may retry; avoids brittle payload counting.
    """

    def __init__(self, responses: list[str]):
        self.responses = list(responses)

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        if len(self.responses) > 1:
            return LLMResponse(text=self.responses.pop(0))
        return LLMResponse(text=self.responses[0])


def _config(tmp_path: Path, *, tests_required: bool = True, pre_continue: str | None = "true") -> Config:
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = tests_required
    cfg.tests.pre_continue = pre_continue  # `true` always exits 0
    cfg.tests.final = pre_continue
    # Write artifacts under the repo's .rebase-agent (cwd of the repo).
    return cfg


def _make_resolved_payload(text: str) -> str:
    return json.dumps({"resolved_text": text, "explanation": "merge", "self_reported_confidence": 0.8})


# ---------------------------------------------------------------------------
# M1: inspect (no mutation)
# ---------------------------------------------------------------------------


def test_inspect_no_mutation(conflicted_repo):
    repo = conflicted_repo["repo"]
    before = (repo / "app.py").read_text()
    orch = Orchestrator(_config(repo), repo=str(repo))
    result = orch.inspect()
    assert not result.escalated
    # worktree file untouched
    assert (repo / "app.py").read_text() == before
    # one conflict unit extracted
    assert "app.py" in result.units_by_path
    # review bundle written
    assert (orch.paths.final / "review-bundle.md").exists()
    # journal exists
    assert orch.paths.journal.exists()


def test_inspect_no_rebase(repo):
    orch = Orchestrator(_config(repo), repo=str(repo))
    result = orch.inspect()
    assert result.escalated
    assert "no rebase" in (result.reason or "")


# ---------------------------------------------------------------------------
# M2: manual mode
# ---------------------------------------------------------------------------


def test_manual_mode_resolves(conflicted_repo):
    repo = conflicted_repo["repo"]
    # Manual mode reads the literal resolved text (not JSON).
    inputs = ["    return 'merged'"]
    orch = Orchestrator(
        _config(repo), repo=str(repo),
        stdin_reader=lambda _prompt: inputs.pop(0),
        out=lambda *_a, **_k: None,
    )
    result = orch.manual()
    assert not result.escalated
    # file no longer has markers
    text = (repo / "app.py").read_text()
    assert "<<<<<<<" not in text
    assert "merged" in text
    # staged
    staged = git(repo, "diff", "--cached", "--name-only")
    assert "app.py" in staged.stdout


def test_manual_mode_rejects_bad_resolution(conflicted_repo):
    repo = conflicted_repo["repo"]
    # resolution that leaves a marker -> validation fails
    inputs = ["    x\n<<<<<<< leaked\n"]
    orch = Orchestrator(
        _config(repo), repo=str(repo),
        stdin_reader=lambda _prompt: inputs.pop(0),
        out=lambda *_a, **_k: None,
    )
    result = orch.manual()
    assert result.escalated


# ---------------------------------------------------------------------------
# M3: full run (fake model)
# ---------------------------------------------------------------------------


def test_run_resolves_and_continues(conflicted_repo):
    repo = conflicted_repo["repo"]
    # A resolution that merges both sides (differs from either verbatim) so the
    # preservation heuristic does not force retries.
    payload = _make_resolved_payload("    return 'hi' + 'howdy'")
    engine = ResolutionEngine(_config(repo).model, client=CyclingClient([payload]))
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    # rebase completed cleanly
    assert not result.escalated, result.reason
    # no conflict markers anywhere
    assert "<<<<<<<" not in (repo / "app.py").read_text()
    # rebase no longer in progress
    r = git(repo, "rebase", "--abort", check=False)  # ensure clean state readable
    # HEAD should be the replayed branch tip rebased onto main.
    log = git(repo, "log", "--oneline").stdout
    assert "replayed change" in log


def test_run_escalates_when_model_returns_markers(conflicted_repo):
    repo = conflicted_repo["repo"]
    # model keeps returning a leaked marker across all retries -> escalate
    payload = _make_resolved_payload("    x\n<<<<<<< still\n")
    engine = ResolutionEngine(_config(repo).model, client=CyclingClient([payload]))
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert result.escalated
    assert (orch.paths.final / "review-bundle.md").exists()


def test_run_escalates_on_needs_human(conflicted_repo):
    repo = conflicted_repo["repo"]
    payload = json.dumps({"resolved_text": "    return 1", "needs_human": True})
    engine = ResolutionEngine(_config(repo).model, client=CyclingClient([payload]))
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert result.escalated


def test_run_aborts_tests_when_required_and_failing(conflicted_repo):
    repo = conflicted_repo["repo"]
    payload = _make_resolved_payload("    return 'hi' + 'howdy'")
    engine = ResolutionEngine(_config(repo).model, client=CyclingClient([payload]))
    cfg = _config(repo, tests_required=True, pre_continue="false")  # exits 1
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert result.escalated
    assert "tests failed" in (result.reason or "")


def test_run_retries_after_transient_error(conflicted_repo):
    """A request_failed candidate (timeout/network) should retry, then succeed."""
    from tests.test_resolution_engine import MetaClient
    from capybase.adapters.llm_openai import LLMResponse

    repo = conflicted_repo["repo"]
    # First call: a runtime error -> request_failed -> retry.
    # Second call: a valid merged resolution -> accept.
    seq = [
        RuntimeError("connection timed out"),
        LLMResponse(
            text=_make_resolved_payload("    return 'hi' + 'howdy'"),
            raw={"choices": [{"finish_reason": "stop"}]},
        ),
    ]
    engine = ResolutionEngine(_config(repo).model, client=MetaClient(seq))
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert not result.escalated, result.reason
    assert "<<<<<<<" not in (repo / "app.py").read_text()


# ---------------------------------------------------------------------------
# Multi-unit-per-file (the regression class this whole fix targets)
# ---------------------------------------------------------------------------


def test_run_resolves_multi_unit_file(multi_unit_conflicted_repo):
    """Two hunks in one file: both must be resolved and accumulated into the
    final file. This is the direct regression test for the splice bug —
    previously only the last unit's resolution survived."""
    repo = multi_unit_conflicted_repo["repo"]
    payload1 = _make_resolved_payload(multi_unit_conflicted_repo["services_merged"])
    payload2 = _make_resolved_payload(multi_unit_conflicted_repo["flags_merged"])
    # Sequential: unit 0 (services) then unit 1 (flags).
    engine = ResolutionEngine(_config(repo).model, client=FakeClient([payload1, payload2]))
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert not result.escalated, result.reason
    text = (repo / "cfg.py").read_text()
    # No markers anywhere in the whole file.
    assert "<<<<<<<" not in text
    # BOTH resolutions present (the bug dropped the first one).
    assert "scheduler" in text and "reloader" in text
    assert '"cache": "on"' in text and '"metrics": "on"' in text


def test_manual_mode_resolves_multi_unit(multi_unit_conflicted_repo):
    """Manual mode must also accumulate both units' resolutions."""
    repo = multi_unit_conflicted_repo["repo"]
    inputs = [
        multi_unit_conflicted_repo["services_merged"],
        multi_unit_conflicted_repo["flags_merged"],
    ]
    orch = Orchestrator(
        _config(repo), repo=str(repo),
        stdin_reader=lambda _prompt: inputs.pop(0),
        out=lambda *_a, **_k: None,
    )
    result = orch.manual()
    assert not result.escalated, result.reason
    text = (repo / "cfg.py").read_text()
    assert "<<<<<<<" not in text
    assert "scheduler" in text and "reloader" in text
    assert '"cache": "on"' in text and '"metrics": "on"' in text


def test_run_escalates_when_whole_file_invalid(multi_unit_conflicted_repo):
    """Two candidates that individually pass Phase A but produce invalid Python
    when juxtaposed → Phase B (verify_file) fails → escalate.

    We craft both resolutions to be syntactically fine in isolation but to
    duplicate a definition across the file (a cross-unit error Phase A
    structurally cannot detect)."""
    repo = multi_unit_conflicted_repo["repo"]
    # Both hunks resolve to a top-level ``x = 1`` — valid alone, but two
    # module-level assignments aren't a syntax error per se. Instead make each
    # resolution an incomplete statement fragment so the spliced file is a
    # SyntaxError: bare ``return`` at module level.
    bad = _make_resolved_payload("return 1")
    engine = ResolutionEngine(_config(repo).model, client=FakeClient([bad, bad]))
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert result.escalated
    assert "whole-file validation failed" in (result.reason or "")

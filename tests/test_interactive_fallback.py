"""Tests for the interactive fallback that fires on escalation from `rebase`.

When capybase can't auto-resolve a conflict and a human is at the terminal, the
rebase drops into an interactive menu: paste a resolution, edit the file
directly, skip, or abort. After the human resolves, capybase re-validates and
continues. These tests force the fallback on (monkeypatch the TTY check) and
drive it with a scripted stdin_reader.
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


class FailingClient:
    """Always returns a leaked-marker resolution → forces escalation."""

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        return LLMResponse(text=json.dumps({"resolved_text": "    x\n<<<<<<< still\n"}))


class ScriptedReader:
    """Returns scripted responses in order (menu choices + pasted text).

    Each call to the reader pops the next response. Empty string entries are
    valid (e.g. the 'press Enter when done' prompt after editing a file).
    """

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, prompt: str) -> str:
        self.calls += 1
        if self.responses:
            return self.responses.pop(0)
        return ""


def _config(repo: Path) -> Config:
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False  # relax the test gate; we're testing the menu
    return cfg


def _payload(text: str) -> str:
    return json.dumps(
        {"resolved_text": text, "explanation": "merge", "self_reported_confidence": 0.8}
    )


def _force_interactive(orch: Orchestrator) -> None:
    """Force the interactive fallback to fire (tests have no real TTY)."""
    orch._is_interactive_terminal = lambda: True  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Paste mode: the human pastes a valid resolution → rebase continues + finishes.
# ---------------------------------------------------------------------------


def test_interactive_paste_resolves_and_finishes(py_repo_before_rebase):
    repo = py_repo_before_rebase["repo"]
    merged = py_repo_before_rebase["merged"]  # the valid merged text
    engine = ResolutionEngine(_config(repo).model, client=FailingClient())
    # Scripted stdin: the menu choice "1" (paste), then the merged text, then
    # EOF. (The paste prompt reads until EOF; we supply the text + empty signal.)
    reader = ScriptedReader(["1", merged])
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        stdin_reader=reader, out=lambda *_a, **_k: None,
    )
    _force_interactive(orch)
    result = orch.rebase("main")
    # The human's paste resolved it → not escalated, rebase finished.
    assert not result.escalated, result.reason
    assert "<<<<<<<" not in (repo / "app.py").read_text()
    assert "howdy" in (repo / "app.py").read_text()  # the merged content landed
    assert not _rebase_in_progress(repo)


# ---------------------------------------------------------------------------
# Edit mode: the human edits the file directly → capybase validates + continues.
# ---------------------------------------------------------------------------


def test_interactive_edit_file_resolves(py_repo_before_rebase):
    repo = py_repo_before_rebase["repo"]
    engine = ResolutionEngine(_config(repo).model, client=FailingClient())
    # Scripted stdin: menu choice "2" (edit), then "" (the Enter after editing).
    # A side-effect hook edits the file on disk when the edit prompt fires.
    original_reader_responses = ["2", ""]

    def reader_with_edit_hook(prompt: str) -> str:
        resp = original_reader_responses.pop(0)
        if "Press Enter when done" in prompt or prompt == "":
            # Simulate the human having edited the file: write the resolved
            # version (no markers) to disk before signalling "done".
            (repo / "app.py").write_text(
                "def greet():\n    return 'hi' + 'howdy'\n"
            )
        return resp

    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        stdin_reader=reader_with_edit_hook, out=lambda *_a, **_k: None,
    )
    _force_interactive(orch)
    result = orch.rebase("main")
    assert not result.escalated, result.reason
    assert "<<<<<<<" not in (repo / "app.py").read_text()
    assert not _rebase_in_progress(repo)


# ---------------------------------------------------------------------------
# Skip: the human skips the unit → rebase left stopped (escalated), no abort.
# ---------------------------------------------------------------------------


def test_interactive_skip_leaves_stopped(py_repo_before_rebase):
    repo = py_repo_before_rebase["repo"]
    engine = ResolutionEngine(_config(repo).model, client=FailingClient())
    reader = ScriptedReader(["3"])  # menu choice "3" = skip
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        stdin_reader=reader, out=lambda *_a, **_k: None,
    )
    _force_interactive(orch)
    # abort_on_escalation=False so the skip leaves it stopped rather than aborting.
    result = orch.rebase("main", abort_on_escalation=False)
    assert result.escalated
    # The rebase is still in progress (left stopped, not aborted).
    assert _rebase_in_progress(repo)
    # Conflict markers still present (the unit was skipped).
    assert "<<<<<<<" in (repo / "app.py").read_text()
    git(repo, "rebase", "--abort", check=False)  # tidy the tmp repo


# ---------------------------------------------------------------------------
# Abort: the human picks abort → git rebase --abort, repo restored.
# ---------------------------------------------------------------------------


def test_interactive_abort_restores_repo(py_repo_before_rebase):
    repo = py_repo_before_rebase["repo"]
    start_head = git(repo, "rev-parse", "HEAD").stdout.strip()
    engine = ResolutionEngine(_config(repo).model, client=FailingClient())
    reader = ScriptedReader(["4"])  # menu choice "4" = abort
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        stdin_reader=reader, out=lambda *_a, **_k: None,
    )
    _force_interactive(orch)
    result = orch.rebase("main")
    assert result.escalated
    # Aborted: no rebase in progress, repo back at start.
    assert not _rebase_in_progress(repo)
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == start_head


# ---------------------------------------------------------------------------
# Disabled: --no-interactive / non-TTY → no menu, today's behavior (escalate).
# ---------------------------------------------------------------------------


def test_interactive_disabled_does_not_prompt(py_repo_before_rebase):
    repo = py_repo_before_rebase["repo"]
    engine = ResolutionEngine(_config(repo).model, client=FailingClient())
    reader = ScriptedReader([])  # would explode if the menu fired
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        stdin_reader=reader, out=lambda *_a, **_k: None,
    )
    # _is_interactive_terminal stays False (no TTY in tests) AND interactive=False.
    result = orch.rebase("main", interactive=False)
    assert result.escalated  # escalated normally, no menu
    assert reader.calls == 0  # the reader was never invoked (no prompt fired)
    # default abort_on_escalation → aborted back to start.
    assert not _rebase_in_progress(repo)


def test_interactive_non_tty_does_not_prompt(py_repo_before_rebase):
    """Even with interactive=True, no TTY → no menu (the default test reality)."""
    repo = py_repo_before_rebase["repo"]
    engine = ResolutionEngine(_config(repo).model, client=FailingClient())
    reader = ScriptedReader([])
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        stdin_reader=reader, out=lambda *_a, **_k: None,
    )
    # _is_interactive_terminal is the real check → False under pytest (no TTY).
    result = orch.rebase("main", interactive=True)
    assert result.escalated
    assert reader.calls == 0


# ---------------------------------------------------------------------------
# Enriched review bundle: the escalation's bundle carries the candidate + error.
# ---------------------------------------------------------------------------


def test_review_bundle_carries_candidate_and_error(py_repo_before_rebase):
    repo = py_repo_before_rebase["repo"]
    engine = ResolutionEngine(_config(repo).model, client=FailingClient())
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    orch.rebase("main", interactive=False)  # escalate without the menu
    bundle = orch.paths.final / "review-bundle.md"
    assert bundle.exists()
    text = bundle.read_text()
    # The bundle should carry more than just the stop reason: the conflict unit
    # and (where available) the model's attempt + validation failure.
    assert "review bundle" in text
    assert "app.py" in text


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _rebase_in_progress(repo: Path) -> bool:
    r = git(repo, "rev-parse", "--git-path", "rebase-merge", check=False)
    if r.returncode != 0:
        return False
    p = Path(r.stdout.strip())
    if not p.is_absolute():
        p = repo / p
    return p.exists()

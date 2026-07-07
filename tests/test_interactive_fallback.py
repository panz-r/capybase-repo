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
    valid (e.g. the 'press Enter when done' prompt after editing a file). Accepts
    ``multiline`` (and any other kwargs) for signature parity with the real
    reader, ignoring them — the scripted response is returned whole either way.
    """

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, prompt: str, **_kwargs) -> str:
        self.calls += 1
        if self.responses:
            return self.responses.pop(0)
        return ""


def _config(repo: Path) -> Config:
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False  # relax the test gate; we're testing the menu
    cfg.validation.enable_per_unit_syntax_check = False  # fragmentary fake candidates
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


def test_interactive_edit_reprompts_when_markers_remain(py_repo_before_rebase):
    """Edit mode must RE-PROMPT (not abort) when the human presses Enter before
    resolving. Regression: a prior version printed "Re-offering" then returned
    False, which the caller treated as a skip — so a single premature Enter
    aborted the whole rebase. It must loop until the markers are gone."""
    repo = py_repo_before_rebase["repo"]
    engine = ResolutionEngine(_config(repo).model, client=FailingClient())
    enter_count = {"n": 0}
    # Menu: "2" (edit). Then Enter is read multiple times: the FIRST Enter
    # leaves markers (human hasn't resolved yet); the SECOND Enter follows a fix.
    responses = ["2"]

    def reader(prompt: str) -> str:
        if "Press Enter" in prompt or prompt == "":
            enter_count["n"] += 1
            if enter_count["n"] == 1:
                # First Enter: human pressed Enter WITHOUT resolving. Leave the
                # file marker-laden (the conflict is still on disk).
                return ""
            # Second Enter: human has now resolved. Write the clean version.
            (repo / "app.py").write_text(
                "def greet():\n    return 'hi' + 'howdy'\n"
            )
            return ""
        return responses.pop(0)

    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        stdin_reader=reader, out=lambda *_a, **_k: None,
    )
    _force_interactive(orch)
    result = orch.rebase("main")
    # The rebase completed (the second Enter cleared the markers), not aborted.
    assert not result.escalated, result.reason
    # The reader was called at least twice for Enter (the re-prompt happened).
    assert enter_count["n"] >= 2, "edit mode must re-prompt when markers remain"


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
# Regression: the default stdin reader must return a single line for the menu
# (the old impl read until EOF, so typing "4" + Enter blocked forever and the
# choice was swallowed — the program ignored the user until Ctrl-C).
# ---------------------------------------------------------------------------


def test_default_reader_single_line_returns_after_one_line(monkeypatch):
    """A menu choice (one line) returns immediately, not at EOF.

    This is the core of the bug report: typing ``4`` + Enter had no effect
    because the reader looped on ``input()`` until EOF. Single-line mode must
    read exactly one line and return.
    """
    from capybase.orchestrator import _default_stdin_reader

    # Simulate a terminal: input() yields "4" then, if called again, more lines
    # (which should NEVER be consumed in single-line mode).
    remaining = iter(["4", "should-not-be-read", "nor-this"])

    def fake_input(prompt=""):  # noqa: ANN001
        return next(remaining)

    monkeypatch.setattr("builtins.input", fake_input)
    result = _default_stdin_reader("  choice [1-4]: ")
    assert result == "4"
    # Single-line mode must NOT have drained the remaining lines.
    assert next(remaining) == "should-not-be-read"


def test_default_reader_multiline_reads_until_eof(monkeypatch):
    """Paste mode (multiline=True) reads all lines until EOF, as before."""
    from capybase.orchestrator import _default_stdin_reader

    lines = iter(["line one", "line two", "line three"])

    def fake_input(prompt=""):  # noqa: ANN001
        try:
            return next(lines)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr("builtins.input", fake_input)
    result = _default_stdin_reader("paste (Ctrl-D to finish): ", multiline=True)
    assert result == "line one\nline two\nline three"


def test_default_reader_single_line_eof_returns_empty(monkeypatch):
    """EOF on a single-line read (no input) returns "" instead of raising."""
    from capybase.orchestrator import _default_stdin_reader

    def fake_input(prompt=""):  # noqa: ANN001
        raise EOFError

    monkeypatch.setattr("builtins.input", fake_input)
    assert _default_stdin_reader("press Enter: ") == ""


def _rebase_in_progress(repo: Path) -> bool:
    r = git(repo, "rev-parse", "--git-path", "rebase-merge", check=False)
    if r.returncode != 0:
        return False
    p = Path(r.stdout.strip())
    if not p.is_absolute():
        p = repo / p
    return p.exists()

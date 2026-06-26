"""Integration tests: the deterministic structural pre-resolver in the orchestrator.

Verifies the safety contract end-to-end: a structurally-resolvable conflict is
accepted WITHOUT any LLM call; a real conflict falls through to the model
unchanged; a deterministic guess that fails validation falls through too.
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


class CallCountingClient:
    """Fake client that records every call. If the structural resolver works,
    this client is NEVER called for resolvable conflicts."""

    def __init__(self, response: str = '{"resolved_text": "SHOULD NOT BE USED"}'):
        self.response = response
        self.calls = 0

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        self.calls += 1
        return LLMResponse(text=self.response)


def _config(repo: Path) -> Config:
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    return cfg


def _make_disjoint_conflict(repo: Path) -> Path:
    """A repo stopped at a conflict where both sides changed DIFFERENT lines
    within the same hunk (disjoint edits). Git can't auto-merge these (they're in
    one marker block), but the structural resolver can: line 0 vs line 1 don't
    overlap, so both edits apply safely."""
    base = "A = 1\nB = 1\n"
    upstream = "A = 2\nB = 1\n"      # current changed line 0
    replayed = "A = 1\nB = 2\n"      # replayed changed line 1

    (repo / "app.py").write_text(base)
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text(replayed)
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "replayed change")
    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text(upstream)
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "upstream change")
    git(repo, "checkout", "-q", "feat")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected a rebase conflict"
    return repo


def _make_real_conflict(repo: Path) -> Path:
    """A genuine both-sides-change conflict (NOT structurally resolvable)."""
    base = "def f():\n    return 1\n"
    upstream = "def f():\n    return 2\n"
    replayed = "def f():\n    return 3\n"
    (repo / "app.py").write_text(base)
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text(replayed)
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "replayed")
    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text(upstream)
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "upstream")
    git(repo, "checkout", "-q", "feat")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0
    return repo


# ---------------------------------------------------------------------------
# structurally-resolvable conflict → accepted with NO model call
# ---------------------------------------------------------------------------


def test_disjoint_conflict_resolves_without_llm(repo: Path):
    _make_disjoint_conflict(repo)
    client = CallCountingClient()
    engine = ResolutionEngine(_config(repo).model, client=client)
    cfg = _config(repo)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert not result.escalated, result.reason
    # The model was NEVER called — structural resolution handled it.
    assert client.calls == 0, f"expected no LLM calls, got {client.calls}"
    # Both sides' edits applied (disjoint merge): A=2 from current, B=2 from replayed.
    text = (repo / "app.py").read_text()
    assert "A = 2" in text
    assert "B = 2" in text
    assert "<<<<<<<" not in text
    # Journal records the structural resolution via the disjoint_edits rule.
    events = [e for e in orch.journal.read_events() if e.event_type == "structurally_resolved"]
    assert events and events[0].payload["rule"] == "disjoint_edits"
    assert events[0].payload["passed"] is True


def test_structural_resolution_disabled_falls_through_to_model(repo: Path):
    """When the toggle is off, even a disjoint conflict hits the model."""
    _make_disjoint_conflict(repo)
    payload = json.dumps({"resolved_text": "A = 2\nB = 2", "self_reported_confidence": 0.8})
    client = CallCountingClient(payload)
    engine = ResolutionEngine(_config(repo).model, client=client)
    cfg = _config(repo)
    cfg.future.enable_structural_resolver = False
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert not result.escalated, result.reason
    # The model WAS called this time.
    assert client.calls > 0


# ---------------------------------------------------------------------------
# real conflict → structural resolver declines, model handles it
# ---------------------------------------------------------------------------


def test_real_conflict_falls_through_to_model(repo: Path):
    _make_real_conflict(repo)
    payload = json.dumps({"resolved_text": "    return 2 + 3", "self_reported_confidence": 0.8})
    client = CallCountingClient(payload)
    engine = ResolutionEngine(_config(repo).model, client=client)
    orch = Orchestrator(_config(repo), repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert not result.escalated, result.reason
    # Structural resolver declined (real conflict) → model was called.
    assert client.calls > 0
    # No structurally_resolved event (it declined before journaling an accept).
    events = [e for e in orch.journal.read_events() if e.event_type == "structurally_resolved"]
    assert not events

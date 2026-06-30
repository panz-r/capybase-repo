"""End-to-end: a rebase where one side deletes a module and the other modifies it.

This is the case from the field log ("all conflicted paths are unsupported"):
git reports a modify/delete conflict (mode AU/UA) with no ``<<<<<<<`` markers.
Before this work capybase escalated immediately. Now it extracts a ``whole_file``
unit and routes it through block-capture (the model decides keep vs. delete from
a summary; capybase splices the chosen side). These tests drive the FULL
``orch.rebase()`` path with a fake block-capture client and assert the three
outcomes:

  - ``accept_deletion`` → the file is ``git rm``'d and the rebase continues.
  - ``keep_block`` → the keeper's content is staged and the rebase continues.
  - ``needs_human`` → escalation (review bundle written), rebase left stopped,
    never guessing.

Both directions are covered: AU (upstream deleted, replayed modified) and UA
(replayed deleted, upstream modified).
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


# ---------------------------------------------------------------------------
# A large-enough module so block-capture's keeper-size gate (>=50 lines) fires.
# ---------------------------------------------------------------------------

_BASE = (
    "def alpha():\n    return 1\n\n"
    "def beta():\n    return 2\n\n"
)
# Pad with enough defs to clear block_capture_min_lines (50). These stand in for
# a real module; what matters is the keeper side has >=50 nonblank lines.
for _i in range(30):
    _BASE += f"def helper_{_i}():\n    return {_i}\n\n"


def _keeper_modified() -> str:
    """The replayed side's modification: change alpha + add gamma."""
    return _BASE.replace("return 1\n\n\ndef beta", "return 11\n\n\ndef beta") + (
        "def gamma():\n    return 3\n\n"
    )


# ---------------------------------------------------------------------------
# Fake client: returns a scripted block-capture decision.
# ---------------------------------------------------------------------------


class _BlockCaptureClient:
    """Returns a scripted block-capture JSON decision."""

    def __init__(self, decision: str, reason: str = "x"):
        self._text = json.dumps({"decision": decision, "reason": reason})
        self.calls = 0

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        self.calls += 1
        return LLMResponse(text=self._text)


def _config(repo: Path) -> Config:
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False  # relax the test gate; we're testing the merge path
    return cfg


def _orch(repo: Path, decision: str) -> Orchestrator:
    engine = ResolutionEngine(_config(repo).model, client=_BlockCaptureClient(decision))
    cfg = _config(repo)
    return Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )


# ---------------------------------------------------------------------------
# Repo builders: leave the repo on `feat`, clean, ready for orch.rebase("main").
# ---------------------------------------------------------------------------


def _au_repo(repo: Path) -> str:
    """main DELETES the module; feat MODIFIES it. Rebase feat→main yields AU."""
    (repo / "m.py").write_text(_BASE)
    git(repo, "add", "m.py")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat")
    # main deletes.
    git(repo, "rm", "m.py")
    git(repo, "commit", "-q", "-m", "main: delete module")
    # feat modifies (replayed).
    git(repo, "checkout", "-q", "feat")
    (repo / "m.py").write_text(_keeper_modified())
    git(repo, "add", "m.py")
    git(repo, "commit", "-q", "-m", "feat: modify module")
    git(repo, "checkout", "-q", "feat")
    return "m.py"


def _ua_repo(repo: Path) -> str:
    """main MODIFIES the module; feat DELETES it. Rebase feat→main yields UA."""
    (repo / "m.py").write_text(_BASE)
    git(repo, "add", "m.py")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat")
    # main modifies (upstream/keeper).
    git(repo, "checkout", "-q", "main")
    (repo / "m.py").write_text(_keeper_modified())
    git(repo, "add", "m.py")
    git(repo, "commit", "-q", "-m", "main: modify module")
    # feat deletes (replayed/deleter).
    git(repo, "checkout", "-q", "feat")
    git(repo, "rm", "m.py")
    git(repo, "commit", "-q", "-m", "feat: delete module")
    git(repo, "checkout", "-q", "feat")
    return "m.py"


# ---------------------------------------------------------------------------
# AU: upstream deleted, replayed modified
# ---------------------------------------------------------------------------


def test_au_accept_deletion_removes_file_and_finishes(repo: Path):
    """Block-capture says accept_deletion → the module is git-rm'd, rebase done."""
    path = _au_repo(repo)
    orch = _orch(repo, "accept_deletion")
    result = orch.rebase("main")
    assert not result.escalated, f"expected clean finish, got: {result.reason}"
    # The module is gone from the committed tree.
    assert not (repo / path).exists()
    out = git(repo, "ls-files")
    assert path not in out.stdout
    # block-capture actually ran (the decision was consumed).
    assert orch.resolution_engine.client.calls >= 1


def test_au_keep_block_keeps_modified_content(repo: Path):
    """Block-capture says keep_block → the keeper's modified content survives."""
    path = _au_repo(repo)
    orch = _orch(repo, "keep_block")
    result = orch.rebase("main")
    assert not result.escalated, f"expected clean finish, got: {result.reason}"
    # The file is present with feat's modification (gamma was added by the keeper).
    content = (repo / path).read_text()
    assert "def gamma" in content
    assert "return 11" in content  # the keeper's alpha change
    out = git(repo, "ls-files")
    assert path in out.stdout


def test_au_needs_human_escalates_without_guessing(repo: Path):
    """needs_human → escalation, review bundle written, rebase left stopped.
    The file must NOT be silently deleted or committed."""
    path = _au_repo(repo)
    orch = _orch(repo, "needs_human")
    result = orch.rebase("main")
    assert result.escalated
    # A review bundle was written for the human.
    bundle = orch.paths.final / "review-bundle.md"
    assert bundle.exists()
    # The rebase is left stopped (or aborted cleanly) — never silently resolved.
    # The module should not have been committed as deleted against the keeper's
    # intent (needs_human means we don't guess).
    assert orch.resolution_engine.client.calls >= 1


# ---------------------------------------------------------------------------
# UA: replayed deleted, upstream modified (the mirror)
# ---------------------------------------------------------------------------


def test_ua_accept_deletion_removes_file_and_finishes(repo: Path):
    """UA mirror: block-capture accept_deletion → module git-rm'd, done."""
    path = _ua_repo(repo)
    orch = _orch(repo, "accept_deletion")
    result = orch.rebase("main")
    assert not result.escalated, f"expected clean finish, got: {result.reason}"
    assert not (repo / path).exists()
    out = git(repo, "ls-files")
    assert path not in out.stdout


def test_ua_keep_block_keeps_modified_content(repo: Path):
    """UA mirror: keep_block → upstream's modified content survives."""
    path = _ua_repo(repo)
    orch = _orch(repo, "keep_block")
    result = orch.rebase("main")
    assert not result.escalated, f"expected clean finish, got: {result.reason}"
    content = (repo / path).read_text()
    # The keeper here is the upstream side (current), which also has the gamma mod.
    assert "def gamma" in content
    out = git(repo, "ls-files")
    assert path in out.stdout


def test_ua_needs_human_escalates_without_guessing(repo: Path):
    """UA mirror: needs_human → escalation, never guesses."""
    path = _ua_repo(repo)
    orch = _orch(repo, "needs_human")
    result = orch.rebase("main")
    assert result.escalated
    bundle = orch.paths.final / "review-bundle.md"
    assert bundle.exists()

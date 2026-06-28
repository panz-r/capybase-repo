"""Tests for ``capybase check`` — the pre-flight confidence command.

``check`` aggregates git-state, calibration-presence, tooling, and an LLM ping
into one "ready to rebase?" report. The LLM ping uses an injectable
``client_factory`` (mirroring ``_run_calibrate``) so these tests are hermetic.

Exit-code contract: 0 when ready (warnings allowed), non-zero on any blocking
failure.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from capybase.adapters.llm_openai import LLMResponse
from capybase.cli import _run_check
from capybase.config import Config

from tests.conftest import git


class Reachable:
    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        return LLMResponse(text="ok")


class Unreachable:
    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        raise ConnectionError("boom")


def _cfg() -> Config:
    cfg = Config()
    cfg.model.model = "fake"
    return cfg


def _init_repo(repo: Path) -> None:
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")


def test_check_ready_when_llm_reachable(repo: Path):
    _init_repo(repo)
    buf = io.StringIO()
    rc = _run_check(_cfg(), repo=str(repo), out=buf, client_factory=lambda mc: Reachable())
    text = buf.getvalue()
    assert rc == 0
    assert "ready to rebase" in text
    # The git-state checks pass.
    assert "[ok  ] git-repo" in text
    assert "[ok  ] on-branch" in text
    assert "[ok  ] llm-reachable" in text


def test_check_blocks_when_llm_unreachable(repo: Path):
    _init_repo(repo)
    buf = io.StringIO()
    rc = _run_check(_cfg(), repo=str(repo), out=buf, client_factory=lambda mc: Unreachable())
    text = buf.getvalue()
    assert rc != 0
    assert "[FAIL ] llm-reachable" in text
    assert "NOT ready to rebase" in text


def test_check_reports_config_source_and_tools(repo: Path):
    _init_repo(repo)
    buf = io.StringIO()
    rc = _run_check(_cfg(), repo=str(repo), out=buf, client_factory=lambda mc: Reachable())
    text = buf.getvalue()
    assert "config source" in text
    assert "tools" in text and "pyright" in text
    assert rc == 0


def test_check_warns_on_missing_profile(repo: Path):
    _init_repo(repo)
    cfg = _cfg()
    # Point profile at a path that doesn't exist.
    cfg.calibration.model_profile_path = str(Path(repo) / "nope" / "model_profile.json")
    buf = io.StringIO()
    rc = _run_check(cfg, repo=str(repo), out=buf, client_factory=lambda mc: Reachable())
    text = buf.getvalue()
    assert "absent" in text
    # Missing profile is a warning, not a blocker.
    assert rc == 0


def test_check_blocks_on_in_progress_operation(repo: Path):
    _init_repo(repo)
    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    # feat changes the SAME line main will change → cherry-pick conflicts.
    (repo / "a.txt").write_text("from feat\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "feat")
    git(repo, "checkout", "-q", "main")
    (repo / "a.txt").write_text("from main\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "main")
    # Cherry-pick feat onto main — conflicts, leaving CHERRY_PICK_HEAD.
    feat = git(repo, "rev-parse", "feat").stdout.strip()
    git(repo, "cherry-pick", feat, check=False)

    buf = io.StringIO()
    rc = _run_check(_cfg(), repo=str(repo), out=buf, client_factory=lambda mc: Reachable())
    text = buf.getvalue()
    assert rc != 0
    assert "cherry-pick" in text
    git(repo, "cherry-pick", "--abort", check=False)

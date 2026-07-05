"""Tests for the test-gated side picker (survey §4.2 / conftest port pattern).

When both pre-LLM resolvers (structural + SBCR) decline a same-line scalar
conflict where taking either side verbatim is plausible, the side picker tries
each side and lets the TEST GATE discriminate. The test gate is the
discriminator: it knows which value is correct (e.g. a test asserting
``port == 9090``), so it accepts the side that matches and rejects the other.
This resolves value conflicts the deterministic resolvers correctly decline
(there's no deterministic answer for 9090 vs 7070) WITHOUT an LLM call.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from capybase.config import Config
from capybase.orchestrator import Orchestrator

from tests.conftest import git


def _git(repo: Path, *args: str, **kw) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(
        GIT_AUTHOR_NAME="t", GIT_COMMITTER_NAME="t",
        GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_EMAIL="t@e",
        GIT_AUTHOR_DATE="2000-01-01", GIT_COMMITTER_DATE="2000-01-01",
    )
    return subprocess.run(
        ["git", "-C", str(repo), *args], env=env,
        capture_output=True, text=True, input=kw.get("input_text"),
    )


def _make_value_conflict(repo: Path) -> dict:
    """A repo stopped at a UU conflict over a config value, with a test asserting
    the upstream value. Both sides change the same line (base=8080, upstream=9090,
    replayed=7070); the test asserts ==9090."""
    base = "PORT = 8080\n"
    upstream = "PORT = 9090\n"  # current side
    replayed = "PORT = 7070\n"  # replayed side
    # A test that asserts PORT == 9090 (only the upstream value passes).
    test_file = (
        "from app import PORT\n"
        "def test_port():\n"
        "    assert PORT == 9090\n"
    )

    (repo / "app.py").write_text(base)
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "__init__.py").write_text("")
    (repo / "tests" / "test_app.py").write_text(test_file)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")

    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text(replayed)
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "replayed: port 7070")

    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text(upstream)
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "upstream: port 9090")

    git(repo, "checkout", "-q", "feat")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected a rebase conflict"
    return {"repo": repo, "path": "app.py"}


def _config(repo: Path) -> Config:
    cfg = Config()
    cfg.model.model = "fake"
    cfg.model.samples = 1
    cfg.model.enable_self_consistency = False
    cfg.tests.required = True
    # Scope pytest to the tmp repo ONLY: -c /dev/null prevents pytest from
    # walking up to find capybase's pyproject.toml (which would collect
    # capybase's 2000+ tests that pass regardless of the merged file).
    cfg.tests.pre_continue = f"{sys.executable} -m pytest -v -c /dev/null tests/"
    cfg.tests.final = cfg.tests.pre_continue
    return cfg


def test_test_gated_side_picks_upstream_value(repo):
    """The side picker tries the upstream side (PORT=9090) → test passes → accepted.
    The replayed side (PORT=7070) would fail the test. No LLM call needed."""
    fixture = _make_value_conflict(repo)
    cfg = _config(repo)
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    result = orch.run()
    # The side picker resolved it: PORT=9090 (the value the test asserts).
    text = (repo / "app.py").read_text()
    assert "9090" in text, f"expected 9090, got: {text}"
    assert "7070" not in text
    assert "<<<<<<<" not in text


def test_test_gated_side_declines_when_no_test_configured(repo):
    """When no real test command is configured (the `true` no-op shim), the side
    picker declines — there's no way to discriminate → falls through to the LLM."""
    fixture = _make_value_conflict(repo)
    cfg = _config(repo)
    cfg.tests.pre_continue = "true"  # no-op; can't discriminate
    cfg.tests.final = "true"
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    # The side picker should NOT fire (no real test). The LLM (fake client, none
    # configured) will fail → escalate. The key assertion: no side was picked.
    result = orch.run()
    assert result.escalated  # fell through to LLM, which has no client → escalate


def test_test_gated_side_declines_when_both_sides_fail_tests(repo):
    """When NEITHER side passes the test gate, the picker declines (restores the
    worktree, falls through). A test asserting PORT==8080 (neither 9090 nor 7070)
    means both sides fail."""
    fixture = _make_value_conflict(repo)
    # Override the test to assert a value NEITHER side has (after the conflict
    # setup created the tests/ dir and the base test file).
    (repo / "tests" / "test_app.py").write_text(
        "from app import PORT\n"
        "def test_port():\n"
        "    assert PORT == 8080\n"  # neither 9090 nor 7070
    )
    cfg = _config(repo)
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    result = orch.run()
    # Both sides fail the test → picker declined → LLM (no client) → escalate.
    assert result.escalated

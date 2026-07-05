"""Integration tests for the orchestrator's cross-commit dependency guardian
(Phase 3 / survey §3.1).

Exercises the wired-in completion-path audit: build a source branch where an
early commit defines a symbol and a later commit references it, point the final
HEAD at a tree where that symbol is gone (renamed away), and assert the guardian
surfaces the break — closing the per-commit blind spot no per-commit validator
sees. Mirrors the resurrection_orchestrator test pattern (direct method call,
decoupled from git's auto-resolve heuristics).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from capybase.adapters import structural
from capybase.config import Config
from capybase.orchestrator import Orchestrator
from capybase.session import SessionPaths

from tests.conftest import git

pytestmark = pytest.mark.skipif(
    not structural.is_available("python"),
    reason="tree-sitter Python grammar unavailable",
)


def _make_repo(repo: Path) -> dict:
    """A repo whose source branch DEFINES a symbol in an early commit and
    REFERENCES it in a later commit — the cross-commit dependency structure.

      base  : empty app.py (defines nothing)
      feat  : commit A defines foo(); commit B calls foo() — so foo is a symbol
              committed in A and used across-commit in B.

    Returns the merge-base OID and the source-tip OID the guardian needs.
    """
    (repo / "app.py").write_text("# empty\n")
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "base")
    base_oid = git(repo, "rev-parse", "HEAD").stdout.strip()

    # Source branch (feat): commit A defines foo, commit B references foo.
    git(repo, "checkout", "-q", "-b", "feat")
    (repo / "app.py").write_text("def foo():\n    return 1\n")
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "feat: add foo()")  # commit A (defines foo)
    (repo / "app.py").write_text("def foo():\n    return 1\n\ndef main():\n    return foo()\n")
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "feat: add main() calling foo()")  # commit B (uses foo)
    feat_tip = git(repo, "rev-parse", "HEAD").stdout.strip()

    return {"base_oid": base_oid, "source_tip": feat_tip}


def _orch(repo: Path, *, policy: str = "warn") -> Orchestrator:
    cfg = Config()
    cfg.validation.enable_cross_commit_guardian = True
    cfg.validation.cross_commit_policy = policy
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    orch.paths = SessionPaths("t", repo_root=repo)
    # The plan builder persists rebase_plan.json under paths.root; ensure it exists.
    orch.paths.root.mkdir(parents=True, exist_ok=True)
    return orch


def test_guardian_finds_rename_away_break(tmp_path: Path):
    """The headline case: the final rebased tree renamed ``foo``→``bar``, but a
    replayed commit still calls ``foo`` (the old name). The per-commit validators
    all passed locally; the window-level guardian flags the now-missing name."""
    repo = tmp_path
    git(repo, "init", "-q", "-b", "main")
    ctx = _make_repo(repo)
    orch = _orch(repo)

    # Build the history plan from the source sequence (base..feat-tip).
    orch._history_plan = orch._build_rebase_plan(ctx["source_tip"], ctx["base_oid"])
    assert orch._history_plan is not None, "plan must build for the guardian to run"
    assert len(orch._history_plan.source_commits) >= 1

    # Point HEAD at a tree where foo was renamed to bar (foo gone by name) —
    # this expresses the post-rebase break. feat-tip's tree has foo; make a new
    # commit off feat that renames foo→bar and set HEAD there.
    git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text("def bar():\n    return 1\n\ndef main():\n    return foo()\n")
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "rename foo->bar (final tree)")
    # HEAD is now the renamed tree.

    result = orch._run_cross_commit_guardian_on_completion()
    # The guardian found at least one cross-commit break (foo referenced by the
    # earlier replayed commit, gone by name in the final tree).
    assert result is not None, "expected the guardian to flag the rename-away break"
    # In "warn" policy it surfaces but does not escalate.
    assert not result.escalated


def test_guardian_clean_when_symbol_survives(tmp_path: Path):
    """When the dependency symbol survives in the final tree by name, the
    guardian finds nothing (returns None)."""
    repo = tmp_path
    git(repo, "init", "-q", "-b", "main")
    ctx = _make_repo(repo)
    orch = _orch(repo)
    orch._history_plan = orch._build_rebase_plan(ctx["source_tip"], ctx["base_oid"])
    assert orch._history_plan is not None
    # HEAD stays at feat-tip (foo present by name) → no break.
    git(repo, "checkout", "-q", "feat")
    result = orch._run_cross_commit_guardian_on_completion()
    assert result is None


def test_guardian_disabled_is_noop(tmp_path: Path):
    """When enable_cross_commit_guardian is False, the guardian short-circuits."""
    repo = tmp_path
    git(repo, "init", "-q", "-b", "main")
    ctx = _make_repo(repo)
    cfg = Config()
    cfg.validation.enable_cross_commit_guardian = False
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    orch.paths = SessionPaths("t", repo_root=repo)
    orch.paths.root.mkdir(parents=True, exist_ok=True)
    orch._history_plan = orch._build_rebase_plan(ctx["source_tip"], ctx["base_oid"])
    git(repo, "checkout", "-q", "feat")
    assert orch._run_cross_commit_guardian_on_completion() is None


def test_guardian_no_plan_is_noop(tmp_path: Path):
    """With no history plan, the guardian degrades to a no-op (no crash)."""
    repo = tmp_path
    git(repo, "init", "-q", "-b", "main")
    git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    orch = _orch(repo)
    orch._history_plan = None
    assert orch._run_cross_commit_guardian_on_completion() is None

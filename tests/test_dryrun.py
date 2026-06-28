"""Tests for the full temp-worktree dry-run rehearsal.

The dry-run runs the ENTIRE rebase pipeline in a throwaway linked worktree and
reports whether it would succeed — without ever moving the user's branch pointer.
These tests assert the two invariants that make a dry-run trustworthy:

1. The user's real branch pointers (HEAD, the branch, main) are UNCHANGED after
   the rehearsal, regardless of outcome.
2. The throwaway worktree + dry-run branch are cleaned up afterward.

They also cover both outcomes (would-succeed, would-escalate) using a fake LLM,
mirroring test_rebase_command.py's CyclingClient/FailingClient pattern.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from capybase.adapters.llm_openai import LLMResponse
from capybase.config import Config
from capybase.dryrun import rehearse_rebase
from capybase.resolution_engine import ResolutionEngine

from tests.conftest import git


class CyclingClient:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        if len(self.responses) > 1:
            return LLMResponse(text=self.responses.pop(0))
        return LLMResponse(text=self.responses[0])


class FailingClient:
    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        return LLMResponse(text=json.dumps({"resolved_text": "    x\n<<<<<<< still\n"}))


def _config(repo: Path) -> Config:
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = True
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    return cfg


def _payload(text: str) -> str:
    return json.dumps(
        {"resolved_text": text, "explanation": "merge", "self_reported_confidence": 0.8}
    )


def _branch_oid(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", ref).stdout.strip()


def _worktree_count(repo: Path) -> int:
    return len(git(repo, "worktree", "list").stdout.strip().splitlines())


# ---------------------------------------------------------------------------
# Invariant: the real branch pointer is never moved.
# ---------------------------------------------------------------------------


def test_dryrun_clean_rebase_succeeds_and_leaves_pointers_unchanged(py_repo_clean_rebase):
    repo = py_repo_clean_rebase
    feat_before = _branch_oid(repo, "feat")
    main_before = _branch_oid(repo, "main")
    assert _worktree_count(repo) == 1

    engine = ResolutionEngine(_config(repo).model, client=FailingClient())
    report = rehearse_rebase(_config(repo), repo, "main", resolution_engine=engine)

    assert report.would_succeed, report.summary()
    # CRITICAL: real branch pointers unchanged.
    assert _branch_oid(repo, "feat") == feat_before
    assert _branch_oid(repo, "main") == main_before
    # Worktree cleaned up.
    assert _worktree_count(repo) == 1


def test_dryrun_resolvable_conflict_succeeds_and_leaves_pointers_unchanged(
    py_repo_before_rebase,
):
    repo = py_repo_before_rebase["repo"]
    merged_block = py_repo_before_rebase["merged_block"]
    feat_before = _branch_oid(repo, "feat")
    main_before = _branch_oid(repo, "main")

    engine = ResolutionEngine(
        _config(repo).model, client=CyclingClient([_payload(merged_block)])
    )
    report = rehearse_rebase(_config(repo), repo, "main", resolution_engine=engine)

    assert report.would_succeed, report.summary()
    assert _branch_oid(repo, "feat") == feat_before
    assert _branch_oid(repo, "main") == main_before
    assert _worktree_count(repo) == 1


def test_dryrun_unresolvable_conflict_reports_escalation_and_leaves_pointers_unchanged(
    py_repo_before_rebase,
):
    repo = py_repo_before_rebase["repo"]
    feat_before = _branch_oid(repo, "feat")
    main_before = _branch_oid(repo, "main")

    engine = ResolutionEngine(_config(repo).model, client=FailingClient())
    report = rehearse_rebase(_config(repo), repo, "main", resolution_engine=engine)

    assert not report.would_succeed, report.summary()
    # The escalation is captured in the report, not raised.
    assert report.errors or any(s.escalated for s in report.steps)
    # Still: pointers unchanged, worktree gone.
    assert _branch_oid(repo, "feat") == feat_before
    assert _branch_oid(repo, "main") == main_before
    assert _worktree_count(repo) == 1


# ---------------------------------------------------------------------------
# Preflight gating: a bad real-repo state aborts before any worktree is made.
# ---------------------------------------------------------------------------


def test_dryrun_blocks_on_detached_head(py_repo_before_rebase):
    repo = py_repo_before_rebase["repo"]
    feat_before = _branch_oid(repo, "feat")
    git(repo, "checkout", "-q", "--detach", "HEAD")

    from capybase.git_backend import GitError
    engine = ResolutionEngine(_config(repo).model, client=FailingClient())
    with pytest.raises(GitError, match="detached"):
        rehearse_rebase(_config(repo), repo, "main", resolution_engine=engine)

    # No worktree was created.
    assert _worktree_count(repo) == 1
    # Back on a branch for cleanup.
    git(repo, "checkout", "-q", "feat")
    assert _branch_oid(repo, "feat") == feat_before


def test_dryrun_report_has_summary():
    """The report's summary() is a readable multi-line string."""
    from capybase.dryrun import RehearsalReport, RehearsalStep

    r = RehearsalReport(would_succeed=True, target="main", head_before="abcdef1", head_after="1234567")
    r.steps.append(RehearsalStep(step=1, accepted=True, detail="app.py"))
    s = r.summary()
    assert "DRY RUN: would succeed" in s
    assert "step 1 [ACCEPT]" in s

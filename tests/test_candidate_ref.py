"""Candidate-ref rebase mode (candidate-ref-architecture-design P1).

The invariants that make the mode trustworthy:
1. The source branch OID is UNCHANGED after the run, in BOTH outcomes —
   the mutation-free contract, by construction.
2. On success: the candidate branch exists at the rebased OID, the audit
   bundle (journal + session_state.json) is retained in the real repo,
   and the state file records the exact OIDs + fingerprints P2's
   promotion will verify.
3. On escalation: the candidate branch is deleted, nothing is retained
   except the state file (outcome=escalated), and the source is
   untouched.
4. No orphaned worktrees.
"""

from __future__ import annotations

import json
from pathlib import Path

from capybase.adapters.llm_openai import LLMResponse
from capybase.candidate_ref import (
    CANDIDATES_DIR,
    run_candidate_rebase,
)
from capybase.config import Config
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
        return LLMResponse(text=json.dumps(
            {"resolved_text": "    x\n<<<<<<< still\n"}))


def _config(repo: Path) -> Config:
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = True
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    # Let the (failing/fake) model decide — disable the deterministic
    # layers so both outcomes are reachable.
    cfg.future.enable_source_portfolio = False
    cfg.future.enable_structural_resolver = False
    return cfg


def _payload(text: str) -> str:
    return json.dumps(
        {"resolved_text": text, "explanation": "merge",
         "self_reported_confidence": 0.8}
    )


def _branch_oid(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", ref).stdout.strip()


def _candidate_branches(repo: Path) -> list[str]:
    out = git(repo, "branch", "--list", "capybase/candidate/*").stdout
    return [l.strip().lstrip("* ").strip() for l in out.splitlines() if l.strip()]


def _worktree_count(repo: Path) -> int:
    return len(git(repo, "worktree", "list").stdout.strip().splitlines())


def test_candidate_success_retains_branch_and_never_touches_source(
    py_repo_before_rebase,
):
    repo = py_repo_before_rebase["repo"]
    merged_block = py_repo_before_rebase["merged_block"]
    feat_before = _branch_oid(repo, "feat")
    main_before = _branch_oid(repo, "main")

    engine = ResolutionEngine(
        _config(repo).model, client=CyclingClient([_payload(merged_block)])
    )
    report = run_candidate_rebase(
        _config(repo), repo, "main", resolution_engine=engine)

    assert report.would_succeed, report.summary()
    # THE contract: source untouched.
    assert _branch_oid(repo, "feat") == feat_before
    assert _branch_oid(repo, "main") == main_before
    # The candidate branch is retained at the rebased OID.
    cands = _candidate_branches(repo)
    assert len(cands) == 1, cands
    assert _branch_oid(repo, cands[0]) == report.candidate_oid
    assert report.candidate_oid != feat_before  # the series actually replayed
    # The state file: OIDs + fingerprints for P2's promotion.
    state = json.loads(Path(report.state_path).read_text())
    assert state["source_oid"] == feat_before
    assert state["target_oid"] == main_before
    assert state["candidate_oid"] == report.candidate_oid
    assert state["outcome"] == "success"
    assert state["fingerprints"]["config"]
    assert "toolchain" in state["fingerprints"]
    # The audit bundle retained the session journal.
    session_dir = Path(report.state_path).parent / "session"
    assert (session_dir / "journal.jsonl").exists()
    # No orphaned worktree.
    assert _worktree_count(repo) == 1


def test_candidate_escalation_deletes_candidate_and_never_touches_source(
    py_repo_before_rebase,
):
    repo = py_repo_before_rebase["repo"]
    feat_before = _branch_oid(repo, "feat")

    engine = ResolutionEngine(_config(repo).model, client=FailingClient())
    report = run_candidate_rebase(
        _config(repo), repo, "main", resolution_engine=engine)

    assert report.escalated
    assert _branch_oid(repo, "feat") == feat_before
    # Nothing to promote: the candidate branch is gone.
    assert _candidate_branches(repo) == []
    # The state file records the honest outcome.
    state = json.loads(Path(report.state_path).read_text())
    assert state["outcome"] == "escalated"
    assert state["candidate_oid"] is None
    # No orphaned worktree.
    assert _worktree_count(repo) == 1


def test_candidate_summary_prints_expected_oid_cas(py_repo_before_rebase):
    """The report's promotion line is the EXPLICIT expected-OID form —
    the design's core rule (update-ref <new> <expected-old>)."""
    repo = py_repo_before_rebase["repo"]
    merged_block = py_repo_before_rebase["merged_block"]
    engine = ResolutionEngine(
        _config(repo).model, client=CyclingClient([_payload(merged_block)])
    )
    report = run_candidate_rebase(
        _config(repo), repo, "main", resolution_engine=engine)
    assert report.would_succeed
    line = next(
        ln for ln in report.summary().splitlines() if "update-ref" in ln)
    assert report.source_oid in line and report.candidate_oid in line
    assert "refs/heads/feat" in line


def test_candidates_dir_layout(py_repo_before_rebase):
    """Retained bundles live under .rebase-agent/candidates/ in the REAL
    repo (the worktree is removed; the artifact outlives it)."""
    repo = py_repo_before_rebase["repo"]
    merged_block = py_repo_before_rebase["merged_block"]
    engine = ResolutionEngine(
        _config(repo).model, client=CyclingClient([_payload(merged_block)])
    )
    report = run_candidate_rebase(
        _config(repo), repo, "main", resolution_engine=engine)
    state_path = Path(report.state_path)
    assert CANDIDATES_DIR in str(state_path)
    assert state_path.is_relative_to(repo if isinstance(repo, Path) else Path(repo))

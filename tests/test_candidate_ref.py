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


# ---------------------------------------------------------------------------
# P2: compare-and-swap promotion
# ---------------------------------------------------------------------------

from capybase.candidate_ref import promote_candidate  # noqa: E402


def _successful_candidate(repo, merged_block):
    """Run a candidate to success; return (report, feat_before)."""
    feat_before = _branch_oid(repo, "feat")
    engine = ResolutionEngine(
        _config(repo).model, client=CyclingClient([_payload(merged_block)])
    )
    report = run_candidate_rebase(
        _config(repo), repo, "main", resolution_engine=engine)
    assert report.would_succeed, report.summary()
    return report, feat_before


def test_promote_cas_moves_source_exactly(py_repo_before_rebase):
    repo = py_repo_before_rebase["repo"]
    report, feat_before = _successful_candidate(
        repo, py_repo_before_rebase["merged_block"])

    result = promote_candidate(repo, approve=True)
    assert result.promoted, result.summary()
    # The source moved to EXACTLY the candidate OID.
    assert _branch_oid(repo, "feat") == report.candidate_oid
    assert _branch_oid(repo, "feat") != feat_before
    # The state file records the promotion.
    state = json.loads(Path(result.state_path).read_text())
    assert state["promoted"]["to"] == report.candidate_oid
    # The consumed candidate branch is deleted by default.
    assert _candidate_branches(repo) == []


def test_promote_refuses_on_drift(py_repo_before_rebase):
    """THE rule: any drift refuses, never forces — both OIDs named."""
    repo = py_repo_before_rebase["repo"]
    report, feat_before = _successful_candidate(
        repo, py_repo_before_rebase["merged_block"])
    # Simulate drift: the source branch moved after the candidate ran.
    git(repo, "commit", "--allow-empty", "-q", "-m", "drift")
    drifted = _branch_oid(repo, "feat")

    result = promote_candidate(repo)
    assert not result.promoted
    assert "DRIFT" in result.summary()
    assert drifted[:8] in result.summary()
    assert feat_before[:8] in result.summary()
    # The source was NOT moved to the candidate.
    assert _branch_oid(repo, "feat") == drifted
    # The candidate branch is retained for inspection.
    assert len(_candidate_branches(repo)) == 1


def test_promote_checkout_updates_clean_tree_refuses_dirty(
    py_repo_before_rebase,
):
    repo = py_repo_before_rebase["repo"]
    report, feat_before = _successful_candidate(
        repo, py_repo_before_rebase["merged_block"])
    # feat is NOT checked out here (HEAD is on it in these fixtures?
    # drive both branches of the checkout dance explicitly).
    git(repo, "checkout", "-q", "feat")

    # Clean tree + --checkout → the worktree follows the ref.
    result = promote_candidate(repo, checkout=True, approve=True)
    assert result.promoted, result.summary()
    assert result.checked_out_updated
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == report.candidate_oid

    # Dirty tree + --checkout → refused BEFORE any ref move.
    report2, feat2 = _successful_candidate(
        repo, py_repo_before_rebase["merged_block"])
    (repo / "uncommitted.txt").write_text("dirty\n")
    result2 = promote_candidate(repo, checkout=True, approve=True)
    assert not result2.promoted
    assert "uncommitted" in result2.summary().lower()
    assert _branch_oid(repo, "feat") == report.candidate_oid  # unmoved


def test_promote_no_candidate_refuses_cleanly(py_repo_before_rebase):
    repo = py_repo_before_rebase["repo"]
    result = promote_candidate(repo)
    assert not result.promoted
    assert "no retained successful candidate" in result.summary()


def test_cli_rebase_defaults_to_candidate_mode(
    py_repo_before_rebase, monkeypatch, tmp_path,
):
    """P2 default flip: plain `capybase rebase <tgt>` routes to the
    candidate mode; --in-place opts back into the legacy path."""
    import capybase.candidate_ref as cref
    from capybase import cli

    repo = py_repo_before_rebase["repo"]
    called = {"candidate": 0, "in_place": 0}

    class _Rep:
        would_succeed = True
        def summary(self):
            return "fake candidate report"

    monkeypatch.setattr(
        cref, "run_candidate_rebase",
        lambda *a, **k: (called.__setitem__("candidate", called["candidate"] + 1), _Rep())[1])
    monkeypatch.setattr(
        cli.Orchestrator, "rebase",
        lambda self, *a, **k: (called.__setitem__("in_place", called["in_place"] + 1)) or type("R", (), {"escalated": False})())

    cfg = tmp_path / "c.toml"
    cfg.write_text("")

    # The strict calibration gate: resolution commands refuse without a
    # provider. Inject a synthetic ResolvedProvider (in-memory profile)
    # so the DISPATCH is what's under test.
    import capybase.provider_config as pcfg
    from capybase.calibration_profile import ModelProfile
    from capybase.provider_config import ProviderConfig, ResolvedProvider

    _mp = ModelProfile(model="fake-model")
    _fake = ResolvedProvider(
        provider=ProviderConfig(
            name="t", profile="synthetic", base_url="http://x",
            model="fake-model", api_key="k"),
        profile=_mp, profile_path="synthetic",
    )
    monkeypatch.setattr(pcfg, "resolve_provider", lambda **k: _fake)

    cli.main(["--config", str(cfg), "--repo", str(repo), "rebase", "main"])
    assert called["candidate"] == 1 and called["in_place"] == 0

    cli.main(["--config", str(cfg), "--repo", str(repo),
              "rebase", "--in-place", "main"])
    assert called["candidate"] == 1 and called["in_place"] == 1


# ---------------------------------------------------------------------------
# P4: fingerprint-matched reuse + transitions
# ---------------------------------------------------------------------------


def test_reuse_returns_retained_candidate_without_rerunning(
    py_repo_before_rebase,
):
    """The promotable-artifact contract: a second run with identical
    fingerprints returns the retained candidate — ZERO model calls."""
    repo = py_repo_before_rebase["repo"]
    merged = py_repo_before_rebase["merged_block"]
    calls = {"n": 0}

    class CountingClient:
        def complete(self, *a, **k):
            calls["n"] += 1
            return LLMResponse(text=_payload(merged))

    from capybase.adapters.llm_openai import LLMResponse as _R

    engine = ResolutionEngine(_config(repo).model, client=CountingClient())
    first = run_candidate_rebase(
        _config(repo), repo, "main", resolution_engine=engine)
    assert first.would_succeed
    assert calls["n"] > 0
    feat_before = _branch_oid(repo, "feat")

    second = run_candidate_rebase(
        _config(repo), repo, "main", resolution_engine=engine)
    assert second.reused
    assert second.candidate_oid == first.candidate_oid
    assert calls["n"] == 0 or second.reused  # no NEW calls (reused before run)
    assert second.llm_calls == 0
    assert _branch_oid(repo, "feat") == feat_before
    assert "reused" in second.summary().lower()
    # One candidate branch only — reuse didn't create another.
    assert len(_candidate_branches(repo)) == 1


def test_fresh_flag_reruns_despite_matching_candidate(py_repo_before_rebase):
    repo = py_repo_before_rebase["repo"]
    merged = py_repo_before_rebase["merged_block"]
    engine = ResolutionEngine(
        _config(repo).model, client=CyclingClient([_payload(merged)]))
    first = run_candidate_rebase(
        _config(repo), repo, "main", resolution_engine=engine)
    assert first.would_succeed and not first.reused

    second = run_candidate_rebase(
        _config(repo), repo, "main", resolution_engine=engine, reuse=False)
    assert not second.reused and second.would_succeed
    assert second.session_id != first.session_id


def test_toolchain_mismatch_blocks_reuse(py_repo_before_rebase, monkeypatch):
    """Evidence from a different toolchain is not the same evidence —
    the unknown-is-not-pass rule at the artifact level."""
    import capybase.candidate_ref as cref

    repo = py_repo_before_rebase["repo"]
    merged = py_repo_before_rebase["merged_block"]
    engine = ResolutionEngine(
        _config(repo).model, client=CyclingClient([_payload(merged)]))
    first = run_candidate_rebase(
        _config(repo), repo, "main", resolution_engine=engine)
    assert first.would_succeed

    # Simulate a changed environment (a compiler appeared/disappeared).
    real = cref._toolchain_fingerprint

    def _changed():
        d = real()
        d["gcc"] = not d["gcc"]
        return d

    monkeypatch.setattr(cref, "_toolchain_fingerprint", _changed)
    second = run_candidate_rebase(
        _config(repo), repo, "main", resolution_engine=engine)
    assert not second.reused and second.would_succeed


def test_interrupted_state_is_never_reused(py_repo_before_rebase):
    """outcome=None (worktree died mid-series) is not promotable: git
    only advances the branch at completion, so there IS nothing
    mid-series to resume from — re-run happens instead."""
    repo = py_repo_before_rebase["repo"]
    merged = py_repo_before_rebase["merged_block"]
    engine = ResolutionEngine(
        _config(repo).model, client=CyclingClient([_payload(merged)]))
    first = run_candidate_rebase(
        _config(repo), repo, "main", resolution_engine=engine)
    # Corrupt the retained state into an interrupted shape.
    sp = Path(first.state_path)
    state = json.loads(sp.read_text())
    state["outcome"] = None
    sp.write_text(json.dumps(state))

    second = run_candidate_rebase(
        _config(repo), repo, "main", resolution_engine=engine)
    assert not second.reused and second.would_succeed


def test_state_records_transitions(py_repo_before_rebase):
    repo = py_repo_before_rebase["repo"]
    merged = py_repo_before_rebase["merged_block"]
    engine = ResolutionEngine(
        _config(repo).model, client=CyclingClient([_payload(merged)]))
    report = run_candidate_rebase(
        _config(repo), repo, "main", resolution_engine=engine)
    state = json.loads(Path(report.state_path).read_text())
    names = [t["name"] for t in state["transitions"]]
    assert names == ["snapshot", "completed"]
    assert state["transitions"][0]["source_oid"] == report.source_oid


# ---------------------------------------------------------------------------
# P3 remainder: the tier-table policy gates promotion
# ---------------------------------------------------------------------------


def test_policy_module_tier_table():
    """acceptance.decide's table: deterministic+complete → A; unknown
    oracle → B; verifier disagreement on an accepted unit → C/STOP."""
    from capybase.acceptance import AUTO_APPLY, PROPOSE_FOR_REVIEW, STOP, decide

    class _U:
        unit_id = "u1"
    class _V:
        features = {"syntax_passed": True}
        warnings = []
    class _C_det:
        provenance = "deterministic_structural"
        suspected_validator_error = False
    class _O:
        def __init__(self, val, cand):
            self.unit = _U(); self.validation = val; self.accepted = cand

    det = decide([_O(_V(), _C_det())], True)
    assert (det.tier, det.decision) == ("A", AUTO_APPLY)

    class _C_model:
        provenance = "plain_llm"
        suspected_validator_error = False
    model = decide([_O(_V(), _C_model())], True)
    assert (model.tier, model.decision) == ("B", PROPOSE_FOR_REVIEW)

    class _V_unknown:
        features = {"syntax_outcome": "unknown"}
        warnings = []
    unk = decide([_O(_V_unknown(), _C_det())], True)
    assert (unk.tier, unk.decision) == ("B", PROPOSE_FOR_REVIEW)
    assert any("unknown" in r.lower() for r in unk.reasons)

    class _C_disagree:
        provenance = "deterministic_structural"
        suspected_validator_error = True
    dis = decide([_O(_V(), _C_disagree())], True)
    assert (dis.tier, dis.decision) == ("C", STOP)
    assert "verifier disagreement" in dis.reasons[0].lower()


def test_promote_refuses_tier_b_without_approve(py_repo_before_rebase):
    """The test fixtures resolve via the LLM (plain_llm provenance) —
    tier B. Promotion refuses; --approve is the review act."""
    repo = py_repo_before_rebase["repo"]
    merged = py_repo_before_rebase["merged_block"]
    engine = ResolutionEngine(
        _config(repo).model, client=CyclingClient([_payload(merged)]))
    report = run_candidate_rebase(
        _config(repo), repo, "main", resolution_engine=engine)
    assert report.would_succeed
    assert report.policy_tier == "B", report.policy_reasons

    refused = promote_candidate(repo)
    assert not refused.promoted
    assert "--approve" in refused.summary()
    assert _branch_oid(repo, "feat") != report.candidate_oid  # unmoved

    approved = promote_candidate(repo, approve=True)
    assert approved.promoted, approved.summary()
    assert _branch_oid(repo, "feat") == report.candidate_oid


# ---------------------------------------------------------------------------
# P5: lease-protected remote publication (hermetic bare-repo remote)
# ---------------------------------------------------------------------------


def _repo_with_remote(py_repo_before_rebase, tmp_path):
    """The fixture repo + a bare 'origin' holding the source branch.

    The bare lives OUTSIDE the repo worktree (tmp_path is the repo root
    for these fixtures — a sibling remote.git/ reads as untracked and
    trips the preflight's dirty-tree check).
    """
    import subprocess as sp
    import tempfile

    repo = py_repo_before_rebase["repo"]
    bare = Path(tempfile.mkdtemp(prefix="p5-remote-")) / "remote.git"
    sp.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    # Push the current branches to the bare remote as the starting state.
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "origin", "main")
    git(repo, "push", "-q", "origin", "feat")
    git(repo, "fetch", "-q", "origin")
    return repo, bare


def _remote_ref_oid(bare, ref="refs/heads/feat"):
    import subprocess as sp

    out = sp.run(
        ["git", "--git-dir", str(bare), "rev-parse", ref],
        capture_output=True, text=True, check=True)
    return out.stdout.strip()


def test_publish_lease_pushes_and_local_source_untouched(
    py_repo_before_rebase, tmp_path,
):
    repo, bare = _repo_with_remote(py_repo_before_rebase, tmp_path)
    merged = py_repo_before_rebase["merged_block"]
    engine = ResolutionEngine(
        _config(repo).model, client=CyclingClient([_payload(merged)]))
    report = run_candidate_rebase(
        _config(repo), repo, "main", resolution_engine=engine)
    assert report.would_succeed
    feat_before = _branch_oid(repo, "feat")

    from capybase.candidate_ref import publish_candidate
    result = publish_candidate(repo, approve=True)
    assert result.published, result.summary()
    assert _remote_ref_oid(bare) == report.candidate_oid
    # Publishing the remote does NOT move the local source (promote does).
    assert _branch_oid(repo, "feat") == feat_before
    state = json.loads(Path(report.state_path).read_text())
    assert state["published"]["remote"] == "origin"


def test_publish_lease_refuses_when_remote_moved(
    py_repo_before_rebase, tmp_path,
):
    repo, bare = _repo_with_remote(py_repo_before_rebase, tmp_path)
    merged = py_repo_before_rebase["merged_block"]
    engine = ResolutionEngine(
        _config(repo).model, client=CyclingClient([_payload(merged)]))
    report = run_candidate_rebase(
        _config(repo), repo, "main", resolution_engine=engine)
    assert report.would_succeed

    # The remote MOVED after the snapshot (someone else pushed).
    git(repo, "fetch", "-q", "origin")
    git(repo, "branch", "-q", "-f", "tmp-remote-head",
        _remote_ref_oid(bare))
    import subprocess as sp
    env = dict(**__import__("os").environ)
    git(repo, "commit", "--allow-empty", "-q", "-m", "remote-side change",
        "--author=a <a@a>")
    # Push the local feat (now with an extra commit) to move the remote.
    git(repo, "push", "-q", "-f", "origin", "feat")
    moved = _remote_ref_oid(bare)

    from capybase.candidate_ref import publish_candidate
    result = publish_candidate(repo, approve=True)
    assert not result.published
    assert "lease" in result.summary().lower()
    assert _remote_ref_oid(bare) == moved  # NOT overwritten — never forced


def test_publish_tier_b_refuses_without_approve(py_repo_before_rebase, tmp_path):
    repo, bare = _repo_with_remote(py_repo_before_rebase, tmp_path)
    merged = py_repo_before_rebase["merged_block"]
    engine = ResolutionEngine(
        _config(repo).model, client=CyclingClient([_payload(merged)]))
    report = run_candidate_rebase(
        _config(repo), repo, "main", resolution_engine=engine)
    assert report.would_succeed and report.policy_tier == "B"

    from capybase.candidate_ref import publish_candidate
    result = publish_candidate(repo)
    assert not result.published
    assert "--approve" in result.summary()
    # The remote is untouched.
    assert _remote_ref_oid(bare) == report.source_oid


def test_publish_dry_run_transfers_nothing(py_repo_before_rebase, tmp_path):
    repo, bare = _repo_with_remote(py_repo_before_rebase, tmp_path)
    merged = py_repo_before_rebase["merged_block"]
    engine = ResolutionEngine(
        _config(repo).model, client=CyclingClient([_payload(merged)]))
    run_candidate_rebase(
        _config(repo), repo, "main", resolution_engine=engine)
    remote_before = _remote_ref_oid(bare)

    from capybase.candidate_ref import publish_candidate
    result = publish_candidate(repo, approve=True, dry_run=True)
    assert result.published  # the lease held in rehearsal
    assert _remote_ref_oid(bare) == remote_before  # nothing transferred

"""Integration tests for the orchestrator against real temp git repos.

A fake LLM client (no network) returns a pre-baked merged resolution so the
full M3 loop — extract → propose → verify → risk → splice → stage → continue
— can be exercised end to end without a live model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from capybase.adapters.llm_openai import LLMResponse
from capybase.config import Config
from capybase.orchestrator import Orchestrator, StepResult
from capybase.resolution_engine import ResolutionEngine

from tests.conftest import git


class FakeClient:
    """Returns canned JSON responses in order; repeats the last one forever."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        if self.responses:
            r = self.responses.pop(0)
        else:
            raise RuntimeError("no more fake responses")
        return LLMResponse(text=r)


class CyclingClient:
    """Like FakeClient but repeats the final response indefinitely.

    Used where the orchestrator may retry; avoids brittle payload counting.
    """

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls = 0

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        self.calls += 1
        if len(self.responses) > 1:
            return LLMResponse(text=self.responses.pop(0))
        return LLMResponse(text=self.responses[0])


def _config(tmp_path: Path, *, tests_required: bool = True, pre_continue: str | None = "true") -> Config:
    cfg = Config()
    cfg.model.model = "fake"
    # The hermetic suite scripts exact fake-client responses for single-
    # resolution flows; force samples=1 so the production default (samples=3 +
    # self-consistency) doesn't triple the candidate draw and exhaust them. Tests
    # that exercise the multi-sample path set samples explicitly.
    cfg.model.samples = 1
    cfg.model.enable_self_consistency = False
    cfg.tests.required = tests_required
    cfg.tests.pre_continue = pre_continue  # `true` always exits 0
    cfg.tests.final = pre_continue
    # The per-unit syntax validators (PythonSyntaxValidator/RustSyntaxValidator)
    # are a production feature; the hermetic suite's fake clients produce partial
    # conflict-region snippets (not complete parseable files), so the per-unit
    # compile would false-fail on them. Disable here — the validators have their
    # own dedicated tests with complete code.
    cfg.validation.enable_per_unit_syntax_check = False
    # Write artifacts under the repo's .rebase-agent (cwd of the repo).
    return cfg


def _make_resolved_payload(text: str) -> str:
    return json.dumps({"resolved_text": text, "explanation": "merge", "self_reported_confidence": 0.8})


# ---------------------------------------------------------------------------
# M1: inspect (no mutation)
# ---------------------------------------------------------------------------


def test_inspect_no_mutation(conflicted_repo):
    repo = conflicted_repo["repo"]
    before = (repo / "app.py").read_text()
    orch = Orchestrator(_config(repo), repo=str(repo))
    result = orch.inspect()
    assert not result.escalated
    # worktree file untouched
    assert (repo / "app.py").read_text() == before
    # one conflict unit extracted
    assert "app.py" in result.units_by_path
    # review bundle written
    assert (orch.paths.final / "review-bundle.md").exists()
    # journal exists
    assert orch.paths.journal.exists()


def test_inspect_no_rebase(repo):
    orch = Orchestrator(_config(repo), repo=str(repo))
    result = orch.inspect()
    assert result.escalated
    assert "no rebase" in (result.reason or "")


# ---------------------------------------------------------------------------
# M2: manual mode
# ---------------------------------------------------------------------------


def test_manual_mode_resolves(conflicted_repo):
    repo = conflicted_repo["repo"]
    # Manual mode reads the literal resolved text (not JSON).
    inputs = ["    return 'merged'"]
    orch = Orchestrator(
        _config(repo), repo=str(repo),
        stdin_reader=lambda _prompt, **_kw: inputs.pop(0),
        out=lambda *_a, **_k: None,
    )
    result = orch.manual()
    assert not result.escalated
    # file no longer has markers
    text = (repo / "app.py").read_text()
    assert "<<<<<<<" not in text
    assert "merged" in text
    # staged
    staged = git(repo, "diff", "--cached", "--name-only")
    assert "app.py" in staged.stdout


def test_manual_mode_rejects_bad_resolution(conflicted_repo):
    repo = conflicted_repo["repo"]
    # resolution that leaves a marker -> validation fails
    inputs = ["    x\n<<<<<<< leaked\n"]
    orch = Orchestrator(
        _config(repo), repo=str(repo),
        stdin_reader=lambda _prompt, **_kw: inputs.pop(0),
        out=lambda *_a, **_k: None,
    )
    result = orch.manual()
    assert result.escalated


# ---------------------------------------------------------------------------
# M3: full run (fake model)
# ---------------------------------------------------------------------------


def test_run_resolves_and_continues(conflicted_repo):
    repo = conflicted_repo["repo"]
    # A resolution that merges both sides (differs from either verbatim) so the
    # preservation heuristic does not force retries.
    payload = _make_resolved_payload("    return 'hi' + 'howdy'")
    engine = ResolutionEngine(_config(repo).model, client=CyclingClient([payload]))
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    # rebase completed cleanly
    assert not result.escalated, result.reason
    # no conflict markers anywhere
    assert "<<<<<<<" not in (repo / "app.py").read_text()
    # rebase no longer in progress
    r = git(repo, "rebase", "--abort", check=False)  # ensure clean state readable
    # HEAD should be the replayed branch tip rebased onto main.
    log = git(repo, "log", "--oneline").stdout
    assert "replayed change" in log


def test_run_journals_prompt_trims_when_context_window_is_tight(conflicted_repo):
    """With context_window set, an over-large prompt is trimmed and the trims
    are journaled on the candidate_generated event."""
    repo = conflicted_repo["repo"]
    cfg = _config(repo)
    # A very tight window: the boilerplate (intro+contract+rules) is ~300 tokens,
    # so even this small conflict's full prompt exceeds it → augmentations trimmed.
    cfg.model.context_window = 350
    cfg.model.completion_reserve = 10
    payload = _make_resolved_payload("    return 'hi' + 'howdy'")
    engine = ResolutionEngine(cfg.model, client=CyclingClient([payload]))
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    orch.run()
    # Read the journal and find a candidate_generated event with prompt_trims.
    events = []
    for line in orch.paths.journal.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            d = json.loads(line)
            if d["event_type"] == "candidate_generated":
                events.append(d.get("payload", {}))
    trimmed = [e for e in events if e.get("prompt_trims")]
    assert trimmed, "expected a candidate_generated event carrying prompt_trims"
    assert any(t["section"] for t in trimmed[0]["prompt_trims"])


def test_run_no_prompt_trims_when_context_window_disabled(conflicted_repo):
    """context_window=0 (default) → no trimming, no prompt_trims in the journal."""
    repo = conflicted_repo["repo"]
    cfg = _config(repo)
    assert cfg.model.context_window == 0  # disabled by default
    payload = _make_resolved_payload("    return 'hi' + 'howdy'")
    engine = ResolutionEngine(cfg.model, client=CyclingClient([payload]))
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    orch.run()
    for line in orch.paths.journal.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            d = json.loads(line)
            if d["event_type"] == "candidate_generated":
                assert not d.get("prompt_trims"), "no trims when window disabled"


def test_run_skips_llm_when_conflict_oversized_for_window(conflicted_repo):
    """When the conflict SIDES alone exceed the available context window, the
    LLM call is doomed (server truncates) — skip it and escalate instead of
    wasting the call. An llm_skipped_oversized event is journaled and no
    candidate_generated event appears."""
    repo = conflicted_repo["repo"]
    cfg = _config(repo)
    # A window so tiny that even the small test conflict's sides don't fit.
    # completion_reserve=1 → available = 5 - 1 = 4 tokens; the sides are ~30+
    # tokens → oversized. (The trim test uses 350 which fits the sides; here we
    # go below the sides to trigger the hopeless case.)
    cfg.model.context_window = 5
    cfg.model.completion_reserve = 1
    payload = _make_resolved_payload("    return 'hi' + 'howdy'")
    client = CyclingClient([payload])
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    orch.run()
    events = {}
    for line in orch.paths.journal.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            d = json.loads(line)
            events[d["event_type"]] = d.get("payload", {})
    # The oversized-skip event fired.
    assert "llm_skipped_oversized" in events, "expected llm_skipped_oversized event"
    assert events["llm_skipped_oversized"]["essential_tokens"] > events["llm_skipped_oversized"]["available_tokens"]
    # No LLM candidate was generated (the call was skipped).
    assert "candidate_generated" not in events, "LLM should not have been called"
    # The fake client was never asked to complete (the call was skipped pre-loop).
    assert client.calls == 0, f"LLM client should not have been called, got {client.calls} calls"


def test_run_does_not_skip_llm_when_window_disabled(conflicted_repo):
    """context_window=0 (default) → the size guard is a no-op; the LLM runs
    normally even for large conflicts (historical behavior)."""
    repo = conflicted_repo["repo"]
    cfg = _config(repo)
    assert cfg.model.context_window == 0  # disabled
    payload = _make_resolved_payload("    return 'hi' + 'howdy'")
    engine = ResolutionEngine(cfg.model, client=CyclingClient([payload]))
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    orch.run()
    has_oversized = False
    has_candidate = False
    for line in orch.paths.journal.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            d = json.loads(line)
            if d["event_type"] == "llm_skipped_oversized":
                has_oversized = True
            if d["event_type"] == "candidate_generated":
                has_candidate = True
    assert not has_oversized, "size guard must not fire when window is disabled"
    assert has_candidate, "LLM should run normally when window is disabled"


def test_run_escalates_when_model_returns_markers(conflicted_repo):
    repo = conflicted_repo["repo"]
    # model keeps returning a leaked marker across all retries -> escalate
    payload = _make_resolved_payload("    x\n<<<<<<< still\n")
    engine = ResolutionEngine(_config(repo).model, client=CyclingClient([payload]))
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert result.escalated
    assert (orch.paths.final / "review-bundle.md").exists()


def test_run_escalates_on_needs_human(conflicted_repo):
    repo = conflicted_repo["repo"]
    payload = json.dumps({"resolved_text": "    return 1", "needs_human": True})
    engine = ResolutionEngine(_config(repo).model, client=CyclingClient([payload]))
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert result.escalated


def test_run_aborts_tests_when_required_and_failing(conflicted_repo):
    repo = conflicted_repo["repo"]
    payload = _make_resolved_payload("    return 'hi' + 'howdy'")
    engine = ResolutionEngine(_config(repo).model, client=CyclingClient([payload]))
    cfg = _config(repo, tests_required=True, pre_continue="false")  # exits 1
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert result.escalated
    assert "tests failed" in (result.reason or "")


# ---------------------------------------------------------------------------
# Step 3: rank-order candidate validation (try the next sample if the
# consensus winner fails validation, before falling back to CEGIS repair)
# ---------------------------------------------------------------------------


class FakeConsensusEngine:
    """Returns a fixed candidate list + trivial consensus report.

    Mimics ResolutionEngine.propose_with_consensus so the orchestrator's
    self-consistency path can be driven with controlled candidates without a
    live model. The candidates are returned in the order given (index 0 is the
    consensus "winner").
    """

    def __init__(self, candidates):
        from capybase.consensus import ConsensusReport

        self._candidates = list(candidates)
        # A unanimous report so the risk engine doesn't escalate on entropy/
        # agreement — we want to isolate the rank-order validation behavior.
        self._report = ConsensusReport(
            winner=candidates[0] if candidates else None,
            clusters=[],
            n_samples=len(candidates),
            agreement_score=1.0,
            cluster_count=1,
            entropy=0.0,
        )

    def propose_with_consensus(self, unit, context, *, failures=None,
                               prev_candidate=None, n_samples=None):
        return list(self._candidates), self._report


def _self_consistency_config(repo):
    """Enable self-consistency so the orchestrator takes the multi-candidate path."""
    cfg = _config(repo)
    cfg.future.enable_self_consistency = True
    return cfg


def _cand(text, *, cid="c"):
    from capybase.conflict_model import CandidateResolution

    return CandidateResolution(
        candidate_id=cid, unit_id="u", model_name="fake",
        prompt_version="v", resolved_text=text,
    )


def test_run_accepts_second_candidate_when_winner_fails(conflicted_repo):
    """The consensus winner has a syntax error; the 2nd sample is valid.

    Step 3 says "discard that candidate immediately" — the orchestrator should
    validate the 2nd/3rd samples (already in memory) and accept the first that
    passes, rather than discarding all N and jumping to CEGIS regeneration.
    """
    repo = conflicted_repo["repo"]
    # Winner: leaks a conflict marker -> per-unit validation fails
    # (no_conflict_markers is a hard check the per-unit validator enforces).
    # 2nd: a valid merge of both sides -> per-unit AND whole-file pass.
    # 3rd: also valid (untouched, the loop stops at the 2nd).
    engine = FakeConsensusEngine([
        _cand("    x\n<<<<<<< leaked\n", cid="winner-broken"),
        _cand("    return 'hi' + 'howdy'", cid="second-valid"),
        _cand("    return 'hi' + 'howdy'", cid="third-valid"),
    ])
    orch = Orchestrator(
        _self_consistency_config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert not result.escalated, result.reason
    # The accepted candidate is the second (valid) one, not the broken winner.
    assert result.outcomes
    accepted = result.outcomes[0].accepted
    assert accepted is not None
    assert accepted.candidate_id == "second-valid"
    # No markers leaked into the file.
    assert "<<<<<<<" not in (repo / "app.py").read_text()


def test_run_escalates_when_all_candidates_fail(conflicted_repo):
    """When every surviving candidate fails validation, fall back to the normal
    retry/escalate path (the winner's failures feed CEGIS repair)."""
    repo = conflicted_repo["repo"]
    engine = FakeConsensusEngine([
        _cand("    return 'hi'(", cid="a-broken"),
        _cand("    return 'howdy'(", cid="b-broken"),
        _cand("    x\n<<<<<<< leaked\n", cid="c-marker"),
    ])
    orch = Orchestrator(
        _self_consistency_config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    # All candidates fail across retries -> escalation (CEGIS repair is itself a
    # fresh generation via the same FakeConsensusEngine, which keeps failing).
    assert result.escalated



def test_run_retries_after_transient_error(conflicted_repo):
    """A request_failed candidate (timeout/network) should retry, then succeed."""
    from tests.test_resolution_engine import MetaClient
    from capybase.adapters.llm_openai import LLMResponse

    repo = conflicted_repo["repo"]
    # First call: a runtime error -> request_failed -> retry.
    # Second call: a valid merged resolution -> accept.
    seq = [
        RuntimeError("connection timed out"),
        LLMResponse(
            text=_make_resolved_payload("    return 'hi' + 'howdy'"),
            raw={"choices": [{"finish_reason": "stop"}]},
        ),
    ]
    engine = ResolutionEngine(_config(repo).model, client=MetaClient(seq))
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert not result.escalated, result.reason
    assert "<<<<<<<" not in (repo / "app.py").read_text()


# ---------------------------------------------------------------------------
# Multi-unit-per-file (the regression class this whole fix targets)
# ---------------------------------------------------------------------------


def test_run_resolves_multi_unit_file(multi_unit_conflicted_repo):
    """Two hunks in one file: both must be resolved and accumulated into the
    final file. This is the direct regression test for the splice bug —
    previously only the last unit's resolution survived."""
    repo = multi_unit_conflicted_repo["repo"]
    payload1 = _make_resolved_payload(multi_unit_conflicted_repo["services_merged"])
    payload2 = _make_resolved_payload(multi_unit_conflicted_repo["flags_merged"])
    # Sequential: unit 0 (services) then unit 1 (flags).
    engine = ResolutionEngine(_config(repo).model, client=FakeClient([payload1, payload2]))
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert not result.escalated, result.reason
    text = (repo / "cfg.py").read_text()
    # No markers anywhere in the whole file.
    assert "<<<<<<<" not in text
    # BOTH resolutions present (the bug dropped the first one).
    assert "scheduler" in text and "reloader" in text
    assert '"cache": "on"' in text and '"metrics": "on"' in text


def test_manual_mode_resolves_multi_unit(multi_unit_conflicted_repo):
    """Manual mode must also accumulate both units' resolutions."""
    repo = multi_unit_conflicted_repo["repo"]
    inputs = [
        multi_unit_conflicted_repo["services_merged"],
        multi_unit_conflicted_repo["flags_merged"],
    ]
    orch = Orchestrator(
        _config(repo), repo=str(repo),
        stdin_reader=lambda _prompt, **_kw: inputs.pop(0),
        out=lambda *_a, **_k: None,
    )
    result = orch.manual()
    assert not result.escalated, result.reason
    text = (repo / "cfg.py").read_text()
    assert "<<<<<<<" not in text
    assert "scheduler" in text and "reloader" in text
    assert '"cache": "on"' in text and '"metrics": "on"' in text


def test_run_escalates_when_whole_file_invalid(multi_unit_conflicted_repo):
    """Two candidates that individually pass Phase A but produce invalid Python
    when juxtaposed → Phase B (verify_file) fails. With execution-driven
    whole-file CEGIS, the system now attempts to REPAIR (feed the cross-unit
    failure back to the unit), escalating only when the repair also fails.

    Here the FakeClient has no responses left for the repair attempt, so the
    re-resolution fails and the file escalates with a whole-file repair
    message (not the old immediate "whole-file validation failed")."""
    repo = multi_unit_conflicted_repo["repo"]
    # Both hunks resolve to a bare ``return`` at module level: valid alone in
    # the per-unit context but a SyntaxError when juxtaposed at module scope.
    bad = _make_resolved_payload("return 1")
    cfg = _config(repo)
    # The ``return 1`` candidate deliberately drops both sides' content — it's
    # not a real merge. This test is about Phase B (whole-file juxtaposition),
    # so relax the Phase A both-sides-represented check so the candidate passes
    # Phase A and actually reaches Phase B (the behavior under test). The
    # dependency-preservation check (P3) is likewise relaxed: it would flag the
    # same dropped-content pattern and reroute to a retry before Phase B.
    cfg.validation.reject_if_drops_a_side = False
    cfg.validation.reject_if_drops_referenced_symbol = False
    engine = ResolutionEngine(cfg.model, client=FakeClient([bad, bad]))
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert result.escalated
    # New behavior: repair was attempted, then failed → escalate.
    assert "whole-file" in (result.reason or "")


def test_whole_file_repair_recovers_and_accepts(multi_unit_conflicted_repo):
    """Execution-driven whole-file CEGIS: both units pass per-unit validation
    in isolation, but unit 1's first resolution breaks the file when juxtaposed
    (an unclosed bracket). The whole-file validator catches it, feeds the
    concrete SyntaxError back to unit 1, which re-resolves to the valid merge
    on the repair attempt. The file is then ACCEPTED (not escalated).

    This is the survey §4 principle: ground the model's correction in concrete
    execution feedback instead of escalating the cross-unit error."""
    repo = multi_unit_conflicted_repo["repo"]
    services = multi_unit_conflicted_repo["services_merged"]   # unit 0, valid
    # Per-unit-valid-but-whole-file-invalid: an unclosed paren survives the
    # per-unit splice (where the sibling block is blanked) but breaks the full
    # file when both resolutions are juxtaposed.
    flags_broken = '    "cache": "on", "metrics": "on"\n    extra_stale_line('
    flags_good = multi_unit_conflicted_repo["flags_merged"]
    # Sequence: unit0(services), unit1(flags broken) → whole-file fails →
    # repair re-resolves unit1 → flags_good. CyclingClient repeats the last.
    client = CyclingClient([
        _make_resolved_payload(services),
        _make_resolved_payload(flags_broken),
        _make_resolved_payload(flags_good),
    ])
    engine = ResolutionEngine(_config(repo).model, client=client)
    orch = Orchestrator(
        _config(repo), repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert not result.escalated, result.reason
    text = (repo / "cfg.py").read_text()
    assert "<<<<<<<" not in text
    # Both merges present after repair.
    assert "scheduler" in text and "reloader" in text
    assert '"cache": "on"' in text and '"metrics": "on"' in text


# ---------------------------------------------------------------------------
# Verifier-model critic integration (surveys §1/§5): the LLM judge gates the
# orchestrator's accept path end-to-end when enable_verifier_model is on.
# ---------------------------------------------------------------------------


class SequenceClient:
    """Serves canned responses in strict order; raises if exhausted.

    Unlike CyclingClient, this lets a test script an exact call sequence —
    resolution payloads followed by critic verdicts — so we can assert the
    critic's effect on accept vs escalate.
    """

    def __init__(self, responses: list[str]):
        self.responses = list(responses)

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        if not self.responses:
            raise RuntimeError("no more fake responses")
        return LLMResponse(text=self.responses.pop(0))


def _verifier_config(repo):
    cfg = _config(repo)
    cfg.validation.enable_verifier_model = True
    # Disable the critic guardrail phases in the raw-critic-behavior tests: they
    # exercise the critic's verdict→escalation path with fake clients that return
    # the verdict schema (not the reassessment schema), so Phase 2 would squash a
    # genuine drop via null-evidence. The guardrail has its own test module.
    cfg.validation.enable_verifier_assertion = False
    cfg.validation.enable_verifier_reflection = False
    cfg.validation.enable_verifier_guardrail = False
    return cfg


def test_verifier_blocks_accept_when_it_flags_dropped_intent(distinct_additions_repo, verifier_critic_enabled):
    """Flag on + critic says the resolution drops a side → NOT accepted. The
    candidate is structurally clean (no markers, valid merge) so the syntactic
    validators pass; only the semantic critic catches the dropped intent, and at
    error severity it blocks the accept path (escalation).

    Uses a DISTINCT-ADDITIONS conflict (each side adds a different import) so a
    one-sided merge genuinely drops an addition — the critic SHOULD block it.
    (A same-line value conflict like ``return 'hi'`` vs ``return 'howdy'`` is a
    value resolution where one-sided merging is correct, so it wouldn't test the
    blocking path.)"""
    repo = distinct_additions_repo["repo"]
    # 1st call: a structurally-clean, one-sided resolution. 2nd call: the critic
    # verdict saying the replayed side's import was dropped.
    client = SequenceClient([
        _make_resolved_payload(distinct_additions_repo["current_only"]),  # drops replayed
        json.dumps({"preserves_current": True, "preserves_replayed": False,
                    "reason": "dropped import sys", "confidence": 0.9}),
    ])
    cfg = _verifier_config(repo)
    cfg.validation.verifier_severity = "error"
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    # The critic caught the semantic drop the structural checks could not.
    assert result.escalated


def test_verifier_allows_accept_when_it_confirms_both_sides(conflicted_repo, verifier_critic_enabled):
    """Flag on + critic confirms both sides preserved → accepted (rebase
    completes), proving the critic does not over-reject clean merges."""
    repo = conflicted_repo["repo"]
    client = SequenceClient([
        _make_resolved_payload("    return 'hi' + 'howdy'"),  # real merge of both
        json.dumps({"preserves_current": True, "preserves_replayed": True,
                    "reason": "both preserved", "confidence": 0.9}),
    ])
    cfg = _verifier_config(repo)
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert not result.escalated, result.reason
    assert "<<<<<<<" not in (repo / "app.py").read_text()


class CapturingSequenceClient:
    """Like SequenceClient but records the prompt of each complete() call.

    Used to assert the critic's verdict is seeded into the repair prompt on
    retry (the Step-2 feedback-seeding fix): without it, a critic-driven retry
    regenerated with no feedback and the model kept reproducing the same
    dropped-side merge.
    """

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.prompts: list[str] = []  # the user-message text of each call, in order

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        self.prompts.append(messages[-1]["content"])
        if not self.responses:
            raise RuntimeError("no more fake responses")
        return LLMResponse(text=self.responses.pop(0))


def test_verifier_seeds_verdict_into_repair_prompt_on_retry(distinct_additions_repo, verifier_critic_enabled):
    """A retry's repair prompt CONTAINS the validator feedback — so the model
    sees concrete evidence ("dropped a side's addition") instead of regenerating
    blind. This is what makes retries converge on a correct merge.

    Uses a DISTINCT-ADDITIONS conflict (each side adds a different flag) so a
    one-sided merge genuinely drops an addition and the deterministic validators
    flag it, driving a retry. (A value-resolution conflict would accept the
    one-sided merge, so it can't exercise the retry-seeding path.)

    Sequence: (1) one-sided resolution (drops replayed) → validators flag it →
    (2) the retry: model returns the correct merge. We assert the retry's prompt
    carried the validator feedback, and the run converged on the correct merge."""
    repo = distinct_additions_repo["repo"]
    client = CapturingSequenceClient([
        _make_resolved_payload(distinct_additions_repo["current_only"]),  # drops replayed
        _make_resolved_payload(distinct_additions_repo["correct_merged"]),  # correct merge on retry
    ])
    cfg = _verifier_config(repo)
    cfg.validation.verifier_severity = "warning"
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert not result.escalated, result.reason
    text = (repo / "app.py").read_text()
    assert "<<<<<<<" not in text
    assert "cache_on" in text and "metrics_on" in text  # both sides preserved
    # The retry (2nd complete() call) must carry seeded validator feedback.
    assert len(client.prompts) >= 2, client.prompts
    retry_prompt = client.prompts[1]
    assert "drop" in retry_prompt.lower() or "both_sides" in retry_prompt.lower() or "verifier" in retry_prompt.lower(), (
        "validator feedback not seeded into the repair prompt: " + retry_prompt[:300]
    )


def test_critic_retry_names_specific_dropped_unit():
    """The critic-driven retry feedback names the SPECIFIC entity the resolution
    dropped (quantitative per-side preservation), not just 'you dropped a side'.
    A conflict where the replayed side ADDS a function and the candidate drops
    it: the seeded failure must carry "reintroduce: function '<name>'" so the
    model has an exact target. This converges faster than a vague verdict."""
    from capybase.orchestrator import _dropped_units_for, _critic_failure
    from capybase.conflict_model import (
        CandidateResolution, ConflictSide, ConflictUnit, VerificationWarning,
    )
    base = "def main():\n    return 1\n"
    replayed = "def main():\n    return 1\n\ndef helper():\n    return 2\n"
    unit = ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=base),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text="", marker_span=(1, 5),
    )
    # Candidate resolves to base → drops the helper() the replayed side added.
    cand = CandidateResolution(
        candidate_id="c", unit_id="u", model_name="m", prompt_version="v",
        resolved_text=base,
    )
    dropped = _dropped_units_for(unit, cand)
    assert ("function", "helper") in dropped, dropped
    warning = VerificationWarning(
        validator="verifier_model",
        message="verifier: resolution may drop replayed side intent",
        detail={"reason": "dropped helper"},
    )
    failure = _critic_failure(warning, dropped)
    # The rendered failure names the specific unit to reintroduce.
    assert "reintroduce: function 'helper'" in failure.message, failure.message
    assert failure.detail["dropped_units"] == [("function", "helper")]
    # _render_failure surfaces it in the prompt the model sees on retry.
    from capybase.resolution_engine import _render_failure
    rendered = _render_failure(failure)
    assert "helper" in rendered and "reintroduce" in rendered, rendered


def test_dropped_units_no_false_positive_when_entity_present():
    """An entity the resolution preserves (even renamed) is NOT reported dropped
    — a rename is a legitimate merge, not a drop. Only genuinely-absent entities
    surface (matched by name)."""
    from capybase.orchestrator import _dropped_units_for
    from capybase.conflict_model import (
        CandidateResolution, ConflictSide, ConflictUnit,
    )
    base = "def main():\n    return 1\n"
    replayed = "def main():\n    return 1\n\ndef helper():\n    return 2\n"
    unit = ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=base),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text="", marker_span=(1, 5),
    )
    # Candidate keeps helper (under its original name) → nothing dropped.
    cand = CandidateResolution(
        candidate_id="c", unit_id="u", model_name="m", prompt_version="v",
        resolved_text="def main():\n    return 1\n\ndef helper():\n    return 2\n",
    )
    assert _dropped_units_for(unit, cand) == []


def test_soft_warning_failures_lifts_actionable_warnings():
    """A warning-driven retry must carry the validator's SPECIFIC finding into the
    prompt — not regenerate from scratch with zero feedback. The old retry seed
    lifted only hard_failures + the critic warning, so actionable soft warnings
    (intent_coverage, unattributed_code, ...) produced feedback-free
    regenerations. ``_soft_warning_failures`` lifts them into failure shape so
    ``_render_failure`` surfaces their structured detail (dropped names, ratios)
    and ``propose`` selects the targeted repair path."""
    from capybase.orchestrator import _soft_warning_failures
    from capybase.conflict_model import VerificationResult, VerificationWarning
    validation = VerificationResult(
        candidate_id="c", unit_id="u", passed=False,
        hard_failures=[],
        warnings=[
            VerificationWarning(
                validator="intent_coverage",
                message="replayed side coverage 0.00 below floor 0.50",
                detail={"dropped_names": ["helper"], "ratio": 0.0},
            ),
            VerificationWarning(
                validator="unattributed_code",
                message="resolved introduces unit in neither side",
                detail={"names": ["mystery_fn"]},
            ),
        ],
    )
    lifted = _soft_warning_failures(validation)
    validators = {f.validator for f in lifted}
    assert validators == {"intent_coverage", "unattributed_code"}
    # The structured detail (dropped names) is preserved → reaches the prompt.
    ic = next(f for f in lifted if f.validator == "intent_coverage")
    assert ic.detail == {"dropped_names": ["helper"], "ratio": 0.0}
    assert ic.severity == "warning"  # distinguishable from a real hard failure
    # _render_failure surfaces the dropped name so the model gets a concrete target.
    from capybase.resolution_engine import _render_failure
    assert "helper" in _render_failure(ic)


def test_soft_warning_failures_excludes_critic_and_unrelated_warnings():
    """``verifier_model*`` warnings are handled by ``_critic_failure`` against the
    separate critic budget — they must NOT also be lifted here (double-counting).
    Unrelated soft warnings (no retry semantics) are likewise excluded."""
    from capybase.orchestrator import _soft_warning_failures
    from capybase.conflict_model import VerificationResult, VerificationWarning
    validation = VerificationResult(
        candidate_id="c", unit_id="u", passed=False, hard_failures=[],
        warnings=[
            VerificationWarning(validator="verifier_model", message="critic flag"),
            VerificationWarning(
                validator="verifier_model_conflict", message="jury flag"),
            VerificationWarning(validator="something_unrelated", message="noise"),
        ],
    )
    assert _soft_warning_failures(validation) == []


def test_warning_driven_retry_uses_repair_prompt_with_feedback():
    """End-to-end: when the only signal driving a retry is an actionable soft
    warning (no hard failures, no critic), the retry must (a) reuse the previous
    candidate and (b) feed the validator's specific finding back via the repair
    prompt — not regenerate from scratch. This pins the Phase 1 fix: a non-empty
    ``failures`` list selects ``build_repair_prompt`` over ``build_resolve_prompt``.
    """
    from capybase.adapters.llm_openai import LLMResponse
    from capybase.conflict_model import (
        CandidateResolution, ConflictSide, ConflictUnit, VerificationResult,
        VerificationWarning,
    )
    from capybase.config import ModelConfig
    from capybase.context_builder import ContextBuilder
    from capybase.resolution_engine import ResolutionEngine, PROMPT_REPAIR

    class _CapturingClient:
        def __init__(self, text):
            self._text = text
            self.calls = []

        def complete(self, messages, **kw):
            self.calls.append({"messages": messages, **kw})
            return LLMResponse(
                text=self._text,
                raw={"_accumulated": {"finish_reason": "stop"}},
            )

    base = "def main():\n    return 1\n"
    replayed = "def main():\n    return 1\n\ndef helper():\n    return 2\n"
    unit = ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=base),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text="", marker_span=(1, 5),
    )
    prev = CandidateResolution(
        candidate_id="c1", unit_id="u", model_name="m", prompt_version="v",
        resolved_text=base,  # dropped the helper the replayed side added
    )
    # The intent_coverage warning is the ONLY signal — no hard failures, no critic.
    soft_warning = VerificationWarning(
        validator="intent_coverage",
        message="replayed side coverage 0.00 below floor 0.50",
        detail={"dropped_names": ["helper"], "ratio": 0.0},
    )
    validation = VerificationResult(
        candidate_id="c1", unit_id="u", passed=False, hard_failures=[],
        warnings=[soft_warning],
    )
    # Simulate what the orchestrator's retry seed now produces (Phase 1 fix).
    from capybase.orchestrator import _soft_warning_failures
    failures = _soft_warning_failures(validation)
    assert failures, "expected the soft warning to lift to a failure"

    client = _CapturingClient('{"resolved_text": "def main():\\n    return 1\\n\\ndef helper():\\n    return 2\\n"}')
    engine = ResolutionEngine(ModelConfig(samples=1), client=client)
    cands = engine.propose(unit, ContextBuilder().build(unit), failures=failures, prev_candidate=prev)
    # (a) The targeted repair path was chosen (not a fresh resolve).
    assert cands[0].prompt_version == PROMPT_REPAIR, cands[0].prompt_version
    # (b) The validator's specific finding reached the prompt.
    sent = client.calls[0]["messages"][1]["content"]
    assert "helper" in sent, "dropped entity name must reach the model on retry"


def test_wall_time_budget_escalates_non_converging_unit(distinct_additions_repo, verifier_critic_enabled):
    """A unit that can't converge within its wall-clock budget escalates instead
    of looping indefinitely. The critic keeps flagging a dropped-side merge
    (CyclingClient returns the same one-sided resolution + verdict forever), so
    retries would normally pile up; the wall-time deadline bounds total latency
    by escalating once it's exceeded.

    Uses a DISTINCT-ADDITIONS conflict so the one-sided merge genuinely drops an
    addition and the critic keeps flagging it (a value-resolution conflict would
    accept the one-sided merge, so it wouldn't loop)."""
    repo = distinct_additions_repo["repo"]
    # Always returns a one-sided resolution (drops the replayed addition) so the
    # deterministic both-sides-represented + preservation-heuristic validators keep
    # flagging it. CyclingClient repeats forever, so the loop never converges and
    # the wall-time deadline must bound it.
    client = CyclingClient([
        _make_resolved_payload(distinct_additions_repo["current_only"]),  # drops replayed
        json.dumps({"preserves_current": True, "preserves_replayed": False,
                    "reason": "dropped metrics_on", "confidence": 0.5}),
    ])
    cfg = _verifier_config(repo)
    cfg.validation.verifier_severity = "warning"  # soft → would retry forever
    # Disable the pre-LLM resolvers: distinct additions would otherwise be
    # unioned deterministically (resolving without the LLM), so the loop the
    # wall-time deadline must bound would never start.
    cfg.future.enable_structural_resolver = False
    cfg.future.enable_combination_search = False
    # Tiny wall budget so the loop escalates quickly (well under a second). The
    # retry-count budgets are large enough that they wouldn't trigger first.
    cfg.policy.max_wall_time_per_unit_seconds = 0.2
    cfg.policy.max_retries_per_unit = 50
    cfg.policy.max_critic_retries_per_unit = 50
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert result.escalated
    assert "wall-time" in (result.reason or ""), result.reason


def test_wall_time_disabled_does_not_escalate(conflicted_repo, verifier_critic_enabled):
    """wall budget = 0 (disabled, the default) → the loop is governed only by the
    retry-count budgets, never by a wall-clock check."""
    repo = conflicted_repo["repo"]
    client = SequenceClient([
        _make_resolved_payload("    return 'hi' + 'howdy'"),  # correct merge
        json.dumps({"preserves_current": True, "preserves_replayed": True,
                    "reason": "both preserved", "confidence": 0.9}),
    ])
    cfg = _verifier_config(repo)
    cfg.policy.max_wall_time_per_unit_seconds = 0.0  # disabled
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    # Converges on the first attempt — no escalation, no wall-time trigger.
    assert not result.escalated, result.reason


def test_verifier_not_registered_when_flag_off(conflicted_repo):
    """Flag off → the verifier validator is not in the engine's chain at all,
    so no critic call is ever made (zero-cost default)."""
    repo = conflicted_repo["repo"]
    cfg = _config(repo)  # enable_verifier_model defaults False
    cfg.validation.enable_verifier_model = False
    orch = Orchestrator(cfg, repo=str(repo))
    names = [type(v).__name__ for v in orch.verification.validators]
    assert "VerifierModelValidator" not in names


def test_verifier_registered_when_flag_on(conflicted_repo):
    """Flag on → the verifier validator is registered in the chain."""
    repo = conflicted_repo["repo"]
    orch = Orchestrator(_verifier_config(repo), repo=str(repo))
    names = [type(v).__name__ for v in orch.verification.validators]
    assert "VerifierModelValidator" in names


# ---------------------------------------------------------------------------
# VeriGuard policy gate integration (survey §4): the deterministic safety gate
# blocks an unsafe patch end-to-end when enable_policy_gate + a rule are set.
# ---------------------------------------------------------------------------


def _policy_config(repo):
    from capybase.config import PolicyRule

    cfg = _config(repo)
    cfg.validation.enable_policy_gate = True
    cfg.validation.policy_rules = [
        PolicyRule(name="no_eval", kind="forbid_call", pattern="eval",
                   severity="error", reason="eval is forbidden"),
    ]
    return cfg


def test_policy_gate_registered_when_enabled(conflicted_repo):
    """enable_policy_gate on + a rule → the gate is auto-registered by the
    engine factory (no orchestrator register() call needed)."""
    repo = conflicted_repo["repo"]
    orch = Orchestrator(_policy_config(repo), repo=str(repo))
    names = [type(v).__name__ for v in orch.verification.validators]
    assert "PolicyGateValidator" in names


def test_policy_gate_not_registered_when_disabled(conflicted_repo):
    """Flag off → the gate is absent from the chain (zero-cost default)."""
    repo = conflicted_repo["repo"]
    cfg = _config(repo)  # enable_policy_gate defaults False
    orch = Orchestrator(cfg, repo=str(repo))
    names = [type(v).__name__ for v in orch.verification.validators]
    assert "PolicyGateValidator" not in names


def test_policy_gate_not_registered_when_no_rules(conflicted_repo):
    """Flag on but no rules → still absent (the gate ships no built-in rules)."""
    repo = conflicted_repo["repo"]
    cfg = _config(repo)
    cfg.validation.enable_policy_gate = True
    cfg.validation.policy_rules = []  # no rules → no-op
    orch = Orchestrator(cfg, repo=str(repo))
    names = [type(v).__name__ for v in orch.verification.validators]
    assert "PolicyGateValidator" not in names


def test_policy_gate_blocks_unsafe_patch(conflicted_repo):
    """Gate on + a forbid_call eval rule → a patch that uses eval is blocked
    from auto-apply (escalated). The patch is structurally a valid merge, so
    only the policy gate catches the unsafe call."""
    repo = conflicted_repo["repo"]
    # A candidate that resolves the merge but smuggles in an eval() call.
    client = SequenceClient([
        _make_resolved_payload("    return eval('1') + 'howdy'"),
    ])
    cfg = _policy_config(repo)
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert result.escalated


def test_policy_gate_allows_safe_patch(conflicted_repo):
    """Gate on + a forbid_call eval rule → a patch without eval is accepted
    (rebase completes). Proves the gate doesn't over-reject clean merges."""
    repo = conflicted_repo["repo"]
    client = SequenceClient([
        _make_resolved_payload("    return 'hi' + 'howdy'"),  # no forbidden call
    ])
    cfg = _policy_config(repo)
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert not result.escalated, result.reason


# ---------------------------------------------------------------------------
# LLM code-smell detection integration (survey §7): the ast-based checker is
# auto-registered when enabled and flags smelly patches through the accept path.
# ---------------------------------------------------------------------------


def _smell_config(repo, severity="warning"):
    cfg = _config(repo)
    cfg.validation.enable_code_smell_checks = True
    cfg.validation.code_smell_severity = severity
    return cfg


def test_code_smell_registered_when_enabled(conflicted_repo):
    """enable_code_smell_checks on → the checker is auto-registered."""
    repo = conflicted_repo["repo"]
    orch = Orchestrator(_smell_config(repo), repo=str(repo))
    names = [type(v).__name__ for v in orch.verification.validators]
    assert "CodeSmellValidator" in names


def test_code_smell_not_registered_when_disabled(conflicted_repo):
    """Flag off (default) → checker absent from the chain."""
    repo = conflicted_repo["repo"]
    cfg = _config(repo)  # enable_code_smell_checks defaults False
    orch = Orchestrator(cfg, repo=str(repo))
    names = [type(v).__name__ for v in orch.verification.validators]
    assert "CodeSmellValidator" not in names


def test_code_smell_error_severity_blocks_smelly_patch(conflicted_repo):
    """Gate on + error severity + a patch with a NaN comparison → blocked from
    auto-apply (escalated). The patch is structurally a valid merge, so only
    the smell checker catches it."""
    repo = conflicted_repo["repo"]
    client = SequenceClient([
        _make_resolved_payload("    return a == np.nan"),  # NaN smell
    ])
    cfg = _smell_config(repo, severity="error")
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert result.escalated


def test_code_smell_warning_does_not_block_clean_merge(conflicted_repo):
    """Gate on + warning severity + a clean patch → accepted (rebase completes).
    The checker doesn't over-reject clean merges."""
    repo = conflicted_repo["repo"]
    client = SequenceClient([
        _make_resolved_payload("    return 'hi' + 'howdy'"),  # no smell
    ])
    cfg = _smell_config(repo, severity="warning")
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert not result.escalated, result.reason


# ---------------------------------------------------------------------------
# F4: retrieval scores journaled into context_built (end-to-end RAG)
# ---------------------------------------------------------------------------


def test_context_built_event_carries_retrieval_scores(conflicted_repo):
    """When RAG retrieves few-shot examples, the ``context_built`` journal event
    records the per-example retrieval scores — the diagnostic data for validating
    the calibrated min_similarity floor in production."""
    from capybase.conflict_model import HistoricalExample
    from capybase.memory.store import Experience, ExperienceStore

    repo = conflicted_repo["repo"]
    # Seed the experience store at the path the orchestrator will read.
    store = ExperienceStore.for_repo(str(repo), ".rebase-agent/memory/experiences.jsonl")
    store.append(
        Experience(
            example=HistoricalExample(
                summary="greet", base="def greet(): return hi",
                current="return hi", replayed="return howdy",
                resolved="return ('hi','howdy')",
            ),
            outcome="accepted", language="python", path="app.py",
        )
    )

    cfg = _config(repo)
    cfg.memory.enabled = True
    cfg.future.enable_rag = True
    cfg.memory.retriever = "lexical"  # dependency-free; no network needed
    cfg.memory.min_examples_for_retrieval = 1
    payload = _make_resolved_payload("    return 'hi' + 'howdy'")
    engine = ResolutionEngine(cfg.model, client=CyclingClient([payload]))
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert not result.escalated, result.reason

    events = orch.journal.read_events()
    built = [e for e in events if e.event_type == "context_built"]
    assert built, "expected a context_built event"
    payload_evt = built[0].payload
    assert "retrieval_scores" in payload_evt
    # The seeded 'greet' example overlaps the conflict's tokens → at least one
    # score is journaled, and they parallel the retrieved examples.
    assert isinstance(payload_evt["retrieval_scores"], list)
    assert len(payload_evt["retrieval_scores"]) >= 1
    assert all(isinstance(s, (int, float)) for s in payload_evt["retrieval_scores"])


def test_context_built_event_has_empty_scores_when_rag_disabled(conflicted_repo):
    """Without RAG, ``context_built`` still carries the key but it's empty —
    the schema is stable whether or not retrieval ran."""
    repo = conflicted_repo["repo"]
    cfg = _config(repo)
    payload = _make_resolved_payload("    return 'hi' + 'howdy'")
    engine = ResolutionEngine(cfg.model, client=CyclingClient([payload]))
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.run()
    assert not result.escalated, result.reason

    events = orch.journal.read_events()
    built = [e for e in events if e.event_type == "context_built"]
    assert built
    assert built[0].payload["retrieval_scores"] == []



# ---------------------------------------------------------------------------
# Difficulty-aware routing: the "simple" fast path must use exactly ONE sample
# even when config.model.samples > 1 (a calibrated profile must not leak into
# the cheap path). Regression: the simple branch called propose() with no
# n_samples, falling back to config.samples (3 if calibrated).
# ---------------------------------------------------------------------------


class CountingClient:
    """FakeClient that counts complete() calls and returns one fixed payload."""

    def __init__(self, payload: str):
        self.payload = payload
        self.calls = 0

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        self.calls += 1
        return LLMResponse(text=self.payload)


def test_simple_routing_uses_one_sample_even_when_samples_is_three(conflicted_repo):
    """The simple fast path must force n_samples=1 even when
    config.model.samples > 1 (a calibrated profile must not leak into the cheap
    path). Regression: the simple branch called propose() with no n_samples,
    falling back to config.samples (3 if calibrated).

    Verified by spying on the n_samples argument the engine receives, not by
    counting complete() calls (those conflate with retry behavior). The pre-LLM
    layers are disabled so the conflict reaches the LLM simple path directly."""
    repo = conflicted_repo["repo"]
    cfg = _config(repo)
    cfg.routing.enabled = True  # classify difficulty
    cfg.future.enable_structural_resolver = False  # reach the LLM path
    cfg.future.enable_combination_search = False  # isolate the simple LLM path
    cfg.future.enable_block_capture = False
    cfg.model.samples = 3  # the value that must NOT leak into the simple path
    payload = _make_resolved_payload("a = 1\nx = 9\nb = 2\nc = 3")
    client = CountingClient(payload)
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    from capybase.conflict_model import ConflictSide, ConflictUnit
    # Disjoint insertion: trivial band (deterministically mergeable) → simple.
    base = "a = 1\nb = 2\nc = 3\n"
    worktree = "a = 1\n<<<<<<<\nb = 2\n=======\nx = 9\nb = 2\n>>>>>>>\nc = 3\n"
    unit = ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE",
                             text="a = 1\nb = 2\nc = 3\n"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE",
                              text="a = 1\nx = 9\nb = 2\nc = 3\n"),
        original_worktree_text=worktree,
        marker_span=(1, 5),
        structural_metadata={"sibling_count": 0},
    )
    from capybase.classifier import classify
    assert classify(unit).difficulty == "simple"

    # Spy on propose's n_samples argument.
    seen_n_samples: list = []
    real_propose = engine.propose

    def spying_propose(*args, **kwargs):
        seen_n_samples.append(kwargs.get("n_samples"))
        return real_propose(*args, **kwargs)

    engine.propose = spying_propose  # type: ignore[method-assign]
    orch.step = 1
    orch._resolve_unit(unit)
    # Every propose() call from the simple path carried n_samples=1, NEVER 3 (or
    # None, which would fall back to config.samples=3).
    assert seen_n_samples, "the simple path never called propose()"
    assert all(n == 1 for n in seen_n_samples), (
        f"simple path proposed with n_samples={seen_n_samples}, expected all 1 "
        f"(a calibrated samples>1 leaked into the cheap path)"
    )


# ---------------------------------------------------------------------------
# Snapshot correctness: the ".before" snapshot must capture the PRE-WRITE
# worktree content (what's on disk before the resolution overwrites it), not
# the resolved buffer being written. Regression: it snapshotted `buffer`, making
# the ".before" name misleading and the audit trail useless.
# ---------------------------------------------------------------------------


def test_before_snapshot_captures_pre_write_worktree_content(repo):
    """The .before snapshot is the on-disk file BEFORE mutation, not the buffer."""
    cfg = _config(repo)
    cfg.journal.enabled = True
    cfg.journal.store_snapshots = True
    engine = ResolutionEngine(cfg.model, client=CyclingClient(["{}"]))
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    # Put a known PRE-EXISTING file on disk, then write a DIFFERENT buffer.
    (repo / "existing.py").write_text("# OLD CONTENT ON DISK\nold = 1\n")
    new_buffer = "# NEW RESOLVED BUFFER\nnew = 2\n"
    orch._write_and_stage("existing.py", new_buffer, StepResult(step_index=1))
    snap = orch.paths.snapshots / "existing.py.before"
    assert snap.exists(), "no .before snapshot was written"
    snap_text = snap.read_text()
    # The snapshot is the PRE-WRITE worktree content, not the resolved buffer.
    assert "OLD CONTENT ON DISK" in snap_text
    assert "new = 2" not in snap_text  # the buffer must NOT have been snapshotted


def test_before_snapshot_absent_for_new_file(repo):
    """A brand-new file (nothing pre-existing on disk) has no .before snapshot."""
    cfg = _config(repo)
    cfg.journal.enabled = True
    cfg.journal.store_snapshots = True
    engine = ResolutionEngine(cfg.model, client=CyclingClient(["{}"]))
    orch = Orchestrator(
        cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    orch._write_and_stage(
        "brand_new.py", "# fresh file\n", StepResult(step_index=1)
    )
    # No prior content existed → no .before snapshot (no crash, no empty file).
    assert not (orch.paths.snapshots / "brand_new.py.before").exists()

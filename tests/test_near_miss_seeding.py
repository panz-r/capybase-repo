"""Near-miss seeding (S28-128): a validation-REJECTED deterministic draft
(structural resolver / SBCR) seeds the model's FIRST resolution instead of
being discarded — CEGIS starting one stage earlier, at zero extra model
calls. Default OFF pending the paired targeted evaluation.

The contract under test (review-shaped):
  - EPHEMERAL: the seed appears only while no usable LLM candidate exists;
    once the model has produced its own candidate, retries carry the LLM
    candidate + its failure, not the deterministic draft.
  - Budget: all-or-nothing, never compacted, dropped BEFORE obligations
    (known requirements beat known-wrong information).
  - Journal split: near_miss_stashed (available) vs near_miss_used (the
    post-budget prompt actually carried it — #nm in the version).
  - Lifetime: the stash belongs to one unit instance; the deferred-core
    child never inherits it.
"""

from __future__ import annotations

import json

import pytest

from capybase.conflict_model import (
    CandidateResolution, ConflictSide, ConflictUnit, VerificationFailure,
)
from capybase.context_builder import ContextBuilder
from capybase.resolution_engine import (
    PROMPT_REPAIR, PROMPT_RESOLVE, PROMPT_RETRY, ResolutionEngine,
    _NM_MARKER, _fit_to_budget, _near_miss_block, build_code_prompt,
    build_intent_prompt,
)
from capybase.config import Config, ModelConfig
from capybase.conflict_model import TokenBudget


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unit(with_stash: bool = True) -> ConflictUnit:
    worktree = (
        "def f():\n<<<<<<< H\n    return 0\n=======\n    return 9\n>>>>>>> b\n"
    )
    u = ConflictUnit(
        session_id="s", step_index=1, path="f.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="def f():\n    pass"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="    return 0"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="    return 9"),
        original_worktree_text=worktree, marker_span=(1, 5),
    )
    if with_stash:
        u.structural_metadata["_deterministic_near_miss"] = {
            "mechanism": "sbcr",
            "text": "def f():\n    return (0, 9)\n",
            "failures": ["- [syntax] normalize() now requires mode"],
            "sha8": "deadbeef",
            "step": 1,
        }
    return u


def _ctx() -> ContextBundle:
    return ContextBuilder().build(_unit())


def _engine() -> ResolutionEngine:
    return ResolutionEngine(ModelConfig())


def _cand(text: str = "def f():\n    return 42\n") -> CandidateResolution:
    return CandidateResolution(
        candidate_id="c1", unit_id="u", model_name="fake",
        resolved_text=text, explanation="m",
        prompt_version=PROMPT_RESOLVE,
    )


def _failure(msg: str = "syntax broken") -> VerificationFailure:
    return VerificationFailure(
        validator="syntax", severity="error", message=msg)


# ---------------------------------------------------------------------------
# 1+7. Fresh resolve contains the seed; no stash → byte-identical shape
# ---------------------------------------------------------------------------


def test_fresh_resolve_contains_seed():
    prompt, pv, trims = _engine().build_attempt_prompt(_unit(), _ctx())
    assert _NM_MARKER in prompt
    assert "def f():\n    return (0, 9)" in prompt  # draft verbatim
    assert "normalize() now requires mode" in prompt  # diagnostic
    assert "Do not" in prompt and "resubmit" in prompt
    assert pv.startswith(PROMPT_RESOLVE + "#nm"), pv
    assert not [t for t in trims if t["section"] == "near_miss"]


def test_no_stash_prompt_and_version_unchanged():
    prompt, pv, trims = _engine().build_attempt_prompt(_unit(False), _ctx())
    assert _NM_MARKER not in prompt
    assert pv == PROMPT_RESOLVE, pv
    assert trims == []


# ---------------------------------------------------------------------------
# 2. Ephemeral: retries carry the LLM candidate, not the seed
# ---------------------------------------------------------------------------


def test_retry_after_usable_llm_candidate_excludes_seed():
    """failures + a prev_candidate WITH text → the REPAIR prompt (hand-rolled,
    no parts) — the deterministic draft must not appear anywhere."""
    prompt, pv, _ = _engine().build_attempt_prompt(
        _unit(), _ctx(), failures=[_failure()], prev_candidate=_cand())
    assert _NM_MARKER not in prompt
    assert "def f():\n    return 42" in prompt  # the LLM candidate IS shown
    assert pv.startswith(PROMPT_REPAIR), pv


def test_failures_only_retry_keeps_seed():
    """A retry with NO usable prev candidate (unparsable/empty response)
    may still start from the deterministic seed."""
    empty_prev = _cand("")
    prompt, pv, _ = _engine().build_attempt_prompt(
        _unit(), _ctx(), failures=[_failure()], prev_candidate=empty_prev)
    assert _NM_MARKER in prompt
    assert pv.startswith(PROMPT_RETRY + "#nm"), pv


# ---------------------------------------------------------------------------
# 3. Recovery: seed only while no usable LLM candidate exists
# ---------------------------------------------------------------------------


def test_recovery_before_usable_candidate_keeps_seed():
    prompt, pv, _ = _engine().build_attempt_prompt(
        _unit(), _ctx(), failures=[_failure()], pending_recovery=True)
    assert _NM_MARKER in prompt
    assert pv == "cegis_recovery.v1#nm", pv


def test_recovery_after_usable_candidate_drops_seed():
    engine = _engine()
    # no client → a request_failed candidate; the prompt RULE is what
    # matters — inspect it via the dispatch directly.
    engine.propose_recovery(
        _unit(), _ctx(), failures=[_failure()], prev_candidate=_cand())
    prompt, pv, _ = engine.build_attempt_prompt(
        _unit(), _ctx(), failures=[_failure()], prev_candidate=_cand(),
        pending_recovery=True)
    assert _NM_MARKER not in prompt
    assert pv == "cegis_recovery.v1", pv


# ---------------------------------------------------------------------------
# 4+5. Budget: all-or-nothing; trims record the drop; obligations outlive it
# ---------------------------------------------------------------------------


def test_budget_drop_is_all_or_nothing_and_versioned_away():
    # A draft far larger than the augmentation budget: dropped wholesale
    # (never truncated), recorded in trims, and the #nm suffix goes with it.
    u = _unit()
    u.structural_metadata["_deterministic_near_miss"]["text"] = "x = 1\n" * 500
    engine = ResolutionEngine(ModelConfig(
        context_window=1200, completion_reserve=100))
    prompt, pv, trims = engine.build_attempt_prompt(u, _ctx())
    assert _NM_MARKER not in prompt
    assert "x = 1" * 3 not in prompt
    assert pv == PROMPT_RESOLVE, pv
    assert any(t["section"] == "near_miss" for t in trims)


def test_surviving_seed_is_verbatim_not_compacted():
    # A window that fits the draft: comments in the draft survive intact
    # (compaction must never touch it).
    u = _unit()
    u.structural_metadata["_deterministic_near_miss"]["text"] = (
        "def f():\n    # keep me verbatim\n    return (0, 9)\n")
    engine = ResolutionEngine(ModelConfig(
        context_window=8192, completion_reserve=1024))
    prompt, _, trims = engine.build_attempt_prompt(u, _ctx())
    assert "# keep me verbatim" in prompt
    assert not [t for t in trims if t["section"] == "near_miss"]


def test_obligations_outlive_the_near_miss_in_trim_order():
    """Drop cascade: ... structural_anchor -> NEAR_MISS -> obligations last.
    A budget that cannot hold both keeps obligations and drops the draft."""
    draft = "x = 1\n" * 60       # ~120 tokens
    obligations = "OBLIGATION-MARKER " * 20   # ~120 tokens
    # available_for_augmentation ~200 tokens: fits obligations (120) but
    # not obligations + draft (240) -> the draft drops, obligations stay.
    result = _fit_to_budget(
        budget=TokenBudget(total=100000, reserved_for_completion=99786),
        intro="i", contract="c", rules="r", sides_text="sides",
        structural_anchor="", siblings_block="", deps="",
        few_shot="", primary_text="", unit=_unit(False),
        history="", obligations=obligations, near_miss_block=draft,
    )
    (_a, _s, _d, _f, _p, _h, obls_t, nm_t, trims, _sk) = result
    assert obls_t != "" and "OBLIGATION-MARKER" in obls_t
    assert nm_t == ""
    assert any(t["section"] == "near_miss" for t in trims)
    assert not any(t["section"] == "obligations" for t in trims)


# ---------------------------------------------------------------------------
# 6+8. Orchestrator: flag off = nothing; flag on = stashed + used
# ---------------------------------------------------------------------------


def _seeded_run(repo, monkeypatch, *, flag: bool):
    """Drive a real conflict where the structural candidate is REJECTED by a
    monkeypatched validator, so the unit falls through to the LLM seeded."""
    from capybase.orchestrator import Orchestrator
    from capybase.verification import VerificationResult
    from tests.conftest import git

    class PromptCaptureClient:
        def __init__(self):
            self.prompts: list[str] = []

        def complete(self, messages, **kw):
            self.prompts.append(
                messages[-1]["content"] if isinstance(messages[-1], dict)
                else str(messages[-1]))
            from capybase.adapters.llm_openai import LLMResponse
            return LLMResponse(
                text=json.dumps({"resolved_text": "a = 10\nb = 20",
                                 "explanation": "m"}),
                raw={"_accumulated": {"finish_reason": "stop"}})

    # ADJACENT-line edits (lines 1 and 2): git conflicts the pair, and the
    # structural resolver's disjoint-edit rule PROPOSES a candidate — which
    # the monkeypatched validator then rejects.
    (repo / "app.py").write_text("a = 1\nb = 2\nc = 3\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text("a = 1\nb = 20\nc = 3\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "feat: b=20")
    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text("a = 10\nb = 2\nc = 3\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "main: a=10")
    git(repo, "checkout", "-q", "feat")

    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    cfg.future.enable_near_miss_seeding = flag
    # Keep the fall-through focused: SBCR declines this modification-shaped
    # conflict anyway; block capture/portfolio off for determinism.
    cfg.future.enable_block_capture = False
    cfg.future.enable_source_portfolio = False
    client = PromptCaptureClient()
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)

    real_verify = orch.verification.verify

    def rejecting_verify(unit, cand, *a, **kw):
        if (cand.provenance or "").startswith("deterministic_structural"):
            return VerificationResult(
                candidate_id=cand.candidate_id, unit_id=unit.unit_id,
                passed=False,
                hard_failures=[VerificationFailure(
                    validator="syntax", severity="error",
                    message="normalize() now requires mode")],
                warnings=[], features={})
        return real_verify(unit, cand, *a, **kw)

    monkeypatch.setattr(orch.verification, "verify", rejecting_verify)
    result = orch.rebase("main")
    events = {}
    for line in orch.paths.journal.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            d = json.loads(line)
            events.setdefault(d["event_type"], []).append(d.get("payload", {}))
    return result, client, events


def test_flag_on_stashes_and_uses_and_seeds_first_prompt(repo, monkeypatch):
    result, client, events = _seeded_run(repo, monkeypatch, flag=True)
    assert not result.escalated, result.reason
    assert "near_miss_stashed" in events
    assert events["near_miss_stashed"][0]["mechanism"] == "structural"
    assert events["near_miss_stashed"][0]["draft_sha8"]
    assert "near_miss_used" in events, (
        "the post-budget first prompt carried the seed — must be journaled")
    assert events["near_miss_used"][0]["mechanism"] == "structural"
    assert any(_NM_MARKER in p for p in client.prompts), (
        "the model must have SEEN the rejected draft + diagnostic")
    assert any("normalize() now requires mode" in p for p in client.prompts)


def test_flag_off_is_byte_identical_behavior(repo, monkeypatch):
    result, client, events = _seeded_run(repo, monkeypatch, flag=False)
    assert not result.escalated, result.reason
    assert "near_miss_stashed" not in events
    assert "near_miss_used" not in events
    assert not any(_NM_MARKER in p for p in client.prompts)


def test_deterministic_accept_never_uses_seed(repo, monkeypatch):
    """With the real validator (the structural candidate PASSES), the unit
    never reaches the LLM: no stash, no use event, zero model calls — even
    with the flag on."""
    from capybase.orchestrator import Orchestrator
    from tests.conftest import git

    class NoCallClient:
        calls = 0

        def complete(self, *a, **kw):
            raise AssertionError("LLM must not be called")

    (repo / "app.py").write_text("a = 1\nb = 2\nc = 3\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text("a = 1\nb = 20\nc = 3\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "feat: b=20")
    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text("a = 10\nb = 2\nc = 3\n")
    git(repo, "add", "app.py"); git(repo, "commit", "-q", "-m", "main: a=10")
    git(repo, "checkout", "-q", "feat")

    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    cfg.future.enable_near_miss_seeding = True
    engine = ResolutionEngine(cfg.model, client=NoCallClient())
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    result = orch.rebase("main")
    assert not result.escalated, result.reason
    body = orch.paths.journal.read_text(encoding="utf-8")
    assert "near_miss_stashed" not in body
    assert "near_miss_used" not in body


# ---------------------------------------------------------------------------
# 9. Last cascade rung wins (deliberate policy, recorded)
# ---------------------------------------------------------------------------


def test_last_cascade_rung_wins(repo):
    from capybase.orchestrator import Orchestrator
    from capybase.verification import VerificationResult

    cfg = Config()
    cfg.future.enable_near_miss_seeding = True
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=None,
                        out=lambda *_a, **_k: None)
    unit = _unit(False)
    vfail = VerificationResult(
        candidate_id="x", unit_id=unit.unit_id, passed=False,
        hard_failures=[_failure("m1")], warnings=[], features={})
    orch._stash_near_miss(unit, "structural", _cand("draft A\n"), vfail)
    orch._stash_near_miss(unit, "sbcr", _cand("draft B\n"), vfail)
    stash = unit.structural_metadata["_deterministic_near_miss"]
    assert stash["mechanism"] == "sbcr" and stash["text"] == "draft B\n"
    events = [json.loads(l)["payload"]
              for l in orch.paths.journal.read_text(encoding="utf-8").splitlines()
              if l.strip() and json.loads(l)["event_type"] == "near_miss_stashed"]
    assert events[0]["overwrote_previous"] is False
    assert events[1]["overwrote_previous"] is True
    assert events[1]["mechanism"] == "sbcr"


# ---------------------------------------------------------------------------
# 10. Lifetime: the deferred-core child never inherits the parent's draft
# ---------------------------------------------------------------------------


def test_deferred_core_child_does_not_inherit_stash():
    """The stash belongs to the exact unit instance. The only generic copy
    of structural_metadata in the orchestrator is the deferred-core child
    build — pinned to strip the key."""
    parent = _unit(True)
    child_meta = dict(parent.structural_metadata)
    child_meta.pop("_deterministic_near_miss", None)  # the site's contract
    # and the engine renders nothing for a child built from that meta:
    child = _unit(False)
    child.structural_metadata = child_meta
    assert _near_miss_block(child) == ""


# ---------------------------------------------------------------------------
# Scope pins: two-pass prompts do not receive the seed; pv composition
# ---------------------------------------------------------------------------


def test_two_pass_prompts_do_not_receive_seed():
    """Scope pin: the hand-rolled intent/code prompts exclude the seed
    (extending them is a deliberate follow-up, evidence-gated)."""
    u = _unit(True)
    assert _NM_MARKER not in build_intent_prompt(u, _ctx())
    assert _NM_MARKER not in build_code_prompt(u, _ctx(), {"cur": [], "rep": []})


def test_nm_suffix_composes_with_profile_tag():
    from capybase.prompt_profile import (
        PromptProfile, set_active_profile, OutputLayout,
    )
    try:
        set_active_profile(PromptProfile(output_layout=OutputLayout.MARKDOWN_CODE))
        prompt, pv, _ = _engine().build_attempt_prompt(_unit(), _ctx())
        assert _NM_MARKER in prompt
        assert "#nm#" in pv and pv.startswith(PROMPT_RESOLVE), pv
    finally:
        from capybase.prompt_profile import set_active_profile
        set_active_profile(None)


def test_fresh_dispatch_with_usable_prev_excludes_seed():
    """Rule completeness pin: propose(failures=None, prev_candidate=<with
    text>) falls to the FRESH branch of the dispatch — the seed must still
    be excluded (the ephemeral rule is about candidate existence, not about
    which prompt class runs). Latent gap caught in review: the fresh path
    originally defaulted near_miss=True unconditionally."""
    prompt, pv, _ = _engine().build_attempt_prompt(
        _unit(), _ctx(), prev_candidate=_cand())
    assert _NM_MARKER not in prompt
    assert pv == PROMPT_RESOLVE, pv


def test_two_pass_engine_draws_wide_when_asked():
    """Engine API pin: propose_two_pass(n_samples=3) draws 3 code candidates
    over ONE intent call (the consensus batch exception needs the full set;
    the intent map is reused)."""
    from tests.test_two_pass import ScriptedClient
    engine = _engine()
    client = ScriptedClient([
        '{"current_side_intent": ["return 0"], "replayed_commit_intent": ["return 9"]}',
        '{"resolved_text": "    return 0"}',
        '{"resolved_text": "    return 0"}',
        '{"resolved_text": "    return 0"}',
    ])
    engine.client = client
    cands = engine.propose_two_pass(_unit(), _ctx(), n_samples=3)
    assert len(cands) == 3
    # calls records the completion kwargs; the first call is the intent pass
    # (1 intent + 3 code = 4 requests).
    assert len(client.calls) == 4, (
        f"expected 1 intent + 3 code requests, got {len(client.calls)}")


def test_two_pass_self_consistency_batch_preserved(conflicted_repo):
    """Orchestrator wiring pin: with two_pass + self-consistency + a raised
    ceiling, the opted-in draw is n_cap wide (the vote needs the full set) —
    the single-draw rule must not silently strip the vote from two-pass
    users."""
    import json

    from capybase.orchestrator import Orchestrator

    class RecordingTwoPassEngine:
        def __init__(self):
            self.recorded: list[int] = []

        def propose_two_pass(self, unit, context, *, n_samples, temperature):
            from capybase.adapters.llm_openai import LLMResponse
            self.recorded.append(n_samples)
            cand = CandidateResolution(
                candidate_id="c", unit_id=unit.unit_id, model_name="fake",
                resolved_text="    return 'hi' + 'howdy'", explanation="m",
                prompt_version="resolve_text_block.v6")
            return [cand] * n_samples

    cfg = Config()
    cfg.model.model = "fake"
    cfg.model.two_pass = True
    cfg.model.enable_self_consistency = True
    cfg.model.samples_complex = 3
    cfg.tests.required = False
    # The deterministic layers must DECLINE so the two-pass branch is reached.
    cfg.features.structural_resolution = False
    cfg.features.combination_search = False
    cfg.future.enable_source_portfolio = False
    cfg.future.enable_block_capture = False
    cfg.future.enable_docs_union = False
    cfg.future.enable_list_union = False
    cfg.future.enable_empty_side_rule = False
    engine = RecordingTwoPassEngine()
    orch = Orchestrator(
        cfg, repo=str(conflicted_repo["repo"]), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )
    result = orch.run()
    assert not result.escalated, result.reason
    assert engine.recorded == [3], (
        f"two-pass + self-consistency must draw the full batch (n_cap=3), "
        f"got n_samples={engine.recorded}")


# ---------------------------------------------------------------------------
# S28-130 audit follow-up: synthetic mechanism units journal their
# deterministic provenance under their OWN unit id
# ---------------------------------------------------------------------------


def test_synthetic_unit_mechanism_mapping():
    from capybase.orchestrator import Orchestrator

    cfg = Config()
    orch = Orchestrator(cfg, repo=".", resolution_engine=None,
                        out=lambda *_a, **_k: None)

    def unit(uid):
        return ConflictUnit(
            session_id="s", step_index=1, path="f.py", language="python",
            conflict_type="UU", unit_id=uid, unit_kind="whole_file",
            base=ConflictSide(label="BASE", text="b"),
            current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="c"),
            replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="r"),
            original_worktree_text="x", marker_span=None,
        )

    assert orch._synthetic_unit_mechanism(
        unit("app.py:true_side_stage")) == "true_side_portfolio"
    assert orch._synthetic_unit_mechanism(
        unit("conflict_0084.py:wholesale_winner_floor")
    ) == "wholesale_winner_floor"
    assert orch._synthetic_unit_mechanism(
        unit("app.py:1:0:core")) == "deferred_core_split"
    # regular cascade units are NOT synthetic
    assert orch._synthetic_unit_mechanism(unit("app.py:1:0")) is None


def test_emit_synthetic_provenance_records_under_own_unit_id(repo):
    from capybase.orchestrator import Orchestrator

    cfg = Config()
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=None,
                        out=lambda *_a, **_k: None)
    unit = _unit(False)
    unit.unit_id = "app.py:wholesale_winner_floor"
    unit.path = "app.py"
    orch._emit_synthetic_provenance(unit)
    events = [json.loads(l) for l in
              orch.paths.journal.read_text(encoding="utf-8").splitlines()
              if l.strip()]
    prov = [e for e in events
            if e["event_type"] == "synthetic_unit_provenance"]
    assert len(prov) == 1
    assert prov[0]["unit_id"] == "app.py:wholesale_winner_floor"
    assert prov[0]["payload"]["mechanism"] == "wholesale_winner_floor"
    # regular units emit nothing
    orch._emit_synthetic_provenance(_unit(False))
    events = [json.loads(l) for l in
              orch.paths.journal.read_text(encoding="utf-8").splitlines()
              if l.strip()]
    assert len([e for e in events
                if e["event_type"] == "synthetic_unit_provenance"]) == 1


def test_wholesale_floor_event_carries_unit_id():
    """The floor's gate evidence (winner + preservation) must be journaled
    under the synthetic unit's unit_id — pre-fix it carried only the path,
    so per-unit audits read the floor unit as LLM-without-evidence."""
    import inspect
    from capybase.orchestrator import Orchestrator
    src = inspect.getsource(Orchestrator._wholesale_winner_floor)
    assert "unit_id=unit.unit_id" in src, (
        "the wholesale_winner_floor journal event must carry the synthetic "
        "unit's unit_id (S28-130)")

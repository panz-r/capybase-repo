"""Regression tests for the audit-2 architecture repairs (sprint-27).

Locks in the intended prompt-building architecture:
- D1: ONE retry-prompt implementation, carrying the D5c declaration guard
  (formerly journal-mirror-only), composer-based (instruction_position
  applies), with no layout-hardcoded tail.
- D1b/D2: the engine owns the attempt-prompt dispatch; calling it twice per
  round (journal mirror + model path) is idempotent.
- V1/V2/V3: recovery, two-pass code, and shatter prompts follow the
  calibrated output layout.
- D3: the D1-seam rule is one constant embedded in both layout rule sets.
- D4: adjudication prompts share one decision footer.
- U1: the prompt profile's retry_schedule axis applies at provider-config
  time.
"""

from __future__ import annotations

import pytest

import capybase.prompt_profile as pp
from capybase.conflict_model import CandidateResolution, ConflictSide, ConflictUnit
from capybase.context_builder import ContextBuilder
from capybase.resolution_engine import (
    _RESOLVE_RULES_JSON_V6,
    _RESOLVE_RULES_MD,
    _RULE_SEAM_D1,
    build_code_prompt,
    build_recovery_prompt,
    build_retry_prompt,
    build_shattered_repair_prompt,
    retry_prompt_with_trims,
)
from capybase.verification import VerificationFailure


def _unit():
    worktree = "def f():\n<<<<<<< H\n    return 0\n=======\n    return 9\n>>>>>>> b\n"
    return ConflictUnit(
        session_id="s", step_index=1, path="f.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="def f():\n    pass"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="    return 0"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="    return 9"),
        original_worktree_text=worktree, marker_span=(1, 5),
    )


def _ctx():
    return ContextBuilder().build(_unit())


def _fail(msg: str) -> VerificationFailure:
    return VerificationFailure(validator="test", severity="error", message=msg)


def _candidate(text="    return [0, 9]"):
    return CandidateResolution(
        candidate_id="c1", unit_id="u", model_name="m",
        prompt_version="resolve_text_block.v6", resolved_text=text,
        provenance="plain_llm", self_reported_confidence=0.9,
    )


@pytest.fixture
def md_profile():
    pp.set_active_profile(pp.PromptProfile(output_layout=pp.OutputLayout.MARKDOWN_CODE))
    yield
    pp.set_active_profile(None)


@pytest.fixture
def default_profile():
    pp.set_active_profile(None)
    yield


# ---------------------------------------------------------------------------
# D1 — the single retry implementation
# ---------------------------------------------------------------------------


def test_retry_prompt_carries_d5c_guard(default_profile):
    """The declaration guard is in THE retry prompt (formerly the live inline
    copy in propose() lacked it — the guard was journal-mirror-only)."""
    failures = [_fail("'cache' was not declared in this scope")]
    prompt, _trims = retry_prompt_with_trims(_unit(), _ctx(), failures)
    assert "declaration guard" in prompt
    assert "'cache'" in prompt or "cache" in prompt
    # build_retry_prompt is the same single implementation
    assert build_retry_prompt(_unit(), _ctx(), failures) == prompt


def test_retry_prompt_has_no_layout_hardcoded_tail(default_profile):
    """The former tail hardcoded "End with the ```json fenced answer" — a
    layout leak under markdown_code. The contract inside the prompt carries
    its own closing instruction."""
    prompt, _ = retry_prompt_with_trims(
        _unit(), _ctx(), [_fail("syntax error at line 2")])
    assert "End with the ```json" not in prompt


def test_retry_prompt_respects_instruction_position(default_profile):
    """The former inline retry concatenated intro+data+contract+rules directly,
    silently ignoring the profile's instruction_position axis."""
    profile = pp.PromptProfile(
        instruction_position=pp.InstructionPosition.TOP_HEAVY)
    pp.set_active_profile(profile)
    try:
        prompt, _ = retry_prompt_with_trims(
            _unit(), _ctx(), [_fail("syntax error at line 2")])
        # TOP_HEAVY puts the contract+rules BEFORE the data payload marker
        assert prompt.index("CRITICAL rules") < prompt.index("DATA PAYLOAD")
        assert prompt.index("CURRENT_UPSTREAM_SIDE") > prompt.index("DATA PAYLOAD")
    finally:
        pp.set_active_profile(None)


def test_retry_prompt_layout_follows_profile(md_profile):
    prompt, _ = retry_prompt_with_trims(
        _unit(), _ctx(), [_fail("syntax error at line 2")])
    assert "fenced code block" in prompt          # the MD contract
    assert "Escape newlines as" not in prompt      # not the v6 rules


# ---------------------------------------------------------------------------
# D1b/D2 — one dispatch, idempotent per round
# ---------------------------------------------------------------------------


def test_engine_dispatch_matches_public_builders(default_profile):
    from capybase.config import ModelConfig
    from capybase.resolution_engine import ResolutionEngine
    eng = ResolutionEngine(ModelConfig(), client=object())
    failures = [_fail("syntax error at line 2")]
    # Retry path: the dispatch output is exactly the public retry builder's
    p1, v1, _t = eng.build_attempt_prompt(
        _unit(), _ctx(), failures=failures)
    assert p1 == build_retry_prompt(_unit(), _ctx(), failures)
    assert v1.startswith("cegis_retry")
    # Recovery path routes to the recovery builder
    p2, v2, _t2 = eng.build_attempt_prompt(
        _unit(), _ctx(), failures=failures, pending_recovery=True)
    assert p2 == build_recovery_prompt(_unit(), _ctx(), failures)
    assert v2 == "cegis_recovery.v1"


def test_engine_dispatch_idempotent_per_round(default_profile):
    """The journal mirror and the model path both call the dispatch in one
    round — the second call must produce the SAME prompt (history dedup)."""
    from capybase.config import ModelConfig
    from capybase.resolution_engine import ResolutionEngine
    eng = ResolutionEngine(ModelConfig(), client=object())
    unit, ctx = _unit(), _ctx()
    cand = _candidate()
    failures = [_fail("syntax error at line 2")]
    first, _v, _t = eng.build_attempt_prompt(
        unit, ctx, failures=failures, prev_candidate=cand)
    second, _v2, _t2 = eng.build_attempt_prompt(
        unit, ctx, failures=failures, prev_candidate=cand)
    assert first == second


def test_repair_memory_is_per_unit(default_profile):
    """The former orchestrator copy was one list leaked across units; the
    engine keys history per unit_id."""
    from capybase.config import ModelConfig
    from capybase.resolution_engine import ResolutionEngine
    eng = ResolutionEngine(ModelConfig(), client=object())
    ctx = _ctx()
    unit_a = _unit()
    unit_b = _unit()
    unit_b.unit_id = "u2"
    cand = _candidate()
    failures = [_fail("syntax error at line 2")]
    eng.build_attempt_prompt(unit_a, ctx, failures=failures, prev_candidate=cand)
    eng.build_attempt_prompt(unit_a, ctx, failures=failures, prev_candidate=cand)
    assert len(eng._repair_failure_history.get("u", [])) == 1
    assert "u2" not in eng._repair_failure_history


# ---------------------------------------------------------------------------
# V1/V2/V3 — layout follows the calibration on every code-output path
# ---------------------------------------------------------------------------


def test_recovery_prompt_layout_branch(md_profile):
    p = build_recovery_prompt(_unit(), _ctx(), [_fail("needs human")])
    assert "fenced code block" in p
    assert "Escape newlines as" not in p
    assert '"needs_human"' not in p  # no output-key escape hatch


def test_recovery_prompt_default_layout(default_profile):
    p = build_recovery_prompt(_unit(), _ctx(), [_fail("needs human")])
    assert '"resolved_text"' in p
    assert '"needs_human"' not in p


def test_code_prompt_layout_branch(md_profile):
    p = build_code_prompt(_unit(), _ctx(), {"current_side_intent": ["x"]})
    assert "fenced code block" in p
    assert '"resolved_text"' not in p


def test_code_prompt_default_layout(default_profile):
    p = build_code_prompt(_unit(), _ctx(), {"current_side_intent": ["x"]})
    assert '"resolved_text"' in p


def test_shattered_prompt_layout_branch(md_profile):
    p = build_shattered_repair_prompt(_unit(), _candidate(), [_fail("a.py:3:1 error")])
    assert "fenced code block" in p


def test_shattered_prompt_default_layout(default_profile):
    p = build_shattered_repair_prompt(_unit(), _candidate(), [_fail("a.py:3:1 error")])
    assert '"resolved_text"' in p


# ---------------------------------------------------------------------------
# D3 — one seam constant
# ---------------------------------------------------------------------------


def test_seam_rule_single_source():
    assert _RULE_SEAM_D1 in _RESOLVE_RULES_JSON_V6
    assert _RULE_SEAM_D1 in _RESOLVE_RULES_MD
    # exactly one occurrence per rule set (no drift back to duplication)
    assert _RESOLVE_RULES_JSON_V6.count(_RULE_SEAM_D1) == 1
    assert _RESOLVE_RULES_MD.count(_RULE_SEAM_D1) == 1


# ---------------------------------------------------------------------------
# D4 — one decision footer
# ---------------------------------------------------------------------------


def test_decision_footer_shared():
    from capybase.orchestrator import (
        _json_decision_footer,
        _whole_side_adjudication_prompt,
    )
    footer = _json_decision_footer('{"choice": "x"}')
    assert footer.startswith("Respond with ONLY a JSON object:")
    assert footer.endswith('{"choice": "x"}')
    p = _whole_side_adjudication_prompt(
        "f.py", "python", "base", {"current": "a", "replayed": "b"})
    assert p.rstrip().endswith('"}')


# ---------------------------------------------------------------------------
# U1 — retry_schedule applies at provider-config time
# ---------------------------------------------------------------------------


def _resolved_with_schedule(schedule) -> "object":
    from capybase.calibration_profile import ModelProfile
    from capybase.prompt_profile import PromptProfile
    from capybase.provider_config import ProviderConfig, ResolvedProvider
    mp = ModelProfile(model="test-model")
    mp.prompt.profile = PromptProfile(retry_schedule=schedule)
    return ResolvedProvider(
        provider=ProviderConfig(
            name="t", profile="synthetic", base_url="http://x",
            model="test-model", api_key="k"),
        profile=mp, profile_path="synthetic",
    )


def test_retry_schedule_light_applies():
    from capybase.config import Config
    from capybase.provider_config import apply_to_config
    from capybase.prompt_profile import RetrySchedule
    cfg = Config()
    cfg.policy.max_retries_per_unit = 5  # explicit config value
    cfg2, overridden, report = apply_to_config(
        cfg, _resolved_with_schedule(RetrySchedule.LIGHT))
    assert cfg2.policy.max_retries_per_unit == 1
    assert "prompt.retry_schedule" in overridden


def test_retry_schedule_aggressive_applies():
    from capybase.config import Config
    from capybase.provider_config import apply_to_config
    from capybase.prompt_profile import RetrySchedule
    cfg2, _, _ = apply_to_config(
        Config(), _resolved_with_schedule(RetrySchedule.AGGRESSIVE))
    assert cfg2.policy.max_retries_per_unit == 3


def test_retry_schedule_standard_leaves_policy():
    from capybase.config import Config
    from capybase.provider_config import apply_to_config
    from capybase.prompt_profile import RetrySchedule
    cfg = Config()
    cfg.policy.max_retries_per_unit = 5
    cfg2, overridden, _ = apply_to_config(
        cfg, _resolved_with_schedule(RetrySchedule.STANDARD))
    assert cfg2.policy.max_retries_per_unit == 5
    assert "prompt.retry_schedule" not in overridden

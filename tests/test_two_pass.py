"""Tests for two-pass prompting, parallel sampling, and sampling temperature.

Step 2 of the multi-request pipeline. A 3B model reasons better when it
understands the conflict before fixing it: pass 1 extracts intents, pass 2
generates code conditioned on them. Samples are drawn concurrently for speed,
at a raised temperature for diversity.
"""

from __future__ import annotations

from capybase.adapters.llm_openai import LLMResponse
from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.config import ModelConfig
from capybase.context_builder import ContextBuilder
from capybase.resolution_engine import (
    PROMPT_CODE,
    PROMPT_INTENT,
    ResolutionEngine,
    build_code_prompt,
    build_intent_prompt,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class ScriptedClient:
    """Returns canned responses in sequence, recording kwargs per call."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self._i = 0
        self.calls: list[dict] = []

    def complete(self, messages, **kw):
        self.calls.append(kw)
        t = self._responses[self._i % len(self._responses)]
        self._i += 1
        return LLMResponse(text=t, raw={"_accumulated": {"finish_reason": "stop"}})


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


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def test_intent_prompt_asks_only_for_intents():
    unit = _unit()
    prompt = build_intent_prompt(unit, _ctx())
    assert "JSON" in prompt
    assert "current_side_intent" in prompt
    assert "replayed_commit_intent" in prompt
    assert "Do NOT write code" in prompt


def test_code_prompt_includes_intent_map():
    unit = _unit()
    intents = {
        "current_side_intent": ["changed return to 0"],
        "replayed_commit_intent": ["changed return to 9"],
    }
    prompt = build_code_prompt(unit, _ctx(), intents)
    assert "changed return to 0" in prompt
    assert "changed return to 9" in prompt
    assert "resolved_text" in prompt


# ---------------------------------------------------------------------------
# Two-pass generation
# ---------------------------------------------------------------------------


def test_two_pass_extracts_intents_then_generates_code():
    # Pass 1 returns intents; pass 2 returns code.
    client = ScriptedClient([
        '{"current_side_intent": ["return 0"], "replayed_commit_intent": ["return 9"]}',
        '{"resolved_text": "    return (0, 9)"}',
    ])
    cfg = ModelConfig(samples=1, two_pass=True)
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose_two_pass(_unit(), _ctx(), n_samples=1)
    assert len(cands) == 1
    assert "return (0, 9)" in cands[0].resolved_text
    assert cands[0].prompt_version == PROMPT_CODE
    # First call was the intent pass.
    assert len(client.calls) == 2


def test_two_pass_falls_back_on_intent_failure():
    # Intent pass returns garbage → engine degrades to single-pass propose.
    client = ScriptedClient([
        "not json at all",
        '{"resolved_text": "    return 0"}',
    ])
    cfg = ModelConfig(samples=1, two_pass=True)
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose_two_pass(_unit(), _ctx(), n_samples=1)
    assert len(cands) == 1
    # Fell back to single-pass (prompt_version should be the resolve prompt,
    # not the code-from-intent prompt).
    assert cands[0].prompt_version != PROMPT_CODE


def test_two_pass_multiple_samples():
    client = ScriptedClient([
        '{"current_side_intent": ["a"], "replayed_commit_intent": ["b"]}',
        '{"resolved_text": "    return 1"}',
        '{"resolved_text": "    return 2"}',
        '{"resolved_text": "    return 3"}',
    ])
    cfg = ModelConfig(samples=3, two_pass=True, parallel_samples=False)
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose_two_pass(_unit(), _ctx(), n_samples=3)
    assert len(cands) == 3
    # All should have the code prompt version.
    assert all(c.prompt_version == PROMPT_CODE for c in cands)


# ---------------------------------------------------------------------------
# Parallel sampling
# ---------------------------------------------------------------------------


def test_parallel_sampling_produces_n_candidates():
    client = ScriptedClient([
        '{"resolved_text": "    return 1"}',
        '{"resolved_text": "    return 2"}',
        '{"resolved_text": "    return 3"}',
    ])
    cfg = ModelConfig(samples=3, parallel_samples=True)
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose(_unit(), _ctx())
    assert len(cands) == 3
    texts = [c.resolved_text for c in cands]
    assert "    return 1" in texts
    assert "    return 2" in texts


def test_parallel_sampling_uses_sampling_temperature():
    client = ScriptedClient([
        '{"resolved_text": "x"}',
    ] * 3)
    cfg = ModelConfig(
        samples=3, parallel_samples=True,
        temperature=0.2, sampling_temperature=0.8,
    )
    engine = ResolutionEngine(cfg, client=client)
    engine.propose(_unit(), _ctx())
    # All calls should use the sampling temperature (0.8), not the base (0.2).
    assert all(abs(c.get("temperature", 0) - 0.8) < 0.01 for c in client.calls)


def test_sequential_sampling_uses_base_temperature():
    client = ScriptedClient([
        '{"resolved_text": "x"}',
    ])
    cfg = ModelConfig(samples=1, parallel_samples=True, temperature=0.2)
    engine = ResolutionEngine(cfg, client=client)
    engine.propose(_unit(), _ctx())
    # Single sample uses base temperature.
    assert abs(client.calls[0].get("temperature", 0) - 0.2) < 0.01


def test_parallel_disabled_uses_sequential():
    client = ScriptedClient([
        '{"resolved_text": "a"}',
        '{"resolved_text": "b"}',
    ])
    cfg = ModelConfig(samples=2, parallel_samples=False)
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose(_unit(), _ctx())
    assert len(cands) == 2
    assert cands[0].resolved_text == "a"
    assert cands[1].resolved_text == "b"


# ---------------------------------------------------------------------------
# Server-side N sampling (Step 2): one request with n=N instead of N requests
# ---------------------------------------------------------------------------


class BatchClient:
    """A client that serves server-side ``n`` sampling via ``complete_many``.

    Returns all N samples from a single call, recording that one batched
    request was made (not N). Used to verify the optimization that collapses
    N concurrent HTTP requests into one round-trip on a single-GPU server.
    """

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.batch_calls = 0
        self.complete_calls = 0

    def complete(self, messages, **kw):
        self.complete_calls += 1
        return LLMResponse(text=self._responses[0], raw={"_accumulated": {"finish_reason": "stop"}})

    def complete_many(self, messages, *, n, **kw):
        self.batch_calls += 1
        return [
            LLMResponse(text=t, raw={"_accumulated": {"finish_reason": "stop"}})
            for t in self._responses[:n]
        ]


def test_server_side_n_sampling_one_request():
    """When complete_many is available, N samples come from ONE request."""
    client = BatchClient([
        '{"resolved_text": "    return 1"}',
        '{"resolved_text": "    return 2"}',
        '{"resolved_text": "    return 3"}',
    ])
    cfg = ModelConfig(samples=3, parallel_samples=True)
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose(_unit(), _ctx())
    assert len(cands) == 3
    texts = [c.resolved_text for c in cands]
    assert "    return 1" in texts and "    return 2" in texts
    # Exactly one batched request — not three concurrent complete() calls.
    assert client.batch_calls == 1
    assert client.complete_calls == 0


def test_server_side_n_uses_sampling_temperature():
    client = BatchClient(['{"resolved_text": "x"}'] * 3)
    cfg = ModelConfig(
        samples=3, parallel_samples=True,
        temperature=0.2, sampling_temperature=0.8,
    )
    engine = ResolutionEngine(cfg, client=client)
    engine.propose(_unit(), _ctx())
    assert client.batch_calls == 1


def test_server_side_n_falls_back_when_request_fails():
    """If complete_many raises, fall back to the thread-pool path."""
    class FlakyBatch(BatchClient):
        def complete_many(self, messages, *, n, **kw):
            self.batch_calls += 1
            raise RuntimeError("server does not support n")

    client = FlakyBatch([
        '{"resolved_text": "    return 1"}',
        '{"resolved_text": "    return 2"}',
        '{"resolved_text": "    return 3"}',
    ])
    cfg = ModelConfig(samples=3, parallel_samples=True)
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose(_unit(), _ctx())
    # Fell back: still got 3 candidates via complete() calls, not the failed batch.
    assert len(cands) == 3
    assert client.batch_calls == 1
    assert client.complete_calls == 3


def test_server_side_n_falls_back_when_too_few_choices():
    """If the server ignores n and returns fewer choices, fall back."""
    class ShortBatch(BatchClient):
        def complete_many(self, messages, *, n, **kw):
            self.batch_calls += 1
            # Returns only 1 choice regardless of n -> server ignored n.
            return [LLMResponse(text=self._responses[0], raw={"_accumulated": {"finish_reason": "stop"}})]

    client = ShortBatch([
        '{"resolved_text": "    return 1"}',
        '{"resolved_text": "    return 2"}',
        '{"resolved_text": "    return 3"}',
    ])
    cfg = ModelConfig(samples=3, parallel_samples=True)
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose(_unit(), _ctx())
    assert len(cands) == 3
    # Batch was attempted but short; fell back to 3 complete() calls.
    assert client.batch_calls == 1
    assert client.complete_calls == 3


def test_server_side_n_client_without_complete_many():
    """A client with no complete_many uses the thread-pool path unchanged."""
    client = ScriptedClient([
        '{"resolved_text": "    return 1"}',
        '{"resolved_text": "    return 2"}',
        '{"resolved_text": "    return 3"}',
    ])
    cfg = ModelConfig(samples=3, parallel_samples=True)
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose(_unit(), _ctx())
    assert len(cands) == 3
    # ScriptedClient.complete called once per sample (thread-pool fallback).
    assert len(client.calls) == 3


# ---------------------------------------------------------------------------
# Diverse sampling (survey §4.1): per-sample temperature portfolio
# ---------------------------------------------------------------------------


def test_sample_temperatures_uniform_when_disabled():
    """diverse_sampling off (default) → all samples at one temperature."""
    cfg = ModelConfig(samples=5, sampling_temperature=0.9, temperature=0.2)
    engine = ResolutionEngine(cfg, client=ScriptedClient([]))
    temps = engine._sample_temperatures(5, temperature_override=0.9)
    assert temps == [0.9] * 5


def test_sample_temperatures_uniform_n1():
    """N=1 is always a single temperature regardless of the flag."""
    cfg = ModelConfig(samples=1, diverse_sampling=True, sampling_temperature=0.9)
    engine = ResolutionEngine(cfg, client=ScriptedClient([]))
    assert engine._sample_temperatures(1) == [cfg.temperature]


def test_sample_temperatures_diverse_split():
    """diverse_sampling on, N=3 → ceil(3/2)=2 high + 1 low."""
    cfg = ModelConfig(
        samples=3, diverse_sampling=True, sampling_temperature=0.9, temperature=0.2,
    )
    engine = ResolutionEngine(cfg, client=ScriptedClient([]))
    temps = engine._sample_temperatures(3, temperature_override=0.9)
    assert temps == [0.9, 0.9, 0.2]


def test_sample_temperatures_diverse_even_n():
    """N=4 → 2 high + 2 low."""
    cfg = ModelConfig(
        samples=4, diverse_sampling=True, sampling_temperature=0.8, temperature=0.2,
    )
    engine = ResolutionEngine(cfg, client=ScriptedClient([]))
    temps = engine._sample_temperatures(4, temperature_override=0.8)
    assert temps == [0.8, 0.8, 0.2, 0.2]


def test_sample_temperatures_guarantees_both_for_n2():
    """N=2 → 1 high + 1 low (at least one of each)."""
    cfg = ModelConfig(
        samples=2, diverse_sampling=True, sampling_temperature=0.8, temperature=0.3,
    )
    engine = ResolutionEngine(cfg, client=ScriptedClient([]))
    temps = engine._sample_temperatures(2, temperature_override=0.8)
    assert temps == [0.8, 0.3]


def test_sample_temperatures_no_diversity_when_high_le_low():
    """If sampling_temperature <= temperature, no diversity to exploit → uniform
    at the override temperature (the caller's explicit request is honored)."""
    cfg = ModelConfig(
        samples=3, diverse_sampling=True, sampling_temperature=0.2, temperature=0.5,
    )
    engine = ResolutionEngine(cfg, client=ScriptedClient([]))
    temps = engine._sample_temperatures(3, temperature_override=0.2)
    assert temps == [0.2] * 3


def test_diverse_sampling_uses_thread_pool_temperatures():
    """When diverse_sampling is on, samples are drawn at both temperatures via
    N separate complete() calls (the batched n path is bypassed because it
    forces one temperature)."""
    client = ScriptedClient([
        '{"resolved_text": "a"}',
        '{"resolved_text": "b"}',
        '{"resolved_text": "c"}',
    ])
    cfg = ModelConfig(
        samples=3, diverse_sampling=True,
        sampling_temperature=0.8, temperature=0.2, parallel_samples=True,
    )
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose(_unit(), _ctx())
    assert len(cands) == 3
    # Three separate calls, with the diverse temperature portfolio applied.
    assert len(client.calls) == 3
    used_temps = sorted(c.get("temperature", 0) for c in client.calls)
    assert used_temps == [0.2, 0.8, 0.8]


# ---------------------------------------------------------------------------
# Prompt-variant sampling (survey §4 Code Roulette robustness): when
# prompt_variants is on, samples are drawn across distinct resolve-prompt
# phrasings, each tagged on prompt_version, instead of one prompt at varied
# temperatures.
# ---------------------------------------------------------------------------


class MessageRecordingClient:
    """Like ScriptedClient but also records the user prompt per call so we can
    assert which prompt variant each sample used."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self._i = 0
        self.calls: list[dict] = []

    def complete(self, messages, **kw):
        self.calls.append({"messages": messages, **kw})
        t = self._responses[self._i % len(self._responses)]
        self._i += 1
        return LLMResponse(text=t, raw={"_accumulated": {"finish_reason": "stop"}})


def test_prompt_variants_draws_one_sample_per_variant():
    """With prompt_variants on + samples>1 + fresh resolve, one sample is drawn
    per prompt variant via N separate complete() calls, and each candidate's
    prompt_version carries the variant suffix."""
    client = MessageRecordingClient([
        '{"resolved_text": "a"}',
        '{"resolved_text": "b"}',
        '{"resolved_text": "c"}',
    ])
    cfg = ModelConfig(
        samples=3, prompt_variants=True, parallel_samples=True,
        sampling_temperature=0.8, temperature=0.2,
    )
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose(_unit(), _ctx())
    assert len(cands) == 3
    assert len(client.calls) == 3  # one call per variant
    # Each candidate is tagged with its variant suffix on the base version.
    versions = sorted(c.prompt_version for c in cands if c.prompt_version)
    assert versions == ["resolve_text_block.v6", "resolve_text_block.v6#v1", "resolve_text_block.v6#v2"]
    # The three calls used three distinct prompt texts (the variants).
    prompts = [c["messages"][-1]["content"] for c in client.calls]
    assert len(set(prompts)) == 3


def test_prompt_variants_applies_temperature_portfolio():
    """The diverse temperature portfolio still applies across the variants."""
    client = MessageRecordingClient(["a", "b", "c"])
    cfg = ModelConfig(
        samples=3, prompt_variants=True, diverse_sampling=True,
        sampling_temperature=0.8, temperature=0.2, parallel_samples=True,
    )
    engine = ResolutionEngine(cfg, client=client)
    engine.propose(_unit(), _ctx())
    used_temps = sorted(c.get("temperature", 0) for c in client.calls)
    # ceil(3/2)=2 high + 1 low, same portfolio as the diverse path.
    assert used_temps == [0.2, 0.8, 0.8]


def test_prompt_variants_off_keeps_single_prompt():
    """Default (flag off): one prompt reused across all N samples, no suffix on
    prompt_version — identical behavior to before the feature."""
    client = MessageRecordingClient(["a", "b", "c"])
    cfg = ModelConfig(
        samples=3, prompt_variants=False, parallel_samples=True,
    )
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose(_unit(), _ctx())
    prompts = [c["messages"][-1]["content"] for c in client.calls]
    assert len(set(prompts)) == 1  # same prompt for all samples
    assert all(c.prompt_version == "resolve_text_block.v6" for c in cands)


def test_prompt_variants_skipped_on_retry():
    """The variant path must NOT engage on a CEGIS retry — retries need a single
    canonical prompt for reproducible counterexample feedback."""
    from capybase.conflict_model import VerificationFailure

    client = MessageRecordingClient(["a", "b"])
    cfg = ModelConfig(
        samples=3, prompt_variants=True, parallel_samples=True,
    )
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose(
        _unit(), _ctx(),
        failures=[VerificationFailure(validator="x", message="leaked")],
    )
    # Retry path: single cegis_retry version, no variant suffixes.
    assert all(c.prompt_version == "cegis_retry.v6" for c in cands)


def test_prompt_variants_skipped_when_samples_one():
    """samples == 1 never engages the variant path (it needs >1 to span)."""
    client = MessageRecordingClient(["a"])
    cfg = ModelConfig(samples=1, prompt_variants=True, parallel_samples=True)
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose(_unit(), _ctx())
    assert len(cands) == 1
    assert cands[0].prompt_version == "resolve_text_block.v6"  # no suffix


# ---------------------------------------------------------------------------
# Difficulty-aware sample allocation (survey §4 UAB-lite): the n_samples
# override lets a caller draw more samples than config.samples — used by the
# orchestrator to concentrate compute on "complex" units.
# ---------------------------------------------------------------------------


def test_propose_n_samples_override_draws_that_many():
    """propose(n_samples=K) draws exactly K samples even when config.samples
    differs — the override is the source of truth for the count."""
    client = ScriptedClient(["a", "b", "c", "d"])
    cfg = ModelConfig(samples=1, parallel_samples=True)  # base count = 1
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose(_unit(), _ctx(), n_samples=4)
    assert len(cands) == 4
    assert len(client.calls) == 4


def test_propose_n_samples_none_uses_config():
    """No override (default None) → config.samples is used, unchanged."""
    client = ScriptedClient(["a", "b", "c"])
    cfg = ModelConfig(samples=3, parallel_samples=True)
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose(_unit(), _ctx())
    assert len(cands) == 3
    assert len(client.calls) == 3


def test_propose_n_samples_override_beats_config():
    """The override wins over config.samples when both are set."""
    client = ScriptedClient(["a", "b"])
    cfg = ModelConfig(samples=5, parallel_samples=True)  # would draw 5
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose(_unit(), _ctx(), n_samples=2)  # override → 2
    assert len(cands) == 2
    assert len(client.calls) == 2


def test_propose_with_consensus_forwards_n_samples():
    """propose_with_consensus(n_samples=K) forwards the override to propose."""
    from capybase.config import ModelConfig as _MC

    client = ScriptedClient(["a", "b", "c", "d"])
    cfg = _MC(samples=1, parallel_samples=True)
    engine = ResolutionEngine(cfg, client=client)
    cands, _rep = engine.propose_with_consensus(_unit(), _ctx(), n_samples=4)
    assert len(cands) == 4
    assert len(client.calls) == 4


# ---------------------------------------------------------------------------
# PlanSearch-style multi-plan sampling (survey §1): sample N distinct plans →
# one code candidate per plan, tagged code_from_intent.v1#plan{i}.
# ---------------------------------------------------------------------------


def test_plan_search_prompt_asks_for_distinct_plans():
    from capybase.resolution_engine import build_plan_search_prompt

    prompt = build_plan_search_prompt(_unit(), _ctx())
    assert "DISTINCT" in prompt
    assert "constraints" in prompt
    assert "plans" in prompt
    # It must NOT ask for the merged code itself (that's Pass 2).
    assert "Do NOT write the merged code" in prompt


def test_parse_plans_valid_multi():
    from capybase.resolution_engine import parse_plans

    text = '{"constraints": ["a"], "plans": [' \
           '{"strategy": "merge both", "steps": ["x", "y"]}, ' \
           '{"strategy": "guard current", "steps": ["z"]}]}'
    plans = parse_plans(text)
    assert plans is not None
    assert len(plans) == 2
    assert plans[0]["strategy"] == "merge both"


def test_parse_plans_returns_none_for_single_plan():
    """PlanSearch needs >=2 plans to add planning-axis diversity."""
    from capybase.resolution_engine import parse_plans

    text = '{"plans": [{"strategy": "only one", "steps": ["x"]}]}'
    assert parse_plans(text) is None


def test_parse_plans_returns_none_for_malformed():
    from capybase.resolution_engine import parse_plans

    assert parse_plans("not json at all") is None
    assert parse_plans('{"plans": "not a list"}') is None


def test_code_prompt_with_plan_prepends_constraint():
    """With a plan, the 'Implement THIS plan exactly' block appears and carries
    the plan's steps. Without, byte-identical to the original."""
    unit = _unit()
    ctx = _ctx()
    intents = {"current_side_intent": ["a"], "replayed_commit_intent": ["b"]}
    plan = {"strategy": "guard both", "steps": ["add check", "return"]}
    with_prompt = build_code_prompt(unit, ctx, intents, plan=plan)
    without = build_code_prompt(unit, ctx, intents)
    assert "Implement THIS plan exactly" in with_prompt
    assert "guard both" in with_prompt
    assert "add check" in with_prompt
    # Without plan: no plan block.
    assert "Implement THIS plan exactly" not in without


def _plans_json():
    import json

    return json.dumps({
        "constraints": ["c"],
        "plans": [
            {"strategy": "merge both", "steps": ["s1"]},
            {"strategy": "guard current", "steps": ["s2"]},
            {"strategy": "restructure", "steps": ["s3"]},
        ],
    })


def test_plan_search_draws_one_candidate_per_plan():
    """plan_search on + samples>1 → one code candidate per plan, each tagged
    #plan{i}. Call sequence: intent(1) → plan-search(1) → code(N)."""
    import json

    cfg = ModelConfig(
        samples=3, two_pass=True, plan_search=True, parallel_samples=True,
    )
    # Sequence: intent resp, plan-search resp, then 3 code resps.
    client = ScriptedClient([
        json.dumps({"current_side_intent": ["a"], "replayed_commit_intent": ["b"]}),
        _plans_json(),
        '{"resolved_text": "    return 1"}',
        '{"resolved_text": "    return 2"}',
        '{"resolved_text": "    return 3"}',
    ])
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose_two_pass(_unit(), _ctx(), n_samples=3)
    assert len(cands) == 3
    versions = sorted(c.prompt_version for c in cands if c.prompt_version)
    assert versions == [
        "code_from_intent.v1#plan0",
        "code_from_intent.v1#plan1",
        "code_from_intent.v1#plan2",
    ]


def test_plan_search_uses_distinct_prompts_per_plan():
    """Each plan conditions its own code prompt — the 3 code calls carry
    distinct user messages (the plans differ). Order is nondeterministic
    (thread pool), so assert on the SET of strategies present, not indices."""
    import json

    cfg = ModelConfig(samples=3, two_pass=True, plan_search=True, parallel_samples=True)
    client = MessageRecordingClient([
        json.dumps({"current_side_intent": ["a"], "replayed_commit_intent": ["b"]}),
        _plans_json(),
        '{"resolved_text": "1"}', '{"resolved_text": "2"}', '{"resolved_text": "3"}',
    ])
    engine = ResolutionEngine(cfg, client=client)
    engine.propose_two_pass(_unit(), _ctx(), n_samples=3)
    # Calls 3,4,5 are the code calls (indices 2,3,4); each has distinct messages.
    code_calls = client.calls[2:5]
    prompts = [c["messages"][-1]["content"] for c in code_calls]
    assert len(set(prompts)) == 3  # all distinct
    # Each of the three plan strategies appears in exactly one code prompt
    # (completion order is nondeterministic, so check membership not index).
    joined = "\n".join(prompts)
    assert "merge both" in joined
    assert "guard current" in joined
    assert "restructure" in joined


def test_plan_search_falls_back_when_plan_parse_fails():
    """A flaky plan-search response (no valid plans) → falls back to the
    standard single-intent → N-code path (no crash, candidates still produced)."""
    import json

    cfg = ModelConfig(samples=3, two_pass=True, plan_search=True, parallel_samples=True)
    client = ScriptedClient([
        json.dumps({"current_side_intent": ["a"], "replayed_commit_intent": ["b"]}),
        "garbage, not json",  # plan-search fails to parse
        '{"resolved_text": "1"}', '{"resolved_text": "2"}', '{"resolved_text": "3"}',
    ])
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose_two_pass(_unit(), _ctx(), n_samples=3)
    assert len(cands) == 3
    # Fallback path: standard code_from_intent.v1, no #plan tags.
    assert all(c.prompt_version == "code_from_intent.v1" for c in cands)


def test_plan_search_off_uses_standard_two_pass():
    """plan_search off (default) → the standard one-plan → N-code path, no
    plan-search call made."""
    import json

    cfg = ModelConfig(samples=3, two_pass=True, plan_search=False, parallel_samples=True)
    client = ScriptedClient([
        json.dumps({"current_side_intent": ["a"], "replayed_commit_intent": ["b"]}),
        '{"resolved_text": "1"}', '{"resolved_text": "2"}', '{"resolved_text": "3"}',
    ])
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose_two_pass(_unit(), _ctx(), n_samples=3)
    assert len(cands) == 3
    assert all(c.prompt_version == "code_from_intent.v1" for c in cands)
    # Only 4 calls: 1 intent + 3 code (no plan-search call).
    assert len(client.calls) == 4




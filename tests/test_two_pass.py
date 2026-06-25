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

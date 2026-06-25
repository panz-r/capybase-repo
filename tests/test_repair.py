"""Tests for targeted CEGIS repair (Step 4 of the multi-request pipeline).

When a candidate fails a validator, the repair loop sends the broken candidate
back to the model alongside the exact error, asking for a surgical fix rather
than full regeneration. A 3B model is highly capable of fixing its own minor
errors when shown the code + the error.
"""

from __future__ import annotations

from capybase.adapters.llm_openai import LLMResponse
from capybase.conflict_model import (
    CandidateResolution,
    ConflictSide,
    ConflictUnit,
    VerificationFailure,
)
from capybase.config import ModelConfig
from capybase.context_builder import ContextBuilder
from capybase.resolution_engine import (
    PROMPT_REPAIR,
    PROMPT_RETRY,
    ResolutionEngine,
    build_repair_prompt,
)


class FakeClient:
    def __init__(self, responses):
        self._r = list(responses)
        self._i = 0
        self.calls = []

    def complete(self, messages, **kw):
        self.calls.append({"messages": messages, **kw})
        t = self._r[self._i % len(self._r)]
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


def _candidate(text="    return [0, 9"):
    return CandidateResolution(
        candidate_id="c1", unit_id="u", model_name="m",
        prompt_version="resolve_text_block.v5",
        resolved_text=text,
    )


def _failures():
    return [
        VerificationFailure(
            validator="syntax", severity="error",
            message="unexpected EOF while parsing",
            detail={"line": 1, "column": 18},
        )
    ]


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def test_repair_prompt_includes_previous_attempt():
    cand = _candidate()
    prompt = build_repair_prompt(_unit(), _ctx(), cand, _failures())
    assert "YOUR PREVIOUS ATTEMPT" in prompt
    assert "return [0, 9" in prompt  # the broken code is shown
    assert "unexpected EOF" in prompt  # the error is shown
    assert "line: 1" in prompt  # structured detail


def test_repair_prompt_says_fix_not_rewrite():
    prompt = build_repair_prompt(_unit(), _ctx(), _candidate(), _failures())
    assert "fix" in prompt.lower()
    assert "do not rewrite from scratch" in prompt.lower()


# ---------------------------------------------------------------------------
# Engine integration
# ---------------------------------------------------------------------------


def test_propose_uses_repair_prompt_with_prev_candidate():
    client = FakeClient(['{"resolved_text": "    return [0, 9]"}'])
    cfg = ModelConfig(samples=1)
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose(
        _unit(), _ctx(), failures=_failures(), prev_candidate=_candidate()
    )
    assert len(cands) == 1
    assert cands[0].prompt_version == PROMPT_REPAIR
    # The prompt sent to the model should include the broken code.
    sent_prompt = client.calls[0]["messages"][1]["content"]
    assert "return [0, 9" in sent_prompt


def test_propose_uses_retry_prompt_without_prev_candidate():
    client = FakeClient(['{"resolved_text": "    return [0, 9]"}'])
    cfg = ModelConfig(samples=1)
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose(_unit(), _ctx(), failures=_failures())
    assert cands[0].prompt_version == PROMPT_RETRY
    # The retry prompt does NOT include a "previous attempt" section.
    sent_prompt = client.calls[0]["messages"][1]["content"]
    assert "YOUR PREVIOUS ATTEMPT" not in sent_prompt


def test_propose_uses_retry_prompt_when_prev_candidate_empty():
    """If the previous candidate has no resolved_text (parse failure), fall
    back to the full retry prompt — there's nothing to repair."""
    empty_cand = _candidate(text="")
    client = FakeClient(['{"resolved_text": "    return 1"}'])
    cfg = ModelConfig(samples=1)
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose(
        _unit(), _ctx(), failures=_failures(), prev_candidate=empty_cand
    )
    assert cands[0].prompt_version == PROMPT_RETRY


def test_propose_uses_resolve_prompt_without_failures():
    client = FakeClient(['{"resolved_text": "    return 1"}'])
    cfg = ModelConfig(samples=1)
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose(_unit(), _ctx())  # no failures, no prev_candidate
    assert cands[0].prompt_version == "resolve_text_block.v5"

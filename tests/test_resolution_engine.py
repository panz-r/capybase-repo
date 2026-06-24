from capybase.adapters.llm_openai import LLMResponse, coerce_candidate_dict
from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.context_builder import ContextBuilder
from capybase.resolution_engine import (
    PROMPT_RESOLVE,
    ResolutionEngine,
    build_resolve_prompt,
)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        self.calls.append({"messages": messages, "json_mode": json_mode})
        if not self.responses:
            raise RuntimeError("no more fake responses")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return LLMResponse(text=r)


def _unit():
    return ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="def f():\n    return 1"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="    return 2"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="    return 3"),
        original_worktree_text="def f():\n<<<<<<< H\n    return 2\n=======\n    return 3\n>>>>>>> b\n",
        marker_span=(1, 5),
    )


def test_propose_parses_json():
    payload = '{"resolved_text": "    return 5", "self_reported_confidence": 0.9, "explanation": "sum"}'
    engine = ResolutionEngine(_cfg(), client=FakeClient([payload]))
    cands = engine.propose(_unit(), ContextBuilder().build(_unit()))
    assert len(cands) == 1
    c = cands[0]
    assert c.resolved_text == "    return 5"
    assert c.self_reported_confidence == 0.9
    assert c.prompt_version == PROMPT_RESOLVE
    assert c.parse_warnings == []


def test_propose_handles_bad_json():
    engine = ResolutionEngine(_cfg(), client=FakeClient(["not json at all"]))
    cands = engine.propose(_unit(), ContextBuilder().build(_unit()))
    assert cands[0].needs_human is True
    assert cands[0].parse_warnings


def test_propose_handles_request_error():
    engine = ResolutionEngine(_cfg(), client=FakeClient([RuntimeError("boom")]))
    cands = engine.propose(_unit(), ContextBuilder().build(_unit()))
    assert cands[0].needs_human is True


def test_retry_prompt_uses_failures():
    from capybase.conflict_model import VerificationFailure

    engine = ResolutionEngine(
        _cfg(), client=FakeClient(['{"resolved_text": "    return 9"}'])
    )
    failures = [VerificationFailure(validator="no_conflict_markers", message="leaked")]
    cands = engine.propose(
        _unit(), ContextBuilder().build(_unit()), failures=failures
    )
    assert cands[0].prompt_version == "cegis_retry.v4"


def test_resolve_prompt_contains_sides():
    u = _unit()
    prompt = build_resolve_prompt(u, ContextBuilder().build(u))
    assert "CURRENT_UPSTREAM_SIDE" in prompt
    assert "REPLAYED_COMMIT_SIDE" in prompt
    assert "BASE" in prompt


def _cfg():
    from capybase.config import ModelConfig

    return ModelConfig(base_url="http://x/v1", model="m", samples=1)

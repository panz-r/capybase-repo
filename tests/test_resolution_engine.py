from capybase.adapters.llm_openai import LLMResponse, coerce_candidate_dict
from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.context_builder import ContextBuilder
from capybase.resolution_engine import (
    PROMPT_RESOLVE,
    ResolutionEngine,
    build_resolve_prompt,
    build_resolve_prompt_variants,
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
    assert cands[0].prompt_version == "cegis_retry.v5"


def test_resolve_prompt_contains_sides():
    u = _unit()
    prompt = build_resolve_prompt(u, ContextBuilder().build(u))
    assert "CURRENT_UPSTREAM_SIDE" in prompt
    assert "REPLAYED_COMMIT_SIDE" in prompt
    assert "BASE" in prompt


# --- prompt-variant generation (survey §4 Code Roulette robustness) ---


def test_variants_baseline_equals_resolve_prompt_byte_for_byte():
    """Variant 0 (the baseline suffix) must be byte-identical to
    build_resolve_prompt — the refactor to parts must not change the canonical
    prompt at all."""
    u = _unit()
    ctx = ContextBuilder().build(u)
    baseline = build_resolve_prompt(u, ctx)
    variants = build_resolve_prompt_variants(u, ctx, k=3)
    v0_text, v0_suffix = variants[0]
    assert v0_suffix == ""
    assert v0_text == baseline


def test_variants_count_and_clamp():
    """Returns up to k variants; k clamps to at least 1."""
    u = _unit()
    ctx = ContextBuilder().build(u)
    assert len(build_resolve_prompt_variants(u, ctx, k=3)) == 3
    assert len(build_resolve_prompt_variants(u, ctx, k=2)) == 2
    assert len(build_resolve_prompt_variants(u, ctx, k=1)) == 1
    # k=0 still yields the baseline (clamped to >= 1).
    assert len(build_resolve_prompt_variants(u, ctx, k=0)) == 1


def test_variants_carry_identical_contract_and_sides():
    """Every variant must contain the exact same three sides and the full JSON
    contract block + CRITICAL rules — only ordering/framing differs, so the
    spliced-output semantics are invariant across phrasings."""
    u = _unit()
    ctx = ContextBuilder().build(u)
    variants = build_resolve_prompt_variants(u, ctx, k=3)
    texts = [t for t, _ in variants]
    # The conflict sides appear verbatim in every variant.
    for t in texts:
        assert "    return 2" in t  # current side
        assert "    return 3" in t  # replayed side
        assert "def f():\n    return 1" in t  # base side
        # The JSON contract keys + CRITICAL rules are present in every variant.
        assert '"resolved_text": "<merged replacement text>"' in t
        assert "CRITICAL rules:" in t
        assert "PRESERVE leading indentation" in t


def test_variants_are_mutually_distinct():
    """The phrasings must differ from one another (else there is no diversity
    to exploit)."""
    u = _unit()
    ctx = ContextBuilder().build(u)
    variants = build_resolve_prompt_variants(u, ctx, k=3)
    texts = [t for t, _ in variants]
    assert len(set(texts)) == 3
    # v1 puts the contract before the data; v2 prepends the minimal-diff steer.
    assert texts[1].index('"resolved_text"') < texts[1].index("CURRENT_UPSTREAM_SIDE body")
    assert "smallest change that merges both intents" in texts[2]
    assert "smallest change that merges both intents" not in texts[0]


# --- diff3-refined sides (Step 1 wiring): prefer the minimized window ---


def _unit_with_refined():
    """A unit whose worktree markers are wider than the diff3-minimized sides."""
    u = _unit()
    # Simulate diff3 finding tighter boundaries than the raw marker sides.
    u.structural_metadata["diff3_refined"] = {
        "current": "    return 2  # refined-current",
        "base": "def f():\n    return 1  # refined-base",
        "replayed": "    return 3  # refined-replayed",
    }
    return u


def test_resolve_prompt_uses_refined_sides_when_present():
    u = _unit_with_refined()
    prompt = build_resolve_prompt(u, ContextBuilder().build(u))
    # The refined (minimal) side texts appear in the conflict-side sections,
    # NOT the raw marker sides. (The surrounding-context window still shows the
    # original worktree text — that is correct and separate from the sides.)
    assert "refined-current" in prompt
    assert "refined-base" in prompt
    assert "refined-replayed" in prompt
    # The refined sides replace the raw sides in the dedicated side sections:
    # the BASE section must carry the refined-base marker, not just raw base.
    assert prompt.count("refined-base") >= 1


def test_intent_prompt_uses_refined_sides():
    from capybase.resolution_engine import build_intent_prompt

    u = _unit_with_refined()
    prompt = build_intent_prompt(u, ContextBuilder().build(u))
    assert "refined-current" in prompt
    assert "refined-replayed" in prompt
    assert "refined-base" in prompt


def test_code_prompt_uses_refined_sides():
    from capybase.resolution_engine import build_code_prompt

    u = _unit_with_refined()
    prompt = build_code_prompt(u, ContextBuilder().build(u), {"current_side_intent": [], "replayed_commit_intent": []})
    assert "refined-current" in prompt
    assert "refined-replayed" in prompt


def test_resolve_prompt_falls_back_to_raw_sides_without_refinement():
    # No diff3_refined in metadata → raw sides used (the common path).
    u = _unit()
    assert u.refined_sides is None
    prompt = build_resolve_prompt(u, ContextBuilder().build(u))
    assert "    return 2" in prompt
    assert "    return 3" in prompt


def test_refined_sides_property_reads_metadata():
    u = _unit_with_refined()
    sides = u.refined_sides
    assert sides is not None
    assert sides == (
        "    return 2  # refined-current",
        "def f():\n    return 1  # refined-base",
        "    return 3  # refined-replayed",
    )


def test_refined_sides_property_none_when_absent():
    u = _unit()
    assert u.refined_sides is None


def _cfg():
    from capybase.config import ModelConfig

    return ModelConfig(base_url="http://x/v1", model="m", samples=1)


# --- failure_kind + truncation detection ---


class MetaClient:
    """Returns LLMResponses with controllable raw metadata (finish_reason)."""

    def __init__(self, responses):
        self.responses = list(responses)

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_truncated_finish_length_marks_truncated():
    from capybase.adapters.llm_openai import LLMResponse

    # raw carries finish_reason=length in _accumulated (streaming shape)
    resp = LLMResponse(text="<think>ramble...</think>", raw={"_accumulated": {"finish_reason": "length"}})
    engine = ResolutionEngine(_cfg(), client=MetaClient([resp]))
    cand = engine.propose(_unit(), ContextBuilder().build(_unit()))[0]
    assert cand.failure_kind == "truncated"
    assert cand.needs_human is True


def test_request_error_marks_request_failed():
    engine = ResolutionEngine(_cfg(), client=MetaClient([RuntimeError("timeout")]))
    cand = engine.propose(_unit(), ContextBuilder().build(_unit()))[0]
    assert cand.failure_kind == "request_failed"


def test_parse_failure_marks_parse_failed():
    from capybase.adapters.llm_openai import LLMResponse

    resp = LLMResponse(text="not json at all", raw={"choices": [{"finish_reason": "stop"}]})
    engine = ResolutionEngine(_cfg(), client=MetaClient([resp]))
    cand = engine.propose(_unit(), ContextBuilder().build(_unit()))[0]
    assert cand.failure_kind == "parse_failed"


def test_model_says_needs_human_marks_model_refusal():
    from capybase.adapters.llm_openai import LLMResponse

    payload = '{"resolved_text": "x", "needs_human": true}'
    resp = LLMResponse(text=payload, raw={"choices": [{"finish_reason": "stop"}]})
    engine = ResolutionEngine(_cfg(), client=MetaClient([resp]))
    cand = engine.propose(_unit(), ContextBuilder().build(_unit()))[0]
    assert cand.failure_kind == "model_refusal"
    assert cand.needs_human is True


def test_well_formed_has_no_failure_kind():
    from capybase.adapters.llm_openai import LLMResponse

    payload = '{"resolved_text": "    return 1", "needs_human": false}'
    resp = LLMResponse(text=payload, raw={"choices": [{"finish_reason": "stop"}]})
    engine = ResolutionEngine(_cfg(), client=MetaClient([resp]))
    cand = engine.propose(_unit(), ContextBuilder().build(_unit()))[0]
    assert cand.failure_kind == ""
    assert cand.needs_human is False


def test_candidate_carries_token_entropy_from_response():
    """TECP (survey §4.1): the model-side uncertainty signal on the LLMResponse
    is surfaced onto the candidate so the calibration seam can learn from it."""
    from capybase.adapters.llm_openai import LLMResponse

    payload = '{"resolved_text": "    return 1", "needs_human": false}'
    resp = LLMResponse(
        text=payload,
        raw={"choices": [{"finish_reason": "stop"}]},
        mean_token_entropy=0.73,
    )
    engine = ResolutionEngine(_cfg(), client=MetaClient([resp]))
    cand = engine.propose(_unit(), ContextBuilder().build(_unit()))[0]
    assert cand.failure_kind == ""
    assert cand.mean_token_entropy == 0.73


def test_candidate_entropy_none_when_response_had_none():
    """A response with no entropy (capture off, or server omitted logprobs)
    yields a candidate with mean_token_entropy=None, not 0.0."""
    from capybase.adapters.llm_openai import LLMResponse

    payload = '{"resolved_text": "    return 1", "needs_human": false}'
    resp = LLMResponse(text=payload, raw={"choices": [{"finish_reason": "stop"}]})
    engine = ResolutionEngine(_cfg(), client=MetaClient([resp]))
    cand = engine.propose(_unit(), ContextBuilder().build(_unit()))[0]
    assert cand.mean_token_entropy is None

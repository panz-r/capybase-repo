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
        prompt_version="resolve_text_block.v6",
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


def _multi_hunk_rust_unit():
    """A multi-hunk Rust conflict (struct + impl): base is the full file, the
    sides are narrow conflict-block fragments — the case that triggers the
    multi-hunk file-structure-only annotation path in _structural_context_block.
    Mirrors the rust_impl live-eval scenario."""
    base = (
        "pub struct Config {\n    pub name: String,\n    pub max_retries: u32,\n}\n\n"
        "impl Config {\n    pub fn new() -> Self {\n        Config {\n"
        '            name: "capybase".to_string(),\n            max_retries: 3,\n        }\n    }\n\n'
        '    pub fn label(&self) -> String {\n'
        '        format!("{} (retries={})", self.name, self.max_retries)\n    }\n}\n'
    )
    return ConflictUnit(
        session_id="s", step_index=1, path="src/config.rs", language="rust",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="            max_retries: 5,"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="            max_retries: 3,\n            timeout_ms: 10000,"),
        original_worktree_text=base, marker_span=(8, 10),
    )


def test_repair_preserve_directive_is_tentative_multihunk():
    """The 'Required: preserve ALL units' DIRECTIVE is tentative: shown on the
    first repair (attempt 0), omitted on subsequent repairs (attempt >= 1). But
    the file-structure INVENTORY (unit list, enclosing unit) stays on all
    attempts — it's orientation the model needs every time.

    Only the preserve directive is gated, because a small model can take it too
    literally on a second repair where the correct fix may legitimately
    restructure a unit (inlining, splitting). The structure inventory doesn't
    have that risk."""
    unit = _multi_hunk_rust_unit()
    ctx = ContextBuilder().build(unit)
    cand = _candidate()
    failures = _failures()
    first = build_repair_prompt(unit, ctx, cand, failures, attempt=0)
    second = build_repair_prompt(unit, ctx, cand, failures, attempt=1)
    # Structure inventory present on BOTH attempts.
    assert "STRUCTURAL CONTEXT" in first
    assert "STRUCTURAL CONTEXT" in second
    assert "File structure:" in first
    assert "File structure:" in second
    assert "This conflict is inside:" in first
    assert "This conflict is inside:" in second
    # Preserve directive: present on attempt 0, ABSENT on attempt 1.
    assert "Required: preserve ALL units" in first
    assert "Required: preserve ALL units" not in second


def test_repair_structure_inventory_always_present():
    """The structural-context BLOCK (file structure, unit changes) is always
    shown regardless of attempt — only the preserve directive is tentative."""
    unit = _multi_hunk_rust_unit()
    ctx = ContextBuilder().build(unit)
    cand = _candidate()
    failures = _failures()
    for attempt in (0, 1, 2, 3):
        prompt = build_repair_prompt(unit, ctx, cand, failures, attempt=attempt)
        assert "STRUCTURAL CONTEXT" in prompt, f"attempt {attempt} missing structure"
        assert "File structure:" in prompt, f"attempt {attempt} missing file structure"


def test_repair_prompt_says_fix_not_rewrite():
    prompt = build_repair_prompt(_unit(), _ctx(), _candidate(), _failures())
    assert "fix" in prompt.lower()
    assert "do not rewrite from scratch" in prompt.lower()


def test_repair_prompt_requires_plan_before_fix():
    """Self-correction plan step (survey §3.3): the repair prompt forces the
    model to reason about WHY each failure happened + the fix BEFORE emitting
    resolved_text — internalizing the critic feedback so retries converge
    instead of reproducing the same mistake. The plan is a `plan` field the
    candidate parser preserves for audit."""
    prompt = build_repair_prompt(_unit(), _ctx(), _candidate(), _failures())
    assert "plan" in prompt.lower()
    # Asks the model to state why + the fix per failure, then emit the code.
    assert "why it happened" in prompt.lower() or "why" in prompt.lower()
    # The JSON schema includes the plan field.
    assert '"plan"' in prompt


def test_repair_plan_field_is_captured_on_candidate():
    """A model response that includes a `plan` field is parsed and stored on the
    CandidateResolution as repair_plan (auditable), and resolved_text is still
    extracted correctly — the plan field doesn't disrupt the candidate contract."""
    client = FakeClient([
        '{"plan": "The syntax error is an unclosed bracket; I will close it at line 1.", '
        '"resolved_text": "    return [0, 9]", "explanation": "closed bracket"}'
    ])
    cfg = ModelConfig(samples=1)
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose(
        _unit(), _ctx(), failures=_failures(), prev_candidate=_candidate()
    )
    assert len(cands) == 1
    assert cands[0].resolved_text == "    return [0, 9]"
    assert "unclosed bracket" in cands[0].repair_plan


# ---------------------------------------------------------------------------
# SEARCH/REPLACE focused repair (§3): the model emits targeted edits against the
# previous attempt instead of reproducing the whole resolved_text.
# ---------------------------------------------------------------------------

from capybase.resolution_engine import apply_search_replace  # noqa: E402


def test_apply_search_replace_single_edit():
    out, warns = apply_search_replace("def f():\n    return 1\n", [{"search": "return 1", "replace": "return 2"}])
    assert out == "def f():\n    return 2\n"
    assert warns == []


def test_apply_search_replace_multiple_edits_in_order():
    out, warns = apply_search_replace("a=1\nb=2\n", [
        {"search": "a=1", "replace": "a=10"},
        {"search": "b=2", "replace": "b=20"},
    ])
    assert out == "a=10\nb=20\n"
    assert warns == []


def test_apply_search_replace_not_found_warns_and_skips():
    out, warns = apply_search_replace("def f():\n    return 1\n", [{"search": "nope", "replace": "x"}])
    assert out == "def f():\n    return 1\n"  # unchanged
    assert len(warns) == 1 and "not found" in warns[0]


def test_apply_search_replace_all_missed_returns_prev_unchanged():
    out, warns = apply_search_replace("abc", [{"search": "x", "replace": "y"}, {"search": "z", "replace": "w"}])
    assert out == "abc"
    assert len(warns) == 2


def test_apply_search_replace_empty_search_skipped():
    out, warns = apply_search_replace("x", [{"search": "", "replace": "y"}])
    assert out == "x"
    assert "empty search" in warns[0]


def test_apply_search_replace_string_edit_skipped():
    """A malformed edit (a string instead of a dict) is skipped, not crashed on.

    Small models sometimes emit edits as bare strings instead of
    {"search": ..., "replace": ...} objects. The parser must degrade to the
    full-resolved_text fallback, not crash the retry loop."""
    out, warns = apply_search_replace("def f():\n    1\n", [
        "def f():\n    1\n",  # string instead of dict
        {"search": "1", "replace": "2"},
    ])
    assert out == "def f():\n    2\n"  # second (valid) edit still applies
    assert "not an object" in warns[0]


def test_repair_prompt_forces_full_mode():
    """The repair prompt forces FULL mode (complete replacement text) — no
    EDIT/search-replace mode. Small models are unreliable at exact substring
    matching, so the prompt asks for the full corrected text only."""
    prompt = build_repair_prompt(_unit(), _ctx(), _candidate(), _failures())
    assert "resolved_text" in prompt
    # EDIT mode (search/replace) is NOT offered.
    assert '"edits"' not in prompt
    assert "EDIT mode" not in prompt
    assert "SEARCH/REPLACE" not in prompt


def test_repair_edit_mode_applies_to_previous_attempt():
    """A repair response with `edits` (edit mode) → the edits are applied to the
    previous attempt's resolved_text; the candidate carries the applied result."""
    prev = _candidate()  # resolved_text = "    return [0, 9" (unclosed bracket)
    client = FakeClient([
        '{"edits": [{"search": "return [0, 9", "replace": "return [0, 9]"}], '
        '"plan": "close the bracket", "explanation": "added ]"}'
    ])
    cfg = ModelConfig(samples=1)
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose(_unit(), _ctx(), failures=_failures(), prev_candidate=prev)
    assert len(cands) == 1
    # The edit was applied: the bracket is now closed.
    assert cands[0].resolved_text == "    return [0, 9]"


def test_repair_full_mode_still_works():
    """A repair response with `resolved_text` (full mode, no edits) → unchanged
    behavior: the candidate carries the model's full resolved_text."""
    client = FakeClient(['{"resolved_text": "    return [0, 9]", "explanation": "full rewrite"}'])
    cfg = ModelConfig(samples=1)
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose(_unit(), _ctx(), failures=_failures(), prev_candidate=_candidate())
    assert len(cands) == 1
    assert cands[0].resolved_text == "    return [0, 9]"


def test_repair_edit_mode_all_missed_falls_back_to_full():
    """When edits are present but ALL miss, fall back to the model's resolved_text
    (full mode) if provided — graceful degradation, never empty/garbage."""
    prev = _candidate()
    client = FakeClient([
        '{"edits": [{"search": "NONEXISTENT", "replace": "x"}], '
        '"resolved_text": "    return [0, 9]", "explanation": "fallback to full"}'
    ])
    cfg = ModelConfig(samples=1)
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose(_unit(), _ctx(), failures=_failures(), prev_candidate=prev)
    assert len(cands) == 1
    # Edits all missed → fall back to the provided full resolved_text.
    assert cands[0].resolved_text == "    return [0, 9]"



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
    assert cands[0].prompt_version == "resolve_text_block.v6"


# ---------------------------------------------------------------------------
# CEGIS resilience: no-op edit detection, escape hatch, CoT ordering
# ---------------------------------------------------------------------------


def test_apply_search_replace_detects_noop_edit():
    """A SEARCH/REPLACE where search == replace is a no-op — the model is
    signaling it can't find anything to fix. The edit is skipped and a warning
    is recorded so the caller can detect 'all edits were no-ops'."""
    from capybase.resolution_engine import apply_search_replace
    applied, warnings = apply_search_replace("hello world", [
        {"search": "hello", "replace": "hello"},  # no-op
        {"search": "world", "replace": "planet"},  # real
    ])
    assert applied == "hello planet"
    assert any("no-op" in w for w in warnings)


def test_apply_search_replace_all_noops_produces_warning():
    """When every edit is a no-op, the result is unchanged and all warnings
    carry the no-op marker — _apply_repair_edits reads this to set
    failure_kind='no_op_repair'."""
    from capybase.resolution_engine import apply_search_replace
    applied, warnings = apply_search_replace("hello world", [
        {"search": "hello", "replace": "hello"},
        {"search": "world", "replace": "world"},
    ])
    assert applied == "hello world"
    assert len(warnings) == 2
    assert all("no-op" in w for w in warnings)


def test_repair_prompt_has_validator_escape_hatch():
    """The repair prompt includes the suspected_validator_error escape hatch
    field and the validator-context explanation (HOW YOUR CODE IS TESTED)."""
    prompt = build_repair_prompt(_unit(), _ctx(), _candidate(), _failures())
    assert "suspected_validator_error" in prompt
    assert "HOW YOUR CODE IS TESTED" in prompt
    assert "VALIDATOR FEEDBACK ANOMALIES" in prompt


def test_resolve_prompt_has_cot_before_code():
    """The resolve schema puts merge_analysis BEFORE resolved_text so the model
    reasons about the merge before generating code (front-loaded CoT)."""
    from capybase.resolution_engine import build_resolve_prompt
    prompt = build_resolve_prompt(_unit(), _ctx())
    analysis_pos = prompt.find('"merge_analysis"')
    resolved_pos = prompt.find('"resolved_text"')
    assert analysis_pos > 0, "merge_analysis field missing from schema"
    assert resolved_pos > 0, "resolved_text field missing from schema"
    assert analysis_pos < resolved_pos, "merge_analysis must come before resolved_text"


def test_candidate_parses_suspected_validator_error():
    """The candidate parser extracts suspected_validator_error from the model's
    JSON response."""
    client = FakeClient([
        '{"plan": "code is correct", "edits": [], "resolved_text": "    return 1", '
        '"explanation": "snippet is valid", "suspected_validator_error": true, '
        '"self_reported_confidence": 0.9}'
    ])
    cfg = ModelConfig(samples=1)
    engine = ResolutionEngine(cfg, client=client)
    cands = engine.propose(
        _unit(), _ctx(),
        failures=[VerificationFailure(validator="rust_syntax", message="error")],
        prev_candidate=_candidate(),
    )
    assert cands[0].suspected_validator_error is True


def test_risk_escalates_on_no_op_repair():
    """The risk engine escalates immediately when failure_kind == 'no_op_repair'
    — retrying would produce the identical candidate (infinite loop)."""
    from capybase.risk import RiskEngine
    from capybase.verification import VerificationResult
    vr = VerificationResult(
        candidate_id="c1", unit_id="u", passed=False,
        hard_failures=[], warnings=[], features={"language": "rust"},
    )
    engine = RiskEngine()
    decision = engine.decide(vr, retry_count=0, failure_kind="no_op_repair")
    assert decision.action == "escalate"


def test_risk_escalates_on_suspected_validator_error():
    """The risk engine escalates immediately when the model flags
    suspected_validator_error — the candidate is preserved for human review."""
    from capybase.risk import RiskEngine
    from capybase.verification import VerificationResult
    vr = VerificationResult(
        candidate_id="c1", unit_id="u", passed=False,
        hard_failures=[], warnings=[], features={"language": "rust"},
    )
    engine = RiskEngine()
    decision = engine.decide(vr, retry_count=0, suspected_validator_error=True)
    assert decision.action == "escalate"

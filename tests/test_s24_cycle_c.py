"""Sprint-24 cycle-C retry-diversity tests.

Covers the wiring of the R5 retry-presentation ladder into ``propose()`` and
the truncation-looping breaker discovered by the C7' diagnostics (all four
"parsed-empty" specimen cases — flask-0006, redis-0052, tokio-0108,
zenodo-hdiff-0079 — are ``failure_kind="truncated"``: the model runs past the
output ceiling on 160-1,317-token units, i.e. a repetition loop, not a
refusal; same-family retries re-loop).
"""

from __future__ import annotations

from capybase.adapters.llm_openai import LLMResponse
from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.context_builder import ContextBuilder
from capybase.prompt_profile import (
    SideOrdering,
    active_profile,
    set_active_profile,
)
from capybase.resolution_engine import ResolutionEngine

_PAYLOAD = (
    '{"resolved_text": "    return 5", "self_reported_confidence": 0.9,'
    ' "explanation": "m"}'
)


class RecordingClient:
    """FakeClient that records temperature + the active profile per call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        self.calls.append({
            "temperature": temperature,
            "profile": active_profile(),
        })
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return LLMResponse(text=r)


def _cfg():
    from capybase.config import ModelConfig

    return ModelConfig(base_url="http://x/v1", model="m", samples=1,
                       temperature=0.2)


def _unit():
    return ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="def f():\n    return 1"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="    return 2"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="    return 3"),
        original_worktree_text=(
            "def f():\n<<<<<<< H\n    return 2\n=======\n    return 3\n>>>>>>> b\n"
        ),
        marker_span=(1, 5),
    )


def _truncated_candidate():
    from capybase.conflict_model import CandidateResolution

    return CandidateResolution(
        candidate_id="prev", unit_id="u", model_name="m",
        prompt_version="resolve_text_block.v6",
        resolved_text="", failure_kind="truncated",
        finish_reason="length",
    )


def _teardown_profile():
    set_active_profile(None)


def test_truncated_previous_attempt_raises_temperature():
    """The loop-breaker: when the previous attempt truncated (finish_reason=
    length / failure_kind=truncated), the retry samples at base + 0.35 so the
    sampler escapes the repetition cycle. Same-temperature retries re-loop."""
    try:
        client = RecordingClient([_PAYLOAD])
        engine = ResolutionEngine(_cfg(), client=client)
        engine.propose(
            _unit(), ContextBuilder().build(_unit()),
            prev_candidate=_truncated_candidate(), attempt=1,
        )
        assert client.calls, "no sample drawn"
        assert client.calls[0]["temperature"] == 0.55  # 0.2 + 0.35
    finally:
        _teardown_profile()


def test_explicit_temperature_override_respected():
    """An explicit override (R3's diverse probes ride this path with
    n_samples=1) passes through to the client — the sequential path used to
    drop it, so R3's 0.4/0.6 temperatures were dead wires."""
    try:
        client = RecordingClient([_PAYLOAD])
        engine = ResolutionEngine(_cfg(), client=client)
        engine.propose(
            _unit(), ContextBuilder().build(_unit()),
            temperature_override=0.6,
        )
        assert client.calls[0]["temperature"] == 0.6
    finally:
        _teardown_profile()


def test_no_truncation_keeps_base_temperature():
    """A normal retry (previous attempt didn't truncate) samples at the base
    temperature — the loop-breaker is truncation-triggered, not a blanket
    retry-temperature change."""
    try:
        from capybase.conflict_model import CandidateResolution

        prev = CandidateResolution(
            candidate_id="prev", unit_id="u", model_name="m",
            prompt_version="resolve_text_block.v6",
            resolved_text="    return 4",
        )
        client = RecordingClient([_PAYLOAD])
        engine = ResolutionEngine(_cfg(), client=client)
        engine.propose(
            _unit(), ContextBuilder().build(_unit()),
            prev_candidate=prev, attempt=1,
        )
        assert client.calls[0]["temperature"] == 0.2
    finally:
        _teardown_profile()


def test_retry_ladder_rotates_profile_per_attempt():
    """attempt 1+ activates one presentation axis per attempt (R5): attempt 1
    flips side ordering, attempt 2 flips the output layout, attempt 3 flips
    the instruction position. The variant is active DURING the call (prompt
    build + parse stay in sync) and restored after."""
    try:
        base = active_profile()
        base_ordering = base.side_ordering

        client = RecordingClient([_PAYLOAD, _PAYLOAD, _PAYLOAD])
        engine = ResolutionEngine(_cfg(), client=client)
        ctx = ContextBuilder().build(_unit())
        unit = _unit()
        from capybase.conflict_model import VerificationFailure
        failures = [VerificationFailure(validator="syntax", message="bad")]

        engine.propose(unit, ctx, failures=failures, attempt=1)
        assert client.calls[0]["profile"].side_ordering != base_ordering, (
            "attempt 1 should flip side ordering"
        )
        # Restored after the call.
        assert active_profile().side_ordering == base_ordering

        engine.propose(unit, ctx, failures=failures, attempt=2)
        assert (client.calls[1]["profile"].output_layout
                != base.output_layout), (
            "attempt 2 should flip the output layout"
        )
        assert active_profile().output_layout == base.output_layout

        engine.propose(unit, ctx, failures=failures, attempt=3)
        assert (client.calls[2]["profile"].instruction_position
                != base.instruction_position), (
            "attempt 3 should flip the instruction position"
        )
        assert active_profile().instruction_position == base.instruction_position
    finally:
        _teardown_profile()


def test_fresh_resolve_does_not_touch_profile():
    """attempt 0 (fresh resolve) leaves the calibrated profile active — the
    ladder is retry-only, preserving the calibration's baseline."""
    try:
        client = RecordingClient([_PAYLOAD])
        engine = ResolutionEngine(_cfg(), client=client)
        before = active_profile()
        engine.propose(_unit(), ContextBuilder().build(_unit()))
        assert client.calls[0]["profile"] is before
        assert active_profile() is before
    finally:
        _teardown_profile()


def test_profile_restored_when_sampling_raises():
    """An exception mid-propose (transport error) must still restore the
    process-wide profile — a leaked variant would contaminate every
    subsequent unit's prompt AND parser in the session."""
    try:
        from capybase.conflict_model import VerificationFailure

        client = RecordingClient([RuntimeError("endpoint down")])
        engine = ResolutionEngine(_cfg(), client=client)
        base_before = active_profile()
        try:
            engine.propose(
                _unit(), ContextBuilder().build(_unit()),
                failures=[VerificationFailure(validator="syntax",
                                              message="bad")],
                attempt=1,
            )
        except RuntimeError:
            pass
        assert active_profile() is base_before
    finally:
        _teardown_profile()

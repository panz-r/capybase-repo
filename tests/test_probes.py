"""Tests for the calibration probes — no network, all via a fake client.

The fake client controls ``finish_reason`` and parseability as a function of
``max_tokens``/``json_mode`` so we can drive the binary search, capability
detection, and end-to-end paths deterministically.
"""

from __future__ import annotations

import pytest

from capybase.adapters.llm_openai import LLMResponse
from capybase.config import ModelConfig
from capybase.probes import (
    _DEFAULT_MAX_TOKENS,
    _LATENCY_HEADROOM,
    _MAX_TOKENS_CEIL,
    _MAX_TOKENS_TIMEOUT_SCALE,
    _MIN_GEN_TIMEOUT,
    _apply_max_tokens_headroom,
    _gen_timeout_from_latency,
    probe_context_window,
    probe_end_to_end,
    probe_json_mode,
    probe_logprobs,
    probe_max_tokens,
    probe_reachability,
    run_calibration,
)

_VALID = '{"resolved_text": "x = 3", "needs_human": false}'


def _resp(text: str, finish: str = "stop", entropy: float | None = None) -> LLMResponse:
    return LLMResponse(
        text=text,
        raw={"_accumulated": {"finish_reason": finish}},
        mean_token_entropy=entropy,
    )


class CalibClient:
    """A fake LLMClient scripted by call-arity rather than call-order.

    Behavior is decided from the incoming kwargs (``max_tokens``/``json_mode``),
    which is how the real probes vary their calls. Records every call so tests
    can assert which probes fired and with what parameters.
    """

    def __init__(
        self,
        *,
        truncate_below: int = 0,        # finish_reason="length" when max_tokens < this
        text: str = _VALID,
        finish: str = "stop",
        entropy: float | None = None,
        reject_json_mode: bool = False,  # raise when json_mode=True
        reachable: bool = True,
    ) -> None:
        self.truncate_below = truncate_below
        self.text = text
        self.finish = finish
        self.entropy = entropy
        self.reject_json_mode = reject_json_mode
        self.reachable = reachable
        self.calls: list[dict] = []

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        self.calls.append(
            {"max_tokens": max_tokens, "json_mode": json_mode, "temperature": temperature}
        )
        if not self.reachable:
            raise RuntimeError("server down")
        if json_mode and self.reject_json_mode:
            raise RuntimeError("400 response_format unsupported")
        # entropy is a server capability orthogonal to finish_reason: a server
        # that returns logprobs does so on truncated and complete responses alike.
        if max_tokens < self.truncate_below:
            return _resp(self.text, finish="length", entropy=self.entropy)
        return _resp(self.text, finish=self.finish, entropy=self.entropy)


def _cfg(**over) -> ModelConfig:
    return ModelConfig(model="vibethink", **over)


# ---------------------------------------------------------------------------
# reachability
# ---------------------------------------------------------------------------


def test_reachability_ok():
    r = probe_reachability(CalibClient(), _cfg())
    assert r.ok and r.name == "reachability"


def test_reachability_reports_failure_without_raising():
    r = probe_reachability(CalibClient(reachable=False), _cfg())
    assert not r.ok
    assert "request failed" in r.detail


def test_reachability_empty_response_is_failure():
    client = CalibClient(text="   ")
    r = probe_reachability(client, _cfg())
    assert not r.ok


# ---------------------------------------------------------------------------
# max_tokens binary search
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "first_success, expected",
    [
        (1024, 2048),    # 1.5x = 1536 -> snap up to 2048
        (2048, 4096),    # 1.5x = 3072 -> snap up to 4096
        (8192, 16384),   # 1.5x = 12288 -> snap up to 16384
        (16384, 32768),  # 1.5x = 24576 -> snap up to 32768
        (32768, 32768),  # already at ceiling -> stays (1.5x capped)
        (100000, 32768), # way over ceiling -> capped
    ],
)
def test_apply_max_tokens_headroom_snaps_up(first_success, expected):
    """Headroom accounts for reasoning-chain variance: the budget that fit once
    must accommodate a longer chain next time, so we multiply and snap up."""
    assert _apply_max_tokens_headroom(first_success) == expected
    assert _apply_max_tokens_headroom(first_success) <= _MAX_TOKENS_CEIL


def test_max_tokens_returns_smallest_sufficient_budget():
    # Truncates below 8192 → first success at 8192. Headroom (1.5x = 12288) snaps
    # UP to the next ladder rung, 16384, so the stored budget has room for a
    # longer-than-average <think> chain on a reasoning model.
    client = CalibClient(truncate_below=8192)
    result, budget, latencies, first_success = probe_max_tokens(client, _cfg())
    assert result.ok
    assert budget == 16384  # 8192 first success -> 12288 target -> snap up to 16384
    assert first_success == 8192  # the rung at which latency was measured
    assert len(latencies) == 1  # latency recorded only on the successful rung
    # Tried every rung at and below the first-success 8192.
    tried = [c["max_tokens"] for c in client.calls]
    assert tried == [1024, 2048, 4096, 8192]


def test_max_tokens_succeeds_at_first_rung_when_no_truncation():
    # First success at 1024. Headroom 1.5x = 1536 → snap up to 2048.
    client = CalibClient()
    result, budget, _, first_success = probe_max_tokens(client, _cfg())
    assert result.ok and budget == 2048
    assert first_success == 1024


def test_max_tokens_falls_back_to_default_when_never_parses():
    # Garbage output never parses → no rung succeeds.
    client = CalibClient(text="not json at all")
    result, budget, _, first_success = probe_max_tokens(client, _cfg())
    assert not result.ok
    assert budget == _DEFAULT_MAX_TOKENS
    assert first_success == 0  # nothing succeeded → no probe budget


def test_max_tokens_falls_back_when_all_rungs_truncate():
    # A huge truncate threshold (beyond the ladder) → every rung truncates.
    client = CalibClient(truncate_below=10**9)
    result, budget, _, first_success = probe_max_tokens(client, _cfg())
    assert not result.ok
    assert budget == _DEFAULT_MAX_TOKENS


# ---------------------------------------------------------------------------
# json_mode / logprobs capability detection
# ---------------------------------------------------------------------------


def test_json_mode_supported():
    r = probe_json_mode(CalibClient(), _cfg())
    assert r.ok and "honors" in r.detail


def test_json_mode_rejected_by_server_disables_it():
    client = CalibClient(reject_json_mode=True)
    r = probe_json_mode(client, _cfg())
    assert not r.ok
    assert "rejected" in r.detail


def test_json_mode_unparseable_output_disables_it():
    client = CalibClient(text="not json")
    r = probe_json_mode(client, _cfg())
    assert not r.ok
    assert "unparseable" in r.detail


def test_logprobs_detected_when_entropy_present():
    r = probe_logprobs(CalibClient(entropy=1.234), _cfg())
    assert r.ok and "1.234" in r.detail


def test_logprobs_absent_when_no_entropy():
    r = probe_logprobs(CalibClient(), _cfg())
    assert not r.ok


# ---------------------------------------------------------------------------
# context window discovery (/v1/models GET)
# ---------------------------------------------------------------------------


def _models_resp(body: bytes):
    from unittest.mock import MagicMock
    resp = MagicMock()
    resp.__enter__ = lambda self: self
    resp.__exit__ = MagicMock(return_value=False)
    resp.read.return_value = body
    return resp


def test_context_window_discovered_from_models_endpoint():
    from unittest.mock import patch
    body = b'{"data":[{"id":"vibethink","context_length":32768},{"id":"other","context_length":4096}]}'
    with patch("urllib.request.urlopen", return_value=_models_resp(body)):
        r, window = probe_context_window(_cfg())
    assert r.ok and window == 32768


def test_context_window_accepts_alias_field_names():
    from unittest.mock import patch
    for field in ("context_length", "max_context_length", "context_window"):
        body = f'{{"data":[{{"id":"vibethink","{field}":16384}}]}}'.encode()
        with patch("urllib.request.urlopen", return_value=_models_resp(body)):
            r, window = probe_context_window(_cfg())
        assert r.ok and window == 16384, (field, window)


def test_context_window_model_not_listed_returns_zero():
    from unittest.mock import patch
    body = b'{"data":[{"id":"other","context_length":4096}]}'
    with patch("urllib.request.urlopen", return_value=_models_resp(body)):
        r, window = probe_context_window(_cfg())
    assert not r.ok and window == 0
    assert "not found" in r.detail


def test_context_window_endpoint_error_returns_zero():
    from unittest.mock import patch
    with patch("urllib.request.urlopen", side_effect=ConnectionError("boom")):
        r, window = probe_context_window(_cfg())
    assert not r.ok and window == 0


def test_context_window_no_data_list_returns_zero():
    from unittest.mock import patch
    body = b'{"data":[]}'
    with patch("urllib.request.urlopen", return_value=_models_resp(body)):
        r, window = probe_context_window(_cfg())
    assert not r.ok and window == 0


# ---------------------------------------------------------------------------
# end-to-end
# ---------------------------------------------------------------------------


def test_end_to_end_ok_with_valid_candidate():
    r = probe_end_to_end(CalibClient(), _cfg())
    assert r.ok


def test_end_to_end_truncated_is_failure():
    # max_tokens is the tuned/default 8192 here; force truncation on every call.
    r = probe_end_to_end(CalibClient(truncate_below=10**9), _cfg())
    assert not r.ok and "truncated" in r.detail


def test_end_to_end_unparseable_is_failure():
    r = probe_end_to_end(CalibClient(text="garbage"), _cfg())
    assert not r.ok and "resolved_text" in r.detail


# ---------------------------------------------------------------------------
# run_calibration orchestration
# ---------------------------------------------------------------------------


def test_run_calibration_assembles_profile_for_healthy_server():
    client = CalibClient(truncate_below=8192, entropy=0.5)
    report = run_calibration(client, _cfg())
    assert report.ok
    p = report.profile
    assert p.model == "vibethink"
    assert p.max_tokens == 16384         # 8192 first success -> 1.5x -> snap up to 16384
    assert p.json_mode is True           # supported
    assert p.capture_token_entropy is True
    assert p.generation_timeout_seconds >= _MIN_GEN_TIMEOUT
    # All nine probes ran (context_window discovery + embeddings capability +
    # The two-phase designed calibration: screening + refinement, plus the
    # derived mechanism/prompt-profile summary rows. context_window is 0 because
    # the test's fake server doesn't serve /v1/models.
    assert [r.name for r in report.results] == [
        "reachability",
        "max_tokens",
        "context_window",
        "json_mode",
        "logprobs",
        "embeddings",
        "end_to_end",
        "two_phase",
        "mechanisms",
        "prompt_profile",
    ]
    assert p.context_window == 0  # /v1/models not served in the fake harness
    assert p.enable_embedding_rag is False  # CalibClient doesn't serve embeddings


def test_run_calibration_disables_json_mode_when_rejected():
    client = CalibClient(reject_json_mode=True, truncate_below=4096, entropy=0.5)
    report = run_calibration(client, _cfg())
    # max_tokens still tuned (probed without json_mode at that rung), profile ok.
    assert report.ok
    assert report.profile.json_mode is False
    assert any("json_mode disabled" in n for n in report.profile.notes)


def test_run_calibration_unreachable_returns_defaults_and_not_ok():
    client = CalibClient(reachable=False)
    report = run_calibration(client, _cfg())
    assert not report.ok
    # Only reachability ran.
    assert [r.name for r in report.results] == ["reachability"]
    assert report.profile.max_tokens == _DEFAULT_MAX_TOKENS
    assert report.profile.json_mode is True  # conservative default
    assert any("unreachable" in n for n in report.profile.notes)


def test_run_calibration_generation_timeout_respects_floor():
    # Impossibly fast (0 latency) → timeout should still hit the floor.
    report = run_calibration(CalibClient(), _cfg())
    assert report.profile.generation_timeout_seconds == _MIN_GEN_TIMEOUT


# ---------------------------------------------------------------------------
# generation-timeout derivation: latency × headroom × max_tokens-scaling
# ---------------------------------------------------------------------------


def test_gen_timeout_no_latencies_returns_default():
    from capybase.probes import _DEFAULT_GEN_TIMEOUT
    assert _gen_timeout_from_latency([]) == _DEFAULT_GEN_TIMEOUT


def test_gen_timeout_respects_floor_even_for_fast_model():
    # A 5s-average model: 5 × 3 headroom = 15s, floored at _MIN_GEN_TIMEOUT.
    t = _gen_timeout_from_latency([5000.0])
    assert t == _MIN_GEN_TIMEOUT


def test_gen_timeout_scales_by_max_tokens_ratio():
    # Probe measured 20s latency at a 2048-token budget; real budget is 16384.
    # Base: 20 × 3 headroom = 60s. Scale: 16384/2048 = 8× → 480s (×8 = the cap).
    t = _gen_timeout_from_latency([20000.0], max_tokens=16384, probed_budget=2048)
    assert t == 480, t
    # Above the floor, so the floor doesn't clamp it.


def test_gen_timeout_scaling_capped():
    # Pathological: real budget 1000000, probed 1024 → ratio ~977, capped at 8×.
    # Base: 30 × 3 = 90s. × 8 cap = 720s.
    t = _gen_timeout_from_latency([30000.0], max_tokens=1_000_000, probed_budget=1024)
    assert t == 720, t


def test_gen_timeout_no_scaling_when_budgets_equal():
    # max_tokens == probed_budget → ratio 1 → no scaling, just headroom × floor.
    # 40s × 3 = 120s, floored at 180.
    t = _gen_timeout_from_latency([40000.0], max_tokens=2048, probed_budget=2048)
    assert t == _MIN_GEN_TIMEOUT


def test_gen_timeout_scaling_matches_real_rebase_scenario():
    # The bug: 30s probe latency, calibrated max_tokens 16384, probed at 2048.
    # Old derivation: 30 × 2 = 60s (floored 60) → killed real 4146-token conflict.
    # New: 30 × 3 = 90s base × (16384/2048 = 8, capped) = 720s. Real conflict
    # now gets ample time. Floor (180) is well below, so the scaled value wins.
    t = _gen_timeout_from_latency([30000.0], max_tokens=16384, probed_budget=2048)
    assert t == 720, t  # the real-rebase scenario now gets 720s, not 60s


def test_run_calibration_uses_tuned_budget_for_capability_probes():
    # Truncates below 16384 → first success 16384 → headroom snaps up to 32768;
    # the json_mode probe must use that tuned budget, not the original default.
    client = CalibClient(truncate_below=16384)
    run_calibration(client, _cfg())
    jm_calls = [c for c in client.calls if c["json_mode"]]
    assert jm_calls, "expected at least one json_mode=True probe call"
    assert any(c["max_tokens"] == 32768 for c in jm_calls)


# ---------------------------------------------------------------------------
# probe_mechanisms — empirical A/B selection on the blessed corpus
# ---------------------------------------------------------------------------


def _json_text(resolved: str) -> str:
    """Wrap a resolved_text in the candidate-JSON envelope the parser expects."""
    import json as _json
    return _json.dumps({"resolved_text": resolved, "needs_human": False})


class CorpusAwareClient:
    """Fake LLMClient that resolves corpus conflicts by matching the prompt.

    Inspects the user message for each known conflict's *side text* and returns
    the blessed merge (so it always scores correct). Used to exercise the
    mechanism A/B machinery: by subclassing and overriding resolution quality per
    mechanism, tests can drive which mechanisms get enabled.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def _resolve_for(self, messages) -> str:
        from capybase.calibration_corpus import CALIBRATION_CONFLICTS

        user = next((m["content"] for m in messages if m["role"] == "user"), "")
        for c in CALIBRATION_CONFLICTS:
            # Match on a distinctive slice of the conflict's replayed side.
            if c.unit.replayed.text[:20] in user:
                return _json_text(c.expected_text)
        return _json_text("WRONG")  # unmatched prompt → wrong answer

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        self.calls.append({"temperature": temperature, "max_tokens": max_tokens})
        return LLMResponse(
            text=self._resolve_for(messages),
            raw={"_accumulated": {"finish_reason": "stop"}},
        )


def test_probe_mechanisms_correct_baseline_leaves_all_off():
    """When the model already resolves everything correctly at samples=1,
    multi-sampling and mechanisms don't strictly improve → all stay off."""
    from capybase.probes import probe_mechanisms

    client = CorpusAwareClient()  # always correct, regardless of mechanism
    base = ModelConfig(model="vibethink")
    result, choices = probe_mechanisms(client, base, base_cfg=base)
    # Baseline is already perfect; no mechanism beats it strictly.
    assert choices.samples == 1
    assert not choices.two_pass
    assert not choices.enable_self_consistency


class SingleSampleWrongClient(CorpusAwareClient):
    """Returns a WRONG answer at low temperature (single-sample path) but the
    CORRECT blessed merge at high temperature (the multi-sample/exploratory
    path). Drives the decision to enable multi-sampling."""

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        self.calls.append({"temperature": temperature})
        from capybase.calibration_corpus import CALIBRATION_CONFLICTS

        user = next((m["content"] for m in messages if m["role"] == "user"), "")
        # High temperature (the diverse/sampling pass) → correct; low → wrong.
        correct = temperature is not None and temperature >= 0.6
        for c in CALIBRATION_CONFLICTS:
            if c.unit.replayed.text[:20] in user:
                return LLMResponse(
                    text=_json_text(c.expected_text if correct else "WRONG"),
                    raw={"_accumulated": {"finish_reason": "stop"}},
                )
        return LLMResponse(text=_json_text("WRONG"),
                           raw={"_accumulated": {"finish_reason": "stop"}})


def test_probe_mechanisms_enables_multi_sampling_when_it_helps():
    """When multi-sampling resolves the corpus correctly but single-sample
    fails, the samples count must be raised above 1. (Which exact sub-mechanism
    flips on is engine-temperature-dependent and not asserted here — the core
    invariant is that the probe detects multi-sampling helps and raises N.)"""
    from capybase.probes import probe_mechanisms

    client = SingleSampleWrongClient()
    base = ModelConfig(model="vibethink")
    result, choices = probe_mechanisms(client, base, base_cfg=base)
    assert choices.samples > 1, f"expected multi-sampling enabled, got {result.detail}"
    assert "beats 1" in result.detail


def test_probe_mechanisms_degrades_gracefully_on_eval_error():
    """If the mechanism eval itself raises, probe_mechanisms must not abort — it
    returns all-off choices and an ok=False result."""
    from capybase.probes import probe_mechanisms

    class BoomClient(CorpusAwareClient):
        def complete(self, *a, **k):
            raise RuntimeError("eval exploded")

    client = BoomClient()
    base = ModelConfig(model="vibethink")
    result, choices = probe_mechanisms(client, base, base_cfg=base)
    assert not result.ok
    assert choices.samples == 1
    assert "off" in result.detail.lower() or "failed" in result.detail.lower()


# ---------------------------------------------------------------------------
# Min-corpus gate: below the floor, probe_mechanisms refuses to A/B-select and
# leaves all mechanisms off (a too-small corpus can't support a confident
# one-case correctness difference). Regression guard: as the corpus grows past
# the floor, selection re-enables automatically.
# ---------------------------------------------------------------------------


def test_probe_mechanisms_refuses_to_select_below_min_corpus(monkeypatch):
    """With the corpus shrunk below _MIN_CORPUS_FOR_MECHANISM_SELECTION, the
    probe must leave all mechanisms off and report the refusal — never guess."""
    from capybase import probes
    from capybase import calibration_corpus
    from capybase.probes import probe_mechanisms

    # Shrink the corpus the probe reads below the floor (keep the first 3).
    small = calibration_corpus.CALIBRATION_CONFLICTS[:3]
    monkeypatch.setattr(calibration_corpus, "CALIBRATION_CONFLICTS", small)
    monkeypatch.setattr(probes, "_MIN_CORPUS_FOR_MECHANISM_SELECTION", 15)

    client = CorpusAwareClient()
    base = ModelConfig(model="vibethink")
    result, choices = probe_mechanisms(client, base, base_cfg=base)
    assert choices.samples == 1
    assert not choices.two_pass
    assert not choices.enable_self_consistency
    assert "too small" in result.detail


def test_probe_mechanisms_selects_at_or_above_min_corpus():
    """At the floor the gate passes and the probe runs its normal A/B (here the
    always-correct client leaves everything off — selection ran, just found no
    improvement)."""
    from capybase.probes import _MIN_CORPUS_FOR_MECHANISM_SELECTION, probe_mechanisms
    from capybase.calibration_corpus import CALIBRATION_CONFLICTS

    # The shipped corpus must be at/above the floor so selection is active.
    assert len(CALIBRATION_CONFLICTS) >= _MIN_CORPUS_FOR_MECHANISM_SELECTION
    client = CorpusAwareClient()
    base = ModelConfig(model="vibethink")
    result, choices = probe_mechanisms(client, base, base_cfg=base)
    # The gate did NOT fire (no "too small" refusal).
    assert "too small" not in result.detail


# ---------------------------------------------------------------------------
# probe_prompt_profile — empirical A/B of the prompt-rendering profile
# ---------------------------------------------------------------------------


class _LayoutSensitiveClient(CorpusAwareClient):
    """Returns the CORRECT merge when the active profile is markdown_code, WRONG
    otherwise. Simulates a model that escapes code reliably only under the raw-
    fenced-block layout (the 3B failure mode the layout was built for)."""

    def complete(self, messages, *, model, temperature, max_tokens, json_mode):
        import capybase.prompt_profile as pp
        from capybase.calibration_corpus import CALIBRATION_CONFLICTS

        user = next((m["content"] for m in messages if m["role"] == "user"), "")
        correct = pp.active_profile().output_layout.value == "markdown_code"
        for c in CALIBRATION_CONFLICTS:
            if c.unit.replayed.text[:20] in user:
                return LLMResponse(
                    text=_json_text(c.expected_text if correct else "WRONG"),
                    raw={"_accumulated": {"finish_reason": "stop"}},
                )
        return LLMResponse(
            text=_json_text("WRONG"),
            raw={"_accumulated": {"finish_reason": "stop"}},
        )


def test_probe_prompt_profile_selects_markdown_when_it_scores_higher():
    """When markdown_code strictly beats the default on the corpus, it's kept."""
    from capybase.probes import probe_prompt_profile
    import capybase.prompt_profile as pp

    pp.set_active_profile(None)
    client = _LayoutSensitiveClient()  # correct only under markdown_code
    base = ModelConfig(model="vibethink")
    result, winner = probe_prompt_profile(client, base, base_cfg=base)
    assert winner.output_layout is pp.OutputLayout.MARKDOWN_CODE
    assert result.ok is True
    pp.set_active_profile(None)


def test_probe_prompt_profile_returns_default_when_no_improvement():
    """When markdown_code doesn't improve (both correct, or both wrong), default wins."""
    from capybase.probes import probe_prompt_profile
    import capybase.prompt_profile as pp

    pp.set_active_profile(None)
    client = CorpusAwareClient()  # always correct regardless of layout
    base = ModelConfig(model="vibethink")
    result, winner = probe_prompt_profile(client, base, base_cfg=base)
    assert winner == pp.DEFAULT_PROFILE
    assert result.ok is False  # no layout beat the default
    pp.set_active_profile(None)


def test_probe_prompt_profile_preserves_existing_on_small_corpus(monkeypatch):
    """Below the min-corpus floor, the probe refuses and returns `existing`."""
    from capybase import probes
    from capybase.probes import probe_prompt_profile
    import capybase.prompt_profile as pp

    monkeypatch.setattr(probes, "_MIN_CORPUS_FOR_MECHANISM_SELECTION", 10**9)
    existing = pp.PromptProfile(output_layout=pp.OutputLayout.MARKDOWN_CODE)
    client = CorpusAwareClient()
    base = ModelConfig(model="vibethink")
    result, winner = probe_prompt_profile(
        client, base, base_cfg=base, existing=existing)
    assert winner is existing  # preserved, not clobbered by the default
    assert "too small" in result.detail


def test_probe_prompt_profile_baseline_error_returns_default(monkeypatch):
    """When the baseline eval raises, the probe degrades to the default."""
    from capybase.probes import probe_prompt_profile
    import capybase.prompt_profile as pp

    pp.set_active_profile(None)

    def _boom(*a, **kw):
        raise RuntimeError("eval exploded")

    # Patch the eval primitive to raise.
    import capybase.probes as _probes_mod
    monkeypatch.setattr(_probes_mod, "_evaluate_mechanism_setting", _boom)
    client = CorpusAwareClient()
    base = ModelConfig(model="vibethink")
    result, winner = probe_prompt_profile(client, base, base_cfg=base)
    assert winner == pp.DEFAULT_PROFILE
    assert "baseline eval failed" in result.detail
    pp.set_active_profile(None)


def test_run_calibration_writes_prompt_section():
    """The assembled ModelProfile carries the two-phase probe's winning prompt profile."""
    from capybase.probes import run_calibration
    import capybase.prompt_profile as pp

    pp.set_active_profile(None)
    client = _LayoutSensitiveClient()  # markdown_code is the dominant factor
    # The two-phase probe unifies mechanism + prompt selection; running it
    # (the default) discovers markdown_code via the screening design.
    report = run_calibration(
        client, ModelConfig(model="vibethink"),
    )
    assert report.profile.prompt.profile.output_layout is pp.OutputLayout.MARKDOWN_CODE
    pp.set_active_profile(None)


# ---------------------------------------------------------------------------
# Calibration harness correctness (bugs found via the E4B calibrate run)
# ---------------------------------------------------------------------------


def test_resolve_under_config_self_consistency_unpacks_tuple():
    """propose_with_consensus returns (candidates, report); the harness must
    unpack it, not treat it as a bare list. Regression: the self-consistency
    A/B errored on every prior calibrate (AttributeError: 'list' object has no
    attribute 'resolved_text')."""
    from capybase.probes import _resolve_under_config
    from capybase.calibration_corpus import CALIBRATION_CONFLICTS
    from capybase.context_builder import ContextBuilder

    class _Stub:
        def complete(self, messages, **kw):
            return LLMResponse(
                text=_json_text("merged"),
                raw={"_accumulated": {"finish_reason": "stop"}},
            )

    cfg = ModelConfig(model="m", samples=3, enable_self_consistency=True)
    c = CALIBRATION_CONFLICTS[0]
    ctx = ContextBuilder().build(c.unit)
    winner, latency = _resolve_under_config(_Stub(), cfg, c, ctx)
    assert winner is not None
    assert winner.resolved_text == "merged"  # not an AttributeError


def test_evaluate_setting_replicated_majority_votes():
    """A noisy model that resolves a conflict correctly 2/3 reps → majority
    correct. This is the noise-robustness fix for thinking models whose
    per-call success is a coin-flip."""
    from capybase.quality import evaluate_setting_replicated
    from capybase.calibration_corpus import CALIBRATION_CONFLICTS
    from capybase.context_builder import ContextBuilder
    from capybase.conflict_model import CandidateResolution, VerificationResult

    # Per-conflict call counter so the alternating pattern is reset for each
    # conflict (rep 1,3 correct; rep 2 wrong → 2/3 majority correct).
    call_counts: dict[str, int] = {}

    class _FlakyClient:
        def complete(self, messages, **kw):
            from capybase.calibration_corpus import CALIBRATION_CONFLICTS as CC
            user = next((m["content"] for m in messages if m["role"] == "user"), "")
            for c in CC:
                if c.unit.replayed.text[:20] in user:
                    key = c.unit.unit_id
                    call_counts[key] = call_counts.get(key, 0) + 1
                    # Reps 1,3 → correct (odd); rep 2 → wrong (even).
                    correct = (call_counts[key] % 2 == 1)
                    return LLMResponse(
                        text=_json_text(c.expected_text if correct else "WRONG"),
                        raw={"_accumulated": {"finish_reason": "stop"}},
                    )
            return LLMResponse(text=_json_text("WRONG"), raw={"_accumulated": {"finish_reason": "stop"}})

    def resolve_one(conflict, context, cfg):
        from capybase.probes import _resolve_under_config
        winner, lat = _resolve_under_config(client, cfg, conflict, context)
        if winner is None:
            raise RuntimeError("no candidate")
        return winner, None, lat

    client = _FlakyClient()
    cfg = ModelConfig(model="m")
    triple = evaluate_setting_replicated(resolve_one, cfg, n_reps=3)
    assert triple.reps == 3
    assert len(triple.per_conflict_agreement) == triple.total
    # Every conflict was correct in the majority (2/3 = 0.67 > 0.5).
    assert all(a >= 0.5 for a in triple.per_conflict_agreement), triple.per_conflict_agreement
    # And the majority-vote n_correct reflects all-correct (each conflict 2/3).
    assert triple.n_correct == triple.total


def test_evaluate_setting_replicated_single_rep_matches_evaluate_setting():
    """n_reps=1 is byte-identical to evaluate_setting (the default for stable models)."""
    from capybase.quality import evaluate_setting, evaluate_setting_replicated

    def resolve_one(conflict, context, cfg):
        cand = CandidateResolution(
            candidate_id="c", unit_id=conflict.unit.unit_id, model_name="m",
            prompt_version="x", resolved_text=conflict.expected_text,
        )
        return cand, None, 100.0

    cfg = ModelConfig(model="m")
    a = evaluate_setting(resolve_one, cfg)
    b = evaluate_setting_replicated(resolve_one, cfg, n_reps=1)
    assert a.n_correct == b.n_correct
    assert a.proxy_sum == b.proxy_sum
    assert b.reps == 1


def test_two_phase_evals_under_complete_configs():
    """Each two-phase design point is a COMPLETE config (mechanism + prompt axes
    together), so the layout comparison always runs under the point's own
    mechanism settings — the regression the old test guarded against is now
    structurally guaranteed by the unified design. This pins it by asserting a
    design point carrying samples=3 actually resolves at samples=3."""
    from capybase.probes import _two_phase_factors, _apply_design_point
    from capybase.calibration_design import DesignPoint

    cfg = ModelConfig(model="m", samples=1)
    # A design point that sets samples=3 (the high level).
    point = DesignPoint(config_id="test", levels={"samples": 3})
    applied_cfg, _ = _apply_design_point(cfg, point)
    assert applied_cfg.samples == 3, "design point's samples must reach the config"


# ---------------------------------------------------------------------------
# probe_two_phase — screening design + focused refinement
# ---------------------------------------------------------------------------


def test_probe_two_phase_finds_dominant_layout_factor():
    """When output_layout is the ONLY factor that drives correctness, Phase 1
    ranks it #1 and Phase 2 selects the markdown_code layout."""
    from capybase.probes import probe_two_phase
    import capybase.prompt_profile as pp

    pp.set_active_profile(None)
    client = _LayoutSensitiveClient()  # correct only under markdown_code
    cfg = ModelConfig(model="vibethink")
    result, choices, profile = probe_two_phase(client, cfg)
    # The dominant factor should be output_layout, and it should win.
    assert profile.output_layout is pp.OutputLayout.MARKDOWN_CODE, (
        f"expected markdown_code, got {profile.output_layout}"
    )
    assert "Phase 1 ranking" in result.detail
    assert result.ok  # a non-default profile was selected
    pp.set_active_profile(None)


def test_probe_two_phase_keeps_default_when_nothing_helps():
    """When the model is correct regardless of layout (CorpusAwareClient), no
    factor is significant → Phase 2 keeps the default, samples=1."""
    from capybase.probes import probe_two_phase
    import capybase.prompt_profile as pp

    pp.set_active_profile(None)
    client = CorpusAwareClient()  # always correct
    cfg = ModelConfig(model="vibethink")
    result, choices, profile = probe_two_phase(client, cfg)
    # Nothing beats the perfect baseline → default profile, samples=1.
    assert profile == pp.DEFAULT_PROFILE
    assert choices.samples == 1
    pp.set_active_profile(None)


def test_probe_two_phase_small_corpus_preserves_existing(monkeypatch):
    """Below the min-corpus floor, the probe refuses and returns existing."""
    from capybase import probes
    from capybase.probes import probe_two_phase, MechanismChoices
    import capybase.prompt_profile as pp

    monkeypatch.setattr(probes, "_MIN_CORPUS_FOR_MECHANISM_SELECTION", 10 ** 9)
    pp.set_active_profile(None)
    existing_choices = MechanismChoices(samples=3, diverse_sampling=True)
    existing_profile = pp.PromptProfile(output_layout=pp.OutputLayout.MARKDOWN_CODE)
    client = CorpusAwareClient()
    cfg = ModelConfig(model="vibethink")
    result, choices, profile = probe_two_phase(
        client, cfg,
        existing_choices=existing_choices, existing_profile=existing_profile,
    )
    assert choices is existing_choices  # preserved
    assert profile is existing_profile  # preserved
    assert "too small" in result.detail
    pp.set_active_profile(None)


def test_probe_two_phase_phase1_only_reports_ranking():
    """run_phase2=False runs only the screening and reports the factor ranking
    without committing to a Phase-2 selection (keeps existing config)."""
    from capybase.probes import probe_two_phase
    import capybase.prompt_profile as pp

    pp.set_active_profile(None)
    client = _LayoutSensitiveClient()
    cfg = ModelConfig(model="vibethink")
    result, choices, profile = probe_two_phase(client, cfg, run_phase2=False)
    assert "Phase 1 ranking" in result.detail
    # Phase 2 didn't run → default config retained.
    assert choices.samples == 1
    pp.set_active_profile(None)

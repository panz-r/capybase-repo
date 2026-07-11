"""Calibration probes: auto-discover a model's runtime settings.

``capybase calibrate`` calls :func:`run_calibration`, which probes the live
model endpoint through the existing ``LLMClient`` seam and returns a
:class:`~capybase.calibration_profile.ModelProfile`. The probes are pure
functions of an injectable client, so the whole suite is unit-testable with a
fake client and no network (see ``tests/test_probes.py``).

What each probe tunes, and why it's model-dependent:

- **max_tokens** — reasoning models emit long ``<think>`` chains before the
  final JSON answer. Too low → ``finish_reason == "length"`` → truncated output
  → empty resolution → escalation. :func:`probe_max_tokens` walks a candidate
  ladder from small to large and stops at the first value that finishes AND
  parses to a valid candidate dict. This reuses the truncation signal the
  resolution engine already keys on (``failure_kind == "truncated"``).
- **json_mode** — ``response_format: {type: json_object}`` is sent on every
  completion, but some local servers reject it. :func:`probe_json_mode` checks
  whether a json-mode call succeeds; when not, the profile disables it and
  resolution falls back to the fenced-JSON parser.
- **capture_token_entropy** — logprobs aren't returned by every server.
  :func:`probe_logprobs` requests them once and reports whether the server
  honored the request.
- **generation_timeout_seconds** — derived from observed generation latencies
  during the max_tokens probe, with a headroom multiplier and a sane floor.

All probes degrade gracefully: a probe that can't get a usable signal returns
its conservative default rather than raising, so ``calibrate`` always produces
a profile (and the CLI reports which knobs it could not tune).
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import capybase
from capybase.adapters.llm_openai import LLMResponse, coerce_candidate_dict
from capybase.calibration_profile import ModelProfile
from capybase.config import ModelConfig

# Candidate max_tokens ladder, ascending. We probe small→large so the result is
# the smallest budget that yields a complete, parseable answer (no over-provision).
# 32768 is the typical ceiling for small local reasoning models.
_MAX_TOKENS_LADDER: tuple[int, ...] = (1024, 2048, 4096, 8192, 16384, 32768)

# Default settings returned when a probe can't get a clean signal. These are
# conservative (current built-in defaults) so an untunable knob never makes
# resolution WORSE than the out-of-box behavior.
_DEFAULT_MAX_TOKENS = 8192
_DEFAULT_GEN_TIMEOUT = 180
_DEFAULT_JSON_MODE = True
_DEFAULT_LOGPROBS = False

# Latency→timeout headroom: observed mean latency multiplied by this, so a model
# averaging 60s/answer gets a 180s generation deadline (with the floor applied).
# 3.0 (raised from 2.0): reasoning models' <think> chain length varies
# call-to-call far more than 2× covers, and real conflicts are larger than the
# tiny calibration probe. 3× errs toward waiting longer — the safe direction
# (a too-short deadline kills real generations mid-stream and fails silently).
_LATENCY_HEADROOM = 3.0
# Never tune below this regardless of observed latency. 180s matches the
# built-in ModelConfig default (generation_timeout_seconds) and _DEFAULT_GEN_TIMEOUT,
# so calibration never produces a WORSE timeout than the out-of-box default. The
# old floor of 60s was below what any non-trivial real conflict needs on a
# reasoning model and caused real rebases to time out (the calibration probe is
# tiny; its measured latency underestimates real conflicts by 10-100×).
_MIN_GEN_TIMEOUT = 180
# Cap on the max_tokens-scaling multiplier so a huge output budget doesn't
# produce a multi-hour timeout. The timeout scales latency by
# (real_max_tokens / probed_budget), capped here (×8): a 30s probe calibrated to
# a large budget → 30s × 3 headroom × 8 = 720s max, floored at 180s — enough for
# any real conflict without making a stuck rebase take indefinitely.
_MAX_TOKENS_TIMEOUT_SCALE = 8

# max_tokens safety margin. The binary search finds the smallest budget at which
# ONE call completed. But reasoning models emit a <think> chain whose length
# VARIES call-to-call at temperature > 0 — so the budget that fit once can
# truncate on the next run (the marginal-budget problem). We multiply the first-
# success budget by this and snap UP to the nearest ladder rung, giving headroom
# for a longer-than-average chain so real resolutions don't spuriously truncate.
_MAX_TOKENS_HEADROOM = 1.5
# An absolute ceiling we never exceed, even after headroom — small local models
# rarely benefit past this and it caps request cost/latency.
_MAX_TOKENS_CEIL = 32768


@dataclass
class ProbeResult:
    """Outcome of a single probe, for the CLI's human-readable report."""

    name: str
    ok: bool
    detail: str = ""
    # Any latency observations (ms) gathered by probes that time the model.
    latencies_ms: list[float] = field(default_factory=list)


# ---------------------------------------------------------------------------
# finish_reason helper
# ---------------------------------------------------------------------------


def _finish_reason(resp: LLMResponse) -> str:
    """Extract finish_reason from an LLMResponse, handling both shapes the
    adapter produces (streaming ``raw["_accumulated"]["finish_reason"]`` and
    non-streaming ``raw["choices"][0]["finish_reason"]``)."""
    meta = resp.raw or {}
    acc = meta.get("_accumulated")
    if isinstance(acc, dict) and acc.get("finish_reason"):
        return str(acc["finish_reason"])
    choices = meta.get("choices") or []
    if choices and isinstance(choices[0], dict):
        return str(choices[0].get("finish_reason") or "")
    return ""


def _apply_max_tokens_headroom(first_success: int) -> int:
    """Add a safety margin to the first-success budget and snap to a rung.

    The binary search finds the smallest budget at which ONE call completed.
    Reasoning models emit a ``<think>`` chain whose length VARIES call-to-call
    (especially at temperature > 0), so the budget that fit once can truncate on
    the next run. We multiply by headroom, then snap UP to the nearest ladder
    rung (or just below the ceiling) so the stored value reliably accommodates a
    longer-than-average chain. Snapping to a rung keeps values "round"/familiar.
    """
    target = int(first_success * _MAX_TOKENS_HEADROOM)
    if target >= _MAX_TOKENS_CEIL:
        return _MAX_TOKENS_CEIL
    # Snap up to the nearest ladder rung >= target.
    for rung in _MAX_TOKENS_LADDER:
        if rung >= target:
            return rung
    return _MAX_TOKENS_CEIL


# ---------------------------------------------------------------------------
# Individual probes
# ---------------------------------------------------------------------------


def _resolve_probe_messages() -> list[dict[str, str]]:
    """Build the chat messages for a tiny synthetic conflict using the REAL
    resolve prompt (``build_resolve_prompt``).

    The real prompt explicitly demands the JSON contract (``resolved_text``,
    etc.), which is what makes a probe response parseable. A hand-rolled "think
    then answer" prompt does NOT — reasoning models happily emit prose and never
    produce JSON, which would make every probe spuriously fail. Using the real
    prompt means max_tokens/json_mode/end_to_end all measure the SAME
    parseability that real resolutions require. Shared by all three probes.
    """
    from capybase.conflict_model import ConflictSide, ConflictUnit, ContextBundle
    from capybase.resolution_engine import build_resolve_prompt

    def _side(label: str, text: str) -> ConflictSide:
        return ConflictSide(label=label, text=text)  # type: ignore[arg-type]

    unit = ConflictUnit(
        session_id="calibrate",
        step_index=0,
        path="probe.py",
        language="python",
        unit_id="probe-0",
        base=_side("BASE", "x = 1"),
        current=_side("CURRENT_UPSTREAM_SIDE", "x = 2"),
        replayed=_side("REPLAYED_COMMIT_SIDE", "x = 3"),
        original_worktree_text="x = 1",
    )
    prompt = build_resolve_prompt(unit, ContextBundle(primary_text=""))
    return [
        {"role": "system", "content": "You are a careful merge-resolution assistant."},
        {"role": "user", "content": prompt},
    ]


def probe_reachability(client: Any, model_cfg: ModelConfig) -> ProbeResult:
    """One tiny completion call to confirm the endpoint is up and the model name
    is accepted. Failure here means ``run_calibration`` should abort: nothing
    else can be probed if the server is unreachable."""
    messages = [
        {"role": "system", "content": "Reply with the single word: ok"},
        {"role": "user", "content": "ping"},
    ]
    try:
        resp = client.complete(
            messages,
            model=model_cfg.model,
            temperature=0.0,
            max_tokens=16,
            json_mode=False,
        )
    except Exception as exc:  # noqa: BLE001 - reachability reports, never raises
        return ProbeResult("reachability", ok=False, detail=f"request failed: {exc}")
    if not (resp.text or "").strip():
        return ProbeResult("reachability", ok=False, detail="empty response")
    return ProbeResult("reachability", ok=True, detail=f"replied: {(resp.text or '')[:40]!r}")


def probe_context_window(model_cfg: ModelConfig) -> tuple[ProbeResult, int]:
    """Discover the model's context window from the server's ``/v1/models`` list.

    llama-server (and other OpenAI-compatible servers) expose each model's
    ``context_length`` via ``GET /v1/models``. We find the entry whose ``id``
    matches ``model_cfg.model`` and read its size, accepting the common field
    aliases (``context_length``, ``max_context_length``, ``context_window``).

    Returns ``(ProbeResult, context_window_tokens)``. On any failure — endpoint
    missing, the field absent, the model not listed, a network error — returns
    ``(ok=False, 0)``. A 0 window means "unknown/disabled": the resolve prompt
    is sent unbounded (no trimming), the backward-compatible default. Never
    raises; mirrors :func:`probe_embeddings`'s report-don't-abort contract.

    Direct urllib GET (not a chat completion), so this doesn't consume a
    generation slot and is cheap enough to always run during calibration.
    """
    import json
    import urllib.request

    url = model_cfg.base_url.rstrip("/") + "/models"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {model_cfg.api_key}",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - report, never abort calibrate
        return ProbeResult(
            "context_window", ok=False,
            detail=f"/v1/models unreachable: {exc}",
        ), 0

    # The OpenAI shape is {"data": [{"id": ..., "context_length": N}, ...]}.
    # Some servers use a top-level list; tolerate both.
    models = raw.get("data") if isinstance(raw, dict) else raw
    if not isinstance(models, list):
        return ProbeResult(
            "context_window", ok=False,
            detail="/v1/models returned no model list",
        ), 0
    for entry in models:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") != model_cfg.model:
            continue
        # Accept the common aliases; servers are inconsistent here.
        for field in ("context_length", "max_context_length", "context_window"):
            val = entry.get(field)
            if isinstance(val, (int, float)) and val > 0:
                window = int(val)
                return ProbeResult(
                    "context_window", ok=True,
                    detail=f"{model_cfg.model!r} context_length={window} ({field})",
                ), window
        return ProbeResult(
            "context_window", ok=False,
            detail=f"{model_cfg.model!r} listed but no context_length field; set [model] context_window manually",
        ), 0
    return ProbeResult(
        "context_window", ok=False,
        detail=f"{model_cfg.model!r} not found in /v1/models; set [model] context_window manually",
    ), 0


def probe_max_tokens(client: Any, model_cfg: ModelConfig) -> tuple[ProbeResult, int, list[float], int]:
    """Walk the max_tokens ladder and return (result, best_max_tokens, latencies, first_success_budget).

    Success at a rung = ``finish_reason != "length"`` AND the output parses to a
    candidate dict. Stops at the first success (smallest sufficient budget).
    Records wall-clock latency for each successful rung. If no rung succeeds,
    falls back to the built-in default and marks the probe not-ok.

    Returns ``first_success_budget`` (the smallest rung that succeeded, BEFORE
    headroom) so the timeout derivation can scale latency by the ratio of the
    real ``best_max_tokens`` to the budget at which latency was actually
    measured. 0 when no rung succeeded (latency wasn't measured).
    """
    messages = _resolve_probe_messages()
    latencies: list[float] = []
    tried: list[int] = []
    for budget in _MAX_TOKENS_LADDER:
        tried.append(budget)
        t0 = time.monotonic()
        try:
            resp = client.complete(
                messages,
                model=model_cfg.model,
                temperature=model_cfg.temperature,
                max_tokens=budget,
                # Probe with json_mode OFF: max_tokens discovery must not be
                # gated on json_mode support (a server that rejects
                # response_format would otherwise make every rung "fail"). The
                # real resolve prompt still demands JSON, so the parseability
                # check (coerce_candidate_dict) is meaningful without it.
                json_mode=False,
            )
        except Exception as exc:  # noqa: BLE001 - try next rung
            continue
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        finish = _finish_reason(resp)
        if finish == "length":
            continue  # truncated: budget too small, climb the ladder
        # Parseability check: a non-truncated answer must yield a candidate dict.
        data, _warnings = coerce_candidate_dict(resp.text or "")
        if not data or "resolved_text" not in data:
            continue  # didn't produce capybase JSON at all
        latencies.append(elapsed_ms)
        # Apply a safety margin: one success at `budget` doesn't guarantee
        # budget fits every run (reasoning chains vary at temp > 0). Multiply up
        # and snap to the nearest ladder rung so real resolutions have headroom.
        first_success = budget
        recommended = _apply_max_tokens_headroom(first_success)
        return (
            ProbeResult(
                "max_tokens",
                ok=True,
                detail=(
                    f"smallest sufficient budget: {first_success}; "
                    f"with {int((_MAX_TOKENS_HEADROOM - 1) * 100)}% headroom -> "
                    f"{recommended} (tried {tried})"
                ),
                latencies_ms=latencies,
            ),
            recommended,
            latencies,
            first_success,
        )
    return (
        ProbeResult(
            "max_tokens",
            ok=False,
            detail=f"no rung produced a complete parseable answer (tried {tried}); "
            f"falling back to {_DEFAULT_MAX_TOKENS}",
            latencies_ms=latencies,
        ),
        _DEFAULT_MAX_TOKENS,
        latencies,
        0,
    )


def probe_json_mode(client: Any, model_cfg: ModelConfig) -> ProbeResult:
    """Detect whether the server accepts ``response_format: json_object`` and the
    model still emits parseable JSON under it. A 4xx/exception or unparseable
    output means json_mode should be disabled (resolution then relies on the
    fenced-JSON parser).

    Uses the REAL resolve prompt (which demands JSON) so parseability is judged
    against the same bar as a real resolution — not a hand-rolled prompt where a
    reasoning model might emit prose. Uses the full tuned budget so a long
    ``<think>`` chain isn't truncated before the JSON answer."""
    messages = _resolve_probe_messages()
    try:
        resp = client.complete(
            messages,
            model=model_cfg.model,
            temperature=model_cfg.temperature,
            max_tokens=model_cfg.max_tokens,
            json_mode=True,
        )
    except Exception as exc:  # noqa: BLE001 - server rejects response_format
        return ProbeResult("json_mode", ok=False, detail=f"json_mode rejected: {exc}")
    data, _w = coerce_candidate_dict(resp.text or "")
    if not data or "resolved_text" not in data:
        return ProbeResult(
            "json_mode",
            ok=False,
            detail="json_mode accepted but output unparseable; disabling",
        )
    return ProbeResult("json_mode", ok=True, detail="server honors response_format")


def probe_logprobs(client: Any, model_cfg: ModelConfig) -> ProbeResult:
    """Detect whether the server returns per-token logprobs. We can't force
    logprobs through the public ``complete`` signature, so this relies on the
    client already being configured to request them (the real adapter does via
    ``capture_token_entropy``). Supported iff the response carries entropy."""
    messages = [
        {"role": "system", "content": "Reply with a short JSON object."},
        {"role": "user", "content": "ping"},
    ]
    try:
        resp = client.complete(
            messages,
            model=model_cfg.model,
            temperature=0.0,
            max_tokens=min(model_cfg.max_tokens, 256),
            json_mode=False,
        )
    except Exception as exc:  # noqa: BLE001 - logprobs are optional
        return ProbeResult("logprobs", ok=False, detail=f"request failed: {exc}")
    if resp.mean_token_entropy is not None:
        return ProbeResult(
            "logprobs", ok=True, detail=f"entropy available ({resp.mean_token_entropy:.3f})"
        )
    return ProbeResult("logprobs", ok=False, detail="server returned no logprobs")


def probe_embeddings(model_cfg: ModelConfig, *, embeddings_model: str = "") -> ProbeResult:
    """Detect whether the server serves the ``/v1/embeddings`` endpoint.

    Uses a fresh ``OpenAIEmbeddingsClient`` (distinct from the completion client)
    and the existing capability helper. Supported iff a probe text embeds to a
    non-empty vector. When supported, the profile enables embedding RAG (semantic
    retrieval over past resolutions, survey §4.2); when not (the common case for
    a llama-server started without ``--embeddings``), RAG stays lexical (BM25).

    ``embeddings_model`` is the embedding model name to send. On a multi-model
    llama-server (a completion slot + a separate ``--embeddings`` slot), the
    embeddings endpoint only accepts the EMBEDDING model's id/alias, NOT the
    completion model's — so sending ``model_cfg.model`` here gets a 400 "model
    not found". When ``embeddings_model`` is set, the probe uses it; otherwise it
    falls back to the completion model name (correct for a single-model server
    that also embeds).
    """
    from capybase.memory.embeddings import OpenAIEmbeddingsClient, probe_embeddings_support

    # The embedding model: explicit override, else the completion model name
    # (single-model server that also embeds).
    emb_cfg = model_cfg
    if embeddings_model:
        emb_cfg = model_cfg.model_copy(update={"model": embeddings_model})
    try:
        client = OpenAIEmbeddingsClient(emb_cfg)
        supported = probe_embeddings_support(client)
    except Exception as exc:  # noqa: BLE001 - unsupported = BM25, never abort calibrate
        return ProbeResult("embeddings", ok=False, detail=f"probe failed: {exc}")
    if supported:
        return ProbeResult("embeddings", ok=True, detail="server serves /v1/embeddings")
    return ProbeResult(
        "embeddings", ok=False,
        detail="endpoint does not support embeddings (start llama-server with --embeddings)",
    )


def probe_end_to_end(client: Any, model_cfg: ModelConfig) -> ProbeResult:
    """Confirm the model can produce a complete, parseable candidate for a tiny
    synthetic conflict via the REAL resolve prompt path. This is the strongest
    signal that capybase's schema is achievable on this model at all."""
    messages = _resolve_probe_messages()
    try:
        resp = client.complete(
            messages,
            model=model_cfg.model,
            temperature=model_cfg.temperature,
            max_tokens=model_cfg.max_tokens,
            json_mode=model_cfg.json_mode,
        )
    except Exception as exc:  # noqa: BLE001 - end-to-end reports, never raises
        return ProbeResult("end_to_end", ok=False, detail=f"request failed: {exc}")
    finish = _finish_reason(resp)
    if finish == "length":
        return ProbeResult("end_to_end", ok=False, detail="truncated (finish_reason=length)")
    data, _w = coerce_candidate_dict(resp.text or "")
    if not data or not data.get("resolved_text"):
        return ProbeResult("end_to_end", ok=False, detail="did not parse to resolved_text")
    return ProbeResult("end_to_end", ok=True, detail="model produced a valid candidate")


# ---------------------------------------------------------------------------
# Structured capability probe (adaptive calibration §5): measures CoT length,
# JSON success rate, and instruction-following to drive factor selection + early-exit
# ---------------------------------------------------------------------------


@dataclass
class ModelCapability:
    """Structured capability signals from the pre-calibration probe.

    Advisory data that drives (1) adaptive factor selection — WHICH factors the
    two-phase DOE screens — and (2) the early-exit for near-perfect models. Not
    a profile section; lives on the CalibrationReport as diagnostic context.
    """

    json_success_rate: float = 0.0   # fraction of probe calls that parsed to resolved_text
    mean_cot_chars: int = 0          # mean response-text length across probe calls
    is_thinking_model: bool = False  # <think>/</think> tags detected
    follows_instructions: bool = True  # no conflict markers in any response
    n_samples: int = 0               # how many calls were made

    @property
    def is_strong_model(self) -> bool:
        """A near-perfect model: high JSON success, follows instructions, not a
        thinking model. Triggers the early-exit (skip the DOE, lock in baseline)."""
        return (
            self.json_success_rate >= 0.95
            and self.follows_instructions
            and not self.is_thinking_model
        )


def probe_capabilities_detailed(
    client: Any, model_cfg: ModelConfig, *, n_samples: int = 5
) -> tuple[ProbeResult, ModelCapability]:
    """Run N quick resolve-prompt calls and measure structured capability signals.

    Replaces the single-shot binary end-to-end probe with an N-of-M diagnostic
    that captures: JSON success rate (fraction that parse to resolved_text),
    CoT length (mean response length + thinking-tag detection), and
    instruction-following (whether conflict markers leak into responses). These
    signals drive the adaptive factor selection and the early-exit.

    Uses the REAL resolve prompt (same as probe_end_to_end) so the parseability
    bar matches real resolutions. Degrades gracefully: any call failure counts
    as a miss (not a crash). Returns ``(ProbeResult, ModelCapability)``.
    """
    from capybase.adapters.parsers import contains_markers as _has_markers

    messages = _resolve_probe_messages()
    cap = ModelCapability(n_samples=n_samples)
    successes = 0
    total_chars = 0
    saw_think_tag = False
    leaked_markers = False

    for _ in range(n_samples):
        try:
            resp = client.complete(
                messages,
                model=model_cfg.model,
                temperature=model_cfg.temperature,
                max_tokens=model_cfg.max_tokens,
                json_mode=model_cfg.json_mode,
            )
        except Exception:  # noqa: BLE001 - a failed call is a miss
            continue
        text = resp.text or ""
        total_chars += len(text)
        if "<think>" in text.lower() or "</think>" in text.lower():
            saw_think_tag = True
        if _has_markers(text):
            leaked_markers = True
        data, _w = coerce_candidate_dict(text)
        if data and data.get("resolved_text") and _finish_reason(resp) != "length":
            successes += 1

    cap.json_success_rate = successes / n_samples if n_samples else 0.0
    cap.mean_cot_chars = int(total_chars / n_samples) if n_samples else 0
    cap.is_thinking_model = saw_think_tag
    cap.follows_instructions = not leaked_markers

    detail = (
        f"json_success={cap.json_success_rate:.0%} ({successes}/{n_samples}), "
        f"mean_chars={cap.mean_cot_chars}, "
        f"thinking_model={cap.is_thinking_model}, "
        f"follows_instructions={cap.follows_instructions}"
    )
    return ProbeResult("capabilities", ok=True, detail=detail), cap


# ---------------------------------------------------------------------------
# Mechanism calibration: empirically A/B-select resolution strategies
# ---------------------------------------------------------------------------


@dataclass
class MechanismChoices:
    """The generation-mechanism settings selected by :func:`probe_mechanisms`.

    Mirrors the ModelConfig fields they overlay. All default to current built-in
    behavior (samples=1, everything off); a field is only non-default if
    calibration measured it to actually improve resolution quality on this model.
    """

    samples: int = 1
    two_pass: bool = False
    plan_search: bool = False
    prompt_variants: bool = False
    diverse_sampling: bool = False
    enable_self_consistency: bool = False


def _resolve_under_config(
    client: Any, model_cfg: ModelConfig, conflict, context
) -> tuple[Any, float]:
    """Resolve one conflict under ``model_cfg`` and return (winner_candidate, latency_ms).

    Mirrors the orchestrator's ``_resolve_unit`` branch: two_pass vs consensus vs
    plain propose, gated on the config flags. Calibration thus evaluates each
    setting through the SAME resolution path the orchestrator uses at runtime, so
    the A/B result reflects real behavior. The winner is ``candidates[0]`` (the
    engine already ranks by consensus when applicable).
    """
    from capybase.resolution_engine import ResolutionEngine

    engine = ResolutionEngine(model_cfg, client=client)
    t0 = time.monotonic()
    n = max(1, model_cfg.samples)
    if model_cfg.two_pass and n > 1:
        candidates = engine.propose_two_pass(
            conflict.unit, context, n_samples=n,
            temperature=model_cfg.sampling_temperature,
        )
    elif model_cfg.enable_self_consistency:
        # propose_with_consensus returns (candidates, report); the other paths
        # return just candidates. Unpack here so the winner extraction below is
        # uniform — otherwise candidates[0] is the whole list, not the winner,
        # and .resolved_text raises AttributeError (the "eval error" that
        # silently disabled the self-consistency A/B on every prior calibrate).
        candidates, _report = engine.propose_with_consensus(
            conflict.unit, context, n_samples=n,
        )
    else:
        candidates = engine.propose(conflict.unit, context, n_samples=n)
    latency_ms = (time.monotonic() - t0) * 1000.0
    winner = candidates[0] if candidates else None
    return winner, latency_ms


def _evaluate_mechanism_setting(client: Any, model_cfg: ModelConfig) -> Any:
    """Resolve the whole corpus under ``model_cfg`` and return a SettingScore."""
    from capybase.quality import evaluate_setting

    def resolve_one(conflict, context, cfg):
        winner, latency = _resolve_under_config(client, cfg, conflict, context)
        if winner is None:
            raise RuntimeError("no candidate produced")
        return winner, None, latency

    return evaluate_setting(resolve_one, model_cfg)


def _compare_quality(a: Any, b: Any) -> int:
    """Compare two SettingScores on correctness then proxy ONLY (no latency).

    Used for the enable decision: a mechanism turns on only if it improves
    correctness or the validator-proxy sum. Latency is deliberately excluded —
    it's a noisy signal (especially on near-instant error paths) and must never
    enable a mechanism on its own. Use the full ``compare_scores`` only when
    latency is a legitimate tiebreaker (e.g. reporting)."""
    if a.n_correct != b.n_correct:
        return (a.n_correct > b.n_correct) - (a.n_correct < b.n_correct)
    if a.proxy_sum != b.proxy_sum:
        return (a.proxy_sum > b.proxy_sum) - (a.proxy_sum < b.proxy_sum)
    return 0


# Candidate mechanisms to A/B. Each entry: (field_name, default_value). The probe
# compares config-with-mechanism-ON vs config-with-it-OFF at the chosen sample
# count and keeps ON only if it strictly improves the corpus score.
_CANDIDATE_MECHANISMS: tuple[tuple[str, Any], ...] = (
    ("two_pass", False),
    ("plan_search", False),
    ("prompt_variants", False),
    ("diverse_sampling", False),
    ("enable_self_consistency", False),
)

# The sample count used when evaluating multi-sample mechanisms. Small enough to
# keep calibration bounded (each eval resolves the whole corpus N times), large
# enough that self-consistency/variants have samples to agree over.
_MECHANISM_EVAL_SAMPLES = 3

# Minimum corpus size below which mechanism A/B selection is refused. With a
# small corpus a single noisy case can flip a mechanism on/off (n_correct is an
# integer in [0, len(corpus)]), so below this floor ``probe_mechanisms`` leaves
# ALL multi-sample mechanisms off (samples=1) and records why — it does not
# guess. Bumping the corpus past this floor re-enables selection automatically.
_MIN_CORPUS_FOR_MECHANISM_SELECTION = 15


def probe_mechanisms(
    client: Any, model_cfg: ModelConfig, *, base_cfg: ModelConfig
) -> tuple[ProbeResult, MechanismChoices]:
    """Empirically A/B-select resolution mechanisms on the blessed corpus.

    Strategy (independent A/B per mechanism — bounded cost, no combinatorial
    explosion): first decide whether multi-sampling helps at all (N=3 vs N=1 on
    correctness); if it does, set ``samples=3`` and A/B each mechanism ON vs OFF
    at that N, keeping ON only when it strictly beats OFF. If multi-sampling
    doesn't help, all multi-sample mechanisms stay off and ``samples=1``.

    Every eval resolves the full corpus; a mechanism that errors during its eval
    is treated as "off" (graceful — never aborts calibration). Returns the
    winning choices + a ProbeResult summarizing the decisions.

    Below ``_MIN_CORPUS_FOR_MECHANISM_SELECTION`` the corpus is too small to
    A/B-select confidently (a single noisy case can flip a mechanism on/off), so
    all mechanisms are left off (samples=1) and the refusal is recorded — it
    never guesses.
    """
    from capybase.calibration_corpus import CALIBRATION_CONFLICTS

    choices = MechanismChoices()
    decisions: list[str] = []

    if len(CALIBRATION_CONFLICTS) < _MIN_CORPUS_FOR_MECHANISM_SELECTION:
        # Too few cases to trust a one-case correctness difference. Leave every
        # multi-sample mechanism off and report the refusal so the user knows
        # selection was skipped for this reason (not that nothing helped).
        n = len(CALIBRATION_CONFLICTS)
        decisions.append(
            f"corpus too small for confident selection ({n} < "
            f"{_MIN_CORPUS_FOR_MECHANISM_SELECTION} min); leaving all mechanisms off"
        )
        return (
            ProbeResult(
                "mechanisms", ok=False,
                detail="; ".join(decisions),
            ),
            choices,
        )

    # Base resolution config: all mechanisms off, samples=1.
    off_base = base_cfg.model_copy(update={
        "samples": 1, "two_pass": False, "plan_search": False,
        "prompt_variants": False, "diverse_sampling": False,
        "enable_self_consistency": False,
    })
    try:
        baseline_1 = _evaluate_mechanism_setting(client, off_base)
    except Exception as exc:  # noqa: BLE001 - mechanisms are optional; don't abort
        return (
            ProbeResult("mechanisms", ok=False,
                        detail=f"baseline eval failed ({exc}); leaving all mechanisms off"),
            choices,
        )

    # --- Does multi-sampling help? (N=3 vs N=1) ---
    multi_base = off_base.model_copy(update={"samples": _MECHANISM_EVAL_SAMPLES})
    try:
        baseline_multi = _evaluate_mechanism_setting(client, multi_base)
    except Exception:  # noqa: BLE001 - multi-sampling unavailable
        baseline_multi = baseline_1  # treat as no better

    if _compare_quality(baseline_multi, baseline_1) > 0:
        choices.samples = _MECHANISM_EVAL_SAMPLES
        working_cfg = multi_base
        decisions.append(f"samples={_MECHANISM_EVAL_SAMPLES} beats 1 "
                         f"({baseline_multi.n_correct}>{baseline_1.n_correct} correct)")
    else:
        choices.samples = 1
        working_cfg = off_base
        decisions.append(f"samples=1 ({baseline_1.n_correct} correct); "
                         f"multi-sampling didn't help ({baseline_multi.n_correct})")

    # --- A/B each mechanism independently at the chosen sample count ---
    for field, _default in _CANDIDATE_MECHANISMS:
        on_cfg = working_cfg.model_copy(update={field: True})
        try:
            on_score = _evaluate_mechanism_setting(client, on_cfg)
            off_score = _evaluate_mechanism_setting(client, working_cfg)
        except Exception as exc:  # noqa: BLE001 - a broken mechanism stays off
            decisions.append(f"{field}: off (eval error)")
            continue
        # Enable ONLY on a correctness-or-proxy improvement — NOT on latency
        # alone. Latency is noisy (especially for near-instant error paths) and
        # must never flip a mechanism on by itself; it's a pure tiebreaker for
        # genuinely equal-quality settings. This avoids the spurious "0->0
        # correct, improved" enable when both paths error equally.
        quality_cmp = _compare_quality(on_score, off_score)
        if quality_cmp > 0:
            setattr(choices, field, True)
            working_cfg = on_cfg  # carry the winner forward (mild interaction benefit)
            decisions.append(f"{field}: ON (improved {off_score.n_correct}->"
                             f"{on_score.n_correct} correct, proxy {off_score.proxy_sum:.0f}->"
                             f"{on_score.proxy_sum:.0f})")
        else:
            decisions.append(f"{field}: off (no improvement; {on_score.n_correct} vs "
                             f"{off_score.n_correct} correct)")

    detail = "; ".join(decisions)
    any_on = (choices.samples > 1 or any(
        getattr(choices, f) for f, _ in _CANDIDATE_MECHANISMS
    ))
    return ProbeResult("mechanisms", ok=any_on, detail=detail), choices


# ---------------------------------------------------------------------------
# Prompt-rendering profile A/B (calibrate)
# ---------------------------------------------------------------------------


def probe_prompt_profile(
    client: Any,
    model_cfg: ModelConfig,
    *,
    base_cfg: ModelConfig,
    existing: "PromptProfile | None" = None,
) -> tuple[ProbeResult, "PromptProfile"]:
    """Empirically A/B-select the prompt-rendering profile on the blessed corpus.

    Compares the default (v6 JSON) layout against the markdown-code layout (and,
    if markdown-code wins, the top-heavy instruction position against the
    winner), keeping whichever scores higher on the corpus. Mirrors
    :func:`probe_mechanisms`'s independent-A/B strategy and its caution on small
    corpora: below :data:`_MIN_CORPUS_FOR_MECHANISM_SELECTION` the probe refuses
    and returns the ``existing`` profile (or DEFAULT) unchanged, so a hand-tuned
    profile survives a recalibrate on a small corpus.

    Each candidate layout is evaluated by resolving the whole corpus under it
    (the active prompt profile is a process global, so we ``set_active_profile``
    before each eval). Returns ``(ProbeResult, winning PromptProfile)``.
    """
    from capybase.calibration_corpus import CALIBRATION_CONFLICTS
    from capybase.prompt_profile import (
        DEFAULT_PROFILE, InstructionPosition, OutputLayout,
        PromptProfile, set_active_profile,
    )

    decisions: list[str] = []
    winner: PromptProfile = existing if existing is not None else DEFAULT_PROFILE

    if len(CALIBRATION_CONFLICTS) < _MIN_CORPUS_FOR_MECHANISM_SELECTION:
        # Too few cases to trust a correctness difference — preserve the
        # existing profile (or default) and record the refusal.
        n = len(CALIBRATION_CONFLICTS)
        decisions.append(
            f"corpus too small for prompt-profile selection ({n} < "
            f"{_MIN_CORPUS_FOR_MECHANISM_SELECTION} min); keeping existing profile"
        )
        return (
            ProbeResult("prompt_profile", ok=False, detail="; ".join(decisions)),
            winner,
        )

    # Baseline: the default (v6 JSON) layout.
    try:
        set_active_profile(DEFAULT_PROFILE)
        baseline = _evaluate_mechanism_setting(client, base_cfg)
    except Exception as exc:  # noqa: BLE001 - prompt profile is optional
        decisions.append(f"baseline eval failed ({exc}); keeping default profile")
        return (
            ProbeResult("prompt_profile", ok=False, detail="; ".join(decisions)),
            winner,
        )

    # Candidate 1: markdown-code layout.
    md_profile = PromptProfile(output_layout=OutputLayout.MARKDOWN_CODE)
    try:
        set_active_profile(md_profile)
        md_score = _evaluate_mechanism_setting(client, base_cfg)
    except Exception as exc:  # noqa: BLE001 - a broken candidate stays off
        decisions.append(f"markdown_code: off (eval error: {exc})")
        md_score = baseline  # treat as no better

    if _compare_quality(md_score, baseline) > 0:
        winner = md_profile
        decisions.append(
            f"markdown_code: ON (improved {baseline.n_correct}->"
            f"{md_score.n_correct} correct, proxy {baseline.proxy_sum:.0f}->"
            f"{md_score.proxy_sum:.0f})"
        )
        # Candidate 2 (only if markdown won): top-heavy position. A model that
        # benefits from the raw-code layout may also benefit from rules-first
        # ordering; A/B it against the markdown winner.
        top_profile = PromptProfile(
            output_layout=OutputLayout.MARKDOWN_CODE,
            instruction_position=InstructionPosition.TOP_HEAVY,
        )
        try:
            set_active_profile(top_profile)
            top_score = _evaluate_mechanism_setting(client, base_cfg)
        except Exception as exc:  # noqa: BLE001
            decisions.append(f"top_heavy: off (eval error: {exc})")
            top_score = md_score
        if _compare_quality(top_score, md_score) > 0:
            winner = top_profile
            decisions.append(
                f"top_heavy: ON (improved {md_score.n_correct}->"
                f"{top_score.n_correct} correct)"
            )
        else:
            decisions.append(
                f"top_heavy: off (no improvement; {top_score.n_correct} vs "
                f"{md_score.n_correct} correct)"
            )
    else:
        decisions.append(
            f"markdown_code: off (no improvement; {md_score.n_correct} vs "
            f"{baseline.n_correct} correct)"
        )

    # Restore the winner as the active profile so any downstream probe (and the
    # caller's profile construction) sees it.
    set_active_profile(winner if winner != DEFAULT_PROFILE else None)
    return (
        ProbeResult("prompt_profile", ok=(winner != DEFAULT_PROFILE), detail="; ".join(decisions)),
        winner,
    )


# ---------------------------------------------------------------------------
# Two-phase screening design (replaces the independent A/B probes)
# ---------------------------------------------------------------------------


def _two_phase_factors(
    base_cfg: ModelConfig,
    *,
    capabilities: "ModelCapability | None" = None,
    force_factors: tuple[str, ...] = (),
) -> list:
    """The factor set Phase 1 screens, adaptively chosen from the model's
    measured capabilities.

    Every factor is sampled at two levels (low/high) by the fractional-factorial
    design. The mechanism levels are derived from the base config's current
    values so the screening explores VARIATIONS of the known-good config: the
    'low' level is the current setting, the 'high' level is the alternative.

    ``capabilities`` (from :func:`probe_capabilities_detailed`) adapts WHICH
    factors are screened to the model's actual weaknesses (feedback §5):
    - Low JSON success (< 0.5) → include output_layout + rule_emphasis (formatting
      matters when the model is escaping-broken).
    - Thinking model (CoT detected) → include instruction_position, example_limit,
      samples (structure + budget matter for verbose reasoners).
    - Strong model (high success, not thinking) → minimal set (samples,
      diverse_sampling); don't waste runs on factors that won't move.

    ``force_factors`` (from ``--enable-factor``) forces specific factors in
    regardless of the capability signals, for manual exploration.
    """
    from capybase.calibration_design import Factor
    from capybase.prompt_profile import (
        ConflictSummaryMode, HistoryFraming, InstructionPosition, OutputLayout,
        ParseRepairMode, PromptProfile, RetrySchedule, RuleEmphasis, SideOrdering,
    )

    # Build the full factor catalog; we select from it based on capabilities.
    # The three new axes (rule_emphasis, conflict_summary_mode, side_ordering)
    # are opt-in via --enable-factor or the low-JSON-success branch; they're NOT
    # in the default set to keep the DOE bounded (≤7 factors → 16 runs).
    all_factors = {
        "output_layout": Factor("output_layout",
                                OutputLayout.JSON_V6, OutputLayout.MARKDOWN_CODE),
        "instruction_position": Factor("instruction_position",
                                       InstructionPosition.BOTTOM, InstructionPosition.TOP_HEAVY),
        "history_framing": Factor("history_framing",
                                  HistoryFraming.UNTRUSTED, HistoryFraming.NEUTRAL),
        "example_limit": Factor("example_limit", 2, 1),
        "samples": Factor("samples", max(1, base_cfg.samples), max(3, base_cfg.samples)),
        "diverse_sampling": Factor("diverse_sampling",
                                   bool(base_cfg.diverse_sampling), not bool(base_cfg.diverse_sampling)),
        "prompt_variants": Factor("prompt_variants",
                                  bool(base_cfg.prompt_variants), not bool(base_cfg.prompt_variants)),
        # New axes (feedback §3.1) — opt-in only:
        "rule_emphasis": Factor("rule_emphasis", RuleEmphasis.PLAIN, RuleEmphasis.FORMATTED),
        "conflict_summary_mode": Factor("conflict_summary_mode",
                                        ConflictSummaryMode.FULL, ConflictSummaryMode.INTENT_ONLY),
        "side_ordering": Factor("side_ordering",
                                SideOrdering.CURRENT_FIRST, SideOrdering.BASE_FIRST),
        # self_consistency as a DOE factor (feedback §3.2): opt-in via
        # --enable-factor or when capabilities signal semantic disagreements.
        "enable_self_consistency": Factor("enable_self_consistency", False, True),
        # Mechanism-factor parity (feedback §3.2):
        "parse_repair_mode": Factor("parse_repair_mode",
                                    ParseRepairMode.AUTO_REPAIR, ParseRepairMode.STRICT),
        "retry_schedule": Factor("retry_schedule",
                                 RetrySchedule.STANDARD, RetrySchedule.LIGHT),
    }

    # Default: screen the standard 7 factors (backward compat when no
    # capabilities given). The three new axes (rule_emphasis,
    # conflict_summary_mode, side_ordering) are opt-in via --enable-factor or
    # the capability-driven branches below; they're NOT in the default set to
    # keep the DOE bounded (≤8 factors → 16 runs via the Res-IV design).
    selected: set[str] = {
        "output_layout", "instruction_position", "history_framing",
        "example_limit", "samples", "diverse_sampling", "prompt_variants",
    }

    if capabilities is not None:
        selected.clear()
        # Always include the core mechanism levers.
        selected |= {"samples", "diverse_sampling"}
        if capabilities.json_success_rate < 0.5:
            # The model struggles with JSON — the layout + rule formatting matter.
            selected |= {"output_layout", "instruction_position"}
        elif capabilities.is_thinking_model:
            # A verbose reasoner — structure + budget + few-shot density matter.
            selected |= {"instruction_position", "example_limit", "samples",
                         "output_layout", "history_framing"}
        elif capabilities.json_success_rate >= 0.8:
            # A strong model — minimal set; don't waste runs.
            pass  # just the core {samples, diverse_sampling}
        else:
            # Middle ground — screen the standard prompt axes.
            selected |= {"output_layout", "instruction_position", "example_limit"}

    # Force-in factors from --enable-factor.
    selected |= set(force_factors)

    # Preserve a stable ordering (prompt axes first, then mechanism, then the
    # new opt-in axes) for deterministic design-matrix construction.
    order = [
        "output_layout", "instruction_position", "history_framing",
        "example_limit", "samples", "diverse_sampling", "prompt_variants",
        "enable_self_consistency",
        "rule_emphasis", "conflict_summary_mode", "side_ordering",
        "parse_repair_mode", "retry_schedule",
    ]
    return [all_factors[name] for name in order if name in selected]


def _apply_design_point(
    base_cfg: ModelConfig, point, *, n_reps: int = 1
) -> tuple[ModelConfig, "PromptProfile"]:
    """Encode a DesignPoint's levels onto a (ModelConfig, PromptProfile).

    The design point carries the factor settings for one experimental run; this
    applies them to a fresh copy of the base config + a fresh PromptProfile.
    Mechanism/sampling axes mutate the ModelConfig; prompt axes mutate the
    PromptProfile (and the process-global active profile is set so the engine
    renders under it).
    """
    from capybase.prompt_profile import PromptProfile, set_active_profile

    levels = point.levels
    # Mechanism/sampling axes → ModelConfig fields.
    cfg_updates = {}
    if "samples" in levels:
        cfg_updates["samples"] = int(levels["samples"])
    if "diverse_sampling" in levels:
        cfg_updates["diverse_sampling"] = bool(levels["diverse_sampling"])
    if "prompt_variants" in levels:
        cfg_updates["prompt_variants"] = bool(levels["prompt_variants"])
    if "enable_self_consistency" in levels:
        cfg_updates["enable_self_consistency"] = bool(levels["enable_self_consistency"])
    cfg = base_cfg.model_copy(update=cfg_updates) if cfg_updates else base_cfg
    # Prompt axes → PromptProfile. All axes default to PromptProfile()'s defaults
    # so a design point that doesn't set an axis keeps the production value.
    _d = PromptProfile()
    profile = PromptProfile(
        output_layout=levels.get("output_layout", _d.output_layout),
        instruction_position=levels.get("instruction_position", _d.instruction_position),
        history_framing=levels.get("history_framing", _d.history_framing),
        example_limit=int(levels.get("example_limit", _d.example_limit)),
        rule_emphasis=levels.get("rule_emphasis", _d.rule_emphasis),
        conflict_summary_mode=levels.get("conflict_summary_mode", _d.conflict_summary_mode),
        side_ordering=levels.get("side_ordering", _d.side_ordering),
        parse_repair_mode=levels.get("parse_repair_mode", _d.parse_repair_mode),
        retry_schedule=levels.get("retry_schedule", _d.retry_schedule),
    )
    set_active_profile(profile if profile != PromptProfile() else None)
    return cfg, profile


# ---------------------------------------------------------------------------
# Multi-fidelity epoch calibration: anytime, haltable, successive refinement
# (replaces the fixed two-phase batch with a sequence of cheap-to-deep epochs)
# ---------------------------------------------------------------------------


# How many configurations survive into Epoch 2 (carried forward from Epoch 1's
# top performers, alongside the factorial on the top-3 factors). Keeps the
# epoch-2 pool bounded while guaranteeing the epoch-1 best isn't dropped before
# it can be re-evaluated at higher fidelity.
_EPOCH2_SURVIVORS = 3

# How many finalists Epoch 3 evaluates head-to-head on the full corpus when
# Epoch 2 couldn't separate them.
_EPOCH3_FINALISTS = 2

# Epoch-3 fires only when the Epoch-2 top-2 finalists are within this margin:
# equal correctness AND proxy within this delta. A wider gap means Epoch 2
# already identified the winner and Epoch 3 would be pure confirmation cost.
_TIEBREAKER_PROXY_EPS = 1.0


def _fidelity_schedule(corpus_size: int, *, n_epochs: int = 3) -> tuple[int, ...]:
    """Evenly-spaced corpus-prefix sizes for the multi-fidelity epochs.

    For a 15-conflict corpus and 3 epochs: ``(5, 10, 15)`` — Epoch 1 screens
    cheaply on a third of the corpus, Epoch 2 refines on two-thirds, Epoch 3
    validates finalists on the full corpus. The last epoch is always the full
    corpus so the final adoption decision reflects full-corpus quality.

    Small corpora are floored at their full size (no artificial subsampling
    below ~3 conflicts — a 1-2 conflict "epoch" is pure noise): a 2-conflict
    corpus yields ``(2, 2, 2)`` (all epochs full). A 3-conflict corpus yields
    ``(1, 2, 3)``.
    """
    if corpus_size <= 0:
        return ()
    # For tiny corpora, just repeat the full size — multi-fidelity only helps
    # when there's room to subsample meaningfully.
    if corpus_size < n_epochs:
        return tuple(corpus_size for _ in range(n_epochs))
    # Evenly-spaced fractions: i/n_epochs of the corpus, rounded, floored at 1.
    # The last entry is clamped to the full size (not a rounding artifact).
    sizes: list[int] = []
    for i in range(1, n_epochs + 1):
        s = max(1, round(corpus_size * i / n_epochs))
        sizes.append(min(s, corpus_size))
    sizes[-1] = corpus_size
    return tuple(sizes)


@dataclass
class _EpochTracker:
    """Tracks per-point scores across epochs for anytime best-so-far selection.

    Scores are keyed by ``(config_id, epoch)`` because the SAME configuration
    may be re-evaluated at different fidelities (a survivor from Epoch 1 is
    re-scored in Epoch 2 on a larger corpus). All comparisons are WITHIN a
    single epoch (same corpus size → same ``n_correct`` denominator); cross-
    epoch comparison is invalid because ``_compare_quality`` uses absolute
    counts. The ``best_so_far`` property sidesteps this by always returning the
    best point from the HIGHEST epoch that has any data (highest fidelity wins).
    """

    scores: dict[tuple[str, int], Any] = field(default_factory=dict)
    points_by_id: dict[str, Any] = field(default_factory=dict)
    _max_epoch: int = 0

    def record(self, point, score, *, epoch: int) -> None:
        """Record a point's score at a given epoch (fidelity level)."""
        self.scores[(point.config_id, epoch)] = score
        self.points_by_id[point.config_id] = point
        if epoch > self._max_epoch:
            self._max_epoch = epoch

    def best_in_epoch(self, epoch: int):
        """Return (best point, its score) in a single epoch, or None if empty.

        "Best" is by ``_compare_quality`` (correctness → proxy, no latency).
        Ties pick the first-recorded (deterministic)."""
        entries = [
            (pid, self.scores[(pid, epoch)])
            for pid in self.points_by_id
            if (pid, epoch) in self.scores
        ]
        if not entries:
            return None
        best_pid, best_score = entries[0]
        for pid, score in entries[1:]:
            if _compare_quality(score, best_score) > 0:
                best_pid, best_score = pid, score
        return self.points_by_id[best_pid], best_score

    @property
    def best_so_far(self):
        """The best point from the highest epoch that has any completed scores.

        Highest-fidelity epoch wins because cross-epoch comparison is invalid
        (different corpus sizes). Returns None when nothing has been recorded."""
        for epoch in range(self._max_epoch, 0, -1):
            best = self.best_in_epoch(epoch)
            if best is not None:
                return best
        return None

    def top_k(self, k: int, *, epoch: int):
        """Return the top-k (point, score) pairs in an epoch, best-first."""
        entries = [
            (self.points_by_id[pid], self.scores[(pid, epoch)])
            for pid in self.points_by_id
            if (pid, epoch) in self.scores
        ]
        # Sort by _compare_quality descending (a > b → a first). Stable on ties.
        for i in range(1, len(entries)):
            j = i
            while j > 0 and _compare_quality(entries[j][1], entries[j - 1][1]) > 0:
                entries[j], entries[j - 1] = entries[j - 1], entries[j]
                j -= 1
        return entries[:k]


def _decode_point(point) -> tuple[dict, "PromptProfile"]:
    """Decode a DesignPoint's levels into (MechanismChoices kwargs, PromptProfile).

    Extracted from the inline Phase-2 decode so any epoch's winning point can
    be decoded uniformly. Only profile-persisted knobs are extracted (mech
    fields → MechanismChoices kwargs; prompt axes → PromptProfile). Factors not
    present in the point's levels fall back to DEFAULT_PROFILE / default mech.
    """
    from capybase.prompt_profile import DEFAULT_PROFILE, PromptProfile

    levels = point.levels
    cfg_kwargs: dict[str, Any] = {}
    if "samples" in levels:
        cfg_kwargs["samples"] = int(levels["samples"])
    if "diverse_sampling" in levels:
        cfg_kwargs["diverse_sampling"] = bool(levels["diverse_sampling"])
    if "prompt_variants" in levels:
        cfg_kwargs["prompt_variants"] = bool(levels["prompt_variants"])
    if "enable_self_consistency" in levels:
        cfg_kwargs["enable_self_consistency"] = bool(levels["enable_self_consistency"])
    profile = PromptProfile(
        output_layout=levels.get("output_layout", DEFAULT_PROFILE.output_layout),
        instruction_position=levels.get("instruction_position", DEFAULT_PROFILE.instruction_position),
        history_framing=levels.get("history_framing", DEFAULT_PROFILE.history_framing),
        example_limit=int(levels.get("example_limit", DEFAULT_PROFILE.example_limit)),
        rule_emphasis=levels.get("rule_emphasis", DEFAULT_PROFILE.rule_emphasis),
        conflict_summary_mode=levels.get("conflict_summary_mode", DEFAULT_PROFILE.conflict_summary_mode),
        side_ordering=levels.get("side_ordering", DEFAULT_PROFILE.side_ordering),
        parse_repair_mode=levels.get("parse_repair_mode", DEFAULT_PROFILE.parse_repair_mode),
        retry_schedule=levels.get("retry_schedule", DEFAULT_PROFILE.retry_schedule),
    )
    return cfg_kwargs, profile


def probe_two_phase(
    client: Any,
    model_cfg: ModelConfig,
    *,
    existing_choices: "MechanismChoices | None" = None,
    existing_profile: "PromptProfile | None" = None,
    n_reps: int = 1,
    run_phase2: bool = True,
    capabilities: "ModelCapability | None" = None,
    force_factors: tuple[str, ...] = (),
) -> tuple[ProbeResult, "MechanismChoices", "PromptProfile"]:
    """Multi-fidelity epoch calibration: screen, refine, tie-break — anytime.

    A sequence of cheap-to-deep epochs, each a valid stopping point:

    - **Epoch 1 (screening)**: the Resolution-IV fractional-factorial design
      samples all factor variations on a SMALL corpus prefix. Main effects +
      t-stats rank which dimensions drive performance. The epoch-1 best point is
      a usable (if rough) profile — the first "anytime" output.
    - **Epoch 2 (refinement)**: a full 2^k factorial on the top-3 factors (de-
      aliases their interactions) PLUS the top-3 epoch-1 survivors (re-evaluated
      at higher fidelity), on a LARGER corpus prefix. Discovers configurations
      the screening couldn't represent.
    - **Epoch 3 (tie-breaker)**: the top-2 epoch-2 finalists on the FULL corpus.
      Runs ONLY when epoch 2 couldn't separate them (within ``_TIEBREAKER_PROXY_EPS``).

    **Anytime halt**: at any point after the first completed eval, the caller
    can interrupt (Ctrl-C → ``KeyboardInterrupt``). The probe catches it,
    finalizes from the highest-fidelity epoch's best point, and returns normally
    so the CLI persists the best-so-far profile. A completed run is unaffected.

    Multi-fidelity: ``_compare_quality`` uses absolute ``n_correct``, so cross-
    corpus-size comparison is invalid. The ``_EpochTracker`` therefore compares
    only WITHIN an epoch (same denominator) and selects best-so-far from the
    highest epoch reached (highest fidelity wins).

    Below ``_MIN_CORPUS_FOR_MECHANISM_SELECTION`` the probe refuses and returns
    the existing choices/profile (or defaults), preserving a hand-tuned config
    through a recalibrate on a small corpus.

    ``run_phase2=False`` runs only Epoch 1 (screening) and reports the factor
    ranking without adopting — useful on slow models to read which dimensions
    matter before paying for refinement (same semantics as the old ``--calibrate-
    phase1-only``).
    """
    from capybase.calibration_corpus import CALIBRATION_CONFLICTS, conflicts_with_context
    from capybase.calibration_design import (
        DesignPoint, fractional_factorial_2k, rank_factors,
    )
    from capybase.prompt_profile import DEFAULT_PROFILE, PromptProfile, set_active_profile
    from capybase.quality import evaluate_setting_replicated

    decisions: list[str] = []
    choices = existing_choices if existing_choices is not None else MechanismChoices()
    winner_profile = existing_profile if existing_profile is not None else DEFAULT_PROFILE

    if len(CALIBRATION_CONFLICTS) < _MIN_CORPUS_FOR_MECHANISM_SELECTION:
        n = len(CALIBRATION_CONFLICTS)
        decisions.append(
            f"corpus too small for two-phase selection ({n} < "
            f"{_MIN_CORPUS_FOR_MECHANISM_SELECTION} min); keeping existing config"
        )
        return (
            ProbeResult("two_phase", ok=False, detail="; ".join(decisions)),
            choices, winner_profile,
        )

    factors = _two_phase_factors(model_cfg, capabilities=capabilities,
                                 force_factors=force_factors)

    full_corpus = conflicts_with_context()
    schedule = _fidelity_schedule(len(full_corpus))
    n_epochs = len(schedule)

    def _corpus_for(epoch: int):  # epoch is 1-based
        k = schedule[min(epoch - 1, n_epochs - 1)] if schedule else len(full_corpus)
        return full_corpus[:k]

    def _eval_point(point, *, epoch: int) -> Any:
        cfg, _prof = _apply_design_point(model_cfg, point, n_reps=n_reps)
        subset = _corpus_for(epoch)

        def resolve_one(conflict, context, c):
            w, lat = _resolve_under_config(client, c, conflict, context)
            if w is None:
                raise RuntimeError("no candidate produced")
            return w, None, lat

        return evaluate_setting_replicated(resolve_one, cfg, n_reps=n_reps, corpus=subset)

    tracker = _EpochTracker()
    ranking: list = []
    interrupted = False
    last_epoch_completed = 0

    try:
        # --- Epoch 1: screening on a small corpus prefix ---
        p1_design = fractional_factorial_2k(factors)
        k1 = len(_corpus_for(1))
        _progress(f"calibrate: Epoch 1/{n_epochs} screening ({len(factors)} factors, "
                  f"{len(p1_design)} points, {k1} conflicts"
                  f"{f', {n_reps} reps/point' if n_reps > 1 else ''})...")
        for i, point in enumerate(p1_design):
            _progress(f"calibrate: Epoch 1 point {i+1}/{len(p1_design)} "
                      f"({point.tag() or 'baseline'})...")
            score = _eval_point(point, epoch=1)
            tracker.record(point, score, epoch=1)
            _progress(f"calibrate:   -> {score.n_correct}/{score.total} correct")
        last_epoch_completed = 1
        # Score for effect estimation: correctness fraction (robust to corpus size).
        p1_scores = [tracker.scores[(p.config_id, 1)] for p in p1_design]
        p1_correct = [s.n_correct / max(1, s.total) for s in p1_scores]
        ranking = rank_factors(p1_correct, p1_design, factors)
        rank_summary = "; ".join(
            f"{r.name}({r.direction},|t|={abs(r.tstat):.1f})" for r in ranking[:4]
        )
        decisions.append(f"Phase 1 ranking: {rank_summary}")
        _progress(f"calibrate: Epoch 1 done — top factors: {rank_summary}")

        if run_phase2 and ranking:
            # --- Epoch 2: factorial refinement + survivors on a larger prefix ---
            top_factors = ranking[:3]
            factor_by_name = {f.name: f for f in factors}
            p2_factors = [factor_by_name[r.name] for r in top_factors if r.name in factor_by_name]
            if p2_factors:
                # The existing config's level for every factor — refinement points
                # inherit these for their non-top factors so each point is a complete
                # config. Makes the existing-baseline comparison apples-to-apples.
                existing_levels = {f.name: f.low for f in factors}

                def _merge_point(top_levels: dict, *, cid: str = "p2") -> DesignPoint:
                    merged = dict(existing_levels)
                    merged.update(top_levels)
                    return DesignPoint(config_id=cid, levels=merged)

                # Factorial on the top-3 factors (de-aliases their interactions).
                factorial_points: list[DesignPoint] = []
                for combo in itertools.product((-1, 1), repeat=len(p2_factors)):
                    top_levels = {
                        factor.name: factor.high if combo[j] > 0 else factor.low
                        for j, factor in enumerate(p2_factors)
                    }
                    factorial_points.append(_merge_point(top_levels, cid=f"e2-f{len(factorial_points)+1}"))

                # Survivors carried forward from Epoch 1 (re-evaluated at higher
                # fidelity). Ordered best-first so a mid-epoch interrupt retains
                # the most promising points. Deduped against the factorial set
                # (same levels → skip the re-eval).
                survivors = tracker.top_k(_EPOCH2_SURVIVORS, epoch=1)
                survivor_points: list[DesignPoint] = []
                seen_levels = {tuple(sorted(p.levels.items(), key=lambda kv: kv[0]))
                               for p in factorial_points}
                for spt, _sscore in survivors:
                    key = tuple(sorted(spt.levels.items(), key=lambda kv: kv[0]))
                    if key not in seen_levels:
                        survivor_points.append(DesignPoint(
                            config_id=f"e2-s{spt.config_id}", levels=dict(spt.levels),
                        ))
                        seen_levels.add(key)

                e2_pool = survivor_points + factorial_points
                k2 = len(_corpus_for(2))
                _progress(f"calibrate: Epoch 2/{n_epochs} refinement "
                          f"({len(e2_pool)} points: {len(survivor_points)} survivors + "
                          f"{len(factorial_points)} factorial, {k2} conflicts)...")
                for j, point in enumerate(e2_pool):
                    _progress(f"calibrate: Epoch 2 point {j+1}/{len(e2_pool)} "
                              f"({point.tag() or 'baseline'})...")
                    score = _eval_point(point, epoch=2)
                    tracker.record(point, score, epoch=2)
                    _progress(f"calibrate:   -> {score.n_correct}/{score.total} correct")
                last_epoch_completed = 2
                _progress(f"calibrate: Epoch 2 done")

                # --- Epoch 3: tie-breaker (only if epoch 2 top-2 are close) ---
                finalists = tracker.top_k(_EPOCH3_FINALISTS, epoch=2)
                if len(finalists) >= 2:
                    _s1, sc1 = finalists[0]
                    _s2, sc2 = finalists[1]
                    close = (
                        sc1.n_correct == sc2.n_correct
                        and abs(sc1.proxy_sum - sc2.proxy_sum) <= _TIEBREAKER_PROXY_EPS
                    )
                    if close and n_epochs >= 3:
                        k3 = len(_corpus_for(3))
                        e3_points = [
                            DesignPoint(config_id=f"e3-{fpt.config_id}",
                                        levels=dict(fpt.levels))
                            for fpt, _ in finalists
                        ]
                        _progress(f"calibrate: Epoch 3/{n_epochs} tie-breaker "
                                  f"({len(e3_points)} finalists, {k3} conflicts)...")
                        for j, point in enumerate(e3_points):
                            _progress(f"calibrate: Epoch 3 point {j+1}/{len(e3_points)} "
                                      f"({point.tag() or 'baseline'})...")
                            score = _eval_point(point, epoch=3)
                            tracker.record(point, score, epoch=3)
                            _progress(f"calibrate:   -> {score.n_correct}/{score.total} correct")
                        last_epoch_completed = 3
                        _progress(f"calibrate: Epoch 3 done")
                    elif close:
                        _progress("calibrate: top-2 close but no Epoch 3 in schedule; "
                                  "keeping Epoch 2 winner")
    except KeyboardInterrupt:
        interrupted = True
        _progress(f"calibrate: interrupted after epoch {last_epoch_completed} "
                  f"— using best-so-far")

    # --- Finalize: adopt best-so-far if it beats the existing baseline ---
    best = tracker.best_so_far
    best_cfg_kwargs = {
        "samples": choices.samples, "two_pass": choices.two_pass,
        "plan_search": choices.plan_search, "prompt_variants": choices.prompt_variants,
        "diverse_sampling": choices.diverse_sampling,
        "enable_self_consistency": choices.enable_self_consistency,
    }
    best_profile = winner_profile

    if best is not None and run_phase2:
        best_point, best_score = best
        # Evaluate the existing baseline at the SAME fidelity (corpus size) as the
        # best-so-far so the adoption comparison is valid (same n_correct
        # denominator). ``best_score.total`` is the corpus size at the winning
        # epoch — slice the full corpus to match.
        existing_point = DesignPoint(
            config_id="existing",
            levels={f.name: f.low for f in factors},
        )
        existing_subset = full_corpus[:best_score.total]
        _apply_design_point(model_cfg, existing_point, n_reps=n_reps)

        def _resolve_existing(conflict, context, c):
            w, lat = _resolve_under_config(client, c, conflict, context)
            if w is None:
                raise RuntimeError("no candidate produced")
            return w, None, lat

        # The baseline eval is itself a corpus sweep — protect it from a second
        # interruption (or any eval failure). When the baseline can't be scored,
        # adopt best-so-far ONLY if it showed positive correctness (it resolved
        # SOMETHING; keeping a config we can't even evaluate would be guessing).
        # This keeps the anytime promise: an interrupted run returns the best
        # data it has, never crashes.
        try:
            existing_score = evaluate_setting_replicated(
                _resolve_existing, model_cfg, n_reps=n_reps, corpus=existing_subset,
            )
            baseline_available = True
        except KeyboardInterrupt:
            interrupted = True
            baseline_available = False
            _progress("calibrate: interrupted during baseline comparison "
                      "— adopting best-so-far without baseline gate")
        except Exception as exc:  # noqa: BLE001 - never crash on finalize
            baseline_available = False
            decisions.append(f"baseline eval failed ({exc}); adopting best-so-far")

        if baseline_available:
            # Adopt ONLY on a strict improvement (correctness → proxy, no latency).
            if _compare_quality(best_score, existing_score) > 0:
                decoded_kwargs, best_profile = _decode_point(best_point)
                best_cfg_kwargs.update(decoded_kwargs)
                decisions.append(
                    f"Epoch {last_epoch_completed} winner: {best_point.config_id} "
                    f"({best_score.n_correct}/{best_score.total} correct"
                    f"{f', {n_reps} reps' if n_reps > 1 else ''})"
                )
            else:
                cmp = _compare_quality(best_score, existing_score)
                if cmp == 0:
                    decisions.append(
                        f"best-so-far ties existing ({existing_score.n_correct}/"
                        f"{existing_score.total} correct); keeping existing"
                    )
                else:
                    decisions.append(
                        f"existing beats best-so-far ({existing_score.n_correct}/"
                        f"{existing_score.total} correct); keeping it"
                    )
        else:
            # Baseline unavailable: adopt best-so-far iff it has positive
            # correctness; else keep existing (don't adopt a zero-score config
            # we couldn't validate against the baseline).
            if best_score.n_correct > 0:
                decoded_kwargs, best_profile = _decode_point(best_point)
                best_cfg_kwargs.update(decoded_kwargs)
                decisions.append(
                    f"adopted best-so-far {best_point.config_id} "
                    f"({best_score.n_correct}/{best_score.total} correct) "
                    f"without baseline comparison"
                )
            else:
                decisions.append(
                    "keeping existing (best-so-far has no correct resolutions "
                    "and baseline unavailable)"
                )
    elif best is not None and not run_phase2:
        decisions.append("Phase 1 only (screening); keeping existing config")
    elif interrupted:
        decisions.append("interrupted before any epoch completed; keeping existing config")

    if interrupted:
        decisions.append(
            f"INTERRUPTED after epoch {last_epoch_completed} "
            f"(profile reflects best-so-far, may be less refined)"
        )

    choices = MechanismChoices(**best_cfg_kwargs)
    set_active_profile(best_profile if best_profile != DEFAULT_PROFILE else None)
    ok = (
        choices.samples > 1
        or any(getattr(choices, f) for f, _ in _CANDIDATE_MECHANISMS)
        or best_profile != DEFAULT_PROFILE
    )
    return (
        ProbeResult("two_phase", ok=ok, detail="; ".join(decisions)),
        choices, best_profile,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _progress(msg: str) -> None:
    """Print a calibration progress line to stderr, flushed immediately.

    The calibration report is buffered to stdout and printed only at the end,
    which makes a multi-hour corpus sweep on a slow model a black box. This
    emits phase markers to stderr (so it interleaves correctly when stdout is
    redirected to a file) with flush=True so each line appears live. Disabled
    when stderr isn't a tty? No — always emit: a redirected run greps the log
    for progress too. Quiet under --json is the caller's job (it suppresses
    stderr separately if needed).
    """
    import sys
    print(msg, file=sys.stderr, flush=True)


def _gen_timeout_from_latency(
    latencies_ms: list[float],
    *,
    max_tokens: int = 0,
    probed_budget: int = 0,
) -> int:
    """Derive a generation timeout (seconds) from observed latencies.

    The probe's mean latency was measured at a SMALL output budget (the smallest
    ladder rung that succeeded, ``probed_budget`` — often 1024-2048). The real
    resolve uses the calibrated ``max_tokens`` (e.g. 16384 after headroom), and a
    larger output budget takes proportionally longer to generate. So the timeout
    scales latency by ``(max_tokens / probed_budget)``, capped by
    :data:`_MAX_TOKENS_TIMEOUT_SCALE` so a huge budget doesn't produce a
    multi-hour deadline.

    Floored at :data:`_MIN_GEN_TIMEOUT` (180s) so calibration never produces a
    worse timeout than the out-of-box default, and headroom-multiplied
    (:data:`_LATENCY_HEADROOM`) for reasoning-chain variance. Returns
    :data:`_DEFAULT_GEN_TIMEOUT` when no latencies were observed.
    """
    if not latencies_ms:
        return _DEFAULT_GEN_TIMEOUT
    mean_ms = sum(latencies_ms) / len(latencies_ms)
    timeout = (mean_ms / 1000.0) * _LATENCY_HEADROOM
    # Scale by the output-budget ratio: real generations use max_tokens, the
    # probe measured latency at probed_budget. A 16384-token answer takes far
    # longer than the 2048-token probe that timed it.
    if max_tokens > 0 and probed_budget > 0 and max_tokens > probed_budget:
        scale = min(_MAX_TOKENS_TIMEOUT_SCALE, max_tokens / probed_budget)
        timeout = timeout * scale
    return max(_MIN_GEN_TIMEOUT, int(round(timeout)))


@dataclass
class CalibrationReport:
    """Full output of :func:`run_calibration`: the profile plus per-probe detail
    for the CLI's report. ``ok`` reflects whether reachability succeeded; probes
    that couldn't be tuned are reported but never abort the run."""

    profile: ModelProfile
    results: list[ProbeResult]
    ok: bool


def run_calibration(
    client: Any,
    model_cfg: ModelConfig,
    *,
    run_mechanisms: bool = True,
    run_prompt_profile: bool = True,
    n_reps: int = 1,
    run_phase2: bool = True,
    force_factors: tuple[str, ...] = (),
    task: str | None = None,
    embeddings_model: str = "",
    existing_profile: "ModelProfile | None" = None,
) -> CalibrationReport:
    """Run every probe and assemble a :class:`ModelProfile`.

    Order matters: reachability gates the rest (if the server is down, we still
    return a profile of conservative defaults and ``ok=False`` so the CLI can
    report the failure and exit non-zero without writing). max_tokens is probed
    before json_mode/logprobs/end-to-end so those probes use a tuned budget.

    The mechanism + prompt-rendering selection is now a single **two-phase
    designed experiment** (``probe_two_phase``): a Resolution-IV fractional-
    factorial screening of all factors (prompt axes + mechanism/sampling) →
    focused full-factorial refinement on the top factors. ``run_mechanisms`` /
    ``run_prompt_profile`` both gate this single probe (either False skips the
    whole sweep). Used by ``--dry-run`` so a dry run is a quick capability
    check, not a multi-hour corpus evaluation.

    ``n_reps`` is the replication count for each design point's corpus eval
    (majority vote across reps) — the noise-robustness fix for thinking models.
    Default 1 (single-pass, for fast/stable models).

    ``run_phase2=False`` runs only the Phase-1 screening and reports the factor
    ranking without committing to a Phase-2 selection (keeps existing config) —
    useful on slow models to read which dimensions matter before paying for
    refinement.

    ``existing_profile``: when the sweep is SKIPPED, its choices are seeded from
    this prior profile (when its model matches) so a partial recalibrate
    preserves the known settings rather than silently resetting them.

    ``model_cfg`` is the active config (its ``model``/``base_url``/``api_key``
    identify the target). The returned profile's ``model`` is taken from
    ``model_cfg`` so the runtime overlay matches by name.

    ``embeddings_model`` is the embedding model name for the ``/v1/embeddings``
    probe. On a multi-model server this is the EMBEDDING slot's id/alias (distinct
    from the completion model); passing the completion model name there yields a
    400 "model not found" and a spurious unsupported verdict. Empty = reuse the
    completion model name (single-model server that also embeds).
    """
    results: list[ProbeResult] = []

    # Set the active task-type filter so conflicts_with_context() (called inside
    # evaluate_setting) uses the right task family's corpus. None = the default
    # merge_conflict_resolution corpus (backward compat).
    from capybase.calibration_corpus import set_active_task_type
    set_active_task_type(task)

    reach = probe_reachability(client, model_cfg)
    results.append(reach)
    if not reach.ok:
        # Server unreachable: return a default profile but flag failure so the
        # CLI doesn't persist settings it couldn't actually verify.
        profile = ModelProfile(
            model=model_cfg.model,
            max_tokens=_DEFAULT_MAX_TOKENS,
            json_mode=_DEFAULT_JSON_MODE,
            capture_token_entropy=_DEFAULT_LOGPROBS,
            generation_timeout_seconds=_DEFAULT_GEN_TIMEOUT,
            context_window=0,
            avg_latency_ms=0.0,
            probed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            capybase_version=getattr(capybase, "__version__", ""),
            notes=["calibration aborted: endpoint unreachable"],
        )
        return CalibrationReport(profile=profile, results=results, ok=False)

    mt_result, max_tokens, latencies, first_success = probe_max_tokens(client, model_cfg)
    results.append(mt_result)

    # Context window discovery (cheap GET to /v1/models). 0 = unknown/disabled;
    # never aborts calibration. Done early so the profile carries it.
    cw_result, context_window = probe_context_window(model_cfg)
    results.append(cw_result)

    # Use the tuned budget for the remaining probes so they reflect reality.
    tuned_cfg = model_cfg.model_copy(update={"max_tokens": max_tokens})

    jm_result = probe_json_mode(client, tuned_cfg)
    results.append(jm_result)
    lp_result = probe_logprobs(client, tuned_cfg)
    results.append(lp_result)
    # Embeddings capability (survey §4.2): a quick one-call check, parallel to
    # the logprobs probe. When supported, the profile enables semantic RAG; the
    # BM25 retriever is the fallback otherwise. Cheap, so always run it.
    emb_result = probe_embeddings(tuned_cfg, embeddings_model=embeddings_model)
    results.append(emb_result)
    # End-to-end uses the DETECTED json_mode (not the config default): if the
    # server rejects response_format, exercising it here would only re-prove
    # the failure we already recorded. The e2e probe checks parseability of a
    # real resolution under the settings we'll actually run with. The structured
    # capability probe (§5) runs alongside it: N quick calls measuring JSON
    # success rate, CoT length, and instruction-following — the signals that
    # drive adaptive factor selection (Part 1b) and the early-exit (Part 2).
    e2e_cfg = tuned_cfg.model_copy(update={"json_mode": jm_result.ok})
    e2e_result = probe_end_to_end(client, e2e_cfg)
    results.append(e2e_result)
    cap_result, capabilities = probe_capabilities_detailed(client, e2e_cfg)
    results.append(cap_result)

    # Two-phase designed calibration: a single fractional-factorial screening of
    # all factors (prompt axes + mechanism/sampling) → focused refinement on the
    # top factors. Replaces the prior independent mechanism + prompt-profile
    # A/Bs. Uses the tuned budget + detected json_mode so the eval reflects the
    # settings we'd actually run with. Degrades gracefully (any error leaves the
    # config at defaults/existing); never aborts calibration. Skipped under
    # --dry-run (run_mechanisms/run_prompt_profile both gate it). n_reps makes
    # each design point's score noise-robust (majority vote) for thinking models.
    # The capability signals adapt WHICH factors are screened (Part 1b) and can
    # short-circuit the DOE for near-perfect models (Part 2).
    mech_cfg = e2e_cfg
    choices = MechanismChoices()
    existing_prompt = None
    if existing_profile is not None and existing_profile.model == model_cfg.model:
        q = existing_profile.quality
        choices = MechanismChoices(
            samples=q.samples, two_pass=q.two_pass, plan_search=q.plan_search,
            prompt_variants=q.prompt_variants, diverse_sampling=q.diverse_sampling,
            enable_self_consistency=q.enable_self_consistency,
        )
        existing_prompt = existing_profile.prompt.profile

    from capybase.prompt_profile import DEFAULT_PROFILE as _DEFAULT_PROMPT

    # Early-exit (Part 2): a near-perfect model (high JSON success, follows
    # instructions, not a thinking model) doesn't need the DOE — lock in the
    # cheap baseline and skip the expensive corpus sweep. This turns a strong
    # model's calibration from hours to minutes.
    if (
        run_mechanisms and run_prompt_profile
        and capabilities.is_strong_model
        and not force_factors
    ):
        results.append(ProbeResult(
            "two_phase", ok=False,
            detail="early-exit: model scored near-perfect on capability probe "
                   f"(json_success={capabilities.json_success_rate:.0%}); "
                   "DOE skipped, cheap baseline locked in",
        ))
        prompt_winner = existing_prompt if existing_prompt is not None else _DEFAULT_PROMPT
        mech_result = ProbeResult(
            "mechanisms", ok=False,
            detail="skipped (early-exit: near-perfect model); samples=1, all off",
        )
        results.append(mech_result)
        pp_result = ProbeResult(
            "prompt_profile", ok=(prompt_winner != _DEFAULT_PROMPT),
            detail=f"skipped (early-exit); layout={prompt_winner.output_layout.value}",
        )
        results.append(pp_result)
    elif run_mechanisms and run_prompt_profile:
        tp_result, choices, prompt_winner = probe_two_phase(
            client, mech_cfg,
            existing_choices=choices, existing_profile=existing_prompt,
            n_reps=n_reps, run_phase2=run_phase2,
            capabilities=capabilities, force_factors=force_factors,
        )
        results.append(tp_result)
        mech_result = ProbeResult(
            "mechanisms", ok=(
                choices.samples > 1 or any(
                    getattr(choices, f) for f, _ in _CANDIDATE_MECHANISMS
                )
            ),
            detail=f"samples={choices.samples}, "
                   f"{', '.join(f for f in ('two_pass','plan_search','prompt_variants','diverse_sampling','enable_self_consistency') if getattr(choices, f)) or 'all mechanisms off'}",
        )
        results.append(mech_result)
        pp_result = ProbeResult(
            "prompt_profile",
            ok=(prompt_winner != _DEFAULT_PROMPT),
            detail=f"layout={prompt_winner.output_layout.value}, position={prompt_winner.instruction_position.value}",
        )
        results.append(pp_result)
    else:
        prompt_winner = existing_prompt if existing_prompt is not None else _DEFAULT_PROMPT
        if choices.samples > 1 or any(getattr(choices, f) for f, _ in _CANDIDATE_MECHANISMS):
            mech_result = ProbeResult(
                "mechanisms", ok=False,
                detail=f"skipped (sweep elided); preserved existing choices: "
                       f"samples={choices.samples}, "
                       f"{', '.join(f for f in ('two_pass','plan_search','prompt_variants','diverse_sampling','enable_self_consistency') if getattr(choices, f)) or 'all mechanisms off'}",
            )
        else:
            mech_result = ProbeResult(
                "mechanisms", ok=False,
                detail="skipped (sweep elided); no existing choices to preserve",
            )
        results.append(mech_result)
        pp_result = ProbeResult(
            "prompt_profile", ok=False,
            detail="skipped (--dry-run: prompt-rendering sweep elided)",
        )
        results.append(pp_result)

    notes: list[str] = []
    if not jm_result.ok:
        notes.append("json_mode disabled (server rejected or mishandled response_format)")
    if not lp_result.ok:
        notes.append("logprobs unavailable; capture_token_entropy off")
    if not cw_result.ok:
        notes.append(f"context window not discovered: {cw_result.detail}")
    else:
        notes.append(f"context window {context_window} tokens; prompt trimming enabled")
    if not emb_result.ok:
        notes.append("embeddings unavailable; RAG stays lexical (BM25)")
    else:
        notes.append("embeddings available; semantic RAG enabled")
    if not e2e_result.ok:
        notes.append(f"end-to-end check failed: {e2e_result.detail}")
    if not mech_result.ok:
        notes.append("no mechanism beat the single-sample baseline; all left off")

    avg_ms = sum(latencies) / len(latencies) if latencies else 0.0
    from capybase.calibration_profile import PromptProfileSection
    profile = ModelProfile(
        model=model_cfg.model,
        max_tokens=max_tokens,
        json_mode=jm_result.ok,
        capture_token_entropy=lp_result.ok,
        generation_timeout_seconds=_gen_timeout_from_latency(
            latencies, max_tokens=max_tokens, probed_budget=first_success,
        ),
        context_window=context_window,
        samples=choices.samples,
        two_pass=choices.two_pass,
        plan_search=choices.plan_search,
        prompt_variants=choices.prompt_variants,
        diverse_sampling=choices.diverse_sampling,
        enable_self_consistency=choices.enable_self_consistency,
        enable_embedding_rag=emb_result.ok,
        prompt=PromptProfileSection(profile=prompt_winner),
        avg_latency_ms=round(avg_ms, 1),
        probed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        capybase_version=getattr(capybase, "__version__", ""),
        notes=notes,
    )
    # ``ok`` requires the core knobs to actually have been tuned: max_tokens
    # must have succeeded (reachability already passed). Capability/mechanism
    # probes that come back unsupported are legitimate findings, not failures.
    ok = mt_result.ok
    return CalibrationReport(profile=profile, results=results, ok=ok)

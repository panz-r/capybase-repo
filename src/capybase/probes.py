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
# averaging 60s/answer gets a 120s generation deadline (with the floor applied).
_LATENCY_HEADROOM = 2.0
_MIN_GEN_TIMEOUT = 60  # never tune below this regardless of observed latency

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


def probe_max_tokens(client: Any, model_cfg: ModelConfig) -> tuple[ProbeResult, int, list[float]]:
    """Walk the max_tokens ladder and return (result, best_max_tokens, latencies).

    Success at a rung = ``finish_reason != "length"`` AND the output parses to a
    candidate dict. Stops at the first success (smallest sufficient budget).
    Records wall-clock latency for each successful rung. If no rung succeeds,
    falls back to the built-in default and marks the probe not-ok.
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
        candidates = engine.propose_with_consensus(
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
    """
    choices = MechanismChoices()
    decisions: list[str] = []

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
# Orchestration
# ---------------------------------------------------------------------------


def _gen_timeout_from_latency(latencies_ms: list[float]) -> int:
    """Derive a generation timeout (seconds) from observed latencies.

    Uses the mean successful latency × headroom, floored at ``_MIN_GEN_TIMEOUT``
    so a very fast model doesn't get an impractically tiny deadline. Returns the
    default when no latencies were observed."""
    if not latencies_ms:
        return _DEFAULT_GEN_TIMEOUT
    mean_ms = sum(latencies_ms) / len(latencies_ms)
    timeout = (mean_ms / 1000.0) * _LATENCY_HEADROOM
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
    embeddings_model: str = "",
) -> CalibrationReport:
    """Run every probe and assemble a :class:`ModelProfile`.

    Order matters: reachability gates the rest (if the server is down, we still
    return a profile of conservative defaults and ``ok=False`` so the CLI can
    report the failure and exit non-zero without writing). max_tokens is probed
    before json_mode/logprobs/end-to-end so those probes use a tuned budget.

    ``run_mechanisms=False`` skips the empirical mechanism A/B sweep — the
    expensive phase (resolves the whole corpus ~14×). Used by ``--dry-run`` so a
    dry run is a quick capability check, not a multi-hour corpus evaluation.

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

    mt_result, max_tokens, latencies = probe_max_tokens(client, model_cfg)
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
    # real resolution under the settings we'll actually run with.
    e2e_cfg = tuned_cfg.model_copy(update={"json_mode": jm_result.ok})
    e2e_result = probe_end_to_end(client, e2e_cfg)
    results.append(e2e_result)

    # Mechanism calibration: empirically A/B-select resolution strategies on the
    # blessed corpus. Uses the tuned budget + detected json_mode so the eval
    # reflects the settings we'd actually run with. Degrades gracefully (any
    # error leaves mechanisms at defaults); never aborts calibration. Skipped
    # when run_mechanisms=False (the expensive corpus sweep; --dry-run elides it).
    mech_cfg = e2e_cfg
    choices = MechanismChoices()
    if run_mechanisms:
        mech_result, choices = probe_mechanisms(client, model_cfg, base_cfg=mech_cfg)
    else:
        mech_result = ProbeResult(
            "mechanisms", ok=False,
            detail="skipped (--dry-run: capability-only, mechanism sweep elided)",
        )
    results.append(mech_result)

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
    profile = ModelProfile(
        model=model_cfg.model,
        max_tokens=max_tokens,
        json_mode=jm_result.ok,
        capture_token_entropy=lp_result.ok,
        generation_timeout_seconds=_gen_timeout_from_latency(latencies),
        context_window=context_window,
        samples=choices.samples,
        two_pass=choices.two_pass,
        plan_search=choices.plan_search,
        prompt_variants=choices.prompt_variants,
        diverse_sampling=choices.diverse_sampling,
        enable_self_consistency=choices.enable_self_consistency,
        enable_embedding_rag=emb_result.ok,
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

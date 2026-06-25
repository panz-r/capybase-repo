"""Resolution engine: candidate generator over the model adapter.

``propose`` returns a *list* of ``CandidateResolution`` even in the MVP
(samples=1) so that self-consistency is a parameter change rather than an
architectural one. Every prompt has a stable version string so prompt
versions can be compared in offline eval and recorded in training data.

Prompt versions::

    resolve_text_block.v1   — initial resolution request
    cegis_retry.v1          — retry with concrete validator feedback
"""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable

from capybase.adapters.llm_openai import (
    LLMClient,
    LLMResponse,
    OpenAICompatibleClient,
    coerce_candidate_dict,
)
from capybase.conflict_model import (
    CandidateResolution,
    ConflictUnit,
    ContextBundle,
    VerificationFailure,
)
from capybase.config import ModelConfig
from capybase.consensus import ConsensusReport, rank_by_consensus

PROMPT_RESOLVE = "resolve_text_block.v5"
PROMPT_RETRY = "cegis_retry.v5"
# Two-pass prompting (Step 2): intent extraction then code generation.
PROMPT_INTENT = "intent.v1"
PROMPT_CODE = "code_from_intent.v1"
# Targeted repair (Step 4): send back the broken candidate for surgical fixing.
PROMPT_REPAIR = "cegis_repair.v1"


def _prompt_sides(unit: ConflictUnit) -> tuple[str, str, str]:
    """Return the conflict sides to show in the prompt.

    Prefers the diff3-minimized sides (``unit.refined_sides``) so the model
    sees the smallest possible conflict window — adjacent non-conflicting lines
    that the worktree markers still wrap are stripped. Falls back to the raw
    marker sides when no refinement is recorded. Returns
    ``(current, base, replayed)``.
    """
    refined = unit.refined_sides
    if refined is not None:
        return refined
    return unit.current.text, unit.base.text, unit.replayed.text


def build_resolve_prompt(unit: ConflictUnit, context: ContextBundle) -> str:
    # Show a visible marker so the model can see the exact indentation it must
    # reproduce (leading spaces are invisible in normal prose).
    # Prefer the diff3-minimized sides when available (Step 1: shrink the
    # conflict window so the model isn't distracted by adjacent non-conflicting
    # lines). Falls back to the raw marker sides.
    cur_lines, base_lines, rep_lines = _prompt_sides(unit)
    sv = context.structural_view
    enc_sig = sv.get("enclosing_node_signature") if sv else None
    enc_text = sv.get("enclosing_node_text") if sv else None
    # Structural anchor: when tree-sitter resolved the enclosing definition,
    # show it so the model knows the logical block it is merging inside (e.g.
    # "def greet()") — sharper than inferring from indentation alone.
    structural_anchor = ""
    if enc_sig and enc_text:
        structural_anchor = f"""Logical block you are merging inside (tree-sitter AST):
{enc_sig}
{enc_text}

"""
    # RAG few-shot: when past similar merges were retrieved from the experience
    # store, show them as demonstrations so the model learns this codebase's
    # merge conventions dynamically. Each example shows the three sides and the
    # accepted resolution.
    few_shot = ""
    if context.retrieved_examples:
        blocks = []
        for i, ex in enumerate(context.retrieved_examples, 1):
            blocks.append(
                f"Example {i}:\n"
                f"  CURRENT: {ex.current}\n"
                f"  REPLAYED: {ex.replayed}\n"
                f"  RESOLVED: {ex.resolved}"
            )
        few_shot = "Similar past merges (for reference — match this style):\n" + "\n".join(blocks) + "\n\n"
    return f"""Resolve ONE git merge conflict by merging BOTH sides into one coherent
result preserving each side's intent. Be CONCISE: reason in a few sentences,
then answer. Do not over-explain.

file: {unit.path}
language: {unit.language or 'unknown'}

{structural_anchor}{few_shot}CURRENT_UPSTREAM_SIDE body (exact, including leading spaces):
{cur_lines}

REPLAYED_COMMIT_SIDE body (exact, including leading spaces):
{rep_lines}

BASE (common ancestor) body, for context:
{base_lines}

Surrounding file context:
{context.primary_text}

Your resolved_text REPLACES the whole conflict marker block (``<<<<<<<``
through ``>>>>>>>``) and is spliced in verbatim. End with ONE ```json fenced
object having EXACTLY these keys:

```json
{{
  "resolved_text": "<merged replacement text>",
  "current_side_intent": ["..."],
  "replayed_commit_intent": ["..."],
  "preserved_current_side": true,
  "preserved_replayed_commit_side": true,
  "dropped_current_side_details": [],
  "dropped_replayed_commit_details": [],
  "assumptions": [],
  "needs_human": false,
  "self_reported_confidence": 0.0,
  "explanation": "one short sentence"
}}
```

CRITICAL rules:
- PRESERVE leading indentation. If the bodies start with 4 spaces, EVERY line
  of resolved_text must start with 4 spaces. Getting this wrong causes a syntax
  error and rejection.
- No conflict markers (``<<<<<<<`` / ``=======`` / ``>>>>>>>``).
- Do not add or change the enclosing def/class line.
- Escape newlines as \\n and double quotes as \\" inside resolved_text.
- Output the ```json block last; nothing after it.
- If you cannot merge safely, set needs_human=true and explain.
"""


def build_retry_prompt(
    unit: ConflictUnit,
    context: ContextBundle,
    failures: Iterable[VerificationFailure],
) -> str:
    feedback = "\n".join(_render_failure(f) for f in failures) or "- (no specific failures reported)"
    return f"""Your previous merge attempt was rejected. Fix it.

{build_resolve_prompt(unit, context)}

### validator feedback (previous attempt failed these checks)
{feedback}

Address every failure above; do not repeat the mistake. End with the ```json
fenced answer as instructed.
"""


def build_repair_prompt(
    unit: ConflictUnit,
    context: ContextBundle,
    candidate: CandidateResolution,
    failures: Iterable[VerificationFailure],
) -> str:
    """Targeted repair: send the broken candidate back for surgical fixing.

    Unlike ``build_retry_prompt`` (full regeneration from scratch), this
    includes the previous attempt's ``resolved_text`` verbatim so the model can
    fix the specific error locally rather than re-deriving the whole merge. A
    3B model is highly capable of fixing its own minor errors (missing bracket,
    wrong indentation) when shown the exact code + the exact error.
    """
    feedback = "\n".join(_render_failure(f) for f in failures) or "- (no specific failures reported)"
    cur_lines, _base_lines, rep_lines = _prompt_sides(unit)
    return f"""Your previous merge attempt had errors. Fix the SPECIFIC errors in
your code below — do not rewrite from scratch unless necessary. Keep all parts
that were correct; change only what the validator flagged.

file: {unit.path}
language: {unit.language or 'unknown'}

CURRENT_UPSTREAM_SIDE body:
{cur_lines}

REPLAYED_COMMIT_SIDE body:
{rep_lines}

YOUR PREVIOUS ATTEMPT (needs fixing):
{candidate.resolved_text}

### validator feedback (fix these specific issues)
{feedback}

Output the corrected resolved_text as a ```json fenced object:
{{
  "resolved_text": "<the fixed replacement text, exact indentation>",
  "explanation": "<what you changed and why>",
  "self_reported_confidence": 0.0
}}
"""


def _render_failure(f: VerificationFailure) -> str:
    """Render a failure richly, surfacing structured counterexample detail.

    Validators populate ``VerificationFailure.detail`` with structured state
    (e.g. the exact syntax-error line/column, the AST fingerprint diff, the
    LSP diagnostic range). Rendering it here gives the model a concrete
    counterexample to fix, rather than a bare message — this is the core of
    CEGIS: the counterexample guides the next synthesis attempt.
    """
    parts = [f"- [{f.validator}] {f.message}"]
    if f.detail:
        for key, val in f.detail.items():
            # Truncate long values (e.g. full AST fingerprints) to keep the
            # prompt focused on the actionable signal.
            sval = str(val)
            if len(sval) > 200:
                sval = sval[:200] + " …"
            parts.append(f"    {key}: {sval}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Two-pass prompting (Step 2): intent extraction → code generation
# ---------------------------------------------------------------------------


def build_intent_prompt(unit: ConflictUnit, context: ContextBundle) -> str:
    """Pass 1: extract semantic intents only. No code generation.

    A 3B model reasons better when asked to *understand* the conflict before
    *fixing* it. This request is small and fast — it asks only for a JSON list
    of what each side changed. The result becomes a "reasoning map" that guides
    the code-generation pass.
    """
    cur_lines, base_lines, rep_lines = _prompt_sides(unit)
    return f"""Analyze this git merge conflict and state what EACH side changed
relative to the base. Output ONLY a JSON object with two string-list fields.
Do NOT write code.

file: {unit.path}
language: {unit.language or 'unknown'}

CURRENT_UPSTREAM_SIDE (stage 2):
{cur_lines}

REPLAYED_COMMIT_SIDE (stage 3):
{rep_lines}

BASE (common ancestor):
{base_lines}

Output this JSON (```json fenced):
{{
  "current_side_intent": ["what the upstream/current side changed", ...],
  "replayed_commit_intent": ["what the local/replayed side changed", ...]
}}
"""


def build_code_prompt(
    unit: ConflictUnit,
    context: ContextBundle,
    intents: dict[str, list[str]],
) -> str:
    """Pass 2: generate code conditioned on the extracted intent map.

    The model sees its own prior reasoning (the intents) and is asked to merge
    BOTH sides into one coherent result guided by that understanding. This is
    the same output schema as the single-pass resolve prompt, but the intent
    context primes the model toward a correct synthesis.
    """
    cur_intents = intents.get("current_side_intent", [])
    rep_intents = intents.get("replayed_commit_intent", [])
    cur_lines, _base_lines, rep_lines = _prompt_sides(unit)
    sv = context.structural_view
    enc_sig = sv.get("enclosing_node_signature") if sv else None
    structural_anchor = ""
    if enc_sig:
        structural_anchor = f"Merging inside: {enc_sig}\n\n"
    return f"""Resolve ONE git merge conflict by merging BOTH sides into one
coherent result. Be CONCISE. A prior analysis identified these intents:

Upstream/current side changed:
{json.dumps(cur_intents, indent=2)}

Replayed/local side changed:
{json.dumps(rep_intents, indent=2)}

{structural_anchor}CURRENT_UPSTREAM_SIDE body (exact, including leading spaces):
{cur_lines}

REPLAYED_COMMIT_SIDE body (exact, including leading spaces):
{rep_lines}

Output a ```json fenced object:
{{
  "resolved_text": "<the merged replacement text, exact indentation>",
  "explanation": "<one sentence>",
  "self_reported_confidence": 0.0,
  "preserved_current_side": true,
  "preserved_replayed_commit_side": true
}}
"""


class ResolutionEngine:
    def __init__(
        self,
        config: ModelConfig,
        *,
        client: LLMClient | None = None,
    ) -> None:
        self.config = config
        self.client = client or OpenAICompatibleClient(config)

    def propose(
        self,
        unit: ConflictUnit,
        context: ContextBundle,
        *,
        failures: list[VerificationFailure] | None = None,
        prev_candidate: CandidateResolution | None = None,
    ) -> list[CandidateResolution]:
        """Generate one or more candidates for ``unit``.

        ``failures`` is non-empty on retry; the retry prompt feeds them back
        (CEGIS-style). When ``prev_candidate`` is also given (the failed
        attempt), the targeted *repair* prompt is used — it includes the broken
        code so the model fixes locally rather than regenerating from scratch.
        The number of samples comes from config so self-consistency is enabled
        by raising ``samples``.
        """
        if failures and prev_candidate and prev_candidate.resolved_text:
            prompt_version = PROMPT_REPAIR
            prompt = build_repair_prompt(unit, context, prev_candidate, failures)
        elif failures:
            prompt_version = PROMPT_RETRY
            prompt = build_retry_prompt(unit, context, failures)
        else:
            prompt_version = PROMPT_RESOLVE
            prompt = build_resolve_prompt(unit, context)
        candidates: list[CandidateResolution] = []
        n = max(1, self.config.samples)
        # Single sample or no parallelism: sequential (fast path, no overhead).
        if n == 1 or not self.config.parallel_samples:
            for _ in range(n):
                cand = self._one(unit, context, prompt, prompt_version)
                candidates.append(cand)
        else:
            # Draw samples concurrently in a thread pool. Each _one() call is a
            # blocking HTTP request; running them in parallel turns N×latency
            # into ~1×latency. Safe because the adapter is stateless per-call.
            candidates = self._sample_parallel(
                unit, context, prompt, prompt_version, n,
                temperature_override=self.config.sampling_temperature,
            )
        return candidates

    def _sample_parallel(
        self,
        unit: ConflictUnit,
        context: ContextBundle,
        prompt: str,
        prompt_version: str,
        n: int,
        *,
        temperature_override: float | None = None,
    ) -> list[CandidateResolution]:
        """Draw N samples: prefer one server-side ``n`` request, fall back to a
        thread pool.

        Step 2 (parallel sampling): a single request with ``n=N`` lets the
        server batch all samples in one round-trip — critical on a single-GPU
        llama-server where N concurrent requests serialize to one batch slot
        and pay N× scheduling overhead. When the client supports
        ``complete_many`` AND returns all N choices, we use them; otherwise we
        fall back to N concurrent ``complete`` calls (the original behavior).

        When ``diverse_sampling`` is enabled we bypass the batched path: the
        server draws all N choices at ONE temperature, so per-sample
        temperature diversity (survey §4.1) requires N separate requests.
        Diversity beats batching efficiency for correctness, so the thread
        pool is used with a per-sample temperature portfolio.
        """
        temps = self._sample_temperatures(n, temperature_override)
        # Only the thread-pool path supports per-sample temperatures; when all
        # temps are equal (diverse_sampling off, or N==1) try the batched path.
        if len(set(temps)) <= 1:
            candidates = self._sample_n(
                unit, prompt, prompt_version, n, temperature_override=temperature_override
            )
            if candidates is not None:
                return candidates
        # Fallback / diverse path: thread pool of independent requests.
        with ThreadPoolExecutor(max_workers=min(n, 8)) as pool:
            futures = [
                pool.submit(self._one, unit, context, prompt, prompt_version, t)
                for t in temps
            ]
            return [f.result() for f in futures]

    def _sample_temperatures(
        self, n: int, temperature_override: float | None = None
    ) -> list[float]:
        """Build the per-sample temperature portfolio (survey §4.1).

        When ``diverse_sampling`` is off (default), every sample uses the same
        temperature (the override, or the base) — returned as a list so callers
        can detect uniformity and try the batched ``n`` path.

        When on, the portfolio splits N into exploratory samples at the higher
        ``sampling_temperature`` and conservative samples at the lower base
        ``temperature``, guaranteeing at least one of each for N >= 2. This
        gives diversity (high-temp explores) AND a reliable fallback
        (low-temp stays close to a safe answer) — on a 3B model it raises the
        odds that at least one sample is both valid and distinct.
        """
        if n <= 1 or not getattr(self.config, "diverse_sampling", False):
            t = temperature_override if temperature_override is not None else self.config.temperature
            return [t] * n
        high = self.config.sampling_temperature
        low = self.config.temperature
        if high <= low:
            # No diversity to exploit (misconfigured); fall back to uniform.
            return [temperature_override if temperature_override is not None else low] * n
        # Split roughly in half: ceil(n/2) exploratory (high), rest conservative.
        n_high = (n + 1) // 2
        n_low = n - n_high
        return [high] * n_high + [low] * n_low


    def _sample_n(
        self,
        unit: ConflictUnit,
        prompt: str,
        prompt_version: str,
        n: int,
        *,
        temperature_override: float | None = None,
    ) -> list[CandidateResolution] | None:
        """Server-side N sampling via ``complete_many``.

        Returns None when the client lacks ``complete_many`` or the server
        ignored ``n`` (returned fewer than ``n`` choices) — the caller then
        falls back to the thread pool. This keeps the optimization transparent
        and safe: any server that doesn't support ``n`` simply yields the
        original behavior.
        """
        complete_many = getattr(self.client, "complete_many", None)
        if not callable(complete_many):
            return None
        messages = [
            {"role": "system", "content": "You are a careful merge-resolution assistant."},
            {"role": "user", "content": prompt},
        ]
        temperature = (
            temperature_override
            if temperature_override is not None
            else self.config.temperature
        )
        try:
            responses = complete_many(
                messages,
                model=self.config.model,
                temperature=temperature,
                max_tokens=self.config.max_tokens,
                json_mode=True,
                n=n,
            )
        except Exception as exc:  # noqa: BLE001 - fall back to thread pool
            return None
        # complete_many is duck-typed (getattr above); coerce defensively.
        responses = list(responses) if responses is not None else []
        if len(responses) < n:
            # Server ignored/doesn't support n — not enough samples returned.
            return None
        return [
            self._candidate_from_response(unit, prompt_version, resp)
            for resp in responses
        ]

    def propose_with_consensus(
        self,
        unit: ConflictUnit,
        context: ContextBundle,
        *,
        failures: list[VerificationFailure] | None = None,
    ) -> tuple[list[CandidateResolution], ConsensusReport | None]:
        """Generate N samples and reorder so the majority winner is first.

        When ``samples <= 1`` there is no voting to do; this returns the single
        candidate unchanged with a trivial report. Otherwise the candidates are
        normalized and clustered; the largest cluster's representative is moved
        to index 0 so the orchestrator's ``candidates[0]`` takes the consensus
        winner. The report (agreement score, cluster count) is returned for
        journaling and as a risk signal — low agreement flags an uncertain
        merge.
        """
        candidates = self.propose(unit, context, failures=failures)
        if len(candidates) <= 1:
            return candidates, None
        ordered, report = rank_by_consensus(candidates, unit.language)
        return ordered, report

    def propose_two_pass(
        self,
        unit: ConflictUnit,
        context: ContextBundle,
        *,
        n_samples: int = 1,
        temperature: float | None = None,
    ) -> list[CandidateResolution]:
        """Two-pass generation: extract intents, then generate code.

        Pass 1 (intent): one cheap request asking only for semantic intents.
        Pass 2 (code): N samples at raised temperature, each conditioned on the
        same intent map. The model generates code guided by its own prior
        reasoning — a 3B model reasons better when it understands the conflict
        before trying to fix it.

        If the intent pass fails, falls back to single-pass ``propose`` so the
        pipeline degrades gracefully.
        """
        # --- Pass 1: extract intents ---
        intents = self._call_intent(unit, context)
        if intents is None:
            # Intent pass failed — degrade to single-pass.
            return self.propose(unit, context)
        # --- Pass 2: generate code conditioned on intents ---
        code_prompt = build_code_prompt(unit, context, intents)
        n = max(1, n_samples)
        temp = temperature if temperature is not None else self.config.sampling_temperature
        if n == 1:
            return [self._one(unit, context, code_prompt, PROMPT_CODE, temp)]
        if not self.config.parallel_samples:
            return [self._one(unit, context, code_prompt, PROMPT_CODE, temp) for _ in range(n)]
        return self._sample_parallel(
            unit, context, code_prompt, PROMPT_CODE, n,
            temperature_override=temp,
        )

    def _call_intent(
        self, unit: ConflictUnit, context: ContextBundle
    ) -> dict[str, list[str]] | None:
        """Pass 1: extract semantic intents via a dedicated lightweight call.

        Unlike ``_one`` (which expects ``resolved_text``), this call parses the
        intent JSON directly — the response has ``current_side_intent`` and
        ``replayed_commit_intent`` fields, not code. Returns None on any failure.
        """
        intent_prompt = build_intent_prompt(unit, context)
        messages = [
            {"role": "system", "content": "You are a careful merge-analysis assistant."},
            {"role": "user", "content": intent_prompt},
        ]
        try:
            resp = self.client.complete(
                messages,
                model=self.config.model,
                temperature=self.config.temperature,
                max_tokens=min(self.config.max_tokens, 2048),
                json_mode=True,
            )
        except Exception:  # noqa: BLE001
            return None
        try:
            parsed, _warnings = coerce_candidate_dict(resp.text)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(parsed, dict):
            return None
        cur = parsed.get("current_side_intent", [])
        rep = parsed.get("replayed_commit_intent", [])
        if not cur and not rep:
            return None
        return {
            "current_side_intent": list(cur) if isinstance(cur, list) else [str(cur)],
            "replayed_commit_intent": list(rep) if isinstance(rep, list) else [str(rep)],
        }

    def _one(
        self,
        unit: ConflictUnit,
        context: ContextBundle,
        prompt: str,
        prompt_version: str,
        temperature_override: float | None = None,
    ) -> CandidateResolution:
        messages = [
            {"role": "system", "content": "You are a careful merge-resolution assistant."},
            {"role": "user", "content": prompt},
        ]
        temperature = (
            temperature_override
            if temperature_override is not None
            else self.config.temperature
        )
        try:
            resp: LLMResponse = self.client.complete(
                messages,
                model=self.config.model,
                temperature=temperature,
                max_tokens=self.config.max_tokens,
                json_mode=True,
            )
        except Exception as exc:  # noqa: BLE001 - degrade to retryable failure
            return _failed_candidate(
                unit, self.config.model, prompt_version, str(exc), "",
                failure_kind="request_failed",
            )
        return self._candidate_from_response(unit, prompt_version, resp)

    def _candidate_from_response(
        self, unit: ConflictUnit, prompt_version: str, resp: LLMResponse
    ) -> CandidateResolution:
        """Build a CandidateResolution from a single LLMResponse.

        Shared by ``_one`` (thread-pool path) and ``_sample_n`` (server-side
        N sampling) so every sample is validated identically regardless of how
        it was drawn. Detects truncation (finish_reason=length) and parse
        failures, mapping them to retryable failure_kinds.
        """
        meta = resp.raw or {}
        finish = ""
        if isinstance(meta, dict):
            acc = meta.get("_accumulated")
            if isinstance(acc, dict):
                finish = acc.get("finish_reason") or ""
            if not finish:
                choices = meta.get("choices") or []
                if choices:
                    finish = choices[0].get("finish_reason") or ""
        if finish == "length":
            return _failed_candidate(
                unit, self.config.model, prompt_version,
                "model output truncated (finish_reason=length); increase max_tokens",
                resp.text, failure_kind="truncated",
            )
        data, warnings = coerce_candidate_dict(resp.text)
        if not data or "resolved_text" not in data:
            warnings = warnings or ["response missing resolved_text"]
            return _failed_candidate(
                unit, self.config.model, prompt_version,
                "could not parse resolution", resp.text, warnings,
                failure_kind="parse_failed",
            )
        needs_human = bool(data.get("needs_human", False))
        return CandidateResolution(
            candidate_id=f"{unit.unit_id}:{uuid.uuid4().hex[:6]}",
            unit_id=unit.unit_id,
            model_name=self.config.model,
            prompt_version=prompt_version,
            current_side_intent=list(data.get("current_side_intent", [])),
            replayed_commit_intent=list(data.get("replayed_commit_intent", [])),
            resolved_text=str(data.get("resolved_text", "")),
            explanation=str(data.get("explanation", "")),
            preserved_current_side=bool(data.get("preserved_current_side", True)),
            preserved_replayed_commit_side=bool(
                data.get("preserved_replayed_commit_side", True)
            ),
            dropped_current_side_details=list(data.get("dropped_current_side_details", [])),
            dropped_replayed_commit_details=list(data.get("dropped_replayed_commit_details", [])),
            assumptions=list(data.get("assumptions", [])),
            needs_human=needs_human,
            self_reported_confidence=float(data.get("self_reported_confidence", 0.0)),
            raw_response=resp.text,
            parse_warnings=warnings,
            # A genuine model refusal (it answered JSON but said needs_human).
            failure_kind="model_refusal" if needs_human else "",
        )



def _failed_candidate(
    unit: ConflictUnit,
    model_name: str,
    prompt_version: str,
    reason: str,
    raw: str,
    warnings: list[str] | None = None,
    *,
    failure_kind: str = "request_failed",
) -> CandidateResolution:
    return CandidateResolution(
        candidate_id=f"{unit.unit_id}:{uuid.uuid4().hex[:6]}",
        unit_id=unit.unit_id,
        model_name=model_name,
        prompt_version=prompt_version,
        resolved_text="",
        explanation=reason,
        needs_human=True,
        raw_response=raw,
        parse_warnings=warnings or [reason],
        failure_kind=failure_kind,
    )

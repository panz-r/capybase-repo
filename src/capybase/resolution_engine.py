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

import uuid
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

PROMPT_RESOLVE = "resolve_text_block.v3"
PROMPT_RETRY = "cegis_retry.v3"


def build_resolve_prompt(unit: ConflictUnit, context: ContextBundle) -> str:
    return f"""You resolve ONE git merge conflict by merging BOTH sides into one coherent
result that preserves each side's intent. Do NOT simply pick one side.

file: {unit.path}
language: {unit.language or 'unknown'}

The conflict block's CURRENT_UPSTREAM_SIDE body is exactly:
{unit.current.text}

The conflict block's REPLAYED_COMMIT_SIDE body is exactly:
{unit.replayed.text}

The BASE (common ancestor) body, for context, was:
{unit.base.text}

The surrounding file context (the conflict markers are in here):
{context.primary_text}

Your resolved_text REPLACES the entire conflict marker block (the lines from
``<<<<<<<`` through ``>>>>>>>``, inclusive). It will be spliced in verbatim.

Think briefly if you must, then output your final answer as ONE JSON object
inside a fenced ```json block with EXACTLY these keys:

```json
{{
  "resolved_text": "<the merged replacement text>",
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

Rules:
- resolved_text must be valid {unit.language or 'code'} in context. PRESERVE the
  exact leading indentation of the lines you are merging (e.g. if the bodies
  are indented 4 spaces, every line of resolved_text must start with 4 spaces).
- Do NOT include conflict markers (``<<<<<<<``, ``=======``, ``>>>>>>>``).
- Do NOT add or change the enclosing function/class definition line.
- Escape newlines in resolved_text as \\n and double quotes as \\".
- Output the ```json fence and nothing after it.
- If you cannot merge safely, set needs_human=true and explain.
"""


def build_retry_prompt(
    unit: ConflictUnit,
    context: ContextBundle,
    failures: Iterable[VerificationFailure],
) -> str:
    feedback = "\n".join(
        f"- [{f.validator}] {f.message}" for f in failures
    ) or "- (no specific failures reported)"
    return f"""Your previous merge attempt was rejected. Fix it.

{build_resolve_prompt(unit, context)}

### validator feedback (previous attempt failed these checks)
{feedback}

Address every failure above; do not repeat the mistake. End with the ```json
fenced answer as instructed.
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
    ) -> list[CandidateResolution]:
        """Generate one or more candidates for ``unit``.

        ``failures`` is non-empty on retry; the retry prompt feeds them back
        (CEGIS-style). The number of samples comes from config so
        self-consistency is enabled by raising ``samples``.
        """
        prompt_version = PROMPT_RETRY if failures else PROMPT_RESOLVE
        prompt = (
            build_retry_prompt(unit, context, failures)
            if failures
            else build_resolve_prompt(unit, context)
        )
        candidates: list[CandidateResolution] = []
        for _ in range(max(1, self.config.samples)):
            cand = self._one(unit, context, prompt, prompt_version)
            candidates.append(cand)
        return candidates

    def _one(
        self,
        unit: ConflictUnit,
        context: ContextBundle,
        prompt: str,
        prompt_version: str,
    ) -> CandidateResolution:
        messages = [
            {"role": "system", "content": "You are a careful merge-resolution assistant."},
            {"role": "user", "content": prompt},
        ]
        try:
            resp: LLMResponse = self.client.complete(
                messages,
                model=self.config.model,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
                json_mode=True,
            )
        except Exception as exc:  # noqa: BLE001 - degrade to escalation
            return _failed_candidate(unit, self.config.model, prompt_version, str(exc), "")

        data, warnings = coerce_candidate_dict(resp.text)
        if not data or "resolved_text" not in data:
            warnings = warnings or ["response missing resolved_text"]
            return _failed_candidate(
                unit, self.config.model, prompt_version,
                "could not parse resolution", resp.text, warnings,
            )
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
            needs_human=bool(data.get("needs_human", False)),
            self_reported_confidence=float(data.get("self_reported_confidence", 0.0)),
            raw_response=resp.text,
            parse_warnings=warnings,
        )


def _failed_candidate(
    unit: ConflictUnit,
    model_name: str,
    prompt_version: str,
    reason: str,
    raw: str,
    warnings: list[str] | None = None,
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
    )

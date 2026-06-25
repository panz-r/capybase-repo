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
from capybase.consensus import ConsensusReport, rank_by_consensus

PROMPT_RESOLVE = "resolve_text_block.v5"
PROMPT_RETRY = "cegis_retry.v5"


def build_resolve_prompt(unit: ConflictUnit, context: ContextBundle) -> str:
    # Show a visible marker so the model can see the exact indentation it must
    # reproduce (leading spaces are invisible in normal prose).
    cur_lines = unit.current.text
    rep_lines = unit.replayed.text
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
    return f"""Resolve ONE git merge conflict by merging BOTH sides into one coherent
result preserving each side's intent. Be CONCISE: reason in a few sentences,
then answer. Do not over-explain.

file: {unit.path}
language: {unit.language or 'unknown'}

{structural_anchor}CURRENT_UPSTREAM_SIDE body (exact, including leading spaces):
{cur_lines}

REPLAYED_COMMIT_SIDE body (exact, including leading spaces):
{rep_lines}

BASE (common ancestor) body, for context:
{unit.base.text}

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
        except Exception as exc:  # noqa: BLE001 - degrade to retryable failure
            return _failed_candidate(
                unit, self.config.model, prompt_version, str(exc), "",
                failure_kind="request_failed",
            )

        # Detect truncation: reasoning models can exhaust the token budget
        # mid-thought (finish_reason=length), never emitting their answer.
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

"""Core data model for capybase.

Every downstream subsystem (context building, resolution, verification,
risk, journal) consumes these types. They are intentionally richer than the
MVP needs so that structural merge, RAG, verifier models, and calibrated
risk can be added later without changing the orchestrator's contracts.

The cardinal invariant::

    A ConflictUnit becomes one or more CandidateResolutions;
    validators produce VerificationResults;
    risk policy chooses accept/retry/escalate;
    only the orchestrator mutates Git.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Conflict domain
# ---------------------------------------------------------------------------

SideLabel = Literal[
    "BASE",
    "CURRENT_UPSTREAM_SIDE",
    "REPLAYED_COMMIT_SIDE",
]
UnitKind = Literal[
    "text_marker_block",
    "ast_region",
    "config_key",
    "import_block",
    "generated_file",
    "lockfile",
    "whole_file",
]


class ConflictSide(BaseModel):
    """One side of a three-way merge: base, current (upstream), or replayed."""

    label: SideLabel
    text: str
    blob_oid: str | None = None


class RelatedSnippet(BaseModel):
    path: str
    text: str
    reason: str = ""


class HistoricalExample(BaseModel):
    """A previously journaled resolution, usable as a retrieved example."""

    summary: str
    base: str
    current: str
    replayed: str
    resolved: str
    source: str = ""


class ConflictUnit(BaseModel):
    """A single resolvable conflict.

    In the MVP ``unit_kind`` is always ``text_marker_block`` and the unit
    corresponds to one ``<<<<<<< ... >>>>>>>`` block in the worktree file.
    Later it may originate from an AST region, a config key, an import block,
    a generated file, or a whole-file conflict — the downstream pipeline does
    not care.
    """

    session_id: str
    step_index: int
    path: str
    language: str | None = None
    conflict_type: str = "UU"  # git unmerged mode

    unit_id: str
    unit_kind: UnitKind = "text_marker_block"

    base: ConflictSide
    current: ConflictSide
    replayed: ConflictSide

    original_worktree_text: str
    # Inclusive 0-based [start_line, end_line] span of the marker block within
    # ``original_worktree_text`` (lines). None for non-marker units.
    marker_span: tuple[int, int] | None = None

    enclosing_symbol: str | None = None
    structural_metadata: dict[str, Any] = Field(default_factory=dict)
    risk_tags: list[str] = Field(default_factory=list)
    # Graded severity computed at extraction from cheap pre-LLM
    # signals (hunk size, definition-touching, both-sides-changed-same-lines).
    # Distinct from risk_tags (validator-added violation names): this is a
    # pre-resolution triage signal for routing/escalation/attribution. Computed
    # by ``compute_severity`` so it's a pure function of already-extracted data.
    severity: Literal["low", "medium", "high"] = "medium"

    @property
    def refined_sides(self) -> tuple[str, str, str] | None:
        """The diff3-minimized conflict sides, if available.

        ``_refine_with_diff3`` (conflict_extractor) runs ``git merge-file
        --diff3`` on the stage blobs to find the tightest possible conflict
        boundaries — adjacent non-conflicting lines that the worktree markers
        still include are stripped. When that tighter view exists, prompt
        builders should prefer it over the raw marker sides so a small model
        sees a minimal conflict window. Returns ``(current, base, replayed)``
        or None when no refinement is recorded (the raw sides are the truth).

        Advisory only: splicing uses ``marker_span`` /
        ``original_worktree_text`` and is unaffected.
        """
        refined = self.structural_metadata.get("diff3_refined")
        if not refined:
            return None
        try:
            return (
                refined.get("current", ""),
                refined.get("base", ""),
                refined.get("replayed", ""),
            )
        except (AttributeError, TypeError):
            return None


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


class TokenBudget(BaseModel):
    """The input-token budget for one LLM resolve prompt.

    ``total`` is the model's context window (0 = disabled → no enforcement,
    the historical default). ``reserved_for_completion`` is held back for the
    model's answer. :attr:`available` is what the prompt's INPUT must fit in;
    the prompt builder trims augmentation sections (few-shot, deps, surrounding
    context) to stay within it, always protecting the conflict sides + JSON
    contract.

    Built via :meth:`from_config` from a ``ModelConfig`` (whose
    ``context_window``/``completion_reserve`` fields), or constructed directly
    for tests.
    """

    total: int = 0
    reserved_for_completion: int = 1024

    @property
    def available(self) -> int:
        # total == 0 means "no window configured" → available 0 signals "do not
        # enforce" to the prompt builder (it short-circuits, current behavior).
        if self.total <= 0:
            return 0
        return max(0, self.total - self.reserved_for_completion)

    @property
    def enabled(self) -> bool:
        """True iff a context window is configured and enforcement is active."""
        return self.total > 0

    @classmethod
    def from_config(cls, model_cfg: Any) -> "TokenBudget":
        """Build a budget from a ``ModelConfig``'s window/reserve fields."""
        return cls(
            total=int(getattr(model_cfg, "context_window", 0) or 0),
            reserved_for_completion=int(getattr(model_cfg, "completion_reserve", 1024) or 1024),
        )


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars/token, minimum 1 for any non-empty text.

    A conservative heuristic (real tokenizers vary 3-4 chars/token by language).
    Erring slightly high means we trim a touch more than strictly necessary,
    which is the safe direction (an over-long prompt truncates silently on the
    server side; a slightly-trimmed prompt just loses a little augmentation).
    """
    return max(1, len(text) // 4) if text else 0


class ContextBundle(BaseModel):
    """Everything a resolver sees about a conflict.

    The MVP fills only ``primary_text`` and ``token_estimate``; the other
    fields exist so program slicing, RAG, and AST views can be added without
    changing the resolver signature.
    """

    primary_text: str
    side_summaries: dict[str, str] = Field(default_factory=dict)
    related_snippets: list[RelatedSnippet] = Field(default_factory=list)
    retrieved_examples: list[HistoricalExample] = Field(default_factory=list)
    # Repair-path few-shot : a STRICTLY-filtered subset of
    # retrieved_examples for the CEGIS repair/retry prompt. The repair path is the
    # A/B failure site; a single high-trust anchor there closes the loop where the
    # model reproduces the same dropped-side merge. Populated by the context
    # builder via a QualityFilteredRetriever (retry-count + higher score floor);
    # empty when no retriever, the corpus is too small, or nothing clears the
    # stricter filter. Top-1 (not top-k) to preserve the surgical-fix signal.
    repair_retrieved_examples: list[HistoricalExample] = Field(default_factory=list)
    # Confidence scores of the retrieved examples (cosine for embedding
    # retrieval, BM25 for lexical), parallel to ``retrieved_examples``. Empty
    # when no retrieval ran or the retriever doesn't expose scores. Surfaced so
    # the orchestrator can journal retrieval confidence (the diagnostic data for
    # validating the calibrated min_similarity threshold in production).
    retrieval_scores: list[float] = Field(default_factory=list)
    # Explainable-retrieval reasons (#9 step 5): one human-readable string per
    # retrieved example, parallel to ``retrieved_examples``/``retrieval_scores``,
    # recording WHY each was chosen (same path/region kind/conflict shape, score,
    # prior outcome). Empty when retrieval didn't run. Surfaced in accept reports
    # so misleading few-shot examples are debuggable.
    retrieval_explanations: list[str] = Field(default_factory=list)
    structural_view: dict[str, Any] = Field(default_factory=dict)
    # History-aware context (#history step 7): a compact summary of where this
    # conflict sits in the replay sequence + what later commits touch the same
    # region. Empty string when no history is available (non-rebase sessions);
    # populated by the context builder from the HistoryQueryService. The replay
    # facts here are the LOWEST-priority budget section (trimmed first).
    history_context: str = ""
    # High-priority history-derived context (#idea 9): future obligations + branch
    # intent. Lifted OUT of history_context into a first-class budget section that
    # trims AFTER structural context (not first, like the replay facts). Populated
    # by the context builder from its future_obligations_block + branch_intent_block.
    # Empty when neither applies.
    obligations_context: str = ""
    token_estimate: int = 0


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


class CandidateResolution(BaseModel):
    """A proposed resolution for one ConflictUnit.

    ``ResolutionEngine.propose`` returns a *list* of these even in the MVP
    (length 1) so that self-consistency (multiple samples + clustering) is a
    parameter change, not an architectural one.
    """

    candidate_id: str
    unit_id: str
    model_name: str
    prompt_version: str
    # Explicit "how was this resolved?" enum (#9 step 8). Stamped at every
    # construction site (structural/sbcr/block_capture/manual/LLM/exact-reuse).
    # Empty string = a candidate built before provenance existed; consumers fall
    # back to inferring from prompt_version/model_name. See capybase.provenance.
    provenance: str = ""

    current_side_intent: list[str] = Field(default_factory=list)
    replayed_commit_intent: list[str] = Field(default_factory=list)
    resolved_text: str
    explanation: str = ""
    # Self-correction plan: on a repair retry, the model's stated
    # reasoning about WHY each failure happened + the fix it will make, emitted
    # BEFORE the resolved_text to force internalization of the critic feedback.
    # Empty on fresh resolves (no plan step there). Auditable but not acted on.
    repair_plan: str = ""

    preserved_current_side: bool = True
    preserved_replayed_commit_side: bool = True
    dropped_current_side_details: list[str] = Field(default_factory=list)
    dropped_replayed_commit_details: list[str] = Field(default_factory=list)

    assumptions: list[str] = Field(default_factory=list)
    needs_human: bool = False
    # Escape hatch (CEGIS resilience): the model signals it believes its code is
    # correct and the validator error is a false positive (e.g. an error from an
    # unresolved sibling hunk in a multi-unit file). When True, the risk engine
    # escalates immediately instead of retrying — preserving the candidate as
    # the "last valid" resolution for human review. The justification (stored in
    # ``explanation`` / ``repair_plan``) explains why the snippet is correct.
    suspected_validator_error: bool = False
    self_reported_confidence: float = 0.0

    # TECP token-entropy: the mean negative log-probability of the
    # generated content tokens, reduced from the API's per-token logprobs. This
    # is the logit-free, black-box uncertainty signal the conformal "flywheel"
    # consumes. ``None`` when logprobs weren't captured (default config) or the
    # server didn't emit them.
    mean_token_entropy: float | None = None

    raw_response: str = ""
    parse_warnings: list[str] = Field(default_factory=list)
    # Distinguishes a genuine model refusal (``"model_refusal"``) from a
    # transient/technical failure (``"request_failed"``, ``"parse_failed"``,
    # ``"truncated"``) — and an LSP/type-check failure (``"lsp_failed"``). Risk
    # policy retries technical and LSP failures but escalates genuine refusals.
    # ``"no_op_repair"`` = the model's SEARCH/REPLACE was a no-op (search ==
    # replace), producing a candidate identical to the previous one — an
    # immediate-escalate signal (the loop is stuck). Empty when well-formed.
    failure_kind: Literal[
        "", "model_refusal", "request_failed", "parse_failed", "truncated",
        "lsp_failed", "no_op_repair",
    ] = ""
    # Token-window trims applied to the prompt that produced this candidate
    # (e.g. few-shot/deps/anchor dropped to fit the context window). Empty when
    # no budget was configured or nothing was trimmed. Carried on the candidate
    # so the orchestrator can journal per-resolution trimming (observability).
    prompt_trims: list[dict[str, Any]] = Field(default_factory=list)


@dataclass
class ResolutionAttempt:
    """One mechanism's attempt to resolve a unit (#idea 6 cohesion).

    Normalizes the 5 resolution mechanisms (exact reuse, structural, combination
    search, block capture, plain/history-augmented LLM) into a uniform record so
    reports, metrics, tests, and retry behavior are consistent. Provenance (the
    mechanism) is assigned by the dispatch, not inferred or restamped afterward
    except in the clearly-named history-augmentation compat path.

    - ``mechanism``: the provenance value (e.g. ``"deterministic_structural"``).
    - ``candidate``: the candidate produced (None for a mechanism that declined
      before producing one, e.g. reuse found no match).
    - ``validation``: the verification result (None if not yet validated).
    - ``decision``: ``"accept"`` | ``"retry"`` | ``"escalate"`` | ``"skip"``
      (skip = the mechanism declined, falling through to the next).
    - ``reason``: human-readable why (e.g. ``"structural resolver declined:
      contradictory edits"`` or ``"accepted via insertion_union rule"``).
    """

    mechanism: str
    candidate: "CandidateResolution | None" = None
    validation: "VerificationResult | None" = None
    decision: str = "skip"
    reason: str = ""


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

FailureSeverity = Literal["error", "warning"]


class VerificationFailure(BaseModel):
    validator: str
    severity: FailureSeverity = "error"
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class VerificationWarning(BaseModel):
    validator: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class VerificationResult(BaseModel):
    """Structured facts about a candidate produced by validators.

    ``features`` is the future training/calibration spine: every validator
    records machine-learnable features here so a later risk classifier or
    conformal model can consume them without validator rewrites.
    """

    candidate_id: str
    unit_id: str
    passed: bool
    hard_failures: list[VerificationFailure] = Field(default_factory=list)
    warnings: list[VerificationWarning] = Field(default_factory=list)
    features: dict[str, float | int | str | bool] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------

RiskAction = Literal["accept", "retry", "escalate"]


class RiskDecision(BaseModel):
    """The orchestrator only consumes ``action``; it never knows how the
    decision was derived. In the MVP this is a rules engine; later it may be a
    calibrated classifier or conformal predictor producing the same shape."""

    action: RiskAction
    reasons: list[str] = Field(default_factory=list)
    risk_score: float | None = None
    required_followups: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


class JournalEvent(BaseModel):
    """An append-only event in a session's journal.

    Rich enough to later become RAG index entries, LoRA training rows, offline
    eval fixtures, and risk-calibration data — without re-journaling."""

    seq: int
    timestamp: datetime
    session_id: str
    event_type: str

    git_head_before: str | None = None
    git_head_after: str | None = None

    step_index: int | None = None
    path: str | None = None
    unit_id: str | None = None

    payload: dict[str, Any] = Field(default_factory=dict)

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)

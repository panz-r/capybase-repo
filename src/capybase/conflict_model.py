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
    # Graded severity computed at extraction (survey §3.3) from cheap pre-LLM
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
    total: int = 4096
    reserved_for_completion: int = 1024

    @property
    def available(self) -> int:
        return max(0, self.total - self.reserved_for_completion)


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
    structural_view: dict[str, Any] = Field(default_factory=dict)
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

    current_side_intent: list[str] = Field(default_factory=list)
    replayed_commit_intent: list[str] = Field(default_factory=list)
    resolved_text: str
    explanation: str = ""

    preserved_current_side: bool = True
    preserved_replayed_commit_side: bool = True
    dropped_current_side_details: list[str] = Field(default_factory=list)
    dropped_replayed_commit_details: list[str] = Field(default_factory=list)

    assumptions: list[str] = Field(default_factory=list)
    needs_human: bool = False
    self_reported_confidence: float = 0.0

    # TECP token-entropy (survey §4.1): the mean negative log-probability of the
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
    # Empty when the candidate is well-formed.
    failure_kind: Literal[
        "", "model_refusal", "request_failed", "parse_failed", "truncated", "lsp_failed"
    ] = ""


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

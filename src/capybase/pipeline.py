"""Pipeline trigger architecture (sprint-24).

The core architectural directive: zero user configuration, typed
stages, mechanism-owned triggers, and a generic pipeline executor.

Design principles:
1. The pipeline OWNS stage sequencing and variable contracts
2. Mechanisms REGISTER for a stage and bring their own trigger
3. Mechanisms receive a typed stage context (never orchestrator internals)
4. The pipeline is GENERIC (stages + contexts + invocation + journaling)
5. Adding/removing a mechanism requires NO pipeline changes

Usage:
    pipeline = Pipeline(journal=orchestrator.journal)
    pipeline.register(F1Tier1Takeover())
    pipeline.register(SymbolInjection())

    result = pipeline.execute(Stage.POST_REPAIR_EXHAUSTION, context)
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Stages: the ordered sequence of the failure path
# ---------------------------------------------------------------------------

class Stage(enum.Enum):
    """Ordered pipeline stages. Mechanisms register for one or more."""

    # Initial resolution: before any model call
    PRE_RESOLVE = "pre_resolve"

    # After the model produces candidates
    POST_CANDIDATE = "post_candidate"

    # After validation of a candidate (pass or fail)
    POST_VALIDATE = "post_validate"

    # Deterministic repair (brace, delimiter, symbol injection, etc.)
    REPAIR = "repair"

    # All deterministic repairs exhausted
    POST_REPAIR_EXHAUSTION = "post_repair_exhaustion"

    # Model returned empty/refused/failed
    POST_MODEL_FAILURE = "post_model_failure"

    # Before escalating to human review
    PRE_ESCALATE = "pre_escalate"


# ---------------------------------------------------------------------------
# Typed stage contexts: what the pipeline guarantees at each stage
# ---------------------------------------------------------------------------

@dataclass
class StageContext:
    """Base context — the minimum every mechanism receives."""

    path: str
    language: str | None
    step_index: int


@dataclass
class PreResolveContext(StageContext):
    """Before any model call — structural/portfolio resolution."""

    base_text: str = ""
    current_text: str = ""
    replayed_text: str = ""


@dataclass
class PostCandidateContext(StageContext):
    """After the model produces candidates."""

    candidates: list[Any] = field(default_factory=list)
    failures: list[Any] = field(default_factory=list)


@dataclass
class PostValidateContext(StageContext):
    """After candidate validation (pass or fail)."""

    candidate: Any = None
    passed: bool = False
    failures: list[Any] = field(default_factory=list)


@dataclass
class RepairContext(StageContext):
    """Deterministic repair stage."""

    spliced_buffer: str = ""
    failures: list[Any] = field(default_factory=list)
    base_text: str = ""
    current_text: str = ""
    replayed_text: str = ""
    retry_count: int = 0


@dataclass
class RepairExhaustedContext(StageContext):
    """All deterministic repairs exhausted — F1/pre-escalate territory."""

    spliced_buffer: str = ""
    failures: list[Any] = field(default_factory=list)
    sides: dict[str, str] = field(default_factory=dict)
    base_text: str = ""
    retry_count: int = 0
    retry_budget: int = 0
    phase2_model_used: bool = False


@dataclass
class ModelFailureContext(StageContext):
    """Model returned empty/refused/failed."""

    failure_kind: str = ""
    resolved_text: str = ""
    raw_response: str = ""
    sides: dict[str, str] = field(default_factory=dict)
    base_text: str = ""
    retry_count: int = 0


@dataclass
class PreEscalateContext(StageContext):
    """Before escalating — last chance for a mechanism to rescue."""

    spliced_buffer: str = ""
    failures: list[Any] = field(default_factory=list)
    sides: dict[str, str] = field(default_factory=dict)
    base_text: str = ""
    escalation_reason: str = ""


# ---------------------------------------------------------------------------
# Mechanism protocol: what every mechanism implements
# ---------------------------------------------------------------------------

@runtime_checkable
class Mechanism(Protocol):
    """A self-contained resolution mechanism.

    Each mechanism declares which stage(s) it engages at and brings
    its own trigger conditions (evaluated against the stage context).
    The pipeline calls `engage(ctx)` at the right time; the mechanism
    decides whether to act.

    Contract:
    - `stage` is a Stage enum value (or list of values)
    - `name` is a unique identifier for journaling
    - `engage(ctx)` returns a MechanismResult or None (decline)
    - Mechanisms NEVER modify the context directly; they return results
    """

    @property
    def stage(self) -> Stage | list[Stage]:
        """Which stage(s) this mechanism engages at."""

    @property
    def name(self) -> str:
        """Unique identifier for journaling."""

    def engage(self, ctx: StageContext) -> "MechanismResult | None":
        """Evaluate trigger and act. Returns None to decline."""


@dataclass
class MechanismResult:
    """What a mechanism returns when it engages successfully."""

    mechanism: str
    action: str  # "accept" | "repair" | "takeover" | "escalate" | ...
    resolved_text: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pipeline executor: the generic engine
# ---------------------------------------------------------------------------

class Pipeline:
    """Generic stage-execution engine.

    Walks stages in order, builds typed contexts, calls registered
    mechanisms. Journaling is built-in (every engagement and decline
    is recorded). Adding a mechanism requires NO pipeline changes.

    Phased execution protocol (the orchestrator's pattern at
    POST_REPAIR_EXHAUSTION): ``execute`` returns on the FIRST
    engagement, so when a caller needs mechanisms that require
    caller-provided state (compile verdicts) or must run in a specific
    order, it re-executes the stage in PHASES with later mechanisms
    latched off (``enabled = False``) until their phase. Two rules
    learned the hard way (sprint-24):

    1. A DETERMINISTIC mechanism re-engages on every re-execution —
       latch it off after its phase or it preempts the later
       mechanisms (the sea-orm preemption bug).
    2. Latches must be restored idempotently at each phase-A entry —
       an exception in a prior round must not poison the next.
    """

    def __init__(self, *, journal: Any = None):
        self._mechanisms: dict[Stage, list[Mechanism]] = {
            stage: [] for stage in Stage
        }
        self.journal = journal

    def register(self, mechanism: Mechanism) -> None:
        """Register a mechanism for its declared stage(s)."""
        stages = mechanism.stage
        if isinstance(stages, Stage):
            stages = [stages]
        for s in stages:
            self._mechanisms[s].append(mechanism)

    def execute(
        self, stage: Stage, context: StageContext,
    ) -> MechanismResult | None:
        """Run all mechanisms registered for this stage.

        Returns the first engaging mechanism's result, or None if all
        declined. Mechanisms run in registration order."""
        for mech in self._mechanisms[stage]:
            result = self._try_engage(mech, stage, context)
            if result is not None:
                return result
        return None

    def _try_engage(
        self, mech: Mechanism, stage: Stage, context: StageContext,
    ) -> MechanismResult | None:
        """Call a mechanism's engage() with error handling and journaling."""
        try:
            result = mech.engage(context)
        except Exception as exc:  # noqa: BLE001 — mechanisms are best-effort
            self._journal(
                stage, mech.name, "error", {"error": str(exc)[:120]})
            return None

        if result is not None:
            self._journal(
                stage, mech.name, "engaged", {
                    "action": result.action,
                    **({k: v for k, v in result.metadata.items()
                        if isinstance(v, (str, int, float, bool))}),
                })
        else:
            self._journal(stage, mech.name, "declined", {})

        return result

    def _journal(
        self, stage: Stage, name: str, status: str,
        payload: dict[str, Any],
    ) -> None:
        """Emit a journal event if a journal is available."""
        if self.journal is None:
            return
        try:
            self.journal.emit(
                "pipeline_mechanism",
                {"stage": stage.value, "mechanism": name,
                 "status": status, **payload},
            )
        except Exception:  # noqa: BLE001 — journaling is best-effort
            pass

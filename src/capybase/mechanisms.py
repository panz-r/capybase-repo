"""Pipeline mechanisms (sprint-24).

Each mechanism is self-contained: trigger conditions + repair action +
safety check. Mechanisms register for a pipeline stage and receive a
typed stage context. They never touch orchestrator internals.

The first mechanism (F1Tier1Takeover) is the proof of concept for
the pipeline architecture. Others migrate progressively.
"""
from __future__ import annotations

from capybase.pipeline import (
    MechanismResult,
    RepairExhaustedContext,
    Stage,
    StageContext,
)


def _side_churn(base_text: str, side_text: str) -> int:
    """Changed-line count of a side vs base (both directions)."""
    from capybase.merge_intent import side_churn
    return side_churn(base_text, side_text)


class F1Tier1Takeover:
    """F1 tier-1: deterministic near-one-sided takeover.

    Trigger: when one side's churn vs base is ≤ threshold (30
    double-counted = 15 changed lines), the other side IS the merge.
    Safety: the takeover side must pass the compile gate.

    Stage: POST_REPAIR_EXHAUSTION — after all deterministic repairs
    and the wholesale floor have declined.
    """

    def __init__(self, *, churn_threshold: int = 30):
        self.churn_threshold = churn_threshold

    @property
    def stage(self) -> Stage:
        return Stage.POST_REPAIR_EXHAUSTION

    @property
    def name(self) -> str:
        return "f1_tier1_takeover"

    def engage(self, ctx: StageContext) -> MechanismResult | None:
        """Evaluate the near-one-sided trigger and return a takeover."""
        if not isinstance(ctx, RepairExhaustedContext):
            return None
        if not ctx.sides:
            return None

        current = ctx.sides.get("current", "")
        replayed = ctx.sides.get("replayed", "")
        if not current.strip() or not replayed.strip():
            return None

        c_churn = _side_churn(ctx.base_text, current)
        r_churn = _side_churn(ctx.base_text, replayed)
        min_churn = min(c_churn, r_churn)

        if min_churn > self.churn_threshold:
            return None  # Both sides changed significantly → tier-2

        # Take the high-churn side (the low-churn side ≈ base)
        side = "current" if c_churn > r_churn else "replayed"
        text = ctx.sides[side]

        if not text.strip():
            return None

        return MechanismResult(
            mechanism=self.name,
            action="takeover",
            resolved_text=text,
            metadata={
                "side": side,
                "churn_current": c_churn,
                "churn_replayed": r_churn,
                "threshold": self.churn_threshold,
            },
        )


class F1CompileCleanTakeover:
    """F1 compile-clean override: if exactly one pristine side compiles
    and the merge doesn't, take the compiling side regardless of churn.

    Trigger: one side compiles cleanly, the spliced buffer doesn't.
    Safety: the compiler validates the takeover directly.

    The compile check is done by the CALLER (the orchestrator/pipeline
    provides `compiling_sides: dict[str, bool]` in the metadata). This
    mechanism reads the verdicts and takes the compiling side.

    Stage: POST_REPAIR_EXHAUSTION (after tier-1's churn check).
    """

    def __init__(self, *, compiling_sides: dict[str, bool] | None = None):
        self._compiling_sides = compiling_sides or {}

    @property
    def stage(self) -> Stage:
        return Stage.POST_REPAIR_EXHAUSTION

    @property
    def name(self) -> str:
        return "f1_compile_clean_takeover"

    def set_compiling_sides(self, verdicts: dict[str, bool]) -> None:
        """Called by the orchestrator before pipeline execution."""
        self._compiling_sides = verdicts

    def engage(self, ctx: StageContext) -> MechanismResult | None:
        """Evaluate the compile-clean trigger."""
        if not isinstance(ctx, RepairExhaustedContext):
            return None
        if not ctx.sides or not self._compiling_sides:
            return None

        compiling = [
            side for side, ok in self._compiling_sides.items() if ok
        ]
        if len(compiling) != 1:
            return None  # 0 or 2 compiling sides → no clear winner

        side = compiling[0]
        text = ctx.sides.get(side, "")
        if not text.strip():
            return None

        return MechanismResult(
            mechanism=self.name,
            action="takeover",
            resolved_text=text,
            metadata={
                "side": side,
                "reason": "compile_clean_override",
                "compiling_sides": dict(self._compiling_sides),
            },
        )

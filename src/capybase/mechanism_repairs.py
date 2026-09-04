"""Deterministic repair mechanisms for the typed registry (stage 2, item 9).

The pattern proof: one orchestrator-inline repair extracted behind the
mechanisms.py registry architecture. The orchestrator constructs the
typed context; the mechanism owns its trigger + edit + evidence — no
orchestrator internals touched.
"""

from __future__ import annotations

from capybase.pipeline import MechanismResult, RepairContext, Stage
from capybase.conflict_model import CandidateResolution


class StorageClassRelocationMechanism:
    """Relocate a misplaced function declaration to file scope (C/C++).

    Trigger: gcc's ``invalid storage class for function X`` — the model's
    merge declared X inside a function body. Remove the misplaced
    declaration and re-place it at file scope via
    ``inject_symbol_declaration``. The next round's C1 derived-prototype
    can also re-place it; this rung short-circuits the round trip
    (redis-0013's wf trace: two rounds burned reaching the state C1
    could fix).

    Whole-file-unit contract: returns a whole-file candidate; the
    compiler re-gates it.
    """

    def __init__(self, *, tried_sigs: set[str] | None = None):
        self._tried = tried_sigs if tried_sigs is not None else set()
        self.enabled = True

    @property
    def stage(self) -> Stage:
        return Stage.REPAIR

    @property
    def name(self) -> str:
        return "storage_class_relocation"

    def engage(self, ctx: RepairContext) -> MechanismResult | None:
        if not isinstance(ctx, RepairContext) or not self.enabled:
            return None
        failure_text = "\n".join(
            str(getattr(f, "message", "")) for f in (ctx.failures or []))
        if "invalid storage class for function" not in failure_text:
            return None  # not this mechanism's shape

        from capybase.verification import (
            find_misplaced_declaration,
            inject_symbol_declaration,
        )
        spliced = ctx.spliced_buffer
        if not spliced:
            return None
        mis = find_misplaced_declaration(spliced, failure_text)
        if mis is None:
            return None
        lines = spliced.split("\n")
        decl = lines.pop(mis[0])
        relocated = inject_symbol_declaration(
            "\n".join(lines), decl, ctx.language)
        if relocated is None:
            return None
        unit = ctx.unit
        wf_unit = unit.model_copy(
            update={"marker_span": None, "unit_kind": "whole_file"})
        candidate = CandidateResolution(
            candidate_id=f"{unit.unit_id}:stcreloc",
            unit_id=unit.unit_id,
            model_name="deterministic",
            resolved_text=relocated,
            prompt_version="deterministic_storage_class_relocation",
            provenance="deterministic_symbol_injection",
            self_reported_confidence=0.0,
            explanation=(
                f"storage-class relocation: moved misplaced declaration "
                f"to file scope: {decl[:80]}"),
        )
        return MechanismResult(
            mechanism=self.name,
            action="repair",
            resolved_text=relocated,
            metadata={
                "kind": "storage_class_relocation",
                "line": mis[0] + 1,
                "declaration": decl[:120],
                "unit": wf_unit,
                "candidate": candidate,
            },
        )

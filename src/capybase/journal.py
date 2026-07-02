"""JSONL event-sourced journal + on-disk artifact store.

The journal is the data spine for everything downstream: RAG, LoRA training,
offline eval, risk calibration, and replay. Every meaningful step emits a
``JournalEvent``; larger artifacts (prompts, raw responses, candidates,
snapshots) are written as separate files and referenced by path in the event
payload.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from capybase.conflict_model import (
    CandidateResolution,
    JournalEvent,
    VerificationResult,
)
from capybase.session import SessionPaths


class Journal:
    def __init__(self, paths: SessionPaths) -> None:
        self.paths = paths
        self._seq = 0
        # In-process event listeners (e.g. the progress spinner), invoked after
        # the event is appended. A listener raising is non-fatal (logged, not
        # propagated) so a UI glitch never breaks a rebase.
        self._listeners: list = []

    # ------------------------------------------------------------- subscribe

    def subscribe(self, callback) -> None:
        """Register an in-process listener invoked with each emitted event.

        The callback receives the :class:`JournalEvent`. Used by the progress
        spinner to map state transitions to a status message without the
        orchestrator scattering spinner calls across every code path. Exceptions
        in a listener are swallowed (a UI issue must not break a rebase).
        """
        self._listeners.append(callback)

    # ------------------------------------------------------------------ emit

    def emit(self, event_type: str, payload: dict[str, Any] | None = None, **fields: Any) -> JournalEvent:
        """Append a journal event. Extra ``fields`` populate top-level
        ``JournalEvent`` slots (step_index, path, unit_id, git_head_*)."""
        self._seq += 1
        event = JournalEvent(
            seq=self._seq,
            timestamp=JournalEvent.now(),
            session_id=self.paths.session_id,
            event_type=event_type,
            payload=payload or {},
            **fields,
        )
        self._append(event)
        # Notify in-process listeners (after the event is durably appended).
        for cb in list(self._listeners):
            try:
                cb(event)
            except Exception:  # noqa: BLE001 - a UI listener must not break a rebase
                pass
        return event

    def emit_advisory(
        self, event_type: str, reason: str, *, path: str | None = None,
        unit_id: str | None = None, **fields: Any
    ) -> JournalEvent:
        """Emit an ADVISORY event: a subsystem degraded silently and wants it
        recorded without crashing the rebase (#idea 4 — observability).

        The tag lives in ``payload`` (``{"advisory": True, "reason": ...}``) so
        the flat journal model needs no new field. Advisory events are kept out
        of normal terminal output (never printed via self.out) but surface in the
        dry-run report + escalation review bundle, so a silently-degraded history
        feature is observable rather than invisible. ``**fields`` carries extra
        detail (e.g. dropped_symbols, the exception message).
        """
        payload = {"advisory": True, "reason": reason, **fields}
        kw: dict[str, Any] = {}
        if path is not None:
            kw["path"] = path
        if unit_id is not None:
            kw["unit_id"] = unit_id
        return self.emit(event_type, payload, **kw)

    def _append(self, event: JournalEvent) -> None:
        self.paths.journal.parent.mkdir(parents=True, exist_ok=True)
        line = event.model_dump_json()
        with open(self.paths.journal, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    # ------------------------------------------------------------------ artifacts

    def write_artifact(self, subdir: Path, name: str, data: bytes | str) -> Path:
        subdir.mkdir(parents=True, exist_ok=True)
        target = subdir / name
        if isinstance(data, str):
            target.write_text(data, encoding="utf-8")
        else:
            target.write_bytes(data)
        return target

    def store_prompt(self, unit_id: str, attempt: int, text: str) -> Path:
        return self.write_artifact(
            self.paths.prompts, f"{_safe(unit_id)}.attempt{attempt}.txt", text
        )

    def store_response(self, unit_id: str, attempt: int, text: str) -> Path:
        return self.write_artifact(
            self.paths.responses, f"{_safe(unit_id)}.attempt{attempt}.txt", text
        )

    def store_candidate(self, candidate: CandidateResolution) -> Path:
        return self.write_artifact(
            self.paths.candidates, f"{_safe(candidate.candidate_id)}.json",
            candidate.model_dump_json(indent=2),
        )

    def store_validation(self, result: VerificationResult) -> Path:
        return self.write_artifact(
            self.paths.validations, f"{_safe(result.candidate_id)}.json",
            result.model_dump_json(indent=2),
        )

    def store_snapshot(self, name: str, data: bytes | str) -> Path:
        return self.write_artifact(self.paths.snapshots, name, data)

    # ------------------------------------------------------------------ read

    def read_events(self) -> list[JournalEvent]:
        if not self.paths.journal.exists():
            return []
        events: list[JournalEvent] = []
        for line in self.paths.journal.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            events.append(JournalEvent.model_validate_json(line))
        return events


def _safe(name: str) -> str:
    return name.replace("/", "__").replace(":", "-")

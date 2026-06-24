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
        return event

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

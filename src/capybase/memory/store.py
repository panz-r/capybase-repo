"""Experience store: the labeled corpus behind RAG and calibration.

The journal records every resolution attempt as artifacts, but it does not
distinguish outcomes — an accepted merge and a rejected one look the same in
the raw JSONL. The ExperienceStore closes that gap: after each unit is
resolved or escalated, the orchestrator appends a labeled record here with the
outcome, the validator features, the risk score, and the full
HistoricalExample triple (base/current/replayed/resolved).

Accepted resolutions become positive examples (few-shot demonstrations +
LoRA training data). Rejected/escalated ones become negative labels for
calibration. The store is append-only JSONL, keyed by (path, session).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from capybase.conflict_model import HistoricalExample


@dataclass
class Experience:
    """One labeled resolution outcome, the unit of the memory corpus."""

    example: HistoricalExample
    outcome: str  # "accepted" | "rejected" | "escalated"
    language: str | None = None
    path: str = ""
    session_id: str = ""
    unit_id: str = ""
    validator_features: dict[str, Any] = field(default_factory=dict)
    risk_score: float | None = None
    retry_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "example": self.example.model_dump(),
            "outcome": self.outcome,
            "language": self.language,
            "path": self.path,
            "session_id": self.session_id,
            "unit_id": self.unit_id,
            "validator_features": self.validator_features,
            "risk_score": self.risk_score,
            "retry_count": self.retry_count,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Experience":
        return cls(
            example=HistoricalExample.model_validate(d["example"]),
            outcome=str(d.get("outcome", "accepted")),
            language=d.get("language"),
            path=str(d.get("path", "")),
            session_id=str(d.get("session_id", "")),
            unit_id=str(d.get("unit_id", "")),
            validator_features=dict(d.get("validator_features", {})),
            risk_score=d.get("risk_score"),
            retry_count=int(d.get("retry_count", 0)),
        )


class ExperienceStore:
    """Append-only JSONL corpus of labeled resolution outcomes.

    Records are written under ``store_path`` (default
    ``.rebase-agent/memory/experiences.jsonl``). The store is intentionally
    simple — one JSON object per line — so it can be grepped, appended to by
    concurrent processes, and consumed by offline tooling without a database.
    """

    def __init__(self, store_path: str | Path) -> None:
        self.path = Path(store_path)

    def append(self, experience: Experience) -> None:
        """Append one labeled outcome. Creates parent dirs as needed."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(experience.to_dict(), ensure_ascii=False) + "\n")

    def __iter__(self) -> Iterator[Experience]:
        """Yield every experience in the corpus (oldest first)."""
        if not self.path.is_file():
            return
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield Experience.from_dict(json.loads(line))
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue  # skip corrupt lines

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def accepted(self) -> list[Experience]:
        """Positive examples: successful merges, usable as few-shot."""
        return [e for e in self if e.outcome == "accepted"]

    def rejected(self) -> list[Experience]:
        """Negative examples: failures, for calibration."""
        return [e for e in self if e.outcome in ("rejected", "escalated")]

    @classmethod
    def for_repo(cls, repo_root: str | Path, store_path: str) -> "ExperienceStore":
        """Resolve a store path relative to a repo root."""
        p = Path(store_path)
        if not p.is_absolute():
            p = Path(repo_root) / p
        return cls(p)

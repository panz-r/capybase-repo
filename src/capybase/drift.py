"""Session-level semantic drift detection (embeddings survey §6).

Every other validator is per-commit: it checks whether the merge of a single
commit preserves that commit's intent. None detect when the CUMULATIVE effect
of several merges drifts the branch from its original purpose — for example,
when a merge accidentally incorporates unrelated changes, or when the model's
plan-first step across retries has gradually shifted the merged code.

This module provides an advisory (non-blocking) monitor:

- :meth:`DriftMonitor.set_anchor`: once per session, embed the branch-intent
  summary + commit subjects as a session anchor vector.
- :meth:`DriftMonitor.check`: after each commit's outcomes are recorded, re-embed
  the updated intent and compute cosine DISTANCE from the anchor. Above a
  threshold → a drift advisory.

Uses a general text embedder (the same OpenAIEmbeddingsClient the rest of the
stack uses — commit messages + intent summaries are natural language, and
Qwen3-Embedding handles both code and text). Never raises; a missing/failing
embedder makes the monitor a silent no-op.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DriftReport:
    """One per-commit drift measurement (embeddings survey §6).

    ``distance`` is cosine DISTANCE (1 - similarity) in [0, 2]: 0 = identical to
    the anchor, 1 = orthogonal, 2 = opposite. The advisory fires when distance
    exceeds the configured ``drift_threshold``. ``cumulative`` is the running
    max distance across the session — the headline drift number.
    """

    commit_index: int
    distance: float
    cumulative: float
    threshold: float

    @property
    def is_drift(self) -> bool:
        return self.distance > self.threshold

    def render(self) -> str:
        band = (
            "high" if self.distance > self.threshold * 1.5
            else "medium" if self.distance > self.threshold
            else "low"
        )
        return (
            f"semantic drift @ commit {self.commit_index}: "
            f"{self.distance:.2f} ({band}), cumulative={self.cumulative:.2f}, "
            f"threshold={self.threshold:.2f}"
        )


@dataclass
class DriftMonitor:
    """Advisory session-level drift detector (embeddings survey §6).

    Construct once per session; call :meth:`set_anchor` at the first commit
    merge, then :meth:`check` after each subsequent commit. Never blocks a
    merge — the advisory is journaled by the caller. Best-effort: a None/failing
    embedder makes every method a no-op (returns None / empty).

    ``threshold`` is cosine DISTANCE (0.20 ≈ similarity 0.80). Tune on
    accumulated rebase history; start conservative.
    """

    embedder: object | None
    threshold: float = 0.20
    _anchor: list[float] | None = None
    _anchor_digest: str = ""
    _history: list[DriftReport] = field(default_factory=list)

    def set_anchor(self, text: str) -> None:
        """Embed the session anchor (branch intent + commit subjects).

        Called once at session start. The anchor is the rebase goal vector;
        per-commit distances are measured from it. Idempotent within a session
        (re-calling with the same text is a no-op; a different text re-anchors).
        Best-effort: a failed embed leaves the monitor inactive.
        """
        if self.embedder is None or not text:
            return
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        if self._anchor is not None and digest == self._anchor_digest:
            return  # same anchor text → no-op
        try:
            vecs = self.embedder.embed(text)  # type: ignore[attr-defined]
            if vecs and vecs[0]:
                self._anchor = list(vecs[0])
                self._anchor_digest = digest
        except Exception:  # noqa: BLE001 - drift detection is best-effort
            self._anchor = None

    def check(self, text: str, commit_index: int) -> DriftReport | None:
        """Measure this commit's intent-text distance from the anchor.

        Returns a :class:`DriftReport` (with cumulative history), or None when
        the monitor is inactive (no embedder / no anchor / embed failed). Never
        raises. The report's ``is_drift`` flags whether the threshold was crossed.
        """
        if self.embedder is None or self._anchor is None or not text:
            return None
        try:
            vecs = self.embedder.embed(text)  # type: ignore[attr-defined]
            if not vecs or not vecs[0]:
                return None
            dist = _cosine_distance(self._anchor, list(vecs[0]))
        except Exception:  # noqa: BLE001 - drift detection is best-effort
            return None
        cumulative = max((r.distance for r in self._history), default=0.0)
        cumulative = max(cumulative, dist)
        report = DriftReport(
            commit_index=commit_index, distance=dist,
            cumulative=cumulative, threshold=self.threshold,
        )
        self._history.append(report)
        return report

    @property
    def history(self) -> list[DriftReport]:
        return list(self._history)

    def summary(self) -> str:
        """A one-line post-session drift summary for the report/logs.

        ``"semantic drift over the N-commit window: 0.08 (low) / 0.41 (high)"``.
        Empty when the monitor was inactive.
        """
        if not self._history:
            return ""
        max_dist = max(r.distance for r in self._history)
        band = (
            "high" if max_dist > self.threshold * 1.5
            else "medium" if max_dist > self.threshold
            else "low"
        )
        n = len(self._history)
        return (
            f"semantic drift over the {n}-commit window: "
            f"{max_dist:.2f} ({band}) [threshold {self.threshold:.2f}]"
        )


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """Cosine DISTANCE (1 - cosine similarity) in [0, 2]. 0 = identical."""
    if not a or not b or len(a) != len(b):
        return 1.0  # treat as orthogonal (max uncertainty)
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 1.0
    sim = dot / (math.sqrt(na) * math.sqrt(nb))
    # Clamp similarity to [-1, 1] to absorb float noise, then distance in [0, 2].
    sim = max(-1.0, min(1.0, sim))
    return 1.0 - sim

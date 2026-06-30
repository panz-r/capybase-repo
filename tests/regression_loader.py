"""Loader for the hand-authored regression fixture tree (#8).

Reads committed JSON fixtures from ``tests/fixtures/regression/``. Unlike the
session/realworld datasets (gitignored, generated, model-side), these are
**hand-authored** conflict shapes with a known-correct **outcome** (a resolved
text OR an expected escalation) — the regression suite's source of truth.

Fixture schema (one JSON per file):
- ``id``, ``title``, ``path``, ``language`` — identity + display.
- ``base`` / ``current`` / ``replayed`` — the three-way sides.
- **expected outcome (exactly one):**
  - ``expected_resolved`` — the canonical merged text the pipeline must produce,
    OR
  - ``expected_escalated: true`` (+ optional ``escalation_reason_substr``) — the
    pipeline must ESCALATE (never guess a broken merge).
- optional ``expected_via`` — ``"deterministic"`` (the structural/union rules
  should resolve it with zero LLM calls) or omitted (LLM/verifier path).
- optional ``conflict_type`` — git unmerged mode (``"UU"`` default; ``"AU"``/
  ``"UA"`` for whole-file modify/delete).

The fixtures are committed (not gitignored), so :func:`load_regression_cases`
always returns them — there is no inert-on-empty skip. A malformed fixture is a
real error (the suite should fail loudly, not silently skip a broken case).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = TESTS_DIR / "fixtures" / "regression"


@dataclass(frozen=True)
class RegressionCase:
    """One hand-authored conflict fixture with a known-correct outcome.

    Exactly one of ``expected_resolved`` / ``expected_escalated`` is set. The
    runner dispatches on that: a resolved outcome is driven through the engine +
    verifier (repo-free); an escalated outcome is driven through a synthesized
    git repo + the orchestrator (escalation is orchestrator-only).
    """

    id: str
    title: str
    path: str
    language: str | None
    base: str
    current: str
    replayed: str
    expected_resolved: str | None
    expected_escalated: bool
    escalation_reason_substr: str | None
    expected_via: str | None  # "deterministic" | None
    conflict_type: str  # git unmerged mode
    notes: str


def load_regression_cases() -> list[RegressionCase]:
    """Load every committed fixture in the regression tree.

    Sorted by id for stable parametrization. A missing/malformed fixture raises
    (these are committed source-of-truth cases, not generated data — a broken
    file is a real bug, not a silent skip).
    """
    if not FIXTURES_DIR.is_dir():
        return []
    cases: list[RegressionCase] = []
    for f in sorted(FIXTURES_DIR.glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        required = ("base", "current", "replayed")
        missing = [k for k in required if k not in d]
        if missing:
            raise ValueError(f"{f.name}: missing required field(s) {missing}")
        if "expected_resolved" not in d and not d.get("expected_escalated"):
            raise ValueError(
                f"{f.name}: fixture must set expected_resolved or expected_escalated"
            )
        if "expected_resolved" in d and d.get("expected_escalated"):
            raise ValueError(
                f"{f.name}: set expected_resolved OR expected_escalated, not both"
            )
        cases.append(
            RegressionCase(
                id=d.get("id", f.stem),
                title=d.get("title", f.stem),
                path=d.get("path", f"{f.stem}.py"),
                language=d.get("language") or None,
                base=d["base"],
                current=d["current"],
                replayed=d["replayed"],
                expected_resolved=d.get("expected_resolved"),
                expected_escalated=bool(d.get("expected_escalated", False)),
                escalation_reason_substr=d.get("escalation_reason_substr"),
                expected_via=d.get("expected_via"),
                conflict_type=d.get("conflict_type", "UU"),
                notes=d.get("notes", ""),
            )
        )
    return cases


def resolved_cases() -> list[RegressionCase]:
    """Only the fixtures expecting a resolved-text outcome (engine+verifier path)."""
    return [c for c in load_regression_cases() if c.expected_resolved is not None]


def escalated_cases() -> list[RegressionCase]:
    """Only the fixtures expecting an escalation outcome (orchestrator path)."""
    return [c for c in load_regression_cases() if c.expected_escalated]

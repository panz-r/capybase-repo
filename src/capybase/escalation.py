"""Escalation: when capybase stops, write a useful review bundle.

The bundle explains why the agent stopped, the file/unit involved, the best
candidate it had, the validation failures, any test output, and the exact
command to resume. This makes the MVP useful even when it cannot auto-resolve.
"""

from __future__ import annotations

from pathlib import Path

from capybase.conflict_model import CandidateResolution, ConflictUnit, VerificationResult
from capybase.session import SessionPaths


def write_review_bundle(
    paths: SessionPaths,
    *,
    reason: str,
    step_index: int | None = None,
    unit: ConflictUnit | None = None,
    candidate: CandidateResolution | None = None,
    validation: VerificationResult | None = None,
    test_output: str | None = None,
    resume_hint: str | None = None,
) -> Path:
    """Write ``final/review-bundle.md`` and return its path."""
    paths.final.mkdir(parents=True, exist_ok=True)
    out = paths.final / "review-bundle.md"
    lines: list[str] = []
    lines.append("# capybase review bundle\n")
    lines.append(f"- **session:** `{paths.session_id}`")
    if step_index is not None:
        lines.append(f"- **step:** {step_index}")
    lines.append(f"- **stop reason:** {reason}")
    if resume_hint:
        lines.append(f"- **to resume:** `{resume_hint}`")
    lines.append("")

    if unit is not None:
        lines.append("## conflict unit")
        lines.append(f"- path: `{unit.path}`")
        lines.append(f"- unit id: `{unit.unit_id}`")
        lines.append(f"- type: {unit.conflict_type} / {unit.unit_kind}")
        if unit.language:
            lines.append(f"- language: {unit.language}")
        lines.append("")
        lines.append("### BASE (common ancestor)")
        lines.append("```")
        lines.append(unit.base.text)
        lines.append("```")
        lines.append("### CURRENT_UPSTREAM_SIDE")
        lines.append("```")
        lines.append(unit.current.text)
        lines.append("```")
        lines.append("### REPLAYED_COMMIT_SIDE")
        lines.append("```")
        lines.append(unit.replayed.text)
        lines.append("```")
        lines.append("")

    if candidate is not None:
        lines.append("## best candidate")
        lines.append(f"- model: `{candidate.model_name}` (prompt `{candidate.prompt_version}`)")
        lines.append(f"- self-reported confidence: {candidate.self_reported_confidence}")
        lines.append(f"- needs_human: {candidate.needs_human}")
        lines.append("```")
        lines.append(candidate.resolved_text)
        lines.append("```")
        if candidate.explanation:
            lines.append(f"\n> {candidate.explanation}")
        lines.append("")

    if validation is not None:
        lines.append("## verification")
        lines.append(f"- passed: {validation.passed}")
        for hf in validation.hard_failures:
            lines.append(f"- HARD [{hf.validator}]: {hf.message}")
        for w in validation.warnings:
            lines.append(f"- warn [{w.validator}]: {w.message}")
        lines.append("")

    if test_output:
        lines.append("## test output")
        lines.append("```")
        lines.append(test_output)
        lines.append("```")
        lines.append("")

    out.write_text("\n".join(lines), encoding="utf-8")
    return out

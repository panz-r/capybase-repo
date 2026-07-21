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
    alternates: list[CandidateResolution] | None = None,
    validation: VerificationResult | None = None,
    test_output: str | None = None,
    resume_hint: str | None = None,
    consensus: dict | None = None,
    resurrections: list | None = None,
    advisories: list[str] | None = None,
    reconciliation_report: str | None = None,
) -> Path:
    """Write ``final/review-bundle.md`` and return its path.

    ``alternates`` are other cluster representatives from the consensus vote;
    when present they're rendered as a side-by-side comparison so the developer
    can pick between the top-K variations. ``consensus`` carries the entropy/
    agreement stats for display.

    ``resurrections`` is a list of :class:`resurrection.ResurrectionFinding`
    (deliberately-deleted content the merge result brought back). When present a
    ``## suspected resurrections`` section lists each finding so the developer
    can decide whether the reanimation was intentional or an undo of a cleanup.

    ``advisories`` is a list of human-readable strings from advisory journal
    events (#idea 4): subsystems that degraded silently during the run (e.g.
    "history unavailable: rebase plan build failed"). When present, a
    ``## advisories`` section lists them so the human sees WHY a history feature
    may not have applied, not just that the conflict escalated.

    ``reconciliation_report`` is the pre-rendered §13 comment-reconciliation
    report (from :func:`comment_reconciler.render_reconciliation_report`).
    When present, it's appended verbatim so the reviewer sees what the comment
    pass did (or failed to do) — counts + notable decisions + last verifier
    feedback. Rendered on BOTH success and failure (was failure-only before).
    """
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
        lines.append(_annotated_side_header(unit, "CURRENT_UPSTREAM_SIDE", "current"))
        lines.append("```")
        lines.append(unit.current.text)
        lines.append("```")
        lines.append(_annotated_side_header(unit, "REPLAYED_COMMIT_SIDE", "replayed"))
        lines.append("```")
        lines.append(unit.replayed.text)
        lines.append("```")
        lines.append("")
        # Side analysis: one line stating the conflict shape (e.g. "modify/delete:
        # CURRENT_UPSTREAM_SIDE DELETED this block"). Computed at extraction via
        # merge_intent.direction and stashed on structural_metadata. This is the
        # disambiguation that prevents a deliberate deletion from being read as an
        # addition. Omitted when no classification was recorded.
        md = unit.structural_metadata.get("merge_direction") or {}
        summary = md.get("summary")
        if summary:
            lines.append(f"> **side analysis:** {summary}")
            lines.append("")

    if candidate is not None:
        lines.append("## best candidate")
        lines.append(f"- model: `{candidate.model_name}` (prompt `{candidate.prompt_version}`)")
        # Explicit provenance (#9 step 8), when the candidate carries it.
        provenance = getattr(candidate, "provenance", "") or ""
        if provenance:
            from capybase.provenance import provenance_label

            lines.append(f"- via: {provenance_label(provenance)}")
        lines.append(f"- self-reported confidence: {candidate.self_reported_confidence}")
        lines.append(f"- needs_human: {candidate.needs_human}")
        lines.append("```")
        lines.append(candidate.resolved_text)
        lines.append("```")
        if candidate.explanation:
            lines.append(f"\n> {candidate.explanation}")
        lines.append("")

    # Side-by-side top-K comparison: when consensus produced multiple clusters,
    # show the alternate variations so the developer can pick. This is the
    # "safe escalation" view — the model was uncertain, so we present the top
    # candidates for human judgment.
    if alternates:
        lines.append("## alternate candidates (consensus split)")
        if consensus:
            ent = consensus.get("entropy")
            agr = consensus.get("agreement_score")
            if ent is not None:
                lines.append(f"- consensus entropy: {ent:.2f}")
            if agr is not None:
                lines.append(f"- agreement score: {agr:.2f}")
            lines.append("")
        for i, alt in enumerate(alternates, 1):
            lines.append(f"### variation {i}")
            lines.append(f"- confidence: {alt.self_reported_confidence}")
            lines.append(f"- needs_human: {alt.needs_human}")
            if alt.explanation:
                lines.append(f"- explanation: {alt.explanation}")
            lines.append("```")
            lines.append(alt.resolved_text)
            lines.append("```")
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

    if resurrections:
        lines.append("## suspected resurrections")
        lines.append(
            "The rebase result brought back content the target branch deliberately "
            "deleted (a cleanup predating the replayed commits). Review whether each "
            "reanimation was intentional or an accidental undo of the deletion."
        )
        lines.append("")
        for finding in resurrections:
            commit = getattr(finding, "deleting_commit", "") or "(unknown commit)"
            n = getattr(finding, "resurrected_line_count", 0) or 0
            lines.append(
                f"### `{finding.path}` — {n} lines back (removed by `{commit}`)"
            )
            for blk in finding.blocks[:3]:
                cov = getattr(blk, "coverage", 0.0)
                lines.append(f"- block ({cov:.0%} coverage):")
                lines.append("```")
                shown = blk.text.split("\n")
                if len(shown) > 20:
                    lines.extend(shown[:20])
                    lines.append(f"... ({len(shown) - 20} more lines)")
                else:
                    lines.extend(shown)
                lines.append("```")
        lines.append("")

    if advisories:
        lines.append("## advisories")
        lines.append(
            "History-aware subsystems that degraded silently during this run. "
            "If a feature you expected (future probe, obligations, branch intent) "
            "didn't apply, the reason is likely here."
        )
        lines.append("")
        for a in advisories:
            lines.append(f"- {a}")
        lines.append("")

    if reconciliation_report:
        # The §13 comment-reconciliation report is pre-rendered by the caller
        # (comment_reconciler.render_reconciliation_report). Append verbatim.
        lines.append(reconciliation_report.rstrip("\n"))
        lines.append("")

    out.write_text("\n".join(lines), encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Side classification + provenance rendering (modify/delete disambiguation)
#
# A conflict unit's three sides are raw text; without a label saying what each
# side *did*, a deliberate deletion (current side empty, base full) can look
# like an addition in the non-empty replayed side. These helpers annotate each
# side header with its classification (DELETED / ADDED / MODIFIED / unchanged)
# and the commit that introduced it (already-collected provenance), and render
# a one-line side-analysis stating the conflict shape. This is the single fix
# that would have made the edit_file.rs modify/delete conflict unambiguous.


# Human-readable label for a side's classification, appended to its header.
_SIDE_LABEL = {
    "added": "ADDED this content",
    "deleted": "DELETED this block",
    "modified": "MODIFIED this block",
    "unchanged": "unchanged from base",
}


def _annotated_side_header(unit: ConflictUnit, display_label: str, side_key: str) -> str:
    """Render ``### <label> — <classification> (<N lines>; <provenance>)``.

    ``side_key`` is the key into ``structural_metadata["merge_direction"]`` and
    ``["provenance"]`` (``"current"`` or ``"replayed"``). Falls back to the bare
    header when no classification is recorded, so older units still render fine.
    """
    md = unit.structural_metadata.get("merge_direction") or {}
    kind = md.get(side_key)
    annotation = _SIDE_LABEL.get(kind, "")
    parts = [display_label]
    if annotation:
        parts.append(f"— {annotation}")
    n = (getattr(_side(unit, side_key), "text", "") or "").count("\n") + 1
    parts.append(f"({n} lines)")
    prov = (unit.structural_metadata.get("provenance") or {}).get(side_key) or {}
    subject = prov.get("subject")
    if subject:
        parts.append(f"introduced by `{subject}`")
    return f"### {' '.join(parts)}"


def _side(unit: ConflictUnit, side_key: str) -> ConflictSide:
    """The ConflictSide object for ``side_key`` ('current' | 'replayed' | 'base')."""
    return getattr(unit, side_key)

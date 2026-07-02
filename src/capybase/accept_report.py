"""Semantic post-merge accept reports (#4).

For each accepted resolution, a short human-readable "why we accepted this merge"
report: which side obligations were preserved, that markers/syntax validated, and
the test verdict. This makes accepts auditable — a human (or future calibration)
can see *why* capybase trusted a merge, and a bad accept is easier to diagnose
than a bare "candidate_accepted" journal line.

The report is step-scoped (tests are step-level — known only after all units in a
step resolve), so :func:`build_accept_report` takes a step's worth of
:class:`UnitOutcome`s plus the step's test verdict. It composes three already-
derived signals rather than recomputing anything:

- **obligations** (#3, :mod:`obligations`) — the load-bearing edits per side,
  rendered as "preserved current-side change: ...".
- **classification** (#2, :mod:`classifier`) — the routing band, when present.
- **validation + tests** — markers/syntax passed, and the test verdict.

Pure (no I/O); the orchestrator owns persistence. Empty for a step with no
accepted units (an escalation step), so the caller can omit it there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from capybase.orchestrator import UnitOutcome


def build_accept_report(
    outcomes: list["UnitOutcome"],
    *,
    tests_passed: bool | None,
    test_verdict: str | None = None,
) -> str:
    """Render a markdown accept report for one step's accepted units.

    ``outcomes`` is a step's :class:`UnitOutcome` list; only those with an
    accepted candidate are reported (others are skipped). ``tests_passed`` /
    ``test_verdict`` are the step-level test-gate result (``None`` = the gate
    didn't run / was skipped). Returns the markdown body (no leading ``#``); the
    caller prepends a header / persists it. Empty when no unit was accepted.
    """
    accepted = [o for o in outcomes if o.accepted is not None]
    if not accepted:
        return ""

    lines: list[str] = []
    for outcome in accepted:
        unit = outcome.unit
        cand = outcome.accepted
        lines.append(f"### {unit.path} — {unit.unit_id}")
        # How the unit was resolved (the candidate's prompt_version / model_name
        # encodes the path: structural, sbcr, block_capture, or the LLM model).
        via = _via_label(cand)
        if via:
            lines.append(f"- resolved via: {via}")
        # Exact-reuse auditability (#idea 8): when resolved via verbatim reuse,
        # surface the source prior + explanation so a human can see WHICH prior
        # was replayed and why (not just "exact history reuse").
        if getattr(cand, "provenance", "") == "exact_history_reuse" and cand.explanation:
            lines.append(f"- reuse source: {cand.explanation}")
        # Classification band (#2), when routing ran (LLM path only).
        classification = getattr(outcome, "classification", None)
        band = _band_line(classification)
        if band:
            lines.append(f"- {band}")
        # Preserved obligations (#3): the load-bearing edits each side made.
        lines.extend(_obligation_lines(unit, cand))
        # Validation: markers + syntax.
        lines.extend(_validation_lines(outcome.validation))
        # Consensus, when self-consistency ran (a confidence signal).
        lines.extend(_consensus_lines(outcome))
        # History-aware evidence (#history step 4): compact history features.
        lines.extend(_history_lines(outcome))
        # Explainable retrieval (#9 step 5): why each few-shot example was chosen.
        lines.extend(_retrieval_lines(outcome))
        lines.append("")

    # Step-level test verdict (applies to the whole staged resolution).
    lines.append(f"tests: {_test_line(tests_passed, test_verdict)}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# per-signal rendering (pure)
# ---------------------------------------------------------------------------


def _via_label(cand: Any) -> str:
    """A short 'how was this resolved' label from the candidate.

    Reads the explicit ``provenance`` field first (#9 step 8); falls back to
    inferring from ``prompt_version``/``model_name`` for candidates serialized
    before provenance existed (empty string). Never raises.
    """
    from capybase.provenance import LEGACY_PROVENANCE, provenance_label

    provenance = getattr(cand, "provenance", LEGACY_PROVENANCE) or LEGACY_PROVENANCE
    if provenance != LEGACY_PROVENANCE:
        # Live value: use the human label, enriching the structural rule detail
        # (e.g. "deterministic structural (insertion_union)") when available.
        if provenance == "deterministic_structural":
            rule = _structural_rule(getattr(cand, "prompt_version", ""))
            if rule:
                return f"{provenance_label(provenance)} ({rule})"
        return provenance_label(provenance)
    # Legacy fallback: a candidate with no provenance — infer the old way so
    # historical/serialized data still renders a useful label.
    pv = getattr(cand, "prompt_version", "") or ""
    model = getattr(cand, "model_name", "") or ""
    if pv.startswith("structural."):
        return f"deterministic ({pv.split('.', 1)[1]})"
    if pv == "cegis_block_capture.v1":
        return "block-capture (keep/delete decision)"
    if pv.startswith("sbcr"):
        return "combination search"
    if model:
        return f"model ({model})"
    return ""


def _structural_rule(prompt_version: str) -> str:
    """The rule name from a ``structural.<rule>`` prompt_version, or ''."""
    pv = prompt_version or ""
    if pv.startswith("structural."):
        return pv.split(".", 1)[1]
    return ""


def _band_line(classification: Any) -> str:
    """The classification band line, or '' when no classification ran."""
    if classification is None:
        return ""
    band = getattr(classification, "band", None)
    if not band:
        return ""
    reasons = getattr(classification, "reasons", None) or []
    # One reason (the headline) keeps the line short; the rest are in the journal.
    head = reasons[0] if reasons else band
    return f"difficulty: {band} ({head})"


def _obligation_lines(unit: Any, cand: Any) -> list[str]:
    """Render the preserved-obligation lines for one accepted unit.

    Re-derives the obligations and confirms they're satisfied by the accepted
    candidate (they should be — it passed the ObligationValidator — but the
    report states it explicitly so a human sees WHAT was preserved, not just
    that the check passed). A side with no obligations (unchanged) is omitted.
    """
    try:
        from capybase.obligations import (
            extract_obligations,
            obligations_satisfied,
        )

        obligations = extract_obligations(unit)
        _, dropped = obligations_satisfied(obligations, getattr(cand, "resolved_text", "") or "")
    except Exception:  # noqa: BLE001 - the report is advisory; never block on it
        return []
    out: list[str] = []
    if not obligations.current.empty:
        preserved = [s for s in obligations.current.summary_lines()
                     if not any(s in d for d in dropped)]
        out.append(f"- preserved CURRENT: {', '.join(preserved) or '(none — flagged)'}")
    if not obligations.replayed.empty:
        preserved = [s for s in obligations.replayed.summary_lines()
                     if not any(s in d for d in dropped)]
        out.append(f"- preserved REPLAYED: {', '.join(preserved) or '(none — flagged)'}")
    return out


def _validation_lines(validation: Any) -> list[str]:
    """Markers + syntax lines from the verification result."""
    if validation is None:
        return ["- validation: (not run)"]
    feats = getattr(validation, "features", {}) or {}
    lines: list[str] = []
    markers = feats.get("markers_remaining")
    lines.append("- no conflict markers" if not markers else "- ! conflict markers remain")
    syntax = feats.get("syntax_passed")
    if syntax is True:
        lines.append("- syntax passed")
    elif syntax is False:
        lines.append("- ! syntax failed")
    # Hard-failure count, when non-zero, is worth surfacing (a passed validation
    # has none; a non-zero count means warnings survived).
    hard = getattr(validation, "hard_failures", None) or []
    if hard:
        lines.append(f"- hard failures: {len(hard)}")
    return lines


def _consensus_lines(outcome: "UnitOutcome") -> list[str]:
    """The consensus/agreement line, when self-consistency ran."""
    rep = getattr(outcome, "consensus", None)
    if rep is None:
        return []
    agreement = getattr(rep, "agreement_score", None)
    clusters = getattr(rep, "cluster_count", None)
    if agreement is None and clusters is None:
        return []
    parts = []
    if agreement is not None:
        parts.append(f"agreement {agreement:.2f}")
    if clusters is not None:
        parts.append(f"{clusters} cluster(s)")
    return [f"- consensus: {', '.join(parts)}"] if parts else []


def _test_line(tests_passed: bool | None, test_verdict: str | None) -> str:
    """The step-level test-verdict line."""
    if tests_passed is None:
        return "skipped (no test gate)"
    if tests_passed:
        return f"passed{f' ({test_verdict})' if test_verdict else ''}"
    return f"FAILED{f' ({test_verdict})' if test_verdict else ''}"


def _history_lines(outcome: "UnitOutcome") -> list[str]:
    """History-aware evidence (#history step 4): compact replay-position +
    future-touch signals from the merged features dict. Empty when no history
    was active."""
    validation = getattr(outcome, "validation", None)
    if validation is None:
        return []
    feats = getattr(validation, "features", {}) or {}
    has_ctx = feats.get("history_has_context")
    if not has_ctx:
        return []
    parts: list[str] = []
    idx = feats.get("history_source_commit_index", -1)
    total = feats.get("history_source_commit_count", 0)
    if total and idx >= 0:
        parts.append(f"replay {idx + 1}/{total}")
    file_touches = feats.get("history_future_file_touch_count", 0)
    region_touches = feats.get("history_future_region_touch_count", 0)
    if region_touches:
        parts.append(f"{region_touches} future region touch(es)")
    elif file_touches:
        parts.append(f"{file_touches} future file touch(es)")
    return [f"- history: {', '.join(parts)}"] if parts else []


def _retrieval_lines(outcome: "UnitOutcome") -> list[str]:
    """Explainable-retrieval reasons (#9 step 5): why each few-shot example was
    chosen (same path/region kind/conflict shape, score, prior outcome). Empty
    when no retrieval ran or no explanations were recorded."""
    explanations = getattr(outcome, "retrieval_explanations", None) or []
    if not explanations:
        return []
    out = ["- retrieved examples:"]
    for expl in explanations[:3]:  # cap for report brevity
        out.append(f"  - {expl}")
    return out

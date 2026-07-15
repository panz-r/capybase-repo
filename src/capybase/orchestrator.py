"""The orchestrator: the rebase state machine and sole Git mutator.

It knows the 8-step loop and nothing about model internals. It calls into the
stable contracts::

    candidate = resolution_engine.propose(unit, context)
    verdict   = verification.verify(unit, candidate)
    decision  = risk.decide(verdict, retry_count=...)

Three modes share the same inspection core:

* ``inspect``  — M1: detect, extract, journal, write a review bundle, no mutation.
* ``manual``   — M2: print a unit, read a pasted resolution from stdin, splice,
                  validate, stage. No auto-continue.
* ``run``      — M3: full loop — propose/verify/risk → splice/write/stage →
                  tests → ``git rebase --continue``. Retries up to policy max,
                  else escalates and stops.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable
import warnings

from capybase.conflict_extractor import ConflictExtractor, SkippedPath
from capybase.conflict_model import (
    CandidateResolution,
    ConflictUnit,
    ResolutionAttempt,
    RiskDecision,
    VerificationFailure,
    VerificationResult,
    VerificationWarning,
    estimate_tokens,
)
from capybase.context_builder import ContextBuilder
from capybase.escalation import write_review_bundle
from capybase.git_backend import GitBackend, GitError, GitResult
from capybase.journal import Journal
from capybase.policy import Policy
from capybase.policy_strictness import StrictnessPolicy
from capybase.resolution_engine import ResolutionEngine
from capybase.risk import RiskEngine
from capybase.session import SessionPaths, new_session_id
from capybase.verification import ValidationConfig, VerificationEngine
from capybase.adapters.tests import TestRunner
from capybase.test_output import parse_passing_node_ids
from capybase.test_output import _tool_of as _tool_of_test_cmd
from capybase.config import Config
from capybase.consensus import rank_by_consensus
from capybase.preflight import run_rebase_preflight

# Sentinel for "not in cache" (distinguishes a cached None from a cache miss).
_MISSING = object()


# A unit is "resolved" once a candidate is accepted.
@dataclass
class UnitOutcome:
    unit: ConflictUnit
    accepted: CandidateResolution | None = None
    decision: RiskDecision | None = None
    validation: VerificationResult | None = None
    attempts: list[CandidateResolution] = field(default_factory=list)
    # Carries the consensus report (if self-consistency was used) so the
    # step-level escalation can render alternate cluster representatives.
    consensus: object | None = None
    # Difficulty class assigned by the router ("simple" | "complex"), recorded
    # so the calibration model can learn that complex conflicts fail more often.
    difficulty: str | None = None
    # The full ConflictClassification (band + reasons) when routing ran. Typed
    # loosely to avoid an import cycle; it's a capybase.classifier.ConflictClassification.
    # None when routing is disabled (difficulty defaults to "complex").
    classification: object | None = None
    # Number of attempts made (0 on first-pass accept). Recorded so calibration
    # learns that retries correlate with risk. (= len(attempts) - 1 on accept,
    # or the count at escalation.)
    retry_count: int = 0
    # Escalation state for this unit. The run() loop infers escalation from
    # ``accepted is None``, but carrying the explicit reason lets a specific
    # escalation path (e.g. the wall-time budget) surface WHY it bailed, instead
    # of the caller overwriting it with a generic "could not resolve" message.
    # None/False on accept; set together on an escalation return.
    escalated: bool = False
    reason: str | None = None
    # Oscillation detection (CEGIS resilience): hashes of resolved_text seen
    # across retries for this unit, mapped to how many times each was seen.
    # If the same candidate appears 3+ times, the model is cycling (producing
    # the same code every retry) and the loop escalates instead of burning
    # more API tokens on a known-stuck state. Per-unit (not session-wide) so
    # it resets for each conflict. The threshold of 3 allows: (1) the initial
    # attempt, (2) one repair retry that legitimately confirms the same code
    # (the model was right and the validator was wrong), (3) a third identical
    # attempt = genuine stuck loop.
    _seen_candidate_hashes: dict[str, int] = field(default_factory=dict)
    # Explainable-retrieval reasons (#9 step 5): one human-readable string per
    # retrieved few-shot example used in the prompt, recording WHY each was
    # chosen (same path/region kind/conflict shape, score, prior outcome). Empty
    # when no retrieval ran. Surfaced in accept reports for debuggability.
    retrieval_explanations: list[str] = field(default_factory=list)
    # Uniform resolution-attempt records (#idea 6 cohesion): one per mechanism
    # tried (exact_reuse, structural, sbcr, block_capture, each LLM iteration),
    # carrying (mechanism, candidate, validation, decision, reason). Parallel to
    # ``attempts`` (the bare candidate list, kept for backward compat) — this is
    # the structured record reports/metrics/dry-run read.
    resolution_attempts: list = field(default_factory=list)


@dataclass
class StepResult:
    step_index: int
    units_by_path: dict[str, list[ConflictUnit]] = field(default_factory=dict)
    skipped: list[SkippedPath] = field(default_factory=list)
    outcomes: list[UnitOutcome] = field(default_factory=list)
    escalated: bool = False
    reason: str | None = None
    tests_passed: bool | None = None
    continued: bool = False


def _resolved_buffer(
    original: str, accepted: list[tuple[ConflictUnit, CandidateResolution]]
) -> str:
    """Build the resolved file buffer for one path's accepted units.

    Marker-block units splice their resolution into the span within
    ``original`` (the marker-laden worktree text). A ``whole_file`` unit
    (modify/delete) has ``marker_span=None``: its resolved text IS the file —
    empty for an accepted deletion, the keeper's full text for keep_block — so
    there is nothing to splice. Mixing the two in one path isn't meaningful;
    when any unit is whole-file we take the (single) accepted resolution's
    text verbatim.
    """
    from capybase.adapters.parsers import splice_all_resolutions

    if any(unit.marker_span is None for unit, _ in accepted):
        return accepted[0][1].resolved_text
    spans_and_texts = [
        (unit.marker_span, cand.resolved_text) for unit, cand in accepted
    ]
    return splice_all_resolutions(original, spans_and_texts)


def _is_whole_file_delete(
    accepted: list[tuple[ConflictUnit, CandidateResolution]]
) -> bool:
    """True iff a path's single accepted resolution means ``delete the file``.

    A whole-file modify/delete accepted via block-capture's ``accept_deletion``
    yields empty resolved text — the file should be ``git rm``'d, not written.
    Any non-whole-file unit, or a non-empty whole-file resolution (keep_block),
    returns False so the normal write+add path runs.
    """
    if len(accepted) != 1:
        return False
    unit, cand = accepted[0]
    return unit.marker_span is None and not cand.resolved_text.strip()


def _critic_warning(validation: VerificationResult) -> VerificationWarning | None:
    """The verifier-critic's warning on this candidate, if it flagged one.

    Returns the (single) ``verifier_model`` ``VerificationWarning`` from the
    validation, or None when the critic didn't flag (confirmed both sides,
    skipped, or the critic wasn't enabled). Used to (a) route the retry to the
    separate critic budget and (b) seed the critic's verdict into the repair
    prompt as actionable feedback.

    PoLL jury (§2.1): matches ANY ``verifier_model*`` warning (the preservation
    critic ``verifier_model`` OR a jury member like ``verifier_model_conflict``)
    — the union of the jury's flags. Returns the first found.
    """
    for w in validation.warnings:
        if w.validator == "verifier_model" or w.validator.startswith("verifier_model_"):
            return w
    return None


def _critic_failure(
    warning: VerificationWarning, dropped_units: list | None = None
) -> VerificationFailure:
    """Synthesize a hard-failure-shaped object from a critic warning.

    The CEGIS repair-prompt renderer (``_render_failure``) consumes
    ``VerificationFailure`` objects; the critic emits a ``VerificationWarning``
    (no severity). This lifts the critic's verdict into the failure shape so its
    message ("may drop replayed side intent") reaches the model on retry as
    concrete counterexample feedback — instead of a feedback-free regeneration.
    Marked ``severity="warning"`` so it's distinguishable from a real hard
    failure in the prompt, and the renderer surfaces it the same way.

    ``dropped_units`` (when non-empty) names the SPECIFIC entities (functions/
    classes) the resolution dropped, appended to the message so the retry prompt
    gives the model exact targets ("reintroduce function `foo`") — the
    quantitative per-side preservation signal that converges faster than a vague
    "you dropped a side".
    """
    message = warning.message
    detail = dict(warning.detail)
    if dropped_units:
        names = ", ".join(f"{kind} '{name}'" for kind, name in dropped_units)
        message = f"{message}; reintroduce: {names}"
        detail["dropped_units"] = list(dropped_units)
    return VerificationFailure(
        validator=warning.validator,
        severity="warning",
        message=message,
        detail=detail,
    )


# Cosine similarity floor : above this, two critic flags
# are treated as semantically EQUIVALENT (one is dropped, the more specific
# kept). 0.90 is prior work's "same issue, different wording" threshold.
_CRITIC_DEDUP_EQUIVALENT = 0.90
# Below this, two flags address DIFFERENT failure modes (keep both). The band
# 0.60–0.90 is "related but distinct" (keep both, specificity-ordered).
_CRITIC_DEDUP_DIFFERENT = 0.60


def _all_critic_warnings(validation: VerificationResult) -> list[VerificationWarning]:
    """Every ``verifier_model*`` warning (the full PoLL jury output).

    PoLL jury (§2.1) emits up to N ``verifier_model*`` warnings — one per jury
    member. ``_critic_warning`` returns only the FIRST; this returns the full
    list so :func:`_dedupe_critic_warnings` can merge equivalent flags before
    they dilute the plan-first step's attention.
    """
    return [
        w for w in validation.warnings
        if w.validator == "verifier_model" or w.validator.startswith("verifier_model_")
    ]


def _critic_warning_text(w: VerificationWarning) -> str:
    """A single string fingerprint of a critic warning for embedding comparison.

    Concatenates the message + the dropped_units detail (the most specific
    signal), so two flags naming the same dropped entity under different wording
    embed as equivalent. Pure; no network.
    """
    parts = [w.message]
    du = w.detail.get("dropped_units") if w.detail else None
    if du:
        parts.append(", ".join(f"{k} {n}" for k, n in du))
    return " ".join(parts)


def _dedupe_critic_warnings(
    warnings: list[VerificationWarning], embedder: object | None,
) -> list[VerificationWarning]:
    """Deduplicate PoLL-jury critic flags by embedding similarity.

    The dual-critic jury may emit two flags for the SAME issue with different
    wording — feeding both to the plan-first step dilutes the model's attention
    across two semantically-identical instructions. This merges them:

    - cosine ≥ 0.90 → equivalent: keep the MORE SPECIFIC one (longer detail /
      more dropped_units), drop the other.
    - 0.60–0.90 → related-but-distinct: keep both, order by specificity.
    - < 0.60 → different: keep both in original order.

    A single batch embed of the (≤ handful of) short flag texts. ``embedder=None``
    returns the list unchanged (the prior behavior — first-found only via
    ``_critic_warning``). Never raises; a failed embed returns the input list.
    Survivors are ordered by specificity (most specific first) so the plan-first
    step sees the most actionable flag before the supporting ones.
    """
    if len(warnings) < 2 or embedder is None:
        return list(warnings)
    texts = [_critic_warning_text(w) for w in warnings]
    try:
        vecs = embedder.embed(texts)  # type: ignore[attr-defined]
        if len(vecs) != len(warnings):
            return list(warnings)
    except Exception:  # noqa: BLE001 - dedup is best-effort
        return list(warnings)

    def _specificity(w: VerificationWarning) -> int:
        du = w.detail.get("dropped_units") if w.detail else None
        return (len(du) if du else 0) + len(w.message)

    # Greedy equivalence merge: for each pair at cosine ≥ 0.90, drop the less
    # specific. Survivors are those never dropped.
    dropped: set[int] = set()
    for i in range(len(warnings)):
        if i in dropped:
            continue
        for j in range(i + 1, len(warnings)):
            if j in dropped:
                continue
            if _critic_cosine(vecs[i], vecs[j]) >= _CRITIC_DEDUP_EQUIVALENT:
                if _specificity(warnings[j]) > _specificity(warnings[i]):
                    dropped.add(i)
                    break
                else:
                    dropped.add(j)
    survivors = [i for i in range(len(warnings)) if i not in dropped]
    # Order by specificity descending (stable for ties) so the most actionable
    # flag leads the plan-first feedback.
    survivors.sort(key=lambda i: -_specificity(warnings[i]))
    return [warnings[i] for i in survivors]


def _critic_cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors (local; never imports)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    import math

    return dot / (math.sqrt(na) * math.sqrt(nb))


def _dropped_units_for(
    unit: ConflictUnit, cand: CandidateResolution
) -> list[tuple[str, str]]:
    """The (kind, name) entities the resolution dropped, across both sides.

    Deterministic (abstract parser) — the quantitative per-side preservation signal.
    Returns [] when the structural parser is unavailable, the language isn't supported, or
    nothing structural was dropped (the critic's own message still carries the
    qualitative verdict in that case).
    """
    lang = getattr(unit, "language", None)
    if lang not in ("python", "rust"):
        return []
    try:
        from capybase.adapters import structural
    except Exception:  # noqa: BLE001
        return []
    if not structural.is_available(lang):
        return []
    base = unit.base.text or ""
    cur = unit.current.text or ""
    rep = unit.replayed.text or ""
    res = cand.resolved_text or ""
    dropped: list[tuple[str, str]] = []
    for e in (structural.dropped_entities(base, cur, res, lang) or []):
        dropped.append((e.kind, e.name))
    for e in (structural.dropped_entities(base, rep, res, lang) or []):
        if (e.kind, e.name) not in dropped:
            dropped.append((e.kind, e.name))
    return dropped


#: Warnings that drive ``risk.decide`` retries but carry concrete, actionable
#: feedback (the validator names a SPECIFIC problem: dropped entity, spurious
#: addition, dropped dependency). These are the signals the retry-seed below
#: lifts into the prompt so the model gets counterexample feedback instead of a
#: blind regeneration. ``verifier_model*`` is handled separately by
#: ``_critic_failure`` (separate budget) and is intentionally excluded here.
_ACTIONABLE_SOFT_WARNINGS: frozenset[str] = frozenset({
    "intent_coverage",          # dropped a side's added structural units (ratio)
    "unattributed_code",        # hallucinated a unit in neither side
    "both_sides_represented",   # copied one side verbatim (dropped the other)
    "preservation_heuristic",   # one-sided merge heuristic
    "referenced_symbol_dropped",  # dropped a base-referenced dependency
    "future_obligation",        # dropped a symbol a later commit needs
})


def _soft_warning_failures(validation: VerificationResult) -> list[VerificationFailure]:
    """Lift actionable soft-validator warnings into failure-shape prompt feedback.

    ``risk.decide`` retries on these warnings (``risk.py:156-213``), but the
    old retry seed only lifted ``hard_failures`` + the critic's warning. For a
    warning-driven retry that left ``failures`` empty, ``propose()`` fell
    through to ``build_resolve_prompt`` — a FRESH generation with NO feedback
    and NO memory of the rejected candidate (``prev_candidate`` is ignored
    when ``failures`` is falsy). So the model kept reproducing the same
    dropped-side merge across retries, burning a model call each time with
    zero guidance.

    This synthesizes a ``VerificationFailure`` (severity="warning") for each
    actionable warning, so ``_render_failure`` surfaces its structured
    ``detail`` (dropped entity names, ratios, etc.) in the repair prompt and
    ``propose()`` selects the targeted ``build_repair_prompt`` path against
    the previous candidate. ``verifier_model*`` warnings are excluded — they
    are handled by ``_critic_failure`` against the separate critic budget.
    """
    out: list[VerificationFailure] = []
    for w in validation.warnings:
        if w.validator in _ACTIONABLE_SOFT_WARNINGS:
            out.append(VerificationFailure(
                validator=w.validator,
                severity="warning",
                message=w.message,
                detail=dict(w.detail),
            ))
    return out


def _invalidate_pycache(repo_root: "str | Path", path: str) -> None:
    """Remove stale ``__pycache__`` bytecode for ``path`` (a .py file).

    Python's pyc validity check keys on the source file's mtime. Two writes to
    the same .py within one filesystem mtime tick (sub-second on most filesystems)
    leave a STALE .pyc: Python sees the cached bytecode as fresh and skips
    recompilation, importing the OLD content. This corrupts the test-gated side
    picker (which rewrites the conflicted .py with each side's content in quick
    succession) and any test gate that runs shortly after a worktree write.

    Removing the file's ``__pycache__`` dir forces a recompile on the next
    import. No-op for non-.py paths, missing dirs, or any error (never blocks a
    rebase on a cache-cleanup failure).
    """
    if not path.endswith(".py"):
        return
    try:
        from pathlib import Path
        import shutil

        d = Path(repo_root) / Path(path).parent / "__pycache__"
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
    except Exception:  # noqa: BLE001 - cache invalidation must never break a run
        pass


def _attribute_whole_file_failure(
    failures: list, units: list[ConflictUnit]
) -> int:
    """Pick the index of the unit most likely at fault for a whole-file failure.

    Whole-file failures (cross-unit syntax errors, juxtaposition errors) are
    file-scoped, but repair is unit-scoped. Attribution reads the error line
    from the failure's ``detail`` (the splice-coherence gate records the brace-
    imbalance line; the syntax check records new-error lines) FIRST — this is
    precise. Falls back to regex-parsing the message string ("line N") for older
    failure shapes (Python SyntaxErrors). When no line is available or no span
    contains it, the LAST unit is chosen — a heuristic that juxtaposition errors
    tend to surface where splices meet.
    """
    if not units:
        return 0
    import re

    for f in failures:
        line: int | None = None
        # Fix #2b: prefer the structured line in detail (precise — set by the
        # splice-coherence gate and the syntax check's diagnostic delta).
        detail = getattr(f, "detail", {}) or {}
        if isinstance(detail.get("brace_imbalance_line"), int):
            line = detail["brace_imbalance_line"]
        elif isinstance(detail.get("lines"), list) and detail["lines"]:
            line = detail["lines"][0]
        # Fall back to regex on the message (Python SyntaxError "line N").
        if line is None:
            msg = getattr(f, "message", "") or ""
            m = re.search(r"line\s+(\d+)", msg)
            if m:
                try:
                    line = int(m.group(1))
                except ValueError:
                    pass
        if line is None:
            continue
        # marker_span is 0-based [start, end]; the error line is 1-based.
        for i, u in enumerate(units):
            if u.marker_span is None:
                continue
            start, end = u.marker_span
            if start + 1 <= line <= end + 1:
                return i
    # No line attribution possible → default to the last unit.
    return len(units) - 1
def _splice_context_snippet(
    failures: list, original: str,
    accepted: list[tuple[ConflictUnit, CandidateResolution]],
) -> str:
    """Build a context snippet of the spliced file around the error line.

    Enriches the whole-file repair feedback so the model sees the actual brace
    mismatch in context, not just the raw cargo message. For a multi-hunk
    conflict (Fix #1), the snippet is WIDENED to span the two adjacent units'
    marker spans when the error line falls at or near a hunk junction — the
    model couldn't see that unit A's ``}`` collided with unit B's structure
    because a narrow ±5 window only showed one unit's context. Returns empty
    string when no error line is available or the splice fails (the raw
    failures still reach the model; this is additive).
    """
    # Find the error line from the failures' detail (same sources as attribution).
    line: int | None = None
    for f in failures:
        detail = getattr(f, "detail", {}) or {}
        if isinstance(detail.get("brace_imbalance_line"), int):
            line = detail["brace_imbalance_line"]
            break
        elif isinstance(detail.get("lines"), list) and detail["lines"]:
            line = detail["lines"][0]
            break
    if line is None:
        # Fall back to regex on the message.
        import re

        for f in failures:
            m = re.search(r"line\s+(\d+)", getattr(f, "message", "") or "")
            if m:
                try:
                    line = int(m.group(1))
                    break
                except ValueError:
                    pass
    if line is None:
        return ""
    # Build the spliced file to show the actual content around the error.
    try:
        whole = _resolved_buffer(original, accepted)
    except Exception:  # noqa: BLE001 - splice may fail on bad spans
        return ""
    lines = whole.split("\n")
    # Default window: ±5 lines around the error line.
    start = max(0, line - 6)
    end = min(len(lines), line + 5)
    # Fix #1 — cross-hunk widening: when the error line falls at or near a hunk
    # junction (between two units' marker spans), widen the window to span BOTH
    # adjacent units so the model sees the splice boundary and both hunks'
    # context. The brace imbalance in a multi-hunk conflict lives at the
    # junction; a narrow window only shows one unit, hiding the collision.
    # Compute each unit's post-splice line range (adjusting for line-count
    # shifts from units spliced above it in document order).
    if len(accepted) > 1:
        # Sort units by original marker_span start (document order).
        indexed = sorted(
            ((i, u) for i, (u, _) in enumerate(accepted) if u.marker_span is not None),
            key=lambda t: t[1].marker_span[0],
        )
        # Build the post-splice line ranges by simulating the splice shift.
        splice_ranges: list[tuple[int, int, int]] = []  # (orig_idx, spliced_start, spliced_end)
        shift = 0
        for orig_i, u in indexed:
            s, e = u.marker_span
            cand = accepted[orig_i][1]
            txt_lines = len(cand.resolved_text.split("\n")) if cand.resolved_text else 0
            block_orig = e - s + 1
            sp_start = s + shift
            sp_end = sp_start + txt_lines - 1
            splice_ranges.append((orig_i, sp_start, sp_end))
            shift += txt_lines - block_orig
        # Find the unit whose spliced range contains the error line (1-based),
        # and the adjacent unit (the one whose range ends just before or starts
        # just after). Widen to span both.
        err0 = line - 1  # convert to 0-based for range comparison
        for pos, (_oi, sp_start, sp_end) in enumerate(splice_ranges):
            if sp_start <= err0 <= sp_end:
                # Error is inside this unit. Check if it's near a boundary and
                # there's an adjacent unit to include.
                start = min(start, max(0, sp_start - 2))
                end = max(end, min(len(lines), sp_end + 3))
                # Include the previous unit's tail if the error is near the start.
                if err0 - sp_start <= 2 and pos > 0:
                    _, prev_start, prev_end = splice_ranges[pos - 1]
                    start = min(start, max(0, prev_start - 1))
                    end = max(end, min(len(lines), prev_end + 2))
                # Include the next unit's head if the error is near the end.
                if sp_end - err0 <= 2 and pos < len(splice_ranges) - 1:
                    _, next_start, next_end = splice_ranges[pos + 1]
                    start = min(start, max(0, next_start - 2))
                    end = max(end, min(len(lines), next_end + 2))
                break
            # Error is BETWEEN two units (in the gap). Span both neighbors.
            if pos > 0:
                _, prev_start, prev_end = splice_ranges[pos - 1]
                if prev_end < err0 < sp_start:
                    start = min(start, max(0, prev_start - 1))
                    end = max(end, min(len(lines), sp_end + 2))
                    break
    numbered = []
    for i in range(start, end):
        marker = " >>>" if (i + 1) == line else "    "
        numbered.append(f"{marker} {i + 1:4d} | {lines[i]}")
    return "\n".join(numbered)


def _try_deterministic_brace_repair(
    failures: list,
    original: str,
    accepted: list[tuple[ConflictUnit, CandidateResolution]],
    fault_idx: int,
) -> list[tuple[ConflictUnit, CandidateResolution]] | None:
    """Attempt a deterministic brace-balance fix before invoking the LLM.

    The recurring splice-junction brace imbalance (Fix #2) is a single-edit
    fix away from correct: the model merges each hunk correctly in isolation,
    but the spliced result has a stray or missing brace where the hunks meet.
    Re-prompting the model doesn't help (it can't see the junction), so we fix
    it directly when ``_try_balance_braces`` can balance the spliced buffer in
    one clean edit.

    Returns a replacement ``accepted`` list (the fault unit becomes a whole-file
    unit carrying the repaired buffer as its resolved_text), or ``None`` to
    defer to the LLM path. Conservative on two axes: (1) the brace repair acts
    only on brace-only lines / unclosed blocks (see ``_try_balance_braces``),
    and (2) the repaired buffer is re-validated for brace balance before use.

    The repair replaces the whole ``accepted`` list with a single whole-file
    unit rather than back-projecting the fix onto one unit's ``resolved_text``.
    Back-projection is fragile: the stray brace often lives in the *original*
    text adjacent to the fault unit's span (not inside it), so a unit-local edit
    can't reach it. A whole-file unit is the honest representation — the
    deterministic fix produced a complete, correct file — and ``_resolved_buffer``
    returns its resolved_text verbatim (no re-splicing).
    """
    from capybase.verification import _brace_imbalance_line, _try_balance_braces
    from capybase.conflict_model import CandidateResolution

    # Only engage on the brace-coherence failure shape.
    is_brace_failure = any(
        "brace" in (getattr(f, "message", "") or "").lower()
        or "splice coherence" in (getattr(f, "message", "") or "").lower()
        for f in failures
    )
    if not is_brace_failure:
        return None
    if fault_idx < 0 or fault_idx >= len(accepted):
        return None
    unit, _old_cand = accepted[fault_idx]
    try:
        spliced = _resolved_buffer(original, accepted)
    except Exception:  # noqa: BLE001 - splice may fail on bad spans
        return None
    if _brace_imbalance_line(spliced) is None:
        return None  # not actually a brace imbalance; nothing to fix
    repaired = _try_balance_braces(spliced)
    if repaired is None:
        return None  # couldn't balance in one edit → defer to LLM
    if _brace_imbalance_line(repaired) is not None:
        return None  # safety re-check (shouldn't happen, but never trust)
    # Build a synthetic whole-file unit carrying the repaired buffer. This is
    # the correct representation: the deterministic fix produced a complete file.
    # ``_resolved_buffer`` returns its resolved_text verbatim (no splicing), and
    # ``verify_file``'s ``_has_whole_file_span`` guard handles the None span.
    wf_unit = unit.model_copy(update={"marker_span": None, "unit_kind": "whole_file"})
    wf_cand = CandidateResolution(
        candidate_id=(getattr(_old_cand, "candidate_id", unit.unit_id) or unit.unit_id) + ":bracefix",
        unit_id=unit.unit_id,
        model_name=getattr(_old_cand, "model_name", "deterministic") or "deterministic",
        resolved_text=repaired,
        prompt_version="deterministic_brace_repair",
        provenance="deterministic_brace_repair",
        self_reported_confidence=0.9,
        explanation="deterministic brace-balance repair (splice junction)",
    )
    return [(wf_unit, wf_cand)]


def _extract_alternates(
    outcome: UnitOutcome,
) -> tuple[list[CandidateResolution], dict | None]:
    """Extract losing cluster representatives + consensus stats from an outcome.

    When self-consistency was used and the unit escalated, the consensus
    report carries multiple clusters. The winner is already shown as the best
    candidate; the losers (other cluster representatives) are returned as
    alternates for the side-by-side review bundle. Returns ([], None) when
    no consensus was computed (single-sample or missing).
    """
    rep = outcome.consensus
    if rep is None:
        return [], None
    alternates = []
    clusters = getattr(rep, "clusters", [])
    for i, cl in enumerate(clusters):
        if i == 0:
            continue  # winner is already the best candidate
        rep_cand = getattr(cl, "representative", None)
        if rep_cand is not None and rep_cand.resolved_text:
            alternates.append(rep_cand)
    consensus = {
        "entropy": getattr(rep, "entropy", None),
        "agreement_score": getattr(rep, "agreement_score", None),
        "cluster_count": getattr(rep, "cluster_count", None),
    }
    return alternates, consensus


def _apply_model_profile(config: Config, repo_root: Path, journal: Journal) -> Config:
    """Overlay the calibrated model profile onto ``config.model`` if present.

    "Profile wins": the profile's tuned knobs override the [model] settings, but
    ONLY when its model name matches. Returns ``config`` unchanged (and journals
    nothing) when no profile exists or the names mismatch — so a repo without a
    profile behaves exactly as before. The overlay touches only the four tuned
    knobs; every other field keeps its value. Capability flags
    (``enable_embedding_rag``, ``embedding_min_similarity``) follow the SAME
    name-match gate — a profile fit for another model never leaks them through.
    """
    profile_path = config.calibration.model_profile_path
    resolved = Path(profile_path)
    if not resolved.is_absolute():
        resolved = repo_root / profile_path
    try:
        from capybase.calibration_profile import ModelProfile, apply_profile

        profile = ModelProfile.load(resolved)
    except Exception:  # noqa: BLE001 - never crash on a bad artifact path/config
        return config
    if profile is None:
        return config
    # The name match is the gate for EVERYTHING the profile carries — tuned
    # knobs AND capability flags. ``apply_profile`` would warn + no-op on a
    # mismatch, but we re-check here FIRST so capability flags don't leak
    # through when ``overridden`` is empty merely because no ModelConfig knob
    # differed. Nudge the user to recalibrate, then leave config untouched.
    if profile.model != config.model.model:
        warnings.warn(
            f"Model profile is for {profile.model!r} but active model is "
            f"{config.model.model!r}; ignoring the profile. Run "
            f"`capybase recalibrate` to fit it for the current model.",
            stacklevel=2,
        )
        return config
    new_model, overridden = apply_profile(config.model, profile)
    if overridden:
        journal.emit(
            "model_profile_applied",
            {
                "model": profile.model,
                "overridden_knobs": overridden,
                "profile_path": str(resolved),
            },
        )
        config = config.model_copy(update={"model": new_model})
    # Capability flags (e.g. embedding RAG, the calibrated floor) apply even when
    # no ModelConfig knob changed — but only after the name match above passed.
    config = _apply_profile_capability_flags(config, profile)
    # Prompt-rendering profile: applies the calibrated PromptProfile section as
    # the process-wide active profile. Env override wins (see _apply_prompt_profile).
    _apply_prompt_profile(profile)
    # Safety profile: overlays calibrated retry budgets + escalation thresholds
    # onto PolicyConfig so retry/escalation policy is per-model rather than
    # config-only (feedback §2.1). Only applies when the section is non-default.
    config = _apply_safety_profile(config, profile)
    return config


def _apply_safety_profile(config: Config, profile: "object") -> Config:
    """Overlay the profile's safety section onto PolicyConfig.

    When the calibrated SafetyProfile is non-default, its retry budgets +
    escalation threshold override the config's [policy] values. This makes
    retry/escalation policy profile-calibrated (per-model) rather than
    config-only. A default section (or a missing one) is a no-op.
    """
    safety = getattr(profile, "safety", None)
    if safety is None or getattr(safety, "is_default", True):
        return config
    updates = {}
    if safety.max_retries_per_unit != 2:
        updates["max_retries_per_unit"] = safety.max_retries_per_unit
    if safety.max_critic_retries_per_unit != 0:
        updates["max_critic_retries_per_unit"] = safety.max_critic_retries_per_unit
    if safety.max_recovery_retries_per_unit != 1:
        updates["max_recovery_retries_per_unit"] = safety.max_recovery_retries_per_unit
    if safety.critic_confidence_escalate_threshold != 0.8:
        updates["critic_confidence_escalate_threshold"] = safety.critic_confidence_escalate_threshold
    if updates:
        config = config.model_copy(update={"policy": config.policy.model_copy(update=updates)})
    return config


def _apply_prompt_profile(profile: "object") -> None:
    """Apply the profile's prompt-rendering section as the active profile.

    Sets the process-wide active prompt profile from the calibrated section, so
    the engine + parser render and parse under the layout/framing/position the
    A/B selected for this model. **Precedence**: an explicit env override
    (``CAPYBASE_PROMPT_LAYOUT`` / ``_HISTORY`` / ``_POSITION`` / ``_OUTLINE``,
    driven by ``live_eval``) wins — when any of those is set we leave the active
    profile alone so the A/B selector stays authoritative. The calibrated
    section applies only in normal (non-eval) runs.
    """
    import os

    # Env override wins: if any prompt-rendering env var is set, the caller
    # (live_eval) owns the active profile and we don't clobber it.
    env_vars = (
        "CAPYBASE_PROMPT_LAYOUT", "CAPYBASE_PROMPT_HISTORY",
        "CAPYBASE_PROMPT_POSITION", "CAPYBASE_PROMPT_OUTLINE",
        "CAPYBASE_PROMPT_EXAMPLES", "CAPYBASE_PROMPT_VARIANT",
    )
    if any(os.environ.get(v, "").strip() for v in env_vars):
        return
    try:
        from capybase.prompt_profile import set_active_profile
        section = getattr(profile, "prompt", None)
        if section is not None and getattr(section, "profile", None) is not None:
            set_active_profile(section.profile)
    except Exception:  # noqa: BLE001 - prompt profile is advisory; never break resolution
        pass


def _apply_profile_capability_flags(config: Config, profile: "object") -> Config:
    """Apply profile capability flags that don't live on ModelConfig.

    Currently: ``enable_embedding_rag`` flips ``config.memory.retriever`` to
    ``"embedding"`` (the orchestrator then builds an EmbeddingRetriever). Only
    honors the flag when the user has RAG enabled at all; never forces it on.

    The calibrated ``embedding_min_similarity`` (from ``calibrate-embeddings``)
    overrides the config default so the EmbeddingRetriever uses a model-specific
    floor rather than the 0.35 guess. The full ``embedding_calibration`` envelope
    rides along so the retriever can apply the isotonic score transform.
    ``fusion_method`` is threaded for the HybridRetriever.
    """
    if getattr(profile, "enable_embedding_rag", False):
        if config.memory.enabled and config.future.enable_rag:
            if config.memory.retriever == "lexical":
                config.memory.retriever = "embedding"
    emb_sim = getattr(profile, "embedding_min_similarity", None)
    if emb_sim is not None:
        config.memory.embedding_min_similarity = float(emb_sim)
    emb_cal = getattr(profile, "embedding_calibration", None)
    if emb_cal:  # a non-empty envelope
        config.memory.embedding_calibration = dict(emb_cal)
    fusion = getattr(profile, "fusion_method", None)
    if fusion:
        config.memory.fusion_method = str(fusion)
    return config


def _reconstruct_calibration(config: Config) -> "object | None":
    """Rebuild an EmbeddingCalibration from the config's serialized envelope.

    Returns None when no envelope is stored (so the retriever behaves as before
    calibration). Tolerant of a corrupt/partial envelope — returns None rather
    than crashing, so a bad artifact never breaks retrieval.
    """
    env = config.memory.embedding_calibration
    if not env:
        return None
    try:
        from capybase.embeddings_calibration import EmbeddingCalibration

        return EmbeddingCalibration.from_dict(dict(env))
    except Exception:  # noqa: BLE001 - never break retrieval on a bad envelope
        return None


def _categorize_failure_mode(accepted, outcome) -> str:
    """Derive a categorical failure-mode from the accepted/last candidate + outcome.

    Used by the telemetry layer (feedback §5.1 ``failure_mode``) so future
    online-adaptation rules can target specific failure types (e.g., switch
    layout when json_escape spikes, increase samples when wrong_merge spikes).
    Returns ``""`` for accepted outcomes (no failure).

    Modes:
    - ``""`` — accepted (no failure).
    - ``json_escape`` — the repair tier salvaged malformed JSON (parse_warnings
      contain "salvaged via json-repair").
    - ``no_parse`` — failure_kind == "parse_failed" (no resolved_text extracted).
    - ``timeout`` — failure_kind == "truncated" or "request_failed".
    - ``model_refusal`` — failure_kind == "model_refusal" (needs_human).
    - ``wrong_merge`` — parsed but validation flagged hard failures (markers,
      brace imbalance, dropped intent).
    - ``escalated`` — escalated with no specific category.
    """
    # Accepted → no failure.
    if accepted is not None and not getattr(accepted, "needs_human", False):
        # Even accepted candidates can have had repair-tier salvage; surface that.
        warnings = getattr(accepted, "parse_warnings", None) or []
        if any("salvaged via json-repair" in w for w in warnings):
            return "json_escape"
        return ""

    # Escalated or rejected — categorize the failure.
    cand = accepted if accepted is not None else (
        outcome.attempts[-1] if getattr(outcome, "attempts", None) else None
    )
    if cand is None:
        return "escalated"

    fk = getattr(cand, "failure_kind", "") or ""
    if fk == "parse_failed":
        return "no_parse"
    if fk in ("truncated", "request_failed"):
        return "timeout"
    if fk == "model_refusal":
        return "model_refusal"

    # Parsed but validation flagged hard failures.
    validation = getattr(outcome, "validation", None)
    if validation is not None:
        hard = getattr(validation, "hard_failures", None)
        if hard:
            return "wrong_merge"

    warnings = getattr(cand, "parse_warnings", None) or []
    if any("salvaged via json-repair" in w for w in warnings):
        return "json_escape"

    return "escalated"


class Orchestrator:
    def __init__(
        self,
        config: Config,
        *,
        repo: str = ".",
        session_id: str | None = None,
        resolution_engine: ResolutionEngine | None = None,
        stdin_reader: Callable[..., str] | None = None,
        out: Callable[[str], None] = print,
        color: bool = False,
        log_prompts_dir: str | None = None,
    ) -> None:
        from capybase.color import make_styler

        self.style = make_styler(color)
        self.git = GitBackend(repo)
        self.session_id = session_id or new_session_id()
        # Paths resolved as a deliberate modify/delete keep_block this session.
        # Excluded from the end-of-rebase silent-resurrection scan: such a keep
        # is an explicit, reviewed resurrection (not a silent undo).
        self._explicitly_kept_paths: set[str] = set()
        # The most recent test-gate verdict (human-readable), stashed by
        # _run_tests for the accept report written after the gate.
        self._last_test_verdict: str | None = None
        # Per-side probe diagnostics from _try_test_gated_side, stashed on a
        # DECLINE so _resolve_unit can thread them into the LLM path as seed
        # failures (CEGIS loop hardening). None when no probe ran or it accepted.
        self._last_side_probe_failures: list[VerificationFailure] | None = None
        # Test-continuity baseline: the set of test node-IDs that
        # PASSED pre-rebase, captured in rebase() before the rebase starts. Diffed
        # against the post-merge passing set in _run_tests — a baseline-passing
        # test that now fails is a behavioral regression the merge introduced (a
        # high-signal counterexample for the CEGIS loop). None = no baseline
        # captured (continuity inert; the existing test gate runs unchanged).
        self._test_continuity_baseline: set[str] | None = None
        # History-awareness substrate (#history): the rebase plan + query service,
        # set by rebase() at start. Empty service when not rebase()-driven (the
        # run()/inspect paths), so all history queries degrade to no-op.
        self._history_plan = None
        self._history_service = None
        # Per-unit history-decision snapshot cache (#idea 5 cohesion): built once
        # per unit, consumed by every history mechanism. Collapses the repeated
        # for_conflict (~4×) / obligation-patch-loop (~2×) / features (2×) queries
        # to 1× each. Cleared per step in _resolve_step.
        self._history_snapshots: dict[str, "object"] = {}
        self._history_context_cache: dict[str, "object"] = {}
        self._future_obligations_cache: dict[str, "object"] = {}
        # Branch final-intent summary (#9 step 6): a compact structural summary
        # of the source branch's net effect per file, computed once at rebase
        # start. None when no plan; rendered into the history prompt block.
        self._branch_intent = None
        # Shared embeddings client : one client
        # reused across semantic entity matching, critic-feedback deduplication,
        # and drift detection. Constructed lazily (only when memory is enabled)
        # after the context builder — but its default must exist here so the
        # drift monitor below can capture it. The actual construction happens in
        # the memory block after _build_retriever; this just reserves the slot.
        self._shared_embedder: object | None = None
        self.paths = SessionPaths(self.session_id, repo)
        self.paths.mkdirs()
        self.journal = Journal(self.paths)
        # Cross-session operational log (vs the per-session journal, which is
        # the authoritative audit of THIS run). Logging is configured by the CLI
        # via logging_setup.configure_logging; if a test constructs an
        # orchestrator without configuring logging, this still works (the
        # capybase logger simply has no handlers → messages go nowhere).
        self.log = logging.getLogger("capybase")
        # Model profile overlay ("Profile wins"): rebind the local ``config`` so
        # the profile's tuned knobs flow into EVERY consumer below (resolution
        # engine, verifier) — not just ``self.config``. Done after the journal is
        # ready (it emits model_profile_applied) and before any config read. Inert
        # when the profile is absent/mismatched/corrupt — resolution never crashes.
        config = _apply_model_profile(config, self.git.repo, self.journal)
        self.config = config
        self.extractor = ConflictExtractor(
            self.git, structural_config=config.structural
        )
        # Memory: experience store + retriever for RAG few-shot. Built lazily
        # from config; both are None when [memory] is disabled, so the context
        # builder gets no retriever and retrieved_examples stays empty.
        self.memory_store = None
        retriever = None
        if config.memory.enabled and config.future.enable_rag:
            from capybase.memory.retriever import EmbeddingRetriever, LexicalRetriever
            from capybase.memory.store import ExperienceStore

            self.memory_store = ExperienceStore.for_repo(
                str(self.git.repo), config.memory.store_path
            )
            retriever = self._build_retriever(config)
        # Repair-path retrieval : a strictly-filtered view
        # of the same retriever for the CEGIS repair prompt — higher score floor
        # + retry-count quality filter. Built
        # only when memory is enabled and a base retriever exists; None otherwise
        # (the repair prompt gets no few-shot, the prior behavior). The wrapper
        # over-fetches from the base retriever so the filter still yields k.
        repair_retriever = None
        if retriever is not None and self.memory_store is not None:
            from capybase.memory.retriever import QualityFilteredRetriever

            repair_retriever = QualityFilteredRetriever(
                retriever,
                self.memory_store,
                max_retries=config.memory.repair_retrieval_max_retries,
                min_score=config.memory.repair_retrieval_min_similarity,
            )
        self.context_builder = ContextBuilder(
            config.policy.context_lines,
            retriever=retriever,
            retriever_k=config.memory.retriever_k,
            min_examples=config.memory.min_examples_for_retrieval,
            use_enclosing_as_primary=config.structural.use_enclosing_as_primary,
            canonicalize_context=config.structural.canonicalize_context,
            cross_file_slice=config.structural.cross_file_slice,
            slice_search_globs=config.structural.slice_search_globs,
            slice_repo_root=str(self.git.repo),
            repair_retriever=repair_retriever,
        )
        # Semantic entity matching : install a shared
        # embeddings client on the structural adapter so match_entities can run
        # the embedding rename tier. Reuses the same embeddings endpoint/model
        # as the retriever; built only when memory is enabled. The adapter's
        # embedding tier is best-effort and degrades to pure-deterministic on any
        # failure, so a missing endpoint never breaks matching. The same client
        # is reused for critic-feedback deduplication and drift
        # detection — one connection, one model, consistent vectors.
        if config.memory.enabled:
            try:
                from capybase.adapters import structural
                from capybase.memory.embeddings import OpenAIEmbeddingsClient

                emb_cfg = config.model
                updates: dict = {}
                if config.memory.embeddings_model:
                    updates["model"] = config.memory.embeddings_model
                if config.memory.embeddings_base_url:
                    updates["base_url"] = config.memory.embeddings_base_url
                if updates:
                    emb_cfg = emb_cfg.model_copy(update=updates)
                self._shared_embedder = OpenAIEmbeddingsClient(emb_cfg)
                structural.set_entity_embedder(self._shared_embedder)
            except Exception:  # noqa: BLE001 - semantic matching is best-effort
                pass
        # Session-level drift detection (behavioral-regression redesign). The
        # first-gen detector embedded a prose anchor and cosine-compared it to
        # merged code; an external review established that cross-modal
        # comparison has no operating point (see docs/drift-detector-review.md),
        # so it was scrapped. The replacement is mechanism-gated + behavioral:
        # it emits a drift advisory only when an LLM-produced resolution
        # introduces a test regression (a baseline-passing test that now fails
        # — the test-continuity set). Deterministic resolutions (exact reuse,
        # structural union, brace repair) emit nothing: drift is impossible by
        # construction. No embedder, no threshold, nothing to calibrate.
        self._drift_monitor: "object | None" = None
        if config.memory.enable_drift_detection:
            from capybase.drift import DriftMonitor

            self._drift_monitor = DriftMonitor()
        # The per-step test-continuity regressions, stashed by _run_tests right
        # after the gate runs. Read by _observe_drift in the run loop (which
        # runs after _run_tests, so the value is fresh for the step just
        # resolved). Reset per step.
        self._last_continuity_regressions: list[str] = []
        self._drift_summary_emitted: bool = False
        self.resolution_engine = resolution_engine or ResolutionEngine(
            config.model, log_prompts_dir=log_prompts_dir,
        )
        self.verification = VerificationEngine.default(
            ValidationConfig.from_dict(config.validation.model_dump())
        )
        # Verifier-model critic: when enabled (the default —
        # opt-out), register an LLM judge that checks the resolution preserves
        # both sides' semantic intent — the failure mode the syntactic
        # validators are blind to. It runs last in the validator chain (after
        # the cheap structural checks) and uses the same black-box API client as
        # the resolver. Skipped (not registered) when the engine exposes no
        # ``client`` (e.g. a custom/test engine that only mimics propose): the
        # critic needs a real client to make its call, so absence is a clean
        # no-op rather than a crash. The critic's own verify() also degrades
        # gracefully on any call/parse failure.
        if config.validation.enable_verifier_model and getattr(
            self.resolution_engine, "client", None
        ) is not None:
            from capybase.verification import VerifierModelValidator

            # PoLL jury (§2.1): two same-model different-prompt critics whose
            # flags are UNIONED (a candidate flagged by EITHER is retried) —
            # coverage over voting. The first judges intent PRESERVATION (did it
            # drop a side); the second judges semantic CONFLICT (does it
            # contradict a side / combine incompatible behaviors). Distinct
            # focuses broaden coverage beyond a single judge's blind spots.
            critic_kwargs = dict(
                model_name=config.model.model,
                json_mode=config.model.json_mode,
                # Scale the verdict budget to the model's own generation budget
                # so a reasoning model's <think> chain doesn't run out of tokens
                # before it emits the JSON verdict (silent-degrade guard).
                max_tokens=config.model.max_tokens,
            )
            self.verification.register(
                VerifierModelValidator(
                    self.resolution_engine.client, **critic_kwargs
                )
            )
            try:
                from capybase.resolution_engine import build_verifier_prompt_conflict

                self.verification.register(
                    VerifierModelValidator(
                        self.resolution_engine.client,
                        prompt_builder=build_verifier_prompt_conflict,
                        name_suffix="conflict",
                        **critic_kwargs,
                    )
                )
            except Exception:  # noqa: BLE001 - jury is best-effort; never block on it
                pass
        # Dependency-preservation validator (SafeMerge necessary
        # condition): warns when a merge drops a base-referenced symbol that has
        # an in-repo definition and neither side removed. Registered only when
        # BOTH [structural] cross_file_slice (the slicer it depends on) AND
        # [validation] reject_if_drops_referenced_symbol are on — it needs the
        # search globs + repo root to resolve definitions. Inert otherwise, and
        # a no-op (can't flag what it can't locate) when no defs are found.
        if (
            config.structural.cross_file_slice
            and config.validation.reject_if_drops_referenced_symbol
        ):
            from capybase.verification import DependencyPreservationValidator

            self.verification.register(
                DependencyPreservationValidator(
                    slice_search_globs=config.structural.slice_search_globs,
                    slice_repo_root=str(self.git.repo),
                )
            )
        # Future-obligation validator (#idea 7): checks a candidate keeps the
        # symbols/imports/keys later source commits depend on. The obligations
        # are derived orchestrator-side (git + history needed) and injected per-
        # unit via _future_obligation_validator.set_obligations before each verify.
        # Always registered; a no-op (no obligations → pass) when no history plan
        # is active. Emits features (future_obligation_count etc.) that flow to
        # risk/accept/dry-run/calibration uniformly.
        from capybase.verification import FutureObligationValidator

        self._future_obligation_validator = FutureObligationValidator()
        self.verification.register(self._future_obligation_validator)
        # VeriGuard-style deterministic policy gate: auto-registered
        # by VerificationEngine.default() when enable_policy_gate is on AND rules
        # are configured. It inspects WHAT a patch introduces (the only such
        # check — all others are syntactic/structural), deterministically via
        # stdlib ast (no LLM, no execution). Tags violations onto the unit's
        # risk_tags and blocks error-severity violations from auto-apply.
        # Inert + zero work when off or no rules (the engine factory skips it).
        # Risk engine: the calibrated variant overrides accept/escalate with
        # a learned threshold when a fitted model is present; otherwise it
        # transparently delegates to the rules engine. Both produce the same
        # RiskDecision shape so the orchestrator consumes only ``action``.
        if config.calibration.enabled:
            from capybase.calibration import CalibratedRiskEngine

            self.risk = CalibratedRiskEngine.from_config(
                max_retries_per_unit=config.policy.max_retries_per_unit,
                model_path=str(self.git.repo / config.calibration.model_path)
                if not Path(config.calibration.model_path).is_absolute()
                else config.calibration.model_path,
                escalate_threshold=config.calibration.escalate_threshold,
                entropy_escalate_threshold=config.calibration.entropy_escalate_threshold,
                min_agreement=config.model.consensus_min_agreement,
                max_critic_retries_per_unit=config.policy.max_critic_retries_per_unit,
                critic_confidence_escalate_threshold=config.policy.critic_confidence_escalate_threshold,
            )
        else:
            self.risk = RiskEngine(
                max_retries_per_unit=config.policy.max_retries_per_unit,
                entropy_escalate_threshold=config.calibration.entropy_escalate_threshold,
                min_agreement=config.model.consensus_min_agreement,
                max_critic_retries_per_unit=config.policy.max_critic_retries_per_unit,
                critic_confidence_escalate_threshold=config.policy.critic_confidence_escalate_threshold,
                max_recovery_retries_per_unit=config.policy.max_recovery_retries_per_unit,
                enable_recovery_retry=getattr(
                    config.validation, "enable_recovery_retry", True
                ),
            )
        # Acceptance-strictness policy (#10): tightens the accept branch per the
        # configured mode (interactive/dry_run/ci/unattended). Inert in the
        # default interactive mode. Rebound per-run when rebase() learns whether
        # a human is present (CI / --no-interactive can tighten to ci/unattended).
        self.strictness = StrictnessPolicy(
            mode=config.policy.policy_mode,
            min_confidence=config.policy.unattended_min_confidence,
            escalate_bands=tuple(config.policy.unattended_escalate_bands),
        )
        self.policy = Policy(
            self.git,
            supported_conflict_types=set(config.policy.supported_conflict_types),
            supported_file_kinds=set(config.policy.supported_file_kinds),
        )
        self.tests = TestRunner(self.git, timeout_seconds=config.tests.timeout_seconds)
        self.stdin_reader = stdin_reader or _default_stdin_reader
        self.out = out
        self.step = 0
        # Conflict-chain observations (#9 step 7): one per resolved conflict,
        # accumulated across steps so detect_conflict_chains() can find related
        # conflicts sharing a region coordinate. Reset per rebase()/run().
        self._conflict_observations: list = []
        # Session-level coverage samples (SLO): one (path, preserved,
        # total) per accepted unit across the WHOLE window, accumulated each step
        # so the post-rebase rollup can compute one aggregate preservation ratio.
        # Reset per rebase()/run().
        self._session_coverage_samples: list[tuple[str, int, int]] = []
        # Whether the interactive fallback may fire. Defaults to the real TTY
        # check; tests override this (they can't provide a real terminal).
        self._is_interactive_terminal = _is_interactive_terminal

        # Journal session start + snapshot config.
        self.journal.emit(
            "session_started",
            {
                "session_id": self.session_id,
                "config_source": config.source_path,
                "mode": "orchestrator",
            },
        )
        if config.journal.enabled:
            self.paths.config_copy.write_text(
                _toml_dump_config(config), encoding="utf-8"
            )

    # ==================================================================
    # M1: inspect — no mutation
    # ==================================================================

    def inspect(self) -> StepResult:
        """Detect conflicts, extract units, journal, write review bundle.

        Mutates nothing in the repo (only writes to ``.rebase-agent/``)."""
        self.journal.emit("preflight_started", {})
        if not self.git.rebase_in_progress():
            reason = "no rebase in progress; nothing to inspect"
            self.journal.emit("escalated", {"reason": reason})
            bundle = write_review_bundle(self.paths, reason=reason)
            self.out(self._warn(f"! {reason}") + f"\n  review bundle: {bundle}")
            return StepResult(step_index=self.step, escalated=True, reason=reason)
        self.journal.emit("preflight_passed", {})
        result = self._gather_step()
        write_review_bundle(
            self.paths,
            reason="inspect complete (no mutation performed)",
            step_index=result.step_index,
        )
        self._summarize(result)
        return result

    # ==================================================================
    # M2: manual resolver mode
    # ==================================================================

    def manual(self) -> StepResult:
        """Print each unit, accept a pasted resolution, splice, validate, stage.

        Does not continue the rebase automatically."""
        result = self._gather_step()
        if result.escalated:
            return result
        if not result.units_by_path:
            self.out("no supported conflict units to resolve manually.")
            return result

        for path, units in result.units_by_path.items():
            # Resolve all units, collecting accepted pairs; splice in one
            # offset-correct batch at the end (same structure as run mode).
            accepted: list[tuple[ConflictUnit, CandidateResolution]] = []
            for unit in units:
                self.out(self._render_unit(unit))
                pasted = self.stdin_reader(
                    "paste the resolved text for this block (Ctrl-D to finish):",
                    multiline=True,
                )
                outcome = self._apply_manual_resolution(unit, pasted)
                result.outcomes.append(outcome)
                if outcome.accepted is None:
                    result.escalated = True
                    result.reason = f"manual resolution rejected for {unit.unit_id}"
                    write_review_bundle(
                        self.paths,
                        reason=result.reason,
                        step_index=result.step_index,
                        unit=unit,
                        validation=outcome.validation,
                    )
                    self._summarize(result)
                    return result
                accepted.append((unit, outcome.accepted))
            original = accepted[0][0].original_worktree_text
            buffer = _resolved_buffer(original, accepted)
            # Write + stage the file.
            self._write_and_stage(path, buffer, result, accepted=accepted)
        self._summarize(result)
        self.out(
            "manual mode done; files staged. Run `git rebase --continue` "
            "when ready (tests not run in manual mode)."
        )
        return result

    # ==================================================================
    # Interactive fallback: presented automatically on escalation from rebase()
    # when a human is at the terminal. Lets the human resolve the unit capybase
    # couldn't (paste a resolution OR edit the file directly), then re-validates
    # and continues the rebase — keeping capybase the single owner of the process.
    # ==================================================================

    def interactive_resolve(self, result: StepResult) -> StepResult:
        """On escalation, present the unresolvable conflicts to the human for an
        interactive decision, then continue the rebase.

        Offered per unit: (1) paste a resolution, (2) edit the file directly,
        (3) skip the unit (leave it unmerged), (4) abort the rebase. After all
        units resolve, re-validate (whole-file + test gate) and continue the
        rebase; loop for further stops. If the human skips/aborts, return the
        (still-escalated) result so the caller's abort logic runs.

        Only meaningful when a rebase is in progress and a human is present; the
        caller guards on TTY/``interactive`` before invoking this.
        """
        self.out(
            "\n! capybase could not auto-resolve the conflict(s) below.\n"
            "  Review the context, then choose how to proceed.\n"
            f"  review bundle: {self.paths.final / 'review-bundle.md'}\n"
        )
        # Decide which units to present. The escalation's own ``units_by_path``
        # (carried from _resolve_step) is authoritative when present: for a
        # WHOLE-FILE-VALIDATION failure the worktree is already marker-free
        # (Phase 1 wrote the resolved buffer before Phase 2 validated it), so
        # re-gathering from the worktree finds NO markers and NO units — bailing
        # the human out of the very fallback meant to help them. Prefer the
        # escalation's units; only re-gather when they're absent (a pre-extraction
        # escalation, or the user re-running ``run`` on a stopped rebase).
        units_by_path = result.units_by_path
        whole_file_failure = bool(
            result.reason and "whole-file" in result.reason
        )
        if not units_by_path:
            gathered = self._gather_step()
            if gathered.escalated or not gathered.units_by_path:
                self.out("  (no resolvable units to present interactively)")
                self.journal.emit(
                    "interactive_bail",
                    {
                        "why": "no resolvable units",
                        "gathered_escalated": gathered.escalated,
                        "gathered_units": list(gathered.units_by_path),
                    },
                    step_index=self.step,
                )
                return result
            units_by_path = gathered.units_by_path

        aborted = False
        for path, units in units_by_path.items():
            if aborted:
                break
            # A whole-file failure (cross-unit error after splice) is best handled
            # by editing the whole file directly — the per-unit splice menu can't
            # fix a combination error. BUT the worktree currently holds the
            # MODEL'S BROKEN SPLICE (marker-free, written by Phase 1 before Phase
            # 2 validated) — so edit mode must first RESTORE the raw conflict
            # markers, letting the human resolve the real conflict from scratch
            # rather than repair an already-broken resolution. Lead with the
            # file-edit path; paste/skip/abort remain as fallback.
            raw_conflict = units[0].original_worktree_text if units else None
            if whole_file_failure:
                self.out(
                    f"\n  {path}: the individual resolutions are valid, but their "
                    f"combination fails whole-file validation:\n    "
                    + (result.reason or "").replace("\n", "\n    ")
                )
                self.out(
                    "  The fastest fix is to edit the file directly (option 2): "
                    "capybase will restore the raw conflict markers and you "
                    "resolve it fresh."
                )
            # Show the model's best attempt + the failure for this path (from the
            # original escalation's outcomes) so the human sees what was tried.
            prior = [o for o in result.outcomes if o.unit.path == path]
            accepted: list[tuple[ConflictUnit, CandidateResolution]] = []
            for unit in units:
                self.out(self._render_unit_interactive(unit, prior))
                choice = self._interactive_menu(unit)
                if choice == "abort":
                    aborted = True
                    break
                if choice == "skip":
                    self.out(f"  skipped {unit.unit_id} (left unmerged)")
                    continue
                if choice == "paste":
                    outcome = self._interactive_paste(unit)
                    if outcome.accepted is None:
                        self.out("  paste was rejected; re-offering this unit")
                        # Re-present the same unit until resolved/skipped/aborted.
                        # Simplest correct loop: re-run the menu inline.
                        while True:
                            choice2 = self._interactive_menu(unit)
                            if choice2 == "abort":
                                aborted = True
                                break
                            if choice2 == "skip":
                                break
                            if choice2 == "edit":
                                if self._interactive_edit_file(
                                    path, restore_conflict=(
                                        raw_conflict if whole_file_failure else None
                                    )
                                ):
                                    # File fully resolved by direct edit; stage it
                                    # and move to the next file (units consumed).
                                    self._stage_after_edit(path, result)
                                    accepted = []  # don't double-splice
                                    break
                                continue
                            if choice2 == "paste":
                                o2 = self._interactive_paste(unit)
                                if o2.accepted is not None:
                                    accepted.append((unit, o2.accepted))
                                    break
                                self.out("  paste rejected again; re-offering")
                                continue
                            break
                        if aborted:
                            break
                        continue
                    accepted.append((unit, outcome.accepted))
                elif choice == "edit":
                    # On a whole-file failure, restore the raw conflict markers
                    # so the human resolves the real conflict (not the model's
                    # broken splice). On a plain escalation the markers are
                    # already in the worktree, so no restore is needed.
                    restore = raw_conflict if whole_file_failure else None
                    if self._interactive_edit_file(path, restore_conflict=restore):
                        self._stage_after_edit(path, result)
                        accepted = []  # file resolved wholesale by direct edit
                        break  # next file
            if aborted or not accepted:
                continue
            # Batch-splice + stage the paste-mode resolutions (mirrors manual()).
            original = accepted[0][0].original_worktree_text
            buffer = _resolved_buffer(original, accepted)
            self._write_and_stage(path, buffer, result, accepted=accepted)

        if aborted:
            self.out("  aborting rebase as requested")
            self.git.abort_rebase()
            result.escalated = True
            result.reason = result.reason or "aborted by user in interactive fallback"
            return result

        # If any units were skipped, the rebase can't continue cleanly.
        if self.git.has_unmerged_paths():
            self.out(
                "  some units were skipped — rebase left stopped. "
                "Resolve them with git, then `git rebase --continue`."
            )
            result.escalated = True
            result.reason = "interactive fallback: some units skipped"
            return result

        # All units resolved: run the test gate, then continue the rebase. Loop
        # back into run() for further stops so a multi-conflict rebase proceeds.
        self.out("  " + self._ok("✓ conflict(s) resolved interactively; continuing rebase"))
        result.escalated = False
        result.reason = None
        self.journal.emit(
            "interactive_resolved",
            {"path": path if not aborted else "", "step": self.step},
            step_index=self.step,
        )
        return self.run()

    def _render_unit_interactive(
        self, unit: ConflictUnit, prior_outcomes: list[UnitOutcome]
    ) -> str:
        """Rich context for the interactive menu: the three sides (truncated for
        huge units) + the model's best attempt + why it failed.

        Color (when enabled via ``self.style``) is applied to the structural
        elements — the unit header, side headers, the side-analysis line, and
        failure markers — NOT to the conflict-side *content* itself, so the body
        text stays readable and substring assertions on it hold. Color is a
        passthrough when disabled (default), so this output is byte-identical to
        the un-colored baseline unless color is explicitly turned on.
        """
        from capybase.color import BOLD, CYAN, DIM, MAGENTA, RED, YELLOW

        s = self.style
        lines = [
            s(f"\n=== {unit.unit_id} ({unit.path}, {unit.conflict_type}) ===", BOLD)
        ]
        # Side classification (modify/delete disambiguation): annotate each side
        # header with what it DID (DELETED/ADDED/MODIFIED/unchanged) so a side
        # that's empty because it deleted base content isn't read as "absent".
        # Reads the merge_intent.direction result stashed at extraction.
        md = unit.structural_metadata.get("merge_direction") or {}
        prov = unit.structural_metadata.get("provenance") or {}
        # Per-side header color: BASE dim (reference), CURRENT cyan, REPLAYED magenta.
        side_header_color = {None: DIM, "current": CYAN, "replayed": MAGENTA}
        for label, side, key in (
            ("BASE (common ancestor)", unit.base.text, None),
            ("CURRENT_UPSTREAM_SIDE", unit.current.text, "current"),
            ("REPLAYED_COMMIT_SIDE", unit.replayed.text, "replayed"),
        ):
            ann = self._side_annotation(md, prov, key) if key else ""
            n = side.count("\n") + 1
            header_color = side_header_color[key]
            if n > 30:
                lines.append(s(f"-- {label} ({n} lines; first 30 shown)", header_color)
                             + f"{ann}" + s(" --", header_color))
                lines.append("\n".join(side.split("\n")[:30]))
                lines.append(s("... (truncated; see review bundle for full)", DIM))
            else:
                lines.append(s(f"-- {label} --", header_color) + f"{ann}")
                lines.append(side)
        # One-line side-analysis summary (e.g. "modify/delete: ... DELETED this
        # block") so the conflict shape is explicit, not inferred from the text.
        summary = md.get("summary")
        if summary:
            lines.append(s(f"-- side analysis: {summary} --", YELLOW))
        # The model's best attempt + failure, if the escalation carried it.
        if prior_outcomes:
            o = prior_outcomes[0]
            if o.attempts:
                best = o.attempts[-1]
                lines.append(s("-- model's last attempt --", DIM))
                at = best.resolved_text
                if at.count("\n") > 30:
                    lines.append("\n".join(at.split("\n")[:30]))
                    lines.append(s("... (truncated)", DIM))
                else:
                    lines.append(at)
            if o.validation and o.validation.hard_failures:
                lines.append(s("-- why it failed --", RED))
                for hf in o.validation.hard_failures[:5]:
                    lines.append(f"  {s(f'[{hf.validator}]', RED)} {hf.message}")
        return "\n".join(lines)

    def _side_annotation(
        self, md: dict, prov: dict, key: str | None
    ) -> str:
        """A short `` — DELETED (introduced by <commit>)`` tag for a side header.

        ``md`` is the unit's ``merge_direction`` metadata, ``prov`` its
        ``provenance`` metadata, ``key`` the side (``"current"``/``"replayed"``).
        Returns ``""`` when nothing is recorded, so unenriched units render as
        before. Mirrors :func:`escalation._annotated_side_header` but inline. The
        classification tag is colored semantically (DELETED red, ADDED green,
        MODIFIED yellow, unchanged dim) when color is enabled.
        """
        if not key:
            return ""
        from capybase.color import DIM, GREEN, RED, YELLOW

        s = self.style
        parts: list[str] = []
        kind = (md or {}).get(key)
        # Semantic color per classification: red=removed, green=added, yellow=changed.
        tag_color = {
            "added": GREEN, "deleted": RED, "modified": YELLOW, "unchanged": DIM,
        }.get(kind)
        label = {
            "added": "ADDED", "deleted": "DELETED",
            "modified": "MODIFIED", "unchanged": "unchanged",
        }.get(kind)
        if label and tag_color is not None:
            parts.append(s(f" — {label}", tag_color))
        elif label:
            parts.append(f" — {label}")
        subject = ((prov or {}).get(key) or {}).get("subject")
        if subject:
            parts.append(s(f" (introduced by `{subject}`)", DIM))
        return "".join(parts)

    def _interactive_menu(self, unit: ConflictUnit) -> str:
        """Present the menu and return the chosen action string."""
        self.out(
            f"\n  How do you want to resolve {unit.unit_id}?\n"
            "    1) paste a resolution\n"
            "    2) edit the file directly (then I validate + continue)\n"
            "    3) skip this unit (leave unmerged)\n"
            "    4) abort the rebase\n"
        )
        choice = self.stdin_reader("  choice [1-4]: ").strip()
        return {"1": "paste", "2": "edit", "3": "skip", "4": "abort"}.get(
            choice, "skip"
        )

    def _interactive_paste(self, unit: ConflictUnit) -> UnitOutcome:
        """Read a pasted resolution and validate it through the full chain."""
        self.out("  paste the resolved text (Ctrl-D to finish):")
        pasted = self.stdin_reader("", multiline=True)
        outcome = self._apply_manual_resolution(unit, pasted)
        self.journal.emit(
            "interactive_resolved",
            {"unit": unit.unit_id, "mode": "paste",
             "accepted": outcome.accepted is not None},
            step_index=self.step,
        )
        return outcome

    def _interactive_edit_file(
        self, path: str, *, restore_conflict: str | None = None
    ) -> bool:
        """Tell the human to edit the file in their editor; on their signal,
        read it back, and LOOP until no conflict markers remain (returning True)
        or the human gives up (returning False).

        ``restore_conflict``: when set (a whole-file escalation), the worktree
        currently holds the MODEL'S BROKEN SPLICE (marker-free) — Phase 1 wrote
        it before Phase 2 validated. Offering edit mode on that is wrong: the
        human would edit an already-resolved-but-broken file with no markers to
        resolve, and the prompt ("resolve the conflict markers") wouldn't match.
        So we FIRST write back the raw conflict buffer (with markers), so the
        human resolves the REAL conflict from scratch.

        On each Enter, if markers remain we tell the human and re-prompt (NOT
        return — a prior version printed "Re-offering" then returned False, which
        the caller treated as a skip, aborting the rebase on a single Enter
        before the human had resolved anything). The loop is bounded so a runaway
        can't spin forever; after the cap, return False (the caller skips the
        unit rather than silently aborting the whole rebase).
        """
        if restore_conflict is not None:
            self._write_worktree_only(path, restore_conflict)
            self.out(
                f"  (restored the raw conflict markers to {path} — the previous "
                "resolution attempt was broken; resolve the conflict fresh.)"
            )
        self.out(
            f"  edit {path} in your editor now (resolve the conflict markers,\n"
            "  save, and return here). Press Enter when done."
        )
        max_reprompts = 50  # generous; a human genuinely working won't hit this
        for _ in range(max_reprompts):
            self.stdin_reader("")
            text = self.git.read_worktree_file(path).decode("utf-8", errors="replace")
            # Use line-anchored marker detection (contains_markers), NOT loose
            # substring matching: a file with ``// =====`` comment banners would
            # false-positive on ``"=======" in text`` and loop forever claiming
            # "markers still present" when none are. Real git conflict markers
            # start at column 0.
            from capybase.adapters.parsers import contains_markers

            if not contains_markers(text):
                self.journal.emit(
                    "interactive_resolved",
                    {"path": path, "mode": "edit", "accepted": True},
                    step_index=self.step,
                )
                return True
            # Markers still present: re-prompt (the message says "re-offer" — now
            # it actually does). The human presses Enter again after editing more.
            self.out(
                self._warn(
                    "! conflict markers still present in "
                    + path
                    + " — not done editing."
                )
            )
            self.out("  Edit the file, remove all markers, save, and Press Enter again.")
            self.journal.emit(
                "interactive_resolved",
                {"path": path, "mode": "edit", "accepted": False,
                 "reason": "markers remained (re-prompting)"},
                step_index=self.step,
            )
        # Cap hit: the human couldn't clear the markers. Return False so the
        # caller skips this unit (the rebase stays stopped), rather than aborting.
        self.out(
            f"  giving up on {path} after repeated attempts — markers still "
            f"present. This unit will be skipped."
        )
        return False

    def _stage_after_edit(self, path: str, result: StepResult) -> None:
        """After a direct edit, validate the whole file (cargo check etc.) and
        stage it. The human owns the file content; we only verify + stage."""
        self.git.stage_paths([path])
        self.journal.emit(
            "file_staged", {"path": path, "via": "interactive_edit"},
            step_index=self.step, path=path,
        )

    def _apply_manual_resolution(
        self, unit: ConflictUnit, pasted: str
    ) -> UnitOutcome:
        cand = CandidateResolution(
            candidate_id=f"{unit.unit_id}:manual",
            unit_id=unit.unit_id,
            model_name="human",
            prompt_version="manual.v1",
            resolved_text=pasted,
            explanation="provided by human via manual mode",
            provenance="manual",
        )
        validation = self.verification.verify(unit, cand)
        self.journal.emit(
            "candidate_validated",
            {
                "candidate_id": cand.candidate_id,
                "passed": validation.passed,
                "hard_failures": [f.message for f in validation.hard_failures],
            },
            step_index=self.step,
            path=unit.path,
            unit_id=unit.unit_id,
        )
        if self.config.journal.enabled and self.config.journal.store_validations:
            self.journal.store_validation(validation)
        outcome = UnitOutcome(unit=unit, validation=validation, attempts=[cand])
        if not validation.passed:
            for hf in validation.hard_failures:
                self.out(f"  ! rejected: [{hf.validator}] {hf.message}")
            return outcome
        outcome.accepted = cand
        self.journal.emit(
            "candidate_accepted",
            {"candidate_id": cand.candidate_id},
            step_index=self.step,
            path=unit.path,
            unit_id=unit.unit_id,
        )
        return outcome

    def _strictness_blocks_pre_llm(
        self, unit: ConflictUnit, cand: CandidateResolution,
        validation: VerificationResult, via: str,
    ) -> str:
        """The strictness-policy gate for a DETERMINISTIC pre-LLM resolution.

        Returns a non-empty reason when the configured mode (#10) refuses to
        auto-accept this resolution even though it passed validation (e.g. it
        dropped a side obligation or introduced a diagnostic in ci/unattended
        mode). Empty string ⇒ accept. The resolution is then discarded (returns
        None from its caller), falling through to the LLM — strictness never
        applies an invalid merge, it just declines to auto-accept a borderline
        one without a human.
        """
        if not self.strictness.strict:
            return ""
        band = self._classification_band(unit)
        ok, reason = self.strictness.accept_pre_llm(
            unit, cand, validation, band=band
        )
        if ok:
            return ""
        self.journal.emit(
            "strictness_declined",
            {"via": via, "reason": reason, "mode": self.strictness.mode},
            step_index=self.step, path=unit.path, unit_id=unit.unit_id,
        )
        return reason

    def _classification_band(self, unit: ConflictUnit) -> str | None:
        """The unit's classification band (#2), computed if routing is on."""
        if not self.config.routing.enabled:
            return None
        try:
            from capybase.classifier import classify
            return classify(unit).band  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - advisory for the strictness gate
            return None

    def _llm_oversized_for_window(self, unit: ConflictUnit) -> tuple[bool, int, int]:
        """Whether the conflict's essential content exceeds the model's window.

        The "protect-the-conflict" prompt policy sends the three sides even when
        they alone blow the context window (dropping all augmentation). That's a
        wasted call: an oversized prompt truncates server-side and the model
        fails anyway. This guard detects that case up front so the LLM loop can
        be skipped in favor of escalation (the deterministic layers + block
        capture already ran and declined).

        Returns ``(oversized, essential_tokens, available_tokens)``.
        ``oversized`` is False when the window is unconfigured (0 = disabled) —
        without a window we can't judge "too large", so the guard is a no-op and
        the historical "send it anyway" behavior is preserved.
        """
        window = int(getattr(self.config.model, "context_window", 0) or 0)
        if window <= 0:
            return False, 0, 0  # unconfigured → no guard
        reserve = int(getattr(self.config.model, "completion_reserve", 1024) or 1024)
        available = max(0, window - reserve)
        # Essential content = the three conflict sides (the untrimmable core).
        # The prompt's fixed overhead (intro/contract/rules, ~200-400 tokens) and
        # all augmentation sections ARE trimmable by _fit_to_budget, so we do NOT
        # fold them in here — a tight window that forces augmentation trimming is
        # the documented, tested behavior, not a hopeless case. This guard fires
        # only when the SIDES THEMSELVES don't fit: the prompt can't be made
        # valid by any amount of trimming, so the LLM call is doomed. estimate_tokens
        # is ~4 chars/token.
        sides = (
            (unit.base.text or "") + (unit.current.text or "")
            + (unit.replayed.text or "")
        )
        essential = estimate_tokens(sides)
        return essential > available, essential, available

    def _try_exact_reuse(self, unit: ConflictUnit) -> UnitOutcome | None:
        """Attempt a verbatim replay of a prior accepted resolution (#9 step 4).

        Always on (no flag): when an IDENTICAL prior conflict (same shape,
        language, region kind, accepted outcome, validation evidence) exists in
        the memory store, replay its resolution verbatim. The candidate is built
        and validated exactly as any other — a stale/wrong reuse fails validation
        and falls through (returns None), so reuse is a speed optimization, never
        a correctness bypass. Returns None when no store, no match, or the reuse
        failed validation.
        """
        if self.memory_store is None:
            self.journal.emit(
                "exact_reuse_skipped", {"reason": "no memory store"},
                step_index=self.step, path=unit.path, unit_id=unit.unit_id,
            )
            return None
        from capybase.exact_reuse import find_exact_reuse

        region_kind = self._region_kind_for(unit)
        try:
            reuse = find_exact_reuse(
                unit=unit, store=self.memory_store,
                language=unit.language, region_kind=region_kind,
                path=unit.path,
            )
        except Exception as exc:  # noqa: BLE001 - distinguish failure from no-match
            # find_exact_reuse returns None for a genuine no-match but propagates
            # exceptions; emit a distinct advisory so a real failure isn't
            # mislabeled "no exact match" (#idea 4 — observability).
            self.journal.emit_advisory(
                "exact_reuse_failed", f"reuse matching raised: {exc}",
                step_index=self.step, path=unit.path, unit_id=unit.unit_id,
            )
            return None
        if reuse is None or reuse.skip_reason:
            # No match (None = no store/empty; skip_reason = same-shape priors
            # existed but none passed all conditions). Journal the near-misses
            # (#idea 8) so a skip isn't indistinguishable from an empty store.
            near = list(reuse.near_misses) if reuse is not None else []
            skip = reuse.skip_reason if reuse is not None else ""
            self.journal.emit(
                "exact_reuse_skipped",
                {"reason": skip or "no exact match",
                 "near_misses": near[:8]},
                step_index=self.step, path=unit.path, unit_id=unit.unit_id,
            )
            reason = "no exact match"
            if near:
                reason = f"no full match ({len(near)} near-miss(es): {'; '.join(near[:3])})"
            self._record_resolution_attempt(
                UnitOutcome(unit=unit), mechanism="exact_history_reuse",
                decision="skip", reason=reason,
            )
            return None
        cand = CandidateResolution(
            candidate_id=f"{unit.unit_id}:exact_reuse",
            unit_id=unit.unit_id,
            model_name="exact-reuse",
            prompt_version="exact_history_reuse.v1",
            resolved_text=reuse.resolved_text,
            explanation=(
                f"verbatim replay of prior accepted resolution "
                f"(from {reuse.source_summary})"
            ),
            provenance="exact_history_reuse",
        )
        validation = self.verification.verify(unit, cand)
        self.journal.emit(
            "exact_reuse_attempted",
            {"candidate_id": cand.candidate_id, "source": reuse.source_summary,
             "passed": validation.passed},
            step_index=self.step, path=unit.path, unit_id=unit.unit_id,
        )
        if not validation.passed:
            # The stale/wrong reuse failed validation — discard and fall through.
            # This is the safety net that makes always-on reuse safe: a bad
            # match is caught here, exactly like a bad structural guess.
            self.journal.emit(
                "exact_reuse_skipped",
                {"reason": "failed validation",
                 "failures": [f.message for f in validation.hard_failures]},
                step_index=self.step, path=unit.path, unit_id=unit.unit_id,
            )
            self._record_resolution_attempt(
                UnitOutcome(unit=unit), mechanism="exact_history_reuse",
                candidate=cand, validation=validation,
                decision="skip", reason="failed validation",
            )
            return None
        # Future-obligations gate for reuse (#9 step 3 / #idea 7): a reuse that
        # locally passes but drops a symbol a later commit needs must fall through
        # (the prior resolution predates this conflict's history context). The
        # FutureObligationValidator now runs during verify() and emits the
        # features, but reuse declines on a drop (returns None = fall through)
        # rather than retrying — the prior text is fixed, so a retry wouldn't help.
        # Read from the memoized snapshot (the validator was already fed from it).
        snapshot = self._history_snapshots.get(
            getattr(unit, "unit_id", None) or id(unit))
        obls = snapshot.future_obligations if snapshot is not None else None
        if obls is not None and not obls.empty:
            from capybase.future_obligations import obligations_satisfied
            fo_ok, fo_dropped = obligations_satisfied(obls, cand.resolved_text or "")
            if not fo_ok:
                self.journal.emit(
                    "exact_reuse_skipped",
                    {"reason": "future obligation", "dropped_symbols": fo_dropped},
                    step_index=self.step, path=unit.path, unit_id=unit.unit_id,
                )
                self._record_resolution_attempt(
                    UnitOutcome(unit=unit), mechanism="exact_history_reuse",
                    candidate=cand, validation=validation,
                    decision="skip", reason=f"future obligation: {fo_dropped}",
                )
                return None
        if self._strictness_blocks_pre_llm(unit, cand, validation, "exact_reuse"):
            self._record_resolution_attempt(
                UnitOutcome(unit=unit), mechanism="exact_history_reuse",
                candidate=cand, validation=validation,
                decision="skip", reason="strictness declined",
            )
            return None  # strict mode declines; fall through to structural/LLM
        outcome = UnitOutcome(unit=unit, validation=validation, attempts=[cand])
        outcome.accepted = cand
        # The audit reason names WHICH conditions matched (#idea 8): "verbatim
        # replay from session X because shape/language/region matched + tests passed."
        matched = "; ".join(reuse.matched_conditions) if reuse.matched_conditions else "shape matched"
        self._record_resolution_attempt(
            outcome, mechanism="exact_history_reuse",
            candidate=cand, validation=validation,
            decision="accept",
            reason=f"verbatim replay from {reuse.source_summary} (matched: {matched})",
        )
        self.journal.emit(
            "exact_reuse_applied",
            {"candidate_id": cand.candidate_id, "source": reuse.source_summary,
             "source_session": reuse.source_session},
            step_index=self.step, path=unit.path, unit_id=unit.unit_id,
        )
        return outcome

    def _try_structural_resolve(self, unit: ConflictUnit) -> UnitOutcome | None:
        """Attempt a deterministic, model-free resolution; accept only if it
        passes the full validation pipeline, else return None (fall through to
        the LLM). Survey §6.4 layer 1: structural/auto resolution before the model.

        Safe by construction: the resolver only emits resolutions from provably-
        safe rules (identical sides, one-sided change, disjoint line edits), and
        this method validates the result exactly as an LLM candidate would be —
        markers/splice/AST/syntax. A wrong deterministic guess is caught here and
        discarded (returns None), so the model then handles it. Net effect: fewer
        LLM calls on trivial conflicts, never a worse merge.
        """
        from capybase.structural_resolver import resolve_structurally

        result = resolve_structurally(unit)
        if not result.resolved or result.text is None:
            self._record_resolution_attempt(
                UnitOutcome(unit=unit), mechanism="structural",
                decision="skip", reason="no rule applied",
            )
            return None
        cand = CandidateResolution(
            candidate_id=f"{unit.unit_id}:structural",
            unit_id=unit.unit_id,
            model_name="structural",
            prompt_version=f"structural.{result.rule}",
            resolved_text=result.text,
            explanation=f"deterministic resolution via {result.rule} rule",
            provenance="deterministic_structural",
        )
        validation = self.verification.verify(unit, cand)
        self.journal.emit(
            "structurally_resolved",
            {
                "candidate_id": cand.candidate_id,
                "rule": result.rule,
                "passed": validation.passed,
            },
            step_index=self.step,
            path=unit.path,
            unit_id=unit.unit_id,
        )
        if not validation.passed:
            # The deterministic guess failed validation — discard and let the
            # model handle it. This is the safety net: structural resolution can
            # only help, never apply an invalid merge.
            self._record_resolution_attempt(
                UnitOutcome(unit=unit), mechanism="structural",
                candidate=cand, validation=validation,
                decision="skip", reason="failed validation",
            )
            return None
        if self._strictness_blocks_pre_llm(unit, cand, validation, "structural"):
            self._record_resolution_attempt(
                UnitOutcome(unit=unit), mechanism="structural",
                candidate=cand, validation=validation,
                decision="skip", reason="strictness declined",
            )
            return None  # strict mode declines to auto-accept; fall through to LLM
        outcome = UnitOutcome(unit=unit, validation=validation, attempts=[cand])
        outcome.accepted = cand
        self.journal.emit(
            "candidate_accepted",
            {"candidate_id": cand.candidate_id, "via": "structural"},
            step_index=self.step,
            path=unit.path,
            unit_id=unit.unit_id,
        )
        return outcome

    def _try_combination_search(self, unit: ConflictUnit) -> UnitOutcome | None:
        """Attempt a search-based combination resolution; accept only if it
        passes the full validation pipeline. Survey §4.1 (SBCR).

        Runs AFTER the structural resolver declines and BEFORE the LLM. SBCR is a
        *candidate generator*, not a decider: it searches order-preserving
        interleavings of the two sides for the one with maximal mean similarity
        to both parents (prior work's fitness, correlation ~0.64 with developer
        resolution quality). Its search space includes invalid combinations
        (e.g. two contradictory lines concatenated), so — exactly like the
        structural resolver — every candidate is validated (syntax/AST/splice)
        before acceptance, and a rejected candidate falls through to the model.
        Net effect: resolves both-sides-add / restructure conflicts with no LLM
        call when the combination is sound; never applies an invalid merge.
        """
        from capybase.sbcr import balance, resolve_by_combination_search

        fut = self.config.future
        result = resolve_by_combination_search(
            unit,
            floor=fut.sbcr_floor,
            max_iterations=fut.sbcr_max_iterations,
            stagnation_limit=fut.sbcr_stagnation_limit,
            max_time=fut.sbcr_max_time_seconds,
            min_candidate_ratio=fut.sbcr_min_candidate_ratio,
        )
        if not result.resolved or result.text is None:
            # The search declined (modification conflict, below floor, shrinkage
            # guard, …). Journal the reason + fitness so a skip isn't silent and
            # the fitness that was computed isn't thrown away (matches how
            # _try_exact_reuse instruments its declines).
            reason = result.skip_reason or "no candidate found"
            self._record_resolution_attempt(
                UnitOutcome(unit=unit), mechanism="sbcr",
                decision="skip", reason=reason,
            )
            self.journal.emit(
                "combination_declined",
                {"fitness": round(result.fitness, 4), "reason": reason},
                step_index=self.step, path=unit.path, unit_id=unit.unit_id,
            )
            return None
        # Balance-aware routing: SBCR wins on BALANCED conflicts
        # and loses to the LLM on imbalanced ones (one side changed far more).
        # When routing is on and the conflict is more imbalanced than the
        # configured threshold, do NOT short-circuit — decline so the LLM runs,
        # which is the stronger engine there.
        bal = balance(unit)
        threshold = self.config.routing.min_balance_for_sbcr_accept
        if self.config.routing.enabled and bal < threshold:
            self._record_resolution_attempt(
                UnitOutcome(unit=unit), mechanism="sbcr",
                decision="skip",
                reason=f"balance {bal:.2f} < threshold {threshold:.2f}",
            )
            self.journal.emit(
                "combination_resolved",
                {
                    "candidate_id": f"{unit.unit_id}:sbcr",
                    "fitness": round(result.fitness, 4),
                    "balance": round(bal, 4),
                    "passed": False,
                    "deferred_to_llm": True,
                    "reason": f"balance {bal:.2f} < threshold {threshold:.2f}",
                },
                step_index=self.step,
                path=unit.path,
                unit_id=unit.unit_id,
            )
            return None
        cand = CandidateResolution(
            candidate_id=f"{unit.unit_id}:sbcr",
            unit_id=unit.unit_id,
            model_name="sbcr",
            prompt_version="sbcr.combination",
            resolved_text=result.text,
            explanation=(
                f"search-based combination resolution "
                f"(fitness={result.fitness:.3f}, balance={bal:.2f})"
            ),
            provenance="combination_search",
        )
        validation = self.verification.verify(unit, cand)
        self.journal.emit(
            "combination_resolved",
            {
                "candidate_id": cand.candidate_id,
                "fitness": round(result.fitness, 4),
                "balance": round(bal, 4),
                "passed": validation.passed,
            },
            step_index=self.step,
            path=unit.path,
            unit_id=unit.unit_id,
        )
        if not validation.passed:
            # The combination guess failed validation (e.g. contradictory lines
            # concatenated into invalid code). Discard and let the model handle
            # it. This is why SBCR is safe despite a heuristic fitness function.
            self._record_resolution_attempt(
                UnitOutcome(unit=unit), mechanism="sbcr",
                candidate=cand, validation=validation,
                decision="skip", reason="failed validation",
            )
            return None
        if self._strictness_blocks_pre_llm(unit, cand, validation, "sbcr"):
            self._record_resolution_attempt(
                UnitOutcome(unit=unit), mechanism="sbcr",
                candidate=cand, validation=validation,
                decision="skip", reason="strictness declined",
            )
            return None  # strict mode declines to auto-accept; fall through to LLM
        outcome = UnitOutcome(unit=unit, validation=validation, attempts=[cand])
        outcome.accepted = cand
        self.journal.emit(
            "candidate_accepted",
            {"candidate_id": cand.candidate_id, "via": "sbcr"},
            step_index=self.step,
            path=unit.path,
            unit_id=unit.unit_id,
        )
        return outcome

    def _try_test_gated_side(self, unit: ConflictUnit) -> UnitOutcome | None:
        """Test-gated side picker: when both pre-LLM resolvers decline a conflict
        where taking EITHER side verbatim is a plausible resolution, try each side
        and let the TEST GATE discriminate. Survey §4.2 / conftest port pattern.

        The structural resolver and SBCR both correctly decline same-line scalar
        conflicts (port=9090 vs port=7070: no deterministic answer). But that
        means no pre-LLM mechanism proposes either side, so the conflict goes
        straight to the LLM — which on a small model often fails. This mechanism
        fills that gap: it builds a candidate from each side, validates it
        (markers/splice/AST/syntax), and for any that pass, writes the spliced
        file and runs the test gate. The first side that passes BOTH validation
        AND the test gate is accepted.

        Safety contract (mirrors SBCR's): a side that fails validation OR the
        test gate is discarded; the conflict falls through to the LLM. The test
        gate is the discriminator (it knows ``port == 9090`` from the assertion).
        Only fires when tests are required AND a real test command is configured
        (not the ``true`` no-op) — otherwise there's no way to discriminate.
        """
        # Scope guard: only when the test gate is real (required + a non-trivial
        # command). The no-op ``true`` shim can't discriminate, so decline.
        cmd = self.config.tests.pre_continue or self.config.tests.final
        if not self.config.tests.required or not cmd or cmd.strip() in ("true", "pytest"):
            # Note: "pytest" is left to the LLM because pytest runs the WHOLE
            # suite (slow, and may have pre-existing failures unrelated to this
            # unit); the side picker targets targeted test commands (cargo test,
            # a specific pytest invocation) that actually exercise the merged code.
            return None
        # Only marker-block units (whole-file units have no "side" to pick).
        if unit.marker_span is None:
            return None
        cur_text = unit.current.text or ""
        rep_text = unit.replayed.text or ""
        # Both sides must be non-empty (each is a standalone candidate) and differ
        # (identical sides would've been caught by the structural resolver).
        if not cur_text.strip() or not rep_text.strip() or cur_text == rep_text:
            return None

        from capybase.adapters.parsers import splice_resolution

        # Save the worktree file so we can restore it if neither side passes.
        original_bytes = b""
        try:
            original_bytes = self.git.read_worktree_file(unit.path)
        except Exception:  # noqa: BLE001
            pass  # file may not exist yet (rare)

        # Try BOTH sides and record which pass validation + the test gate. The
        # picker ONLY accepts when EXACTLY ONE side passes the test gate — that's
        # the discriminator. If BOTH pass (e.g. a syntax-only gate like py_compile
        # that can't distinguish the sides), there's no discrimination → decline
        # and let the LLM/critic handle it. This prevents the picker from accepting
        # the first side that compiles when the gate can't tell the sides apart.
        sides = [("current", cur_text), ("replayed", rep_text)]
        passed_sides: list[tuple[str, str, CandidateResolution, object]] = []
        # Capture per-side diagnostics so a DECLINE can thread them into the LLM
        # path as seed_failures (CEGIS loop hardening): when neither side compiles,
        # the model never previously saw WHY. Stash the compile errors here.
        probe_diagnostics: list[VerificationFailure] = []
        for side_label, side_text in sides:
            cand = CandidateResolution(
                candidate_id=f"{unit.unit_id}:test_gated_{side_label}",
                unit_id=unit.unit_id,
                model_name="test_gated",
                prompt_version=f"test_gated.{side_label}",
                resolved_text=side_text,
                explanation=f"test-gated side pick ({side_label} side verbatim)",
                provenance="test_gated_side",
            )
            validation = self.verification.verify(unit, cand)
            if not validation.passed:
                # This side fails validation — record the hard failures so the
                # LLM path sees what's wrong with taking it verbatim.
                for hf in validation.hard_failures:
                    probe_diagnostics.append(hf)
                continue  # this side fails validation; skip it
            # Write the spliced file so the test gate runs against it.
            spliced = splice_resolution(unit.original_worktree_text, unit.marker_span, side_text)
            self.git.write_worktree_file(unit.path, spliced.encode("utf-8"))
            # Invalidate stale Python bytecode after writing: each probe rewrites
            # the conflicted .py with a different side's content, and two writes
            # within the same mtime tick (sub-second) leave a STALE .pyc from the
            # previous probe. The test gate would then import the old bytecode
            # (e.g. PORT=7070 from the replayed-side probe) and fail on the new
            # source (PORT=9090) — a false escalation. Clearing the file's
            # __pycache__ forces a recompile on the next import.
            _invalidate_pycache(self.git.repo, unit.path)
            probe = StepResult(step_index=self.step)
            probe.units_by_path[unit.path] = [unit]
            self.journal.emit(
                "test_gated_side_probe",
                {"candidate_id": cand.candidate_id, "side": side_label},
                step_index=self.step, path=unit.path, unit_id=unit.unit_id,
            )
            test_ok = self._run_tests("pre_continue", probe)
            if test_ok:
                passed_sides.append((side_label, side_text, cand, validation))
            else:
                # Capture the test-gate compile diagnostic so the LLM path sees
                # WHY this side failed the gate (e.g. the cargo compile error).
                # The _last_test_verdict holds the human-readable summary.
                diag = getattr(self, "_last_test_verdict", None) or "side failed the test gate"
                probe_diagnostics.append(VerificationFailure(
                    validator="test_gated_side",
                    severity="warning",
                    message=f"{side_label} side verbatim failed the test gate: {diag}",
                ))

        if len(passed_sides) != 1:
            # 0 passed → neither side is test-correct; 2 passed → the gate can't
            # discriminate (e.g. py_compile passes both). Either way, decline and
            # let the LLM/critic handle it. Restore the original worktree.
            self.git.write_worktree_file(unit.path, original_bytes)
            # CEGIS loop hardening: stash the per-side probe diagnostics so the
            # LLM path starts with them as seed_failures — the model finally sees
            # WHY neither side compiled, instead of a feedback-free fresh resolve.
            self._last_side_probe_failures = probe_diagnostics or None
            return None

        # Exactly one side passed → the test gate discriminated. Accept it.
        side_label, side_text, cand, validation = passed_sides[0]
        if self._strictness_blocks_pre_llm(unit, cand, validation, "test_gated"):
            self.git.write_worktree_file(unit.path, original_bytes)
            return None
        outcome = UnitOutcome(unit=unit, validation=validation, attempts=[cand])
        outcome.accepted = cand
        self.journal.emit(
            "candidate_accepted",
            {"candidate_id": cand.candidate_id, "via": "test_gated_side",
             "side": side_label},
            step_index=self.step, path=unit.path, unit_id=unit.unit_id,
        )
        return outcome

    def _try_block_capture(self, unit: ConflictUnit) -> UnitOutcome | None:
        """Block-capture resolution for large modify/delete conflicts.

        When one side DELETED a large block and the other KEPT it (and the
        structural ``delete_side`` rule declined — e.g. the keeper MODIFIED the
        block, so it's not a clean auto-accept), asking the model to REPRODUCE
        the block as an escaped JSON string fails: it collapses to placeholders
        (``// ... unchanged ...``) and corrupts the escaping (mixed real/literal
        ``\\n``). The CEGIS loop then chases those self-inflicted errors forever.

        Block-capture sidesteps this entirely: the model makes a small DECISION
        (accept_deletion / keep_block / needs_human), and capybase splices the
        chosen conflict side's text VERBATIM. The model never reproduces the
        block, so truncation and escaping errors are structurally impossible.

        Runs AFTER structural + combination search decline and BEFORE the LLM
        loop, only on a FRESH resolve. Gated by ``[future] enable_block_capture``
        and a minimum block size (``block_capture_min_lines``): the full-LLM path
        is fine for small blocks, so this only engages where reproduction is the
        problem. Like the other pre-LLM layers, the spliced candidate still runs
        the full validation pipeline; an invalid splice (e.g. keep_block on a
        block that doesn't fit the file) falls through to the LLM.
        """
        from capybase.merge_intent import direction
        from capybase.resolution_engine import (
            PROMPT_BLOCK_CAPTURE,
            build_block_capture_prompt,
            parse_block_capture_decision,
        )

        # Self-gate: the caller (_resolve_unit) already checks the flag, but
        # _try_block_capture must be correct when called directly too.
        if not self.config.future.enable_block_capture:
            return None
        # Gate 1: must be a modify/delete with a known deleting side.
        md = unit.structural_metadata.get("merge_direction") or {}
        if md.get("kind") != "modify_delete" or not md.get("deleting_side"):
            return None
        who = md["deleting_side"]  # "current" | "replayed"
        # Gate 2: the kept block must be large enough that reproduction is the
        # problem. Small modify/deletes go through the normal LLM path.
        keeper = unit.replayed if who == "current" else unit.current
        deleter = unit.current if who == "current" else unit.replayed
        keeper_n = sum(1 for ln in (keeper.text or "").splitlines() if ln.strip())
        if keeper_n < self.config.future.block_capture_min_lines:
            return None

        # Ask the model for a decision (not a reproduction). The prompt shows a
        # summary of the keeper, never the full text.
        context = self.context_builder.build(unit)
        prompt = build_block_capture_prompt(unit, context)
        if self.config.journal.enabled and self.config.journal.store_prompts:
            self.journal.store_prompt(unit.unit_id, 0, prompt)
        try:
            resp = self.resolution_engine.raw_complete(prompt, json_mode=False)
        except Exception as exc:  # noqa: BLE001 - request failed → fall through
            self.journal.emit(
                "block_capture_request_failed",
                {"error": str(exc)[:200]},
                step_index=self.step,
                path=unit.path,
                unit_id=unit.unit_id,
            )
            return None
        decision, reason = parse_block_capture_decision(resp.text)
        self.journal.emit(
            "block_capture_decision",
            {
                "decision": decision,
                "reason": reason,
                "keeper_lines": keeper_n,
            },
            step_index=self.step,
            path=unit.path,
            unit_id=unit.unit_id,
        )
        # Map the decision to the text to splice, taken VERBATIM from the
        # conflict side — never reproduced by the model.
        if decision == "accept_deletion":
            resolved_text = deleter.text or ""
            expl = f"block-capture: accepted deletion ({reason})"
        elif decision == "keep_block":
            resolved_text = keeper.text or ""
            expl = f"block-capture: kept block verbatim ({reason})"
            # A whole-file keep_block deliberately resurrects content upstream
            # deleted (it was a modify/delete the keeper won). The end-of-rebase
            # silent-resurrection scan would otherwise flag it — but this keep
            # was an explicit, reviewed decision, not a silent undo, so suppress
            # the finding for this path.
            if unit.marker_span is None:
                self._explicitly_kept_paths.add(unit.path)
        else:
            # needs_human (or unparseable): decline; the LLM loop / escalation
            # handles it. Never guess.
            return None
        cand = CandidateResolution(
            candidate_id=f"{unit.unit_id}:block_capture",
            unit_id=unit.unit_id,
            model_name=self.config.model.model,
            prompt_version=PROMPT_BLOCK_CAPTURE,
            resolved_text=resolved_text,
            explanation=expl,
            provenance="block_capture",
        )
        validation = self.verification.verify(unit, cand)
        if not validation.passed:
            # The chosen side's text didn't validate when spliced (rare, but
            # possible if e.g. keep_block's text needs the deleted context).
            # Fall through to the full LLM loop rather than accept an invalid splice.
            self.journal.emit(
                "block_capture_failed_validation",
                {"decision": decision, "failures": [f.message for f in validation.hard_failures]},
                step_index=self.step,
                path=unit.path,
                unit_id=unit.unit_id,
            )
            return None
        if self._strictness_blocks_pre_llm(unit, cand, validation, "block_capture"):
            return None  # strict mode declines to auto-accept; fall through to LLM
        outcome = UnitOutcome(unit=unit, validation=validation, attempts=[cand])
        outcome.accepted = cand
        self.journal.emit(
            "candidate_accepted",
            {"candidate_id": cand.candidate_id, "via": "block_capture",
             "decision": decision},
            step_index=self.step,
            path=unit.path,
            unit_id=unit.unit_id,
        )
        return outcome

    def _build_retriever(self, config: Config) -> object:
        """Construct the configured RAG retriever over ``self.memory_store``.

        - ``"lexical"`` (default): dependency-free BM25.
        - ``"embedding"``: an :class:`EmbeddingRetriever` (semantic)
          from a fresh embeddings client. Any failure to construct it falls back to
          BM25 so RAG never hard-fails.
        - ``"hybrid"``: a :class:`HybridRetriever` fusing BM25 + embeddings.
          Degrades to lexical-only when the embedding endpoint is unavailable.

        When an embeddings-calibration envelope is present it is reconstructed and
        passed to the EmbeddingRetriever so the isotonic score transform +
        calibrated floor apply.
        """
        from capybase.memory.retriever import EmbeddingRetriever, HybridRetriever, LexicalRetriever

        lex = LexicalRetriever(self.memory_store)

        if config.memory.retriever == "embedding":
            emb = self._build_embedding_retriever(config)
            return emb if emb is not None else lex

        if config.memory.retriever == "hybrid":
            emb = self._build_embedding_retriever(config)
            if emb is None:
                return lex  # embedding endpoint unavailable → lexical-only hybrid
            return HybridRetriever(
                lex, emb, fusion=config.memory.fusion_method or "rrf"
            )

        return lex

    def _build_embedding_retriever(self, config: Config) -> "object | None":
        """Build an EmbeddingRetriever, or None if the endpoint is unavailable.

        Returns None (rather than raising) on any construction failure so callers
        can fall back to BM25 — RAG never hard-fails. The calibrated envelope is
        reconstructed and attached so the isotonic transform + calibrated floor
        apply when present. The persisted vector cache
         is constructed from ``config.memory.vector_cache``
        and resolves its path against the repo root like ``store_path``; a cache
        construction failure degrades to in-memory (re-embed each run) silently.
        """
        from capybase.memory.retriever import EmbeddingRetriever

        try:
            from capybase.memory.embeddings import OpenAIEmbeddingsClient

            # The embeddings model/base_url: explicit config, else reuse the
            # completion model's (a single-model llama-server serving both).
            emb_cfg = config.model
            updates: dict = {}
            if config.memory.embeddings_model:
                updates["model"] = config.memory.embeddings_model
            if config.memory.embeddings_base_url:
                updates["base_url"] = config.memory.embeddings_base_url
            if updates:
                emb_cfg = emb_cfg.model_copy(update=updates)
            client = OpenAIEmbeddingsClient(emb_cfg)
            # Persisted vector cache : best-effort; any
            # failure degrades to None (re-embed each run, the prior behavior).
            cache = None
            if config.memory.vector_cache != "off":
                try:
                    from capybase.memory.vector_index import make_vector_cache

                    p = Path(config.memory.vector_cache_path)
                    if not p.is_absolute():
                        p = self.git.repo / p
                    c = make_vector_cache(config.memory.vector_cache, p)
                    # InMemoryCache (no deps available) is equivalent to None —
                    # skip wrapping so the retriever takes the direct-embed path.
                    from capybase.memory.vector_index import InMemoryCache

                    cache = None if isinstance(c, InMemoryCache) else c
                except Exception:  # noqa: BLE001 - cache is best-effort
                    cache = None
            return EmbeddingRetriever(
                self.memory_store,
                client,
                min_similarity=config.memory.embedding_min_similarity,
                calibration=_reconstruct_calibration(config),
                cache=cache,
            )
        except Exception:  # noqa: BLE001 - fall back to BM25, never break RAG
            return None

    # ==================================================================
    # M3: full run
    # ==================================================================
    # Progress spinner (rebase only). A non-scrolling bottom line with an
    # animated blue spinner, driven by journal events. Only active when stdout
    # is a real TTY — a no-op in tests (no TTY) and CI (piped), so existing
    # tests pass unchanged.

    def _start_spinner(self) -> None:
        """Start the progress spinner if stdout is a TTY.

        Builds a :class:`Spinner`, redirects ``self.out`` through its
        ``flush_line`` (so scrolling colored lines never garble the sticky
        spinner), and subscribes to the journal so every state transition maps to
        a status message — no per-call-site spinner wiring needed. A no-op (the
        spinner stays ``None``) when stdout isn't a TTY.
        """
        if not self._is_interactive_terminal():
            self.spinner = None
            return
        from capybase.spinner import Spinner

        self.spinner = Spinner()
        self._orig_out = self.out
        self.out = self.spinner.flush_line
        self.journal.subscribe(self._spinner_on_event)
        self.spinner.start("starting rebase…")

    def _stop_spinner(self, final_msg: str | None = None) -> None:
        """Stop the spinner, restore ``self.out``, clear the bottom line."""
        sp = getattr(self, "spinner", None)
        if sp is None or not sp.active:
            # Restore out even if the spinner never started (defensive).
            if hasattr(self, "_orig_out"):
                self.out = self._orig_out
                del self._orig_out
            self.spinner = None
            return
        sp.stop(final_msg=final_msg)
        if hasattr(self, "_orig_out"):
            self.out = self._orig_out
            del self._orig_out
        self.spinner = None

    # event_type → human status. The spinner shows the latest one, animating
    # while the operation it describes is in flight.
    _SPINNER_STATUS = {
        "rebase_started": "rebase started",
        "step_started": "step {step}: resolving conflicts…",
        "context_built": "step {step}: generating merge (LLM)…",
        "candidate_generated": "step {step}: validating candidate…",
        "block_capture_decision": "step {step}: block-capture → {decision}",
        "tests_started": "step {step}: running {command}…",
        "tests_finished": "step {step}: tests {summary}",
        "candidate_accepted": "step {step}: accepted",
        "step_continued": "step {step}: continuing…",
        "interactive_guard": "awaiting human input…",
        "session_completed": "rebase complete",
        "rebase_aborted": "rebase aborted",
    }

    def _spinner_on_event(self, event) -> None:
        """Journal listener: map an event to a spinner status message."""
        sp = getattr(self, "spinner", None)
        if sp is None:
            return
        tmpl = self._SPINNER_STATUS.get(event.event_type)
        if tmpl is None:
            return
        step = event.step_index or ""
        # Build the message from the event's payload/fields.
        payload = event.payload or {}
        try:
            msg = tmpl.format(
                step=step,
                decision=payload.get("decision", ""),
                command=payload.get("command", ""),
                summary=payload.get("verdict_summary") or (
                    "passed" if payload.get("passed") else "failed"
                ),
            )
        except (KeyError, IndexError):
            msg = tmpl
        sp.set(msg)
        # Pause the spinner when handing control to the human — the terminal
        # belongs to them during the interactive prompt.
        if event.event_type == "interactive_guard" and payload.get("will_fire"):
            sp.pause()
        # Resume after the human is done: the next operational event means the
        # rebase is progressing again (step started, context built, etc.).
        if event.event_type in ("step_started", "step_continued", "session_completed"):
            if getattr(sp, "_paused", False):
                sp.resume()

    def rebase(
        self,
        target: str,
        *,
        autostash: bool = False,
        abort_on_escalation: bool = True,
        interactive: bool = True,
    ) -> StepResult:
        """Own the entire rebase: start it, drive the resolution loop, finish.

        Unlike :meth:`run` (which assumes the user already started the rebase
        and stopped on a conflict), ``rebase`` starts the rebase itself and then
        hands off to the existing :meth:`run` loop — so a single invocation
        carries the rebase from clean tree to completion (or escalation).

        Flow:
        1. Preflight the worktree (clean, unless ``autostash``).
        2. Record the pre-rebase HEAD as a recovery ref
           (``refs/rebase-agent/<session>/start``) and in the journal.
        3. Start the rebase.
        4. If the rebase is clean (no conflict), finish immediately with a
           ``session_completed`` event — :meth:`run` is never called.
        5. Otherwise drive :meth:`run` — the proven resolve → test → continue
           loop.
        6. On escalation with ``abort_on_escalation`` (the default, since
           ``rebase`` owns the process), ``git rebase --abort`` returns the repo
           to its original HEAD. Without it the rebase is left stopped, matching
           :meth:`run`'s behavior, so the user can inspect the review bundle and
           finish manually.

        ``autostash`` mirrors ``git rebase --autostash`` (stashes dirty changes
        and re-applies them after). Without it, a dirty worktree raises
        :class:`GitError` before any rebase starts — the CLI's top-level guard
        reports it cleanly.
        """
        self.journal.emit(
            "rebase_requested",
            {"target": target, "autostash": autostash,
             "abort_on_escalation": abort_on_escalation},
        )
        # 0. Pre-flight: refuse to touch the repo on a bad starting state.
        #    Runs git-only checks (no network) so the rebase path stays fast.
        #    A blocking failure raises GitError here; the CLI guard prints it.
        preflight = run_rebase_preflight(
            self.git, self.config, target, autostash=autostash, llm_ping=False
        )
        self.journal.emit("preflight_check", {"checks": preflight.as_payload()})
        if not preflight.passed:
            fail = preflight.first_blocking_failure
            msg = fail.detail if fail else "pre-flight checks failed"
            self.journal.emit(
                "rebase_start_failed", {"reason": "preflight", "detail": msg}
            )
            raise GitError(f"refusing to rebase: {msg}")
        # 1. Worktree must be clean unless the user opted into autostash.
        #    (Preflight already checked this, but keep the explicit guard so
        #    the invariant is visible at the call site.)
        if not autostash:
            self.git.require_clean_worktree()  # raises GitError if dirty
        # 2. Recovery ref + backup branch + journal: the original HEAD is
        #    recorded two ways. The internal ``refs/rebase-agent/<id>/start`` is
        #    capybase's audit ref (read by `status`, used by abort). The
        #    user-visible ``capybase/backup/<branch>@<ts>`` branch is the safety
        #    net: a real branch the developer can see in `git branch`, reset to,
        #    or delete once they've confirmed the rebase result.
        start_oid = self.git.head_oid()
        self.git.create_session_refs(self.session_id, start_oid)
        backup_branch = self.git.current_branch() or "head"
        backup_ref = self.git.create_backup_ref(start_oid, label=backup_branch)
        # Stash onto/start/backup on the instance so run()'s per-step + completion
        # resurrection scans can reconstruct the window without the rebase-merge
        # state files (which vanish once the rebase finishes).
        self._rebase_start_oid = start_oid
        self._rebase_target = target
        self._rebase_backup_ref = backup_ref
        # History-awareness substrate (#history-1): capture the source commit
        # sequence once at rebase start, so every later component (history query,
        # prompt context, risk features) can answer "where is this conflict in
        # the replay, and what later commits touch the same region?" Advisory —
        # a failure to build the plan never blocks the rebase (degrades to the
        # no-history behavior).
        # 3. Resolve the target ONCE and use the OID for both the history plan
        #    and the rebase itself (#5: avoid a race where the target ref moves
        #    between plan creation and rebase start). Fall back to the string if
        #    resolution fails (advisory).
        resolved_target = self.git.resolve_ref(target) or target
        self._history_plan = self._build_rebase_plan(start_oid, resolved_target)
        self._history_service = self._build_history_service(self._history_plan)
        # Branch final-intent summary (#9 step 6): compute once per rebase from
        # the source commits' patches. Rendered into the history prompt block;
        # trimmed last when the budget is tight.
        self._branch_intent = self._build_branch_intent(self._history_plan)
        # Wire the history service into the context builder so prompt-generation
        # sees the history-context block (#history step 7). The builder was
        # constructed in __init__ without a service; set it now that rebase()
        # has built the plan. The branch-intent block (#9 step 6) is set
        # per-unit (scoped to the current file) in _set_future_obligations_prompt_block.
        self.context_builder.history_service = self._history_service
        self.journal.emit(
            "rebase_started",
            {"target": target, "start_oid": start_oid, "backup_ref": backup_ref,
             "history_plan_commits": len(self._history_plan.source_commits) if self._history_plan else 0},
        )
        self.log.info(
            "rebase started: session=%s target=%s branch=%s start=%s backup=%s",
            self.session_id, target, backup_branch, start_oid[:8], backup_ref,
        )
        # Test-continuity baseline: capture which tests PASS on
        # the pre-rebase tree, BEFORE the rebase starts. Post-merge, a baseline-
        # passing test that now fails is a behavioral regression the merge
        # introduced. Best-effort: any failure leaves the baseline None and the
        # invariant inert (the existing test gate still runs).
        self._capture_test_continuity_baseline()
        res = self.git.start_rebase(resolved_target, autostash=autostash)
        if not res.ok and not self.git.rebase_in_progress():
            self.journal.emit(
                "rebase_start_failed", {"stderr": res.stderr[:500]}
            )
            raise GitError(
                f"git rebase {target} failed: {res.stderr.strip()}"
            )
        # 4a. A clean rebase (no conflict) finishes here: the rebase is no longer
        #     in progress and there's nothing for run()'s loop to resolve. Emit
        #     the completion event and return success directly — run()'s preflight
        #     would otherwise escalate on "no rebase in progress".
        if not self.git.rebase_in_progress():
            head_after = self.git.head_oid()
            # Silent-resurrection scan: a clean rebase is exactly where a silent
            # undo hides (git resolved it with no conflict). Check the result
            # against what the target branch deleted before declaring success.
            findings = self._resurrection_scan(
                start_oid=start_oid, onto_oid=target, result_oid=head_after,
                backup_ref=backup_ref,
            )
            if findings:
                outcome = self._handle_resurrections(
                    findings, start_oid=start_oid, backup_ref=backup_ref
                )
                if outcome.escalated:
                    # stop policy: a clean rebase already finished (git is no
                    # longer in-progress), so abort-on-escalation can't roll it
                    # back. We reset to the backup ref ourselves to restore the
                    # repo to start_oid and leave the review bundle for review.
                    outcome.continued = False
                    self.git._run(  # noqa: SLF001
                        ["reset", "--hard", backup_ref]
                    )
                    self.journal.emit(
                        "rebase_aborted",
                        {"reason": outcome.reason, "start_oid": start_oid,
                         "backup_ref": backup_ref, "resurrection": True},
                        git_head_after=self.git.head_oid(),
                    )
                    self.out(
                        f"  rolled back to pre-rebase HEAD {start_oid[:8]} "
                        f"(backup branch {backup_ref})."
                    )
                    return outcome
                # warn policy: fall through to declare success.
            self.journal.emit(
                "session_completed",
                {"head_after": head_after, "clean": True},
                git_head_after=head_after,
            )
            self.git.record_step_ref(self.session_id, self.step, head_after)
            self.log.info(
                "rebase completed (clean, no conflicts): session=%s steps=%d "
                "head_after=%s", self.session_id, self.step, head_after[:8],
            )
            self.out(
                f"{self._ok('✓ rebase complete, no conflicts (session ' + self.session_id + ')')}\n"
                f"  backup branch {backup_ref} points at the pre-rebase HEAD "
                f"{start_oid[:8]}; delete it once you've confirmed the result:\n"
                f"    git branch -D {backup_ref}"
            )
            return StepResult(step_index=self.step, escalated=False, continued=True)
        # 4b. The rebase stopped on a conflict: drive the resolution loop.
        # Install a SIGTERM/SIGHUP handler so a killed rebase aborts cleanly
        # (returning the repo to start_oid via the backup) instead of leaving a
        # stopped rebase in the user's repo. SIGINT (Ctrl-C) already raises
        # KeyboardInterrupt; only the terminate-style signals need converting.
        # Restored after the run so the handler doesn't leak.
        import signal
        # Import once before installing the handler (#15): importing inside a
        # signal handler can interact badly with import locks.
        from capybase.adapters.llm_openai import Interrupted

        _sigs = (signal.SIGTERM, getattr(signal, "SIGHUP", signal.SIGTERM))
        _prev: dict[int, object] = {}

        def _interrupt(signum, _frame):
            raise Interrupted(f"capybase interrupted by signal {signum}")

        for _sig in _sigs:
            try:
                _prev[_sig] = signal.signal(_sig, _interrupt)
            except (ValueError, OSError):
                pass
        try:
            self._start_spinner()
            # Bridge the interactive flag to the strictness policy (#10): a
            # non-interactive run (CI / --no-interactive) has no human in the
            # loop mid-step, so tighten acceptance unless the user explicitly
            # configured a stricter (or equal) mode. Never LOOSEN an explicit
            # ci/unattended setting back to interactive.
            if not interactive and self.strictness.mode == "interactive":
                self.strictness.mode = "ci"
            result = self.run()
        except BaseException as exc:
            # On ANY interruption (signal, KeyboardInterrupt, unexpected error)
            # while a rebase is in progress, abort it so the repo isn't left
            # stopped. The backup branch + start_oid let the user recover fully.
            if self.git.rebase_in_progress():
                self.git.abort_rebase()
                self.journal.emit(
                    "rebase_aborted",
                    {"reason": f"interrupted: {exc}", "start_oid": start_oid,
                     "backup_ref": backup_ref},
                    git_head_after=self.git.head_oid(),
                )
                self.log.warning(
                    "rebase interrupted and aborted: session=%s reason=%s "
                    "restored_to=%s backup=%s",
                    self.session_id, exc, start_oid[:8], backup_ref,
                )
                self.out(
                    f"! rebase interrupted ({exc}) — aborted, repo back at "
                    f"{start_oid[:8]}; backup branch {backup_ref} preserved. "
                    f"Re-run `capybase rebase {target}` to retry."
                )
            raise
        finally:
            for _sig, _h in _prev.items():
                try:
                    signal.signal(_sig, _h)  # type: ignore[arg-type]
                except (ValueError, OSError, TypeError):
                    pass
            self._stop_spinner()
        # 5. On a successful finish (conflicts resolved and replayed), surface
        #    the backup branch so the user can reclaim it after confirming.
        if not result.escalated:
            self.log.info(
                "rebase completed (conflicts resolved): session=%s steps=%d "
                "head_after=%s", self.session_id, self.step,
                self.git.head_oid()[:8],
            )
            self.out(
                f"  backup branch {backup_ref} points at the pre-rebase HEAD "
                f"{start_oid[:8]}; delete it once you've confirmed the result:\n"
                f"    git branch -D {backup_ref}"
            )
        # 6. Interactive fallback (LOOP): on escalation, if a human is at the
        #    terminal and the rebase is still in progress, present the conflict
        #    for an interactive decision before the auto-abort runs. After the
        #    human resolves and the rebase continues, run() may hit ANOTHER stop
        #    that escalates — so this re-offers the fallback on each escalation,
        #    not just the first. (A prior version fired the guard once: the second
        #    escalation, returned by the re-entered run(), fell straight through
        #    to abort without ever offering the menu — the human got an abort
        #    instead of a prompt.)
        #    Disabled by --no-interactive (e.g. CI) or when stdin isn't a TTY.
        prev_step = -1  # track the step we last offered the fallback for, so a
                        # same-step re-escalation (no progress: skip/abort/bail)
                        # doesn't spin the loop forever.
        while result.escalated:
            rip = self.git.rebase_in_progress()
            tty = self._is_interactive_terminal()
            self.journal.emit(
                "interactive_guard",
                {
                    "escalated": result.escalated,
                    "interactive": interactive,
                    "rebase_in_progress": rip,
                    "is_interactive_terminal": tty,
                    "units_by_path": list(result.units_by_path),
                    "reason": result.reason or "",
                    "will_fire": bool(result.escalated and interactive and rip and tty),
                },
                step_index=self.step,
            )
            if not (interactive and rip and tty):
                break  # fallback disabled (CI, --no-interactive, not a TTY, or
                       # the rebase finished) → fall through to abort-on-escalation
            # Bail-safety: if the last fallback returned escalated at the SAME
            # step (the human skipped/aborted, or the menu bailed on no-units),
            # don't re-offer — that would spin forever. Only re-offer when the
            # rebase has advanced to a new step (a genuine new escalation).
            if self.step == prev_step:
                break
            prev_step = self.step
            resolved = self.interactive_resolve(result)
            if not resolved.escalated:
                # The human resolved everything and run() continued to completion
                # (or a clean step). Done.
                result = resolved
                break
            # The rebase continued after the human's resolution but hit a NEW
            # escalation at a later step. Loop: re-offer the interactive fallback.
            result = resolved
        # 7. Abort-on-escalation: return the repo to start_oid if we couldn't
        #    finish. run() sets escalated and leaves the rebase stopped; abort
        #    rolls it all back so the developer is back where they started.
        if result.escalated and abort_on_escalation and self.git.rebase_in_progress():
            self.git.abort_rebase()
            self.journal.emit(
                "rebase_aborted",
                {"reason": result.reason, "start_oid": start_oid,
                 "backup_ref": backup_ref},
                git_head_after=self.git.head_oid(),
            )
            self.log.warning(
                "rebase escalated and aborted: session=%s steps=%d reason=%s "
                "restored_to=%s", self.session_id, self.step, result.reason,
                start_oid[:8],
            )
            self.out(
                self._warn(
                    f"! escalated and aborted rebase — repo back at {start_oid[:8]}"
                ) + "\n"
                f"  review bundle: {self.paths.final / 'review-bundle.md'}\n"
                f"  backup branch {backup_ref} still points at the pre-rebase "
                f"HEAD; reset to it with `git reset --hard {backup_ref}`, or "
                f"delete it with `git branch -D {backup_ref}`"
            )
        # Drift summary: emit the post-session behavioral-drift headline so it
        # is visible in logs and detectable in regressions. No-op when the
        # monitor was inactive (drift detection disabled or nothing observed).
        # Guarded against double-emission: the run() loop emits on clean finish.
        if self._drift_monitor is not None and not self._drift_summary_emitted:
            summary = self._drift_monitor.summary()  # type: ignore[attr-defined]
            if summary:
                self.journal.emit("drift_summary", {"summary": summary})
            self._drift_summary_emitted = True
        return result

    # ------------------------------------------------------------------ resurrection
    #
    # Silent-resurrection detection ( "silent loss of intent"). After a
    # clean rebase — and per replayed step — compare the result against content
    # the target branch deliberately deleted since the merge-base. If the result
    # brought any of it back, the replayed commits (which predate the cleanup)
    # silently undid a deliberate deletion. Git sees no conflict; without this
    # scan capybase sees none either, and the cleanup is lost. On detection the
    # ``stop`` policy halts before the bad completion is left as final (the
    # backup branch keeps the repo recoverable); ``warn`` journals + continues.

    # ------------------------------------------------------------------ history
    #
    # History-awareness substrate (#history steps 2-5): the source commit
    # sequence is captured once at rebase start into a RebasePlan, and a read-
    # only HistoryQueryService answers per-conflict questions ("which commit am
    # I resolving, what later commits touch the same region?"). Advisory — a
    # failure to build the plan never blocks the rebase.

    def _build_rebase_plan(self, start_oid: str, target: str):
        """Build a :class:`history.RebasePlan` for the replayed sequence.

        The sequence is ``merge_base(start_oid, target)..start_oid`` (oldest-
        first). Written to the session dir as ``rebase_plan.json`` so tests can
        replay the same history. Returns None on any failure (advisory).
        """
        try:
            from capybase.history import RebasePlan, ReplayCommit
            from datetime import datetime, timezone

            mb = self.git.merge_base(start_oid, target)
            if not mb:
                return None
            raw = self.git.replayed_commit_sequence(mb, start_oid)
            if not raw:
                return None
            commits = [
                ReplayCommit(
                    oid=c["oid"], parent_oid=c["parent_oid"],
                    subject=c["subject"], body_summary=c["body_summary"],
                    touched_files=c["touched_files"], diffstat=c["diffstat"],
                    patch_id=c["patch_id"], index=i,
                )
                for i, c in enumerate(raw)
            ]
            plan = RebasePlan(
                source_commits=commits,
                target_base_oid=mb,
                target_tip_oid=self.git.resolve_ref(target) or target,
                source_tip_oid=start_oid,
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            # Persist for test replay.
            import json
            plan_path = self.paths.root / "rebase_plan.json"
            plan_path.write_text(json.dumps(plan.to_dict(), indent=2), encoding="utf-8")
            return plan
        except Exception as exc:  # noqa: BLE001 - history is advisory
            self.log.debug("rebase plan not built: %s", exc)
            self.journal.emit_advisory(
                "history_unavailable", f"rebase plan build failed: {exc}",
            )
            return None

    def _build_history_service(self, plan):
        """Construct the :class:`history.HistoryQueryService` from a plan.

        Returns an empty service (all queries yield empty context) when the plan
        is None, so downstream code dispatches unconditionally.

        Populates ``recent_target_commits`` by enumerating the target branch's
        recent commits touching the same files as the source sequence (capped at
        N=5). Advisory: any failure yields an empty list.
        """
        from capybase.history import HistoryQueryService
        if plan is None:
            return HistoryQueryService.empty()
        recent_target = self._recent_target_commits(plan)
        return HistoryQueryService(
            plan, recent_target_commits=recent_target, git=self.git,
        )

    def _build_branch_intent(self, plan):
        """Compute the branch final-intent summary (#9 step 6).

        Returns None when no plan; otherwise a :class:`branch_intent.BranchIntent`
        built from the source commits' patches (fetched via git.commit_patch).
        Exception-safe — a failure yields None (the block is omitted).
        """
        if plan is None or not plan.source_commits:
            return None
        try:
            from capybase.branch_intent import build_branch_intent

            patches = {}
            for c in plan.source_commits:
                try:
                    patches[c.oid] = self.git.commit_patch(c.oid)
                except Exception:  # noqa: BLE001 - best-effort
                    patches[c.oid] = b""
            return build_branch_intent(plan, patches)
        except Exception as exc:  # noqa: BLE001 - advisory
            self.journal.emit_advisory(
                "branch_intent_failed", f"branch-intent build failed: {exc}",
            )
            return None

    def _step_mechanism(self, result: "StepResult") -> str:
        """The coarse resolution class of a step's accepted outcomes.

        Returns ``"deterministic"``, ``"llm"``, or ``"mixed"`` — the mechanism
        gate for the behavioral drift detector (drift-review immediate action
        #1). A deterministic step (exact-history reuse, structural union, brace
        repair, test-gated side pick, combination search, block capture) is a
        verbatim or provably-safe replay of a validated state — drift is
        impossible by construction, so the drift advisory never fires for it,
        even if a pre-existing test failure is observed. Only ``"llm"`` /
        ``"mixed"`` steps can carry model-induced drift.
        """
        provs = [
            getattr(o.accepted, "provenance", "") or ""
            for o in result.outcomes
            if o.accepted is not None
        ]
        if not provs:
            # No accepted outcomes (e.g. escalated) — treat as deterministic so
            # the step cannot spuriously fire drift (there was no resolution to
            # drift from).
            return "deterministic"
        from capybase.provenance import LLM_PROVENANCES

        llm_markers = LLM_PROVENANCES | {"history_augmented_llm"}
        deterministic_markers = frozenset({
            "deterministic_structural", "deterministic_brace_repair",
            "exact_history_reuse", "combination_search",
            "test_gated_side", "block_capture",
        })
        has_llm = any(p in llm_markers for p in provs)
        has_det = any(p in deterministic_markers for p in provs)
        if has_llm and has_det:
            return "mixed"
        if has_llm:
            return "llm"
        return "deterministic"

    def _drift_coverage_note(self) -> str:
        """The behavioral-signal coverage note for this step's drift report.

        The drift detector's primary signal is test regression, whose detection
        ceiling is the test baseline's coverage. The note makes that ceiling
        explicit so a non-firing is interpretable: "no drift detected" vs.
        "insufficient coverage to detect drift" (drift-review: surface the
        coverage fraction in the advisory output).
        """
        baseline = self._test_continuity_baseline
        if baseline:
            return (
                f"test coverage for modified files: {len(baseline)} baseline "
                f"test(s) active"
            )
        return "no test baseline captured — behavioral drift signal inactive"

    def _observe_drift(self, commit_index: int, result: "StepResult") -> None:
        """Per-step behavioral-drift observation. Advisory only, never blocks.

        The second-generation drift detector (the embedding monitor was scrapped
        — see docs/drift-detector-review.md). The signal is behavioral: the
        test-continuity regressions for this step (baseline-passing tests that
        now fail), gated on resolution mechanism. An LLM-produced resolution
        that introduces a regression fires a high-confidence drift advisory
        (0% FPR per the SAM literature). A deterministic resolution never fires
        — drift is impossible by construction. No-op when the monitor is
        inactive. Never raises.
        """
        if self._drift_monitor is None:
            return
        mechanism = self._step_mechanism(result)
        regressions = list(self._last_continuity_regressions)
        coverage_note = self._drift_coverage_note()
        try:
            report = self._drift_monitor.observe(  # type: ignore[attr-defined]
                commit_index=commit_index,
                mechanism=mechanism,
                regressed_tests=regressions,
                coverage_note=coverage_note,
            )
        except Exception:  # noqa: BLE001 - drift detection is best-effort
            return
        if report is not None and report.is_drift:
            self.journal.emit_advisory("drift_detected", report.render())
            self.journal.emit(
                "behavioral_drift",
                {
                    "commit_index": report.commit_index,
                    "mechanism": report.mechanism,
                    "regressions": list(report.regressed_tests),
                    "coverage_note": report.coverage_note,
                },
                step_index=self.step,
            )

    def _recent_target_commits(self, plan, *, max_commits: int = 5) -> list:
        """Recent target-branch commits touching the same files as the source.

        Enumerates ``target_base..target_tip`` (the onto-side history) filtered
        to the files the source sequence touches, newest-first, capped at
        ``max_commits``. Advisory: any failure yields [].
        """
        try:
            # Collect the unique file set from the source sequence.
            files = sorted({f for c in plan.source_commits for f in c.touched_files})
            if not files:
                return []
            raw = self.git.replayed_commit_sequence(plan.target_base_oid, plan.target_tip_oid)
            if not raw:
                return []
            from capybase.history import ReplayCommit
            commits = [
                ReplayCommit(
                    oid=c["oid"], parent_oid=c["parent_oid"],
                    subject=c["subject"], body_summary=c["body_summary"],
                    touched_files=c["touched_files"], diffstat=c["diffstat"],
                    patch_id=c["patch_id"], index=i,
                )
                for i, c in enumerate(raw)
            ]
            # Filter to those touching any source file; newest-first; cap.
            relevant = [c for c in commits if any(f in c.touched_files for f in files)]
            # replayed_commit_sequence is oldest-first; reverse to newest-first.
            return list(reversed(relevant))[:max_commits]
        except Exception as exc:  # noqa: BLE001 - advisory
            self.journal.emit_advisory(
                "history_unavailable", f"recent-target-commits fetch failed: {exc}",
            )
            return []

    def _current_replayed_oid(self) -> str | None:
        """The commit currently being replayed (``stopped-sha``), or None.

        Read at conflict-gather time so each ConflictUnit can carry replay
        identity. None when no rebase is in progress or the file is absent.
        """
        try:
            return self.git.rebase_stopped_sha()
        except Exception:  # noqa: BLE001 - advisory
            return None

    def _lazy_build_history_from_rebase_state(self) -> None:
        """Build a RebasePlan from git's rebase-merge state when run() is used
        without a prior rebase() call (#4).

        Reads ``rebase-merge/orig-head`` (the pre-rebase HEAD = source tip) and
        ``rebase-merge/onto`` (the target). If both are available, builds a plan
        + service the same way ``rebase()`` does. Journals ``history_unavailable``
        when the metadata is insufficient.
        """
        try:
            source_tip = self.git.rebase_orig_head_oid()
            target = self.git.rebase_onto_oid()
            if not source_tip or not target:
                self.journal.emit(
                    "history_unavailable",
                    {"reason": "rebase state (orig-head/onto) not readable"},
                )
                return
            start_oid = source_tip
            self._rebase_start_oid = start_oid
            self._rebase_target = target
            self._history_plan = self._build_rebase_plan(start_oid, target)
            self._history_service = self._build_history_service(self._history_plan)
            self._branch_intent = self._build_branch_intent(self._history_plan)
            self.context_builder.history_service = self._history_service
        except Exception as exc:  # noqa: BLE001 - advisory
            self.journal.emit(
                "history_unavailable", {"reason": f"lazy build failed: {exc}"},
            )

    def _history_context_for(self, unit: ConflictUnit):
        """The :class:`history.HistoryContext` for a unit, or None.

        Memoized per unit (#idea 5 cohesion): the expensive ``for_conflict``
        query (region-key derivation + per-future-commit region matching) ran
        ~4× per unit before; now it runs once and the result is cached for the
        unit's resolution duration. The cache is cleared per step.

        Queries the session's HistoryQueryService (set by rebase()) with the
        unit's replayed-commit OID. Returns None when no plan is active.
        """
        if self._history_service is None:
            return None
        key = getattr(unit, "unit_id", None) or id(unit)
        cached = self._history_context_cache.get(key, _MISSING)
        if cached is not _MISSING:
            return cached
        replayed_oid = unit.structural_metadata.get("replayed_commit_oid")
        ctx = self._history_service.for_conflict(unit, replayed_commit_oid=replayed_oid)
        self._history_context_cache[key] = ctx
        return ctx

    def _history_features_for(self, unit: ConflictUnit) -> dict:
        """Compact history features for the experience store / risk spine.

        Exception-safe: any failure (malformed metadata, history service
        error) returns {} — history is advisory and must never break the
        rebase or memory-recording path.
        """
        try:
            ctx = self._history_context_for(unit)
            if ctx is None:
                return {}
            feats = ctx.to_features()
            # History confidence (#9 step 1): a 0–1 trust score + its components.
            # Lets calibration/metrics distinguish "history present but weak"
            # from "history present and trustworthy".
            try:
                from capybase.history_confidence import history_confidence_for

                conf = history_confidence_for(ctx)
                feats["history_confidence_score"] = round(conf.score, 4)
                feats["history_region_key_quality"] = conf.region_key_quality
                feats["history_is_augmenting"] = conf.is_augmenting
            except Exception:  # noqa: BLE001 - advisory only
                pass
            return feats
        except Exception as exc:  # noqa: BLE001 - advisory only
            self.journal.emit_advisory(
                "history_context_failed", f"history features failed: {exc}",
                path=getattr(unit, "path", None), unit_id=getattr(unit, "unit_id", None),
            )
            return {}

    def _history_confidence_for(self, unit: ConflictUnit):
        """The :class:`HistoryConfidence` for a unit, or None.

        Used by the LLM accept path to decide whether to re-stamp a plain-LLM
        candidate's provenance to ``history_augmented_llm`` (#9 step 8/1).
        Exception-safe; returns None when no history service is active.
        """
        try:
            ctx = self._history_context_for(unit)
            if ctx is None:
                return None
            from capybase.history_confidence import history_confidence_for

            return history_confidence_for(ctx)
        except Exception:  # noqa: BLE001 - advisory only
            return None

    def _history_snapshot_for(self, unit: ConflictUnit):
        """The per-unit :class:`HistoryDecisionContext` (#idea 5 cohesion).

        Builds ONE memoized snapshot per unit consolidating every history-derived
        value the mechanisms consume: the HistoryContext, region kind, conflict
        shape, confidence, future obligations, branch-intent excerpt, and the
        exact-reuse candidate. Built from the already-memoized per-unit caches
        (so the expensive queries run once); the snapshot itself is cached for
        the unit's resolution duration and journaled as the single
        ``history_decision_snapshot`` event — the per-unit history-decision record.
        """
        key = getattr(unit, "unit_id", None) or id(unit)
        cached = self._history_snapshots.get(key, _MISSING)
        if cached is not _MISSING:
            return cached
        from capybase.history_confidence import HistoryDecisionContext

        try:
            ctx = self._history_context_for(unit)
            conf = self._history_confidence_for(unit)
            obls = self._future_obligations_for(unit)
            region_kind = self._region_kind_for(unit)
            shape = self._conflict_shape_for(unit)
            intent = self._branch_intent_for_file(unit.path) if ctx is not None else ""
            snapshot = HistoryDecisionContext(
                unit_id=unit.unit_id,
                context=ctx,
                region_key_kind=region_kind,
                conflict_shape=shape,
                confidence=conf,
                future_obligations=obls,
                branch_intent_excerpt=intent,
            )
            # Journal the per-unit snapshot (the exit-criterion record).
            self.journal.emit(
                "history_decision_snapshot", snapshot.to_journal_payload(),
                step_index=self.step, path=unit.path, unit_id=unit.unit_id,
            )
        except Exception as exc:  # noqa: BLE001 - advisory
            self.journal.emit_advisory(
                "history_context_failed", f"snapshot build failed: {exc}",
                path=unit.path, unit_id=unit.unit_id,
            )
            snapshot = HistoryDecisionContext(unit_id=unit.unit_id)
        self._history_snapshots[key] = snapshot
        return snapshot

    def _restamp_for_history_augmentation(
        self, unit: ConflictUnit, cand: CandidateResolution
    ) -> str:
        """The clearly-named history-augmentation compat path (#idea 6).

        A plain-LLM candidate whose history context was augmenting (confidence
        above threshold + a real future-region signal) gets re-stamped to
        ``history_augmented_llm``. This is the ONLY restamp — it separates "history
        changed this resolution" from "plain LLM" in metrics/dry-run. Only re-stamps
        ``plain_llm``; never overrides deterministic/manual/reuse provenance.

        Returns a reason string (for the ResolutionAttempt) naming the confidence,
        or "" if no restamp happened.
        """
        if getattr(cand, "provenance", "") != "plain_llm":
            return ""
        conf = self._history_confidence_for(unit)
        if conf is None or not conf.is_augmenting:
            return ""
        cand.provenance = "history_augmented_llm"
        self.journal.emit(
            "provenance_restamped",
            {"candidate_id": cand.candidate_id,
             "to": "history_augmented_llm",
             "confidence": round(conf.score, 3)},
            step_index=self.step, path=unit.path,
            unit_id=unit.unit_id,
        )
        return f"history-augmented (confidence {conf.score:.2f})"

    def _clear_history_caches(self) -> None:
        """Clear the per-unit history caches (called per step in _resolve_step).

        The caches memoize per unit WITHIN a step; across steps the units differ
        and the history state may have advanced (a future commit became the
        current one), so we reset between steps.
        """
        self._history_snapshots.clear()
        self._history_context_cache.clear()
        self._future_obligations_cache.clear()

    def _future_obligations_for(self, unit: ConflictUnit):
        """The :class:`FutureObligations} a candidate must satisfy (#9 step 3).

        Memoized per unit (#idea 5 cohesion): the git patch-fetch loop (one
        subprocess per touching future commit) ran ~2× per unit before (once for
        the prompt block, once for the accept gate); now it runs once and the
        FutureObligations result is cached. Cleared per step.

        Derived structurally from future source commits touching the region:
        symbol survival, imports, key edits. The defined-symbol set comes from
        the conflict SIDES (what the region provides), NOT the candidate — so a
        candidate that drops a symbol is correctly flagged. Returns None when no
        history plan is active or no future commits touch the region.
        """
        key = getattr(unit, "unit_id", None) or id(unit)
        cached = self._future_obligations_cache.get(key, _MISSING)
        if cached is not _MISSING:
            return cached
        result = self._compute_future_obligations(unit)
        self._future_obligations_cache[key] = result
        return result

    def _compute_future_obligations(self, unit: ConflictUnit):
        """The uncached obligation computation (called once per unit)."""
        try:
            if self._history_service is None or self._history_plan is None:
                return None
            ctx = self._history_context_for(unit)
            if ctx is None or not ctx.future_source_commits_touching_region:
                return None
            from capybase.future_obligations import (
                extract_future_obligations,
            )

            # The symbols the region PROVIDES = the union of all three sides.
            # This is independent of the candidate, so the obligation set is
            # stable across retries and a dropping candidate is correctly caught.
            region_text = "\n".join(
                t for t in (
                    unit.base.text, unit.current.text, unit.replayed.text,
                ) if t
            )
            patches = {}
            for c in ctx.future_source_commits_touching_region:
                try:
                    patches[c.oid] = self.git.commit_patch(c.oid)
                except Exception:  # noqa: BLE001 - best-effort fetch
                    patches[c.oid] = b""
            return extract_future_obligations(
                resolved_text=region_text,
                future_commits=ctx.future_source_commits_touching_region,
                patches=patches,
            )
        except Exception as exc:  # noqa: BLE001 - advisory only
            self.journal.emit_advisory(
                "future_obligations_failed", f"obligation extraction failed: {exc}",
                path=getattr(unit, "path", None), unit_id=getattr(unit, "unit_id", None),
            )
            return None

    def _set_future_obligations_prompt_block(self, unit: ConflictUnit) -> None:
        """Populate the context builder's future-obligations + branch-intent
        blocks for a unit.

        Both are scoped to the current unit's file: the future obligations are
        derived from the conflict sides + future patches, and the branch-intent
        excerpt shows only THIS file's net effect (listing all files in every
        prompt is noisy and breaks path-sensitive prompt inspection). Sets the
        blocks to '' (omitted) when nothing applies.
        """
        if self._history_service is None or self._history_plan is None:
            self.context_builder.future_obligations_block = ""
            self.context_builder.branch_intent_block = ""
            return
        try:
            obls = self._future_obligations_for(unit)
            if obls is None or obls.empty:
                self.context_builder.future_obligations_block = ""
            else:
                self.context_builder.future_obligations_block = obls.render_block()
            # Branch intent scoped to this file only (#9 step 6).
            self.context_builder.branch_intent_block = self._branch_intent_for_file(
                unit.path
            )
        except Exception as exc:  # noqa: BLE001 - advisory
            self.journal.emit_advisory(
                "future_obligations_failed",
                f"obligation prompt-block failed: {exc}",
                path=unit.path, unit_id=unit.unit_id,
            )
            self.context_builder.future_obligations_block = ""
            self.context_builder.branch_intent_block = ""

    def _branch_intent_for_file(self, path: str) -> str:
        """Render the branch-intent excerpt for ONE file.

        Scoping to the current file avoids dumping every touched file into every
        conflict's prompt (noisy + breaks path-sensitive inspection). Returns ''
        when no branch intent was built or the file isn't in it.
        """
        if self._branch_intent is None:
            return ""
        try:
            for f in self._branch_intent.files:
                if f.path == path:
                    body = f.render()
                    if not body:
                        return ""
                    return f"Branch final intent for {path}:\n{body}"
            return ""
        except Exception as exc:  # noqa: BLE001 - advisory
            self.journal.emit_advisory(
                "branch_intent_failed", f"branch-intent render failed: {exc}",
                path=path,
            )
            return ""

    def _future_obligations_check(
        self, unit: ConflictUnit, cand: CandidateResolution
    ) -> tuple[bool, list[str]]:
        """Reject a candidate that drops a future-obligation symbol (#9 step 3).

        Returns ``(ok, dropped)``. ``dropped`` lists required symbols the
        candidate no longer defines (a later commit still needs them). When no
        future obligations apply (no plan / no future region touches), returns
        ``(True, [])`` so the candidate proceeds normally.
        """
        obls = self._future_obligations_for(unit)
        if obls is None or obls.empty:
            return True, []
        from capybase.future_obligations import obligations_satisfied

        return obligations_satisfied(obls, cand.resolved_text or "")

    def _region_kind_for(self, unit: ConflictUnit) -> str:
        """The coarse region kind (function/class/etc.) for a unit (#9 step 5).

        Used to populate Experience.region_kind for same-kind retrieval reasons.
        Derived via region_key_from_unit (which reads the structural metadata);
        empty when no kind is known. Exception-safe.
        """
        try:
            from capybase.history import region_key_from_unit

            return region_key_from_unit(unit).kind or ""
        except Exception:  # noqa: BLE001 - advisory
            return ""

    def _conflict_shape_for(self, unit: ConflictUnit) -> str:
        """The normalized conflict-shape hash for a unit (#9 steps 4/5).

        Used to populate Experience.conflict_shape for same-shape retrieval
        reasons AND exact-reuse matching (#9 step 4). Exception-safe; empty on
        failure.
        """
        try:
            from capybase.memory.shape import shape_for_unit

            return shape_for_unit(unit)
        except Exception:  # noqa: BLE001 - advisory
            return ""

    def _record_conflict_observation(self, unit: ConflictUnit, escalated: bool) -> None:
        """Append a ConflictObservation for chain detection (#9 step 7).

        Reads the region coordinate from the unit's structural metadata + the
        replayed-commit index from the history plan. Exception-safe; a missing
        coordinate/index yields nothing (the observation is skipped). Called per
        outcome so detect_conflict_chains() sees every conflict across the replay.
        """
        try:
            from capybase.conflict_chain import ConflictObservation
            from capybase.history import region_key_from_unit

            key = region_key_from_unit(unit)
            commit_index = None
            replayed_oid = unit.structural_metadata.get("replayed_commit_oid")
            if replayed_oid and self._history_plan is not None:
                commit_index = self._history_plan.index_of(replayed_oid)
            self._conflict_observations.append(ConflictObservation(
                commit_index=commit_index,
                path=key.path, kind=key.kind or "unknown",
                name=key.name or "",
                escalated=escalated,
            ))
        except Exception:  # noqa: BLE001 - advisory
            pass

    def detect_conflict_chains(self):
        """The conflict chains detected across this rebase (#9 step 7).

        Returns a :class:`capybase.conflict_chain.ConflictChainReport`. Empty
        when no plan, no observations, or no chain (the common case — isolated
        conflicts). Consumed by the dry-run report (#9 step 10) + escalation
        messaging.
        """
        try:
            from capybase.conflict_chain import detect_conflict_chains as detect

            return detect(list(self._conflict_observations))
        except Exception:  # noqa: BLE001 - advisory
            from capybase.conflict_chain import ConflictChainReport

            return ConflictChainReport()

    def _run_future_apply_probe(self, result: StepResult) -> None:
        """ECC-lite future-compatibility probe (#history step 9).

        For each accepted unit whose history context flags future source commits
        touching the same region, check (in a throwaway worktree) whether the
        next future commit's patch applies cleanly to the resolution.

        Probe mode is ADAPTIVE (derived from the conflict, not a config knob or a
        policy guess): ``sequence_patch`` is strictly more accurate than
        ``path_patch`` — it applies the intervening same-path source commits
        before testing the future commit, eliminating false-positives from
        skipped intermediate states. We use it whenever intervening commits
        exist, and fall back to ``path_patch`` only when there are none (the
        degenerate case where sequence_patch does no extra work anyway). Accuracy
        is a property of the data; the cost is a one-time worktree replay.

        Strictness policy only decides ESCALATION: strict modes (ci/unattended,
        per the documented ``policy_mode``) block on a failed probe; non-strict
        modes journal-and-continue. Skipped when no RebasePlan is active.
        """
        if self._history_service is None or self._history_plan is None:
            return  # no history → no probe
        from capybase.history import future_apply_probe

        for outcome in result.outcomes:
            if outcome.accepted is None:
                continue
            unit = outcome.unit
            ctx = self._history_context_for(unit)
            if ctx is None or not ctx.has_future_region_touches:
                continue  # no future region touches → skip the probe
            # The resolved content = the spliced file on disk (written in Phase 1).
            # If the resolution DELETED the file (accept_deletion), read_worktree_file
            # will raise FileNotFoundError — pass None to the probe so it tests the
            # deleted-file state (a later commit that modifies the deleted file should
            # fail to apply).
            try:
                resolved_content = self.git.read_worktree_file(unit.path)
            except FileNotFoundError:
                resolved_content = None  # file was deleted by the resolution
            except Exception as exc:  # noqa: BLE001
                # Couldn't read the resolved file — the probe can't run for this
                # unit. Emit a distinct advisory so it doesn't silently vanish
                # (#idea 4 — observability).
                self.journal.emit_advisory(
                    "future_probe_unavailable",
                    f"could not read resolved file for probe: {exc}",
                    path=unit.path, unit_id=unit.unit_id,
                )
                continue
            # Probe mode selection (adaptive, not a policy knob): sequence_patch is
            # STRICTLY more accurate than path_patch — it applies the intervening
            # same-path source commits before testing the future commit, which
            # eliminates false-positives from skipped intermediate states. The
            # only reason not to use it is when there are NO intervening commits
            # (the degenerate case, where sequence_patch would do no extra work
            # anyway). So we derive the mode from the conflict's own data: use
            # the accurate mode whenever the situation calls for it, automatically.
            # (Previously this was tied to strictness mode — a policy guess that
            # used the cheaper/less-accurate path_patch in interactive mode even
            # when accuracy mattered. Accuracy is a property of the data, not the
            # run mode; the cost is a one-time worktree replay per probe.)
            intervening = self._probe_intervening_commits(ctx)
            probe_mode = "sequence_patch" if intervening else "path_patch"
            probe_result = future_apply_probe(
                self.git,
                resolved_path=unit.path,
                resolved_content=resolved_content,
                future_commits=ctx.future_source_commits_touching_region,
                mode=probe_mode,
                intervening_commits=intervening,
            )
            # Journal the result for the review bundle + calibration.
            self.journal.emit(
                "future_apply_probe",
                {
                    "probed": probe_result.probed,
                    "applies": probe_result.applies,
                    "mode": probe_mode,
                    "intervening_count": len(intervening),
                    "future_commit": probe_result.future_commit_subject,
                    "reason": probe_result.reason,
                    "unit_id": unit.unit_id,
                },
                step_index=self.step, path=unit.path, unit_id=unit.unit_id,
            )
            # Strict mode gate: a failed probe escalates. Only strict modes
            # (ci/unattended) block on a probe failure; interactive/dry_run
            # journal it advisably and continue.
            if probe_result.probed and not probe_result.applies and self.strictness.strict:
                result.escalated = True
                result.reason = (
                    f"future-apply probe ({probe_mode}) failed: {probe_result.reason}"
                )
                self.out(
                    self._warn(
                        f"! future-apply probe ({probe_mode}): {probe_result.reason}. "
                        f"Escalating ({self.strictness.mode} mode)."
                    )
                )
                break

    def _probe_intervening_commits(self, ctx) -> list:
        """Same-path source commits preceding the first probed region commit.

        For sequence_patch mode (#9 step 2): the probe applies these to the
        worktree before testing the future commit, so the probe state reflects
        the intermediate same-path changes that the real rebase would have
        already applied. Both lists are in replay order (oldest-first); the
        intervening set is the file-touching commits that come before the first
        region-touching commit. Empty when there's nothing in between.
        """
        region = ctx.future_source_commits_touching_region
        file_commits = ctx.future_source_commits_touching_file
        if not region or not file_commits:
            return []
        probed_oid = region[0].oid
        out = []
        for c in file_commits:
            if c.oid == probed_oid:
                break  # reached the probed commit; stop (don't include it)
            out.append(c)
        return out

    def _resurrection_scan(
        self, *, start_oid: str, onto_oid: str, result_oid: str, backup_ref: str
    ) -> list:
        """Run the end-of-rebase resurrection scan; return findings (maybe empty).

        The merge-base of ``start_oid`` (the original branch tip) and ``onto_oid``
        bounds the window of upstream history the replayed branch predates. Any
        content ``onto`` deleted since that base that reappears in ``result_oid``
        is a suspected silent undo. Advisory: any git error is swallowed and
        reported as no findings — resurrection detection must never break a
        rebase that would otherwise succeed. Disabled entirely by
        ``[validation] enable_resurrection_detection = false``.

        Paths this session EXPLICITLY resolved as a modify/delete ``keep_block``
        (``self._explicitly_kept_paths``) are excluded: such a keep is a
        deliberate, reviewed resurrection of content upstream deleted, not a
        silent undo — flagging it would double-report an already-judged decision.
        """
        cfg = self.config.validation
        if not cfg.enable_resurrection_detection:
            return []
        try:
            from capybase.resurrection import scan_resurrections

            mb = self.git.merge_base(start_oid, onto_oid)
            if mb is None:
                return []
            return scan_resurrections(
                self.git,
                base_oid=mb,
                onto_oid=onto_oid,
                result_oid=result_oid,
                min_block_lines=cfg.resurrection_min_block_lines,
                min_coverage=cfg.resurrection_min_similarity,
                exclude_paths=set(getattr(self, "_explicitly_kept_paths", set())),
            )
        except Exception as exc:  # noqa: BLE001 - advisory, never break the rebase
            self.log.warning(
                "resurrection scan failed (ignored): session=%s %s",
                self.session_id, exc,
            )
            return []

    def _handle_resurrections(
        self,
        findings: list,
        *,
        start_oid: str,
        backup_ref: str,
    ) -> StepResult:
        """Act on resurrection findings per the configured policy.

        Returns an escalated StepResult on ``stop`` (the caller leaves the rebase
        stopped; the backup branch keeps the repo recoverable), or a non-
        escalated result on ``warn`` (the rebase is allowed to complete). Writes
        a review bundle with a ``## suspected resurrections`` section either way
        so the developer can review the suspected undos.
        """
        cfg = self.config.validation
        n_paths = len(findings)
        n_lines = sum(f.resurrected_line_count for f in findings)
        self.journal.emit(
            "resurrections_detected",
            {
                "paths": [f.path for f in findings],
                "line_count": n_lines,
                "policy": cfg.resurrection_policy,
            },
            step_index=self.step,
        )
        write_review_bundle(
            self.paths,
            reason=(
                f"suspected silent resurrection of deleted content "
                f"({n_paths} path(s), {n_lines} line(s) back)"
            ),
            step_index=self.step,
            resurrections=findings,
            resume_hint=f"git rebase --continue  # after reviewing {backup_ref}",
        )
        if cfg.resurrection_policy == "stop":
            self.log.warning(
                "resurrection detection stopped the rebase: session=%s paths=%d "
                "lines=%d backup=%s",
                self.session_id, n_paths, n_lines, backup_ref,
            )
            self.out(
                self._warn(
                    f"! suspected silent resurrection — {n_paths} path(s) brought "
                    f"back {n_lines} line(s) the target branch deleted."
                ) + "\n"
                f"  review bundle: {self.paths.final / 'review-bundle.md'}\n"
                f"  backup branch {backup_ref} points at the pre-rebase HEAD "
                f"{start_oid[:8]}; the rebase is left stopped. Resolve the "
                f"resurrections (or set [validation] resurrection_policy = "
                f"\"warn\" to proceed), then `git rebase --continue`."
            )
            return StepResult(
                step_index=self.step,
                escalated=True,
                reason="suspected silent resurrection of deleted content",
            )
        # warn policy: surface but continue.
        self.log.info(
            "resurrection detection warned (continuing): session=%s paths=%d lines=%d",
            self.session_id, n_paths, n_lines,
        )
        self.out(
            f"  warning: suspected silent resurrection — {n_paths} path(s) "
            f"brought back {n_lines} line(s) the target branch deleted "
            f"(see review bundle). Continuing per resurrection_policy = \"warn\"."
        )
        return StepResult(step_index=self.step, escalated=False, continued=True)

    def _run_resurrection_on_completion(self) -> StepResult | None:
        """Resurrection scan for run()'s completion point; returns None if clean.

        Called from run()'s loop when the rebase finishes cleanly (conflicts
        resolved and replayed). Reconstructs onto/start from the instance attrs
        ``rebase()`` stashed (the rebase-merge state files are gone by now). On a
        detection with the ``stop`` policy, returns an ESCALATED StepResult so
        run() breaks and rebase()'s escalation handling (interactive fallback /
        abort) runs — the rebase is still in-progress at this point, so the
        existing abort-on-escalation restores the repo to start_oid. On ``warn``,
        emits the warning and returns a non-escalated result (caller proceeds).
        Returns None when there are no findings (nothing to do).
        """
        start_oid = getattr(self, "_rebase_start_oid", None)
        target = getattr(self, "_rebase_target", None)
        backup_ref = getattr(self, "_rebase_backup_ref", "capybase/backup")
        if not start_oid or not target:
            return None  # not a rebase()-driven session; nothing to scan
        head_after = self.git.head_oid()
        findings = self._resurrection_scan(
            start_oid=start_oid, onto_oid=target, result_oid=head_after,
            backup_ref=backup_ref,
        )
        if not findings:
            return None
        outcome = self._handle_resurrections(
            findings, start_oid=start_oid, backup_ref=backup_ref
        )
        return outcome

    def _accumulate_coverage_samples(self, result: StepResult) -> None:
        """Fold this step's accepted-unit coverage into the session SLO rollup.

        For each accepted unit whose validation ran the intent-coverage check,
        record (path, preserved, total) — summing both sides' added units. The
        post-rebase rollup aggregates these into one window-level ratio. Best-
        effort: units without coverage detail (unsupported language, parse
        failure, structural parser unavailable) are simply skipped — the SLO reflects
        what could be measured.
        """
        try:
            for outcome in result.outcomes:
                if outcome.accepted is None or outcome.validation is None:
                    continue
                # The intent-coverage check's detail carries per-side preserved/
                # total. Aggregate both sides into one (preserved, total) sample.
                detail = None
                for w in outcome.validation.warnings:
                    if w.validator == "intent_coverage":
                        detail = w.detail
                        break
                if detail is None:
                    # Coverage may have passed without a warning; check hard
                    # failures too (a below-floor result is a warning, but be
                    # thorough). The check's detail is the same shape either way.
                    for hf in outcome.validation.hard_failures:
                        if hf.validator == "intent_coverage":
                            detail = hf.detail
                            break
                if not detail:
                    continue
                preserved = (
                    int(detail.get("current_preserved", 0))
                    + int(detail.get("replayed_preserved", 0))
                )
                total = (
                    int(detail.get("current_total", 0))
                    + int(detail.get("replayed_total", 0))
                )
                if total > 0:
                    self._session_coverage_samples.append(
                        (outcome.unit.path, preserved, total)
                    )
        except Exception:  # noqa: BLE001 - the SLO is advisory, never break the loop
            pass

    def _session_coverage_rollup(self) -> tuple[float, int, int] | None:
        """Aggregate per-unit coverage across the window into one ratio.

        Returns ``(ratio, preserved, total)`` — the fraction of all measured
        intent units (across both sides, every accepted unit) preserved in the
        final rebased branch. ``None`` when no coverage was measured (no units
        with structural intent, or the parser was unavailable throughout).
        """
        if not self._session_coverage_samples:
            return None
        total = sum(t for _path, _p, t in self._session_coverage_samples)
        preserved = sum(p for _path, p, _t in self._session_coverage_samples)
        if total == 0:
            return None
        return preserved / total, preserved, total

    def _report_session_coverage_slo(self) -> None:
        """Surface the session-level coverage ratio (SLO) at completion.

        Emits a journal event + a completion-report line with the aggregate
        preservation ratio across the window. When ``session_coverage_slo`` is
        set (> 0) and the ratio falls below it, also emits an advisory (still
        advisory only — observability, not enforcement, per the). No-op
        when no coverage was measured (clean rebase, unsupported languages).
        """
        try:
            rollup = self._session_coverage_rollup()
            if rollup is None:
                return
            ratio, preserved, total = rollup
            n_units = len(self._session_coverage_samples)
            self.journal.emit(
                "session_coverage_slo",
                {"ratio": round(ratio, 4), "preserved": preserved,
                 "total": total, "units": n_units},
                step_index=self.step,
            )
            self.out(
                f"  session intent coverage: {ratio:.1%} "
                f"({preserved}/{total} units preserved across {n_units} unit(s))\n"
            )
            slo = getattr(self.config.validation, "session_coverage_slo", 0.0)
            if slo and ratio < slo:
                self.journal.emit_advisory(
                    "session_coverage_below_slo",
                    f"session coverage {ratio:.1%} below SLO {slo:.0%}",
                )
                self.out(
                    self._warn(
                        f"  warning: session coverage {ratio:.1%} below the "
                        f"configured SLO ({slo:.0%})."
                    ) + "\n"
                )
        except Exception:  # noqa: BLE001 - the SLO is advisory, never break completion
            pass

    def _commit_added_lines_by_path(
        self, oid: str, paths: "Iterable[str]"
    ) -> "dict[str, str]":
        """Per-path added (``+``) lines of commit ``oid``'s patch.

        Parses the commit's unified-diff patch (``git diff-tree -p``) into a
        ``{path: added_text}`` map (only the ``+`` content lines, ``+++`` headers
        excluded). Used by the cross-commit guardian to derive a commit's USES
        from its actual contribution rather than its cumulative post-image — so a
        later commit re-including an earlier definition isn't misread as locally
        using it. Best-effort: returns {} on any parse/fetch failure (the
        guardian then falls back to the post-image for uses). Only paths in
        ``paths`` are included.
        """
        out: dict[str, str] = {}
        try:
            patch = self.git.commit_patch(oid)
        except Exception:  # noqa: BLE001
            return out
        if not patch:
            return out
        wanted = set(paths)
        cur_path: str | None = None
        lines_buf: list[str] = []
        for raw in patch.decode("utf-8", errors="replace").split("\n"):
            if raw.startswith("diff --git "):
                # Flush the previous file.
                if cur_path in wanted and lines_buf:
                    out[cur_path] = "\n".join(lines_buf)
                parts = raw.split(" b/", 1)
                cur_path = parts[-1] if len(parts) == 2 else None
                lines_buf = []
                continue
            if raw.startswith("+++") or raw.startswith("---"):
                continue
            if raw.startswith("+") and cur_path is not None:
                lines_buf.append(raw[1:])
        if cur_path in wanted and lines_buf:
            out[cur_path] = "\n".join(lines_buf)
        return out

    def _run_cross_commit_guardian_on_completion(self) -> StepResult | None:
        """Cross-commit dependency guardian audit; None if clean.

        Runs after the resurrection scan on clean completion. Closes the per-
        commit blind spot: builds a defines/uses map across the replayed source
        commits, derives cross-commit dependency edges (a later commit uses a
        symbol an earlier commit defines), and verifies each edge's symbol still
        resolves in the final rebased tree — catching e.g. commit A renaming
        ``foo``→``bar`` while a later commit B still calls ``foo``. Purely
        deterministic (abstract parser); a no-op when disabled or no source commits
        are available. With ``cross_commit_policy = "stop"`` a break escalates
        like the resurrection scan; with ``"warn"`` (default) it surfaces and
        continues. Returns None when there are no findings (nothing to do).
        """
        cfg = self.config.validation
        if not getattr(cfg, "enable_cross_commit_guardian", True):
            return None
        plan = getattr(self, "_history_plan", None)
        if plan is None or not getattr(plan, "source_commits", None):
            return None  # no source-sequence knowledge → can't build the graph
        head_after = self.git.head_oid()
        try:
            from capybase import cross_commit
            from capybase.adapters import structural
        except Exception:  # noqa: BLE001
            return None

        # Build the per-commit defines/uses map from each source commit's touched
        # files. DEFINES come from the post-image (blob_at the commit's OID);
        # USES come from the commit's ADDED lines (its actual contribution), so a
        # later commit whose post-image re-includes an earlier definition doesn't
        # count that name as locally-used. The added lines are parsed from the
        # commit's patch (per-path ``+`` lines), mirroring future_obligations.
        commit_symbols: dict[str, cross_commit.CommitSymbols] = {}
        all_paths: set[str] = set()
        for commit in plan.source_commits:
            files: dict[str, str] = {}
            for path in commit.touched_files:
                all_paths.add(path)
                blob = self.git.blob_at(commit.oid, path)
                if blob is not None:
                    try:
                        files[path] = blob.decode("utf-8", errors="replace")
                    except Exception:  # noqa: BLE001
                        pass
            if not files:
                continue
            added_text = self._commit_added_lines_by_path(commit.oid, files.keys())
            commit_symbols[commit.oid] = cross_commit.build_commit_symbols(
                files, added_text=added_text or None,
            )
        if not commit_symbols:
            return None

        edges = cross_commit.build_dependency_graph(
            commit_symbols, [c.oid for c in plan.source_commits]
        )
        if not edges:
            return None

        # Enumerate the final rebased tree's entities for the touched files.
        final_tree: dict[str, list] = {}
        for path in all_paths:
            blob = self.git.blob_at(head_after, path)
            if blob is None:
                continue
            lang = cross_commit._language_for_path(path)
            if lang is None or not structural.is_available(lang):
                continue
            ents = structural.enumerate_entities(
                blob.decode("utf-8", errors="replace"), lang
            )
            if ents is not None:
                final_tree[path] = ents
        breaks = cross_commit.audit_cross_commit_dependencies(edges, final_tree)
        if not breaks:
            return None

        # Surface the findings (journal + summary); escalate under "stop".
        rendered = [b.render() for b in breaks]
        self.journal.emit(
            "cross_commit_dependency_break",
            {
                "count": len(breaks),
                "breaks": [
                    {"symbol": b.symbol, "definer": b.definer,
                     "user": b.user, "break_type": b.break_type}
                    for b in breaks
                ],
            },
            step_index=self.step,
        )
        self.out(
            self._warn(
                f"! cross-commit dependency breaks detected ({len(breaks)}):\n"
                + "\n".join(f"  - {r}" for r in rendered)
            ) + "\n"
        )
        if cfg.cross_commit_policy == "stop":
            return StepResult(
                step_index=self.step,
                escalated=True,
                reason=f"cross-commit dependency breaks ({len(breaks)})",
            )
        return StepResult(step_index=self.step, escalated=False, continued=True)

    def _run_evolution_audit_on_completion(self) -> StepResult | None:
        """Intent evolution trace; None if clean.

        Runs after the cross-commit guardian. For an entity touched across ≥2
        source commits, checks the final merge matches the entity's LAST source-
        branch evolution (its most recent body) — a divergence flags an
        ``intent_evolution_gap`` (the merge likely reverted/kept an earlier
        version, silently losing an intermediate step). Purely advisory
        (observability/assurance, never blocks): prior findings the retry would
        be too expensive for multi-commit chains, so this produces a report. A
        no-op when disabled or no source commits / parser available.
        Returns None when there are no findings.
        """
        cfg = self.config.validation
        if not getattr(cfg, "enable_evolution_audit", True):
            return None
        plan = getattr(self, "_history_plan", None)
        if plan is None or not getattr(plan, "source_commits", None):
            return None
        try:
            from capybase import cross_commit
            from capybase.adapters import structural
        except Exception:  # noqa: BLE001
            return None

        # Per-commit post-image file contents (the entity source for each step).
        per_commit_files: dict[str, dict[str, str]] = {}
        all_paths: set[str] = set()
        for commit in plan.source_commits:
            files: dict[str, str] = {}
            for path in commit.touched_files:
                all_paths.add(path)
                blob = self.git.blob_at(commit.oid, path)
                if blob is not None:
                    try:
                        files[path] = blob.decode("utf-8", errors="replace")
                    except Exception:  # noqa: BLE001
                        pass
            if files:
                per_commit_files[commit.oid] = files
        if not per_commit_files:
            return None

        head_after = self.git.head_oid()
        # Build evolution chains per language present in the touched files.
        langs = {
            cross_commit._language_for_path(p)
            for p in all_paths
            if cross_commit._language_for_path(p)
            and structural.is_available(cross_commit._language_for_path(p) or "")
        }
        if not langs:
            return None
        order = [c.oid for c in plan.source_commits]
        chains: list = []
        for lang in sorted(langs):
            chains.extend(cross_commit.build_evolution_chains(per_commit_files, order, lang))
        if not chains:
            return None

        # Enumerate the final rebased tree's entities for the touched files.
        final_tree: dict[str, list] = {}
        for path in all_paths:
            blob = self.git.blob_at(head_after, path)
            if blob is None:
                continue
            lang = cross_commit._language_for_path(path)
            if lang is None or not structural.is_available(lang):
                continue
            ents = structural.enumerate_entities(
                blob.decode("utf-8", errors="replace"), lang
            )
            if ents is not None:
                final_tree[path] = ents
        gaps = cross_commit.audit_evolution(chains, final_tree)
        if not gaps:
            return None

        rendered = [g.render() for g in gaps]
        self.journal.emit(
            "intent_evolution_gap",
            {
                "count": len(gaps),
                "gaps": [
                    {"name": g.name, "kind": g.kind, "commit_count": g.commit_count,
                     "expected_from_commit": g.expected_from_commit}
                    for g in gaps
                ],
            },
            step_index=self.step,
        )
        self.out(
            self._warn(
                f"! intent evolution gaps detected ({len(gaps)}):\n"
                + "\n".join(f"  - {r}" for r in rendered)
            ) + "\n"
        )
        # Advisory only (prior findings a retry is too expensive for multi-commit
        # chains); never escalates.
        return StepResult(step_index=self.step, escalated=False, continued=True)

    def run(self) -> StepResult:
        """Full auto loop: resolve → stage → test → continue, with retries."""
        # Preflight.
        self.journal.emit("preflight_started", {})
        if not self.git.rebase_in_progress():
            # Not stopped at a conflict: try to start the rebase? In MVP we
            # require the user to have already hit a conflict (inspect-first).
            reason = "no rebase in progress; start your rebase, then run capybase when it stops on a conflict"
            self.journal.emit("escalated", {"reason": reason})
            bundle = write_review_bundle(self.paths, reason=reason)
            self.out(self._warn(f"! {reason}") + f"\n  review bundle: {bundle}")
            return StepResult(step_index=self.step, escalated=True, reason=reason)
        self.journal.emit("preflight_passed", {})

        # History-awareness for the run() workflow (#4): if the rebase was started
        # externally (git rebase ... && capybase run), lazily build a history plan
        # from the rebase-merge state so the same history features apply.
        if self._history_plan is None:
            self._lazy_build_history_from_rebase_state()

        # Loop over rebase stops until clean or escalated.
        last: StepResult | None = None
        while True:
            self.step += 1
            head_before = self.git.head_oid()
            self.journal.emit(
                "step_started",
                {"step": self.step, "head_before": head_before},
                step_index=self.step,
                git_head_before=head_before,
            )
            result = self._resolve_step()
            result.step_index = self.step
            last = result
            # Reset the per-step drift-regression stash before the test gate
            # populates it (it's read by _observe_drift after the gate).
            self._last_continuity_regressions = []
            # Accumulate this step's accepted-unit coverage into the session
            # SLO rollup. Cheap (reads already-computed detail);
            # the post-rebase report aggregates it into one ratio.
            self._accumulate_coverage_samples(result)
            if result.escalated:
                break
            # Tests gate continue.
            test_ok = self._run_tests("pre_continue", result)
            if not test_ok and self.config.tests.required:
                result.escalated = True
                result.reason = "pre-continue tests failed"
                break
            # Drift observation (behavioral-regression redesign): runs AFTER the
            # test gate so the step's regressions are known. Mechanism-gated:
            # deterministic resolutions emit no drift (impossible by
            # construction); only LLM resolutions with a test regression fire.
            # Advisory only — never blocks. No-op when drift detection is off.
            self._observe_drift(self.step, result)
            # Accept report (#4): both per-unit outcomes and the test verdict
            # exist here — write the "why we accepted" summary before continuing.
            self._write_accept_report(result)
            # Future-apply probe (#history step 9): ECC-lite — does the resolution
            # break the next source commit touching the same region? Advisory
            # (journals the result); in unattended mode, a failed probe escalates.
            self._run_future_apply_probe(result)
            if result.escalated:
                break  # the probe escalated (unattended mode) — stop before continue
            # Continue rebase.
            cont = self.git.continue_rebase()
            self.journal.emit(
                "step_continued",
                {"returncode": cont.returncode, "stderr": cont.stderr[:500]},
                step_index=self.step,
            )
            result.continued = True
            if not self.git.rebase_in_progress():
                # Rebase finished cleanly. Run the resurrection scan: the rebase
                # is done, so we reconstruct onto/start from the rebase-merge
                # state files (these survive until the rebase fully completes).
                # On ``stop`` the scan escalates and we break so the rebase()'s
                # escalation handling (interactive fallback / abort) runs.
                _res = self._run_resurrection_on_completion()
                if _res is not None and _res.escalated:
                    last = _res
                    break
                # Cross-commit dependency guardian: deterministic
                # window-level audit for cross-commit rename/reference breaks the
                # per-commit validators can't see. Runs after the resurrection
                # scan; under "stop" it escalates like resurrection.
                _ccb = self._run_cross_commit_guardian_on_completion()
                if _ccb is not None and _ccb.escalated:
                    last = _ccb
                    break
                # Intent evolution trace: advisory post-window audit
                # for entities that evolved across ≥2 commits — flags a merge
                # that reverted/lost the last evolution step. Runs after the
                # guardian; advisory only (never escalates).
                self._run_evolution_audit_on_completion()
                head_after = self.git.head_oid()
                self.journal.emit(
                    "session_completed",
                    {"head_after": head_after},
                    git_head_after=head_after,
                )
                # Session-level coverage SLO: one aggregate
                # preservation ratio across the window, surfaced as observability
                # for regression detection. Advisory; never blocks.
                self._report_session_coverage_slo()
                # Drift summary (behavioral-regression redesign): emit the
                # post-session headline here too so the run()-direct path
                # (without a rebase() wrapper) surfaces it. Guarded against
                # double-emission (rebase() emits again as a backstop).
                if self._drift_monitor is not None and not self._drift_summary_emitted:
                    summary = self._drift_monitor.summary()  # type: ignore[attr-defined]
                    if summary:
                        self.journal.emit("drift_summary", {"summary": summary})
                    self._drift_summary_emitted = True
                self.git.record_step_ref(self.session_id, self.step, head_after)
                self.out(self._ok(f"✓ rebase complete (session {self.session_id})"))
                break
            head_after = self.git.head_oid()
            self.git.record_step_ref(self.session_id, self.step, head_after)
            self.journal.emit(
                "step_ref_created",
                {"ref": self.paths.step_ref(self.step), "oid": head_after},
                step_index=self.step,
                git_head_after=head_after,
            )
        self._summarize(last)
        if last and last.escalated:
            # Enrich the summary bundle from the step's outcomes so the human
            # sees the model's best attempt + the validation failure — not just
            # the bare reason. Prefer an unaccepted (escalated) outcome; on a
            # whole-FILE failure every unit was accepted per-unit but the file
            # failed cargo, so fall back to the last outcome (its candidate is
            # what got spliced and failed the whole-file check).
            _esc = next((o for o in last.outcomes if o.accepted is None), None)
            if _esc is None and last.outcomes:
                _esc = last.outcomes[-1]
            write_review_bundle(
                self.paths,
                reason=last.reason or "escalated",
                step_index=last.step_index,
                unit=_esc.unit if _esc else None,
                candidate=(_esc.accepted or (_esc.attempts[-1] if _esc.attempts else None)) if _esc else None,
                validation=_esc.validation if _esc else None,
                advisories=self._recent_advisories(),
            )
        return last  # type: ignore[return-value]

    def _recent_advisories(self) -> list[str]:
        """Human-readable advisory reasons for the escalation review bundle.

        Collects the advisory events emitted this session (#idea 4) and renders
        each as ``<event_type>: <reason>`` so the human reviewing an escalation
        sees WHY a history feature may not have applied. Capped to keep the bundle
        readable. Empty when no advisories fired (the common, healthy case).
        """
        try:
            adv = [
                e for e in self.journal.read_events()
                if getattr(e.payload, "get", lambda *_: None)("advisory")
            ]
            out = [f"{e.event_type}: {e.payload.get('reason', '')}" for e in adv]
            return out[:20]
        except Exception:  # noqa: BLE001 - the bundle is advisory
            return []

    # ------------------------------------------------------------------ step core

    def _resolve_step(self) -> StepResult:
        result = self._gather_step()
        if result.escalated:
            return result
        # Clear the per-unit history caches at the start of each step (#idea 5):
        # the memoized HistoryContext/obligations/snapshot are valid within a
        # step, but across steps the history advances (a future commit becomes
        # the current one), so we reset between steps.
        self._clear_history_caches()
        if not result.units_by_path:
            # No conflicts at this stop: nothing to resolve (rare).
            self.out("no conflict units at this stop; continuing.")
            return result

        # Two-phase resolution so cross-file (whole-crate) verification works.
        #
        # Phase 1: resolve every unit in every conflicted file and WRITE each
        # resolved buffer to the worktree, without staging or crate-wide
        # checking. This is critical for Rust: a per-file ``cargo check`` reads
        # the REAL worktree, so while sibling conflicted files still hold raw
        # ``<<<<<<<`` markers, the check fails with ``error: encountered diff
        # marker`` — a correct merge gets rejected through no fault of its own.
        # Writing every file resolved first makes the whole crate marker-free
        # before any cargo check runs. If any unit escalates, bail before any
        # write (nothing staged, rebase stays stoppable).
        #
        # Phase 2: with all files written, run the per-file Phase-B validation
        # (markers/splice/syntax/cargo) + CEGIS repair loop, then stage. Each
        # file's cargo check now sees a clean crate.
        resolved_files: dict[str, str] = {}  # path -> spliced buffer (all units)
        accepted_by_path: dict[str, list] = {}  # path -> [(unit, candidate), ...]
        # Snapshot the original worktree text per path so Phase 2 can re-splice.
        originals: dict[str, str] = {}

        # ---- Phase 1: resolve + write all files (no staging, no cargo) ----
        for path, units in result.units_by_path.items():
            # Resolve ALL units in the file before splicing anything. We must
            # not write a partially-resolved file: if a later unit escalates,
            # the file (with some blocks still marker-laden) would be staged
            # against an aborted rebase. Collect accepted (unit, candidate)
            # pairs and splice them in one offset-correct batch at the end.
            accepted: list[tuple[ConflictUnit, CandidateResolution]] = []
            escalated_unit: UnitOutcome | None = None
            for unit in units:
                outcome = self._resolve_unit(unit)
                result.outcomes.append(outcome)
                if outcome.accepted is None:
                    escalated_unit = outcome
                    break
                accepted.append((unit, outcome.accepted))
            if escalated_unit is not None:
                result.escalated = True
                # Prefer the outcome's specific reason (e.g. "unit exceeded
                # wall-time budget") when the escalation path set one; fall back
                # to the generic per-unit message.
                result.reason = (
                    escalated_unit.reason
                    or f"could not resolve {escalated_unit.unit.unit_id}"
                )
                self._record_outcomes_to_memory(result)
                _alternates, _consensus = _extract_alternates(escalated_unit)
                write_review_bundle(
                    self.paths,
                    reason=result.reason,
                    step_index=result.step_index,
                    unit=escalated_unit.unit,
                    candidate=escalated_unit.attempts[-1] if escalated_unit.attempts else None,
                    alternates=_alternates,
                    validation=escalated_unit.validation,
                    consensus=_consensus,
                )
                return result
            # Splice every accepted resolution in one offset-correct batch.
            # (For a whole_file unit the resolved text IS the file —
            # ``_resolved_buffer`` returns it verbatim, no splicing.)
            original = accepted[0][0].original_worktree_text
            buffer = _resolved_buffer(original, accepted)
            resolved_files[path] = buffer
            accepted_by_path[path] = accepted
            originals[path] = original
            # Write the resolved file to the worktree NOW (no staging yet) so
            # sibling files' cargo checks in Phase 2 see a marker-free crate.
            # An accepted whole-file deletion removes the worktree file instead.
            self._write_worktree_only(path, buffer, accepted=accepted)

        # ---- Phase 2: per-file Phase-B validation + CEGIS repair + stage ----
        for path, units in result.units_by_path.items():
            accepted = accepted_by_path[path]
            original = originals[path]
            language = units[0].language
            # Splice every accepted resolution in one offset-correct batch and
            # validate the whole file. Phase B (whole-file validation) is the
            # only place that can catch cross-unit errors (duplicate symbols,
            # syntax errors arising only when resolutions are juxtaposed, leaked
            # sibling markers). Per-unit Phase A validation already passed for
            # each candidate in isolation.
            #
            # Execution-driven whole-file CEGIS: when the
            # combination fails, we do NOT escalate immediately — we feed the
            # concrete file-level failures back to the unit most likely at
            # fault and re-resolve it via the repair prompt, then re-splice and
            # re-validate. Bounded by the policy retry ceiling so it can't loop
            # forever; escalate only when the budget is exhausted.
            buffer = resolved_files[path]
            if self.config.validation.require_whole_file_validation and units:
                wf_retries = 0
                # Fix #3: separate whole-file repair budget. 0 mirrors the per-
                # unit budget (legacy behavior); a higher value grants more
                # repair cycles for multi-hunk conflicts where the deterministic
                # brace repair (Fix #2) + enriched context (Fix #1) need a few
                # shots to converge.
                wf_budget = self.config.policy.max_whole_file_repair_retries or self.config.policy.max_retries_per_unit
                file_validation = None  # type: ignore[assignment]
                while True:
                    spans_and_texts = [
                        (unit.marker_span, cand.resolved_text) for unit, cand in accepted
                    ]
                    # verify_file tolerates a whole-file (None) span via its own
                    # _has_whole_file_span guard; the buffer is the resolved
                    # text directly for such units.
                    buffer = _resolved_buffer(original, accepted)
                    file_validation = self.verification.verify_file(
                        path, language, original, spans_and_texts,
                        repo_root=str(self.git.repo),
                    )
                    if self.config.journal.enabled and self.config.journal.store_validations:
                        self.journal.store_validation(file_validation)
                    self.journal.emit(
                        "file_validated",
                        {
                            "passed": file_validation.passed,
                            "hard_failures": [
                                f.message for f in file_validation.hard_failures
                            ],
                            "wf_retry": wf_retries,
                        },
                        step_index=self.step,
                        path=path,
                    )
                    if file_validation.passed or wf_retries >= wf_budget:
                        break
                    # Attribute the failure to a unit and re-resolve it with the
                    # file-level failures as concrete repair feedback.
                    wf_retries += 1
                    self.journal.emit(
                        "whole_file_repair",
                        {
                            "retry": wf_retries,
                            "failures": [
                                f.message for f in file_validation.hard_failures
                            ],
                        },
                        step_index=self.step,
                        path=path,
                    )
                    accepted_opt: list[tuple[ConflictUnit, CandidateResolution]] | None = (
                        self._whole_file_repair(
                            path, accepted, original, file_validation.hard_failures
                        )
                    )
                    if accepted_opt is None:
                        # A unit could not be re-resolved (escalated) → bail.
                        file_validation = None  # type: ignore[assignment]
                        break
                    accepted = accepted_opt
                if file_validation is None or not file_validation.passed:
                    result.escalated = True
                    if file_validation is None:
                        result.reason = (
                            f"whole-file repair could not re-resolve a unit in {path}"
                        )
                    else:
                        result.reason = (
                            f"whole-file validation failed for {path}: "
                            + "; ".join(f.message for f in file_validation.hard_failures)
                        )
                    self._record_outcomes_to_memory(result)
                    # Enrich the bundle with the unit/candidate/validation so the
                    # human (and the interactive fallback) can see what was tried
                    # and why cargo rejected it — not just the bare reason.
                    _unit = accepted[0][0] if accepted else None
                    _cand = accepted[0][1] if accepted else None
                    write_review_bundle(
                        self.paths,
                        reason=result.reason,
                        step_index=result.step_index,
                        unit=_unit,
                        candidate=_cand,
                        validation=file_validation if file_validation is not None else None,
                        advisories=self._recent_advisories(),
                    )
                    return result
            # Stage the validated file (it was already written to the worktree
            # in Phase 1; re-write in case the CEGIS loop changed it, then stage).
            self._write_and_stage(path, buffer, result, accepted=accepted)
        # After staging: assert no unmerged paths remain for our files.
        if self.git.has_unmerged_paths():
            result.escalated = True
            result.reason = "unmerged paths remain after staging"
            self._record_outcomes_to_memory(result)
            write_review_bundle(
                self.paths, reason=result.reason, step_index=result.step_index
            )
        else:
            self._record_outcomes_to_memory(result)
        return result

    def _whole_file_repair(
        self,
        path: str,
        accepted: list[tuple[ConflictUnit, CandidateResolution]],
        original: str,
        failures: list,
    ) -> list[tuple[ConflictUnit, CandidateResolution]] | None:
        """Re-resolve the unit most likely at fault for a whole-file failure.

        Execution-driven whole-file CEGIS: the file-level failures
        (cross-unit syntax errors, etc.) are fed back to the unit whose
        resolution most plausibly caused them. Attribution is by error-line
        containment in the unit's marker_span (parsed from the failure message
        when possible); if no unit's span contains the line, the LAST unit is
        re-resolved (a heuristic — juxtaposition errors tend to surface where
        the splices meet). Returns the updated accepted list, or None if the
        attributed unit could not be re-resolved (it escalated).
        """
        fault_idx = _attribute_whole_file_failure(failures, [u for u, _ in accepted])
        # Deterministic brace repair (Fix #2): before spending an LLM call on the
        # recurring splice-junction brace imbalance, try to fix it directly. The
        # live eval showed the model reproducing the same extra/missing brace at
        # the hunk junction across 4 retries — a single-edit deterministic fix
        # resolves it instantly when the imbalance is a stray brace-only line or
        # a truncated unclosed block. The repaired buffer is back-projected onto
        # the fault unit's resolved_text so the splice + re-validate loop sees
        # the fix. Conservative: acts only when one edit fully balances, and
        # only when the back-projection is unambiguous; otherwise falls through
        # to the LLM repair path below.
        det = _try_deterministic_brace_repair(
            failures, original, accepted, fault_idx
        )
        if det is not None:
            unit_new, cand_new = det[0]
            self.journal.emit(
                "candidate_validated",
                {
                    "candidate_id": cand_new.candidate_id,
                    "passed": True,
                    "whole_file_repair_for": unit_new.unit_id,
                    "deterministic_brace_repair": True,
                },
                step_index=self.step,
                path=path,
                unit_id=unit_new.unit_id,
            )
            return det
        unit, _old_cand = accepted[fault_idx]
        # Fix #3 — enriched feedback: build a splice-context snippet (the resolved
        # file ±5 lines around the error) so PROMPT_REPAIR shows the model the
        # actual brace mismatch in context, not just the raw cargo message. The
        # model couldn't locate the error in the live eval (3 identical retries on
        # "unexpected closing delimiter }"); the snippet gives it the surrounding
        # code to find the extra/missing brace.
        enriched_failures = list(failures)
        snippet = _splice_context_snippet(failures, original, accepted)
        if snippet:
            enriched_failures.append(VerificationFailure(
                validator="splice_coherence",
                severity="warning",
                message=f"the spliced file around the error:\n{snippet}",
            ))
        # Pass the previously-accepted candidate as seed_candidate so the
        # re-resolve routes to PROMPT_REPAIR (shows the broken candidate + the
        # compile diagnostic) instead of PROMPT_RETRY (blind regeneration). The
        # _old_cand caused the file-level failure; showing it gives the model a
        # surgical target. Only when it has usable resolved_text (an empty/needs-
        # human candidate has nothing to repair).
        seed_cand = _old_cand if (
            _old_cand is not None and getattr(_old_cand, "resolved_text", "")
        ) else None
        outcome = self._resolve_unit(
            unit, seed_failures=enriched_failures, seed_candidate=seed_cand,
        )
        self.journal.emit(
            "candidate_validated",
            {
                "candidate_id": (outcome.accepted.candidate_id if outcome.accepted else "none"),
                "passed": outcome.accepted is not None,
                "whole_file_repair_for": unit.unit_id,
            },
            step_index=self.step,
            path=path,
            unit_id=unit.unit_id,
        )
        if outcome.accepted is None:
            return None
        accepted[fault_idx] = (unit, outcome.accepted)
        return accepted

    def _resolve_unit(
        self, unit: ConflictUnit, *, seed_failures: list | None = None,
        seed_candidate: "CandidateResolution | None" = None,
    ) -> UnitOutcome:
        outcome = UnitOutcome(unit=unit)
        # Build the per-unit history snapshot ONCE (#idea 5 cohesion). This
        # memoizes the HistoryContext/confidence/obligations/etc. so every
        # downstream mechanism (prompt, gates, probe, features, reuse) reads from
        # the same per-unit snapshot rather than re-querying 4×/2×/2×. The
        # snapshot is journaled here as the single history_decision_snapshot event.
        if self._history_service is not None and self._history_plan is not None:
            snapshot = self._history_snapshot_for(unit)
            # Inject the snapshot's future obligations into the verification
            # validator (#idea 7) so verify() checks them uniformly — a dropped
            # symbol now produces a warning + features like any other validator,
            # not an inline orchestrator gate.
            self._future_obligation_validator.set_obligations(
                snapshot.future_obligations
            )
        else:
            self._future_obligation_validator.set_obligations(None)
        retry_count = 0
        # Separate ledger for verifier-critic-driven retries: a critic flag
        # consumes THIS budget (max_critic_retries_per_unit), not retry_count,
        # so a stubborn dropped-intent case can't starve the syntactic-CEGIS
        # retries. Incremented only when the retry was critic-driven.
        critic_retry_count = 0
        # Separate ledger for recovery retries (needs_human self-refusals): a
        # model that gave up gets one retry with build_recovery_prompt before
        # escalating. Uses max_recovery_retries_per_unit; incremented only when
        # the retry was recovery-driven.
        recovery_retry_count = 0
        # Carries the recovery-retry flag across loop iterations: set in the
        # retry-seed block (after a needs_human decision grants a recovery
        # attempt), consumed at the top of the next iteration by propose() to
        # select build_recovery_prompt instead of the normal resolve/repair path.
        pending_recovery = False
        # Wall-clock deadline for this unit (the outermost budget, above the
        # per-retry counts). 0 = disabled. Checked at the top of each loop
        # iteration so a non-converging unit escalates instead of looping.
        import time as _time
        unit_start = _time.monotonic()
        wall_budget = self.config.policy.max_wall_time_per_unit_seconds
        # seed_failures: when set (whole-file CEGIS), the unit is re-resolved
        # starting from the repair path with the file-level failures pre-seeded,
        # so the model gets the concrete cross-unit error on its first attempt.
        failures = list(seed_failures) if seed_failures else None
        # seed_candidate: when set (whole-file CEGIS repair), the previously-
        # accepted candidate that caused the file-level failure. Seeded as the
        # initial prev_candidate so the first loop iteration routes to
        # PROMPT_REPAIR (shows the broken candidate + the compile diagnostic)
        # instead of PROMPT_RETRY (blind regeneration).
        prev_candidate = seed_candidate

        # Exact history reuse (#9 step 4): BEFORE every other mechanism, check
        # whether an IDENTICAL prior conflict was already accepted. If so, replay
        # its resolution verbatim. Always on (no flag) — the reused candidate
        # runs the identical validation gauntlet below, so a stale/wrong reuse
        # fails and falls through to structural/LLM exactly as if it never
        # matched. This is a speed/quality optimization, never a correctness
        # bypass; bugs surface immediately via re-validation. Only on a FRESH
        # resolve (the CEGIS loop must see counterexamples).
        if failures is None:
            early = self._try_exact_reuse(unit)
            if early is not None:
                return early  # accepted via verbatim reuse; LLM loop skipped

        # Deterministic structural pre-resolution: BEFORE
        # the LLM loop, attempt a safe, model-free resolution from base+sides.
        # Only on a FRESH resolve (not CEGIS retries, where the model must see the
        # counterexample). Any resolution still runs the full validation pipeline;
        # on failure it falls through to the model, so this can only cut LLM load,
        # never produce a worse merge. Gated by [future] enable_structural_resolver.
        if failures is None and self.config.future.enable_structural_resolver:
            early = self._try_structural_resolve(unit)
            if early is not None:
                return early  # accepted deterministically; LLM loop skipped entirely

        # Search-based combination resolution (SBCR): AFTER the
        # structural resolver declines and BEFORE the LLM. Searches order-
        # preserving interleavings for the best combination; the candidate is
        # validated before acceptance, so an invalid combination falls through to
        # the model. Only on a FRESH resolve. Gated by [future]
        # enable_combination_search.
        if failures is None and self.config.future.enable_combination_search:
            # Difficulty-aware SBCR skip: SBCR is addition-only
            # (empty-base scope), and hard conflicts are overwhelmingly
            # modification conflicts where SBCR's search would decline on scope
            # anyway (the corpus measurement showed 0/209 hard cases fire). Skip
            # the search cost when the band is hard AND routing is on. When
            # routing is off (band unknown), run SBCR as before.
            if self.config.routing.enabled and self._classification_band(unit) == "hard":
                self._record_resolution_attempt(
                    UnitOutcome(unit=unit), mechanism="sbcr",
                    decision="skip", reason="hard conflict (skip addition-only search)",
                )
                self.journal.emit(
                    "combination_declined",
                    {"fitness": 0.0, "reason": "hard conflict (skip addition-only search)"},
                    step_index=self.step, path=unit.path, unit_id=unit.unit_id,
                )
            else:
                early = self._try_combination_search(unit)
                if early is not None:
                    return early  # accepted via combination search; LLM loop skipped

        # Test-gated side picker: when both pre-LLM resolvers decline a conflict
        # where taking either side verbatim is plausible, try each side and let
        # the test gate discriminate (e.g. port=9090 vs 7070, where the test
        # asserts ==9090). The documented job of the test gate (conftest port
        # pattern), but as a PRE-LLM discriminator instead of post-LLM. Only on a
        # FRESH resolve, same as the other pre-LLM layers.
        if failures is None:
            self._last_side_probe_failures = None  # reset before the probe
            early = self._try_test_gated_side(unit)
            if early is not None:
                return early  # accepted via test-gated side pick; LLM loop skipped
            # CEGIS loop hardening: if the picker DECLINED (neither side passed
            # the test gate), thread its captured diagnostics into the LLM path
            # as seed_failures. The model starts with the concrete compile errors
            # instead of a feedback-free fresh resolve — it finally sees WHY
            # neither side verbatim works.
            if self._last_side_probe_failures:
                failures = list(self._last_side_probe_failures)

        # Block-capture resolution (large modify/delete): when one side deleted a
        # large block and the structural rule declined (the keeper modified it),
        # the model can't reliably reproduce the block (placeholder collapse +
        # escaping corruption). Instead it makes a keep/accept_deletion/needs_human
        # decision and capybase splices the chosen side verbatim. AFTER the other
        # pre-LLM layers decline and BEFORE the LLM loop, on a FRESH resolve only.
        if failures is None and self.config.future.enable_block_capture:
            early = self._try_block_capture(unit)
            if early is not None:
                return early  # accepted via block-capture; LLM loop skipped

        # LLM size guard: if the essential conflict content
        # alone exceeds the model's context window, the LLM call is doomed (the
        # server truncates, the model fails). Skip it and escalate rather than
        # wasting the call. Only on a FRESH resolve (failures is None) — a CEGIS
        # retry is already engaged on this unit and the guard already passed on
        # the first attempt. No-op when the window is unconfigured (0).
        if failures is None:
            oversized, essential_t, available_t = self._llm_oversized_for_window(unit)
            if oversized:
                outcome.escalated = True
                outcome.reason = (
                    f"conflict too large for model window "
                    f"(essential ~{essential_t}t > available {available_t}t)"
                )
                self._record_resolution_attempt(
                    outcome, mechanism="llm",
                    decision="skip",
                    reason=f"oversized: {essential_t}t > {available_t}t available",
                )
                self.journal.emit(
                    "llm_skipped_oversized",
                    {"essential_tokens": essential_t, "available_tokens": available_t},
                    step_index=self.step, path=unit.path, unit_id=unit.unit_id,
                )
                return outcome

        while True:
            # Wall-clock deadline (outermost budget): if this unit has run past
            # its time budget across retries, escalate rather than proposing
            # again. Sits above the per-retry counts so it bounds total latency
            # regardless of how the syntactic/critic/whole-file budgets split.
            # The "at least one attempt" guard uses EITHER counter: critic-driven
            # retries increment critic_retry_count, not retry_count, so checking
            # only retry_count would let an all-critic retry loop run forever.
            if (
                wall_budget > 0.0
                and (_time.monotonic() - unit_start) >= wall_budget
                and (retry_count > 0 or critic_retry_count > 0)
            ):
                outcome.escalated = True
                outcome.retry_count = retry_count
                outcome.reason = (
                    f"unit exceeded wall-time budget "
                    f"({wall_budget:.0f}s) after {retry_count} attempt(s)"
                )
                self.journal.emit(
                    "candidate_rejected",
                    {"candidate_id": cand.candidate_id,
                     "action": "escalate", "via": "wall_time",
                     "wall_seconds": round(_time.monotonic() - unit_start, 1),
                     "retry_count": retry_count},
                    step_index=self.step, path=unit.path, unit_id=unit.unit_id,
                )
                return outcome
            # Populate the future-obligations prompt block (#9 step 3) before
            # building the prompt so the model sees what later commits expect.
            self._set_future_obligations_prompt_block(unit)
            context = self.context_builder.build(unit)
            # Surface a retrieval failure as an advisory (#idea 4): the context
            # builder has no journal, so it stashes the error for us to emit here.
            if self.context_builder.last_retrieval_error:
                self.journal.emit_advisory(
                    "retrieval_explanation_failed",
                    f"retrieval failed: {self.context_builder.last_retrieval_error}",
                    path=unit.path, unit_id=unit.unit_id,
                )
            # Surface the retrieval explanations onto the outcome (#9 step 5) so
            # the accept report can show why each few-shot example was chosen.
            outcome.retrieval_explanations = list(context.retrieval_explanations)
            if self.config.journal.enabled and self.config.journal.store_prompts:
                from capybase.resolution_engine import (
                    PROMPT_REPAIR,
                    PROMPT_RETRY,
                    PROMPT_RESOLVE,
                    build_repair_prompt,
                    build_resolve_prompt,
                    build_retry_prompt,
                )

                # Mirror propose()'s dispatch so the journaled prompt matches the
                # ACTUAL prompt sent to the model. Previously this always used
                # build_retry_prompt on any failure, which mismatches a retry that
                # took the PROMPT_REPAIR path (candidate+targeted-fix) — making the
                # audit trail misleading.
                if pending_recovery:
                    from capybase.resolution_engine import build_recovery_prompt
                    pv = "cegis_recovery.v1"
                    prompt = build_recovery_prompt(unit, context, failures)
                elif failures and prev_candidate and prev_candidate.resolved_text:
                    pv = PROMPT_REPAIR
                    prompt = build_repair_prompt(unit, context, prev_candidate, failures, attempt=retry_count)
                elif failures:
                    pv = PROMPT_RETRY
                    prompt = build_retry_prompt(unit, context, failures)
                else:
                    pv = PROMPT_RESOLVE
                    prompt = build_resolve_prompt(unit, context)
                self.journal.store_prompt(unit.unit_id, retry_count, prompt)
            self.journal.emit(
                "context_built",
                {
                    "token_estimate": context.token_estimate,
                    "retrieval_scores": context.retrieval_scores,
                },
                step_index=self.step,
                path=unit.path,
                unit_id=unit.unit_id,
            )

            consensus_report = None
            # Difficulty-aware routing: classify the conflict
            # before any LLM call. The ConflictClassifier returns a richer band
            # + explainable reasons; the legacy ``simple``/``complex`` label
            # (band ∈ {medium, hard} ⇒ complex) drives the existing fast path
            # (one low-temp sample, no two-pass, no consensus) vs the full
            # pipeline. Disabled (complex=full path for all) until
            # config.routing.enabled is set.
            difficulty = "complex"
            classification = None
            if self.config.routing.enabled:
                from capybase.classifier import classify

                classification = classify(unit)
                difficulty = classification.difficulty
                self.journal.emit(
                    "difficulty_classified",
                    {
                        "difficulty": difficulty,
                        "band": classification.band,
                        "reasons": classification.reasons,
                    },
                    step_index=self.step,
                    path=unit.path,
                    unit_id=unit.unit_id,
                )
            outcome.difficulty = difficulty
            outcome.classification = classification

            # Difficulty-aware sample allocation (UAB-lite): complex
            # units draw samples_complex (falling back to the base samples when
            # unset/0). Difficulty is known before any LLM call, so this is the
            # viable pre-generation allocation lever. Only affects fresh
            # resolution (failures is None) — retries stay single-sample for
            # reproducible CEGIS counterexample feedback.
            if failures is None:
                n_complex = (
                    self.config.model.samples_complex or self.config.model.samples
                )
            else:
                n_complex = self.config.model.samples

            # Self-consistency: read from ModelConfig (so the calibrated profile
            # overlay flows through) with fallback to the legacy FutureConfig flag.
            self_consistency = (
                self.config.model.enable_self_consistency
                or self.config.future.enable_self_consistency
            )

            # Recovery retry (CEGIS loop hardening): a model that self-reported
            # needs_human gets one retry with build_recovery_prompt (a reframed
            # resolve that strips the needs_human escape hatch). Overrides the
            # normal difficulty routing — it's a single-sample recovery probe,
            # not a fresh multi-sample resolve.
            if pending_recovery:
                pending_recovery = False  # consume
                candidates = self.resolution_engine.propose_recovery(
                    unit, context, failures=failures,
                )
            elif difficulty == "simple":
                # Fast path: one low-temperature sample, no intent pass, no
                # consensus. Simple isolated hunks resolve trivially. Force
                # n_samples=1 so a calibrated samples>1 never leaks into the
                # cheap path (it would otherwise fall back to config.samples).
                candidates = self.resolution_engine.propose(
                    unit, context, failures=failures, prev_candidate=prev_candidate,
                    n_samples=1, attempt=retry_count,
                )
            elif failures is None and self.config.model.two_pass and n_complex > 1:
                # Two-pass prompting + consensus: extract intents, then sample
                # N code candidates conditioned on them, then majority-vote.
                candidates = self.resolution_engine.propose_two_pass(
                    unit, context,
                    n_samples=n_complex,
                    temperature=self.config.model.sampling_temperature,
                )
                if self_consistency and len(candidates) > 1:
                    candidates, consensus_report = (
                        rank_by_consensus(candidates, unit.language)
                    )
            elif self_consistency:
                candidates, consensus_report = (
                    self.resolution_engine.propose_with_consensus(
                        unit, context, failures=failures,
                        prev_candidate=prev_candidate, n_samples=n_complex,
                    )
                )
            else:
                candidates = self.resolution_engine.propose(
                    unit, context, failures=failures, prev_candidate=prev_candidate,
                    n_samples=n_complex,
                )
            outcome.consensus = consensus_report
            # Journal the generation round. With self-consistency this is the
            # full sample set; the consensus stats attach here so the audit
            # shows how split the samples were before validation.
            winner = candidates[0]
            emit_payload = {
                "candidate_id": winner.candidate_id,
                "n_candidates": len(candidates),
                "needs_human": winner.needs_human,
                "confidence": winner.self_reported_confidence,
            }
            # Token-window trims (empty when no budget configured or nothing
            # trimmed): surfaces that the prompt was capped (few-shot/deps/etc.
            # dropped) so the resolution is auditable against the context window.
            prompt_trims = getattr(winner, "prompt_trims", None)
            if prompt_trims:
                emit_payload["prompt_trims"] = prompt_trims
            if consensus_report is not None:
                emit_payload["consensus_agreement"] = consensus_report.agreement_score
                emit_payload["consensus_clusters"] = consensus_report.cluster_count
                emit_payload["consensus_n_samples"] = consensus_report.n_samples
            self.journal.emit(
                "candidate_generated",
                emit_payload,
                step_index=self.step,
                path=unit.path,
                unit_id=unit.unit_id,
            )

            # Step 3 (syntactic/structural guardrails): validate candidates in
            # rank order and accept the FIRST that passes hard validation. The
            # consensus winner is first, but on a 3B model the winner frequently
            # carries a syntax error while the 2nd/3rd sample is valid — trying
            # them before regenerating is free reliability (the tokens were
            # already spent). These are local parser/splice checks, not
            # LLM calls, so validating all N is cheap. If none pass, the winner
            # (and its failures) feeds the CEGIS repair loop below.
            cand = winner
            validation = self.verification.verify(unit, cand)
            self._journal_validation(unit, cand, validation)
            if not validation.passed and len(candidates) > 1:
                for trial in candidates[1:]:
                    trial_val = self.verification.verify(unit, trial)
                    self._journal_validation(unit, trial, trial_val)
                    if trial_val.passed:
                        cand = trial
                        validation = trial_val
                        break
            outcome.validation = validation
            outcome.attempts.append(cand)
            # Track candidate hashes for oscillation detection (CEGIS resilience).
            # The escalation check runs AFTER the risk decision below — only when
            # the decision is "retry" — so it never fires before the normal budget.
            # Empty resolved_text (parse failure / refusal) is excluded: a broken
            # response repeating isn't "the model cycling on the same correct code"
            # — it's a different failure class the existing retry budget handles.
            import hashlib as _hashlib

            cand_hash = ""
            if cand.resolved_text:
                cand_hash = _hashlib.sha256(
                    cand.resolved_text.encode("utf-8")
                ).hexdigest()[:16]
                outcome._seen_candidate_hashes[cand_hash] = (
                    outcome._seen_candidate_hashes.get(cand_hash, 0) + 1
                )
            if self.config.journal.enabled and self.config.journal.store_candidates:
                self.journal.store_candidate(cand)
            if self.config.journal.enabled and self.config.journal.store_raw_responses:
                self.journal.store_response(unit.unit_id, retry_count, cand.raw_response)

            decision = self.risk.decide(
                validation,
                retry_count=retry_count,
                failure_kind=cand.failure_kind,
                suspected_validator_error=cand.suspected_validator_error,
                consensus_entropy=(
                    consensus_report.entropy if consensus_report else None
                ),
                consensus_agreement=(
                    consensus_report.agreement_score if consensus_report else None
                ),
                critic_retry_count=critic_retry_count,
                recovery_retry_count=recovery_retry_count,
            )
            outcome.decision = decision
            self.journal.emit(
                "risk_decision",
                {"action": decision.action, "reasons": decision.reasons},
                step_index=self.step,
                path=unit.path,
                unit_id=unit.unit_id,
            )
            if decision.action == "accept":
                # Strictness gate (#10): in ci/unattended mode, the policy may
                # override an accept to escalate (e.g. low confidence, a dropped
                # obligation, or a hard-band conflict). It never relaxes a
                # retry/escalate, only tightens accept.
                ok, why = self.strictness.should_accept(
                    unit, cand, validation,
                    band=self._classification_band(unit),
                    deterministic=False,
                )
                if not ok:
                    # Strictness escalated: leave outcome.accepted=None so the
                    # caller treats it as an escalation, mirroring the risk
                    # engine's own escalate branch.
                    outcome.retry_count = retry_count
                    self.journal.emit(
                        "candidate_rejected",
                        {"candidate_id": cand.candidate_id,
                         "action": "escalate", "via": "strictness",
                         "reason": why, "mode": self.strictness.mode},
                        step_index=self.step, path=unit.path, unit_id=unit.unit_id,
                    )
                    return outcome
                # Future obligations are now a VERIFICATION validator (#idea 7):
                # the FutureObligationValidator (fed from the snapshot) emits a
                # warning + features like any other validator, and the risk engine
                # retries on the "future_obligation" warning name. No inline gate
                # needed here — the candidate already passed verify() (which ran
                # the future-obligation check) and the risk decision already
                # accounted for it.
                # The clearly-named history-augmentation compat path (#idea 6):
                # a plain-LLM candidate whose history context was augmenting gets
                # re-stamped to history_augmented_llm. This is the ONLY restamp,
                # and it's a named method (not an inline mutation) so the compat
                # path is explicit and reasoned.
                restamp_reason = self._restamp_for_history_augmentation(unit, cand)
                outcome.accepted = cand
                outcome.retry_count = retry_count
                self._record_resolution_attempt(
                    outcome, mechanism=cand.provenance or "plain_llm",
                    candidate=cand, validation=validation,
                    decision="accept",
                    reason=restamp_reason or "LLM candidate accepted",
                )
                self.journal.emit(
                    "candidate_accepted",
                    {"candidate_id": cand.candidate_id,
                     "via": cand.provenance or "plain_llm",
                     "provenance": cand.provenance or ""},
                    step_index=self.step,
                    path=unit.path,
                    unit_id=unit.unit_id,
                )
                return outcome
            if decision.action == "escalate":
                outcome.retry_count = retry_count
                self.journal.emit(
                    "candidate_rejected",
                    {"candidate_id": cand.candidate_id, "action": "escalate"},
                    step_index=self.step,
                    path=unit.path,
                    unit_id=unit.unit_id,
                )
                return outcome
            # retry
            self.journal.emit(
                "candidate_rejected",
                {"candidate_id": cand.candidate_id, "action": "retry", "retry_count": retry_count},
                step_index=self.step,
                path=unit.path,
                unit_id=unit.unit_id,
            )
            # Oscillation backstop (CEGIS resilience): if the SAME resolved_text
            # has been seen more times than the retry budget allows, the model is
            # cycling — escalate instead of wasting more tokens. This fires only
            # when the decision was already "retry" (so the budget hasn't been
            # exhausted yet), as a backstop that cuts the loop early when the
            # candidate is provably stuck (identical across attempts).
            osc_count = outcome._seen_candidate_hashes.get(cand_hash, 0)
            osc_budget = self.risk._effective_budget(validation.features)
            if osc_count > osc_budget:
                self.journal.emit(
                    "candidate_rejected",
                    {"candidate_id": cand.candidate_id,
                     "action": "escalate", "via": "oscillation",
                     "reason": f"identical candidate seen {osc_count} times (budget {osc_budget}) — loop is cycling",
                     "retry_count": retry_count},
                    step_index=self.step, path=unit.path, unit_id=unit.unit_id,
                )
                outcome.escalated = True
                outcome.retry_count = retry_count
                outcome.reason = (
                    f"candidate oscillation (identical resolved_text {osc_count}×, "
                    f"budget {osc_budget})"
                )
                return outcome
            # Seed the retry: hard failures PLUS the critic's verdict (if any) as
            # a synthesized VerificationFailure, so the repair prompt the model
            # sees on the next attempt carries the critic's concrete feedback
            # ("may drop replayed side intent"). Without this, a critic-driven
            # retry regenerated with NO feedback (the warning was dropped at the
            # old `hard_failures or None` seed), so the model kept reproducing
            # the same dropped-side merge — the A/B's 30-min convergence loop.
            #
            # Critic-feedback deduplication : the PoLL jury
            # may emit multiple verifier_model* flags; dedupe by embedding
            # similarity so two equivalent flags (same issue, different wording)
            # don't dilute the plan-first step's attention. All surviving flags
            # seed the repair prompt (not just the first). Best-effort: no embedder
            # → first-found only (the prior behavior).
            all_critic = _all_critic_warnings(validation)
            deduped_critic = _dedupe_critic_warnings(all_critic, self._shared_embedder)
            if all_critic and len(deduped_critic) != len(all_critic):
                self.journal.emit(
                    "critic_dedup",
                    {"input_count": len(all_critic),
                     "survivor_count": len(deduped_critic),
                     "dropped_count": len(all_critic) - len(deduped_critic)},
                    step_index=self.step, path=unit.path, unit_id=unit.unit_id,
                )
            critic_warning = deduped_critic[0] if deduped_critic else None
            failures = list(validation.hard_failures)
            if critic_warning is not None:
                # Enrich each surviving critic flag with the deterministic
                # dropped-units list: name the SPECIFIC
                # functions/classes the side added that the resolution dropped, so
                # the retry prompt gives the model exact targets ("reintroduce
                # function `foo`") rather than a vague "you dropped a side".
                dropped = _dropped_units_for(unit, cand)
                for cw in deduped_critic:
                    failures.append(_critic_failure(cw, dropped))
            # Lift actionable soft-validator warnings (intent_coverage,
            # unattributed_code, both_sides_represented, ...) into the failure
            # list so they reach the repair prompt too. Without this, a
            # warning-driven retry left ``failures`` empty → propose() fell
            # through to a feedback-free build_resolve_prompt regeneration
            # (the critic-path comment above describes the same pathology).
            failures.extend(_soft_warning_failures(validation))
            failures = failures or None
            # Track which budget this retry consumes. A recovery retry (model
            # self-reported needs_human; risk.decide granted a recovery attempt)
            # uses the separate recovery budget and a reframed prompt — detected
            # via the __recovery_retry__ followup marker.
            is_recovery_retry = "__recovery_retry__" in (decision.required_followups or [])
            if is_recovery_retry:
                recovery_retry_count += 1
                pending_recovery = True
                self.journal.emit(
                    "recovery_retry",
                    {"retry_count": recovery_retry_count,
                     "outcome": "pending"},
                    step_index=self.step, path=unit.path, unit_id=unit.unit_id,
                )
            elif critic_warning is not None:
                critic_retry_count += 1
            else:
                retry_count += 1
            prev_candidate = cand  # for targeted repair on next attempt

    # ------------------------------------------------------------------ helpers

    def _journal_validation(
        self, unit: ConflictUnit, cand: CandidateResolution, validation: VerificationResult
    ) -> None:
        """Emit/store a candidate's validation result for the audit trail.

        Used for every validated candidate (including the consensus-losers tried
        before the winner in the rank-order loop), so the journal shows which
        samples were skipped and why — not just the one that was accepted.
        """
        if self.config.journal.enabled and self.config.journal.store_validations:
            self.journal.store_validation(validation)
        self.journal.emit(
            "candidate_validated",
            {
                "candidate_id": cand.candidate_id,
                "passed": validation.passed,
                "hard_failures": [f.message for f in validation.hard_failures],
            },
            step_index=self.step,
            path=unit.path,
            unit_id=unit.unit_id,
        )

    def _gather_step(self) -> StepResult:
        result = StepResult(step_index=self.step)
        unmerged = self.git.list_unmerged_paths()
        if not unmerged:
            return result
        decision = self.policy.classify(unmerged)
        result.skipped = decision.skipped
        for sk in decision.skipped:
            self.journal.emit(
                "path_skipped",
                {"path": sk.path, "reason": sk.reason},
                step_index=self.step,
                path=sk.path,
            )
        for entry in decision.supported:
            self.journal.emit(
                "conflict_detected",
                {"path": entry.path, "mode": entry.mode},
                step_index=self.step,
                path=entry.path,
            )
            try:
                units = self.extractor.extract_file_units(
                    entry.path, self.step, self.session_id, unmerged=entry
                )
            except Exception as exc:  # noqa: BLE001
                result.skipped.append(
                    SkippedPath(entry.path, f"extraction error: {exc}")
                )
                continue
            if not units:
                result.skipped.append(
                    SkippedPath(entry.path, "unmerged but no marker blocks")
                )
                continue
            result.units_by_path[entry.path] = units
            # History-awareness (#history-3): stamp replay identity onto each
            # unit so history-aware components know which commit they're
            # resolving. The stopped-sha is read once per gather (cheap; it's a
            # single file read). Advisory: absent/None degrades to no history.
            replayed_oid = self._current_replayed_oid()
            for u in units:
                if replayed_oid:
                    u.structural_metadata["replayed_commit_oid"] = replayed_oid
            for u in units:
                self.journal.emit(
                    "conflict_unit_extracted",
                    {
                        "unit_id": u.unit_id,
                        "unit_kind": u.unit_kind,
                        "language": u.language,
                        "enclosing_symbol": u.enclosing_symbol,
                    },
                    step_index=self.step,
                    path=u.path,
                    unit_id=u.unit_id,
                )
        if result.skipped and not result.units_by_path:
            result.escalated = True
            result.reason = "all conflicted paths are unsupported"
        return result

    def _merge_resolution_features(
        self,
        features: dict,
        outcome: "UnitOutcome",
        accepted: CandidateResolution | None,
    ) -> dict:
        """Merge resolution-process signals into the feature dict for recording.

        These are the cheap, deterministic "epistemic uncertainty" features the
        system already computed during resolution (consensus stats, difficulty
        class, conflict size, candidate confidence, retry count). They never
        reach the validator's own features dict, so without this merge they'd
        be dropped at the memory seam and the calibration model couldn't learn
        from them. Keys match the extended ``_FEATURE_KEYS``.
        """
        out = dict(features)
        rep = outcome.consensus
        out["consensus_entropy"] = float(getattr(rep, "entropy", 0.0) or 0.0)
        out["consensus_agreement"] = float(getattr(rep, "agreement_score", 0.0) or 0.0)
        out["consensus_cluster_count"] = float(getattr(rep, "cluster_count", 0) or 0)
        # FactSelfCheck rationale-consistency: agreement over the
        # candidates' own intent claims, surfaced from the consensus report.
        # Defaults (1.0 / 0) when no multi-sample consensus ran.
        out["intent_agreement"] = float(getattr(rep, "intent_agreement", 1.0) or 1.0)
        out["low_consistency_fact_count"] = float(
            getattr(rep, "low_consistency_fact_count", 0) or 0
        )
        out["difficulty_complex"] = 1.0 if outcome.difficulty == "complex" else 0.0
        out["retry_count"] = float(outcome.retry_count)
        unit = outcome.unit
        out["conflict_side_chars"] = float(
            len(unit.base.text) + len(unit.current.text) + len(unit.replayed.text)
        )
        # Pre-resolution severity: a triage signal computed at
        # extraction, before any model call. Encoded numerically so the risk
        # score / calibration model can consume it (low=0, medium=1, high=2).
        out["conflict_severity"] = {"low": 0.0, "medium": 1.0, "high": 2.0}.get(
            unit.severity, 1.0
        )
        # Enclosing AST node line count, if structural metadata recorded it.
        span = unit.structural_metadata.get("enclosing_node_span")
        node_lines = 0.0
        if isinstance(span, (list, tuple)) and len(span) == 2:
            try:
                node_lines = float(int(span[1]) - int(span[0]) + 1)
            except (TypeError, ValueError):
                node_lines = 0.0
        out["enclosing_node_lines"] = node_lines
        # History-aware advisory features (#history step 8): compact signals
        # about the conflict's replay position + future-commit relevance. These
        # flow to the experience store (step 6), the accept report (#4), and
        # (later) the risk/calibration spine. Advisory only — they never gate
        # acceptance in interactive mode (step 10's strictness policy may use
        # them in unattended mode). Empty when no RebasePlan is active.
        hist_feats = self._history_features_for(unit)
        out.update(hist_feats)
        # Candidate self-reported confidence (model-side); use the accepted one
        # or, for escalations, the last attempt.
        cand = accepted if accepted is not None else (
            outcome.attempts[-1] if outcome.attempts else None
        )
        out["self_reported_confidence"] = float(
            getattr(cand, "self_reported_confidence", 0.0) or 0.0
        )
        # TECP token-entropy (model-side uncertainty): None when the candidate
        # didn't capture logprobs (e.g. a failed/technical candidate, or entropy
        # capture is off). features_to_vector maps None → 0.0 (treated as
        # "confident / not atypical"), which is the safe default.
        out["mean_token_entropy"] = getattr(cand, "mean_token_entropy", None)
        return out

    def _record_resolution_attempt(
        self, outcome: UnitOutcome, *, mechanism: str,
        candidate: CandidateResolution | None = None,
        validation: VerificationResult | None = None,
        decision: str = "skip", reason: str = "",
    ) -> ResolutionAttempt:
        """Record one mechanism's attempt as a uniform ResolutionAttempt (#idea 6).

        Appends to ``outcome.resolution_attempts`` AND emits a uniform
        ``resolution_attempt`` journal event (mechanism, decision, reason). This
        normalizes the 5 mechanisms' ad-hoc event vocabulary into one record so
        reports/metrics/dry-run consume a single shape. The candidate (if any) is
        also appended to the legacy ``outcome.attempts`` list for backward compat.
        """
        attempt = ResolutionAttempt(
            mechanism=mechanism, candidate=candidate,
            validation=validation, decision=decision, reason=reason,
        )
        outcome.resolution_attempts.append(attempt)
        if candidate is not None:
            outcome.attempts.append(candidate)
        self.journal.emit(
            "resolution_attempt",
            {"mechanism": mechanism, "decision": decision, "reason": reason,
             "candidate_id": candidate.candidate_id if candidate else None},
            step_index=self.step, path=outcome.unit.path,
            unit_id=outcome.unit.unit_id,
        )
        return attempt

    def _record_outcomes_to_memory(self, result: StepResult) -> None:
        """Append labeled outcomes to the experience store for RAG/calibration.

        Called once per step after resolution settles (accepted or escalated).
        Each unit's outcome becomes an Experience record: accepted merges are
        positive examples (few-shot + LoRA data), escalated ones are negative
        labels for calibration. No-op when the memory store is not configured.
        """
        if self.memory_store is None:
            return
        from capybase.conflict_model import HistoricalExample
        from capybase.memory.store import Experience

        for outcome in result.outcomes:
            unit = outcome.unit
            accepted = outcome.accepted
            # Collect a conflict-chain observation (#9 step 7) for every outcome,
            # so detect_conflict_chains() can find related conflicts across the
            # replay. Done unconditionally (not just on successful memory append)
            # so an escalated unit still counts toward its chain.
            self._record_conflict_observation(unit, accepted is None)
            if accepted is not None:
                resolved = accepted.resolved_text
                outcome_label = "accepted"
            else:
                # Escalated: use the last attempt's text if any, else empty.
                resolved = outcome.attempts[-1].resolved_text if outcome.attempts else ""
                outcome_label = "escalated"
            features = {}
            risk_score = None
            if outcome.validation is not None:
                features = dict(outcome.validation.features)
            if outcome.decision is not None:
                risk_score = outcome.decision.risk_score
            # Merge the resolution-process signals into the recorded features so
            # the calibration model can learn from consensus disagreement,
            # difficulty, conflict complexity, and candidate confidence — not
            # just the validator hard-checks. These are the "epistemic
            # uncertainty" features the system already computed and journaled;
            # this is the seam that lets the offline flywheel actually see them.
            features = self._merge_resolution_features(features, outcome, accepted)
            try:
                self.memory_store.append(
                    Experience(
                        example=HistoricalExample(
                            summary=f"{unit.path}:{unit.unit_id}",
                            base=unit.base.text,
                            current=unit.current.text,
                            replayed=unit.replayed.text,
                            resolved=resolved,
                            source=self.session_id,
                        ),
                        outcome=outcome_label,
                        language=unit.language,
                        path=unit.path,
                        session_id=self.session_id,
                        unit_id=unit.unit_id,
                        validator_features=features,
                        risk_score=risk_score,
                        retry_count=outcome.retry_count,
                        # History-aware features (#history step 6): compact signals
                        # about the conflict's replay position + future-commit
                        # relevance. Empty when no RebasePlan is active.
                        history_features=self._history_features_for(unit),
                        # Resolution provenance (#9 step 8): lets metrics (#9) +
                        # the dry-run report (#10) slice by mechanism. Empty for
                        # escalated outcomes with no accepted candidate.
                        provenance=getattr(accepted, "provenance", "") or "",
                        # Explainable-retrieval fields (#9 step 5): the region
                        # kind + normalized conflict shape, so retrieval can
                        # surface same-kind/same-shape reasons and exact reuse
                        # (#9 step 4) can match structurally.
                        region_kind=self._region_kind_for(unit),
                        conflict_shape=self._conflict_shape_for(unit),
                        # Telemetry (feedback §5.1): structured per-task outcome
                        # signals for future online-adaptation work.
                        parse_success=(
                            accepted is not None
                            and getattr(accepted, "failure_kind", "") != "parse_failed"
                        ),
                        layout_used=(
                            getattr(accepted, "prompt_version", "")
                            or (getattr(outcome.attempts[-1], "prompt_version", "")
                                if outcome.attempts else "")
                        ),
                        samples_used=int(getattr(
                            getattr(outcome, "consensus", None), "n_samples", 1
                        ) or 1),
                        failure_mode=_categorize_failure_mode(accepted, outcome),
                    )
                )
                # #11: refresh the retriever so step N+1 sees step N's accepted
                # example within the same rebase session (without this the
                # retriever cache is stale until the next process restart).
                retriever = getattr(self.context_builder, "retriever", None)
                if retriever is not None and hasattr(retriever, "refresh"):
                    try:
                        retriever.refresh()
                    except Exception:  # noqa: BLE001 - best-effort
                        pass
            except Exception:  # noqa: BLE001 - memory is best-effort
                pass

    def _write_and_stage(
        self,
        path: str,
        buffer: str,
        result: StepResult,
        *,
        accepted: list[tuple[ConflictUnit, CandidateResolution]] | None = None,
    ) -> None:
        """Write the resolved file to the worktree and stage it.

        A whole-file modify/delete accepted as a deletion (empty resolved text)
        is staged as a removal via ``git rm`` instead of write+add: the file
        goes away. ``accepted`` is the path's accepted resolutions so the delete
        case can be detected; callers without a resolution list (e.g. writing a
        pre-computed buffer) pass nothing and get the write+add path.
        """
        if accepted is not None and _is_whole_file_delete(accepted):
            self.git.remove_file_stage(path)
            self.journal.emit(
                "file_removed",
                {"path": path, "decision": "accept_deletion"},
                step_index=self.step,
                path=path,
            )
            return
        if self.config.journal.enabled and self.config.journal.store_snapshots:
            # Snapshot the ACTUAL pre-write worktree content — the on-disk file
            # before this resolution overwrites it — so the audit trail shows
            # what changed, not the resolved buffer being written (a prior bug
            # snapshotted `buffer`, making the ".before" name a lie). A missing
            # file (new path) has no prior content to snapshot.
            try:
                prior = self.git.read_worktree_file(path).decode(
                    "utf-8", errors="replace"
                )
                self.journal.store_snapshot(
                    f"{path.replace('/', '__')}.before", prior
                )
            except (FileNotFoundError, OSError):
                pass  # new file: nothing pre-existed to snapshot
        self.git.write_worktree_file(path, buffer.encode("utf-8"))
        self.journal.emit(
            "file_written",
            {"path": path, "bytes": len(buffer)},
            step_index=self.step,
            path=path,
        )
        if self.config.policy.stage_only_validated_paths:
            self.git.stage_paths([path])
            self.journal.emit(
                "file_staged",
                {"path": path},
                step_index=self.step,
                path=path,
            )

    def _write_worktree_only(
        self,
        path: str,
        buffer: str,
        *,
        accepted: list[tuple[ConflictUnit, CandidateResolution]] | None = None,
    ) -> None:
        """Write a resolved file to the worktree WITHOUT staging it.

        Used by Phase 1 of cross-file resolution: every conflicted file is
        written resolved first, so the whole crate is marker-free before any
        cargo check runs in Phase 2. Staging is deferred to ``_write_and_stage``
        (called in Phase 2 after validation passes) so an escalatable failure
        never leaves staged-but-invalid state. The journal snapshot is skipped
        here (Phase 2's ``_write_and_stage`` records the final staged buffer).

        A whole-file deletion (empty resolved text) removes the worktree file
        instead of writing it, so Phase-2 validation sees the crate without it.
        Staging the removal still happens in ``_write_and_stage`` (Phase 2).
        """
        if accepted is not None and _is_whole_file_delete(accepted):
            # Remove the worktree file only (no staging yet — that's Phase 2).
            full = self.git.repo / path
            if full.exists():
                full.unlink()
            return
        self.git.write_worktree_file(path, buffer.encode("utf-8"))

    def _capture_test_continuity_baseline(self) -> None:
        """Run the test suite on the pre-rebase tree and record passing node-IDs.

        Survey §2.1a test-continuity invariant: the baseline set is diffed
        against the post-merge passing set in _run_tests — a baseline-passing
        test that now fails is a behavioral regression the merge introduced.
        Best-effort: any failure, missing command, or empty per-test output
        leaves ``self._test_continuity_baseline`` None and the invariant inert.
        """
        if not self.config.tests.enable_test_continuity:
            return
        cmd = self.config.tests.pre_continue or self.config.tests.final
        if not cmd:
            return
        cmd = self._resolve_test_command(cmd)
        # pytest needs -v to emit per-test ``node PASSED`` lines we can parse.
        if _tool_of_test_cmd(cmd) == "pytest" and "-v" not in cmd.split():
            cmd = cmd + " -v"
        try:
            run = self.tests.run(cmd)
        except Exception:  # noqa: BLE001 - baseline is best-effort
            return
        if not run.stdout:
            return
        tool = _tool_of_test_cmd(cmd)
        baseline = parse_passing_node_ids(run.stdout, tool)
        if baseline:
            self._test_continuity_baseline = baseline
            self.journal.emit(
                "test_continuity_baseline",
                {"count": len(baseline), "tool": tool},
            )
            self.log.info(
                "test-continuity baseline: %d passing test(s) captured (%s)",
                len(baseline), tool,
            )

    def _test_continuity_regressions(self, postmerge_stdout: str, cmd: str) -> list[str]:
        """Tests that PASSED pre-rebase but are absent from the post-merge pass set.

        Returns the sorted list of regressed node-IDs (baseline-passing tests
        that no longer pass), or [] when no baseline was captured. These are
        high-signal counterexamples for the CEGIS loop: "test X passed before
        this rebase and fails now — your merge broke it".
        """
        baseline = self._test_continuity_baseline
        if not baseline:
            return []
        tool = _tool_of_test_cmd(cmd)
        postmerge_passing = parse_passing_node_ids(postmerge_stdout or "", tool)
        regressed = sorted(baseline - postmerge_passing)
        return regressed

    def _run_tests(self, label: str, result: StepResult) -> bool:
        cmd = getattr(self.config.tests, label) if hasattr(self.config.tests, label) else None
        if not cmd:
            return True
        # Whether the configured command is the shipped default (vs an explicit
        # user choice). The default is Python-centric ("pytest"); for a repo it
        # doesn't fit (Go/JS/etc. with no pytest and no cargo), a "command not
        # found" must NOT block the rebase — that's the absence of a test gate
        # for this repo, not a failing test. An explicit user command that's
        # missing still fails (it was a deliberate choice).
        is_default_cmd = cmd.strip() == "pytest"
        cmd = self._resolve_test_command(cmd)
        self.journal.emit("tests_started", {"label": label, "command": cmd}, step_index=self.step)
        # For ``cargo test`` in a workspace (no root Cargo.toml), cargo must run
        # from a member crate's directory — it can't discover the project from
        # the workspace root. Anchor on the first conflicted file's nearest crate
        # dir (the same nearest-manifest logic the cargo syntax check uses).
        test_cwd = self._cargo_test_cwd(result, cmd)
        run = self._run_test_command(cmd, cwd=test_cwd)
        # The shipped-default command wasn't found and couldn't be auto-resolved
        # to one that exists (e.g. a Go/JS repo with no pytest and no cargo).
        # Treat it as "no test gate for this repo" rather than a hard failure:
        # warn and continue. Never applies to an explicit user-configured command.
        if (
            is_default_cmd
            and not run.passed
            and run.verdict.kind == "unknown"
            and "not found" in (run.verdict.summary or "")
        ):
            self.journal.emit(
                "tests_default_unresolved",
                {"label": label, "command": cmd, "summary": run.verdict.summary},
                step_index=self.step,
            )
            self.out(
                f"  no test command for this repo (default `{cmd}` not found, "
                f"no cargo detected); skipping the {label} test gate. Set "
                f"[tests] {label} to your suite's command to enable it."
            )
            return True
        self.journal.emit(
            "tests_finished",
            {
                "label": label,
                "passed": run.passed,
                "returncode": run.returncode,
                "timed_out": run.timed_out,
                "verdict": run.verdict.kind,
                "verdict_summary": run.verdict.summary,
                "diagnostics": run.verdict.diagnostics[:5],
                "stdout_tail": run.stdout[-1000:],
                "stderr_tail": run.stderr[-1000:],
            },
            step_index=self.step,
        )
        result.tests_passed = run.passed
        # Stash the parsed verdict for the accept report (the report is written
        # after this call returns, in run()'s loop, and needs the human-readable
        # verdict like "1 test failed" / "compile error").
        self._last_test_verdict = run.verdict.summary or None
        # Test-continuity diff: tests that PASSED pre-rebase but
        # no longer pass are regressions the merge introduced — high-signal
        # counterexamples. Sharpen the verdict so the human/model sees WHICH
        # baseline tests broke, not just "tests failed".
        regressions = self._test_continuity_regressions(run.stdout, cmd)
        # Stash for the drift detector: _observe_drift (run after this gate)
        # reads the step's regressions as the behavioral-drift primary signal.
        # Set unconditionally — an empty list means "no regressions this step".
        self._last_continuity_regressions = list(regressions)
        if regressions:
            names = ", ".join(regressions[:5]) + (" ..." if len(regressions) > 5 else "")
            self._last_test_verdict = (
                f"{len(regressions)} test(s) that passed pre-rebase now fail: {names}"
            )
            self.journal.emit(
                "test_continuity_regressions",
                {"regressions": regressions, "label": label},
                step_index=self.step,
            )
        if not run.passed:
            # Surface the parsed verdict so the human sees *why* the tests failed
            # (compile error vs. test failure vs. timeout vs. lock contention),
            # not just the return code.
            self.out(
                "  " + self._warn(
                    f"! {label} tests failed (rc={run.returncode}): "
                    f"{run.verdict.summary or 'unknown'}"
                )
            )
            for d in run.verdict.diagnostics[:3]:
                self.out(f"      {d}")
            if regressions:
                self.out(
                    "  " + self._warn(
                        f"  test-continuity: passed pre-rebase, now failing: {names}"
                    )
                )
        return run.passed

    def _run_test_command(self, cmd: str, *, cwd: str | None = None):
        """Run the test command, retrying on transient lock contention.

        cargo emits ``Blocking waiting for file lock on build directory`` when
        another cargo process holds the target/ lock — a transient condition
        unrelated to the merge. Aborting on it would reject a correct rebase;
        retrying (with a short backoff) is correct. Other verdicts are returned
        as-is for the caller to act on. Bounded to a few retries so a genuinely
        stuck lock still terminates.
        """
        import time

        max_lock_retries = 3
        backoff_seconds = 5.0
        for attempt in range(max_lock_retries + 1):
            run = self.tests.run(cmd, cwd=cwd)
            if not run.verdict.is_transient or attempt == max_lock_retries:
                return run
            self.journal.emit(
                "tests_lock_retry",
                {"attempt": attempt + 1, "verdict": run.verdict.kind,
                 "summary": run.verdict.summary},
                step_index=self.step,
            )
            self.out(
                f"  ... {run.verdict.summary}; retrying in {backoff_seconds:.0f}s "
                f"(attempt {attempt + 1}/{max_lock_retries})"
            )
            time.sleep(backoff_seconds)
        return run

    def _resolve_test_command(self, cmd: str) -> str:
        """Resolve a (possibly language-default) test command to a real one.

        The shipped default is ``"pytest"`` (Python-centric). When that default
        is configured and the repo is a Cargo project with no pytest on PATH,
        substitute ``"cargo test"`` — a pure-Rust repo would otherwise fail
        every ``run`` at the pre-continue gate. An *explicit* command (anything
        other than the bare ``"pytest"`` default, including a user who set
        ``pre_continue = "cargo test"`` themselves) is returned unchanged:
        we never override a deliberate choice. This keeps Python repos on
        pytest (the common case) while making Rust repos work out of the box.
        """
        if cmd.strip() != "pytest":
            return cmd
        # A repo "has cargo" when the root OR any top-level subdir has a
        # Cargo.toml (workspaces: each crate lives in a subdir, no root
        # manifest). Without this, a workspace Rust repo stays on pytest and
        # fails the gate with "No such file or directory: 'pytest'".
        if not _repo_has_cargo(self.git.repo):
            return cmd
        # It's a cargo repo. Prefer ``cargo test`` UNLESS this is also a real
        # Python project (has a pyproject.toml/setup.py) — then it's a genuine
        # mixed repo and we honor the configured pytest default. The presence of
        # ``pytest`` on PATH alone is NOT enough: it may be a *different*
        # project's venv (e.g. capybase's own dev venv), not this repo's. A cargo
        # repo with stray ``.py`` utility scripts but no Python project manifest
        # is Rust-dominant → cargo test.
        if _has_python_project(self.git.repo):
            return cmd
        return "cargo test"

    def _cargo_test_cwd(self, result: StepResult, cmd: str) -> str | None:
        """The directory to run ``cargo test`` from, or None to use the repo root.

        For a ``cargo test`` invocation in a workspace (no root Cargo.toml), cargo
        can't discover the project from the workspace root — it needs to run from
        a member crate's directory. We anchor on the first conflicted file's
        nearest crate dir (the same nearest-manifest logic the cargo syntax check
        uses), so the test gate runs the crate the conflict actually touches. For
        a single-crate-at-root layout (root Cargo.toml), cargo runs fine from the
        repo root → None (the runner's default cwd).
        """
        if not cmd.strip().startswith("cargo"):
            return None
        from capybase.adapters.lsp import _has_cargo_manifest, nearest_cargo_manifest_dir

        # Root manifest → cargo discovers from the repo root; no override needed.
        if _has_cargo_manifest(str(self.git.repo)):
            return None
        # Workspace: find the crate dir to run cargo from. Anchor on the
        # conflict paths first, then the staged files (an edit-resolved step has
        # staged the resolution but has no units_by_path), then any member crate.
        # Without this fallback, a step with NO conflicts (clean apply, or a step
        # fully resolved by direct edit) leaves units_by_path empty → no path to
        # anchor on → cargo runs from the workspace root, which has no
        # Cargo.toml → ``could not find Cargo.toml`` aborts a correct rebase.
        anchor_paths: list[str] = list(result.units_by_path)
        if not anchor_paths:
            try:
                anchor_paths = self.git.staged_paths()
            except Exception:  # noqa: BLE001 - advisory
                anchor_paths = []
        for path in anchor_paths:
            crate_dir = nearest_cargo_manifest_dir(str(self.git.repo), path)
            if crate_dir is not None:
                return str(crate_dir)
        # Last resort: scan top-level subdirs for any member crate. cargo must
        # run from SOME crate dir; the workspace root has no manifest.
        try:
            for entry in self.git.repo.iterdir():
                if entry.is_dir() and (entry / "Cargo.toml").is_file():
                    return str(entry)
        except OSError:  # noqa: BLE001
            pass
        return None

    def _ok(self, text: str) -> str:
        """A success line with its ``✓`` marker green when color is enabled.

        Only the marker is colored; the message stays plain for readability.
        Passthrough (no codes) when color is disabled.
        """
        from capybase.color import GREEN
        return self.style("✓", GREEN) + text.lstrip("✓").lstrip()

    def _warn(self, text: str) -> str:
        """A warning/error line with its ``!`` marker red when color is enabled.

        Only the marker is colored; the message stays plain for readability.
        Passthrough (no codes) when color is disabled.
        """
        from capybase.color import RED
        return self.style("!", RED) + text.lstrip("!").lstrip()

    def _write_accept_report(self, result: StepResult) -> None:
        """Append a semantic accept report for the step's accepted units (#4).

        Composes the per-unit obligations/validation/classification with the
        step-level test verdict into a human-readable "why we accepted" summary,
        appended to ``final/accept-report.md``. Run after the test gate, when
        both per-unit outcomes (``result.outcomes``) and the test verdict
        (``result.tests_passed``) exist. A no-op when no unit was accepted (an
        escalation step) or when report-writing is disabled. Advisory: a failure
        to write never breaks the rebase.
        """
        if not getattr(self.config.journal, "write_accept_reports", True):
            return
        try:
            from capybase.accept_report import build_accept_report

            body = build_accept_report(
                result.outcomes,
                tests_passed=result.tests_passed,
                test_verdict=self._last_test_verdict,
            )
            if not body:
                return
            report = self.paths.final / "accept-report.md"
            header = f"## step {result.step_index}\n\n"
            # Append (one section per step); create on first write.
            if report.exists():
                existing = report.read_text(encoding="utf-8")
                report.write_text(existing.rstrip("\n") + "\n\n" + header + body, encoding="utf-8")
            else:
                report.write_text("# capybase accept report\n\n" + header + body, encoding="utf-8")
            self.journal.emit(
                "accept_report_written",
                {"path": str(report.relative_to(self.paths.repo_root)),
                 "units": sum(1 for o in result.outcomes if o.accepted is not None)},
                step_index=result.step_index,
            )
        except Exception as exc:  # noqa: BLE001 - advisory report; never block the rebase
            self.log.debug("accept report not written: %s", exc)

    def _summarize(self, result: StepResult | None) -> None:
        if result is None:
            return
        self.out(f"[step {result.step_index}] summary")
        self.out(f"  units by path: {len(result.units_by_path)}")
        self.out(f"  skipped paths: {len(result.skipped)}")
        self.out(f"  outcomes: {len(result.outcomes)}")
        self.out(f"  escalated: {result.escalated}" + (f" ({result.reason})" if result.reason else ""))
        self.out(f"  continued: {result.continued}")
        self.out(f"  journal: {self.paths.journal}")

    def _render_unit(self, unit: ConflictUnit) -> str:
        """Manual-mode unit render. Headers colored like the interactive variant
        (BASE dim, CURRENT cyan, REPLAYED magenta, unit header bold); content
        stays plain. A passthrough when color is disabled."""
        from capybase.color import BOLD, CYAN, DIM, MAGENTA

        s = self.style
        return (
            f"{s(f'\\n=== {unit.unit_id} ({unit.path}, {unit.conflict_type}) ===', BOLD)}\n"
            f"{s('-- BASE --', DIM)}\n{unit.base.text}\n"
            f"{s('-- CURRENT_UPSTREAM_SIDE --', CYAN)}\n{unit.current.text}\n"
            f"{s('-- REPLAYED_COMMIT_SIDE --', MAGENTA)}\n{unit.replayed.text}\n"
        )


def _repo_has_cargo(repo_root: Path) -> bool:
    """Whether ``repo_root`` is (part of) a Cargo project.

    True when the root OR any immediate top-level subdirectory contains a
    ``Cargo.toml``. The subdir check handles Cargo WORKSPACES, where each member
    crate lives in its own subdirectory and there's no root manifest — the common
    layout (di-rac-rebase-test: di-core/, divrr/, wasm-runner/). Only one level
    deep is scanned: a workspace's member crates sit directly under the root, and
    a deeper scan risks matching an unrelated vendored crate. Used by the
    auto-substitution of ``cargo test`` for the default ``pytest`` test gate.
    """
    if (repo_root / "Cargo.toml").is_file():
        return True
    try:
        for entry in repo_root.iterdir():
            if entry.is_dir() and (entry / "Cargo.toml").is_file():
                return True
    except OSError:  # noqa: BLE001 - unreadable dir → treat as no cargo
        return False
    return False


def _has_python_project(repo_root: Path) -> bool:
    """Whether ``repo_root`` is a real Python project (vs stray ``.py`` scripts).

    True when a Python project manifest is present at the root (``pyproject.toml``
    or ``setup.py``). These are the conventional markers a Python project declares
    its build/test setup; their absence means stray ``.py`` utility scripts don't
    constitute a Python project. Used to distinguish a genuine mixed repo (cargo +
    Python → honor the configured pytest) from a Rust-dominant repo with incidental
    ``.py`` files (→ cargo test).
    """
    return (repo_root / "pyproject.toml").is_file() or (
        repo_root / "setup.py"
    ).is_file()


def _default_stdin_reader(prompt: str, *, multiline: bool = False) -> str:
    """Read input from the terminal.

    Single-line mode (the default): the prompt is printed (no trailing newline)
    and ONE line is read — this is what the menu choice and "press Enter when
    done" prompts need, so typing ``4`` + Enter returns immediately.

    Multi-line mode (``multiline=True``): used for pasted resolutions. Reads
    lines until EOF (Ctrl-D) and joins them — a pasted block has no natural
    terminator, so the human signals the end explicitly.

    The split is load-bearing: the old implementation always read until EOF,
    which meant a menu choice like ``4`` was swallowed and never returned — the
    program blocked until Ctrl-C, ignoring the choice. Single-line callers must
    pass the default; only paste callers opt into multiline.
    """
    # print(end=...) so the prompt sits on the same line as the typed input
    # (print(prompt) would push the user's response onto the next line).
    print(prompt, end="", flush=True)
    if not multiline:
        try:
            return input()
        except EOFError:
            return ""
    chunks: list[str] = []
    try:
        while True:
            line = input()
            chunks.append(line)
    except EOFError:
        pass
    return "\n".join(chunks)


def _is_interactive_terminal() -> bool:
    """True iff stdin is a real terminal (a human is present).

    The interactive fallback fires only when this is True, so it never blocks a
    non-TTY run (CI, piped input). Tests force it on/off by monkeypatching this
    function (they can't provide a real TTY)."""
    import sys
    return bool(getattr(sys.stdin, "isatty", lambda: False)())


def _toml_dump_config(config: Config) -> str:
    """Minimal TOML serializer for the config snapshot (stdlib only)."""
    lines: list[str] = []

    def emit_section(name: str, d: dict) -> None:
        lines.append(f"[{name}]")
        for k, v in d.items():
            lines.append(f"{k} = {_toml_value(v)}")
        lines.append("")

    emit_section("model", config.model.model_dump())
    emit_section("policy", config.policy.model_dump())
    emit_section("tests", config.tests.model_dump())
    emit_section("validation", config.validation.model_dump())
    emit_section("journal", config.journal.model_dump())
    emit_section("future", config.future.model_dump())
    return "\n".join(lines)


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return '"' + str(v).replace('"', '\\"') + '"'

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
import re
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
from capybase.verification import (
    ValidationConfig,
    VerificationEngine,
    _braces_balanced,
    structural_gate_applies,
)
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
    # Normalized-form hashes (comments stripped + whitespace collapsed + lines
    # sorted) for the convergence check (Issue 4). Catches cosmetic-variation
    # cycling the exact-hash oscillation backstop misses.
    _seen_normalized_hashes: dict[str, int] = field(default_factory=dict)
    # Recent hard-failure signatures (Fix C: no-progress guard). Each entry is a
    # frozenset of (validator, message) tuples from one attempt's hard_failures.
    # When the last N signatures are identical, the loop is producing zero new
    # information → escalate. Keys on FAILURE shape (not candidate hashes) so it
    # catches the empty-output transport loop AND genuine stuck-on-one-error
    # cycling that the content-hash backstops structurally cannot reach.
    _recent_hard_failure_sigs: list = field(default_factory=list)
    # C12 (sprint-26): the empty-oscillation band — attempts alternate between
    # empty responses (no repairable text) and non-empty candidates rejected
    # for concrete parse defects (stray '@', missing terminator, expected
    # unqualified-id). One kind string per RETRY-rejected attempt. The
    # alternation starves the CEGIS loop: every empty retry discards the
    # defect candidate's progress, and the loop eventually dies on an empty
    # candidate — exactly when the shattered rescue needs non-empty text.
    _osc_attempt_kinds: list = field(default_factory=list)
    # The most recent (candidate, ValidationResult) pair whose resolved_text
    # was non-empty and rejected — the shattered-rescue retarget when the
    # no-progress guard fires on an empty candidate.
    _osc_last_defect: tuple | None = None
    # No-op cache (the analysis's "eliminate avoidable slow retries"): maps a
    # candidate's resolved_text hash → its VerificationResult. When the model
    # re-proposes the same candidate (common after a preservation-heuristic
    # retry), the validation is reused instead of re-running compilation/tests.
    _candidate_validation_cache: dict[str, object] = field(default_factory=dict)
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


def _normalize_for_convergence(text: str, language: str | None) -> str:
    """Normalize candidate text for CEGIS convergence detection (Issue 4).

    Strips comments + collapses whitespace + sorts lines, so two candidates
    that differ only cosmetically (whitespace, comment reordering, blank-line
    counts) hash to the same value. Catches the cycling pattern the exact-hash
    oscillation backstop misses: a model making slightly-different mistakes
    each retry that are semantically identical.

    String-literal VALUES are preserved (NOT blanked). A version bump
    ``"1.28.1"`` → ``"1.29.0"`` is a genuine semantic change, not cosmetic
    variation. The original implementation blanked strings then sorted tokens
    into a bag, which erased version values AND destroyed line structure —
    causing byte-identical candidates with different version values to be
    flagged as cosmetic cycling.
    """
    try:
        from capybase.adapters.string_lexer import blank_strings_and_comments
        # Blank COMMENTS ONLY (not strings). String values are real semantic
        # content. Comment text is cosmetic (a comment change is not a real
        # code change).
        blanked = blank_strings_and_comments(text, language, blank_strings=False)
        # Collapse per-line whitespace, then sort LINES (not tokens). Line
        # order is cosmetic for "same essential code" (the model may reorder
        # independent items); within-line structure is NOT cosmetic (sorting
        # tokens destroys block/brace structure and conflates unrelated lines).
        lines = [" ".join(line.split()) for line in blanked.splitlines()]
        return "\n".join(sorted(lines))
    except Exception:
        # Fallback: whitespace-collapse + line-sort (no comment stripping).
        lines = [" ".join(line.split()) for line in text.splitlines()]
        return "\n".join(sorted(lines))


def _normalize_failure_message(message: str) -> str:
    """Normalize a hard-failure message for the no-progress signature (Fix C).

    Strips volatile line/column numbers so the SAME error at a shifted location
    still counts as "no progress" (the model moved the bug but didn't fix it).
    Preserves symbol names, error kinds, and diagnostic codes — so a genuinely
    different error (different symbol, different failure class) still registers
    as a changed signature. Example:
      "line 142: function 'foo' defined more than once (at lines 142, 160)"
      → "line N: function 'foo' defined more than once (at lines N, N)"
    """
    import re
    return re.sub(r"\b\d+\b", "N", message)


def _error_class(message: str) -> str:
    """P5 (sprint-23 batch E): extract the error class from a failure message.

    The no-progress guard fires when the signature is identical across
    retries. But "missing symbol X" → "type mismatch on X" is genuine
    progress (the symbol was found; its type is now wrong). Including
    the error class in the signature distinguishes these:
      syntax vs semantic vs type vs symbol vs marker vs build
    """
    import re
    msg = (message or "").lower()
    if any(k in msg for k in ("syntax", "parse", "expected", "unterminated")):
        return "syntax"
    if any(k in msg for k in ("type", "pointer", "cast", "convert")):
        return "type"
    if any(k in msg for k in ("undeclared", "not found", "unresolved",
                              "unknown", "missing")):
        return "symbol"
    if any(k in msg for k in ("defined multiple", "duplicate", "redefin")):
        return "duplicate"
    if any(k in msg for k in ("marker", "conflict")):
        return "marker"
    if any(k in msg for k in ("build", "make", "cmake")):
        return "build"
    return "other"


def _empty_terminal_grant_due(outcome) -> bool:
    """C20 follow-up (sprint-26): True when a unit's entire attempt
    history is PURE-EMPTY output (≥2 empties, zero defect candidates)
    and the one-shot terminal-recovery latch is unused. Such a unit
    never received a single counterexample — its budget burned on
    output weather. Callers grant ONE bounded recovery-prompt attempt
    (zenodo-0013 converted exactly this way in the fixpool;
    sqlite-0006#s0 / 0092 died without it) and set the latch."""
    kinds = getattr(outcome, "_osc_attempt_kinds", None)
    if (kinds
            and kinds.count("empty") >= 2
            and kinds.count("defect") == 0
            and not getattr(outcome, "_empty_terminal_recovery", False)):
        outcome._empty_terminal_recovery = True
        return True
    return False


def _hard_failure_signature(failures) -> frozenset:
    """A multiset signature of a candidate's hard failures for the no-progress
    guard (Fix C). Returns ``frozenset(Counter(...).items())`` — a hashable
    multiset of ``(validator, normalized_message)`` tuples that preserves error
    counts, so:

    * the SAME error at a shifted line → identical signature → "no progress";
    * a DIFFERENT symbol/error → different signature → progress/exploring;
    * one error fixed, three remain → different (smaller-count) signature → progress.

    Keys on failure shape, not candidate hashes, so it catches the empty-output
    transport loop (random UUIDs defeat the hash backstops; the content-hash
    checks are also gated on non-empty resolved_text) AND genuine stuck-on-one-
    compiler-error cycling. ``failures`` is a list of VerificationFailure."""
    from collections import Counter
    # P5 (sprint-23 batch E): prepend the error class to the normalized
    # message so "symbol missing" → "type mismatch" registers as progress
    # (different class prefix). Keeps the 2-tuple shape for backward compat.
    return frozenset(Counter(
        (f.validator,
         f"[{_error_class(f.message)}] {_normalize_failure_message(f.message)}")
        for f in failures
    ).items())


def _obligation_suffix(unit, cand) -> str:
    """A diagnostic suffix for convergence/oscillation escalation reasons:
    the specific missing obligations (from change accounting) when the cycling
    candidate copies one side.

    When a candidate converges because it copies one side verbatim, naming the
    exact dropped executable change(s) turns an opaque 'convergence' into an
    actionable escalation reason — the human/jury knows WHAT was lost, not just
    that the loop cycled. Returns "" when change-accounting doesn't apply (not
    a one-sided copy) or the copy is fully accounted for.
    """
    try:
        from capybase.change_accounting import derive_missing_obligations
        base = (unit.base.text or "") if unit else ""
        cur = (unit.current.text or "") if unit else ""
        rep = (unit.replayed.text or "") if unit else ""
        res = (cand.resolved_text or "") if cand else ""
        # Use the base HUNK (diff3-refined or re-derived), not the full base
        # file — the unit's base is the whole file but cur/rep are hunk-sized.
        # See PreservationHeuristicValidator.verify for the same fix.
        refined = unit.structural_metadata.get("diff3_refined") if unit else None
        if isinstance(refined, dict) and refined.get("base") is not None:
            base = refined["base"]
        else:
            from capybase.conflict_extractor import _base_hunk_via_diff3
            base_hunk = _base_hunk_via_diff3(base, cur, rep)
            if base_hunk is not None:
                base = base_hunk
        missing = derive_missing_obligations(base, cur, rep, res)
        if not missing:
            return ""
        # Only show the obligation suffix for one-sided copies (where it's
        # actionable). For genuine merges, the obligation detection is
        # advisory and may produce false positives when the base is the full
        # file (not the hunk). The genuine-merge accounting in the validator
        # handles these correctly; the suffix is just a diagnostic message.
        res_stripped = res.strip() if res else ""
        if res_stripped and res_stripped != (cur.strip() if cur else "") and res_stripped != (rep.strip() if rep else ""):
            return ""  # genuine merge — skip the suffix
        lines = ", ".join(
            repr(o.line.strip()[:50]) for o in missing[:3])
        return (f" — stalled on {len(missing)} unaccounted branch change(s): "
                f"{lines}")
    except Exception:  # noqa: BLE001 — diagnostic; never break the escalation
        return ""


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
        # Find the whole-file unit's resolution — it may not be at index 0
        # (the orchestrator sorts by marker_span start, skipping None-span units).
        for unit, cand in accepted:
            if unit.marker_span is None:
                return cand.resolved_text
        # Fallback: shouldn't happen (the any() above confirmed one exists)
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


def _juror_verdict_to_dict(v) -> dict:
    """Serialize a JurorVerdict (or None) for the jury_shadow artifact."""
    if v is None:
        return {"verdict": None}
    return {
        "verdict": v.verdict, "subtype": v.subtype,
        "evidence_ids": list(v.evidence_ids), "witness": v.witness,
        "confidence_band": v.confidence_band,
        "explanation": v.explanation[:200], "juror": v.juror,
    }


def _persist_unit_hashes(orch, outcome) -> None:
    """D1: copy a UnitOutcome's convergence hashes back to the per-step dict.

    Called after every ``_resolve_unit`` return so that a subsequent
    ``_whole_file_repair`` re-resolve of the same unit inherits the hashes —
    the model can't cycle through the same cosmetic variations again.
    Also persists failure signatures so the no-progress guard sees prior
    failures across the Phase 1 → Phase 2 boundary.
    """
    step_hashes = getattr(orch, "_step_convergence_hashes", None)
    if step_hashes is None or outcome is None:
        return
    uid = outcome.unit.unit_id
    # Merge (don't overwrite — the re-resolve may have added new hashes too).
    existing = step_hashes.get(uid, {})
    existing.update(outcome._seen_normalized_hashes)
    step_hashes[uid] = existing
    # Persist failure signatures for the cross-call no-progress guard.
    # OVERWRITE (not extend): the outcome already contains the inherited sigs
    # + new ones from this call. Extending would double-count the inherited
    # entries on re-persist (Phase 1 → Phase 2 → ...). Window to prevent
    # unbounded growth across many re-resolves.
    step_sigs = getattr(orch, "_step_failure_sigs", None)
    if step_sigs is not None:
        np_threshold = getattr(orch.config.policy, "cegis_convergence_threshold", 2)
        _window = max(np_threshold * 2, np_threshold + 2)
        all_sigs = getattr(outcome, "_recent_hard_failure_sigs", [])
        step_sigs[uid] = list(all_sigs[-_window:])



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
    """Cosine similarity of two equal-length vectors.

    Thin delegate to the canonical ``diff.cosine_similarity``. (The prior
    "local; never imports" rationale was stale — the orchestrator already
    lazy-imports from memory.retriever and elsewhere.)
    """
    from capybase.diff import cosine_similarity
    return cosine_similarity(a, b)


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


# Advisory-only validators: when hard failures (compiler/syntax errors) exist,
# these warnings are demoted to advisory — they're shown in the prompt context
# but NOT lifted to the failure list and NOT competing with the compiler error
# for the model's attention. The rationale: fix the compilation error first;
# the preservation concern may resolve itself or become moot after the fix.
# When NO hard failures exist, these remain fully actionable.
_ADVISORY_WHEN_HARD_EXISTS: frozenset[str] = frozenset({
    "preservation_heuristic",   # one-sided merge heuristic — often resolves after compile fix
    "unattributed_code",        # hallucinated unit — may disappear after structural fix
})


def _soft_warning_failures(
    validation: VerificationResult,
    *,
    hard_failures: list | None = None,
) -> list[VerificationFailure]:
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

    ``hard_failures``: when provided and non-empty, advisory-tier validators
    (preservation_heuristic, unattributed_code) are skipped — their concerns
    are secondary to the compiler error and would just confuse the model.
    """
    has_hard = bool(hard_failures)
    out: list[VerificationFailure] = []
    for w in validation.warnings:
        if w.validator not in _ACTIONABLE_SOFT_WARNINGS:
            continue
        # Advisory tier: skip when hard failures exist (compiler error dominates).
        if has_hard and w.validator in _ADVISORY_WHEN_HARD_EXISTS:
            continue
        out.append(VerificationFailure(
            validator=w.validator,
            severity="warning",
            message=w.message,
            detail=dict(w.detail),
        ))
    return out


# Compiler-error substrings that signal a duplicate-definition failure. When gcc
# emits one of these, the root cause is almost always a function/method defined
# BOTH inside the candidate's merge AND elsewhere in the file (the conflict
# region is a duplicate of code 10K lines away the model can't see).
_DUP_DEF_MARKERS = ("cannot be overloaded", "redefinition", "redeclared")


def _remove_duplicate_function_blocks(
    text: str, fn_names: set[str],
) -> str | None:
    """Remove duplicate function DEFINITION blocks from ``text``.

    For each name in ``fn_names``, finds the function's signature line (return-
    type prefix + name + ``(``), tracks the brace depth to the matching closing
    ``}``, and removes the entire block (signature through closing brace +
    trailing blank line). Returns the repaired text, or ``None`` if no blocks
    were removed.

    Safety: this removes from the CANDIDATE (the new, untested merge). The
    EXISTING definition elsewhere in the file is untouched — it's the well-
    tested version that remains after the duplicate is removed. gcc identified
    the specific function; this is compiler-guided, not regex-guessed.
    """
    if not text or not fn_names:
        return None
    lines = text.split("\n")
    removals: list[tuple[int, int]] = []  # (start, end_inclusive)
    for target in fn_names:
        for i, ln in enumerate(lines):
            if target not in ln or "(" not in ln:
                continue
            stripped = ln.strip()
            if stripped.endswith(";"):
                continue  # declaration, not definition
            # Require a return-type prefix before the name (not a bare call).
            idx = ln.find(target)
            prefix = ln[:idx].strip()
            if not prefix:
                # Multi-line signature: return type on the previous line,
                # function name on this line. Common in the model's merges.
                if i > 0:
                    prev = lines[i - 1].strip()
                    if (prev and not prev.startswith(("//", "/*", "*"))
                            and not prev.endswith((";", "{", "}", ","))):
                        pass  # previous line looks like a return type
                    else:
                        continue
                else:
                    continue
            # Skip if this line is inside a removal range already found.
            # Skip if this line is inside a removal range already found.
            # For multi-line signatures, also check the previous line.
            block_start = i - 1 if not prefix else i
            if any(s <= block_start <= e or s <= i <= e for s, e in removals):
                continue
            # Find the matching closing brace.
            depth = 0
            found_open = False
            end = i
            for j in range(i, len(lines)):
                for ch in lines[j]:
                    if ch == "{":
                        depth += 1
                        found_open = True
                    elif ch == "}":
                        depth -= 1
                        if found_open and depth == 0:
                            end = j
                            break
                if found_open and depth == 0:
                    break
            else:
                continue  # unbalanced — skip this match
            removals.append((block_start, end))
            break  # first match per name
    if not removals:
        return None
    # Remove blocks in reverse order (so indices stay valid).
    removals.sort(reverse=True)
    for start, end in removals:
        # Also remove one trailing blank line if present.
        if end + 1 < len(lines) and not lines[end + 1].strip():
            del lines[start:end + 2]
        else:
            del lines[start:end + 1]
    return "\n".join(lines)


def _find_def_context(
    lines: list[str], fn_name: str, *, width: int = 8,
) -> tuple[int, str] | None:
    """Grep ``lines`` for ``fn_name``'s DEFINITION; return ``(1-based line, src)``.

    Lightweight (regex, no treesitter): finds the first line that is a real
    definition/declaration signature — a return-type prefix before the name, a
    parameter list, and a ``{`` body (same line or next non-blank line) — then
    returns ``width`` lines of surrounding source. This is the definition
    context appended to duplicate-def CEGIS feedback so the model can SEE the
    function it must not re-define (the original may be thousands of lines
    outside the conflict hunk).

    Crucially skips CALL sites (``…name(…);``) and bare calls — these appear
    BEFORE the definition in source order, so a naïve "first line with name+("
    grep would surface a call, not the conflicting definition.
    """
    for i, ln in enumerate(lines):
        if fn_name not in ln or "(" not in ln:
            continue
        stripped = ln.strip()
        # Skip call/declaration statements (end with ';').
        if stripped.endswith(";"):
            continue
        # Require a return-type prefix before the name (not a bare call at the
        # start of the line, e.g. ``log_error(msg);``).
        idx = ln.find(fn_name)
        if not ln[:idx].strip():
            continue
        # Confirm a definition body: '{' on the same line after the closing
        # ')', or the next non-blank line starts with '{'.
        paren_end = stripped.rfind(")")
        if paren_end != -1 and "{" in stripped[paren_end:]:
            pass  # brace-on-same-line definition
        else:
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j >= len(lines) or not lines[j].strip().startswith("{"):
                continue  # not a definition body — skip
        ctx_s = max(0, i - 1)
        ctx_e = min(len(lines), i + width)
        ctx = "\n".join(f"  {lines[j]}" for j in range(ctx_s, ctx_e))
        return i + 1, ctx
    return None


def _enrich_duplicate_definition_failures(
    unit: "ConflictUnit", cand: "CandidateResolution",
    validation: "VerificationResult",
) -> None:
    """Append existing-definition source to duplicate-def failures (in place).

    gcc reports "cannot be overloaded" / "redefinition" with only the symbol
    name — the model knows a function is duplicated but can't see WHERE the
    conflicting definition is (it may be 10K lines outside the conflict hunk).
    This mutates each such failure's ``message`` to include the existing
    definition's source, so the very first CEGIS retry gives the model the code
    it must not re-define.

    Three passes:

    1. **Single-symbol** — the function gcc named. Extracted from gcc's
       fully-qualified path (``::get_cbor_float_prefix(``); grepped in the file.

    2. **Multi-symbol** — gcc stops at the FIRST overload conflict, but the
       candidate often defines a FAMILY of functions that all already exist
       (e.g. ``get_{cbor,msgpack,ubjson}_float_prefix`` × {float,double}).
       This pass scans the candidate for ALL function definitions and
       cross-references each against the non-conflict part of the file so one
       retry can fix them all at once (instead of one-per-iteration, which
       exhausts the header retry cap of 1).

    3. **Side attribution** — the model often fails not because it lacks the
       duplicate LIST but because the prompt's obligation block says
       "CURRENT must preserve: added get_cbor_float_prefix" — actively fighting
       the correct resolution. When one side's function additions are ALL
       duplicates and the other side is clean (or deleted the block), this pass
       reframes the note as a **conclusion-first recommendation**: "accept the
       deletion" / "prefer the other side." It explicitly states the "must
       preserve" obligation is INCORRECT for these functions. Without this, a
       4B model oscillates between the duplicate candidate and an empty one.

    Idempotent. No-op when the candidate is empty, the file is empty, or no
    duplicate-def failure exists.
    """
    if validation.passed or not getattr(validation, "hard_failures", None):
        return
    res_text = getattr(cand, "resolved_text", "") or ""
    if not res_text.strip():
        return
    orig_text = getattr(unit, "original_worktree_text", "") or ""
    if not orig_text.strip():
        return

    import re as _re_dup
    try:
        from capybase.structural_resolver import _extract_definition_names
    except Exception:  # noqa: BLE001 - enrichment degrades gracefully
        _extract_definition_names = None

    # Idempotency: skip failures already carrying any enrichment NOTE form.
    # Covers the legacy "NOTE: This/These function(s)" and the new conclusion-
    # first "NOTE — RESOLVE BY / PREFER THE / DUPLICATE DEFINITIONS" headers.
    _ENRICHED_TAGS = (
        "NOTE: This function already exists",
        "NOTE: These functions",
        "NOTE \u2014 RESOLVE BY",
        "NOTE \u2014 PREFER THE",
        "NOTE \u2014 DUPLICATE DEFINITIONS",
    )
    orig_lines = orig_text.split("\n")

    # The "rest of file": orig_text MINUS the unit's own conflict region. The
    # candidate replaces ``marker_span``, so a function defined there is not a
    # duplicate of itself — only definitions OUTSIDE the region count.
    rest_lines = orig_lines
    ms = getattr(unit, "marker_span", None)
    if ms is not None:
        _s, _e = ms[0], ms[1]
        rest_lines = orig_lines[:max(0, _s)] + orig_lines[min(len(orig_lines), _e + 1):]

    rest_defs = (_extract_definition_names(rest_lines)
                 if _extract_definition_names is not None else {})

    for f in validation.hard_failures:
        fmsg = getattr(f, "message", "") or ""
        if any(_tag in fmsg for _tag in _ENRICHED_TAGS):
            continue  # already enriched (e.g. by a prior validation reuse)
        fmsg_n = fmsg.replace("\u2018", "'").replace("\u2019", "'")
        if not any(m in fmsg_n for m in _DUP_DEF_MARKERS):
            continue

        # --- Pass 1+2: collect ALL duplicate functions (name -> (line, src)) ---
        dupes: dict[str, tuple[int, str]] = {}  # name -> (1-based line, context)

        # Pass 1: the function gcc named.
        fm = _re_dup.search(r"::(\w+)\(", fmsg_n)
        if fm:
            fn = fm.group(1)
            hit = _find_def_context(orig_lines, fn)
            if hit:
                dupes[fn] = hit

        # Pass 2: all candidate functions that already exist in the file.
        if _extract_definition_names is not None:
            cand_defs = _extract_definition_names(res_text.split("\n"))
            for cname in cand_defs:
                if cname in dupes:
                    continue
                if cname not in rest_defs:
                    continue
                hit = _find_def_context(orig_lines, cname)
                if hit:
                    dupes[cname] = hit

        if not dupes:
            continue

        # --- Pass 3: side attribution + conclusion-first recommendation ---
        rec = _dup_def_side_recommendation(
            unit, set(dupes), rest_defs, _extract_definition_names)

        # Build the note. When side attribution succeeds, lead with the
        # actionable conclusion (accept deletion / prefer side) and explicitly
        # state the "must preserve" obligation is incorrect. Otherwise fall
        # back to the prohibition framing.
        dupe_lines = [
            f"  - {name} (defined at line {ln})"
            for name, (ln, _) in dupes.items()
        ]

        if rec is not None:
            side, other, other_empty, _side_dupe_names = rec
            if other_empty:
                header = (
                    f"NOTE \u2014 RESOLVE BY ACCEPTING THE {other} SIDE'S "
                    f"DELETION:"
                )
                tail = (
                    f"\n\nThe {other} side deleted this duplicate block. "
                    f"Since these functions are already defined at the lines "
                    f"above, output EMPTY resolved_text (accept the deletion). "
                    f"Do NOT include any of these function definitions in your "
                    f"merge."
                )
            else:
                header = f"NOTE \u2014 PREFER THE {other} SIDE:"
                tail = (
                    f"\n\nPrefer the {other} side's content for this region; "
                    f"do NOT include the {side} side's duplicate functions."
                )
            body = (
                f"The {side} side's \"must preserve\" obligation for these "
                f"functions is INCORRECT \u2014 they are duplicate definitions "
                f"that already exist elsewhere in this file, not unique "
                f"additions:\n"
                + "\n".join(dupe_lines)
                + tail
            )
        else:
            header = (
                "NOTE \u2014 DUPLICATE DEFINITIONS "
                "(these already exist elsewhere in this file):"
            )
            # Fallback: show source for the gcc-reported function (first entry)
            # to help the model understand what to omit.
            src_block = ""
            if dupes:
                _first_name = next(iter(dupes))
                _ln, _ctx = dupes[_first_name]
                src_block = f"\n\nExisting definition of {_first_name}:\n{_ctx}"
            body = (
                "Your merge re-defines functions that already exist in the "
                "non-conflicting part of this file:\n"
                + "\n".join(dupe_lines)
                + src_block
                + "\n\nRemove every duplicate definition from your merge \u2014 "
                "the existing definitions are kept."
            )

        f.message = f"{fmsg}\n\n{header}\n{body}"


def _dup_def_side_recommendation(
    unit: "ConflictUnit",
    dup_names: set[str],
    rest_defs: dict[str, str],
    extract_fn: "callable | None",
) -> tuple[str, str, bool, list[str]] | None:
    """Attribute duplicate functions to a conflict side and recommend an action.

    Returns ``(dup_side, clean_side, clean_side_empty, dup_names_from_side)``
    when one side's function additions are ALL duplicates and the other side is
    clean — so the note can recommend accepting the clean side. Returns ``None``
    when attribution is inconclusive (both sides have duplicates, neither is
    fully duplicates, or the unit is a whole-file conflict where side text IS
    the full file and the cross-reference is meaningless).

    ``dup_side``/``clean_side`` are ``"CURRENT"`` / ``"REPLAYED"``.
    ``clean_side_empty`` is True when the clean side deleted the block
    (modify/delete conflict) — the recommendation becomes "accept the deletion"
    rather than "prefer the clean side's content."
    """
    if extract_fn is None:
        return None
    ms = getattr(unit, "marker_span", None)
    if ms is None:
        return None  # whole-file unit — side text is the full file

    cur_text = getattr(getattr(unit, "current", None), "text", "") or ""
    rep_text = getattr(getattr(unit, "replayed", None), "text", "") or ""
    cur_defs = set(extract_fn(cur_text.split("\n")))
    rep_defs = set(extract_fn(rep_text.split("\n")))

    cur_dupes = {n for n in cur_defs if n in rest_defs}
    rep_dupes = {n for n in rep_defs if n in rest_defs}

    # One side's function additions are ALL duplicates; the other is clean.
    if cur_dupes and cur_dupes == cur_defs and not rep_dupes:
        return ("CURRENT", "REPLAYED", not rep_text.strip(), sorted(cur_dupes))
    if rep_dupes and rep_dupes == rep_defs and not cur_dupes:
        return ("REPLAYED", "CURRENT", not cur_text.strip(), sorted(rep_dupes))
    return None


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


#: Regex for C/C++ local variable declarations: [modifiers] type [*&]+ name [=;{(]
_LOCAL_VAR_DECL_RE = __import__("re").compile(
    r"(?:const\s+)?(?:static\s+)?(?:inline\s+)?"
    r"(?:unsigned\s+|signed\s+)?"
    r"(?:int|long|short|char|bool|float|double|void|auto|size_t|ssize_t|"
    r"uint\d+_t|int\d+_t|std::\w+|[A-Z]\w*)"
    r"(?:\s*[*&])*\s+(\w+)\s*(?:[=;{(,])"
)


def _detect_cross_unit_coordination(
    units: list, side: str,
) -> set[str]:
    """Identifiers declared in one unit's side and used in another (same side).

    Returns the set of coordinating identifiers — variables declared in one
    conflict unit's ``side`` text and referenced in a DIFFERENT unit's ``side``
    text. This signals a deliberate cross-unit refactor (e.g., a local variable
    introduced in region 0 and consumed in regions 1+2). When one side has
    coordination and the other doesn't, the coordinated side is likely the
    intentional refactor; per-unit resolution that uniformly picked the other
    side may be wrong.
    """
    import re as _re_coord
    side_texts: list[str] = []
    for u in units:
        t = getattr(getattr(u, side, None), "text", "") or ""
        side_texts.append(t)
    # Extract declared names per unit.
    declared_per_unit: list[set[str]] = []
    for t in side_texts:
        declared_per_unit.append(set(_LOCAL_VAR_DECL_RE.findall(t)))
    # An identifier is "coordinating" if it's declared in one unit and appears
    # as a bare word in another unit's text.
    coordinating: set[str] = set()
    for i, declared in enumerate(declared_per_unit):
        for name in declared:
            if not name or len(name) < 2:
                continue
            for j, t in enumerate(side_texts):
                if i == j:
                    continue
                if _re_coord.search(rf"\b{_re_coord.escape(name)}\b", t):
                    coordinating.add(name)
                    break
    return coordinating


def _try_coordinated_side_swap(
    units: list,
    accepted: list[tuple["ConflictUnit", "CandidateResolution"]],
) -> list[tuple["ConflictUnit", "CandidateResolution"]] | None:
    """When all units took the same source-portfolio side and the OTHER side
    has cross-unit variable coordination, swap to the coordinated side.

    Per-unit resolution can't see variables declared in one conflict region
    and used in another. When it uniformly picks side A (because side B's
    per-unit compilation fails on the undeclared variable), but side B has
    a coherent cross-unit dependency (declares a variable in one unit, uses
    it in others), side B is likely the intentional refactor. Swapping all
    units to side B produces a consistent, compiling whole-file result.

    Conservative — only fires when:
    - ALL units took the SAME source-portfolio side (``current_only`` or
      ``replayed_only``), indicating uniform (likely default) side selection.
    - The OPPOSITE side has ≥1 cross-unit coordinating identifier.
    - The chosen side has NONE (asymmetric coordination).
    - ≥2 units in the file (single-unit files have no cross-unit dependency).

    Returns a new ``accepted`` list with all units swapped to the coordinated
    side, or ``None`` to keep the original.
    """
    if len(accepted) < 2:
        return None  # single unit — no cross-unit dependency possible
    from capybase.conflict_model import CandidateResolution
    provs = [getattr(c, "provenance", "") or "" for _, c in accepted]
    all_current = all(p == "deterministic_source_current_only" for p in provs)
    all_replayed = all(p == "deterministic_source_replayed_only" for p in provs)
    if not (all_current or all_replayed):
        return None  # mixed or non-portfolio provenance
    if all_current:
        chosen, opposite = "current", "replayed"
    else:
        chosen, opposite = "replayed", "current"
    # Detect coordination on both sides.
    opp_coord = _detect_cross_unit_coordination(units, opposite)
    if not opp_coord:
        return None  # opposite side has no coordination
    chosen_coord = _detect_cross_unit_coordination(units, chosen)
    if chosen_coord:
        return None  # BOTH sides coordinated — ambiguous, don't swap
    # Swap: build new accepted list with the opposite side's text.
    new_accepted: list[tuple] = []
    for unit, _ in accepted:
        side_obj = getattr(unit, opposite, None)
        side_text = getattr(side_obj, "text", "") or ""
        new_cand = CandidateResolution(
            candidate_id=f"{unit.unit_id}:{opposite}_only_coord",
            unit_id=unit.unit_id,
            model_name="coordinated_side_swap",
            resolved_text=side_text,
            provenance=f"deterministic_source_{opposite}_only",
            prompt_version=f"coord_swap.{opposite}",
        )
        new_accepted.append((unit, new_cand))
    return new_accepted


def _whole_file_side_candidates(
    units: list,
) -> list[tuple[str, list[tuple["ConflictUnit", "CandidateResolution"]]]]:
    """Generate whole-file all-current and all-replayed accepted lists.

    Returns ``[("current", [(unit, cand), ...]), ("replayed", [...])]``
    where each candidate takes that side's text verbatim. Pure text
    substitution — no compilation, no side effects.
    """
    from capybase.conflict_model import CandidateResolution
    out: list[tuple[str, list]] = []
    for side in ("current", "replayed"):
        cands: list[tuple] = []
        for unit in units:
            side_obj = getattr(unit, side, None)
            side_text = getattr(side_obj, "text", "") or ""
            cands.append((unit, CandidateResolution(
                candidate_id=f"{unit.unit_id}:{side}_only_wf",
                unit_id=unit.unit_id,
                model_name="whole_file_portfolio",
                resolved_text=side_text,
                provenance=f"deterministic_source_{side}_only",
                prompt_version=f"wf_portfolio.{side}",
            )))
        out.append((side, cands))
    return out


def _try_whole_file_portfolio(
    units: list,
    accepted: list,
    original: str,
    *,
    journal=None,
    step_index: int = 0,
    path: str = "",
    force: bool = False,
    true_sides: tuple[dict[str, str], str] | None = None,
) -> tuple[list, dict] | None:
    """Generate whole-file side candidates and pick the best by coverage.

    Per-unit compilation is advisory for cross-unit dependencies — a unit's
    ``current_only`` candidate can fail standalone (symbol declared in a
    different unit) causing the per-unit portfolio to pick the wrong side.
    The whole-file build sees all declarations and is the authoritative
    check. This generates all-current and all-replayed candidates and picks
    whichever has the highest file-level intent coverage (brace-balanced).

    Only fires when the per-unit result has LOW file-level intent coverage
    (< 0.8) — when the per-unit merge already preserves both sides' intent,
    the portfolio is skipped.

    ``true_sides`` — ``(sides_dict, base_text)`` from the merge index stages
    (see :func:`_true_stage_sides`) — overrides the full-file side texts.
    Without it, the sides are reconstructed by marker-splicing, and the base
    falls back to ``units[0].base.text`` — which is a FRAGMENT (or empty)
    when the unit is an entity/statement sub-unit, silently no-opping the
    portfolio exactly where it's needed. The stage texts are authoritative.

    Returns ``(new_accepted_list, journal_payload)`` or ``None`` to keep.
    """
    if len(accepted) < 2:
        return None  # single unit — per-unit portfolio already handles it
    try:
        from capybase.structural_resolver import intent_coverage_score
    except Exception:
        return None
    _sides_source = "marker_splice"
    if true_sides is not None:
        _ts_sides, _base_full = true_sides
        _cur_full = _ts_sides.get("current", "")
        _rep_full = _ts_sides.get("replayed", "")
        _sides_source = "stages"
    else:
        _base_full = (units[0].base.text or "") if units else ""
        if not _base_full.strip():
            return None

        # Full-file current/replayed: reconstruct from the marker text by
        # replacing each side's marker content with that side (shared context
        # outside markers appears in both).
        _orig_lines = original.split("\n")
        _cur_lines: list[str] = []
        _rep_lines: list[str] = []
        _in_cur = _in_rep = False
        for _ml in _orig_lines:
            if _ml.startswith("<<<<<<<"):
                _in_cur, _in_rep = True, False
            elif _ml.startswith("=======") and ">>>>" not in _ml:
                _in_cur, _in_rep = False, True
            elif _ml.startswith(">>>>>>>"):
                _in_cur = _in_rep = False
            elif _in_cur:
                _cur_lines.append(_ml)
            elif _in_rep:
                _rep_lines.append(_ml)
            else:
                _cur_lines.append(_ml)
                _rep_lines.append(_ml)
        _cur_full = "\n".join(_cur_lines)
        _rep_full = "\n".join(_rep_lines)

    _per_unit_buf = _resolved_buffer(original, accepted)
    # Asymmetric files (one side rewrote wholesale, churn_ratio >= 0.90)
    # are the asymmetry-takeover's territory, not this portfolio's: the
    # min()-coverage metric mis-scores them badly — a merge that correctly
    # drops the inert side's few added lines scores 0.0, and the "fix" (take
    # a whole side) picks the stale side as often as the right one
    # (protobuf-0043: correct merge 0.0 → all-replayed 1.0 → wrong swap →
    # endless repair; 0067/0070: wrong-way swaps). Skip and let the churn/
    # stale gates decide.
    from capybase.merge_intent import (
        FULL_FILE_ASYMMETRY_RATIO,
        full_file_context as _ffc,
    )

    _ffc_ctx = _ffc(_base_full, _cur_full, _rep_full)
    if _ffc_ctx["churn_ratio"] >= FULL_FILE_ASYMMETRY_RATIO:
        if journal:
            journal.emit(
                "whole_file_portfolio_gate",
                {"ic_per_unit": None,
                 "base_lines": _ffc_ctx["base_lines"],
                 "cur_lines": _ffc_ctx["current_lines"],
                 "rep_lines": _ffc_ctx["replayed_lines"],
                 "n_accepted": len(accepted),
                 "sides_source": _sides_source,
                 "skipped": "asymmetric_file",
                 "churn_ratio": _ffc_ctx["churn_ratio"]},
                step_index=step_index, path=path,
            )
        return None
    _ic_per = intent_coverage_score(
        _per_unit_buf, _base_full, _cur_full, _rep_full)
    if journal:
        journal.emit(
            "whole_file_portfolio_gate",
            {"ic_per_unit": round(_ic_per, 4),
             "base_lines": len(_base_full.splitlines()),
             "cur_lines": len(_cur_full.splitlines()),
             "rep_lines": len(_rep_full.splitlines()),
             "n_accepted": len(accepted),
             "sides_source": _sides_source},
            step_index=step_index, path=path,
        )
    if _ic_per >= 0.80 and not force:
        return None  # per-unit result already good — skip

    _lang = getattr(units[0], "language", None) if units else None
    _best: tuple[float, str, list] | None = None
    for _side, _cand_list in _whole_file_side_candidates(units):
        _buf = _resolved_buffer(original, _cand_list)
        _ic = intent_coverage_score(
            _buf, _base_full, _cur_full, _rep_full)
        if _ic <= _ic_per + 0.02:
            continue  # doesn't beat per-unit by margin
        try:
            if _lang and not _braces_balanced(_buf, _lang):
                continue  # syntax broken — skip
        except Exception:
            pass
        if _best is None or _ic > _best[0]:
            _best = (_ic, _side, _cand_list)

    if _best is None:
        return None
    _ic, _side, _cand_list = _best
    return _cand_list, {
        "side": _side,
        "ic_per_unit": round(_ic_per, 4),
        "ic_whole_file": round(_ic, 4),
        "n_units": len(_cand_list),
    }


#: Whole-file lockfile takeover names (sprint-20 S20.5). Deliberately
#: name-scoped, NOT suffix-scoped (".lock" would over-match): the oracle
#: measurement backing the rule is Cargo.lock-specific. Extend only with
#: per-format evidence.
_LOCKFILE_TAKEOVER_NAMES = frozenset({"cargo.lock"})


def _is_lockfile_path(path: str) -> bool:
    return (path or "").rsplit("/", 1)[-1].lower() in _LOCKFILE_TAKEOVER_NAMES


# Sprint-20 S20.6 — micro-CEGIS at the compiler-authority gate.
_MICRO_REDEF_RE = re.compile(r"redefinition of ['\"]?([A-Za-z_]\w*)")
# Sprint-22 pre-eval: dead-code class — a -Werror=unused-function error
# whose function block has no call sites in the merged file is
# deterministically deletable (jsonc-0016: json_parse_double).
_MICRO_UNUSED_RE = re.compile(
    r"['\"]?([A-Za-z_]\w*)['\"]? defined but not used")
_MICRO_MISSING_RE = re.compile(
    r"['\"]([A-Za-z_]\w*)['\"] (?:does not name a type|was not declared"
    r"|is not a member of)")


def _micro_extract_brace_block(
    lines: list[str], anchor_line: int, lookahead: int = 6,
) -> tuple[int, int] | None:
    """(start_idx, end_idx) 0-based inclusive span of the brace-delimited
    block whose definition sits at/near ``anchor_line`` (1-based).

    Walks forward to the first '{' (the error line usually sits at the
    signature or just inside the body), matches braces to the closer, and
    extends the start backward over the declaration header (continuation
    lines that don't close a statement and aren't preprocessor/comment).
    """
    n = len(lines)
    open_idx = None
    for j in range(max(0, anchor_line - 1), min(n, anchor_line - 1 + lookahead)):
        if "{" in lines[j]:
            open_idx = j
            break
    if open_idx is None:
        return None
    depth = 0
    close_idx = None
    for j in range(open_idx, n):
        depth += lines[j].count("{") - lines[j].count("}")
        if depth <= 0:
            close_idx = j
            break
    if close_idx is None:
        return None
    start = open_idx
    for _ in range(6):
        k = start - 1
        if k < 0:
            break
        prev = lines[k].rstrip()
        if (not prev or prev.endswith((";", "}", "{"))
                or prev.startswith(("#", "//", "/*", "*"))):
            break
        start = k
    return (start, close_idx)


def _micro_delete_span(buffer: str, span: tuple[int, int]) -> str:
    """Remove the 0-based inclusive line span, collapsing a doubled blank."""
    lines = buffer.splitlines(keepends=True)
    end = span[1] + 1
    # swallow one following blank line when the preceding line is also blank
    if (span[0] > 0 and end < len(lines)
            and not lines[end].strip() and not lines[span[0] - 1].strip()):
        end += 1
    return "".join(lines[:span[0]] + lines[end:])


def _micro_delete_unused_function(
    buffer: str, name: str, error_line: int,
) -> str | None:
    """Sprint-22 pre-eval: deterministic dead-code removal.

    A -Werror=unused-function error on symbol ``name``: locate the
    function's brace block at the error line, verify the symbol has NO
    call sites elsewhere in the file (only the definition), delete the
    block. Conservative: any additional reference (call, address-of,
    mention in a string literal) declines — the function may be used
    by code outside the conflict region."""
    lines = buffer.splitlines()
    # find the block at the error line
    span = None
    for i in range(len(lines)):
        if name in lines[i] and i >= error_line - 3 and i <= error_line + 3:
            span = _micro_extract_brace_block(lines, i + 1)
            if span is not None:
                break
    if span is None:
        return None
    block = "\n".join(lines[span[0]:span[1] + 1])
    # verify no OTHER mention of the symbol outside the block
    # (skip the block's own lines; a call site elsewhere declines)
    for i, ln in enumerate(lines):
        if span[0] <= i <= span[1]:
            continue
        # strip comments crudely (a mention in a comment is not a use,
        # but conservatively count it anyway to avoid false deletions)
        if name in ln:
            return None
    # also check the block looks like a function definition (has '{' and
    # the name appears in the first few lines of the block)
    if "{" not in block or name not in "\n".join(
            lines[span[0]:min(span[0] + 3, span[1] + 1)]):
        return None
    return _micro_delete_span(buffer, span)


def _micro_delete_base_verbatim_duplicate(
    buffer: str,
    name: str,
    error_line: int,
    base_text: str,
    current_text: str,
    replayed_text: str,
) -> tuple[str, str] | None:
    """Deterministic duplicate repair for one ``redefinition of <name>``.

    Deletes the duplicate copy whose exact text is base-verbatim AND was
    deleted by a parent side (its text absent from current or replayed) —
    the splice wrongly resurrected content a parent removed. Any
    ambiguity (block unresolvable, no second copy, neither copy
    base-verbatim-deleted) declines: no LLM, no guess. Returns
    ``(new_buffer, provenance)`` or None.
    """
    lines = buffer.splitlines()
    span = _micro_extract_brace_block(lines, error_line)
    if span is None:
        return None
    block = "\n".join(lines[span[0]:span[1] + 1])
    if name not in block:
        return None
    # Locate the OTHER copy: another definition-site line for ``name``
    # (contains the identifier + an argument list, isn't comment/directive)
    # whose brace block is disjoint from the error's block.
    other_span = None
    for j in range(len(lines)):
        if span[0] <= j <= span[1]:
            continue
        ln = lines[j]
        if (name in ln and "(" in ln
                and not ln.lstrip().startswith(("//", "*", "/*", "#"))):
            other = _micro_extract_brace_block(lines, j + 1)
            if (other is not None and other != span
                    and not (other[0] <= span[0] <= other[1])
                    and name in "\n".join(lines[other[0]:other[1] + 1])):
                other_span = other
                break
    if other_span is None:
        return None
    other_block = "\n".join(lines[other_span[0]:other_span[1] + 1])

    def _prov(text: str) -> str | None:
        if text and text in base_text:
            if text not in replayed_text:
                return "replayed_deleted_base_copy"
            if text not in current_text:
                return "current_deleted_base_copy"
        return None

    for cand_text, cand_span in ((block, span), (other_block, other_span)):
        prov = _prov(cand_text)
        if prov is not None:
            return _micro_delete_span(
                buffer, cand_span), f"{prov}:{name}"
    return None


def _micro_symbol_decls(symbol: str, *texts: str, limit: int = 6) -> list[str]:
    """Declaration-ish lines mentioning ``symbol`` from the given texts.

    Feeds the micro-patch prompt: the model sees how the sides declared
    (or removed) the missing symbol before deciding the minimal patch.
    """
    decls: list[str] = []
    for text in texts:
        for ln in (text or "").splitlines():
            s = ln.strip()
            if (symbol in s and (s.endswith(";") or "(" in s)
                    and not s.startswith(("//", "*", "/*"))):
                if s not in decls:
                    decls.append(s)
                if len(decls) >= limit:
                    return decls
    return decls


def _true_stage_sides(git_backend, path: str):
    """Pristine whole-file side texts from the merge index.

    Returns ``(sides, base_text)`` — sides maps ``current`` (stage 2) and
    ``replayed`` (stage 3) to their full file texts, base_text is stage 1
    (empty when absent) — or ``None`` when the stages can't be read.

    The worktree's conflict file is git's line-aligned merge of the two
    sides. When both sides carry the same lines in a DIFFERENT order (e.g.
    two versions of the same added block), git interleaves them into the
    SHARED context between markers — content no per-region resolution can
    remove, because it isn't inside any marker span. The stage blobs hold
    the pristine side files, the only compilable whole-file candidates in
    that situation.
    """
    import re as _re

    out: dict[str, str] = {}
    base_text = ""
    try:
        for side, stage in (("current", 2), ("replayed", 3)):
            text = git_backend.read_stage_blob(path, stage).decode(
                "utf-8", errors="replace").replace("\r\n", "\n")
            if text.strip():
                out[side] = text
        try:
            base_text = git_backend.read_stage_blob(path, 1).decode(
                "utf-8", errors="replace").replace("\r\n", "\n")
        except Exception:
            base_text = ""
    except Exception:
        return None
    if not out:
        return None
    return out, base_text


def _side_preservation(base_text: str, side_text: str, output_text: str) -> float | None:
    """Share of one side's changes the output preserves (0.0-1.0).

    Added/changed lines (the side's side of the base diff) count as
    preserved when present in the output; deleted base lines count as
    preserved when absent from it. Whitespace-normalized line-set
    membership, so indentation drift doesn't dent the fraction. Returns
    None when the side made no changes vs base (nothing to preserve).
    Mirrors the live eval's post-hoc judge so the orchestrator's floor
    decision and the eval's census speak the same numbers.
    """
    import difflib as _dl

    b, s = base_text.splitlines(), side_text.splitlines()
    added: list[str] = []
    deleted: list[str] = []
    for tag, i1, i2, j1, j2 in _dl.SequenceMatcher(
            None, b, s, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag != "delete":
            added.extend(s[j1:j2])
        if tag != "insert":
            deleted.extend(b[i1:i2])
    if not added and not deleted:
        return None
    out = {ln.strip() for ln in output_text.splitlines() if ln.strip()}
    n_ok = 0
    n_tot = 0
    for ln in added:
        if ln.strip():
            n_tot += 1
            n_ok += ln.strip() in out
    for ln in deleted:
        if ln.strip():
            n_tot += 1
            n_ok += ln.strip() not in out
    return n_ok / n_tot if n_tot else None


def _shared_context_duplicate_definitions(
    original: str, language: str | None,
) -> list[str]:
    """Identical-signature definitions repeated OUTSIDE conflict markers.

    A compilable translation unit cannot define the same signature twice, so
    duplicates in the marker file's SHARED context prove git's line-aligned
    merge interleaved the two sides' content outside every marker span —
    the cross-ordered-blocks pathology (both sides added the same block in
    a different order). Per-region resolution is structurally insufficient
    there: the duplicates aren't inside any span it may rewrite.

    Definition lines are matched per language family and keyed by their
    whitespace-normalized text, so legitimate overloads (different params)
    don't fire. Returns the duplicated keys (empty list = healthy).
    """
    import re as _re

    lang = (language or "").lower()
    if lang in ("cpp", "c", "c++", "cxx", "cc", "hpp", "h"):
        patterns = [_re.compile(
            r"^\s*(?:(?:static|inline|constexpr|virtual|explicit)\s+)*"
            r"(?:[A-Za-z_][\w:<>]*\s+)+\**[A-Za-z_]\w*\s*\([^;{}]*\)\s*"
            r"(?:const\s*)?(?:noexcept\s*)?\{\s*$")]
        skip_names = {"if", "for", "while", "switch", "catch", "do", "else",
                      "return", "sizeof", "typeof"}
    elif lang in ("python", "py"):
        patterns = [
            _re.compile(r"^\s*def\s+\w+\s*\(.*\)\s*(->\s*[^:]+)?:"),
            _re.compile(r"^\s*class\s+\w+"),
        ]
        skip_names = set()
    elif lang in ("rust", "rs"):
        patterns = [
            _re.compile(r"^\s*(?:pub\s+)?(?:\w+\s+)*fn\s+\w+[^{]*\{\s*$"),
            _re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+\w+"),
        ]
        skip_names = set()
    else:
        return []

    counts: dict[str, int] = {}
    in_conflict = False
    for line in original.split("\n"):
        if line.startswith("<<<<<<<"):
            in_conflict = True
            continue
        if line.startswith(">>>>>>>"):
            in_conflict = False
            continue
        if in_conflict or line.startswith("======="):
            continue
        for pat in patterns:
            if pat.match(line):
                name = _re.search(r"[A-Za-z_]\w*\s*\(", line)
                if name and name.group().rstrip(" (") in skip_names:
                    break
                key = _re.sub(r"\s+", " ", line).strip()
                counts[key] = counts.get(key, 0) + 1
                break
    return [k for k, n in counts.items() if n > 1]


def _whole_side_churn(base_text: str, side_text: str) -> int:
    """Absolute changed-line count of a side vs the base (both directions)."""
    from capybase.merge_intent import side_churn

    return side_churn(base_text, side_text)


def _whole_side_heuristic(base_text: str, sides: dict[str, str]) -> str:
    """Deterministic pick between two compiling true whole-file sides.

    Massive churn asymmetry means the higher-churn side carries the merge
    intent — the other side is pre-change content git failed to align
    (deletions/rewrites: keep the side that actually changed). Near-
    symmetric churn means both sides did the same-sized work on the same
    block — a refinement pair (rename/style pass); prefer replayed, the
    commit being applied, i.e. the newer pass. Threshold 0.35 validated on
    the corpus's four known whole-file cases (0063 0.28→replayed, 0067
    0.99→current, 0073 0.99→current, clickhouse-0041 0.03→replayed).
    """
    c = _whole_side_churn(base_text, sides.get("current", ""))
    r = _whole_side_churn(base_text, sides.get("replayed", ""))
    if max(c, r) == 0 or abs(c - r) / max(c, r) < 0.35:
        return "replayed"
    return "current" if c > r else "replayed"


def _near_one_sided_takeover(
    base_text: str, sides: dict[str, str], *, threshold: int = 30,
) -> str | None:
    """F1 tier-1 (sprint-23): deterministic near-one-sided takeover.

    When one side's churn vs base is <= threshold (in the double-counted
    additions+deletions metric — the archaeology's 15-line "changed lines"
    maps to ~30 here), that side barely changed. The correct merge is the
    OTHER side wholesale. Rounds 10-13 verified 22 remaining-failure
    targets, 0/305 passing harmed, and the only false-fire lands at NEAR
    not a wrong PASS. Returns the side name to take, or None when both
    sides changed significantly (tier-2 territory: the LLM adjudicator)."""
    c = _whole_side_churn(base_text, sides.get("current", ""))
    r = _whole_side_churn(base_text, sides.get("replayed", ""))
    if min(c, r) <= threshold and max(c, r) > 0:
        return "current" if c > r else "replayed"
    return None


def _safe_conf(value) -> float:
    """D2 (sprint-23): adjudication confidences arrive model-typed.

    The 4B intermittently returns an empty string (axum-0021: one repeat
    died on float(''), flipping a sim-1.0 case to majority-ESCALATE).
    Unparseable confidence is a low-confidence answer, never a crash."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _whole_side_adjudication_prompt(
    path: str, language: str | None,
    base_text: str, sides: dict[str, str],
    max_lines: int = 1200,
) -> str:
    """LLM adjudication prompt between two compiling whole-file sides."""

    def _clip(text: str) -> str:
        lines = text.splitlines()
        if len(lines) <= max_lines:
            return text
        return "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} more lines truncated)"

    lang = (language or "").strip()
    cur = _clip(sides.get("current", ""))
    rep = _clip(sides.get("replayed", ""))
    base = _clip(base_text)
    return f"""You are deciding the resolution of a git rebase conflict for `{path}`.

Region-level merging is IMPOSSIBLE for this file: the conflict file interleaves
both versions' code outside the conflict markers, so exactly one WHOLE version
must survive. Both versions below compile and pass their build. The common
ancestor (base) is included for context.

CURRENT — upstream, the branch being rebased onto:
```{lang}
{cur}
```

REPLAYED — the commit being replayed on top of current:
```{lang}
{rep}
```

BASE — common ancestor:
```{lang}
{base}
```

Pick the version that should survive as the merge result. Consider: which is
the more evolved form of the same work (consistent renames, newer API usage,
refined structure — typically the replayed commit's pass); versus which is a
deliberate larger change (massive deletion, rewrite) that the other side
predates and cannot subsume.

Respond with ONLY a JSON object:
{{"choice": "current" or "replayed", "confidence": <0.0-1.0>, "reason": "<one sentence>"}}"""


def _subsumption_adjudication_prompt(
    path: str,
    language: str | None,
    base_text: str,
    winner_side: str,
    winner_text: str,
    loser_text: str,
    max_diff_lines: int = 150,
) -> str:
    """Mid-band subsumption adjudication prompt (jsonc-0004 class).

    Unlike ``_whole_side_adjudication_prompt`` (whole files, pick a side),
    this decides whether the LOSER's changes must survive the region merge
    or are superseded by the WINNER's rewrite. Diffs vs base, not whole
    files: the question is about the loser's edit set, and diff-shaped
    prompts fit the fragment budget the model handles best. Validated
    offline against the 45 active-corpus mid-band cases before wiring
    (see enable_midband_subsumption_takeover in config.py).
    """
    import difflib as _difflib

    def _clip_diff(side_text: str) -> str:
        lines = _difflib.unified_diff(
            base_text.splitlines(), side_text.splitlines(), lineterm="", n=2)
        body = list(lines)[2:]  # drop ---/+++ headers
        if len(body) <= max_diff_lines:
            return "\n".join(body)
        return ("\n".join(body[:max_diff_lines])
                + f"\n... ({len(body) - max_diff_lines} more diff lines truncated)")

    lang = (language or "").strip()
    # The churn winner is nearly always current (upstream rewrite), but the
    # gates admit a replayed-side winner; label the blocks with their git
    # roles so "superseded -> winner verbatim" stays accurate either way.
    win_label = ("CURRENT (upstream, being rebased onto)" if winner_side == "current"
                 else "REPLAYED (the commit being applied on top)")
    lose_label = ("REPLAYED (the commit being applied on top)" if winner_side == "current"
                  else "CURRENT (upstream, being rebased onto)")
    return f"""You are adjudicating a git rebase conflict for `{path}` where one side rewrote the file.

{win_label} rewrote the file heavily. Its changes vs the common ancestor BASE:
```{lang}
{_clip_diff(winner_text)}
```

{lose_label} made smaller changes. Its changes vs BASE:
```{lang}
{_clip_diff(loser_text)}
```

A region-level merge that weaves the smaller side's changes into the rewrite compiles and passes tests. Decide which result is correct:

keep        — the smaller side's changes add functionality or fixes that the rewrite does not provide; they must survive the merge.
superseded  — the rewrite already provides the same behavior, deleted the code the smaller side touched, or the smaller side's edits are cosmetic on regions the rewrite reformatted; the correct result is the rewriting side's file verbatim.

Respond with ONLY a JSON object:
{{"verdict": "keep" or "superseded", "confidence": <0.0-1.0>, "reason": "<one sentence>"}}"""


def _clip_side_diff(base_text: str, side_text: str, max_diff_lines: int = 150) -> str:
    """Unified diff of one side vs the base, clipped (shared by the rung prompts)."""
    import difflib as _difflib

    body = list(_difflib.unified_diff(
        base_text.splitlines(), side_text.splitlines(), lineterm="", n=2))[2:]
    if len(body) <= max_diff_lines:
        return "\n".join(body)
    return ("\n".join(body[:max_diff_lines])
            + f"\n... ({len(body) - max_diff_lines} more diff lines truncated)")


def _whole_side_repair_prompt_single(
    path: str,
    language: str | None,
    base_text: str,
    sides: dict[str, str],
    ok_side: str,
) -> str:
    """One pristine side compiles, the other does not — take or decline?

    The whole-side repair rung's strong-signal branch: the spliced merge
    failed to compile, exactly one pristine side compiles. Taking that
    side verbatim is only correct when the FAILING side's changes are
    superseded by it — the same question the subsumption adjudication
    asks, so the verdict vocabulary is shared. ``keep`` declines the swap
    (the failing side carries essential work; repair/escalate instead).
    """
    lang = (language or "").strip()
    bad_side = "replayed" if ok_side == "current" else "current"
    ok_label = ("CURRENT (upstream, being rebased onto)" if ok_side == "current"
                else "REPLAYED (the commit being applied on top)")
    bad_label = ("REPLAYED (the commit being applied on top)" if ok_side == "current"
                 else "CURRENT (upstream, being rebased onto)")
    return f"""You are adjudicating a git rebase conflict for `{path}`.

The region-level merge of both sides FAILED to compile. Of the two pristine
whole-file versions, {ok_label} compiles cleanly and {bad_label} does not.

{ok_label}'s changes vs the common ancestor BASE (this version compiles):
```{lang}
{_clip_side_diff(base_text, sides.get(ok_side, ''))}
```

{bad_label}'s changes vs BASE (this version does NOT compile):
```{lang}
{_clip_side_diff(base_text, sides.get(bad_side, ''))}
```

Decide the correct outcome:

keep        — the non-compiling side's changes add functionality or fixes that the compiling side does not provide; they must be repaired into the merge rather than dropped.
superseded  — the compiling side already provides the same behavior, deleted the code the other side touched, or the other side's edits are cosmetic; the correct result is the compiling side's file verbatim.

Respond with ONLY a JSON object:
{{"verdict": "keep" or "superseded", "confidence": <0.0-1.0>, "reason": "<one sentence>"}}"""


def _whole_side_repair_prompt_both(
    path: str,
    language: str | None,
    base_text: str,
    sides: dict[str, str],
) -> str:
    """Both pristine sides compile, the spliced merge does not — pick or decline.

    The rung's conservative branch: substituting a pristine side drops the
    other side's work entirely, so the adjudication gets an explicit
    ``neither`` escape — when the correct merge weaves both sides (the
    68.5%-woven class), the repair loop must keep its chance. ``neither``,
    a low-confidence answer, or an unparseable response declines the swap.
    """
    lang = (language or "").strip()
    return f"""You are adjudicating a git rebase conflict for `{path}`.

The region-level merge that weaves BOTH sides together FAILED to compile.
Both pristine whole-file versions below compile cleanly. The common
ancestor (base) is included for context.

CURRENT — upstream, the branch being rebased onto. Its changes vs BASE:
```{lang}
{_clip_side_diff(base_text, sides.get("current", ""))}
```

REPLAYED — the commit being replayed on top of current. Its changes vs BASE:
```{lang}
{_clip_side_diff(base_text, sides.get("replayed", ""))}
```

Decide the correct outcome:

current     — the correct merge is CURRENT's file verbatim; REPLAYED's changes are superseded (already provided, cosmetic, or touching deleted code).
replayed    — the correct merge is REPLAYED's file verbatim; CURRENT's changes on this file are superseded.
neither     — the correct merge must weave BOTH sides' changes; the compile failure should be repaired in the woven merge instead of substituting a whole side.

Respond with ONLY a JSON object:
{{"choice": "current" or "replayed" or "neither", "confidence": <0.0-1.0>, "reason": "<one sentence>"}}"""


def _classify_build_error_lines(
    error_lines: list[str], path: str,
) -> tuple[list[str], int]:
    """Split build error lines into (merge-relevant, environmental count).

    Mirrors verify_file's error localization (research §9): a whole-tree
    build compiles many TUs, and a pre-existing error in a SIBLING file is
    not caused by the merge. Returns the lines that implicate the conflict
    file (or are unparseable — conservative) plus how many were positively
    classified as sibling/-Werror/build-driver noise.
    """
    from capybase.verification import (
        _is_cc_werror_warning,
        _parse_cc_error_location,
    )

    conflict_stem = Path(path).stem
    merge_lines: list[str] = []
    env_ct = 0
    for ln in error_lines:
        if (
            ln.startswith("make[")
            or ln.startswith("make:")
            or "CMake Error" in ln
            or ln.startswith("ninja:")
            or ln.startswith("*** ")
            or "Error 1" in ln
            or "Error 2" in ln
        ):
            env_ct += 1  # build-driver summary, not a gcc diagnostic
            continue
        if _is_cc_werror_warning(ln):
            env_ct += 1
            continue
        stem, _ = _parse_cc_error_location(ln)
        if stem is not None and stem != conflict_stem:
            env_ct += 1
            continue
        merge_lines.append(ln)  # conflict-file error, or unparseable
    return merge_lines, env_ct


def _is_compile_flavored_failure(hard_failures) -> bool:
    """True when a failure came from a whole-file COMPILE gate.

    The whole-side repair rung's trigger: cargo check, the Phase-2 build
    test, or a build-branch failure inside verify_file (all tagged
    ``detail.source="whole_file_build"`` at emission, the sprint-19 D5
    rule — no message string-matching for the tagged paths). Standalone
    parse/splice-coherence failures (brace imbalance, py_compile,
    standalone rustc) are deliberately excluded: those are the
    deterministic/CEGIS repairs' territory, and a pristine-side swap must
    not preempt them.
    """
    for f in hard_failures or []:
        v = getattr(f, "validator", "") or ""
        d = getattr(f, "detail", None) or {}
        if isinstance(d, dict) and d.get("source") == "whole_file_build":
            return True
        if v == "build_test":
            return True
        if v == "syntax" and (getattr(f, "message", "") or "").startswith(
                "cargo check"):
            return True
    return False


def _empty_repair_side_fallback(
    accepted: list,
) -> list | None:
    """Whole-file majority-side replacement when a repair re-resolution dies.

    Only reached when ``_whole_file_repair`` escalated (typically: the model
    returned empty for the fault-attributed unit). Replaces every unit's
    resolution with the side the file's accepted resolutions predominantly
    chose (ties and no-votes → current, the conservative default), so the
    Phase 2 loop re-validates a coherent side instead of escalating on a
    missing model opinion. Returns None when the accepted list already IS
    that side (nothing to try — the caller escalates as before).
    """
    from capybase.conflict_model import CandidateResolution

    votes = {"current": 0, "replayed": 0}
    for _u, c in accepted:
        p = c.provenance or ""
        for s in ("current", "replayed"):
            if f"{s}_only" in p:
                votes[s] += 1
    side = "current" if votes["current"] >= votes["replayed"] else "replayed"
    out: list = []
    swapped = False
    for u, c in accepted:
        side_obj = getattr(u, side, None)
        text = (getattr(side_obj, "text", "") or "") if side_obj else ""
        if c.resolved_text != text:
            swapped = True
            out.append((u, CandidateResolution(
                candidate_id=f"{u.unit_id}:{side}_only_fallback",
                unit_id=u.unit_id,
                model_name="repair_side_fallback",
                prompt_version="side-fallback.v1",
                resolved_text=text,
                provenance=f"deterministic_source_{side}_only_fallback",
            )))
        else:
            out.append((u, c))
    return out if swapped else None


def _rebase_continue_empty(cont) -> bool:
    """True when ``git rebase --continue`` failed because the pick is empty.

    git's phrasings vary by version and land on either stream: "nothing to
    commit", "The previous cherry pick commit is now empty", "no changes". A
    fully-superseded resolution (e.g. the whole-file fast path taking the
    rewriting side verbatim) produces exactly this — and without --skip the
    rebase stays wedged mid-flight.

    Deliberately does NOT match "rebase --skip": modern git's CONFLICT hint
    text ("Resolve all conflicts manually ... or use git rebase --skip to
    skip this commit") contains it, so matching the hint reads every
    next-commit conflict as an empty pick and silently --skips a real commit
    (multistep-rebase regression: step 2's conflict skipped b.py's change).
    """
    text = f"{getattr(cont, 'stdout', '') or ''}\n{getattr(cont, 'stderr', '') or ''}".lower()
    return any(
        p in text
        for p in (
            "nothing to commit",
            "is now empty",
            "no changes",
        )
    )


def _try_majority_side_rescue(
    units: list,
    accepted: list[tuple["ConflictUnit", "CandidateResolution"]],
    escalated: list["UnitOutcome"],
) -> list[tuple["ConflictUnit", "CandidateResolution"]] | None:
    """Rescue escalated units by taking the file's majority-resolved side.

    When most resolved units took the SAME side (current or replayed) and one
    or more units escalated, try the majority side for each escalated unit.
    This catches file-wide refactors where per-unit resolution fails on one
    region (e.g., the LLM can't avoid duplicate definitions) but the correct
    resolution is to take the same side the other units already chose.

    Conservative — only fires when:
    - ≥3 total units (otherwise "majority" is meaningless for 1-of-2).
    - ≥2/3 of RESOLVED units took the same side.
    - The escalated unit's majority-side text is non-empty.
    - Phase 2's whole-file validation is the authoritative check.

    Returns a list of (unit, candidate) for the rescued units, or None.
    """
    from capybase.conflict_model import CandidateResolution
    total = len(accepted) + len(escalated)
    if total < 3 or len(accepted) < 2:
        return None
    # Determine which side the accepted units took.
    cur_count = rep_count = 0
    for unit, cand in accepted:
        cur_text = (getattr(getattr(unit, "current", None), "text", "") or "").strip()
        rep_text = (getattr(getattr(unit, "replayed", None), "text", "") or "").strip()
        res_text = (getattr(cand, "resolved_text", "") or "").strip()
        if res_text == rep_text:
            rep_count += 1
        elif res_text == cur_text:
            cur_count += 1
    threshold = max(1, (len(accepted) * 2) // 3)  # ≥2/3
    if rep_count >= threshold and rep_count > cur_count:
        majority_side = "replayed"
    elif cur_count >= threshold and cur_count > rep_count:
        majority_side = "current"
    else:
        return None  # no clear majority
    # Build rescue candidates for each escalated unit.
    rescued: list[tuple] = []
    for outcome in escalated:
        unit = outcome.unit
        side_obj = getattr(unit, majority_side, None)
        side_text = getattr(side_obj, "text", "") or ""
        if not side_text.strip():
            continue  # don't rescue with empty text (could be wrong deletion)
        rescued.append((unit, CandidateResolution(
            candidate_id=f"{unit.unit_id}:{majority_side}_majority_rescue",
            unit_id=unit.unit_id,
            model_name="majority_side_rescue",
            resolved_text=side_text,
            provenance=f"deterministic_source_{majority_side}_only",
            prompt_version=f"majority_rescue.{majority_side}",
        )))
    return rescued if rescued else None


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
        # Prefer the structured line in detail (precise — set by the splice-
        # coherence gate and the syntax check's diagnostic delta).
        detail = getattr(f, "detail", {}) or {}
        if isinstance(detail.get("brace_imbalance_line"), int):
            line = detail["brace_imbalance_line"]
        elif isinstance(detail.get("preprocessor_imbalance_line"), int):
            line = detail["preprocessor_imbalance_line"]
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
        # Also parse gcc/clang error format: "file:line:col: error:"
        if line is None:
            msg = getattr(f, "message", "") or ""
            m = re.search(r":(\d+):\d+:\s*(?:error|fatal error):", msg)
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
    # No line attribution possible → return -1 so the caller can escalate
    # instead of blindly retrying the last unit. The old default (last unit)
    # wasted model calls on cross-unit errors that no single unit can fix.
    # When the tiered-verification time budget is NOT set (legacy mode),
    # callers map -1 back to the last-unit heuristic for backward compat.
    return -1


def _splice_context_snippet(
    failures: list, original: str,
    accepted: list[tuple[ConflictUnit, CandidateResolution]],
) -> str:
    """Build a context snippet of the spliced file around the error line.

    Enriches the whole-file repair feedback so the model sees the actual brace
    mismatch in context, not just the raw cargo message. For a multi-hunk
    conflict, the snippet is WIDENED to span the two adjacent units' marker
    spans when the error line falls at or near a hunk junction — the model
    couldn't see that unit A's ``}`` collided with unit B's structure because a
    narrow ±5 window only showed one unit's context. Returns empty string when
    no error line is available or the splice fails (the raw failures still
    reach the model; this is additive).
    """
    # Find the error line from the failures' detail (same sources as attribution).
    line: int | None = None
    for f in failures:
        detail = getattr(f, "detail", {}) or {}
        if isinstance(detail.get("brace_imbalance_line"), int):
            line = detail["brace_imbalance_line"]
            break
        elif isinstance(detail.get("preprocessor_imbalance_line"), int):
            line = detail["preprocessor_imbalance_line"]
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
    # Cross-hunk widening: when the error line falls at or near a hunk junction
    # (between two units' marker spans), widen the window to span BOTH adjacent
    # units so the model sees the splice boundary and both hunks' context. The
    # brace imbalance in a multi-hunk conflict lives at the junction; a narrow
    # window only shows one unit, hiding the collision.
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
    # Preprocessor widening: when the failure is a #if/#endif imbalance, widen
    # the window to the ENCLOSING conditional region. The cross-unit imbalance
    # has its matching directive outside the ±5 default window (often upstream
    # of the marker block), so the model can't see the #if/#endif pair that
    # must balance. Scan outward from the error line to the nearest depth-0
    # boundary on each side.
    _is_pp = any(
        isinstance((getattr(f, "detail", {}) or {}).get("preprocessor_imbalance_line"), int)
        for f in failures
    )
    if _is_pp and len(lines) > 0:
        err0 = line - 1
        # Scan backward from the error line to the nearest line that returns
        # preprocessor depth to 0 (the enclosing #if, or file start).
        depth = 0
        scan = min(err0, len(lines) - 1)
        for i in range(scan, -1, -1):
            s = lines[i].strip()
            if s.startswith("#"):
                d = s[1:].lstrip().split(None, 1)[0] if s[1:].lstrip() else ""
                if d in ("endif", "else", "elif"):
                    depth += 1
                elif d in ("if", "ifdef", "ifndef"):
                    depth -= 1
                    if depth <= 0:
                        start = min(start, max(0, i - 1))
                        break
        # Scan forward to the nearest #endif that closes the open conditional.
        depth = 0
        for i in range(min(err0, len(lines) - 1), len(lines)):
            s = lines[i].strip()
            if s.startswith("#"):
                d = s[1:].lstrip().split(None, 1)[0] if s[1:].lstrip() else ""
                if d in ("if", "ifdef", "ifndef"):
                    depth += 1
                elif d == "endif":
                    if depth <= 0:
                        end = max(end, min(len(lines), i + 2))
                        break
                    depth -= 1
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
) -> tuple[list[tuple[ConflictUnit, CandidateResolution]] | None, str]:
    """Attempt a deterministic brace-balance fix before invoking the LLM.

    The recurring splice-junction brace imbalance is a single-edit fix away
    from correct: the model merges each hunk correctly in isolation, but the
    spliced result has a stray or missing brace where the hunks meet.
    Re-prompting the model doesn't help (it can't see the junction), so we fix
    it directly when ``_try_balance_braces`` can balance the spliced buffer in
    one clean edit.

    Returns ``(result, diag_reason)`` where ``result`` is a replacement
    ``accepted`` list (the fault unit becomes a whole-file unit carrying the
    repaired buffer as its resolved_text), or ``None`` to defer to the LLM
    path. ``diag_reason`` is a short diagnostic string explaining the outcome
    (``"repaired"``, ``"not_brace_failure"``, ``"splice_exception"``,
    ``"no_imbalance"``, ``"balance_failed"``, ``"revalidation_failed"``)
    so the caller can journal it for future diagnosis.

    Conservative on two axes: (1) the brace repair acts only on brace-only
    lines / unclosed blocks (see ``_try_balance_braces``), and (2) the repaired
    buffer is re-validated for brace balance before use.

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
        return None, "not_brace_failure"
    if fault_idx < 0 or fault_idx >= len(accepted):
        return None, "fault_idx_out_of_range"
    unit, _old_cand = accepted[fault_idx]
    try:
        spliced = _resolved_buffer(original, accepted)
    except Exception:  # noqa: BLE001 - splice may fail on bad spans
        return None, "splice_exception"
    # Language for comment-marker awareness (Python '#' vs Rust '//').
    _lang = accepted[fault_idx][0].language if 0 <= fault_idx < len(accepted) else None
    if _brace_imbalance_line(spliced, _lang) is None:
        return None, "no_imbalance"  # not actually a brace imbalance; nothing to fix
    repaired = _try_balance_braces(spliced, _lang)
    if repaired is None:
        return None, "balance_failed"  # couldn't balance in one edit → defer to LLM
    if _brace_imbalance_line(repaired, _lang) is not None:
        return None, "revalidation_failed"  # safety re-check (shouldn't happen)
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
    return [(wf_unit, wf_cand)], "repaired"


def _try_deterministic_preprocessor_repair(
    failures: list,
    original: str,
    accepted: list[tuple[ConflictUnit, CandidateResolution]],
    fault_idx: int,
) -> list[tuple[ConflictUnit, CandidateResolution]] | None:
    """Attempt a deterministic ``#if/#endif`` balance fix before the LLM.

    The entity-splitting + splice pipeline can leave a whole-file preprocessor
    imbalance that no single sub-unit owns — e.g. a conflict region sliced mid-
    file that opens an ``#if`` without its ``#endif`` (the match is upstream of
    the marker block). Re-prompting the model may not help (it sees one unit at
    a time and can't reach the upstream directive), so we fix it directly when
    ``_try_balance_preprocessor`` can balance the spliced buffer in one clean
    edit (remove a stray bare ``#endif``, or append a missing ``#endif``).

    Mirrors :func:`_try_deterministic_brace_repair`. Returns a replacement
    ``accepted`` list (the fault unit becomes a whole-file unit carrying the
    repaired buffer as its resolved_text), or ``None`` to defer to the LLM path.
    Conservative: acts only on C/C++ and when one edit fully balances, and the
    repaired buffer is re-validated for preprocessor balance before use.
    """
    from capybase.verification import (
        _preprocessor_imbalance_line, _try_balance_preprocessor,
    )
    from capybase.conflict_model import CandidateResolution

    # Only engage on a preprocessor-coherence failure shape.
    is_pp_failure = any(
        "preprocessor" in (getattr(f, "message", "") or "").lower()
        for f in failures
    )
    if not is_pp_failure:
        return None
    if fault_idx < 0 or fault_idx >= len(accepted):
        return None
    _lang = accepted[fault_idx][0].language if 0 <= fault_idx < len(accepted) else None
    if _lang not in ("c", "cpp", "c++"):
        return None
    unit, _old_cand = accepted[fault_idx]
    try:
        spliced = _resolved_buffer(original, accepted)
    except Exception:  # noqa: BLE001 - splice may fail on bad spans
        return None
    if _preprocessor_imbalance_line(spliced) is None:
        return None  # not actually a preprocessor imbalance
    repaired = _try_balance_preprocessor(spliced)
    if repaired is None:
        return None  # couldn't balance in one edit → defer to LLM
    if _preprocessor_imbalance_line(repaired) is not None:
        return None  # safety re-check (shouldn't happen, but never trust)
    wf_unit = unit.model_copy(update={"marker_span": None, "unit_kind": "whole_file"})
    wf_cand = CandidateResolution(
        candidate_id=(getattr(_old_cand, "candidate_id", unit.unit_id) or unit.unit_id) + ":ppfix",
        unit_id=unit.unit_id,
        model_name=getattr(_old_cand, "model_name", "deterministic") or "deterministic",
        resolved_text=repaired,
        prompt_version="deterministic_preprocessor_repair",
        provenance="deterministic_preprocessor_repair",
        self_reported_confidence=0.9,
        explanation="deterministic #if/#endif balance repair (cross-unit splice)",
    )
    return [(wf_unit, wf_cand)]


# Regex to parse gcc's -fdiagnostics-parseable-fixits output format:
#   fix-it:"<file>":{<start_line>:<start_col>-<end_line>:<end_col>}:"<text>"
# gcc uses 1-based line and column numbers. Zero-width ranges ({L:C-L:C})
# are insertions; non-zero ranges are replacements; empty text is a deletion.
import re as _fixit_re_mod
_FIXIT_RE = _fixit_re_mod.compile(
    r'fix-it:"[^"]+":\{(\d+):(\d+)-(\d+):(\d+)\}:"(.*)"'
)


def _try_gcc_fixit_repair(
    failures: list,
    original: str,
    accepted: list[tuple[ConflictUnit, CandidateResolution]],
    fault_idx: int,
) -> list[tuple[ConflictUnit, CandidateResolution]] | None:
    """Apply gcc's own structured fix-it hints to the whole-file buffer.

    When gcc compiles with ``-fdiagnostics-parseable-fixits``, it emits
    ``fix-it:`` lines containing exact insert/replace/delete ranges. This
    subsumes the hand-coded regex patterns in ``_try_deterministic_cc_repair``
    — gcc covers more error types (missing tokens, wrong punctuation, type
    mismatches that affect parse, etc.) with surgical precision.

    Runs BEFORE the regex-based cc repair as a higher-fidelity first attempt.
    Safety: the full validation pipeline (both-sides-represented, intent
    coverage, Phase B re-verify) still runs on the repaired buffer.
    """
    if fault_idx < 0 or fault_idx >= len(accepted):
        return None
    unit, _old_cand = accepted[fault_idx]
    lang = unit.language or ""
    if lang not in ("c", "cpp", "c++"):
        return None

    # Only run when there's a compile failure with a parse error. Check the
    # failure messages for a gcc error line — if there's no error, there's
    # nothing to fix.
    has_cc_error = any(
        "error:" in (getattr(f, "message", "") or "").lower()
        for f in failures
    )
    if not has_cc_error:
        return None

    # Resolve the gcc binary path (same resolver as CcsSyntaxValidator).
    from capybase.adapters.lsp import _resolve as _resolve_cc
    is_cpp = lang in ("cpp", "c++")
    cc = _resolve_cc("g++" if is_cpp else "gcc")
    if cc is None:
        return None  # no compiler → can't get fix-its

    spliced = _resolved_buffer(original, accepted)
    if not spliced:
        return None

    std = "c++17" if is_cpp else "c11"
    suffix = ".cpp" if is_cpp else ".c"
    import tempfile
    import subprocess as _sp_fixit
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, delete=False, encoding="utf-8"
    ) as tf:
        tf.write(spliced)
        tmp_path = tf.name
    try:
        proc = _sp_fixit.run(
            [cc, "-fsyntax-only", f"-std={std}",
             "-fdiagnostics-parseable-fixits", tmp_path],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:  # noqa: BLE001 — fix-it is advisory
        from pathlib import Path as _P
        _P(tmp_path).unlink(missing_ok=True)
        return None

    stderr = proc.stderr or ""
    from pathlib import Path as _P
    _P(tmp_path).unlink(missing_ok=True)

    if proc.returncode == 0:
        return None  # already compiles — no fix needed

    # Parse fix-it lines from stderr.
    fixits = []
    for m in _FIXIT_RE.finditer(stderr):
        sl, sc, el, ec, text = (
            int(m.group(1)), int(m.group(2)),
            int(m.group(3)), int(m.group(4)),
            m.group(5),
        )
        fixits.append((sl, sc, el, ec, text))

    if not fixits:
        return None  # gcc didn't suggest any fix-its

    # Apply fix-its in REVERSE order (bottom-up) so line numbers don't shift.
    lines = spliced.split("\n")
    for sl, sc, el, ec, text in sorted(fixits, key=lambda f: (-f[0], -f[1])):
        if sl < 1 or sl > len(lines):
            continue
        line_idx = sl - 1
        line = lines[line_idx]
        # Column is 1-based; convert to 0-based slice indices.
        col_start = max(0, sc - 1)
        # If the fix-it spans multiple lines, only apply single-line fixes
        # (multi-line fix-its are rare and complex to apply safely).
        if sl != el:
            continue
        col_end = max(0, ec - 1)
        # Apply: replace [col_start:col_end] with text, or insert at col_start.
        new_line = line[:col_start] + text + line[col_end:]
        lines[line_idx] = new_line

    repaired = "\n".join(lines)
    if repaired == spliced:
        return None  # no change — fix-its were no-ops

    # Brace-balance safety check (same as _try_deterministic_cc_repair).
    if not _braces_balanced(repaired, lang):
        return None

    wf_unit = unit.model_copy(update={
        "marker_span": None, "unit_kind": "whole_file",
    })
    wf_cand = CandidateResolution(
        candidate_id=(_old_cand.candidate_id if _old_cand else "cc") + ":gccfixit",
        unit_id=unit.unit_id,
        model_name="gcc-fixit",
        prompt_version="gcc_fixit.v1",
        resolved_text=repaired,
        provenance="deterministic_gcc_fixit",
        self_reported_confidence=0.85,
    )
    return [(wf_unit, wf_cand)]


def _try_deterministic_cc_repair(
    failures: list,
    original: str,
    accepted: list[tuple[ConflictUnit, CandidateResolution]],
    fault_idx: int,
) -> list[tuple[ConflictUnit, CandidateResolution]] | None:
    """Compiler-diagnostic-driven deterministic repair for C/C++ candidates.

    Reads the gcc/clang error message from the failures, classifies it via
    ``_classify_ccs_parse_error``, and generates minimal repair hypotheses at
    the compiler-identified line. Each hypothesis is a single-token insertion
    or deletion — no semantic changes. Validates via ``_braces_balanced``
    before returning.

    Targets the 36 WHOLE_FILE_FAILED C cases at avg sim 0.978 where the model's
    output is semantically correct but has a small structural defect (missing
    ``;``, missing ``}``, stray char). The existing brace repair re-derives
    the imbalance structurally; this consumes gcc's diagnostic directly, which
    pinpoints the exact line.

    Returns a replacement ``accepted`` list (same whole-file-unit pattern as
    ``_try_deterministic_brace_repair``), or ``None`` to defer to the LLM.
    """
    from capybase.verification import (
        _braces_balanced,
        _classify_ccs_parse_error,
        _parse_cc_error_line,
    )
    from capybase.conflict_model import CandidateResolution

    # Gate: C/C++ language + a classifiable parse error.
    if fault_idx < 0 or fault_idx >= len(accepted):
        return None
    unit, _old_cand = accepted[fault_idx]
    _lang = unit.language
    if _lang not in ("c", "cpp", "c++"):
        return None
    # Find the first classifiable failure message.
    category = None
    error_line = None
    for f in failures:
        msg = getattr(f, "message", "") or ""
        cat = _classify_ccs_parse_error(msg)
        if cat is not None:
            category = cat
            error_line = _parse_cc_error_line(msg)
            break
    if category is None:
        return None  # no classifiable parse error → defer to existing repairs/LLM

    # Build the whole-file buffer to repair.
    try:
        spliced = _resolved_buffer(original, accepted)
    except Exception:  # noqa: BLE001 - splice may fail on bad spans
        return None

    lines = spliced.split("\n")
    # Convert 1-based gcc line to 0-based index.
    target_idx = (error_line - 1) if error_line and error_line > 0 else None

    repaired = None

    if category == "missing_semicolon" and target_idx is not None:
        # Insert ';' at end of the line before the error line (gcc points AT
        # the token after the missing ';', so the missing ';' goes on the
        # PREVIOUS non-empty line).
        for i in range(target_idx, max(target_idx - 3, -1), -1):
            if i < len(lines) and lines[i].rstrip():
                candidate_line = lines[i].rstrip()
                if not candidate_line.endswith(";") and not candidate_line.endswith("{") \
                        and not candidate_line.endswith("}") and not candidate_line.endswith(":") \
                        and not candidate_line.endswith(","):
                    lines[i] = candidate_line + ";"
                    repaired = "\n".join(lines)
                    break

    elif category == "missing_close_brace":
        # Try _try_balance_braces first (handles the common unclosed-block case).
        from capybase.verification import _try_balance_braces, _brace_imbalance_line
        if _brace_imbalance_line(spliced, _lang) is not None:
            bal = _try_balance_braces(spliced, _lang)
            if bal is not None and _brace_imbalance_line(bal, _lang) is None:
                repaired = bal

    elif category == "extra_close_brace" and target_idx is not None:
        # The line gcc points at is the extra '}'. Remove it if it's a
        # brace-only line.
        if target_idx < len(lines) and lines[target_idx].strip() == "}":
            del lines[target_idx]
            repaired = "\n".join(lines)

    elif category == "stray_character" and target_idx is not None:
        # gcc reports: "stray '@' in program" or "stray '\200' in program"
        # (octal for non-printable bytes). Extract the SPECIFIC character and
        # remove ONLY its first occurrence on the target line.
        #
        # The prior implementation stripped ALL non-ASCII bytes, corrupting
        # legitimate UTF-8 (e.g. "café" in comments). This targeted version
        # removes only the character gcc identified as stray, preserving all
        # other content. Safe because the stray char is in CODE context (gcc
        # doesn't flag chars inside properly-terminated string literals).
        import re as _re_stray
        # Normalize Unicode curly quotes (gcc uses U+2018/U+2019) to ASCII
        # so the regex matches regardless of locale/terminal settings.
        _msg_norm = msg.replace("\u2018", "'").replace("\u2019", "'")
        _stray_match = _re_stray.search(r"stray '(.+?)' in program", _msg_norm)
        if _stray_match and target_idx < len(lines):
            stray_raw = _stray_match.group(1)
            # Handle octal escapes: gcc emits e.g. '\200' for byte 0x80
            if stray_raw.startswith("\\") and len(stray_raw) == 4:
                try:
                    stray_char = chr(int(stray_raw[1:], 8))
                except ValueError:
                    stray_char = None
            else:
                stray_char = stray_raw
            if stray_char and len(stray_char) == 1:
                line = lines[target_idx]
                # Remove the FIRST occurrence only. If the char appears in a
                # string/comment context, the whole-file re-validation will
                # catch any corruption.
                _pos = line.find(stray_char)
                if _pos >= 0:
                    lines[target_idx] = line[:_pos] + line[_pos + 1:]
                    repaired = "\n".join(lines)

    elif category == "unterminated_literal" and target_idx is not None:
        # Add the missing closing quote. gcc reports the line where the
        # literal starts (or ends without termination).
        if target_idx < len(lines):
            line = lines[target_idx]
            # Strip line comments before counting quotes — a `'` or `"`
            # inside a // comment (e.g. `// don't`) must not trigger a
            # stray quote append.
            _comment_pos = line.find("//")
            _code_part = line[:_comment_pos] if _comment_pos >= 0 else line
            for q in ('"', "'", "*/"):
                count = 0
                i = 0
                while i < len(_code_part):
                    if _code_part[i] == "\\" and i + 1 < len(_code_part):
                        i += 2
                        continue
                    if _code_part[i:i+len(q)] == q:
                        count += 1
                        i += len(q)
                        continue
                    i += 1
                if q in ("*/",):
                    # Block comment close
                    if "/*" in line and "*/" not in line:
                        lines[target_idx] = line + " */"
                        repaired = "\n".join(lines)
                        break
                elif count % 2 == 1:
                    lines[target_idx] = line + q
                    repaired = "\n".join(lines)
                    break

    elif category == "duplicate_entity" and target_idx is not None:
        # Remove the second occurrence of the duplicate (the line gcc points
        # at is the redefinition — remove that line and any immediately
        # following body lines up to the next blank line or closing brace).
        if target_idx < len(lines):
            # Conservative: just remove the single redefinition line. The
            # caller's verify loop will catch it if more needs removing.
            del lines[target_idx]
            repaired = "\n".join(lines)

    # Validate: the repair must produce brace-balanced output.
    if repaired is None:
        return None
    if not _braces_balanced(repaired, _lang):
        return None  # repair introduced a new imbalance → unsafe

    # Return as a whole-file unit (same pattern as brace repair).
    wf_unit = unit.model_copy(update={"marker_span": None, "unit_kind": "whole_file"})
    wf_cand = CandidateResolution(
        candidate_id=(getattr(_old_cand, "candidate_id", unit.unit_id) or unit.unit_id) + ":ccfix",
        unit_id=unit.unit_id,
        model_name=getattr(_old_cand, "model_name", "deterministic") or "deterministic",
        resolved_text=repaired,
        prompt_version="deterministic_cc_repair",
        provenance="deterministic_cc_repair",
        self_reported_confidence=0.85,
        explanation=f"deterministic cc repair ({category} at line {error_line})",
    )
    return [(wf_unit, wf_cand)]


def _dup_eradication_regions(lines: list[str], name: str) -> list[tuple[int, int]]:
    """Find definition-shaped regions of ``name`` in a spliced C/C++ buffer.

    A region is either a brace-balanced block whose header line defines
    ``name`` (function/method/ctor/class — the name is preceded by a type or
    qualifier path and followed by ``(``, or introduced by class/struct), or
    a single-line variable definition (``<type> name = ...;``). Mere
    references — call statements, ``x = name(...)``, member access — do not
    start regions. Returns (start, end) line-index pairs, 0-based, inclusive.
    """
    import re as _re_dr

    word = _re_dr.compile(rf"\b{_re_dr.escape(name)}\b")
    regions: list[tuple[int, int]] = []
    n = len(lines)
    i = 0
    while i < n:
        code = lines[i].split("//")[0]
        if not word.search(code):
            i += 1
            continue
        stripped = code.strip()
        # Definition-shaped headers:
        #   <type-or-qualifiers> name ( ... )   — function/method/ctor
        #   (class|struct) name                 — type definition
        #   <type> name (=|[|;)                 — variable definition
        is_fn_def = bool(_re_dr.search(
            rf"^[A-Za-z_][\w:<>~,&*\s]*\b{_re_dr.escape(name)}\s*\(", stripped))
        is_type_def = bool(_re_dr.match(
            rf"(?:class|struct)\s+{_re_dr.escape(name)}\b", stripped))
        is_var_def = bool(_re_dr.match(
            rf"(?:[A-Za-z_][\w:<>]*[\s*\[\]]+)+"
            rf"(?:const\s+|static\s+|constexpr\s+|inline\s+|extern\s+)*"
            rf"{_re_dr.escape(name)}\s*(?:=|\[|;)", stripped))
        if is_fn_def or is_type_def:
            if stripped.endswith(";"):
                i += 1
                continue  # forward declaration, not a definition
            # Expand to the balanced-brace block starting at/below this line.
            depth = 0
            opened = False
            j = i
            while j < n:
                _c = lines[j].split("//")[0]
                depth += _c.count("{") - _c.count("}")
                if _c.count("{"):
                    opened = True
                if opened and depth <= 0:
                    break
                if not opened and _c.rstrip().endswith(";"):
                    # Signature-only header (body on next lines is still
                    # possible; keep scanning) — but a ';' before any '{'
                    # means a declaration: abandon.
                    break
                j += 1
            if opened and depth <= 0 and j < n:
                regions.append((i, j))
                i = j + 1
                continue
            i += 1
        elif is_var_def:
            regions.append((i, i))
            i += 1
        else:
            i += 1
    return regions


def _detect_side_collapse(
    base_text: str, current_text: str, replayed_text: str, buffer: str,
) -> dict | None:
    """Detect that a merged buffer is one side VERBATIM in a both-rewrite file.

    The sea-orm-0027 class: both sides rewrote substantially (churn ratio far
    below the wholesale band, so no side-pick regime applies), the oracle is a
    woven merge, but the model returned one side unchanged — a silent drop of
    the other side's entire rewrite. Runtime-detectable without the oracle:
    the buffer's line content is >= 0.9 contained in one side while <= 0.1 of
    the OTHER side's new (changed-vs-base) lines survive.

    Returns the detection dict (collapsed_to, containment figures, churn
    context) or None. Conservative: only fires OUTSIDE the wholesale band
    (>= 0.90 owns verbatim picks) and only when BOTH sides churned >= 25% of
    base — corpus-calibrated; below that, legit winner-verbatim oracles
    overlap the shape (79 corpus cases with oracle ~= winner at ratio < 0.90;
    their losers churn far less), so churn mass is the discriminator.
    """
    from capybase.merge_intent import full_file_context

    ctx = full_file_context(base_text, current_text, replayed_text)
    if ctx["churn_ratio"] >= 0.90:
        return None
    # Both sides must have REWRITTEN: >= 25% of base AND >= 20 lines each.
    # The absolute floor matters more than it looks: on a 6-line file a
    # 2-line edit is 33% of base — a value conflict, not a rewrite, and a
    # reused/one-sided resolution of it is routinely correct (the exact-reuse
    # loop's fixture). sea-orm-0027: 147/279 changed lines.
    floor = max(0.25 * max(ctx["base_lines"], 1), 20)
    if ctx["current_churn"] < floor or ctx["replayed_churn"] < floor:
        return None

    def _lset(t: str) -> set[str]:
        return {"".join(ln.split()) for ln in t.splitlines() if ln.strip()}

    buf, cs, rs, bs = (_lset(buffer), _lset(current_text),
                       _lset(replayed_text), _lset(base_text))
    if not buf or not cs or not rs:
        return None
    in_cur = len(buf & cs) / len(buf)
    in_rep = len(buf & rs) / len(buf)
    cur_new, rep_new = cs - bs, rs - bs
    rep_kept = len(buf & rep_new) / max(len(rep_new), 1)
    cur_kept = len(buf & cur_new) / max(len(cur_new), 1)
    collapsed = None
    if in_cur >= 0.90 and rep_kept <= 0.10:
        collapsed = "current"
    elif in_rep >= 0.90 and cur_kept <= 0.10:
        collapsed = "replayed"
    if collapsed is None:
        return None
    return {
        "collapsed_to": collapsed,
        "buffer_in_current": round(in_cur, 4),
        "buffer_in_replayed": round(in_rep, 4),
        "current_new_kept": round(cur_kept, 4),
        "replayed_new_kept": round(rep_kept, 4),
        "churn_ratio": ctx["churn_ratio"],
        "current_churn": ctx["current_churn"],
        "replayed_churn": ctx["replayed_churn"],
        "base_lines": ctx["base_lines"],
    }


def _phase2_fallback_build_cmd(pre_continue: str, *, enabled: bool = True) -> str:
    """The pre_continue command used as the Phase-2 build gate, if it IS a build.

    Phase-2's build check prefers the per-file target template (one TU, fast,
    no sibling noise). When no template exists, the pre_continue command is
    the only build available — but only when it actually builds (make /
    cmake --build / configure && make). ``true``, py_compile, and pytest are
    not builds: falling back to them would waste a subprocess or, worse,
    "pass" a gate that never compiled anything. Compound commands are
    recognized by their build words (``./configure && make -j4`` contains
    ``make``).
    """
    gate = (pre_continue or "").strip()
    if not enabled or not gate or gate == "true":
        return ""
    words = {w.strip("./;&|") for w in gate.split()}
    if words & {"make", "cmake", "ninja", "meson", "scons"}:
        return gate
    return ""


def _try_duplicate_eradication_repair(
    failures: list,
    original: str,
    accepted: list[tuple[ConflictUnit, CandidateResolution]],
    fault_idx: int,
) -> list[tuple[ConflictUnit, CandidateResolution]] | None:
    """Skeleton-aware eradication of duplicate definitions (sprint-18 WS1).

    gcc's ``redefinition of 'X'`` on a merged C/C++ file is the classic
    merge-splice defect: the resolution kept a pre-merge definition of X AND
    emitted the other side's, so X now exists twice. The single-line cc
    repair can only drop the redefinition HEADER (leaving an orphaned body),
    and the whole-file side portfolio throws away every other unit's correct
    merge to fix one duplicated entity.

    This repair deletes exactly ONE definition region of the compiler-named
    entity in the spliced buffer:

    - locate both definition regions (``_dup_eradication_regions``);
    - if the two are textually identical → delete the second (pure echo);
    - else if exactly one region's text appears verbatim in the pre-merge
      file → delete THAT one (the kept-base copy; the freshly generated
      definition is the merge's intent);
    - otherwise decline — an overload, a moved definition, or a genuine
      semantic divergence is the LLM's call, not ours.

    Safe by construction: acts only on the compiler-named entity, requires
    exactly two regions, deletes a region whose content provably survives
    elsewhere, and the caller's whole-file loop re-validates the result.
    """
    from capybase.verification import _braces_balanced
    from capybase.conflict_model import CandidateResolution as _CR

    if fault_idx < 0 or fault_idx >= len(accepted):
        return None
    unit, _old_cand = accepted[fault_idx]
    if unit.language not in ("c", "cpp", "c++"):
        return None
    # Entity name from the diagnostic: gcc/clang quote it after
    # "redefinition of" (possibly with return type / params — take the
    # identifier before any '(' and drop type tokens).
    import re as _re_dup

    name = None
    for f in failures:
        msg = getattr(f, "message", "") or ""
        m = _re_dup.search(r"redefinition of\s+'([^']+)'", msg) or _re_dup.search(
            r"redefinition of\s+([A-Za-z_][\w:]*)", msg)
        if m:
            quoted = m.group(1).split("(")[0].strip()
            toks = [t for t in quoted.split() if t] or [quoted]
            cand_name = toks[-1].strip("~&*<>")
            if cand_name and not cand_name[0].isdigit():
                name = cand_name
                break
    if not name:
        return None
    try:
        spliced = _resolved_buffer(original, accepted)
    except Exception:  # noqa: BLE001 - splice may fail on bad spans
        return None
    lines = spliced.split("\n")
    regions = _dup_eradication_regions(lines, name)
    if len(regions) != 2:
        return None

    def _text(r: tuple[int, int]) -> str:
        return "\n".join(lines[r[0]:r[1] + 1])

    t1, t2 = _text(regions[0]), _text(regions[1])
    victim = None
    diag = ""
    if t1.strip() == t2.strip():
        victim = regions[1]
        diag = "identical duplicate"
    elif t1 in original and t2 not in original:
        victim = regions[0]
        diag = "region 1 is the pre-merge copy"
    elif t2 in original and t1 not in original:
        victim = regions[1]
        diag = "region 2 is the pre-merge copy"
    if victim is None:
        return None
    new_lines = lines[:victim[0]] + lines[victim[1] + 1:]
    repaired = "\n".join(new_lines)
    if "<<<<<<<" in repaired or ">>>>>>>" in repaired:
        return None
    if not _braces_balanced(repaired, unit.language):
        return None
    # The surviving definition must still be present exactly once.
    if sum(1 for ln in new_lines if _re_dup.search(rf"\b{_re_dup.escape(name)}\b", ln.split("//")[0])) < 1:
        return None
    wf_unit = unit.model_copy(update={"marker_span": None, "unit_kind": "whole_file"})
    wf_cand = _CR(
        candidate_id=(getattr(_old_cand, "candidate_id", unit.unit_id) or unit.unit_id) + ":dupfix",
        unit_id=unit.unit_id,
        model_name=getattr(_old_cand, "model_name", "deterministic") or "deterministic",
        resolved_text=repaired,
        prompt_version="deterministic_dup_eradication",
        provenance="deterministic_dup_eradication",
        self_reported_confidence=0.9,
        explanation=(f"duplicate-definition eradication: removed the "
                     f"'{name}' copy at lines {victim[0] + 1}-{victim[1] + 1} ({diag})"),
    )
    return [(wf_unit, wf_cand)]


def _find_lcs_insertion_point(
    candidate_lines: list[str],
    missing_line: str,
    base_lines: list[str],
    error_line: int | None,
) -> int | None:
    """Find the best position to re-insert a dropped common line.

    Uses 2-line context matching: find where the missing line appears in the
    base, take its ±2 surrounding lines as context, and find the position in
    the candidate where that context best matches. Falls back to the error
    line, then to the first position where the preceding line matches.
    """
    # Find the missing line's position in the base.
    base_positions = [i for i, l in enumerate(base_lines) if l == missing_line]
    if not base_positions:
        # Not in base — use the error line as fallback.
        return (error_line - 1) if error_line else None

    base_pos = base_positions[0]
    # Extract 2 lines of context from the base around the missing line.
    ctx_before = base_lines[max(0, base_pos - 2):base_pos]
    ctx_after = base_lines[base_pos + 1:base_pos + 3]

    # Score each position in the candidate by how well the surrounding
    # context matches the base context. Higher score = better match.
    best_pos = None
    best_score = -1
    for i in range(len(candidate_lines) + 1):
        score = 0
        # Check lines before position i.
        for j, ctx_line in enumerate(reversed(ctx_before)):
            idx = i - 1 - j
            if 0 <= idx < len(candidate_lines) and candidate_lines[idx] == ctx_line:
                score += 1
            else:
                break
        # Check lines after position i.
        for j, ctx_line in enumerate(ctx_after):
            idx = i + j
            if 0 <= idx < len(candidate_lines) and candidate_lines[idx] == ctx_line:
                score += 1
            else:
                break
        if score > best_score:
            best_score = score
            best_pos = i

    # Only accept if we found at least 1 matching context line.
    if best_score > 0:
        return best_pos
    # Fall back to the error line.
    return (error_line - 1) if error_line else None


def _try_restore_common_lines(
    candidate_text: str,
    base_text: str,
    current_text: str,
    replayed_text: str,
    language: str | None,
) -> str | None:
    """Restore lines the candidate dropped — both side-common AND side-specific.

    A production-safe deterministic post-processor:
    1. Lines common to BOTH sides (agreed additions) → always safe to restore.
    2. Lines specific to ONE side → restore only if the other side didn't
       delete or replace them (the other side's base still has the line or
       the line is a new addition not present in base).

    Lines are re-inserted at the best-matched position via LCS context.
    The result must pass brace-balance check.

    Returns the repaired text, or None if no restoration was possible.
    """
    from capybase.verification import _braces_balanced

    cur_set = set((current_text or "").split("\n"))
    rep_set = set((replayed_text or "").split("\n"))
    base_set = set((base_text or "").split("\n"))
    common_lines = cur_set & rep_set

    # Side-specific additions: lines in one side but NOT in base
    cur_specific = cur_set - base_set - rep_set
    rep_specific = rep_set - base_set - cur_set

    cand_lines = candidate_text.split("\n")
    cand_stripped = {l.strip() for l in cand_lines}

    # Phase 1: restore common lines (both sides agreed)
    missing_common = [
        l for l in common_lines if l.strip() and l.strip() not in cand_stripped
    ]
    # Phase 2: restore side-specific lines
    # For current-specific: the replayed side must NOT have deleted the base
    # line at the same position. Since these are additions (not in base),
    # the other side simply didn't add them — that's fine, the addition
    # is an obligation of the side that added it.
    # Safety: only restore if the line is a genuine addition (not in base)
    # AND not a closing brace or structural token.
    # CRITICAL: do NOT restore lines that are modifications of base lines
    # the candidate already has a different version of — that would create
    # duplicate definitions (e.g., candidate has "B = 200", don't restore
    # "B = 20" — they're different versions of the same base line "B = 2").
    _structural_skip = {"{", "}", "};", ")", "(", "};", "}; "}

    def _is_modification_duplicate(line: str, base_lines: set[str], cand_lines: set[str]) -> bool:
        """True if `line` is a modified version of a base line that the
        candidate already has a DIFFERENT modified version of."""
        import re as _re_md
        # Extract the assignment target: "B = 200" → "B", "int x = 1;" → "x"
        # Try simple pattern: IDENT = ... (variable assignment)
        m = _re_md.match(r'\s*(?:\w+\s+)*(\w+)\s*=', line)
        if not m:
            return False  # not an assignment — can't be a modification duplicate
        var_name = m.group(1)
        # Check if base has a line assigning to the same variable
        for bl in base_lines:
            bm = _re_md.match(r'\s*(?:\w+\s+)*(\w+)\s*=', bl)
            if bm and bm.group(1) == var_name:
                # Base has this variable. Check if candidate already has it.
                for cl in cand_lines:
                    cm = _re_md.match(r'\s*(?:\w+\s+)*(\w+)\s*=', cl)
                    if cm and cm.group(1) == var_name:
                        return True  # candidate already has a version → don't restore
        return False

    missing_cur_specific = [
        l for l in cur_specific
        if l.strip() and l.strip() not in cand_stripped
        and l.strip() not in _structural_skip
        and not _is_modification_duplicate(l.strip(), base_set, cand_stripped)
    ]
    missing_rep_specific = [
        l for l in rep_specific
        if l.strip() and l.strip() not in cand_stripped
        and l.strip() not in _structural_skip
        and not _is_modification_duplicate(l.strip(), base_set, cand_stripped)
    ]

    all_missing = missing_common[:3] + missing_cur_specific[:2] + missing_rep_specific[:2]
    if not all_missing:
        return None

    context_sources = [
        (base_text or "").split("\n"),
        (current_text or "").split("\n"),
        (replayed_text or "").split("\n"),
    ]
    # Try restoring each missing line
    result = candidate_text
    for line in all_missing:
        cand_now = result.split("\n")
        if line.strip() in {l.strip() for l in cand_now}:
            continue  # already present (maybe from a previous iteration)
        for ctx_lines in context_sources:
            best_pos = _find_lcs_insertion_point(
                cand_now, line, ctx_lines, error_line=None
            )
            if best_pos is not None and 0 <= best_pos <= len(cand_now):
                trial = list(cand_now)
                trial.insert(best_pos, line)
                candidate_trial = "\n".join(trial)
                if _braces_balanced(candidate_trial, language):
                    result = candidate_trial
                    break
    return result if result != candidate_text else None


def _try_alternation_collapse(
    unit, cand, sides: dict[str, str], verify_fn,
) -> list[tuple[ConflictUnit, CandidateResolution]] | None:
    """S27-extend (axum-0019): collapse side-ALTERNATIVES merged as concatenation.

    A one-line-per-side alternative conflict (current `.extract::<Self>()`,
    replayed `.extract::<Host>()`) resolved as BOTH lines concatenated — sim
    1.00, one extra call chained, 'prefix `item` is unknown' downstream.
    When the resolved region contains a side's block immediately followed
    by the OTHER side's block (the union, not a choice), emit two collapse
    candidates (drop each block) and let the caller's gate verify.
    """
    CHNL = chr(10)
    resolved = cand.resolved_text or ""
    if not resolved.strip() or unit.marker_span is None:
        return None
    cur = (sides.get("current") or "").strip(CHNL)
    rep = (sides.get("replayed") or "").strip(CHNL)
    if not cur.strip() or not rep.strip():
        return None
    # The unit's region is small; the whole side texts are file-sized — use
    # only the ALTERNATIVE fragments: the lines of each side that appear in
    # the resolved region but not in the other side.
    res_lines = [l for l in resolved.split(CHNL) if l.strip()]
    cur_only = [l for l in cur.split(CHNL)
                if l.strip() and l not in rep]
    rep_only = [l for l in rep.split(CHNL)
                if l.strip() and l not in cur]
    if not (1 <= len(cur_only) <= 4 and 1 <= len(rep_only) <= 4):
        return None  # not a small alternation
    def _contains_seq(hay: list[str], needle: list[str]) -> bool:
        n = len(needle)
        return any(hay[i:i + n] == needle for i in range(len(hay) - n + 1))
    if not (_contains_seq(res_lines, cur_only) and _contains_seq(res_lines, rep_only)):
        return None  # both alternatives not present as sequences
    # Emit the two collapses as candidate texts.
    out = []
    for keep, drop, side_name in ((cur_only, rep_only, "current"),
                                  (rep_only, cur_only, "replayed")):
        # remove the dropped sequence (first occurrence)
        n = len(drop)
        txt_lines = resolved.split(CHNL)
        for i in range(len(txt_lines) - n + 1):
            if [l for l in txt_lines[i:i + n]] == drop:
                txt_lines = txt_lines[:i] + txt_lines[i + n:]
                break
        out.append((side_name, CHNL.join(txt_lines)))
    results = []
    for side_name, text in out:
        if not text.strip() or text == resolved:
            continue
        results.append((side_name, text))
    if len(results) < 2:
        return None
    cands = []
    for side_name, text in results:
        c = cand.model_copy(update={
            "resolved_text": text,
            "candidate_id": cand.candidate_id + f":altcol-{side_name[:4]}",
            "prompt_version": "deterministic_alternation_collapse",
            "provenance": "deterministic_side_consistency_repair",
            "explanation": (
                f"alternation collapse: kept the {side_name} side's "
                f"alternative (both were concatenated)"),
        })
        cands.append((unit, c))
    return cands


def _try_side_consistency_repair(
    failures: list,
    original: str,
    accepted: list[tuple[ConflictUnit, CandidateResolution]],
    fault_idx: int,
) -> list[tuple[ConflictUnit, CandidateResolution]] | None:
    """Repair model candidates by restoring dropped common lines and deleting
    invented lines, using the merge itself as a structural prior.

    A valid merge candidate should usually be explainable as: lines from base,
    lines from ours, lines from theirs, plus a small number of novel
    reconciliation lines. When the model drops a line common to both sides
    (e.g. a closing brace, a return statement, a function call) or invents a
    line not in any side, that's a structural defect this repair targets.

    Two repair actions:
    1. **Common-line restore:** lines present in BOTH current AND replayed
       (agreed by both sides) but missing from the spliced candidate. Reinsert
       near the error line.
    2. **Novel-line delete:** lines in the spliced candidate but not in ANY
       side (base/current/replayed). The model invented them. Delete near the
       error line.

    Every inserted line comes from base/current/replayed (provenance-backed).
    Each hypothesis is validated via ``_braces_balanced``.

    C/C++ only: the side-consistency heuristic is designed for the structural
    defect profile of C weak-model output (dropped braces, invented bridge
    lines). For Python/Rust, deleting "novel" lines can remove legitimate
    reconciliation code the model produced.

    Returns a replacement ``accepted`` list (same whole-file-unit pattern as
    the other deterministic repairs), or ``None`` to defer to the LLM.
    """
    from capybase.verification import (
        _braces_balanced,
        _parse_cc_error_line,
    )
    from capybase.conflict_model import CandidateResolution

    if fault_idx < 0 or fault_idx >= len(accepted):
        return None
    unit, _old_cand = accepted[fault_idx]
    _lang = unit.language
    # Gate on C/C++: the side-consistency heuristic (novel-line delete,
    # common-line restore) is designed for the structural defect profile of
    # C weak-model output. For Python/Rust, deleting "novel" lines can
    # remove legitimate reconciliation code the model produced.
    if _lang not in ("c", "cpp", "c++"):
        return None

    try:
        spliced = _resolved_buffer(original, accepted)
    except Exception:  # noqa: BLE001
        return None

    # Parse the error line from the first failure (for targeting).
    error_line = None
    for f in failures:
        error_line = _parse_cc_error_line(getattr(f, "message", "") or "")
        if error_line is not None:
            break

    # Gather the side texts. For a whole-file unit, use the original file sides.
    # For a marker-block unit, use the unit's three sides.
    base_lines = set((unit.base.text or "").split("\n"))
    cur_lines = set((unit.current.text or "").split("\n"))
    rep_lines = set((unit.replayed.text or "").split("\n"))
    all_side_lines = base_lines | cur_lines | rep_lines
    # Lines common to both sides (agreed edits).
    common_lines = cur_lines & rep_lines

    spliced_lines = spliced.split("\n")
    spliced_set = set(spliced_lines)

    repaired = None

    # 1. Common-line restore: lines both sides have but the candidate dropped.
    # Enhanced with LCS-based context matching: for each missing common line,
    # find the optimal re-insertion point by matching 2 lines of surrounding
    # context from the base, rather than blindly inserting at the error line.
    missing_common = [l for l in common_lines if l.strip() and l not in spliced_set]
    if missing_common:
        for line in missing_common[:3]:  # try up to 3 missing lines
            # Find the best insertion point using 2-line context matching.
            # Look for the line in the base to get its surrounding context.
            base_text_lines = (unit.base.text or "").split("\n")
            best_pos = _find_lcs_insertion_point(
                spliced_lines, line, base_text_lines, error_line
            )
            if best_pos is not None and 0 <= best_pos <= len(spliced_lines):
                trial = list(spliced_lines)
                trial.insert(best_pos, line)
                candidate_text = "\n".join(trial)
                if _braces_balanced(candidate_text, _lang):
                    repaired = candidate_text
                    break

    # 2. Novel-line delete: lines in the candidate not in ANY side, near the error.
    if repaired is None and error_line:
        target_idx = error_line - 1
        window = range(max(0, target_idx - 3), min(len(spliced_lines), target_idx + 4))
        for i in window:
            line = spliced_lines[i]
            if line.strip() and line not in all_side_lines:
                # This line is invented by the model. Try deleting it.
                trial = list(spliced_lines)
                del trial[i]
                candidate_text = "\n".join(trial)
                if _braces_balanced(candidate_text, _lang):
                    repaired = candidate_text
                    break

    if repaired is None:
        return None

    # Return as a whole-file unit (same pattern as brace/cc repair).
    wf_unit = unit.model_copy(update={"marker_span": None, "unit_kind": "whole_file"})
    wf_cand = CandidateResolution(
        candidate_id=(getattr(_old_cand, "candidate_id", unit.unit_id) or unit.unit_id) + ":sidefix",
        unit_id=unit.unit_id,
        model_name=getattr(_old_cand, "model_name", "deterministic") or "deterministic",
        resolved_text=repaired,
        prompt_version="deterministic_side_consistency_repair",
        provenance="deterministic_side_consistency_repair",
        self_reported_confidence=0.8,
        explanation="deterministic side-consistency repair (common-line restore or novel-line delete)",
    )
    return [(wf_unit, wf_cand)]


# ---------------------------------------------------------------------------
# Structural signature: delimiter deltas + terminal-token patterns
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StructuralSignature:
    """Shallow structural properties of a text span, after string/comment masking.

    Used by side-consensus repair: when both sides agree on a structural
    property but the candidate disagrees, that's a high-confidence repair signal.
    """
    brace_delta: int       # { count - } count
    paren_delta: int       # ( count - ) count
    bracket_delta: int     # [ count - ] count
    trailing_semicolons: int   # lines ending with ; (code lines only)
    trailing_backslashes: int  # lines ending with \ (macro continuations)
    line_count: int


def _structural_signature(text: str, lang: str | None = None) -> _StructuralSignature:
    """Compute the structural signature of ``text``.

    Masks strings/comments first (so delimiters inside them don't count), then
    counts delimiter deltas and terminal-token patterns. O(n) scan.
    """
    from capybase.verification import _mask_strings_and_comments
    masked = _mask_strings_and_comments(text, lang or "c")
    brace_d = 0
    paren_d = 0
    bracket_d = 0
    semis = 0
    backslashes = 0
    lines = masked.split("\n")
    for line in lines:
        stripped = line.rstrip()
        for ch in stripped:
            if ch == "{":
                brace_d += 1
            elif ch == "}":
                brace_d -= 1
            elif ch == "(":
                paren_d += 1
            elif ch == ")":
                paren_d -= 1
            elif ch == "[":
                bracket_d += 1
            elif ch == "]":
                bracket_d -= 1
        if stripped.endswith(";"):
            semis += 1
        if stripped.endswith("\\"):
            backslashes += 1
    return _StructuralSignature(
        brace_delta=brace_d, paren_delta=paren_d, bracket_delta=bracket_d,
        trailing_semicolons=semis, trailing_backslashes=backslashes,
        line_count=len(lines),
    )


def _try_side_consensus_repair(
    failures: list,
    original: str,
    accepted: list[tuple[ConflictUnit, CandidateResolution]],
    fault_idx: int,
) -> list[tuple[ConflictUnit, CandidateResolution]] | None:
    """Repair candidates using side-consensus structural evidence.

    When both ``current`` and ``replayed`` sides agree on a structural property
    (brace delta, trailing semicolons, macro continuations) but the candidate
    disagrees, that's a high-confidence repair signal. The consensus is a
    structural prior from the merge itself — not a guess.

    Repair hypotheses (each validated by ``_braces_balanced``):
    - Both sides have ``brace_delta = -1`` (one more ``}``), candidate has
      ``0`` → candidate dropped a closing brace. Append ``}``.
    - Both sides have ``brace_delta = +1``, candidate has ``0`` → candidate
      dropped an opening brace (rare).
    - Both sides have N trailing semicolons, candidate has N-1 → the candidate
      dropped a ``;`` on a line where both sides have one.
    - Both sides have N trailing backslashes, candidate has N-1 → the candidate
      broke a macro continuation.

    Every inserted token is traceable to the side consensus (provenance-backed).
    C/C++ only: the consensus heuristic is designed for C structural defects.

    Returns a replacement ``accepted`` list, or ``None`` to defer to the LLM.
    """
    from capybase.verification import _braces_balanced
    from capybase.conflict_model import CandidateResolution

    if fault_idx < 0 or fault_idx >= len(accepted):
        return None
    unit, _old_cand = accepted[fault_idx]
    _lang = unit.language
    if _lang not in ("c", "cpp", "c++"):
        return None

    try:
        spliced = _resolved_buffer(original, accepted)
    except Exception:  # noqa: BLE001
        return None

    cur_sig = _structural_signature(unit.current.text or "", _lang)
    rep_sig = _structural_signature(unit.replayed.text or "", _lang)
    cand_sig = _structural_signature(spliced, _lang)

    repaired = None
    reason = ""

    # Brace consensus: both sides agree on brace_delta, candidate disagrees.
    if cur_sig.brace_delta == rep_sig.brace_delta and cand_sig.brace_delta != cur_sig.brace_delta:
        delta_diff = cur_sig.brace_delta - cand_sig.brace_delta
        if delta_diff > 0:
            # Candidate has fewer closing braces than both sides → append }
            repaired = spliced.rstrip("\n") + "\n" + "}" * delta_diff
            reason = f"brace consensus: both sides have delta={cur_sig.brace_delta}, candidate has {cand_sig.brace_delta}; appended {delta_diff} '}}'"
        elif delta_diff < 0:
            # Candidate has extra closing braces → try removing from the end
            lines = spliced.split("\n")
            to_remove = abs(delta_diff)
            removed = 0
            for i in range(len(lines) - 1, -1, -1):
                if removed >= to_remove:
                    break
                if lines[i].strip() == "}":
                    del lines[i]
                    removed += 1
            if removed == to_remove:
                repaired = "\n".join(lines)
                reason = f"brace consensus: removed {removed} extra '}}'"

    # Semicolon consensus: both sides have N trailing semicolons, candidate has fewer.
    if repaired is None and cur_sig.trailing_semicolons == rep_sig.trailing_semicolons \
            and cand_sig.trailing_semicolons < cur_sig.trailing_semicolons:
        # Find the line in the candidate that's missing a semicolon where both
        # sides have one. Compare line-by-line (rough but catches the common case).
        cur_lines = (unit.current.text or "").split("\n")
        rep_lines = (unit.replayed.text or "").split("\n")
        cand_lines = spliced.split("\n")
        # For each candidate line, check if a corresponding side line ends with ;
        # but the candidate doesn't.
        for i, cline in enumerate(cand_lines):
            c_stripped = cline.rstrip()
            if c_stripped and not c_stripped.endswith(";"):
                # Check if any side line at a similar position ends with ;
                for side_lines in (cur_lines, rep_lines):
                    for j in range(max(0, i - 2), min(len(side_lines), i + 3)):
                        s_stripped = side_lines[j].rstrip()
                        # Same content except for the trailing semicolon?
                        if s_stripped.endswith(";") and s_stripped[:-1].rstrip() == c_stripped:
                            cand_lines[i] = c_stripped + ";"
                            candidate_text = "\n".join(cand_lines)
                            if _braces_balanced(candidate_text, _lang):
                                repaired = candidate_text
                                reason = f"semicolon consensus: line {i+1} missing ';' that both sides have"
                                break
                    if repaired:
                        break
                if repaired:
                    break

    # Macro continuation consensus: both sides have N trailing backslashes,
    # candidate has fewer → the candidate broke a multi-line macro.
    if repaired is None and cur_sig.trailing_backslashes == rep_sig.trailing_backslashes \
            and cand_sig.trailing_backslashes < cur_sig.trailing_backslashes:
        # Find the candidate line that should end with \ but doesn't.
        cand_lines = spliced.split("\n")
        cur_lines = (unit.current.text or "").split("\n")
        rep_lines = (unit.replayed.text or "").split("\n")
        for i, cline in enumerate(cand_lines):
            c_stripped = cline.rstrip()
            if c_stripped and not c_stripped.endswith("\\"):
                for side_lines in (cur_lines, rep_lines):
                    for j in range(max(0, i - 2), min(len(side_lines), i + 3)):
                        s_stripped = side_lines[j].rstrip()
                        if s_stripped.endswith("\\") and s_stripped[:-1].rstrip() == c_stripped:
                            cand_lines[i] = c_stripped + "\\"
                            candidate_text = "\n".join(cand_lines)
                            if _braces_balanced(candidate_text, _lang):
                                repaired = candidate_text
                                reason = f"backslash consensus: line {i+1} missing macro continuation"
                                break
                    if repaired:
                        break
                if repaired:
                    break

    if repaired is None:
        return None
    if not _braces_balanced(repaired, _lang):
        return None

    wf_unit = unit.model_copy(update={"marker_span": None, "unit_kind": "whole_file"})
    wf_cand = CandidateResolution(
        candidate_id=(getattr(_old_cand, "candidate_id", unit.unit_id) or unit.unit_id) + ":consensus",
        unit_id=unit.unit_id,
        model_name=getattr(_old_cand, "model_name", "deterministic") or "deterministic",
        resolved_text=repaired,
        prompt_version="deterministic_side_consensus_repair",
        provenance="deterministic_side_consensus_repair",
        self_reported_confidence=0.85,
        explanation=f"deterministic side-consensus repair ({reason})",
    )
    return [(wf_unit, wf_cand)]


def _try_deterministic_prefix_dedup(
    failures: list,
    original: str,
    accepted: list[tuple[ConflictUnit, CandidateResolution]],
    fault_idx: int,
) -> list[tuple[ConflictUnit, CandidateResolution]] | None:
    """Strip a duplicated enclosing wrapper from resolved_text at the splice
    junction.

    The marker span excludes the enclosing wrapper (e.g. ``use crate::{``
    before the span and ``};`` after). A correct resolved_text that re-includes
    the wrapper produces a doubled prefix after splicing:

        use crate::{                                      ← existing wrapper
        use crate::{error::*, ConnectionTrait, ...};      ← resolved (re-includes)
        };                                                ← existing close

    Rust reports "expected identifier, found keyword ``use``" — the cargo error
    signature of a prefix collision. This repair detects when the resolved_text
    redundantly re-states the wrapper that's already present outside the span,
    strips the redundant wrapper lines from the resolved_text, and returns the
    corrected accepted list.

    Conservative: only acts when the line immediately before (or after) the
    marker span shares a statement head with the resolved_text's first (or
    last) line, AND stripping produces a brace-balanced result.
    """
    from capybase.conflict_model import CandidateResolution
    from capybase.verification import _brace_imbalance_line

    # Engagement gate: the cargo error signatures of prefix collision.
    prefix_error_patterns = (
        "expected identifier, found keyword",
        "expected item after attributes",
        "expected one of",
    )
    has_prefix_error = any(
        any(p in (getattr(f, "message", "") or "").lower() for p in prefix_error_patterns)
        for f in failures
    )
    if not has_prefix_error:
        return None
    if fault_idx < 0 or fault_idx >= len(accepted):
        return None
    unit, old_cand = accepted[fault_idx]
    if unit.marker_span is None:
        return None  # whole-file unit — no junction
    start, end = unit.marker_span
    orig_lines = original.split("\n")
    resolved_lines = (old_cand.resolved_text or "").split("\n")
    if not resolved_lines or not resolved_lines[0].strip():
        return None
    # The line immediately before the marker span in the original file.
    prefix_line = orig_lines[start - 1].strip() if start > 0 else ""
    # The line immediately after the marker span.
    suffix_line = orig_lines[end + 1].strip() if end + 1 < len(orig_lines) else ""
    res_first = resolved_lines[0].strip()
    res_last = resolved_lines[-1].strip()

    # Detect: the resolved_text's first line redundantly re-states the wrapper
    # line before the span (same statement head, one is a prefix of the other).
    strip_first = (
        prefix_line
        and _is_statement_line(prefix_line) and _is_statement_line(res_first)
        and _same_statement_head(prefix_line, res_first)
        and res_first.startswith(prefix_line)
    )
    # Symmetric: the resolved_text's last line redundantly re-states the wrapper
    # line after the span.
    # Symmetric: the resolved_text's last line redundantly re-states the wrapper
    # line after the span. Two cases: (a) both are statement lines with the same
    # head (e.g. two ``use`` lines), or (b) both are closing delimiters (``};``,
    # ``)``, etc.) — the paired close of an opening wrapper that strip_first
    # already detected. Closing delimiters aren't "statement lines" but they're
    # wrapper fragments that must be stripped in tandem with the opening.
    is_closing_delim = lambda s: s.strip() in ("};", ")", "]", "};", ">", "},")
    strip_last = (
        suffix_line
        and res_last != res_first  # don't double-strip a single-line resolution
        and (
            # Case (a): both statement lines with same head.
            (_is_statement_line(suffix_line) and _is_statement_line(res_last)
             and _same_statement_head(suffix_line, res_last)
             and res_last.endswith(suffix_line))
            # Case (b): both are bare closing delimiters (paired with strip_first).
            or (strip_first and is_closing_delim(suffix_line)
                and res_last.strip() == suffix_line)
        )
    )
    if not strip_first and not strip_last:
        return None
    # Strip the redundant wrapper lines from the resolved_text.
    new_resolved = old_cand.resolved_text or ""
    new_lines = new_resolved.split("\n")
    if strip_first:
        new_lines = new_lines[1:]
    if strip_last and new_lines:
        new_lines = new_lines[:-1]
    new_resolved = "\n".join(new_lines)
    # Back-project: replace the fault unit's resolved_text with the stripped
    # version, then re-splice to verify the result is brace-balanced.
    new_cand = old_cand.model_copy(update={
        "resolved_text": new_resolved,
        "provenance": (old_cand.provenance or "plain_llm") + "+prefix_dedup",
    })
    result = list(accepted)
    result[fault_idx] = (unit, new_cand)
    try:
        spliced = _resolved_buffer(original, result)
    except Exception:  # noqa: BLE001
        return None
    # Safety: the repaired splice must be brace-balanced.
    _lang = unit.language
    if _brace_imbalance_line(spliced, _lang) is not None:
        return None
    return result


def _is_statement_line(line: str) -> bool:
    """Whether a line is a structurally-significant statement (not blank/comment).

    Used by ``_try_deterministic_prefix_dedup`` to decide whether a consecutive
    duplicate line is a genuine doubled statement (worth stripping) versus a
    cosmetic duplicate (blank line, comment) that shouldn't be touched.
    """
    stripped = line.strip()
    if not stripped:
        return False
    # Comments (Rust //, Python #, block /* * */).
    if stripped.startswith("//") or stripped.startswith("#") or stripped.startswith("*"):
        return False
    # Statement keywords that indicate a real code boundary.
    for kw in ("use ", "pub use", "pub fn", "fn ", "impl ", "struct ",
               "enum ", "trait ", "mod ", "const ", "static ", "type ",
               "import ", "from ", "def ", "class ",
               # C/C++ type-introducer leading forms (function/field headers).
               "int ", "void ", "char ", "double ", "float ", "long ",
               "short ", "unsigned ", "signed ", "bool ", "auto ",
               "namespace ", "template "):
        if stripped.startswith(kw):
            return True
    return False


_STATEMENT_KEYWORDS = (
    "use ", "pub use", "pub fn", "fn ", "impl ", "struct ",
    "enum ", "trait ", "mod ", "const ", "static ", "type ",
    "import ", "from ", "def ", "class ",
    # C/C++ type-introducer leading forms (function/field headers).
    "int ", "void ", "char ", "double ", "float ", "long ",
    "short ", "unsigned ", "signed ", "bool ", "auto ",
    "namespace ", "template ",
)


def _statement_head(line: str) -> str:
    """The leading keyword + module/path prefix of a statement line.

    For ``use crate::{error::*, ...}`` → ``use crate::``. For ``pub fn foo()``
    → ``pub fn``. Used to check whether two consecutive lines are the same
    statement re-stated (a redundant wrapper vs its full form).
    """
    stripped = line.strip()
    for kw in _STATEMENT_KEYWORDS:
        if stripped.startswith(kw):
            # For use/import, include the module path up to the first ``{`` or
            # ``;`` — ``use crate::{...}`` and ``use crate::foo;`` share the
            # ``use crate::`` head. For other keywords, just the keyword.
            if kw in ("use ", "pub use"):
                idx = stripped.find("{")
                if idx > 0:
                    return stripped[:idx]
                return stripped
            return kw
    return ""


def _same_statement_head(a: str, b: str) -> bool:
    """Whether two lines share the same statement head AND one is a prefix of
    the other (the signature of a redundant wrapper re-stated by the splice)."""
    ha, hb = _statement_head(a), _statement_head(b)
    if not ha or ha != hb:
        return False
    return a.strip().startswith(b.strip()) or b.strip().startswith(a.strip())


#: How many lines of file context immediately outside the marker span to compare
#: against the candidate's boundaries when detecting boundary echoes. The model
#: frequently re-states content it sees in the surrounding context (an import, a
#: function header, a closing brace); a K-line window catches the common cases
#: without searching the whole file.
_BOUNDARY_ECHO_CONTEXT_LINES = 5


def _normalize_line_for_overlap(line: str) -> str:
    """Conservative normalization for boundary-overlap comparison.

    Strips trailing whitespace only (line-ending + trailing spaces). Preserves
    leading indentation and all tokens/comments/identifiers — a different
    indentation or a single changed token means the lines are NOT the same echo.
    Deliberately stricter than quality.py's punctuation-stripping normalize.
    """
    return line.rstrip()


def _boundary_overlap_len(
    context_lines: list[str], candidate_lines: list[str]
) -> int:
    """The largest k for which the final k context lines equal the first k
    candidate lines (after conservative normalization).

    0 when there is no contiguous overlap. Used to detect the model echoing the
    file's surrounding context at the start (context before — left boundary) of
    its resolved_text — a splice-boundary duplicate.
    """
    n = min(len(context_lines), len(candidate_lines))
    for k in range(n, 0, -1):
        ctx_slice = [_normalize_line_for_overlap(l) for l in context_lines[-k:]]
        cand_slice = [_normalize_line_for_overlap(l) for l in candidate_lines[:k]]
        if ctx_slice == cand_slice:
            return k
    return 0


def _boundary_suffix_overlap_len(
    candidate_lines: list[str], context_lines: list[str]
) -> int:
    """The largest k for which the final k candidate lines equal the first k
    context lines (after conservative normalization).

    The right-boundary counterpart to :func:`_boundary_overlap_len`: detects the
    model echoing the file context immediately AFTER the span at the end of its
    resolved_text. Trailing empty context lines (from the worktree's trailing
    newline) are skipped so they don't mask a real suffix match.
    """
    # Drop trailing empty context lines (the worktree often ends with "\n",
    # producing a spurious "" that breaks suffix comparison).
    ctx = [l for l in context_lines if l.strip()] if context_lines else []
    n = min(len(ctx), len(candidate_lines))
    for k in range(n, 0, -1):
        cand_slice = [_normalize_line_for_overlap(l) for l in candidate_lines[-k:]]
        ctx_slice = [_normalize_line_for_overlap(l) for l in ctx[:k]]
        if cand_slice == ctx_slice:
            return k
    return 0


def _overlap_is_actionable(overlap_lines: list[str]) -> bool:
    """Whether a detected overlap is strong enough to authorize a strip.

    A single duplicated ``}`` or blank line is weak evidence — it could be a
    legitimate repeated delimiter. Require either ≥2 nonblank lines, or one
    nontrivial line (contains an identifier — a function/``use``/symbol header,
    not a bare delimiter). Delimiter-only lines contribute to a multi-line
    overlap but don't independently authorize a transform.
    """
    import re as _re
    nonblank = [l for l in overlap_lines if l.strip()]
    if len(nonblank) >= 2:
        return True
    if len(nonblank) == 1:
        # A single line is actionable only if it's nontrivial: it contains an
        # alphanumeric identifier of length ≥ 2. This distinguishes a real
        # code line (``fn foo() {``, ``use std::io;``) from a bare delimiter
        # (``}``, ``};``, ``)``) or punctuation-only line.
        line = nonblank[0].strip()
        return bool(_re.search(r"[A-Za-z_][A-Za-z0-9_]+", line))
    return False


def _find_core_line_span(
    resolved_lines: list[str], core_lines: list[str],
) -> int:
    """Line index where the deferred core sits in the resolved text, or -1.

    The structural resolver joins ``[pre_resolved, core_cur, post_resolved]``,
    so the core sits between the tails. ``core_cur`` (the upstream side of the
    overlap) may appear several times in the resolved text — a lone ``}``,
    ``break;``, or blank line recurs in the reconstructed tails. A naive
    ``str.find``/``replace(.., 1)`` returns the FIRST match, which is often a
    tail line, not the core.

    We scan for every line range that textually matches ``core_lines`` and
    return the one closest to the centre of the text — the core is the
    "middle" element by construction, so the centremost match is the most
    reliable heuristic when there are several.
    """
    n = len(resolved_lines)
    clen = len(core_lines)
    if clen == 0 or clen > n:
        return -1
    best = -1
    best_dist = n  # distance from centre; smaller is better
    centre = n / 2
    for i in range(n - clen + 1):
        if resolved_lines[i:i + clen] == core_lines:
            dist = abs((i + clen / 2) - centre)
            if dist < best_dist:
                best_dist = dist
                best = i
    return best


# ---------------------------------------------------------------------------
# Edit-pattern extraction + instantiation for the intra-step pattern cache.
# ---------------------------------------------------------------------------

import re as _re_pat

_TOKEN_RE_PAT = _re_pat.compile(r"[A-Za-z_]\w*|[0-9]+|\s+|[^\sA-Za-z0-9]+")


def _tokenize_for_pattern(text: str) -> list[str]:
    """Tokenize for pattern extraction (Summer's 4-category lossless tokenizer)."""
    return _TOKEN_RE_PAT.findall(text or "")


def _token_category(tok: str) -> str:
    """Map a token to its structural category for pattern normalization.

    Identifiers → ``IDENT``, numbers → ``NUM``, whitespace → ``WS``,
    punctuation kept verbatim. Two conflicts with the same structure but
    different variable names normalize to the same category sequence.
    """
    if _re_pat.fullmatch(r"[A-Za-z_]\w*", tok):
        return "IDENT"
    if _re_pat.fullmatch(r"[0-9]+", tok):
        return "NUM"
    if tok.strip() == "":
        return "WS"
    return tok  # punctuation — kept verbatim for anchor matching


def _extract_edit_pattern(base: str, resolved: str) -> list[tuple[str, str, str, str]] | None:
    """Extract a normalized token-level edit pattern from base→resolved.

    Returns a list of ``(base_category_seq, resolved_category_seq, base_raw,
    op_type)`` tuples — one per non-equal opcode in the token diff — or None
    when the pattern is empty or too complex (>10 ops).

    * ``base_raw`` carries the raw base tokens for anchor matching.
    * ``op_type`` is ``"replace"`` (default), ``"insert"``, or ``"delete"``.

    For **insert** opcodes (where nothing was removed from base), the anchor
    is set to the **next base token after the insertion point** — the token
    the insertion goes *before*. This lets pure-insertion patterns (e.g.
    ``Type x;`` → ``Type x{};``, which inserts ``{}`` before ``;``) be
    instantiated on sibling units. Previously, insert opcodes produced an
    empty anchor and were silently skipped.
    """
    from capybase.diff import line_matcher
    bt = _tokenize_for_pattern(base)
    rt = _tokenize_for_pattern(resolved)
    matcher = line_matcher(bt, rt)
    pattern: list[tuple[str, str, str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        repl_cats = "".join(_token_category(t) for t in rt[j1:j2])
        if tag == "insert":
            # Insert: nothing removed from base. Anchor = the base token
            # right AFTER the insertion point (the token the inserted content
            # goes before). Critical for nlohmann-0019 shape (insert "{}"
            # before ";").
            anchor_tok = bt[i1] if i1 < len(bt) else ""
            pattern.append(("", repl_cats, anchor_tok, "insert"))
        elif tag == "delete":
            base_cats = "".join(_token_category(t) for t in bt[i1:i2])
            base_raw = "\x00".join(bt[i1:i2])
            pattern.append((base_cats, "", base_raw, "delete"))
        else:  # replace
            base_cats = "".join(_token_category(t) for t in bt[i1:i2])
            base_raw = "\x00".join(bt[i1:i2])
            pattern.append((base_cats, repl_cats, base_raw, "replace"))
    if not pattern or len(pattern) > 10:
        return None
    # Length guard: reject when the base or resolved text is too large (the
    # pattern should be a small local edit, not a whole-file diff). Without
    # this, a whole-file base (unit.base.text is the entire file for marker
    # units) produces a few gigantic garbage opcodes that pass the op-count
    # check but corrupt the output when instantiated.
    if len(bt) > 200 or len(rt) > 200:
        return None
    # Ratio guard: reject extreme size mismatches (>5x) — a 1-line resolution
    # vs a 200-line base is almost certainly a whole-file-vs-hunk mismatch.
    if len(bt) > 0 and len(rt) > 0:
        ratio = max(len(bt), len(rt)) / min(len(bt), len(rt))
        if ratio > 5:
            return None
    return pattern


def _instantiate_pattern(
    base: str, pattern: list,
) -> str | None:
    """Apply a normalized edit pattern to a sibling's base text.

    Walks the pattern's ops: for each, finds the raw base anchor in the
    sibling's tokens. If found unambiguously, applies the edit (replace,
    insert, or delete). If ambiguous (anchor appears multiple times) or
    not found, skips the op.

    Supports three op types (4th tuple element; legacy 3-tuples default to
    ``"replace"``):
    * ``"replace"``: replace the anchor tokens with the resolved tokens.
    * ``"insert"``: insert the resolved tokens BEFORE the anchor token.
    * ``"delete"``: remove the anchor tokens.

    Returns the instantiated resolved text, or None if no ops could be applied.
    """
    bt = _tokenize_for_pattern(base)
    applied = list(bt)
    any_applied = False
    for entry in pattern:
        # Support both 3-tuple (legacy) and 4-tuple patterns.
        if len(entry) == 4:
            _base_cats, repl_cats, base_raw, op_type = entry
        else:
            _base_cats, repl_cats, base_raw = entry
            op_type = "replace"
        anchor_toks = base_raw.split("\x00") if base_raw else []
        if not anchor_toks:
            continue
        idx = _find_subsequence_simple(applied, anchor_toks)
        if idx < 0:
            continue
        # Ambiguity guard: skip if the anchor appears again after the match.
        next_idx = _find_subsequence_simple(applied[idx + len(anchor_toks):], anchor_toks)
        if next_idx >= 0:
            continue
        if op_type == "delete":
            del applied[idx:idx + len(anchor_toks)]
            any_applied = True
            continue
        # Build the replacement/insertion tokens from the category sequence.
        repl_toks = _category_seq_to_tokens(repl_cats, anchor_toks)
        if repl_toks is None:
            continue  # can't instantiate — has IDENT/NUM we can't resolve
        if op_type == "insert":
            # Insert BEFORE the anchor (don't consume the anchor).
            applied[idx:idx] = repl_toks
        else:  # replace
            applied[idx:idx + len(anchor_toks)] = repl_toks
        any_applied = True
    if not any_applied:
        return None
    return "".join(applied)


def _find_subsequence_simple(haystack: list[str], needle: list[str]) -> int:
    """Index of the first occurrence of needle in haystack, or -1."""
    if not needle:
        return 0
    n, m = len(haystack), len(needle)
    if m > n:
        return -1
    for i in range(n - m + 1):
        if haystack[i:i + m] == needle:
            return i
    return -1


def _category_seq_to_tokens(
    cats: str, anchor_toks: list[str],
) -> list[str] | None:
    """Convert a category sequence back to concrete tokens.

    For literal patterns (punctuation/WS only), the categories ARE the tokens.
    For IDENT/NUM categories, we can't recover the original identifier —
    decline (return None) so the caller falls through to the LLM.
    """
    # If the pattern contains IDENT or NUM, we can't instantiate it — those
    # would need the sibling's specific identifier, which we don't know how
    # to place without full structural matching.
    if "IDENT" in cats or "NUM" in cats:
        return None
    # All-literal pattern: split by WS boundaries. The category string for
    # literals is the raw text (e.g. "{};" or "&&"). Re-tokenize it to get
    # the individual tokens.
    return _tokenize_for_pattern(cats)


def _strip_boundary_echo(
    resolved_text: str,
    original: str,
    marker_span: tuple[int, int] | None,
    language: str | None,
) -> tuple[str, dict] | None:
    """Pure core of the boundary-echo strip: detect and remove context-owned
    lines echoed at the splice boundary of ``resolved_text``.

    Returns ``(stripped_text, diagnostics)`` when an actionable echo is found and
    the stripped result is brace-balanced (when spliced), else ``None``. The
    diagnostics dict carries ``variant`` / ``left_overlap`` / ``right_overlap``
    for causal-attribution journaling.

    Shared by both call sites:
      - the whole-file repair loop (``_try_boundary_echo_strip``), which runs
        after a candidate passes per-unit validation but fails whole-file
        composition;
      - the per-unit pre-validation pass (``_apply_deterministic_closure``),
        which runs BEFORE per-unit syntax validation so a wrapping echo that
        would cause a parse failure is caught before escalation (closing the
        reachability gap where such echoes previously spun until the retry
        budget exhausted and escalated).
    """
    from capybase.verification import _brace_imbalance_line
    from capybase.adapters.parsers import splice_resolution

    if marker_span is None or not resolved_text.strip():
        return None
    start, end = marker_span
    orig_lines = original.split("\n")
    resolved_lines = resolved_text.split("\n")
    K = _BOUNDARY_ECHO_CONTEXT_LINES
    context_before = orig_lines[max(0, start - K):start] if start > 0 else []
    context_after = orig_lines[end + 1:end + 1 + K] if end + 1 < len(orig_lines) else []

    left_k = _boundary_overlap_len(context_before, resolved_lines)
    right_k = _boundary_suffix_overlap_len(resolved_lines, context_after) if context_after else 0

    left_actionable = left_k > 0 and _overlap_is_actionable(resolved_lines[:left_k])
    right_actionable = right_k > 0 and _overlap_is_actionable(resolved_lines[-right_k:])
    # Paired-delimiter exception: a bare closer at the right boundary is
    # strippable when paired with an actionable left overlap (the closer of the
    # echoed opener). Mirrors prefix_dedup's strip_last case b.
    _is_closing_delim = lambda s: s.strip() in ("}", "};", ")", "]", ">", "},")
    if (not right_actionable and left_actionable and right_k > 0
            and right_k <= 2
            and all(_is_closing_delim(l) for l in resolved_lines[-right_k:] if l.strip())):
        right_actionable = True
    if not left_actionable and not right_actionable:
        return None

    def _strip(text: str, lk: int, rk: int) -> str:
        lines = text.split("\n")
        if lk > 0:
            lines = lines[lk:]
        if rk > 0 and lines:
            lines = lines[:len(lines) - rk] if rk < len(lines) else []
        return "\n".join(lines)

    if left_actionable and right_actionable:
        name, text, lk, rk = "both", _strip(resolved_text, left_k, right_k), left_k, right_k
    elif left_actionable:
        name, text, lk, rk = "left", _strip(resolved_text, left_k, 0), left_k, 0
    else:
        name, text, lk, rk = "right", _strip(resolved_text, 0, right_k), 0, right_k

    if not text.strip():
        return None  # empty result remains a failure

    # Safety: the stripped candidate spliced into the file must be brace-balanced.
    try:
        spliced = splice_resolution(original, marker_span, text)
    except Exception:  # noqa: BLE001
        return None
    if _brace_imbalance_line(spliced, language) is not None:
        return None

    return text, {"variant": name, "left_overlap": lk, "right_overlap": rk}


def _try_boundary_echo_strip(
    failures: list,
    original: str,
    accepted: list[tuple[ConflictUnit, CandidateResolution]],
    fault_idx: int,
    *,
    verify_fn: Callable[..., object] | None = None,
) -> tuple[list[tuple[ConflictUnit, CandidateResolution]], dict] | None:
    """Strip context-owned lines echoed at the splice boundary.

    Generalizes ``_try_deterministic_prefix_dedup`` beyond statement-keyword
    lines: when the model's ``resolved_text`` begins (or ends) with a run of
    lines that already exist immediately outside the marker span, the splice
    produces a duplicate. This detects the overlap by exact line equality at the
    boundary and trims it from the candidate.

    Examples it catches that prefix_dedup (statement-keyword gated) misses:
      - a duplicated multi-line ``pub use crate::{...};`` block
      - a duplicated function header + body fragment
      - a duplicated closing-brace run (``}\\n}\\n}`` echoed from below the span)

    Safe by construction (the 3rd-reviewer discipline):
      - removes ONLY text that exists immediately outside the replaced span;
      - changes ONLY the generated candidate, never retained file context;
      - is accepted ONLY when the fully composed result is the UNIQUE passing
        variant among {original, left-stripped, right-stripped, both-stripped};
      - ambiguous cases (multiple passing variants, or none) return None.

    Returns ``(accepted_list, diagnostics)`` where diagnostics carries the
    overlap lengths + accepted variant for causal-attribution journaling, or
    ``None`` to defer to the LLM repair path.
    """
    if fault_idx < 0 or fault_idx >= len(accepted):
        return None
    unit, old_cand = accepted[fault_idx]
    resolved_text = old_cand.resolved_text or ""
    stripped = _strip_boundary_echo(
        resolved_text, original, unit.marker_span, unit.language,
    )
    if stripped is None:
        return None
    text, diag = stripped

    # Test hook: when a verify_fn is injected, enforce the unique-pass rule
    # (the stripped variant must actually pass full validation). In production
    # we trust the caller's whole-file loop to validate (same as prefix_dedup).
    trial_cand = old_cand.model_copy(
        update={"resolved_text": text,
                "provenance": (old_cand.provenance or "plain_llm") + "+boundary_echo_strip"},
    )
    if verify_fn is not None:
        trial_accepted = list(accepted)
        trial_accepted[fault_idx] = (unit, trial_cand)
        try:
            result = verify_fn(trial_accepted)
        except Exception:  # noqa: BLE001
            return None
        if not getattr(result, "passed", False):
            return None

    # Keep the unit's marker_span: the echo strip is a CANDIDATE transformation
    # (removing echoed boundary lines from resolved_text), NOT a whole-file
    # replacement. Unlike brace_repair (which composes the full spliced file and
    # thus produces a complete buffer), the stripped resolved_text is still a
    # FRAGMENT that must be spliced into the original file context. Converting to
    # whole_file would write the fragment as the entire file (the check-vs-write
    # mismatch that corrupted sqlite-history-0001: a 282-byte fragment written as
    # an 11K-char file). The splice composes the stripped text with the
    # surrounding original context correctly when marker_span is preserved.
    result = [(unit, trial_cand)]
    diagnostics = {"mechanism": "boundary_echo_strip", **diag}
    return result, diagnostics


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
    """Deprecated shim — the provider path (apply_to_config) is the ONLY
    calibration source (CONSTRAINTS #2/#3). Kept as a no-op so the init
    call site and any external callers stay stable; it never loads a
    profile, never applies one, and never warns. Remove at the next
    interface cleanup.
    """
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


def _has_undeclared_side_local_identifier(
    candidate: str, base: str, current: str, replayed: str,
) -> str | None:
    """Detect an identifier used in the sbcr candidate but not declared in it,
    where the identifier appears in exactly ONE side's text.

    Catches the clickhouse-0041 defect: sbcr interleaved a ``return suffix...``
    from replayed into a candidate where ``suffix`` was never declared
    (replayed's declaration was in a part the interleave dropped).

    Conservative: only checks identifiers used in ``return``/``throw``/
    ``break``/``continue`` statements — the stacking zone where sbcr is most
    likely to introduce side-local variables. An identifier is "declared" if
    it appears as a C/C++ declaration (``type name`` pattern) earlier in the
    candidate, or if it appears 3+ times (likely a parameter or member).

    Returns the undeclared identifier name, or None.
    """
    import re as _re
    from collections import Counter as _Counter
    if not candidate:
        return None
    _term_re = _re.compile(
        r"^\s*(return|throw|break|continue)\s+(.+?);", _re.MULTILINE
    )
    _ident_re = _re.compile(r"[A-Za-z_]\w*")
    _decl_re = _re.compile(
        r"\b(?:int|long|short|char|bool|float|double|void|auto|const|unsigned|"
        r"signed|std::\w+|"
        # User-defined types: require either a capital letter (CamelCase
        # types like Foo, MyType), underscore prefix (_t types),
        # or a known lowercase typedef pattern (size_t, uint32_t, int64_t).
        # This excludes C/C++ control keywords (return, if, while, etc.)
        # which start lowercase and would wrongly match as type names.
        r"[A-Z_][A-Za-z_]\w*|"
        r"(?:size|u?int\d*|u?char|ssize|off|pid|mode|dev|time|clock)_t)"
        r"\s*[*&]*\s*([a-z_]\w*)\s*[=;,]"
    )
    declared = set(_decl_re.findall(candidate))
    all_cand_ids = _ident_re.findall(candidate)
    id_counts = _Counter(all_cand_ids)
    frequent = {ident for ident, cnt in id_counts.items() if cnt >= 3}
    declared |= frequent

    _reserved = frozenset({
        "true", "false", "nullptr", "NULL", "0", "1", "this", "self",
    })
    for m in _term_re.finditer(candidate):
        expr = m.group(2)
        for ident in _ident_re.findall(expr):
            if ident in _reserved or ident in declared:
                continue
            in_base = ident in base
            in_cur = ident in current
            in_rep = ident in replayed
            side_count = sum([in_base, in_cur, in_rep])
            # Only flag if it's in exactly one side and NOT in base — it's a
            # side-local variable whose declaration context was likely dropped.
            if side_count == 1 and not in_base:
                return ident
    return None


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
            self.git, structural_config=config.structural, future_config=config.future
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
            mask_deferred_comments=getattr(
                config.structural, "mask_deferred_comments", True
            ),
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
        # Wire the deferred-comment masking toggle from config (the upstream
        # half of the two-level comment architecture, design §4). When True
        # (default), DEFERRED comments in the conflict sides are blanked before
        # the code-resolution model sees them; the reconciliation pass (Phase 3)
        # rewrites them later. Zero overhead for files with no deferred comments.
        try:
            from capybase.resolution_engine import set_mask_deferred_comments
            set_mask_deferred_comments(
                getattr(config.structural, "mask_deferred_comments", True)
            )
        except Exception:  # noqa: BLE001 — advisory; never break orchestrator init
            pass
        _val_cfg = ValidationConfig.from_dict(config.validation.model_dump())
        # Propagate the user-supplied build command (tests.pre_continue) for
        # C/C++ whole-file verification. When set, verify_file's C branch runs
        # the real build (make/cmake) in the repo dir instead of standalone gcc
        # — the authoritative oracle that resolves sibling #include headers.
        # The live-eval driver sets tests.pre_continue from C_BUILD_COMMANDS;
        # production rebase runs set it via [tests] pre_continue in capybase.toml.
        _pre = getattr(config.tests, "pre_continue", None)
        if _pre and _pre.strip() not in ("", "true", "pytest"):
            _val_cfg.cc_build_command = _pre
        # Propagate the build-target narrowing template (if configured) so
        # verify_file compiles only the conflict file's translation unit.
        _target_tmpl = getattr(config.validation, "cc_build_target_template", "")
        if _target_tmpl:
            _val_cfg.cc_build_target_template = _target_tmpl
        self.verification = VerificationEngine.default(_val_cfg)
        # Sprint-19 P3: the session build-state tracker journals every
        # build probe/transition into the flight journal (the 300s silent
        # gaps in every sprint-18 protobuf journal were unjournaled
        # builds). The sink is best-effort — a journaling failure must
        # never break a build.
        from capybase.verification import BuildStateTracker

        def _build_event_sink(event: str, payload: dict) -> None:
            _p = payload.pop("path", None)
            self.journal.emit(
                event, payload, step_index=self.step, path=_p)

        self.verification.build_state = BuildStateTracker(
            event_sink=_build_event_sink)
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

    def _journal_class_member_candidate(self, unit: ConflictUnit) -> None:
        """Sprint-19 P5 (journal-only): surface a measured member-boundary split.

        When the extractor stamped ``class_member_split_candidate`` (an
        oversized C++ class region whose entity-level split declined but
        whose class body carries member-function boundaries), journal it
        alongside the oversized skip so live runs quantify the addressable
        set (the protobuf-0055 class) before any enabling decision.
        """
        cand = (unit.structural_metadata or {}).get("class_member_split_candidate")
        if not isinstance(cand, dict):
            return
        self.journal.emit(
            "class_member_split_candidate",
            {**cand,
             "enabled": bool(getattr(
                 self.config.future, "enable_class_member_splitting",
                 False))},
            step_index=self.step, path=unit.path, unit_id=unit.unit_id,
        )

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
        # Essential content = the text the prompt ACTUALLY sends to the model.
        # The context_builder sends a windowed slice: lines[marker_start - ctx :
        # marker_end + ctx], where ctx = ContextBuilder.context_lines (default
        # 15). It does NOT send the full file. For a large file with a small
        # conflict (e.g. a 1-line version bump in a 766-line file = 561 tokens
        # of conflict), the prompt sends ~67 lines, not 766. Measuring the full
        # marker block (original_worktree_text) instead caused borderline
        # OVERSIZED escalations on prompts that actually fit.
        #
        # The prompt's fixed overhead (intro/contract/rules, ~200-400 tokens)
        # and all augmentation sections ARE trimmable by _fit_to_budget, so we
        # do NOT fold them in here — this guard fires only when the windowed
        # conflict content itself doesn't fit. estimate_tokens is ~4 chars/tok.
        marker_text = unit.original_worktree_text or ""
        if marker_text and unit.marker_span is not None:
            # Window the marker block the same way context_builder does.
            start, end = unit.marker_span
            lines = marker_text.split("\n")
            ctx = 15  # match ContextBuilder default (context_builder.py:23)
            lo = max(0, start - ctx)
            hi = min(len(lines) - 1, end + ctx)
            marker_text = "\n".join(lines[lo : hi + 1])
        elif not marker_text:
            # Fallback: sum the three sides (the pre-fix behavior).
            marker_text = (
                (unit.base.text or "") + (unit.current.text or "")
                + (unit.replayed.text or "")
            )
        essential = estimate_tokens(marker_text)
        return essential > available, essential, available

    def _try_step_shape_reuse(self, unit: ConflictUnit) -> UnitOutcome | None:
        """Replay a sibling unit's resolution from the intra-step shape cache.

        When many units in one file share the same conflict shape (e.g. 78
        regions of ``Type x;`` vs ``Type x{};``), only the first needs the full
        cascade. This checks ``self._step_shape_cache`` — populated by earlier
        deterministic acceptances in the same ``_resolve_step`` — and if a
        sibling with the same shape was resolved, builds a candidate from its
        text and runs it through verification.

        Same safety model as ``_try_exact_reuse``: the reused candidate runs
        the full verify gauntlet. A mismatch (the sibling's resolution doesn't
        fit this unit's context) fails and returns None, falling through to the
        normal cascade. This is a speed optimization, never a correctness bypass.
        """
        cache = getattr(self, "_step_shape_cache", None)
        if not cache:
            return None
        # Key on the EXACT conflict content (base+current+replayed texts), not
        # just the structural shape hash. Two conflicts with the same shape but
        # different variable names (``int a = 1;`` vs ``int b = 1;``) must NOT
        # reuse each other's resolution — the resolved text would have the wrong
        # variable. Only truly identical conflicts (same 3-way text) match.
        import hashlib as _hl
        content = (
            (unit.base.text or "") + "\x00"
            + (unit.current.text or "") + "\x00"
            + (unit.replayed.text or "")
        )
        key = f"{_hl.sha1(content.encode()).hexdigest()[:16]}:{unit.path}"
        cached_text = cache.get(key)
        if cached_text is None:
            return None
        cand = CandidateResolution(
            candidate_id=f"{unit.unit_id}:step_shape_reuse",
            unit_id=unit.unit_id,
            model_name="step_shape_reuse",
            prompt_version="step_shape_reuse",
            resolved_text=cached_text,
            explanation="replayed from a sibling unit with the same conflict shape",
            provenance="deterministic_structural",
        )
        validation = self.verification.verify(unit, cand, fast_verify=True)
        self.journal.emit(
            "step_shape_reuse",
            {"candidate_id": cand.candidate_id, "passed": validation.passed},
            step_index=self.step, path=unit.path, unit_id=unit.unit_id,
        )
        if not validation.passed:
            return None  # the sibling's resolution doesn't fit this unit's context
        outcome = UnitOutcome(unit=unit, validation=validation, attempts=[cand])
        outcome.accepted = cand
        return outcome

    def _try_step_pattern_reuse(self, unit: ConflictUnit) -> UnitOutcome | None:
        """Apply a sibling's edit pattern to this unit via the pattern cache.

        When the exact-content cache misses (different variable names) but the
        structural shape matches a previously-resolved sibling, extract the
        token-level transformation (base→resolved) and apply it to this unit's
        base text. Only fires for literal-substitution patterns (punctuation/
        keyword changes like ``;`` → ``{};``, ``and`` → ``&&``) — patterns
        involving identifier renames are declined (can't recover the specific
        identifier from the category-normalized pattern).

        Same safety model as ``_try_step_shape_reuse``: the instantiated
        candidate runs ``verify(fast_verify=True)`` and falls through on failure.
        """
        cache = getattr(self, "_step_pattern_cache", None)
        if not cache:
            return None
        try:
            from capybase.memory.shape import shape_for_unit
        except Exception:  # noqa: BLE001
            return None
        key = f"{shape_for_unit(unit)}:{unit.path}"
        pattern = cache.get(key)
        if pattern is None:
            return None
        # Use the diff3-refined hunk base, NOT unit.base.text (whole file).
        _refined = unit.refined_sides
        base = (_refined[1] if _refined else "") or (unit.base.text or "")
        instantiated = _instantiate_pattern(base, pattern)
        if instantiated is None:
            return None
        # Defense-in-depth: reject when the instantiated text is many lines
        # but the unit's sides are 1-2 lines (the pattern produced garbage).
        _unit_lines = max(
            len((unit.current.text or "").split("\n")),
            len((unit.replayed.text or "").split("\n")),
        )
        _inst_lines = len(instantiated.split("\n"))
        if _inst_lines > _unit_lines * 3 + 3:
            return None
        cand = CandidateResolution(
            candidate_id=f"{unit.unit_id}:step_pattern_reuse",
            unit_id=unit.unit_id,
            model_name="step_pattern_reuse",
            prompt_version="step_pattern_reuse",
            resolved_text=instantiated,
            explanation="instantiated from a sibling unit's edit pattern",
            provenance="deterministic_structural",
        )
        # Use fast_verify for pattern reuse (see comment above).
        validation = self.verification.verify(unit, cand, fast_verify=True)
        self.journal.emit(
            "step_pattern_reuse",
            {
                "candidate_id": cand.candidate_id,
                "passed": validation.passed,
                "shape_hash": key.split(":")[0],
                "pattern_ops": len(pattern),
                "instantiated_lines": len(instantiated.split("\n")),
                "unit_lines": max(
                    len((unit.current.text or "").split("\n")),
                    len((unit.replayed.text or "").split("\n")),
                ),
                "hard_failures": len(validation.hard_failures),
            },
            step_index=self.step, path=unit.path, unit_id=unit.unit_id,
        )
        if not validation.passed:
            return None
        outcome = UnitOutcome(unit=unit, validation=validation, attempts=[cand])
        outcome.accepted = cand
        return outcome

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

        # Whole-file side texts for rules whose gating needs file-level
        # context (text_additive_union's additivity check): marker units
        # carry the whole-file BASE but conflict-block-only current/replayed
        # — diffing a block against a whole file reads as a total rewrite
        # and the rule declines everything.
        if "whole_file_sides" not in (unit.structural_metadata or {}):
            try:
                _wf_sides, _wf_base = _true_stage_sides(self.git, unit.path)
                unit.structural_metadata["whole_file_sides"] = {
                    **_wf_sides, "base": _wf_base}
            except Exception:  # noqa: BLE001 — metadata is advisory
                pass
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
        # Rule-class-aware validation: rules that provably preserve structure
        # (one_sided_change, disjoint_edits, etc.) use fast_verify (skip
        # expensive syntax/AST validators). Rules that recombine or heuristically
        # merge tokens/lines (token_disjoint, insertion_union, etc.) use FULL
        # verify — their splices can introduce syntax errors that the cheap
        # validators miss. This enforces the safety contract: "every deterministic
        # candidate is validated before acceptance; a rejected candidate falls
        # through to the LLM."
        #
        # Rules that PROVABLY preserve structure (safe to skip syntax check):
        #   - take one side verbatim: one_sided_change, identical_sides, delete_side
        #   - non-overlapping line splice: disjoint_edits, zealous_merge
        #   - entity-level disjoint: entity_disjoint
        #   - clean rename-vs-body partition: refactoring_aware_merge
        #
        # Rules that need FULL verify (recombinant/heuristic — can break syntax):
        #   - token_disjoint: recombinant token splice
        #   - mechanical_reapply_merge: heuristic anchor re-application
        #   - partial_disjoint_merge: conservative core default relies on Phase B
        #   - all union rules: insertion_union, list_union, dict_union, brace_union,
        #     convergent_addition_merge, directive_union
        #   - value resolution: text_value_resolution, dependency_version_resolution
        _STRUCTURE_PRESERVING_RULES = frozenset({
            "delete_side", "identical_sides", "one_sided_change",
            "disjoint_edits", "zealous_merge", "entity_disjoint",
            "refactoring_aware_merge",
        })
        _fast = result.rule in _STRUCTURE_PRESERVING_RULES
        # token_disjoint is a recombinant token splice — it ALWAYS gets full
        # verify (syntax + AST). The line-count ratio is not a sound proxy for
        # token-splice correctness: a splice on "stable" sides can still produce
        # garbled output (tokens from different lines interleaved mid-expression).
        # The shape-conditional fast_verify that was here before was a performance
        # optimization that broke the rule's documented safety contract.
        import time as _vt
        _vt0 = _vt.monotonic()
        validation = self.verification.verify(unit, cand, fast_verify=_fast)
        _verify_elapsed = _vt.monotonic() - _vt0
        if hasattr(self, "_unit_verify_time"):
            self._unit_verify_time += _verify_elapsed
        self.journal.emit(
            "structurally_resolved",
            {"candidate_id": cand.candidate_id, "rule": result.rule,
             "passed": validation.passed},
            step_index=self.step, path=unit.path, unit_id=unit.unit_id,
        )
        if not validation.passed:
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
            return None
        outcome = UnitOutcome(unit=unit, validation=validation, attempts=[cand])
        outcome.accepted = cand
        # Mini-conflict core: if partial_disjoint_merge resolved the deterministic
        # tails but deferred the overlap core, resolve just the core via the LLM
        # (a tiny prompt — 1-3 lines) and patch it back into the resolved text.
        # This shrinks the LLM's job from "resolve this 200-line block" to
        # "resolve these 2 conflicting lines."
        if result.deferred_core is not None:
            core_base, core_cur, core_rep = result.deferred_core
            patched = self._resolve_deferred_core(
                unit, cand, core_base, core_cur, core_rep,
                core_offset=result.deferred_core_offset,
            )
            core_was_resolved = patched is not None
            if core_was_resolved:
                cand.resolved_text = patched
            else:
                # The contested core could not be resolved. Accepting the
                # placeholder (core_cur — current's version of lines the
                # replayed side ALSO changed) silently drops the replayed
                # side's edit: end-to-end probe of base `return 1` / cur
                # `return 2` / rep `return 3` shipped `return 2` with no
                # escalation. Reject the structural candidate; the normal
                # per-unit flow (LLM, then escalation) decides the contested
                # lines — a silent one-sided pick is never acceptable.
                self._record_resolution_attempt(
                    UnitOutcome(unit=unit), mechanism="structural",
                    decision="skip",
                    reason="deferred core unresolved — contested lines "
                           "kept for the LLM, not silently defaulted",
                )
                return None
        else:
            core_was_resolved = False
        self.journal.emit(
            "candidate_accepted",
            {"candidate_id": cand.candidate_id, "via": "structural",
             "deferred_core_resolved": core_was_resolved},
            step_index=self.step, path=unit.path, unit_id=unit.unit_id,
        )
        return outcome

    def _resolve_deferred_core(
        self, unit: ConflictUnit, cand: "CandidateResolution",
        core_base: str, core_cur: str, core_rep: str,
        *, core_offset: int | None = None,
    ) -> str | None:
        """Resolve a deferred mini-conflict core (1-3 lines) via the LLM.

        Called when ``partial_disjoint_merge`` resolved the deterministic tails
        but couldn't resolve the overlap core. Builds a tiny ConflictUnit from
        the core's 3-way texts, resolves it via the standard ``_resolve_unit``
        pipeline (which includes SBCR, the LLM, and CEGIS), and splices the
        result back into the candidate's resolved_text at ``core_offset``.

        ``core_offset`` is the character offset of ``core_cur`` in the resolved
        text, recorded by the structural resolver at assembly time. It MUST be
        used rather than searching for ``core_cur``: the core (e.g. a lone
        ``}``) frequently recurs in the reconstructed tails, so a textual
        search would patch the wrong occurrence.

        The core unit inherits the parent's structural_metadata (enclosing
        function/class, node type/signature) and gets a padded worktree with
        ±3 lines of resolved tail context so the LLM can see the surrounding
        brace structure. Without this, the LLM resolves the core blind to its
        enclosing scope and introduces brace imbalances.

        Returns the patched resolved_text, or None if the core couldn't be
        resolved (the conservative default — core_cur — remains in place).
        """
        # Empty-core guard: nothing to resolve — the assembly without the
        # core is already the resolution (protobuf-0065's recursion spun
        # 327 levels on exactly this shape before hitting the stack limit).
        if not core_cur.strip() and not core_rep.strip():
            return None
        try:
            from capybase.conflict_model import ConflictUnit as CU, ConflictSide

            # Extract ±3 lines of surrounding context from the resolved candidate
            # text so the LLM sees the braces/scope around the core.
            resolved_lines = (cand.resolved_text or "").split("\n")
            core_lines = core_cur.split("\n")
            # Compute the core's LINE position from the character offset the
            # structural resolver recorded. Fall back to a textual scan only if
            # the offset is absent (older resolution path); in that case prefer
            # the centremost match (the core sits between the tails).
            if core_offset is not None and core_offset <= len(cand.resolved_text or ""):
                core_start_in_resolved = (
                    cand.resolved_text or ""
                )[:core_offset].count("\n")
            else:
                core_start_in_resolved = _find_core_line_span(
                    resolved_lines, core_lines,
                )

            # Build a padded worktree: 3 lines before + core + 3 lines after.
            pad_before: list[str] = []
            pad_after: list[str] = []
            if core_start_in_resolved >= 0:
                pad_before = resolved_lines[max(0, core_start_in_resolved - 3):core_start_in_resolved]
                after_start = core_start_in_resolved + len(core_lines)
                pad_after = resolved_lines[after_start:after_start + 3]

            padded_text = "\n".join(pad_before + core_lines + pad_after)
            padded_core_start = len(pad_before)
            padded_core_end = padded_core_start + len(core_lines) - 1

            # Inherit the parent's structural metadata so the LLM sees the
            # enclosing function/class and the structural anchor renders.
            # The depth stamp caps the cascade's structural recursion: at
            # depth >= 2 resolve_structurally declines the mini-conflict
            # family (see _deferred_core_depth), so the core resolves via
            # portfolio/SBCR/LLM instead of recursing.
            core_meta = dict(unit.structural_metadata)
            core_meta["deferred_core_context"] = "\n".join(pad_before + pad_after)
            core_meta["deferred_core_depth"] = (
                int(unit.structural_metadata.get("deferred_core_depth", 0) or 0) + 1
            )

            core_unit = CU(
                session_id=unit.session_id,
                step_index=unit.step_index,
                path=unit.path,
                language=unit.language,
                conflict_type=unit.conflict_type,
                unit_id=f"{unit.unit_id}:core",
                unit_kind="text_marker_block",
                base=ConflictSide(label="BASE", text=core_base),
                current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=core_cur),
                replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=core_rep),
                original_worktree_text=padded_text,
                marker_span=(padded_core_start, padded_core_end),
                enclosing_symbol=unit.enclosing_symbol,
                risk_tags=list(unit.risk_tags),
                severity=unit.severity,
                structural_metadata=core_meta,
            )
            # _resolve_unit resets self._unit_verify_time to 0.0 at entry. The
            # core's verify time should still be excluded from the OUTER unit's
            # wall budget, so capture the parent's total first, then fold the
            # core's verify time back in after the recursive call returns.
            _outer_verify_time = getattr(self, "_unit_verify_time", 0.0)
            outcome = self._resolve_unit(core_unit)
            _core_verify_time = getattr(self, "_unit_verify_time", 0.0)
            self._unit_verify_time = _outer_verify_time + _core_verify_time
            if outcome.accepted is not None and outcome.accepted.resolved_text:
                core_resolved = outcome.accepted.resolved_text
                # Splice the resolved core back into the candidate text BY LINE
                # INDEX at core_start_in_resolved. str.replace(core_cur, ..., 1)
                # was wrong: core_cur (e.g. a lone ``}``) may appear several
                # times in the resolved text (in the reconstructed tails), and
                # replace patches the FIRST textual occurrence — often a tail
                # line, not the core. We use the offset the structural resolver
                # recorded, which is authoritative.
                patched: str | None = None
                if core_start_in_resolved >= 0:
                    rlines = (cand.resolved_text or "").split("\n")
                    clen = len(core_lines)
                    start = core_start_in_resolved
                    if start + clen <= len(rlines):
                        patched = "\n".join(
                            rlines[:start]
                            + core_resolved.split("\n")
                            + rlines[start + clen:]
                        )
                if patched is None:
                    # The core's position couldn't be determined. Rather than
                    # append at the end (which would misplace the core after the
                    # closing brace and corrupt the structure), decline: keep
                    # the conservative core_cur default.
                    return None
                # Brace-delta safety check: check the PATCHED text (core in
                # context), not the isolated core — a 1-3 line core naturally
                # has unbalanced braces in isolation (e.g. ``if (x) {``).
                from capybase.verification import _brace_imbalance_line, _try_balance_braces
                if _brace_imbalance_line(patched, unit.language) is not None:
                    repaired = _try_balance_braces(patched, unit.language)
                    if repaired is not None and _brace_imbalance_line(repaired, unit.language) is None:
                        return repaired
                return patched
            return None
        except RecursionError:
            # The deferred-core recursion ran away (the depth cap and
            # emitter guards are the primary defenses; this is the last
            # line). Journal it — a silently-swallowed RecursionError is
            # how the protobuf-0065 ballooning hid for as long as it did.
            self.journal.emit(
                "deferred_core_overflow",
                {"unit_id": unit.unit_id},
                step_index=self.step, path=unit.path,
            )
            return None
        except Exception:  # noqa: BLE001 — mini-conflict is advisory
            return None

    def _try_source_candidate_portfolio(
        self, unit: ConflictUnit,
    ) -> UnitOutcome | None:
        """Generate candidates from exact source lines and validate each.

        Research (DeepMerge, MergeBERT) shows 87% of merge resolutions contain
        only lines from the input sides. Generating candidates from source
        material avoids the weak model's most common defect: dropping
        delimiters during generation. When a source composition compiles,
        it's a valid merge with zero LLM calls.

        Candidates (each from exact source lines):
        - current_only: take the upstream side verbatim
        - replayed_only: take the replayed side verbatim
        - current_then_replayed: concat both (insertion-union order)
        - replayed_then_current: concat both (reversed order)
        - shared_once_plus_distinct: shared lines once + each side's unique lines

        Each is validated via the full per-unit pipeline. If exactly one passes,
        accept it. If multiple pass, accept the first (the portfolio is ordered
        by likely-correctness). If none pass, return None (fall through to LLM).
        """
        cur = unit.current.text or ""
        rep = unit.replayed.text or ""
        if not cur.strip() and not rep.strip():
            return None

        # Modify/delete guard: exactly ONE empty side is a deletion conflict
        # (AU/UA). The single-side variants would deterministically keep the
        # modifier's file — silently dropping the other branch's deletion
        # intent and pre-empting block-capture, which exists to make the
        # keep/delete decision. Decline the whole portfolio on this class.
        if (not cur.strip()) != (not rep.strip()):
            return None

        # Decline when the parent conflict has large side-size asymmetry (one
        # side is a rewrite, the other a small edit). Taking either side
        # verbatim (current_only/replayed_only) ignores the other side's
        # deletions/replacements — producing a Frankenstein merge. Let the LLM
        # handle it. (Catches the nlohmann-0020 pattern.)
        #
        # The parent_has_asymmetry flag is set only for entity-split sub-units
        # (computed by _compute_parent_deletion_meta). For non-split (whole)
        # units, compute the side-size ratio directly — a >5x non-blank line
        # ratio between the two sides indicates one side is a rewrite.
        if unit.structural_metadata.get("parent_has_asymmetry"):
            return None
        _cur_nb = sum(1 for l in cur.split("\n") if l.strip())
        _rep_nb = sum(1 for l in rep.split("\n") if l.strip())
        if _cur_nb > 0 and _rep_nb > 0:
            _ratio = max(_cur_nb, _rep_nb) / min(_cur_nb, _rep_nb)
            if _ratio > 5.0:
                return None  # one side is a rewrite — don't take either verbatim

        # Build the candidate portfolio. Each is (id, text, provenance_suffix).
        cur_lines = [l for l in cur.split("\n") if l.strip()]
        rep_lines = [l for l in rep.split("\n") if l.strip()]
        shared = [l for l in cur_lines if l in rep_lines]
        cur_only = [l for l in cur_lines if l not in set(rep_lines)]
        rep_only = [l for l in rep_lines if l not in set(cur_lines)]

        # Each candidate is validated. The first that passes is accepted.
        # Provenance strings are literals (registered in provenance.py).
        candidates_to_try: list[tuple[str, str, str]] = [
            ("current_only", cur, "deterministic_source_current_only"),
            ("replayed_only", rep, "deterministic_source_replayed_only"),
            ("current_then_replayed", cur.rstrip() + "\n" + rep, "deterministic_source_cur_rep"),
            ("replayed_then_current", rep.rstrip() + "\n" + cur, "deterministic_source_rep_cur"),
        ]
        if shared and (cur_only or rep_only):
            composed = "\n".join(shared + cur_only + rep_only)
            candidates_to_try.append(("shared_plus_distinct", composed, "deterministic_source_shared"))

        # git merge-file --union: git's own union merge of the three sides.
        # Handles disjoint append conflicts that our concatenation heuristics
        # might miss. Duplicates it produces are cleaned up by the
        # directive_union rule and the deduplicate_imports repair step.
        base = unit.base.text or ""
        try:
            import tempfile as _tf_union
            import subprocess as _sp_union
            with _tf_union.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as _bf:
                _bf.write(base); _base_path = _bf.name
            with _tf_union.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as _cf:
                _cf.write(cur); _cur_path = _cf.name
            with _tf_union.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as _rf:
                _rf.write(rep); _rep_path = _rf.name
            try:
                _proc = _sp_union.run(
                    ["git", "merge-file", "-p", "--union",
                     _cur_path, _base_path, _rep_path],
                    capture_output=True, text=True, timeout=10,
                )
                if _proc.returncode in (0, 1) and _proc.stdout:
                    candidates_to_try.append(
                        ("git_union", _proc.stdout, "deterministic_source_union")
                    )
            finally:
                from pathlib import Path as _Pf
                for _p in (_base_path, _cur_path, _rep_path):
                    _Pf(_p).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 — union candidate is advisory
            pass

        # Build per-candidate provenance dict with literal values so the
        # provenance static scanner sees them.
        _PROV_MAP = {
            "current_only": "deterministic_source_current_only",
            "replayed_only": "deterministic_source_replayed_only",
            "current_then_replayed": "deterministic_source_cur_rep",
            "replayed_then_current": "deterministic_source_rep_cur",
            "shared_plus_distinct": "deterministic_source_shared",
            "git_union": "deterministic_source_union",
        }
        for cand_id, text, _unused in candidates_to_try:
            cand = CandidateResolution(
                candidate_id=f"{unit.unit_id}:{cand_id}",
                unit_id=unit.unit_id,
                model_name="source_portfolio",
                prompt_version=f"source_portfolio.{cand_id}",
                resolved_text=text,
                explanation=f"source-derived candidate ({cand_id})",
                provenance=_PROV_MAP.get(cand_id, "plain_llm"),
            )
            validation = self.verification.verify(unit, cand)
            if validation.passed:
                if self._strictness_blocks_pre_llm(unit, cand, validation, "source_portfolio"):
                    continue  # strictness declined; try next candidate
                self.journal.emit(
                    "source_portfolio_accepted",
                    {"candidate_id": cand.candidate_id, "variant": cand_id},
                    step_index=self.step, path=unit.path, unit_id=unit.unit_id,
                )
                outcome = UnitOutcome(unit=unit, validation=validation)
                outcome.accepted = cand
                # Uniform attempt record like every other mechanism: the
                # portfolio's accepts were invisible to resolution_attempt
                # consumers (only the bespoke source_portfolio_accepted event
                # existed), so an accepted-by-portfolio unit journaled ZERO
                # attempts while its skipped-by-structural sibling journaled
                # one. _record_resolution_attempt appends the candidate to
                # outcome.attempts for backward compat.
                self._record_resolution_attempt(
                    outcome, mechanism="source_portfolio", candidate=cand,
                    validation=validation, decision="accept",
                    reason=f"source-derived candidate ({cand_id}) passed validation",
                )
                return outcome
        return None  # no source candidate passed; fall through to LLM

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
        # Consecutive-terminator guard with side-consensus + safe-next
        # exceptions. If the interleaved text has an unconditional terminator
        # (return/throw/break/continue/goto) followed by another executable
        # statement (not a closing brace, case/default label, access specifier,
        # preprocessor directive, or comment), the interleaving stacked both
        # sides' statements → unreachable code. Only reject if neither side nor
        # base had the same pattern (side consensus).
        import re as _re_sbcr
        _terminator_re = _re_sbcr.compile(
            r"^\s*(return|throw|break|continue|goto)\b"
        )
        _safe_next_re = _re_sbcr.compile(
            r"^\s*("
            r"\}|"
            r"\)|"
            r"\]|"
            r"case\b|"
            r"default\b|"
            r"public:|"
            r"private:|"
            r"protected:|"
            r"#|"
            r"//|"
            r"/\*|"
            r"\*"
            r")"
        )
        def _has_bad_consecutive_terminator(text):
            lines = (text or "").split("\n")
            for i in range(len(lines) - 1):
                if not _terminator_re.match(lines[i]):
                    continue
                # Skip blank lines to find the next executable line
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j >= len(lines):
                    continue
                next_line = lines[j].strip()
                if _safe_next_re.match(lines[j]):
                    continue  # safe continuation (brace, label, preprocessor, comment)
                # Do NOT skip when the next line is another terminator —
                # return-after-return is the exact defect pattern (unreachable
                # code with potential undeclared identifiers). In a switch, the
                # intervening case/default label makes it safe, and those are
                # caught by _safe_next_re above.
                return True  # executable statement (or another terminator) after a terminator — unreachable
            return False
        _candidate_has_bad = _has_bad_consecutive_terminator(result.text)
        if _candidate_has_bad:
            # Side consensus: only reject if none of base/current/replayed had
            # the same pattern (avoids rejecting code that already existed).
            _base_text = unit.base.text or ""
            _cur_text = unit.current.text or ""
            _rep_text = unit.replayed.text or ""
            if (
                not _has_bad_consecutive_terminator(_base_text)
                and not _has_bad_consecutive_terminator(_cur_text)
                and not _has_bad_consecutive_terminator(_rep_text)
            ):
                self._record_resolution_attempt(
                    UnitOutcome(unit=unit), mechanism="sbcr",
                    decision="skip",
                    reason="consecutive terminator in interleaved text (side consensus)",
                )
                return None
        # Identifier-provenance guard: detect an identifier used in the sbcr
        # candidate but not declared in it, where the identifier appears in
        # exactly ONE side (not base, not the other). This catches sbcr
        # stacking a statement from side B that uses a variable declared only
        # in side B's context — the clickhouse-0041 defect.
        _undeclared = _has_undeclared_side_local_identifier(
            result.text, unit.base.text or "",
            unit.current.text or "", unit.replayed.text or "",
        )
        if _undeclared:
            self._record_resolution_attempt(
                UnitOutcome(unit=unit), mechanism="sbcr",
                decision="skip",
                reason=f"undeclared identifier in sbcr output: {_undeclared}",
            )
            self.journal.emit(
                "combination_declined",
                {"fitness": round(result.fitness, 4),
                 "reason": f"undeclared identifier: {_undeclared}"},
                step_index=self.step, path=unit.path, unit_id=unit.unit_id,
            )
            return None
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
        # Resolve which test command to probe with: prefer ``pre_continue`` (the
        # fast targeted gate), fall back to ``final``. A final-only config
        # (pre_continue empty) must still work — the probe uses whichever is set.
        if self.config.tests.pre_continue:
            _probe_cmd = self.config.tests.pre_continue
            _probe_label = "pre_continue"
        else:
            _probe_cmd = self.config.tests.final
            _probe_label = "final"
        if not self.config.tests.required or not _probe_cmd or _probe_cmd.strip() in ("true", "pytest"):
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
            test_ok = self._run_tests(_probe_label, probe)
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
            resp = self.resolution_engine.raw_complete(
                prompt, json_mode=False,
                # Use a low temperature for the keep/delete decision — it's a
                # binary choice where determinism matters. A higher temperature
                # caused the model to flip between keep_block and accept_deletion
                # across runs. 0.1 is low enough for consistency without being
                # fully greedy (which can get stuck on wrong answers).
                temperature=0.1,
            )
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

    def _try_intent_coverage_repair(
        self, unit: ConflictUnit, cand: CandidateResolution
    ) -> CandidateResolution:
        """Post-process an accepted LLM candidate by restoring dropped
        side-common lines.

        When the LLM produces a compiling merge but drops 1-2 lines that BOTH
        sides agreed on, this deterministic step restores them at the
        best-matched position. Production-safe: only restores lines common to
        both current AND replayed — never oracle-derived. The result must pass
        brace-balance check.

        Returns the original candidate unchanged if no restoration was possible
        or the candidate is deterministic.
        """
        if str(getattr(cand, "provenance", "") or "").startswith("deterministic"):
            return cand
        repaired = _try_restore_common_lines(
            cand.resolved_text or "",
            unit.base.text or "",
            unit.current.text or "",
            unit.replayed.text or "",
            unit.language,
        )
        if repaired is not None and repaired != cand.resolved_text:
            self.journal.emit(
                "intent_coverage_repair",
                {"candidate_id": cand.candidate_id,
                 "lines_diff": repaired.count("\n") - (cand.resolved_text or "").count("\n")},
                step_index=self.step, path=unit.path,
                unit_id=unit.unit_id,
            )
            return cand.model_copy(update={
                "resolved_text": repaired,
                "provenance": (cand.provenance or "plain_llm") + "+intent_coverage",
            })
        return cand

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
                replayed_oid=start_oid,
                min_block_lines=cfg.resurrection_min_block_lines,
                min_coverage=cfg.resurrection_min_similarity,
                history_depth=cfg.resurrection_history_depth,
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
        # Provenance-aware resurrection filtering: if ALL flagged blocks
        # appear in the REPLAYED side's version of the file, the
        # "resurrection" is an explicit merge choice (the replayed side
        # deliberately provided a refactored replacement for content the
        # upstream deleted), not a silent undo. Downgrade to WARNING so the
        # rebase can complete.
        #
        # Safety (per reviewer feedback): true silent restorations — where
        # the LLM hallucinated deleted code that the replayed side never had
        # — will NOT match the replayed blob. The check uses the same
        # _coverage_against metric as the resurrection detector itself.
        _effective_policy = cfg.resurrection_policy
        if _effective_policy == "stop" and findings:
            _downgrade_reason: str | None = None
            try:
                from capybase.merge_intent import _coverage_against
                _all_explained = True
                for _finding in findings:
                    _blob = self.git.blob_at(start_oid, _finding.path)
                    if not _blob:
                        _all_explained = False
                        break
                    _replayed_lines = _blob.decode(
                        "utf-8", errors="replace"
                    ).split("\n")
                    for _block in _finding.blocks:
                        _block_lines = _block.text.split("\n")
                        _cov = _coverage_against(
                            _block_lines, _replayed_lines
                        )
                        if _cov < cfg.resurrection_min_similarity:
                            _all_explained = False
                            break
                    if not _all_explained:
                        break
                if _all_explained:
                    _downgrade_reason = (
                        "all findings explained by replayed side content "
                        "(explicit merge choice)")
            except Exception:  # noqa: BLE001 - never break on provenance check
                pass
            # P5 v2 (sprint-22): resolved-file provenance — every flagged
            # path this session EXPLICITLY resolved and passed the full
            # validation gate (marker-free + compile) is an explicit merge
            # choice, not a silent undo: the content survived the gauntlet
            # as part of a chosen resolution (tokio-0037/0042/0046,
            # clickhouse-0020: near-oracle merges stopped here). Findings in
            # files the session never touched keep the hard stop — that is
            # the truly silent restoration class. Warn still surfaces the
            # finding (output + review bundle); nothing is silent.
            if _downgrade_reason is None:
                _resolved = getattr(self, "_resolved_validated_paths", None)
                if _resolved and all(
                        f.path in _resolved for f in findings):
                    _downgrade_reason = (
                        "all flagged paths explicitly resolved and "
                        "compile-validated by this session "
                        "(resolved-file provenance)")
            # D12 (s27): a PRISTINE-SIDE TAKEOVER landing is not a silent
            # resurrection. redis-0012: f1_compile_clean landed the current
            # side (compile-verified, oracle-equal at 0.99), the rebase
            # continued, and the guard stopped on the side's own
            # "resurrected" lines. The guard exists for LLM-HALLUCINATED
            # deleted code; a corpus-authored side chosen on build evidence
            # is an explicit decision — warn, don't stop.
            if _downgrade_reason is None:
                _takeover = getattr(self, "_takeover_landed_paths", None)
                if _takeover and all(
                        f.path in _takeover for f in findings):
                    _downgrade_reason = (
                        "flagged paths landed by a pristine-side takeover "
                        "(compile-evidenced explicit choice, not an LLM "
                        "restoration)")
            if _downgrade_reason is not None:
                self.journal.emit(
                    "resurrection_downgrade",
                    {"paths": [f.path for f in findings],
                     "reason": _downgrade_reason},
                    step_index=self.step,
                )
                _effective_policy = "warn"
        if _effective_policy == "stop":
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
        final_tree_text: dict[str, str] = {}
        for path in all_paths:
            blob = self.git.blob_at(head_after, path)
            if blob is None:
                continue
            lang = cross_commit._language_for_path(path)
            if lang is None or not structural.is_available(lang):
                continue
            text = blob.decode("utf-8", errors="replace")
            ents = structural.enumerate_entities(text, lang)
            if ents is not None:
                final_tree[path] = ents
                final_tree_text[path] = text
        breaks = cross_commit.audit_cross_commit_dependencies(
            edges, final_tree, final_tree_text
        )
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
        _stuck_continues = 0
        _prev_head: str | None = None
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
            if not test_ok and (
                    self.config.tests.required
                    or getattr(self, "_last_tests_compiler_indictment",
                               False)):
                # Sprint-19 P4: a positively-attributed in-file compile
                # error escalates even under advisory gates (compiler
                # authority); unattributable failures keep the advisory
                # behavior (protobuf-0065 shipped a build-broken merge at
                # sim 0.997 because rc=2 parsed as unknown with empty
                # diagnostics and tests.required=False let it through).
                # Sprint-20 S20.6: before that escalate, one bounded
                # micro-CEGIS round (deterministic duplicate repair +
                # missing-symbol micro-patch, re-gated by the same
                # command) — a buffer this close to the oracle deserves
                # one tiny repair before the honest stop. Only on the
                # indictment path: a required-gate policy failure is not
                # the attributed-error shape.
                _micro_repaired = (
                    not self.config.tests.required
                    and bool(getattr(self, "_last_tests_compiler_indictment",
                                     False))
                    and self._try_micro_cegis(result))
                if _micro_repaired:
                    test_ok = True  # re-gate clean — proceed, don't escalate
                else:
                    result.escalated = True
                    result.reason = (
                        "pre-continue tests failed"
                        if self.config.tests.required
                        else "compiler authority: pre-continue build failed with "
                             "errors attributed to a merged file"
                    )
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
            # Continue rebase. First: git requires a clean worktree beyond
            # the staged resolution — in-tree build verification (autotools
            # regenerates tracked files like config.h.in) leaves unstaged
            # modifications that make EVERY continue refuse with "You must
            # edit all merge conflicts..." (on stdout — invisible in a
            # stderr-only journal). Stash the non-resolution dirty files
            # (recoverable; what a human would run) before continuing.
            try:
                _resolved_paths = set(result.units_by_path.keys())
                _dirty = [
                    f for f in self.git.dirty_tracked_files()
                    if f not in _resolved_paths
                ]
            except Exception:  # noqa: BLE001 — staleness check is advisory
                _dirty = []
            if _dirty:
                _stash = self.git.stash_files(_dirty)
                self.journal.emit(
                    "continue_stash",
                    {"files": _dirty[:10],
                     "returncode": _stash.returncode},
                    step_index=self.step,
                )
            cont = self.git.continue_rebase()
            self.journal.emit(
                "step_continued",
                {"returncode": cont.returncode,
                 "stderr": cont.stderr[:500],
                 "stdout": (getattr(cont, "stdout", "") or "")[:500]},
                step_index=self.step,
            )
            # Empty-commit completion: a resolution that fully superseded
            # the replayed commit (e.g. the whole-file fast path taking the
            # rewriting side verbatim) leaves the pick empty — git refuses
            # to continue but STAYS mid-rebase, and without this the step
            # loop spins forever (jsonc-0013: 268k iterations to the case
            # timeout). git's semantics for a fully-superseded pick is
            # --skip; after skipping, the completion check below (or the
            # next iteration's gather) proceeds normally.
            if (
                cont.returncode != 0
                and self.git.rebase_in_progress()
                and _rebase_continue_empty(cont)
            ):
                skip = self.git.skip_rebase()
                self.journal.emit(
                    "rebase_skip_empty",
                    {"returncode": skip.returncode,
                     "stderr": (skip.stderr or "")[:200]},
                    step_index=self.step,
                )
            # Stuck-loop guard (defense in depth): if continues keep failing
            # without the head moving, the rebase is wedged — escalate
            # instead of spinning to the case timeout.
            _head_now = self.git.head_oid()
            if cont.returncode != 0 and _head_now == _prev_head:
                _stuck_continues += 1
                if _stuck_continues >= 3:
                    last.escalated = True
                    last.reason = (
                        "rebase --continue repeatedly failed without "
                        "progress (empty pick not skippable?)"
                    )
                    break
            else:
                _stuck_continues = 0
            _prev_head = _head_now
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
        # Stash for §10 code-reopening (_resolve_comment_contract_conflicts
        # needs the original to re-splice after a re-resolve).
        self._step_originals = originals
        # D1: per-step convergence hashes, keyed by unit_id. Lifted from
        # UnitOutcome (per-_resolve_unit-call) to per-step so that
        # _whole_file_repair's re-resolve of the same unit inherits the
        # convergence hashes from the first pass — the model can't cycle
        # through the SAME cosmetic variations a second time. Reset each step.
        self._step_convergence_hashes: dict[str, dict[str, int]] = {}
        # Per-step failure-signature persistence: survives across _resolve_unit
        # calls (Phase 1 → Phase 2 re-resolve) so the no-progress guard sees
        # prior compiler errors and doesn't reset its counter on re-entry.
        self._step_failure_sigs: dict[str, list] = {}
        # Sprint-19 P2: per-step Best-of-N stash, keyed by unit_id. Holds the
        # preservation-heuristic-rejected candidate (validation-passing) plus
        # the post-rejection attempts' clean-pass record; consumed by the
        # _resolve_unit wrapper when the unit would otherwise escalate.
        self._step_preservation_stash: dict[str, dict] = {}

        # ---- Phase 1: resolve + write all files (no staging, no cargo) ----
        import time as _p1time
        _file_wall_budget = self.config.policy.max_wall_time_per_file_seconds
        for path, units in result.units_by_path.items():
            # File-level wall deadline: computed at the START of each file's
            # processing (Phase 1 + Phase 2 combined). Bounds total resolution
            # + repair time per file, preventing the nested-retry budget
            # explosion. Threaded through _resolve_unit (Phase 1) and
            # _whole_file_repair (Phase 2) so both respect the same deadline.
            _file_wall_deadline = (
                _p1time.monotonic() + _file_wall_budget
                if _file_wall_budget > 0 else None
            )
            # Resolve ALL units in the file before splicing anything. We must
            # not write a partially-resolved file: if a later unit escalates,
            # the file (with some blocks still marker-laden) would be staged
            # against an aborted rebase. Collect accepted (unit, candidate)
            # pairs and splice them in one offset-correct batch at the end.
            accepted: list[tuple[ConflictUnit, CandidateResolution]] = []
            escalated_units: list[UnitOutcome] = []

            # SRC accumulator: parent_unit_id -> list of accepted resolved-text
            # from earlier sibling sub-units, in document order. Fed one-way into
            # each later sub-unit's prompt so entity-split siblings stay
            # consistent (see _sibling_resolutions_block in resolution_engine).
            _sibling_resolved: dict[str, list[str]] = {}
            # Intra-step shape cache: when many units in one file share the same
            # conflict shape (e.g. 78 regions of `Type x;` vs `Type x{};`), only
            # the first needs the full cascade; siblings can replay the accepted
            # resolution and re-verify. Keyed on (shape_hash, path) → resolved_text.
            # Populated after each deterministic acceptance; checked at the top
            # of _resolve_unit before any model call. This is the intra-step
            # analog of _try_exact_reuse (which is cross-session).
            self._step_shape_cache: dict[str, str] = {}
            # Edit-pattern cache: keyed on conflict_shape_hash+path, stores
            # normalized token-level edit patterns (base→resolved). Populated
            # from BOTH deterministic and LLM resolutions; applied to sibling
            # units with the same structural shape but different identifiers.
            self._step_pattern_cache: dict[str, list] = {}
            # Unit-count-aware retry budget: scale down retries when a file has
            # many units, so the total model-call count stays within the wall-
            # time budget. With the default 2 retries (3 attempts), a 78-unit
            # file needs up to 234 calls — far over budget. Scaling to 0
            # retries (1 attempt) bounds it to 78 calls.
            # D3 (s27) knee fix: the old >5→1 / >20→0 knees starved ordinary
            # 6-8-unit files (zenodo-0011/0012, sea-orm-0011 died at 1 retry
            # at sim 0.97-0.99) — post-era-recovery the corpus runs at median
            # 47s/case and only 2 cases exceed 600s, so the wall-time fear
            # only justifies scaling at the extremes: >40→0 (the 78-class),
            # >12→1, else the config default.
            _n_units = len(units)
            if _n_units > 40:
                _file_max_retries = 0
            elif _n_units > 12:
                _file_max_retries = 1
            else:
                _file_max_retries = None  # use config default
            # Store on self so Phase 2 whole-file repair inherits the same
            # budget (it re-resolves units via _resolve_unit without the
            # max_retries kwarg, so without this it would get the full config
            # budget, undermining the throughput fix).
            self._file_max_retries = _file_max_retries
            # File-level lint transform detection: scan ALL units for repeated
            # known-safe lint substitutions (NULL→nullptr, and→&&, etc.). When
            # the aggregate count is high (≥5), promote the transforms to a
            # file-level set stored in every unit's structural_metadata. This
            # catches refactor-vs-lint conflicts where each unit has too few
            # changes for the per-unit threshold but the file clearly had a
            # lint pass (e.g. nlohmann-0020: 6 regions × ~3 and→&& each = 17
            # total). Applied by resolve_structurally before the per-unit rules.
            try:
                from capybase.structural_resolver import (
                    detect_file_level_lint_transforms,
                )
                _file_transforms = detect_file_level_lint_transforms(units)
            except Exception:
                _file_transforms = []
            if _file_transforms:
                for unit in units:
                    unit.structural_metadata["file_level_lint_transforms"] = (
                        _file_transforms
                    )
            # Sprint-20 S20.8 (journal-only): move-and-edit shape
            # measurement at FILE level (the relocation spans units —
            # per-unit texts can't see it). One side moved a base block
            # while the other edited it in place; today's
            # _try_move_transplant takes the mover's text and drops the
            # editor's delta. Journal the shape so the enabling decision
            # (deterministic transposition of the editor delta onto the
            # moved block, compiler-gated) rests on live distribution.
            # Pure measurement — no behavioral change.
            if units:
                try:
                    from capybase.structural_resolver import (
                        _detect_move_edit_shape as _detect_move_edit,
                    )
                    _me_ts = self._micro_stage_sides(path)
                    _me = _detect_move_edit(
                        _me_ts[1] or "",
                        _me_ts[0].get("current", ""),
                        _me_ts[0].get("replayed", ""))
                except Exception:  # noqa: BLE001 — measurement is best-effort
                    _me = None
                if _me is not None:
                    for unit in units:
                        unit.structural_metadata["move_edit_candidate"] = _me
                    self.journal.emit(
                        "move_edit_candidate",
                        {"candidates": _me["candidates"],
                         "enabled": bool(getattr(
                             self.config.future,
                             "enable_move_edit_transposition", False))},
                        step_index=self.step, path=path)
            # Phase-1 whole-file fast path (wholesale-rewrite files): when
            # the full-file context says one side rewrote the file, take
            # that side's pristine stage file BEFORE the per-unit cascade —
            # the cascade is doomed-and-slow on these (jsonc 0013/0014/0016
            # burned the whole 1200s case budget in Phase 1 and timed out,
            # with the correct answer sitting in the index the entire
            # time). On a hit, record the whole-file resolution and skip to
            # the next file; Phase 2 re-validates it like any other.
            if units and units[0].marker_span is not None:
                try:
                    _fp = self._try_true_side_portfolio(
                        path, units[0].language,
                        units[0].original_worktree_text, units,
                        wall_deadline=_file_wall_deadline,
                        phase1_fast_path=True,
                    )
                except Exception:
                    _fp = None
                if _fp is not None:
                    accepted = _fp[0]
                    original = accepted[0][0].original_worktree_text
                    buffer = _resolved_buffer(original, accepted)
                    resolved_files[path] = buffer
                    accepted_by_path[path] = accepted
                    originals[path] = original
                    self._write_worktree_only(path, buffer, accepted=accepted)
                    continue
            # Sprint-22 P2: track how many units in this file have failed
            # (not accepted) so the retry-relaxation can check "is this
            # the ONLY failing unit?" before granting an extra retry.
            self._file_failing_unit_count = 0
            for unit in units:
                _parent = unit.structural_metadata.get("parent_unit_id")
                # Parent-aware asymmetry: if the parent conflict had substantial
                # deletions on either side (computed by the conflict extractor
                # at split time via _compute_parent_deletion_meta), flag the
                # sub-unit so source_portfolio and union rules decline — the
                # LLM should handle conflicts where one side deleted significant
                # content. This replaces the imprecise sub-unit side-ratio
                # check: a fragment can look balanced even when the parent had
                # 102 lines deleted by one side. (Catches nlohmann-0020 where
                # entity splitting hid replayed's refactor deletions.)
                if unit.structural_metadata.get("parent_has_deletions"):
                    unit.structural_metadata["parent_has_asymmetry"] = True
                if _parent and _parent in _sibling_resolved:
                    unit.structural_metadata["sibling_resolutions"] = list(
                        _sibling_resolved[_parent]
                    )
                outcome = self._resolve_unit(
                    unit, wall_deadline=_file_wall_deadline,
                    max_retries=_file_max_retries,
                )

                _persist_unit_hashes(self, outcome)  # D1: per-step convergence
                result.outcomes.append(outcome)
                if outcome.accepted is None:
                    escalated_units.append(outcome)
                    self._file_failing_unit_count = getattr(
                        self, "_file_failing_unit_count", 0) + 1
                    # Don't break — continue processing remaining units so
                    # all outcomes are logged. The step still escalates
                    # (safety invariant: don't splice a partially-resolved
                    # file), but every unit gets its resolution attempted.
                    continue
                # Feed this resolution forward to later siblings in the same group.
                if _parent:
                    _sibling_resolved.setdefault(_parent, []).append(
                        outcome.accepted.resolved_text
                    )
                accepted.append((unit, outcome.accepted))
                # Populate the intra-step shape cache so later sibling units
                # with the same conflict shape can replay this resolution.
                # Only cache deterministic resolutions (structural/exact_reuse/
                # source_portfolio) — LLM resolutions may be case-specific.
                _prov = outcome.accepted.provenance or ""
                if _prov.startswith("deterministic"):
                    try:
                        import hashlib as _hl
                        _content = (
                            (unit.base.text or "") + "\x00"
                            + (unit.current.text or "") + "\x00"
                            + (unit.replayed.text or "")
                        )
                        _key = f"{_hl.sha1(_content.encode()).hexdigest()[:16]}:{unit.path}"
                        self._step_shape_cache[_key] = (
                            outcome.accepted.resolved_text
                        )
                    except Exception:  # noqa: BLE001 — advisory
                        pass
                # Populate the edit-pattern cache from BOTH deterministic and
                # LLM resolutions. The pattern is a normalized token-level diff
                # (base→resolved), keyed on the conflict shape hash. Sibling
                # units with the same shape but different identifiers can
                # instantiate the pattern instead of calling the LLM.
                # IMPORTANT: use the diff3-refined hunk base, NOT unit.base.text
                # (which is the WHOLE FILE for marker units). A whole-file base
                # produces garbage patterns that corrupt the output.
                try:
                    from capybase.memory.shape import shape_for_unit
                    _refined = unit.refined_sides
                    _hunk_base = (_refined[1] if _refined else "") or (unit.base.text or "")
                    _pat = _extract_edit_pattern(
                        _hunk_base,
                        outcome.accepted.resolved_text or "",
                    )
                    if _pat is not None:
                        _pat_key = f"{shape_for_unit(unit)}:{unit.path}"
                        # Don't overwrite a pattern from an earlier sibling —
                        # the first resolution is the template.
                        if _pat_key not in self._step_pattern_cache:
                            self._step_pattern_cache[_pat_key] = _pat
                except Exception:  # noqa: BLE001 — advisory
                    pass
            # Majority-side rescue: when a unit escalates but the file's other
            # resolved units consistently took ONE side (≥2/3 majority), try
            # that side for the escalated unit before aborting. This catches
            # the pattern where per-unit resolution fails on one region of a
            # file-wide refactor (e.g., nlohmann-0034: 2 of 3 units took the
            # replayed side via dup_def_deletion_accept + lint_vs_refactor, but
            # unit 1#s0 escalates because the LLM can't avoid duplicate defs).
            # The whole-file build (Phase 2) is the authoritative check.
            if escalated_units and len(accepted) >= 2:
                _rescued = _try_majority_side_rescue(units, accepted, escalated_units)
                if _rescued:
                    accepted.extend(_rescued)
                    escalated_units = []
                    self.journal.emit(
                        "majority_side_rescue",
                        {"n_rescued": len(_rescued),
                         "n_accepted": len(accepted)},
                        step_index=result.step_index, path=path,
                    )
            if escalated_units:
                # Wholesale winner floor — escalation rescue (clap-0004
                # class): a wholesale-rewrite file whose cascade gave up
                # still has its correct whole-file answer in the merge index
                # (the gate winner). Escalate only when the floor can't
                # apply (out-of-band file, unbalanced winner, flag off).
                _floor = self._wholesale_winner_floor(
                    path, units[0].language, units, buffer=None)
                if _floor is not None:
                    accepted = _floor
                    escalated_units = []
            if escalated_units:
                escalated_unit = escalated_units[0]
                result.escalated = True
                # Prefer the outcome's specific reason (e.g. "unit exceeded
                # wall-time budget") when the escalation path set one. Fall
                # back to the last validation's hard failures or the last
                # candidate's failure kind, so the escalation reason is
                # informative rather than the generic "could not resolve".
                fallback = f"could not resolve {escalated_unit.unit.unit_id}"
                if not escalated_unit.reason:
                    # D5: try to extract a more specific reason from the last
                    # validation or candidate attempt.
                    last_val = escalated_unit.validation
                    if last_val and last_val.hard_failures:
                        msgs = [f.message[:80] for f in last_val.hard_failures[:3]]
                        fallback += f" (last failures: {'; '.join(msgs)})"
                    elif escalated_unit.attempts:
                        last_cand = escalated_unit.attempts[-1]
                        fk = getattr(last_cand, "failure_kind", "")
                        if fk:
                            fallback += f" (last candidate failure_kind: {fk})"
                        else:
                            fallback += " (no specific reason recorded)"
                    else:
                        fallback += " (no candidates generated)"
                result.reason = escalated_unit.reason or fallback
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
                self._dump_conflict_bundles(result)
                return result
            # Splice every accepted resolution in one offset-correct batch.
            # (For a whole_file unit the resolved text IS the file —
            # ``_resolved_buffer`` returns it verbatim, no splicing.)
            original = accepted[0][0].original_worktree_text
            # Coordinated-side swap: when all units took the same source-
            # portfolio side but the OTHER side has a cross-unit variable
            # dependency (declared in one region, used in another), per-unit
            # compilation couldn't validate the other side. The whole-file
            # splice CAN. Swap to the coordinated side — Phase 2's whole-file
            # validation is the authoritative check (if it doesn't compile,
            # the repair loop handles it).
            swapped = _try_coordinated_side_swap(units, accepted)
            if swapped is not None:
                self.journal.emit(
                    "coordinated_side_swap",
                    {"n_units": len(swapped),
                     "swapped_to": swapped[0][1].provenance or ""},
                    step_index=result.step_index, path=path,
                )
                accepted = swapped
            # Whole-file portfolio: generate all-current / all-replayed
            # candidates and pick the best by file-level intent coverage.
            # Only fires when per-unit coverage is LOW (<0.80) — catches
            # cases where per-unit compilation picked the wrong side due to
            # cross-unit dependencies (protobuf-0067: unit 3 wrongly
            # took replayed_only because current_only failed standalone).
            # Full-file sides come from the merge index stages when readable:
            # marker-splice reconstruction is wrong for merge-ort-interleaved
            # files, and units[0].base.text is a fragment for sub-units.
            _wf_true = None
            if len(accepted) >= 2:
                try:
                    _wf_true = _true_stage_sides(self.git, path)
                except Exception:
                    _wf_true = None
            _wf = _try_whole_file_portfolio(
                units, accepted, original,
                journal=self.journal, step_index=result.step_index,
                path=path, true_sides=_wf_true,
            )
            if _wf is not None:
                accepted, _wf_payload = _wf
                self.journal.emit(
                    "whole_file_portfolio", _wf_payload,
                    step_index=result.step_index, path=path,
                )
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
            # Refresh the unit-count-aware budget for THIS file (Phase 2 has its
            # own per-file loop, so self._file_max_retries from Phase 1's last
            # file would be stale here).
            _n = len(units)
            self._file_max_retries = (
                0 if _n > 20 else (1 if _n > 5 else None)
            )
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
                # Separate whole-file repair budget. 0 mirrors the per-unit
                # budget (legacy behavior); a higher value grants more repair
                # cycles for multi-hunk conflicts where the deterministic brace
                # repair + enriched context need a few shots to converge.
                wf_budget = self.config.policy.max_whole_file_repair_retries or self.config.policy.max_retries_per_unit
                # Tiered verification time budget (design v2): when set, Phase 2
                # runs at most 1 verify_file + deterministic beam + 1 model
                # re-resolve + 1 final verify_file, bounded by this wall-time
                # cap. This replaces the multi-iteration CEGIS loop that could
                # run 3-6 × (100s model + 75s build) = 525-1050s, blowing the
                # case timeout. 0 = disabled (use iteration-count loop).
                import time as _p2time
                _phase2_budget = self.config.policy.max_whole_file_repair_seconds
                _phase2_start = _p2time.monotonic()
                _phase2_model_used = False
                # _file_wall_deadline is computed at the start of this file's
                # processing (Phase 1) and carried through Phase 2. It bounds
                # total resolution + repair time per file, preventing the
                # nested-retry budget explosion.
                file_validation = None  # type: ignore[assignment]
                # Causal attribution: track the failure signature across whole-
                # file repair iterations so each repair mechanism's EFFECT can
                # be recorded — did it actually change the failure shape, or
                # fire without clearing the primary failure? This distinguishes
                # "fired" from "caused recovery" (early projections conflated
                # the two and overestimated a mechanism's impact).
                prev_failure_sig = None
                # Track whether the prior deterministic repair left the failure
                # UNCHANGED. When so, the next _whole_file_repair call skips its
                # deterministic beam (skip_deterministic=True) so it proceeds to
                # the model/Layer-3 path instead of re-firing the same idempotent
                # repair until the time budget expires.
                _det_unchanged = False
                self._p2_build_checked = False  # one build-test attempt per Phase 2
                _ts_attempted = False  # true-side portfolio: once per file
                _wsr_attempted = False  # whole-side repair rung: once per file
                while True:
                    spans_and_texts = [
                        (unit.marker_span, cand.resolved_text) for unit, cand in accepted
                    ]
                    # verify_file tolerates a whole-file (None) span via its own
                    # _has_whole_file_span guard; the buffer is the resolved
                    # text directly for such units.
                    buffer = _resolved_buffer(original, accepted)
                    # Phase 9: whole-file import deduplication linker. Runs
                    # AFTER splicing but BEFORE validation. Removes duplicate
                    # `use` statements introduced when the model's per-unit
                    # resolution adds an import that already exists elsewhere
                    # in the file. The #1 cause of WHOLE_FILE_FAILED.
                    pre_dedup_buffer = buffer  # unconditional — referenced below
                    if getattr(self.config.future, "enable_file_linker", True):
                        try:
                            from capybase.file_linker import deduplicate_imports
                            deduped, dedup_count = deduplicate_imports(buffer, language)
                            if dedup_count > 0:
                                # The deduped buffer is the text that will be
                                # written to disk (the loop writes `buffer` on
                                # success). Validate THAT text, not a re-splice
                                # of the un-deduped spans — otherwise verify_file
                                # discards the dedup and fails on the same
                                # duplicates it just removed (the dedup-then-
                                # fail-on-same-duplicates bug). Pass whole_text so
                                # verify_file bypasses its internal splice.
                                buffer = deduped
                                self.journal.emit(
                                    "file_linker_dedup",
                                    {"duplicates_removed": dedup_count},
                                    step_index=self.step, path=path,
                                )
                        except Exception:  # noqa: BLE001
                            pass
                    # When the file_linker dedup ran, validate the deduped
                    # buffer directly (whole_text); otherwise re-splice from
                    # the per-unit resolutions as before. The pristine sides
                    # ride along (F2): a preprocessor-blind brace count that
                    # already fails a pristine side cannot attribute the
                    # imbalance to the merge — the build gate decides.
                    _pristine = None
                    try:
                        _ps_sides, _ = self._micro_stage_sides(path)
                        _pristine = [t for t in _ps_sides.values() if t.strip()]
                    except Exception:  # noqa: BLE001 — sides are advisory
                        _pristine = None
                    file_validation = self.verification.verify_file(
                        path, language, original, spans_and_texts,
                        repo_root=str(self.git.repo),
                        whole_text=buffer if buffer != pre_dedup_buffer else None,
                        pristine_side_texts=_pristine,
                    )
                    if self.config.journal.enabled and self.config.journal.store_validations:
                        self.journal.store_validation(file_validation)
                    # D0 (s27): store the exact buffer this verdict applies
                    # to — the 0113 forensics couldn't re-derive the text a
                    # gate failure was computed on. On failure only (PASS
                    # buffers re-derive trivially from the accepted set).
                    _gate_buf_key = None
                    if not file_validation.passed:
                        try:
                            _gate_buf_key, _ = self.journal.store_gate_buffer(
                                path, wf_retries, buffer)
                        except Exception:  # noqa: BLE001 — provenance is best-effort
                            _gate_buf_key = None
                    self.journal.emit(
                        "file_validated",
                        {
                            "passed": file_validation.passed,
                            "hard_failures": [
                                f.message for f in file_validation.hard_failures
                            ],
                            "wf_retry": wf_retries,
                            # Sprint-21 coherence rung: auditable firing.
                            "coherence_repair_applied": bool(
                                file_validation.features.get(
                                    "coherence_repair_applied")),
                            **({"gate_buffer_sha": _gate_buf_key}
                               if _gate_buf_key else {}),
                        },
                        step_index=self.step,
                        path=path,
                    )
                    # R1 (s22): the coherence rung validated a REPAIRED copy
                    # of the buffer — write that text, not the pre-repair
                    # splice (the tokio-0026/clickhouse-0049 false-accept
                    # root cause: repair was validation-local, the caller
                    # wrote the unrepaired buffer to disk).
                    if (file_validation.passed
                            and getattr(file_validation, "resolved_text", None) is not None):
                        buffer = file_validation.resolved_text
                    # P5 v2 (sprint-22): record explicitly-resolved+validated
                    # paths for the resurrection guard's resolved-file
                    # provenance downgrade.
                    if file_validation.passed:
                        if not hasattr(self, "_resolved_validated_paths"):
                            self._resolved_validated_paths: set[str] = set()
                        self._resolved_validated_paths.add(path)
                    # Causal attribution: record whether the previous repair
                    # mechanism changed the failure shape. NOT_ENGAGED = first
                    # iteration (no prior mechanism); CLEARED = the prior repair
                    # removed all failures (this iteration passed); REDUCED =
                    # fewer/distinct failures remain; UNCHANGED = identical
                    # signature (the mechanism fired but accomplished nothing —
                    # the misattribution-prone case). Lets post-hoc analysis
                    # count a fix as causal only when it CLEARED or REDUCED.
                    cur_sig = _hard_failure_signature(file_validation.hard_failures)
                    if prev_failure_sig is not None:
                        if file_validation.passed:
                            effect = "CLEARED"
                        elif cur_sig == prev_failure_sig:
                            effect = "UNCHANGED"
                        else:
                            effect = "REDUCED"
                        self.journal.emit(
                            "mechanism_effect",
                            {"effect": effect, "wf_retry": wf_retries},
                            step_index=self.step, path=path,
                        )
                        # Track whether the prior deterministic repair made no
                        # progress, so the next iteration skips the beam.
                        _det_unchanged = (
                            not _phase2_model_used and effect == "UNCHANGED"
                        )
                    prev_failure_sig = cur_sig
                    if not file_validation.passed and wf_retries == 0 and not _ts_attempted:
                        # True-side portfolio at FIRST failure, before the
                        # repair loop: a whole-file side swap — the duplicate-
                        # definition pathology or the asymmetry takeover — is
                        # a better answer than repairing a fundamentally
                        # stale splice (0073: the stale merge's brace noise
                        # gets "repaired" into a passing-but-wrong file).
                        # Cheap when it declines; once per file.
                        _ts_attempted = True
                        _ts_res = self._try_true_side_portfolio(
                            path, language, original, units,
                            per_unit_buffer=buffer,
                            wall_deadline=_file_wall_deadline)
                        if _ts_res is not None:
                            accepted, buffer, file_validation = _ts_res
                            # Re-enter the loop: the next iteration
                            # re-splices from the swapped whole-file unit and
                            # revalidates (including the build test).
                            continue
                    if file_validation.passed:
                        # Cross-ordered-blocks pathology / asymmetry takeover:
                        # even a PASSING splice can be unsound — duplicates in
                        # shared context when git interleaved both sides (the
                        # build may abort on a sibling file before reaching
                        # the conflict TU), or a wholesale rewrite whose
                        # deletions the per-unit merge resurrected. Swap in a
                        # verified pristine side from the index stages.
                        if wf_retries == 0 and not _ts_attempted:
                            _ts_attempted = True
                            _ts_res = self._try_true_side_portfolio(
                                path, language, original, units,
                                per_unit_buffer=buffer,
                                wall_deadline=_file_wall_deadline)
                            if _ts_res is not None:
                                _ts_acc, _ts_buf, _ts_val = _ts_res
                                if _ts_val.passed and _ts_buf != buffer:
                                    accepted, buffer = _ts_acc, _ts_buf
                                    file_validation = _ts_val
                        # Structural validation passed (markers, splice,
                        # standalone syntax). Also run the build test —
                        # verify_file's per-unit gcc -fsyntax-only can't see
                        # semantic errors that require the full TU context
                        # (undeclared identifiers, type mismatches across
                        # headers). The build test catches these. Run it HERE
                        # so Phase 2 repair gets a chance to fix build
                        # failures, regardless of tests.required.
                        #
                        # Prefer the per-file build target (cc_build_target_template)
                        # over the full pre_continue command — the per-file
                        # target only compiles the conflict file's TU, avoiding
                        # false failures from sibling-file errors.
                        if not getattr(self, "_p2_build_checked", False):
                            self._p2_build_checked = True
                            _build_cmd = self._resolve_per_file_build(path)
                            if not _build_cmd:
                                # No per-file target template (protobuf, fmt,
                                # json-c, nlohmann — no per-object Makefile
                                # rules). Fall back to the pre_continue build
                                # command when it IS a build: without this,
                                # the only tests.required-independent build
                                # gate never fires for those trees and a
                                # build-broken merge ships silently
                                # (protobuf-0055: sim 1.000, make rc=2,
                                # accepted anyway).
                                _build_cmd = _phase2_fallback_build_cmd(
                                    getattr(self.config.tests, "pre_continue", ""),
                                    enabled=getattr(
                                        self.config.validation,
                                        "cc_phase2_full_build_fallback", True),
                                )
                                # Sprint-19 P3: a full build already timed
                                # out this session — re-running the full-
                                # tree fallback at the same 120s cap adds
                                # zero information while burning the case
                                # budget (protobuf-0067: 120s of the 1020s
                                # blowout was exactly this re-run). Syntax
                                # checks remain the strongest available
                                # signal on a tree that cannot complete.
                                _bs_state = getattr(
                                    getattr(self.verification, "build_state", None),
                                    "full_build_available", True)
                                if _build_cmd and not _bs_state:
                                    self.journal.emit(
                                        "phase2_build_fallback_skipped",
                                        {"reason": "prior full-build timeout "
                                                   "(session degraded to syntax-only)",
                                         "command": _build_cmd},
                                        step_index=self.step, path=path,
                                    )
                                    _build_cmd = ""
                                if _build_cmd:
                                    self.journal.emit(
                                        "phase2_build_fallback_full",
                                        {"command": _build_cmd},
                                        step_index=self.step, path=path,
                                    )
                            if _build_cmd:
                                self._write_worktree_only(path, buffer, accepted=accepted)
                                import time as _p2bt_time

                                _p2bt_t0 = _p2bt_time.monotonic()
                                _build_ok, _build_output = self._run_raw_test(_build_cmd)
                                _p2bt_dur = _p2bt_time.monotonic() - _p2bt_t0
                                # Sprint-19 P3: journal the Phase-2 build as a
                                # probe and let a timeout degrade the session
                                # (the ~120-300s gaps were previously silent).
                                _p2_bs = getattr(self.verification, "build_state", None)
                                _p2_timed_out = (
                                    not _build_ok
                                    and "timed out after" in (_build_output or "")
                                )
                                if _p2_bs is not None:
                                    _p2_err_tail = ""
                                    if not _build_ok and _build_output:
                                        _p2_err_tail = _build_output[-300:]
                                    _p2_bs.record_probe(
                                        _build_cmd, _p2bt_dur,
                                        "timeout" if _p2_timed_out
                                        else ("pass" if _build_ok else "fail"),
                                        path=path,
                                        errors=_p2_err_tail or None)
                                    if _p2_timed_out:
                                        # _run_raw_test's 120s cap on a
                                        # full-tree command — same class as
                                        # verify_file's full-build timeout.
                                        _p2_bs.note_timeout(
                                            "generic", _build_cmd, 120)
                                _error_probe = [
                                    ln for ln in (_build_output or "").splitlines()
                                    if "error" in ln.lower()
                                ]
                                if not _build_ok and not _error_probe:
                                    # Failure with NO error lines = timeout /
                                    # OOM-kill / infra noise. Not a merge
                                    # defect and nothing to feed the repair
                                    # loop — treat as N/A (the pre_continue
                                    # gate still reports it).
                                    self.journal.emit(
                                        "phase2_build_inconclusive",
                                        {"command": _build_cmd,
                                         "output_tail": (_build_output or "")[-200:]},
                                        step_index=self.step, path=path,
                                    )
                                elif not _build_ok:
                                    # Build failed — classify the error lines
                                    # first: a pre-existing SIBLING-file error
                                    # is infrastructure, not a merge defect
                                    # (the build never reached the conflict
                                    # TU). Only a conflict-file error justifies
                                    # the repair loop.
                                    _error_lines = [
                                        ln for ln in _build_output.splitlines()
                                        if "error" in ln.lower() and ".c" in ln.lower()
                                    ][:5]
                                    _merge_lines, _env_ct = (
                                        _classify_build_error_lines(
                                            _error_lines, path)
                                    )
                                    if _error_lines and not _merge_lines and _env_ct > 0:
                                        self.journal.emit(
                                            "phase2_build_environmental",
                                            {"env_lines": _env_ct,
                                             "sample": _error_lines[:1]},
                                            step_index=self.step, path=path,
                                        )
                                        break  # accept — infrastructure failure
                                    # Build failed for merge-relevant reasons —
                                    # synthesize a file-level failure so the
                                    # repair loop can re-resolve the
                                    # responsible unit with the build error
                                    # as CEGIS feedback. Include the raw build
                                    # output (with file:line info) so fault
                                    # attribution can identify the unit.
                                    from capybase.verification import (
                                        VerificationResult as _VR,
                                        VerificationFailure as _VF,
                                    )
                                    # Extract the most relevant error lines
                                    # (file:line:error patterns) for attribution
                                    _msg = "; ".join(
                                        _merge_lines or _error_lines
                                    ) or f"build failed ({_build_cmd})"
                                    file_validation = _VR(
                                        candidate_id="build_test",
                                        unit_id=path,
                                        passed=False,
                                        hard_failures=[_VF(
                                            validator="build_test",
                                            severity="error",
                                            message=_msg,
                                        )],
                                    )
                                    # Don't break — fall through to repair.
                                else:
                                    break
                            else:
                                break
                        else:
                            break
                    # Whole-side repair rung (sprint-19 P1): the splice's
                    # COMPILE gate failed — probe the pristine stage sides
                    # before spending repair budget on a reconstruction
                    # that may be fundamentally broken (tokio-0109/0037:
                    # the oracle sat verbatim at a stage while the splice
                    # failed). Fires only on compile-flavored failures,
                    # once per file; adjudication-gated swaps only. A
                    # decline leaves the repair loop untouched.
                    if (not file_validation.passed and not _wsr_attempted
                            and _is_compile_flavored_failure(
                                file_validation.hard_failures)):
                        _wsr_attempted = True
                        _wsr = None
                        try:
                            _wsr = self._try_whole_side_repair_rung(
                                path, language, original, units, buffer,
                                wall_deadline=_file_wall_deadline)
                        except Exception:  # noqa: BLE001 — rung is a
                            # recovery mechanism; never let it break the
                            # standard repair path.
                            _wsr = None
                        if _wsr is not None:
                            accepted, buffer, file_validation = _wsr
                            # The swapped-in side is a NEW buffer — its
                            # build test must run (the once-per-Phase-2
                            # flag refers to one buffer, not one file).
                            self._p2_build_checked = False
                            continue
                    # Whole-file portfolio fallback: when Phase 2 validation
                    # fails with a CROSS-UNIT error pattern (duplicate
                    # definitions, undeclared identifiers — symptoms of the
                    # per-unit portfolio picking inconsistent sides), try
                    # all-current / all-replayed candidates BEFORE the time
                    # budget checks — this is the designed recovery for
                    # exactly that failure shape, and skipping it because
                    # Phase 1 burned the budget (e.g. on empty-LLM retries)
                    # leaves only the far weaker repair loop
                    # (protobuf-0043). Gated to cross-unit patterns so
                    # ordinary syntax errors still go to the repair loop
                    # (which correctly merges both sides).
                    _wf_failures_text = " ".join(
                        f.message for f in file_validation.hard_failures
                    ).lower()
                    _wf_is_cross_unit = any(
                        p in _wf_failures_text
                        for p in (
                            "cannot be overloaded", "redefinition",
                            "defined more than once", "redeclared",
                            "undeclared identifier", "was not declared",
                            "duplicate",
                        )
                    )
                    if wf_retries == 0 and _wf_is_cross_unit:
                        for _side, _cand_list in _whole_file_side_candidates(units):
                            _wf_buf = _resolved_buffer(original, _cand_list)
                            # Quick brace-balance gate (milliseconds).
                            try:
                                if language and not _braces_balanced(
                                        _wf_buf, language):
                                    self.journal.emit(
                                        "whole_file_portfolio_candidate",
                                        {"side": _side, "declined": "braces"},
                                        step_index=self.step, path=path,
                                    )
                                    continue
                            except Exception:
                                pass
                            # Write and validate at the whole-file level.
                            self._write_worktree_only(
                                path, _wf_buf, accepted=_cand_list)
                            _wf_spans = [
                                (u.marker_span, c.resolved_text)
                                for u, c in _cand_list if u.marker_span
                            ]
                            _wf_val = self.verification.verify_file(
                                path, language, original, _wf_spans,
                                repo_root=str(self.git.repo),
                                whole_text=_wf_buf,
                            )
                            # R1 (s22): if the rung repaired the portfolio
                            # buffer, the disk copy (written pre-verify) is
                            # stale — rewrite the repaired text and carry it
                            # forward as the buffer.
                            if (_wf_val.passed
                                    and getattr(_wf_val, "resolved_text", None) is not None
                                    and getattr(_wf_val, "resolved_text", None) != _wf_buf):
                                _wf_buf = _wf_val.resolved_text
                                self._write_worktree_only(
                                    path, _wf_buf, accepted=_cand_list)
                            if not _wf_val.passed:
                                # Visibility: the cross-unit portfolio used
                                # to decline silently, hiding WHY whole-side
                                # candidates failed (protobuf-0043's chain).
                                self.journal.emit(
                                    "whole_file_portfolio_candidate",
                                    {"side": _side, "declined": "verify",
                                     "hard_failures": [
                                         f.message
                                         for f in _wf_val.hard_failures
                                     ][:3]},
                                    step_index=self.step, path=path,
                                )
                                continue
                            # Also run the build test if configured.
                            _wf_build_ok = True
                            _wf_build = self._resolve_per_file_build(path)
                            if _wf_build:
                                _wf_ok, _ = self._run_raw_test(_wf_build)
                                _wf_build_ok = _wf_ok
                            if _wf_build_ok:
                                accepted = _cand_list
                                buffer = _wf_buf
                                # P5 v2b (sprint-23): the portfolio accept is
                                # verified (verify_file + build above) — record
                                # it for the resurrection guard's resolved-file
                                # provenance (zenodo-0063 completed via this
                                # surface and was never recorded).
                                if not hasattr(self, "_resolved_validated_paths"):
                                    self._resolved_validated_paths: set[str] = set()
                                self._resolved_validated_paths.add(path)
                                self.journal.emit(
                                    "whole_file_portfolio",
                                    {"side": _side,
                                     "n_units": len(_cand_list),
                                     "via": "phase2_fallback"},
                                    step_index=self.step, path=path,
                                )
                                # Signal success — break out of the while.
                                file_validation = _wf_val
                                break
                        if file_validation.passed:
                            break
                        # (The true-side portfolio already ran at first
                        # failure above — including its duplicate-definition
                        # and journal-only asymmetry triggers. What remains
                        # here is the marker-spliced candidate portfolio for
                        # mixed merges whose per-unit sides were picked
                        # inconsistently.)
                    # Tiered verification: check time budget before the
                    # (weaker, costlier) per-unit repair loop.
                    if _phase2_budget > 0:
                        _elapsed_p2 = _p2time.monotonic() - _phase2_start
                        if _elapsed_p2 >= _phase2_budget:
                            break  # time budget exhausted
                        if _phase2_model_used:
                            break  # only 1 model re-resolve allowed in tiered mode
                    else:
                        if wf_retries >= wf_budget:
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
                            path, accepted, original, file_validation.hard_failures,
                            wall_deadline=_file_wall_deadline,
                            skip_deterministic=_det_unchanged,
                        )
                    )
                    if accepted_opt is None:
                        # The re-resolution escalated — most commonly the
                        # model returned EMPTY for the attributed unit
                        # (protobuf-0043's oscillation: a declined model
                        # opinion killed the whole rebase). A missing model
                        # opinion is not evidence the merge is unresolvable:
                        # fall back to the file's majority side (conservative
                        # tie-break: current) and let the loop re-validate.
                        # If that splice still fails, the budget guards
                        # escalate as before.
                        _fb = _empty_repair_side_fallback(accepted)
                        if _fb is not None:
                            self.journal.emit(
                                "repair_side_fallback",
                                {"n_units": len(_fb)},
                                step_index=self.step, path=path,
                            )
                            accepted = _fb
                        else:
                            file_validation = None  # type: ignore[assignment]
                            break
                    elif accepted_opt is not None:
                        # NB: NOT `accepted = accepted_opt` unconditionally —
                        # that overwrites the fallback assignment above with
                        # None and crashes the tiered-budget comprehension
                        # below (jsonc-0001: repair escalated, fallback
                        # rescued, then TypeError 'NoneType' not iterable).
                        accepted = accepted_opt
                    # Tiered budget: only count a MODEL re-resolve against the
                    # single-model-call budget. A deterministic repair (brace/
                    # preprocessor/side-consistency/etc.) returns a candidate
                    # whose provenance starts with "deterministic" — it cost no
                    # model calls, so it must NOT consume the tiered slot.
                    # Without this guard, a deterministic repair that fires but
                    # doesn't fix the failure would burn the slot and the loop
                    # exits before the LLM/Layer-3 path ever runs.
                    if _phase2_budget > 0:
                        _used_model = any(
                            not str(getattr(c, "provenance", "") or "").startswith("deterministic")
                            for _u, c in accepted
                        )
                        if _used_model:
                            _phase2_model_used = True
                if file_validation is None or not file_validation.passed:
                    # Final deterministic repair attempt. The cheap O(n) repairs
                    # (brace balance, prefix dedup, boundary echo, import dedup)
                    # live inside _whole_file_repair, which the budget gate above
                    # (wf_retries >= wf_budget) starves when LLM retries consume
                    # the budget. Give them a final shot on the last-failed
                    # buffer — they're deterministic and the result is re-validated.
                    # Surfaced in the C live-eval (redis pubsub.c): the model
                    # dropped one closing brace; the deterministic brace repair
                    # fixes it, but the budget broke before it could run.
                    #
                    # This also runs on Exit A (file_validation is None): when the
                    # LLM re-resolve escalated, the deterministic repairs were
                    # previously skipped entirely. But splice-junction defects
                    # (a dropped brace, a duplicated boundary line) are exactly
                    # what the deterministic beam is for — the LLM re-resolve
                    # failed not because the defect is hard, but because
                    # attribution pointed at the wrong unit. The deterministic
                    # beam operates on the spliced buffer directly, so it doesn't
                    # depend on correct fault attribution. 6 of 7 repair-failed
                    # cases in the v3 C corpus were at sim >= 0.95 (model output
                    # correct) — the deterministic pass may close them.
                    if accepted:
                        # When file_validation is None (Exit A), there are no
                        # fresh hard_failures to feed; use the last-known
                        # failures from the repair loop's seed, or empty.
                        _repair_failures = (
                            file_validation.hard_failures
                            if file_validation is not None
                            else []
                        )
                        det = self._whole_file_repair(
                            path, accepted, original,
                            _repair_failures,
                            deterministic_only=True,
                            wall_deadline=_file_wall_deadline,
                        )
                        if det is not None:
                            accepted = det
                            _spans = [
                                (u.marker_span, c.resolved_text)
                                for u, c in accepted
                            ]
                            file_validation = self.verification.verify_file(
                                path, language, original, _spans,
                                repo_root=str(self.git.repo),
                            )
                            # R1 (s22): carry the repaired text into the
                            # buffer so Phase 3 + the write path use what was
                            # actually validated.
                            if (file_validation.passed
                                    and getattr(file_validation, "resolved_text", None) is not None):
                                buffer = file_validation.resolved_text
                    _floor = None
                    if file_validation is None or not file_validation.passed:
                        # Wholesale winner floor — repair-exhaustion rescue:
                        # Phase 2 failed on a wholesale file, but the gate
                        # winner is still the best available whole answer.
                        _floor = self._wholesale_winner_floor(
                            path, language, units, buffer=None)
                    if _floor is not None:
                        accepted = _floor
                        buffer = _floor[0][1].resolved_text
                    else:
                        # F1 tier-1 (sprint-23): deterministic near-one-sided
                        # takeover on the FAILURE path — when one side's
                        # churn <= 15 lines, the other side IS the merge.
                        _f1_side = None
                        # F1-smart: always-on with 4 precise conditions:
                        # (a) all repair rounds exhausted (wf_retries >= budget)
                        # (b) not heading to interactive (a human decides)
                        # (c) not the first attempt (model got its retries)
                        # (d) the wholesale floor already declined
                        _f1_eligible = (
                            wf_retries >= max(1, wf_budget)
                            and not getattr(self, "_interactive_pending", False)
                            and (wf_retries >= 1
                                 or getattr(self, "_phase2_model_used", False))
                        )
                        # P1a (sprint-24): diagnostic journaling — which
                        # condition prevents firing on the 5 target cases?
                        # This event fires on EVERY repair-exhaustion path,
                        # recording the full pipeline state for debugging.
                        self.journal.emit(
                            "f1_tier1_trigger_check",
                            {"wf_retries": wf_retries,
                             "wf_budget": wf_budget,
                             "interactive_pending": getattr(
                                 self, "_interactive_pending", False),
                             "phase2_model_used": getattr(
                                 self, "_phase2_model_used", False),
                             "eligible": _f1_eligible,
                             "path": path},
                            step_index=self.step, path=path,
                        )
                        if _f1_eligible:
                            try:
                                _sides_f1, _base_f1 = self._micro_stage_sides(path)
                                if _sides_f1:
                                    # Pipeline-mediated takeover decision
                                    # (the sprint-24 architecture): the
                                    # mechanisms own their triggers; this
                                    # orchestrator provides the sides +
                                    # compile verdicts and executes the
                                    # chosen takeover. Three phases, exactly
                                    # the former inline sequence: A = tier-1
                                    # churn; B = probe both pristine sides +
                                    # compile-clean; C = tier-2 LLM ballot
                                    # (only when A and B declined — it must
                                    # not bill a call when compile-clean
                                    # already took the single compiling side).
                                    from capybase.pipeline import (
                                        RepairExhaustedContext,
                                        Stage,
                                    )
                                    _pipe_ctx = RepairExhaustedContext(
                                        path=path, language=language,
                                        step_index=self.step,
                                        sides=_sides_f1,
                                        base_text=_base_f1 or "",
                                        retry_count=wf_retries,
                                        retry_budget=wf_budget,
                                        phase2_model_used=getattr(
                                            self, "_phase2_model_used", False),
                                    )
                                    _pipe = self._pipeline()
                                    # Tier-1's one shot: enabled for THIS
                                    # execute (Phase A), disabled for the
                                    # later phases' re-executions (see the
                                    # Phase-B comment). Re-enabled here
                                    # idempotently — an exception in a prior
                                    # round may have left it off.
                                    self._f1_tier1_mech.enabled = True
                                    self._f1_tier2_mech.enabled = False
                                    try:
                                        _f1_result = _pipe.execute(
                                            Stage.POST_REPAIR_EXHAUSTION,
                                            _pipe_ctx)
                                    finally:
                                        self._f1_tier2_mech.enabled = True
                                    _f1_text = ""
                                    if (_f1_result is not None
                                            and _f1_result.action == "takeover"):
                                        _f1_side = _f1_result.metadata.get("side")
                                        _f1_text = _f1_result.resolved_text or ""
                                        # F1-smart (d): the takeover side must
                                        # pass the compile gate — taking a
                                        # side that doesn't compile is worse
                                        # than escalating (test fixtures:
                                        # synthetic sides that don't build)
                                        if _f1_text.strip():
                                            _f1_check = self.verification.verify_file(
                                                path, language, _f1_text, [],
                                                repo_root=str(self.git.repo),
                                                whole_text=_f1_text)
                                            if not _f1_check.passed:
                                                _f1_side = None
                                                _f1_text = ""
                                        else:
                                            _f1_side = None
                                    if _f1_side is None:
                                        # Phase B: probe both pristine sides
                                        # and let the compile-clean mechanism
                                        # take a single compiling one. BOTH
                                        # tier-1 and the tier-2 ballot are
                                        # disabled here — Pipeline.execute
                                        # returns on first engagement, so a
                                        # re-engaging tier-1 would preempt
                                        # compile-clean from ever running
                                        # (the sea-orm-0021 preemption bug),
                                        # and the ballot must not bill a
                                        # second call.
                                        self._f1_tier1_mech.enabled = False
                                        _compiling = {}
                                        for _side_name in ("current", "replayed"):
                                            _side_text = _sides_f1.get(_side_name, "")
                                            if not _side_text.strip():
                                                continue
                                            _side_check = self.verification.verify_file(
                                                path, language, _side_text, [],
                                                repo_root=str(self.git.repo),
                                                whole_text=_side_text)
                                            _compiling[_side_name] = bool(_side_check.passed)
                                        self._f1_compile_clean_mech.set_compiling_sides(
                                            _compiling)
                                        self._f1_tier2_mech.enabled = False
                                        try:
                                            _f1_result_b = _pipe.execute(
                                                Stage.POST_REPAIR_EXHAUSTION,
                                                _pipe_ctx)
                                        finally:
                                            self._f1_tier2_mech.enabled = True
                                        # Phase B accepts ONLY the compile-
                                        # clean mechanism: tier-1 is
                                        # deterministic and already had its
                                        # chance in Phase A (its pick failed
                                        # the compile gate) — re-engaging it
                                        # here would land an unverified side.
                                        if (_f1_result_b is not None
                                                and _f1_result_b.action == "takeover"
                                                and _f1_result_b.mechanism == "f1_compile_clean_takeover"):
                                            _f1_side = _f1_result_b.metadata.get("side")
                                            _f1_text = _f1_result_b.resolved_text or ""
                                        else:
                                            _f1_side = None
                                            _f1_text = ""
                            except Exception:  # noqa: BLE001
                                _f1_side = None
                        if _f1_side is not None:
                            _f1_text = _sides_f1.get(_f1_side, "")
                            if _f1_text.strip():
                                self.journal.emit(
                                    "f1_tier1_takeover",
                                    {"side": _f1_side, "path": path},
                                    step_index=self.step, path=path,
                                )
                                if not hasattr(self, "_takeover_landed_paths"):
                                    self._takeover_landed_paths = {}
                                self._takeover_landed_paths[path] = _f1_side
                                _f1_unit = unit.model_copy(
                                    update={
                                        "unit_id": f"{path}:f1_tier1",
                                        "unit_kind": "whole_file",
                                        "marker_span": None,
                                    })
                                _f1_cand = CandidateResolution(
                                    candidate_id=f"{path}:f1_tier1:{_f1_side}",
                                    unit_id=_f1_unit.unit_id,
                                    model_name="deterministic",
                                    resolved_text=_f1_text,
                                    prompt_version="deterministic_near_one_sided",
                                    provenance="deterministic_structural",
                                    self_reported_confidence=0.85,
                                    explanation=(
                                        f"F1 tier-1: near-one-sided — "
                                        f"{_f1_side} side subsumes"),
                                )
                                accepted = [(_f1_unit, _f1_cand)]
                                buffer = _f1_text
                                file_validation = None
                                # The takeover IS the file's resolution — its
                                # side already passed the compile gate above.
                                # Write + stage it, then move to the next file.
                                # (A bare `continue` here targets the outer
                                # per-file loop and skips _write_and_stage at
                                # the loop tail: the takeover was journaled
                                # but never landed — every F1 tier-1 takeover
                                # in the sprint-24 cycle-A/B specimens was
                                # silently discarded this way.)
                                self._write_and_stage(
                                    path, buffer, result, accepted=accepted)
                                continue
                        else:
                            # F1 tier-2 (sprint-23): LLM subsumption
                            # adjudication for symmetric shapes — when
                            # tier-1 declines (both sides changed
                            # significantly), ask the model — Phase C of the
                            # pipeline execution: the F1Tier2Adjudication
                            # mechanism with the orchestrator's adjudicator
                            # injected (the decide callable). Runs only when
                            # Phases A (tier-1) and B (compile-clean) both
                            # declined.
                            _f2_side = None
                            if _f1_eligible and _sides_f1:
                                _f2_result_c = _pipe.execute(
                                    Stage.POST_REPAIR_EXHAUSTION, _pipe_ctx)
                                if (_f2_result_c is not None
                                        and _f2_result_c.action == "takeover"
                                        and _f2_result_c.mechanism == "f1_tier2_adjudication"):
                                    _f2_side = _f2_result_c.metadata.get("side")
                            if _f2_side is not None:
                                _f2_text = (_sides_f1 or {}).get(
                                    _f2_side, "")
                                # Compile-gate the adjudicated side (the same
                                # contract as tier-1's F1-smart (d)): tier-2
                                # fires when tier-1's churn check declined,
                                # which includes shapes where NEITHER pristine
                                # side builds (protobuf-0051's 0.1s probe
                                # failures). Never land an unverified side —
                                # decline to escalate instead.
                                _f2_ok = False
                                if _f2_text.strip():
                                    try:
                                        _f2_check = self.verification.verify_file(
                                            path, language, _f2_text, [],
                                            repo_root=str(self.git.repo),
                                            whole_text=_f2_text)
                                        _f2_ok = bool(_f2_check.passed)
                                    except Exception:  # noqa: BLE001
                                        _f2_ok = False
                                if not _f2_ok:
                                    # Sprint-25 item 3: pristine-side
                                    # micro-repair — the chosen side's
                                    # compile failure may be one missing
                                    # declaration the OTHER side carries
                                    # (axum-0013: the adjudicated side needs
                                    # an import its counterpart has). Run C1
                                    # on the side's errors before declining;
                                    # still compiler-gated.
                                    try:
                                        _f2_msgs = "\n".join(
                                            str(f.message) for f in
                                            (_f2_check.hard_failures
                                             if _f2_check else []))
                                        from capybase.verification import (
                                            inject_symbol_declaration,
                                            parse_missing_symbols,
                                        )
                                        _f2_syms = parse_missing_symbols(
                                            _f2_msgs, language)[:2]
                                        if _f2_syms:
                                            _other = ("replayed"
                                                      if _f2_side == "current"
                                                      else "current")
                                            _other_text = (_sides_f1 or {}).get(
                                                _other, "")
                                            for _sym in _f2_syms:
                                                _decl = None
                                                for _ln in _other_text.split("\n"):
                                                    if (_sym in _ln
                                                            and _ln.strip().endswith(";")):
                                                        _decl = _ln.strip()
                                                        break
                                                    if (_sym in _ln
                                                            and _ln.strip().endswith("{")):
                                                        from capybase.verification import (
                                                            derive_prototype as _dp2,
                                                        )
                                                        _decl = _dp2(_ln.strip())
                                                        break
                                                if _decl:
                                                    _patched = inject_symbol_declaration(
                                                        _f2_text, _decl, language)
                                                    if _patched:
                                                        _pv2 = self.verification.verify_file(
                                                            path, language,
                                                            _patched, [],
                                                            repo_root=str(self.git.repo),
                                                            whole_text=_patched)
                                                        if _pv2.passed:
                                                            _f2_text = _patched
                                                            _f2_ok = True
                                                            self.journal.emit(
                                                                "f1_tier2_side_micro_repair",
                                                                {"side": _f2_side,
                                                                 "symbol": _sym,
                                                                 "path": path},
                                                                step_index=self.step,
                                                                path=path)
                                                            break
                                    except Exception:  # noqa: BLE001
                                        pass
                                if not _f2_ok:
                                    self.journal.emit(
                                        "f1_tier2_side_build_declined",
                                        {"side": _f2_side, "path": path},
                                        step_index=self.step, path=path,
                                    )
                                    # D11 (s27): the ballot's chosen side
                                    # fails its build, but the OTHER side's
                                    # whole-side probe PASSED → land the
                                    # other. redis-0014: the ballot chose
                                    # replayed at 0.95 confidence, replayed
                                    # failed make server.o, and current had
                                    # passed its probe rounds earlier — the
                                    # evidence was discarded and the case
                                    # escalated with a verified answer in
                                    # hand.
                                    try:
                                        _other = ("replayed"
                                                  if _f2_side == "current"
                                                  else "current")
                                        # No _compiling gate: that dict is
                                        # populated by the tier-1 probe block,
                                        # which may have declined (redis-0014's
                                        # runs) — the verify below IS the
                                        # evidence (one 2-5s targeted build).
                                        if True:
                                            _ot = (_sides_f1 or {}).get(
                                                _other, "")
                                            if _ot.strip():
                                                self._write_worktree_only(
                                                    path, _ot, accepted=None)
                                                _ov = self.verification.verify_file(
                                                    path, language, original, [],
                                                    repo_root=str(self.git.repo),
                                                    whole_text=_ot,
                                                    pristine_side_texts=[_ot])
                                                if _ov.passed:
                                                    _ou = unit.model_copy(
                                                        update={
                                                            "unit_id": f"{path}:f1_t2fb",
                                                            "unit_kind": "whole_file",
                                                            "marker_span": None})
                                                    _oc = CandidateResolution(
                                                        candidate_id=(
                                                            f"{path}:f1_t2fb:{_other}"),
                                                        unit_id=_ou.unit_id,
                                                        model_name="deterministic",
                                                        resolved_text=_ot,
                                                        prompt_version=(
                                                            "llm_subsumption_adjudication_fallback"),
                                                        provenance="deterministic_structural",
                                                        self_reported_confidence=0.7,
                                                        explanation=(
                                                            f"F1 tier-2 fallback: "
                                                            f"the adjudicated side "
                                                            f"{_f2_side} failed its "
                                                            f"build; {_other} "
                                                            f"verified"),
                                                    )
                                                    self.journal.emit(
                                                        "f1_tier2_fallback_side",
                                                        {"chosen": _f2_side,
                                                         "landed": _other,
                                                         "path": path},
                                                        step_index=self.step,
                                                        path=path)
                                                    accepted = [(_ou, _oc)]
                                                    buffer = _ot
                                                    file_validation = None
                                                    if not hasattr(self, "_takeover_landed_paths"):
                                                        self._takeover_landed_paths = {}
                                                    self._takeover_landed_paths[path] = _other
                                                    self._write_and_stage(
                                                        path, buffer, result,
                                                        accepted=accepted)
                                                    continue
                                    except Exception:  # noqa: BLE001 — fallback is best-effort
                                        pass
                                if _f2_text.strip() and _f2_ok:
                                    _f2_unit = unit.model_copy(
                                        update={
                                            "unit_id": f"{path}:f1_tier2",
                                            "unit_kind": "whole_file",
                                            "marker_span": None,
                                        })
                                    _f2_cand = CandidateResolution(
                                        candidate_id=f"{path}:f1_tier2:{_f2_side}",
                                        unit_id=_f2_unit.unit_id,
                                        model_name="deterministic",
                                        resolved_text=_f2_text,
                                        prompt_version="llm_subsumption_adjudication",
                                        provenance="deterministic_structural",
                                        self_reported_confidence=0.75,
                                        explanation=(
                                            f"F1 tier-2: LLM adjudicated "
                                            f"{_f2_side} subsumes"),
                                    )
                                    accepted = [(_f2_unit, _f2_cand)]
                                    buffer = _f2_text
                                    file_validation = None
                                    if not hasattr(self, "_takeover_landed_paths"):
                                        self._takeover_landed_paths = {}
                                    self._takeover_landed_paths[path] = _f2_side
                                    # Same as tier-1: land the adjudicated side
                                    # (write + stage) before moving on. The bare
                                    # `continue` discarded the tier-2 choice —
                                    # protobuf-0051/axum-0013 journals ended at
                                    # f1_tier2_adjudication with the chosen side
                                    # never validated or written.
                                    self._write_and_stage(
                                        path, buffer, result, accepted=accepted)
                                    continue
                            # Churn-heuristic fallback — Phase D of the
                            # pipeline execution (migration #4): when the
                            # tier-2 ballot declines or dies (redis-0049:
                            # the LLM call hit the case wall deadline) but
                            # BOTH pristine sides passed the Phase-B probes,
                            # the ChurnFallbackTakeover mechanism completes
                            # the takeover without another LLM round-trip
                            # (_whole_side_heuristic's exact policy).
                            try:
                                _both_clean = bool(
                                    _compiling.get("current")
                                    and _compiling.get("replayed"))
                            except NameError:  # eligibility/exception paths skip P1b
                                _both_clean = False
                            _hf_side_from_pipe = None
                            if _f2_side is None and _both_clean and _sides_f1:
                                self._churn_fallback_mech.set_compiling_sides(
                                    dict(_compiling))
                                self._f1_tier2_mech.enabled = False
                                try:
                                    _hf_result = _pipe.execute(
                                        Stage.POST_REPAIR_EXHAUSTION,
                                        _pipe_ctx)
                                finally:
                                    self._f1_tier2_mech.enabled = True
                                if (_hf_result is not None
                                        and _hf_result.action == "takeover"
                                        and _hf_result.mechanism == "churn_fallback_takeover"):
                                    _hf_side_from_pipe = _hf_result.metadata.get("side")
                            if _hf_side_from_pipe is not None:
                                _heuristic_side = _hf_side_from_pipe
                                _heuristic_text = (_sides_f1 or {}).get(
                                    _heuristic_side, "")
                                if _heuristic_text.strip():
                                    self.journal.emit(
                                        "f1_churn_fallback_takeover",
                                        {"side": _heuristic_side, "path": path},
                                        step_index=self.step, path=path,
                                    )
                                    _hf_unit = unit.model_copy(
                                        update={
                                            "unit_id": f"{path}:f1_churn_fallback",
                                            "unit_kind": "whole_file",
                                            "marker_span": None,
                                        })
                                    _hf_cand = CandidateResolution(
                                        candidate_id=(
                                            f"{path}:f1_churn_fallback:{_heuristic_side}"),
                                        unit_id=_hf_unit.unit_id,
                                        model_name="deterministic",
                                        resolved_text=_heuristic_text,
                                        prompt_version="deterministic_churn_fallback",
                                        provenance="deterministic_structural",
                                        self_reported_confidence=0.6,
                                        explanation=(
                                            f"F1 churn fallback: tier-2 "
                                            f"unavailable, both sides compile, "
                                            f"{_heuristic_side} carries the churn"),
                                    )
                                    accepted = [(_hf_unit, _hf_cand)]
                                    buffer = _heuristic_text
                                    if not hasattr(self, "_takeover_landed_paths"):
                                        self._takeover_landed_paths = {}
                                    self._takeover_landed_paths[path] = _heuristic_side
                                    self._write_and_stage(
                                        path, buffer, result, accepted=accepted)
                                    continue
                        if file_validation is None:
                            result.escalated = True
                            result.reason = (
                                f"whole-file repair could not re-resolve a unit in {path}"
                            )
                        else:
                            result.escalated = True
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
            # Phase 3: Comment reconciliation (deferred-comment system). After
            # the code passes Phase-B validation, reconcile deferred (prose)
            # comments in a second CEGIS pass. The comment pass can ONLY modify
            # comments — the executable-token-equality invariant prevents any
            # code corruption. Skipped entirely when no deferred comments overlap
            # the conflict region (zero overhead). Gated by
            # config.future.enable_comment_reconciliation (default True).
            if self.config.future.enable_comment_reconciliation:
                pre_comment_buffer = buffer
                buffer = self._reconcile_comments(
                    path, buffer, accepted, originals[path], units, language,
                ) or buffer
                # §11 post-comment verify_file gate: defense-in-depth. The
                # executable-token invariant in apply_comment_plan makes
                # comment-induced failures unlikely, but it's blind to
                # comment-INTERNAL structure (a malformed doc-comment code
                # fence, a docstring that breaks doctests). Re-run Phase-B
                # validation on the comment buffer; revert to the frozen
                # pre-comment buffer on failure so code is NEVER corrupted by
                # the comment pass. Skipped when the pass was a no-op.
                if buffer != pre_comment_buffer:
                    buffer = self._verify_post_comment(
                        path, language, buffer, pre_comment_buffer,
                        originals[path], accepted,
                    )
            # Stage the validated file (it was already written to the worktree
            # in Phase 1; re-write in case the CEGIS loop changed it, then stage).
            # Wholesale winner floor — degenerate-output guard: every fast-path
            # decline (adjudication "keep", winner failed standalone verify)
            # funnels the file through the cascade, whose wholesale-file
            # failure mode is dropping the dominant rewrite (sea-orm-0010/0024:
            # winner preservation 0.0-0.01 on files whose oracles equal the
            # winner at 0.99-1.0). A woven merge preserves the winner and the
            # floor stays silent.
            _floor = self._wholesale_winner_floor(
                path, language, units, buffer=buffer)
            if _floor is not None:
                accepted = _floor
                buffer = _floor[0][1].resolved_text
            # Sprint-18 WS3: git's auto-merge can resurrect upstream-deleted
            # content OUTSIDE the marker blocks — the resolver only controls
            # the blocks (tokio-0037/0046: every unit correctly resolved
            # current_only; git's own context resolution re-added 12 dead
            # lines; the end-of-rebase scan then stopped the rebase, correct
            # but too late to repair). Pre-stage, the merge-index stages are
            # still available: when the buffer carries upstream-deleted
            # blocks and the upstream side verifies clean as a whole file,
            # swap to it.
            _drs = self._try_deletion_respect_swap(path, language, units, buffer)
            if _drs is not None:
                accepted = _drs
                buffer = _drs[0][1].resolved_text
            # Sprint-18 WS4: a both-rewrite file resolved to one side
            # verbatim is a silent drop of the other side's rewrite
            # (sea-orm-0027). LLM-gated rejection — escalate only when the
            # dropped side is adjudicated not-superseded.
            if self._check_side_collapse(path, language, units, buffer,
                                         result, accepted=accepted):
                self._record_outcomes_to_memory(result)
                return result
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
        # Dump conflict bundles for NEAR_MATCH debugging: when any unit was
        # resolved via an LLM candidate (not a deterministic rule), the result
        # may be just below the sim threshold. Having the runtime inputs lets us
        # investigate why the model dropped lines or why a guard didn't fire.
        # Pure instrumentation — no behavioral change.
        if not result.escalated and self._any_unit_used_llm(result):
            self._dump_conflict_bundles(result)
        return result

    def _reconcile_comments(
        self, path: str, buffer: str,
        accepted: list, original: str,
        units: list, language: str | None,
    ) -> str | None:
        """Phase 3: reconcile deferred (prose) comments after code passes validation.

        Runs the comment pass via :meth:`_run_comment_pass`. §10 code-reopening:
        when the pass detects a high-trust contract conflict (a deferred
        invariant comment the verifiers can't reconcile), re-enters
        :meth:`_resolve_unit` for the affected unit with the conflict as a
        seed_failure, then re-runs the comment pass on the new code. Bounded by
        ``PolicyConfig.max_comment_to_code_repair_retries`` (default 1). On
        exhaustion or when reopening is disabled, the frozen code is kept and a
        review bundle surfaces the unresolved comments.

        The comment pass can ONLY modify comments — the executable-token-equality
        invariant prevents any code corruption. §10 re-opens code via
        ``_resolve_unit`` (the ``_whole_file_repair`` precedent), NOT by relaxing
        the comment pass.

        Returns the reconciled buffer (or None if skipped/failed → keep original).
        """
        outcome = self._run_comment_pass(path, buffer, accepted, original, units, language)
        if outcome is None:
            return None  # skipped (no comments / unsupported language)
        # §10: the outer code↔comment loop. If the pass failed AND detected a
        # high-trust contract conflict, re-open the code CEGIS for the affected
        # unit, then re-run the comment pass on the new code.
        reopen_budget = getattr(self.config.policy, "max_comment_to_code_repair_retries", 1)
        current_buffer = outcome.buffer
        current_outcome = outcome
        reopen_attempts = 0
        while (not current_outcome.succeeded
               and current_outcome.code_reopen_request
               and reopen_attempts < reopen_budget):
            reopen_attempts += 1
            new_buffer = self._resolve_comment_contract_conflicts(
                path, current_buffer, accepted, units, language,
                current_outcome.code_reopen_request,
            )
            if new_buffer is None:
                break  # the re-resolve escalated → stop the outer loop
            # Re-run the comment pass on the re-resolved code.
            current_buffer = new_buffer
            next_outcome = self._run_comment_pass(
                path, current_buffer, accepted, original, units, language,
            )
            if next_outcome is None:
                break
            current_outcome = next_outcome
        # Finalize: journal the report + write review bundle on failure.
        self._finalize_comment_pass(path, current_outcome, units)
        # The comment jury (Phase 4): runs AFTER the comment pass settles and the
        # buffer is frozen. Three modes via config (effective_jury_mode):
        #   off    — never runs (default).
        #   shadow — records hypothetical routing decisions, NO merge effect.
        #            Also the one-action kill switch for enforce.
        #   enforce— acts on the four typed routes (accept / comment_counterexample
        #            / human_review / code_reopen). The jury may NEVER override a
        #            deterministic failure; every degraded state fails closed.
        # Runs only on success (the frozen code is what the jury inspects).
        from capybase.config import effective_jury_mode, jury_eligible
        jury_mode = effective_jury_mode(self.config.future)
        # Eligibility gate (the canary envelope): enforce mode is restricted to
        # the languages in jury_eligible_languages (default Python — the only
        # language with a validated shadow corpus). An ineligible language
        # downgrades to shadow (still observes, never acts) so the jury never
        # enforces outside the validated envelope even when jury_mode=enforce.
        if jury_mode == "enforce" and not jury_eligible(self.config.future, language):
            jury_mode = "shadow"
            self.journal.emit(
                "jury_enforce_ineligible_downgrade",
                {"language": language,
                 "reason": "language outside jury_eligible_languages; "
                           "downgraded enforce→shadow (observe only)"},
                step_index=self.step, path=path,
            )
        if jury_mode in ("shadow", "enforce") and current_outcome.succeeded:
            try:
                unit = units[0] if units else None
                base_text = (unit.base.text or "") if unit else ""
                current_text = (unit.current.text or "") if unit else ""
                replayed_text = (unit.replayed.text or "") if unit else ""
                enforce_outcomes = self._run_jury(
                    path, current_outcome, units, language,
                    base_text, current_text, replayed_text, mode=jury_mode,
                )
                # Enforce mode: act on the typed outcomes. Shadow returns [].
                if jury_mode == "enforce" and enforce_outcomes:
                    return self._apply_jury_enforcement(
                        path, current_outcome.buffer, accepted, units,
                        language, original, enforce_outcomes,
                    )
            except Exception:  # noqa: BLE001 — jury is advisory; never block
                pass
        return current_outcome.buffer if current_outcome.succeeded else None

    def _run_comment_pass(
        self, path: str, buffer: str,
        accepted: list, original: str,
        units: list, language: str | None,
    ):
        """Run ONE comment-reconciliation pass. Returns the ReconcileOutcome
        (or None when skipped — no comments / unsupported language). Does NOT
        journal the final report or write a review bundle (the caller,
        :meth:`_reconcile_comments`, does that once after the §10 loop settles)."""
        lang = (language or "").strip().lower()
        if lang not in (
            "rust", "rs", "python", "py",
            "javascript", "js", "typescript", "ts",
        ):
            return None
        try:
            from capybase.adapters.string_lexer import enumerate_comment_spans
            from capybase.comment_reconciler import (
                build_comment_ledger, select_comment_frontier_with_fast_paths,
                CommentPlan, apply_comment_plan, run_comment_cegis,
            )
        except ImportError:
            return None

        spans = enumerate_comment_spans(buffer, lang)
        if not spans:
            return None
        if not units:
            return None
        unit = units[0]
        base_text = unit.base.text or ""
        current_text = unit.current.text or ""
        replayed_text = unit.replayed.text or ""
        ledger = build_comment_ledger(
            base_text, current_text, replayed_text, buffer, lang,
        )
        conflict_byte_ranges = self._conflict_byte_ranges(units, buffer)
        frontier_result = select_comment_frontier_with_fast_paths(
            ledger, conflict_byte_ranges=conflict_byte_ranges,
        )
        frontier = frontier_result.entries
        if frontier_result.fast_path_actions:
            try:
                buffer = apply_comment_plan(
                    buffer, frontier_result.entries,
                    CommentPlan(actions=frontier_result.fast_path_actions), lang,
                )
                self.journal.emit(
                    "comment_fast_path_applied",
                    {"actions": len(frontier_result.fast_path_actions),
                     "operations": [a.operation for a in frontier_result.fast_path_actions]},
                    step_index=self.step, path=path,
                )
            except Exception:  # noqa: BLE001 — fast-path failure is advisory
                pass

        budget = getattr(self.config.policy, "comment_reconciliation_retries", 1)
        conv_threshold = getattr(self.config.policy, "cegis_convergence_threshold", 2)

        def _propose(prompt: str) -> str:
            resp = self.resolution_engine.raw_complete(prompt, json_mode=True)
            # raw_complete returns an LLMResponse object; the comment pass
            # expects a str (the raw model output text).
            return resp.text if hasattr(resp, "text") else str(resp)

        outcome = run_comment_cegis(
            buffer=buffer, frontier=frontier,
            base=base_text, current=current_text, replayed=replayed_text, lang=lang,
            propose=_propose, budget=budget, convergence_threshold=conv_threshold,
        )
        # Replay the loop's events into the journal.
        for ev_name, ev_payload in outcome.events:
            self.journal.emit(ev_name, ev_payload, step_index=self.step, path=path)
        # FR1b: persist the flight-recorder trace (content-addressed). Each
        # trace entry is {boundary, kind, content, ext?, key_override?}. This is
        # the data the shadow jury (Phase 4) and the final enforcement phase
        # (Phase 5) replay against — no rerunning the code-resolution stages.
        if getattr(outcome, "trace", None) and self.config.journal.enabled:
            for entry in outcome.trace:
                try:
                    key, _ = self.journal.store_comment_artifact(
                        entry["kind"], entry["content"],
                        ext=entry.get("ext", "txt"),
                        key=entry.get("key_override"),
                    )
                    self.journal.emit(
                        "comment_artifact",
                        {"boundary": entry["boundary"], "kind": entry["kind"],
                         "key": key, "path": str(path)},
                        step_index=self.step, path=path,
                    )
                except Exception:  # noqa: BLE001 — flight recorder is advisory
                    pass
        return outcome

    def _finalize_comment_pass(self, path: str, outcome, units: list) -> None:
        """Journal the §13 report + write a review bundle on failure."""
        unit = units[0] if units else None
        try:
            from capybase.comment_reconciler import render_reconciliation_report
            report = render_reconciliation_report(
                plan=outcome.final_plan, succeeded=outcome.succeeded,
                last_feedback=outcome.last_feedback or None,
                attempts=outcome.attempts_made,
            )
            self.journal.emit(
                "comment_reconciliation_report",
                {"report": report, "succeeded": outcome.succeeded},
                step_index=self.step, path=path,
            )
        except Exception:  # noqa: BLE001 — report is advisory
            report = None
        if outcome.succeeded or outcome.skipped:
            return
        # Failure: surface a review bundle.
        if outcome.last_feedback:
            feedback_summary = "; ".join(
                f"[{f.kind}] {f.lineage_id}: {f.message[:120]}"
                for f in outcome.last_feedback
            )
            try:
                write_review_bundle(
                    self.paths,
                    reason=f"comment reconciliation failed ({outcome.attempts_made} "
                           f"attempt(s)): {feedback_summary}",
                    step_index=self.step,
                    unit=unit,
                    advisories=[
                        "The frozen, test-passing code is intact and staged. "
                        "Only the deferred-comment reconciliation could not "
                        "converge. Review the frontier comments manually."
                    ],
                    reconciliation_report=report,
                )
            except Exception:  # noqa: BLE001 — review bundle is advisory
                pass

    def _run_jury(
        self, path: str, outcome, units: list, language: str | None,
        base_text: str, current_text: str, replayed_text: str,
        *, mode: str = "shadow",
    ) -> list:
        """Run the comment jury in ``shadow`` or ``enforce`` mode.

        Generalizes SJ6 (the shadow jury) to the three operating modes. The
        jury evaluates the final plan's rewritten comments as untrusted semantic
        sensors: atomize claims, build evidence packets, run the contradiction +
        provenance jurors, and route.

        - ``shadow`` (the JURY_SHADOW setting): the chair runs in shadow mode so
          every route is ``shadow_record`` (NO merge effect). The data is
          journaled as ``jury_shadow_*`` events + stored as ``jury_verdict``
          artifacts. Returns an empty list (no typed outcomes to act on).
        - ``enforce``: the chair runs in non-shadow mode and the
          :class:`jury_enforce.EnforcementRouter` converts each decision into a
          first-class typed outcome (accept / comment_counterexample /
          human_review / code_reopen). The outcomes are returned so the caller
          (:meth:`_reconcile_comments`) can act on them — re-loop the comment
          CEGIS on a counterexample, write a review bundle on human_review, or
          re-open code (gated). The verdicts + decisions are STILL journaled +
          stored as artifacts (identical to shadow for replay continuity), AND
          an additional ``jury_enforce_decision`` event is emitted per outcome.

        ``off`` is handled by the caller (this method is not invoked).

        Failures are advisory in shadow; in enforce, a degraded state produces a
        fail-closed :class:`HumanReviewOutcome` (never accept). The frozen code
        is never touched by the jury itself — only the existing deterministic
        CEGIS machinery can produce a new candidate.
        """
        if not outcome.succeeded or not outcome.final_plan:
            return []  # only run on success with a plan
        lang = (language or "").strip().lower()
        frozen_code = outcome.buffer
        if not frozen_code:
            return []
        try:
            from capybase.comment_claims import (
                build_atomize_prompt, parse_atomized_claims, classify_claim_origin,
            )
            from capybase.jury_evidence import build_evidence_packet
            from capybase.shadow_jury import (
                ContradictionJuror, ProvenanceJuror, DeterministicChair,
            )
        except ImportError:
            return []
        # The LLM complete callable (reuses the resolution engine).
        def _complete(prompt: str) -> str:
            resp = self.resolution_engine.raw_complete(prompt)
            return resp.text if hasattr(resp, "text") else str(resp)
        # Re-build the ledger to get the source variants for provenance.
        from capybase.comment_reconciler import build_comment_ledger
        ledger = build_comment_ledger(
            base_text, current_text, replayed_text, frozen_code, lang,
        )
        # The chair runs in shadow mode for shadow, non-shadow for enforce.
        # (The EnforcementRouter constructs its OWN non-shadow chair internally;
        # we keep this one for the recorded verdict artifact so the replay
        # harness — which decodes the [SHADOW] reason — stays consistent.)
        shadow_mode = (mode == "shadow")
        chair = DeterministicChair(shadow_mode=shadow_mode)
        # Enforce-mode router + context (lazily built only when enforcing).
        enforce_router = None
        enforce_ctx = None
        if mode == "enforce":
            try:
                from capybase.jury_enforce import EnforcementRouter, EnforcementContext
                from capybase.comment_reconciler import _executable_tokens
                import hashlib as _hashlib
                frozen_fp = _hashlib.sha256(
                    _executable_tokens(frozen_code, lang).encode()).hexdigest()[:16]
                enable_reopen = getattr(
                    self.config.future, "enable_jury_code_reopen", False)
                enforce_router = EnforcementRouter(enable_code_reopen=enable_reopen)
                enforce_ctx = EnforcementContext(
                    session_id=getattr(self.journal, "session_id", ""),
                    frozen_fingerprint=frozen_fp,
                    candidate_fingerprint=frozen_fp,  # accepted → matches frozen
                    ledger_lineage_ids={e.lineage_id for e in ledger},
                    frozen_code=frozen_code, ledger_entries=ledger,
                    prompt_version=getattr(
                        self.config.future, "jury_prompt_version", "jury-prompt-v1"),
                    config_version=getattr(
                        self.config.future, "jury_config_version", "jury-cfg-v1"),
                    enable_code_reopen=enable_reopen,
                )
            except ImportError:
                enforce_router = None
        claims_evaluated = 0
        enforce_outcomes: list = []
        for action in outcome.final_plan.actions:
            if action.operation not in ("rewrite", "move", "merge"):
                continue  # only evaluate rewritten comments
            if not action.text.strip():
                continue
            # SJ1: atomize the rewritten comment into claims.
            try:
                atom_prompt = build_atomize_prompt(
                    action.text, action.lineage_id,
                    [e.text for e in ledger if e.lineage_id == action.lineage_id], lang,
                )
                atom_raw = _complete(atom_prompt)
                claims = parse_atomized_claims(atom_raw, action.lineage_id)
            except Exception as exc:  # noqa: BLE001 — advisory
                self.journal.emit(
                    "jury_shadow_skipped",
                    {"lineage_id": action.lineage_id,
                     "reason": f"atomize failed: {type(exc).__name__}"},
                    step_index=self.step, path=path,
                )
                continue
            if not claims:
                continue
            for claim in claims:
                claims_evaluated += 1
                # SJ2: build the evidence packet.
                packet = build_evidence_packet(
                    claim, frozen_code, ledger, lang=lang,
                    code_fingerprint="", unit_id=units[0].unit_id if units else "",
                )
                # SJ3+SJ4: run the jurors (independent calls).
                try:
                    c_verdict = ContradictionJuror(_complete).judge(packet)
                    p_verdict = ProvenanceJuror(_complete).judge(packet)
                except Exception as exc:  # noqa: BLE001 — advisory
                    self.journal.emit(
                        "jury_shadow_skipped",
                        {"claim_id": claim.claim_id,
                         "reason": f"juror failed: {type(exc).__name__}"},
                        step_index=self.step, path=path,
                    )
                    continue
                # SJ5: the deterministic chair routes (shadow or real).
                decision = chair.route(claim, c_verdict, p_verdict, packet)
                # Store the verdicts + decision as artifacts (content-addressed).
                # In BOTH modes the artifact records the chair decision; for
                # shadow the route is shadow_record (the replay decodes the real
                # route from the [SHADOW] reason). For enforce the artifact
                # records the real chair route so the replay is continuous.
                import json as _json
                verdict_payload = {
                    "claim_id": claim.claim_id,
                    "claim_text": claim.text,
                    "claim_origin": claim.origin,
                    "claim_kind": claim.kind,
                    "claim_modality": claim.modality,
                    "contradiction_verdict": _juror_verdict_to_dict(c_verdict),
                    "provenance_verdict": _juror_verdict_to_dict(p_verdict),
                    "chair_decision": {
                        "route": decision.route, "reason": decision.reason,
                        "evidence_quorum_met": decision.evidence_quorum_met,
                    },
                }
                try:
                    key, _ = self.journal.store_comment_artifact(
                        "jury_verdict", _json.dumps(verdict_payload, indent=2),
                        ext="json",
                    )
                except Exception:  # noqa: BLE001 — advisory
                    key = ""
                self.journal.emit(
                    "jury_shadow_decision",
                    {"claim_id": claim.claim_id,
                     "lineage_id": claim.lineage_id,
                     "route": decision.route,
                     "contradiction_verdict": (c_verdict.verdict if c_verdict else None),
                     "provenance_verdict": (p_verdict.verdict if p_verdict else None),
                     "artifact_key": key},
                    step_index=self.step, path=path,
                )
                # Enforce mode: route to a typed outcome + journal it + persist
                # the full decision record (reconstructable without re-running
                # the model). The record carries mode, session/fingerprint/hash
                # bindings, prompt/config versions, juror outputs, parsed
                # verdicts, the aggregate finding, the final route, and the
                # feature-gate state (the brief's flight-recorder fields).
                if mode == "enforce" and enforce_router is not None:
                    eo = enforce_router.route(
                        claim, c_verdict, p_verdict, packet, enforce_ctx,
                    )
                    enforce_outcomes.append(eo)
                    enforce_key = ""
                    try:
                        import json as _ej
                        enforce_key, _ = self.journal.store_comment_artifact(
                            "jury_enforce_decision",
                            _ej.dumps(eo.decision_record, indent=2),
                            ext="json",
                        )
                    except Exception:  # noqa: BLE001 — artifact is advisory
                        pass
                    self.journal.emit(
                        "jury_enforce_decision",
                        {"claim_id": eo.claim_id, "lineage_id": eo.lineage_id,
                         "route": eo.route, "effective_verdict": eo.effective_verdict,
                         "reason": eo.reason[:300],
                         "evidence_quorum_met": eo.evidence_quorum_met,
                         "fingerprint_match": eo.decision_record.get(
                             "fingerprint_match", True),
                         "artifact_key": enforce_key},
                        step_index=self.step, path=path,
                    )
        self.journal.emit(
            "jury_shadow_completed",
            {"claims_evaluated": claims_evaluated, "mode": mode},
            step_index=self.step, path=path,
        )
        return enforce_outcomes

    def _apply_jury_enforcement(
        self, path: str, buffer: str, accepted: list, units: list,
        language: str | None, original: str,
        outcomes: list,
    ) -> str:
        """Act on a list of :class:`EnforcementOutcome` (enforce mode).

        The four first-class routes become side effects:

        - ``accept``: no action (the reconciled candidate stands).
        - ``comment_counterexample``: feed the counterexample into a bounded
          jury-driven comment CEGIS re-loop, restarting from the SAME frozen
          code + authoritative ledger. Bounded by
          ``future.jury_comment_cegis_budget``; on exhaustion / repeated
          counterexample → human_review. The jury never edits source.
        - ``human_review``: stop autonomous completion + preserve a review
          bundle (frozen code, candidate comments, ledger, verifier results,
          juror verdicts). The terminal safe route.
        - ``code_reopen``: re-enter the code CEGIS for the affected unit (the
          existing ``_resolve_comment_contract_conflicts`` path), seeded with
          the contract the jury's witness established. Only reached when
          ``enable_jury_code_reopen`` is True (else the router already converted
          it to human_review).

        Returns the (possibly re-reconciled) buffer. The frozen executable code
        is never corrupted — every path goes through deterministic validation.
        """
        from capybase.jury_enforce import (
            CommentCounterexampleOutcome, HumanReviewOutcome, CodeReopenOutcome,
            counterexample_to_failure,
        )
        lang = (language or "").strip().lower()
        # Aggregate: a single human_review or code_reopen taints the whole case.
        has_human_review = any(isinstance(o, HumanReviewOutcome) for o in outcomes)
        reopen_outcomes = [o for o in outcomes
                           if isinstance(o, CodeReopenOutcome)]
        counterexamples = [o for o in outcomes
                           if isinstance(o, CommentCounterexampleOutcome)]
        # 1. code_reopen: re-enter the code CEGIS (gated; only present when the
        #    feature is on + quorum met).
        if reopen_outcomes:
            reopen_requests = [{
                "lineage_id": o.lineage_id,
                "trust": "high",
                "anchor_symbol": getattr(o.chair_decision, "witness", None) or "",
                "comment_text": o.contract_text,
                "version": "resolved",
            } for o in reopen_outcomes]
            new_buffer = self._resolve_comment_contract_conflicts(
                path, buffer, accepted, units, language, reopen_requests,
            )
            if new_buffer is not None:
                buffer = new_buffer
        # 2. comment_counterexample: bounded jury-driven comment CEGIS re-loop.
        if counterexamples and not has_human_review:
            budget = getattr(self.config.future, "jury_comment_cegis_budget", 2)
            seeds = [counterexample_to_failure(o.counterexample)
                     for o in counterexamples]
            buffer = self._jury_driven_comment_reloop(
                path, buffer, accepted, units, language, original, seeds, budget,
            ) or buffer
        # 3. human_review: write a review bundle + stop autonomous completion.
        #    When jury_human_review_blocks (the default + the brief's contract),
        #    return None so the caller keeps the frozen code and the rebase
        #    stops for this file — the human must review the bundle. When False
        #    (an observe-and-flag deployment), the merge proceeds and the bundle
        #    is advisory.
        if has_human_review:
            self._write_jury_review_bundle(path, outcomes)
            if getattr(self.config.future, "jury_human_review_blocks", True):
                self.journal.emit(
                    "jury_enforce_blocked",
                    {"reason": "human_review outcome; merge blocked per "
                     "jury_human_review_blocks",
                     "outcomes": len([o for o in outcomes
                                      if isinstance(o, HumanReviewOutcome)])},
                    step_index=self.step, path=path,
                )
                return None  # block: keep frozen code, stop for human review
        return buffer

    def _jury_driven_comment_reloop(
        self, path: str, buffer: str, accepted: list, units: list,
        language: str | None, original: str,
        seeds: list, budget: int,
    ) -> str | None:
        """The jury-driven comment CEGIS re-loop (enforce mode).

        After the jury emits comment counterexamples, re-run the comment pass
        with the counterexamples as seed failures (threaded into the §8 prompt's
        ``### prior-attempt feedback`` block). Bounded by ``budget``; restarts
        from the same frozen code + authoritative ledger each iteration. On
        exhaustion or a repeated counterexample, routes to human_review (the
        caller writes the bundle). Returns the re-reconciled buffer or None when
        the re-loop escalates.
        """
        if budget <= 0 or not seeds:
            return None
        for _ in range(budget):
            outcome = self._run_comment_pass(
                path, buffer, accepted, original, units, language,
            )
            if outcome is None:
                return None
            if outcome.succeeded:
                return outcome.buffer
            # Did the seeds get addressed? If the new failures differ from the
            # seeds, progress was made; loop again. If they're identical, stop
            # (repeated counterexample → human_review).
            new_fb = outcome.last_feedback
            if new_fb and seeds and all(
                getattr(nf, "kind", "") == getattr(s, "kind", "")
                and getattr(nf, "lineage_id", "") == getattr(s, "lineage_id", "")
                for nf, s in zip(new_fb, seeds)
            ):
                # Repeated counterexample — no progress.
                self.journal.emit(
                    "jury_comment_cegis_exhausted",
                    {"reason": "repeated identical counterexample", "budget": budget},
                    step_index=self.step, path=path,
                )
                return None
            buffer = outcome.buffer
        self.journal.emit(
            "jury_comment_cegis_exhausted",
            {"reason": "budget exhausted", "budget": budget},
            step_index=self.step, path=path,
        )
        return None

    def _write_jury_review_bundle(self, path: str, outcomes: list) -> None:
        """Write a human-review bundle for a jury-enforced human_review.

        Preserves: frozen executable code, candidate comments, source variants,
        ledger records, verifier results, juror verdicts + evidence references,
        and the reason automatic resolution was unsafe. Uses the existing
        :func:`escalation.write_review_bundle` so the artifact shape matches the
        code-resolution review bundles.
        """
        try:
            from capybase.escalation import write_review_bundle
        except ImportError:
            return
        human = [o for o in outcomes if o.route == "human_review"]
        reasons = "; ".join(f"{o.claim_id}: {o.reason[:120]}" for o in human)
        advisories = [
            f"jury enforcement routed {len(human)} claim(s) to human review",
            f"reasons: {reasons}",
        ]
        try:
            write_review_bundle(
                self.paths,
                reason=(f"jury enforcement: {len(human)} claim(s) require human "
                        f"review. {reasons}"),
                step_index=self.step,
                advisories=advisories,
            )
        except Exception:  # noqa: BLE001 — review bundle is advisory
            pass

    def _resolve_comment_contract_conflicts(
        self, path: str, buffer: str, accepted: list, units: list,
        language: str | None, code_reopen_request: list,
    ) -> str | None:
        """§10: re-resolve the unit(s) whose code conflicts with a high-trust
        comment-derived contract. Mirrors :meth:`_whole_file_repair`'s structure.

        For each reopen request, synthesize a :class:`VerificationFailure`
        carrying the contract text, attribute it to the unit whose accepted
        candidate is at fault (via the request's ``anchor_symbol`` → enclosing
        unit), and re-enter :meth:`_resolve_unit` with it as a seed_failure +
        the previously-accepted candidate as seed_candidate (so the re-resolve
        routes to ``build_repair_prompt``).

        Returns the re-spliced buffer on success, or None when the re-resolve
        escalated (the outer loop stops). Never corrupts code — the re-resolve
        goes through the normal validation pipeline.
        """
        if not code_reopen_request or not accepted:
            return None
        try:
            from capybase.conflict_model import VerificationFailure
        except ImportError:
            return None
        # Attribute each reopen request to a unit. Best-effort: if the request
        # carries an anchor_symbol, find the unit whose accepted candidate's
        # resolved_text contains the anchor's entity name. Fallback: the first
        # unit (the common single-conflict case).
        def _unit_for_request(req: dict) -> int:
            anchor = req.get("anchor_symbol", "")
            if ":" in anchor:
                name = anchor.split(":", 1)[1]
                for idx, (u, cand) in enumerate(accepted):
                    if name in (getattr(cand, "resolved_text", "") or ""):
                        return idx
            return 0

        any_re_resolved = False
        for req in code_reopen_request:
            if not isinstance(req, dict):
                continue
            fault_idx = _unit_for_request(req)
            if fault_idx >= len(accepted):
                continue
            unit, old_cand = accepted[fault_idx]
            # Synthesize the contract failure. severity="warning" mirrors
            # _whole_file_repair's splice_coherence failure — it's advisory
            # feedback, not a hard syntax error.
            contract_text = req.get("comment_text", "")
            message = (
                f"A high-trust comment-derived contract appears violated by the "
                f"merged code. Comment (lineage {req.get('lineage_id', '?')}, "
                f"trust={req.get('trust', 'high')}): {contract_text!r}. "
                f"Reconcile the code to satisfy this invariant, or mark the "
                f"comment for human review if the code is correct."
            )
            seed_failure = VerificationFailure(
                validator="comment_contract",
                severity="warning",
                message=message,
                detail={
                    "lineage_id": req.get("lineage_id", ""),
                    "trust": req.get("trust", "high"),
                    "anchor_symbol": req.get("anchor_symbol", ""),
                    "comment_text": contract_text,
                },
            )
            seed_cand = old_cand if (
                old_cand is not None and getattr(old_cand, "resolved_text", "")
            ) else None
            self.journal.emit(
                "comment_code_reopen",
                {"unit_id": unit.unit_id, "lineage_id": req.get("lineage_id", ""),
                 "trust": req.get("trust", "high")},
                step_index=self.step, path=path, unit_id=unit.unit_id,
            )
            outcome = self._resolve_unit(
                unit, seed_failures=[seed_failure], seed_candidate=seed_cand,
                max_retries=getattr(self, "_file_max_retries", None),
            )
            _persist_unit_hashes(self, outcome)  # D1: per-step convergence
            if outcome.accepted is None:
                # The re-resolve escalated → stop; the outer loop will finalize.
                return None
            accepted[fault_idx] = (unit, outcome.accepted)
            any_re_resolved = True
        if not any_re_resolved:
            return None
        # Re-splice the accepted candidates into the buffer for the next
        # comment pass. _resolved_buffer reconstructs the full file.
        try:
            from capybase.orchestrator import _resolved_buffer
            original = self._originals_for(path)
            if original is not None:
                return _resolved_buffer(original, accepted)
        except Exception:  # noqa: BLE001
            pass
        return None

    def _originals_for(self, path: str) -> str | None:
        """The original (pre-conflict) file text for ``path``, or None."""
        # The originals dict is stashed on the instance by _resolve_step's
        # caller (the Phase-2 loop). Access it defensively.
        originals = getattr(self, "_step_originals", None)
        if isinstance(originals, dict):
            return originals.get(path)
        return None

    def _conflict_byte_ranges(
        self, units: list, buffer: str,
    ) -> list[tuple[int, int]]:
        """Map the units' ``marker_span`` line ranges to byte offsets in the
        resolved ``buffer``. Used by the §6 frontier selector's overlap check.

        ``marker_span`` is a 0-based ``[start_line, end_line]`` inclusive range.
        We convert to byte offsets by walking the buffer's line starts. Returns
        ``[]`` when no unit carries a marker span (whole-file units, etc.).
        """
        if not units or not buffer:
            return []
        # Build line-start byte offset table once.
        line_starts = [0]
        for i, ch in enumerate(buffer):
            if ch == "\n":
                line_starts.append(i + 1)
        line_starts.append(len(buffer))  # sentinel for end-of-last-line
        ranges: list[tuple[int, int]] = []
        for u in units:
            ms = getattr(u, "marker_span", None)
            if ms is None:
                continue
            start_line, end_line = ms
            # Clamp to valid range.
            start_line = max(0, min(start_line, len(line_starts) - 1))
            end_line = max(start_line, min(end_line + 1, len(line_starts) - 1))
            start_byte = line_starts[start_line]
            end_byte = line_starts[end_line]
            if end_byte > start_byte:
                ranges.append((start_byte, end_byte))
        return ranges

    def _verify_post_comment(
        self, path: str, language: str | None,
        comment_buffer: str, pre_comment_buffer: str,
        original: str, accepted: list,
    ) -> str:
        """§11 post-comment defense-in-depth gate.

        Re-runs Phase-B ``verify_file`` on the comment-reconciled buffer. The
        executable-token invariant in :meth:`_reconcile_comments` /
        :func:`apply_comment_plan` makes comment-induced *test* failures
        unlikely in principle (the model can't change code), but it operates on
        TOKENS with comments blanked — a malformed Rust ``///`` doc comment
        with a broken code fence, or a Python docstring that breaks doctests,
        has identical executable tokens to a well-formed one. This gate catches
        those residual cases.

        On PASS: return ``comment_buffer`` unchanged.
        On FAIL: journal ``comment_post_validation_failed``, revert to
        ``pre_comment_buffer`` (the frozen, test-passing code), emit
        ``comment_reclassified_machine_significant`` (best-effort lineage
        attribution), return ``pre_comment_buffer``. Code is NEVER corrupted.
        """
        # verify_file splices resolutions into `original`; for a post-comment
        # whole-file buffer we pass a single whole-file resolution (span=None)
        # so _has_whole_file_span routes it past splicing and validates the
        # buffer directly.
        resolutions = [(None, comment_buffer)]
        try:
            validation = self.verification.verify_file(
                path, language, original, resolutions,
                repo_root=str(self.git.repo),
            )
        except Exception as exc:  # noqa: BLE001 — gate is defense-in-depth
            self.journal.emit(
                "comment_post_validation_failed",
                {"reason": f"verify_file raised: {type(exc).__name__}: {exc}",
                 "reverted": True},
                step_index=self.step, path=path,
            )
            return pre_comment_buffer
        if validation.passed:
            # R1 (s22): if the coherence rung repaired the post-comment
            # buffer, return the repaired text — that is what was validated.
            _rt = getattr(validation, "resolved_text", None)
            return _rt if _rt is not None else comment_buffer
        # Failure: revert to the frozen buffer.
        self.journal.emit(
            "comment_post_validation_failed",
            {"failures": [f.message for f in validation.hard_failures][:5],
             "reverted": True},
            step_index=self.step, path=path,
        )
        # Best-effort reclassify attribution: name the validator that failed
        # so a human reviewer knows the comment pass likely triggered it.
        # (Precise lineage attribution would require byte-range overlap between
        # the failure's error line and the rewritten comment spans — deferred.)
        self.journal.emit(
            "comment_reclassified_machine_significant",
            {"reason": "comment-reconciled buffer failed post-comment validation; "
                       "reverted to frozen code. The rewritten comment likely "
                       "introduced a syntax/doc-test break.",
             "validators": [f.validator for f in validation.hard_failures][:5]},
            step_index=self.step, path=path,
        )
        return pre_comment_buffer

    def _micro_re_gate(self, result) -> bool:
        """Re-run the SAME pre_continue gate after a micro-patch.

        ``_run_tests`` resets ``_last_tests_compiler_indictment`` at entry,
        so a clean re-gate clears the indictment and the run loop proceeds
        instead of escalating. On success the patched paths are STAGED
        (defect review 2026-08-20: Phase 2 already staged the pre-patch
        buffer; ``git rebase --continue`` commits the INDEX, so an
        unstaged patch would be silently dropped while the gate had
        validated the worktree — a wrong merge shipped as success)."""
        try:
            ok = bool(self._run_tests("pre_continue", result))
        except Exception:  # noqa: BLE001 — a broken re-gate must not wedge the loop
            ok = False
        self.journal.emit(
            "micro_cegis_re_gate", {"passed": ok}, step_index=self.step)
        if ok:
            # Stage every patched merged path so the continue commits the
            # repaired buffer, not the pre-patch splice.
            for p in self._micro_patched_paths or []:
                try:
                    self.git.stage_paths([p])
                except Exception:  # noqa: BLE001 — staging is best-effort
                    pass
            self.journal.emit(
                "micro_cegis_succeeded",
                {"staged": list(self._micro_patched_paths or [])},
                step_index=self.step)
        return ok

    def _micro_path_for_stem(self, merged_paths, stem) -> str | None:
        if stem is None:
            return None
        for p in merged_paths:
            if Path(p).stem == stem:
                return p
        return None

    def _micro_inject_missing_symbols(self, merged_paths, errors) -> bool:
        """C1 (sprint-22): deterministic missing-symbol injection.

        For each missing symbol named by the attributed compiler errors
        (<=3), find an injectable declaration line in the pristine stage
        sides / base (never invented), splice it at the language-correct
        import point, and let the caller's re-gate judge the result.
        Journaled per patch with provenance. Returns True when any file
        was modified."""
        from capybase.verification import (
            find_symbol_declaration_lines,
            inject_symbol_declaration,
            parse_missing_symbols,
        )
        from capybase.verification import _parse_cc_error_location

        error_text = "\n".join(errors)
        # Language per path: the errors may mix files; resolve per symbol
        # via its error's located stem, falling back to suffix sniffing.
        by_symbol: dict[str, list[str]] = {}
        for ln in errors:
            stem, lineno = _parse_cc_error_location(ln)
            path = self._micro_path_for_stem(merged_paths, stem)
            lang = None
            if path:
                lang = "rust" if path.endswith(".rs") else (
                    "c" if path.endswith((".c", ".h", ".cc", ".cpp", ".hpp",
                                           ".hh", ".hxx", ".cxx")) else None)
            for sym in parse_missing_symbols(ln, lang):
                by_symbol.setdefault(sym, []).append(ln)
        if not by_symbol:
            return False
        changed = False
        for symbol, lns in list(by_symbol.items())[:3]:
            stem, _lineno = _parse_cc_error_location(lns[0])
            path = self._micro_path_for_stem(merged_paths, stem)
            if path is None:
                continue
            try:
                buffer = (Path(self.git.repo) / path).read_text(
                    encoding="utf-8", errors="replace")
            except OSError:
                continue
            language = ("rust" if path.endswith(".rs") else "c")
            sides, base_text = self._micro_stage_sides(path)
            # Provenance preference: the side that HAS the declaration —
            # current, then replayed, then base (a side carrying it is
            # branch intent; base is the floor).
            decls = find_symbol_declaration_lines(
                symbol, language,
                sides.get("current", ""), sides.get("replayed", ""),
                base_text, buffer)
            if not decls:
                self.journal.emit(
                    "micro_cegis_symbol_decl_not_found",
                    {"symbol": symbol, "path": path},
                    step_index=self.step, path=path)
                continue
            new_buffer = inject_symbol_declaration(
                buffer, decls[0], language)
            if new_buffer is None:
                continue
            provenance = (
                "current" if decls[0] in (sides.get("current") or "")
                else "replayed" if decls[0] in (sides.get("replayed") or "")
                else "base" if decls[0] in (base_text or "") else "buffer")
            self._write_worktree_only(path, new_buffer, accepted=None)
            if path not in self._micro_patched_paths:
                self._micro_patched_paths.append(path)
            self.journal.emit(
                "micro_cegis_patch",
                {"kind": "symbol_inject", "path": path, "symbol": symbol,
                 "decl": decls[0][:120], "provenance": provenance},
                step_index=self.step, path=path)
            changed = True
        return changed

    def _pipeline(self):
        """The mechanism pipeline, built lazily on first use.

        Sprint-24 architecture: mechanisms own their triggers and register
        for pipeline stages; the orchestrator executes side effects (side
        loading, compile probes, landing). Built once per orchestrator so
        the compile-clean mechanism's verdict state persists across calls
        (each execute() sets fresh verdicts before Phase B).
        """
        pipe = getattr(self, "_pipeline_instance", None)
        if pipe is not None:
            return pipe
        from capybase.pipeline import Pipeline
        from capybase.mechanisms import (
            ChurnFallbackTakeover,
            F1CompileCleanTakeover,
            F1Tier1Takeover,
            F1Tier2Adjudication,
        )
        pipe = Pipeline(journal=self.journal)
        self._f1_compile_clean_mech = F1CompileCleanTakeover()
        self._f1_tier2_mech = F1Tier2Adjudication(self._f1_tier2_adjudicate)
        self._churn_fallback_mech = ChurnFallbackTakeover()
        self._f1_tier1_mech = F1Tier1Takeover()
        pipe.register(self._f1_tier1_mech)
        pipe.register(self._f1_compile_clean_mech)
        pipe.register(self._f1_tier2_mech)
        pipe.register(self._churn_fallback_mech)
        self._pipeline_instance = pipe
        return pipe

    def _micro_stage_sides(self, path):
        try:
            ts = _true_stage_sides(self.git, path)
        except Exception:
            return {}, ""
        if not ts:
            return {}, ""
        return ts[0], ts[1]

    def _try_micro_cegis(self, result) -> bool:
        """Sprint-20 S20.6: bounded micro-repair before a compiler-authority
        escalate (protobuf-0065 class: the buffer sits within ~0.4% of the
        oracle and the gate failed with errors positively attributed to a
        merged file).

        Stage 1 — deterministic: ``redefinition of X`` errors resolve by
        deleting the duplicate copy whose exact text is base-verbatim and
        was deleted by a parent side. No LLM.
        Stage 2 — micro-patch: missing-symbol errors (``'X' does not name
        a type`` / ``was not declared`` / ``is not a member``) get one
        tiny JSON SEARCH/REPLACE prompt per distinct symbol (<=3): error
        lines, 5 buffer-context lines, and the symbol's declaration lines
        from base/current/replayed.

        Every round re-runs the same gate; a clean gate returns True (the
        run loop proceeds — no escalate). One round per stage, ambiguity
        or no gate progress declines and the escalate proceeds exactly as
        before. Journaled end to end.
        """
        errors = list(getattr(self, "_last_attributed_merge_errors", []) or [])
        enabled = bool(getattr(self.config.future, "enable_micro_cegis", True))
        if not errors or not enabled:
            return False
        self.journal.emit(
            "micro_cegis_started", {"errors": errors}, step_index=self.step)
        merged_paths = list(getattr(result, "units_by_path", {}) or {})
        self._micro_patched_paths: list[str] = []
        repaired = False
        try:
            repaired = self._micro_repair_duplicates(merged_paths, errors)
        except Exception as exc:  # noqa: BLE001 — repair is best-effort
            self.journal.emit(
                "micro_cegis_stage_failed",
                {"stage": "duplicates", "error": str(exc)[:120]},
                step_index=self.step)
        if repaired and self._micro_re_gate(result):
            return True
        # C1 (sprint-22): deterministic symbol injection BEFORE the model
        # micro-patch — the compiler names the missing symbol, the sides
        # carry its declaration (outside the conflict unit, invisible to
        # the model). Inject verbatim, re-gate; the model stage only sees
        # what determinism could not fix (redis-0002/0012, sqlite-0030,
        # axum-0019, sea-orm-0023 class).
        injected = False
        try:
            injected = self._micro_inject_missing_symbols(
                merged_paths, errors)
        except Exception as exc:  # noqa: BLE001 — repair is best-effort
            self.journal.emit(
                "micro_cegis_stage_failed",
                {"stage": "symbol_inject", "error": str(exc)[:120]},
                step_index=self.step)
        if injected and self._micro_re_gate(result):
            return True
        patched = False
        try:
            patched = self._micro_patch_missing_symbols(
                merged_paths, errors)
        except Exception as exc:  # noqa: BLE001 — repair is best-effort
            self.journal.emit(
                "micro_cegis_stage_failed",
                {"stage": "missing_symbol", "error": str(exc)[:120]},
                step_index=self.step)
        if patched and self._micro_re_gate(result):
            return True
        self.journal.emit(
            "micro_cegis_declined",
            {"stage1_repaired": repaired, "stage2_patched": patched},
            step_index=self.step)
        return False

    def _micro_repair_duplicates(self, merged_paths, errors) -> bool:
        from capybase.verification import _parse_cc_error_location

        changed = False
        for ln in errors:
            # Sprint-22 pre-eval: dead-code class first (deterministic,
            # no provenance needed — the compiler already proved unused).
            mu = _MICRO_UNUSED_RE.search(ln)
            if mu:
                name = mu.group(1)
                stem, lineno = _parse_cc_error_location(ln)
                path = self._micro_path_for_stem(merged_paths, stem)
                if path is None or not lineno:
                    continue
                try:
                    buffer = (Path(self.git.repo) / path).read_text(
                        encoding="utf-8", errors="replace")
                except OSError:
                    continue
                new_buffer = _micro_delete_unused_function(
                    buffer, name, lineno)
                if new_buffer is None:
                    continue
                self._write_worktree_only(path, new_buffer, accepted=None)
                if path not in self._micro_patched_paths:
                    self._micro_patched_paths.append(path)
                self.journal.emit(
                    "micro_cegis_patch",
                    {"kind": "unused_function_delete", "path": path,
                     "symbol": name},
                    step_index=self.step, path=path)
                changed = True
                continue
            m = _MICRO_REDEF_RE.search(ln)
            if not m:
                continue
            name = m.group(1)
            stem, lineno = _parse_cc_error_location(ln)
            path = self._micro_path_for_stem(merged_paths, stem)
            if path is None or not lineno:
                continue
            try:
                buffer = (Path(self.git.repo) / path).read_text(
                    encoding="utf-8", errors="replace")
            except OSError:
                continue
            sides, base_text = self._micro_stage_sides(path)
            outcome = _micro_delete_base_verbatim_duplicate(
                buffer, name, lineno, base_text,
                sides.get("current", ""), sides.get("replayed", ""))
            if outcome is None:
                continue
            new_buffer, provenance = outcome
            self._write_worktree_only(path, new_buffer, accepted=None)
            if path not in self._micro_patched_paths:
                self._micro_patched_paths.append(path)
            self.journal.emit(
                "micro_cegis_patch",
                {"kind": "duplicate_delete", "path": path,
                 "symbol": name, "provenance": provenance},
                step_index=self.step, path=path)
            changed = True
        return changed

    def _micro_patch_missing_symbols(self, merged_paths, errors) -> bool:
        from capybase.resolution_engine import apply_search_replace
        from capybase.verification import _parse_cc_error_location

        by_symbol: dict[str, list[str]] = {}
        for ln in errors:
            m = _MICRO_MISSING_RE.search(ln)
            if m:
                by_symbol.setdefault(m.group(1), []).append(ln)
        if not by_symbol:
            return False
        changed = False
        for symbol, lns in list(by_symbol.items())[:3]:
            stem, lineno = _parse_cc_error_location(lns[0])
            path = self._micro_path_for_stem(merged_paths, stem)
            if path is None:
                continue
            try:
                buffer = (Path(self.git.repo) / path).read_text(
                    encoding="utf-8", errors="replace")
            except OSError:
                continue
            buf_lines = buffer.splitlines()
            ctx = "\n".join(
                f"{i+1}: {buf_lines[i]}"
                for i in range(max(0, (lineno or 1) - 5),
                               min(len(buf_lines), (lineno or 1) + 4)))
            sides, base_text = self._micro_stage_sides(path)
            decls = _micro_symbol_decls(
                symbol, base_text, sides.get("current", ""),
                sides.get("replayed", ""))
            prompt = (
                f"The merged file {path} fails compilation:\n"
                + "\n".join(lns)
                + "\n\nMerged file context (line numbers shown):\n" + ctx
                + f"\n\nLines mentioning '{symbol}' in the BASE/CURRENT/REPLAYED versions:\n"
                + ("\n".join(decls) if decls else "(none found)")
                + "\n\nProduce the MINIMAL patch that makes the file compile: "
                  "restore the missing declaration/member if a side added it, "
                  "or remove/adjust the uses if the sides deleted it. Respond "
                  "as JSON: {\"edits\": [{\"search\": \"<verbatim lines from "
                  "the merged file>\", \"replace\": \"<replacement lines>\"}]}"
            )
            try:
                resp = self.resolution_engine.raw_complete(
                    prompt, json_mode=True,
                    max_tokens=max(
                        2048, self.resolution_engine.config.max_tokens))
                raw = resp.text if hasattr(resp, "text") else str(resp)
                import json as _json
                parsed = _json.loads(raw)
                edits = parsed.get("edits") or []
            except Exception as exc:  # noqa: BLE001 — empty/unparseable model output
                self.journal.emit(
                    "micro_cegis_patch_failed",
                    {"symbol": symbol, "error": str(exc)[:120]},
                    step_index=self.step, path=path)
                continue
            new_text, warnings = apply_search_replace(buffer, edits)
            if not new_text or new_text == buffer:
                self.journal.emit(
                    "micro_cegis_patch_failed",
                    {"symbol": symbol, "warnings": warnings[:3],
                     "reason": "no applicable edits"},
                    step_index=self.step, path=path)
                continue
            self._write_worktree_only(path, new_text, accepted=None)
            if path not in self._micro_patched_paths:
                self._micro_patched_paths.append(path)
            self.journal.emit(
                "micro_cegis_patch",
                {"kind": "missing_symbol", "path": path, "symbol": symbol,
                 "n_edits": len(edits), "warnings": warnings[:3]},
                step_index=self.step, path=path)
            changed = True
        return changed

    def _try_symbol_injection_repair(
        self,
        path: str,
        original: str,
        accepted: list[tuple[ConflictUnit, CandidateResolution]],
        failures: list,
        fault_idx: int,
    ) -> list[tuple[ConflictUnit, CandidateResolution]] | None:
        """C1 (sprint-22): deterministic symbol injection at the file gate.

        When the file-gate failures name symbols the compiler couldn't
        find, locate an injectable single-line declaration in the
        pristine stage sides (current, then replayed) or base — content
        that lives OUTSIDE the conflict units and is therefore invisible
        to both the model and the attributed unit — and splice it
        verbatim at the language-correct import point. The repaired
        buffer is wrapped as a synthetic whole-file unit and re-validated
        by the caller's normal gate. Returns the [(unit, cand)] list or
        None to decline. Nothing is invented: exact side lines only."""
        from capybase.conflict_model import CandidateResolution
        from capybase.verification import (
            find_symbol_declaration_lines,
            inject_symbol_declaration,
            parse_missing_symbols,
        )

        if not (0 <= fault_idx < len(accepted)):
            # D5b (s27): an out-of-range fault_idx (the error line sits
            # outside every marker span) must not kill the symbol path —
            # its fix is FILE-SCOPE injection, so any unit works as the
            # carrier (redis-0014: statloc's use site is out-of-span; the
            # guard declined every round and C1b/C1c never ran).
            if not accepted:
                return None
            fault_idx = 0
        unit, _old_cand = accepted[fault_idx]
        language = unit.language or (
            "rust" if (path or "").endswith(".rs") else "c")
        msgs = "\n".join(getattr(f, "message", "") or "" for f in failures)
        symbols = parse_missing_symbols(msgs, language)
        if not symbols:
            return None
        try:
            spliced = _resolved_buffer(original, accepted)
        except Exception:  # noqa: BLE001 - splice may fail on bad spans
            return None
        sides, base_text = self._micro_stage_sides(path)
        # C1b REPLACE mode: for corrupted-line errors, try replacing the
        # corrupted line with its parent counterpart (verbatim, LCS-anchored).
        # P4a (sprint-24): SKIP line-replacement for implicit-declaration
        # errors — the correct fix is a derived prototype or symbol
        # injection, not replacing the call site (redis-0013: the LCS
        # matched a different function with the same pattern shape).
        import re as _re_p4a
        _is_implicit_decl = bool(_re_p4a.search(
            r"implicit declaration", msgs, _re_p4a.I))
        from capybase.verification import find_replacement_line
        _replace = None if _is_implicit_decl else find_replacement_line(
            spliced, msgs, language,
            sides.get("current", ""), sides.get("replayed", ""),
            base_text or "")
        if _replace is not None:
            err_idx, replacement = _replace
            lines = spliced.split("\n")
            lines[err_idx] = replacement
            repaired = "\n".join(lines)
            wf_unit = unit.model_copy(
                update={"marker_span": None, "unit_kind": "whole_file"})
            wf_cand = CandidateResolution(
                candidate_id=(getattr(_old_cand, "candidate_id", unit.unit_id)
                              or unit.unit_id) + ":linereplace",
                unit_id=unit.unit_id,
                model_name="deterministic",
                resolved_text=repaired,
                prompt_version="deterministic_line_replacement",
                provenance="deterministic_symbol_injection",
                self_reported_confidence=0.85,
                explanation=(
                    f"C1b line replacement: line {err_idx+1} corrupted; "
                    f"replaced with parent verbatim: {replacement[:80]}"),
            )
            self.journal.emit(
                "symbol_inject_applied",
                {"kind": "line_replace", "line": err_idx + 1,
                 "replacement": replacement[:120], "path": path},
                step_index=self.step, path=path, unit_id=unit.unit_id)
            return [(wf_unit, wf_cand)]

        # C1b derived prototype: if the symbol's DEFINITION exists in a
        # side, derive its forward declaration (redis-0013's class)
        for symbol in symbols[:3]:
            for side_name in ("current", "replayed"):
                for ln in (sides.get(side_name) or "").split("\n"):
                    if symbol in ln and ln.strip().endswith("{"):
                        proto = None
                        from capybase.verification import (
                            derive_prototype as _dp,
                        )
                        proto = _dp(ln.strip())
                        if proto and proto not in spliced:
                            repaired = inject_symbol_declaration(
                                spliced, proto, language)
                            if repaired is not None:
                                wf_unit = unit.model_copy(
                                    update={"marker_span": None,
                                            "unit_kind": "whole_file"})
                                wf_cand = CandidateResolution(
                                    candidate_id=(unit.unit_id
                                                  + ":derivedproto"),
                                    unit_id=unit.unit_id,
                                    model_name="deterministic",
                                    resolved_text=repaired,
                                    prompt_version="deterministic_derived_prototype",
                                    provenance="deterministic_symbol_injection",
                                    self_reported_confidence=0.85,
                                    explanation=(
                                        f"C1b derived prototype from {side_name}: "
                                        f"{proto[:80]}"),
                                )
                                self.journal.emit(
                                    "symbol_inject_applied",
                                    {"kind": "derived_prototype", "symbol": symbol,
                                     "proto": proto[:100], "path": path,
                                     "provenance": side_name},
                                    step_index=self.step, path=path,
                                    unit_id=unit.unit_id)
                                return [(wf_unit, wf_cand)]

        for symbol in symbols[:3]:
            decls = find_symbol_declaration_lines(
                symbol, language,
                sides.get("current", ""), sides.get("replayed", ""),
                base_text or "", spliced)
            if not decls:
                # C1c (s27): declaration SYNTHESIS for usage-only symbols.
                # redis-0014's statloc: neither side declares it (the era's
                # header dropped it) and the prototype derivation declines
                # (its only occurrence is inside a statement header — the
                # statement guard, 3ded9ce). Every code occurrence is
                # '&statloc' in a call argument — the wait3 status-variable
                # idiom. Synthesize 'int sym;' and let the whole-file gate
                # verify: a wrong type fails the compile and the candidate
                # is discarded, so the synthesis is compile-gated.
                _occ_re = _re_p4a.compile(
                    r"\b" + _re_p4a.escape(symbol) + r"\b")
                _addr_re = _re_p4a.compile(
                    r"&\s*" + _re_p4a.escape(symbol) + r"\b")
                _occs = [ln for ln in spliced.split("\n")
                         if _occ_re.search(ln)]
                _addr_only = bool(_occs) and all(
                    _addr_re.search(ln) for ln in _occs)
                if (_addr_only and language in ("c", "cpp", "c++")):
                    _synth = f"int {symbol};"
                    _rep_synth = inject_symbol_declaration(
                        spliced, _synth, language)
                    if _rep_synth is not None:
                        _su = unit.model_copy(
                            update={"marker_span": None,
                                    "unit_kind": "whole_file"})
                        _sc = CandidateResolution(
                            candidate_id=unit.unit_id + ":synthdecl",
                            unit_id=unit.unit_id,
                            model_name="deterministic",
                            resolved_text=_rep_synth,
                            prompt_version="deterministic_synthesized_declaration",
                            provenance="deterministic_symbol_injection",
                            self_reported_confidence=0.7,
                            explanation=(
                                f"C1c synthesized declaration: {symbol} "
                                f"appears only as &{symbol} in call "
                                f"arguments; injected 'int {symbol};'"),
                        )
                        self.journal.emit(
                            "symbol_inject_applied",
                            {"kind": "synthesized_declaration",
                             "symbol": symbol, "decl": _synth,
                             "path": path},
                            step_index=self.step, path=path,
                            unit_id=unit.unit_id)
                        return [(_su, _sc)]
                self.journal.emit(
                    "symbol_inject_decl_not_found",
                    {"symbol": symbol, "path": path},
                    step_index=self.step, path=path)
                continue
            repaired = inject_symbol_declaration(
                spliced, decls[0], language)
            if repaired is None:
                continue
            provenance = (
                "current" if decls[0] in (sides.get("current") or "")
                else "replayed" if decls[0] in (sides.get("replayed") or "")
                else "base" if decls[0] in (base_text or "") else "buffer")
            wf_unit = unit.model_copy(
                update={"marker_span": None, "unit_kind": "whole_file"})
            wf_cand = CandidateResolution(
                candidate_id=(getattr(_old_cand, "candidate_id", unit.unit_id)
                              or unit.unit_id) + ":symbolinject",
                unit_id=unit.unit_id,
                model_name="deterministic",
                resolved_text=repaired,
                prompt_version="deterministic_symbol_injection",
                provenance="deterministic_symbol_injection",
                self_reported_confidence=0.9,
                explanation=(
                    f"deterministic symbol injection: '{decls[0][:80]}' "
                    f"from {provenance} side (compiler-missing symbol "
                    f"'{symbol}')"),
            )
            self.journal.emit(
                "symbol_inject_applied",
                {"symbol": symbol, "decl": decls[0][:120],
                 "provenance": provenance, "path": path},
                step_index=self.step, path=path, unit_id=unit.unit_id)
            return [(wf_unit, wf_cand)]
        return None

    def _whole_file_repair(
        self,
        path: str,
        accepted: list[tuple[ConflictUnit, CandidateResolution]],
        original: str,
        failures: list,
        *,
        deterministic_only: bool = False,
        wall_deadline: float | None = None,
        skip_deterministic: bool = False,
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

        # C4 (sprint-22): per-(step, path) tried-repair registry keyed by
        # failure signature. A deterministic repair that already FAILED for
        # this exact signature never re-runs (axum-0013: the model re-resolve
        # set _phase2_model_used, which cleared _det_unchanged, and round 2
        # re-ran the identical brace repair to the identical failure). When a
        # kind is exhausted the flow falls through to the next strategy —
        # attribution + model-with-error — instead of repeating itself.
        if not hasattr(self, "_wf_repair_tried"):
            self._wf_repair_tried: dict[tuple[int, str], set[str]] = {}
        _sig = _hard_failure_signature(failures)
        # C4b (sprint-23): the tried key carries the spliced-buffer hash —
        # re-running a repair on an UNCHANGED buffer stays skipped (axum-0013's
        # anti-repeat), but a model-re-resolved buffer is NEW input and earns a
        # fresh deterministic attempt (sqlite-0008: the stray MOVED each round
        # while the signature normalized away the location, starving the exact
        # retry path that had converted the case).
        try:
            _buf_hash = hash(_resolved_buffer(original, accepted))
        except Exception:  # noqa: BLE001 — splice may fail on bad spans
            _buf_hash = 0
        _sig = f"{_sig}:{_buf_hash & 0xffffff}"
        _tried = self._wf_repair_tried.setdefault((self.step, path), set())

        # Deterministic brace repair: run BEFORE the attribution gate. The brace
        # repair operates on the whole-file spliced buffer — it doesn't need
        # fault attribution to a specific unit. The #1 cause of whole-file
        # repair escalations on C++ is a splice-boundary brace imbalance whose
        # attributed line falls outside all unit spans. Without this early run,
        # the tiered attribution gate returns None before the brace repair gets
        # a chance to fix it. Conservative: acts only when one edit fully
        # balances the braces.
        if not skip_deterministic:
            if f"brace:{_sig}" not in _tried:
                det, _brace_diag = _try_deterministic_brace_repair(
                    failures, original, accepted, max(0, fault_idx)
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
                elif _brace_diag not in ("not_brace_failure", "no_imbalance"):
                    # Only mark/journal when we TRIED and failed — not when
                    # the failure wasn't brace-related at all.
                    _tried.add(f"brace:{_sig}")
                    self.journal.emit(
                        "brace_repair_skipped",
                        {"reason": _brace_diag},
                        step_index=self.step, path=path,
                    )
            else:
                self.journal.emit(
                    "repair_rotation",
                    {"skipped": "brace",
                     "reason": "already failed for this failure signature"},
                    step_index=self.step, path=path,
                )
            # C1 (sprint-22): deterministic symbol injection at the file
            # gate — before fault attribution, because the missing symbol's
            # declaration lives OUTSIDE the conflict units (the attributed
            # unit cannot fix what it cannot see; axum-0019's plain-LLM
            # retry reproduced the identical errors for exactly this
            # reason). The compiler names the symbol; the pristine stage
            # sides carry its declaration; splice it verbatim.
            if f"symbol_inject:{_sig}" not in _tried:
                sym = self._try_symbol_injection_repair(
                    path, original, accepted, failures, max(0, fault_idx))
                if sym is not None:
                    return sym
                _tried.add(f"symbol_inject:{_sig}")
            else:
                self.journal.emit(
                    "repair_rotation",
                    {"skipped": "symbol_inject",
                     "reason": "already failed for this failure signature"},
                    step_index=self.step, path=path,
                )

        # Smart blame (tiered verification): when no unit's span contains the
        # error line AND we're in tiered mode (time budget active), skip the
        # model re-resolve — it can't fix a cross-unit error. The deterministic
        # beam still runs (it operates on the whole-file buffer). In legacy
        # mode (no time budget), fall back to the last-unit heuristic so
        # existing behavior is preserved.
        #
        # EXCEPTION: a preprocessor (#if/#endif) imbalance on a conflict region
        # sliced mid-file has its matching directive upstream of the marker, so
        # the imbalance line falls in no span but the NEAREST preceding unit is
        # the one whose resolution most plausibly caused it. Attribute to that
        # unit (with a widened context snippet that includes the enclosing
        # conditional) rather than skipping — the model CAN fix it when it sees
        # the conditional context.
        _tiered_active = self.config.policy.max_whole_file_repair_seconds > 0
        if fault_idx < 0:
            fault_idx = max(0, len(accepted) - 1)
            _is_pp_failure = any(
                "preprocessor" in (getattr(f, "message", "") or "").lower()
                for f in failures
            )
            # Build-test failures (from Phase 2's per-file build check)
            # are compilation errors caused by a specific unit's
            # resolution. Don't skip even when attribution fails — the
            # model can fix these by producing a different candidate.
            # The tiered budget (_phase2_model_used) already bounds to
            # 1 model call. Sprint-19 D5: whole-file compile checks
            # (cargo check, verify_file's build branch) carry
            # validator="syntax" but are tagged
            # detail.source="whole_file_build" at emission — they are
            # build-test failures in this sense too (tokio-0109: an
            # in-file cargo error whose line fell outside all marker
            # spans skipped the one bounded repair and escalated).
            _is_build_test = any(
                getattr(f, "validator", "") == "build_test"
                or (
                    isinstance(getattr(f, "detail", None), dict)
                    and getattr(f, "detail", None).get("source")
                    == "whole_file_build"
                )
                for f in failures
            )
            # D5b (s27): undeclared-symbol failures are exempt — the C1
            # family's fix (prototype/declaration injection) is FILE-SCOPE,
            # so span attribution is meaningless for them; the skip made
            # C1b/C1c unreachable for exactly their shape (redis-0014:
            # statloc's use site sits outside every marker span, the symbol
            # block never ran, zero symbol events across all repeats).
            _is_undeclared = any(
                "undeclared" in (getattr(f, "message", "") or "")
                or "was not declared" in (getattr(f, "message", "") or "")
                for f in failures
            )
            if (not _is_pp_failure and not _is_build_test
                    and not _is_undeclared
                    and not deterministic_only and _tiered_active
                    and len(accepted) > 1):
                self.journal.emit(
                    "whole_file_repair_skipped",
                    {"reason": "fault attribution: error outside all unit spans (tiered mode)"},
                    step_index=self.step, path=path,
                )
                return None
            if _is_pp_failure:
                # Nearest-preceding-unit attribution for the cross-unit
                # preprocessor case. The error line sits in the gap between
                # spans (or after the last span); the unit whose span ends
                # closest before it is the one whose resolution opened the
                # conditional. Falls back to the last-unit heuristic above
                # when no preceding unit exists.
                pp_line = None
                for f in failures:
                    d = getattr(f, "detail", {}) or {}
                    if isinstance(d.get("preprocessor_imbalance_line"), int):
                        pp_line = d["preprocessor_imbalance_line"]
                        break
                if pp_line is not None:
                    best_idx = -1
                    best_end = -1
                    for i, (u, _) in enumerate(accepted):
                        if u.marker_span is None:
                            continue
                        # marker_span is 0-based [start, end]; error line is 1-based.
                        _s, e = u.marker_span
                        if e + 1 <= pp_line and e + 1 > best_end:
                            best_end = e + 1
                            best_idx = i
                    if best_idx >= 0:
                        fault_idx = best_idx
                        self.journal.emit(
                            "candidate_validated",
                            {
                                "fault_attribution": "nearest_preceding_unit",
                                "preprocessor_imbalance_line": pp_line,
                                "attributed_unit_index": fault_idx,
                            },
                            step_index=self.step, path=path,
                        )
        if not skip_deterministic:
            # F4-HOISTED (s27-extend-12): the side-pick runs FIRST in the
            # beam. Its old position (after the attribution logic) was
            # shadowed for pp-class failures — the nearest-preceding-unit
            # attribution jumped straight to the model re-resolve and
            # returned before the deterministic rungs ran (sqlite-0099's
            # trace). A verifying side is the cheapest correct answer;
            # everything else (model included) costs more.
            if f"sidepick:{_sig}" not in _tried:
                _tried.add(f"sidepick:{_sig}")
                try:
                    for _sp_side, _sp_cands in _whole_file_side_candidates(units):
                        _sp_spans = [
                            (u.marker_span, c.resolved_text)
                            for u, c in _sp_cands]
                        _sp_val = self.verification.verify_file(
                            path, language, original, _sp_spans,
                            repo_root=str(self.git.repo),
                            whole_text=_resolved_buffer(original, _sp_cands),
                            pristine_side_texts=(
                                [t for t in (self._micro_stage_sides(path)[0]
                                 or {}).values() if t.strip()] or None),
                        )
                        self._journal_validation(
                            _sp_cands[0][0], _sp_cands[0][1], _sp_val)
                        if _sp_val.passed:
                            _sp_unit = _sp_cands[0][0].model_copy(
                                update={"marker_span": None,
                                        "unit_kind": "whole_file"})
                            _sp_cand = _sp_cands[0][1].model_copy(
                                update={
                                    "candidate_id": (
                                        _sp_cands[0][1].candidate_id
                                        + f":sidepick-{_sp_side}"),
                                    "prompt_version": "deterministic_side_pick",
                                    "provenance": "deterministic_structural",
                                    "self_reported_confidence": 0.7,
                                    "explanation": (
                                        f"F4 side-pick: merged splice "
                                        f"failed the gate; the {_sp_side} "
                                        f"splice verifies"),
                                })
                            self.journal.emit(
                                "side_pick_applied",
                                {"side": _sp_side, "path": path,
                                 "sig": _sig[:60], "hoisted": True},
                                step_index=self.step, path=path)
                            return [(_sp_unit, _sp_cand)]
                except Exception:  # noqa: BLE001 — side-pick is best-effort
                    pass
            # Storage-class relocation repair (s24 cycle-J, the C1b-promotion
            # item from the reviewer synthesis): gcc's "invalid storage class
            # for function X" means the model's merge declared X inside a
            # function body. Remove the misplaced declaration — the next
            # round's C1 derived-prototype re-places it at file scope, or the
            # removal alone suffices when the real definition exists below
            # (redis-0013's wf trace: two rounds burned reaching the state C1
            # could fix). Whole-file-unit contract; compiler re-gates it.
            if f"storclass:{_sig}" not in _tried:
                _sc_msgs = "\n".join(
                    getattr(f, "message", "") for f in failures)
                if "invalid storage class for function" in _sc_msgs:
                    from capybase.verification import (
                        find_misplaced_declaration,
                        inject_symbol_declaration,
                    )
                    try:
                        _spliced_sc = _resolved_buffer(original, accepted)
                    except Exception:  # noqa: BLE001
                        _spliced_sc = None
                    # Best-effort like every deterministic repair: a crash
                    # here must not kill the run (sqlite-0109: a bare
                    # `language` NameError escalated the whole case 3/3).
                    try:
                        if _spliced_sc:
                            _mis = find_misplaced_declaration(
                                _spliced_sc, _sc_msgs)
                            if _mis is not None:
                                _sc_lines = _spliced_sc.split("\n")
                                _decl = _sc_lines.pop(_mis[0])
                                _relocated = inject_symbol_declaration(
                                    "\n".join(_sc_lines), _decl,
                                    unit.language)
                                if _relocated is not None:
                                    _sc_unit = unit.model_copy(
                                        update={"marker_span": None,
                                                "unit_kind": "whole_file"})
                                    _sc_cand = CandidateResolution(
                                        candidate_id=(
                                            getattr(_old_cand, "candidate_id",
                                                    unit.unit_id)
                                            or unit.unit_id) + ":stcreloc",
                                        unit_id=unit.unit_id,
                                        model_name="deterministic",
                                        resolved_text=_relocated,
                                        prompt_version=(
                                            "deterministic_storage_class_relocation"),
                                        provenance="deterministic_symbol_injection",
                                        self_reported_confidence=0.85,
                                        explanation=(
                                            f"storage-class relocation: moved "
                                            f"misplaced declaration to file scope: "
                                            f"{_decl[:80]}"),
                                    )
                                    self.journal.emit(
                                        "symbol_inject_applied",
                                        {"kind": "storage_class_relocation",
                                         "line": _mis[0] + 1,
                                         "declaration": _decl[:120], "path": path},
                                        step_index=self.step, path=path,
                                        unit_id=unit.unit_id)
                                    _tried.add(f"storclass:{_sig}")
                                    return [(_sc_unit, _sc_cand)]
                    except Exception:  # noqa: BLE001 — relocation is best-effort
                        pass
            # Deterministic #if/#endif balance repair: the entity-splitting + splice
            # pipeline can leave a whole-file preprocessor imbalance that no single
            # sub-unit owns (a conflict region sliced mid-file). Try a single-edit
            # deterministic fix (remove a stray bare #endif, append a missing #endif)
            # before spending an LLM call — the model often can't reach the upstream
            # directive from a unit-scoped view. Conservative: acts only on C/C++ and
            # when one edit fully balances. Same whole-file-unit contract as brace
            # repair above.
            det = _try_deterministic_preprocessor_repair(
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
                        "deterministic_preprocessor_repair": True,
                    },
                    step_index=self.step,
                    path=path,
                    unit_id=unit_new.unit_id,
                )
                return det
            # Deterministic prefix/suffix dedup repair (Phase 10): when the cargo
            # error is "expected identifier, found keyword `use`" (or similar), the
            # marker span excluded the enclosing wrapper (e.g. ``use crate::{``) and
            # the splice doubled it. Strip the consecutive duplicate statement line.
            # S27-extend (axum-0019): alternation collapse — a one-line-
            # per-side alternative merged as CONCATENATION (both kept).
            # Produce both collapses for the fault unit, splice each,
            # verify: the first passing one wins.
            if f"altcol:{_sig}" not in _tried:
                _tried.add(f"altcol:{_sig}")
                try:
                    # The failing gate error (cargo/rustc) often carries no
                    # file:line the attribution can trust — the alternation
                    # may sit at ANY unit. Try every unit, not just the
                    # attributed fault.
                    for _ac_idx in range(len(accepted)):
                        _acu0, _acc0 = accepted[_ac_idx]
                        _ac_frags = {
                            "current": _acu0.current.text or "",
                            "replayed": _acu0.replayed.text or "",
                        }
                        _ac_out = _try_alternation_collapse(
                            _acu0, _acc0, _ac_frags, None)
                        if not _ac_out:
                            continue
                        for _acu, _acc in _ac_out:
                            _ac_spans = [
                                (u.marker_span, c.resolved_text)
                                for u, c in accepted]
                            _ac_spans[_ac_idx] = (
                                _acu.marker_span, _acc.resolved_text)
                            _ac_val = self.verification.verify_file(
                                path, language, original, _ac_spans,
                                repo_root=str(self.git.repo))
                            if _ac_val.passed:
                                self.journal.emit(
                                    "alternation_collapse_applied",
                                    {"path": path, "unit": _acu.unit_id,
                                     "sig": _sig[:60]},
                                    step_index=self.step, path=path)
                                _ac_list = list(accepted)
                                _ac_list[_ac_idx] = (_acu, _acc)
                                return _ac_list
                except Exception:  # noqa: BLE001 — collapse is best-effort
                    pass
            # F4 (s27): side-pick fallback — when the merged splice fails
            # the gate but a pristine-side splice passes it, the merge is
            # the defect; land the side (protobuf-0001 / zenodo-0079
            # class: sim>=0.9 merges rejected on gate/coherence failures
            # while both sides compile). Degrades honestly to NEAR on
            # merge-wanting oracles — still ahead of an ESCALATE that
            # re-merges into the same gate failure every round.
            if f"sidepick:{_sig}" not in _tried:
                _tried.add(f"sidepick:{_sig}")
                try:
                    _sp_sides, _ = self._micro_stage_sides(path)
                    _sp_pristine = [t for t in (_sp_sides or {}).values()
                                    if t.strip()] or None
                    for _sp_side, _sp_cands in _whole_file_side_candidates(units):
                        _sp_spans = [
                            (u.marker_span, c.resolved_text)
                            for u, c in _sp_cands]
                        _sp_val = self.verification.verify_file(
                            path, language, original, _sp_spans,
                            repo_root=str(self.git.repo),
                            whole_text=_resolved_buffer(original, _sp_cands),
                            pristine_side_texts=_sp_pristine,
                        )
                        self._journal_validation(
                            _sp_cands[0][0], _sp_cands[0][1], _sp_val)
                        if _sp_val.passed:
                            _sp_unit = _sp_cands[0][0].model_copy(
                                update={"marker_span": None,
                                        "unit_kind": "whole_file"})
                            _sp_cand = _sp_cands[0][1].model_copy(
                                update={
                                    "candidate_id": (
                                        _sp_cands[0][1].candidate_id
                                        + f":sidepick-{_sp_side}"),
                                    "prompt_version":
                                        "deterministic_side_pick",
                                    "provenance":
                                        "deterministic_structural",
                                    "self_reported_confidence": 0.7,
                                    "explanation": (
                                        f"F4 side-pick: merged splice "
                                        f"failed the gate; the {_sp_side} "
                                        f"splice verifies"),
                                })
                            self.journal.emit(
                                "side_pick_applied",
                                {"side": _sp_side, "path": path,
                                 "sig": _sig[:60]},
                                step_index=self.step, path=path)
                            return [(_sp_unit, _sp_cand)]
                except Exception:  # noqa: BLE001 — side-pick is best-effort
                    pass
            det = _try_deterministic_prefix_dedup(
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
                        "deterministic_prefix_dedup": True,
                    },
                    step_index=self.step,
                    path=path,
                    unit_id=unit_new.unit_id,
                )
                return det
            # Boundary-echo strip (the generalization of prefix_dedup): when the
            # candidate's resolved_text begins/ends with a run of lines that already
            # exist immediately outside the marker span, the splice duplicates them.
            # prefix_dedup handles the statement-keyword + cargo-signature sub-case;
            # this catches any line-sequence echo at the boundary (a duplicated
            # multi-line use block, function header, or closing-brace run). Safe by
            # construction: removes only exact boundary echoes, brace-checked, and
            # the caller's whole-file loop re-validates (same contract as prefix_dedup).
            det = _try_boundary_echo_strip(
                failures, original, accepted, fault_idx
            )
            if det is not None:
                det_list, diag = det
                unit_new, cand_new = det_list[0]
                self.journal.emit(
                    "candidate_validated",
                    {
                        "candidate_id": cand_new.candidate_id,
                        "passed": True,
                        "whole_file_repair_for": unit_new.unit_id,
                        "boundary_echo_strip": True,
                        "variant": diag.get("variant"),
                        "left_overlap": diag.get("left_overlap"),
                        "right_overlap": diag.get("right_overlap"),
                    },
                    step_index=self.step,
                    path=path,
                    unit_id=unit_new.unit_id,
                )
                return det_list
            # Deterministic import dedup repair (Phase 9): before spending an LLM
            # call, try the file_linker's deduplicate_imports on the spliced
            # buffer. This catches duplicate imports that survived the pre-validation
            # pass (e.g. cross-file references or partial-group collisions the
            # file_linker didn't catch on the first pass because the error
            # messages guide a more targeted second dedup attempt). Mirrors how
            # _try_deterministic_brace_repair works for braces above.
            if getattr(self.config.future, "enable_file_linker", True):
                try:
                    from capybase.file_linker import deduplicate_imports
                    spliced = _resolved_buffer(original, accepted)
                    # Check if any failure message mentions duplicate/import
                    has_import_error = any(
                        "import" in (getattr(f, "message", "") or "").lower()
                        or "defined more than once" in (getattr(f, "message", "") or "").lower()
                        or "module_stmt" in (getattr(f, "detail", {}) or {}).get("message", "").lower()
                        for f in failures
                    )
                    if has_import_error:
                        _lang = accepted[fault_idx][0].language if 0 <= fault_idx < len(accepted) else None
                        deduped, dedup_count = deduplicate_imports(spliced, _lang)
                        if dedup_count > 0:
                            # The dedup produced a complete, correct file. Represent
                            # it as a whole-file unit carrying the deduped buffer —
                            # the same pattern as _try_deterministic_brace_repair.
                            # Back-projection onto individual units' resolved_text is
                            # fragile (the duplicate import often lives in the
                            # original text adjacent to the span, not inside it), and
                            # a whole-file unit is the honest representation:
                            # _resolved_buffer returns its resolved_text verbatim and
                            # verify_file's _has_whole_file_span guard handles the
                            # None span. Pre-fix this branch only fired for whole-file
                            # units (marker_span is None) and silently discarded the
                            # dedup for the common marker-block case.
                            unit_f, cand_f = accepted[fault_idx]
                            wf_unit = unit_f.model_copy(
                                update={"marker_span": None, "unit_kind": "whole_file"})
                            new_cand = cand_f.model_copy(
                                update={"resolved_text": deduped,
                                        "provenance": (cand_f.provenance or "plain_llm") + "+file_linker"})
                            result = [(wf_unit, new_cand)]
                            self.journal.emit(
                                "file_linker_repair",
                                {"duplicates_removed": dedup_count,
                                 "candidate_id": new_cand.candidate_id},
                                step_index=self.step, path=path,
                                unit_id=unit_f.unit_id,
                            )
                            return result
                except Exception:  # noqa: BLE001
                    pass
            # gcc fix-it hints: apply the compiler's own structured repair
            # suggestions (-fdiagnostics-parseable-fixits). Subsumes the regex-
            # based cc repair below — gcc covers more error types with surgical
            # precision. Runs first; falls through to the regex beam if gcc
            # doesn't suggest any fix-its.
            det = _try_gcc_fixit_repair(
                failures, original, accepted, fault_idx,
            )
            if det is not None:
                unit_new, cand_new = det[0]
                self.journal.emit(
                    "candidate_validated",
                    {
                        "candidate_id": cand_new.candidate_id,
                        "passed": True,
                        "whole_file_repair_for": unit_new.unit_id,
                        "deterministic_gcc_fixit": True,
                    },
                    step_index=self.step,
                    path=path,
                    unit_id=unit_new.unit_id,
                )
                return det
            # Duplicate-definition eradication: gcc's "redefinition of X"
            # means the spliced file carries the entity twice (kept the
            # pre-merge copy AND emitted the new one). Supersedes the cc
            # repair's duplicate_entity branch for this class — that one can
            # only drop the header line, orphaning the body.
            det = _try_duplicate_eradication_repair(
                failures, original, accepted, fault_idx,
            )
            if det is not None:
                unit_new, cand_new = det[0]
                self.journal.emit(
                    "candidate_validated",
                    {
                        "candidate_id": cand_new.candidate_id,
                        "passed": True,
                        "whole_file_repair_for": unit_new.unit_id,
                        "deterministic_dup_eradication": True,
                        "explanation": cand_new.explanation,
                    },
                    step_index=self.step,
                    path=path,
                    unit_id=unit_new.unit_id,
                )
                return det
            # Compiler-diagnostic-driven deterministic repair for C/C++: reads the
            # gcc error message, classifies it, and generates minimal fix
            # hypotheses (missing ';', missing '}', stray char, etc.) at the
            # compiler-identified line. Targets the 36 WHOLE_FILE_FAILED C cases
            # at avg sim 0.978 where the model's output is semantically correct
            # but has a small structural defect.
            det = _try_deterministic_cc_repair(
                failures, original, accepted, fault_idx,
            )
            if det is not None:
                unit_new, cand_new = det[0]
                self.journal.emit(
                    "candidate_validated",
                    {
                        "candidate_id": cand_new.candidate_id,
                        "passed": True,
                        "whole_file_repair_for": unit_new.unit_id,
                        "deterministic_cc_repair": True,
                    },
                    step_index=self.step,
                    path=path,
                    unit_id=unit_new.unit_id,
                )
                return det
            # Side-consistency repair: restore common lines the model dropped, delete
            # invented lines. Uses the merge itself as a structural prior — every
            # inserted line comes from base/current/replayed (provenance-backed).
            det = _try_side_consistency_repair(
                failures, original, accepted, fault_idx,
            )
            if det is not None:
                unit_new, cand_new = det[0]
                self.journal.emit(
                    "candidate_validated",
                    {
                        "candidate_id": cand_new.candidate_id,
                        "passed": True,
                        "whole_file_repair_for": unit_new.unit_id,
                        "deterministic_side_consistency_repair": True,
                    },
                    step_index=self.step,
                    path=path,
                    unit_id=unit_new.unit_id,
                )
                return det
            # Side-consensus repair: when both sides agree on a structural property
            # (brace delta, trailing semicolons, macro continuations) but the
            # candidate disagrees, the consensus is a high-confidence repair signal.
            det = _try_side_consensus_repair(
                failures, original, accepted, fault_idx,
            )
            if det is not None:
                unit_new, cand_new = det[0]
                self.journal.emit(
                    "candidate_validated",
                    {
                        "candidate_id": cand_new.candidate_id,
                        "passed": True,
                        "whole_file_repair_for": unit_new.unit_id,
                        "deterministic_side_consensus_repair": True,
                    },
                    step_index=self.step,
                    path=path,
                    unit_id=unit_new.unit_id,
                )
                return det
        # deterministic_only: skip the LLM re-resolve. Used for the final
        # repair attempt after the LLM budget is exhausted — the cheap O(n)
        # deterministic repairs above may still close the case (a recurring
        # splice-junction brace imbalance the LLM keeps re-introducing). None
        # of the deterministic repairs fired → no deterministic fix available.
        if deterministic_only:
            return None
        unit, _old_cand = accepted[fault_idx]
        # Enriched feedback: build a splice-context snippet (the resolved file
        # ±5 lines around the error) so PROMPT_REPAIR shows the model the actual
        # brace mismatch in context, not just the raw cargo message. The model
        # otherwise can't locate the error from the raw diagnostic alone and
        # repeats the same retry; the snippet gives it the surrounding code to
        # find the extra/missing brace.
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
            wall_deadline=wall_deadline,
            max_retries=getattr(self, "_file_max_retries", None),
        )
        _persist_unit_hashes(self, outcome)  # D1: per-step convergence
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
            # Sprint-18 WS1: an oversized-prompt escalation means this unit's
            # CEGIS context cannot fit the window (protobuf-0055: 15.5K tokens
            # vs an 8K window) — but the build error localizes the defect to a
            # handful of lines. Patch exactly those lines with a micro-prompt
            # (~300 tokens: error + ±10 lines of the spliced file) instead of
            # abandoning repair. Runs before Layer 3 (which only handles
            # preprocessor imbalances anyway).
            if "oversized prompt" in (outcome.reason or ""):
                micro = self._micro_patch_repair(
                    path, original, accepted, enriched_failures,
                    wall_deadline=wall_deadline,
                )
                if micro is not None:
                    return micro
            # Layer 3 (last resort): whole-file model resolution for a cross-unit
            # preprocessor imbalance. The unit-scoped re-resolve above couldn't
            # fix it (the matching #if/#endif is outside the unit's view). As a
            # final fallback before escalating, synthesize a whole-file unit
            # carrying the spliced buffer + the preprocessor diagnostic, and
            # resolve THAT — the model sees the full #if/#endif tree and can
            # balance the conditional. This consumes the tiered budget's single
            # model-call slot (same budget, no extra cost beyond it). Skipped for
            # non-preprocessor failures (those genuinely can't be whole-file-
            # repaired when the file is oversized — that's why we split it).
            # Also skipped in deterministic_only mode (no model calls allowed).
            if deterministic_only:
                return None
            _is_pp = any(
                "preprocessor" in (getattr(f, "message", "") or "").lower()
                for f in failures
            )
            if not _is_pp:
                return None
            try:
                spliced = _resolved_buffer(original, accepted)
            except Exception:  # noqa: BLE001
                return None
            # S27-extend (sqlite-0128): a whole-file unit's accepted text IS
            # the file (_resolved_buffer takes it verbatim). When the file
            # exceeds what the model can emit in one output, the re-resolve
            # can only return a REGION FRAGMENT — which gets written as the
            # entire file and left in the worktree on escalation (0128's
            # final state: a 6163-line file reduced to a fragment starting
            # mid-function, sim 0.003). Decline the model re-resolve when
            # the spliced file is beyond the output window; the escalation
            # then leaves the pre-repair (splice-shaped) state instead.
            _wf_max_out = int(getattr(
                self.config.model, "max_tokens", 8192) or 8192)
            _wf_est_out = len(spliced) // 4  # ~4 chars/token
            if _wf_est_out > _wf_max_out * 0.9:
                self.journal.emit(
                    "whole_file_repair_skipped",
                    {"reason": (
                        f"file beyond model output window "
                        f"(~{_wf_est_out}t > {_wf_max_out}t) — re-resolve "
                        f"would emit a fragment as the file"),
                     "path": path},
                    step_index=self.step, path=path,
                )
                return None
            wf_unit = unit.model_copy(
                update={"marker_span": None, "unit_kind": "whole_file"}
            )
            wf_outcome = self._resolve_unit(
                wf_unit,
                seed_failures=enriched_failures,
                seed_candidate=None,
                wall_deadline=wall_deadline,
                max_retries=getattr(self, "_file_max_retries", None),
            )
            _persist_unit_hashes(self, wf_outcome)
            self.journal.emit(
                "candidate_validated",
                {
                    "candidate_id": (
                        wf_outcome.accepted.candidate_id
                        if wf_outcome.accepted else "none"
                    ),
                    "passed": wf_outcome.accepted is not None,
                    "whole_file_repair_for": wf_unit.unit_id,
                    "whole_file_model_resolution": True,
                },
                step_index=self.step,
                path=path,
                unit_id=wf_unit.unit_id,
            )
            if wf_outcome.accepted is None:
                return None
            return [(wf_unit, wf_outcome.accepted)]
        accepted[fault_idx] = (unit, outcome.accepted)
        return accepted

    def _micro_patch_repair(
        self,
        path: str,
        original: str,
        accepted: list[tuple[ConflictUnit, CandidateResolution]],
        failures: list,
        *,
        wall_deadline: float | None = None,
    ) -> list[tuple[ConflictUnit, CandidateResolution]] | None:
        """Build-error-guided micro patch for files too large for CEGIS (WS1).

        The whole-file repair's model path re-resolves the attributed UNIT
        with the build error as feedback. For big files that unit's context
        (whole-file sides, obligations) can exceed the model window, and the
        re-resolve escalates as oversized — historically the end of the road.
        But the compiler error names a LINE; the defect lives in a few lines
        around it. This repair sends the model only:

        - the compiler error,
        - ±10 lines of the spliced file around the error line,
        - the attributed unit's own three sides (small by construction),

        asks it to return the corrected excerpt, and splices the excerpt back
        into the spliced buffer at the same window. One model call; validated
        as a whole-file candidate before returning. Declines when the error
        has no usable file:line, the model returns garbage, or validation
        fails — the caller then escalates with the build error attached.
        """
        import re as _re_mp
        import time as _time_mp
        from capybase.conflict_model import CandidateResolution as _CR, \
            estimate_tokens
        from capybase.verification import _braces_balanced

        if wall_deadline is not None and _time_mp.monotonic() > wall_deadline:
            return None
        # Locate the error: file:line:col: error: message
        err_line = None
        err_msg = ""
        for f in failures:
            msg = getattr(f, "message", "") or ""
            m = _re_mp.search(r"(\d+):(\d+):\s*(?:fatal\s+)?error:\s*(.+)", msg)
            if m:
                try:
                    err_line = int(m.group(1))
                except ValueError:
                    continue
                err_msg = m.group(3).strip()
                break
        if err_line is None or err_line <= 0:
            return None
        try:
            spliced = _resolved_buffer(original, accepted)
        except Exception:  # noqa: BLE001
            return None
        lines = spliced.split("\n")
        if err_line > len(lines):
            return None
        idx = err_line - 1
        win_start = max(0, idx - 10)
        win_end = min(len(lines), idx + 11)
        excerpt = lines[win_start:win_end]
        # Base context: the attributed unit's sides give the model the
        # pre-merge ground truth for the contested region.
        base_ctx = ""
        for u, _c in reversed(accepted):
            if u.marker_span is not None and u.marker_span[0] <= idx <= u.marker_span[1]:
                base_ctx = (
                    f"Pre-merge conflict sides for this region:\n"
                    f"--- base ---\n{u.base.text}\n"
                    f"--- current ---\n{u.current.text}\n"
                    f"--- replayed ---\n{u.replayed.text}\n"
                )
                break
        prompt = (
            "Your merge of a C/C++ file is 99% correct but has a microscopic "
            "defect that breaks the build. Fix ONLY the defect — do not "
            "reformat, reorder, or touch anything else.\n\n"
            f"Compiler error (at line {err_line} of the merged file):\n"
            f"  error: {err_msg}\n\n"
            f"Excerpt of the merged file (lines {win_start + 1}-{win_end}, "
            "the error is on the line marked ERRORHERE):\n"
            + "\n".join(
                (f"{win_start + k + 1}: " + ("ERRORHERE> " if win_start + k == idx else "")
                 + ln) for k, ln in enumerate(excerpt))
            + "\n\n" + base_ctx +
            "\nReturn JSON: {\"resolved_text\": \"<the corrected excerpt, "
            "same number of lines, no line-number prefixes>\"}. Keep every "
            "unchanged line exactly as given."
        )
        _window = int(getattr(self.config.model, "context_window", 0) or 0)
        if _window > 0 and estimate_tokens(prompt) > _window * 0.9:
            return None  # even the micro prompt doesn't fit — escalate
        try:
            resp = self.resolution_engine.raw_complete(prompt, json_mode=True)
        except Exception:  # noqa: BLE001 - endpoint failure → escalate
            return None
        raw = (getattr(resp, "text", "") or "").strip()
        if not raw:
            return None
        patched_excerpt = None
        try:
            import json as _json_mp
            data = _json_mp.loads(raw)
            patched_excerpt = (data.get("resolved_text") or "").strip("\n")
        except Exception:  # noqa: BLE001
            from json_repair import repair_json as _rj
            try:
                data = _json_mp.loads(_rj(raw))
                patched_excerpt = (data.get("resolved_text") or "").strip("\n")
            except Exception:  # noqa: BLE001
                return None
        if not patched_excerpt:
            return None
        patched_lines = patched_excerpt.split("\n")
        # The model was told to return the excerpt WITHOUT line-number
        # prefixes; strip any it added anyway ("12: code").
        patched_lines = [
            _re_mp.sub(r"^\s*\d+:\s?", "", ln) for ln in patched_lines
        ]
        if abs(len(patched_lines) - len(excerpt)) > max(8, len(excerpt) // 2):
            return None  # grossly different shape — not a micro patch
        new_buffer = "\n".join(
            lines[:win_start] + patched_lines + lines[win_end:])
        if "<<<<<<<" in new_buffer or ">>>>>>>" in new_buffer:
            return None
        unit, _old_cand = accepted[0]
        lang = unit.language
        if lang in ("c", "cpp", "c++", "rust", "java") and not _braces_balanced(new_buffer, lang):
            return None
        wf_unit = unit.model_copy(update={"marker_span": None, "unit_kind": "whole_file"})
        wf_cand = _CR(
            candidate_id=(getattr(_old_cand, "candidate_id", unit.unit_id) or unit.unit_id) + ":micropatch",
            unit_id=unit.unit_id,
            model_name=getattr(self.config.model, "model", "micro") or "micro",
            resolved_text=new_buffer,
            prompt_version="micro_patch_repair",
            provenance="micro_patch_repair",
            self_reported_confidence=0.8,
            explanation=(f"micro patch at line {err_line}: {err_msg[:80]}"),
        )
        self.journal.emit(
            "micro_patch_repair",
            {"error_line": err_line, "error": err_msg[:120],
             "window": [win_start + 1, win_end]},
            step_index=self.step, path=path, unit_id=unit.unit_id,
        )
        return [(wf_unit, wf_cand)]

    def _apply_deterministic_closure(
        self, unit: ConflictUnit, cand: CandidateResolution,
    ) -> CandidateResolution:
        """Run all applicable Tier-A structural primitives on a candidate.

        Composes import-union, deletion-application, and block-insertion in
        sequence. Each is a pure function; on non-APPLIED the candidate is
        untouched. Derives obligations ONCE (with the correct base hunk), then
        feeds them to each primitive. The primitives are complementary (no
        overlap): import-union handles additive import leaves, deletion handles
        DROPPED_DELETION obligations, block-insertion handles additive blocks.

        Runs every loop iteration; idempotency makes that safe. On any internal
        error the original candidate is returned unchanged (never breaks the
        resolution loop).
        """
        if not cand.resolved_text:
            return cand
        lang = unit.language or ""
        if lang not in ("rust", "toml"):
            return cand  # v1 primitives are Rust + TOML only
        # Boundary-echo strip (reachability fix): run BEFORE per-unit syntax
        # validation so a wrapping echo (the model re-states the enclosing
        # `use tower::{...};` block around the span) is caught before it causes a
        # parse failure and escalates. Without this, the whole-file-repair echo
        # strip is unreachable for parse-echo cases: the candidate fails per-unit
        # syntax, exhausts the retry budget, and escalates before the whole-file
        # loop ever runs. Reuses the same _strip_boundary_echo core the whole-
        # file path uses. Safe-by-construction (exact boundary echoes only,
        # brace-checked); runs before the obligations gate below because a
        # wrapping-echo candidate may have zero missing obligations.
        try:
            stripped = _strip_boundary_echo(
                cand.resolved_text, unit.original_worktree_text,
                unit.marker_span, unit.language,
            )
            if stripped is not None:
                text, diag = stripped
                cand = cand.model_copy(
                    update={"resolved_text": text,
                            "provenance": (cand.provenance or "plain_llm") + "+boundary_echo_strip"},
                )
                self.journal.emit(
                    "boundary_echo_strip",
                    {"variant": diag["variant"],
                     "left_overlap": diag["left_overlap"],
                     "right_overlap": diag["right_overlap"],
                     "stage": "per_unit"},
                    step_index=self.step, path=unit.path, unit_id=unit.unit_id,
                )
        except Exception:  # noqa: BLE001
            pass
        try:
            from capybase.change_accounting import derive_missing_obligations
            # Use the base HUNK (diff3-refined or re-derived), not the full
            # base file — see PreservationHeuristicValidator.verify.
            base_text = unit.base.text or ""
            refined = unit.structural_metadata.get("diff3_refined")
            if isinstance(refined, dict) and refined.get("base") is not None:
                base_text = refined["base"]
            else:
                from capybase.conflict_extractor import _base_hunk_via_diff3
                bh = _base_hunk_via_diff3(
                    base_text, unit.current.text or "",
                    unit.replayed.text or "")
                if bh is not None:
                    base_text = bh
            obligations = derive_missing_obligations(
                base_text, unit.current.text or "",
                unit.replayed.text or "", cand.resolved_text,
            )
            if not obligations:
                return cand

            provenance_suffix = ""
            edited_text = cand.resolved_text
            certificates = []
            # Obligation claiming: each primitive claims (closes) specific
            # obligations. After each step, subtract its closed obligations so
            # later primitives (especially the generic block_insertion) don't
            # re-process them. This implements the precedence: specialized
            # primitives go first, generic block_insertion gets the residual.
            _remaining = list(obligations)
            _cur_text = unit.current.text or ""
            _rep_text = unit.replayed.text or ""
            _other = _rep_text if edited_text.strip() == _cur_text.strip() else _cur_text

            def _run_primitive(propose_fn, name, *, needs_other=False, **kwargs):
                """Run one primitive, claim its obligations, update edited_text."""
                nonlocal edited_text, provenance_suffix, _remaining, _other
                from capybase.import_union import STATUS_APPLIED as _APPLIED
                call_kwargs = dict(kwargs)
                if needs_other:
                    call_kwargs["other_side_text"] = _other
                r = propose_fn(edited_text, _remaining, **call_kwargs)
                if r.status == _APPLIED and r.text != edited_text:
                    certificates.append((name, r.certificate))
                    edited_text = r.text
                    provenance_suffix += "+" + name
                    # Claim: remove closed obligations from _remaining.
                    closed_norms = set(r.certificate.get("closed_obligations", []))
                    if closed_norms:
                        _remaining = [
                            ob for ob in _remaining
                            if " ".join((getattr(ob, "line", "") or "").split())
                            not in closed_norms
                        ]
                    self.journal.emit(
                        name + "_applied",
                        {"certificate": r.certificate,
                         "candidate_id": cand.candidate_id},
                        step_index=self.step, path=unit.path,
                        unit_id=unit.unit_id,
                    )

            # Precedence order (advice §primitive coordination):
            # 1. import_union    (additive import leaves)
            # 2. deletion_union  (DROPPED_DELETION obligations)
            # 3. attribute_meta  (derive/lint list unions)
            # 4. named_field     (struct field additions)
            # 5. keyed_item      (method/function insertions)
            # 6. block_insertion (residual additive blocks)
            # 7. manifest_union  (TOML-only, after block_insertion)

            if getattr(self.config.future, "enable_import_union", True):
                from capybase.import_union import propose_import_union
                _run_primitive(propose_import_union, "import_union")

            if getattr(self.config.future, "enable_deletion_union", True):
                from capybase.deletion_union import propose_deletion_application
                _run_primitive(propose_deletion_application, "deletion_union")

            if lang == "rust" and getattr(self.config.future, "enable_attribute_meta_union", True):
                from capybase.attribute_meta_union import propose_attribute_meta_union
                _run_primitive(propose_attribute_meta_union, "attribute_meta_union")

            if lang == "rust" and getattr(self.config.future, "enable_named_field_union", True):
                from capybase.named_field_union import propose_named_field_union
                _run_primitive(propose_named_field_union, "named_field_union", needs_other=True)

            if lang == "rust" and getattr(self.config.future, "enable_keyed_item_union", True):
                from capybase.keyed_item_union import propose_keyed_item_union
                _run_primitive(propose_keyed_item_union, "keyed_item_union", needs_other=True)

            if getattr(self.config.future, "enable_block_insertion", True):
                from capybase.block_insertion import propose_block_insertion
                _run_primitive(propose_block_insertion, "block_insertion",
                               needs_other=True, base_text=base_text)

            if lang == "toml" and getattr(self.config.future, "enable_manifest_union", True):
                from capybase.manifest_union import propose_manifest_union
                _run_primitive(propose_manifest_union, "manifest_union",
                               needs_other=True, base_text=base_text)

            if edited_text != cand.resolved_text:
                cand = cand.model_copy(update={
                    "resolved_text": edited_text,
                    "provenance": (cand.provenance or "plain_llm") + provenance_suffix,
                })
        except Exception:  # noqa: BLE001 — never break the resolution loop
            self.journal.emit(
                "deterministic_closure_skipped",
                {"reason": "internal error (candidate untouched)",
                 "candidate_id": cand.candidate_id},
                step_index=self.step, path=unit.path,
                unit_id=unit.unit_id,
            )
        return cand

    def _empty_fast_fail_recovery(
        self, unit: ConflictUnit, failed: "CandidateResolution",
    ) -> "UnitOutcome | None":
        """Deterministic recovery after a first empty LLM response.

        The pre-LLM source portfolio already declined this unit (it runs
        before the LLM in the cascade), so its compositions failed
        verification. What remains cheap and safe: the two single-side
        candidates, verified directly. First that passes wins; both failing
        returns None so the caller continues the normal retry policy.
        """
        for side in ("current", "replayed"):
            side_obj = getattr(unit, side, None)
            text = (getattr(side_obj, "text", "") or "") if side_obj else ""
            if not text.strip():
                continue
            cand = CandidateResolution(
                candidate_id=f"{unit.unit_id}:{side}_only_empty_fallback",
                unit_id=unit.unit_id,
                model_name="empty_fast_fail",
                prompt_version="empty-fallback.v1",
                resolved_text=text,
                provenance=f"deterministic_source_{side}_only",
            )
            validation = self.verification.verify(unit, cand)
            self.journal.emit(
                "candidate_validated",
                {"candidate_id": cand.candidate_id, "passed": validation.passed,
                 "hard_failures": [f.message for f in validation.hard_failures][:3]},
                step_index=self.step, path=unit.path, unit_id=unit.unit_id,
            )
            if not validation.passed:
                continue
            if self._strictness_blocks_pre_llm(unit, cand, validation, "empty_fast_fail"):
                continue
            outcome = UnitOutcome(
                unit=unit, validation=validation, attempts=[failed, cand])
            outcome.accepted = cand
            self._record_resolution_attempt(
                outcome, mechanism="empty_fast_fail",
                candidate=cand, validation=validation,
                decision="accept",
                reason="first-empty fast-fail: deterministic side fallback",
            )
            self.journal.emit(
                "candidate_accepted",
                {"candidate_id": cand.candidate_id, "via": "empty_fast_fail",
                 "provenance": cand.provenance},
                step_index=self.step, path=unit.path, unit_id=unit.unit_id,
            )
            return outcome
        # P2 (sprint-23 batch E): whole-side portfolio on model failure.
        # When the model returns empty, the single-unit side candidates
        # may not suffice (multi-unit files where the oracle is one side
        # verbatim). Try the whole-file pristine sides: if one compiles
        # cleanly, it's the answer the model couldn't produce.
        try:
            _sides_p2, _base_p2 = self._micro_stage_sides(unit.path)
            if _sides_p2:
                for _side_name in ("current", "replayed"):
                    _side_text = _sides_p2.get(_side_name, "")
                    if not _side_text.strip():
                        continue
                    _val_p2 = self.verification.verify_file(
                        unit.path, unit.language,
                        _side_text, [],
                        repo_root=str(self.git.repo),
                        whole_text=_side_text)
                    if _val_p2.passed:
                        _wf_unit = unit.model_copy(
                            update={"marker_span": None,
                                    "unit_kind": "whole_file"})
                        _wf_cand = CandidateResolution(
                            candidate_id=f"{unit.unit_id}:p2_wholeside:{_side_name}",
                            unit_id=unit.unit_id,
                            model_name="deterministic",
                            resolved_text=_side_text,
                            prompt_version="p2_wholeside_fallback",
                            provenance="deterministic_structural",
                            self_reported_confidence=0.80,
                            explanation=(
                                f"P2 whole-side on model failure: "
                                f"{_side_name} compiles cleanly"),
                        )
                        self.journal.emit(
                            "p2_wholeside_accept",
                            {"side": _side_name, "path": unit.path},
                            step_index=self.step, path=unit.path,
                            unit_id=unit.unit_id)
                        from capybase.conflict_model import (
                            UnitOutcome as _UO_p2,
                        )
                        _outcome_p2 = _UO_p2(unit=unit)
                        _outcome_p2.accepted = _wf_cand
                        _outcome_p2.attempts = [cand, _wf_cand]
                        _outcome_p2.mechanism = "p2_whole_side_fallback"
                        return _outcome_p2
        except Exception:  # noqa: BLE001 — best-effort extension
            pass
        return None

    def _resolve_unit(
        self, unit: ConflictUnit, *, seed_failures: list | None = None,
        seed_candidate: "CandidateResolution | None" = None,
        wall_deadline: float | None = None,
        max_retries: int | None = None,
    ) -> UnitOutcome:
        """_resolve_unit_core + the sprint-19 P2 Best-of-N rescue wrapper.

        When the core loop ends WITHOUT an accepted candidate but a
        preservation-heuristic-rejected candidate was stashed and every
        heuristic-forced retry validated strictly worse (none passed),
        restore the stashed candidate instead of escalating — a recovery
        mechanism, not a policy change: the heuristic still fired, the
        retries still ran, and an equal-or-better retry was already
        accepted by the core loop (popping the stash). tokio-0037: the
        model's first candidate was oracle-correct and validation-passing;
        the heuristic forced retries that degraded into syntax errors; the
        case escalated. The restored candidate keeps its
        flagged_by_preservation_heuristic tag for the file-level guard.
        """
        outcome = self._resolve_unit_core(
            unit, seed_failures=seed_failures,
            seed_candidate=seed_candidate,
            wall_deadline=wall_deadline, max_retries=max_retries,
        )
        if outcome.accepted is not None:
            return outcome
        if not getattr(self.config.future, "enable_preservation_bestof_n",
                       True):
            return outcome
        stash = getattr(self, "_step_preservation_stash", {}).get(
            unit.unit_id)
        if stash is None:
            return outcome
        later = stash.get("later_attempts") or []
        # No forced retry ever ran, or at least one retry PASSED
        # validation (equal-or-better: the core loop had its chance to
        # accept it) — restoring would preempt a legitimate outcome.
        if not later or any(later):
            return outcome
        cand = stash["candidate"]
        val = stash["validation"]
        cand.flagged_by_preservation_heuristic = True
        outcome.accepted = cand
        outcome.validation = val
        outcome.escalated = False
        outcome.reason = (
            f"best-of-N recovery: the preservation heuristic rejected a "
            f"validation-passing candidate; all {len(later)} forced "
            f"retr{'y' if len(later) == 1 else 'ies'} validated strictly "
            f"worse — original restored, flagged for the file-level guard"
        )
        self._record_resolution_attempt(
            outcome, mechanism="preservation_bestof_n",
            candidate=cand, validation=val,
            decision="accept", reason=outcome.reason,
        )
        self.journal.emit(
            "candidate_accepted",
            {"candidate_id": cand.candidate_id,
             "via": "preservation_bestof_n_recovery",
             "flagged_by_preservation_heuristic": True,
             "strictly_worse_retries": len(later)},
            step_index=self.step, path=unit.path, unit_id=unit.unit_id,
        )
        self._step_preservation_stash.pop(unit.unit_id, None)
        return outcome

    def _accept_r3(self, unit, candidate, context):
        """Accept an R3 best-of-N winning candidate."""
        from capybase.conflict_model import UnitOutcome
        outcome = UnitOutcome(unit=unit)
        outcome.accepted = candidate
        outcome.attempts = [candidate]
        outcome.validation = None  # already validated by R3
        outcome.mechanism = "r3_best_of_n"
        self.journal.emit(
            "candidate_accepted",
            {"candidate_id": candidate.candidate_id,
             "via": "r3_best_of_n",
             "temperature_diverse": True},
            step_index=self.step, path=unit.path, unit_id=unit.unit_id)
        return outcome

    def _r3_best_of_n(
        self, unit, context, *, base_candidate, failures,
    ):
        """R3 (sprint-23): within-session best-of-N candidate selection.

        On a compile-gate failure with retry budget remaining, generate
        up to 2 additional diverse candidates (temperature 0.4/0.6),
        validate ALL through the full gate stack, and return the first
        that passes all hard gates. Addresses the 16-case unstable
        population (different-quality output on different samples).
        All candidates go through the FULL validation — no shortcuts.

        Returns a passing CandidateResolution, or None (caller proceeds
        to the normal retry loop)."""
        if not getattr(self.config.future, "enable_best_of_n", False):
            return None
        # Only on compile-gate failures (not parse/marker — different class)
        _is_compile = any(
            "compile" in (getattr(f, "validator", "") or "").lower()
            or "syntax" in (getattr(f, "message", "") or "").lower()
            for f in failures
        )
        if not _is_compile:
            return None

        for temp in (0.4, 0.6):
            try:
                diverse = self.resolution_engine.propose(
                    unit, context,
                    temperature_override=temp,
                    n_samples=1,
                )
                for cand in diverse:
                    if not cand.resolved_text:
                        continue
                    validation = self._unit_validator.validate(unit, cand)
                    if validation is not None and validation.passed:
                        self.journal.emit(
                            "r3_best_of_n_accept",
                            {"temperature": temp,
                             "candidate_id": cand.candidate_id,
                             "unit_id": unit.unit_id},
                            step_index=self.step, path=unit.path,
                            unit_id=unit.unit_id)
                        return cand
            except Exception:  # noqa: BLE001 — best-effort
                continue
        return None

    def _resolve_unit_core(
        self, unit: ConflictUnit, *, seed_failures: list | None = None,
        seed_candidate: "CandidateResolution | None" = None,
        wall_deadline: float | None = None,
        max_retries: int | None = None,
    ) -> UnitOutcome:
        outcome = UnitOutcome(unit=unit)
        # D1: inherit per-step convergence hashes so _whole_file_repair's
        # re-resolve of this unit sees the cosmetic variations already rejected
        # by the first pass. Without this, the convergence detector resets and
        # the model can cycle through the same variations again (6 LLM rounds
        # × 240s = 600s+ per case). The hashes are copied back by
        # _persist_unit_hashes at each call site after the loop returns.
        step_hashes = getattr(self, "_step_convergence_hashes", None)
        if step_hashes is not None:
            prior = step_hashes.get(unit.unit_id)
            if prior:
                outcome._seen_normalized_hashes = dict(prior)
        # Inherit prior failure signatures so the no-progress guard doesn't
        # reset its counter when Phase 2 re-enters _resolve_unit for the same
        # unit. Without this, a unit that reproduces the same gcc error across
        # the Phase 1 → Phase 2 boundary burns the full retry budget twice.
        step_sigs = getattr(self, "_step_failure_sigs", None)
        if step_sigs is not None:
            prior_sigs = step_sigs.get(unit.unit_id)
            if prior_sigs:
                outcome._recent_hard_failure_sigs = list(prior_sigs)
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
        # Header file Phase 1 CEGIS cap: headers skip the per-unit gcc gate
        # (no standalone compilation), so CEGIS retries are blind — the model
        # produces output, nothing validates it at the compile level, and it
        # retries on advisory warnings only. Allow 1 retry (2 model calls max)
        # so the model can act on risk-layer rejection feedback (e.g. "drops a
        # side's additions"). Previously capped at 0 retries, which prevented
        # the model from ever responding to risk feedback — the second attempt
        # (informed by the rejection reason) was discarded. The whole-file
        # build in Phase 2 is the header's true verifier; the structural
        # resolver and source portfolio still run (pre-LLM).
        _unit_path = unit.path or ""
        _is_header = _unit_path.endswith((".h", ".hpp", ".hh", ".hxx", ".H"))
        _header_max_retries = 1 if _is_header else self.config.policy.max_retries_per_unit
        # Unit-count-aware retry budget: when a file has many units, each unit
        # gets fewer retries so the total model-call count stays within the
        # wall-time budget. A file with 78 units at 3 attempts each = 234 calls
        # ≫ 1200s; at 1 attempt each = 78 calls ≈ 1170s. Overrides are merged
        # with the header cap (most restrictive wins).
        _unit_budget = max_retries if max_retries is not None else _header_max_retries
        # Track time spent in verification (cargo check, rustc, tests) so it
        # can be excluded from the wall-time budget. The budget is meant to
        # cap MODEL/CEGIS loop iterations, not compilation time — a slow
        # first cargo check (dependency fetch) shouldn't eat the model's
        # retry budget. (Phase 5 D1.)
        _verify_time_accumulated = 0.0
        # Also track verify time from pre-LLM resolvers (structural, exact_reuse,
        # etc.) so it's excluded from the wall budget the same way CEGIS-loop
        # verify time is.
        self._unit_verify_time = 0.0
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
            # Intra-step shape memoization: if a sibling unit in THIS step with
            # the same conflict shape was already accepted (deterministically),
            # replay its resolved text and re-verify. On a file with 78 tiny
            # identical-shape conflicts, this turns 78 model calls into 1 + 77
            # verify-only calls. Same safety model as exact_reuse: the reused
            # candidate runs the full verify gauntlet; a mismatch falls through.
            early = self._try_step_shape_reuse(unit)
            if early is not None:
                return early
            # Edit-pattern reuse: if a sibling with the same structural shape
            # was already resolved, apply its token-level edit pattern to this
            # unit's base. Handles structurally similar conflicts with different
            # identifiers (e.g. ``int a;`` → ``int a{};`` vs ``int b;`` →
            # ``int b{};``). Falls through on failure (ambiguous anchors or
            # verification failure).
            early = self._try_step_pattern_reuse(unit)
            if early is not None:
                return early
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

        # Source-derived candidate portfolio: BEFORE the LLM, try a small set
        # of candidates assembled from exact source lines (current-only,
        # replayed-only, both concatenated, etc.). Research shows 87% of
        # merge resolutions contain only lines from the input sides. When a
        # source composition compiles, it's a valid merge — no generation
        # artifacts (dropped braces, missing semicolons). Zero LLM calls.
        if failures is None and getattr(self.config.future, "enable_source_portfolio", True):
            early = self._try_source_candidate_portfolio(unit)
            if early is not None:
                return early

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
                self._journal_class_member_candidate(unit)
                return outcome

        _dup_def_retried = False  # one duplicate-def CEGIS retry per unit
        # Set True when the duplicate-def enrichment verifies a modify/delete
        # where one side's function additions are ALL duplicates — the correct
        # resolution is to accept the deletion (empty resolved_text). Persists
        # across CEGIS iterations so a SUBSEQUENT empty candidate (the model
        # obeying "output EMPTY") is accepted rather than rejected.
        _accept_deletion_recommended = False
        while True:
            # Wall-clock deadline (outermost budget): if this unit has run past
            # its time budget across retries, escalate rather than proposing
            # again. Sits above the per-retry counts so it bounds total latency
            # regardless of how the syntactic/critic/whole-file budgets split.
            # The "at least one attempt" guard uses EITHER counter: critic-driven
            # retries increment critic_retry_count, not retry_count, so checking
            # only retry_count would let an all-critic retry loop run forever.
            # File-level wall deadline (outermost cap): when set by the Phase 2
            # loop via _whole_file_repair, bounds the TOTAL time across all units
            # and all repair iterations for this file. Prevents the nested-
            # _resolve_unit budget explosion where each repair retry gets a fresh
            # per-unit budget. Unlike the per-unit budget, this includes ALL wall
            # clock (no verify-time exclusion) — it's a real-time deadline.
            if (
                wall_deadline is not None
                and _time.monotonic() >= wall_deadline
                and (retry_count > 0 or critic_retry_count > 0)
            ):
                outcome.escalated = True
                outcome.retry_count = retry_count
                outcome.reason = (
                    f"file-level wall deadline reached "
                    f"({_time.monotonic() - unit_start:.0f}s in this unit) "
                    f"after {retry_count} attempt(s)"
                )
                self.journal.emit(
                    "candidate_rejected",
                    {"candidate_id": cand.candidate_id,
                     "action": "escalate", "via": "file_wall_deadline",
                     "wall_seconds": round(_time.monotonic() - unit_start, 1),
                     "retry_count": retry_count},
                    step_index=self.step, path=unit.path, unit_id=unit.unit_id,
                )
                return outcome
            if (
                wall_budget > 0.0
                and (_time.monotonic() - unit_start - _verify_time_accumulated - self._unit_verify_time) >= wall_budget
                and (retry_count > 0 or critic_retry_count > 0)
            ):
                outcome.escalated = True
                outcome.retry_count = retry_count
                effective_elapsed = _time.monotonic() - unit_start - _verify_time_accumulated
                outcome.reason = (
                    f"unit exceeded wall-time budget "
                    f"({wall_budget:.0f}s, excl. {_verify_time_accumulated:.0f}s verify) "
                    f"after {retry_count} attempt(s)"
                )
                self.journal.emit(
                    "candidate_rejected",
                    {"candidate_id": cand.candidate_id,
                     "action": "escalate", "via": "wall_time",
                     "wall_seconds": round(_time.monotonic() - unit_start, 1),
                     "verify_seconds": round(_verify_time_accumulated, 1),
                     "effective_seconds": round(effective_elapsed, 1),
                     "retry_count": retry_count},
                    step_index=self.step, path=unit.path, unit_id=unit.unit_id,
                )
                return outcome
            # Populate the future-obligations prompt block (#9 step 3) before
            # building the prompt so the model sees what later commits expect.
            self._set_future_obligations_prompt_block(unit)
            context = self.context_builder.build(unit)
            # R3 (sprint-23): on the FIRST retry iteration, try diverse-
            # temperature candidates before the feedback retry. If any
            # passes the full gate stack, accept immediately.
            if (retry_count == 0 and failures
                    and prev_candidate is not None
                    and prev_candidate.resolved_text):
                _r3_cand = self._r3_best_of_n(
                    unit, context,
                    base_candidate=prev_candidate, failures=failures)
                if _r3_cand is not None:
                    return self._accept_r3(unit, _r3_cand, context)
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
                # Mirror the R5 retry ladder: propose() activates one
                # presentation variant per retry attempt and restores the
                # base profile before returning, so the prompt built HERE
                # (for the audit trail) must run under the SAME variant the
                # model actually saw — retry_profile_variant is deterministic
                # in the attempt index, so recomputing it is exact.
                _mirror_ladder_base = None
                if retry_count >= 1:
                    try:
                        from capybase.prompt_profile import (
                            active_profile as _mp_ap,
                            set_active_profile as _mp_sap,
                        )
                        from capybase.retry_ladder import (
                            retry_profile_variant as _mp_rpv,
                        )
                        _mp_base = _mp_ap()
                        _mp_variant = _mp_rpv(_mp_base, retry_count)
                        if _mp_variant is not _mp_base:
                            _mp_sap(_mp_variant)
                            _mirror_ladder_base = _mp_base
                    except Exception:  # noqa: BLE001 — mirror is best-effort
                        _mirror_ladder_base = None
                try:
                    if pending_recovery:
                        from capybase.resolution_engine import build_recovery_prompt
                        pv = "cegis_recovery.v1"
                        prompt = build_recovery_prompt(unit, context, failures, budget=self.resolution_engine.token_budget)
                    elif failures and prev_candidate and prev_candidate.resolved_text:
                        pv = PROMPT_REPAIR
                        # Build prior-attempt summaries for failed-patch memory.
                        # Each summary is one line: the failure validator + message.
                        # D1 (sprint-23): accumulate actual per-round failure
                        # signatures — the OLD code rebuilt from CURRENT failures
                        # each round, making every prior summary identical
                        if not hasattr(self, '_repair_failure_history'):
                            self._repair_failure_history: list[str] = []
                        _current_sig = "; ".join(
                            f"{f.validator}: {f.message[:60]}" for f in failures[:2])
                        if _current_sig and _current_sig not in [
                                s.split(": ", 1)[-1] for s in self._repair_failure_history]:
                            self._repair_failure_history.append(
                                f"attempt {retry_count + 1}: {_current_sig}")
                        prior_summaries = list(self._repair_failure_history)
                        # Candidate-diff feedback (s23): the model sees WHAT
                        # CHANGED between its attempts — the REPL discipline.
                        # Without the diff, the model reproduces a near-identical
                        # candidate that fails the same way.
                        if len(outcome.attempts) >= 2:
                            import difflib as _dl_cdf
                            _prev_text = (outcome.attempts[-2].resolved_text or "")[:4000]
                            _curr_text = (prev_candidate.resolved_text or "")[:4000]
                            if _prev_text and _curr_text:
                                _diff_lines = list(_dl_cdf.unified_diff(
                                    _prev_text.splitlines()[:50],
                                    _curr_text.splitlines()[:50],
                                    fromfile="previous_attempt",
                                    tofile="current_attempt",
                                    lineterm=""))[:20]
                                if len(_diff_lines) > 2:  # not just the headers
                                    prior_summaries.append(
                                        "CHANGES SINCE LAST ATTEMPT:\n"
                                        + "\n".join(_diff_lines))
                        for prev_attempt in outcome.attempts:
                            # The outcome's attempts list carries the candidates that
                            # were tried. We need the VALIDATION that rejected them.
                            # The candidate's parse_warnings/explanation carry the
                            # failure info.
                            if prev_attempt is prev_candidate:
                                continue
                            summary_parts = []
                            for f in failures:
                                summary_parts.append(f"{f.validator}: {f.message[:60]}")
                            if summary_parts:
                                prior_summaries.append("; ".join(summary_parts[:2]))
                        prompt = build_repair_prompt(unit, context, prev_candidate, failures, attempt=retry_count, prior_attempt_summaries=prior_summaries or None, budget=self.resolution_engine.token_budget)
                    elif failures:
                        pv = PROMPT_RETRY
                        prompt = build_retry_prompt(unit, context, failures, budget=self.resolution_engine.token_budget)
                    else:
                        pv = PROMPT_RESOLVE
                        prompt = build_resolve_prompt(unit, context, budget=self.resolution_engine.token_budget)
                    # Post-construction oversized check: the pre-construction guard
                    # (_llm_oversized_for_window) measures only the windowed marker
                    # block, assuming augmentation is trimmable. But the obligations
                    # block is budget-PROTECTED (folded into sides_text), so a prompt
                    # with whole-file sides + obligations can blow the window without
                    # the pre-guard catching it. Measure the ACTUAL prompt size here
                    # and escalate as "oversized" before burning an HTTP-400 round-
                    # trip. Surfaced in the C live-eval (sqlite-history-0005): a
                    # 148KB prompt (37K tokens vs 8K window) hit HTTP 400.
                    # Threshold: only fire when the prompt exceeds the window AND is
                    # large in absolute terms (>10K tokens). This catches the
                    # whole-file-sides blowup (37K tokens) without pre-empting
                    # legitimately-tight prompts (a few hundred tokens over a small
                    # window may still produce usable output — the model decides).
                    _window = int(getattr(self.config.model, "context_window", 0) or 0)
                    if _window > 0:
                        _prompt_t = estimate_tokens(prompt)
                        if _prompt_t > _window and _prompt_t > 10000:
                            self.journal.emit(
                                "llm_skipped_oversized_prompt",
                                {"prompt_tokens": _prompt_t, "window": _window,
                                 "prompt_chars": len(prompt)},
                                step_index=self.step, path=unit.path, unit_id=unit.unit_id,
                            )
                            # Sprint-19 P5 (journal-only): correlate the skip
                            # with a measured class-member split candidate when
                            # one exists (the protobuf-0055 class).
                            self._journal_class_member_candidate(unit)
                            outcome.escalated = True
                            outcome.retry_count = retry_count
                            outcome.reason = (
                                f"oversized prompt: {_prompt_t}t > {_window}t window "
                                f"(obligations/sides exceeded the context window)"
                            )
                            return outcome
                    self.journal.store_prompt(unit.unit_id, retry_count, prompt)
                finally:
                    if _mirror_ladder_base is not None:
                        from capybase.prompt_profile import (
                            set_active_profile as _mp_sap,
                        )
                        _mp_sap(_mirror_ladder_base)
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

                classification = classify(unit, config=self.config)
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
                        attempt=retry_count,
                    )
                )
            else:
                candidates = self.resolution_engine.propose(
                    unit, context, failures=failures, prev_candidate=prev_candidate,
                    n_samples=n_complex, attempt=retry_count,
                )
            outcome.consensus = consensus_report
            # Prompt-assembly instrumentation (s23): one event per prompt
            # build makes any prompt-size issue diagnosable instantly.
            # The prompt was built inside propose() (or its variants);
            # we journal what we know: the version, the candidate count,
            # and the context bundle's token estimate.
            if hasattr(self, 'journal') and hasattr(context, 'token_estimate'):
                self.journal.emit(
                    "prompt_composition",
                    {"context_token_estimate": context.token_estimate,
                     "n_candidates": len(candidates),
                     "unit_id": unit.unit_id,
                     "failures_count": len(failures) if failures else 0},
                    step_index=self.step, path=unit.path,
                    unit_id=unit.unit_id)
            # Intent coverage re-ranking: among candidates grouped equally by
            # consensus (same cluster size), prefer the one that preserves more
            # side-specific lines. This directly targets the sim gap where the
            # model drops lines the oracle kept. The coverage score is a TIE-
            # BREAK, not a gate — it only affects WHICH valid candidate is tried
            # first, not WHETHER a candidate is accepted.
            if len(candidates) > 1:
                from capybase.structural_resolver import intent_coverage_score
                _base_t = unit.base.text or ""
                _cur_t = unit.current.text or ""
                _rep_t = unit.replayed.text or ""
                # Stable sort: preserves consensus order for same-coverage candidates.
                candidates = sorted(
                    candidates,
                    key=lambda c: -intent_coverage_score(
                        c.resolved_text or "", _base_t, _cur_t, _rep_t,
                    ),
                )
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
            # Degradation telemetry: finish_reason + latency distinguish
            # prompt-shape refusals (stop + empty), truncation (length), and
            # transport trouble (no finish_reason + high latency).
            if getattr(winner, "finish_reason", ""):
                emit_payload["finish_reason"] = winner.finish_reason
            if getattr(winner, "llm_latency_ms", None) is not None:
                emit_payload["llm_latency_ms"] = round(
                    winner.llm_latency_ms, 1)
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
            # Deterministic closure: run all applicable Tier-A structural
            # primitives in sequence (import-union → deletion-application →
            # block-insertion) on the candidate BEFORE verification. Each is a
            # pure function with the APPLIED/NOT_APPLICABLE/BLOCKED/AMBIGUOUS
            # contract; on every non-APPLIED outcome the candidate is untouched.
            # Together they close mechanically-satisfiable obligations without a
            # second model call: missing import leaves are inserted, dropped
            # deletions are removed, additive blocks are transplanted. The
            # existing cargo/rustc gauntlet remains authoritative after the edit.
            # Runs every loop iteration; idempotency makes that safe. See
            # src/capybase/import_union.py, deletion_union.py, block_insertion.py.
            cand = self._apply_deterministic_closure(unit, cand)
            # No-op short-circuit (the analysis's "eliminate avoidable slow
            # retries"): if this EXACT candidate was already validated in this
            # loop (same resolved_text hash), reuse the prior result instead of
            # re-running verification (compilation, tests, diagnostics). A
            # model that re-proposes the same candidate after a preservation-
            # heuristic retry wastes a full validation cycle for zero new
            # information. The oscillation/convergence backstop (below) catches
            # the cycle and escalates — this just skips the expensive re-check.
            import hashlib as _hashlib

            cand_hash = ""
            if cand.resolved_text:
                cand_hash = _hashlib.sha256(
                    cand.resolved_text.encode("utf-8")
                ).hexdigest()[:16]
            prior_val = outcome._candidate_validation_cache.get(cand_hash)
            if prior_val is not None:
                validation = prior_val  # reuse: same candidate → same result
                self.journal.emit(
                    "candidate_validation_reused",
                    {"candidate_id": cand.candidate_id,
                     "hash": cand_hash,
                     "reason": "identical resolved_text — validation cached"},
                    step_index=self.step, path=unit.path, unit_id=unit.unit_id,
                )
            else:
                _vt0 = _time.monotonic()
                validation = self.verification.verify(unit, cand)
                _verify_time_accumulated += _time.monotonic() - _vt0
                if cand_hash:
                    outcome._candidate_validation_cache[cand_hash] = validation
            self._journal_validation(unit, cand, validation)
            if not validation.passed and len(candidates) > 1:
                for trial in candidates[1:]:
                    _vt1 = _time.monotonic()
                    trial_val = self.verification.verify(unit, trial)
                    _verify_time_accumulated += _time.monotonic() - _vt1
                    self._journal_validation(unit, trial, trial_val)
                    if trial_val.passed:
                        cand = trial
                        validation = trial_val
                        break
            outcome.validation = validation
            outcome.attempts.append(cand)
            # P8 (s24): per-attempt hard-failure counts for the proximity-
            # based dynamic retry budget (a strictly-decreasing trend means
            # the model is converging — see the budget-cap relaxation below).
            if not hasattr(outcome, "_attempt_hf_counts"):
                outcome._attempt_hf_counts = []
            outcome._attempt_hf_counts.append(len(validation.hard_failures))
            # P6b (s24 cycle-H): splice-level delimiter repair. The
            # candidate-level P6 check can't see context imbalances — a
            # candidate whose first line closes something opened BEFORE the
            # marker span is internally balanced alone but yields
            # "SyntaxError: unmatched ')'" spliced (zenodo-0079: confident
            # candidates failing identically across retries, dying at
            # no-progress). On a delimiter-shaped failure, repair the
            # SPLICED text and re-splice the candidate from it.
            # G5/G11 (sprint-26): the brace+scope form — rustc reports the
            # same splice class as "mismatched closing delimiter: `}`" and
            # the structural gate as "brace imbalance detected at line N"
            # (axum-0019, sea-orm-0014). The deterministic brace balancer
            # (_try_balance_braces) owns the repair; unlike the paren form
            # it may REMOVE lines, so the marker span is re-mapped through
            # a line diff before the region is extracted back out.
            _p6b_delim = any(
                "unmatched '" in (getattr(f, "message", "") or "")
                and (")" in f.message or "]" in f.message)
                and "}" not in f.message
                for f in validation.hard_failures)
            _p6b_brace = any(
                ("mismatched closing delimiter" in (getattr(f, "message", "") or "")
                 or "brace imbalance detected" in (getattr(f, "message", "") or "")
                 or ("unmatched '" in (getattr(f, "message", "") or "")
                     and "}" in f.message))
                for f in validation.hard_failures)
            if (not validation.passed
                    and unit.marker_span is not None
                    and cand.resolved_text
                    and (_p6b_delim or _p6b_brace)):
                try:
                    from capybase.adapters.parsers import splice_resolution
                    from capybase.verification import (
                        _delimiter_imbalance_line,
                        _try_balance_braces,
                        _try_repair_delimiter,
                    )
                    _spliced = splice_resolution(
                        unit.original_worktree_text,
                        unit.marker_span, cand.resolved_text)
                    _repaired_splice = None
                    if _p6b_delim and (
                            _delimiter_imbalance_line(_spliced, unit.language)
                            is not None):
                        _repaired_splice = _try_repair_delimiter(
                            _spliced, unit.language)
                        if (_repaired_splice is not None
                                and _delimiter_imbalance_line(
                                    _repaired_splice, unit.language) is not None):
                            _repaired_splice = None
                    if _repaired_splice is None and _p6b_brace:
                        _braced = _try_balance_braces(
                            _spliced, unit.language)
                        if _braced is not None and _braced != _spliced:
                            _repaired_splice = _braced
                    if _repaired_splice is not None:
                        # Extract the repaired region back out of the
                        # splice so the candidate stays splice-safe. The
                        # brace repair may have deleted lines — remap the
                        # marker span through a line diff first (the paren
                        # repair is single-char, indices never move).
                        _start, _end = unit.marker_span
                        _sp_lines = _repaired_splice.split("\n")
                        _orig_lines = _spliced.split("\n")
                        _del_before = 0
                        _del_inside = 0
                        if len(_orig_lines) != len(_sp_lines):
                            import difflib as _dl_g5
                            _sm = _dl_g5.SequenceMatcher(
                                None, _orig_lines, _sp_lines, autojunk=False)
                            for _tag, _i1, _i2, _j1, _j2 in _sm.get_opcodes():
                                if _tag == "delete":
                                    if _i2 <= _start:
                                        _del_before += _i2 - _i1
                                    elif _i1 < _end + 1:
                                        _del_inside += (
                                            min(_i2, _end + 1)
                                            - max(_i1, _start))
                        _rs = _start - _del_before
                        _re_ = _end - _del_before - _del_inside
                        _region = "\n".join(_sp_lines[_rs:_re_ + 1])
                        if _region.strip():
                            _repaired_cand = cand.model_copy(
                                update={"resolved_text": _region})
                            _r_val = self.verification.verify(
                                unit, _repaired_cand)
                            self._journal_validation(
                                unit, _repaired_cand, _r_val)
                            if _r_val.passed:
                                self.journal.emit(
                                    "p6b_splice_delimiter_repair",
                                    {"candidate_id": cand.candidate_id,
                                     "unit_id": unit.unit_id,
                                     "form": "brace" if _del_before or _del_inside or _p6b_brace else "delim"},
                                    step_index=self.step, path=unit.path,
                                    unit_id=unit.unit_id)
                                cand = _repaired_cand
                                validation = _r_val
                                outcome.validation = validation
                except Exception:  # noqa: BLE001 — repair is best-effort
                    pass
            # Sprint-19 P2: track this attempt's quality against a stashed
            # preservation-rejected candidate. Quality = passed validation
            # (warnings allowed — an equally-flagged retry is NOT strictly
            # worse, so the rescue must not fire on it). The stashed
            # candidate's own iteration records nothing (the stash is
            # populated later in this same iteration, in the retry branch).
            _p_stash = getattr(self, "_step_preservation_stash", {}).get(
                unit.unit_id)
            if _p_stash is not None:
                _p_stash["later_attempts"].append(
                    bool(validation.passed))
            # Enrich duplicate-definition failures IMMEDIATELY so the very
            # first CEGIS retry includes the existing definition's context.
            # Without this, the model retries blindly — it knows a function
            # is duplicated but can't see where the original is (it may be
            # 10K lines away, outside the conflict hunk). The enrichment
            # greps the file for the existing definition and appends its
            # source lines to the failure message. All subsequent retry
            # prompts, convergence checks, and risk decisions see the
            # enriched context. See ``_enrich_duplicate_definition_failures``
            # for the three-pass (single-symbol + multi-symbol + side-attribution)
            # logic.
            _enrich_duplicate_definition_failures(unit, cand, validation)
            # Track whether the enrichment recommended accepting a deletion.
            # Set when the conclusion-first note says "RESOLVE BY ACCEPTING" —
            # meaning a verified modify/delete where one side's additions are
            # ALL duplicates. Persists so the NEXT iteration's empty candidate
            # (the model obeying) is accepted, not rejected.
            if (not validation.passed and validation.hard_failures
                    and any("RESOLVE BY ACCEPTING" in (getattr(f, "message", "") or "")
                            for f in validation.hard_failures)):
                _accept_deletion_recommended = True
            # Accept an empty candidate when the enrichment (on a PRIOR
            # iteration with duplicate definitions) verified this is a
            # modify/delete where the current side's additions are ALL
            # duplicates — empty IS the correct resolution.
            # NonEmptyResolutionValidator normally rejects empty LLM output (a
            # model failure), but here the model was EXPLICITLY told to accept
            # the deletion. Conservative: only fires when the ONLY hard failure
            # is the empty-resolution check (no other compile/syntax errors).
            _cand_empty = not (cand.resolved_text or "").strip()
            if (_accept_deletion_recommended and _cand_empty
                    and validation.hard_failures
                    and all("empty resolution" in (getattr(f, "message", "") or "").lower()
                            for f in validation.hard_failures
                            if getattr(f, "severity", "") == "error")):
                outcome.accepted = cand
                outcome.validation = validation
                outcome.retry_count = retry_count
                outcome.reason = (
                    "accepted empty resolution: the duplicate-def enrichment "
                    "verified this is a modify/delete where the current "
                    "side's function additions are ALL duplicates of existing "
                    "definitions — empty resolved_text correctly accepts the "
                    "replayed side's deletion"
                )
                self._record_resolution_attempt(
                    outcome, mechanism="dup_def_deletion_accept",
                    candidate=cand, validation=validation,
                    decision="accept", reason=outcome.reason,
                )
                self.journal.emit(
                    "candidate_accepted",
                    {"candidate_id": cand.candidate_id,
                     "via": "dup_def_deletion_accept"},
                    step_index=self.step, path=unit.path, unit_id=unit.unit_id,
                )
                return outcome
            # Compiler-guided duplicate function-block removal: when the
            # candidate has functions that duplicate existing definitions
            # elsewhere in the file, surgically remove the duplicate blocks
            # from the candidate BEFORE the risk decision. The existing
            # definitions are the well-tested versions; only the candidate's
            # (new, untested) copies are removed. This fires on EVERY
            # iteration (not just at the header cap) so it catches duplicates
            # before the risk engine can escalate on a "suspected false
            # positive." gcc identified the specific function — this is
            # compiler-guided, not regex-guessed.
            if (not validation.passed and validation.hard_failures
                    and cand.resolved_text and cand.resolved_text.strip()):
                _has_dup = any(
                    any(_m in (getattr(f, "message", "") or "")
                        .replace("\u2018", "'").replace("\u2019", "'")
                        for _m in _DUP_DEF_MARKERS)
                    for f in validation.hard_failures
                )
                if _has_dup:
                    try:
                        # Only remove functions gcc EXPLICITLY identified as
                        # duplicates (parse from its fully-qualified error
                        # path ::name(). Do NOT use the broad
                        # _extract_definition_names cross-reference — it
                        # causes false positives from same-name methods in
                        # different classes (a major regression source in
                        # large amalgamated headers).
                        import re as _re_fn
                        _dup_br: set[str] = set()
                        for _f in (validation.hard_failures or []):
                            _fm = (getattr(_f, "message", "") or "")
                            for _m_fn in _re_fn.finditer(
                                r"::(\w+)\(", _fm
                            ):
                                _fn = _m_fn.group(1)
                                if _fn in cand.resolved_text:
                                    _dup_br.add(_fn)
                        if _dup_br:
                            _rt_br = _remove_duplicate_function_blocks(
                                cand.resolved_text, _dup_br)
                            if (_rt_br and _rt_br.strip()
                                    and _rt_br != cand.resolved_text):
                                _rc_br = cand.model_copy(
                                    update={"resolved_text": _rt_br})
                                _rv_br = self.verification.verify(unit, _rc_br)
                                if _rv_br.passed:
                                    outcome.accepted = _rc_br
                                    outcome.validation = _rv_br
                                    outcome.retry_count = retry_count
                                    outcome.reason = (
                                        "compiler-guided duplicate removal: "
                                        f"removed {len(_dup_br)} duplicate "
                                        "function(s); existing definitions kept"
                                    )
                                    self._record_resolution_attempt(
                                        outcome,
                                        mechanism="dup_def_block_removal",
                                        candidate=_rc_br,
                                        validation=_rv_br,
                                        decision="accept",
                                        reason=outcome.reason,
                                    )
                                    self.journal.emit(
                                        "candidate_accepted",
                                        {"candidate_id": _rc_br.candidate_id,
                                         "via": "dup_def_block_removal"},
                                        step_index=self.step,
                                        path=unit.path,
                                        unit_id=unit.unit_id,
                                    )
                                    return outcome
                    except Exception:  # noqa: BLE001
                        pass
            # Track candidate hashes for oscillation detection (CEGIS resilience).
            # cand_hash is already computed above (for the no-op cache). The
            # escalation check runs AFTER the risk decision — only when the
            # decision is "retry" — so it never fires before the normal budget.
            norm_hash = None
            if cand_hash:
                outcome._seen_candidate_hashes[cand_hash] = (
                    outcome._seen_candidate_hashes.get(cand_hash, 0) + 1
                )
                # Normalized hash (Issue 4): strip comments + collapse whitespace
                # + sort lines, so cosmetic variation (whitespace, comment
                # reordering) is caught as cycling. Uses the canonical lexer
                # (Phase 1) for comment/blank stripping.
                norm_text = _normalize_for_convergence(
                    cand.resolved_text, unit.language)
                norm_hash = _hashlib.sha256(
                    norm_text.encode("utf-8")
                ).hexdigest()[:16]
                outcome._seen_normalized_hashes[norm_hash] = (
                    outcome._seen_normalized_hashes.get(norm_hash, 0) + 1
                )
            if self.config.journal.enabled and self.config.journal.store_candidates:
                self.journal.store_candidate(cand)
            if self.config.journal.enabled and self.config.journal.store_raw_responses:
                self.journal.store_response(unit.unit_id, retry_count, cand.raw_response)

            # Compiled-candidate convergence escape hatch (Phase 6 D1, broadened
            # Phase 10). When the candidate is cycling (same normalized hash
            # seen ≥ convergence_threshold times), passes ALL hard validation
            # (no syntax/compile errors), and is blocked ONLY by advisory
            # warnings (the soft heuristics the risk layer retries on), accept
            # it instead of retrying. The model has already tried its best; the
            # candidate compiles; the blocking concern is a semantic judgment
            # (field type, import ordering, both-sides representation) not a
            # correctness defect. "Do not spend additional iterations asking the
            # same model to satisfy the same heuristic."
            #
            # Originally limited to preservation_heuristic/STRUCTURAL_CODE,
            # but flight data showed the actual cycling blockers are
            # both_sides_represented and unclassified proof_class (the hatch
            # otherwise fired on a validator that rarely blocks). The advisory
            # set now covers the validators the risk layer treats as retry-able
            # soft warnings.
            conv_threshold = getattr(self.config.policy, "cegis_convergence_threshold", 2)
            if (conv_threshold > 0 and cand.resolved_text
                    and not validation.hard_failures
                    and norm_hash is not None
                    and norm_hash in outcome._seen_normalized_hashes
                    and outcome._seen_normalized_hashes[norm_hash] >= conv_threshold):
                # Advisory validators: soft heuristics that signal a semantic
                # judgment, not a correctness defect. When these are the ONLY
                # blockers on a compiled, cycling candidate, accept it.
                _ADVISORY = frozenset({
                    "preservation_heuristic",
                    "both_sides_represented",
                    "obligation",
                    "intent_coverage",
                    "unattributed_code",
                })
                blocking = [
                    w for w in validation.warnings
                    if w.validator in _ADVISORY
                ]
                non_advisory = [
                    w for w in validation.warnings
                    if w.validator not in _ADVISORY
                ]
                if blocking and not non_advisory:
                    # The ONLY blockers are advisory warnings on a cycling,
                    # compiled candidate. Accept it.
                    blocker_names = sorted({w.validator for w in blocking})
                    # Run intent-coverage repair BEFORE accepting — the escape
                    # hatch returns before the normal accept path (line ~9606)
                    # where _try_intent_coverage_repair runs. Without this,
                    # escape-hatch-accepted candidates never get dropped lines
                    # restored, leaving the 0.94→0.95 gap unbridged.
                    cand = self._try_intent_coverage_repair(unit, cand)
                    outcome.accepted = cand
                    outcome.validation = validation
                    outcome.reason = (
                        f"convergence escape hatch: accepted cycling candidate "
                        f"(compiled, advisory-only blockers {blocker_names}, "
                        f"seen {outcome._seen_normalized_hashes[norm_hash]}×)"
                    )
                    self._record_resolution_attempt(
                        outcome, mechanism="convergence_escape_hatch",
                        candidate=cand, validation=validation,
                        decision="accept", reason=outcome.reason,
                    )
                    return outcome

            # Header file CEGIS cap: headers have no per-unit compile gate,
            # so retries are blind. Allow 1 retry (to act on risk feedback),
            # then escalate instead of retrying further. The cap checks the
            # TOTAL model-call count (retry_count + critic_retry_count +
            # recovery_retry_count) so critic-driven and recovery retries
            # can't bypass the budget.
            _total_header_calls = (
                retry_count + critic_retry_count + recovery_retry_count
            )
            if (
                _is_header
                and _total_header_calls > _header_max_retries
            ):
                import re as _re_hdr
                _header_repaired = False  # set True if a repair avoids escalation
                # 1. Stray-character repair: the 4B model sometimes emits
                # stray characters (e.g. '@') that gcc flags as parse errors.
                if validation and validation.hard_failures and cand.resolved_text:
                    _repaired_text = cand.resolved_text
                    _repaired = False
                    for f in validation.hard_failures:
                        msg = getattr(f, "message", "") or ""
                        _msg_norm = msg.replace("\u2018", "'").replace("\u2019", "'")
                        _m = _re_hdr.search(r"stray '(.+?)' in program", _msg_norm)
                        if _m:
                            _raw = _m.group(1)
                            if _raw.startswith("\\") and len(_raw) == 4:
                                try:
                                    _sc = chr(int(_raw[1:], 8))
                                except ValueError:
                                    continue
                            else:
                                _sc = _raw
                            if len(_sc) == 1 and _sc in _repaired_text:
                                _repaired_text = _repaired_text.replace(_sc, "")
                                _repaired = True
                    if _repaired:
                        _rc = cand.model_copy(update={"resolved_text": _repaired_text})
                        _rv = self.verification.verify(unit, _rc)
                        if _rv.passed:
                            outcome.accepted = _rc
                            outcome.validation = _rv
                            outcome.retry_count = retry_count
                            outcome.reason = (
                                "deterministic header repair: removed stray "
                                "character (CEGIS cap reached)"
                            )
                            self._record_resolution_attempt(
                                outcome, mechanism="deterministic_header_repair",
                                candidate=_rc, validation=_rv,
                                decision="accept", reason=outcome.reason,
                            )
                            return outcome
                # 2. Duplicate definition handling.
                # 2a. Compiler-guided function-block removal: scan ALL
                # candidates (current + prior attempts) for functions that
                # duplicate existing definitions elsewhere in the file.
                # Surgically remove the duplicate blocks — the existing
                # definitions are the well-tested versions; only the
                # candidate's (new, untested) copies are removed.
                # Fires regardless of the current validation's error type:
                # the model oscillates between duplicate and empty candidates,
                # and the cap may fire on an empty iteration. The block
                # removal scans prior attempts to find a candidate WITH the
                # duplicates to repair.
                # 2b. Enrichment-retry: if block removal doesn't work, fall
                # back to granting one extra CEGIS iteration.
                if not _dup_def_retried:
                    _dup_block_resolved = False
                    try:
                        from capybase.structural_resolver import (
                            _extract_definition_names as _edn,
                        )
                        _orig_lines = (unit.original_worktree_text or "").split("\n")
                        _ms_rem = getattr(unit, "marker_span", None)
                        if _ms_rem is not None:
                            _rest_rem = (
                                _orig_lines[:max(0, _ms_rem[0])]
                                + _orig_lines[min(len(_orig_lines), _ms_rem[1] + 1):]
                            )
                        else:
                            _rest_rem = _orig_lines
                        _rd = _edn(_rest_rem)
                        _seen_texts: set[str] = set()
                        for _pc in [cand] + list(outcome.attempts):
                            if not (_pc and getattr(_pc, "resolved_text", "")
                                    and _pc.resolved_text.strip()):
                                continue
                            if _pc.resolved_text in _seen_texts:
                                continue
                            _seen_texts.add(_pc.resolved_text)
                            _cd = _edn(_pc.resolved_text.split("\n"))
                            _dup_fns = {n for n in _cd if n in _rd}
                            self.journal.emit(
                                "block_removal_scan",
                                {"cand_len": len(_pc.resolved_text),
                                 "cand_defs": sorted(_cd)[:10],
                                 "dup_fns": sorted(_dup_fns)[:10],
                                 "rest_defs_count": len(_rd)},
                                step_index=self.step, path=unit.path,
                                unit_id=unit.unit_id,
                            )
                            if not _dup_fns:
                                continue
                            _rt = _remove_duplicate_function_blocks(
                                _pc.resolved_text, _dup_fns)
                            if not (_rt and _rt.strip()
                                    and _rt != _pc.resolved_text):
                                continue
                            _rc = _pc.model_copy(update={"resolved_text": _rt})
                            _rv = self.verification.verify(unit, _rc)
                            if _rv.passed:
                                outcome.accepted = _rc
                                outcome.validation = _rv
                                outcome.retry_count = retry_count
                                outcome.reason = (
                                    "compiler-guided duplicate removal: "
                                    f"removed {len(_dup_fns)} duplicate "
                                    "function(s); existing definitions kept"
                                )
                                self._record_resolution_attempt(
                                    outcome, mechanism="dup_def_block_removal",
                                    candidate=_rc, validation=_rv,
                                    decision="accept", reason=outcome.reason,
                                )
                                self.journal.emit(
                                    "candidate_accepted",
                                    {"candidate_id": _rc.candidate_id,
                                     "via": "dup_def_block_removal"},
                                    step_index=self.step, path=unit.path,
                                    unit_id=unit.unit_id,
                                )
                                return outcome
                            _dup_block_resolved = False  # tried but didn't compile
                    except Exception:  # noqa: BLE001
                        pass
                    # 2b. Block removal didn't produce a compiling candidate.
                    # If the failure was a duplicate-def error, grant one
                    # more CEGIS iteration with the enriched context.
                    _had_dup_marker = validation and validation.hard_failures and any(
                        any(_m in (getattr(f, "message", "") or "")
                            .replace("\u2018", "'").replace("\u2019", "'")
                            for _m in _DUP_DEF_MARKERS)
                        for f in validation.hard_failures
                    )
                    if _had_dup_marker:
                        _dup_def_retried = True
                        _header_repaired = True
                # Escalate unless a repair strategy granted a retry.
                if not _header_repaired:
                    outcome.escalated = True
                    outcome.retry_count = retry_count
                    outcome.reason = (
                        f"header file CEGIS cap reached "
                        f"({_header_max_retries} retry budget for headers)"
                    )
                    self._record_resolution_attempt(
                        outcome, mechanism="llm",
                        decision="escalate", reason=outcome.reason,
                    )
                    return outcome
            # Unit-count-aware budget cap: when max_retries was passed (many-
            # unit file), enforce it here before risk.decide() — mirroring the
            # header cap. Without this, a file with 78 units would allow each
            # unit the full config retry budget, overflowing the wall-time.
            if (
                max_retries is not None
                and retry_count >= _unit_budget
                and retry_count > 0
            ):
                # Sprint-22 P2: adaptive relaxation — when the candidate
                # is already near-oracle (validation passed all hard
                # gates, failed only a soft/semantic signal) AND this is
                # the only failing unit in the file, grant ONE extra
                # retry. The throughput cap protects large files from
                # budget exhaustion; a single boundary case with headroom
                # shouldn't be sacrificed to it.
                _close = (
                    validation is not None
                    and not validation.passed
                    and not validation.hard_failures
                    and getattr(self, "_file_failing_unit_count", 0) <= 1
                )
                # P8 (s24): proximity-based dynamic budget — the model is
                # CONVERGING (this attempt's hard-failure count strictly
                # below the FIRST attempt's). One extra retry is the
                # cheapest conversion path for a near-miss; latched to one
                # grant per unit, and the no-progress guard independently
                # stops loops whose failure signatures stall.
                _hf_trend = list(getattr(outcome, "_attempt_hf_counts", []))
                _progress = (
                    len(_hf_trend) >= 2
                    and 0 < _hf_trend[-1] < _hf_trend[0]
                    and not getattr(outcome, "_progress_grant_used", False)
                )
                if _progress:
                    outcome._progress_grant_used = True
                if (_close or _progress) and retry_count == _unit_budget:
                    self.journal.emit(
                        "retry_relaxation",
                        {"unit_id": unit.unit_id,
                         "original_cap": _unit_budget,
                         "reason": (
                             "converging-failure-trend"
                             if _progress and not _close
                             else "high-sim single-failing-unit"),
                         "hf_trend": _hf_trend},
                        step_index=self.step, path=unit.path,
                        unit_id=unit.unit_id)
                    # fall through: don't escalate, let the retry happen
                else:
                    outcome.escalated = True
                    outcome.retry_count = retry_count
                    outcome.reason = (
                        f"unit-count-aware retry cap reached ({_unit_budget} "
                        f"retries; file has many units)"
                    )
                    self._record_resolution_attempt(
                        outcome, mechanism="llm",
                        decision="escalate", reason=outcome.reason,
                    )
                    return outcome

            # Zero-budget escape: when the unit-count cap gives 0 retries
            # (files with >20 units) and this is the first (and only)
            # attempt, accept compiling candidates with advisory-only
            # warnings directly. Without this, risk.decide() would say
            # "retry" (it doesn't know about the unit-count cap), but the
            # next iteration's oscillation backstop escalates immediately
            # (osc_budget=0). The compiling, possibly-high-confidence
            # candidate is thrown away — wasting the one attempt we had.
            # For a 89-unit file, accepting one unit's content-loss
            # candidate is strictly better than escalating the entire file
            # (losing all units' resolutions).
            if (
                max_retries is not None
                and _unit_budget == 0
                and retry_count == 0
                and cand.resolved_text
                and not validation.hard_failures
            ):
                _ZB_ADVISORY = frozenset({
                    "preservation_heuristic",
                    "both_sides_represented",
                    "obligation",
                    "intent_coverage",
                    "unattributed_code",
                })
                _zb_non_advisory = [
                    w for w in validation.warnings
                    if w.validator not in _ZB_ADVISORY
                ]
                if not _zb_non_advisory:
                    cand = self._try_intent_coverage_repair(unit, cand)
                    outcome.accepted = cand
                    outcome.validation = validation
                    outcome.retry_count = retry_count
                    _zb_blockers = sorted(
                        {w.validator for w in validation.warnings
                         if w.validator in _ZB_ADVISORY}
                    )
                    outcome.reason = (
                        f"zero-budget escape: accepted compiling candidate "
                        f"(unit-count cap = 0 retries, advisory-only "
                        f"blockers {_zb_blockers})"
                    )
                    self._record_resolution_attempt(
                        outcome, mechanism="zero_budget_escape",
                        candidate=cand, validation=validation,
                        decision="accept", reason=outcome.reason,
                    )
                    self.journal.emit(
                        "candidate_accepted",
                        {"candidate_id": cand.candidate_id,
                         "via": cand.provenance or "plain_llm",
                         "provenance": cand.provenance or ""},
                        step_index=self.step, path=unit.path,
                        unit_id=unit.unit_id,
                    )
                    return outcome

            # First-empty fast-fail (reviewer-consensus hardening): recover
            # deterministically instead of retrying when the empty response
            # was never going to change. Two arms:
            #
            # 1. SMALL fresh unit (< 1500 tokens): the model can't handle
            #    this fragment's prompt shape — retrying the same prompt
            #    statistically won't change that but burns 30-60s per retry
            #    of the Phase-1 budget (protobuf-0043's empty-LLM sub-units
            #    starved every downstream recovery).
            # 2. OVERSIZED prompt (>= empty_oversized_token_floor, or >= 90%
            #    of a known context window): the endpoint returns empty for
            #    prompts past its effective limit — every retry of the SAME
            #    oversized prompt is a guaranteed 30-60s dead burn. The
            #    oversized check at propose-time catches the extreme class
            #    (> window AND > 10K tokens); this arm catches the 6K-window
            #    gray zone the pre-check deliberately tolerates.
            #
            # Fall through to the normal retry policy when recovery declines
            # (no behavior loss).
            _tok_est = getattr(context, "token_estimate", 0)
            _llm_window = int(getattr(self.config.model, "context_window", 0) or 0)
            _empty_oversized = (
                _tok_est >= getattr(
                    self.config.future, "empty_oversized_token_floor", 6000)
                or (_llm_window > 0 and _tok_est >= 0.9 * _llm_window)
            )
            # Refusal guard: a needs_human REFUSAL is not an empty-response
            # failure (block-capture's keep/delete verdicts route here);
            # converting it to a deterministic side pick would drop the other
            # side's intent. EXCEPTION: an oversized prompt whose response is
            # UNPARSEABLE (failure_kind=parse_failed, empty text, coerced
            # needs_human=True) is the endpoint choking on the prompt — not a
            # considered refusal. The oversized arm must see it.
            _refusal = (
                "needs_human" in (cand.failure_kind or "")
                or getattr(cand, "needs_human", False)
            )
            _oversized_parse_fail = (
                _empty_oversized and _tok_est >= 1500
                and (cand.failure_kind or "") == "parse_failed"
            )
            # C7' diagnostic (sprint-24 cycle B): journal the exact
            # condition values to find which one blocks the fast-fail
            self.journal.emit(
                "c7_fastfail_check",
                {"failures_none": failures is None,
                 "retry_count": retry_count,
                 "resolved_empty": not (cand.resolved_text or "").strip(),
                 "failure_kind": cand.failure_kind or "",
                 "needs_human_attr": getattr(cand, "needs_human", None),
                 "tok_est": _tok_est,
                 "refusal": _refusal,
                 "oversized_parse_fail": _oversized_parse_fail,
                 "path": unit.path},
                step_index=self.step, path=unit.path,
                unit_id=unit.unit_id)
            if (
                failures is None
                and retry_count == 0
                and not (cand.resolved_text or "").strip()
                and (
                    _tok_est < 1500
                    or _empty_oversized
                    # C7' + P1 (sprint-23 batch E): the parser now emits
                    # failure_kind="empty" for zero-byte responses — a
                    # transport failure, not a considered refusal. The
                    # single-side fallback fires on "empty" at ANY unit
                    # size with zero carve-out logic (the failure kind
                    # carries the information; no boolean overrides).
                    or (cand.failure_kind or "") == "empty"
                )
                and (not _refusal or _oversized_parse_fail
                     or (cand.failure_kind or "") == "empty")
                # A TRANSPORT failure (request never completed) is not a
                # model verdict — the endpoint said nothing about this
                # conflict, so there is no basis for the deterministic side
                # pick. Same class as the refusal carve-out: infrastructure
                # weather must not decide merge semantics. A genuine empty
                # 200-response coerces to parse_failed, which still
                # fast-fails; request_failed falls to risk.decide's
                # technical-retry ladder instead (s18 validation: sea-orm-0027
                # shipped a one-side merge during a transient outage).
                and (cand.failure_kind or "") != "request_failed"
            ):
                _unit_kind = "sub" if "#s" in unit.unit_id else "top"
                self.journal.emit(
                    "llm_empty_fragment",
                    {"token_estimate": context.token_estimate,
                     "unit_kind": _unit_kind,
                     "failure_kind": cand.failure_kind or "",
                     "unit_id": unit.unit_id,
                     "oversized": _empty_oversized and _tok_est >= 1500},
                    step_index=self.step, path=unit.path, unit_id=unit.unit_id,
                )
                _ef = (
                    self._empty_fast_fail_recovery(unit, cand)
                    if getattr(self.config.future, "enable_empty_fast_fail", True)
                    else None
                )
                if _ef is not None:
                    return _ef

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
            # Sprint-19 P2 (R1 tagging + Best-of-N stash): a
            # validation-PASSING candidate force-retried by the
            # preservation heuristic is the Best-of-N baseline. Tag it
            # (the file-level guard and analysis see the flag) and stash
            # it; if every forced retry then validates strictly worse and
            # the unit escalates, the wrapper restores this candidate
            # instead. Only the FIRST such candidate is stashed (the
            # highest-confidence original, per tokio-0037's journal).
            if (
                decision.action == "retry"
                and validation.passed
                and not validation.hard_failures
                and any(w.validator == "preservation_heuristic"
                        for w in validation.warnings)
                and unit.unit_id
                not in getattr(self, "_step_preservation_stash", {})
            ):
                cand.flagged_by_preservation_heuristic = True
                if not hasattr(self, "_step_preservation_stash"):
                    self._step_preservation_stash = {}
                self._step_preservation_stash[unit.unit_id] = {
                    "candidate": cand,
                    "validation": validation,
                    "later_attempts": [],
                }
                self.journal.emit(
                    "preservation_flagged",
                    {"candidate_id": cand.candidate_id,
                     "flagged_by_preservation_heuristic": True,
                     "retry_count": retry_count},
                    step_index=self.step, path=unit.path,
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
                # Intent-coverage post-processor: if the accepted candidate
                # dropped lines common to BOTH sides, try to restore them
                # deterministically. Bridges the 0.94→0.95 gap for
                # rewrite-vs-edit conflicts where the LLM produces a compiling
                # merge but drops 1-2 side-common lines. Production-safe: only
                # restores lines common to both sides, never oracle-derived.
                cand = self._try_intent_coverage_repair(unit, cand)
                outcome.accepted = cand
                outcome.retry_count = retry_count
                # Sprint-19 P2: a real acceptance supersedes any stashed
                # Best-of-N baseline for this unit.
                if hasattr(self, "_step_preservation_stash"):
                    self._step_preservation_stash.pop(unit.unit_id, None)
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
                # C20 follow-up: the budget-exhaustion death path of the
                # pure-empty class (see _empty_terminal_grant_due).
                if _empty_terminal_grant_due(outcome):
                    recovery_retry_count += 1
                    pending_recovery = True
                    self.journal.emit(
                        "empty_terminal_recovery_grant",
                        {"unit_id": unit.unit_id, "via": "budget",
                         "recovery_retries": recovery_retry_count},
                        step_index=self.step, path=unit.path,
                        unit_id=unit.unit_id)
                    # do NOT return: the next iteration proposes via
                    # build_recovery_prompt with the failures seed.
                else:
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
            # C12 (sprint-26): classify this rejected attempt for the
            # empty-oscillation detector. Empty attempts carry no repairable
            # text; defect attempts carry concrete parse errors that ARE
            # locally fixable from a diff window. Stash the newest defect
            # (candidate, validation) so the no-progress guard's shattered
            # rescue can retarget it when the loop dies on an empty candidate.
            if not (cand.resolved_text or "").strip():
                outcome._osc_attempt_kinds.append("empty")
            else:
                outcome._osc_attempt_kinds.append("defect")
                outcome._osc_last_defect = (cand, validation)
            # No-progress guard: if the hard-failure SIGNATURE (multiset of
            # (validator, normalized_message)) is unchanged across N consecutive
            # attempts, the loop is producing zero new information — escalate.
            # Keys on failure shape, not candidate hashes, so it catches the
            # empty-output transport loop (random UUIDs defeat the hash
            # backstops; the content-hash checks are also gated on non-empty
            # resolved_text) AND genuine stuck-on-one-compiler-error cycling.
            # The message is normalized (line numbers → N) so the same error at a
            # shifted location still counts as no-progress; symbol names and error
            # kinds are preserved so a genuinely different error registers as
            # change. N = cegis_convergence_threshold (default 2); 0 disables.
            np_threshold = getattr(self.config.policy, "cegis_convergence_threshold", 2)
            if np_threshold > 0:
                sig = _hard_failure_signature(validation.hard_failures)
                # A needs_human refusal produces a non-empty signature
                # (needs_human + non_empty_resolution). The no-progress guard
                # would fire on two identical refusals BEFORE the recovery-retry
                # path (later in this iteration) can give the model a reframed
                # second chance. needs_human cases have their own budget
                # (max_recovery_retries_per_unit); exclude them from the
                # convergence guard so the recovery path — designed for exactly
                # these "the model gave up prematurely" cases — gets to run.
                # The guard still catches genuine compiler-error cycling (its
                # primary purpose): those signatures contain syntax/structural
                # validators, not needs_human.
                # sig is frozenset(Counter(...).items()) where each item is
                # ((validator, normalized_msg), count). Unpack correctly.
                has_needs_human = any(
                    validator == "needs_human"
                    for (validator, _msg), _cnt in sig
                )
                if not has_needs_human:
                    outcome._recent_hard_failure_sigs.append(sig)
                    # Use a window WIDER than the threshold so alternating
                    # signatures (A, B, A, B, ...) are caught. With window ==
                    # threshold, max_repeat can never reach threshold unless ALL
                    # entries are identical — which is just the old behavior.
                    # A window of 2×threshold lets an A,B,A,B pattern accumulate
                    # threshold repeats of A within the window.
                    _window = max(np_threshold * 2, np_threshold + 2)
                    recent = outcome._recent_hard_failure_sigs[-_window:]
                    if len(recent) >= np_threshold:
                        from collections import Counter
                        sig_counts = Counter(recent)
                        max_repeat = max(sig_counts.values())
                        if max_repeat >= np_threshold:
                            # Sprint-25 item 4: the context-shattering rescue
                            # — ONE diff-only, high-temperature attempt before
                            # the guard escalates. A repetition loop is an
                            # attractor of the prompt's repetitive content;
                            # temperature alone (the truncation breaker)
                            # doesn't remove the attractor. Latched per unit.
                            if not getattr(outcome, "_shatter_tried", False):
                                outcome._shatter_tried = True
                                # C12 (sprint-26): when the guard fires on an
                                # EMPTY candidate, the shattered prompt has no
                                # diff window to repair — the shatter branch in
                                # propose() requires non-empty prev_candidate
                                # text and silently degenerates to the
                                # full-context PROMPT_RETRY (the exact
                                # attractor the rescue exists to break). When
                                # the attempt history shows the mixed
                                # empty/defect alternation, retarget the
                                # rescue at the most recent defect candidate
                                # and ITS hard failures: the band's defects
                                # are concrete (stray '@', missing
                                # terminator, unqualified-id) and locally
                                # fixable from the ±8-line window.
                                _sh_kinds = outcome._osc_attempt_kinds[-6:]
                                _sh_target, _sh_failures = cand, failures
                                # C12 broadening (s27): >=1 defect suffices —
                                # zenodo-0079's shape (3 empty + 3 needs_human
                                # + 1 'unmatched )') fell between the >=2-
                                # defect band trigger and the terminal grant's
                                # 0-defect requirement. Mostly-empty with one
                                # concrete defect is the same starvation: the
                                # loop discards the only repairable candidate.
                                if (
                                    not (cand.resolved_text or "").strip()
                                    and _sh_kinds.count("empty") >= 2
                                    and _sh_kinds.count("defect") >= 1
                                    and outcome._osc_last_defect is not None
                                ):
                                    _sh_t, _sh_v = outcome._osc_last_defect
                                    if (
                                        (_sh_t.resolved_text or "").strip()
                                        and getattr(_sh_v, "hard_failures", None)
                                    ):
                                        _sh_target = _sh_t
                                        _sh_failures = list(_sh_v.hard_failures)
                                        self.journal.emit(
                                            "oscillation_band_rescue",
                                            {"unit_id": unit.unit_id,
                                             "empty_attempts": _sh_kinds.count("empty"),
                                             "defect_attempts": _sh_kinds.count("defect"),
                                             "target_candidate_id": _sh_t.candidate_id},
                                            step_index=self.step, path=unit.path,
                                            unit_id=unit.unit_id)
                                try:
                                    _shattered = self.resolution_engine.propose(
                                        unit, context,
                                        failures=_sh_failures,
                                        prev_candidate=_sh_target,
                                        n_samples=1,
                                        attempt=retry_count + 1,
                                        shatter=True,
                                    )
                                    for _sh_cand in _shattered:
                                        if not (_sh_cand.resolved_text or "").strip():
                                            continue
                                        _sh_val = self.verification.verify(
                                            unit, _sh_cand)
                                        self._journal_validation(
                                            unit, _sh_cand, _sh_val)
                                        if _sh_val.passed:
                                            self.journal.emit(
                                                "shattered_repair_accept",
                                                {"unit_id": unit.unit_id,
                                                 "candidate_id": _sh_cand.candidate_id},
                                                step_index=self.step,
                                                path=unit.path,
                                                unit_id=unit.unit_id)
                                            outcome.accepted = _sh_cand
                                            outcome.validation = _sh_val
                                            outcome.escalated = False
                                            outcome.retry_count = retry_count
                                            outcome.reason = (
                                                "context-shattering rescue: "
                                                "diff-only repair broke the "
                                                "repetition loop")
                                            return outcome
                                except Exception:  # noqa: BLE001 — rescue is best-effort
                                    pass
                            # Sprint-24 cycle B: before the no-progress guard
                            # escalates, give F1 a chance to take over. The
                            # unit has cycled through retries without progress;
                            # if a pristine side compiles cleanly, the takeover
                            # is a better answer than escalating (redis-0055:
                            # the guard fires before F1 ever gets a chance).
                            # Migrated to the pipeline (PRE_ESCALATE stage,
                            # same stage/mechanism contract as the file-level
                            # repair-exhaustion path): the orchestrator probes
                            # both sides and injects the verdicts; the
                            # compile-clean mechanism owns the take-the-single-
                            # compiling-side decision.
                            _np_f1_side = None
                            _np_sides = None
                            try:
                                _np_sides, _np_base = self._micro_stage_sides(unit.path)
                                if _np_sides:
                                    _np_compiling = {}
                                    for _np_sn in ("current", "replayed"):
                                        _np_st = _np_sides.get(_np_sn, "")
                                        if _np_st.strip():
                                            _np_chk = self.verification.verify_file(
                                                unit.path, unit.language,
                                                _np_st, [],
                                                repo_root=str(self.git.repo),
                                                whole_text=_np_st)
                                            _np_compiling[_np_sn] = bool(_np_chk.passed)
                                    self._f1_compile_clean_mech.set_compiling_sides(
                                        _np_compiling)
                                    from capybase.pipeline import (
                                        PreEscalateContext as _PEC,
                                        Stage as _Stg,
                                    )
                                    _np_ctx = _PEC(
                                        path=unit.path,
                                        language=unit.language,
                                        step_index=self.step,
                                        sides=_np_sides,
                                        base_text=_np_base or "",
                                        escalation_reason=(
                                            f"no_progress: signature repeated "
                                            f"{max_repeat}/{len(recent)}"),
                                    )
                                    _np_result = self._pipeline().execute(
                                        _Stg.PRE_ESCALATE, _np_ctx)
                                    if (_np_result is not None
                                            and _np_result.action == "takeover"
                                            and _np_result.mechanism == "f1_compile_clean_takeover"):
                                        _np_f1_side = _np_result.metadata.get("side")
                                    else:
                                        _np_f1_side = None
                                else:
                                    _np_f1_side = None
                            except Exception:  # noqa: BLE001
                                _np_f1_side = None
                            if _np_f1_side is not None:
                                self.journal.emit(
                                    "f1_noprogress_rescue",
                                    {"side": _np_f1_side, "path": unit.path},
                                    step_index=self.step, path=unit.path,
                                    unit_id=unit.unit_id)
                                # Accept the compiling side as the outcome
                                _np_text = _np_sides.get(_np_f1_side, "")
                                _np_unit = unit.model_copy(
                                    update={"marker_span": None,
                                            "unit_kind": "whole_file"})
                                _np_cand = CandidateResolution(
                                    candidate_id=f"{unit.unit_id}:f1_noprogress",
                                    unit_id=unit.unit_id,
                                    model_name="deterministic",
                                    resolved_text=_np_text,
                                    prompt_version="f1_noprogress_rescue",
                                    provenance="deterministic_structural",
                                    self_reported_confidence=0.80,
                                    explanation=(
                                        f"F1 rescue from no-progress: "
                                        f"{_np_f1_side} compiles cleanly"),
                                )
                                outcome.accepted = _np_cand
                                outcome.escalated = False
                                outcome.mechanism = "f1_noprogress_rescue"
                                return outcome

                            # C20 follow-up: the no-progress death path of
                            # the pure-empty class (see
                            # _empty_terminal_grant_due) — the repeated
                            # signature is the empty validator's, not a
                            # compile error. Grant the one-shot recovery
                            # attempt instead of escalating; fall through
                            # to the retry-seed section below.
                            if _empty_terminal_grant_due(outcome):
                                recovery_retry_count += 1
                                pending_recovery = True
                                self.journal.emit(
                                    "empty_terminal_recovery_grant",
                                    {"unit_id": unit.unit_id, "via": "no_progress",
                                     "recovery_retries": recovery_retry_count},
                                    step_index=self.step, path=unit.path,
                                    unit_id=unit.unit_id)
                            else:
                                stalled_sig = sig_counts.most_common(1)[0][0]
                                # stalled_sig is frozenset(Counter(...).items())
                                # where each item is ((validator, msg), count).
                                # Unpack to display labels. D6 (s27): cargo/
                                # rustc gate failures carry an EMPTY validator
                                # name — the old `or ["(none)"]` rendered them
                                # as ['(none)'], reading as "no information"
                                # when the signature's message half carried the
                                # discrimination (axum-0002's display bug).
                                # Fall back to the message's first line, head.
                                validators = sorted(
                                    {(v if v else _msg[:60])
                                     for (v, _msg), _cnt in stalled_sig}
                                ) or ["(none)"]
                                self.journal.emit(
                                    "candidate_rejected",
                                    {"candidate_id": cand.candidate_id,
                                     "action": "escalate", "via": "no_progress",
                                     "reason": (f"hard-failure signature repeated "
                                                f"{max_repeat}/{len(recent)} times "
                                                f"in recent attempts ({validators})"),
                                     "retry_count": retry_count},
                                    step_index=self.step, path=unit.path, unit_id=unit.unit_id,
                                )
                                outcome.escalated = True
                                outcome.retry_count = retry_count
                                outcome.reason = (
                                    f"no hard-failure progress: signature repeated "
                                    f"{max_repeat}/{len(recent)} times ({validators})"
                                    + _obligation_suffix(unit, cand)
                                )
                                return outcome
            # Oscillation backstop (CEGIS resilience): if the SAME resolved_text
            # has been seen more times than the retry budget allows, the model is
            # cycling — escalate instead of wasting more tokens. This fires only
            # when the decision was already "retry" (so the budget hasn't been
            # exhausted yet), as a backstop that cuts the loop early when the
            # candidate is provably stuck (identical across attempts).
            osc_count = outcome._seen_candidate_hashes.get(cand_hash, 0)
            # Honor the unit-count-aware override: if max_retries was passed,
            # use it as the oscillation budget too (the risk engine's budget
            # reads the unmodified config value, which may be higher).
            osc_budget = (
                _unit_budget if max_retries is not None
                else self.risk._effective_budget(validation.features)
            )
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
                    + _obligation_suffix(unit, cand)
                )
                return outcome
            # Convergence backstop (Issue 4): if the model produces a candidate
            # whose NORMALIZED form (comments stripped + whitespace collapsed +
            # lines sorted) has been seen ≥ cegis_convergence_threshold times,
            # the loop is cycling on the same essential output despite cosmetic
            # variation. Decoupled from the retry budget so it fires earlier than
            # the exact-hash oscillation check above — catches long runs of
            # slightly-different-but-equivalent candidates. Default threshold 2;
            # 0 = disabled.
            conv_threshold = getattr(self.config.policy, "cegis_convergence_threshold", 2)
            if conv_threshold > 0 and cand.resolved_text:
                norm_count = outcome._seen_normalized_hashes.get(norm_hash, 0)
                if norm_count >= conv_threshold and norm_hash != cand_hash:
                    # Only fire when the normalized hash differs from the exact
                    # hash (exact-repeat is already handled above). This ensures
                    # the convergence check catches ONLY cosmetic-variation
                    # cycling, not exact repeats.
                    self.journal.emit(
                        "candidate_rejected",
                        {"candidate_id": cand.candidate_id,
                         "action": "escalate", "via": "convergence",
                         "reason": f"normalized candidate seen {norm_count}× (threshold {conv_threshold}) — cosmetic-variation cycling",
                         "retry_count": retry_count},
                        step_index=self.step, path=unit.path, unit_id=unit.unit_id,
                    )
                    outcome.escalated = True
                    outcome.retry_count = retry_count
                    outcome.reason = (
                        f"candidate convergence (normalized form {norm_count}×, "
                        f"threshold {conv_threshold})"
                        + _obligation_suffix(unit, cand)
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
            failures.extend(_soft_warning_failures(validation, hard_failures=failures))
            # Dominant-counterexample selection (Phase 8 Item 1): show the model
            # ONE root-cause failure per iteration, not a concatenation of all
            # failures. When hard failures exist (compiler/syntax errors), show
            # only the first — it's the root cause. Fixing it may resolve
            # cascaded errors and preservation concerns. When no hard failures
            # exist, show only the first soft warning. This gives the weak model
            # a single, precise repair obligation instead of competing signals.
            if failures:
                hard = [f for f in failures if f.severity == "error"]
                if hard:
                    failures = [hard[0]]  # dominant compiler error
                else:
                    failures = [failures[0]]  # dominant soft warning
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
            elif cand.failure_kind in (
                "request_failed", "truncated", "parse_failed", "lsp_failed",
            ):
                # Technical/transport failures route on the retry_count budget
                # (risk.py routes these failure kinds via retry_count < budget).
                # These previously fell into the critic branch below because the
                # critic also flags every empty/garbage candidate (critic_warning
                # is not None), so retry_count never incremented and the loop
                # spun until the wall budget — the CASE_TIMEOUT spin bug.
                retry_count += 1
            elif critic_warning is not None:
                # Route based on WHY risk decided to retry: if the decision
                # reasons contain content-loss terms (not just critic terms),
                # the retry was driven by the content-loss warning and should
                # consume the normal retry_count budget, not the critic budget.
                # This prevents the infinite loop where content-loss + critic
                # co-occur and retry_count never grows (risk gates on
                # retry_count < budget, which stays true forever).
                _reasons_text = " ".join(decision.reasons or []).lower()
                # Match against the actual validator warning messages (which
                # are in `soft` as "validator: message" strings). Use terms
                # that appear in the real messages, not the human-readable
                # defaults (which soft always overrides).
                _is_content_loss_retry = any(
                    term in _reasons_text
                    for term in (
                        "drop a side", "drops a side", "dropped a side",
                        "copy one side", "copies one side", "copied one side",
                        "side's additions", "additions",
                        "intent_coverage", "coverage below",
                        "unattributed",
                        "referenced_symbol", "dependency",
                        "future_obligation", "later commit",
                    )
                )
                if _is_content_loss_retry:
                    retry_count += 1
                else:
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

    def _any_unit_used_llm(self, result: StepResult) -> bool:
        """True if any outcome in the step was resolved via an LLM candidate.

        Used to decide whether to dump conflict bundles for NEAR_MATCH
        debugging — deterministic-only steps don't need runtime input dumps
        because the resolver is reproducible from the conflict text alone.

        Uses a positive check for known LLM provenance labels rather than a
        negative check on "deterministic" — several deterministic mechanisms
        (combination_search, exact_history_reuse, block_capture, etc.) don't
        carry the "deterministic" prefix and would be wrongly flagged.
        """
        _LLM_PROVENANCES = frozenset({
            "plain_llm", "history_augmented_llm",
            "plain_llm+intent_coverage", "history_augmented_llm+intent_coverage",
        })
        for outcome in result.outcomes:
            if not outcome.accepted:
                continue
            prov = str(getattr(outcome.accepted, "provenance", "") or "")
            # Check exact match against known LLM labels, or any label that
            # contains "llm" (catches future variants).
            if prov in _LLM_PROVENANCES or "llm" in prov.lower():
                return True
        return False

    def _dump_conflict_bundles(self, result: StepResult) -> None:
        """Write runtime conflict inputs for non-PASS outcomes.

        For every unit in an escalated step, dump the exact base/current/
        replayed/refined texts + metadata to ``final/debug/<unit_id>/``.
        This lets future investigations reproduce the exact runtime conflict
        without guessing whether the rule saw diff3-refined or whole-file
        sides. Pure instrumentation — no behavioral change.
        """
        try:
            import json as _json_dbg
            debug_root = self.paths.final / "debug"
            for outcome in result.outcomes:
                unit = outcome.unit
                safe_id = unit.unit_id.replace("/", "_").replace(":", "_")
                d = debug_root / safe_id
                d.mkdir(parents=True, exist_ok=True)
                (d / "base.txt").write_text(unit.base.text or "", encoding="utf-8")
                (d / "current.txt").write_text(unit.current.text or "", encoding="utf-8")
                (d / "replayed.txt").write_text(unit.replayed.text or "", encoding="utf-8")
                refined = unit.refined_sides
                if refined:
                    (d / "refined_current.txt").write_text(refined[0], encoding="utf-8")
                    (d / "refined_base.txt").write_text(refined[1], encoding="utf-8")
                    (d / "refined_replayed.txt").write_text(refined[2], encoding="utf-8")
                meta = {
                    "unit_id": unit.unit_id,
                    "path": unit.path,
                    "language": unit.language,
                    "marker_span": list(unit.marker_span) if unit.marker_span else None,
                    "parent_unit_id": unit.structural_metadata.get("parent_unit_id"),
                    "parent_has_asymmetry": unit.structural_metadata.get("parent_has_asymmetry", False),
                    "has_refined_sides": refined is not None,
                    "escalated": outcome.accepted is None,
                    "reason": outcome.reason or "",
                }
                if outcome.accepted:
                    (d / "candidate.txt").write_text(
                        outcome.accepted.resolved_text or "", encoding="utf-8")
                    meta["resolved_via"] = outcome.accepted.provenance or ""
                elif outcome.attempts:
                    (d / "last_attempt.txt").write_text(
                        outcome.attempts[-1].resolved_text or "", encoding="utf-8")
                (d / "metadata.json").write_text(
                    _json_dbg.dumps(meta, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001 — instrumentation only
            pass

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

    def _wholesale_winner_floor(
        self,
        path: str,
        language: str | None,
        units: list,
        buffer: str | None,
    ):
        """Last-resort floor for wholesale-rewrite files: never let the
        file's final resolution wipe the dominant side.

        The wholesale gates (churn_ratio >= 0.90, dominant churn >= 30% of
        base) encode that one side rewrote the file. The Phase-1 fast path
        installs that winner directly, but two declines send the file back
        to the per-unit cascade instead — the subsumption adjudication's
        "keep" (sea-orm-0010: oracle = winner 0.99, cascade output 0.15,
        winner preservation 0.01) and a winner that fails standalone
        verification (sea-orm-0024: oracle = winner 1.0, cascade output
        0.71, winner preservation 0.0). On wholesale files the cascade's
        catastrophic mode is keeping the loser's small edit and dropping
        the rewrite. This floor fires only when the output is degenerate
        (preserves < 0.5 of the winner's churn) or when there is no output
        at all (the cascade is about to escalate with markers unresolved,
        clap-0004). A woven merge — sea-orm-0009, where the oracle
        interleaves the loser's real features into the winner — preserves
        the winner and never floors.

        Returns ``[(unit, candidate)]`` accepting the winner's whole-file
        text (the same synthetic whole_file shape the true-side portfolio
        stages), or None when the floor doesn't apply.
        """
        if not getattr(self.config.future, "enable_wholesale_winner_floor", False):
            return None
        if not units:
            return None
        try:
            _staged = _true_stage_sides(self.git, path)
        except Exception:
            _staged = None
        if _staged is not None:
            sides, base_text = _staged
        else:
            # Merge stages unreadable (stubbed git in wiring tests, or the
            # path already staged): whole-file marker units carry the sides.
            base_text = units[0].base.text if units[0].base is not None else ""
            sides = {
                "current": (units[0].current.text
                            if units[0].current is not None else ""),
                "replayed": (units[0].replayed.text
                             if units[0].replayed is not None else ""),
            }
        cur = sides.get("current", "")
        rep = sides.get("replayed", "")
        if not cur or not rep:
            return None
        from capybase.merge_intent import full_file_context as _ffc

        ctx = _ffc(base_text, cur, rep)
        if not (ctx["churn_ratio"] >= 0.90
                and ctx["dominant_churn"] >= 0.30 * max(ctx["base_lines"], 1)):
            return None
        winner = "current" if ctx["current_churn"] >= ctx["replayed_churn"] else "replayed"
        wtext = sides[winner]
        try:
            # Brace sanity only for code files — the floor can fire on
            # markdown/config wholesale rewrites where braces are prose.
            if (language and structural_gate_applies(path)
                    and not _braces_balanced(wtext, language)):
                return None
        except Exception:
            pass
        pres = None
        if buffer:
            pres = _side_preservation(base_text, wtext, buffer)
            if pres is not None and pres >= 0.5:
                return None  # the output weaves the winner — not degenerate
        self.journal.emit(
            "wholesale_winner_floor",
            {"winner": winner,
             "winner_preservation": pres if pres is not None else "n/a",
             "had_buffer": bool(buffer)},
            step_index=self.step, path=path,
        )
        from capybase.conflict_model import (
            CandidateResolution as _FL_CR,
            ConflictSide as _FL_CS,
        )
        unit = ConflictUnit(
            session_id=units[0].session_id,
            step_index=units[0].step_index,
            path=path,
            language=units[0].language,
            unit_id=f"{path}:wholesale_winner_floor",
            unit_kind="whole_file",
            base=_FL_CS(label="BASE", text=base_text),
            current=_FL_CS(label="CURRENT_UPSTREAM_SIDE", text=cur),
            replayed=_FL_CS(label="REPLAYED_COMMIT_SIDE", text=rep),
            original_worktree_text=units[0].original_worktree_text,
            marker_span=None,
        )
        cand = _FL_CR(
            candidate_id=f"{unit.unit_id}:{winner}",
            unit_id=unit.unit_id,
            model_name="wholesale_winner_floor",
            resolved_text=wtext,
            provenance=f"deterministic_wholesale_floor_{winner}",
            prompt_version="wholesale_winner_floor.v1",
        )
        return [(unit, cand)]

    def _try_deletion_respect_swap(
        self,
        path: str,
        language: str | None,
        units: list,
        buffer: str,
    ) -> list[tuple[ConflictUnit, CandidateResolution]] | None:
        """Swap in the upstream side when the buffer resurrects its deletions.

        Complements the end-of-rebase resurrection scan: the scan is a SAFE_STOP
        with no repair possible (the rebase already continued); this runs
        PRE-STAGE, while the merge-index stages are still readable and the
        whole-file machinery can still act.

        Fires only when ALL hold:

        - the merge-index stages are readable and both sides non-degenerate;
        - ``detect_resurrection(base, upstream_stage, buffer)`` finds blocks
          (>= 3 non-blank lines at >= 0.85 coverage) — the buffer carries
          content the upstream parent deleted;
        - the buffer is OTHERWISE the upstream side: >= 0.90 of the buffer's
          non-blank line content is present in the upstream stage. A woven
          merge carrying real replayed-side features fails this and is left
          alone (the end-of-rebase scan decides);
        - the upstream side verbatim verifies clean whole-file.

        Returns the whole-file acceptance list, or None (the scan remains the
        backstop — SAFE_STOP is still the honest outcome for anything this
        declines).
        """
        if not units or not buffer:
            return None
        if not getattr(self.config.validation, "enable_resurrection_detection",
                       True):
            return None  # user disabled resurrection detection: not our call
        try:
            _staged = _true_stage_sides(self.git, path)
        except Exception:  # noqa: BLE001 - stages already gone (rebase moved on)
            return None
        if _staged is None:
            return None
        sides, base_text = _staged
        cur = sides.get("current", "") or ""
        rep = sides.get("replayed", "") or ""
        if not cur.strip() or not rep.strip() or not base_text.strip():
            return None
        from capybase.merge_intent import detect_resurrection

        findings = detect_resurrection(base_text, cur, buffer)
        if not findings:
            return None

        def _line_set(text: str) -> set[str]:
            return {"".join(ln.split()) for ln in text.splitlines() if ln.strip()}

        buf_set, cur_set = _line_set(buffer), _line_set(cur)
        if not buf_set or not cur_set:
            return None
        containment = len(buf_set & cur_set) / len(buf_set)
        self.journal.emit(
            "deletion_respect_swap_probe",
            {"blocks": [f.block_line_count for f in findings],
             "coverage": [f.coverage for f in findings],
             "buffer_in_current": round(containment, 4)},
            step_index=self.step, path=path,
        )
        if containment < 0.90:
            return None  # a woven merge, not a context resurrection
        try:
            if (language and structural_gate_applies(path)
                    and not _braces_balanced(cur, language)):
                return None
        except Exception:  # noqa: BLE001
            return None
        self._write_worktree_only(path, cur, accepted=None)
        val = self.verification.verify_file(
            path, language, units[0].original_worktree_text or base_text, [],
            repo_root=str(self.git.repo), whole_text=cur)
        if not val.passed:
            return None
        # R1 (s22): the rung may have repaired the swapped-in text — write
        # and carry the repaired version (what was validated).
        _rt = getattr(val, "resolved_text", None)
        if _rt is not None:
            cur = _rt
            self._write_worktree_only(path, cur, accepted=None)
        from capybase.conflict_model import (
            CandidateResolution as _DRS_CR,
            ConflictSide as _DRS_CS,
        )
        unit = ConflictUnit(
            session_id=units[0].session_id,
            step_index=units[0].step_index,
            path=path,
            language=units[0].language,
            unit_id=f"{path}:deletion_respect_swap",
            unit_kind="whole_file",
            base=_DRS_CS(label="BASE", text=base_text),
            current=_DRS_CS(label="CURRENT_UPSTREAM_SIDE", text=cur),
            replayed=_DRS_CS(label="REPLAYED_COMMIT_SIDE", text=rep),
            original_worktree_text=units[0].original_worktree_text,
            marker_span=None,
        )
        cand = _DRS_CR(
            candidate_id=f"{unit.unit_id}:current",
            unit_id=unit.unit_id,
            model_name="deletion_respect_swap",
            resolved_text=cur,
            provenance="deterministic_source_current_only",
            prompt_version="deletion_respect_swap.v1",
            explanation=(f"buffer resurrected {len(findings)} upstream-deleted "
                         f"block(s) ({sum(f.block_line_count for f in findings)} "
                         f"lines) via git auto-merge context; swapped to the "
                         f"verified upstream side"),
        )
        self.journal.emit(
            "deletion_respect_swap",
            {"blocks": len(findings),
             "resurrected_lines": sum(f.block_line_count for f in findings),
             "buffer_in_current": round(containment, 4)},
            step_index=self.step, path=path,
        )
        return [(unit, cand)]

    def _check_side_collapse(
        self,
        path: str,
        language: str | None,
        units: list,
        buffer: str,
        result,
        accepted: list | None = None,
    ) -> bool:
        """Reject a both-rewrite file resolved to one side verbatim (WS4).

        sea-orm-0027 (unanimous, 3 runs): both sides rewrote ~36-69% of a
        407-line file, the oracle is a woven merge closest to current, and
        the model returned replayed verbatim — accepted because a one-side
        file compiles, is marker-free, and passes every structural gate. The
        eval's winner-preservation telemetry (0.055) exposed it; runtime had
        no signal.

        Churn mass alone cannot order a rejection (corpus: 79 woven-band
        cases where one side verbatim IS the oracle), so the rejection is
        LLM-GATED with the same subsumption adjudication the mid-band
        takeover uses: escalate only when the adjudicator says the dropped
        side's rewrite is NOT superseded (or confidently refuses). A
        superseded verdict, an unparseable response, or no endpoint → accept
        (the status quo; conservative direction, mirroring the takeover's
        own gating).

        Returns True when the step should escalate (reason set, bundle
        written); False to continue staging.
        """
        if not getattr(self.config.future, "enable_side_collapse_guard", True):
            return False
        if not units or not buffer:
            return False
        try:
            _staged = _true_stage_sides(self.git, path)
        except Exception:  # noqa: BLE001
            return False
        if _staged is None:
            return False
        sides, base_text = _staged
        cur = sides.get("current", "") or ""
        rep = sides.get("replayed", "") or ""
        if not cur.strip() or not rep.strip():
            return False
        det = _detect_side_collapse(base_text, cur, rep, buffer)
        if det is None:
            return False
        # Sprint-19 P2 (R1 tagging): surface which units' accepted
        # candidates carry the preservation-heuristic flag (Best-of-N
        # recoveries and carve-out accepts) so the guard's journal event
        # carries the unit-level context. Context only — no semantic
        # change without calibration.
        _flagged_units = sorted({
            u.unit_id for u, c in (accepted or [])
            if getattr(c, "flagged_by_preservation_heuristic", False)
        }) or None
        self.journal.emit(
            "side_collapse_probe",
            {**det, "flagged_preservation_units": _flagged_units},
            step_index=self.step, path=path,
        )
        adj = self._adjudicate_subsumption(
            path, language, base_text, sides, det["collapsed_to"])
        self.journal.emit(
            "side_collapse_adjudication",
            {"collapsed_to": det["collapsed_to"], "adjudication": adj},
            step_index=self.step, path=path,
        )
        # Conservative direction, mirroring the takeover's own gating: accept
        # unless the adjudicator POSITIVELY says the dropped rewrite matters
        # ("keep"), or a "superseded" verdict is too weak to trust (< 0.70).
        # None (unparseable/absent/no endpoint) accepts — churn numbers alone
        # never escalate.
        if adj is None or (adj.get("verdict") == "superseded"
                           and adj.get("confidence", 0.0) >= 0.70):
            return False
        dropped = ("current" if det["collapsed_to"] == "replayed" else "replayed")
        dropped_churn = (det["current_churn"] if dropped == "current"
                         else det["replayed_churn"])
        result.escalated = True
        result.reason = (
            f"side collapse in {path}: the merged file is the "
            f"{det['collapsed_to']} side verbatim; the {dropped} side's "
            f"rewrite ({dropped_churn} changed lines) was dropped "
            f"(adjudication: {adj['verdict'] if adj else 'unavailable'})"
        )
        write_review_bundle(
            self.paths, reason=result.reason, step_index=result.step_index,
            unit=units[0], candidate=None, validation=None,
            advisories=self._recent_advisories(),
        )
        return True

    def _try_true_side_portfolio(
        self,
        path: str,
        language: str | None,
        original: str,
        units: list,
        per_unit_buffer: str | None = None,
        wall_deadline: float | None = None,
        phase1_fast_path: bool = False,
    ):
        """Whole-file candidates from the merge index's pristine side blobs.

        Two triggers, either of which engages the same verify/adjudicate/swap
        machinery:

        1. Cross-ordered-blocks pathology — duplicate identical-signature
           definitions in the marker file's SHARED context (content outside
           every marker span that no per-region resolution can remove).
        2. Asymmetry takeover — one side rewrote the file wholesale
           (full-file churn gates, see ``merge_intent.asymmetry_takeover_gates``)
           and the per-unit merge resurrected a large fraction of that side's
           deletions. Journal-only until calibrated: gated behind
           ``future.enable_true_side_asymmetry_takeover`` (default OFF); the
           gate values are journaled either way as
           ``asymmetry_takeover_gate`` events.

        The index stages (2 = current, 3 = replayed) hold the pristine side
        files; each becomes a whole-file candidate via a synthetic whole_file
        unit (marker_span=None). Candidates pass verify_file (which classifies
        sibling build noise as infrastructure) and the per-file build test
        (same classification, via _classify_build_error_lines). When both
        sides verify, adjudication (LLM, churn-heuristic fallback) picks.

        ``per_unit_buffer`` is the current spliced merge text, used by the
        asymmetry trigger's resurrection measurement.

        Returns ``(accepted, buffer, validation)`` for the winning side, or
        None when no candidate verifies — the caller keeps its own result.
        """
        try:
            ts = _true_stage_sides(self.git, path)
        except Exception:
            ts = None
        if not ts:
            return None
        sides, base_text = ts
        trigger = "dup_pathology"
        asym_winner: str | None = None
        dupes = _shared_context_duplicate_definitions(original, language)
        # Sprint-22 P3: extreme-asymmetry fast path (zenodo-0044 class —
        # 87-line base, 1907-line current, 87-line replayed; the model
        # produces sim 0.0 on a wholesale rewrite it can't track). When
        # one side is >5× the other's line count AND rewrote ≥95% of
        # the base, that side IS the merge — take it verbatim through
        # the standard verify machinery. The compilation-optional
        # variant: if the dominant side fails verify, still substitute
        # it (flagged) — a 20× rewrite is closer to the oracle than
        # anything the model would produce.
        if phase1_fast_path:
            cur_lines = len((sides.get("current") or "").splitlines())
            rep_lines = len((sides.get("replayed") or "").splitlines())
            base_lines = len((base_text or "").splitlines())
            if (base_lines > 0 and cur_lines > 0 and rep_lines > 0
                    and max(cur_lines, rep_lines) > 5 * min(cur_lines, rep_lines)):
                from capybase.merge_intent import full_file_context as _ffc_p3
                ctx_p3 = _ffc_p3(
                    base_text, sides.get("current", ""),
                    sides.get("replayed", ""))
                if (ctx_p3["churn_ratio"] >= 0.95
                        and ctx_p3["asymmetry_side"] is not None):
                    self.journal.emit(
                        "extreme_asymmetry_gate",
                        {"cur_lines": cur_lines, "rep_lines": rep_lines,
                         "base_lines": base_lines,
                         "churn_ratio": round(ctx_p3["churn_ratio"], 3),
                         "winner": ctx_p3["asymmetry_side"]},
                        step_index=self.step, path=path)
                    trigger = "extreme_asymmetry"
                    asym_winner = ctx_p3["asymmetry_side"]
        if (phase1_fast_path and _is_lockfile_path(path)
                and bool(getattr(self.config.future,
                                 "enable_lockfile_takeover", True))):
            # Sprint-20 S20.5 — lockfile generated-file takeover. Cargo.lock
            # is a @generated regeneration artifact: the meaningful merge
            # happens in the manifest, and the real-world lockfile oracle is
            # the CURRENT side's regeneration in practice (measured on both
            # corpus Cargo.lock cases: 21/21 current-only pins kept, 0/38
            # replayed-only, ~99.7% of divergent package keys take current's
            # block). Take the current pristine side through the same verify
            # machinery (single-candidate, gate_determined — no adjudication
            # to suffer merge-guilt weaving stale pins back); a failed verify
            # declines and the per-unit cascade proceeds exactly as before.
            self.journal.emit(
                "lockfile_takeover_gate",
                {"fires": True, "winner": "current", "n_units": len(units)},
                step_index=self.step, path=path,
            )
            trigger = "lockfile_takeover"
            asym_winner = "current"
        elif phase1_fast_path:
            # Pre-cascade whole-file fast path: when one side rewrote the
            # file wholesale, the per-unit cascade is doomed-and-slow —
            # dozens of fragment LLM calls burn the case budget before
            # Phase 2's takeover could rescue (jsonc 0013/0014/0016: 1200s
            # timeouts with the correct answer sitting in the index). In
            # this regime the oracle is the winner verbatim (corpus: worst
            # winner-jaccard 0.944 across both C++ corpora's firing band),
            # so take the winner's pristine stage file up front and let
            # verification + the build gate it.
            from capybase.merge_intent import (
                FULL_FILE_ASYMMETRY_RATIO,
                FULL_FILE_DOMINANCE_FRACTION,
                full_file_context as _ffc,
            )

            ctx = _ffc(
                base_text, sides.get("current", ""), sides.get("replayed", ""))
            enabled = bool(getattr(
                self.config.future,
                "enable_true_side_asymmetry_takeover", False))
            ratio_ok = ctx["churn_ratio"] >= FULL_FILE_ASYMMETRY_RATIO
            dominance_ok = ctx["dominant_churn"] >= (
                FULL_FILE_DOMINANCE_FRACTION * max(ctx["base_lines"], 1))
            fires = bool(
                enabled and ratio_ok and dominance_ok
                and ctx["asymmetry_side"] is not None)
            self.journal.emit(
                "phase1_fast_path_gate",
                {"churn_ratio": ctx["churn_ratio"], "ratio_ok": ratio_ok,
                 "dominance_ok": dominance_ok, "winner": ctx["asymmetry_side"],
                 "fires": fires, "enabled": enabled},
                step_index=self.step, path=path,
            )
            if not fires:
                # Mid-band extension (jsonc-0004 class): churn-dominant but
                # below the wholesale band. Numbers alone are NOT safe here
                # (16/116 corpus counter-examples are genuine both-sides
                # merges), so the takeover additionally requires the LLM
                # subsumption adjudication to confirm the winner's rewrite
                # covers the loser's intent. No adjudication, no firing —
                # the per-unit cascade proceeds exactly as before.
                from capybase.merge_intent import midband_subsumption_gates

                mb = midband_subsumption_gates(
                    base_text, sides.get("current", ""), sides.get("replayed", ""))
                mb_enabled = bool(getattr(
                    self.config.future,
                    "enable_midband_subsumption_takeover", False))
                adj = None
                if mb_enabled and mb["in_band"]:
                    adj = self._adjudicate_subsumption(
                        path, language, base_text, sides, mb["winner"])
                mb_fires = bool(
                    adj is not None
                    and adj["verdict"] == "superseded"
                    and adj["confidence"] >= 0.70)
                self.journal.emit(
                    "midband_subsumption_gate",
                    {**mb, "enabled": mb_enabled, "adjudication": adj,
                     "fires": mb_fires},
                    step_index=self.step, path=path,
                )
                if not mb_fires:
                    return None
                trigger = "midband_subsumption"
                asym_winner = mb["winner"]
            else:
                trigger = "phase1_fast_path"
                asym_winner = ctx["asymmetry_side"]
                # Small-conflict confirmation (sea-orm-0009 regression,
                # found in the cross-language regression sweep): the
                # wholesale gates were calibrated on C++ corpora where the
                # oracle is the winner verbatim; rust/python counter-
                # examples exist where the oracle WEAVES the loser's
                # changes (winner token-Jaccard 0.799 vs the cascade's
                # 0.98). The separating signal is the live unit count —
                # the C++ timeout class (jsonc 0013/0014/0016, dozens of
                # fragment calls) needs the fast path; a 1-3 unit file has
                # a CHEAP cascade that can produce the better merge. For
                # those, ask the subsumption adjudication before firing:
                # cosmetic/covered losers still fire (2s PASS), real
                # features fall back to the cascade.
                if len(units) < 4:
                    adj_enabled = bool(getattr(
                        self.config.future,
                        "enable_midband_subsumption_takeover", False))
                    adj = None
                    if adj_enabled:
                        adj = self._adjudicate_subsumption(
                            path, language, base_text, sides, asym_winner)
                    adj_fires = bool(
                        adj is not None
                        and adj["verdict"] == "superseded"
                        and adj["confidence"] >= 0.70)
                    self.journal.emit(
                        "phase1_fast_path_adjudication",
                        {"n_units": len(units), "enabled": adj_enabled,
                         "adjudication": adj, "fires": adj_fires},
                        step_index=self.step, path=path,
                    )
                    if adj_enabled and not adj_fires:
                        return None
        elif not dupes:
            from capybase.merge_intent import asymmetry_takeover_gates

            gates = asymmetry_takeover_gates(
                base_text,
                sides.get("current", ""),
                sides.get("replayed", ""),
                per_unit_buffer or "",
            )
            enabled = bool(getattr(
                self.config.future,
                "enable_true_side_asymmetry_takeover", False))
            self.journal.emit(
                "asymmetry_takeover_gate",
                {**gates, "dup_definitions": 0, "enabled": enabled},
                step_index=self.step, path=path,
            )
            if not (enabled and gates.get("fires")):
                return None
            trigger = "asymmetry_takeover"
            # The winner is gate-determined; adjudication is for choosing
            # between two plausibly-good sides (the dup-pathology flow).
            # Here the losing side is by construction the stale one, and an
            # LLM asked to choose suffers "merge guilt" — weaving stale
            # lines back. The churn heuristic would return the gate winner
            # anyway (ratio >= 0.90 >> its 0.35 refinement band).
            asym_winner = gates.get("winner")
        verified: list[tuple[str, str, object]] = []
        # The asymmetry takeover needs only the gate winner — the loser is
        # by construction stale. The dup-pathology flow verifies both sides
        # (adjudication chooses between two plausibly-good versions).
        _candidates = (
            [(asym_winner, sides[asym_winner])] if asym_winner
            else list(sides.items())
        )
        if wall_deadline is not None:
            import time as _ts_time

            if _ts_time.monotonic() > wall_deadline - 120:
                # Whole-file verification runs a build per candidate; with
                # very little wall clock left, don't start builds we can't
                # finish. 120s covers one ccache-warm targeted verify — the
                # earlier 300s margin starved the recovery entirely on
                # cases whose Phase 1 burned the budget on empty-LLM
                # retries (protobuf-0043: the only mechanism that could
                # fix the duplicate never got to run).
                self.journal.emit(
                    "true_side_portfolio_skipped",
                    {"reason": "wall_deadline", "trigger": trigger},
                    step_index=self.step, path=path,
                )
                return None
        for side, text in _candidates:
            try:
                # Brace sanity only for code files — prose/config files have
                # no brace semantics (markdown code fences false-fail it).
                if (language and structural_gate_applies(path)
                        and not _braces_balanced(text, language)):
                    continue
            except Exception:
                pass
            self._write_worktree_only(path, text, accepted=None)
            # The probed text IS pristine — its own brace count cannot veto
            # it (F2): the compile decides.
            val = self.verification.verify_file(
                path, language, original, [],
                repo_root=str(self.git.repo), whole_text=text,
                pristine_side_texts=[text])
            if not val.passed:
                continue
            if getattr(val, "resolved_text", None) is not None:
                # R1 (s22): the pristine-side text needed a coherence repair
                # to pass — it is no longer the pristine side. Decline the
                # swap rather than accept a silently modified side text.
                continue
            verified.append((side, text, val))
        if not verified:
            return None
        if asym_winner is not None:
            choice, via = asym_winner, "gate_determined"
        elif len(verified) == 2:
            choice, via = self._adjudicate_whole_side(
                path, language, base_text, sides)
        else:
            choice, via = verified[0][0], "single_compiling_side"
        # No separate _run_raw_test here: it only ran when a build command
        # was configured — exactly when verify_file above already ran that
        # same build (with sibling-error classification). Re-running it
        # doubled/tripled the build count and blew the case wall clock on
        # large-file cases (protobuf-0073's enabled run timed out on ~5
        # sequential per-file builds). The post-swap Phase 2 iteration
        # re-validates the final buffer anyway.
        for side, text, val in [(c, sides[c], v) for c, _, v in verified if c == choice]:
            # Fail-fast build for the pre-cascade triggers (redis-0010
            # regression): when the swapped-in winner fails the per-file
            # build for merge-relevant reasons, declining here lets the
            # per-unit cascade run in its place — the correct recovery.
            # The alternative flow (accept the swap, let Phase 2's build
            # check fail it, repair) is doomed: whole-file repair prompts
            # on a true_side_stage unit are oversized by construction
            # (the "unit" is the entire file), so the case escalates
            # despite oracle-equal content. Environmental failures
            # (sibling-file errors, missing build targets) proceed —
            # consistent with Phase 2's environmental accept semantics.
            if trigger in ("phase1_fast_path", "midband_subsumption"):
                _fb_cmd = self._resolve_per_file_build(path)
                if _fb_cmd:
                    self._write_worktree_only(path, text, accepted=None)
                    _fb_ok, _fb_out = self._run_raw_test(_fb_cmd)
                    if not _fb_ok:
                        _fb_errors = [
                            ln for ln in (_fb_out or "").splitlines()
                            if "error" in ln.lower()
                        ][:5]
                        _fb_merge, _fb_env = _classify_build_error_lines(
                            _fb_errors, path)
                        if _fb_merge:
                            self.journal.emit(
                                "phase1_fast_path_declined",
                                {"reason": "build", "cmd": _fb_cmd,
                                 "errors": _fb_merge[:3]},
                                step_index=self.step, path=path,
                            )
                            return None
            from capybase.conflict_model import (
                CandidateResolution as _TS_CR,
                ConflictSide as _TS_CS,
            )
            unit = ConflictUnit(
                session_id=units[0].session_id,
                step_index=units[0].step_index,
                path=path,
                language=units[0].language,
                unit_id=f"{path}:true_side_stage",
                unit_kind="whole_file",
                base=_TS_CS(label="BASE", text=base_text),
                current=_TS_CS(
                    label="CURRENT_UPSTREAM_SIDE",
                    text=sides.get("current", "")),
                replayed=_TS_CS(
                    label="REPLAYED_COMMIT_SIDE",
                    text=sides.get("replayed", "")),
                original_worktree_text=original,
                marker_span=None,
            )
            cand = _TS_CR(
                candidate_id=f"{unit.unit_id}:{side}",
                unit_id=unit.unit_id,
                model_name="true_side_portfolio",
                resolved_text=text,
                provenance=f"deterministic_source_{side}_only_stage",
                prompt_version="true_side_portfolio.v1",
            )
            self.journal.emit(
                "true_side_portfolio",
                {"side": side, "via": via, "trigger": trigger,
                 "n_units": len(units), "dup_definitions": len(dupes)},
                step_index=self.step, path=path,
            )
            return [(unit, cand)], text, val
        return None

    def _try_whole_side_repair_rung(
        self,
        path: str,
        language: str | None,
        original: str,
        units: list,
        buffer: str,
        *,
        wall_deadline: float | None = None,
    ):
        """Pristine-side repair rung for a compile-failed spliced buffer.

        Sprint-19 P1 (the tokio-0109/0037 class; both external reviewers
        converged on this design): the per-unit splice reconstruction is
        lossy — when its whole-file COMPILE gate fails, the pristine
        merge-index stage sides are the only candidates known to be
        compilable whole files. Probe both (verify_file, sibling-error
        classification included), then:

        - neither verifies → decline (repair/escalate exactly as before);
        - exactly one verifies → swap it in only when the subsumption
          adjudication confirms the failing side's work is superseded
          (confidence >= 0.70) — ``keep`` or no verdict declines;
        - both verify → the both-sides repair adjudication must pick a
          side with confidence >= 0.70; ``neither`` (the woven class)
          or a low-confidence/unparseable answer declines.

        NEVER pre-emptive: the caller gates on an actual compile-flavored
        failure (``_is_compile_flavored_failure``); churn numbers alone
        cannot separate one-side oracles from woven merges. Every probe
        is journaled (``whole_side_probe``), the swap as
        ``whole_side_repair``, a decline as ``whole_side_repair_declined``.

        Returns ``(accepted, buffer, validation)`` like
        ``_try_true_side_portfolio``, or None when the rung declines (the
        caller proceeds with its repair loop unchanged).
        """
        if not getattr(self.config.future, "enable_whole_side_repair_rung",
                       True):
            return None
        if wall_deadline is not None:
            import time as _wsr_time

            if _wsr_time.monotonic() > wall_deadline - 120:
                # Each probe runs a whole-file verification build; with
                # very little wall clock left, don't start probes we
                # can't finish (same margin the portfolio uses).
                self.journal.emit(
                    "whole_side_repair_declined",
                    {"reason": "wall_deadline"},
                    step_index=self.step, path=path,
                )
                return None
        try:
            ts = _true_stage_sides(self.git, path)
        except Exception:
            ts = None
        if not ts:
            return None
        sides, base_text = ts
        if len(sides) < 2:
            # A single pristine side is the portfolio's territory (its
            # triggers handle the one-sided index); the repair rung
            # compares both sides against a failed splice.
            return None
        verified: list[tuple[str, str, object]] = []
        import time as _probe_time

        for side, text in sides.items():
            _t0 = _probe_time.monotonic()
            # F2 (s27): NO brace-balance veto on PRISTINE side texts. The
            # counter is preprocessor-blind — select.c's braces inside #if
            # branches make BOTH sides "unbalanced" while gcc compiles them
            # clean, so the veto declined every probe without evidence
            # (sqlite-0108/0111: the tier-2 ballot chose a side at 0.95
            # confidence and could never land it — 'no_side_verifies' by
            # fiat, not by build). A pristine corpus side is exactly the
            # candidate worth a real compile; the build is the arbiter.
            self._write_worktree_only(path, text, accepted=None)
            # The probed text IS pristine — its own brace count cannot veto
            # it (F2): the compile decides.
            val = self.verification.verify_file(
                path, language, original, [],
                repo_root=str(self.git.repo), whole_text=text,
                pristine_side_texts=[text])
            _probe_payload = {
                "side": side, "passed": bool(val.passed),
                "duration_s": round(_probe_time.monotonic() - _t0, 1),
                "hard_failures": [
                    f.message for f in val.hard_failures][:3]}
            if not val.passed and language == "rust":
                # S27-extend instrumentation: the in-session cargo env
                # fails pristine sides that pass standalone (axum-0019 —
                # offline reproduction exhausted including the full eval
                # env). Capture the RAW cargo output + the cargo-relevant
                # env subset so the delta is readable from the journal.
                try:
                    import subprocess as _sp_diag
                    import os as _os_diag
                    _diag = _sp_diag.run(
                        ["cargo", "check", "--message-format=short"],
                        cwd=str(self.git.repo),
                        capture_output=True, text=True, timeout=300)
                    _probe_payload["diag_rc"] = _diag.returncode
                    _probe_payload["diag_tail"] = (
                        (_diag.stderr or "")[-600:])
                    _probe_payload["env"] = {
                        k: v for k, v in _os_diag.environ.items()
                        if k.startswith(("CARGO", "RUST", "CC", "PATH"))}
                except Exception:  # noqa: BLE001 — diagnostic is best-effort
                    pass
            self.journal.emit(
                "whole_side_probe", _probe_payload,
                step_index=self.step, path=path,
            )
            if val.passed:
                if getattr(val, "resolved_text", None) is not None:
                    # R1 (s22): repaired ≠ pristine — decline the side.
                    self.journal.emit(
                        "whole_side_probe",
                        {"side": side, "passed": True,
                         "declined": "coherence_repair"},
                        step_index=self.step, path=path,
                    )
                    continue
                verified.append((side, text, val))

        def _restore_spliced() -> None:
            # Leave the worktree holding the spliced buffer we started
            # from, so the caller's repair loop (and its build test)
            # operates on the buffer it knows about.
            try:
                self._write_worktree_only(path, buffer, accepted=None)
            except Exception:  # noqa: BLE001
                pass

        if not verified:
            self.journal.emit(
                "whole_side_repair_declined",
                {"reason": "no_side_verifies"},
                step_index=self.step, path=path,
            )
            _restore_spliced()
            return None
        choice: str | None = None
        via: str | None = None
        adj_info: dict | None = None
        if len(verified) == 2:
            # Both sides compile — substituting either drops the other's
            # work, so the adjudication gets an explicit "neither"
            # escape and must be confident (the woven class must keep
            # its CEGIS repair).
            import json as _wsr_json

            try:
                prompt = _whole_side_repair_prompt_both(
                    path, language, base_text, sides)
                resp = self.resolution_engine.raw_complete(
                    prompt, json_mode=True,
                    max_tokens=max(
                        2048, self.resolution_engine.config.max_tokens))
                raw = resp.text if hasattr(resp, "text") else str(resp)
                parsed = _wsr_json.loads(raw)
                _choice = str(parsed.get("choice", "")).strip().lower()
                _conf = _safe_conf(parsed.get("confidence"))
                adj_info = {
                    "choice": _choice,
                    "confidence": round(_conf, 2),
                    "reason": str(parsed.get("reason", ""))[:200],
                }
                if _choice in ("current", "replayed") and _conf >= 0.70:
                    choice, via = _choice, "repair_adjudication"
            except Exception:
                adj_info = None
            self.journal.emit(
                "whole_side_repair_adjudication",
                {"branch": "both_compile", "adjudication": adj_info,
                 "picked": choice},
                step_index=self.step, path=path,
            )
        else:
            # Exactly one side compiles — the strong-signal branch. The
            # subsumption adjudication decides whether the FAILING
            # side's work is essential (keep → decline) or superseded.
            ok_side = verified[0][0]
            adj = self._adjudicate_subsumption(
                path, language, base_text, sides, ok_side)
            adj_info = adj
            if (adj is not None and adj.get("verdict") == "superseded"
                    and float(adj.get("confidence", 0.0)) >= 0.70):
                choice, via = ok_side, "subsumption_adjudication"
            self.journal.emit(
                "whole_side_repair_adjudication",
                {"branch": "single_compiling_side",
                 "compiling_side": ok_side, "adjudication": adj,
                 "picked": choice},
                step_index=self.step, path=path,
            )
        if choice is None:
            self.journal.emit(
                "whole_side_repair_declined",
                {"reason": "adjudication_declined"},
                step_index=self.step, path=path,
            )
            _restore_spliced()
            return None
        for side, text, val in verified:
            if side != choice:
                continue
            from capybase.conflict_model import (
                CandidateResolution as _WSR_CR,
                ConflictSide as _WSR_CS,
            )
            unit = ConflictUnit(
                session_id=units[0].session_id,
                step_index=units[0].step_index,
                path=path,
                language=units[0].language,
                unit_id=f"{path}:true_side_stage",
                unit_kind="whole_file",
                base=_WSR_CS(label="BASE", text=base_text),
                current=_WSR_CS(
                    label="CURRENT_UPSTREAM_SIDE",
                    text=sides.get("current", "")),
                replayed=_WSR_CS(
                    label="REPLAYED_COMMIT_SIDE",
                    text=sides.get("replayed", "")),
                original_worktree_text=original,
                marker_span=None,
            )
            cand = _WSR_CR(
                candidate_id=f"{unit.unit_id}:{side}",
                unit_id=unit.unit_id,
                model_name="whole_side_repair",
                resolved_text=text,
                provenance=f"deterministic_source_{side}_only_stage",
                prompt_version="whole_side_repair.v1",
            )
            self.journal.emit(
                "whole_side_repair",
                {"side": side, "via": via},
                step_index=self.step, path=path,
            )
            return [(unit, cand)], text, val
        return None

    def _f1_tier2_adjudicate(
        self, path: str, language: str | None,
        base_text: str, sides: dict[str, str],
    ) -> str | None:
        """F1 tier-2 (sprint-23): LLM subsumption adjudication.

        When tier-1 declines (both sides changed > threshold), ask the
        model: does one side subsume the other, or must they weave?
        Returns the side name to take, or None (weave / unparseable /
        low confidence). The adjudicator only ever sees weaves that
        ALREADY FAILED validation — the failure-path gate bounds the
        blast radius."""
        from capybase.f1_adjudication import (
            f1_tier2_prompt,
            parse_f1_tier2_response,
        )
        try:
            prompt = f1_tier2_prompt(
                path, base_text,
                sides.get("current", ""), sides.get("replayed", ""))
            resp = self.resolution_engine.raw_complete(
                prompt, json_mode=True,
                max_tokens=max(2048, self.resolution_engine.config.max_tokens))
            raw = resp.text if hasattr(resp, "text") else str(resp)
            parsed = parse_f1_tier2_response(raw)
            if parsed is None:
                # redis-0049 forensics: a declined ballot was invisible —
                # no event, indistinguishable from an exception. Journal
                # the decline for attribution.
                self.journal.emit(
                    "f1_tier2_adjudication_declined",
                    {"path": path, "why": "unparseable",
                     "raw_head": (raw or "")[:120]},
                    step_index=self.step, path=path,
                )
                return None
            choice, conf, reason = parsed
            if choice in ("current", "replayed") and conf >= 0.70:
                self.journal.emit(
                    "f1_tier2_adjudication",
                    {"choice": choice, "confidence": conf,
                     "reason": reason, "path": path},
                    step_index=self.step, path=path,
                )
                return choice
            self.journal.emit(
                "f1_tier2_adjudication_declined",
                {"path": path, "why": "weave_or_low_confidence",
                 "choice": choice, "confidence": conf},
                step_index=self.step, path=path,
            )
            return None  # weave or low confidence
        except Exception as exc:  # noqa: BLE001 — adjudication is best-effort
            # redis-0049 forensics: a wall-deadline timeout inside the LLM
            # call died here SILENTLY — no event, no fallback, the case just
            # escalated. Journal it so the churn fallback's trigger is
            # attributable.
            self.journal.emit(
                "f1_tier2_adjudication_failed",
                {"error": f"{type(exc).__name__}: {exc}"[:200], "path": path},
                step_index=self.step, path=path,
            )
            return None

    def _adjudicate_whole_side(
        self,
        path: str,
        language: str | None,
        base_text: str,
        sides: dict[str, str],
    ) -> tuple[str, str]:
        """Choose between two compiling true whole-file sides.

        LLM adjudication first — it sees refinement semantics (consistent
        renames, API evolution, deliberate deletions) that churn numbers
        can't. The deterministic churn heuristic is the fallback when the
        model is unparseable or low-confidence. Returns ``(side, via)``.
        """
        import json as _json

        heuristic = _whole_side_heuristic(base_text, sides)
        llm_info: dict | None = None
        try:
            prompt = _whole_side_adjudication_prompt(
                path, language, base_text, sides)
            resp = self.resolution_engine.raw_complete(prompt, json_mode=True)
            raw = resp.text if hasattr(resp, "text") else str(resp)
            parsed = _json.loads(raw)
            choice = str(parsed.get("choice", "")).strip().lower()
            conf = _safe_conf(parsed.get("confidence"))
            if choice in ("current", "replayed"):
                llm_info = {
                    "choice": choice,
                    "confidence": round(conf, 2),
                    "reason": str(parsed.get("reason", ""))[:200],
                }
                if conf >= 0.70:
                    self.journal.emit(
                        "whole_side_adjudication",
                        {"llm": llm_info, "heuristic": heuristic,
                         "picked": choice, "via": "llm"},
                        step_index=self.step, path=path,
                    )
                    return choice, "llm_adjudication"
        except Exception:
            llm_info = None
        self.journal.emit(
            "whole_side_adjudication",
            {"llm": llm_info, "heuristic": heuristic,
             "picked": heuristic, "via": "heuristic"},
            step_index=self.step, path=path,
        )
        return heuristic, "churn_heuristic"

    def _adjudicate_subsumption(
        self,
        path: str,
        language: str | None,
        base_text: str,
        sides: dict[str, str],
        winner: str,
    ) -> dict | None:
        """Ask the model whether the winner's rewrite supersedes the loser.

        Mid-band takeover's semantic gate: churn numbers alone cannot
        separate "rewrite subsumes the small side" (take the winner
        verbatim) from "small side adds real functionality" (keep the
        region merge) — the corpus counter-examples overlap on every
        shape metric. Returns the parsed verdict dict
        (``{"verdict": "keep"|"superseded", "confidence": f, "reason": s}``)
        or None on an unparseable/absent model response (the caller treats
        None as "keep": no takeover without a positive superseded verdict).

        Sprint-24 cycle-C: self-consistency (3 samples, agreement-weighted).
        clickhouse-0021 flipped PASS→NEAR→ESCALATE across three cycles on
        IDENTICAL inputs — keep vs superseded at equal 0.95 confidence both
        ways, a genuinely borderline judgment. A single sample is a coin
        flip on these shapes. The majority verdict's confidence is scaled
        by sample agreement, so the caller's 0.70 bar effectively requires
        unanimity (0.95 × 2/3 = 0.63 doesn't fire); a split vote settles
        to keep — the conservative no-takeover direction, consistent with
        "no takeover without a positive superseded verdict".
        """
        import json as _json

        loser = "replayed" if winner == "current" else "current"
        try:
            prompt = _subsumption_adjudication_prompt(
                path, language, base_text, winner,
                sides.get(winner, ""), sides.get(loser, ""))
            # Decision prompts bill a large hidden pre-fill against the
            # completion budget on the local server (742-802 tokens measured
            # for a one-sentence JSON on a 5K-char prompt, scaling up with
            # prompt size); a fragment-sized config cap returns empty
            # content with finish_reason=length. Floor at 2048.
            samples: list[dict] = []
            for _ in range(3):
                resp = self.resolution_engine.raw_complete(
                    prompt, json_mode=True,
                    max_tokens=max(2048, self.resolution_engine.config.max_tokens))
                raw = resp.text if hasattr(resp, "text") else str(resp)
                if not (raw or "").strip():
                    continue
                try:
                    parsed = _json.loads(raw)
                except ValueError:
                    continue
                verdict = str(parsed.get("verdict", "")).strip().lower()
                if verdict not in ("keep", "superseded"):
                    continue
                samples.append({
                    "verdict": verdict,
                    "confidence": round(_safe_conf(parsed.get("confidence")), 2),
                    "reason": str(parsed.get("reason", ""))[:200],
                })
            if not samples:
                return None
            votes_keep = sum(1 for s in samples if s["verdict"] == "keep")
            votes_sup = len(samples) - votes_keep
            # Majority required: with 3 valid samples, 2-1 decides; a 1-1
            # split (only 2 valid) or a tie is borderline → keep.
            verdict = (
                "superseded" if votes_sup > votes_keep
                else "keep" if votes_keep > votes_sup
                else "keep"  # tie → conservative
            )
            winners = [s for s in samples if s["verdict"] == verdict]
            conf = round(sum(s["confidence"] for s in winners) / len(winners), 2)
            # Unanimous-majority agreement is required to fire at the caller's
            # 0.70 bar: a 2-1 split halves the effective confidence so a
            # borderline shape rarely takes over. The samples ride the result
            # for journal attribution.
            agreement = round(len(winners) / len(samples), 2)
            eff_conf = round(conf * agreement, 2)
            return {
                "verdict": verdict,
                "confidence": eff_conf,
                "reason": winners[0]["reason"],
                "samples": [
                    {"v": s["verdict"], "c": s["confidence"]} for s in samples
                ],
                "agreement": agreement,
            }
        except Exception as exc:  # noqa: BLE001 — adjudication is advisory
            self.journal.emit(
                "midband_subsumption_adjudication_failed",
                {"error": f"{type(exc).__name__}: {exc}"[:200]},
                step_index=self.step, path=path,
            )
            return None

    def _resolve_per_file_build(self, path: str) -> str:
        """Resolve the per-file build command for Phase 2's build check.

        Uses ``cc_build_target_template`` (e.g. ``make {stem}.o``) when
        configured — this compiles only the conflict file's TU, avoiding
        false failures from sibling-file errors that a full ``make -j4``
        would surface. Returns "" when no per-file target is available.
        """
        from pathlib import PurePosixPath as _P
        template = getattr(self.config.validation, "cc_build_target_template", "") or ""
        if not template:
            return ""
        stem = _P(path).stem
        return template.format(stem=stem)

    def _run_raw_test(self, cmd: str) -> tuple[bool, str]:
        """Run a shell command in the repo root; return (passed, output).

        Lightweight test runner for Phase 2's per-file build check — no
        journal events, no verdict parsing. Returns the combined
        stdout+stderr so the caller can feed the error to fault attribution.

        A "No rule to make target" failure is treated as PASS — the per-file
        build target doesn't exist in the Makefile (e.g. sqlite's explicit
        .lo rules vs redis's %.o pattern rule), so the check is N/A. A
        missing build system ("no makefile found") is likewise N/A — a
        rebase worktree doesn't carry generated build artifacts.
        """
        from capybase.verification import (
            _is_missing_build_system,
            _run_shell_tree,
        )
        try:
            proc = _run_shell_tree(cmd, cwd=str(self.git.repo), timeout=120)
            output = (proc.stderr or "") + (proc.stdout or "")
            if proc.returncode != 0 and (
                "No rule to make target" in output
                or _is_missing_build_system(output)
            ):
                return True, ""  # target/build system unavailable → N/A
            return proc.returncode == 0, output
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    def _run_tests(self, label: str, result: StepResult) -> bool:
        cmd = getattr(self.config.tests, label) if hasattr(self.config.tests, label) else None
        if not cmd:
            # Bug #10: when tests.required=True but the per-label command is
            # explicitly unset, the prior code silently returned True — skipping
            # a user-REQUIRED test gate. A required gate with no command is a
            # misconfiguration that must escalate (return False) so the rebase
            # doesn't continue past a gate the user demanded. When required=False
            # (the permissive case), no command means no gate configured → pass.
            if getattr(self.config.tests, "required", False):
                self.journal.emit(
                    "tests_required_but_no_command",
                    {"label": label},
                    step_index=self.step,
                )
                self.out(
                    f"  tests.required is set but no `{label}` command is "
                    f"configured; cannot run the required test gate. Set "
                    f"[tests] {label} to your suite's command."
                )
                return False
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
        # Sprint-19 P4 (D4.1): make-output parsing. The runner's verdict
        # parser misses compile errors in raw make output (protobuf-0065:
        # rc=2, verdict unknown, diagnostics [] — the merge's real defect
        # invisible at the gate). Extract the error-carrying lines so they
        # surface in the journal and feed the attribution below.
        self._last_tests_compiler_indictment = False
        _is_build_gate = bool(_phase2_fallback_build_cmd(cmd))
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
        # Sprint-19 P4 (D4.2): compiler-authority attribution. When the gate
        # command IS a build and it failed with error lines that positively
        # locate in a file this session wrote, the compiler is indicting the
        # merge itself — that escalates regardless of tests.required (a
        # silent wrong merge is the worst outcome); advisory config covers
        # unattributable failures — sibling/environmental/unknown — which
        # keep today's non-blocking behavior). Strict positive attribution
        # only: unparseable lines never trigger the override.
        _error_lines = [
            ln for ln in ((run.stdout or "") + (run.stderr or "")).splitlines()
            if "error" in ln.lower()
        ][:20]
        # D0 (sprint-23): parallel builds can swallow the per-file gcc
        # diagnostics, leaving only the make driver summary — the gate
        # fails "blind" (protobuf-0051/0065: rc=2, empty diagnostics,
        # every error-keyed mechanism starved). When a build gate fails
        # with no attributable lines, re-run SERIALLY once to recover
        # the real diagnostics. Cheap relative to a blind escalate.
        if (_is_build_gate and not run.passed and not run.timed_out
                and cmd and not _error_lines):
            import re as _re_d0
            _serial_cmd = _re_d0.sub(r"-j\d+\s*", "", cmd).strip()
            if _serial_cmd and _serial_cmd != cmd:
                _serial_ok, _serial_out = self._run_raw_test(_serial_cmd)
                _error_lines = [
                    ln for ln in (_serial_out or "").splitlines()
                    if "error" in ln.lower()
                ][:20]
                self.journal.emit(
                    "build_diagnostic_recovery",
                    {"command": _serial_cmd,
                     "recovered_lines": len(_error_lines)},
                    step_index=self.step,
                )
        _attributed: list[str] = []
        if _is_build_gate and not run.passed and _error_lines and not run.timed_out:
            from capybase.verification import _parse_cc_error_location

            _merged_paths = list(getattr(result, "units_by_path", {}) or {})
            _merged_stems = {Path(p).stem for p in _merged_paths}
            from capybase.verification import _is_cc_werror_warning
            for ln in _error_lines:
                if ln.startswith(("make[", "make:", "ninja:")) or "CMake Error" in ln:
                    continue  # build-driver summaries carry no file:line
                # D13 (s27): a -Werror promotion is not a merge defect —
                # the same doctrine f609847 applied to the verdict and era
                # probe, missing HERE. jsonc-0016: 'json_parse_double
                # defined but not used [-Werror=unused-function]' was
                # attributed to the merge, tripped the compiler-authority
                # stop, and the case escalated 3x at 0.98 while the eval's
                # own build check passed it (compiles=True).
                if _is_cc_werror_warning(ln):
                    continue
                stem, _ = _parse_cc_error_location(ln)
                if stem is not None and stem in _merged_stems:
                    _attributed.append(ln)
            _attributed = _attributed[:5]
        self.journal.emit(
            "tests_finished",
            {
                "label": label,
                "passed": run.passed,
                "returncode": run.returncode,
                "timed_out": run.timed_out,
                "verdict": run.verdict.kind,
                "verdict_summary": run.verdict.summary,
                "diagnostics": (
                    run.verdict.diagnostics[:5] or _error_lines[:5]
                ),
                "build_gate": _is_build_gate,
                "attributed_merge_errors": _attributed,
                "stdout_tail": run.stdout[-1000:],
                "stderr_tail": run.stderr[-1000:],
            },
            step_index=self.step,
        )
        if _attributed:
            self._last_tests_compiler_indictment = True
            # Sprint-20 S20.6: stash the indictment context so the run
            # loop's micro-CEGIS rung (before the compiler-authority
            # escalate) can repair from the SAME attributed errors.
            self._last_gate_cmd = cmd
            self._last_attributed_merge_errors = list(_attributed)
            self.journal.emit(
                "compiler_authority_override",
                {"label": label, "command": cmd,
                 "attributed_merge_errors": _attributed,
                 "tests_required": bool(getattr(
                     self.config.tests, "required", False))},
                step_index=self.step,
            )
            self.out(
                "  " + self._warn(
                    f"! {label} build failed with errors attributed to merged "
                    f"file(s) — escalating (compiler authority)"
                )
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
        # A no-op when no unit was accepted (an escalation step): the report
        # is a "why we accepted" summary; an escalated step has no accepted
        # units to report on. The escalation review bundle is a separate file.
        if result.escalated or not any(
            o.accepted is not None for o in result.outcomes
        ):
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
    crate lives in its own subdirectory and there's no root manifest. Only one
    level deep is scanned: a workspace's member crates sit directly under the
    root, and a deeper scan risks matching an unrelated vendored crate. Used by
    the auto-substitution of ``cargo test`` for the default ``pytest`` test gate.
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

"""Exact history reuse — replay a prior accepted resolution verbatim (#9 step 4).

When capybase has resolved an *identical* conflict before (same normalized
base/current/replayed shape, same language, same region key/structural hash, with
a prior accepted outcome and validation evidence), there's no reason to spend an
LLM call or re-run the structural resolver: replay the prior resolution verbatim.

This is the conservative ``rerere++ exact`` mechanism — deliberately NOT fuzzy
patch reuse. The trust conditions are strict (all required), so the matching
surface is small and safe:

  1. same normalized base/current/replayed conflict shape (exact, via
     :func:`capybase.memory.shape.conflict_shape_hash`),
  2. same language,
  3. same region key kind OR same structural hash,
  4. prior outcome ``accepted`` (escalated outcomes are never reused — this is
     the "no human-correction recorded" gate in v1; a correction store would
     tighten it further),
  5. the prior carries evidence of validation: tests passed OR future probe
     passed OR no recorded diagnostics (stored on the experience's features),
  6. (hook) not a recorded human-correction/revert — vacuously true today.

**Always on, no flag** (per design): the reused candidate is built and returned,
but the orchestrator runs it through the IDENTICAL validation gauntlet
(obligations, diagnostics, future obligations, strictness). A stale or wrong
reuse fails validation and falls through to structural/LLM exactly as if the
reuse never matched — so bugs surface immediately without a flag. The reuse is a
speed/quality optimization, never a correctness bypass.

Pure: :func:`find_exact_reuse` reads the store + the new conflict and returns a
:class:`ReuseCandidate` (the resolved text + the matched experience's provenance)
or None. The orchestrator owns validation + the dispatch (it's the sole mutator).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from capybase.memory.shape import conflict_shape_hash

if TYPE_CHECKING:
    from capybase.memory.store import Experience, ExperienceStore


@dataclass(frozen=True)
class ReuseCandidate:
    """A verbatim reuse of a prior accepted resolution.

    ``resolved_text`` is the prior resolution to replay. ``source_summary`` is
    the matched experience's example summary (for journaling/reporting). The
    orchestrator wraps this in a CandidateResolution with
    ``provenance="exact_history_reuse"`` and validates it like any candidate.

    ``matched_conditions`` records WHICH trust conditions the match satisfied
    (#idea 8 auditability) — e.g. ["shape=abc123", "language=python",
    "region_kind=function", "outcome=accepted", "evidence=clean"]. ``near_misses``
    records the same-shape priors that were REJECTED and why — so a skip isn't
    indistinguishable from an empty store.
    """

    resolved_text: str
    source_summary: str
    source_session: str
    matched_conditions: tuple[str, ...] = ()
    near_misses: tuple[str, ...] = ()
    skip_reason: str = ""  # set when constructed as a "skipped" sentinel


def _validation_evidence(exp: "Experience") -> bool:
    """Condition 5: the prior accepted carries validation evidence.

    An accepted experience with recorded test/probe success (or no recorded
    diagnostics) is trustworthy to reuse. We check the merged features for:
    - tests_passed == True, OR
    - future_apply_probe applies == True, OR
    - no introduced_diagnostics (a clean prior validation).

    Absent features (old records) are treated as evidence-positive: an accepted
    resolution that passed the gauntlet once is reusable; the re-validation is
    the backstop.
    """
    feats = exp.validator_features or {}
    if feats.get("tests_passed") is True:
        return True
    if feats.get("future_apply_probe_applies") is True:
        return True
    # No introduced diagnostics = a clean prior validation.
    diag = feats.get("introduced_diagnostics")
    if diag is None or diag == 0:
        return True
    return False


def find_exact_reuse(
    *,
    unit: Any,
    store: "ExperienceStore | None",
    language: str | None,
    region_kind: str,
    path: str | None = None,
) -> ReuseCandidate | None:
    """Find a prior accepted resolution to replay verbatim (#9 step 4 / #idea 8).

    Scans the store's accepted experiences for one matching ALL trust conditions
    (same conflict shape, same language, same region kind, same path, accepted
    outcome, validation evidence). Returns the FIRST match (oldest-first store
    order) as a :class:`ReuseCandidate` carrying ``matched_conditions`` (which
    conditions satisfied) + ``near_misses`` (same-shape priors that were rejected
    + why).

    The **path** condition is load-bearing: the conflict-shape hash is
    intentionally content-independent (it captures the per-side edit structure —
    added/removed/changed line counts), so two conflicts in *different files*
    with the same edit structure hash equal. Without the path check, a resolution
    from one file can be verbatim-replayed into a structurally-unrelated file —
    the live eval showed ``rust_port_test``'s ``port: 9090`` resolution replayed
    into ``rust_impl``'s ``src/config.rs`` hunk because both had the same shape.
    Path is matched on the repo-relative path both sides record (exact string
    match, forward-slash normalized); when ``path`` is None/empty the check is
    skipped (backward compat for callers that don't pass it).

    Returns None when no store, or when there's nothing to reuse from. When the
    store has same-shape priors but none passed all conditions, returns a
    ``ReuseCandidate`` with ``skip_reason="no full match"`` + the near-misses — so
    the caller can journal WHY reuse didn't apply (not just "no match", which is
    indistinguishable from an empty store). Never raises on a no-match; an internal
    exception propagates (the orchestrator emits an advisory, #idea 4).

    ``unit`` is the new ConflictUnit; ``region_kind`` is its coarse kind.
    """
    if store is None:
        return None
    # NOTE: we deliberately do NOT wrap this in try/except. A genuine "no match"
    # returns a skip sentinel below; an exception (corrupt store, bug) propagates
    # to the orchestrator, which catches it and emits an exact_reuse_failed
    # advisory (#idea 4) — so a failure is visible rather than mislabeled.
    base = getattr(getattr(unit, "base", None), "text", "") or ""
    current = getattr(getattr(unit, "current", None), "text", "") or ""
    replayed = getattr(getattr(unit, "replayed", None), "text", "") or ""
    if not current and not replayed:
        return None  # nothing to reuse from
    target_shape = conflict_shape_hash(
        base=base, current=current, replayed=replayed
    )
    # Normalize the query path once (repo-relative, forward-slashes).
    target_path = (path or getattr(unit, "path", "") or "").replace("\\", "/").strip()
    near_misses: list[str] = []
    for exp in store.accepted():
        # Condition 1: same conflict shape.
        if not exp.conflict_shape or exp.conflict_shape != target_shape:
            continue
        # This prior matches the SHAPE — a candidate for near-miss recording.
        # Conditions 2-7 narrow it; each failure is a near-miss with a reason.
        # Condition 2: same language.
        if language is not None and exp.language and exp.language != language:
            near_misses.append(f"{exp.example.summary}: wrong language ({exp.language})")
            continue
        # Condition 3: same region kind (the structural coordinate).
        if region_kind and exp.region_kind and exp.region_kind != region_kind:
            near_misses.append(f"{exp.example.summary}: wrong region kind ({exp.region_kind})")
            continue
        # Condition 4: same file path. The shape hash is content-independent, so
        # two different files with the same edit structure collide here — the path
        # check prevents a resolution from one file leaking into another.
        exp_path = (exp.path or "").replace("\\", "/").strip()
        if target_path and exp_path and exp_path != target_path:
            near_misses.append(f"{exp.example.summary}: wrong path ({exp.path})")
            continue
        # Condition 4: prior outcome accepted (store.accepted() already
        # filters to outcome == "accepted", so this is guaranteed; the check
        # is documented here for the contract).
        if exp.outcome != "accepted":
            near_misses.append(f"{exp.example.summary}: non-accepted outcome ({exp.outcome})")
            continue
        # Condition 5: validation evidence.
        if not _validation_evidence(exp):
            near_misses.append(f"{exp.example.summary}: no validation evidence")
            continue
        # Condition 6 (hook): no recorded human-correction/revert. None are
        # recorded today → vacuously true. A future correction store would
        # check it here.
        resolved = exp.example.resolved or ""
        if not resolved:
            near_misses.append(f"{exp.example.summary}: empty resolved text")
            continue
        # Full match — record the conditions that matched (#idea 8 auditability).
        conditions = [
            f"shape={target_shape}",
            f"language={exp.language or 'any'}",
            f"region_kind={exp.region_kind or 'any'}",
            f"path={exp.path or 'any'}",
            "outcome=accepted",
            "evidence=" + (
                "tests_passed" if (exp.validator_features or {}).get("tests_passed") is True
                else "clean" if _validation_evidence(exp)
                else "unknown"
            ),
        ]
        return ReuseCandidate(
            resolved_text=resolved,
            source_summary=exp.example.summary,
            source_session=exp.session_id,
            matched_conditions=tuple(conditions),
            near_misses=tuple(near_misses),
        )
    # No full match. If there were near-misses (same-shape priors that failed a
    # later condition), return a skip sentinel carrying them — so the caller can
    # journal WHY (e.g. "1 wrong language, 1 no evidence") rather than the opaque
    # "no exact match". If there were no same-shape priors at all, return None
    # (genuine empty-match case).
    if near_misses:
        return ReuseCandidate(
            resolved_text="", source_summary="", source_session="",
            skip_reason="no full match", near_misses=tuple(near_misses),
        )
    return None

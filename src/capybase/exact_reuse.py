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
    """

    resolved_text: str
    source_summary: str
    source_session: str
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
) -> ReuseCandidate | None:
    """Find a prior accepted resolution to replay verbatim (#9 step 4).

    Scans the store's accepted experiences for one matching ALL trust conditions
    (same conflict shape, same language, same region kind, accepted outcome,
    validation evidence). Returns the FIRST match (oldest-first store order), or
    None when no match or no store. Never raises.

    ``unit`` is the new ConflictUnit; ``region_kind`` is its coarse kind
    (function/class/etc., from region_key_from_unit). The conflict shape is
    computed from the unit's three sides.
    """
    if store is None:
        return None
    # NOTE: we deliberately do NOT wrap this in try/except. A genuine "no match"
    # returns None below; an exception (corrupt store, bug in shape comparison)
    # propagates to the orchestrator, which catches it and emits an
    # exact_reuse_failed advisory (#idea 4) — so a failure is visible rather than
    # mislabeled as "no match".
    base = getattr(getattr(unit, "base", None), "text", "") or ""
    current = getattr(getattr(unit, "current", None), "text", "") or ""
    replayed = getattr(getattr(unit, "replayed", None), "text", "") or ""
    if not current and not replayed:
        return None  # nothing to reuse from
    target_shape = conflict_shape_hash(
        base=base, current=current, replayed=replayed
    )
    for exp in store.accepted():
        # Condition 1: same conflict shape.
        if not exp.conflict_shape or exp.conflict_shape != target_shape:
            continue
        # Condition 2: same language.
        if language is not None and exp.language and exp.language != language:
            continue
        # Condition 3: same region kind (the structural coordinate).
        if region_kind and exp.region_kind and exp.region_kind != region_kind:
            continue
        # Condition 4: prior outcome accepted (store.accepted() already
        # filters to outcome == "accepted", so this is guaranteed; the check
        # is documented here for the contract).
        if exp.outcome != "accepted":
            continue
        # Condition 5: validation evidence.
        if not _validation_evidence(exp):
            continue
        # Condition 6 (hook): no recorded human-correction/revert. None are
        # recorded today → vacuously true. A future correction store would
        # check it here.
        resolved = exp.example.resolved or ""
        if not resolved:
            continue
        return ReuseCandidate(
            resolved_text=resolved,
            source_summary=exp.example.summary,
            source_session=exp.session_id,
        )
    return None

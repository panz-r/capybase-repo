"""Resolution provenance — the explicit "how was this resolved?" enum.

Replaces the fragile string-prefix inference in :func:`accept_report._via_label`,
which previously reverse-engineered the resolution mechanism from
``candidate.prompt_version`` / ``candidate.model_name`` prefixes (``structural.*``,
``sbcr*``, ``cegis_block_capture.v1``, …). That coupling hid the mechanism behind
convention and made it impossible to slice the experience corpus by mechanism for
quality metrics (#9) or to label the dry-run report (#10).

This module defines the stable string enum stamped on every
:class:`~capybase.conflict_model.CandidateResolution` at construction time. The
values are plain strings (not an :class:`enum.Enum`) to stay JSON-friendly and to
match the codebase's existing style for ``structural_resolver.Rule`` and
``policy_strictness.PolicyMode`` (both ``Literal`` unions of strings).

Values
------
``deterministic_structural``
    The structural resolver applied a provably-safe union/identity rule.
``combination_search``
    The SBCR search-based combination resolver won.
``block_capture``
    A modify/delete block-capture keep/delete decision was applied.
``exact_history_reuse``
    A prior accepted resolution was reused verbatim (exact-match, no LLM).
``history_augmented_llm``
    The LLM resolved it AND history context meaningfully augmented the prompt
    (history confidence above the re-stamp threshold, future touches present).
``plain_llm``
    The LLM resolved it without meaningful history augmentation.
``manual``
    A human provided the resolution via interactive/manual mode.

The empty string ``""`` is reserved for backward-compatibility with candidates
serialized before provenance existed; consumers fall back to the legacy
``prompt_version`` inference for it.
"""

from __future__ import annotations

from typing import Literal

# The complete set of provenance values. Add new mechanisms here; the union is
# the single source of truth. ``""`` is intentionally excluded (it means "old
# data, infer the legacy way") — see :data:`LEGACY_PROVENANCE`.
ResolutionProvenance = Literal[
    "deterministic_structural",
    "combination_search",
    "block_capture",
    "exact_history_reuse",
    "history_augmented_llm",
    "plain_llm",
    "manual",
]

#: All valid provenance values, in display order. Used by metrics (#9) to emit a
#: stable table and by the dry-run report (#10).
PROVENANCE_VALUES: tuple[str, ...] = (
    "deterministic_structural",
    "deterministic_brace_repair",
    "exact_history_reuse",
    "combination_search",
    "test_gated_side",
    "block_capture",
    "history_augmented_llm",
    "plain_llm",
    "manual",
)

#: Sentinel for candidates serialized before provenance existed. Consumers must
#: treat this as "unknown — infer the legacy way" rather than as a real value.
LEGACY_PROVENANCE = ""

#: Short human labels for reports. Keys are provenance values; the legacy empty
#: string maps to ``(legacy)`` so old candidates render something sane.
PROVENANCE_LABELS: dict[str, str] = {
    "deterministic_structural": "deterministic structural",
    "deterministic_brace_repair": "deterministic brace repair",
    "combination_search": "combination search",
    "test_gated_side": "test-gated side pick",
    "block_capture": "block-capture (keep/delete)",
    "exact_history_reuse": "exact history reuse",
    "history_augmented_llm": "history-augmented LLM",
    "plain_llm": "LLM",
    "manual": "manual (human)",
    LEGACY_PROVENANCE: "(legacy)",
}

#: Provenance values produced by LLM-adjacent paths. Used by the orchestrator to
#: decide whether a candidate *might* be re-stamped to ``history_augmented_llm``
#: once history confidence is known.
LLM_PROVENANCES: frozenset[str] = frozenset({"plain_llm"})


def provenance_label(provenance: str) -> str:
    """Human label for a provenance value, falling back gracefully."""
    return PROVENANCE_LABELS.get(provenance or LEGACY_PROVENANCE, provenance or LEGACY_PROVENANCE)


def is_valid(provenance: str) -> bool:
    """True when ``provenance`` is a known value (or the legacy empty string)."""
    return provenance == LEGACY_PROVENANCE or provenance in PROVENANCE_LABELS

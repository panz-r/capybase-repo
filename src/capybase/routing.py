"""Difficulty-aware routing (survey §6.1, ICoT/RoutingGen pattern).

A rebase conflict is classified as ``simple`` or ``complex`` *before* any LLM
call, using signals already present on the :class:`ConflictUnit`. The class
decides which generation pipeline the orchestrator runs:

* ``simple``  — one low-temperature sample, no two-pass, no consensus. The
  common case (a single isolated hunk) resolves trivially; spending N samples
  and an intent pass on it wastes tokens for no accuracy gain.
* ``complex`` — the full pipeline (two-pass intent → code + N parallel samples
  + consensus voting). Multi-hunk files and large enclosing nodes are where a
  3B model genuinely struggles and where test-time compute pays off.

This concentrates test-time compute where it matters and cuts ~half the tokens
on easy cases, without changing the contracts downstream — both paths produce
the same ``list[CandidateResolution]`` consumed by the validators and risk
engine. The classifier is a pure function of the unit's metadata; thresholds
live in :class:`RoutingConfig` so they are tunable without code changes.

Only structural signals are used (no model call) so classification is free,
deterministic, and instant. A unit is ``complex`` if ANY of:

- ``sibling_count > 0`` — the file has more than one conflict hunk; the 3B
  model must keep sibling regions consistent (the documented failure mode).
- the enclosing AST node spans more than ``max_simple_node_lines`` — a large
  logical block means more context the model must reconcile.
- the combined side text exceeds ``max_simple_side_chars`` — large bodies are
  likelier to confuse a small model ("lost in the middle").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from capybase.conflict_model import ConflictUnit

Difficulty = Literal["simple", "complex"]


@dataclass(frozen=True)
class RoutingConfig:
    """Thresholds + enable flag for difficulty-aware routing.

    ``enabled`` defaults to False so routing is opt-in: until set, the
    orchestrator uses the existing flat dispatch and behavior is unchanged.
    """

    enabled: bool = False
    # A file with more than one conflict hunk is treated as complex. The common
    # multi-hunk failure mode (settings-uu) is where a 3B model breaks down.
    complex_if_sibling_count_gt: int = 0
    # Enclosing AST node larger than this (lines) → complex. Tuned so an
    # ordinary function body is "simple" but a large class/module is not.
    max_simple_node_lines: int = 40
    # Combined base+current+replayed side text longer than this (chars) →
    # complex. Guards against large bodies that distract a small model.
    max_simple_side_chars: int = 1200


def _side_chars(unit: "ConflictUnit") -> int:
    """Total characters across the three conflict sides."""
    return len(unit.base.text) + len(unit.current.text) + len(unit.replayed.text)


def _node_lines(unit: "ConflictUnit") -> int | None:
    """Line count of the enclosing AST node, or None if unrecorded."""
    span = unit.structural_metadata.get("enclosing_node_span")
    if isinstance(span, (list, tuple)) and len(span) == 2:
        try:
            return int(span[1]) - int(span[0]) + 1
        except (TypeError, ValueError):
            return None
    return None


def classify_difficulty(
    unit: "ConflictUnit", config: RoutingConfig | None = None
) -> Difficulty:
    """Classify a conflict unit as ``simple`` or ``complex``.

    Pure function of the unit's structural metadata and side texts; no I/O, no
    model call. See module docstring for the decision rules. When ``config`` is
    None, :class:`RoutingConfig` defaults are used.
    """
    cfg = config or RoutingConfig()
    meta = unit.structural_metadata
    # 1. Multi-hunk file → complex.
    try:
        sibling_count = int(meta.get("sibling_count", 0) or 0)
    except (TypeError, ValueError):
        sibling_count = 0
    if sibling_count > cfg.complex_if_sibling_count_gt:
        return "complex"
    # 2. Large enclosing node → complex.
    lines = _node_lines(unit)
    if lines is not None and lines > cfg.max_simple_node_lines:
        return "complex"
    # 3. Large combined side text → complex.
    if _side_chars(unit) > cfg.max_simple_side_chars:
        return "complex"
    return "simple"

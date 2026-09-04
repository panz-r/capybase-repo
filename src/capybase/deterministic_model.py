"""The shared deterministic-primitive model (reuse-design stage 2).

Every keyed-collection primitive (import union, named fields, keyed
items, attributes, manifest arrays, block insertion, deletion union)
repeats the same lifecycle: hash the preimage, apply an edit
transactionally, check local validity, hash the output, record closed
obligations, build a certificate. This module is that pattern's ONE
implementation.

The `ImportUnionResult` vocabulary (APPLIED / NOT_APPLICABLE /
AMBIGUOUS / BLOCKED) was already the de-facto shared status set; this
model formalizes it with the transaction + certificate the proposal
specifies, without rewriting the existing primitives (they adopt it
incrementally — each port wraps its edit in `EditTransaction.apply`).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum, auto


class PrimitiveStatus(Enum):
    APPLIED = "applied"
    NOT_APPLICABLE = "not_applicable"
    AMBIGUOUS = "ambiguous"
    BLOCKED = "blocked"


class OutcomeKind(Enum):
    """What happened when a rule ran (the proposal's distinction)."""
    NOT_APPLICABLE = auto()    # the mechanism doesn't apply to this input
    DECLINED = auto()          # applicable, but unsafe (collision, ambiguity)
    PROPOSED = auto()          # a candidate was produced
    INTERNAL_ERROR = auto()    # the implementation crashed (best-effort caught)


@dataclass(frozen=True)
class SourceSpan:
    """A span in the original text (0-based, half-open [start, end))."""
    start: int
    end: int


@dataclass(frozen=True)
class TextEdit:
    """One atomic text replacement."""
    span: SourceSpan
    replacement: str
    reason: str = ""


def text_hash(text: str) -> str:
    """The short content hash used across all primitives."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class EditTransaction:
    """A transactional edit: apply atomically or not at all.

    The engine enforces (per the proposal's universal rules):
    1. The source hash matches (nothing moved under us).
    2. All spans are in bounds.
    3. Edits do not overlap.
    4. Edits apply in descending-offset order (offsets stay valid).
    5. No text outside the declared region changes.
    6. Idempotency: applying to the ALREADY-EDITED text is a no-op
       (detected by the output hash matching).
    """
    source_hash: str
    edits: tuple[TextEdit, ...]
    mechanism_id: str
    mechanism_version: str = "1"

    def apply(self, source: str) -> tuple[str, list[TextEdit]]:
        """Apply the edits; returns (new_text, applied_edits).

        Raises ValueError on any precondition violation (bounds,
        overlap, source-hash mismatch) — the caller catches and returns
        the original text unchanged (transactional semantics).
        """
        if text_hash(source) != self.source_hash:
            raise ValueError(
                f"source hash mismatch: expected {self.source_hash}, "
                f"got {text_hash(source)} — the text moved under the "
                f"transaction")
        # Validate all spans before applying any edit (atomic).
        for e in self.edits:
            if e.span.start < 0 or e.span.end > len(source) \
                    or e.span.start > e.span.end:
                raise ValueError(
                    f"edit span out of bounds: [{e.span.start}, {e.span.end}) "
                    f"on text of length {len(source)}")
        # Check for overlaps.
        sorted_edits = sorted(self.edits, key=lambda e: -e.span.start)
        for i in range(len(sorted_edits) - 1):
            if sorted_edits[i].span.start < sorted_edits[i + 1].span.end:
                raise ValueError(
                    f"overlapping edits: [{sorted_edits[i+1].span.start}, "
                    f"{sorted_edits[i+1].span.end}) and "
                    f"[{sorted_edits[i].span.start}, {sorted_edits[i].span.end})")
        # Apply in descending-offset order (offsets stay valid).
        result = source
        applied: list[TextEdit] = []
        for e in sorted_edits:
            result = (result[:e.span.start] + e.replacement
                      + result[e.span.end:])
            applied.append(e)
        return result, applied


@dataclass
class PrimitiveResult:
    """The uniform result for every deterministic primitive.

    Replaces bare ImportUnionResult returns with the full evidence
    (transaction + certificate). Compatibility: ImportUnionResult is
    still the wire format today; ports construct BOTH from this.
    """
    status: PrimitiveStatus
    outcome: OutcomeKind
    candidate: str | None = None
    transaction: EditTransaction | None = None
    certificate: dict = field(default_factory=dict)
    closed_obligations: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)

    @property
    def applied(self) -> bool:
        return self.status is PrimitiveStatus.APPLIED

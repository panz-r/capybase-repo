"""The generic keyed-collection merge engine (reuse-design stage 2).

ONE lifecycle implementation for the five collection primitives
(import union, named fields, keyed items, attributes, manifest
arrays). Each primitive currently repeats: hash the preimage, filter
obligations, check idempotency, try the edit, validate locally, hash
the output, build the certificate. This engine implements that
lifecycle ONCE; the language-specific codec supplies only:

- what counts as an applicable obligation (the filter)
- what "already present" means (the idempotency key)
- how to apply one edit (the edit function)
- what local validity means (the validator)

The engine enforces the EditTransaction's universal rules (source-hash
CAS, bounds, no overlap) via :mod:`capybase.deterministic_model` and
produces the uniform certificate.

Ports run in SHADOW MODE first: the existing primitive stays
authoritative; the engine's result is compared and divergences
recorded before any switch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from capybase.deterministic_model import (
    EditTransaction,
    OutcomeKind,
    PrimitiveResult,
    PrimitiveStatus,
    SourceSpan,
    TextEdit,
    text_hash,
)


class CollectionCodec(Protocol):
    """The language/construct-specific half of a keyed-collection merge."""

    def applicable_obligations(
        self, obligations: list,
    ) -> list[str]:
        """Filter to the additive, non-exclusive, executable lines."""
        ...

    def already_present(
        self, candidate_text: str, item: str,
    ) -> bool:
        """Idempotency: is this item already in the text?"""
        ...

    def try_edit(
        self, candidate_text: str, item: str, context: str,
    ) -> tuple[int, int, str] | None:
        """Apply one item: returns (start, end, replacement) or None.

        The engine converts the span+replacement into a TextEdit and
        enforces the transaction rules.
        """
        ...

    def local_validity(self, edited_text: str) -> bool:
        """Local structural validity after the edit (balance, etc.)."""
        ...


@dataclass
class ShadowDivergence:
    """One old-vs-new divergence found during shadow execution."""
    mechanism: str
    item: str
    old_status: str
    new_status: str
    old_text_head: str = ""
    new_text_head: str = ""


def to_wire_result(
    result: PrimitiveResult,
    resolved_text: str,
    *,
    mechanism_id: str,
    reason_map: dict[str, str] | None = None,
    applied_cert: Callable[[PrimitiveResult], dict] | None = None,
):
    """Map the engine's :class:`PrimitiveResult` to the legacy wire format.

    Stage 4 (adapter consolidation): every switched primitive's public
    function reduces to codec + engine + THIS mapping — status
    translation, text fallback, the ``primitive`` key, reason
    remapping, and the APPLIED-certificate extension via a
    per-primitive callback (closed over its codec). The wire type
    (:class:`~capybase.import_union.ImportUnionResult`) keeps its
    historical home in import_union; moving it is the deferred package
    restructure. Imported lazily so the engine module stays standalone.
    """
    from capybase.import_union import (
        ImportUnionResult,
        STATUS_APPLIED, STATUS_NOT_APPLICABLE,
        STATUS_AMBIGUOUS, STATUS_BLOCKED,
    )
    status_map = {
        PrimitiveStatus.APPLIED: STATUS_APPLIED,
        PrimitiveStatus.NOT_APPLICABLE: STATUS_NOT_APPLICABLE,
        PrimitiveStatus.AMBIGUOUS: STATUS_AMBIGUOUS,
        PrimitiveStatus.BLOCKED: STATUS_BLOCKED,
    }
    text = result.candidate if result.candidate is not None else resolved_text
    cert = dict(result.certificate)
    cert["primitive"] = mechanism_id
    if reason_map and "reason" in cert:
        cert["reason"] = reason_map.get(cert["reason"], cert["reason"])
    if result.status == PrimitiveStatus.APPLIED and applied_cert is not None:
        cert.update(applied_cert(result))
    return ImportUnionResult(
        status=status_map[result.status], text=text, certificate=cert)


def merge_keyed_collection(
    codec: CollectionCodec,
    resolved_text: str,
    missing_obligations: list,
    *,
    other_side_text: str = "",
    mechanism_id: str = "keyed_collection/v0",
) -> PrimitiveResult:
    """The ONE keyed-collection lifecycle.

    Filter → idempotency → per-item edits (transactional) → local
    validity → certificate. Never raises (internal errors →
    INTERNAL_ERROR outcome with the original text).
    """
    before_hash = text_hash(resolved_text)
    base_cert = {
        "primitive": mechanism_id,
        "before_hash": before_hash,
        "after_hash": before_hash,
    }
    try:
        items = codec.applicable_obligations(missing_obligations)
        if not items:
            return PrimitiveResult(
                PrimitiveStatus.NOT_APPLICABLE, OutcomeKind.NOT_APPLICABLE,
                candidate=resolved_text,
                certificate={**base_cert, "reason": "no applicable items"})

        # Idempotency: drop items already present.
        fresh = [i for i in items if not codec.already_present(resolved_text, i)]
        if not fresh:
            return PrimitiveResult(
                PrimitiveStatus.NOT_APPLICABLE, OutcomeKind.NOT_APPLICABLE,
                candidate=resolved_text,
                certificate={**base_cert,
                             "reason": "all items already present (idempotent)"})

        # Apply each item's edit SEQUENTIALLY — each try_edit sees the
        # text after the previous edit (matching the existing primitives'
        # behavior: later fields may depend on earlier insertions for
        # their destination location). The edits accumulate into ONE
        # transaction against the ORIGINAL text via running offsets.
        edits: list[TextEdit] = []
        closed: list[str] = []
        unresolved: list[str] = []
        running_text = resolved_text
        running_offset = 0  # cumulative shift from prior edits
        for item in fresh:
            result = codec.try_edit(running_text, item, other_side_text)
            if result is None:
                unresolved.append(item.strip()[:60])
                continue
            start, end, replacement = result
            # Record the edit against the ORIGINAL text coordinates by
            # shifting back the running offset.
            edits.append(TextEdit(
                span=SourceSpan(start - running_offset,
                                end - running_offset),
                replacement=replacement,
                reason=f"insert: {item.strip()[:50]}"))
            closed.append(item.strip())
            # Update the running text + offset for the next item.
            running_text = (running_text[:start] + replacement
                            + running_text[end:])
            running_offset += len(replacement) - (end - start)

        if not closed:
            return PrimitiveResult(
                PrimitiveStatus.NOT_APPLICABLE, OutcomeKind.DECLINED,
                candidate=resolved_text,
                certificate={**base_cert,
                             "reason": "no items could be safely inserted",
                             "unresolved": unresolved})

        # The EditTransaction certificate records the source hash +
        # edit list (auditability). The sequential application above
        # produced the authoritative text — same-position insertions
        # legitimately diverge from a batch re-application (ordering:
        # sequential inserts AFTER prior inserts at the same point,
        # batch descending-order inserts BEFORE them), so the
        # transaction is the RECORD, not the re-application.
        tx = EditTransaction(
            source_hash=before_hash, edits=tuple(edits),
            mechanism_id=mechanism_id)
        edited_text = running_text

        # Local structural validity.
        if not codec.local_validity(edited_text):
            return PrimitiveResult(
                PrimitiveStatus.BLOCKED, OutcomeKind.DECLINED,
                candidate=resolved_text,
                certificate={**base_cert,
                             "reason": "local validity check failed"})

        after_hash = text_hash(edited_text)
        return PrimitiveResult(
            PrimitiveStatus.APPLIED, OutcomeKind.PROPOSED,
            candidate=edited_text,
            transaction=tx,
            closed_obligations=closed,
            certificate={
                **base_cert,
                "after_hash": after_hash,
                "closed_obligations": closed,
                "remaining_obligations": len(unresolved),
                "edits": [e.reason for e in edits],
                "unresolved": unresolved,
            })
    except Exception as exc:  # noqa: BLE001 — never crash resolution
        return PrimitiveResult(
            PrimitiveStatus.BLOCKED, OutcomeKind.INTERNAL_ERROR,
            candidate=resolved_text,
            certificate={**base_cert, "reason": f"internal error: {exc}"})


def shadow_compare(
    mechanism: str,
    old_result,  # ImportUnionResult from the existing primitive
    new_result: PrimitiveResult,
) -> list[ShadowDivergence]:
    """Compare old and new during shadow execution.

    Divergences are recorded, not acted on — the switch decision comes
    after the divergence set is understood (the proposal's rule).
    """
    divergences: list[ShadowDivergence] = []
    old_status = str(getattr(old_result, "status", "?"))
    new_status = new_result.status.value
    if old_status != new_status:
        divergences.append(ShadowDivergence(
            mechanism, "(status)", old_status, new_status))
    old_text = str(getattr(old_result, "text", "") or "")
    new_text = str(new_result.candidate or "")
    if old_status == "applied" and new_status == "applied" and old_text != new_text:
        divergences.append(ShadowDivergence(
            mechanism, "(text)", old_status, new_status,
            old_text_head=old_text[:80], new_text_head=new_text[:80]))
    return divergences

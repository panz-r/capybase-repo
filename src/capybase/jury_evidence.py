"""Evidence packet builder + validator for the shadow jury (Part SJ2, design §7).

Builds a bounded packet with stable evidence IDs so jurors reference only
supplied evidence (no hallucinated line numbers or outside knowledge). A
deterministic validator confirms every code evidence hash belongs to the frozen
buffer, source-comment provenance matches the ledger, and the candidate comment
is not cited as support for itself.

The packet shape (design §7):
```json
{
  "claim": {...},
  "evidence": [
    {"id": "SIG:fetch", "kind": "signature", "text": "def fetch(...)"},
    {"id": "CODE:fetch:B4", "kind": "code", "text": "...", "anchor_hash": "..."},
    {"id": "SRC:target:C42", "kind": "source_comment", "text": "...", "provenance": "target"},
    {"id": "TEST:...", "kind": "test", "text": "...", "result": "passed"}
  ]
}
```

The model may reference only evidence IDs in the packet. Routing depends only
on validated structured fields, not free-text explanations.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from capybase.comment_claims import Claim


@dataclass
class EvidenceItem:
    """One piece of evidence in the packet (design §7)."""
    id: str               # stable ID: "SIG:fetch", "CODE:fetch:B4", "SRC:target:C42"
    kind: str             # signature | code | source_comment | test
    text: str
    provenance: str = ""  # for source_comment: base/current/replayed
    anchor_hash: str = "" # for code: sha256 of the anchored block (validates it's in the frozen buffer)
    result: str = ""      # for test: passed/failed/not_run


@dataclass
class EvidencePacket:
    """The bounded evidence set for one claim (design §7)."""
    claim: Claim
    evidence: list[EvidenceItem] = field(default_factory=list)
    code_fingerprint: str = ""  # sha256 of the frozen executable tokens
    unit_id: str = ""


# ---------------------------------------------------------------------------
# Evidence ID schemes (stable, parseable)
# ---------------------------------------------------------------------------


def _sig_id(func_name: str) -> str:
    return f"SIG:{func_name}"


def _code_id(func_name: str, block_label: str) -> str:
    return f"CODE:{func_name}:{block_label}"


def _src_id(version: str, lineage_id: str) -> str:
    return f"SRC:{version}:{lineage_id}"


def _test_id(test_name: str) -> str:
    return f"TEST:{test_name}"


# ---------------------------------------------------------------------------
# Code block extraction (for CODE evidence)
# ---------------------------------------------------------------------------


def _extract_code_blocks(
    frozen_code: str, anchor_symbol: str, lang: str, max_blocks: int = 6,
) -> list[tuple[str, str, str]]:
    """Extract anchored code blocks for the claim's enclosing function.

    Returns ``[(block_label, block_text, anchor_hash), ...]``. Blocks are
    labeled by line range (e.g. "L10-15") so the IDs are stable. ``anchor_hash``
    is sha256(block_text)[:16] — the validator confirms it's present in the
    frozen code. Capped at ``max_blocks`` to keep the packet bounded.
    """
    if not frozen_code or not anchor_symbol:
        return []
    # Parse the function name from the anchor_symbol ("function:foo" → "foo").
    func_name = anchor_symbol.split(":", 1)[-1] if ":" in anchor_symbol else anchor_symbol
    if not func_name:
        return []
    # Find the function body by locating the def/fn line + matching braces/indent.
    lines = frozen_code.split("\n")
    # Locate the function start (a line containing the func name + def/fn keyword).
    start_idx = None
    for i, ln in enumerate(lines):
        if func_name in ln and re.search(r"\b(?:def|fn|function|fun|func)\b", ln):
            start_idx = i
            break
    if start_idx is None:
        return []
    # Extract a bounded window (the function + a few lines). For brace languages,
    # find the matching closing brace; for Python, find the dedent.
    window = lines[start_idx:start_idx + 40]  # cap at 40 lines
    blocks: list[tuple[str, str, str]] = []
    # Split the window into ~3-4 chunks of ~5-10 lines each for granular evidence.
    chunk_size = max(5, len(window) // 3)
    for ci in range(0, min(len(window), max_blocks * chunk_size), chunk_size):
        chunk = window[ci:ci + chunk_size]
        if not any(ln.strip() for ln in chunk):
            continue
        block_text = "\n".join(chunk)
        label = f"L{start_idx + ci + 1}-{start_idx + ci + len(chunk)}"
        h = hashlib.sha256(block_text.encode()).hexdigest()[:16]
        blocks.append((label, block_text, h))
        if len(blocks) >= max_blocks:
            break
    return blocks


def _extract_signature(
    frozen_code: str, anchor_symbol: str, lang: str,
) -> str:
    """The enclosing function's signature line (the first line of its body)."""
    if not frozen_code or not anchor_symbol:
        return ""
    func_name = anchor_symbol.split(":", 1)[-1] if ":" in anchor_symbol else anchor_symbol
    if not func_name:
        return ""
    for ln in frozen_code.split("\n"):
        if func_name in ln and re.search(r"\b(?:def|fn|function|fun|func)\b", ln):
            return ln.strip()
    return ""


# ---------------------------------------------------------------------------
# Packet builder (SJ2)
# ---------------------------------------------------------------------------


def build_evidence_packet(
    claim: Claim,
    frozen_code: str,
    ledger_entries: list,  # list of LedgerEntry (the comment provenance)
    code_fingerprint: str = "",
    unit_id: str = "",
    lang: str = "",
    test_results: list[dict] | None = None,  # [{"name": ..., "result": "passed"}, ...]
) -> EvidencePacket:
    """Build the bounded evidence packet for one claim (design §7).

    Evidence kinds:
    - ``signature``: the enclosing function's signature line.
    - ``code``: anchored code blocks from the frozen buffer (with anchor_hash).
    - ``source_comment``: the provenance variants from the ledger.
    - ``test``: relevant tests with results (when supplied).

    The model may reference only evidence IDs in the packet. A deterministic
    validator (:func:`validate_evidence_packet`) confirms every code evidence
    hash belongs to the frozen buffer, source-comment provenance matches the
    ledger, and the candidate comment is not cited as support for itself.
    """
    packet = EvidencePacket(claim=claim, code_fingerprint=code_fingerprint, unit_id=unit_id)
    anchor = getattr(claim, "lineage_id", "")  # not the anchor; we need the ledger's anchor
    # Find the claim's lineage in the ledger to get the anchor_symbol.
    anchor_symbol = ""
    source_variants: list[tuple[str, str]] = []  # (version, text)
    for entry in ledger_entries:
        if getattr(entry, "lineage_id", "") == claim.lineage_id:
            if not anchor_symbol:
                anchor_symbol = getattr(entry, "anchor_symbol", "")
            source_variants.append((getattr(entry, "version", ""), getattr(entry, "text", "")))
    # Signature evidence.
    sig = _extract_signature(frozen_code, anchor_symbol, lang)
    if sig:
        func_name = anchor_symbol.split(":", 1)[-1] if ":" in anchor_symbol else ""
        packet.evidence.append(EvidenceItem(
            id=_sig_id(func_name), kind="signature", text=sig,
        ))
    # Code evidence (anchored blocks with hashes).
    for label, block_text, h in _extract_code_blocks(frozen_code, anchor_symbol, lang):
        func_name = anchor_symbol.split(":", 1)[-1] if ":" in anchor_symbol else "fn"
        packet.evidence.append(EvidenceItem(
            id=_code_id(func_name, label), kind="code", text=block_text, anchor_hash=h,
        ))
    # Source-comment evidence (the provenance variants).
    for version, text in source_variants:
        packet.evidence.append(EvidenceItem(
            id=_src_id(version, claim.lineage_id), kind="source_comment",
            text=text, provenance=version,
        ))
    # Test evidence (when supplied).
    if test_results:
        for t in test_results[:5]:  # cap at 5 tests
            name = t.get("name", "")
            result = t.get("result", "not_run")
            if name:
                packet.evidence.append(EvidenceItem(
                    id=_test_id(name), kind="test", text=name, result=result,
                ))
    return packet


# ---------------------------------------------------------------------------
# Packet validator (SJ2 — deterministic, runs before any juror sees the packet)
# ---------------------------------------------------------------------------


def validate_evidence_packet(
    packet: EvidencePacket, frozen_code: str, ledger_entries: list,
) -> list[str]:
    """Validate the evidence packet deterministically (design §7).

    Returns a list of validation errors (empty = valid). Confirms:
    - every code evidence's anchor_hash belongs to the frozen buffer,
    - source-comment provenance matches a ledger entry's version,
    - the candidate comment (the claim's own text) is not cited as support,
    - evidence IDs are unique + well-formed.

    A juror verdict citing invalid evidence routes to ``abstain`` (no action).
    """
    errors: list[str] = []
    seen_ids: set[str] = set()
    # Pre-compute all sha256[:16] of contiguous substrings of the frozen code.
    # (Expensive in general; we approximate by hashing line-chunks of the frozen
    # code and checking membership. A code evidence block is valid if its hash
    # matches any contiguous chunk.)
    frozen_lines = frozen_code.split("\n") if frozen_code else []
    frozen_hashes: set[str] = set()
    # Hash all 1..40-line windows starting at each line (bounded: O(lines*40)).
    for start in range(len(frozen_lines)):
        for span in range(1, min(41, len(frozen_lines) - start + 1)):
            chunk = "\n".join(frozen_lines[start:start + span])
            frozen_hashes.add(hashlib.sha256(chunk.encode()).hexdigest()[:16])
    # Ledger provenance lookup.
    ledger_versions: dict[tuple[str, str], str] = {}
    for entry in ledger_entries:
        lid = getattr(entry, "lineage_id", "")
        ver = getattr(entry, "version", "")
        text = getattr(entry, "text", "")
        if lid and ver:
            ledger_versions[(lid, ver)] = text
    for ev in packet.evidence:
        if ev.id in seen_ids:
            errors.append(f"duplicate evidence id: {ev.id}")
        seen_ids.add(ev.id)
        if ev.kind == "code":
            if ev.anchor_hash and ev.anchor_hash not in frozen_hashes:
                errors.append(
                    f"code evidence {ev.id} anchor_hash not found in frozen buffer "
                    f"(hash {ev.anchor_hash})"
                )
        elif ev.kind == "source_comment":
            # Provenance must match a ledger entry for this lineage + version.
            # Parse the version from the SRC id: "SRC:target:C42" → "target".
            parts = ev.id.split(":", 2)
            if len(parts) >= 2:
                version = parts[1]
                if (packet.claim.lineage_id, version) not in ledger_versions:
                    errors.append(
                        f"source_comment evidence {ev.id} provenance '{version}' "
                        f"not in ledger for lineage {packet.claim.lineage_id}"
                    )
    # The candidate comment must not be cited as support for itself.
    claim_text_norm = " ".join(packet.claim.text.lower().split())
    for ev in packet.evidence:
        ev_text_norm = " ".join(ev.text.lower().split())
        if ev_text_norm == claim_text_norm and ev.kind != "source_comment":
            errors.append(
                f"evidence {ev.id} cites the candidate comment as support for itself"
            )
    return errors


def packet_to_dict(packet: EvidencePacket) -> dict:
    """Serialize an EvidencePacket to a JSON-friendly dict (for the juror prompt)."""
    return {
        "claim": {
            "claim_id": packet.claim.claim_id,
            "lineage_id": packet.claim.lineage_id,
            "text": packet.claim.text,
            "origin": packet.claim.origin,
            "kind": packet.claim.kind,
            "modality": packet.claim.modality,
        },
        "code_fingerprint": packet.code_fingerprint,
        "unit_id": packet.unit_id,
        "evidence": [
            {"id": ev.id, "kind": ev.kind, "text": ev.text,
             "provenance": ev.provenance, "anchor_hash": ev.anchor_hash,
             "result": ev.result}
            for ev in packet.evidence
        ],
    }


__all__ = [
    "EvidenceItem",
    "EvidencePacket",
    "build_evidence_packet",
    "validate_evidence_packet",
    "packet_to_dict",
]

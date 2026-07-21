"""Tests for the evidence packet builder + validator (Part SJ2, design §7)."""

from __future__ import annotations

from capybase.comment_claims import Claim
from capybase.comment_reconciler import LedgerEntry
from capybase.adapters.comment_classifier import CommentClass
from capybase.jury_evidence import (
    EvidencePacket, EvidenceItem,
    build_evidence_packet, validate_evidence_packet, packet_to_dict,
)


def _ledger(lineage_id, anchor, versions_and_texts):
    """Build a minimal ledger for testing."""
    out = []
    for version, text in versions_and_texts:
        out.append(LedgerEntry(
            lineage_id=lineage_id, version=version, text=text,
            cls=CommentClass.DEFERRED, start=0, end=len(text),
            anchor_symbol=anchor,
        ))
    return out


# ---------------------------------------------------------------------------
# Packet builder
# ---------------------------------------------------------------------------


def test_build_evidence_packet_includes_signature():
    """The packet includes the enclosing function's signature."""
    claim = Claim(claim_id="LC1.1", lineage_id="LC1", text="Retries errors",
                  origin="inherited_paraphrase", kind="error_behavior")
    code = "fn fetch(url: &str) -> Response {\n    do_fetch(url)\n}\n"
    ledger = _ledger("LC1", "function:fetch", [("base", "// retries"), ("current", "// retries v2")])
    packet = build_evidence_packet(claim, code, ledger, lang="rust")
    sigs = [e for e in packet.evidence if e.kind == "signature"]
    assert len(sigs) == 1
    assert "fetch" in sigs[0].text


def test_build_evidence_packet_includes_code_blocks_with_hashes():
    """Code evidence has anchor_hashes that validate against the frozen buffer."""
    claim = Claim(claim_id="LC1.1", lineage_id="LC1", text="Retries errors")
    code = "fn fetch() {\n    let x = 1;\n    let y = 2;\n    x + y\n}\n"
    ledger = _ledger("LC1", "function:fetch", [("base", "// base comment")])
    packet = build_evidence_packet(claim, code, ledger, lang="rust")
    code_evs = [e for e in packet.evidence if e.kind == "code"]
    assert len(code_evs) >= 1
    assert all(e.anchor_hash for e in code_evs)  # all have hashes


def test_build_evidence_packet_includes_source_comments():
    """Source-comment evidence carries the provenance variants."""
    claim = Claim(claim_id="LC1.1", lineage_id="LC1", text="Retries errors")
    code = "fn fetch() { 1 }\n"
    ledger = _ledger("LC1", "function:fetch",
                     [("base", "// base"), ("current", "// cur"), ("replayed", "// rep")])
    packet = build_evidence_packet(claim, code, ledger, lang="rust")
    src_evs = [e for e in packet.evidence if e.kind == "source_comment"]
    assert len(src_evs) == 3  # base + current + replayed
    versions = {e.provenance for e in src_evs}
    assert versions == {"base", "current", "replayed"}


def test_build_evidence_packet_includes_tests_when_supplied():
    """Test evidence is included when test_results is supplied."""
    claim = Claim(claim_id="LC1.1", lineage_id="LC1", text="Returns 42")
    code = "fn answer() { 42 }\n"
    ledger = _ledger("LC1", "function:answer", [("base", "// returns 42")])
    packet = build_evidence_packet(
        claim, code, ledger, lang="rust",
        test_results=[{"name": "test_answer", "result": "passed"}],
    )
    test_evs = [e for e in packet.evidence if e.kind == "test"]
    assert len(test_evs) == 1
    assert test_evs[0].result == "passed"


# ---------------------------------------------------------------------------
# Packet validator
# ---------------------------------------------------------------------------


def test_validate_evidence_packet_clean():
    """A well-formed packet with valid hashes → no errors."""
    claim = Claim(claim_id="LC1.1", lineage_id="LC1", text="Retries errors")
    code = "fn fetch() {\n    retry()\n}\n"
    ledger = _ledger("LC1", "function:fetch", [("base", "// retries")])
    packet = build_evidence_packet(claim, code, ledger, lang="rust")
    errors = validate_evidence_packet(packet, code, ledger)
    assert errors == [], f"expected no errors, got: {errors}"


def test_validate_evidence_packet_detects_bad_code_hash():
    """A code evidence with a hash not in the frozen buffer → error."""
    claim = Claim(claim_id="LC1.1", lineage_id="LC1", text="Retries errors")
    code = "fn fetch() {\n    retry()\n}\n"
    ledger = _ledger("LC1", "function:fetch", [("base", "// retries")])
    packet = build_evidence_packet(claim, code, ledger, lang="rust")
    # Tamper with a code evidence's hash.
    for ev in packet.evidence:
        if ev.kind == "code":
            ev.anchor_hash = "deadbeefdeadbeef"
            break
    errors = validate_evidence_packet(packet, code, ledger)
    assert any("not found in frozen buffer" in e for e in errors)


def test_validate_evidence_packet_detects_bad_provenance():
    """A source_comment with a version not in the ledger → error."""
    claim = Claim(claim_id="LC1.1", lineage_id="LC1", text="Retries errors")
    code = "fn fetch() { 1 }\n"
    ledger = _ledger("LC1", "function:fetch", [("base", "// retries")])
    packet = build_evidence_packet(claim, code, ledger, lang="rust")
    # Inject a source_comment with a fake version.
    packet.evidence.append(EvidenceItem(
        id="SRC:fake_version:LC1", kind="source_comment",
        text="// fake", provenance="fake_version",
    ))
    errors = validate_evidence_packet(packet, code, ledger)
    assert any("not in ledger" in e for e in errors)


def test_validate_evidence_packet_detects_self_citation():
    """The candidate comment cited as its own support → error."""
    claim = Claim(claim_id="LC1.1", lineage_id="LC1", text="Retries errors")
    code = "fn fetch() { 1 }\n"
    ledger = _ledger("LC1", "function:fetch", [("base", "// retries")])
    packet = build_evidence_packet(claim, code, ledger, lang="rust")
    # Inject a code evidence whose text IS the claim text.
    packet.evidence.append(EvidenceItem(
        id="CODE:self", kind="code", text="Retries errors", anchor_hash="",
    ))
    errors = validate_evidence_packet(packet, code, ledger)
    assert any("support for itself" in e for e in errors)


def test_packet_to_dict_serializable():
    """packet_to_dict produces a JSON-serializable dict for the juror prompt."""
    import json
    claim = Claim(claim_id="LC1.1", lineage_id="LC1", text="Retries errors")
    code = "fn fetch() { 1 }\n"
    ledger = _ledger("LC1", "function:fetch", [("base", "// retries")])
    packet = build_evidence_packet(claim, code, ledger, lang="rust")
    d = packet_to_dict(packet)
    # Must be JSON-serializable.
    json.dumps(d)
    assert d["claim"]["claim_id"] == "LC1.1"
    assert len(d["evidence"]) >= 1

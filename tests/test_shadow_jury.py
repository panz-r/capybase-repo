"""Tests for the shadow jury: jurors + deterministic chair (SJ3+SJ4+SJ5)."""

from __future__ import annotations

import json

from capybase.comment_claims import Claim
from capybase.comment_reconciler import LedgerEntry
from capybase.adapters.comment_classifier import CommentClass
from capybase.jury_evidence import build_evidence_packet
from capybase.shadow_jury import (
    JurorVerdict, ContradictionJuror, ProvenanceJuror, DeterministicChair,
    RoutingDecision, parse_juror_verdict, build_contradiction_prompt,
    build_provenance_prompt, VERDICTS,
)


# ---------------------------------------------------------------------------
# Verdict parsing
# ---------------------------------------------------------------------------


def test_parse_juror_verdict_valid():
    raw = json.dumps({
        "claim_id": "LC1.1", "verdict": "CONTRADICTED",
        "subtype": "OVERBROAD_QUANTIFIER",
        "evidence_ids": ["CODE:fetch:L10-15"],
        "witness": {"precondition": "auth fails", "observed_behavior": "returns immediately"},
        "confidence_band": "high", "explanation": "the claim says all but code shows auth exception",
    })
    v = parse_juror_verdict(raw, "contradiction")
    assert v is not None
    assert v.verdict == "CONTRADICTED"
    assert v.subtype == "OVERBROAD_QUANTIFIER"
    assert v.witness is not None
    assert v.juror == "contradiction"


def test_parse_juror_verdict_invalid_verdict_falls_back():
    """An invalid verdict string maps to NON_CHECKABLE (safest abstention)."""
    raw = json.dumps({"claim_id": "LC1.1", "verdict": "MAYBE_WRONG"})
    v = parse_juror_verdict(raw, "contradiction")
    assert v.verdict == "NON_CHECKABLE"


def test_parse_juror_verdict_garbage_falls_back_safely():
    """Garbage input → a safe NON_CHECKABLE verdict (not None, not a wrong action).

    The lenient parser salvages an empty dict from garbage; parse_juror_verdict
    maps the missing verdict to NON_CHECKABLE (the safest abstention). This is
    the graceful-degrade contract — a malformed response never produces a wrong
    action, just an abstention."""
    v = parse_juror_verdict("this is not json at all", "contradiction")
    assert v is not None
    assert v.verdict == "NON_CHECKABLE"


def test_parse_juror_verdict_normalizes_bracketed_evidence_ids():
    """Small models (e.g. gemma-4-e4b) occasionally wrap evidence IDs in square
    brackets — ``[SRC:base:LC1]`` instead of ``SRC:base:LC1``. The parser
    strips a single wrapping pair so the EnforcementRouter's evidence-reference
    resolver can match them (otherwise the bracketed string is unresolvable → a
    false fail-closed human_review). Observed in the gemma-4-e4b live run:
    2 of 26 evidence IDs were bracketed."""
    raw = json.dumps({
        "claim_id": "LC1.1", "verdict": "SUPPORTED",
        "evidence_ids": ["[SRC:base:LC1]", "[SRC:resolved:LC1]", "CODE:f:L1-5"],
    })
    v = parse_juror_verdict(raw, "contradiction")
    assert v.evidence_ids == ["SRC:base:LC1", "SRC:resolved:LC1", "CODE:f:L1-5"]


def test_normalize_evidence_id_only_strips_wrapping_pair():
    """Only a single wrapping bracket pair is stripped; internal brackets
    (none in the ID scheme, but defensive) are left alone."""
    from capybase.shadow_jury import _normalize_evidence_id
    assert _normalize_evidence_id("[SRC:base:LC1]") == "SRC:base:LC1"
    assert _normalize_evidence_id("SRC:base:LC1") == "SRC:base:LC1"
    assert _normalize_evidence_id(" [SRC:base:LC1] ") == "SRC:base:LC1"
    assert _normalize_evidence_id("[]") == ""
    assert _normalize_evidence_id("CODE:f:[L1]") == "CODE:f:[L1]"  # internal kept


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def test_contradiction_prompt_has_core_instructions():
    """The prompt states the §14 evidence-auditor constraints."""
    claim = Claim(claim_id="LC1.1", lineage_id="LC1", text="Retries all errors")
    packet = build_evidence_packet(claim, "fn f() { 1 }", [], lang="rust")
    prompt = build_contradiction_prompt(packet)
    assert "evidence auditor" in prompt.lower()
    assert "untrusted data" in prompt.lower()
    assert "concrete witness" in prompt.lower()
    assert "OVERBROAD_QUANTIFIER" in prompt  # subtype listed


def test_provenance_prompt_has_grounding_questions():
    claim = Claim(claim_id="LC1.1", lineage_id="LC1", text="Uses exponential backoff")
    packet = build_evidence_packet(claim, "fn f() { 1 }", [], lang="rust")
    prompt = build_provenance_prompt(packet)
    assert "PROVENANCE" in prompt
    assert "synthesized" in prompt
    assert "UNGROUNDED_NEW_CLAIM" in prompt


# ---------------------------------------------------------------------------
# Jurors (with a stub complete callable)
# ---------------------------------------------------------------------------


def _stub_complete(response: str):
    def _complete(prompt: str) -> str:
        return response
    return _complete


def test_contradiction_juror_judges():
    claim = Claim(claim_id="LC1.1", lineage_id="LC1", text="Retries all errors")
    ledger = [LedgerEntry(lineage_id="LC1", version="base", text="// retries errors",
                          cls=CommentClass.DEFERRED, start=0, end=20,
                          anchor_symbol="function:f")]
    packet = build_evidence_packet(claim, "fn f() {\n    retry()\n}\n", ledger, lang="rust")
    response = json.dumps({
        "claim_id": "LC1.1", "verdict": "CONTRADICTED",
        "subtype": "OVERBROAD_QUANTIFIER",
        "evidence_ids": ["CODE:f:L1-3"],
        "witness": {"precondition": "auth fails", "observed_behavior": "returns immediately"},
    })
    juror = ContradictionJuror(_stub_complete(response))
    v = juror.judge(packet)
    assert v is not None
    assert v.verdict == "CONTRADICTED"


def test_provenance_juror_judges():
    claim = Claim(claim_id="LC1.1", lineage_id="LC1", text="Uses exponential backoff",
                  origin="synthesized")
    ledger = [LedgerEntry(lineage_id="LC1", version="base", text="// retries errors",
                          cls=CommentClass.DEFERRED, start=0, end=20,
                          anchor_symbol="function:f")]
    packet = build_evidence_packet(claim, "fn f() { 1 }", ledger, lang="rust")
    response = json.dumps({
        "claim_id": "LC1.1", "verdict": "UNGROUNDED_NEW_CLAIM",
        "evidence_ids": [], "explanation": "no backoff in code or sources",
    })
    juror = ProvenanceJuror(_stub_complete(response))
    v = juror.judge(packet)
    assert v is not None
    assert v.verdict == "UNGROUNDED_NEW_CLAIM"


def test_juror_returns_none_on_model_failure():
    claim = Claim(claim_id="LC1.1", lineage_id="LC1", text="x")
    packet = build_evidence_packet(claim, "fn f() { 1 }", [], lang="rust")
    def _raise(prompt): raise RuntimeError("connection refused")
    juror = ContradictionJuror(_raise)
    assert juror.judge(packet) is None


# ---------------------------------------------------------------------------
# Deterministic chair — the §9 routing matrix
# ---------------------------------------------------------------------------


def test_chair_synthesized_supported_accepts():
    """Synthesized + SUPPORTED → accept."""
    chair = DeterministicChair(shadow_mode=False)
    claim = Claim(claim_id="C1", lineage_id="LC1", text="x", origin="synthesized", kind="implementation_description")
    c = JurorVerdict(claim_id="C1", verdict="SUPPORTED", juror="contradiction")
    p = JurorVerdict(claim_id="C1", verdict="SUPPORTED", juror="provenance")
    packet = build_evidence_packet(claim, "fn f() { 1 }", [], lang="rust")
    d = chair.route(claim, c, p, packet)
    assert d.route == "accept"


def test_chair_synthesized_ungrounded_comment_counterexample():
    """Synthesized + UNGROUNDED_NEW_CLAIM → comment counterexample."""
    chair = DeterministicChair(shadow_mode=False)
    claim = Claim(claim_id="C1", lineage_id="LC1", text="x", origin="synthesized")
    c = JurorVerdict(claim_id="C1", verdict="SUPPORTED", juror="contradiction")
    p = JurorVerdict(claim_id="C1", verdict="UNGROUNDED_NEW_CLAIM", juror="provenance")
    packet = build_evidence_packet(claim, "fn f() { 1 }", [], lang="rust")
    d = chair.route(claim, c, p, packet)
    assert d.route == "comment_counterexample"


def test_chair_inherited_contract_contradicted_code_reopen():
    """Inherited high-trust invariant + CONTRADICTED with quorum → code_reopen."""
    chair = DeterministicChair(shadow_mode=False)
    claim = Claim(claim_id="C1", lineage_id="LC1", text="MUST not retry auth",
                  origin="inherited_exact", kind="invariant")
    c = JurorVerdict(
        claim_id="C1", verdict="CONTRADICTED", subtype="WRONG_POLARITY",
        evidence_ids=["CODE:f:L1-3", "TEST:t1"], juror="contradiction",
        witness={"precondition": "auth fails", "observed_behavior": "retries"},
    )
    p = JurorVerdict(claim_id="C1", verdict="SUPPORTED", juror="provenance")  # inherited, so supported
    packet = build_evidence_packet(claim, "fn f() {\n    retry()\n}\n", [], lang="rust")
    d = chair.route(claim, c, p, packet)
    assert d.route == "code_reopen"
    assert d.evidence_quorum_met is True


def test_chair_synthesized_never_reopens_code():
    """§3 hard invariant: synthesized claim NEVER reopens code, even if CONTRADICTED."""
    chair = DeterministicChair(shadow_mode=False)
    claim = Claim(claim_id="C1", lineage_id="LC1", text="MUST not retry auth",
                  origin="synthesized", kind="invariant")  # synthesized!
    c = JurorVerdict(
        claim_id="C1", verdict="CONTRADICTED", evidence_ids=["CODE:f:L1-3"],
        juror="contradiction",
    )
    p = JurorVerdict(claim_id="C1", verdict="SUPPORTED", juror="provenance")
    packet = build_evidence_packet(claim, "fn f() { 1 }", [], lang="rust")
    d = chair.route(claim, c, p, packet)
    assert d.route == "comment_counterexample"  # downgraded, NOT code_reopen


def test_chair_unverifiable_never_reopens_code():
    """§9 hard invariant: UNVERIFIABLE NEVER reopens code."""
    chair = DeterministicChair(shadow_mode=False)
    claim = Claim(claim_id="C1", lineage_id="LC1", text="Requires separate writes",
                  origin="inherited_exact", kind="rationale")
    c = JurorVerdict(claim_id="C1", verdict="UNVERIFIABLE_INHERITED_CLAIM", juror="contradiction")
    p = JurorVerdict(claim_id="C1", verdict="UNVERIFIABLE_INHERITED_CLAIM", juror="provenance")
    packet = build_evidence_packet(claim, "fn f() { 1 }", [], lang="rust")
    d = chair.route(claim, c, p, packet)
    assert d.route == "preserve_and_audit"  # NOT code_reopen


def test_chair_shadow_mode_records_decision():
    """In shadow mode, every route becomes shadow_record (no merge effect)."""
    chair = DeterministicChair(shadow_mode=True)
    claim = Claim(claim_id="C1", lineage_id="LC1", text="x", origin="synthesized")
    c = JurorVerdict(claim_id="C1", verdict="SUPPORTED", juror="contradiction")
    p = JurorVerdict(claim_id="C1", verdict="UNGROUNDED_NEW_CLAIM", juror="provenance")
    packet = build_evidence_packet(claim, "fn f() { 1 }", [], lang="rust")
    d = chair.route(claim, c, p, packet)
    assert d.route == "shadow_record"
    assert "[SHADOW]" in d.reason
    assert "comment_counterexample" in d.reason  # records what it WOULD do


def test_chair_code_reopen_blocked_without_quorum():
    """code_reopen requires evidence quorum (≥1 contradiction citing CODE/TEST evidence)."""
    chair = DeterministicChair(shadow_mode=False)
    claim = Claim(claim_id="C1", lineage_id="LC1", text="MUST not retry",
                  origin="inherited_exact", kind="invariant")
    # Contradiction cites only a source comment (no executable evidence) → no quorum.
    c = JurorVerdict(
        claim_id="C1", verdict="CONTRADICTED",
        evidence_ids=["SRC:base:LC1"], juror="contradiction",
    )
    p = JurorVerdict(claim_id="C1", verdict="SUPPORTED", juror="provenance")
    packet = build_evidence_packet(claim, "fn f() { 1 }", [], lang="rust")
    d = chair.route(claim, c, p, packet)
    assert d.route == "comment_counterexample"  # downgraded (no executable evidence)
    assert d.evidence_quorum_met is False


def test_chair_inherited_rationale_unverifiable_preserved():
    """Inherited rationale + UNVERIFIABLE → preserve_and_audit (not deleted)."""
    chair = DeterministicChair(shadow_mode=False)
    claim = Claim(claim_id="C1", lineage_id="LC1", text="Required by the remote protocol",
                  origin="inherited_exact", kind="rationale")
    c = JurorVerdict(claim_id="C1", verdict="UNVERIFIABLE_INHERITED_CLAIM", juror="contradiction")
    p = JurorVerdict(claim_id="C1", verdict="UNVERIFIABLE_INHERITED_CLAIM", juror="provenance")
    packet = build_evidence_packet(claim, "fn f() { 1 }", [], lang="rust")
    d = chair.route(claim, c, p, packet)
    assert d.route == "preserve_and_audit"


def test_chair_inherited_ungrounded_routes_to_human_review():
    """Inherited claim + UNGROUNDED_NEW_CLAIM → human_review (NOT accept).

    The origin classifier marked this claim inherited, but the provenance juror
    could not ground it in any source variant — a provenance/origin
    disagreement. The claim's actual provenance is ambiguous, so route to human
    review rather than accepting a potentially-synthesized clause on the
    inherited origin's trust. Previously this fell through to the NON_CHECKABLE
    'accept' branch (a routing-matrix gap surfaced by the gemma-4-e4b live run:
    zenodo-hdiff-0097/LC1.1)."""
    chair = DeterministicChair(shadow_mode=False)
    claim = Claim(claim_id="C1", lineage_id="LC1", text="some ungrounded claim",
                  origin="inherited_paraphrase", kind="implementation_description")
    c = JurorVerdict(claim_id="C1", verdict="NON_CHECKABLE", juror="contradiction")
    p = JurorVerdict(claim_id="C1", verdict="UNGROUNDED_NEW_CLAIM", juror="provenance")
    packet = build_evidence_packet(claim, "fn f() { 1 }", [], lang="rust")
    d = chair.route(claim, c, p, packet)
    assert d.route == "human_review"

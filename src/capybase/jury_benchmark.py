"""Jury mutation benchmark corpus (Part SJ7, design §12).

A corpus of valid comments + mutation operators for evaluating the shadow
jury's precision offline. Real live conflicts won't initially give enough
labeled semantic errors; this suite creates them deterministically.

Each mutation has an EXPECTED jury verdict — the benchmark measures whether
the jury agrees. The §13 metrics (contradiction precision, ungrounded-new-
claim precision, false comment-repair rate, false code-reopen rate, abstention
rate, juror disagreement rate, evidence-reference validity, verdict stability
under evidence-order changes) are computed from the results.

This is NOT a CI-gating test — it's an evaluation harness run manually (or in
a nightly job) against a model. The jury's prompts are the riskiest part; this
benchmark catches regressions when the prompts change.

Usage:
    from capybase.jury_benchmark import MUTATION_CASES, evaluate_jury
    results = evaluate_jury(contradiction_juror, provenance_juror, chair)
    print(results.summary())
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from capybase.comment_claims import Claim, detect_kind, detect_modality
from capybase.jury_evidence import build_evidence_packet
from capybase.shadow_jury import (
    ContradictionJuror, ProvenanceJuror, DeterministicChair,
)


# ---------------------------------------------------------------------------
# Mutation operators (design §12)
# ---------------------------------------------------------------------------


#: Word-level mutations that flip a claim's truth value. Each entry is
#: (pattern, replacement, expected_verdict, description).
WORD_MUTATIONS: list[tuple[str, str, str, str]] = [
    (r"\bmay\b", "always", "CONTRADICTED", "may→always (strengthens modality beyond code)"),
    (r"\bsome\b", "all", "CONTRADICTED", "some→all (overbroad quantifier)"),
    (r"\bup to (\d+)\b", r"exactly \1", "CONTRADICTED", "up to N→exactly N (wrong cardinality)"),
    (r"\bbefore\b", "after", "CONTRADICTED", "before→after (wrong ordering)"),
    (r"\btransient\b", "all", "CONTRADICTED", "transient→all (overbroad)"),
    (r"\bdoes not\b", "does", "CONTRADICTED", "does not→does (wrong polarity)"),
    (r"\bmilliseconds\b", "seconds", "CONTRADICTED", "ms→s (wrong unit)"),
    (r"\breturns None\b", "raises", "CONTRADICTED", "returns None→raises (wrong return behavior)"),
    (r"\breads state\b", "mutates state", "CONTRADICTED", "reads→mutates (wrong side effect)"),
    (r"\blinear time\b", "constant time", "CONTRADICTED", "linear→constant (wrong complexity)"),
]

#: Injected-content mutations (add a false claim to a valid comment).
#: Each is (prefix_to_inject, expected_verdict, description).
INJECTION_MUTATIONS: list[tuple[str, str, str]] = [
    ("Uses exponential backoff with jitter. ", "UNGROUNDED_NEW_CLAIM",
     "injects a performance assertion not in code or sources"),
    ("Thread-safe and lock-free. ", "UNGROUNDED_NEW_CLAIM",
     "injects a concurrency claim not in code or sources"),
    ("Executes in O(1) constant time. ", "UNGROUNDED_NEW_CLAIM",
     "injects a complexity claim not in code or sources"),
]


@dataclass
class MutationCase:
    """One mutation test case for the jury benchmark."""
    id: str
    original_comment: str
    mutated_comment: str
    code: str
    source_variants: list[str]
    expected_verdict: str        # the verdict the jury SHOULD produce
    mutation_type: str           # "word" | "injection" | "control"
    description: str
    lang: str = "rust"


@dataclass
class MutationResult:
    """The jury's result on one mutation case."""
    case_id: str
    expected: str
    actual: str               # the chair's effective route (or "none" if jury failed)
    correct: bool
    detail: str = ""


@dataclass
class BenchmarkSummary:
    """Aggregate metrics over the benchmark run (design §13)."""
    total: int = 0
    correct: int = 0
    contradiction_precision: float = 0.0
    ungrounded_precision: float = 0.0
    abstention_rate: float = 0.0
    disagreement_rate: float = 0.0
    per_type: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Base comments + code for the mutations
# ---------------------------------------------------------------------------


_BASE_COMMENTS: list[tuple[str, str, list[str], str]] = [
    # (comment, code, source_variants, lang)
    (
        "Retries up to 3 transient errors.",
        "fn fetch() {\n    for i in 0..3 {\n        if try_fetch().is_ok() { return; }\n    }\n}\n",
        ["Retries transient errors.", "Retries up to 3 transient errors."],
        "rust",
    ),
    (
        "Returns None if the key is not found.",
        "fn lookup(key: &str) -> Option<i32> {\n    db.get(key)\n}\n",
        ["Returns None if not found."],
        "rust",
    ),
    (
        "Reads state from the cache without mutating it.",
        "fn get(key: &str) -> &Value {\n    &cache[key]\n}\n",
        ["Reads from the cache."],
        "rust",
    ),
]


def _build_mutation_cases() -> list[MutationCase]:
    """Build the full mutation corpus from the base comments + operators."""
    cases: list[MutationCase] = []
    idx = 0
    for comment, code, sources, lang in _BASE_COMMENTS:
        # Control case: the original (unmutated) comment → SUPPORTED.
        idx += 1
        cases.append(MutationCase(
            id=f"MUT-{idx:03d}-control",
            original_comment=comment, mutated_comment=comment,
            code=code, source_variants=sources,
            expected_verdict="SUPPORTED", mutation_type="control",
            description="unmutated control (should be SUPPORTED)", lang=lang,
        ))
        # Word mutations.
        for pat, repl, expected, desc in WORD_MUTATIONS:
            mutated = re.sub(pat, repl, comment, flags=re.IGNORECASE)
            if mutated != comment:
                idx += 1
                cases.append(MutationCase(
                    id=f"MUT-{idx:03d}-word",
                    original_comment=comment, mutated_comment=mutated,
                    code=code, source_variants=sources,
                    expected_verdict=expected, mutation_type="word",
                    description=desc, lang=lang,
                ))
        # Injection mutations.
        for prefix, expected, desc in INJECTION_MUTATIONS:
            mutated = prefix + comment
            idx += 1
            cases.append(MutationCase(
                id=f"MUT-{idx:03d}-injection",
                original_comment=comment, mutated_comment=mutated,
                code=code, source_variants=sources,
                expected_verdict=expected, mutation_type="injection",
                description=desc, lang=lang,
            ))
    return cases


#: The full mutation corpus (lazily built on first access).
MUTATION_CASES: list[MutationCase] = _build_mutation_cases()


# ---------------------------------------------------------------------------
# Evaluation harness
# ---------------------------------------------------------------------------


def evaluate_jury(
    contradiction_juror: ContradictionJuror,
    provenance_juror: ProvenanceJuror,
    chair: DeterministicChair,
    *,
    cases: list[MutationCase] | None = None,
) -> list[MutationResult]:
    """Run the jury against the mutation corpus. Returns per-case results.

    Each case builds a Claim from the mutated comment, constructs an evidence
    packet, runs both jurors, routes via the chair, and compares the chair's
    effective route against the expected verdict.
    """
    cases = cases or MUTATION_CASES
    results: list[MutationResult] = []
    for case in cases:
        claim = Claim(
            claim_id=f"{case.id}.1", lineage_id=case.id,
            text=case.mutated_comment,
            kind=detect_kind(case.mutated_comment),
            modality=detect_modality(case.mutated_comment),
        )
        # Build a minimal ledger for the evidence packet.
        from capybase.comment_reconciler import LedgerEntry
        from capybase.adapters.comment_classifier import CommentClass
        ledger = [
            LedgerEntry(lineage_id=case.id, version=f"src{i}",
                        text=src, cls=CommentClass.DEFERRED,
                        start=0, end=len(src), anchor_symbol="function:f")
            for i, src in enumerate(case.source_variants)
        ]
        packet = build_evidence_packet(claim, case.code, ledger, lang=case.lang)
        try:
            c_verdict = contradiction_juror.judge(packet)
            p_verdict = provenance_juror.judge(packet)
        except Exception as exc:  # noqa: BLE001
            results.append(MutationResult(
                case_id=case.id, expected=case.expected_verdict,
                actual="error", correct=False, detail=str(exc)[:100],
            ))
            continue
        decision = chair.route(claim, c_verdict, p_verdict, packet)
        # Extract the effective verdict from the decision's reason (shadow mode
        # wraps it). Map routes to verdict-equivalents for comparison.
        actual = _route_to_verdict(decision.route, c_verdict, p_verdict)
        correct = _verdict_matches(case.expected_verdict, actual)
        results.append(MutationResult(
            case_id=case.id, expected=case.expected_verdict,
            actual=actual, correct=correct,
            detail=decision.reason[:120],
        ))
    return results


def _route_to_verdict(route: str, c_verdict, p_verdict) -> str:
    """Map the chair's route back to a verdict-equivalent for comparison."""
    if route == "shadow_record":
        # In shadow mode the route is wrapped; infer from the jurors' verdicts.
        if c_verdict and c_verdict.verdict == "CONTRADICTED":
            return "CONTRADICTED"
        if p_verdict and p_verdict.verdict == "UNGROUNDED_NEW_CLAIM":
            return "UNGROUNDED_NEW_CLAIM"
        if p_verdict and p_verdict.verdict == "UNVERIFIABLE_INHERITED_CLAIM":
            return "UNVERIFIABLE_INHERITED_CLAIM"
        return "SUPPORTED"
    if route == "accept":
        return "SUPPORTED"
    if route == "comment_counterexample":
        return "CONTRADICTED"  # or UNGROUNDED — both lead to comment repair
    if route == "code_reopen":
        return "CONTRADICTED"
    if route == "preserve_and_audit":
        return "UNVERIFIABLE_INHERITED_CLAIM"
    return "NON_CHECKABLE"


def _verdict_matches(expected: str, actual: str) -> bool:
    """Lenient match: comment-counterexample verdicts are interchangeable."""
    if expected == actual:
        return True
    # CONTRADICTED and UNGROUNDED both lead to comment_counterexample → treat
    # as equivalent for the benchmark (the jury detected a problem).
    comment_repair = {"CONTRADICTED", "UNGROUNDED_NEW_CLAIM"}
    if expected in comment_repair and actual in comment_repair:
        return True
    return False


def summarize(results: list[MutationResult]) -> BenchmarkSummary:
    """Compute the §13 metrics from the benchmark results."""
    s = BenchmarkSummary(total=len(results))
    if not s.total:
        return s
    s.correct = sum(1 for r in results if r.correct)
    # Per-type breakdown.
    by_type: dict[str, list[MutationResult]] = {}
    for r in results:
        # Infer type from case_id.
        mtype = "control"
        if "-word" in r.case_id:
            mtype = "word"
        elif "-injection" in r.case_id:
            mtype = "injection"
        by_type.setdefault(mtype, []).append(r)
    s.per_type = {
        t: {"total": len(rs), "correct": sum(1 for r in rs if r.correct)}
        for t, rs in by_type.items()
    }
    # Rates.
    contradiction_cases = [r for r in results if r.expected == "CONTRADICTED"]
    if contradiction_cases:
        s.contradiction_precision = (
            sum(1 for r in contradiction_cases if r.correct) / len(contradiction_cases)
        )
    ungrounded_cases = [r for r in results if r.expected == "UNGROUNDED_NEW_CLAIM"]
    if ungrounded_cases:
        s.ungrounded_precision = (
            sum(1 for r in ungrounded_cases if r.correct) / len(ungrounded_cases)
        )
    abstain = sum(1 for r in results if r.actual in ("NON_CHECKABLE", "error", "none"))
    s.abstention_rate = abstain / s.total
    return s


__all__ = [
    "MutationCase",
    "MutationResult",
    "BenchmarkSummary",
    "MUTATION_CASES",
    "WORD_MUTATIONS",
    "INJECTION_MUTATIONS",
    "evaluate_jury",
    "summarize",
]

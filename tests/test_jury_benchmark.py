"""Tests for the jury mutation benchmark corpus (Part SJ7, design §12)."""

from __future__ import annotations

import json

from capybase.jury_benchmark import (
    MUTATION_CASES, WORD_MUTATIONS, INJECTION_MUTATIONS,
    evaluate_jury, summarize, MutationCase,
)
from capybase.shadow_jury import (
    ContradictionJuror, ProvenanceJuror, DeterministicChair,
)


def test_mutation_corpus_built():
    """The corpus builds from the base comments + operators."""
    assert len(MUTATION_CASES) > 0
    # Has control + word + injection cases.
    types = set()
    for c in MUTATION_CASES:
        if "-control" in c.id:
            types.add("control")
        elif "-word" in c.id:
            types.add("word")
        elif "-injection" in c.id:
            types.add("injection")
    assert types == {"control", "word", "injection"}


def test_control_cases_expect_supported():
    """Control cases (unmutated) expect SUPPORTED."""
    controls = [c for c in MUTATION_CASES if c.mutation_type == "control"]
    assert len(controls) >= 1
    assert all(c.expected_verdict == "SUPPORTED" for c in controls)


def test_word_mutations_expect_contradicted():
    """Word mutations expect CONTRADICTED (they flip the claim's truth)."""
    word_cases = [c for c in MUTATION_CASES if c.mutation_type == "word"]
    assert len(word_cases) >= 1
    assert all(c.expected_verdict == "CONTRADICTED" for c in word_cases)


def test_injection_mutations_expect_ungrounded():
    """Injection mutations expect UNGROUNDED_NEW_CLAIM."""
    injection_cases = [c for c in MUTATION_CASES if c.mutation_type == "injection"]
    assert len(injection_cases) >= 1
    assert all(c.expected_verdict == "UNGROUNDED_NEW_CLAIM" for c in injection_cases)


def test_mutation_corpus_mutated_differs_from_original():
    """Each mutated comment differs from its original (the mutation applied)."""
    for c in MUTATION_CASES:
        if c.mutation_type != "control":
            assert c.mutated_comment != c.original_comment, f"{c.id}: mutation didn't apply"


def test_evaluate_jury_with_stub_jurors():
    """The evaluation harness runs end-to-end with stub jurors."""
    # Stub that always returns SUPPORTED (the control verdict).
    def _stub_supported(prompt):
        return json.dumps({"verdict": "SUPPORTED", "evidence_ids": []})

    c_juror = ContradictionJuror(_stub_supported)
    p_juror = ProvenanceJuror(_stub_supported)
    chair = DeterministicChair(shadow_mode=True)

    # Run on a small subset to keep the test fast.
    cases = MUTATION_CASES[:5]
    results = evaluate_jury(c_juror, p_juror, chair, cases=cases)
    assert len(results) == 5
    # The stub always says SUPPORTED → controls are correct, mutations are wrong.
    control_results = [r for r in results if "-control" in r.case_id]
    assert all(r.correct for r in control_results)


def test_summarize_computes_metrics():
    """summarize produces the §13 metrics."""
    from capybase.jury_benchmark import MutationResult
    results = [
        MutationResult("MUT-001-control", "SUPPORTED", "SUPPORTED", True),
        MutationResult("MUT-002-word", "CONTRADICTED", "CONTRADICTED", True),
        MutationResult("MUT-003-word", "CONTRADICTED", "SUPPORTED", False),
        MutationResult("MUT-004-injection", "UNGROUNDED_NEW_CLAIM", "UNGROUNDED_NEW_CLAIM", True),
    ]
    s = summarize(results)
    assert s.total == 4
    assert s.correct == 3
    assert s.contradiction_precision == 0.5  # 1/2 word cases correct
    assert s.ungrounded_precision == 1.0     # 1/1 injection correct
    assert "control" in s.per_type
    assert "word" in s.per_type

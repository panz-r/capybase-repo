"""Unit tests for RepairOscillationTracker (whole-file repair cycle stop).

The d40d105a flight (sqlite-0040) spun 1,221 deterministic repair cycles —
two repairs undoing each other (A→B→A→B…) — until the time budget died.
The retry-count budget bounds the spin; the tracker stops it semantically,
which matters for production configs with higher repair budgets.
"""

from __future__ import annotations

from dataclasses import dataclass

from capybase.orchestrator import RepairOscillationTracker


@dataclass
class _Fail:
    message: str


@dataclass
class _Cand:
    provenance: str


@dataclass
class _Unit:
    pass


def _sig(*msgs: str) -> tuple[str, ...]:
    return RepairOscillationTracker.signature([_Fail(m) for m in msgs])


def test_signature_is_order_independent():
    a = _sig("brace imbalance at 10", "marker at 3")
    b = _sig("marker at 3", "brace imbalance at 10")
    assert a == b


def test_deterministic_round_is_tracked_and_cycles():
    t = RepairOscillationTracker()
    sig_a = _sig("error X")
    # Round 1 from state A: deterministic-only repair (a candidate whose
    # provenance starts with "deterministic").
    t.record([(_Unit(), _Cand("deterministic_brace"))], sig_a)
    assert not t.is_cycle(_sig("error Y"))
    # The loop returned to state A — a deterministic repeat is guaranteed.
    assert t.is_cycle(sig_a)


def test_model_round_is_not_tracked():
    """A model repair is stochastic: returning to the same signature does
    NOT mean the next model attempt repeats it."""
    t = RepairOscillationTracker()
    sig_a = _sig("error X")
    t.record([(_Unit(), _Cand("plain_llm"))], sig_a)
    assert not t.is_cycle(sig_a)


def test_mixed_round_is_not_tracked():
    """A round containing ANY model candidate is not deterministic — even
    alongside deterministic candidates."""
    t = RepairOscillationTracker()
    sig_a = _sig("error X")
    t.record(
        [(_Unit(), _Cand("deterministic_symbol_inject")),
         (_Unit(), _Cand("plain_llm"))],
        sig_a,
    )
    assert not t.is_cycle(sig_a)


def test_ab_alternation_detected_on_return_to_a():
    """The d40d105a shape: A → (det repair) → B → (det repair) → A."""
    t = RepairOscillationTracker()
    sig_a, sig_b = _sig("error at line 1: EXTERN unknown"), _sig("expected ';'")
    t.record([(_Unit(), _Cand("deterministic_symbol_inject"))], sig_a)
    assert not t.is_cycle(sig_b)  # B is new
    t.record([(_Unit(), _Cand("deterministic_derived_proto"))], sig_b)
    assert t.is_cycle(sig_a)  # back to A — cycle


def test_empty_accepted_not_tracked():
    t = RepairOscillationTracker()
    sig_a = _sig("error X")
    t.record([], sig_a)
    assert not t.is_cycle(sig_a)


def test_empty_provenance_counts_as_model():
    """Matches the tiered-budget convention (the `not startswith` check):
    a candidate with no provenance string is treated as having cost a
    model call — conservative for cycle detection too."""
    t = RepairOscillationTracker()
    sig_a = _sig("error X")
    t.record([(_Unit(), _Cand(""))], sig_a)
    assert not t.is_cycle(sig_a)

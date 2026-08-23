# PLAN-LEDGER — Sprint 22 (living document, update as work proceeds)

Purpose: same durable-record discipline as prior ledgers. **Read this
first on resume.** All work on `dev`; never push (user's job).

## Context

Sprint-22 is the sharded harvest sprint: run each language shard,
analyze failures, implement improvements, launch the next shard with
the improvements. Shard 1 (Python) landed 88.3% (+3.6pp vs harvest).
Three external reviews of the failure report were collected; this
ledger records the synthesized improvement plan.

## Reviewer rating (recorded per the user's request)

**Reviewer 1 — most useful.** The provenance-aware resurrection guard
(check parent final states, not just "any reappeared content") is the
single best idea across all three: it's MORE precise, not a
relaxation — exactly capybase's philosophy. The insertion-within-
deletion salvage rule for flask-0006 is the correct deterministic
approach. Weak point: the "repair-by-example" line-substitution
mechanism risks subtle context mismatches.

**Reviewer 2 — strong on porting, but contains one dangerous
suggestion.** The AST skeleton intent override (accept PASS when
jaccard < 0.85 but skeleton match > 0.90) directly violates the
pre-registered decision: "skeleton-hash eval-only — never overrides
the compiler or production gates" (template §E, sprint-20). This is
metric gaming and is REJECTED. The whole-side compile probe for
Python is a good port of existing C++ machinery. The micro-CEGIS
suggestion is already implemented (S20.6).

**Reviewer 3 — best prioritization discipline.** The "what I would
NOT do" section is the most valuable: explicitly rejecting guard
weakening, global retry increases, and more few-shot examples. The
pre-resolution deletion context injection (prompt-level, no guard
change) is the safest approach to Class A. Decision-point
decomposition is interesting but too ambitious for this sprint.

## Standing constraints (carried forward)

- Compiler is authority; no silent wrong merge
- Skeleton-hash: eval-only, never a gate
- No partial-buffer compilation backtracking
- No heavy parser (clangd/rust-analyzer/tree-sitter)
- Conservative-by-construction escalation

## Improvement plan (from reviewer synthesis)

| # | Item | Source | Target | Effort | Status |
|---|------|--------|--------|--------|--------|
| P1 | SAFE_SKIP filter for no-conflict cases | all (R1#7, R2#5, R3) | zenodo-0010/0038 | 30m | ✅ DONE (93b61de) |
| P2 | Adaptive retry relaxation (high-sim + 1 failing unit) | R1#6, R2#4, R3#1 | zenodo-0012 | 1h | ✅ DONE (93b61de) |
| P3 | Asymmetric rewrite fast path (extreme-asymmetry trigger) | R1#3, R2#2, R3#C3 | zenodo-0044 | 2h | ✅ DONE (943b8d5) |
| P4 | Insertion-within-deletion salvage rule | R1#2, R2 (implicit), R3#5 | flask-0006 | 3h | ✅ DONE (943b8d5) |
| P5 | Provenance-aware resurrection guard | R1#1 (best idea) | zenodo-0063/0064 | 3h | TODO |
| P6 | Pre-resolution deletion context injection | R3#2 | Class A prevention | 3h | TODO |
| P7 | Journal archaeology: 0085's stubborn unit | R3#7 | zenodo-0085 | 2h | TODO |
| P8 | Live validation: P3 on zenodo-0044, P4 on flask-0006 | — | P3/P4 specimens | 30m | TODO (after shard 2) |
| P9 | Shard 2 (C) analysis + failure report | — | C failures | 1h | TODO (when shard 2 lands) |
| P10 | Shard 3 (Rust) launch with all P1-P7 improvements | — | 194 rust cases | — | TODO (after shard 2 analysis) |
| P11 | Shard 4 (C++) launch + analysis | — | 167 cpp cases | — | TODO |
| P12 | README results table from the tracker | — | all shards | 30m | TODO (at sprint close) |
| P13 | Sprint-22 results doc | — | complete record | 1h | TODO (at sprint close) |

### Explicitly rejected (with reasons)

- **AST skeleton intent override (R2#3)**: violates pre-registration;
  metric gaming; the eval-only constraint exists precisely to prevent
  "semantic correctness" from overriding the compiler
- **Decision-point decomposition (R3#C1)**: too ambitious for this
  sprint; partially overlaps the rejected incremental-splice
  architecture; deferred to **sprint-23** with calibration data
- **Repair-by-example line substitution (R1#4)**: substitution risks
  context mismatches the compiler can't catch (semantic drift);
  golden-path examples are for the model to learn from, not for
  automated copy-paste into outputs
- **Increasing few-shot examples for mid-band (R1#4b)**: R3's
  analysis is correct — the failure is decision-point ambiguity, not
  "what a merge looks like"; more examples won't fix it
- **Guard weakening / global retry increase**: R3's explicit rejection
  is correct; any relaxation must be provenance-gated (P5) or
  closeness-gated (P2), never unconditional

### Deferred to sprint-23 (with carry-forward context)

- **Decision-point decomposition** (R3's C1): resolve agreement regions
  deterministically, prompt the model only at divergence points. Needs
  calibration data from the sharded harvest to size the investment.
  Estimated 6-8h. The rejected-architecture concern (partial-buffer
  compilation) doesn't apply — this is prompt granularity, not
  verification granularity.
- **Skeleton-guided refinement** (R3's C2): after the model produces a
  merge, compare its skeleton to both sides' union; repair divergent
  elements. The skeleton extractor exists (S20.11); the repair prompt
  is new. Estimated 4-6h.
- **Golden-path structural fingerprint retrieval** (R1's C4 partial):
  index by (hunk count, churn ratio, entity type, dominance) instead
  of raw text. Worth revisiting after the sharded harvest data shows
  which shapes actually benefit from retrieval.
- **Era-corpus pinned toolchains**: carried from sprint-21's S21.4
  decision; revisit only if the resolver-side gap closes below ~5%.

## Work log (append, newest last)

- 2026-08-23 01:0x: ledger created from the three-reviewer synthesis.
- 2026-08-23 01:1x: **P1+P2 IMPLEMENTED** (93b61de): SAFE_SKIP for
  no-conflict cases (terminal_reason set, excluded from real-conflict
  denominator) + adaptive retry relaxation (no hard failures + only
  failing unit → one extra retry, journaled retry_relaxation).
- 2026-08-23 01:3x: **P3+P4 IMPLEMENTED** (943b8d5): extreme-asymmetry
  fast path (>5× line ratio + churn ≥0.95 → whole-side takeover,
  journaled extreme_asymmetry_gate) + insertion-within-deletion
  salvage (zone-based detection, deletion honored, self-contained
  insertion survives, dependent insertion declines). 147 tests green.
  P4's zone-based detection was a bring-up correction: the first
  version required one contiguous deletion block; flask-0006's shape
  has multiple gaps with surviving lines — the rewrite merges all
  deletion opcodes into a zone.
- 2026-08-23 06:5x: **SHARD 2 (C) COMPLETE — 40.5% raw, a -1.9pp
  regression that needs investigation.** 83/205 PASS vs harvest 87/205
  (42.4%). Era census 98 (harvest 97; redis-0038 newly classified).
  **9 flips**: 2 improvements (sqlite-0008/0014 → PASS — the coherence
  rung's perfect-buffer class converting, exactly as designed) vs
  **6 regressions (PASS → ESCALATE)** — the concerning signal:
  - jsonc-0007: splice coherence unbalanced braces (the rung fired but
    the repair failed?)
  - redis-0013/0014/0047: compile errors on cases that passed before
  - sqlite-0019: whole-file repair failure
  - sqlite-0039: expected identifier (compile)
  These 6 need journal analysis: are they (a) sampling variance on
  borderline cases, (b) regressions from P1-P4 changes, or (c) the
  golden-path memory layer surfacing different (worse) examples?
  The P9 analysis task is now the sprint's priority before shard 3.

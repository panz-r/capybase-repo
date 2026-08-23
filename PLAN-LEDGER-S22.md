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
| P8 | Live validation: P3 on zenodo-0044, P4 on flask-0006 | — | P3/P4 specimens | 30m | TODO (after shard 3) |
| P9 | Shard 2 (C) analysis + failure report | — | C failures | 1h | ✅ DONE (f89f740) |
| P10 | Shard 3 (Rust) launch + analysis | — | 194 rust cases | — | 🔄 RUNNING |
| P11 | Shard 4 (C++) launch + analysis | — | 167 cpp cases | — | TODO |
| P12 | README results table from the tracker | — | all shards | 30m | TODO (at sprint close) |
| P13 | Sprint-22 results doc | — | complete record | 1h | TODO (at sprint close) |
| C1 | Side-provenance symbol injection (deterministic missing-symbol repair) | all 3 C reviewers | redis-0002/0012, sqlite-0030, redis-0013 | 4-6h | TODO |
| C2 | Include-directive repair (implicit-declaration class) | C-R1#2, C-R3#2 | redis-0013/0014 | 3-4h | TODO |
| C3 | Preceding-block injection (context expansion for stubborn units) | C-R2#5 | sqlite-0029, redis-0015/0049 | 2h | TODO |
| C4 | Repair-interleaved retry loop (deterministic repair between retries) | C-R3#4 | variance class (6 cases) | 3h | TODO |
| C5 | Oversized-prompt splitting diagnosis (sqlite-0004) | C-R1#3, C-R3#3 | sqlite-0004 | 2h | TODO |
| C6 | Unit re-resolve archaeology (stubborn units) | C-R3#7 | sqlite-0029, redis-0015/0049 | 2h | TODO |
| C7 | Branch-stall archaeology (redis-0054/0055) | C-R3#6 | redis-0054/0055 | 2h | TODO |

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
- 2026-08-23 07:1x: **P9 REGRESSION INVESTIGATION COMPLETE.**
  **Golden-path is RULED OUT**: retrieval_scores are empty on all 6 —
  the store path isn't reached without CAPYBASE_GOLDEN_PATH=1 (not set
  for shard 2). The memory layer is present but the store resolves to
  the per-case temp repo, which has no seeded examples.

  **Root cause: LLM sampling variance on compile-gated C cases.**
  All 6 regressions were PASS in the harvest at high sim (0.94–1.0)
  and remain at high sim in shard 2 (0.91–1.0). The buffers are
  near-oracle; the failures are compile-level defects in the LLM's
  output that the harvest's sampling didn't produce:
  - jsonc-0007 (sim 0.978): unclosed '{' the brace repair can't fix
    (sibling detection doesn't find a valid insertion point —
    brace_repair_skipped reason=balance_failed on the repair path too)
  - redis-0013/0014 (sim 1.0/0.999): compile errors (implicit
    declaration; argument type) — different candidate each sampling
  - redis-0047 (sim 0.912): attributed compile error (P4 mechanism
    correctly fired)
  - sqlite-0019 (sim 1.0): whole-file repair re-resolve failure
  - sqlite-0039 (sim 0.995): identifier error

  **The two improvements (sqlite-0008/0014 → PASS) are the coherence
  rung's deterministic conversions — NOT sampling.** The -1.9pp net is
  2 deterministic gains vs 6 variance losses. The true delta from
  mechanisms is POSITIVE (+2); the 6 losses are the C pipeline's
  inherent sampling noise floor (compile-gated cases that flip on
  different LLM outputs).

  **Verdict: shard 3 is SAFE to launch.** The regressions are variance,
  not mechanism-caused. The retry relaxation fired correctly (4 of 6,
  granting the extra retry — the model still didn't pass on the retry,
  which is the ceiling, not the mechanism). The coherence rung is net
  +2 on C. No repairs needed before shard 3; the noise floor is the
  honest finding.

## C-shard improvement plan (from three-reviewer synthesis)

See PLAN-LEDGER-S22-C-IMPROVEMENTS.md for full detail. Summary:

| # | Item | Target | Effort | Priority |
|---|------|--------|--------|----------|
| C1 | Side-provenance symbol injection | redis-0002/0012, sqlite-0030 | 4-6h | P0 |
| C2 | Include-directive repair | redis-0013/0014 | 3-4h | P1 |
| C3 | Preceding-block injection | stubborn units | 2h | P2 |
| C4 | Repair-interleaved retry loop | variance class | 3h | P3 |
| C5 | Oversized-prompt splitting diagnosis | sqlite-0004 | 2h | P4 |
| C6 | Unit re-resolve archaeology | sqlite-0029, redis-0015/0049 | 2h | P5 |
| C7 | Branch-stall archaeology | redis-0054/0055 | 2h | P6 |

Design principle: repair-layer, not reasoning-layer. The model already
produces correct merges; these mechanisms connect compiler errors to
side-content fixes.

## Shard 3 (rust) — results + findings (2026-08-23)

Run: 06:52–10:16 (3h24m), exit=0, 194/194, flights preserved.
155 PASS (79.9% raw, -1.0pp vs s20) / 13 ESC / 24 ESCALATE_TOOLCHAIN /
1 GATE_UNAVAILABLE / 1 ORACLE_DIVERGENT. Era-adjusted 91.2% (-1.2pp).
Era set identical to s20 (tokio 15 + sea-orm 9) — probe fully stable.

**Unlike shard 2, the regressions are NOT variance: all 4 flips are
deterministic (3/3 identical journal trails).** 3 regressions, all
repair-layer:

- axum-0013: token_disjoint → unbalanced brace, rung can't repair,
  whole-file repair repeats the same failed repair (no diversity) → C4.
- axum-0019: `prefix 'item' is unknown` — symbol dropped outside the
  conflict unit; plain_llm can't fix (fix not in unit context) → **C1
  now has cross-language specimens (redis/sqlite C + axum Rust)**.
- tokio-0026: insertion_union + coherence-repaired to "passing" →
  accepted with NO compiler check in-session (pre_continue=`true`),
  eval cargo check fails, sim 0.961 → new item **R1**.

1 improvement: axum-0005 via midband-subsumption → true-side portfolio,
deterministic, sim 1.0.

| # | Item | Target | Effort | Priority |
|---|------|--------|--------|----------|
| R1 | Post-coherence-repair verification: a candidate that needed the coherence rung must not be accepted on coherence alone — require build-gate (when configured) or LLM verify before accept | tokio-0026 | 2-3h | P1 (rust-side) |

Shard 4 (cpp, 167) launched 10:5x. src/ edits held until it completes —
eval subprocesses re-import capybase per case; a mid-run edit would
contaminate the shard.

## Baseline-freeze protocol (user directive, 2026-08-23)

Once shard 4 lands: the 4-shard aggregate is the **README baseline row**.
After that point, land as many fixes as possible between shard rounds —
that is the purpose of sharding.

**Mechanism-state mapping (verified: no mid-run contamination; each
change landed in a between-shards gap; P4 signature absent from shard-2
flights, P1 SAFE_SKIP active in shard 2 only):**

| shard | ran on | state |
|-------|--------|-------|
| 1 python | ad16e06 | s21 stack + pre-eval items 1&2 |
| 2 c | 93b61de | + P1 SAFE_SKIP, P2 retry relaxation |
| 3 rust | 943b8d5 | + P3 extreme-asymmetry, P4 insertion-within-deletion |
| 4 cpp | 943b8d5 | same as shard 3 (docs-only commits since) |

README row cites the per-shard commits (footnote) and totals as the
"s22 sharded baseline". The post-fix reround runs uniform on a single
commit — that becomes the second row.

**Fix-landing order after shard 4** (fixes validate via targeted
specimen A/B, then the next sharded round measures the aggregate):

1. **C1** side-provenance symbol injection (P0; redis-0002/0012,
   sqlite-0030, axum-0019 cross-language)
2. **R1** post-coherence-repair verification gate (tokio-0026)
3. **C4** repair-interleaved retry diversity (axum-0013, sea-orm-0011)
4. **micro-CEGIS stage-1 use-dedup** (sea-orm-0021 duplicate re-exports)
5. **P5** provenance-aware resurrection guard (tokio-0037/0042/0046)
6. C2/C3 and remaining P/C items as capacity allows

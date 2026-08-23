# Sprint-22 sharded harvest — results tracker

Accumulates results across all shards. Each shard's entry is appended
when it completes; the README table is generated from this file.

## Shards

| shard | lang | cases | PASS | ESC | ERA | WORK | NEAR | DIV | GATE | PASS% | Δ vs harvest | flips (imp/reg) | status |
|-------|------|-------|------|-----|-----|------|------|-----|------|-------|-------------|-----------------|--------|
| 1 | python | 111 | 98 | 9 | 0 | 2 | 2 | 0 | 0 | **88.3%** | **+3.6pp** | 4/1 | ✅ DONE |
| 2 | c | 205 | 83 | 22 | 98 | 1 | 0 | 0 | 1 | **40.5%** | **-1.9pp** | 2/6 ⚠️ | ✅ DONE |
| 3 | rust | 194 | 155 | 13 | 24 | 0 | 0 | 1 | 1 | **79.9%** | **-1.0pp** | 1/3 ⚠️ | ✅ DONE |
| 4 | cpp | 167 | 98 | 20 | 45 | 0 | 0 | 4 | 0 | **58.7%** | **-2.4pp** | 2/6 ⚠️ | ✅ DONE |
| **total** | | **677** | **434** | **64** | **167** | 3 | 2 | 5 | 2 | **64.1%** | **-0.9pp** | 9/17 | ✅ |

Adjusted % = PASS/(cases − era-dead − SAFE_SKIP); the 16 SAFE_SKIP
cases (git-resolved-cleanly) are counted inside ESC. Uniform formula,
recomputable from the committed extracts. s20 under the same formula:
440/677 = 65.0% raw, 440/495 = 88.9% adjusted.

## Shard 4 (cpp) triage notes

Run: first launch 10:53 WITHOUT the size-guard env (80/167 subset,
incident logged); corrected relaunch 12:52–13:54, exit=0, 167/167,
flights preserved. Final: 98 PASS (58.7%) / 20 ESC / 45
ESCALATE_TOOLCHAIN / 4 ORACLE_DIVERGENT. Adjusted 98/(167-45-13) =
98/109 = 89.9%. (Correction 2026-08-23: an earlier version of this
row said 80.3%, forgetting the 13 SAFE_SKIP exclusions; the uniform
formula is PASS/(cases − era − SAFE_SKIP) throughout.)

**Flip audit: 6 regressions, 2 improvements. Era set IDENTICAL to s20
(45 = 45, zero churn — third language confirming probe stability).**

- 4 deterministic PASS→ORACLE_DIVERGENT (3/3 identical repeats):
  clickhouse-0023 (0.990), clickhouse-0049 (**1.000**), protobuf-0012
  (0.974), protobuf-0038 (0.965). Same sims as s20 (same-shaped
  buffers), `compiles` flipped True→False. clickhouse-0049's journal
  is **tokio-0026's exact pattern**: `lint_vs_refactor` accept →
  coherence rung repaired → gate passed → completed; eval compile
  failed after. **R1 family confirmed in a second language.**
  Companion eval gap: `oracle_builds` was unprobed (None) for all
  four, so the GATE_UNAVAILABLE sandbox-artifact rescue cannot fire —
  probe-on-divergence is a harness follow-up (ledger E1).
- 2 variance PASS→ESCALATE with mixed repeats: clickhouse-0040
  (ESC/PASS/ESC, TIMEOUT_CONVERGENCE), protobuf-0008
  (ESC/DIV/PASS, OTHER). Coin-flip class, majority rule kept honest.
- Improvements: protobuf-0065 ESCALATE→PASS at sim 1.000 (the s19
  D7 fixed-gate specimen landing); clickhouse-0013 0.843→0.998 PASS
  (majority-PASS variance conversion).

ESCALATE breakdown (20, of which 13 are SAFE_SKIP corpus noise):
active 7 = REPAIR_FAILURE/TIMEOUT/OTHER mix (see failure report when
written); 4 DIV are the R1 family above.

Era-adjusted: rust 91.2% (24 era-dead) vs s20 92.4% (-1.2pp). The 24-case
era set is IDENTICAL to s20's (tokio 15 + sea-orm 9) — the era probe is
fully stable across the sprint boundary.

## Shard 3 (rust) triage notes

**Headline: the -1.0pp raw dip is 3 deterministic mechanism regressions vs
1 deterministic gain — NOT sampling variance (unlike shard 2's noise
floor). All 4 flips are 3/3 repeat-consistent with identical journal
trails.** The failure modes are repair-layer gaps, matching the C-shard
design principle.

Regressions (all repair-layer, all deterministic):

- `axum-history-0013` PASS→ESCALATE (sim 0.994): `token_disjoint`
  structural splice → unbalanced brace at line 102; coherence rung fired
  but could not repair; whole-file repair looped the SAME failed repair
  twice (no retry diversity). Feeds C4.
- `axum-history-0019` PASS→ESCALATE (sim 0.996): cargo gate fails with
  `prefix 'item' is unknown` + mismatched delimiter — a symbol/import
  dropped outside the conflict unit. `plain_llm` retry could not fix it
  because the needed `use` isn't in the unit context. **Cross-language
  confirmation of C1 (was redis/sqlite in C, now axum in Rust).**
- `tokio-history-0026` PASS→ORACLE_DIVERGENT (sim 0.961):
  `insertion_union` accepted → coherence rung repaired it to
  "coherent" → file gate PASSED → accepted without LLM escalation and
  without any compiler check (pre-continue was `true`); eval's cargo
  check then failed. **New gap: coherence-repaired candidates are
  accepted without verification.** → new item R1.

Improvement:

- `axum-history-0005` ESCALATE→PASS (sim 1.0): midband subsumption gate →
  true-side portfolio (current-side takeover), fully deterministic, no
  LLM, no repair. Clean attribution to the S21 gate. (Its comment-phase
  LLM call failed HTTP 400 — correctly non-blocking.)

ESCALATE breakdown (13): REPAIR_FAILURE 4, TIMEOUT_CONVERGENCE 3,
SAFE_STOP 3, OTHER 2, MODEL_NEEDS_HUMAN 1. tokio-0037 SAFE_STOP is the
same case id as the s19 D7 resurrection-backstop specimen.

## Pre-eval rounds (selected samples, not full shards)

| round | cases | PASS | non-PASS | conversions | regressions | note |
|-------|-------|------|----------|-------------|-------------|------|
| pre-eval 1 | 14 | 8 | 6 | 3 (0014/0065/0036) | 0 | mechanism targets |
| items validation | 2 | 0 | 2 | 0 | 0 | 0016 patch fired, re-gate failed; 0034 unchanged |
| pre-eval 2 | 13 | 1 | 12 | 0 | 0 | all-new investigate tier |

## Baseline (sprint-20 harvest, for Δ comparison)

| lang | cases | PASS | PASS% |
|------|-------|------|------|
| python | 111 | 94 | 84.7% |
| c | 205 | 87 | 42.4% (era-heavy: sqlite 90 era-dead) |
| rust | 194 | 157 | 80.9% |
| cpp | 167 | 102 | 61.1% (era-heavy: nlohmann 38 era-dead) |
| **total** | **677** | **440** | **67.4%** (raw) / **89.1%** (era-adjusted) |

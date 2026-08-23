# Sprint-22 sharded harvest — results tracker

Accumulates results across all shards. Each shard's entry is appended
when it completes; the README table is generated from this file.

## Shards

| shard | lang | cases | PASS | ESC | ERA | WORK | NEAR | DIV | GATE | PASS% | Δ vs harvest | flips (imp/reg) | status |
|-------|------|-------|------|-----|-----|------|------|-----|------|-------|-------------|-----------------|--------|
| 1 | python | 111 | 98 | 9 | 0 | 2 | 2 | 0 | 0 | **88.3%** | **+3.6pp** | 4/1 | ✅ DONE |
| 2 | c | 205 | 83 | 22 | 98 | 1 | 0 | 0 | 1 | **40.5%** | **-1.9pp** | 2/6 ⚠️ | ✅ DONE |
| 3 | rust | 194 | 155 | 13 | 24 | 0 | 0 | 1 | 1 | **79.9%** | **-1.0pp** | 1/3 ⚠️ | ✅ DONE |
| 4 | cpp | 167 | — | — | — | — | — | — | — | — | — | — | ⏳ RUNNING |

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

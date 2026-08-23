# Sprint-22 sharded harvest — results tracker

Accumulates results across all shards. Each shard's entry is appended
when it completes; the README table is generated from this file.

## Shards

| shard | lang | cases | PASS | ESC | ERA | WORK | NEAR | DIV | GATE | PASS% | Δ vs harvest | flips (imp/reg) | status |
|-------|------|-------|------|-----|-----|------|------|-----|------|-------|-------------|-----------------|--------|
| 1 | python | 111 | 98 | 9 | 0 | 2 | 2 | 0 | 0 | **88.3%** | **+3.6pp** | 4/1 | ✅ DONE |
| 2 | c | 205 | 83 | 22 | 98 | 1 | 0 | 0 | 1 | **40.5%** | **-1.9pp** | 2/6 ⚠️ | ✅ DONE |
| 3 | rust | 194 | — | — | — | — | — | — | — | — | — | — | ⏳ ready to launch |
| 4 | cpp | 167 | — | — | — | — | — | — | — | — | — | — | ⏳ queued |

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

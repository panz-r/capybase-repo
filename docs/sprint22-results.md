# Sprint-22 Results

**Sprint thesis**: sharded harvest — run each language shard on a pinned
mechanism state, analyze failures between shards, land fixes in the
gaps, freeze the 4-shard aggregate as the README baseline, then fix
sprint + uniform-commit reround.

## Phase 1: Sharded baseline harvest (complete)

| shard | lang | ran on | PASS% raw | adj% | Δ raw vs s20 |
|-------|------|--------|-----------|------|--------------|
| 1 | python | ad16e06 | 88.3% | 89.9% | +3.6pp |
| 2 | c | 93b61de | 40.5% | 78.3% | −2.0pp |
| 3 | rust | 943b8d5 | 79.9% | 91.2% | −1.0pp |
| 4 | cpp | 943b8d5 | 58.7% | 89.9% | −2.4pp |
| **total** | | | **64.1%** | **87.9%** | **−0.9pp** |

Baseline frozen 2026-08-23 (README table + `docs/results/s22/`
per-case extracts; verification recipe runs from a clone). Era census
stable across all four shards (zero classification churn). Flip
accounting per shard in the failure reports
(`docs/sprint22-{python,c,rust,cpp}-failures-report.md`).

Operational incidents, both root-caused and repaired:
- shard-4 first launch omitted `CAPYBASE_SKIP_SIZE_GUARD=1` (env was
  ambient for shards 1-3) — 80/167 silent subset, exit 0. Harness now
  prints a SUBSET RUN banner; env gates are explicit in commands.
- full-suite gate run #3 overlapped a targeted run — 11 contention
  flakes. Rule: never overlap suite runs.

## Phase 2: Fix sprint (in flight)

Ordered by the rust-round reviewer synthesis; honest scoreboard per
item:

1. **R1 — coherence-repair propagation + fail-closed guard (DONE).**
   Root cause ran deeper than reviewed: the rung's repair was
   validation-local — verify_file validated a repaired copy while the
   caller wrote the unrepaired buffer to disk. `resolved_text`
   propagation + compiler-verification guard + pristine-probe decline.
   **5/5 specimen validation PASS** (tokio-0026 0.985, clickhouse-0049
   1.000, +3): five deterministic false accepts became genuine passes.
2. **Gate fallout A/B/C (DONE, same landing).** Lifetime-aware quote
   parity (ad16e06 regression), IndexError guard, P4 modify/delete
   guards + broken test fixture. Suite green 6241/0.
3. **C1 — deterministic missing-symbol repair (DONE).** Unified C+Rust
   signature table, conservative declaration finder, language-correct
   injection; hooks at micro-CEGIS stage 2a + file gate. Integration
   fixture resolves via injection with no model call. **No live
   conversion claimed** — the named specimens were mis-triaged
   (delimiter cascade / resurrection / corrupted decl / variance); the
   reround measures the real effect.
4. **R2 — use-statement dedup (DONE).** Scope-aware exact-duplicate
   sweep riding R1's propagation + guard.
5. **C4 — repair-diversity rotation** (planned: per-file tried-repair
   registry; deterministic kinds rotate before model-with-error).
6. **P5 — provenance-aware resurrection guard** (planned: replayed-
   parent presence downgrades stop→warning; 5 specimens).
7. **R3/R4/E1** (planned: within-session best-of-N; pre-registered
   near-floor window; eval probe-on-divergence).

## Honest accounting

- The baseline's −0.9pp aggregate decomposes into: python +3.6
  (mechanism gains), C −2.0 (variance floor over +2 deterministic), rust
  −1.0 (3 deterministic repair-layer gaps — all R1-fixed), cpp −2.4 (4
  deterministic R1 false accepts — all fixed — plus 2 variance).
- R1's five conversions apply to the baseline's cpp/rust rows; they are
  NOT retro-edited into the frozen table — the reround row will carry
  them.
- Coin-flip cases (passed 1-of-3 repeats) remain honestly non-PASS;
  majority-of-3 is pre-registered.
- Two reviewer proposals rejected as metric gaming (threshold tuned to
  convert a named case; floor fitted to an observed value) — recorded
  in PLAN-LEDGER-S22-R-IMPROVEMENTS.md.

## Phase 3: Reround (pending)

Uniform-commit sharded harvest → second README row; era-sweep
invariant: never declassify a prior PASS.

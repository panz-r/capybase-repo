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
| R1 | Post-coherence-repair verification: a candidate modified by ANY deterministic repair rung carries `provisional=True`; acceptance requires the baseline-relative per-file compile gate (never coherence alone) | tokio-0026 | 1-2h | **P0** (all 3 reviewers; safety hole) |
| R2 | `use`-statement dedup sweep: post-splice exact-duplicate removal + in-brace item dedup; micro-CEGIS stage-1 response to `defined multiple times` (precedent: s17 #include dedup) | sea-orm-0021 | 1-2h | P1 (all 3) |
| R3 | Within-session best-of-N candidate selection: on compile-gate failure + retry budget remaining, up to 2 extra diverse candidates, ALL fully validated; paired A/B before default-on | axum-0002-class coin-flips | 2-3h | P4 |
| R4 | Near-floor sbcr acceptance: fixed pre-registered window (fitness ≥ floor − 0.02 AND retry-cap active) → one extra attempt; full validation still applies; conversions count as WORKING where sim < 0.90, never PASS | sea-orm-0011 | 1h | P5 |

Rust-round reviewer synthesis (full detail: PLAN-LEDGER-S22-R-IMPROVEMENTS.md):
R3-most-useful (within-session best-of-N is the legitimate form; parent-
provenance P5 the most precise), R1-strong-diagnosis-but-two-integrity-
lapses (cross-repeat best-of-N threshold tuned to 0.94 to convert a named
case; fitness floor fitted to the observed 0.591 — both REJECTED as
metric gaming), R2-best-vocabulary (Compiler-is-Authority flag; span-
intersection adopted as audit field only; ~98% projection not adopted —
honest projection ~93-95% era-adjusted). C1/C4 confirmed cross-language;
implementation order after baseline freeze: R1 → C1 → R2 → C4 → P5 → R3 → R4.

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

(Execution order superseded by the rust-round synthesis: R1 → C1 → R2
use-dedup → C4 → P5 → R3 → R4.)

## Shard-4 launch incident (2026-08-23, 11:30–12:55)

**Defect**: the shard-4 launch omitted `CAPYBASE_SKIP_SIZE_GUARD=1`.
Shards 1-3 inherited the env gate from their launching shell; the
reconstructed shard-4 command (from ps output, which does not show env)
did not. The loader silently drops `marker_original > 48K` cases
without it — shard 4 ran 80 of 167 (C++ has 87 guarded cases; C has
132, python 13, rust 51, all of which ran in their shards, proving the
gate was set for 1-3). The truncated run exited 0 at 11:30 (37min) —
a complete-looking but incomplete subset.

**Disposition**: the 80 completed cases are valid (same mechanism state
943b8d5, same provider; the guard affects case selection only, not
execution) and are kept. `s22-shard4b-cpp` relaunched 12:55 with the
env gate passed explicitly (`env CAPYBASE_SKIP_SIZE_GUARD=1 ...`) +
`--skip-existing`, running only the missing 87. README cpp row waits
for the full 167.

**Lesson recorded**: eval launches must carry the env gate explicitly
in the command, never via ambient shell state.

**Post-incident verification + repairs (12:55-13:05)**:
- longrun `cmd` files prove shards 1-3 carried exactly ONE env gate
  (`CAPYBASE_SKIP_SIZE_GUARD=1`) and shard4b's command is identical to
  theirs modulo `--lang cpp` — no other ambient state was lost; the
  4b relaunch is provably uniform. No second relaunch needed.
- Harness repaired: `load_cases(dropped_ids=...)` + SUBSET RUN banner
  at startup and in the final summary when the 48K guard drops cases
  (verified: without gate 80 loaded/87 dropped/banner fires; with gate
  167/0/silent). A future missing-env launch is now visible in the
  first minute, not after a complete-looking exit 0.
- `docs/results/s22/meta.json` command_template corrected to include
  the env gate (it had the same omission that caused the incident).

## Shard 4 (cpp) — results (2026-08-23, BASELINE FROZEN)

Complete 13:54 exit=0, 167/167, flights preserved. 98 PASS (58.7%
raw) / 20 ESC (13 are SAFE_SKIP corpus noise) / 45 ESCALATE_TOOLCHAIN
/ 4 ORACLE_DIVERGENT. Adjusted 98/122 = 80.3%. Δ vs s20: −2.4pp raw,
−3.7pp adjusted. Era set identical to s20 (45=45, zero churn — third
language confirming probe stability).

Flips: 6 regressions / 2 improvements.
- **4 deterministic PASS→ORACLE_DIVERGENT** (clickhouse-0023/0049,
  protobuf-0012/0038; sims 0.965–1.000; same sims as s20 —
  same-shaped buffers, compiles flipped True→False):
  clickhouse-0049's journal = tokio-0026's exact pattern
  (lint_vs_refactor accept → coherence-repair → gate passed → eval
  compile failed). **R1 family confirmed in a second language — R1
  promotes to first implementation item.**
- 2 variance flips (mixed repeats): clickhouse-0040 (ESC/PASS/ESC),
  protobuf-0008 (ESC/DIV/PASS).
- Improvements: protobuf-0065 ESCALATE→PASS sim 1.000 (the s19 D7
  fixed-gate specimen); clickhouse-0013 0.843→0.998.

| # | Item | Target | Effort | Priority |
|---|------|--------|--------|----------|
| E1 | Eval probe-on-divergence: when a marker-free, non-escalated, near-oracle buffer fails compile, probe `oracle_builds` so the GATE_UNAVAILABLE sandbox-artifact rescue can fire (it could not for the 4 cpp DIV regressions — oracle_builds was None) | cpp DIV class | 1h | P1 (eval-side) |

## BASELINE FROZEN (2026-08-23 14:0x)

Four-shard aggregate committed to README + docs/results/s22 extracts:
**434/677 = 64.1% raw, 434/494 = 87.9% adjusted** (uniform formula
PASS/(cases − era − SAFE_SKIP); s20 under same formula 65.0%/88.9%).
Fix sprint opens now: R1 → C1 → R2 use-dedup → C4 → P5 → R3 → R4.

## R1 IMPLEMENTED (2026-08-23, fix sprint item 1)

**Root cause found — deeper than the reviewers' framing.** The rung's
repair was VALIDATION-LOCAL: verify_file repaired its local `whole`,
validated the repaired copy, returned passed=True — but
VerificationResult carried no text, so the caller wrote its UNREPAIRED
buffer to disk. tokio-0026's final file was brace-unbalanced (eval
compiles = brace-balance for rust = False) while the session had
"passed" the repaired copy. clickhouse-0049 same (cpp tree build
failed on the unrepaired file). The in-session cargo/gcc checks were
not too weak — they validated text that never reached the disk. The
sqlite-0008/0014 conversions passed because they flowed the
orchestrator-side repair path (which propagates via a synthetic unit);
the internal rung instead MASKED the failure from ever reaching it.

Changes:
1. `VerificationResult.resolved_text` (conflict_model.py): the repaired
   text when a rung fired, None otherwise.
2. verify_file returns it at the final assembly; adds the R1
   fail-closed guard: repaired + syntax stage did-not-run/pass → hard
   failure `coherence repair applied without compiler verification`
   (never accept on coherence alone — the reviewers' unanimous rule).
3. Orchestrator consumption: main gate + cross-unit portfolio +
   deterministic-repair + comment post-gate + deletion-respect swap
   write/carry the repaired text; the two pristine-side probe sites
   DECLINE a side whose text needed repair (repaired ≠ pristine).
4. tests/test_r1_coherence_propagation.py: propagation, no-repair-
   untouched, fail-closed (no compiler), compiler-backed accept. The
   fail-closed test proved the guard's worth immediately: it rejected
   a rung repair that produced syntactically invalid python.

Validation: 5-specimen rerun (--case) pending after full-suite gate.

**Validation result (16:38): 5/5 PASS** — tokio-0026 sim 0.985 (was
ORACLE_DIVERGENT 3/3), clickhouse-0049 sim 1.000, clickhouse-0023
0.990, protobuf-0012 0.973, protobuf-0038 0.962 (all four cpp DIV
regressions converted). The five false accepts became genuine passes:
the repaired text now reaches disk, compiles, and matches the oracle.

## C4 + P5 + E1 IMPLEMENTED (2026-08-24, fix sprint items 4-6)

**C4 — repair rotation.** Per-(step, path) tried-repair registry keyed
by failure signature: a deterministic repair that failed for a
signature never re-runs (axum-0013's two identical brace-repair
rounds). `repair_rotation` journaled; new signatures re-arm the ladder.

**P5 v2 — resolved-file provenance.** Surprise at implementation: the
reviewers' replayed-coverage downgrade ALREADY existed in
`_handle_resurrections` — the specimens stopped because the coverage
check legitimately fails there (content not in the replayed blob).
v2 adds the second conservative signal: stop downgrades to warn when
EVERY flagged path was explicitly resolved + compile-validated this
session (surfaced in output + bundle, never silent; untouched files
keep the hard stop). **5-specimen validation: 4 PASS** (tokio-0037
1.000, tokio-0042 0.999, clickhouse-0020 1.000, redis-0012 0.987) +
tokio-0046 honest NEAR_MATCH 0.884; 6 journaled downgrades.

**E1 — eval probe-on-divergence.** The WS1c oracle probe now also
fires on marker-free, non-escalated c/cpp tree-build failures (the
four cpp DIV regressions had oracle_builds=None, so the
GATE_UNAVAILABLE rescue was unevaluable).

**Fix-sprint scoreboard (validated conversions): R1 = 5, P5 = 4 (+1
NEAR); C1/R2/C4 land for the reround to measure; R3/R4 deferred
pending value call.**

## C1 + R2 IMPLEMENTED (2026-08-23, fix sprint items 2-3)

**C1 — deterministic missing-symbol repair (unified C+Rust).**
Pure helpers in verification.py: `parse_missing_symbols` (signature
table incl. curly-quote gcc diagnostics — redis-0002's ‘pat’ shape),
`find_symbol_declaration_lines` (only complete injectable lines: rust
use/mod, C prototypes/typedefs/forwards/plain variable decls like
`pubsubPattern *pat;`), `inject_symbol_declaration` (language-correct
placement, dedup). Two hooks: micro-CEGIS stage 2a (before the model
micro-patch) and `_try_symbol_injection_repair` at the file gate
(before fault attribution — the missing decl lives OUTSIDE the units).
Nothing invented: verbatim side/base lines only. 17 unit tests; the
micro-CEGIS integration fixture now resolves via symbol_inject
(`Tokenizer tokenizer_;`) with no model call.

**Honest live-validation scoreboard**: the five named specimens were
mis-triaged at shard analysis — axum-0019's "prefix `item`" is
delimiter-cascade noise (C4-class), redis-0012 is resurrection (P5),
sqlite-0030 a corrupted declaration (not injectable), sea-orm-0023 and
redis-0002 coin-flipped to PASS on sampling variance with C1 unfired.
**No live C1 conversion is claimed.** The hooks verifiably engage
(decl_not_found journaled); the aggregate reround measures its real
effect.

**R2 — exact-duplicate `use` dedup (rust).** Scope-aware sweep in
verify_file before the syntax stage (sea-orm-0021's 17
"defined multiple times"); rides coherence_repair_applied so R1's
propagation + fail-closed guard cover the deduped text. 4 tests.

Gate: full suite pending before this batch lands.

## Gate fallout fixed (suite runs 1-3, 2026-08-23)

Full-suite run #1 post-R1: 23 failed (14 = R1 test-double breakage,
fixed via getattr-tolerant reads). Run #2: 9 failed — stash A/B proved
all 9 pre-date R1 (last green gate was s21-final2; they landed in the
ad16e06/93b61de/943b8d5 window without a full gate). Three clusters:

- **A. lifetime-aware quote parity** (verification.py, ad16e06
  regression): `_try_repair_string_literal` counted Rust lifetimes as
  quote delimiters — a signature line with 5 lifetimes (odd count) got
  a stray `'` appended after `{`, which then broke the brace stripper
  (lifetime_mismatch catalog case). Fix: `'a`-style quotes with no
  closing `'` within char-literal width are lifetimes, not delimiters.
- **B. IndexError in brace-repair candidate 0** (verification.py):
  `cleaned` (stripped) can have fewer lines than `lines`; every
  cross-index access now length-guarded (sqlite-0113/0118 crashes).
- **C. modify/delete hijack + broken fixture**: (1) the AU/UA test
  fixture's replace pattern never matched (`\n\n\n` vs `\n\n`) — the
  keeper never contained "return 11"; shipped broken, caught by this
  gate. (2) P4 fired on whole-file deletion units (empty deleter) —
  block-capture's keep-vs-delete domain — producing deleter+insertion
  output and bypassing the model adjudication. Fixes: fixture anchored
  on the alpha signature; P4 gains a wholesale-deletion guard (empty
  deleter → decline) and a purity guard (inserter with replace/delete
  ops → decline) so only pure insertions inside a surviving file's
  deleted block salvage.

All six affected files green (2210 passed incl. the gcc realworld
corpus); final full-suite gate run #3 in flight before the R1+fixes
commit.

**Gate run #3 postscript**: 13 failed — 2 deterministic (fix A's first
version skipped `'a;` char literals in C too; now LANGUAGE-GATED:
lifetime exemption applies to rust only, `char c = 'a;` in C counts —
80 tests green) and 11 contention flakes (run #3 overlapped the
51-minute targeted run for its first 16 minutes; the 11 e2e/git/lsp
tests pass standalone in 1.3s). Lesson recorded: never overlap full
suite runs with other eval/test work. Gate run #4 (clean, uncontended)
is the landing gate.

## Round-3 reviewer synthesis (post-fix-sprint, 2026-08-24)

Four documents: two proposals + a head-to-head pair (Response 1 vs
Response 2). Rated below. Key context the reviewers partly lacked:
C1 already searches FULL side files (stage blobs), not conflict
regions; deterministic_gcc_fixit, #endif positional repair, use-dedup
(R2), repair rotation (C4), and resolved-file provenance (P5 v2) all
exist and are in the running reround.

### Rating (the head-to-head pair)

**Response 2 — most useful.** Three genuinely NEW mechanisms that
survive contact with our evidence: (a) the error-accumulating repair
chain (repair rounds build on prior output; the model sees the full
tried/failed history so it cannot re-introduce fixed errors — directly
targets the repair-fires-but-regate-fails trio); (b) budget-aware
diverse-strategy scheduling (no two consecutive rounds use the same
strategy — a sharper C4); (c) project-wide symbol index (real delta
over C1: sibling files, headers). Also correctly bounds what will NOT
convert (zenodo-0044, sea-orm-0027) and its strategic frame — "the
honest ceiling is a function of how much repair burden shifts from the
model to deterministic mechanisms" — is the right way to see the
remaining population. Lapse: restates R3 as the cross-repeat
aggregation ("any repeat passes AND sim >= 0.94") — the gaming pattern
rejected twice before.

**Response 1 — solid execution discipline, thinner novelty.** Best
phasing of any reviewer this sprint (tiered ROI, honest effort
estimates, an explicit "would NOT do" list that correctly refuses
gate-weakening, mid-band-as-failures, and era-chasing). But its Tier-1
restates existing/rejected items (full-file injection premise is
factually off — C1 searches full sides; retry relaxation = R4;
best-of-N aggregation = third occurrence of the rejected threshold
pattern), and its Tier-2 is self-admitted low-ROI.

### New sprint-23 candidates (accepted)

| # | Item | Source | Targets | Effort | Priority |
|---|------|--------|---------|--------|----------|
| D1 | Error-accumulating repair chain: repair rounds build on the prior output; model retries carry the full tried/failed error history | R2(pair) | protobuf-0034/0051, jsonc-0016 | 4-5h | P1 |
| C1c | Project-wide symbol search: extend C1's search space to sibling repo files (headers, mod files) | R2(pair), R1(pair) | redis-0013, sqlite-0030 | 4-6h | P2 |
| C1b | Verbatim type/token repair: expected type from the compiler message when it appears verbatim in a parent; parse-error line restored from an unchanged base/side line | proposal-1 #4 | redis-0014, sqlite-0039 | 3-4h | P3 |
| R3' | Diverse-strategy scheduling: no two consecutive repair rounds use the same strategy (model calls alternate with deterministic repairs) | R2(pair) | class D | 3h | P3 (extends C4) |

Existing items reinforced: C5 (oversized-prompt diagnosis — all three
converge), R4 (fitness-gated retry extension, pre-registered 0.02
window), R3 within-session best-of-N (NOT the aggregation form),
C6/P7 archaeology (zenodo-0085, sqlite-0040).

### Rejected (with occurrence counts)

- **Cross-repeat best-of-N aggregation** (3rd occurrence — R1(pair)
  1.3, R2(pair) R3, proposal-1 #6 variant): post-hoc threshold fitting
  on known cases; majority-of-3 is pre-registered.
- **Skeleton-override-to-PASS** (3rd occurrence — playbook #2):
  eval-only verdict manipulation; skeleton_similarity stays a recorded
  diagnostic (S20.11), never a gate.
- **Wall-clock retry budgets** (2nd occurrence): non-reproducible runs;
  budget stays a deterministic function of unit count/fitness.
- **Compile-gate weakening**: nobody proposed it; Response 1's explicit
  rejection endorsed.

### Held (not rejected, not now)

Decision-point decomposition (mid-band, reasoning-layer, 6-8h for
graded conversions); statement-level splitting (mini-conflict +
member/entity split largely cover it); mid-band repair-by-example
(golden-path validated for conversions, not style); deletion-intent
classifier (P5 leftover); cross-unit dependency graph (20-30h, ROI-poor
by its own proponents).

## Round-4 synthesis — the IMPROVED SPRINT-23 PLAN (2026-08-24)

Three documents refining the per-case detail. Strong convergence on
three items (now priority-bumped), one new rejection with a
sim-conservation argument, and one 4th-occurrence rejection with a
factual correction.

### Feasibility verified this round (not assumed)

- redis-0013: `cliSwitchProto` EXISTS in the replayed side as a
  DEFINITION (`static int cliSwitchProto(void) {`) — a forward
  declaration is a mechanical transform of verbatim side content
  (`{` → `;`). CONVERTIBLE via derived prototypes.
- sqlite-0030: `sqlite3_value_frombind` appears in all parents only as
  table identifiers, not as the full correct declaration line —
  C1b-replace target, conversion UNCERTAIN (honest).

### The plan (priority order, post-reround)

| # | Item | Convergence | Targets | Effort | Priority |
|---|------|-------------|---------|--------|----------|
| D1 | Error-accumulating repair chain (rounds build on prior output; model sees full tried/failed history) | r3 | protobuf-0034/0051, jsonc-0016 | 4-5h | P1 |
| C1b | Two-mode repair: REPLACE mode — compiler line-anchored line replacement from parents (best-LCS line restore); type-token replacement (expected type verbatim in a parent); DERIVED prototypes (side definition `{`→`;`) | r3+r4 x3 docs | sqlite-0030 (uncertain), redis-0014, redis-0013 (verified derivable), sqlite-0039 | 3-4h | **P1 (bumped: 4-doc convergence + verified feasibility)** |
| C3 | Adjacent-context injection: on 2nd unit failure, LOCKED_PRECEDING_CONTEXT (10-20 resolved lines before the marker + file imports) | C-round + r2 + r4 x2 docs | redis-0015/0049, sqlite-0029, zenodo-0085 | 2-4h | **P2 (confirmed: 3-round convergence)** |
| R3' | Error-class→strategy rotation: parse→fixit/brace; symbol→C1; dup→R2; type→LLM-micro (replaces blind rotation) | r4 | class C/D coin-flips | 3h | P2 |
| C1c | Project-wide symbol search (sibling files/headers; full-file step already exists in C1 via stage blobs) | r3+r4 | redis-0013 backstop | 4-6h | P3 |
| R3+R4 | Within-session best-of-N (temperature ladder 0.2/0.4/0.6, fast-gate PRE-SCREEN, full validation on accept — never bypass) + deterministic proximity extra attempt | r2+r4 | class D coin-flips | 3h | P3 |
| C5 | Oversized-prompt diagnosis (sqlite-0004) | all rounds | sqlite-0004 | 2h diag | P4 |

Held: decision-point decomposition (top-3 mid-band only, 1-2 graded
conversions), mid-band style transfer, statement splitting (mini-
conflict/member-split cover), mixed-delimiter stack repair (C4
backlog), deletion-intent classifier, P5 non-code extension (minor).

### Rejected this round

- **Whole-side fallback on ANY compile failure** (doc-1 #2): breaks
  both-sides-represented semantics AND cannot produce PASS on the
  near-oracle hard core — a wholesale swap replaces a sim-0.999 buffer
  with different content, tanking oracle similarity; the cases where
  takeover IS right (churn dominance, dup pathology,
  single-compiling-side, wholesale floor) are already gated and
  calibrated. Escalation is not a wrong merge — nothing broken ships.
- **Cross-repeat best-of-N aggregation** (4th occurrence, doc-3 #3):
  besides the standing post-hoc-threshold objection, the justification
  is factually wrong — it lists 11 coin-flips with "sim ≥ 0.94" as
  convertible "if any repeat passed", but those sims are the FAILED
  buffer's oracle-similarity; most listed cases have ZERO PASS repeats
  in the baseline (redis-0040, sqlite-0039, protobuf-0001/0034/0051,
  axum-0013/0019/0033, zenodo-0079 were 3/3 ESCALATE). Buffer-sim is
  not a PASS repeat. The legitimate levers for this population are
  within-session R3 + R4 (in plan).
- **Wall-clock retry budgets** (3rd occurrence): non-reproducible.

### Premise corrections recorded (reviewers' claims vs code)

C1 already searches FULL side files (stage blobs) + the spliced buffer
— not "the conflict region"; empty-response→verified-single-side
fast-fail already exists (resolution layer 6); #endif positional
repair, use-dedup, repair rotation, resolved-file provenance all
landed pre-reround.

## Sprint-23 plan addendum — operational and safety items (2026-08-24)

Proposed from our own operational history (the reviewers structurally
cannot see these); added before implementation begins.

### Gate 0 — reround flip audit PREEMPTS new mechanisms

The reround is the first full-corpus exposure of R1/C1/C4/P5/E1.
Before any sprint-23 mechanism lands:
1. verdict_diff reround vs the frozen s22 extracts AND vs s20 — every
   flip attributed (mechanism / variance / era / E1-reclassification).
2. **A fix-sprint REGRESSION preempts the plan**: a mechanism-caused
   PASS→non-PASS flip is repaired before D1/C1b/C3 land. The python
   shard already shows 97 PASS vs 98 baseline with WORKING 2→4 — one
   flip to examine.
3. Never-declassify invariant (era-sweep E2): no prior PASS may become
   era-dead.

### Zero-cost safety audits (journal mining, no new code)

The fix-sprint mechanisms are validated on their CONVERT specimens but
their false-accept surfaces are unmeasured:
- **P5-downgrade cross-tab**: every reround `resurrection_downgrade`
  event vs the case's final verdict/sim — confirms no downgraded
  resurrection diverged from the oracle (the guard's false-negative
  surface).
- **C1-injection cross-tab**: every `symbol_inject` patch vs final
  verdict/sim — confirms injected content never produced a
  wrong-but-compiling accept.
Both run on the reround flights the day they land; results go in the
sprint-23 results doc before any new mechanism is trusted on top.

### Pre-registered acceptance criteria (written before implementation)

- **D1**: >=1 of protobuf-0034/0051, jsonc-0016 converts with a
  journal-attributed accumulating chain; no new hard failure class.
- **C1b**: redis-0013 converts via derived prototype; sqlite-0030
  converts only if the correct line exists verbatim/derivable — no
  invented content, ever; zero sim-drops on any replace.
- **C3**: >=1 of redis-0015/0049, sqlite-0029, zenodo-0085 converts;
  prompt sizes stay under the 8K window on the cohort.
- **R3'/R3/R4**: net coin-flip conversions positive with no
  never-declassify violation; R3 accepts only through FULL validation
  (fast gate is pre-screen only).
- Each mechanism: paired A/B on its named specimens, then one full
  suite gate per landing batch (no overlapping runs — the suite-overlap
  rule).

### Un-owned archaeology made explicit (P4 tier)

C7 (redis-0054/0055 branch-stall) joins C5 (sqlite-0004 oversized) as
the two named diagnosis tasks; sqlite-0040 truncation and zenodo-0085
stubborn-unit ride C3/P7. Comment-phase LLM 400s (non-blocking, seen
on axum-0005) go to the backlog for a single-retry hardening.

### Execution order

Gate 0 → D1 + C1b (one batch, one gate) → C3 + R3' (one batch) →
C1c → R3/R4 → C5/C7 diagnosis → specimen validations per batch →
sprint-23 reround (README row 3).

### Addendum 2 — kill criteria, row-2 freeze, incremental Gate 0

- **Pre-registered kill criteria**: if the first batch (D1+C1b)
  converts 0 of its 5 targets on paired A/B, the later batches
  (C3/R3'/C1c/R3+R4) are RE-JUSTIFIED against the flip-audit evidence
  before execution — no building on momentum. Upside projections are
  the reviewers' habit, not ours.
- **Row-2 freeze protocol**: the reround becomes README row 2 under
  the same verifiable-attribution scaffolding as row 1 — per-shard
  extracts under docs/results/s22r2/ + meta.json with the pinned
  commit (e7e7eb7) and commands; flip table recomputable from clones.
- **Gate 0 is incremental**: each shard's flip audit + safety
  cross-tabs run the day its flights land, not only at completion.
  The python and C shards are auditable NOW.
- Verdict-class movements count, not just PASS flips: mechanisms that
  convert ESCALATE→WORKING move the honest PASS+WORKING rate and are
  recorded as such (python's WORKING went 2→4 in r2).

### Gate 0 — python-shard early slice (executed 2026-08-24, C shard in flight)

- Flips vs frozen baseline: 2 regressions, 1 improvement, 1 class move.
  **No mechanism-caused regression.** zenodo-0019 (ESC/ESC/PASS) and
  zenodo-0088 (WORKING/PASS/WORKING) are variance-class coin-flips,
  majority rule honest.
- **zenodo-0064 ESCALATE→PASS 0.978: P5 v2 prediction CONFIRMED.**
- **zenodo-0063 still 3/3 ESCALATE: P5's weakest prediction MISSED**
  (the 0.92 band did not hold) — journal archaeology queued for full
  landing (why no downgrade fired or why it still escalated).
- Safety cross-tabs: 4 resurrection_downgrade events, **0 divergent
  outcomes**; 0 symbol_inject patches (python rarely C-style);
  WORKING 2→4 recorded as verdict-class movement per addendum 2.

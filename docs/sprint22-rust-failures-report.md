# Shard 3 Failure Report: Rust (39 non-PASS of 194)

**Context.** Sprint-22 sharded harvest, shard 3 (Rust), full mechanism
stack (member-split composition, coherence-repair rung, mixed-signature
era probe, retry relaxation, insertion-within-deletion, true-side
portfolio with midband trigger). Completed 2026-08-23 10:16 in 3h24m
(exit 0). Flights preserved for every session.

**Result: 155/194 PASS (79.9%)** vs harvest baseline 157/194 (80.9%), a
net **-1.0pp**. Era-adjusted: 155/170 = **91.2%** vs 92.4%. The era
census is **identical to the harvest's** (24 cases: tokio 15 + sea-orm
9) — the probe reproduced its classifications exactly across the sprint
boundary. Flip audit: 3 regressions vs 1 improvement, and **all four
flips are deterministic** (3/3 repeat-consistent, identical journal
trails) — this shard has no variance-class regressions, unlike shard 2.

---

## Population overview

Of 39 non-PASS cases, **24 are era-dead** (un-passable under the
current toolchain; classified in seconds by the preflight probe). The
**15 active failures** decompose into six classes.

---

## Class A: Deterministic mechanism regressions (3 cases — the P0 news)

All three were PASS in the harvest; all three failed 3/3 with identical
trails. None is sampling noise. Two feed existing C-shard items, one is
new.

| case | sim | failure chain | feeds |
|------|-----|---------------|-------|
| axum-0013 | 0.994 | `token_disjoint` splice → unbalanced brace L102; coherence rung fired, could not repair; whole-file repair repeated the *same* failed repair twice (no diversity) | **C4** |
| axum-0019 | 0.996 | sbcr/portfolio candidate → cargo: `prefix 'item' is unknown` + mismatched delimiter; `plain_llm` retry produced the same 2 errors — the needed `use` lives outside the conflict unit | **C1** (cross-language) |
| tokio-0026 | 0.961 | `insertion_union` → coherence rung repaired it to "coherent" → file gate passed → accepted **without the core LLM ever consulted and without any compiler check** (pre-continue was `true`); eval's cargo check then failed | **R1 (new)** |

**Analysis**: axum-0019 is the strongest possible confirmation of C1 —
the same missing-symbol-outside-the-unit class as redis-0002/0012 and
sqlite-0030 in C, now in Rust, and the LLM demonstrably *cannot* fix it
when the symbol isn't in unit context. tokio-0026 exposes a genuine
acceptance-gate hole: a candidate that needed the coherence rung was
accepted on coherence alone. R1 (ledger): coherence-repaired candidates
must be treated as provisional — require a build gate (when configured)
or an LLM verify pass before accept. axum-0013 shows the whole-file
repair loop re-running an unchanged repair; C4's repair-interleaved
retry (vary the repair, not just the model call) addresses it directly.

---

## Class B: Resurrection-guard stops (3 cases — honest conservatism)

| case | sim | harvest | event |
|------|-----|---------|-------|
| tokio-0037 | 1.000 | same (ESCALATE) | `resurrections_detected` policy=stop, 12 lines |
| tokio-0042 | 0.972 | same | resurrection stop after P4 insertion-within-deletion accepted |
| tokio-0046 | 0.884 | same | resurrection stop after plain-LLM accept |

**Analysis**: In all three the file resolved, validated, and staged
cleanly — then the resurrection guard saw deleted-in-base content come
back and stopped per policy. tokio-0037 is the s19 D7 specimen (perfect
buffer, honest stop). These are the system working as designed: no
silent wrong merge. The conversion path is **P5** (provenance-aware
guard: distinguish branch-intent revivals from splice accidents) —
tokio-0037's sim-1.000 buffer says the merge was right; the guard just
cannot know that yet.

---

## Class C: Compile-gated union defects (2 cases)

- sea-orm-0021 (sim 0.983): `lint_vs_refactor` structural accept →
  cargo: **17 errors, all "the name `X` is defined multiple times"** —
  union-merged re-export lists duplicating `EntityName`, `EntityTrait`,
  `EnumIter`. Whole-file repair fired, failed. This is
  micro-CEGIS stage-1 territory (redefinition delete) extended to
  `use`-statement dedup — a deterministic, mechanical fix.
- sea-orm-0023 (sim 0.949): `token_disjoint`+`disjoint_edits` accepts →
  cargo: `.iter` not found on an associated type + type annotations
  needed; `plain_llm` retry hit the identical errors (missing trait
  import outside the unit — **C1 class again**). Repeats were
  ESC/PASS/ESC — a coin-flip that majority-rule honestly kept as
  ESCALATE.

---

## Class D: Convergence timeouts and the retry-cap ceiling (4 cases)

- axum-0002 (sim 0.859, harvest same): unit 2 cycled; the convergence
  escape hatch accepted a compiled candidate with advisory-only
  blockers, but the run still hit convergence timeout. Repeats
  ESC/ESC/**PASS** — mid-band coin-flip.
- axum-0033 (sim 0.981, harvest same): multi-unit file; source
  portfolio accepted unit 1, later units cycled to timeout.
- sea-orm-0011 (sim 0.793, 870s): sbcr failed validation on every
  round; the unit-count-aware retry cap (1 retry — the file has many
  units) exhausted; final sbcr fitness **0.591 vs floor 0.60**. The
  closest possible miss. The retry cap is protecting wall-time, not
  correctness — C4's interleaving and a fitness-floor revisit both
  touch this.
- sea-orm-0014 (sim 0.858, 489s): sbcr validation loop → source
  portfolio accept → overall timeout.

---

## Class E: Model-capability frontier (2 cases)

- tokio-0108 (MODEL_NEEDS_HUMAN, sim 0.857): no mechanism applied
  (structural: no rule; sbcr: modification conflict); the model
  explicitly requested human review.
- sea-orm-0027 (OTHER, sim 0.682): `plain_llm` accepted → 1 mismatched
  type → single-compiling-side takeover fixed the file — but the
  side-collapse adjudication then kept `current` with
  buffer_in_replayed 0.49, and comment reconciliation failed twice.
  Semantic-heavy mid-band; honest escalate.

---

## Class F: Sandbox artifact (1 case) and the era census (24 cases)

- sea-orm-0004 (GATE_UNAVAILABLE, sim 0.981, 3/3, identical to
  harvest): the oracle text fails the same gate — a sandbox artifact,
  not a resolver failure. Stable across s20→s22.
- Era-dead 24: **sea-orm 9** — `sea-query ^0.18.0` no longer resolves
  (dependency-selection failure, identical rc=101 on current, replayed,
  and oracle); **tokio 15** (0099–0116) — rustc attribute/lint drift
  (`#[deprecated]` on trait impls now error, etc.) hitting all three
  sides. tokio-0109 is the documented s19 census-correction case. The
  probe classified all 24 identically to the harvest — zero
  classification churn.

---

## Summary table

| class | cases | nature | action |
|-------|-------|--------|--------|
| A: deterministic regressions | 3 | repair-layer gaps | C1 (now cross-lang), C4, new R1 |
| B: resurrection stops | 3 | honest conservatism, sim up to 1.0 | P5 provenance-aware guard |
| C: union compile defects | 2 | duplicate re-exports; missing trait import | micro-CEGIS stage-1 dedup; C1 |
| D: timeouts / retry cap | 4 | cycling + cap exhaustion (fitness 0.591 vs 0.60) | C4 |
| E: model frontier | 2 | needs-human; semantic mid-band | none (honest) |
| F: sandbox artifact | 1 | oracle fails gate too | none |
| era-dead | 24 | dependency/rustc drift | probe working |

**Bottom line**: The active Rust failure population is 15 cases. The
shard's only regressions are three deterministic repair-layer gaps —
two confirming existing C-shard items (C1, C4) and one new (R1,
post-coherence-repair verification). The improvement (axum-0005,
ESCALATE→PASS at sim 1.0) is cleanly attributable to the
midband-subsumption gate taking the current side whole. Two coin-flip
cases (axum-0002, sea-orm-0023) each passed one of three repeats —
best-of-N would convert them, and the honest majority rule did not.
Zero WORKING/NEAR_MATCH verdicts: Rust's compiler gate leaves no
graded middle band — cases are PASS or they escalate.

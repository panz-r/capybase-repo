# Sprint-21 results — data-driven mechanisms on the harvested frontier

Status: **development phase COMPLETE** (2026-08-22, final suite gate
6223 passed / 0 failed / 0 xfailed in 56m25s). Companion:
`PLAN-LEDGER-S21.md` (working log), `docs/sprint21-decision-template.md`
(pre-registered thresholds), `PLAN-LEDGER-S20.md` + harvest census.

Every sprint-21 item traces to harvested data, per the directive. Baseline
entering the sprint: raw real-conflict PASS 440/653 = 67.4%, era-adjusted
440/494 = 89.1%, era census 166, mechanism waterfall structural 832 /
portfolio 171 / LLM 276.

## What was built

| # | Mechanism | Commit | Live validation |
|---|-----------|--------|-----------------|
| S21.1 | Mixed-signature era-probe semantics (decline only pure-environmental) | d1b92d9 | unit-tested both directions |
| S21.2 | Golden-path few-shot (offline reconstruction → seeding → causal proof → default ON) | bea1815→f5b32c9 | paired A/B: OFF 4/4 escalate, ON 0037 PASS + 0046 NEAR_MATCH, zero regressions |
| S21.3 | Triage curation (19/30 investigate = perfect-buffer escalates) | ab8c305 | backlog re-ranked around mechanisms |
| S21.4 | Era-corpus strategy: pinned toolchains DEFERRED | 8c29193 | explicit revisit triggers recorded |
| S21.5 | S20.10 combined splitting (member split composed into the cascade), default ON | 47efa6f→0fd2903 | 15-case cohort: 8 PASS / 2 WORKING / 0 regressions |
| — | Coherence-repair rung (splice-gate deterministic ladder) | ef3a2d5 | sqlite-0014 PASS sim 1.0; 0034/0049 classes cleared |
| — | Code-glued stray fallback + positional #endif | e4f9e12, 40fc0c9 | 0040 dispositioned (truncation); guards pinned |

## The perfect-buffer family (sim ≥ 0.94 escalates) — fully dispositioned

- **0014 → PASS 1.0** (the rung's clean win)
- **0034 / 0049**: coherence classes CLEARED; distinct deeper defects
  exposed (string literal; unit re-resolve)
- **0040**: content truncation — LLM-path territory

## The golden-path arc — the sprint's methodology story

Reviewer idea → pre-registered §F gate (535 reconstructed pairs) → store
seeding (the entire RAG pipeline pre-existed; seeding was the build) →
**three honest attribution corrections** (unwired env var; portfolio-path
variance; never-ran LLM loop) each caught by journal evidence → paired
majority-of-3 A/B = the causal proof → default ON. The corrections are
the story: variance dressed as mechanism three times, and the
experimental discipline caught it every time.

## Decisions recorded

- S20.10 combined splitting: ENABLED (pre-registered rule met)
- P2 preservation: KEEP as documented unexercised net
- Mid-band fast path: stays OFF (0 idiomatic candidates)
- Era-corpus: pinned toolchains deferred (bounded payoff vs harness cost)
- Golden-path memory + RAG: default ON (empty store degrades to prior)

## Open threads for the next sprint

- 0034's string-literal defect; 0049's unit re-resolve (root causes,
  evidence-pathed in the ledger)
- The zenodo mid-band 0.76–0.89 remains the model-capability frontier
- Golden-path retrieval tuning for shapes where BM25 finds no neighbors
- The remaining oversized sub-unit attribution (sqlite-0004)

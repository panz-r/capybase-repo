# PLAN-LEDGER — Sprint 20 (living document, update as work proceeds)

Purpose: same durable-record discipline as sprint 19 (see
PLAN-LEDGER.md). **Read this first on resume.** Update after every unit
of work (implementation chunk, validation run, decision).

## Context (one paragraph)

Sprint 19 closed complete (P1-P6 + D8 extension; suite green at 6159
passed / 0 failed; docs/sprint19-results.md). Sprint 20's synthesized
plan (user-provided, critically evaluated against capybase's
constraints: no heavy parser, weak 4B model, compiler as ultimate
authority, conservative-by-construction escalation) arrives with a
**restructuring directive**: development proceeds case-by-case with
targeted majority-of-3 validation as each mechanism lands — the
full-corpus soak and every data-dependent decision it feeds move to the
END of the sprint, their outputs consumed by sprint-21 planning. The
full eval is expensive; this ordering makes progress faster. All work on
branch `dev`. Never push (user's job).

## Standing design constraints (the plan's rejections, binding)

- **No incremental partial-buffer verification / backtracking** — units
  aren't independently compilable; Phase-2 whole-file verification is
  the compiler-aligned boundary.
- **No cross-file signature propagation** — needs a full semantic
  parser; architectural shift, not a sprint-20 workstream.
- **No two-stage LLM semantic decomposition** — 4B JSON/intent
  extraction is a trap; latency without solving hard weaves.
- **Skeleton hash: eval-harness metric ONLY** — never overrides the
  compiler or production gates.
- **Edit-pattern reuse: intra-PR / intra-file only**, compiler-gated.

## Priorities

| # | Item | Status | Case-by-case acceptance |
|---|------|--------|-------------------------|
| S20.1 | Resolver R10 xfail: anchor-scoped `mechanical_reapply_merge` | DONE (9d03e3f) | the xfail'd test passes strict (no xfail); 543 resolver-related tests green; full suite gate launched (suite-s20-r1) |
| S20.2 | Toolchain-era preflight probe (`ESCALATE_TOOLCHAIN`) | DONE | live acceptance: tokio-0109 classified in **8.9s** as ESCALATE_TOOLCHAIN, unanimous 3/3 via the probe cache (D7 burned full majority-of-3 pipelines on it); strict conditions (all three texts fail the real gate, real compile errors, identical side signatures); python/crateless-rust/degraded-gate skip |
| S20.3 | queue.rs resurrection fingerprint investigation | DONE — verdict: no policy change | 0037/0046 are byte-identical conflicts (same base/cur/rep, two merge SHAs) whose HUMAN oracles resolve the replayed deletion oppositely (0037 keeps 6/7 deleted lines, 0046 keeps 0/7) — the backstop's stop is the correct conservative disposition on genuinely ambiguous ground truth. Offline census: 645 distinct groups; 30 dupe groups / 62 cases; only this pair diverges |
| S20.4 | Empty-resolution bounded retry | DONE | live-accepted on flask-0006: `recovery_retry=2` fired in every run (reframed prompt via the existing build_recovery_prompt path); all reframes ALSO came back empty → disposition honestly unchanged (ESCALATE / MODEL_NEEDS_HUMAN) — the model is a proven hard limit on this shape (9/9 empties incl. reframes); escalation path preserved exactly |
| S20.5 | Hygiene pack: lockfile exemptions, sweep centralization, `longrun` wrapper, ccache sloppiness measurement | TODO | cross-worktree hit rate measured on a protobuf case pair; stale-process sweep runs from every entry point |
| S20.6 | Micro-CEGIS: provenance-aware duplicate repair + missing-symbol micro-patch | TODO | 0065-class: `redefinition of X` → deterministic delete when one copy is base-verbatim and its parent deleted it; missing-symbol → 5-line-context LLM micro-patch; strictly compiler-gated, escalates on ambiguity |
| S20.7 | Skeleton-aware multi-brace repair | TODO | brace insertion at skeleton entity boundaries instead of EOF; nlohmann-0033-family fixtures pass |
| S20.8 | Move-and-edit transposition (diff-of-diffs) | TODO | journal-only first; deterministic transpose of B's edit onto A's moved block (>70% verbatim match), compiler-gated enable |
| S20.9 | Intelligent prompt compaction | TODO | oversized units: strip comments/blank lines from CONTEXT only (conflict sides verbatim); prompt shrinks measurably |
| S20.10 | Combined splitting: member split + statement-level splitter beneath (P5 enable) | TODO | 0055's prompt under the 8K window via the composed ladder; must-hold: sqlite entity-splitting cases |
| S20.11 | Skeleton intent metric (eval-only) | TODO | harness classifies low-jaccard/high-skeleton-similarity cases; zero production-gate change |
| S20.12 | END-OF-SPRINT DATA HARVEST | TODO | see below; journals archived + decision memo delivered |

## End-of-sprint data harvest (S20.12 — the LAST part; feeds sprint-21)

Full-corpus majority-of-3 soak (Rust + C++), journals mined for:

1. **Oversized-site census** — every `llm_skipped_oversized` firing →
   the true P5 cohort (D8 proved offline churn/density proxies
   over-select: dense-hunk ≠ oversized-prone).
2. **Corpus-wide preservation events** — `deletion_superseded` /
   `preservation_flagged` rates → the P2 keep-or-verify decision.
3. **Mid-band (0.75-0.90 churn) journal-only calibration** data.
4. **Skeleton-metric × jaccard cross-tab** → future metric design.
5. **PASS-rate + failure census** under all sprint-20 mechanisms.

All DECISIONS from these data are sprint-21 planning inputs — explicitly
postponed out of sprint-20 development per the user directive. Every
mechanism above carries its own case-by-case acceptance and does not
wait for corpus data.

## Success metrics (measured at the harvest)

| Metric | Sprint 19 baseline | Sprint 20 target |
|--------|--------------------|------------------|
| Overall PASS rate | ~89.7% | ≥ 93% |
| sim ≈ 1.0 build failures | 2 (protobuf-0065, fmt-0003) | 0 (Micro-CEGIS) |
| Oversized prompt escalates | 1 (protobuf-0055) | 0 (combined splitting) |
| Toolchain-incompatible | mixed into ESCALATE | clean `ESCALATE_TOOLCHAIN` |
| Oracle-divergent merges | 0 | 0 |
| Known correctness gaps | 1 (R10 xfail) | 0 |

Definition of done: R10 fixed and suite 100% green (no resolver
xfails); 0055 + 0065 converted to PASS or definitively dispositioned as
hard capability limits; queue.rs resurrection policy explicitly
documented; harvest journals archived with the sprint-21 decision memo;
zero oracle-divergent merges.

## Work log (append entries, newest last)

- 2026-08-20: ledger created from the synthesized plan + the
  restructuring directive (case-by-case development; whole-corpus
  data-dependent decisions postponed to the end-of-sprint harvest,
  consumed by sprint-21 planning).
- 2026-08-20 00:2x: **S20.1 DONE — R10 xfail closed** (9d03e3f). Root
  cause was two compounding defects on the replace-coalesced
  modify+delete shape: (1) the modify/delete overlap guard used
  `_base_deleted_lines` (delete opcodes only) — difflib coalesces
  "modify line + delete line" into one replace opcode, so the deletion
  was invisible and the guard passed; (2) mechanical ops were applied
  wherever their anchor TOKEN CONTENT occurred uniquely in the semantic
  text — when the rewrite deleted the op's true position, the
  substitution grafted onto an unrelated look-alike token (fabricated
  LINE1 from line1's 'line' while dropping replayed's line3 edit →
  emitted 'LINE1\nLINE2'). Fixes: `_base_removed_lines` (replace spans
  that net-lose base lines count as removals) wired into the guard, and
  anchor-scoped application through a base→semantic token alignment
  (an op applies only at a base position that survived inside an equal
  region; content-verified; insertions stay skipped; descending base
  order). The shape now declines honestly. Test un-xfailed, passes
  strict; 543 resolver-related tests green. Full suite gate: detached,
  log /tmp/capybase-live/s20/val/suite-s20-r1.log, tally lands via the
  progress.log recorder.
- 2026-08-20 00:4x: **S20.3 DONE — queue.rs resurrection verdict: no
  policy change; corpus duplication found instead.** The identical
  fingerprint (12 resurrected lines, queue.rs, unanimous 3/3 in both
  D7-b2-mech and D8-census) is NOT a scanner pattern across independent
  cases: `tokio-history-0037` and `tokio-history-0046` carry
  byte-identical base/current/replayed (same upstream conflict,
  conflict_path tokio/src/runtime/tests/queue.rs, two different
  downstream merge SHAs). The replayed side deletes 7 non-blank lines
  (a `struct Runtime;` stub + its `impl Schedule` block — dead test
  scaffolding). The two HUMAN oracles resolve that deletion
  OPPOSITELY: 0037's oracle keeps 6/7 of the deleted lines (293-line
  resolution, matching current), 0046's keeps 0/7 (273-line resolution,
  matching replayed). A conservative system cannot pass both twins:
  the D7 buffer was sim 1.0 against 0037's oracle — the stop cost that
  PASS — but the same buffer diverges from 0046's oracle; escalating
  both is exactly the designed behavior. Offline corpus census
  (content-hash over base+cur+rep): 677 cases → 645 distinct conflict
  groups; 30 duplicate groups covering 62 cases (mostly benign
  double-counting — identical oracles); EXACTLY ONE divergent-oracle
  pair (0037/0046). Harvest/sprint-21 note: dedupe or pair-treat in
  metrics — a deterministic resolver can PASS at most one twin of a
  divergent pair, so the pair bounds the achievable corpus PASS rate.
- 2026-08-20 01:2x: **S20.2 DONE — toolchain-era preflight live-accepted
  on tokio-0109** (scripts/live_eval_realworld.py +
  tests/test_live_eval_toolchain.py, 12 tests). `_toolchain_era_probe`
  compiles both pristine sides AND the oracle in the materialized
  worktree (the REAL gate: cargo check / the detected C build), before
  the pipeline spends budget; classifies only when all three fail with
  real compile errors and the sides' normalized signatures are
  IDENTICAL; probe cached per case across majority repeats (repeats
  skip even materialization). Python, crateless rust (standalone rustc
  on one file fails on `use crate::` for era-independent reasons), and
  degraded gates skip entirely; empty signatures (driver noise — the
  cf50f4b broken-gate class) never classify. Live discovery: cargo's
  summary line ("...due to 6 previous errors; 71 warnings emitted")
  differs between sides by the warning COUNT (0109: 71 vs 70) — driver
  summaries are excluded from signatures. Acceptance: ESCALATE_TOOLCHAIN
  3/3 in 8.9s wall (vs full-pipeline burns); census/terminal categories
  carry TOOLCHAIN_ERA; probe dicts recorded on results for the harvest
  audit even when classification declines.
- 2026-08-20 01:3x: **S20.4 DONE — empty-resolution recovery retry,
  live-accepted on flask-0006** (risk.py + 4 tests in
  test_recovery_retry.py; 69 tests green across risk/policy/attempt
  files). Root cause of the flask-0006 waste: empty candidates surface
  as failure_kind=parse_failed (NOT model_refusal), so the existing
  needs_human recovery branch never matched and the loop re-asked the
  IDENTICAL prompt three times (3 identical empties, unanimous).
  Fix: risk.decide grants `__recovery_retry__` when
  features.empty_resolution is set (NonEmptyResolutionValidator's flag)
  and the recovery budget remains — routed BEFORE the technical/retryable
  branches so the empty class reaches it; same separate budget/switch as
  the needs_human path (bounded once per unit by default; the eval
  override of 2 applies); no_op_repair/suspected_validator_error
  immediate-escalates keep priority. Live: recovery_retry=2 fired in
  every run; both reframed asks also returned empty → honest
  MODEL_NEEDS_HUMAN escalate preserved (acceptance: "escalation path
  unchanged when the retry also empties"). flask-0006 itself is now a
  PROVEN model-limit datum: one-side oracle (current = 21-line import
  cleanup, oracle == current verbatim; replayed adds one import inside
  the deleted region → structural rules decline), 9/9 empties including
  reframes. Harvest note: this shape (big one-side cleanup + tiny
  in-deletion addition) is a candidate for the S20.6 deterministic class.

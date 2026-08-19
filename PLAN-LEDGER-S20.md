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
| S20.2 | Toolchain-era preflight probe (`ESCALATE_TOOLCHAIN`) | TODO | tokio-0109 classified by one pristine-side probe pair (cacheable across repeats, ~seconds); passable cases behavior-identical |
| S20.3 | queue.rs resurrection fingerprint investigation | TODO | 0037+0046 journals/diffs read; explicit policy verdict documented (true positive vs test-file false positive) |
| S20.4 | Empty-resolution bounded retry | TODO | flask-0006: exactly one retry with reformulated constrained prompt; escalation path unchanged when the retry also empties |
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

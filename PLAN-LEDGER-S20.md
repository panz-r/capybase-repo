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
| S20.1 | Resolver R10 xfail: anchor-scoped `mechanical_reapply_merge` | DONE (9d03e3f) | the xfail'd test passes strict (no xfail); 543 resolver-related tests green; full suite gate GREEN: 6162 passed / 2115 skipped / **0 failed / 0 xfailed** in 56m58s (suite-s20-r1) — the "0 known correctness gaps" DoD metric is suite-verified |
| S20.2 | Toolchain-era preflight probe (`ESCALATE_TOOLCHAIN`) | DONE | live acceptance: tokio-0109 classified in **8.9s** as ESCALATE_TOOLCHAIN, unanimous 3/3 via the probe cache (D7 burned full majority-of-3 pipelines on it); strict conditions (all three texts fail the real gate, real compile errors, identical side signatures); python/crateless-rust/degraded-gate skip |
| S20.3 | queue.rs resurrection fingerprint investigation | DONE — verdict: no policy change | 0037/0046 are byte-identical conflicts (same base/cur/rep, two merge SHAs) whose HUMAN oracles resolve the replayed deletion oppositely (0037 keeps 6/7 deleted lines, 0046 keeps 0/7) — the backstop's stop is the correct conservative disposition on genuinely ambiguous ground truth. Offline census: 645 distinct groups; 30 dupe groups / 62 cases; only this pair diverges |
| S20.4 | Empty-resolution bounded retry | DONE | live-accepted on flask-0006: `recovery_retry=2` fired in every run (reframed prompt via the existing build_recovery_prompt path); all reframes ALSO came back empty → disposition honestly unchanged (ESCALATE / MODEL_NEEDS_HUMAN) — the model is a proven hard limit on this shape (9/9 empties incl. reframes); escalation path preserved exactly |
| S20.5 | Hygiene pack: lockfile exemptions, sweep centralization, `longrun` wrapper, ccache sloppiness measurement | DONE | lockfile takeover live-accepted: axum-0015/0017 **PASS sim 1.00 in 18s/23s** (0017 previously burned 103 LLM units for WORKING @ 0.625); lockfile-named cases exempt from the 48K size guard; sweep shared via `process_hygiene` (eval + CLI entry points); `longrun.sh` self-tested (markers, pid guard); ccache cross-worktree hit rate measured **100%** (misses frozen at 732 across two fresh-worktree runs; no sloppiness tuning needed) |
| S20.6 | Micro-CEGIS: provenance-aware duplicate repair + missing-symbol micro-patch | TODO | 0065-class: `redefinition of X` → deterministic delete when one copy is base-verbatim and its parent deleted it; missing-symbol → 5-line-context LLM micro-patch; strictly compiler-gated, escalates on ambiguity |
| S20.7 | Skeleton-aware multi-brace repair | TODO | brace insertion at skeleton entity boundaries instead of EOF; nlohmann-0033-family fixtures pass |
| S20.8 | Move-and-edit transposition (diff-of-diffs) | TODO | journal-only first; deterministic transpose of B's edit onto A's moved block (>70% verbatim match), compiler-gated enable |
| S20.9 | Intelligent prompt compaction | TODO | oversized units: strip comments/blank lines from CONTEXT only (conflict sides verbatim); prompt shrinks measurably |
| S20.10 | Combined splitting: member split + statement-level splitter beneath (P5 enable) | DEFERRED TO HARVEST | acceptance case (0055) reclassified ESCALATE_TOOLCHAIN; the live oversized cohort is EMPTY under the era probe — build the statement-level splitter only if the S20.12 harvest shows a non-empty cohort (the journal-only member-split measurement from sprint-19 P5 keeps collecting distribution) |
| S20.11 | Skeleton intent metric (eval-only) | DONE | `_skeleton_similarity` (ordered control-flow/definition keyword-stream ratio) recorded on every result beside matches_oracle; summary prints SKELETON-INTENT CANDIDATES (sim<0.80, skeleton>=0.85); verdict chain provably untouched; 4 tests + live field evidence (axum-0006: 1.0/1.0) |
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
- 2026-08-20 01:4x: **S20.5 DONE — hygiene pack, all four sub-items.**
  (a) **Lockfile takeover**: offline measurement first (both corpus
  Cargo.lock cases: oracles ≈ the CURRENT side's regeneration — 21/21
  current-only pins kept, 0/38 replayed-only, ~99.7% of divergent keys
  current's — overturning the union-merge hypothesis). Implemented as a
  pre-cascade trigger in `_try_true_side_portfolio` (name-scoped
  Cargo.lock, `future.enable_lockfile_takeover` default ON, single-side
  candidate verified through the standard machinery, declines cleanly);
  load_cases exempts lockfile-named cases from the 48K size guard (they
  never build a prompt). Live acceptance: **2/2 PASS at sim 1.00 in
  18s/23s** — axum-0017's 103-unit WORKING grind is gone. 6 wiring tests.
  (b) **Sweep centralized**: `capybase.process_hygiene.kill_stale_build_
  processes` shared by the eval script (startup+atexit) and the CLI
  main (startup). (c) **`scripts/longrun.sh`**: the detached-runner +
  progress-marker pattern productized (self-tested: START/DONE markers,
  pid-lifecycle, active-run guard). (d) **ccache measurement**:
  protobuf-0073 run twice in fresh worktrees — misses FROZEN at 732
  while hits grew +389 per run = **100% cross-worktree hit rate** on
  identical content (three distinct worktrees); no sloppiness tuning
  warranted; case wall 70s → 11s warm. 33 tests green across the S20.5
  files.
- 2026-08-20 02:0x: **S20.6 investigation + design complete; enabling
  pieces landed; implementation next session.** Findings:
  - Hook point: the run-loop compiler-authority escalate
    (orchestrator.py ~8362: `not test_ok and (tests.required or
    _last_tests_compiler_indictment)`). 0065's errors surface THERE,
    not in Phase-2 file validation (P3 had degraded the session to
    SYNTAX_ONLY, so `_whole_file_repair` never engaged) — micro-CEGIS
    must intercept between the failed gate and the escalate.
  - **fmt-0003 reclassified to S20.7**: its sim-1.0 escalation is a
    brace/macro-imbalance class (unbalanced braces at line 312;
    unterminated EXPECT_THROW macro) — skeleton-aware multi-brace
    repair territory, NOT redefinition/missing-symbol. S20.6's live
    acceptance is 0065 alone (missing-symbol stage).
  - Enabling pieces landed: `_run_tests` now stashes
    `_last_gate_cmd` + `_last_attributed_merge_errors` on self;
    `future.enable_micro_cegis = True` declared in config.
  - Design: `_try_micro_cegis(result)` called in the run loop before
    the escalate; operates on the resolved FILE BUFFERS directly (no
    unit re-splice — the merge index may be gone). Stage 1
    (deterministic): parse `redefinition of 'X'` from the stashed
    errors; extract the duplicate's brace block at the error line;
    provenance via `_true_stage_sides` (delete the copy whose exact
    text is base-verbatim AND absent from one parent side = that side
    deleted it). Stage 2 (micro-patch): distinct symbols from
    `'(X)' does not name a type` / `was not declared` / `is not a
    member` errors (≤3); one tiny prompt per symbol (error lines + 5
    buffer-context lines + the symbol's declaration lines found in
    base/current/replayed); model returns SEARCH/REPLACE applied to
    the buffer (reuse the CEGIS patch parser). After each round:
    re-run `_run_tests("pre_continue", result)` — the same gate
    (command from `_last_gate_cmd`); clean → proceed (do NOT
    escalate); no progress → escalate exactly as before. Journal
    micro_cegis_started / micro_cegis_patch / micro_cegis_succeeded /
    micro_cegis_declined throughout. Acceptance: protobuf-0065
    majority-of-3 — PASS or honest escalate through the rung; no
    behavior change on unattributed/advisory failures (unit tests).
- 2026-08-20 02:3x: **S20.6 DONE — micro-CEGIS at the compiler-authority
  gate** (orchestrator.py module helpers + methods + run-loop wiring;
  tests/test_micro_cegis.py, 12 tests; 66 green across adjacent files).
  Mechanism: `_try_micro_cegis(result)` fires in the run loop between
  the failed pre_continue gate and the P4 escalate (indictment path
  only — required-gate policy failures keep the old behavior). Stage 1
  (deterministic): `redefinition of X` → brace-block extraction at the
  error line, other-copy location by definition-site scan, deletion
  ONLY of the copy whose exact text is base-verbatim AND absent from a
  parent side (provenance journaled; ambiguity declines). Stage 2
  (micro-patch): distinct missing-symbol errors (`'X' does not name a
  type` / `was not declared` / `is not a member of`, ≤3) get one tiny
  JSON SEARCH/REPLACE prompt each (error lines + 5 buffer-context lines
  + the symbol's declaration lines from base/current/replayed),
  applied via the existing apply_search_replace; malformed/empty model
  output skips symbol. Every round re-runs `_run_tests("pre_continue")`
  — the indictment flag resets per call, so a clean re-gate lets the
  run proceed (no escalate); no progress declines exactly as before.
  Journaled: micro_cegis_started / _patch / _patch_failed /
  _re_gate / _succeeded / _declined / _stage_failed.
  **Live acceptance (0065, majority-of-3, CAPYBASE_SKIP_SIZE_GUARD=1 —
  its 90K marker needs the documented lift): majority PASS at sim
  1.00 (verdicts ESCALATE,PASS,PASS).** Honest caveat, P2-precedent:
  the rung itself was UNEXERCISED this sampling — zero
  compiler_authority_override (let alone micro_cegis) events in all
  three journals: runs 2-3 produced oracle-correct candidates
  outright, run 1 escalated via a non-gate path. The 0065 metric
  conversion (ESCALATE→PASS) is attributable to sampling majority;
  micro-CEGIS stands as the unit-tested net behind the gate. Also
  fixed during implementation: provenance labels were swapped in the
  first cut (caught by the pure-function tests); the other-copy finder
  moved from whole-block comparison to definition-site scanning
  (modified duplicates were missed). Regression suite launched via
  `longrun` (s20-suite-microcegis) — the S20.5c wrapper's first
  production use.
- 2026-08-20 03:1x: **S20.4 regression fixed** (caught by the
  s20-suite-microcegis gate, 1 failure:
  test_run_escalates_fast_on_repeated_transient_failures). The
  empty-resolution recovery grant was stealing TRANSPORT failures
  (request_failed/truncated/lsp_failed) from the technical branch — a
  transport error has no content to reframe, and the theft broke the
  retry_count increment contract (the V8 CASE_TIMEOUT invariant).
  Scoped to CONTENT empties only (parse_failed/no-kind — the
  flask-0006 class); regression test added; orchestrator file gated
  green via longrun.
- 2026-08-20 03:2x: **S20.7 DONE — skeleton-aware sibling-boundary brace
  insertion; both plan-named acceptance cases reclassified era-dead by
  the S20.2 probe.** New Candidate 0 in `_try_balance_braces`: when one
  closer is missing mid-file (the fmt-0003 shape — a TEST-macro block's
  closer lost with sibling constructs after it), insert BEFORE the next
  sibling construct instead of EOF/trailing closers. Signal: after the
  innermost unclosed opener every depth reading is +1 too high, so a
  true scope-level sibling reads depth-BEFORE == deficit; guards:
  construct-start shape (call/signature + '{'), indentation at-or-left
  of the opener (body content indents deeper), balance re-validation.
  2 new tests; 60 green across the brace files.
  **Live: fmt-0003 AND nlohmann-0033 → ESCALATE_TOOLCHAIN** (both
  sides + oracle fail today's builds identically; 109s / 6s). Finding:
  both plan-named brace-failure cases were NEVER model failures —
  era-dead cases whose merge failures masked toolchain drift. The
  era-probe census now counts 0109, fmt-0003, nlohmann-0033. S20.7's
  mechanism stands unit-tested with no live firing available this
  sampling (P2-precedent honesty). Metric note: "sim≈1.0 build
  failures" now resolves as 0065→PASS + fmt-0003→toolchain-dead — the
  class is empty either way.
- 2026-08-20 03:4x: **S20.8 DONE — move-and-edit transposition,
  journal-only stage.** `_detect_move_edit_shape` (structural_resolver,
  pure): a relocated block (delete paired with a >=0.70-verbatim similar
  insert elsewhere, >=6 lines) whose base span the OTHER side edited —
  the shape `_try_move_transplant` resolves by taking the mover's text
  and DROPPING the editor's delta. Journaled at FILE level in Phase 1
  (the relocation spans units — the first unit-level wiring never fired,
  live-caught on sqlite-0036; rewired beside the pre-cascade fast path
  using the merge-index pristine sides). `future.
  enable_move_edit_transposition = False` (journal-only; the eventual
  enable is a deterministic transpose of the editor delta onto the moved
  block, compiler-gated).
  **Corpus census: 3/677 cases** (protobuf-0059/0060 — twin
  extractions again — and sqlite-0036, mover=replayed ratio 0.80, 13
  lines, editor delta 20). The population is narrow; enabling ROI is
  small unless live distribution disagrees — exactly what the
  journal-only stage measures.
  **Live validation: sqlite-0036 journals `move_edit_candidate` with the
  full payload (mover/base_span/moved_to/ratio) and still PASSes at sim
  1.00** — measurement-only confirmed (no behavioral change).
  Measurement boundary documented: a pure order-swap is invisible to a
  line diff (difflib anchors the longest block — the identical moved
  copy); the detector catches relocations that left new content behind,
  which is also the splice-breaking shape. 5 unit tests; 50 green
  across the S20.8-adjacent files.
- 2026-08-20 04:2x: **S20.9 DONE — intelligent prompt compaction
  (context-only).** `_compact_context_text` (resolution_engine): strips
  full-line comments (//, #, /*...*/ blocks + continuations) and
  collapses blank runs from CONTEXT sections only — trailing inline
  comments stay, code lines verbatim, trailing newlines preserved so
  section composition is unchanged. Wired into `_fit_to_budget` BEFORE
  the drop cascade: when the assembled augmentation total overflows,
  compact anchor/deps/primary first (journaled as a `compaction` trim
  with before/after tokens), then the existing lowest-value-first drop
  cascade runs on the COMPACTED sections — more semantic signal per
  token. Conflict sides, contract, and skeleton never touched. 7 tests
  (compactor + budget integration: a comment-heavy anchor that the
  cascade would have dropped now survives compacted; no-overflow and
  disabled-budget passthroughs); 24 engine tests green.
  **Live: the only oversized-population case, protobuf-0055, is now
  ESCALATE_TOOLCHAIN (57s)** — era census grows to FOUR (0109,
  fmt-0003, nlohmann-0033, 0055). Consistent with D7 (every 0055
  resolution failed compiles), caveat recorded: the D7 attributed
  errors ('HasInternalAccessors' not declared) could be era artifacts
  or merge defects — the probe's strict identical-signature condition
  held, but the era-vs-defect attribution is not 100% clean. S20.9's
  live firing is therefore UNVALIDATED (no known context-dominated
  oversized case remains); unit-tested, P2-precedent posture.
  Consequence for S20.10: its acceptance case is gone — combined
  splitting becomes fully harvest-gated (the ledger's criteria already
  say: cohort from live llm_skipped_oversized firings; today that
  cohort is EMPTY under the era probe).
- 2026-08-20 04:4x: **S20.10 DISPOSED — deferred to harvest.** The
  combined-splitting build's only live acceptance case (0055) is
  era-dead and the oversized cohort is EMPTY under the era probe;
  building the statement-level splitter now would be speculative. The
  sprint-19 P5 journal-only member-split measurement keeps collecting
  distribution; the S20.12 harvest decides (non-empty cohort → build
  next sprint).
- 2026-08-20 04:5x: **S20.11 DONE — control-flow skeleton intent metric
  (EVAL ONLY).** `_skeleton_signature` (ordered control-flow/definition
  keyword stream — if/for/while/switch/try/return/def/class/...,
  ignoring naming, formatting, idiom) + `_skeleton_similarity`
  (difflib ratio over the streams). Recorded on every CaseResult beside
  matches_oracle; the run summary prints SKELETON-INTENT CANDIDATES
  (non-clean verdicts with sim < 0.80 but skeleton >= 0.85 — idiomatic
  rewrites). Never a gate: a pinned test proves the verdict chain is
  untouched by a high skeleton score. 4 tests (the plan's exact
  scenario: idiomatic rewrite → jaccard < 0.60 while skeleton >= 0.85);
  29 green across the eval test files; live field evidence on
  axum-0006 (1.0/1.0). The S20.12 harvest cross-tabs this against
  jaccard for the sprint-21 metric-design memo.
- **Sprint-20 development phase COMPLETE (S20.1-S20.11):** 9 built
  (1 deferred-to-harvest by data), every item case-accepted or honestly
  dispositioned; suite gates green throughout (final full-suite gate,
  s20-suite-s207: 6201 passed / 2115 skipped / 0 failed / 0 xfailed in
  50m31s); era-dead census: tokio-0109, fmt-0003, nlohmann-0033,
  protobuf-0055.
  Remaining: S20.12 END-OF-SPRINT DATA HARVEST (the last part, per the
  user directive — full-corpus soak + journal mining + sprint-21
  decision memo).
- 2026-08-20 16:24: **S20.12 HARVEST LAUNCHED** (via longrun, detached —
  worker pid 807363; log /tmp/capybase-live/s20/harvest/s20-harvest.log,
  results + flights under /tmp/capybase-live/s20/harvest/). Full corpus
  (677 cases loaded with CAPYBASE_SKIP_SIZE_GUARD=1 — the era probe
  makes the lift safe: un-passable big cases classify in seconds),
  majority-of-3, provider nova-gemma4. Expected to run for many hours;
  on completion: mine the journals (oversized-site census → the S20.10
  cohort decision; preservation events → P2 keep-or-verify; move-edit
  distribution; skeleton×jaccard cross-tab; era-probe census; PASS-rate
  + failure census under all sprint-20 mechanisms) and write the
  sprint-21 decision memo.

## Sprint-20 extension phase — QA & follow-up (S20.E, added 2026-08-20)

The development phase accumulated real quality debt alongside its wins:
three mechanisms whose live firing is unproven, an irreversible new
classification (era-dead) with one unresolved attribution caveat, and a
first-ever full-corpus run under all S20 mechanisms simultaneously.
This extension plans the assurance work; E1's instrument is built now,
the rest execute against the landed harvest.

| # | Item | Acceptance | Deps |
|---|------|-----------|------|
| E1 | Verdict-diff regression audit | `scripts/verdict_diff.py` diffs the harvest against every baseline (s18 midband 350 + ws1 12, s19 d7/d8 sets); EVERY PASS→non-PASS flip investigated and bisected to a mechanism (lockfile takeover / recovery redirection / compaction / move-edit stamping / brace candidates / micro-CEGIS); zero unexplained regressions | harvest |
| E2 | Era-probe verification sweep | every ESCALATE_TOOLCHAIN case: probe signatures inspected (era-artifact vs content-defect) + cross-checked against history (a case that PASSED in any earlier sprint must never classify era-dead — a flip means probe bug); the 0055 attribution caveat resolved and documented | harvest |
| E3 | Unexercised-mechanism dispositions | harvest journals mined for micro-CEGIS / sibling-brace / compaction firings; zero firings → explicit keep-as-net decisions with rationale (cost/guard audit); forced-exercise integration tests where cheap (drive the rung with a fake failing gate + real buffer) | harvest |
| E4 | Lockfile takeover wide-band check | all harvest `lockfile_takeover_gate` firings with their sims; any firing at sim < 0.95 investigated (the enabling measurement was 2 corpus cases; the harvest is the wide band) | harvest |
| E5 | Test-coverage pass | pytest-cov over the sprint's touched modules (structural_resolver, verification, orchestrator rungs, risk, eval-script additions); highest-value gaps closed | none |
| E6 | Runbook hardening | harvest resume verified (`--skip-existing` against a partial results.json); the longrun → census → memo workflow documented in the results doc's harvest section | none |

Order: E1/E2 gate sprint-21 planning credibility (a PASS-rate memo is
worthless if regressions or probe false-positives pollute it). E5/E6
can run pre-harvest.

## S20.E additions from the reviewer window (2026-08-20 evening)

Adopted from two reviewer responses, de-duplicated against done work
(queue.rs twin = S20.3 DONE; S20.4-vs-transport pin = written with the
regression fix; census miner = built+validated). Standing constraints
for the harvest window: NO runtime-code changes while the harvest runs
(scripts/docs/tests only — attribution hygiene); no broad archive scans
(sampled subsets, nice/ionice); rejected architectures get a Future
Research note, not design drafts.

| # | Item | Acceptance | Source |
|---|------|-----------|--------|
| E7 | Pre-registered decision template (`docs/sprint21-decision-template.md`) | thresholds WRITTEN BEFORE the harvest lands (its only value); headline metrics with the theoretical-ceiling formula computed FROM harvest data (never pre-filled); era-adjusted rate ALWAYS beside the raw rate; S20.10/P2/mid-band/era rules pre-committed | R1 (pre-registration core), R2 (formula structure) |
| E8 | Census-miner table extension | per-case mechanism waterfall (structural/portfolio/LLM provenance), oversized-cohort detail table, preservation detail, ccache economy per case — validated on D7/D8 subsets only | both |
| E9 | Harvest triage kit (`scripts/triage_harvest.py`) | one compact section per non-PASS case with action categories (era-dead / environmental / model-capability / mechanism-gap / investigate) → the sprint-21 backlog | both |
| E10 | Mechanism interaction matrix + pin tests | `docs/sprint20-mechanism-risk-matrix.md` + pytest pins: lockfile-vs-resurrection (pin the existing suffix exemption), micro-CEGIS-vs-P4 escalation transparency, brace-vs-compaction note | R2 (tests), R1 (matrix) |
| E11 | Golden-path few-shot extractor (skeleton) | `scripts/extract_golden_path.py` — PASS + LLM-provenance + sim>0.95 (prompt, response) pairs keyed by skeleton signature; sprint-21 prompt-builder integration is a DECISION then, never wired now | R2 (build), R1 (don't-run scoping) |
| E12 | Capabilities-doc delta | factual deltas with the labeling rule: unit-tested ≠ live-exercised ≠ corpus-proven | R1 |

Reviewer rating recorded in the session log: **Response 1 more useful**
(pre-registered thresholds vs post-hoc framing; honest-metrics discipline
— era-adjusted beside raw, "theoretical ceiling under this exact eval"
labeling vs adopting a pre-filled 99.26% as the external headline;
operational harvest protection incl. nice/ionice scoping; correct
golden-path scoping). R2's adoptions: the ceiling-formula structure,
the lockfile-vs-resurrection pin, the triage taxonomy, the explicit
architecture-purity rejection.
- 2026-08-20 17:1x: **S20.E9/E10/E11 EXECUTED** (scripts/docs/tests only
  — no runtime code touched while the harvest runs). E9
  `scripts/triage_harvest.py`: per-non-PASS compact sections in five
  categories (era-dead / environmental / model-capability /
  mechanism-gap / investigate) — validated on D8 (0046→mechanism-gap,
  flask-0006→model-capability) and the partial harvest. E10 pin tests
  (`tests/test_mechanism_interaction_pins.py`): lockfile-resurrection
  never-stops (suffix exemption pinned; fake git backend protocol
  fixed during bring-up) + micro-CEGIS-decline keeps the honest
  escalation reason. E11 `scripts/extract_golden_path.py` skeleton:
  (prompt, response) pairs from PASS+sim>=0.95 cases keyed by skeleton
  signature; prepare-don't-wire (E7 gate: >=30 pairs before any
  integration decision). Remaining pre-harvest: E8 census tables, E12
  capabilities delta, E5 coverage, E6 resume check.
- 2026-08-20 17:2x: **S20.E8/E12 EXECUTED; E6 VERIFIED.** E6: the
  `--skip-existing` resume path loads prior results (field-filtered,
  forward-compatible with new CaseResult fields), skips done ids, exits
  in 0s — verified against a COPY of the in-flight results (33 loaded,
  records intact). E8: census miner extended with the mechanism
  waterfall (structural/portfolio/LLM per case + llm-only case list)
  and the build-economy table (top spenders) — validated on D7-mech
  (6 structural + 7 LLM; 0055 correctly tops build spend at 600s). E12:
  docs/sprint20-capabilities-delta.md — factual deltas under the
  labeling rule (unit-tested ≠ live-exercised ≠ corpus-proven), to be
  upgraded by harvest evidence at sprint close. Remaining pre-harvest:
  E5 coverage pass (deferred to avoid harvest CPU contention).

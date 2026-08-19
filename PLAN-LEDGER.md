# PLAN-LEDGER — Sprint 19 (living document, update as work proceeds)

> **Sprint 19 is COMPLETE** (2026-08-20, through commit fef1d5d) — the
> active sprint lives in `PLAN-LEDGER-S20.md`. This file is the record.

Purpose: zcode sessions have been crashing mid-sprint. This file is the
durable plan + progress record. **Read this first on resume.** Update it
after every unit of work (implementation chunk, validation run, decision).

## Context (one paragraph)

capybase is a conservative conflict-resolution rebase engine (structural
rules → combination search → source portfolios → small-prompt LLM with
CEGIS, targeting a ~4B/8K-window model). Sprint-18 live validation (21
slots, majority-of-3) had 7 misses; journal archaeology produced
`docs/sprint19-failing-cases-diagnosis.md` (designs D1–D7) and
`docs/sprint19-open-questions-for-review.md` (Q1–Q6). Two external
reviewers responded; their recommendations were synthesized into the
Sprint-19 refined plan (pasted by user; preserved in full below). All
work is on branch `dev`. Never push (user's job).

Key facts:
- Oracle-shape census: 68.5% woven / 23.8% ==current / 7.7% ==replayed.
  Five of seven sprint-18 failures are `==current` (0065, 0067, 0071,
  tokio-0037, tokio-0109).
- Transport-failure → empty-fallback side-pick bug already FIXED (66b780b)
  with a dead-client test. sea-orm-0027 + tokio-0109 need only reruns (D7).
- Sprint-18 full hermetic suite (~8207 tests) was running in
  `/tmp/capybase-live/s18/full-suite-postfix.log` — at session start it was
  ~99% with ZERO failures. Verify final tally on resume; if it finished
  green, record it in the ledger and in docs later.

## Sprint 19 priorities (from synthesized reviewer plan)

Resolutions of reviewer disagreements (final):
1. Q1 → **R2's Best-of-N recovery + churn-aware heuristic**, plus R1's
   metadata tagging (`flagged_by_preservation_heuristic`) as complement.
   NOT accept-then-adjudicate as default.
2. Q2/Q3 → whole-side repair as a **repair rung AFTER spliced-buffer
   compile failure** (NOT a pre-emptive stage — churn can't separate the
   classes; 79 woven counter-examples). Use R1's implementation insights:
   parallel side compiles, cache across majority-of-3 repeats,
   `-fsyntax-only` probes, LLM adjudication with churn context,
   decision matrix (see P1 below).
3. Q6 → **conditional retry** (lock_contention/network_transient/
   compiler_crash retry at 2× cap, max 2 attempts; generic timeout →
   zero retries, degrade to SYNTAX_ONLY per Build State Machine).

| # | Item | Status | Notes |
|---|------|--------|-------|
| P1 | Whole-side repair rung on compile failure | DONE (impl+tests) | see P1 detail below; D5 carve-out extension included |
| P2 | Churn-aware preservation heuristic + Best-of-N recovery | DONE (impl+tests) | see P2 detail below |
| P3 | Build state machine + conditional retry (+ccache/-j$(nproc)) | DONE (impl+tests) | see P3 section; D2 prewarming deferred |
| P4 | Compiler-authority override at final gate | DONE (impl+tests) | see P4 section; fixes protobuf-0065 |
| P5 | Class-with-methods entity splitting (journal-only first) | DONE (journal-only stage) | flag default OFF; enabling awaits live calibration |
| P6 | Near-verbatim band calibration (measure-only) | DONE | script + results below |
| D7 | Post-fix live rerun matrix (sea-orm-0027, tokio-0109 first) | DONE | all batches + 0065 fixed-gate rerun landed 2026-08-19 |

## Sprint-19 extension (replan, 2026-08-19 evening — user directive:
## no deferrals; remaining work rejoins this sprint as D8)

Context: the fresh s19 suite (r1) was killed at 66% by a zcode restart —
and the investigation that followed found the box at load ~92 from ~274
build processes leaked by every timed-out full build since 02:38, plus a
fully-broken ccache wiring (995/995 calls uncacheable, self-recursion
livelock). Both fixed in f836488 (with regression tests). D8 items:

| # | Item | Status | Notes |
|---|------|--------|-------|
| D8.0 | Build-process hygiene (tree-kill + ccache recursion) | DONE (f836488 + follow-up) | `_run_shell_tree` (session + killpg) at every shell build site; shims exec absolute compilers, no CC/CXX double-wrap; stale-process sweep matches by worktree cwd. Follow-up (same day): `CCACHE_NOHASHDIR=1` + `CCACHE_BASEDIR=/var/tmp` (cross-worktree hits — verified live: default env MISSES identical content across two worktrees, NOHASHDIR HITS), `CCACHE_TEMPDIR` on disk (off the 6G /run tmpfs the orphans filled), `CCACHE_MAXSIZE=20G`, and the live script now exports `_ccache_env()` into the process env so orchestrator/TestRunner gate builds inherit it (their counters were frozen). 7 tests in test_build_process_hygiene.py. |
| D8.1 | Full suite rerun over s19 changes (r2, post-fix) | DONE — GREEN | 6159 passed / 2115 skipped / 1 xfailed / **0 failed** in 54m52s (baseline 5h41m; char_ratio flake did not recur on the idle box; +67 passed vs s18 = s19's new tests). Log: suite-s19-r2.log. |
| D8.2 | P6 live deletion-carveout census | DONE — honest zero-event | 8 journals, **0 `deletion_superseded` events — the carveout path was never entered**: the easy pure-deletion cases resolve upstream (axum-0006 PASS 15s + jsonc-0002 PASS 20s, structural/portfolio, no preservation events) and the hard ones escalated through other nets (flask-0006: model empty resolutions, 3/3; tokio-0046: resurrection backstop caught 12 resurrected lines in queue.rs, 3/3 — the deletion-direction safety net live-validated). Carveout stays ON, unit-test-validated. |
| D8.3 | P5 oversized-distribution batch | DONE — stays OFF | 2/2 PASS (0053 sim1.00 96s `structurally_resolved`; 0073 sim1.00 70s `true_side_portfolio`) with ZERO oversized-skip events and ZERO candidate stamps: the corpus's densest hunks are one-side-dominated and never reach the LLM. **dense-hunk ≠ oversized-prone**; the only live distribution remains 0055. Enabling stays OFF; sprint-20 selection must come from live `llm_skipped_oversized` census firings, and enabling needs the statement-level splitter beneath (0055's member fragments alone are 150-250 lines, over-window). |
| D8.4 | Ledger/docs hygiene | DONE | SHA backfills (P1=4dcdd3f, P6=125323e), D7 status, ccache-claim corrections (P3 sections + results doc), untracked design docs committed (bab4c21). |
| D8.5 | Closing entry (suite tally + census/dist dispositions + P5 enabling call) | DONE | see work log; ccache production evidence: 776 cacheable / 44 hits / 0.2 GiB in the D8 legs (vs 995/995 uncacheable pre-fix), incl. cross-worktree hits. |

Acceptance for D8: suite r2 green-or-known-flake (char_ratio load-flake
baseline); census produces a measured unit-level carveout rate (or an
honest zero-event reading); P5 enabling decision recorded ON/OFF with the
accumulated distribution; no regressions in either batch's must-holds.

## P6 results (2026-08-19, scripts/calibrate_near_verbatim.py)

674 corpus cases; jaccard = the eval harness's token metric.

- **Band census**: ≥0.99-jaccard to one side: 81.6%; verbatim == 30.3%;
  ≥0.95: 95.8%. The "woven class" (both j < 0.95) is only 4.2% — token
  jaccard SATURATES on large files, so j≥0.99 does NOT identify "one
  side plus a thread"; most genuinely-woven merges also sit ≥0.99 to
  their dominating side.
- **Churn does not separate the band**: ≥0.99 cases split wholesale 201 /
  mid 176 / symmetric 173 — the band spans all regimes.
- **Residual concentration**: of 346 near-verbatim-not-verbatim cases,
  only 179 are concentrated (≤2 hunks, ≤20 lines) ≈ 26.6% of the corpus;
  the rest carry large scattered residuals (j=0.999 with 416 changed
  lines exists).
- **Conclusion (Q4 answer)**: no special fast-path on jaccard alone —
  the band is not clean. IF a path is ever built, it must gate on
  residual concentration (the 179-case subset), compiler verification,
  and adjudication; even then the woven-dominated class overlaps.
- **Deletion-carveout band (P2 premise, whole-file proxy)**: 25 cases
  have a pure-deletion side; oracle ≈ other side (≥0.99) in 14/25 (56%)
  — NOT a slam-dunk at file level. The unit-level carveout is much
  narrower (only when ALL missing obligations are non-exclusive dropped
  deletions in that unit), the file gates still run, and Best-of-N is
  independent — carveout stays ON but the live round must census
  `preservation_result="deletion_superseded"` events to measure the
  unit-level rate (counterexamples like flask-0006/0007,
  sqlite-0012 exist at file level).

## P5 design (as implemented, 2026-08-19 — journal-only stage)

- `config.py`: `future.enable_class_member_splitting = False` (journal-only).
- `conflict_extractor.py`:
  - `_class_member_split_points(side, lang)`: depth-2 boundary detector —
    an opener STACK tracks class-body depth (namespace-nesting safe);
    member-fn starts = signature lines directly inside the class opener
    that open a body within 2 lines with no ';' first; access specifiers
    (public:/protected:/private:) count as boundaries; declarations
    (ending ';'), data members, control keywords, and ','-continuation
    lines (initializer lists) are excluded.
  - `_stamp_class_member_candidate()`: on the entity-splitter's decline
    paths (no top-level boundary / fragments below min), stamps
    structural_metadata["class_member_split_candidate"] with per-side
    point counts + offsets + region lines. Pure measurement.
- `orchestrator.py`: `_journal_class_member_candidate()` journals the
  stamp (with the flag's enabled state) at BOTH oversized-skip sites
  (`llm_skipped_oversized`, `llm_skipped_oversized_prompt`).
- Tests: `tests/test_class_member_split.py` (11).
- Offline calibration data point (protobuf-history-0055): the single
  marker block is cur=516 lines / rep=0 (one-sided region); detector
  finds 3 member points in the current side (access specifier, ctor
  continuation, GenerateParserLoop). A member split alone yields ~2-3
  fragments of 150-250 lines — still over an 8K window alone; the
  enabling stage would need the statement-level splitter
  (`_find_statement_split_points`, already in the ladder) beneath it.
  Live runs will journal the real distribution before any enabling
  decision (sprint-18 discipline).
- Must-hold when enabling later: sqlite entity-splitting cases; splice
  safety (non-overlapping spans, no reordering, access specifiers
  preserved per fragment).

## P4 design (as implemented, 2026-08-19)

- `orchestrator.py` `_run_tests`:
  - D4.1: error-carrying lines from the command output are extracted and
    surfaced in `tests_finished` (`diagnostics` falls back to them when
    the verdict parser found none; `build_gate` + `attributed_merge_errors`
    fields added).
  - D4.2 compiler-authority attribution: when the gate command is a build
    (`_phase2_fallback_build_cmd` recognition), it failed, didn't time
    out, and error lines POSITIVELY parse (`_parse_cc_error_location`) to
    a stem matching a merged file (`result.units_by_path`) →
    `compiler_authority_override` journal event, warning, gate returns
    False regardless of tests.required. Strict positive attribution only:
    sibling errors, driver summaries, unparseable lines, and timeouts
    keep advisory behavior.
- Caller (run loop, pre_continue): escalates when
  `not test_ok and (tests.required or _last_tests_compiler_indictment)`
  with a distinct reason for the override path.
- Tests: `tests/test_compiler_authority.py` (8) — build recognition,
  attributed escalation under advisory (0065 shape incl. diagnostics
  surfacing), sibling/unparseable/timeout/non-build exemptions, passing
  and required-gate baselines.
- Regression: 100 tests green.

## P3 design (as implemented, 2026-08-19)

- `verification.py`:
  - `BuildStateTracker` (session-scoped; `VerificationEngine.build_state`,
    re-attached by the orchestrator with a journaling event sink):
    FULL_BUILD_AVAILABLE → SYNTAX_ONLY on generic full-build timeout (or
    any kind once the recoverable retry was spent). All probes/transitions
    journaled (`build_probe` / `build_state` / `build_retry`) — the
    sprint-18 300s gaps were silent builds.
  - `_classify_build_failure_kind(output)`: lock_contention /
    compiler_crash / network_transient / generic from the build output.
  - `verify_file` C build branch: degraded session → skip straight to the
    shared `_syntax_only_fallback` (journaled "skipped" probe); timeout →
    recoverable kinds retry ONCE at 2× cap, generic degrades immediately;
    targeted (`make {stem}.o`) timeouts do NOT degrade the session;
    pass/fail recorded as probes. detail.source=whole_file_build kept.
  - The old inline timeout-fallback body was extracted into
    `_syntax_only_fallback` (shared by timeout + degraded paths).
- `orchestrator.py`:
  - tracker wired to the journal at construction.
  - Phase-2 full-build fallback skipped (journaled
    `phase2_build_fallback_skipped`) when the session is degraded.
  - Phase-2 build test (`_run_raw_test` call) now timed + journaled as a
    probe; a detected timeout ("timed out after") degrades the tracker.
- `scripts/live_eval_realworld.py`: all `make -j4` → `make -j$(nproc)`
  (5 sites). ccache was wired but CORRECTION (f836488 archaeology): it
  was 100% inert — CC/CXX=ccache + PATH shim made ccache resolve its own
  shim and re-enter itself (995/995 uncacheable). Fixed 2026-08-19
  evening; the D7-leg builds below ran uncached (the -jN effect stood
  alone).
- Deferred: per-case prewarming build at preflight (D2 optional; ccache +
  nproc + state machine cover the economics; prewarm runs in the eval
  process, outside the tracker).
- Tests: `tests/test_build_state_machine.py` (13) — classifier, tracker
  transitions, sink robustness, verify_file degrade+skip, 2×-cap retry,
  second-timeout degrade, targeted exemption.
- Regression: 258+160 tests green.
- Expected effect on protobuf-0067: verify_file#2/#3 (~600s) + Phase-2
  fallback (120s) skipped post-degradation; case completes under budget
  with identical content decisions (first build + pre_continue still run).

## P2 design (as implemented, 2026-08-19)

- `config.py`: `validation.preservation_deletion_carveout = True`;
  `future.enable_preservation_bestof_n = True`.
- `conflict_model.py`: `CandidateResolution.flagged_by_preservation_heuristic`
  (default False) — R1's metadata tag.
- `verification.py` (PreservationHeuristicValidator): when the loser side's
  ONLY unaccounted obligations are DROPPED_DELETIONs (no additions, no
  exclusive choices — conflict_type == "deletion"), the verbatim copy
  PASSES with features preservation_result="deletion_superseded" (auditable
  detail: deletion_lines). Additions/mixed/exclusive shapes fire as before
  (sea-orm-0027 defense intact).
- `orchestrator.py`:
  - `_resolve_unit` is now a thin wrapper around `_resolve_unit_core`
    applying the Best-of-N rescue: unit unaccepted + stashed
    preservation-rejected candidate + ≥1 forced retry + ALL retries
    failed validation → restore stashed candidate (flagged), journal
    `candidate_accepted` via="preservation_bestof_n_recovery". A retry
    that PASSED blocks the rescue (equal-or-better); no retry → no
    rescue.
  - Stash `self._step_preservation_stash[unit_id]` populated at the risk
    decision when retry is preservation-driven and candidate
    validation-passing (tag set at rejection time; journal
    `preservation_flagged`); quality tracking after each attempt's
    validation; popped on acceptance/rescue.
  - `_check_side_collapse(..., accepted=...)`: journal gains
    flagged_preservation_units (context only, no semantic change).
- Tests: `tests/test_preservation_bestof_n.py` (10 tests) — carve-out
  pass/mirror/additions-still-fire/disabled, rescue restore/block/skip
  paths, flag default.
- Fixture note: same-shape added lines (`fn x() {}` vs `fn y() {}`) are
  classified rename-EXCLUSIVE by change_accounting (structural-suffix
  rule) — additions fixtures need distinct shapes (e.g. `struct Widget;`).
- P6 TODO: corpus-measure the deletion-carveout band (units where loser
  churn is pure deletion: how often oracle == other side verbatim?).
- Regression: 296+88+10 tests green.

## P1 design (as implemented, 2026-08-19)

- `config.py`: `future.enable_whole_side_repair_rung = True`.
- `verification.py`: build-branch failure + cargo-check failure now tagged
  `detail.source="whole_file_build"` at emission (D5 rule — no message
  string-matching).
- `orchestrator.py`:
  - `_is_compile_flavored_failure()` module helper: build_test | tagged
    whole_file_build | "cargo check" prefix. Splice-coherence/standalone
    parse failures EXCLUDED (deterministic/CEGIS territory).
  - `_whole_side_repair_prompt_single()` (one side compiles; subsumption
    verdict vocabulary) + `_whole_side_repair_prompt_both()` (both compile;
    explicit "neither" escape for the woven class) + `_clip_side_diff()`.
  - `_try_whole_side_repair_rung()`: probes both pristine stage sides via
    verify_file (journaled `whole_side_probe` w/ duration), decision matrix:
    0 verify→decline+restore; 1 verifies→subsumption adjudication
    (superseded @ conf≥0.70 required); 2 verify→repair adjudication
    (current/replayed @ conf≥0.70; neither/unparseable declines).
    Swap journals `whole_side_repair`; declines restore the spliced buffer
    to the worktree.
  - Phase-2 loop wiring: fires once per file (`_wsr_attempted`) when the
    buffer fails w/ compile-flavored failure, BEFORE cross-unit portfolio
    and repair; on swap resets `_p2_build_checked` (new buffer deserves
    its build test) and `continue`s.
  - D5: `_whole_file_repair`'s `_is_build_test` carve-out now also matches
    tagged whole_file_build failures (tokio-0109's skip class).
- Tests: `tests/test_whole_side_repair.py` (19 tests) — classifier,
  prompts, full decision matrix incl. declines restore worktree.
- Deferrals (deliberate): parallel side compiles (worktree races; P3's
  build-state machine addresses probe cost); cross-majority-run probe
  caching (ccache/P3 covers); rung NOT triggered on standalone-parse
  failures (only true compile gates).
- Regression: 314+78+65 tests green across orchestrator/verification/
  repair files.

## Code seam map (from diagnosis doc; re-verify lines before editing)

- `verification.py:4481` — `_build_timeout = 30 if target_tmpl else 300`
  inside `verify_file` (the silent 300s gaps).
- `verification.py:4648-4668` — the `g++ -fsyntax-only` timeout fallback.
- `verification.py:4943` — whole-file cargo check stamps
  `validator="syntax"` w/ cargo message prefix.
- `verification.py:4471` — `_ccache_env` exists.
- `orchestrator.py:10243-10267` — tiered fault attribution + `_is_build_test`
  carve-out (only matches `validator == "build_test"`).
- `orchestrator.py:11257-11275` — essential-tokens oversized guard
  (`llm_skipped_oversized`).
- `orchestrator.py:11415-11431` — post-construction oversized guard
  (`llm_skipped_oversized_prompt`; fired on 0055).
- `orchestrator.py:13329-13331` — collapse-guard accept-on-null.
- `orchestrator.py:13705-13757` — `_adjudicate_subsumption` (single call,
  no retry — D6/P3-adjacent).
- `orchestrator.py:13774`, `:13792` — `_run_raw_test` Phase-2 build, 120s cap.
- `orchestrator.py:13862-13876` — tests_finished journaling (diagnostics
  parser missed make compile errors).
- `orchestrator.py:13901-13919` — advisory gate returns `run.passed` under
  `tests.required=False`.
- `risk.py:268` — `preservation_heuristic` RETRY trigger (the 0037 seam).

## Validation matrix (per plan)

- P1: majority-of-3 tokio-0109 + the five one-side-oracle failures;
  must-hold all currently-passing cases.
- P2: tokio-0037; must-hold sea-orm-0027 defense.
- P3: protobuf-0067/0071 under budget.
- P4: protobuf-0065 escalates on attributed defect; advisory cases still
  respect config.
- P5: 0055 prompt under 8K; must-hold sqlite entity-splitting cases.
- P6: corpus measurement only.
- Suite: full pytest run must stay green (0 failures).

## Work log (append entries, newest last)

- 2026-08-19 session start: ledger created; full suite at ~99% zero
  failures (still running, pid was `.venv/bin/python -m pytest tests/ -q`).
- 2026-08-19: **sprint-18 full suite FINISHED** — `1 failed, 6092 passed,
  2115 skipped, 1 xfailed` in 5h41m. The 1 failure is
  `tests/test_diff.py::test_char_ratio_performance` (elapsed<5s wall-clock
  bound) — load flake from running beside live validation; passes in 4.27s
  isolated. Functionally: all green.
- 2026-08-19: **P1 implemented** (whole-side repair rung + D5 carve-out;
  see P1 section). tests/test_whole_side_repair.py 19 tests +
  457 regression tests green. COMMITTED as 4dcdd3f.
- 2026-08-19: P2 started — studying risk.py preservation_heuristic seam.
- 2026-08-19: **P2 implemented** (churn-aware carve-out + Best-of-N
  wrapper; see P2 section). 296+88+10 tests green. COMMITTED as 7dff022.
- 2026-08-19: **P3 implemented** (BuildStateTracker + conditional retry +
  probe journaling + Phase-2 fallback skip + -j$(nproc); see P3 section).
  258+160 tests green. COMMITTED as 0e83374.
- 2026-08-19: **P4 implemented** (compiler-authority override at the
  pre_continue gate + make-output diagnostics; see P4 section). 100
  tests green. COMMITTED as 4680852.
- 2026-08-19: **P5 implemented** (journal-only class-member split
  measurement; see P5 section). 11 + 144 tests green. COMMITTED as
  207e4a5.
- 2026-08-19: **P6 DONE** (scripts/calibrate_near_verbatim.py; results in
  P6 section — jaccard band NOT clean; deletion-carveout premise 56% at
  file level, unit-level census needed from live runs). COMMITTED as
  125323e.
- NEXT: D7 rerun matrix (sea-orm-0027 + tokio-0109 live under provider;
  then 0067/0071/0065/0055 after P1-P4), full suite re-run, docs.
- 2026-08-19 16:05: **D7 batch-1 DONE** (majority-of-3, nova-gemma4,
  /tmp/capybase-live/s19/val/d7-b1.json):
  - sea-orm-0027 → **ESCALATE (unanimous)**, reason: "side collapse ...
    adjudication: keep". The collapse detector finally PAIRED with a live
    adjudication (sprint-18: the call always died on transport). All 3
    runs: buffer collapsed to current, adjudication says replayed's
    TryGetError rewrite is essential (conf 0.95) → honest escalate. The
    silent one-side ORACLE_DIVERGENT is gone — converted to the
    always-acceptable ESCALATE.
  - tokio-0109 → **ESCALATE (unanimous)**, and the **unexplained-error
    mystery is SOLVED by the P1 probes**: whole_side_probe ran in every
    run (the rung fired on the compile failure, as designed); BOTH
    pristine stage sides fail cargo check with the SAME two errors
    (`#[deprecated]` on trait impl blocks; ManuallyDrop misuse) at ~3s
    per probe → the rung correctly declined (no_side_verifies) and the
    case escalated. The errors are TOOLCHAIN-ERA artifacts: the
    historical tokio code (all sides incl. the oracle == current) does
    not compile under the eval's newer rustc. Not a splice artifact, not
    a model defect, not a mechanism gap — the case is un-passable under
    this toolchain. Census correction #3 for 0109.
  - Batch-2 (0067/0071/0065/0037/0055) launched 16:0x, logs at
    /tmp/capybase-live/s19/val/d7-b2-*.log.
- 2026-08-19 16:33: **D7 batch-2 cpp leg DONE**:
  - protobuf-0067 → **PASS** (was budget-blowout ESCALATE). Journal:
    build_probe timeout 300.1s → build_state SYNTAX_ONLY → later probes
    "skipped" → phase2_build_fallback_skipped — the P3 machine worked;
    the one retry that ran passed at ~300s (warm tree + -jN; CORRECTION:
    ccache was inert that day — see f836488 in the work log).
  - protobuf-0071 → **PASS** (same restoration).
  - protobuf-0065 → ORACLE_DIVERGENT — BUT this leg ran with the broken
    `-j$(nproc)` gate command (TestRunner has no shell → invalid make
    option → usage text rc=2, NOTHING attributable → P4 couldn't fire;
    the "failure" wasn't even a compile). Fixed in cf50f4b (job count
    resolved in Python); majority-of-3 rerun queued
    (/tmp/capybase-live/s19/val/d7-0065-retry.*).
- 2026-08-19 16:48: **D7 batch-2 mech leg DONE** (majority-of-3,
  /tmp/capybase-live/s19/val/d7-b2-mech.json):
  - tokio-0037 → **ESCALATE (unanimous 3/3, sim 1.0)** — via the
    resurrection backstop, NOT P2's paths: this sampling candidates
    were accepted plain-LLM with zero preservation forcing (no
    preservation events in any journal); end-of-rebase scan found 12
    resurrected lines in tokio/src/runtime/tests/queue.rs → policy
    stop. Sprint-18's sampling (oracle-correct first candidate
    force-retried) did not recur; P2 stays unit-test-validated.
  - protobuf-0055 → **ESCALATE (unanimous 3/3)** — 2/3 stopped at
    oversized-prompt (16286t/15508t > 8192t); 1/3 wrote the file and
    P4 fired on an attributed error (cpp_helpers.cc:1470
    'HasInternalAccessors'). P5 journaling LIVE at every skip site:
    region 520 lines, member points 3 vs 0, declined
    fragments_below_min_sub_lines — matches the offline probe; first
    live distribution data recorded in sprint19-results.md.
- 2026-08-19 17:18: **D7 0065 fixed-gate rerun DONE — P4 acceptance
  CLEAN** (majority-of-3, /tmp/capybase-live/s19/val/d7-0065-retry.json):
  **ESCALATE unanimous 3/3, sim 0.996.** Pre_continue `make -j12`
  completed rc=2 with 5 error lines all in merged text_format.cc
  ('tokenizer_' ×3 + 2 follow-ons), all positively attributed;
  tests_required=false and compiler_authority_override fired anyway.
  The buffer is 0.996 to oracle — the defect lives in the 0.4% delta
  no sim gate can catch. Sprint-18's shipped build-broken merge is now
  the honest escalate. P3 composed around it (full-build timeout →
  SYNTAX_ONLY → phase2 fallback skipped; warm-tree gate build surfaced
  the errors). **D7 complete** — all batches + rerun landed; results
  in docs/sprint19-results.md.
- 2026-08-19 23:0x: fresh full suite over sprint-19 changes LAUNCHED
  (`.venv/bin/python -m pytest tests/ -q`, log
  /tmp/capybase-live/s19/val/suite-s19.log; baseline 5h41m).
- 2026-08-19 ~22:4x: **r1 suite KILLED at 66% by a zcode restart**
  (0 failures to that point — the restart killed the background task).
  The post-mortem found the real story: **load-92 incident** — every
  timed-out full build since 02:38 had leaked its make/libtool/ccache
  tree (~274 processes alive at 22:45, spinning for hours inside deleted
  /var/tmp/capy-rw-* worktrees), because `subprocess.run(shell=True,
  timeout=...)` kills only the direct child. Compounding it, ccache was
  fully broken (shim self-recursion; 0/995 cacheable; a 1-line TU
  livelocked >90s in repro) — so every build ran cold while leaked trees
  burned the CPUs. The "slow 65% region" and the wedged session were
  both this starvation. Both bugs FIXED in f836488 (+5 tests).
- 2026-08-19 23:04: **suite r2 RELAUNCHED post-fix, detached**
  (setsid — survives zcode restarts; pid 1551158; log
  /tmp/capybase-live/s19/val/suite-s19-r2.log). At 66% within ~10 min on
  the idle box (r1 took ~5h to the same point). D8 census/dist batches
  chained behind it via run-d8-after-suite.sh (also detached).
- 2026-08-19 23:5x: **ccache setup COMPLETED for real workloads**: the
  recursion fix alone wasn't sufficient — ccache's default hash includes
  the compilation directory, and the eval re-materializes a fresh
  /var/tmp/capy-rw-* worktree per case AND per majority repeat, so
  cross-run hits were structurally zero (demonstrated: same content,
  two worktrees, default env → MISS). Added CCACHE_NOHASHDIR=1 +
  CCACHE_BASEDIR=/var/tmp (verified cross-worktree HIT), temp on disk
  (/var/tmp/capybase-ccache-tmp — the 6G /run tmpfs was 77% full of
  orphan cpp_stdout files during the incident), MAXSIZE 20G, and
  os.environ.update(_ccache_env()) in the eval main so the orchestrator
  gate + TestRunner pre_continue builds inherit the wiring (suite build
  tests showed frozen counters — those paths pass no env). New tests:
  cross-worktree hit + env keys (7 total in the hygiene file).
  COMMITTED as aa10db0.
- 2026-08-19 23:55: **D8.1 suite r2 DONE — GREEN**: `6159 passed,
  2115 skipped, 1 xfailed, 0 failed` in **54m52s** (s18 baseline
  5h41m; the char_ratio load-flake did not recur on the idle, leak-free
  box; +67 passed vs s18's 6092 = sprint-19's new tests). The "Suite:
  full pytest run must stay green" must-hold is MET.
- 2026-08-20 00:03: **D8.2 P6 census DONE** (majority-of-3,
  d8-p6-census.json): axum-history-0006 **PASS** sim1.00 15s and
  jsonc-history-0002 **PASS** sim1.00 20s — both resolved UPSTREAM of
  the preservation path (portfolio/structural; zero preservation events
  in journals). flask-history-0006 **ESCALATE 3/3** (sim 0.54; model
  produced empty resolutions every run — honest). tokio-history-0046
  **ESCALATE 3/3** (sim 0.88; end-of-rebase scan caught 12 resurrected
  lines in queue.rs → policy stop — the deletion-direction safety net,
  live-validated 3/3). **Census result: 0 `deletion_superseded` events
  across all 8 journals — the carveout path was never entered.** The
  unit shape it guards (verbatim candidate + loser-side pure-deletion
  obligations reaching validation) did not occur: easy pure-deletion
  cases are consumed by structural resolution before an LLM candidate
  exists. Honest zero-event reading (explicitly allowed by D8
  acceptance); the carveout stays ON — it is zero-risk (it only rescues
  candidates the heuristic would wrongly reject) — and remains
  unit-test-validated (tests/test_preservation_bestof_n.py).
- 2026-08-20 00:06: **D8.3 P5 distribution DONE** (majority-of-3,
  d8-p5-dist.json): **2/2 PASS** — protobuf-history-0053 sim1.00 in 96s
  via `structurally_resolved` and 0073 sim1.00 in 70s via
  `true_side_portfolio`, with **zero oversized-skip events and zero
  class_member_split_candidate stamps**. Finding: the corpus's densest
  conflict hunks are one-side-dominated and resolve mechanically — they
  never reach the LLM, so they can never hit the oversized site.
  **dense-hunk ≠ oversized-prone** (offline churn/density proxies —
  including the one that selected this batch — over-select). The only
  live oversized distribution across ALL sprint-19 runs remains 0055's
  (3 repeats, 520-line region, member points 3-vs-0, declined
  fragments_below_min_sub_lines). **P5 enabling call: stays OFF.**
  Sprint-20 criteria sharpened: (1) select candidate cases from live
  `llm_skipped_oversized` firings in full-corpus runs, not offline
  proxies; (2) enabling requires the statement-level splitter beneath
  the member split (0055's member fragments alone are 150-250 lines,
  still over an 8K window).
- 2026-08-20 00:1x: **D8.5 — SPRINT-19 COMPLETE.** All D8 items closed;
  every acceptance line of the sprint met or honestly dispositioned.
  ccache production evidence from the D8 legs: 776 cacheable calls /
  44 hits / 0.2 GiB cached (vs 995/995 uncacheable pre-fix), including
  cross-worktree hits that were structurally impossible before. `dev`
  holds the sprint's commits locally — push is the user's job.

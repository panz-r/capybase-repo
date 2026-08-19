# PLAN-LEDGER — Sprint 19 (living document, update as work proceeds)

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
| D7 | Post-fix live rerun matrix (sea-orm-0027, tokio-0109 first) | TODO | needs no new mechanisms for 0027/0109 |

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
  (5 sites). ccache was already wired (persistent CCACHE_DIR).
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
  457 regression tests green. COMMITTED as <P1-sha>.
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
  <P6-sha>.
- NEXT: D7 rerun matrix (sea-orm-0027 + tokio-0109 live under provider;
  then 0067/0071/0065/0055 after P1-P4), full suite re-run, docs.

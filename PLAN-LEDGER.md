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
| P3 | Build state machine + conditional retry (+ccache/-j$(nproc)) | TODO | fixes protobuf-0067/0071 budget blowout |
| P4 | Compiler-authority override at final gate | TODO | fixes protobuf-0065 ship-broken |
| P5 | Class-with-methods entity splitting (journal-only first) | TODO | protobuf-0055 |
| P6 | Near-verbatim band calibration (measure-only) | TODO | informs future fast-path |
| D7 | Post-fix live rerun matrix (sea-orm-0027, tokio-0109 first) | TODO | needs no new mechanisms for 0027/0109 |

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
  wrapper; see P2 section). 296+88+10 tests green. COMMITTED as <P2-sha>.

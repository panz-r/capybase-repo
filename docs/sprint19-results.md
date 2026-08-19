# Sprint-19 results — mechanisms for the one-side-oracle class

Status: implementation complete (P1-P6); live validation (D7) in flight.
This doc records what was built, the calibration findings, and the live
results as they land. Companion: `PLAN-LEDGER.md` (working log),
`docs/sprint19-failing-cases-diagnosis.md` (D1-D7 designs),
`docs/sprint19-open-questions-for-review.md` (Q1-Q6).

Input: two external reviews of the sprint-18 failures, synthesized into
the sprint-19 plan (both reviewers' consensus: the one-side oracle class
— 31.5% of the corpus — is systematically underserved by splice-based
reconstruction; the fix is a compiler-gated whole-side repair mechanism;
Q1 resolved to Best-of-N + churn-aware heuristic; Q2/Q3 to a repair rung
after compile failure, never pre-emptive; Q6 to conditional retry within
a build state machine).

## What was implemented

| # | Mechanism | Commit | Tests |
|---|-----------|--------|-------|
| P1 | Whole-side repair rung on compile failure (+D5 carve-out) | 4dcdd3f | 19 |
| P2 | Churn-aware preservation heuristic + Best-of-N recovery | 7dff022 | 10 |
| P3 | Session build state machine + conditional retry + journaled builds | 0e83374 | 13 |
| P4 | Compiler-authority override at the final gate | 4680852 | 8 |
| P5 | Class-with-methods split measurement (journal-only) | 207e4a5 | 11 |
| P6 | Near-verbatim band calibration (measure-only) | 125323e | — |

All six landed with their plan-mandated safety posture: every new
mechanism is behind a `future.`/`validation.` flag, every decline path
restores prior behavior exactly, and nothing pre-empts on churn numbers
alone (the sprint-18 WS4 calibration's lesson stands).

### P1 — whole-side repair rung (`enable_whole_side_repair_rung`, ON)

When the spliced buffer fails a whole-file COMPILE gate (cargo check,
the Phase-2 build test, an attributed build failure — all tagged
`detail.source="whole_file_build"` at emission, D5's no-string-matching
rule), both pristine merge-index stage sides are probed as whole-file
candidates. Decision matrix: neither verifies → decline (repair proceeds
as before); exactly one verifies → subsumption adjudication must confirm
superseded at ≥0.70; both verify → the repair adjudication must pick a
side at ≥0.70 with an explicit "neither" escape (the woven class keeps
its CEGIS repair). Declines restore the spliced buffer to the worktree;
swaps journal `whole_side_repair` and re-validate through the standard
Phase-2 gates (the build test re-runs on the new buffer).

D5 folded in: `_whole_file_repair`'s `_is_build_test` carve-out now
recognizes tagged whole-file compile checks, so an in-file cargo error
whose line attribution falls outside marker spans gets the one bounded
model repair instead of skip→escalate (tokio-0109's skip class).

### P2 — churn-aware heuristic + Best-of-N

(`validation.preservation_deletion_carveout`, ON;
`future.enable_preservation_bestof_n`, ON)

tokio-0037: the model's first candidate was oracle-correct and
validation-passing; the preservation heuristic forced retries that
degraded into syntax errors. Two fixes: (a) a loser churn that is a pure
deletion of base content (no additions, no exclusive choices) passes
with `preservation_result="deletion_superseded"`; (b) a
validation-passing candidate force-retried by the heuristic is stashed
and tagged; if the unit escalates and EVERY forced retry validated
strictly worse, the stash is restored (`via=
preservation_bestof_n_recovery`). A retry that passed blocks the rescue.
The sea-orm-0027 defense (additions present → heuristic fires) is
untouched, and the side-collapse guard's journal now carries
`flagged_preservation_units`.

### P3 — build state machine (`BuildStateTracker`, ON by construction)

One doomed full build per session: after a generic full-build timeout,
subsequent full builds skip straight to the syntax-only fallback and
Phase-2's full-build fallback is skipped (both journaled). Recoverable
failures (lock contention / compiler crash / network) get ONE retry at
2× cap. Every build probes and transitions journal (`build_probe` /
`build_state` / `build_retry`) — the sprint-18 300s silent gaps are
gone. Targeted (`make {stem}.o`) timeouts never degrade the session.
The eval harness now uses `make -j$(nproc)`; ccache was already wired.

Expected on protobuf-0067: ~720s of the ~1020s build blowout skipped,
content decisions unchanged.

### P4 — compiler authority at the final gate

protobuf-0065 shipped a build-broken merge at sim 0.997 (rc=2 parsed as
unknown, empty diagnostics, advisory gate). Now: error lines from the
gate output surface in `tests_finished`; when the gate command IS a
build and error lines POSITIVELY attribute to a merged file's stem, the
gate escalates regardless of `tests.required`
(`compiler_authority_override`). Sibling errors, driver summaries,
unparseable lines and timeouts keep advisory behavior — strict positive
attribution only.

### P5 — class-member split measurement (`enable_class_member_splitting`, OFF)

Journal-only: on the entity splitter's decline paths, a depth-2
boundary detector (opener-stack, namespace-safe; member-function starts
+ access specifiers; declarations/data-members/initializer-continuations
excluded) stamps `class_member_split_candidate`, journaled at both
oversized-skip sites. Offline probe on protobuf-0055: the region is
one-sided (cur 516 / rep 0) with 3 member points — member splitting
alone leaves 150-250-line fragments, so the enabling stage needs the
existing statement-level splitter beneath it. Enabling awaits the live
journal distribution.

### P6 — calibration findings (measure-only)

`scripts/calibrate_near_verbatim.py`, 674 cases, the eval's own token
jaccard:

- 81.6% of oracles are ≥0.99 to one side, but "woven" (both <0.95) is
  only 4.2% — jaccard saturates on large files; the near-verbatim band
  does NOT separate one-side-plus-a-thread from dominated woven merges.
- Churn doesn't separate the band either (201/176/173 across regimes).
- Only 179/674 are concentrated near-verbatim (≤2 hunks, ≤20 lines).
  Verdict (Q4): no special fast-path on jaccard; any future path needs
  the concentration filter + compiler gate + adjudication.
- Deletion-carveout premise: 25 pure-deletion-side cases; oracle ≈ other
  side in 56% at FILE level — the unit-level carveout is far narrower
  (all-missing-obligations-are-non-exclusive-dropped-deletions), stays
  ON, and live runs must census `deletion_superseded` events to measure
  the unit-level truth (file-level counterexamples exist: flask-0006/7,
  sqlite-0012).

## Live validation (D7)

### Batch 1 — the no-new-mechanisms reruns (DONE, unanimous 3/3 each)

- **sea-orm-0027 → ESCALATE** ("side collapse in src/executor/query.rs;
  the replayed side's rewrite (279 changed lines) was dropped
  (adjudication: keep)"). The WS4 collapse detector finally PAIRED with
  a live subsumption adjudication — in sprint-18 the adjudication call
  died on transport in all six runs. Every run: the model produced a
  current-verbatim buffer, the detector fired, the live adjudication
  ruled replayed's TryGetError work essential at 0.95 → escalate. The
  silent one-side ORACLE_DIVERGENT is gone, converted into the
  always-acceptable honest ESCALATE.
- **tokio-0109 → ESCALATE**, and the previously-unexplained error
  provenance is SOLVED by the P1 rung's probes: `whole_side_probe` fired
  in every run and BOTH pristine merge-index sides failed cargo check
  with the SAME two errors (`#[deprecated]` on trait impl blocks;
  `std::mem::drop` on a `ManuallyDrop`) at ~3s per probe. The errors are
  toolchain-era artifacts — the historical tokio code, in every side
  including the oracle (== current verbatim), does not compile under the
  eval's newer rustc. Not a splice artifact, not a model defect, not a
  mechanism gap: the case is un-passable under this toolchain, and the
  honest disposition is exactly what the pipeline produced (the rung
  correctly declined — no_side_verifies — rather than substituting a
  side that also wouldn't compile).

### Batch 2 — the mechanism targets (protobuf-0067/0071/0065,
tokio-0037, protobuf-0055)

In flight; results appended on completion.

## Hermetic suite

The sprint-18 full suite finished green (6092 passed / 2115 skipped /
1 xfailed; the single failure was `test_char_ratio_performance` — a
wall-clock flake under load, passes in 4.3s isolated). A fresh full
suite run over the sprint-19 changes is queued after the live batches.

## Must-holds (validated by the batch-2 matrix + suite)

- All currently-passing cases stay passing (no guard relaxation).
- The sea-orm-0027 defense (additions → heuristic fires) intact.
- sqlite entity-splitting cases unaffected by the P5 measurement.
- Oracle-divergent merges: zero (the rung's adjudication gates + the
  collapse guard + P4's attribution are the three independent nets).

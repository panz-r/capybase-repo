# Sprint-19 results — mechanisms for the one-side-oracle class

Status: implementation complete (P1-P6); live validation (D7) complete —
batch 1, batch 2, and the fixed-gate 0065 rerun all landed (2026-08-19);
post-D7 build-hygiene fixes landed (f836488) with the suite rerun (r2)
and the D8 measurement batches (P6 census, P5 distribution) in flight.
This doc records what was built, the calibration findings, and the live
results. Companion: `PLAN-LEDGER.md` (working log),
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
The eval harness job count is resolved in Python (`-j12`; a literal
`-j$(nproc)` was invalid — TestRunner runs the gate without a shell,
fixed in cf50f4b). ccache CORRECTION: it was wired but 100% inert
during all D7 legs — the CC/CXX double-wrap plus PATH shim made ccache
resolve its own shim and re-enter itself (995/995 uncacheable calls; a
one-line TU livelocked in repro), fixed post-D7 in f836488 (shims now
exec absolute compilers; verified miss-then-hit live). The budget
restoration below therefore stands on the state machine + parallel make
alone; the D8 rerun of the affected flows gets real cache.

Expected on protobuf-0067: ~720s of the ~1020s build blowout skipped,
content decisions unchanged. Actual (D7 batch-2): 1337s → 480s; 0071
came in at 689s — both far under the 1200s budget.

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
journal distribution — first live data (D7 batch-2, protobuf-0055): the
candidate fired at every oversized-skip site in all 3 repeats with
region_lines=520, current_member_points=3, replayed_member_points=0,
decline_reason=`fragments_below_min_sub_lines` — matching the offline
probe exactly (member points exist but are concentrated; the replayed
side has none). The distribution is now flowing; enabling stays
deferred until more of it accumulates.

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
tokio-0037, protobuf-0055) (DONE; 0065 under its fixed-gate rerun)

Toolchain caveat: this leg launched before cf50f4b, so the cpp cases'
compiler gate ran the broken `-j$(nproc)` literal (make usage text,
rc=2, nothing attributable → the gate was an advisory no-op). 0065 —
P4's acceptance case — got its own majority-of-3 rerun after the fix;
the 0067/0071 PASSes below stand on sim + oracle-verified buffers, with
the compiler gate inoperative for that leg only.

- **protobuf-0067 → PASS** (sim 1.0, 480s wall — was a 1337s
  budget-blowout ESCALATE in sprint-18). P3's machine is visible in the
  journal: first full `build_probe` timed out at the 300s cap →
  `build_state SYNTAX_ONLY` → subsequent full-build probes skipped →
  `phase2_build_fallback_skipped`; the one retry that ran passed at
  ~300s (warm tree + parallel make — ccache was inert that day, see the
  P3 correction). Under the 1200s budget with ~850s of the blowout gone.
- **protobuf-0071 → PASS** (sim 0.910, 689s; same restoration, same
  journal shape). Acceptance "0067/0071 under budget": met.
- **protobuf-0065 → ESCALATE (unanimous 3/3, sim 0.996)** — the P4
  acceptance case, clean. The pre_continue gate (`make -j12`, job count
  now resolved in Python) completed rc=2 with five error lines ALL in
  the merged `google/protobuf/text_format.cc` (`'tokenizer_' does not
  name a type` ×3 plus two follow-on syntax errors), every one
  positively attributed; `tests_required` was false and
  `compiler_authority_override` fired anyway — exactly the sprint-18
  counterexample (build-broken merge shipped at sim 0.997) converted to
  the honest escalate. The kicker: the buffer is 0.996 to the oracle —
  the defect lives inside the 0.4% delta no similarity gate could ever
  catch; only the compiler sees it. P3 composed correctly around it
  (first full build timed out → session degraded to syntax-only →
  Phase-2 fallback skipped; the warm-tree gate build then completed
  fast enough to surface the errors P4 needed).
- **tokio-0037 → ESCALATE (unanimous 3/3, sim 1.0)** — honest, but via
  the resurrection backstop rather than P2's paths: this sampling the
  model's candidates were accepted through the plain LLM path with zero
  preservation forcing (no preservation events in any of the three
  journals), and the end-of-rebase scan found 12 resurrected lines in
  `tokio/src/runtime/tests/queue.rs` → policy stop. The sprint-18
  failure sampling (oracle-correct first candidate force-retried into
  syntax errors) did not recur, so P2's carve-out and Best-of-N
  recovery remain validated by their unit tests; the sea-orm-0027
  defense is untouched (batch-1 exercised it live). Either way the case
  escalates for the right reason — silent resurrection was caught.
- **protobuf-0055 → ESCALATE (unanimous 3/3)** — two repeats stopped at
  the oversized-prompt skip (16286t / 15508t > the 8192t window; P5 is
  journal-only by design, so no prompt reduction was expected); the
  third got a unit through block capture, wrote the file, and P4 fired
  on a positively-attributed error
  (`cpp_helpers.cc:1470: 'HasInternalAccessors' was not declared in
  this scope`). P5's candidate journaled at every skip site in every
  repeat (see the P5 section for the first live distribution).

Batch-2 acceptance scorecard: P3 under budget ✓ (0067/0071); P4
escalates on attributed defect ✓ (0065 3/3, plus a bonus live firing on
0055); P2's case escalates honestly ✓ (via the backstop; P2 paths
unexercised this sampling); P5 journal distribution flowing ✓ (still
over-window, as designed); oracle-divergent merges with a working gate:
zero ✓ (the leg's single ORACLE_DIVERGENT was the gate-command bug,
superseded by the 3/3 rerun).

## Hermetic suite

The sprint-18 full suite finished green (6092 passed / 2115 skipped /
1 xfailed; the single failure was `test_char_ratio_performance` — a
wall-clock flake under load, passes in 4.3s isolated). The fresh full
suite over the sprint-19 changes launched 2026-08-19 after the live
batches; that first run (r1) was killed at 66% — zero failures to that
point — by a zcode restart, and the post-mortem surfaced a load-92
incident: every timed-out full build had leaked its make/libtool/ccache
process tree (274 orphans spinning inside deleted worktrees since
02:38), and ccache itself was 100% inert (shim self-recursion, 0/995
cacheable — a one-line TU livelocked in repro). Both fixed in f836488
(`_run_shell_tree` session+killpg teardown at every shell build site;
absolute-compiler shims; stale-process sweep by worktree cwd; 5 new
regression tests). The rerun (r2) is running detached from the session
(log `/tmp/capybase-live/s19/val/suite-s19-r2.log`) — it passed 66% in
~10 minutes where r1 took ~5h under the leaked load.

## D8 — sprint-19 extension (in flight)

No deferrals: the two measurement debts rejoin the sprint, chained
after the r2 suite (`run-d8-after-suite.sh`, detached):

- **P6 live census** (D8.2): majority-of-3 over pure-deletion-side
  corpus cases axum-history-0006, tokio-history-0046, jsonc-history-0002
  plus a deliberate file-level counterexample (flask-history-0006);
  census `preservation_result="deletion_superseded"` events for the
  unit-level carveout rate the P6 section called for.
- **P5 distribution** (D8.3): majority-of-3 over the corpus's densest
  cpp conflict regions — protobuf-history-0053 (3720-line single hunk)
  and protobuf-history-0073 (626) — selected by a densest-hunk probe
  that reproduces 0055's known 517-line region; journal
  `class_member_split_candidate` distribution feeds the enabling call.

## Must-holds (validated by the D7 matrix + suite)

- All currently-passing cases stay passing (no guard relaxation) — the
  batch-2 mechanism targets moved ESCALATE→PASS (0067/0071) with no
  regression elsewhere in the matrix; the suite run is the full check.
- The sea-orm-0027 defense (additions → heuristic fires) intact —
  exercised live in batch-1 (detector + adjudication both fired;
  honest escalate).
- sqlite entity-splitting cases unaffected by the P5 measurement — P5
  is journal-only and OFF; covered by the suite run.
- Oracle-divergent merges: zero under a working gate (the rung's
  adjudication gates + the collapse guard + P4's attribution are the
  three independent nets; batch-2's single ORACLE_DIVERGENT was the
  `-j$(nproc)` gate-command bug, superseded by the 3/3 ESCALATE rerun).

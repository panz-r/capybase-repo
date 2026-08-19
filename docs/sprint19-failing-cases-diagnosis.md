# Sprint-18 failing cases — journal-grounded diagnosis and sprint-19 fix designs

Method: full-event archaeology over the preserved flight journals
(`/tmp/capybase-live/s18/val/flights*/`, both batches — 25 runs across the
seven failing cases), cross-referenced against the code seams that emitted
each event. Every claim below cites a journal signature or a `file:line`.

Two diagnoses recorded in the sprint-18 census are **corrected** by this
analysis: protobuf-0067/0071 are not model-convergence failures (the model
was never called on 0067), and tokio-0109's escalation is
contamination-shaped, not capability-shaped.

## Per-case diagnosis

### protobuf-0067 / 0071 — build economics, not the model (census corrected)

Signature (0067, all 6 runs within seconds of each other):

```
+2s    phase1_fast_path fires (churn 0.993, winner=current, 5 units)
+303s  true_side_portfolio        <<< 301s silent gap
+605s  file_validated passed      <<< 302s silent gap
+606s  phase2_build_fallback_full make -j4
+726s  phase2_build_inconclusive  (120s cap)
+727s  deletion_respect_swap_probe
+1029s deletion_respect_swap      <<< 302s silent gap
+1330s tests_started pre_continue make -j4
+1333s tests_finished timed_out   (300s cap) → step_continued anyway
+1337s session_completed — case budget (1200s) blown → ESCALATE
```

Zero LLM resolution calls on 0067 (fast-path took `current`, the swap fixed
35 resurrected lines — the mechanisms' choices look right; the case was a
PASS control at baseline). 0071 differs only in shape: its fast-path was
correctly overridden by LLM adjudication ("keep"), one unit went through the
source portfolio, the other oversized-skipped (8656 essential > 6144
available tokens) — then the same silent gaps (327s, 304s, 342s).

Each silent ~300s gap is one full-tree `make -j4` timing out inside
`verify_file` (`src/capybase/verification.py:4481` —
`_build_timeout = 30 if target_tmpl else 300`; protobuf has no per-file
target template and no compile_commands.json) and then *passing* via the
`g++ -fsyntax-only` timeout fallback (`verification.py:4648-4668`). A cold
protobuf tree under `-j4` needs far more than 300s, so **no build ever
completed; every gate degraded to its tolerant fallback while burning
wall clock**. ~1020 of 0067's 1337s were four sequential doomed builds
(300+300+120+300).

Root cause class: **sprint-18's own WS1/WS3 additions (Phase-2 full-build
fallback, portfolio/swap whole-file verification) multiplied full-tree build
attempts on a repo whose cold build exceeds every cap.** This is a
self-regression of the controls by added verification, not a model failure
and not contamination (the clean rerun was right about that much).

### protobuf-0065 — the gate saw the defect and couldn't say so

All 3 runs identical: 4 units resolved via plain LLM (correctly), splice
coherence failed (stray brace ~line 2550), deterministic brace repair ran
(`+83s`), then a **silent 300s gap** (the brace-fix's whole-file validation
build — same `verify_file` timeout signature), Phase-2 full build timed out
at 120s → inconclusive (by design), file staged, and — the key event —
`pre_continue make -j4` **completed with rc=2** at +89s (the tree having
accumulated ~209s of build state across the two attempts): the merge's real
compile defect surfaced. But the verdict parsed as `unknown` with empty
diagnostics (the make output's error lines weren't extracted), and with
`tests.required=False` the advisory gate let the rebase continue
(`orchestrator.py:13901-13919` returns `run.passed`; the caller treats a
non-required failure as non-blocking). Result: ORACLE_DIVERGENT at sim 0.997
— a build-broken merge shipped with the defect visible in the journal.

Sub-findings: (a) had the Phase-2 build not timed out, it would have seen
the error lines in `text_format.cc` and routed to repair (the WS1 path
working as designed); (b) the diagnostics parser missed compile errors in
`make` output (rc=2, `diagnostics: []`); (c) the brace-fix validation build
is unjournaled.

### protobuf-0055 — oversized first-resolve, micro-patch unreachable

All 3 runs identical and fast (23 events, ~5s): the core unit's structural
candidate failed validation, single-side declined (non-empty base), and the
LLM resolution prompt measured **15.5K/16.3K tokens vs the 8192 window** →
`llm_skipped_oversized_prompt` (`orchestrator.py:11415-11431`, the
post-construction guard; `llm_skipped_oversized` at `11257-11275` is the
separate essential-tokens guard that fired on 0071). The unit escalates
before any candidate exists, so the file never reaches
`_whole_file_repair` — where the WS1 micro-patch lives — at all. There is
no build error yet, so the micro-patch recipe (error + ±10-line window)
has nothing to anchor on; this is not an ordering bug to patch around but
a missing *reduction* mechanism for oversized first-resolves.

The measured design already exists: `docs/oversized-splitting-design-v3.md`
(entity-boundary sub-conflict splitting — splice-safe, brings oversized
marker blocks under the window; 0055's 521-line replayed churn across
multiple entities is exactly its splittable class). The v3 doc targets the
eval's 48K size guard on sqlite; the same mechanism resolves 0055 at the
orchestrator level.

### tokio-0109 — contaminated verdict + an attribution carve-out gap (census corrected)

All 3 runs (first, DNS-era batch — **never re-run clean**) show the same
signature: units 1:0/1:1 took source-portfolio `current_only`; unit 1:2's
LLM call died `failure_kind=request_failed` → coerced into the
first-empty fast-fail → `current_only` accepted (the bug fixed in 66b780b);
then whole-file `cargo check` found 2 real errors (`#[deprecated]` on trait
impls, `ManuallyDrop` misuse), `whole_file_repair` fired — and **skipped
itself**: `"fault attribution: error outside all unit spans (tiered mode)"`
(`orchestrator.py:10243-10267`). Two independent defects:

1. The escalation input was the contaminated empty-fallback candidate
   (post-66b780b this run would escalate honestly at the unit, or resolve
   properly if the endpoint answers).
2. The tiered-mode skip's `_is_build_test` carve-out
   (`orchestrator.py:10255`) only matches `validator == "build_test"` —
   the whole-file cargo check stamps `validator="syntax"` with a
   `cargo check` message prefix (`verification.py:4943`), so in-file
   compile errors whose line attribution falls outside marker spans skip
   the one bounded model repair and escalate instead. In Rust the whole
   crate is one TU; "outside all unit spans" for an in-file error is a
   splice-shift artifact, not a cross-unit reality.

### tokio-0037 — honest, model-bound (no design change)

Clean rerun, runs 2-3 uncontaminated: units accepted, whole-file
`cargo check` failed, CEGIS re-resolve produced candidates with genuine
Rust syntax errors → max retries → escalate. Run 1 additionally shows the
fixed empty-fallback bug (one `request_failed` during the no-route window)
but the majority verdict stands on runs 2-3 alone. The deletion-respect
swap never probes here because the unit escalates before the pre-stage
path — acceptable; 0046 exercises the swap's backstop class.

### sea-orm-0027 — fixed at the side-pick seam; one residual accepted-risk

All 6 runs (both batches): resolution call transport-killed → empty
fast-fail side-pick (fixed, 66b780b) → collapse detector fired correctly
(buffer 100% contained in current, 0% of replayed's new lines kept) →
adjudication call transport-killed → `adjudication: null` → designed
conservative accept (`orchestrator.py:13329-13331`). Post-fix, an outage
escalates at resolution before any buffer exists, so the guard is never
starved during outage windows. Residual: if resolution *succeeds* but the
adjudication call *alone* dies, accept-on-null still ships.
`_adjudicate_subsumption` (`orchestrator.py:13705-13757`) makes **one**
`raw_complete` call with **no technical retry** — any exception → None →
accept. The WS4 calibration chose accept-on-null to avoid false
escalations; that reasoning covers "no endpoint / unparseable", not
"endpoint answered every other call this session and this one timed out".

## Fix designs (sprint-19 candidates, priority order)

### D1 — Session build-budget triage + journaled builds (fixes 0067/0071 elapsed blowout; helps 0065)

Mechanism: a session-scoped build-state tracker.

1. **Journal every build subprocess.** `verify_file`'s builds
   (`verification.py:4466-4517`) and `_run_raw_test`'s Phase-2 run
   (`orchestrator.py:13774`) currently emit nothing — the 300s gaps in
   every journal above. Emit `build_probe` events (cmd, duration, outcome:
   pass/fail/timeout/unavailable). Pure observability, no semantics.
2. **One doomed full-build per session.** After the first full-tree build
   times out, mark `full_build_timed_out` on the session: subsequent
   `verify_file` calls skip straight to the syntax-only fallback, Phase-2's
   full-build fallback journals `skipped: prior timeout` instead of
   re-burning 120s. Re-running a build that just timed out at the same cap
   yields zero information — the tree's partial state persists in the
   working dir, but the cap is the binding constraint, not the tree.
   Effect on 0067: ~720s saved; the case completes well under budget with
   identical content decisions. On a warm/healthy repo (all historical
   sqlite/redis cases) nothing changes — their builds complete.
3. **Raise the Phase-2 full-build cap for known-big trees, corpus-tuned.**
   The 120s `_run_raw_test` cap (`orchestrator.py:13792`) predates
   whole-tree protobuf use. A per-case override (the eval already layers
   corpus-tuned timeouts on top of the profile) beats a global raise.

Safety: no acceptance semantics change — a skipped build was already
inconclusive; the tracker only stops paying the same 300s twice. Guarded
by a `future.` flag, default-on, with an off switch for A/B.

### D2 — Eval-harness build economics (ccache + parallelism; multiplies D1)

The harness runs each case 3× (majority-of-3) from a fresh worktree —
three identical cold builds per case. Config-level, no orchestrator change:

- enable **ccache** for the C/C++ cases (`_ccache_env` already exists,
  `verification.py:4471`) with a session-persistent cache dir — repeats 2-3
  become near-instant, and Phase-2/verify builds within a run recompile
  only the merge-touched TU after the first full pass;
- use **`-j$(nproc)`** or a corpus-tuned job count instead of the case's
  baked `-j4`;
- optionally **pre-build once per case at preflight** (journaled, budgeted
  from the case cap) so every later build is incremental.

Calibration note: ccache hits must not mask merge defects — the merge
changes the conflicted TU's content, so its compile is never a cache hit;
only the ~2000 untouched TUs amortize. Safe by construction.

### D3 — Oversized first-resolve: entity splitting (v3) with the skip demoted to last resort (fixes 0055)

Adopt `docs/oversized-splitting-design-v3.md` at the orchestrator seam:
split oversized marker regions at entity boundaries into sub-units that fit
the window before `_llm_oversized_for_window`/the post-construction guard
escalate. The oversized-skip guards stay as the terminal decision for
non-splittable oversized units (within-one-entity regions), where the v3
data says splitting doesn't apply. For those, one bounded reduced-context
attempt (conflict-region sides ± N context lines, not whole-file sides) is
the fallback ladder rung before the skip — the same reduction philosophy as
the WS1 micro-patch, anchored on the marker block instead of an error
line.

Ordering rule that fixes the census's "mechanism-order gap" as a class:
**reduction mechanisms outrank the skip** — never escalate a unit as
oversized while an untried reduction exists.

### D4 — Failure attribution for rc≠0 build gates (fixes 0065's ship-broken)

Two narrow hardenings, no gate-semantics change without calibration:

1. **Parse the make output.** rc=2 with `diagnostics: []` lost the defect
   (`orchestrator.py:13862-13876` journals whatever the runner parsed; the
   make error lines weren't extracted). Reuse `_classify_build_error_lines`
   on `tests_finished` output so compile errors in the output surface as
   diagnostics, regardless of verdict kind.
2. **Escalate on attributed in-file errors at the final gate.** When the
   pre_continue build is a build command (the same `_phase2_fallback_build_cmd`
   recognition Phase-2 uses), rc≠0, and extracted error lines attribute to a
   file the session wrote → escalate instead of continuing, even with
   `tests.required=False`. This is the "the gate saw it" rule: errors we can
   positively attribute to the merge never ship silently. Unattributable
   failures keep today's advisory behavior (sibling/environmental
   conservatism stays).

### D5 — Whole-file repair: recognize whole-file compile checks in the attribution carve-out (fixes 0109's class)

Extend `_is_build_test` (`orchestrator.py:10255`) to include failures from
the whole-file compile check (validator `syntax` with the cargo/`cc` build
message prefix — tag them at emission, e.g. `detail.source="whole_file_build"`,
rather than string-matching). Effect: an in-file compile error outside
marker spans gets the existing bounded model repair (already capped at one
call by `_phase2_model_used`) instead of skip→escalate. Contamination note:
0109's specific run may resolve outright once 66b780b's fix lands (its unit
1:2 never got a real answer); D5 stands on its own for the class.

### D6 — Subsumption adjudication: technical retry before accept-on-null (0027 residual)

In `_adjudicate_subsumption` (`orchestrator.py:13736-13757`): retry
`request_failed` transport failures 2× with short backoff (mirroring
risk.decide's technical-retry ladder), and journal the distinction
(`adjudication_transport_failed` vs unparseable vs absent). If retries
exhaust: keep the conservative accept — the WS4 calibration's
accept-on-null reasoning still holds for "no usable signal", and a
session-wide outage already escalates elsewhere post-66b780b. This shrinks
the adjudication-only failure window from one flaky call to three
consecutive failures without touching the escalation threshold.

### D7 — Post-fix live rerun matrix

| Case | Why rerun | Expected |
|---|---|---|
| sea-orm-0027 | 66b780b | honest ESCALATE or woven PASS; detector + live adjudication finally observed together |
| tokio-0109 | contaminated (all 3 runs empty-fallback); never clean-rerun | real verdict, unknown |
| protobuf-0067/0071 | after D1/D2 | PASS controls restored under budget |
| protobuf-0065 | after D1/D4 | PASS via repair, or honest escalate with attributed error |
| protobuf-0055 | after D3 | PASS via split resolution (its splittable class), or honest escalate |

Run 0027 + 0109 first — they need no new mechanisms, only the already-fixed
transport carve-out, and 0109's true disposition is unknown.

## Addendum — oracle-shape measurements (post-draft finding)

Measured across all 676 corpus cases with oracles: **68.5% woven, 23.8%
== current verbatim, 7.7% == replayed verbatim.** Five of the seven
failing cases are `== current` (0065, 0067, 0071, 0037, 0109) — the
correct whole-file answer was verbatim at a stage in every one. Two
consequences that refine the diagnoses above:

- **tokio-0037's escalation was self-inflicted, not purely model-bound.**
  The oracle is `current` verbatim (it keeps the dead test struct
  replayed deleted). Unit 1:1's FIRST candidate was current-verbatim —
  oracle-correct, validation-passing (confidence 0.95) — and the
  `preservation_heuristic` rejected it ("copies CURRENT verbatim, but
  REPLAYED has unaccounted changes (deletion)"; risk.py:268 retry
  trigger), after which retries degraded into syntax errors. The
  heuristic is the sea-orm-0027 defense firing on a case where verbatim
  was right; it lacks the context the file-level WS4 adjudication has.
- **tokio-0109's ideal outcome was stage-available; no mechanism reaches
  it.** Oracle == current (jaccard 1.0); the true-side portfolio only
  engages on dup-pathology/asymmetric-churn triggers (churn here 0.18).
  Additionally, 0109's two cargo errors reference tokens present in NO
  side of the conflicted file — their provenance (replay-series
  worktree context vs. a splice artifact) is unreproduced; the rerun
  preserves worktrees to localize it.

Both are framed as reviewer questions in
`docs/sprint19-open-questions-for-review.md` (Q1-Q3), alongside the
whole-side-availability class question the 31.5% one-side-oracle
measurement raises.

## Non-goals

- No relaxation of the collapse/resurrection guards (WS3/WS4 calibration
  stands; the guards were right in every observed instance).
- No fallback endpoints or profile auto-creation (provider-config contract).
- No global build-timeout raises without corpus calibration (D1.3 is
  per-case, layered by the eval harness).
- The xfailed r10 span-intersection gap stays a separate thread.

## Census corrections (applied alongside this doc)

- 0067/0071: "model does not converge within 1200s" → "build-economics
  blowout: sequential cold full-tree builds (300s verify_file caps ×3,
  120s Phase-2, 300s pre_continue) never complete on protobuf; zero model
  calls on 0067" (this doc, D1/D2).
- tokio-0109: "whole-file repair could not re-resolve" → "contaminated
  empty-fallback (all 3 runs) + tiered attribution skip of a whole-file
  cargo error" (this doc, D5; rerun pending).

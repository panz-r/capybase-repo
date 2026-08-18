# Sprint 18 census — capability gaps, guard hardening, measurement discipline

Baseline: the sprint-17 census (Rust 174/194 = 89.7% stable, majority-of-3).
Sprint 18 shifts from eliminating false failures to closing genuine capability
gaps and hardening acceptance gates — with every threshold change backed by
corpus calibration first. Six commits on `dev`; live artifacts in
`/tmp/capybase-live/s18/`.

**Live validation status: RESOLVED — the endpoint was never down.** The
earlier "endpoint down" call was a measurement error: a stale repo-toml URL
(a dead localhost tunnel) was probed instead of the live server, which had
served every prior run. Endpoint selection is now canonical and
host-free-in-repo: provider configs under `~/.config/capybase/providers/`
(host+model for LLM and embeddings + the REQUIRED calibration profile),
resolved by the eval scripts via `--provider` (no fallbacks; running without
a calibration profile is an error), with a pre-commit hook blocking
IPs/hostnames from entering tracked files. The validation batch
(`/tmp/capybase-live/s18/run_ws1_val.sh`, majority-of-3, targets + PASS
controls) runs under `--provider nova-gemma4`; verdicts below marked
[pending-live] depend on it.

## WS0 — CI unblock + quick wins (66ca8b2, 18e5573)

**Test debt: 31 pre-existing failures → 0** (3680 passing; marathon files
excluded by design). Each failure classified stale-vs-real before touching;
six were REAL bugs found underneath the stale expectations:

- deferred-core silent default: a structural candidate whose contested core
  could not be resolved was accepted anyway — current's placeholder shipped,
  replayed's edit silently dropped (protobuf-0055's acceptance class,
  `deferred_core_resolved: false` in the flight journals). The orchestrator
  now rejects it; `deterministically_mergeable` requires a complete resolution.
- modify/delete guards: `_try_token_disjoint` treated an empty side as "no
  additions" (splicing kept the modifier, deletion intent gone); the source
  portfolio's single-side variants did the same on AU/UA whole files;
  `mechanical_reapply` dropped a side's edit when the other deleted its
  anchors (the r10 span-intersection family — the last known gap is xfailed
  with a tracked reason).
- needs_human refusals were converted to deterministic side picks by the
  first-empty fast-fail (overriding the refusal AND dropping intent).
- `_rebase_continue_empty` matched "rebase --skip" inside modern git's
  CONFLICT hint text — every next-commit conflict was read as an empty pick
  and silently skipped a real commit.
- `lint_vs_refactor` fired before `lint_transform` and took the semantic
  side VERBATIM, undoing the lint branch's project-wide token migration.
  When the substitutions are detectable (file-level metadata or >= 5/unit)
  they are now applied to the taken side.
- source-portfolio accepts recorded no `resolution_attempt` (accepted units
  were invisible to attempt consumers). Now records decision=accept.

**Lockfile exemption [pending-live for axum-0017]:** `.lock`/`.sum` files no
longer enter the resurrection scan — a "resurrection" there is a version pin
reappearing after a bump (axum-0017: 103-marker Cargo.lock SAFE_STOPped on
143 lines of pins). Verified the case materializes at `Cargo.lock`, so the
suffix exemption applies.

## WS1 — C/C++ micro-repair loop (b3f3e9b) [pending-live]

The trio's shape (from flight journals + the oracle probe): protobuf-0055
accepted a structural candidate with `deferred_core_resolved: false` whose
build failed (`make` rc=2) while the ORACLE builds; sim 1.000 — a content
defect token-jaccard cannot see, shipped silently because **the only
tests.required-independent build gate (Phase-2's per-file check) never ran**:
protobuf/fmt/json-c/nlohmann have no per-object Makefile rules, so the
per-file template was empty.

Three mechanisms:

1. **Phase-2 full-build fallback** — the pre_continue command serves as the
   Phase-2 whole-tree gate when it IS a build (make/cmake/ninja/meson/scons;
   compound commands recognized by their build words). A failure with NO
   error lines is a timeout/infra artifact — journaled inconclusive, never a
   merge defect.
2. **Duplicate-definition eradication** (deterministic) — gcc `redefinition
   of X` means the spliced file carries X twice. Finds both definition
   regions (brace-balanced blocks / variable statements; calls and forward
   declarations excluded) and deletes exactly one: the identical echo, or
   the copy present verbatim in the pre-merge file (the fresh definition is
   the merge's intent). Declines overload-like divergence.
3. **Micro-LLM patch** — when the CEGIS re-resolve escalates as oversized
   (protobuf-0055: 15.5K-token context vs an 8K window), send only the
   error + a +/-10-line window + the attributed unit's sides (~300 tokens),
   splice the corrected excerpt back, brace-check, validate whole-file.

The WS0a deferred-core fix also removes 0055/0065's original silent-default
acceptance — those units now route to the LLM/portfolio honestly.

## WS2 — fast-path band extension (0196821): calibration says KEEP 0.90

Offline calibration (new `scripts/calibrate_midband_fastpath.py`; churn_ratio
and the winner are pure functions of the case sides, oracle distances via the
eval's own token-jaccard, verdicts joined from baseline-r3/census/cppsoak):

- Both-sides-changed ratio distribution is bimodal: 188 < 0.5, 138 in
  [0.5, 0.75), 128 in [0.75, 0.90), 223 >= 0.90.
- In [0.80, 0.90) with dominance + both-changed gates: 11 cases, ZERO
  flippable non-PASS, ZERO PASS-at-risk. At 0.85: 8/0/0. The mid-band
  oracles are woven merges — exactly what the cascade is for.
- The only flippable at 0.75 is nlohmann-json-0020 (ratio 0.778, oracle =
  replayed 0.997, NEAR_MATCH 0.813) — one case does not justify crossing
  the corpus-calibrated line, and its shape is lint_transform's origin.

Per the plan's own gate ("enable only if separation is clean"): the
separation is clean in the wrong direction. Threshold unchanged.

## WS3 — resurrection guard (c42a5eb) [pending-live for tokio-0037/0046]

tokio-0037/0046 are NOT guard false positives — verified: the oracle and the
replayed side both LACK the "resurrected" content. Root cause (flight
journals): every unit resolved correctly (`current_only`, jacc 1.000 to
oracle), but git's auto-merge context OUTSIDE the markers re-introduced 12
upstream-deleted lines; the resolver only controls marker blocks; the
end-of-rebase scan stopped the rebase too late to repair.

Fix: **deletion-respect swap** — a pre-stage check (merge-index stages still
readable) that swaps the buffer to the VERIFIED upstream side when the buffer
carries upstream-deleted blocks AND >= 90% of its line content is present in
the upstream stage (a context resurrection, not a woven merge; woven buffers
are left for the scan). End-of-rebase scan remains the backstop. Relaxing
the guard itself would ship wrong merges — not done.

## WS4 — sea-orm-0027 (1c1a039) [pending-live]

Root cause (unanimous x3): both sides rewrote 36%/69% of a 407-line file
(ratio 0.47 — woven band); the oracle is a woven merge closest to current
(0.93); the model returned replayed VERBATIM (winner-preservation 0.055) and
passed every structural gate — one-side files compile.

Corpus calibration killed the hard-guard temptation: 79 woven-band cases
have oracle ~= one side verbatim (verdicts include PASS), loser-churn
reaching 0.36 — overlapping sea-orm-0027 exactly. So the guard is LLM-gated
with the existing subsumption adjudication: detect (both churns >= 25% of
base AND >= 20 lines, buffer >= 0.9 contained in one side, <= 0.1 of the
other's new lines survive), adjudicate, escalate only on a positive "keep"
or weak "superseded". None/unparseable/no-endpoint accepts — churn numbers
never escalate alone. Expected: sea-orm-0027 ESCALATE (honest) instead of a
silent wrong merge at 0.79.

## WS5 — empty-response resilience (b21c879)

The first-empty fast-fail only fired for small prompts (< 1500 tokens); the
>= 6K class burned the full retry ladder on prompts the endpoint will never
answer. New oversized arm (>= `future.empty_oversized_token_floor` [6000],
or >= 90% of a known window): skip retries, go straight to verified
single-side recovery or escalation. Refusal carve-out: an oversized
parse-failure (coerced needs_human + empty text) is the endpoint choking,
not a considered refusal — the small arm keeps the strict exclusion.

## Success metrics vs plan

| Metric | Plan target | Status |
|---|---|---|
| Test failures | 0 | **0** (done, committed) |
| C++ micro-repair cases | 3/3 PASS | built; [pending-live] |
| Fast-path band | [0.80, 1.0] if clean | **keep 0.90** (calibration: nothing to gain) |
| axum-0017 | PASS | exemption in; [pending-live] |
| tokio-0037/0046 | 0-2 PASS w/o weakening safety | swap mechanism in; guard NOT weakened (verified correct); [pending-live] |
| sea-orm-0027 | PASS or safe escalate | guard in; expected ESCALATE; [pending-live] |
| Regressions on passing cases | 0 | hermetic suite green; live must-holds scripted |

## Remaining after sprint 18

- Live majority-of-3 validation batch (endpoint down): WS1 trio + PASS
  controls (protobuf-0061/0067/0071, fmt-0002/0004/0006), axum-0017,
  tokio-0037/0046, sea-orm-0027. Script: `/tmp/capybase-live/s18/run_ws1_val.sh`.
- The xfailed r10 span-intersection gap (mechanical_reapply anchor scoping).
- The micro-patch's build re-verification is once-per-session
  (`_p2_build_checked`); if live runs show repaired-but-still-broken builds,
  re-check the build after whole-file repairs.
- sea-orm-0027's ideal PASS (a correct woven merge) remains model-bound; the
  guard converts silent-wrong into honest-escalate.

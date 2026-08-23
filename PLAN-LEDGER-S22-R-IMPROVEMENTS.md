# Sprint-22 Rust-Shard Improvement Plan (from three-reviewer synthesis)

## Reviewer rating

**Reviewer 3 — most useful.** Every mechanism is specified with its
safety argument attached, and two contributions are decisive: (a) the
best-of-N item is framed as *within-session multi-candidate selection,
all compiler-validated* — the legitimate version of an idea Reviewer 1
corrupts; (b) the P5 resurrection check via parent provenance
(`git show :2:`/`:3:`) is the most precise formulation of the three.
"No guard relaxation without provenance" is the correct principle and
anchors the whole plan.

**Reviewer 1 — strongest diagnosis, one integrity lapse.** The R1
acceptance rule ("NEVER accept a coherence-repaired candidate on
coherence alone"), the C1 full-file-search emphasis, the C4 repair
ladder, and the "what I would NOT do" list are all sound. But its
P5/P6 items are metric gaming: the best-of-N aggregation threshold is
moved from 0.95 to 0.94 *specifically to convert sea-orm-0023* (the
reviewer notices the problem mid-paragraph and tunes the threshold
anyway), and the fitness floor is "lowered to fitness − 0.01" — a gate
fitted to the observed 0.591. Both rejected below.

**Reviewer 2 — best architectural vocabulary, one novel signal.**
"Compiler is Authority" is the right name for R1; the
`modified_by_deterministic_repair` flag is a clean implementation
device; the span-intersection resurrection signal (resolved-conflict
coordinates vs resurrected line ranges) is genuinely new — adopted as
an audit field. But the "~98% era-adjusted" projection is
unsupportable, and its soft floor "accept as PASS" inflates verdicts:
a candidate at oracle-sim 0.793 lands WORKING, not PASS, and the plan
will not over-credit conversions.

## The synthesized plan

All three reviewers converge on the same five mechanisms (R1 gate,
C1 cross-language symbol injection, C4 repair diversity, P5
resurrection provenance, use-dedup) — the Rust shard confirmed the
C-shard architecture cross-language, so C1/C4 are built once,
parameterized by language.

| # | Item | Source | Target cases | Effort | Priority |
|---|------|--------|-------------|--------|----------|
| R1 | Post-coherence-repair verification gate | all 3 | tokio-0026 | 1-2h | **P0** (safety hole) |
| C1 | Side-provenance symbol injection, unified C+Rust | all 3 | axum-0019, sea-orm-0023 + redis-0002/0012, sqlite-0030 | 4-6h | **P0** |
| R2 | `use`-statement dedup sweep | all 3 | sea-orm-0021 | 1-2h | **P1** |
| C4 | Repair-diversity interleaving + deterministic retry budget | all 3 | axum-0013, sea-orm-0011/0014 | 3-4h | **P2** |
| P5 | Provenance-aware resurrection guard | all 3 | tokio-0037/0042/0046 | 3-4h | **P3** |
| R3 | Within-session best-of-N candidate selection (compile-gated) | R3#6 | axum-0002-class coin-flips | 2-3h | **P4** (paired A/B gate) |
| R4 | Near-floor sbcr acceptance window (fixed, pre-registered) | R2#4, R1#6 (sanitized) | sea-orm-0011 | 1h | **P5** |

### Explicitly rejected

- **Cross-repeat best-of-N aggregation** (R1#P5): accepting "any repeat
  PASS + sim ≥ threshold" then tuning the threshold to convert a named
  case is post-hoc gate fitting. Majority-of-3 is the pre-registered
  aggregation; it does not change after seeing results. (The
  within-session variant R3 above is the legitimate form.)
- **Per-case fitness-floor lowering** (R1#P6, "floor → fitness − 0.01"):
  same defect — the gate moves to fit the observed 0.591. Only a fixed,
  pre-registered window (R4: floor − 0.02, retry-cap-active trigger) is
  defensible, and its conversions are counted as WORKING where sim
  < 0.90, never as PASS.
- **PASS-granting soft floor** (R2#4): verdict inflation. Candidates
  below oracle-sim 0.90 may be accepted in-session (full validation
  stack still applies) and will be labeled WORKING/NEAR by the eval —
  reviewers' PASS-conversion claims for these cases are over-credited.
- **Wall-clock-dependent retry budgets** (R3, C4 detail): makes runs
  non-reproducible. The budget is a deterministic function of unit
  count and fitness proximity only.
- **Naive single-file `rustc --emit=metadata` gate** (R2, R1 detail):
  false-fails on `crate::` paths and external deps. R1's re-gate
  reuses the existing baseline-relative per-file compile gate (errors
  the pristine side also has are environmental).
- **Midband-trigger extension** (R3#7): deferred to calibration data
  from the sharded rounds — shape-metric enabling is the sprint-18 WS4
  lesson (second rejection, same as C-round).

## Design notes

### R1: provisional acceptance (all three, converged)

Any candidate modified by a deterministic repair rung (coherence,
brace, preprocessor, micro-CEGIS, use-dedup) carries
`provisional=True`. Acceptance then requires the baseline-relative
per-file compile gate to pass (build gate when configured); on failure
the candidate falls through to the repair loop / LLM / escalation —
never accepted on coherence alone. Converts tokio-0026's false accept
into an honest verdict; if C1/R2 then repair the underlying defect it
becomes PASS legitimately.

### C1: unified cross-language missing-symbol repair

Error signature table: C (`'X' undeclared`, `implicit declaration of
'F'`, `unknown type name 'T'`) and Rust (`cannot find X in this scope`,
`unresolved import`, `prefix 'Z' is unknown`, `no function or
associated item named 'm' found`). Search the FULL FILE plus
base/current/replayed for the symbol's declaration/`use`/`mod`/
trait-impl; inject verbatim from a side (nothing invented) at the
correct scope (import block / trait bound, via the skeleton extractor);
re-gate baseline-relative. Reviewer 2's prompt-injection variant is
kept as the retry-context enrichment path (adjacent to C3), not the
primary mechanism — repair-layer first.

### R2: use-dedup sweep

Post-splice: exact-duplicate `use` lines removed; duplicate items
inside `use {A, B, C}` braces deduplicated; keep first occurrence per
module scope. Also wired as a micro-CEGIS stage-1 response to
`defined multiple times` errors. Precedent: the s17 C++ `#include`
dedup. Purely mechanical; compiler validates.

### P5: provenance-primary resurrection guard

Decision rule: a flagged resurrected block that appears verbatim in the
replayed parent's file (the branch's own content — `git show :3:`) is a
legitimate merge choice → downgrade stop→warning, journal the
provenance evidence, let the verdict be decided by the normal gates. A
block absent from both parents (base echo / splice artifact) keeps the
hard stop. Reviewer 2's span-intersection (overlap with resolved
conflict coordinates) is recorded as an audit field, not the decision
rule — explicit-span overlap alone doesn't distinguish side-intent from
base-echo (insertion_union "chose" inside its span and still echoed
base).

### R3: within-session best-of-N

Fires only when an LLM candidate fails the compile gate and retry
budget remains: generate up to 2 additional diverse candidates
(temperature/diverse_sampling), validate ALL through the full gate
stack, accept the first that passes. Distinct from the C-round's
rejected multi-candidate item: there, no evidence the model could
produce compile-clean output; here, axum-0002 and sea-orm-0023 each
demonstrated a passing sample exists (1-of-3 repeats). Bounded cost,
no aggregation change. Requires paired A/B before default-on.

## Honest projection

Mechanism-addressable: ~9-11 of the 15 active failures (R1: 1 verdict
honestized, C1: 1-2, R2: 1, C4: 1-3, P5: 2-3, R3: 0-2). Expected
remainder: model frontier (tokio-0108, sea-orm-0027), sandbox artifact
(sea-orm-0004), residual variance. Era-adjusted projection
**~93-95%**, not the "~98%" one reviewer projected. Era-dead 24 are
environmental and untouched.

## Sequencing

Shard 4 (cpp) is still running — no src/ edits until it completes
(eval subprocesses re-import capybase per case). On landing: baseline
freeze (README row + totals), then implementation in table order
(R1 → C1 → R2 → C4 → P5 → R3 → R4). Each mechanism validates via
paired A/B on its named specimens; the post-fix uniform-commit sharded
reround measures the aggregate and becomes the second README row.
Never declassify a prior PASS (era-sweep invariant).

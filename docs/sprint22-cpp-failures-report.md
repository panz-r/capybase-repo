# Shard 4 Failure Report: C++ (69 non-PASS of 167)

**Context.** Sprint-22 sharded harvest, shard 4 (C++), full mechanism
stack on commit `943b8d5`. First launch (10:53) omitted the size-guard
env and ran 80/167 as a silent subset (incident logged; harness now
prints a SUBSET RUN banner); the corrected relaunch (12:52, command
verified identical to shards 1-3) completed the remaining 87.
Completed 13:54 in 3h01m combined (exit 0). Flights preserved.

**Result: 98/167 PASS (58.7%)** vs harvest baseline 102/167 (61.1%), a
net **-2.4pp** raw; adjusted (era + SAFE_SKIP excluded: 98/(167-45-13)
= 98/109) **89.9%** vs 93.6% (**-3.7pp**). The era census is
**identical to the harvest's** (45 = 45: nlohmann-json 38, fmt 4,
protobuf 3) — zero classification churn, the third language confirming
probe stability.

---

## Population overview

Of 69 non-PASS: **45 era-dead**, **13 SAFE_SKIP** (git resolved the
conflict cleanly — corpus noise, not resolver work), **7 active
escalates**, **4 ORACLE_DIVERGENT**. The 4 divergents turned out to be
the most consequential finding of the shard — and are already fixed.

---

## Class A: The R1 family (4 cases — root-caused and FIXED)

| case | sim | s20 | what happened |
|------|-----|-----|---------------|
| clickhouse-0049 | 1.000 | PASS | lint_vs_refactor accept → coherence rung "repaired" → gate passed → eval build failed |
| clickhouse-0023 | 0.990 | PASS | same signature |
| protobuf-0012 | 0.974 | PASS | same signature |
| protobuf-0038 | 0.965 | PASS | same signature |

**Analysis**: All four 3/3 deterministic with identical sims to s20 —
same-shaped buffers, `compiles` flipped True→False. The journal trail
(clickhouse-0049) matched tokio-0026 exactly, which led to the true
root cause: **the coherence rung's repair was validation-local** —
verify_file validated a repaired copy while the caller wrote the
unrepaired buffer to disk. R1 (implemented 2026-08-23) threads the
repaired text through `VerificationResult.resolved_text`, adds a
fail-closed guard (repaired ⇒ compiler-verified or rejected), and
makes pristine-side probes decline repaired text. **Post-fix specimen
validation: all four PASS** (sims 0.962–1.000), plus tokio-0026 — five
deterministic regressions converted to genuine passes by one fix.

A companion eval gap remains (ledger E1): `oracle_builds` was unprobed
for these four, so the GATE_UNAVAILABLE sandbox-artifact rescue could
not fire; probe-on-divergence will classify any future residue
correctly.

---

## Class B: The guard stop (1 case)

- clickhouse-0020 (sim **1.000**, SAFE_STOP, s20 same): the
  resurrection guard stopped a perfect merge — the C++ twin of
  tokio-0037. **P5** (provenance-aware guard: content present in the
  replayed parent ⇒ legitimate) is the conversion path.

---

## Class C: Compile-gated repair failures (2 cases)

- protobuf-0034 (sim 0.999, REPAIR_FAILURE, s20 same): the
  unterminated-literal case the string-literal repair (pre-eval item 2)
  was built for — the repair fires but the re-gate still fails;
  C4-style repair diversity (different strategy on retry) is the
  follow-up.
- protobuf-0051 (sim 0.999, REPAIR_FAILURE, s20 same): near-oracle
  buffer, deterministic repair cannot close the remaining defect.
  C1/C2-class candidate (missing symbol/include outside the unit).

---

## Class D: Convergence timeouts (3 cases)

- protobuf-0001 (sim 0.997, s20 same), clickhouse-0021 (0.889, s20
  0.857 — improved but not converted): multi-unit cycling against the
  timeout. **C4** (vary the repair, not just the model call) targets
  this class.
- clickhouse-0040 (sim 0.998): see Class E — a variance regression
  presenting as timeout.

---

## Class E: Sampling-variance regressions (2 cases)

| case | harvest | shard 4 | repeats |
|------|---------|---------|---------|
| clickhouse-0040 | PASS 1.000 | ESC 0.998 | ESC/PASS/ESC — coin-flip |
| protobuf-0008 | PASS 0.999 | ESC 0.998 | ESC/DIV/PASS — coin-flip |

Both passed one of three repeats; majority rule kept them honestly
non-PASS. Same disposition as the C shard's noise floor: no repair;
best-of-N within the session (R3) is the only legitimate lever.

---

## Class F: Improvements (2 cases)

- **protobuf-0065** ESCALATE→PASS at sim **1.000**: the sprint-19 D7
  fixed-gate specimen (5 attributed text_format.cc errors,
  tests_required=false override) finally landing clean.
- clickhouse-0013 ESCALATE→PASS 0.843→0.998: majority-PASS variance
  conversion (repeats ESC/PASS/PASS).

---

## Summary table

| class | cases | nature | action |
|-------|-------|--------|--------|
| A: R1 false accepts | 4 | validation-local repair | **FIXED (R1)** — all 4 PASS post-fix |
| B: guard stop | 1 | resurrection guard vs sim-1.000 merge | P5 |
| C: repair failures | 2 | near-oracle, repair can't close | C4; C1/C2 |
| D: timeouts | 3 | multi-unit cycling | C4 |
| E: variance regressions | 2 | coin-flips (1-of-3 passes) | R3 only; honest noise |
| F: corpus noise | 13 | SAFE_SKIP (git resolved cleanly) | excluded from denominator |
| era-dead | 45 | toolchain/dependency drift | probe working |

**Bottom line**: The active C++ failure population was 14 cases (7
escalates + 4 divergents + 2 variance + 1 guard). One quarter of it —
the four divergents — was a single cross-language defect (R1), now
fixed and validated on all five specimens. The remainder maps onto the
existing fix-sprint items (P5, C4, C1/C2, R3) with no new classes.
C++ produced zero WORKING/NEAR_MATCH verdicts — like Rust, its gates
are binary. With R1's four conversions applied, the shard stands at
102/109 = **93.6%** adjusted — exactly the s20 baseline row, with the
10 remaining active cases as the actionable frontier.

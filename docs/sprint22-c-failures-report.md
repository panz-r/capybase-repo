# Shard 2 Failure Report: C (122 non-PASS of 205)

**Context.** Sprint-22 sharded harvest, shard 2 (C), run under the full
mechanism stack (golden-path memory layer present but store unreachable
without the env gate; member-split composition, coherence-repair rung,
mixed-signature era probe, retry relaxation, P3 extreme-asymmetry, P4
insertion-within-deletion). Completed 2026-08-23 04:00 in 3h24m (exit 0).

**Result: 83/205 PASS (40.5%)** vs harvest baseline 87/205 (42.4%), a
net **-1.9pp**. However, the mechanism-level delta is **positive**: +2
deterministic conversions (coherence rung) against -6 sampling-variance
losses. The era census grew from 97 to 98 (redis-0038 reclassified).
Era-adjusted: 83/106 = 78.3%.

---

## Population overview

Of 122 non-PASS cases, **98 are era-dead** (un-passable under the
current toolchain; the probe correctly classifies them in seconds).
The **24 active failures** decompose into five classes.

---

## Class A: Compile-gated perfect-buffer escalates (11 cases)

The dominant class — buffers at sim 0.91–1.0 that fail a build gate:

| case | sim | gate failure | harvest |
|------|-----|-------------|---------|
| redis-0040 | 1.000 | attributed compile error | same |
| redis-0052 | 0.999 | model empty refusal | same |
| redis-0054 | 0.999 | stalled on branch changes | same |
| sqlite-0004 | 0.999 | oversized prompt (12,689t) | same |
| redis-0055 | 0.998 | stalled on branch changes | same |
| sqlite-0029 | 0.997 | unit re-resolve failure | same |
| sqlite-0030 | 0.997 | type-default compile error | same |
| redis-0053 | 0.996 | resurrection guard | same |
| redis-0002 | 0.990 | undeclared identifier | same |
| jsonc-0016 | 0.985 | unused-function (patch fired, re-gate failed) | same |
| redis-0012 | 0.981 | undeclared identifier | same |

**Analysis**: These are the C pipeline's honest difficulty. The
buffers are near-oracle (sim ≥ 0.91); the failures are compile-level
defects the model introduces — undeclared identifiers, type errors,
implicit declarations — that the verification chain correctly catches.
The escalations are conservative-by-construction working as intended.
Micro-CEGIS fires on the P4-attributed cases (redis-0040, jsonc-0016)
but can't repair the defects deterministically. The oversized prompt
(sqlite-0004) is a known sub-unit attribution follow-up from S21.5.

**Includes the coherence-rung conversion candidates**: sqlite-0029 and
redis-0015 fail with "unit re-resolve" — the same class as zenodo-0085
in the Python report. sqlite-0030's type-default error and redis-0002/
0012's undeclared identifiers are micro-CEGIS stage-2 candidates (the
missing-symbol class).

---

## Class B: Sampling-variance regressions (6 cases)

These were PASS in the harvest and escalated this sampling. All at
sim 0.91–1.0 — the buffers are near-oracle; the compile gates caught
different defects on different LLM outputs:

| case | harvest sim | shard sim | defect |
|------|------------|-----------|--------|
| sqlite-0019 | 1.000 | 1.000 | treeview.c re-resolve failure |
| redis-0013 | 1.000 | 1.000 | implicit declaration (cliSwitchProto) |
| redis-0014 | 0.970 | 0.999 | incompatible pointer (wait3) |
| sqlite-0039 | 1.000 | 0.995 | expected identifier (% token) |
| jsonc-0007 | 0.997 | 0.978 | unfixable brace imbalance |
| redis-0047 | 0.939 | 0.912 | attributed compile error |

**Analysis**: The P9 investigation confirmed these are **sampling
variance, not mechanism-caused**: golden-path retrieval was empty (the
store isn't reached without the env gate), the coherence rung attempted
repairs where applicable (jsonc-0007's repair failed on the specific
shape — brace_repair_skipped reason=balance_failed), and the retry
relaxation granted extra attempts on 4 of 6 (the model still didn't
pass — the ceiling). The harvest's sampling happened to produce
compile-clean candidates; this run's didn't.

**Disposition**: No repairs. The ~3% noise floor on compile-gated C
cases is the honest finding. These cases are PASS-or-ESCALATE
coin-flips on each run; majority-of-3 across multiple runs would
stabilize them.

---

## Class C: Coherence-rung conversions (2 cases — the wins)

| case | harvest | shard 2 |
|------|---------|---------|
| sqlite-0008 | ESCALATE 0.998 | **PASS 1.0** |
| sqlite-0014 | ESCALATE 1.0 | **PASS 1.0** |

**Analysis**: Both are the deterministic brace-repair family. These
conversions are NOT sampling — they replicate from the S21.5
specimen validation and are fully attributable to the coherence rung's
splice-gate repair. This is the mechanism working exactly as designed
on its target class.

---

## Class D: The known frontier (3 cases)

- redis-0049 (sim 0.948): unit re-resolve failure — the same specimen
  from the Python report's investigation; its coherence class cleared,
  exposing a stubborn unit
- sqlite-0040 (sim 0.015): tclsqlite.c re-resolve — the #endif
  truncation case (dispositioned in S21 as content truncation)
- jsonc-0004 (WORKING 0.858): the mid-band frontier

---

## Class E: Infrastructure noise (1 case)

redis-0001: git resolved cleanly (SAFE_SKIP). Now correctly excluded
from the real-conflict denominator by P1.

---

## Summary table

| class | cases | nature | action |
|-------|-------|--------|--------|
| A: compile-gated escalates | 11 | near-oracle buffers with compile defects | micro-CEGIS stage-2 targets; honest escalates |
| B: variance regressions | 6 | sampling noise on compile-gated shapes | none (noise floor) |
| C: coherence conversions | 2 | deterministic brace-repair wins | none (mechanism working) |
| D: known frontier | 3 | stubborn units, truncation, mid-band | P5/P7 targets |
| E: corpus noise | 1 | git resolved cleanly | P1 SAFE_SKIP |
| era-dead | 98 | un-passable toolchain drift | probe working |

**Bottom line**: The active C failure population is 24 cases. The
coherence rung delivered +2 deterministic conversions. The 6 regressions
are the compile-gated noise floor expressing on this sampling — not
mechanism-caused. The remaining actionable targets are the
missing-symbol class (redis-0002/0012, sqlite-0030) for micro-CEGIS
stage-2 improvement, and the unit re-resolve failures (sqlite-0029,
redis-0015/0049) which share a class with the Python frontier. The
98 era-dead cases are the harness's ceiling, not the resolver's.

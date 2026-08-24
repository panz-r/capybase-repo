# Sprint-22 Remaining Failures — Post-Fix-Sprint Projection

**Frame.** The frozen 4-shard baseline carried 60 active failures
(non-PASS, excluding 167 era-dead and 16 SAFE_SKIP). The fix sprint
validated 9 conversions (R1: tokio-0026, clickhouse-0023/0049,
protobuf-0012/0038; P5: tokio-0037/0042, clickhouse-0020, redis-0012)
plus 1 NEAR conversion (tokio-0046 0.884). **Projected remaining: 50
active escalates + 6 mid-band (WORKING/NEAR) + 2 sandbox artifacts.**
These are projections — the reround (in flight, uniform commit
`e7e7eb7`) confirms every number below.

Full case lists live in `docs/results/s22/*.jsonl`; per-shard deep
dives in the four failure reports.

---

## Class A: Compile-gated perfect-buffer escalates (12 cases)

The dominant remaining class — sim 0.949–1.000 buffers failing a gate:

| case | sim | shape |
|------|-----|-------|
| redis-0040 | 1.000 | attributed compile error |
| sqlite-0019 / redis-0013 | 1.000 | re-resolve / implicit declaration |
| redis-0014 | 0.999 | incompatible pointer |
| sqlite-0039 | 0.995 | expected-identifier parse |
| redis-0002 | 0.990 | `‘pat’` undeclared (coin-flip; decl IS injectable) |
| jsonc-0016 | 0.985 | patch fired, re-gate failed |
| redis-0015 / sqlite-0029 / redis-0049 | 0.948–0.997 | unit re-resolve |
| sqlite-0030 | 0.997 | corrupted declaration (type-default) |
| protobuf-0034 / 0051 | 0.999 | literal repair fires, re-gate fails |

**Live mechanisms for the reround**: C1 symbol injection (redis-0002's
`pubsubPattern *pat;` is verbatim-injectable), C4 repair rotation
(protobuf-0034/0051's repeat-failed repairs now rotate), E1
probe-on-divergence (redis-0026-class artifacts get classified, not
escalated). Honest expectation: 2-6 convert; the class A hard core
(defects requiring invented content) is the model-capability frontier.

## Class B: Remaining guard stops (4 cases)

redis-0053 (0.996), redis-0030 (0.994), zenodo-0064 (0.976),
zenodo-0063 (0.922) — SAFE_STOP on near-oracle buffers. **P5 v2's
resolved-file provenance directly targets these**: files this session
resolved and validated now downgrade stop→warn. Strong reround
conversion candidates (tokio-0037 was the same shape at sim 1.000).

## Class C: Delimiter/brace repair failures (4 cases)

axum-0013 (0.994), axum-0019 (0.996 — re-triaged: "prefix `item`" is
delimiter-cascade noise, not a missing symbol), sea-orm-0021 (0.983 —
duplicate re-exports, **R2's direct target**), sea-orm-0023 (0.949,
missing trait import, coin-flip). C4 rotation + R2 address three of
the four deterministically or semi-deterministically.

## Class D: Convergence timeouts / retry caps (9 cases)

redis-0054/0055 (0.999, branch-stall archaeology — C7), zenodo-0079
(0.963), protobuf-0001 (0.997), clickhouse-0040 (0.998, variance),
clickhouse-0021 (0.889), axum-0002 (0.859, coin-flip), axum-0033
(0.981), sea-orm-0014 (0.858), sqlite-0039-adjacent. C4's rotation
prevents wasted identical rounds; the retry-cap relaxation (R4,
deferred) and best-of-N (R3, deferred) are the unexercised levers.

## Class E: Model-capability frontier (4 cases)

redis-0052 (0.999, empty refusal), tokio-0108 (0.857), flask-0006
(0.535), zenodo-0028 (0.909) — MODEL_NEEDS_HUMAN: the model explicitly
declined or produced nothing usable. No mechanism claims these; honest
escalates.

## Class F: Mid-band WORKING/NEAR (6 cases)

zenodo-0014 (0.843), zenodo-0003 (0.825), zenodo-0040 (0.760),
zenodo-0044 (WORKING, sim 0.000 anomaly), jsonc-0004 (0.858),
sea-orm-0027 (0.682). Compiling, both-sides-preserving merges that
diverge from the human oracle. Not failures of safety — of style/
semantics matching. The graded middle band the compiler-only
languages lack.

## Class G: Sandbox artifacts (2 cases)

redis-0026, sea-orm-0004 (GATE_UNAVAILABLE, sim 0.995/0.981) — the
oracle fails the same gate. Not resolver failures; E1 makes this class
self-classifying going forward.

## Class H: Known oddities (3 cases)

sqlite-0040 (0.015, #endif truncation — dispositioned S21),
sqlite-0004 (0.999, OVERSIZED 12,689-token prompt — C5 diagnosis
pending), zenodo-0085 (0.800, stubborn unit — P7).

---

## Summary

| class | cases | reround lever | honest expectation |
|-------|-------|---------------|--------------------|
| A perfect-buffer gates | 12 | C1/C4/E1 | 2-6 convert |
| B guard stops | 4 | P5 v2 | 3-4 convert |
| C delimiter/dup | 4 | C4/R2 | 2-3 convert |
| D timeouts | 9 | C4 (partial) | 0-2 convert |
| E model frontier | 4 | none | 0 |
| F mid-band | 6 | none (graded) | — |
| G sandbox | 2 | E1 classification | reclassified |
| H oddities | 3 | archaeology | — |

Projected post-reround active failures: **~35-40 of 50**, era-adjusted
corpus rate **~90-92%** (from 87.9% baseline). The remaining population
concentrates in the model-capability frontier and the compile-gated
hard core — the honest ceiling for this model/endpoint combination.

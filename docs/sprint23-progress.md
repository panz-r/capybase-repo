# Sprint-23 Progress

**Directive**: targeted reruns of selected cases only; full harvest
deferred until mechanisms are exhausted. Batches land with full-suite
gates; specimens validate per batch.

## Batch A — quick fixes (COMMITTED, gate GREEN, specimens DONE)

| item | mechanism | specimen result |
|------|-----------|-----------------|
| D2 | `_safe_conf` guard on 3 adjudication parse sites | axum-0021 → **PASS 1.000** ✓ |
| P5 v2b | Portfolio-accept provenance recording | zenodo-0063 → **PASS 0.922** ✓ |
| C7' | True-empty fast-fail drops token ceiling | redis-0055 → **PASS 0.997** ✓ (0052/0054 shifted to validation-fail — sampling-dependent) |
| C4b | Tried-keys carry spliced-buffer hash | sqlite-0008 ESC/ESC/PASS (improved from 3/3 ESC) |
| E2 | include_str undecidable from temp copy | Relocated to `_compile_rust`; axum-0005/0033 still failing (see batch B specimens) |
| E3 | Empty-oracle exclusion at load | zenodo-0044 excluded |

## Batch B — repair layer (COMMITTED, gate had 2 failures → fixed)

| item | mechanism | specimens pending |
|------|-----------|-------------------|
| D0 | Serial `-j1` retry when parallel make swallows diagnostics | protobuf-0051 |
| Iterated brace | Multi-round single-imbalance repair (≤4 rounds) | sqlite-0019 (2-gap), 0029 (4-gap) |
| Delimiter repair | Stack-based `()`/`[]` + single-edit stray-closer deletion | zenodo-0085 |
| C1b REPLACE | Line replacement (LCS-anchored parent verbatim) + derived prototypes (`{`→`;`) | redis-0013, 0014, 0040, 0047, 0049, sqlite-0030 |

Gate: 2 failures from D0's tuple return type (`_run_raw_test` returns
`(bool, str)` not an object with `.stdout`). Fixed; verified.

## Batch C — strategic (COMMITTED, gate GREEN after F1 gating fix)

| item | mechanism | specimens pending |
|------|-----------|-------------------|
| F1 tier-1 | Deterministic near-one-sided takeover (min churn ≤30 double-counted) | 22 verified targets |
| F1 tier-2 | LLM subsumption adjudicator (failure-path gated) | Symmetric shapes (24 measured) |
| R5 retry ladder | `retry_profile_variant` via `dataclasses.replace` | Wiring into retry loop (batch D) |

## Batch D — expanded scope (COMMITTED, gate RUNNING)

| item | mechanism | status |
|------|-----------|--------|
| F1-smart | 4 conditions + compile check, always-on (no env gate) | ✅ committed |
| D1 fix | Per-round failure signatures accumulated correctly | ✅ committed |
| with_variant() | PromptProfile frozen-dataclass helper | ✅ committed |
| R5 wiring | Retry ladder uses with_variant, 3 orthogonal axes | ✅ committed |
| Prompt instrumentation | Journal event per prompt build | ✅ committed |
| Candidate-diff feedback | Unified diff of prior attempt in retries | ✅ committed |
| R3 best-of-N | Diverse-temperature candidates, full-gate validated | ✅ committed |
| Repair-retrieval audit | Intentionally unexercised (not a bug) | ✅ recorded |
| C5 investigation | Needs specimen run + instrumentation | pending |
| Priority chain | Design only (sprint-24) | ✅ recorded |

Analytical additions (committed during gate waits):
- Failure-mode stability metric (f77f857)
- Dead-mechanism audit (765fb66)
- Escalation-path priority chain design (33d4aa4)
- F1 tier-2 evaluation script (090df5a)
- Sprint projection: +4.0pp adjusted (090df5a)
- Specimen reliability: 20 stable / 4 unstable targets
- Repair-retrieval audit (598a530)

| item | status |
|------|--------|
| D1 | Infrastructure exists but summaries are identical (data-flow bug); fix = accumulate per-round signatures |
| C5 | Context-cap for null-enclosing-symbol units (sqlite-0004's 50K prompt from context bloat) |
| R5 wiring | Thread `retry_profile_variant` into the retry call site |
| C7' verify-path | Specimens shifted mode; population-level value only |

## Specimen run plan (after batch-C gate)

Combined B+C specimens (~20 cases, minutes each, no shards):
- Batch B repairs: sqlite-0019, 0029, zenodo-0085
- C1b: redis-0013, 0014, 0040, 0047, 0049, sqlite-0030
- D0: protobuf-0051
- E2: axum-0005, 0033
- F1 tier-1: flask-0006, tokio-0108, sqlite-0004, sqlite-0040, axum-0002,
  protobuf-0051 (dual D0+F1), sea-orm-0021, 0023, clickhouse-0021, 0040,
  protobuf-0008, axum-0013, 0033, redis-0053

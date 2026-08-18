# Sprint 17 — False-Failure Elimination & Measurement Hardening

Charter informed by the Rust sweep census (154/194 PASS post-winner-floor, zero
true regressions) and three external reviews of it. All reviewers converge on
the same #1 target; this plan consolidates their recommendations, corrects two
of their over-estimates, and adds the variance-aware measurement the
tokio-0109/0110 chase proved we need.

**Sprint goal:** convert the attributed false-failure classes into PASSes or
honest census categories, make the eval variance-aware, and clear the test
debt — with zero regressions. Targets: Rust ≥ 165/194 (85%), C++ +2–3 flips,
100% green test suite.

**Standing validation protocol (applies to every workstream):**
- Every mechanism lands behind a `config.future` flag covered by the
  `CAPYBASE_DISABLE_TAKEOVER`-style kill switch.
- Must-hold set defined up front: the 22-case floor-val set (15 wholesale
  PASSes, redis-0010, sea-orm-0009, flask-0007, jsonc-0001/0013, the trio)
  plus the C++ wholesale wins.
- Any suspected PASS→fail: 3× on new code + 3× on old code before it is
  called a regression (the tokio-0109/0110 lesson — single runs are not
  evidence at temperature 0.2).

---

## WS1 — Validation-gate calibration (the sim≈1.0 cluster) — top priority

The resolver already produces correct content in these cases; the gates are
the defect. Zero merge-logic changes required.

### 1a. Non-code structural-gate exemption (+4 Rust, ~half day)
The eval's post-hoc `compiles` check runs the Rust brace-balance gate on
markdown prose; `axum/CHANGELOG.md` merges at sim 1.000 land in
ORACLE_DIVERGENT. Fix: key the structural check on the **real** conflict
path's extension (`conflict_path`, not the synthetic `conflict_NNNN.rs`) and
skip brace/AST checks for `.md`/`.txt`/`.toml`/`.lock` — marker-free is the
only gate for prose. Also audit orchestrator `_braces_balanced` call sites
(fast path, winner floor) for the same class — believed inert because
language detection says `markdown` there; prove with a wiring test.
**Acceptance:** axum 0009/0010/0014/0024 → PASS 1.000; all 88 markdown
PASSers and the 22-case must-hold unchanged.
**Correction to feedback:** axum-0017 (Cargo.lock) is NOT in this class — it
is a resurrection-guard SAFE_STOP (103-marker lockfile). It belongs to WS5
guard tuning, not the extension guard.

### 1b. tokio-0110/0109 cargo-delta root-cause (~half day)
Both escalate deterministically on old and new code at sim 0.918–0.999 with
"cargo check: 2 new error(s)". Distinguish misfire from microscopic merge
error: materialize the case sandbox and run the cargo gate on **base,
current, replayed, and the oracle** (expected_resolved).
- Oracle also fails → the delta/baseline comparison is wrong or the errors
  are pre-existing → fix the baseline logic (gate misfire).
- Oracle passes → real microscopic error (sim gap is ~1 line) → add a
  micro-repair step: feed the build error + the oracle's version of the few
  differing lines, "fix only these lines" — a 3–5 line prompt that fits the
  endpoint's ~6K-token context trivially.

### 1c. Oracle-build-check in the eval harness (~half day)
Post-resolution, run the same build gate against `expected_resolved`; record
an `oracle_builds` field. Census category **GATE_UNAVAILABLE** for
"content sim ≥ 0.95, gate fails, oracle fails the same gate" — the resolver
is exonerated and the case stops counting as a quality failure. Land as an
additive field first and re-classify existing result JSONs offline before
touching verdict semantics. Applies to protobuf-0055/0065, fmt-0003, and
possibly tokio-0110.

### 1d. C++ build-error localization follow-through (~half day)
- protobuf-0055/0065: sibling-file errors — `_classify_build_error_lines`
  already separates merge-relevant from environmental; wire it through the
  Phase-2 build-check accept path / eval `_c_builds`, with live repros.
- fmt-0003: error at line 1 col 5 before any code — investigate the build
  adapter's target selection (wrong file compiled / BOM); live repro with
  flights.

---

## WS2 — CHANGELOG/markdown resolution (16 tokio escalations)

### 2a. Endpoint context probe (10 min)
Read `/v1/models` context_length (probes.py already can). If the server can
serve > 8K, retry 2 cases with `CAPYBASE_CONTEXT_WINDOW=16384` — if the
empty-response threshold moves, Option A (config) beats new mechanisms. If
the server is hard-capped at 8K, close this line.

### 2b. Deterministic markdown section policy (+8–14 target, 1–2 days)
**Reality check from the sweep journals:** tokio CHANGELOG conflicts are NOT
add-add — the prompt shape says "both sides modified shared base content",
structural `insertion_union` declined (no rule applied) and sbcr fitness was
0.525 < 0.60. So the reviewers' cheap additive-only union will not fire.
Mechanism instead:
- Split oversized non-code conflict regions on markdown headings; resolve
  per section: `insertion_union` where sections are disjoint, side preference
  by section-level churn where overlapping, text validation (marker-free, no
  duplicate headings), LLM only for still-ambiguous sections — each section
  prompt lands far under the ~20KB empty-response limit.
- Flag `enable_markdown_section_policy`; kill-switch covered.
- **Acceptance:** 8–14 of the 16 flip to PASS; all 88 md PASSers + must-hold
  hold; A/B off reproduces the escalations.

### 2c. MODEL_CONTEXT_LIMIT census category
For any markdown case still unresolvable after 2b — honest accounting, not a
resolver failure.

---

## WS3 — Variance-aware evaluation (cheap; do first — it hardens every later number)

- `--repeat-nonpass N` in live_eval_realworld.py: every first-verdict
  non-PASS case reruns N times, majority verdict wins, per-case variance in
  the summary. Optional `--repeat-borderline` for sim ∈ [0.8, 0.95).
- Immediately re-run the current 43-case Rust non-PASS set with N=3 to
  produce the stable baseline this sprint is judged against (~2–3 h,
  background).
- This retroactively answers "which non-PASS are stochastic vs structural"
  — e.g. the sea-orm cascade drew 0.152 once and 0.967 in the A/B; the
  majority-verdict view is the honest one.

---

## WS4 — Test debt & docs (~half day)

- Update the 7 stale escalation tests in test_orchestrator.py (all fail
  identically at e278e78 — expectations predate the deterministic
  fallbacks): assert the new success paths, name the mechanism in comments,
  full suite green.
- Write the sprint census doc (remaining-cases-sprint17.md): the 194-case
  Rust census, cluster definitions, projections vs actuals.

---

## WS5 — Stretch (gated on WS1–4 completion): real algorithmic work

- **sea-orm-0027** (sim 0.682, outband, .rs) — the one genuine quality
  divergence left in the Rust census; plus NEAR_MATCH sea-orm-0017/0023
  root-cause.
- **Guard tuning:** axum-0017 (Cargo.lock) + tokio-0037/0046 resurrection
  SAFE_STOPs — false-positive analysis, clickhouse-0020 precedent.
- **Fast-path band extension to [0.75, 0.90):** journal-only calibration on
  all corpora first; no behavior change until winner-jaccard separation is
  proven in the journals.
- Pattern store / skeleton-aware brace repair / duplicate repair: later
  sprints.

---

## Order & projections

| Day | Work |
|---|---|
| 1 | WS3 flag + launch N=3 baseline rerun → WS1a (extension guard) → WS1b probe |
| 2 | WS1c/1d (oracle-build-check + C++ localization) → WS2a probe → WS2b implement |
| 3 | WS2b validation + full variance-aware Rust re-census + WS4 |

Conservative projection: Rust 154 → **165–170/194 (85–87%)**; C++ +2–3 flipped
or honestly reclassified; 7 tests fixed. (The most optimistic review projected
175/194 = 90%; that assumes all 16 CHANGELOGs flip and Cargo.lock is an
extension-guard win — both of which the census data contradict.)

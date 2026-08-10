# Remaining Non-PASS Cases — Sprint 10 C++ Corpus Eval

**62/76 = 82% PASS | 0 oracle-divergent | 2 NEAR_MATCH | 24 ESCALATE**

This document catalogs every remaining non-PASS case from the Sprint 10 full
C++ corpus eval (88 cases, 12 SAFE_SKIP = 76 real conflicts). Each case is
classified by root cause and assessed for fixability.

---

## Category 1: SAFE_SKIP (12 cases) — Not Actionable

These cases produce no git conflict — git's own merge resolves them cleanly.
They are not real merge conflicts and require no resolution.

| Case ID | Regions | Notes |
|---------|---------|-------|
| clickhouse-0001 | 1 | git resolves cleanly |
| clickhouse-0002 | 2 | git resolves cleanly |
| clickhouse-0005 | 1 | git resolves cleanly |
| clickhouse-0012 | 2 | git resolves cleanly |
| clickhouse-0016 | 1 | git resolves cleanly |
| clickhouse-0019 | 1 | git resolves cleanly |
| clickhouse-0027 | 1 | git resolves cleanly |
| clickhouse-0030 | 1 | git resolves cleanly |
| clickhouse-0032 | 1 | git resolves cleanly |
| clickhouse-0033 | 1 | git resolves cleanly |
| clickhouse-0035 | 1 | git resolves cleanly |
| clickhouse-0046 | 1 | git resolves cleanly |

**Why we can't handle them:** There is nothing to handle. These cases exist
in the corpus because the extraction pipeline found a 3-way text difference,
but at rebase time git's own merge algorithm resolves them without conflict.
They are correctly classified as SAFE_SKIP.

---

## Category 2: SAFE_STOP — Resurrection True Positives (5 cases)

The resurrection guard caught the merge result silently undoing a deliberate
deletion. All 5 are confirmed true positives (investigated in Sprint 5b).

| Case ID | sim | Lines | Deletion Direction |
|---------|-----|-------|--------------------|
| clickhouse-0013 | 0.85 | 72 | base→current (upstream cleanup) |
| clickhouse-0014 | 0.99 | 28 | base→current |
| clickhouse-0015 | 0.98 | 15 | base→current |
| clickhouse-0018 | 1.00 | 66 | base→current |
| clickhouse-0021 | 0.87 | 58 | base→replayed (feature side cleanup) |

**Why we can't handle them:** The resurrection guard is working correctly.
These are genuine cases where the merge result would have silently undone a
deliberate code cleanup. The guard catches them and escalates safely.

The high sim scores (0.85–1.00) are a red herring: sim is token-Jaccard over
the whole file (thousands of lines), so a resurrected 15–72 line block barely
moves the metric. The resurrection signal is orthogonal to similarity.

**What would be needed to resolve these without escalation:** Entity-level
resurrection analysis — understanding whether the "resurrected" content was
intentionally re-added vs. accidentally restored. This requires semantic
understanding beyond what deterministic rules can provide. All three reviewers
agreed: do not relax the resurrection guard.

---

## Category 3: TIMEOUT_CAPABILITY (3 cases) — Model Capacity Limit

The 4B model cannot solve these conflicts even with multiple attempts.

| Case ID | Regions | File | sim | Notes |
|---------|---------|------|-----|-------|
| clickhouse-0020 | 1 | HashJoin.cpp (96KB) | — | Model produces distinct wrong candidates each retry |
| clickhouse-0045 | 2 | MetadataStorage.cpp (10KB) | — | Genuine hard semantic conflict |
| nlohmann-0020 | 6 | input_adapters.hpp | — | 357-line refactor vs lint pass; model returns empty |

**Why we can't handle them:**

### clickhouse-0020 and clickhouse-0045
The model produces genuinely different (but wrong) candidates on each retry.
The no-progress guard correctly does NOT fire (signatures ARE changing — the
model is exploring, not oscillating). But no candidate converges to a correct
resolution. These are conflicts where the 4B model lacks the reasoning depth
to understand the C++ semantics.

**What would help:**
- A stronger model (8B+ parameters)
- Bounded diagnostic repair (feed compiler errors back, 1–2 retries max)
- Shape-specific prompts that explain the conflict semantics

### nlohmann-0020
A large architectural refactor (357 lines changed by replayed) vs a mechanical
lint pass (`and`→`&&`, template spacing). The parent-aware splitting correctly
detects the deletion asymmetry and forces the LLM path. But the 4B model
returns an empty response — the conflict is too complex for its 8K context
window and reasoning capacity.

**What would help:**
- Mini-conflict extraction: resolve deterministic tails, send only the tiny
  core to the LLM
- Shape-specific prompt: "One side refactored. The other made a lint pass.
  Preserve the refactor. Apply the lint only where it clearly corresponds."
- A stronger model

---

## Category 4: TIMEOUT_THROUGHPUT (2 cases) — Too Many Units

Files with 78–89 conflict regions that overflow the 1200s per-case budget.

| Case ID | Regions | File | Notes |
|---------|---------|------|-------|
| nlohmann-0019 | 78 | binary_reader.hpp (85KB) | Value-init pattern: `Type x;` → `Type x{};` |
| nlohmann-0024 | 89 | single_include/json.hpp (879KB) | Amalgamated header |

**Why we can't handle them:**

The retry budget (max_retries=0 for >20 units) bounds each unit to 1 attempt,
but 78–89 units × ~15s per LLM call ≈ 1170–1335s — right at the timeout edge.
The deterministic resolver handles many of these (disjoint_edits via diff3
refinement), but enough fall through to the LLM that the budget overflows.

The investigation showed the diff3-refined base should make these deterministic
at runtime, but the actual runtime inputs differ from the test inputs (the
diff3 refinement may not always be available, or the conflict regions are
content-diverse enough that disjoint_edits declines).

**What would help:**
- Edit-pattern cache working correctly (currently the cache key requires
  exact 3-way content match — the 78 regions have different variable names)
- Normalized pattern reuse (the same `;` → `{};` transformation applied
  across all 78 regions with different identifiers)
- Parallel unit resolution (not feasible on single-GPU server without
  continuous batching)
- Template-based conflict reuse (extract the edit pattern from the first
  resolved region and apply it to siblings with different identifiers)

---

## Category 5: NEAR_MATCH (2 cases) — At Model Ceiling

### clickhouse-0024 (sim=0.92)

**File:** `src/Analyzer/Passes/InjectRandomOrderIfNoOrderByPass.cpp`

**Conflict shape:** One side (current) rewrote the `wrapWithSelectOrderBy`
function body (multi-column SELECT support). The other side (replayed) made
two mechanical API renames: `getJoinTree()` → `getJoinTreeNode()` and added
a `static_pointer_cast`.

**What happens:** `mechanical_reapply_merge` fires and produces garbled output.
The substitution-context guard should have caught this (the base token
`column` appears at multiple positions in the semantic side's rewrite), but
the runtime refined hunk base collapses to 1 line (`column, query_root`),
making the anchor appear only once in the base. The guard checks the anchor
in `sem_toks` (the semantic side), but the semantic side has `column` on
multiple lines — so `_count_subsequence` should return >1. The guard may not
be reaching this code path, or the runtime inputs differ from what the guard
expects.

**Why we can't handle it:** Even when the deterministic rules correctly
decline and the LLM resolves the conflict, the 4B model produces sim=0.94
(not 0.95+). The remaining gap is the model dropping 1–2 lines the oracle
kept. This is at the model's capability ceiling for rewrite-vs-edit conflicts.

**What would help:**
- Investigate why the substitution-context guard doesn't fire (the debug
  bundles are only written for ESCALATE, not NEAR_MATCH — need to extend
  dumping to NEAR_MATCH outcomes)
- Divergence-guided repair: compute which lines the LLM dropped and inject
  them into a retry prompt
- Side-consistency repair: restore lines common to both sides that the
  candidate dropped

### clickhouse-0041 (sim=0.93)

**File:** `src/Functions/FunctionHelpers.cpp`

**Conflict shape:** Both sides independently fixed the same bug in
`withOrdinalEnding` (the ordinal suffix function for 11th/12th/13th "teens").
Current uses early-return style; replayed uses suffix-variable style. Both
are correct implementations.

**What happens:** source_portfolio takes current's version (2 units via
`current_only`), then sbcr composes a third unit that stacks both return
statements — the second references `suffix` which is never declared in
current's version. The sbcr guard should catch return-after-return, but the
stacked statements appear to be in a context where the guard's safe-next
exceptions allow them.

**Why we can't handle it:** This is **inherent ambiguity**. The oracle picked
replayed's implementation; capybase picked current's. Both are semantically
correct — the sim=0.93 gap is a style preference, not a defect. The
undeclared `suffix` variable in the sbcr output is a real defect, but the
case would still be NEAR_MATCH even without it because the two implementations
are textually different.

**What would help:**
- Nothing deterministic — this is a genuine two-valid-answers case
- A stronger model might match the oracle's style
- AST-based semantic equivalence checking (build + test pass = PASS regardless
  of Jaccard) — but this changes the eval metric

---

## Category 6: REPAIR_FAILURE (1 case)

### nlohmann-0033 (sim=0.96)

**File:** `include/nlohmann/detail/output/binary_writer.hpp`

**What happens:** The whole-file validation fails with "unbalanced braces at
line 1370 (missing closing brace — 3 unclosed `{')." The deterministic
resolution (source_portfolio `current_only` + structural `one_sided_change`)
produces a near-correct result (sim=0.96), but the spliced file has a brace
imbalance that the deterministic brace repair can't fix (it only handles
single stray braces, not 3 missing closing braces).

**Why we can't handle it:** The brace imbalance is at a splice junction where
the entity-split sub-units' resolutions don't align structurally. The Phase 2
whole-file repair tries to re-resolve but the LLM candidate also fails (the
file is a header, so the header CEGIS cap limits retries to 0).

**What would help:**
- Multi-brace repair (handle >1 missing brace, not just single stray braces)
- Better splice coherence checking between sub-units
- Allow header files to use the per-unit gcc gate (currently headers skip it)

---

## Category 7: OTHER (1 case)

### nlohmann-0034 (sim=1.00)

**File:** `single_include/nlohmann/json.hpp` (amalgamated header)

**What happens:** Unit 1:0 resolves via `source_portfolio current_only`.
Unit 1:1#s0 is rejected (candidate_rejected). The step escalates because
the header file CEGIS cap (max_retries=0 for headers) prevents any retry.
The result has sim=1.00 — the resolution IS correct — but a sub-unit
escalated.

**Why we can't handle it:** The header file CEGIS cap (`_header_max_retries = 0`)
is a deliberate design choice: header files skip the per-unit gcc gate (because
headers can't be compiled standalone without their includes), so CEGIS retries
are pointless. But this means any header-file validation failure escalates
immediately with no retry path.

**What would help:**
- Allow 1 retry for header files (the whole-file build gate can still validate)
- Better per-unit validation for headers (wrap in a synthetic TU for gcc)
- Investigate why unit 1:1#s0 was rejected (the sim=1.00 suggests the
  overall resolution is correct)

---

## Summary Table

| Category | Cases | Root Cause | Fixable? |
|----------|------:|------------|----------|
| SAFE_SKIP | 12 | No conflict — git resolves | N/A |
| SAFE_STOP | 5 | Resurrection true positives | No (guard working correctly) |
| TIMEOUT_CAPABILITY | 3 | 4B model can't solve | Needs stronger model |
| TIMEOUT_THROUGHPUT | 2 | Too many units for budget | Needs pattern reuse or parallelism |
| NEAR_MATCH | 2 | Model ceiling / inherent ambiguity | clickhouse-0024: maybe; 0041: no |
| REPAIR_FAILURE | 1 | Brace imbalance at splice junction | Maybe (multi-brace repair) |
| OTHER | 1 | Header CEGIS cap | Maybe (allow 1 retry) |

### What would move the needle (ranked by feasibility)

1. **nlohmann-0033 (REPAIR_FAILURE, sim=0.96):** Multi-brace repair or
   splice-coherence checking — ~20 lines, could convert to PASS.
2. **nlohmann-0034 (OTHER, sim=1.00):** Allow 1 retry for header files —
   ~1 line, likely converts to PASS.
3. **nlohmann-0019/0024 (TIMEOUT_THROUGHPUT):** Normalized pattern reuse —
   would need template-based conflict reuse (the edit-pattern cache extended
   to handle different identifiers). Medium effort.
4. **clickhouse-0024 (NEAR_MATCH 0.92):** Side-consistency repair or
   divergence-guided CEGIS. Medium effort.
5. **Everything else:** Requires a stronger model or fundamental architecture
   changes (tree-sitter is out of scope by design).

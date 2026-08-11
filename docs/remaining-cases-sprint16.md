# Remaining Non-PASS Cases — Sprint 16 C++ Corpus Eval

**Status: 88-case eval in progress (75/88 complete at time of writing)**

This document catalogs every remaining non-PASS case from the Sprint 16 full
C++ corpus eval, with root cause analysis and feasibility assessment for each.

Sprint 16 ships 14 new mechanisms beyond Sprint 10 (pattern reuse, intent
coverage repair, shape router, token equivalence, macro-atomic tokenization,
move detection, statement splitting, compile_commands verification, header
syntax validation, lint transform, confidence calibration, mini-conflict
extraction, sbcr provenance guard, deterministic lint transform) plus 31
defect fixes from 9 code review rounds.

## Summary (as of 75/88 cases completed)

```
Total cases:              88
SAFE_SKIP (git resolves): 12  (not real conflicts)
Real conflicts:           63+ (eval still running)
PASS:                      46
NEAR_MATCH:                 3  (sim 0.92–0.94)
ESCALATE:                  14
ORACLE_DIVERGENT:           0

Real-conflict PASS rate:  46/63 = 73% (so far)
```

---

## Category 1: SAFE_SKIP (12 cases) — Not Actionable

These produce no git conflict — git's own merge resolves them cleanly.

| Cases | Count |
|-------|------:|
| clickhouse-0001, 0002, 0005, 0012, 0016, 0019, 0027, 0030, 0032, 0033, 0035, 0046 | 12 |

**Action:** None needed. These are not real merge conflicts.

---

## Category 2: SAFE_STOP — Resurrection True Positives (3 cases)

The resurrection guard correctly caught the merge silently restoring deleted content.

| Case | sim | Lines | Elapsed | Deletion Direction |
|------|-----|-------|---------|--------------------|
| clickhouse-0013 | 0.85 | 2 regions | 110s | base→current (upstream cleanup) |
| clickhouse-0014 | 0.99 | 1 region | 86s | base→current |
| clickhouse-0015 | 0.98 | 1 region | 70s | base→current |

**Root cause:** One side deliberately deleted code (upstream cleanup); the
merge would silently restore it. The resurrection guard catches this and
escalates safely.

**Feasibility:** Not fixable without relaxing the resurrection guard, which
all 7 reviewers agreed should remain conservative. Entity-level resurrection
analysis (understanding whether the "resurrected" content was intentionally
re-added) would help, but requires semantic understanding beyond what
deterministic rules can provide.

---

## Category 3: TIMEOUT — Wall-Clock Budget Exhausted (6+ cases)

### 3a. Large single-file conflicts (model can't resolve in time)

| Case | sim | File size | Regions | Elapsed | Root cause |
|------|-----|-----------|---------|---------|------------|
| clickhouse-0018 | 0.00 | 335K | 1 | 1200s | 325K-char file — even entity splitting produces oversized sub-units |
| clickhouse-0020 | 0.00 | 98K | 1 | 1200s | 109K-char file — model returns empty on oversized conflict |
| nlohmann-0017 | 0.00 | 888K | many | 1200s | Amalgamated header — many sub-units, CEGIS retries exhaust budget |

**Root cause:** The conflict file is so large (98K–888K chars) that even
after entity splitting and mini-conflict extraction, the sub-units exceed
the model's 8K token context window or the CEGIS loop exhausts the 1200s
per-case timeout.

**Feasibility for clickhouse-0018 (335K):** The file is enormous
(HashJoin.cpp). Entity splitting doesn't help because the conflict spans
a single large function body. Mini-conflict extraction partially helps
(resolves deterministic tails) but the remaining ambiguous core is still
too large. **Needs a stronger model or decomposition-then-composition
prompting.**

**Feasibility for clickhouse-0020 (98K):** Similar — a single large
function. The model returns empty responses. **Needs mini-conflict
extraction to shrink to a small core, but the core is still too large.**

### 3b. High-unit-count throughput cases (too many LLM calls)

| Case | sim | File size | Regions | Elapsed | Root cause |
|------|-----|-----------|---------|---------|------------|
| nlohmann-0019 | 0.00 | 87K | 78 | 1200s | 78 regions × ~15s per LLM call ≈ 1170s — at timeout edge |
| nlohmann-0020 | 0.00 | 19K | 6 | 1200s | 357-line refactor vs lint — model returns empty on large conflict |
| nlohmann-0024 | 0.00 | 900K | 89 | 1200s | Amalgamated header, 89 regions — pattern reuse not yet helping enough |

**Root cause for nlohmann-0019/0024:** Too many conflict regions. Each
region requires a separate LLM call (~15s). 78–89 regions × 15s =
1170–1335s, right at the 1200s timeout. The edit-pattern cache helps for
exact-content matches, but the regions differ by identifiers, so the
insert-pattern reuse (Sprint 11) should help — but apparently doesn't fire
on enough of them.

**Feasibility:** The normalized edit-pattern reuse (insert patterns, Sprint 11)
was designed for exactly this case (`Type x;` → `Type x{};`). If it's not
firing, it may be because:
1. The conflicts aren't pure value-init — they may have mixed changes
2. The pattern extraction fails on the specific token sequence
3. The unit-count-aware retry budget (max_retries=0 for >20 units) prevents
   even the first unit from being resolved if the structural rules decline

**Root cause for nlohmann-0020:** The 357-line refactor-vs-lint conflict
generates an empty model response. The mini-conflict extraction may help
by shrinking the conflict, but if the ambiguous core is still >50 lines,
the model can't handle it. **Needs the lint transform rule to fire
deterministically (and→&& applied to refactor side) or decomposition.**

---

## Category 4: OVERSIZE_PROMPT — Context Window Exceeded (2 cases)

| Case | sim | File size | Prompt tokens | Root cause |
|------|-----|-----------|---------------|------------|
| nlohmann-0004 | 1.00 | 956K | 18,441 > 8,192 | Amalgamated json.hpp — single conflict region is enormous |
| nlohmann-0007 | 0.98 | 940K | 18,823 > 8,192 | Same file, different conflict |

**Root cause:** The entire `json.hpp` amalgamated header is a single
conflict region. Entity splitting can't subdivide it (it's one massive
function or block). The prompt for a single unit is 18K tokens, exceeding
the 8K context window.

**Feasibility:** These need mini-conflict extraction to shrink the prompt.
The ambiguous core should be much smaller than the full conflict.
However, for these specific cases, the conflict may be so entangled that
even the core exceeds the window. **Needs statement-level splitting
(Sprint 14) applied within the mini-conflict core, or a larger context
window model.**

---

## Category 5: NEAR_MATCH — Model At Ceiling (3 cases)

| Case | sim | Regions | Elapsed | Conflict shape |
|------|-----|---------|---------|----------------|
| clickhouse-0024 | 0.92 | 1 | 100s | Rewrite-vs-edit: one side rewrote function body, other renamed API |
| clickhouse-0041 | 0.93 | 3 | 348s | Inherent ambiguity: two valid implementations of ordinal-ending function |
| clickhouse-0017 | 0.94 | 1 | 64s | Model drops a side obligation (accepted by risk but below sim threshold) |

### clickhouse-0024 (sim=0.92)

**What happens:** `mechanical_reapply_merge` fires deterministically and
produces a near-correct result (sim=0.92). The token splice is clean
(macro-atomic tokenization + shape router working). The remaining gap is
the model dropping 1-2 lines when it gets the LLM fallback for the core.

**Feasibility:** The intent coverage repair (Sprint 11) should restore
dropped common lines. It may not be firing because the lines aren't common
to BOTH sides — only to ONE side. **Side-specific line restoration** (not
just side-common) would bridge this gap.

### clickhouse-0041 (sim=0.93)

**What happens:** `source_portfolio` picks `current_only` for all 3 units.
The result compiles and is functionally correct, but uses a different
implementation style than the oracle. Two valid implementations of the
same ordinal-ending function (early-return vs suffix-variable).

**Feasibility:** This is **inherent ambiguity** — both implementations
are correct. The sim gap (0.93) is a style preference, not a defect.
The confidence-calibrated escalation (Sprint 15) helps in CI/unattended
mode (deterministic confidence overrides the sim threshold), but the eval
still counts it as NEAR_MATCH. **Not fixable without semantic equivalence
evaluation.**

### clickhouse-0017 (sim=0.94)

**What happens:** The model produces a compiling merge but drops a side
obligation. The risk engine retries, but the model can't fix it. The
convergence escape hatch accepts the candidate (it compiles). sim=0.94.

**Feasibility:** Same as clickhouse-0024 — side-specific line restoration
or divergence-guided CEGIS would help bridge the 0.94→0.95 gap.

---

## Category 6: MODEL_EMPTY — Model Returns Empty Response (2 cases)

| Case | sim | File size | Elapsed | Root cause |
|------|-----|-----------|---------|------------|
| clickhouse-0021 | 0.89 | 10K | 698s | Model returns empty after retries; wall deadline reached |
| clickhouse-0049 | 1.00 | 136K | 889s | Model returns empty — file too large for 8K window |

### clickhouse-0021 (sim=0.89)

**What happens:** The conflict is in DistinctStep.cpp (10K). The model's
first candidate drops a side's additions. Risk retries. Model returns
empty. Retries exhausted. Wall deadline reached at 698s. The sub-unit
prompt is manageable (~5K tokens) but the model can't resolve the
semantic conflict (both sides modified shared content differently).

**Feasibility:** The confidence-calibrated escalation might accept the
0.89 candidate (if deterministic confidence is high). But 0.89 < 0.80
deterministic threshold, so it escalates. **Needs a stronger model or
better prompt context (symbol declarations for the identifiers the model
is confused about).**

### clickhouse-0049 (sim=1.00)

**What happens:** InterpreterSystemQuery.cpp (136K). The model returns
empty on every attempt — the file is too large for the 8K context window.
The candidate would be correct (sim=1.00 in the 33-case run) but the
model never produces output.

**Feasibility:** Mini-conflict extraction should shrink this to a
tractable core. The issue is that the conflict region is large relative
to the file. **Needs mini-conflict extraction + entity splitting to
produce a small core.**

---

## Category 7: CEGIS_NO_PROGRESS — Hard-Failure Signature Cycling (1 case)

| Case | sim | File size | Elapsed | Root cause |
|------|-----|-----------|---------|------------|
| nlohmann-0003 | 1.00 | 328K | 425s | No-progress guard fires — same failure signature repeats |

**What happens:** Unit 1:0 resolves via `source_portfolio`. Unit 1:1
goes to the LLM, which produces empty responses. The no-progress guard
detects the same failure signature repeating and escalates. The candidate
would be correct (sim=1.00) but the model never produces output for the
second unit.

**Feasibility:** Mini-conflict extraction should shrink unit 1:1 to a
smaller core that the model can handle. The 328K file means the unit's
prompt is likely oversized. **Needs mini-conflict extraction or
oversized-prompt trimming.**

---

## Category 8: REPAIR_FAILURE — Brace Imbalance (1 case, from 33-case run)

| Case | sim | File | Elapsed | Root cause |
|------|-----|------|---------|------------|
| nlohmann-0033 | 0.96 | binary_writer.hpp | 176s | Splice junction brace imbalance — 3 missing `}` |

**What happens:** Entity splitting creates sub-units. Sub-unit s1 resolves
via `one_sided_change` (takes the replayed side's function body, which
has unclosed braces — the marker boundary cut across the function).
Splicing produces a file with 3 unclosed braces. The brace repair can't
fix it (the closers need to go at the correct scope, not at EOF).

**Feasibility:** The mini-conflict extraction should help here by
resolving the deterministic tails (which include the closing braces)
and shrinking the ambiguous core. The brace repair improvement (Sprint 10.5)
with `};` handling should also help. **This case may improve with the
Sprint 16 changes — needs re-verification.**

---

## Summary: Root Causes and Fixability

| Root cause | Cases | Fixable? | How |
|------------|------:|----------|-----|
| SAFE_SKIP (no conflict) | 12 | N/A | — |
| Resurrection true positives | 3 | No | Guard working correctly |
| Timeout: file too large | 3 | Hard | Stronger model or decomposition |
| Timeout: too many regions | 3 | Medium | Pattern reuse needs to fire more |
| Oversize prompt | 2 | Medium | Mini-conflict extraction for core |
| NEAR_MATCH (model ceiling) | 3 | 1 maybe | Side-specific line restoration |
| MODEL_EMPTY | 2 | Medium | Mini-conflict + context trimming |
| CEGIS no-progress | 1 | Medium | Mini-conflict extraction |
| REPAIR_FAILURE | 1 | Maybe | Brace repair + mini-conflict |

### What would move the needle (ranked by feasibility)

1. **Pattern reuse firing more** (nlohmann-0019/0024 throughput): The
   insert-pattern reuse should resolve the `Type x;` → `Type x{};` pattern
   across 78+ regions. If it's not firing, the extraction or matching
   logic needs investigation.

2. **Mini-conflict extraction for oversized prompts** (nlohmann-0003/0004/0007,
   clickhouse-0049): The mini-conflict pass should shrink these to tractable
   cores. If the core is still too large, statement-level splitting
   (Sprint 14) should subdivide further.

3. **Lint transform for refactor-vs-lint** (nlohmann-0020): The
   deterministic lint transform (Sprint 15) should resolve the 357-line
   refactor-vs-lint conflict by applying lint substitutions to the
   refactor side.

4. **Side-specific line restoration** (clickhouse-0024/0017): Extending
   the intent coverage repair to restore side-specific (not just
   side-common) dropped lines would bridge the 0.92→0.95 gap.

5. **Decomposition-then-composition** (clickhouse-0018/0020): For
   genuinely huge single-function conflicts, splitting the LLM task
   into sub-tasks (signature, first branch, second branch, return) and
   composing deterministically would work within the 4B model's capacity.

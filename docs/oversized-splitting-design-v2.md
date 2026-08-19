# Design: Tiered Verification for Oversized C Files (v2)

Revised from v1 incorporating reviewer feedback on cross-unit dependencies,
deadline allocation, header handling, and targeted build extraction.

## Problem

33 of 35 sqlite cases timeout at the 900s case cap. The conflict marker
regions are small (7-29 lines), the model resolves them correctly (sim
0.97-1.00 when given time), but the end-to-end case time exceeds 900s.

The bottleneck is the **cost asymmetry between inner-loop and outer-loop
verification**: each CEGIS iteration costs ~100s (model) + ~75s (whole-file
make) = ~175s, and with 3-5 iterations per file, the budget is consumed
before convergence.

The conflict regions are small, the prompts are small (marker region +
localized base + skeleton + function-local context = ~1000-2000 tokens).
The model gets the right answer. The issue is purely verification latency
× iteration count.

## Solution: Tiered Verification

Shift from O(N×M) build cost (N units × M CEGIS iterations, each running
the full make) to O(N + K) where K is a small bounded number of final
whole-file builds.

### Architecture

```
Phase 1 — Per-unit resolution (fast verification):
  For each conflict unit (with rolling deadline allocation):
    - Source portfolio (5+1 candidates, zero model calls)
    - Structural resolver (15 rules, zero model calls)
    - LLM CEGIS loop with PER-UNIT gcc -fsyntax-only verification (~1s)
    - Semantic errors tolerated (deferred to Phase 2)
    - Rolling deadline: unit_budget = (file_deadline - elapsed) / remaining_units
    - Header files (.h/.hpp): cap at 1 model call (no per-unit gcc gate)
  → accepted per unit, or escalated per unit

Phase 2 — Whole-file verification (authoritative, bounded):
  Splice all accepted units → whole file
  Run whole-file build (make -j4 or targeted build)
  If pass: accept the file
  If fail:
    1. Error localization: parse gcc error → map to fault unit
       - If error maps to NO unit (cross-unit, linker, outside markers):
         escalate immediately (blind retry wastes 100s+)
       - If error maps to a unit: continue to repair
    2. Deterministic repair beam on the attributed unit (brace, gcc fix-it,
       side-consistency, etc. — all model-free, ~1s each)
    3. If deterministic fix found: re-splice, re-verify whole file (build #2)
    4. If no deterministic fix: ONE model re-resolve of the attributed unit
       (per-unit gcc verification only, not whole-file)
    5. Re-splice, re-verify whole file (build #2 or #3)
    6. If still fails: escalate
  Phase 2 time budget: strict cap (e.g., 200s)
```

### Why this preserves safety

The whole-file build remains the **final authority**. The change is that it
runs 1-3 times instead of 3-6 times. Per-unit gcc -fsyntax-only catches
parse errors (the most common model defects) at ~1s; semantic errors are
tolerated per-unit and caught by the final whole-file build. Cross-unit
dependency errors (Unit A adds a typedef, Unit B also adds one) are caught
by the whole-file build and handled by the deterministic repair beam's
`duplicate_entity` rule.

### Five refinements from reviewer feedback

#### 1. Rolling deadline allocation (not static division)

Instead of `file_deadline / num_units` (which wastes leftover time from
fast units), use a rolling calculation:

```
At the start of each unit:
  unit_budget = (file_deadline - time_elapsed_so_far) / remaining_units
```

If Unit 1 resolves in 30s (of a 200s allocation), Unit 2 gets the extra
170s. This maximizes utilization of the file-level deadline and avoids
unnecessary escalations on later units.

#### 2. Header file Phase 1 cap

Header files (.h/.hpp) skip the per-unit gcc gate (headers aren't compiled
standalone). Without a gate, Phase 1 CEGIS retries are blind — the model
produces output, nothing validates it, and it retries on advisory warnings
only. Cap header file Phase 1 to exactly 1 model call (plus the structural
resolver + source portfolio). The whole-file build in Phase 2 is the
header's true verifier.

#### 3. Phase 2 time-bounded, not iteration-bounded

Instead of hardcoding `max_whole_file_repair_retries = 0` (exactly 1
attempt), use a strict time budget:

```
phase_2_budget = 200s  # or remaining file deadline, whichever is smaller
while time_in_phase_2 < phase_2_budget:
    1. Run deterministic repair beam
    2. If fixed: break, accept
    3. If model re-resolve not yet used: run ONE model re-resolve
    4. Re-verify whole file
    5. If fixed: break, accept
    6. If both deterministic + model used: break, escalate
```

This allows a "1-2 punch" (deterministic → model → deterministic) without
risking an infinite loop, while strictly capping Phase 2 wall time.

#### 4. Smart blame attribution for Phase 2 repair

When the whole-file build fails, the error must be mapped to the correct
unit for repair:

- Parse gcc's `file:line:col:` from the error.
- Map the line to the conflict unit whose marker_span contains it.
- If the error is a linker error or maps to NO unit's lines (cross-unit
  dependency, sibling file, outside all markers): **escalate immediately**.
  A blind model retry on a cross-unit error burns 100s+ and fails again.
- If the error maps to a specific unit: re-resolve that unit only.

The existing `_attribute_whole_file_failure` function already does
line-to-unit mapping. The enhancement is the "escalate if no unit matches"
guard.

#### 5. Targeted build extraction (precondition check)

Before implementing the architectural change, extract the exact gcc command
for a single source file from the build system:

- Run `make -n {stem}.lo` (or `.o`) once during prepare to capture the
  full gcc command (with all -I, -D, -std flags).
- Cache the command string.
- In verify_file, run that single gcc command instead of `make -j4`.

If this brings per-iteration verification from 75s to <5s, the existing
multi-iteration CEGIS loop may finish within 900s without the architectural
change — preserving the proven safety of whole-file CEGIS. This should be
attempted first; the tiered verification design is the fallback if targeted
builds can't be reliably extracted.

### Implementation scope

**Precondition check (1 day):**
1. Extract targeted build command via `make -n {stem}.lo` for sqlite.
2. If per-iteration verification drops to <5s: the existing CEGIS loop
   should converge within 900s. Measure with a targeted rerun.

**If targeted builds don't solve it (the tiered design):**
1. Add rolling deadline allocation to Phase 1's per-unit loop.
2. Cap header file Phase 1 CEGIS to 1 model call.
3. Refactor Phase 2 to time-bounded (200s) with smart blame attribution.
4. Reduce whole-file builds to 1-3 per file (from 3-6).

### Expected impact

**With targeted builds alone** (if extraction works): per-iteration drops
from 175s to ~105s. With 3-4 iterations, total = 315-420s. Well within
900s. Projected: 25-30 of 33 sqlite timeouts convert → overall 60-70%.

**With tiered verification** (if targeted builds are infeasible): Phase 1
(~300s for 1-2 units with per-unit gcc) + Phase 2 (~200s for 1-2 builds
+ repair) = ~500s. Also within 900s. Same projected impact.

Both approaches preserve the compiler as the final authority and the
conservative escalation semantics.

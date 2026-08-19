# Design: Multi-Conflict Independent CEGIS for Oversized C Files

## Problem

33 of 35 sqlite cases timeout at the 900s case cap. The conflict marker
regions are small (7-29 lines), the model resolves them correctly (sim
0.97-1.00 when given time), but the end-to-end case time exceeds 900s.

The bottleneck is NOT the model's reasoning (it gets the right answer)
and NOT the prompt size (the marker region is small, the base is localized
to ±15 lines around the conflict). The bottleneck is **verification
latency × CEGIS iterations**:

- Each CEGIS iteration: model call (~100s) + whole-file build verification
  (~75s for sqlite's `make -j4`, or ~30s timeout + gcc fallback for
  targeted builds) = ~130-175s per iteration.
- With 3-5 CEGIS iterations per conflict unit, total = 400-875s.
- For files with 2 conflict units (e.g., sqlite-0056), Phase 2 whole-file
  repair adds another 1-2 iterations × 175s = 350s more.

The file-level wall deadline (600s) bounds the total, but it fires
cooperatively (between model calls), and a single in-flight generation
can block for 100-240s past the deadline.

### Why current approaches don't solve this

- **Build-target narrowing** (`make {stem}.lo`) compiles only one TU in ~1s
  — but headers (.h) have no object target, and the full `make -j4` (75s)
  runs for them.
- **ccache** caches unchanged TUs across iterations — but the first build
  is always cold (75s), and header files trigger full rebuilds.
- **File-level deadline** bounds total time but can't interrupt in-flight
  model calls.

## Proposed solution: per-conflict-unit independent resolution

Split the resolution of multi-conflict files into independent per-unit
CEGIS loops, each with its own smaller prompt, its own build verification,
and its own deadline. Then splice the results together and run a single
final whole-file verification.

### Current flow (all-at-once)

```
Phase 1: for each unit in file:
    resolve unit (model + per-unit gcc verification)
Phase 2: splice all units → whole file
    verify whole file (make -j4)
    if fail: repair (re-resolve attributed unit, re-verify whole file)
    repeat until pass or budget exhausted
```

Problem: Phase 2 re-verifies the WHOLE file on every repair iteration.
For sqlite, each verify_file call is 75s. With 3 repair iterations,
that's 225s of build time alone, on top of 300-500s of model calls.

### Proposed flow (independent + final)

```
Phase 1: for each unit in file:
    resolve unit INDEPENDENTLY:
      - model call (small prompt: marker region + localized base + skeleton)
      - per-unit verification (gcc -fsyntax-only, ~1s)
      - CEGIS retries on the UNIT only (not the whole file)
      - unit-level deadline (e.g., 180s per unit)
    → accepted or escalated per unit
Phase 2: splice all accepted units → whole file
    verify whole file ONCE (make -j4 or targeted build)
    if fail: single repair pass (deterministic + at most 1 model call)
    → final accept or escalate
```

Key difference: **Phase 1 verifies per-unit (gcc -fsyntax-only, ~1s),
not whole-file (make -j4, ~75s)**. The expensive whole-file verification
runs ONCE at the end, not on every CEGIS iteration.

### Why this works

1. **Per-unit gcc -fsyntax-only is ~1s** (vs ~75s for full make). The
   per-unit CcsSyntaxValidator already does this — it compiles the spliced
   candidate in /tmp with `-fsyntax-only`. It catches parse errors (missing
   semicolons, unbalanced braces) which are the most common model defects.

2. **Semantic errors (unknown types, missing declarations) are tolerated**
   by the per-unit gate (deferred to Phase B). So per-unit verification
   won't false-reject correct merges that depend on project-internal types.

3. **The whole-file build runs once.** If it fails, the deterministic
   repair beam + at most one model re-resolve attempt handle it. This
   bounds the expensive build verification to 1-2 calls instead of 3-6.

4. **Independent deadlines per unit** (e.g., 180s) mean a hard unit
   escalates cleanly without consuming the entire file's budget. Other
   units still resolve.

### What changes in the orchestrator

The orchestrator's Phase 1 loop already resolves units independently
(`_resolve_unit` per unit). The change is in Phase 2:

**Current Phase 2:** splice → verify_file → if fail, repair → verify_file
→ if fail, repair → ... (CEGIS on the WHOLE FILE)

**Proposed Phase 2:** splice → verify_file → if fail, ONE deterministic
repair pass + at most ONE model re-resolve → verify_file → accept or
escalate. No multi-iteration CEGIS on the whole file.

This means reducing `max_whole_file_repair_retries` from 1 to 0 (or
making it conditional on file size), and relying on Phase 1's per-unit
CEGIS (which already has its own retry budget + wall deadline) to
produce correct output.

### Per-unit prompt size

The per-unit prompt is already small:
- Conflict sides: the marker region only (7-29 lines for sqlite cases)
- Base: localized to ±15 lines around the marker via anchor-based windowing
- Skeleton: compact entity list (~400 tokens) for oversized files
- Function-local context: enclosing function signature + ±3 lines

Total: ~1000-2000 tokens per unit. Well within the 8K window.

### Wall-time budget allocation

With per-unit independent resolution:

```
File with 2 conflict units, 35K chars:
  Unit 1: model call (~100s) + gcc verify (~1s) + CEGIS retry (~100s) = ~201s
  Unit 2: model call (~100s) + gcc verify (~1s) = ~101s
  Phase 2: splice + make -j4 (~75s) = ~75s
  Total: ~377s (well within 900s)
```

vs current:

```
  Phase 1: 2 × model call (~100s) + per-unit gcc = ~202s
  Phase 2: make -j4 (~75s) + repair model (~100s) + make -j4 (~75s) = ~250s
  Total: ~452s + convergence overhead → 600-900s
```

### Safety

The whole-file build still runs as the final authority. Any incorrect
splicing (e.g., a unit's resolution breaks a cross-unit dependency) fails
the build gate. The deterministic repair beam handles common splice
defects (brace imbalance, boundary echoes). The model re-resolve handles
semantic defects. Escalation remains the safe fallback.

The per-unit gcc -fsyntax-only is LESS authoritative than the whole-file
build (can't resolve sibling #include headers), but the semantic-pattern
tolerance (unknown type, missing header → deferred, not hard-failed)
means it only hard-rejects genuine parse errors — which are exactly the
defects we want to catch early, before spending 75s on a full build.

### What does NOT change

- The conflict unit model (one resolution per marker block) — unchanged.
- The per-unit validator chain — unchanged.
- The source-derived candidate portfolio — unchanged.
- The deterministic repair beam — unchanged.
- The escalation semantics — unchanged.

### Implementation scope

1. **Reduce whole-file repair CEGIS to 0-1 iterations** (currently mirrors
   `max_retries_per_unit = 2`). Set `max_whole_file_repair_retries = 0`
   for C/C++ files > 10K chars, or make the Phase 2 loop run at most one
   repair iteration instead of the full CEGIS budget.

2. **Per-unit wall deadline**: allocate the file-level deadline across
   units (e.g., `file_deadline / num_units` per unit), so a single hard
   unit can't consume the entire budget.

3. **Phase 2 single-pass**: after splicing, run verify_file once. If it
   fails, run the deterministic repair beam + at most one model re-resolve
   (the existing Exit-A fix already enables the deterministic pass). If
   that fails, escalate. No multi-iteration CEGIS loop in Phase 2.

### Expected impact

For sqlite: Phase 1 model calls (~200-300s total for 1-2 units) + Phase 2
single build (75s) + at most one repair (~175s) = ~550s. Well within the
900s case timeout. Projected: 20-25 of the 33 timeout cases should
complete, pushing overall success from 38% to 60-70%.

For redis and json-c: minimal impact (they already resolve in 60-320s).
The reduced Phase 2 CEGIS saves 1-2 build cycles (~75-150s) on repair
cases.

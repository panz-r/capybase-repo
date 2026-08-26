# Sprint-23 Final Specimen Run: Detailed Failure Report

29 cases ran with every sprint-23 mechanism active. 11 PASS, 1
NEAR_MATCH, 17 ESCALATE. This report provides per-case mechanism
trails, root-cause analysis, and fix recommendations for reviewer
feedback.

---

## 1. axum-history-0013 (sim 0.994, 3/3 ESCALATE)

**File**: `axum-extra/src/lib.rs`
**Oracle**: current side (sim 1.00)
**Failure reason**: "unmerged paths remain after staging"

**Mechanism trail** (first session):
1. `token_disjoint` structural rule → accepted
2. File gate fails: "unbalanced braces at line 102 (1 unclosed '{')"
3. Coherence repair applied (rung fires) but doesn't fully balance
4. Whole-file repair retry 1 → model returns **empty** on every attempt
   (4 retries, all empty)
5. Whole-file repair retry 2 → same (3 more empty retries)
6. F1 tier-2 fires: chose `current` (correct side) at 0.95 confidence
   in 3/5 sessions — but the takeover still failed to compile

**Root cause**: The structural `token_disjoint` merge produces a
single-imbalance brace gap that the coherence rung can't close. The
model returns empty on every retry for this unit (the endpoint
refuses this conflict shape). F1 tier-2 adjudicates correctly but
the takeover's file still doesn't compile.

**What's needed**: The brace gap at line 102 needs a deterministic
insertion that the current rung can't produce — likely a
multi-entity boundary the single-imbalance repair can't locate.
The F1 tier-1 takeover would bypass this (take the whole current
side) but doesn't fire because the churn is 36 vs 18 (tier-1
threshold is 30 on the LOWER side, which is 18 — this SHOULD fire).

**Fix recommendation**: Investigate why F1 tier-1 isn't firing
despite min-churn 18 ≤ 30. The `_f1_eligible` conditions may not be
satisfied (repairs may not be fully "exhausted" in the pipeline's
counting). This is a trigger-condition bug, not a mechanism gap.

---

## 2. flask-history-0006 (sim 0.535, 3/3 ESCALATE)

**File**: `src/flask/__init__.py`
**Oracle**: current side (sim 1.00)
**Failure reason**: "model produced an empty resolution; model
self-reported needs_human=true"

**Mechanism trail**:
1. All pre-LLM mechanisms decline (no structural rule applies)
2. Model produces empty on all 3 retries
3. P1 empty check fires (failure_kind would be "empty" for zero-byte)
   BUT: the failure reason says "model self-reported
   needs_human=true" — suggesting the model returned a JSON shell
   with empty resolved_text AND needs_human=true
4. The P1 "empty" check catches zero-byte responses; this is a
   JSON-shell response (has text, empty resolved_text)
5. P2 whole-side portfolio never fires (P1 didn't classify as
   "empty", so the fallback chain didn't engage)

**Root cause**: The model returns a valid JSON response with
`{"resolved_text": "", "needs_human": true}` — P1's `len(raw_text)
< 10` check doesn't fire because the raw response has ~40 chars of
JSON. The parser coerces this to `failure_kind="parse_failed"` or
`"model_refusal"`, not `"empty"`.

**What's needed**: Extend P1 to also check for parsed-empty (JSON
parses but resolved_text is empty/whitespace). The coercion-gap
fix in batch A partially addressed this but only at the C7' level,
not the parser level.

**Fix recommendation**: In `_candidate_from_response`, after JSON
parsing succeeds, check if `resolved_text` is empty and the model
didn't provide edits. If so, emit `failure_kind="empty"` instead of
proceeding with the empty candidate. This is the "parsed-empty"
extension documented in the sprint-23 ledger.

---

## 3. protobuf-history-0051 (sim 0.999, 3/3 ESCALATE)

**File**: `src/google/protobuf/descriptor.cc`
**Oracle**: replayed side (sim 1.00)
**Failure reason**: "unmerged paths remain after staging"

**Mechanism trail**:
1. Source portfolio accepts `current_only` candidate
2. File gate fails: `make[1]: *** [Makefile:1917: all-recursive]
   Error 1` — build failure with NO per-file diagnostics
3. Whole-file repair retry 1 → model produces a candidate that
   passes validation at the unit level
4. File gate still fails with the same driver-line-only error
5. D0 fires: serial `-j1` retry recovers diagnostics (but the
   specific error isn't shown in the journal)
6. Repair rotation fires: symbol_inject skipped ("already failed
   for this failure signature")
7. F1 tier-2 fires: chose `current` at conf 1.0 — **WRONG SIDE**
   (oracle is replayed). Reason: "The current and replayed versions
   are identical to the base version, indicating no actual conflict"

**Root cause**: **Two stacked issues**:
(a) The build failure has no per-file diagnostics on the first
attempt (D0's serial retry should recover them, but the recovered
diagnostics aren't visible in the journal)
(b) F1 tier-2's prompt clips the sides to 6000 chars. For this
large file, the differences between current and replayed are
beyond the clip boundary. The model sees "identical" snippets and
defaults to current — which happens to be the wrong side.

**What's needed**: 
- F1 tier-2 needs a diff-centered prompt (show only the changed
  regions, not the full clipped sides)
- D0's serial-retry diagnostics should be journaled for repair
  feedback (currently only the count is journaled, not the content)

**Fix recommendation**: The F1 tier-2 prompt should compute a
unified diff between current and replayed, show only the diff
hunks with ±3 lines of context, and ask the model to judge
subsumption on the ACTUAL changes. This eliminates the clip problem
for all large files.

---

## 4. redis-history-0013 (sim 1.000, 3/3 ESCALATE)

**File**: `src/redis-cli.c`
**Oracle**: current side (sim 1.00)
**Failure reason**: "unmerged paths remain after staging"

**Mechanism trail**:
1. Unit 1: sbcr resolves (fitness 0.66)
2. Unit 2 sbcr declines (fitness 0.599 < floor 0.60 — closest miss)
3. Model produces a candidate for unit 2 that passes unit validation
4. Risk engine retries on "preservation_heuristic: drops a side
   obligation" → eventually escape hatch accepts
5. Unit 3: `lint_vs_refactor` structural rule → accepted
6. File gate fails: `implicit declaration of function
   'cliSwitchProto'`
7. Whole-file repair → C1b line_replace fires: line 890 replaced
   with `if (cliAuth() != REDIS_OK)` (WRONG replacement — should
   be the cliSwitchProto prototype)
8. File gate PASSES after the (wrong) replacement
9. Build still fails (the line replacement didn't fix the actual
   missing prototype)
10. F1 tier-2 chose current (correct) in 2/3 sessions but the
    takeover's file still fails

**Root cause**: **C1b's line replacement chose the wrong line.**
The error is `implicit declaration of function 'cliSwitchProto'`
at line 890. The correct fix is to add a forward declaration for
`cliSwitchProto` near the top of the file. Instead, C1b replaced
line 890 (the line where cliSwitchProto is CALLED) with a
completely different function call (`cliAuth()`). The LCS-based
parent matching found `cliAuth()` as the "closest" line because
both are `if (... != REDIS_OK)` patterns.

**What's needed**: C1b's line replacement should verify that the
replacement actually ADDRESSES the compile error (the symbol
mentioned in the error should be present in the replacement, or
the replacement should be a declaration of that symbol). The
derived-prototype arm (which would create `static int
cliSwitchProto(void);`) is the correct fix and should be preferred
over the line-replacement arm for implicit-declaration errors.

**Fix recommendation**: For `implicit declaration` errors, skip
the line-replacement arm entirely and go straight to
derived-prototype or symbol-injection. The line-replacement should
only fire for type-mismatch/incompatible-pointer errors where the
model's line itself is wrong.

---

## 5. redis-history-0040 (sim 1.000, ESC/PASS/ESC)

**File**: `src/redis-cli.c`
**Oracle**: current side (sim 0.95)
**Failure reason**: "compiler authority: pre-continue build failed
with errors attributed to a merged file"

**Mechanism trail**:
1. `disjoint_edits` structural rule → accepted
2. File gate fails: `passing argument 2 of 'output_help' from
   incompatible pointer type`
3. Whole-file repair → model produces a passing candidate
4. File gate passes, but pre-continue BUILD fails (rc=2)
5. Micro-CEGIS fires: stage 1 (duplicates) declines, stage 2
   (missing symbol) declines
6. P5 resurrection downgrade fires → the case is allowed to
   complete
7. Build still fails → the eval classifies as ESCALATE

**Root cause**: The merge passes the per-file gate (gcc syntax
check) but fails the whole-tree build (the argument type mismatch
only manifests at link time or with full compilation). Micro-CEGIS
declines because the error isn't a duplicate or a missing symbol.
The resurrection downgrade lets the rebase complete, but the build
still fails.

**What's needed**: The `output_help` argument type needs the
correct call from the parent side (redis-0040's correct call:
`output_help(--argc, ++argv)` exists verbatim in the replayed
side). C1b's line-replacement should fire here but the error
message doesn't contain a symbol name for the
`find_replacement_line` to anchor on.

**Fix recommendation**: C1b should also parse `incompatible
pointer type` errors for the function name and argument position,
then search parents for the correct call. The correct call IS in
the replayed side (verified in sprint-23 archaeology).

---

## 6. redis-history-0047 (sim 0.912, ESC/ESC/PASS)

**File**: `src/redis-cli.c`
**Oracle**: replayed side (sim 0.94)
**Failure reason**: "compiler authority: pre-continue build failed"

**Mechanism trail**:
1. Unit 1: `token_disjoint` → accepted
2. Unit 2: model produces candidates that pass unit validation but
   risk engine retries on "both_sides_represented: drops a side's
   additions" → 2 retries → escalate
3. Source portfolio accepts `current_only` candidate
4. File gate fails: `'struct config' has no member named
   'interactive'`
5. The `interactive` member was removed by current (upstream
   deprecation) but replayed code references it
6. File gate eventually passes (repair or portfolio succeeded)
7. Build fails → ESC

**Root cause**: The `struct config` member `interactive` exists in
replayed but not current. The merge uses current's struct
(no `interactive`) but replayed's code references it. C1's symbol
injection should add `int interactive;` to the struct but doesn't
fire because the error message says "has no member named" not
"undeclared" — C1's error parser doesn't match this pattern.

**What's needed**: C1's error signature table should also match
`'struct X' has no member named 'Y'` → inject `TYPE Y;` into
struct X from the parent that has it.

**Fix recommendation**: Add the struct-member pattern to
`_MISSING_SYMBOL_PATTERNS` in verification.py.

---

## 7. redis-history-0049 (sim 0.966, 3/3 ESCALATE)

**File**: `redis.c` (multi-unit, 18 resolution attempts)
**Oracle**: replayed side (sim 0.97)
**Failure reason**: "whole-file repair could not re-resolve a unit
in redis.c"

**Mechanism trail**:
1. 7+ units resolved via `token_disjoint`, `disjoint_edits`,
   `source_portfolio`
2. File gate fails: `implicit declaration of function 'deleteKey'`
3. Coherence repair fires (brace imbalance from the multi-unit
   splice)
4. File gate fails again: "coherence repair applied without
   compiler verification" — the R1 fail-closed guard
5. Build fails (`make redis.o`)

**Root cause**: `deleteKey`'s prototype `static int
deleteKey(redisDb *db, robj *key);` exists verbatim in
base+replayed. C1's symbol injection should fire but the multi-
unit splice creates a brace imbalance FIRST, which triggers the
R1 fail-closed guard before C1 can inject the prototype. The
repair cascade is: brace imbalance → coherence repair → R1
fail-closed → escalate (C1 never gets a chance).

**What's needed**: The R1 fail-closed guard should not block C1's
symbol injection when the coherence repair has already balanced
the braces. The guard fires because the repaired text hasn't been
compiler-verified — but C1's injection would ADD the missing
symbol and potentially resolve the compile error.

**Fix recommendation**: After the coherence rung repairs the
braces, run C1's symbol injection BEFORE the R1 fail-closed check.
The symbol injection may fix the underlying compile error that the
brace repair exposed.

---

## 8. redis-history-0052 (sim 0.999, 3/3 ESCALATE)

**File**: `redis.c`
**Oracle**: current side (sim 0.99)
**Failure reason**: "model produced an empty resolution; model
self-reported needs_human=true"

**Mechanism trail**:
1. sbcr resolves unit 1 (fitness 0.62)
2. sbcr declines unit 2 (fitness 0.50 < floor 0.60)
3. Model produces empty on all 3 retries for unit 2
4. Same JSON-shell pattern as flask-0006: model returns JSON with
   empty resolved_text + needs_human=true

**Root cause**: Same as flask-0006 — the parsed-empty gap. P1
catches zero-byte responses but not JSON shells with empty
resolved_text.

**Fix recommendation**: Same as flask-0006 (parsed-empty
extension).

---

## 9. redis-history-0055 (sim 0.998, 3/3 ESCALATE)

**File**: `redis.c`
**Oracle**: current side (sim ~1.00)
**Failure reason**: "no hard-failure progress: signature repeated
2/2 times — stalled on 17 unaccounted branch changes"

**Mechanism trail**:
1. Model produces candidates that pass unit validation
2. Risk engine retries on preservation_heuristic (drops side
   obligations)
3. After 2 retries with same signature → no-progress guard fires
4. Model returns empty on subsequent retries
5. P1 empty fast-fail: fires but single-side candidates also fail
   verification

**Root cause**: The model oscillates between non-empty candidates
that drop replayed-side changes and empty responses. The
no-progress guard correctly identifies the cycling. The
empty-response fallback fires but the single-side candidates don't
pass the build gate.

**What's needed**: This case's oracle is one side verbatim
(current, sim ~1.00). F1 tier-1 should fire but the churn is too
symmetric. P2's whole-side portfolio should try the pristine sides
but the empty fast-fail's single-unit candidates already failed.

**Fix recommendation**: F1 tier-1 needs a compile-clean trigger:
if one side's pristine text compiles cleanly and the other doesn't,
take the compiling side regardless of churn ratio. This is the
"compile-clean as primary condition" from the sprint-24 plan.

---

## 10. sea-orm-history-0021 (sim 0.983, 3/3 ESCALATE)

**File**: `src/entity/prelude.rs`
**Oracle**: replayed side (sim 0.98)
**Failure reason**: "whole-file repair could not re-resolve a unit"

**Mechanism trail**:
1. `lint_vs_refactor` structural rule → accepted
2. File gate fails: "17 new error(s): the name `EntityName` is
   defined multiple times; the name `EntityTrait` is defined
   multiple times..."
3. Model returns empty on all 4 retries
4. F1 tier-2 fires: chose `replayed` (correct side!) at conf 0.90
5. But the takeover still fails to compile

**Root cause**: The R2 use-dedup mechanism should remove the
duplicate `use` statements, but the duplicates persist. The
`lint_vs_refactor` structural rule creates a merge with duplicated
imports. R2's dedup fires but doesn't remove all 17 duplicates
(only exact duplicates are removed; these may have slightly
different formatting or ordering).

**What's needed**: R2's dedup should also handle near-duplicate
`use` statements (same path, different formatting/ordering).
Alternatively, the F1 tier-1 takeover would bypass the splice
entirely, but min-churn is 3 (very asymmetric — tier-1 SHOULD
fire!).

**Fix recommendation**: Check why F1 tier-1 doesn't fire with
min-churn=3. Same trigger-condition investigation as axum-0013.

---

## 11. sqlite-history-0004 (sim 0.999, 3/3 ESCALATE)

**File**: `src/sqliteInt.h` (5899 lines)
**Oracle**: current side (sim 1.00)
**Failure reason**: "oversized prompt: 12689t > 8192t window"

**Mechanism trail**:
1. Units 1 and 2 resolve via sbcr
2. Unit 3: sbcr declines (modification conflict)
3. LLM called for unit 3 → prompt is 50,759 chars (12,689 tokens)
4. Prompt exceeds the 8192-token window → skipped
5. No mechanism fires (the case never reaches the failure path)

**Root cause**: The prompt for unit 3 contains 50K chars despite
the units being only 2-3 lines each. The `_SIDES_MAX_CHARS=4000`
cap should limit the sides. The structural context block is capped
at 30 units. The 50K must come from entity-split sub-unit context
or sibling-resolution accumulation (the C5 investigation's
finding).

**What's needed**: The prompt_composition instrumentation (now
active) should show the decomposition. The fix is likely capping
the sibling-resolutions block or the entity-split context
injection.

**Fix recommendation**: Run this case with the instrumentation
active, read the prompt_composition event, and cap the dominant
component.

---

## 12. sqlite-history-0019 (sim 1.000, 3/3 ESCALATE)

**File**: `src/treeview.c`
**Oracle**: replayed side (sim ~1.00)
**Failure reason**: "whole-file repair could not re-resolve a unit"

**Mechanism trail**:
1. Source portfolio + plain_llm resolve all units
2. File gate fails: "unbalanced braces at line 1290 (2 unclosed)"
3. Whole-file repair retry 1 → still "2 unclosed at line 1280"
   (same count, different line — the imbalance MOVED)
4. Whole-file repair retry 2 → model produces a candidate → still
   fails
5. The iterated brace repair's 3-round convergence stop correctly
   stopped after detecting the moving imbalance

**Root cause**: The 2-unclosed-brace gap moves between repairs
(line 1290 → 1280), indicating the model's re-resolve produces a
new splice with the same structural issue at a shifted location.
The iterated repair correctly identifies this as non-convergent
(fundamental corruption) and escalates.

**What's needed**: The oracle is one side (replayed). F1 tier-1
should fire and bypass the splice entirely. min-churn would be
low (the case is near-one-sided).

**Fix recommendation**: Same F1 tier-1 trigger investigation.

---

## 13. sqlite-history-0029 (sim 0.996, 3/3 ESCALATE)

**File**: `src/insert.c` (very large)
**Oracle**: replayed side (sim ~1.00)
**Failure reason**: "whole-file repair could not re-resolve a unit"

**Mechanism trail**:
1. Source portfolio accepts for unit 1
2. Unit 2 sub-units: model produces candidates that fail with
   `expected ')' before '!=' token` — a syntax error from the
   entity-split fragments being incomplete
3. After 3 retries per sub-unit, escalate
4. Unit 3 eventually resolves via plain_llm
5. File gate fails: "4 unclosed '{' at line 2761"
6. Iterated brace repair fires → repairs to "3 unclosed at line 16"
7. More retries → still failing
8. F1 tier-2 chose `replayed` (CORRECT side) but the takeover's
   build still fails

**Root cause**: The entity-split sub-units produce incomplete
fragments (the `expected ')'` errors are from split boundaries
cutting through expressions). The whole-file brace imbalance (4
unclosed) is beyond what the iterated repair can fix in 3 rounds.
F1 tier-2 chose the correct side but the pristine replayed text
also fails to compile (the case is in the C corpus where the
build uses strict flags).

**What's needed**: The entity-split boundary detection needs
improvement (don't split through parenthesized expressions). The
4-unclosed brace gap is genuinely fundamental corruption.

---

## 14. sqlite-history-0030 (sim 0.997, 3/3 ESCALATE)

**File**: `src/loadext.c`
**Oracle**: replayed side (sim ~1.00)
**Failure reason**: "unmerged paths remain after staging"

**Mechanism trail**:
1. Source portfolio accepts `current_only`
2. File gate fails: incompatible pointer type in the sqlite3_value
   function table
3. Model retries produce empty responses
4. C1b line_replace fires (replaces line 2)
5. F1 tier-1 takeover fires (`replayed` side)
6. Still fails: `unknown type name 'sqlite3_hard_heap_limit64'`
7. C1b line_replace fires again (replaces with
   `sqlite3_soft_heap_limit64`)
8. F1 tier-1 takeover fires again
9. Still fails

**Root cause**: Multiple stacked type errors in the function
pointer table. C1b's line replacement fires but each fix exposes
the next error. The F1 tier-1 takeover takes the replayed side
(which is the oracle) but it also has the type errors — suggesting
the replayed side's build fails too (strict compiler flags on the
C corpus).

**What's needed**: The correct resolution is the replayed side's
function table, but the C compiler's strict flags reject both
sides' code. This is approaching the "era-adjacent" class where
the era's code doesn't compile under modern compilers.

---

## 15. sqlite-history-0040 (sim 0.015, 3/3 ESCALATE)

**File**: `src/tclsqlite.c`
**Oracle**: replayed side (sim 1.00)
**Failure reason**: "unmerged paths remain after staging"

**Mechanism trail**:
1. `one_sided_change` resolves units 1 and 2
2. File gate fails: "unbalanced preprocessor directives at line
   3921 (missing #endif)"
3. Preprocessor repair fires → still fails at line 3920
4. Model retries → empty responses (28,958-token prompts)
5. Coherence repair fires → "unknown type name 'Tcl_Interp'"
6. C1b derived-prototype fires for Tcl_Interp
7. Still fails — the prototype doesn't resolve the missing type
8. F1 tier-2 chose `replayed` (correct) but the takeover also
   has the missing #endif

**Root cause**: The long-standing #endif truncation issue.
The splice loses a `#endif` and the preprocessor repair can't
close the gap (the content between the #ifdef and the expected
#endif is missing — it's not just a missing directive, it's
missing content). The sim=0.015 confirms catastrophic divergence.

**What's needed**: Content reconstruction (the missing block
needs to be recovered from the parent). This is a known
long-standing case (documented since sprint-21).

---

## 16. tokio-history-0108 (sim 0.857, 3/3 ESCALATE)

**File**: `tokio/src/io/util/mod.rs`
**Oracle**: current side (sim 1.00)
**Failure reason**: "model produced an empty resolution; model
self-reported needs_human=true"

**Mechanism trail**:
1. All pre-LLM mechanisms decline
2. Model produces empty on all 4 retries
3. Same JSON-shell pattern as flask-0006

**Root cause**: Same parsed-empty gap.

**Fix recommendation**: Same parsed-empty extension.

---

## 17. zenodo-hdiff-0079 (sim 0.963, 3/3 ESCALATE)

**File**: `conflict_0079.py`
**Oracle**: current side (sim 0.97)
**Failure reason**: "model produced an empty resolution; model
self-reported needs_human=true"

**Mechanism trail**:
1. sbcr resolves at fitness 0.61 but fails validation
2. Model's first attempt: `SyntaxError: unmatched ')'` — the
   delimiter error
3. Subsequent attempts: all empty
4. Same JSON-shell pattern

**Root cause**: The delimiter repair (batch B) should fix the
unmatched `)` on the first attempt, but it doesn't fire because
the model's first candidate has the error, not the spliced
buffer. The delimiter repair operates on the coherence rung's
input (the file-level buffer), not on the model's individual
candidates. After the first candidate fails with the unmatched
paren, the model returns empty on all subsequent attempts.

**What's needed**: The delimiter repair should also fire on the
model's candidate level (not just the file-splice level), or the
retry prompt should include the specific delimiter error for the
model to fix.

---

## 18. clickhouse-history-0021 (sim 0.847, NEAR/ESC/NEAR)

**File**: (large C++ file)
**Oracle**: replayed side (sim 0.92)
**Failure reason**: (none — completed as NEAR_MATCH)

**Mechanism trail**:
1. File gate passes (first session)
2. Build passes
3. Session completes
4. Eval judges sim=0.847 (below the 0.90 PASS bar)

**Root cause**: The merge compiles and is marker-free but doesn't
match the oracle closely enough. This is the graded mid-band —
the merge is defensible but stylistically different from the human
resolution.

**What's needed**: The F1 tier-1 takeover (taking the replayed
side verbatim, which is the oracle at sim 0.92) would produce a
PASS. But the churn is too symmetric for tier-1 to fire.

---

## Cross-cutting findings

### 1. F1 tier-1 not firing on clear targets (5 cases)

axum-0013, sea-orm-0021, sqlite-0019, clickhouse-0021, redis-0055
all have oracle≈parent at high sim and low min-churn, but F1
tier-1 doesn't engage. The trigger conditions (`_f1_eligible`)
may be too strict, or the pipeline-state variables (wf_retries,
wf_budget, _phase2_model_used) aren't reaching the expected values
in these flows.

### 2. Parsed-empty gap (4 cases)

flask-0006, redis-0052, tokio-0108, zenodo-0079 — all show the
model returning a JSON shell with empty resolved_text and
needs_human=true. P1 catches zero-byte only.

### 3. F1 tier-2 prompt starvation (2 cases)

protobuf-0051 and axum-0013 — the tier-2 prompt clips sides to
6000 chars, hiding the actual differences for large files.

### 4. C1b wrong-line replacement (1 case)

redis-0013 — the line-replacement arm fires on an
implicit-declaration error but replaces the CALL line instead of
injecting the prototype.

### 5. Model empty-response as dominant failure mode (8 of 17)

axum-0013, flask-0006, protobuf-0051 (model retries), redis-0052,
redis-0055, sqlite-0030, tokio-0108, zenodo-0079 — all have the
model returning empty on at least some retries. This is the
single largest failure mode in the specimen population.

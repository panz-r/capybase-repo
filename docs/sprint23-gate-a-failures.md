# Sprint-23 Gate A: Failing Cases Without Expected Fixes

**Context.** Batch A's specimen run (9 cases) produced 3 conversions
and 6 failures. This report analyzes the 6 failures: what mechanism
was supposed to fix them, why it didn't fire, and whether a fix is
pending in the current tree or genuinely missing.

---

## Case 1: axum-history-0005 (sim 0.994, 3/3 ESCALATE)

**Expected fix**: E2 (include_str guard)
**What actually failed**: The model produced an **empty resolution**
(`no resolved_text`) — not an include_str path issue. E2's relocation
to `_compile_rust` doesn't address empty responses.

**Root cause**: The model declines to answer on this conflict shape.
C7′ (empty fast-fail) should engage, but the failure_kind is
`needs_human` (the parser coerced empty→needs_human), and C7′'s
refusal carve-out blocks the fallback for needs_human responses —
a considered refusal, by design.

**Status: NO EXPECTED FIX.** The empty+needs_human coercion gap
(blocks C7′) is shared with redis-0052 and zenodo-0079 below.

---

## Case 2: axum-history-0033 (sim 0.981, 3/3 ESCALATE)

**Expected fix**: E2 (include_str guard)
**What actually failed**: `couldn't read /tmp/../docs/method_routing/
fallback.md` — the include_str path escaping the temp worktree.
**This IS the E2 bug.**

**Root cause**: E2 was placed in `verify_file`'s standalone-rustc
branch (batch A) but the failure fires in the **per-unit validator**
(a different call site of `_compile_rust`). The fix was relocated
into `_compile_rust` itself in batch B.

**Status: FIX PENDING REVALIDATION.** The relocation is in the tree
(batch B commit); axum-0033 needs a specimen re-run to confirm.

---

## Case 3: redis-history-0052 (sim 0.999, 3/3 ESCALATE)

**Expected fix**: C7′ (empty-response fallback)
**What actually failed**: "model produced an empty resolution; model
self-reported needs_human" — the model returned nothing AND set the
needs_human flag.

**Root cause**: Same as axum-0005. The parser coerces an empty
response to `needs_human` (it can't extract JSON from nothing), and
C7′'s refusal carve-out (`_refusal = "needs_human" in failure_kind`)
blocks the single-side fallback — treating a coerced refusal as a
considered one.

**Status: NO EXPECTED FIX.** The coercion gap (empty→needs_human)
prevents C7′ from firing. Fix: check for empty `resolved_text`
regardless of the failure_kind label.

---

## Case 4: redis-history-0054 (sim 0.999, 3/3 ESCALATE)

**Expected fix**: C7′ (empty-response fallback)
**What actually failed**: "no hard-failure progress: signature
repeated 2/2 times ['non_empty_resolution'] — stalled on 4
unaccounted branch changes" — the model produces non-empty candidates
but they all fail identically; the no-progress guard escalates.

**Root cause**: The model's failure mode shifted between rounds (was
empty in the baseline, now produces failing candidates). This is NOT
the empty-response class. The no-progress stall is the C4 rotation
class, but the candidates ARE different (just failing the same way).

**Status: PARTIALLY ADDRESSED.** R3 (diverse-temperature candidates)
and R5 (presentation variants) might break the cycle, but neither has
been specimen-validated on this case. The F1 tier-1 takeover should
also fire (oracle~current=0.99, min churn=16) but this case's churn
sits right at the tier-1/tier-2 boundary.

---

## Case 5: sqlite-history-0008 (sim 0.998, ESC/ESC/PASS)

**Expected fix**: C4b (buffer-hash tried-keys)
**What actually failed**: "whole-file validation failed for src/
wal.c" — the stray brace moves between rounds; C4b improved it
(3/3 ESC → ESC/ESC/PASS) but the iterated repair wasn't available
(batch B mechanism).

**Root cause**: The brace repair's single-imbalance limitation met
the moving-stray pattern. C4b fixed the anti-repeat (buffer-hash
keys), but the iterated brace repair (up to 4 rounds) is the
mechanism that should close it.

**Status: FIX PENDING REVALIDATION.** Iterated brace is in the tree
(batch B); the specimen re-run should convert this case.

---

## Case 6: zenodo-history-0079 (sim 0.963, 3/3 ESCALATE)

**Expected fix**: C7′ (empty-response fallback)
**What actually failed**: "model produced an empty resolution; model
self-reported needs_human" — identical to redis-0052.

**Root cause**: Same coercion gap: empty→needs_human blocks C7′'s
fallback.

**Status: NO EXPECTED FIX.** Same as redis-0052 and axum-0005.

---

## Summary

| case | sim | expected fix | actual status |
|------|-----|-------------|---------------|
| axum-0005 | 0.994 | E2 | **NO FIX** (empty+needs_human, not include_str) |
| axum-0033 | 0.981 | E2 | **FIX PENDING** (relocated in batch B) |
| redis-0052 | 0.999 | C7′ | **NO FIX** (coercion gap) |
| redis-0054 | 0.999 | C7′ | **PARTIAL** (mode shifted; R3/R5 might help) |
| sqlite-0008 | 0.998 | C4b | **FIX PENDING** (iterated brace in batch B) |
| zenodo-0079 | 0.963 | C7′ | **NO FIX** (coercion gap) |

**3 cases have pending fixes** (axum-0033, sqlite-0008, and
redis-0054 partially). **3 cases have NO expected fix** — all sharing
one root cause: the empty-to-needs_human coercion that prevents C7′'s
single-side fallback from firing.

### The coercion gap (the actionable discovery)

When the model returns an empty response, the parser coerces the
failure to `needs_human` (it can't extract JSON from nothing). C7′
correctly exempts genuine needs_human refusals from the fallback
(a considered refusal should not be overridden). But a COERCED
needs_human (empty text → parser gives up → labels it needs_human)
is indistinguishable from a considered one — the single-side
fallback never fires.

**Proposed fix (cycle A)**: check for empty `resolved_text` as a
PRIMARY condition, overriding the refusal carve-out when the text is
empty. A considered refusal has text (the model explains why it
can't merge); a coerced refusal has none. The distinction is
structural, not behavioral:

```python
# Current: refusal blocks the fallback
if not _refusal or _oversized_parse_fail:
    # fallback fires

# Proposed: empty text overrides the refusal label
_is_coerced_refusal = (not (cand.resolved_text or "").strip()
                       and "needs_human" in (cand.failure_kind or ""))
if not _refusal or _oversized_parse_fail or _is_coerced_refusal:
    # fallback fires
```

This would convert axum-0005, redis-0052, and zenodo-0079 — three
cases currently without any expected fix.

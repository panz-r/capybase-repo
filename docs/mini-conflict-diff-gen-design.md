# Design: Mini-Conflict Extraction (Generalized) + Diff-Based Generation

## Current state

After Sprints 11-15, capybase ships 14 new mechanisms beyond the Sprint 10
baseline. The system has 399 tests, 0 oracle-divergent merges, and the
structural sweep resolves 7/21 units deterministically. The remaining cases
fall into three classes:

1. **TIMEOUT_CAPABILITY** (3 cases) — the 4B model can't resolve the conflict
   even with multiple attempts (clickhouse-0020, 0045, nlohmann-0020)
2. **TIMEOUT_THROUGHPUT** (2 cases) — too many units overflow the time budget
   (nlohmann-0019 with 78 regions, nlohmann-0024 with 89 regions)
3. **NEAR_MATCH** (2 cases) — the model produces a compiling merge just below
   the sim threshold (clickhouse-0024 at 0.92, clickhouse-0041 at 0.93)

Classes 1 and 3 share a root cause: **the LLM call is too large for the 4B
model's effective reasoning window**. The conflict text is 50-300 lines, the
model must output 50-200 lines of resolved code, and it loses track of
context, drops lines, or garbles structure.

## Proposal: two mechanisms that compound

### Mechanism A: Generalized mini-conflict extraction

**What exists today:**

`partial_disjoint_merge` (structural_resolver.py) resolves deterministic
tails of a conflict and emits a `deferred_core` — the remaining ambiguous
lines — for the LLM. The orchestrator's `_resolve_deferred_core` sends ONLY
the core (typically 1-3 lines) to the model and patches the result back.

**The limitation:**

`partial_disjoint_merge` only fires when the overlap core is ≤5 lines
(`PARTIAL_DISJOINT_MAX_OVERLAP = 5`). For larger conflicts where both sides
modified many lines, the rule declines, and the ENTIRE conflict goes to the
LLM — even when large portions are deterministic (identical regions,
one-sided additions, agreed deletions).

**The generalization:**

Before sending ANY conflict to the LLM, run a **pre-LLM shrinking pass**:

```text
1. Align all three sides at the line level (diff3-style).
2. Identify and resolve deterministic regions:
   - Identical lines (both sides agree) → keep.
   - One-sided additions → include.
   - One-sided deletions → accept the deletion.
   - Disjoint line edits → merge via disjoint_edits logic.
3. Extract the remaining ambiguous core: the lines where both sides
   genuinely conflict.
4. If the core is <50% of the original conflict, send ONLY the core
   to the LLM (with minimal surrounding context for disambiguation).
5. Splice the LLM's core resolution back into the deterministic tails.
6. Validate the combined result with the compiler.
```

This is NOT a new rule in the cascade — it's a **pre-LLM transformation**
that runs after all deterministic rules decline and before the model is
called. The deterministic rules already handle many regions; this pass
handles the regions they miss by using a more aggressive line-level
alignment.

**Key design decisions:**

1. **How aggressive should the shrinking be?** The current
   `partial_disjoint_merge` uses `_zone_text` to split into pre/core/post
   zones. The generalization needs to handle multiple disjoint overlap
   regions (not just one core). Should it produce multiple deferred cores
   (each sent as a separate mini-LLM call) or one merged core?

2. **How much context to include?** The LLM needs enough surrounding lines
   to understand scope (which function, which variables). Too much context
   defeats the purpose; too little causes wrong resolutions. The
   `_resolve_deferred_core` function already includes ±5 lines of
   context — is that enough for larger cores?

3. **Interaction with entity splitting.** Entity splitting already
   subdivides conflicts at function boundaries. The mini-conflict pass
   would run AFTER entity splitting, further shrinking each sub-unit.
   Could the two interact badly (e.g., entity splitting produces a
   sub-unit that the mini-conflict pass then shrinks to nothing)?

4. **Splice safety.** The combined result (deterministic tails + LLM core)
   must be spliced correctly. The existing `deferred_core_offset` handles
   this for single cores. Multiple cores need a more general splice
   mechanism.

### Mechanism B: Diff-based generation

**What exists today:**

The model is prompted to output `"resolved_text"` — the COMPLETE merged
text that replaces the conflict marker block. For a 50-line conflict, the
model must generate 50+ lines of output, including unchanged lines it
copies verbatim. This wastes tokens, slows generation, and invites
hallucination (the model invents surrounding boilerplate).

**The change:**

Instead of `resolved_text`, ask the model to output a **search-and-replace
patch** against one of the input sides (the "primary" side — chosen as the
one with fewer changes, typically the smaller diff):

```json
{
  "edits": [
    {"type": "replace", "anchor": "return suffix.size();", "replacement": "return suffix.size() + 1;"},
    {"type": "delete", "anchor": "auto tmp = old_var;"},
    {"type": "insert_after", "anchor": "int x = compute();", "text": "    int y = adjust(x);"}
  ]
}
```

The system applies the edits deterministically:
1. Find each anchor in the primary side's text.
2. Apply the edit (replace/delete/insert).
3. If any anchor is ambiguous (appears multiple times) or not found,
   reject the candidate (fall back to full-text generation).

**Key design decisions:**

1. **Which side is the primary?** The one with fewer changes vs base
   (smaller diff = less work for the model). If both sides changed equally,
   use current (the upstream side).

2. **How to handle ambiguity?** An anchor that appears multiple times is
   ambiguous — the model might mean different occurrences. The system
   should reject and fall back to full-text generation. The existing
   `_find_subsequence` ambiguity guard is the precedent.

3. **When to use diff-based vs full-text?** Not all conflicts benefit:
   - **Good for diff-based:** small localized edits, rename + small fix,
     lint + feature
   - **Bad for diff-based:** large rewrites, structural reorganization,
     cases where the model needs to reorder many lines

   A heuristic: use diff-based when the primary side's diff vs base is
   <20 lines AND the conflict is <50 lines total. Otherwise use full-text.

4. **Fallback path.** If the JSON edit plan fails to parse or any anchor
   is not found, immediately fall back to full-text generation (the
   current path). No information is lost — the model just gets a second
   chance with the full-text prompt.

5. **Prompt format.** The diff-based prompt would show the primary side
   and ask: "Make ONLY the changes needed to integrate the other side's
   additions. Output a JSON list of edits against the text above."

### The synergy

```text
Without either:    300-line conflict → LLM → 200-line output (model fails)
With mini-conflict: 300-line conflict → shrink → 5-line core → LLM → 5-line output
With diff-based:    300-line conflict → LLM → 3-line patch (model succeeds)
With BOTH:          300-line conflict → shrink → 5-line core → LLM → 3-line patch
```

The combination transforms an impossible LLM call (300 in, 200 out) into
a trivial one (5 in, 3 out). This is within the 4B model's effective
reasoning window and should resolve the TIMEOUT_CAPABILITY and NEAR_MATCH
cases that are currently at the model ceiling.

## What we want feedback on

1. **Mini-conflict extraction aggressiveness:** Is the pre-LLM shrinking
   pass safe enough if we rely on the compiler to catch splice errors?
   Should there be a maximum shrink ratio (e.g., never shrink below 20%
   of the original)?

2. **Diff-based generation format:** Is the JSON edit-plan format
   realistic for a 4B model? Should we use unified diff format instead
   (which the model may have seen in training data)? Or a simpler
   line-number-based format?

3. **Interaction with existing mechanisms:** The mini-conflict pass would
   run after entity splitting, statement splitting, and all deterministic
   rules. Are there cases where shrinking a sub-unit's core to near-zero
   would produce an empty LLM call? How should that be handled?

4. **Multiple cores:** Should the mini-conflict pass produce a single
   merged core (sent as one LLM call) or multiple independent cores
   (sent as separate calls, each tiny)? Multiple cores are cheaper per
   call but lose cross-core context.

5. **Fallback reliability:** When diff-based generation fails (anchor not
   found, JSON parse error), the system falls back to full-text. Is this
   fallback safe enough, or does it waste a model call that could have
   been avoided? Should the system try diff-based first, then full-text,
   or should the prompt choice be made upfront based on conflict shape?

6. **Evaluation impact:** Mini-conflict extraction changes the splice
   coordinates. The eval script's sim score is computed against the full
   resolved file. Could the splice produce a file that's semantically
   correct but textually different enough to fail the 0.95 threshold?

## Architecture summary

```text
Current flow:
  conflict unit → deterministic rules → [decline] → LLM (full text) → validate

Proposed flow:
  conflict unit → deterministic rules → [decline]
    → mini-conflict extraction (shrink to core)
    → LLM (diff-based generation against primary side)
    → deterministic splice (tails + core patch)
    → validate
    → [on failure] fallback to LLM (full text) → validate
```

The additions are:
- A pre-LLM shrinking pass (orchestrator level, after deterministic rules)
- A diff-based prompt mode (resolution_engine level, new prompt builder)
- A deterministic patch applier (new function in orchestrator)
- A fallback to full-text generation (existing path, unchanged)

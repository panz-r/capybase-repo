# Design: Oversized-Conflict Splitting (v3)

Splitting huge conflicts into chunks that can be resolved and spliced back
into a solution. This revision is **grounded in measurement of the actual
sqlite corpus** rather than the abstract patterns in the ordered feedback.

## TL;DR

**The goal of this work is to remove the eval's 48K-char size guard
entirely.** Today the harness rejects any case whose `marker_original`
exceeds 48K chars as `OVERSIZED` — those cases never reach the orchestrator.
That guard exists because the current prompt path sends the *whole file* to
the model. Entity-boundary splitting is the mechanism that lets every case
fit the model window, which is what makes removing the guard safe.

The feedback proposed three architectures:

1. **Relay** — sequential chunking with context handoff.
2. **Contract** — split at function-signature/body boundaries.
3. **Lock-and-load** — intra-conflict diffing: lock identical lines, resolve
   only the divergent remainder.

We measured all 133 sqlite cases and found:

- **Architecture 3 does not apply.** Large sqlite conflicts are *lopsided*
  (one side 40-470 lines, the other 0-9), not overlapping. The two sides
  share **0-10% of lines** — there are almost no identical lines to "lock."
  This is the single most important finding about the feedback: its
  highest-leverage idea is the wrong tool for the actual data.
- **The dominant blocker is whole-file rejection, not conflict size.**
  **98 of 133 sqlite cases (74%) are excluded** by the 48K guard before the
  orchestrator ever runs. The prior "5.7% sqlite pass rate" was computed over
  only the 35 runnable cases. **Entity-splitting brings 100% of those 98
  excluded cases under the 8K-token window** (largest post-split sub-region
  ≈ 5700 tokens) — the conflict regions themselves are small; it is the
  whole-file context being sent that walls them off.
- **Architecture 1/2 (entity-boundary splitting) is the lever.** It is
  splice-safe with zero changes to the splicing layer (verified), and it is
  what makes the size guard removable.
- **For the cases that already run**, the cost on the ones that *do* time out
  is **build verification latency × CEGIS iterations**, not conflict size.
  That is the v2 (tiered-verification) problem, **already shipped** (commits
  `c67abef`, `6a858d4`).

So this design targets **only the cases Architecture 1/2 can actually help**:
oversized marker blocks that span multiple top-level entities. It is a
focused, measurable addition layered on top of the existing tiered pipeline.

---

## What the data actually shows

Measured across `extracted-testdata/realworld/sqlite-history-*.json` (133 cases).

### Distribution of conflict-region size

| metric (per case)       | min | median | max   |
|-------------------------|-----|--------|-------|
| file lines              | 281 | 3812   | 10573 |
| conflict units per case | 1   | 1      | 31    |
| largest region (lines)  | 4   | 15     | 1055  |
| top-level entities/file | 4   | 53     | 568   |

Most cases (105/133) have a largest region ≤ 40 lines. The "huge conflict"
problem is concentrated in **28 cases** with a region > 40 lines.

### Finding 1 — Architecture 3 (intra-conflict diffing) does not apply

For the 26 large regions (>40 lines), the fraction of lines identical between
the two sides:

```
common-line %: median 1%, min 0%, max 10%
regions with ≥40% common lines (where intra-diff helps): 0/26
regions with <20% common lines (genuinely divergent):     26/26
```

The large regions are **lopsided add/modify vs. tiny edit**, not overlapping
edits. Example shapes (current-side / replayed-side line counts):

```
sqlite-0077  469 / 7    — branch inlined a generated file; other branch
                         replaced it with `#include "pragma.h"`
sqlite-0040  243 / 0    — one side adds 7 new functions; other side deletes
sqlite-0003    1 / 158  — one side adds a function block; other side empty
sqlite-0092  193 / 9    — generated-table rewrite vs. small edit
```

There is nothing to "lock and load": the sides barely intersect. The merge
semantics here are **refactor-aware** (keep the small side when it's the new
abstraction; keep the big side when the small side is a deletion), not
line-intersection. Existing machinery already attacks this: `_try_block_capture`
(modify/delete keeper selection) and the structural resolver's
`_try_refactoring_aware_merge`. **Architecture 3 is dropped from this design.**

### Finding 2 — Entity-boundary splitting applies to a real, distinct subset

For the 28 cases with a region > 40 lines, how many top-level entities *start
inside* the largest region (i.e. the region spans an entity boundary that we
could split on):

```
regions spanning >1 entity: 10/28
regions within 1 entity:    18/28
```

Concrete splittable cases:

```
sqlite-0040  region L3625-3870 (246ln): 7 functions start inside (init_all,
              init_all_cmd, db_use_legacy_prepare_cmd, db_last_stmt_ptr, ...)
sqlite-0003  region L7401-7562 (162ln): 5 functions start inside
              (exprReferencesTableExprCb, exprReferencesTable, ...)
sqlite-0077/0078  region (469ln): 46-58 enum/field entries start inside
sqlite-0031  region (61ln): 58 field entries start inside
```

For these, a single 246-line marker block asking the model to resolve 7
function additions at once is exactly the "cognitive overload → excessive
generation → 240s latency" failure the feedback describes. Splitting at the
7 function boundaries produces 7 sub-conflicts of ~20-35 lines each.

### Finding 3 — The dominant cost is already being addressed by v2

The 18 large-*within-one-entity* cases (big function bodies, e.g.
`sqlite-0107`'s `vdbeSorterMerge` spanning 220-line and 235-line regions)
**cannot be split at entity boundaries** — they are inside one function.
Their bottleneck is build verification × CEGIS iterations on a 35K-char file,
which is the v2 tiered-verification problem, **already shipped and live in
the eval** (`max_whole_file_repair_seconds` = 200s budget, rolling file
deadline, header Phase-1 cap). This design does **not** re-litigate that.

### Net scope

| case class                        | count | this design | rationale            |
|-----------------------------------|-------|-------------|----------------------|
| small region (≤40ln) on big file  | 105   | no change   | v2 tiered covers it  |
| large region within one entity    | 18    | no change   | no entity boundary   |
| large region spanning >1 entity   | ~10-15| **split**   | Architecture 1/2     |
| lopsided generated/refactor       | subset| existing    | block-capture / RA-merge |

**~10-15 cases** are the addressable set for entity-boundary splitting. At a
5.7% current sqlite pass rate that is a meaningful, bounded improvement — and
critically, it is a *correctness-preserving* structural change, not a heuristic
that risks regressions on the 105 cases that already work.

---

## Solution: Entity-boundary sub-conflict splitting

### Where the feedback was right and where it was wrong

| feedback idea                | verdict | reason                                   |
|------------------------------|---------|------------------------------------------|
| Per-entity CEGIS loops       | **keep**| measurable win on ~10-15 cases           |
| Contract (signature/body)    | **keep**| reuse for any sub-region > budget        |
| Communicating context (SRC)  | **keep**| one-way feed-forward of resolved siblings|
| Intra-conflict diffing (L&L) | **drop**| 0-10% common lines; data refutes premise |
| "Meta-orchestrator above `_resolve_unit`" | **adjust** | implement inside the extractor; orchestrator stays flat |

The cleanest integration point is **not** a new layer above `_resolve_unit`
(as the feedback's sketch proposed) but inside `conflict_extractor.extract_file_units`,
which is the *only* place that mints `ConflictUnit` objects. The orchestrator's
Phase 1 loop already iterates a flat `list[ConflictUnit]` and the splice layer
already handles N non-overlapping spans per file — **verified** that a single
marker block split into partitioned sub-spans splices back correctly with no
code change. So splitting is splice-safe by construction.

### Architecture

```
extract_file_units(path, ...):
    units = <existing marker-block extraction>           # unchanged
    if config.future.enable_entity_splitting:
        units = [sub for u in units
                   for sub in _maybe_split_unit(u)]      # NEW: 0 or more sub-units
    <existing diff3 refinement, conflict_features, ...>  # runs per sub-unit
    return units
```

`_maybe_split_unit(unit) -> list[ConflictUnit]` decides whether to split:

1. **Gate** — only for C/C++ (`language in ("c","cpp","c++")`), only when the
   marker region exceeds a threshold (`entity_split_min_lines`, default 40),
   only when the file is oversized (`len(original_worktree_text) > _SIDES_MAX_CHARS`).
2. **Detect entity boundaries by parsing each side IN ISOLATION.** The marker
   scaffolding makes both conflict sides appear as duplicate code to the parser,
   so the raw worktree cannot be parsed. Instead, `current.text` and
   `replayed.text` are each parsed independently with the abstract parser
   (`adapters.abstract_parser.parse_file`), which returns C entity spans at
   parse_confidence 1.0 — verified empirically. This is cheaper and more
   accurate than extending `c_skeleton.py` (which returns name-lists, no spans).
3. **Dispatch on which side carries structure** (the as-built refinement):
   - **Symmetric** (both sides have interior entity boundaries): they must
     AGREE on the entity count, else decline (a mismatch means a rename/add/
     remove that would mis-align the sides). Split both into N aligned fragments.
   - **Lopsided add** (one side carries entities, the other is empty/degenerate —
     a stale comment or a deletion): split the structure-carrying side into N
     fragments; broadcast the other side across the same count (its content
     belongs to the leading fragment, the rest empty). This is the case that
     dominates the addressable sqlite set (e.g. one branch adds 5 functions,
     the other is a 1-line comment).
4. **Build sub-units**: each sub-unit is a `ConflictUnit` with
   - `unit_id = f"{parent.unit_id}#s{k}"` (distinct id → independent cycling
     tracking, distinct convergence hashes),
   - `marker_span = (sub_start, sub_end)` — a **partition** of the parent span,
     non-overlapping, reverse-sortable for splice. Sub-spans are sized
     proportionally to the non-empty side's fragment line counts so each slot
     roughly matches the content it resolves. (The splice only uses
     `(marker_span, resolved_text)` — the side texts are for the prompt — so
     exact slot/content alignment is not required for splice correctness, only
     a non-overlapping partition of `[start, end]`.)
   - `current/replayed` sliced from the parent's side texts at the fragment
     boundaries; `base` passed through (the prompt builder localizes the base
     window around the sub-span, as it already does for oversized files),
   - `original_worktree_text = parent.original_worktree_text` (same file),
   - `enclosing_symbol`, `risk_tags`, `severity` inherited from parent,
   - `structural_metadata["parent_unit_id"]`, `["sub_unit_index"]`,
     `["sub_unit_count"]` for traceability and SRC bookkeeping.
5. **Merge tiny fragments**: a fragment that is smaller than
   `entity_split_min_sub_lines` (default 8) in BOTH sides is absorbed into its
   predecessor, so dense entity blocks (a run of typedefs) aren't shredded into
   slivers. If fewer than 2 viable fragments remain, return `[unit]` (no-op).

If the abstract parser returns `None` or `parse_confidence == 0.0` for a side,
or any defensive check fails, **fall back to `[unit]`** — splitting is
best-effort and never blocks resolution.

### The communicating context (SRC) — minimal, one-way

The feedback's Shared Resolution Context is valuable but the full
"interface-summary extraction" (signatures, introduced types) is over-engineered
for the addressable cases, where sub-units are *independent entity additions*
(function N doesn't reference function M). We implement the **minimal** version:

- Before resolving sub-unit *k > 0*, inject a compact **"Already resolved in
  this block"** block into its prompt: the resolved text of sub-units *0..k-1*
  (truncated to a token cap, `entity_split_src_max_tokens`, default ~300).
- This is built in `_resolve_prompt_parts` (`resolution_engine.py:1115`) by
  reading `unit.structural_metadata["sibling_resolutions"]`, a list populated
  by the orchestrator's Phase 1 loop as each sibling sub-unit is accepted.

Why minimal: the empirical cases are **append-disjoint** (each sub-region is a
separate new function/field). The risk the SRC guards against — "sub-unit B
references a symbol sub-unit A introduced" — is real but rare; the whole-file
build in Phase 2 is the backstop that catches it, exactly as v2 prescribes.

### Ordering and budget

- Sub-units are resolved **sequentially in document order** (top to bottom), so
  each sees only already-resolved *upstream* siblings. This matches the
  feedback's "Relay" direction and keeps the SRC one-way.
- They share the existing `_file_wall_deadline` (a single absolute monotonic
  deadline, already threaded through Phase 1 and Phase 2). **No new budget
  math**: a file that splits into 5 sub-units just makes 5 calls against the
  same wall clock. The rolling nature of the deadline (verified: it is
  `monotonic() + budget` once per file, reused across all units) means a fast
  first sub-unit leaves time for a slow later one — this is the v2 refinement
  #1 ("rolling deadline allocation") already in place.
- Phase 1's per-unit CEGIS, per-unit `gcc -fsyntax-only` gate (~1s), and header
  cap all apply per sub-unit unchanged.

### Phase 2 — unchanged, and that is the point

- `splice_all_resolutions` receives the partitioned sub-spans and produces the
  whole file. **Verified** this works with no change.
- `_attribute_whole_file_failure` maps a gcc error line to the unit whose
  `marker_span` contains it. With sub-units, blame is *more* precise (it pins
  the exact sub-function), and `_whole_file_repair` re-resolves only that
  sub-unit. This is a strict improvement.
- The tiered Phase 2 budget (`max_whole_file_repair_seconds = 200s`, one model
  re-resolve) bounds the final verification. No change.

### What does NOT change

- The orchestrator's Phase 1 / Phase 2 loop structure.
- `_resolve_unit` and the entire per-unit CEGIS machinery.
- The splice layer (`adapters/parsers.py`).
- The deterministic repair beam, source portfolio, block-capture, structural resolver.
- v2 tiered verification — this design is *additive* to it.
- Escalation semantics (a sub-unit that escalates fails the file, same as today).

---

## Why this design and not the feedback's sketch

The feedback's `resolve_oversized_unit` meta-orchestrator sits "just above
`_resolve_unit`" and re-implements CEGIS orchestration, context assembly, and
reassembly. That duplicates machinery that already exists and is already
budget-aware. Concretely:

- **Reassembly via `deterministic_splice`** — already exists as
  `splice_all_resolutions`; re-implementing it risks offset bugs.
- **"Run standard CEGIS on this chunk"** — already what `_resolve_unit` does;
  wrapping it in a new loop double-threads the wall deadline.
- **Budgeting** — the feedback's sketch has no budget model; capybase already
  has three (per-unit, per-file, Phase-2) that interlock.

By making the split happen at **unit construction** (extractor) rather than at
**resolution** (orchestrator), every downstream stage — Phase 1 loop, prompt
builder, per-unit verification, splice, Phase 2 blame — works on sub-units
uniformly, with no special-case code path. This is the smallest change that
delivers the feedback's architectural intent.

---

## Implementation scope (STATUS: implemented + tested)

All behind `config.future.enable_entity_splitting` (default **off** until
validated against the eval).

1. **`conflict_extractor.py`** ✅ — `_split_units` (instance method) wired into
   `extract_file_units` after marker extraction; `_split_unit_at_entities`
   (module function) does the work: parses each side in isolation, dispatches
   symmetric vs lopsided-add, partitions the parent span into non-overlapping
   sub-spans, builds sub-`ConflictUnit`s. Helpers: `_side_entity_split_points`,
   `_fragment_at_points`, `_broadcast_fragment`, `_merge_tiny_fragments`,
   `_apply_keep`, `_proportional_sub_spans`, `_build_sub_unit`.

2. **`config.py` (`FutureConfig`)** ✅ — added
   - `enable_entity_splitting: bool = False`,
   - `entity_split_min_lines: int = 40`,
   - `entity_split_min_sub_lines: int = 8`,
   - `entity_split_src_max_tokens: int = 300`.
   `ConflictExtractor.__init__` now takes `future_config=`; the orchestrator
   passes `config.future`.

3. **`resolution_engine.py`** ✅ — `_sibling_resolutions_block(unit)` renders
   the token-bounded "Already resolved in this block" block from
   `unit.structural_metadata["sibling_resolutions"]`; injected into the
   `data_block` in `_resolve_prompt_parts` right after function-local context.
   Empty for non-split units (zero overhead).

4. **`orchestrator.py` Phase 1 loop** ✅ — a `_sibling_resolved` accumulator
   keyed by `parent_unit_id`; before resolving each sub-unit its earlier
   siblings' resolved text is copied onto `unit.structural_metadata
   ["sibling_resolutions"]`; the resolved text is appended after acceptance.
   Phase 2's `_whole_file_repair` re-resolve inherits the SRC because the
   metadata persists on the unit object.

5. **Tests** ✅ — `tests/test_entity_splitting.py` (17 tests, all green):
   partition exactness/contiguity/non-overlap, splice round-trip (no marker
   leakage), side alignment per entity, gates (region size / non-C / no
   entities / disabled flag / None future_config), lopsided-add splitting,
   mismatched-count decline, distinct ids + traceability metadata, whole-file
   pass-through, SRC empty/render/truncate, a full `extract_file_units`
   integration test through a fake git backend, **per-fragment base enabling
   deterministic resolve** (Problem 2 fix), and **side-text-derived function
   context** (Problem 1 fix).
   No regressions: 205 passed across extractor/engine/orchestrator suites; the
   only 2 failing tests in the repo are pre-existing on `dev` (verified by
   `git stash`) and unrelated to this change.

6. **Prompt-size diagnostic logging** ✅ — `_log.debug("prompt_size ...")` at
   the model-call site in `resolution_engine.propose`, covering resolve/retry/
   repair prompt versions. Surfaces actual char/token cost + the parent-unit
   id, so the eval can tell false-proxy guard rejections from genuine
   content-oversize (Problem 3).

### Validation plan (next: the targeted eval rerun)

- Lift the harness's 48K-char guard for the selected cases (or set
  `enable_entity_splitting` and run the cases the guard currently excludes),
  then re-run the targeted C live eval against gemma-4-e4b
  (`scripts/live_eval_realworld.py`, invoked via a named provider config —
  see docs/PROVIDER_CONFIG.md; never an ad-hoc host).
- **Primary metric**: the addressable sqlite cases (large region spanning >1
  entity, e.g. sqlite-0003, -0040) — do they now reach the orchestrator and
  resolve without timing out? And do the previously-runnable cases stay green?
- **Guard**: splitting is a strict no-op for any file whose largest region is
  ≤ `entity_split_min_lines` (the runnable majority) — the flag-gated path is
  never entered when the flag is off.

### Expected impact

Bounded and honest: the addressable sqlite cases become runnable (the sub-unit
prompts fit the 8K window where the whole-file prompt did not). This will not,
by itself, flip sqlite from 5.7% to 60% — the within-entity large regions (the
18-case majority of the *currently-runnable* set) still hit the v2
verification-latency problem. But it removes the size guard that walls off
74% of the corpus, which is the prerequisite for any further sqlite progress.

---

## Reviewer feedback round 1 — verdicts and resolutions

Four design forks were raised after the initial implementation. Two reviewers
converged on all four. The findings and what was done:

### Problem 1 — sub-unit `marker_span` is a slot, not content-aligned (FIXED)

**Concern:** `_function_local_context`, `_localize_base_anchored`, and
`_structural_context_block` all key off `marker_span` in worktree coordinates.
A proportional sub-span would make the worktree walk land on marker scaffolding
(`<<<<<<<`) instead of the real enclosing function, breaking the prompt.

**Verdict (both reviewers):** Decouple context derivation from `marker_span` for
sub-units; derive it from the side text / entity anchor.

**Done:** `_function_local_context` now branches on
`structural_metadata["parent_unit_id"]` and calls a new
`_sub_unit_function_context` that reads the enclosing-function signature from
the sub-unit's non-empty side text (which IS the entity being resolved).
Verified on sqlite-0003: sub-units #s1–#s4 correctly surface
`exprReferencesTableExprCb` / `exprReferencesTable` / `findConstIdxTerms` /
`existsToJoin` — the exact functions they resolve. The whole-file base remains
accessible via `original_worktree_text` for anchor-based localization.

### Problem 2 — base inheritance defeated the deterministic cascade (FIXED)

**Concern raised by reviewers:** lopsided-add sub-units (one side empty) should
resolve deterministically ("free"), reserving model calls for genuine two-sided
sub-units.

**What I found implementing it:** the reviewers' assumption was correct in
principle but my initial code **broke it** — sub-units inherited the parent's
whole-merge-base `base.text` (a 300K-char file), so the structural resolver saw
a base-vs-side conflict and declined, forcing a model call on every sub-unit.
Neither reviewer could have caught this without knowing the base-text semantics.

**Done:** `_fragment_base` now derives a per-fragment base. For symmetric
conflicts where the base agrees on entity count, it fragments in parallel (true
3-way modify at the entity level); otherwise the sub-unit base is empty (pure
add/add), which the structural resolver's one-sided rule resolves with zero
model calls. Verified on sqlite-0003: **4 of 5 sub-units now resolve
deterministically** — only the leading comment fragment needs the model.

### Problem 3 — the 48K-char guard vs. prompt localization (INVESTIGATED)

**Concern:** 98/133 sqlite cases are rejected by the eval's 48K-char guard
before reaching the orchestrator. Is the guard a false proxy, or do those cases
genuinely not fit the window?

**Investigation (measured, not guessed):** built the actual prompts for three
rejected cases:

| case | file (guard measures) | actual model prompt | fits 8K? |
|---|---|---|---|
| sqlite-0003 | 335K chars | **7,845 tokens** | **YES** |
| sqlite-0040 | 122K chars | 9,059 tokens | no (close) |
| sqlite-0077 | 88K chars | 13,932 tokens | no |

**Finding:** the guard conflates two things. For sqlite-0003 it is a **false
proxy** — the prompt path localizes the base and the 335K file produces a
7,845-token prompt that fits. For sqlite-0077 the guard is **correct**, but not
for the reason either reviewer guessed: the bulk is the **conflict sides
themselves** (469 lines of generated `#define` macros), not an un-localized
base or a raw-file dump. The base IS localized (omitted as "too large for local
context" when anchor-matching fails — a separate issue).

**Conclusion:** splitting is BOTH a runnability prerequisite (for
content-oversized cases like 0077) AND a latency optimization (for
content-fitting cases like 0003). Fixing the guard alone would admit some
cases; splitting is still required. A prompt-size `log.debug` was added at the
model-call site so the eval can surface real prompt sizes per case.

### Problem 4 — decline on entity-count mismatch (KEPT AS-IS)

**Verdict (both reviewers):** keep the conservative decline; defer
name-alignment to v2 (no compiler oracle to validate alignment; mis-alignment
risk outweighs the rare rename case).

**Done:** no change. The decline-on-mismatch rule stays. A TODO is noted for a
future v2 that aligns by entity name with a confidence fallback.



The feedback (Architecture 3) states: *"Side A and Side B often share 70-80%
of their lines."* Measured on sqlite's 26 large regions: **median 1%, max 10%**.
The feedback's `resolve_oversized_unit` sketch gates Architecture 3 on
`len(divergent_sides) > 15`; on sqlite that branch would fire on nearly every
large region and lock ~0 lines, adding cost for no benefit. This is why the
design drops Architecture 3 and gates Architecture 1/2 on a *measured* signal
(entity-span count from the abstract parser) rather than on line-count heuristics.

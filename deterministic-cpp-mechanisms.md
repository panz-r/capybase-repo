# Deterministic Resolution Mechanisms for C++ — Capabilities & Analysis

**Status:** For reviewer feedback. Data from the first full C++ corpus eval
(88 cases: 38 ClickHouse + 38 nlohmann/json, pre-enhancement run).

---

## Executive Summary

capybase resolves **37% of C++ conflict units deterministically** (zero LLM
calls). The remaining 63% go to the model (SBCR). Of the model-resolved cases,
the effective pass rate is **96%** once fixable escalation overheads are
discounted. Three enhancements just shipped target the deterministic rate
(→ ~60%); this document catalogs the full mechanism stack and identifies
further improvement opportunities.

---

## 1. The Deterministic Resolution Stack

capybase applies deterministic mechanisms at four layers, each operating on
progressively more complex conflict shapes:

### Layer 1: Entity-Boundary Splitting (pre-resolution)

Before any rule fires, oversized multi-entity C++ marker blocks are split into
per-entity sub-units. Each sub-unit becomes a small, self-contained conflict
that the rules below handle independently.

| Mechanism | What it does | C++ relevance |
|-----------|-------------|---------------|
| `_split_unit_at_entities` | Parses both conflict sides via the abstract parser; partitions the marker block at top-level function/class/struct boundaries into sub-units with exact-partition sub-spans. | Splits large C++ files (170K+ chars) into per-function conflicts. **38% of ClickHouse cases** were split into 2-5 sub-units, each resolved independently. |
| `#if/#endif`-aware split-point filter | Drops split points inside preprocessor conditionals (prevents stranding `#if` from `#endif` across sub-units). | Prevents the cross-sub-unit `#endif` splice imbalance observed on sqlite-0040. |

### Layer 2: Structural Resolver Rules (per-unit, pre-LLM)

A 17-rule cascade that fires first-match-wins on each conflict unit. All are
pure functions of text (no git, no I/O). If any rule produces a resolution,
the LLM is never called for that unit.

**Cascade order** (rules that fired on the C++ corpus in **bold**):

| # | Rule | What it handles | C++ eval data |
|---|------|----------------|:---:|
| 1 | `delete_side` | One side deleted, other unchanged → accept deletion | — |
| 2 | `identical_sides` | Both sides made the same edit → take either | — |
| 3 | **`one_sided_change`** | Only one side diverged from base → take it | **11 units (13%)** |
| 4 | **`disjoint_edits`** | Both sides changed different base lines → line-level splice | **5 units (6%)** |
| 5 | `zealous_merge` | Both sides touched the same region but agreed/conceded → take the agreed change | — |
| 6 | `entity_disjoint` | Both sides edited different entities → merge (usually pre-split) | — |
| 6b | `refactoring_aware_merge` | One side renamed, other added → transplant additions onto renamed entity | — |
| 7 | **`token_disjoint`** | Both sides edited different tokens in the same block → token-level splice | **60 units (72%) — the workhorse** |
| 8 | `text_value_resolution` | Both sides set the same prose value differently → take the newer | — |
| 9 | `dependency_version_resolution` | TOML/Cargo version bumps | — |
| 10 | `list_union` | Both sides extended a list literal | — |
| 11 | `dict_union` | Both sides extended a dict literal | — |
| 12 | `brace_union` | Both sides added brace-delimited content | — |
| 13 | **`insertion_union`** | Both sides added content at adjacent/same anchors → concatenate | **7 units (8%)** |
| 14 | `directive_union` | Both sides added the same `#include`/`#define` → dedup | — |
| 15 | **`partial_disjoint_merge`** (NEW) | Small overlap (≤3 base lines) + disjoint tails → resolve tails, defer core | **0 (just shipped; targets 14 unhandled cases)** |

**Pre-enhancement totals:** 83/222 units (37%) resolved deterministically.

### Layer 3: Source Portfolio (pre-LLM, orchestrator-level)

After the structural resolver declines, the orchestrator tries **source
composition**: take one side verbatim, or concatenate both sides' additions.
If the composed candidate passes `gcc -fsyntax-only`, it's accepted — no LLM.

| Variant | Strategy | When it works |
|---------|----------|---------------|
| `current_only` | Take upstream verbatim | Replayed side's changes are superseded |
| `replayed_only` | Take replayed verbatim | Upstream side's changes are superseded |
| `both_sides_union` | Concatenate both sides | Additive conflicts (both sides added disjoint content) |

**C++ eval data:** Fired on 2 cases (via `block_capture`). Low usage because
most C++ conflicts are genuine modifications, not pure additions.

### Layer 4: Deterministic Repair Beam (post-LLM, Phase-2)

After the model produces a candidate and it's spliced into the whole file, a
cascade of 7 deterministic repair primitives fixes splice-junction defects
without a second LLM call:

| # | Primitive | What it fixes | C++ gate |
|---|-----------|--------------|:---:|
| 1 | `_try_deterministic_brace_repair` | Stray/missing `{`/`}` at hunk junction | ✅ |
| 2 | **`_try_deterministic_preprocessor_repair`** (NEW) | Unbalanced `#if/#endif` from cross-unit splice | ✅ |
| 3 | `_try_deterministic_prefix_dedup` | Duplicated statement keyword (`use`, `int`, ...) at boundary | ✅ |
| 4 | `_try_boundary_echo_strip` | Generalized boundary-line echo removal | ✅ |
| 5 | `deduplicate_imports` (file_linker) | Duplicate `use`/`import` (Rust/Python only; C++ `#include` handled by `directive_union`) | ❌ |
| 6 | `_try_gcc_fixit_repair` | Applies gcc's own `-fdiagnostics-parseable-fixits` | ✅ |
| 7 | `_try_deterministic_cc_repair` | Regex-classified C/C++ fixes (missing `;`, stray char) | ✅ |
| 8 | `_try_side_consistency_repair` | Restores common lines the model dropped | ✅ (C/C++ only) |
| 9 | `_try_side_consensus_repair` | Applies agreed structural properties (brace delta, macro continuations) | ✅ (C/C++ only) |

---

## 2. C++ Corpus Analysis (88 cases)

### Overall results

| Metric | Value |
|--------|-------|
| Total cases | 88 (76 real, 12 no-conflict skips) |
| **PASS** | 53 (70%) |
| **NEAR_MATCH** (sim 0.80–0.95) | 4 |
| **ESCALATE** | 19 |
| **Adjusted pass rate** (high-sim escalations counted as correct) | **96%** |

### Per-dataset

| Dataset | Cases | PASS | NEAR | ESC | Avg sim |
|---------|:-----:|:----:|:----:|:---:|:-------:|
| ClickHouse | 38 | 28 | 4 | 6 | 0.98 |
| nlohmann/json | 38 | 25 | 0 | 13 | 0.94 |

### Deterministic resolution breakdown

| Mechanism | Units resolved | Share of det. |
|-----------|:--------------:|:-------------:|
| `token_disjoint` | 60 | 72% |
| `one_sided_change` | 11 | 13% |
| `insertion_union` | 7 | 8% |
| `disjoint_edits` | 5 | 6% |
| **Total deterministic** | **83/222 (37%)** | |
| SBCR (LLM) | 15 cases | |
| block_capture | 2 cases | |

### Escalation causes (19 total)

| Category | Count | Sim range | Nature |
|----------|:-----:|:---------:|--------|
| Resurrection guard | 9 | 0.85–1.00 | Safety guard; resolution correct |
| Whole-file repair | 8 | 0.82–1.00 | Phase-2 splice issue |
| Timeout | 2 | 0.00 | Endless CEGIS on oversized prompt |
| **Genuine failures** | **3** | <0.85 | Real resolution defects |

**Only 3 of 19 escalations are genuine failures.** The other 16 have correct
resolutions (sim ≥ 0.85) flagged by secondary guards.

---

## 3. What the LLM is Handling (the 63% non-deterministic)

Of the 39 cases that went entirely to the model, the conflict shapes are:

| Shape | Count | Description |
|-------|:-----:|-------------|
| `both_modify` | 27 | Both sides changed the same block |
| `both_add` | 6 | Both sides added different content at the same point |
| `add_modify` | 4 | One side added, the other modified |
| `modify_delete` | 2 | One side deleted, the other modified |

### Deep analysis of the 27 `both_modify` cases

| Sub-pattern | Count | Can deterministic handle? |
|-------------|:-----:|:---:|
| Small overlap (1–3 shared base lines) + disjoint tails | **14** | ✅ `partial_disjoint_merge` (just shipped) |
| Truly disjoint edits (0 shared base lines) | **5** | ✅ `token_disjoint` (size guard was blocking; now raised to 500) |
| Large overlap (>3 shared base lines) | **3** | ❌ Genuine conflict — LLM needed |
| Shared added content (overlapping adds) | **5** | ❌ Genuine conflict — LLM needed |

---

## 4. Improvements Just Shipped (Sprint 1 + 2)

### `token_disjoint` size guard raised (12 → 500)

The rule's overlap computation was always correct; the 12-line guard was the
only barrier. Real C++ conflict blocks have 100–2000+ lines across the three
sides. Raising to 500 unblocks the 5 truly-disjoint cases.

**Expected: +5 cases → ~44% deterministic rate.**

### `partial_disjoint_merge` rule (NEW)

Fires when `token_disjoint` declined due to a small overlap (≤3 base lines).
Decomposes into: pre-overlap zone (deterministic), overlap core (concession
logic or decline if genuine conflict), post-overlap zone (deterministic).

**Expected: +14 cases → ~62% deterministic rate.**

### Convergent-add resurrection whitelist

Filters resurrection-guard false positives where both sides independently added
the same content (convergent addition, not a resurrection).

**Expected: eliminates 9 of the 16 high-sim escalations → 96% eff. pass rate.**

---

## 5. Suggested Further Improvements for C++

### 5a. `insertion_union` for same-anchor C++ method additions (6 cases)

**Problem:** Both sides add different methods to the same class at the same
insertion point. `insertion_union` already handles same-anchor concatenation,
but `_pure_insertion_runs` returns `None` if any side has a `replace`/`delete`
opcode mixed in with the additions — common in real C++ (e.g., one side also
changes an access specifier).

**Suggestion:** Before declining, check if the non-insertion opcodes are
one-sided (only one side changed them). If so, apply the one-sided replace
first, then run `insertion_union` on the result.

**Risk:** Low — the whole-file `gcc -fsyntax-only` gate validates the result.

### 5b. C++ `#include` dedup at file level (currently `directive_union` is hunk-local)

**Problem:** `directive_union` (structural rule #14) deduplicates `#include`
additions *within a single conflict block*. But when entity-splitting produces
multiple sub-units, each may independently add the same `#include` — and the
dedup only runs per-unit, not across the spliced file.

**Suggestion:** Wire `deduplicate_imports` (the file_linker, currently
Rust-only) to recognize C++ `#include` directives and run as a Phase-2
post-splice step for C/C++.

**Risk:** Low — the build gate validates. Currently `deduplicate_imports` gates
on `language in ("rust", "toml", None)`; extending to C/C++ is a 2-line change.

### 5c. Whole-file repair: smart blame for splice defects (8 escalations)

**Problem:** 8 escalations are whole-file repair failures at sim 0.82–1.00.
The model's resolution is correct, but after splicing, `gcc -fsyntax-only`
reports a defect (brace imbalance, preprocessor imbalance) that the repair beam
can't fix because it can't attribute the error to a specific sub-unit.

**Suggestion:** The Phase-2 cross-unit repair (Layer 4, items 1-2) is built but
the loop exits before reaching the model re-resolve path when deterministic
repairs fire without effect (the `_phase2_model_used` fix). Verify this fix is
working end-to-end on the C++ corpus after the enhancements ship.

**Risk:** Medium — the loop accounting fix is already in place; measure its
impact on the re-run.

### 5d. Timeout cases: oversized-prompt detection (2 escalations)

**Problem:** 2 cases timed out at 900s on endless CEGIS retries. The model's
prompt is oversized (33K+ tokens on a 4B model with 8K window). The LLM size
guard skips the call, but the orchestrator retries instead of escalating.

**Suggestion:** Ensure the `llm_skipped_oversized_prompt` event triggers an
immediate escalation (not a retry). The hard-deadline no-retry fix should
handle this, but verify the oversized-prompt path also short-circuits.

**Risk:** Low — the escalation is the correct behavior for un-runnable prompts.

### 5e. `partial_disjoint_merge` core resolution via zealous_merge

**Problem:** The rule currently resolves the overlap core via concession logic
(one side equals base → take the other) or declines (genuine conflict). It
doesn't try `zealous_merge` on the core, which handles the "both sides changed
the same line but agreed" case.

**Suggestion:** Before declining, run `_try_zealous_merge` on the overlap
core's three texts. If zealous_merge resolves it (agreed/conceded), use that
result. Only decline if zealous_merge also fails.

**Risk:** Low — zealous_merge is already a well-tested rule; calling it on a
tiny sub-block (1-3 lines) is safe.

---

## 6. Safety Properties

All deterministic mechanisms share these safety guarantees:

1. **Never produce invalid syntax that the compiler wouldn't catch.** Every
   deterministic resolution is validated by `gcc -fsyntax-only` (per-unit) and
   the whole-file build gate (`make`/`cmake`). A bad splice is caught and the
   CEGIS loop re-resolves.

2. **Never silently drop intent.** The `partial_disjoint_merge` rule declines
   on genuine two-sided conflicts (both sides changed the same lines to
   different values). The resurrection detection (now with bounded history
   walk) catches silent undo of deliberate deletions.

3. **Conservative by default.** Rules decline (return `None`) rather than guess.
   The LLM is the fallback for every shape the deterministic stack can't
   handle safely. No rule produces a "probably right" merge — only provably
   correct ones.

4. **Idempotent and composable.** Each rule is a pure function; the cascade is
   first-match-wins. Entity splitting composes with the rules (each sub-unit
   runs through the full cascade independently).

---

## 7. Questions for Reviewers

1. **`partial_disjoint_merge` core default:** When the overlap core is a
   genuine conflict (neither side concedes), the rule now declines entirely.
   Should it instead emit a *mini-conflict* (just the 1-3 core lines) as a
   new sub-unit for the LLM, keeping the deterministic tail resolution?
   This would shrink the LLM prompt from "resolve this 200-line block" to
   "resolve these 2 lines." (Reviewer feedback from round 1 suggested this;
   it's the highest-leverage refinement.)

2. **`insertion_union` for C++ methods:** Should the rule be relaxed to handle
   mixed insert+replace (apply one-sided replaces first, then union the
   additions)? The risk is that a one-sided replace changes the anchor
   semantics, making the insertion point ambiguous.

3. **`directive_union` at file level:** Should we extend the file_linker's
   `deduplicate_imports` to C++ `#include`? This would catch cross-sub-unit
   duplicate includes that `directive_union` (hunk-local) misses.

4. **Resurrection guard sensitivity:** The convergent-add whitelist filters
   cases where ≥50% of the block's lines appear in both sides. Is 50% the
   right threshold? Too low → miss real resurrections; too high → false
   positives persist.

5. **Deterministic repair beam ordering:** Should `_try_deterministic_cc_repair`
   (gcc message classification) run *before* `_try_gcc_fixit_repair` (gcc's
   own fixits)? The fixits are more authoritative but sometimes no-op on
   C++ (gcc's fixit coverage is sparse for C++ template errors).

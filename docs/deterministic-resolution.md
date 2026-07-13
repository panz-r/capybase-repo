# Deterministic Conflict Resolution Machinery

This document describes the model-free conflict resolution layers in capybase —
the algorithms that resolve conflicts without any LLM call, and the decision
cascade that governs when each fires. The goal is a description accurate enough
that an academic reader could recreate a similar system from this document
alone.

The core design principle: **every deterministic layer is safe-by-construction
and declines on any doubt**. A layer that cannot prove its output is correct
returns `None`, and the dispatch falls through to the next layer. Every accepted
deterministic candidate still traverses the full validation pipeline (splice
scope, compile floor, AST preservation, both-sides-represented) before it is
applied — so the deterministic layers can only reduce LLM load, never produce a
worse merge than the LLM would.

---

## The decision cascade

For each conflict unit, the orchestrator runs a strictly ordered cascade
(`orchestrator.py:_resolve_unit`). The first layer that produces a validated
outcome wins; the LLM is only invoked when every prior layer declines. The
ordering is cheapest-first (least computation, highest confidence):

| # | Layer | Module | Provenance | Gate |
|---|-------|--------|------------|------|
| 1 | Exact-history reuse | `exact_reuse.py` | `exact_history_reuse` | always on |
| 2 | Structural resolver | `structural_resolver.py` | `deterministic_structural` | `enable_structural_resolver` (default on) |
| 3 | Combination search (SBCR) | `sbcr.py` | `combination_search` | `enable_combination_search` (default on) |
| 4 | Test-gated side picker | orchestrator | `test_gated_side` | `tests.required` + a specific test command |
| 5 | Block capture | resolution_engine | `block_capture` | `enable_block_capture` (default on); modify/delete only |
| 6 | LLM resolution | resolution_engine | `plain_llm` / `history_augmented_llm` | fallback |

An out-of-band **deterministic brace repair** (`deterministic_brace_repair`,
whole-file post-splice) runs in the Phase-2 CEGIS path to fix splice-junction
brace imbalances with a single-edit balance heuristic. It is not part of the
per-unit cascade.

The cascade runs only on a fresh resolve (no prior failures). On a CEGIS retry,
the LLM loop is entered directly with the failure feedback.

---

## Classification

Two classification systems feed routing and the structural rules.

### Per-side intent classification (`merge_intent.py`)

`classify_side(base, side)` uses `difflib` opcode analysis on line lists to
classify each side as `unchanged`, `added`, `deleted`, or `modified`:

- Identical to base → `unchanged`
- Base empty, side non-empty → `added`
- Side empty, base non-empty → `deleted`
- Only deletions → `deleted`; only insertions → `added`; both → `modified`

`direction(base, current, replayed)` classifies the whole conflict into a
`ConflictKind`: `both_unchanged`, `one_unchanged`, `modify_delete`,
`delete_delete`, `both_add`, `add_modify`, `both_modify`. Delete cases take
precedence over `one_unchanged` (a delete-vs-unchanged is classified as
`modify_delete` — the dangerous ambiguous case). This produces a
`deleting_side` field ("current" / "replayed" / None) consumed by the
`delete_side` rule and block capture.

### Difficulty banding (`classifier.py`)

`classify(unit)` assigns a `Band`: `trivial`, `easy`, `medium`, or `hard`.
`trivial` when the structural resolver's feasibility probe
(`deterministically_mergeable`) succeeds, or the conflict is
both-unchanged/one-unchanged/delete-delete. Otherwise, a set of "hard signals"
accumulates (≥40 lines, touches a definition, same-symbol overlap, same-line
overlap, modify/delete with a modified keeper, high severity). Two or more
coincident signals → `hard`; one → `medium`; zero → `easy`. The band drives
routing (trivial conflicts short-circuit through the deterministic cascade;
hard conflicts may get elevated sample counts).

### Operation signatures (ConGra-style, §3.3)

The feature spine (`conflict_extractor.conflict_features`) carries per-entity
operation counts derived from the BASE→REPLAYED entity diff:
`ops_added`, `ops_removed`, `ops_modified` (signature + body changes),
`ops_renamed`, `ops_moved`. These give the difficulty classifier a
discriminative operation view (pure-rename vs heavy body-modify vs additive).
The entity diff is computed **once per unit** and cached on
`structural_metadata["entity_changes"]`, shared by the commit-change-type
classifier, the operation counts, and the LLM prompt's semantic-change block —
so the BASE→current and BASE→replayed parses happen at most twice per unit, not
re-parsed by every consumer.

### Conflict-shape hash (`memory/shape.py`)

A content-agnostic 12-character SHA1 of per-side (added, removed, changed) line
counts against a normalized base. Used by exact-history reuse and RAG retrieval
to match structurally-similar conflicts without comparing content.

---

## Layer 1 — Exact-history reuse (`exact_reuse.py`)

Replays a previously-accepted resolution verbatim when the current conflict
matches a stored experience. Always on; no configuration gate.

**Matching** — all conditions must hold:
1. Same conflict-shape hash (structural fingerprint).
2. Same language.
3. Same region kind (marker-block vs. whole-file).
4. Same file path (load-bearing — the shape hash is content-independent, so the
   path disambiguates two files with the same edit structure).
5. Prior outcome was `accepted` (the store pre-filters).
6. Validation evidence exists: `tests_passed`, or `future_apply_probe_applies`,
   or no recorded introduced diagnostics.
7. Non-empty resolved text.

**Near-miss tracking.** A same-shape prior that fails a later condition is
recorded as a "near miss" with the reason, for diagnostics. If no full match
exists but near-misses do, a skip sentinel is returned (the orchestrator
journals it); if no same-shape priors exist at all, returns `None`.

The reused candidate is re-validated identically to any candidate (splice,
compile, AST, both-sides-represented) plus a future-obligations gate. A stale or
wrong reuse fails validation and falls through — reuse is a speed optimization,
never a correctness bypass.

---

## Layer 2 — Structural resolver (`structural_resolver.py`)

A model-free resolver applying provably-safe merge rules. Entry point:
`resolve_structural(unit)`. Returns `StructuralResolution(rule, text)` where
`text is None` means "decline." The rules fire in strict priority order:

### Rule 1 — `delete_side`
One side cleanly deleted base content and the other side is `unchanged` or
`deleted`. Accepts the deletion. Declines if the keeper side is `modified` or
`added` (the keeper has load-bearing changes that a deletion would lose).

### Rule 2 — `identical_sides`
Both sides have identical normalized text (whitespace-collapse equality). Emits
the non-empty side.

### Rule 3 — `one_sided_change`
Exactly one side's normalized text diverges from base. Emits the diverging side.

### Rule 4 — `disjoint_edits`
Both sides changed base, but on **disjoint line ranges** (the sets of base-line
indices each side modified do not intersect). Reconstructs by applying each
side's changes to their respective base regions. Declines on any overlap.

### Rule 5 — `zealous_merge`
Per-base-region 3-way merge: agreed changes (both sides made the same edit) →
emit; one-sided → take it; genuine two-sided disagreement → decline. **Declines
on any pure insertion** (two insertions at the same anchor have ambiguous
ordering). Overlapping changed regions must share the exact same base span or
it bails.

### Rule 6 — `entity_disjoint`
Both sides touched structural entities (functions, methods, structs — for
Python/Rust) in an enclosing container, on **disjoint entity sets**. Enumerates
entities via a grammar-free abstract parser (a state-machine parser covering
brace-delimited and indentation-delimited languages — no tree-sitter dependency),
computes per-side touched identities (added, modified, renamed). Includes
**rename detection**: if an entity was renamed on one side (body content
identical to base, old name gone, new name similar), the rename is tracked and
the renamed entity is treated as touched only by that side. Declines if any
canonical entity is touched by both sides (unless both made the identical
rename). Reconstructs the container with all entities from both sides.

### Rule 6b — `refactoring_aware_merge` (RefMerge pattern)
Fires when `entity_disjoint` **declined on overlap**, but the overlap decomposes
into a clean **rename + body-modify partition**. For each entity touched by both
sides, the rule classifies each side's touch:

- **Pure rename**: the entity's header line changed but its body content is
  identical to base (detected via `_detect_renames`).
- **Body-only modify**: the entity's header line is identical to base but its
  body content changed (header unchanged rules out a signature change).

If every overlapping entity is a clean {rename, modify} pair (one side renamed,
the other modified the body — never both modified, both renamed, or a signature
change), the rule **composes**: takes the renamer's header + the modifier's
body. This preserves both intents (the rename and the body change). Declines on
any pair that isn't a clean partition. This is the RefMerge
normalize→merge→reapply pattern specialized to renames; it runs only on the
overlap tail (after `entity_disjoint` declined), so its cost is paid only on the
hard cases.

### Rule 7 — `token_disjoint`
A fine-grained token-level merge for small conflicts (≤12 lines). Tokenizes each
side with a 4-category tokenizer (letters, digits, whitespace, symbols —
lossless). Aligns each side against base at the token level. If the changed
base-token spans are disjoint and there are no pure-insertion anchor collisions,
splices both edits into a single-pass walk. Declines on any token overlap.

### Rule 8 — `list_union`
Both sides appended distinct items to the same `[...]` list literal, preserving
base items in place. Declines if no single list is found, if a side reordered or
removed base items, or if the appended sets intersect. Merge = base items +
current's appends + replayed's appends (current first).

### Rule 9 — `dict_union`
Both sides added disjoint keys to a single-line `{...}` dict with no shared-key
value change. Declines multi-line dicts (indentation reconstruction is unsafe).

### Rule 10 — `insertion_union`
Both sides made pure whole-line insertions at disjoint base anchors. Interleaves
the insertion runs, preserving each side's relative order (current's run first at
each anchor).

**Decline semantics.** Every rule returns `None` to signal "can't handle this
conflict safely." The resolver tries the next rule. If none applies, it returns
`StructuralResolution(rule=None, text=None)` and the dispatch continues to SBCR.

---

## Layer 3 — Combination search / SBCR (`sbcr.py`)

Search-based combination resolution: enumerates order-preserving interleavings of
the two sides' lines to find the combination most similar to both parents. Entry
point: `resolve_by_combination_search(unit)`.

**Scope gate.** Fires only when the effective base is **empty** (a true addition
conflict, not a modification). The effective base is the diff3-refined base when
available, else `unit.base.text`. This is safe-by-scope: on a modification
conflict, the search space would contain semantically-wrong last-wins
concatenations, so it refuses to propose.

**Search space.** All order-preserving interleavings of the two sides' line
lists — each side's lines keep their relative order, but the two sides may merge
arbitrarily. The count is C(m+n, m) where m and n are the side lengths.

**Fitness function.** Mean **character-level Gestalt similarity** of the
candidate to each parent: `2·|LCS|/(|a|+|b|)` over the joined text, computed via
`difflib.SequenceMatcher.ratio()` on the joined strings. Character-level LCS
(mean-aggregated) reaches median Spearman ≈0.79 to developer-chosen resolutions,
outperforming line-level and token-level alternatives. The search is
**constrained to full unions** (every candidate contains all lines from both
sides): char-level mean similarity has a length bias — a shorter candidate has
proportionally fewer non-matching chars and scores spuriously high — so the
fitness must be a tie-breaker *over orderings of the union*, not a selector over
subsets.

**Search strategy:**
- **Exhaustive** when C(m+n, m) ≤ 1024: tries every interleaving (each is a full
  union by construction), returns the highest-fitness.
- **Random-restart hill climbing** for larger spaces. The neighborhood is
  **adjacent cross-side swaps**: swapping two adjacent lines that originate from
  different sides. This preserves each side's internal order (the
  order-preserving invariant) while exploring exactly the interleaving space,
  keeping every candidate a full union. First-improvement move selection. Three
  termination criteria: a hard budget (`sbcr_max_iterations`, default 2000
  evaluations), a stagnation limit (`sbcr_stagnation_limit`, default 10
  consecutive non-improving evaluations), and a wall-clock budget
  (`sbcr_max_time_seconds`, default 15s).

**Acceptance threshold.** The best candidate must clear a fitness floor
(`sbcr_floor`, default 0.6). Below this, the candidate is essentially one-sided
and is rejected.

**Shrinkage guard.** A candidate shorter than `sbcr_min_candidate_ratio`
(default 0.5) of the larger side is rejected — it has dropped too much of a side
to be a genuine combination. (This is a backstop; the union-constrained search
already keeps every line, but the guard documents the invariant and protects
against future neighborhood changes.)

**Balance routing.** Even when SBCR resolves, a balance check
(`min(cur_lines, rep_lines) / max(...)`) decides whether to accept. If the
conflict is imbalanced (one side much larger) and routing is enabled with a
non-zero `min_balance_for_sbcr_accept`, SBCR declines and defers to the LLM
(which handles imbalanced conflicts better). Default threshold 0.15 (a
conservative floor vs the research's ~0.2 crossover); set to 0.0 to accept
whenever SBCR resolves.

---

## Layer 4 — Test-gated side picker

Accepts one conflict side verbatim when **exactly one side** passes both
validation and the test gate. This is a behavioral discriminator: the test suite
is the ground truth for which side is correct.

**Activation conditions:**
- Tests are required (`config.tests.required`).
- A specific, non-trivial test command is configured (not the `true` shim or
  bare `pytest`, which may have unrelated failures).
- Marker-block unit only.
- Both sides non-empty and different.

**Algorithm:**
1. Build a candidate from each side verbatim (the full conflict-side text).
2. Validate each candidate (splice, compile, AST, both-sides-represented).
3. For candidates that pass validation, write the spliced file to the worktree
   and run the test gate.
4. Accept the side **iff exactly one side passes both validation and tests**.
   Zero passing → decline (neither is correct). Two passing → decline (the gate
   cannot discriminate; defer to the LLM/critic).

**CEGIS hardening.** On decline, the per-side diagnostics (validation failures +
test verdicts) are stashed and threaded into the LLM path as seed failures, so
the model starts with concrete error context rather than a feedback-free resolve.

---

## Layer 5 — Block capture

For large modify/delete conflicts where the model cannot reliably reproduce a
large block: the model makes a keep/delete/escalate **decision** (not a
reproduction), and capybase splices the chosen side verbatim.

**Activation conditions:**
- `enable_block_capture` (default on).
- The conflict is classified `modify_delete` with a known deleting side.
- The kept block is large (non-blank lines ≥ `block_capture_min_lines`).

**The decision.** The model is shown a summary of the block (entity signatures +
first/last lines, never the full text) and answers with one of:
- `accept_deletion` — the deletion stands; the deleting side wins.
- `keep_block` — the keeper side survives.
- `needs_human` — escalate.

Any unparseable or unrecognized response normalizes to `needs_human`.

**Splicing.** The decision maps to text taken verbatim from the conflict side:
- `accept_deletion` → the deleter's (empty/near-empty) text.
- `keep_block` → the keeper's text, spliced verbatim.
- `needs_human` → decline (return `None`).

A whole-file `keep_block` registers the path to suppress the end-of-rebase
silent-resurrection scan (a reviewed keep, not a silent undo). The candidate is
validated; an invalid splice falls through to the LLM.

---

## Validation pipeline (applied to every accepted candidate)

Every candidate — whether from a deterministic layer or the LLM — passes
through the same validation before it is applied to the worktree:

1. **No conflict markers** remaining in the splice.
2. **Exact splice scope** — the merge didn't bleed outside the conflict region.
3. **Compile floor** — the fully-spliced file is compiled (Python: `py_compile`;
   Rust: `cargo check` or standalone `rustc`). For multi-hunk files, sibling
   conflict markers are blanked (first side's body kept live, second side's
   commented out) so the compile reflects the candidate's hunk in context.
4. **Syntax / AST preservation** — the merge didn't drop unchanged structural
   entities (parsed via the grammar-free abstract parser — no tree-sitter
   dependency).
5. **Both-sides-represented** — a side's additions weren't silently dropped.
6. **Verifier-model critic** (default on) — an LLM judge checks semantic intent
   preservation.

A candidate that fails any hard gate is rejected; the layer returns `None` and
the cascade continues. This is what makes the deterministic layers safe: they can
only short-circuit the LLM when validation confirms their output is sound.

---

## Provenance

Every resolution carries a provenance string stamped at the point it was
produced. The canonical set (`provenance.py:PROVENANCE_VALUES`, in display
order):

| Provenance | Meaning |
|---|---|
| `deterministic_structural` | Structural resolver (rules 1–11, incl. `refactoring_aware_merge`) |
| `deterministic_brace_repair` | Whole-file post-splice brace repair |
| `exact_history_reuse` | Verbatim replay of a prior accepted resolution |
| `combination_search` | SBCR order-preserving interleaving search |
| `test_gated_side` | Test-gated side picker (exactly one side passed) |
| `block_capture` | Model decided keep/delete; capybase spliced verbatim |
| `history_augmented_llm` | LLM resolved, augmented with RAG few-shot context |
| `plain_llm` | LLM resolved (no history augmentation) |
| `manual` | Human provided the resolution interactively |

Provenance is immutable except for one case: `plain_llm` may be re-stamped to
`history_augmented_llm` when RAG context is confirmed to have been used. No
deterministic, reuse, or manual provenance is ever overwritten.

The drift detector uses provenance as its primary gate: deterministic
resolutions (`deterministic_structural`, `exact_history_reuse`,
`combination_search`, `test_gated_side`, `block_capture`,
`deterministic_brace_repair`) can never produce semantic drift by construction,
so drift signals are only evaluated for LLM-produced resolutions.

---

## Decline-reason journaling (§5.3)

Every pre-LLM layer records a uniform `resolution_attempt` event
(`{mechanism, decision, reason}`) on BOTH accept and decline — so a skip is
never invisible and the reason a layer passed is debuggable. Exact-history reuse
was always fully instrumented; structural and SBCR now emit the same shape:

- **Structural declines** — `reason` is `"no rule applied"`, `"failed
  validation"`, or `"strictness declined"`.
- **SBCR declines** — a dedicated `combination_declined` event carries the
  `fitness` of the best-seen candidate and a populated `reason` naming the
  decline cause: `"modification conflict (non-empty base)"`, `"fitness X < floor
  Y"`, `"shrinkage: N candidate lines < R% of larger side"`, `"one side empty"`,
  or `"balance X < threshold Y"`.

This gives the audit log a complete picture of why each conflict reached (or was
resolved before) the LLM.

---

## Recreating this system

A minimal re-implementation of the deterministic cascade needs:

1. **Side classification** — diff each side against base to classify as
   unchanged/added/deleted/modified, then classify the conflict direction.
2. **A structural rule engine** — the 11 rules above, tried in priority order,
   each returning `None` on any doubt. The critical rules for coverage are
   `disjoint_edits`, `entity_disjoint` (with rename detection),
   `refactoring_aware_merge` (rename + body-modify composition), and
   `insertion_union` — these handle the majority of real-world mergeable
   conflicts.
3. **An interleaving search** — for pure-addition conflicts, search
   order-preserving interleavings of the **full union** by mean
   **character-level similarity** to both parents, exhaustive for small spaces
   and hill-climbed over adjacent cross-side swaps for large ones, with a
   shrinkage guard and fitness floor.
4. **Validation** — a compile floor (splice + compile the whole file), plus
   both-sides-represented and splice-scope checks, applied to every candidate
   before acceptance.
5. **Decline-on-doubt semantics throughout** — every layer that cannot prove
   correctness returns `None`, so the LLM is always the fallback.

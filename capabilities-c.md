# capybase — C/C++ Conflict Resolution Capabilities

This document describes the current capabilities and mechanics of capybase
when resolving merge conflicts in C and C++ codebases. It covers the full
pipeline from conflict detection through resolution, verification, and
escalation. It is written for review — every mechanism described here is
implemented and tested.

## Design principles

capybase is **conservative by construction**: it escalates rather than
guesses, and a silent wrong merge is considered the worst outcome. It has
no parser, no AST, no CST, and no bundled language grammar. It reasons
over raw spans, diffs, delimiter scans, lexical masking, compiler
diagnostics, and provenance. The compiler is the authority on correctness;
capybase's own analysis is advisory.

The system is built for a **weak local model** (a single 4B-parameter
endpoint with an ~8K-token context window). Every mechanism is designed to
either (a) resolve the conflict without calling the model at all, or (b)
give the model the smallest, most relevant prompt possible.

---

## Overall flow

A `git rebase` produces conflict markers on disk. capybase processes each
conflicted file through two phases:

### Phase 1 — Per-unit resolution

Each conflict marker block in the file becomes a **conflict unit**. Every
unit is resolved independently through a layered cascade (cheapest/safest
first). Each layer declines to the next on any doubt. The cascade is:

1. **Exact history reuse** — if an identical prior conflict was previously
   accepted, replay its resolution verbatim (re-validated).
2. **Structural resolver** — 15 model-free rules (detailed below).
3. **Combination search** — enumerates order-preserving interleavings of
   the two sides for the best combination (difficulty-aware).
4. **Test-gated side picker** — when a real build/test command is
   configured, builds each side as a candidate and uses the test result to
   discriminate.
5. **Block-capture** — for large modify/delete conflicts: makes a
   keep/delete/escalate decision and splices the chosen side verbatim.
6. **Source-derived candidate portfolio** — assembles 5 candidates from
   exact source lines and validates them through the full pipeline. When
   one passes, the conflict resolves with zero model calls.
7. **Oversized guard** — if the essential conflict content exceeds the
   model's context window, the unit escalates immediately.

Only when ALL of these decline does the **LLM resolution** loop begin.

### Phase 2 — Whole-file verification and repair

After all units in a file are resolved, the fully-spliced file is verified
as a whole. If verification fails, a **CEGIS repair loop** re-resolves the
unit most likely at fault, using deterministic repair mechanisms first,
then the model. After the repair budget is exhausted, a final
deterministic-only pass runs as a last attempt.

---

## Structural resolver — 15 model-free rules

The structural resolver (`resolve_structurally`) dispatches rules in
priority order. The first rule that applies wins; every rule runs the full
validation pipeline before its result is accepted.

| # | Rule | What it handles |
|---|------|----------------|
| 1 | `delete_side` | One side cleanly deleted the block; the other is unchanged |
| 2 | `identical_sides` | Both sides resolved to the same text (modulo whitespace) |
| 3 | `one_sided_change` | Only one side diverged from the base |
| 4 | `disjoint_edits` | Both sides changed non-intersecting line ranges |
| 5 | `zealous_merge` | Per-base-line 3-way merge resolving agreed and one-sided sub-regions |
| 6 | `entity_disjoint` | Both sides inserted distinct entities (different kind/name) at the same anchor |
| 7 | `refactoring_aware_merge` | A clean rename-vs-body-modify partition — compose the renamer's header with the modifier's body |
| 8 | `token_disjoint` | Both sides changed different tokens on the same line |
| 9 | `text_value_resolution` | Both sides edited the same prose/config value differently — take the lexicographically-later value |
| 10 | `dependency_version_resolution` | TOML dependency-version literals — take the semver-greater version |
| 11 | `list_union` | Both sides appended distinct list items — opinionated current-then-replayed order |
| 12 | `dict_union` | Both sides appended distinct dictionary entries |
| 13 | `brace_union` | Both sides appended distinct single-line `{...}` items |
| 14 | `insertion_union` | Both sides inserted distinct lines at the same anchor |
| 15 | `directive_union` | C/C++ preprocessor dedup — both sides added the same `#include`/`#define`, collapse to one copy |

Rules 13–15 are "easy-merge union" rules that fill the gap every prior rule
declines. An opinionated deterministic ordering resolves them; a wrong
guess still fails the validation pipeline and falls through to the model,
so the policy is safe.

---

## Source-derived candidate portfolio

Before invoking the model, capybase assembles 5 candidates from exact
source lines and validates each through the full per-unit pipeline:

1. **Current side only** — the upstream side verbatim
2. **Replayed side only** — the replayed commit verbatim
3. **Current then replayed** — both concatenated (insertion-union order)
4. **Replayed then current** — reversed concatenation order
5. **Shared + distinct** — shared lines once, then current-only lines, then replayed-only lines

When exactly one candidate passes every hard gate, it is accepted with zero
model calls. This exploits the empirical finding that ~87% of merge
resolutions contain only lines from the input sides.

---

## Prompt construction

When the model is needed, the resolve prompt is assembled from multiple
context blocks, all token-budgeted to fit the model's window:

- **Conflict sides** — current, base, and replayed, preferring diff3-refined
  sides (tighter conflict regions via zdiff3). Rendered as labeled blocks.
- **Side intent** — a conflict-shape label and per-side obligation contract
  ("must preserve" edits relative to base).
- **Structural context** — file structure / unit inventory / change summary.
- **File skeleton** — for oversized C/C++ files, a compact block of
  top-level entity names (includes, macros, typedefs, structs, functions,
  globals) extracted by the skeleton parser. Budget-protected (never trimmed).
- **Obligations** — future obligations and branch intent.
- **Few-shot examples** — up to 2 retrieved similar-past-merge examples.
- **Dependencies** — cross-file related snippets.
- **History** — replay-position and future-commit relevance facts.

When the prompt exceeds the token budget, augmentation sections are trimmed
in priority order (few-shot first, then dependencies, then structural
context), always protecting the conflict sides, skeleton, and obligations.

### Base localization for oversized files

When the base side exceeds the character threshold (4000 chars) and
diff3 refinement is unavailable, capybase localizes the base using
**anchor-based windowing**: it takes context lines before and after the
marker block as anchors, finds the corresponding region in the base file,
and returns only that region. If the anchors can't be uniquely matched,
the base is explicitly omitted rather than showing a wrong region.

### Prompt variants

The model sees different prompt framings depending on the retry context:

- **Resolve** — the initial prompt (conflict sides + context + contract).
- **Retry** — the resolve prompt enriched with the specific failure
  feedback (which validator rejected the candidate and why).
- **Repair** — shows the broken candidate + the compiler/build diagnostic
  + a splice-context snippet (±5 lines around the error), plus
  **failed-patch memory** (up to 3 prior failed-attempt summaries labeled
  "do NOT repeat these fixes").
- **Recovery** — a reframed prompt for cases where the model self-reported
  `needs_human`, giving it another chance with different framing.

---

## C skeleton extractor

A self-contained, depth-tracking token scanner that extracts top-level
entity names from C files without building a full parser. It handles:

- `//` and `/* */` comments (stripped)
- String/char literals (masked — braces/parens inside don't affect depth)
- Line continuations (`\` at EOL — joined before scanning)
- Preprocessor directives (isolated — macro bodies don't affect brace depth)
- `#include`, `#define`, `typedef`, `struct`/`union`/`enum`,
  function definitions/declarations, global variables
- `extern "C" { ... }` blocks (braces skipped so declarations inside are
  scanned at depth 0)
- Function-pointer typedefs (`typedef T (*name)(...)` — name extracted from
  raw tokens)
- `typedef struct Foo { ... } Foo` (alias captured via pending-typedef
  tracking across the brace boundary)

The skeleton is rendered as a compact block (one line per category,
order-preserving dedup, ~400 tokens max) and prepended to the prompt for
oversized C/C++ files. It gives the model global entity awareness that
the windowed conflict region cannot provide.

The extractor never raises — it degrades gracefully to "skip" on any
construct it can't handle (macro-generated code, X-macros, C++ templates).
It is cached per-file in the unit's structural metadata.

---

## Verification pipeline

Every accepted resolution passes through multiple verification layers.

### Phase A — Per-unit validators

Each candidate is validated by a chain of per-unit checks before it can be
accepted:

1. **Non-empty** — rejects empty/whitespace-only resolutions
2. **No conflict markers** — no leaked `<<<<<<<`/`>>>>>>>` markers
3. **Exact splice scope** — the merge didn't bleed outside the conflict region
4. **AST preservation** — structure outside the conflict span is unchanged
5. **Preservation heuristic** — flags one-sided copies that drop the other side
6. **Both-sides-represented** — each side that added content must contribute
   at least one distinctive line
7. **Intent coverage** — quantitative floor on each side's added units surviving
8. **Unattributed code** — flags code present in none of base/current/replayed
9. **Obligation contract** — each side's added/changed/removed-vs-base edits
   must be carried
10. **Needs-human** — rejects model self-reported `needs_human=true`
11. **Syntax check** (C/C++) — per-unit `gcc`/`g++ -fsyntax-only` on the
    spliced candidate (see C/C++ specific verification below)
12. **Verifier-model critic** (optional, default on) — an LLM judge checks
    the resolution preserves both sides' semantic intent

### Phase B — Whole-file verification (`verify_file`)

After all units are resolved and spliced, the whole file is checked:

1. **No markers** — the fully-spliced file is marker-free
2. **Splice coherence** — brace balance and preprocessor `#if`/`#endif` balance
3. **Compile floor** — the fully-spliced file is compile-checked:
   - **Python**: `py_compile`
   - **Rust**: `cargo check` or `rustc`
   - **C/C++**: per-unit `gcc`/`g++ -fsyntax-only` plus an optional
     whole-tree build (make/cmake) — detailed below
4. **Duplicate definitions** — no duplicate function/struct/macro definitions
5. **Semantic checks** — introduced diagnostics, LSP diagnostics (optional)
6. **Silent-resurrection detection** (post-rebase) — after a clean rebase,
   compares the result against content the target branch deliberately
   deleted and flags any that came back

---

## C/C++ specific verification

### Per-unit syntax gate (CcsSyntaxValidator)

The per-unit gate compiles the spliced candidate with `gcc -fsyntax-only`:

- **Header files** (`.h`/`.hpp`/`.hh`/`.hxx`): the per-unit gate is **skipped
  entirely**. Headers are never compiled standalone in real projects — they
  are always `#include`d from a `.c` file that provides type definitions.
  Standalone gcc reports false-positive "unknown type name" errors for
  project-internal types. Phase B's whole-file build is the authoritative
  check for headers.
- **`.c`/`.cpp` files**: the candidate is spliced, sibling markers are
  blanked to comments, and `gcc -fsyntax-only` runs. **Semantic errors**
  (undeclared identifier, unknown type name, incomplete type, missing
  header, etc.) are deferred to Phase B — they are artifacts of compiling
  out of translation-unit context, not parse defects. Only true **parse
  errors** (missing semicolon, stray brace, unterminated string) surface
  as hard failures.

### Whole-file build gate

When a user-supplied build command is configured (`cc_build_command`,
e.g. `make -j4`, `cmake --build build`), the resolved file is written to
its real path in the repo and the build runs. This is the authoritative
oracle for C — it resolves sibling `#include` headers that standalone gcc
cannot.

**Error localization**: when the build fails, each gcc error line is
classified by WHERE it occurs:

- **Linker errors** (`collect2`, `ld returned`, `undefined reference`) →
  compile-pass (the model's code compiled fine; the link failure is
  infrastructure).
- **Sibling-file errors** — the gcc error's file prefix doesn't match the
  conflict file → the error is in a file the merge didn't touch (e.g.
  `tool/lemon.c` has a pre-existing type conflict while resolving
  `src/delete.c`). Classified as infrastructure, not a merge defect.
- **-Werror warning promotions** (`[-Werror=...]` tag) → the code compiled
  successfully but triggered a strictness flag. Classified as infrastructure.
- **Build-driver lines** (`make[2]: *** ...`, `CMake Error`, `ninja:`) →
  skipped (they reference build targets, not source files).
- **Real conflict-file errors** — the error is positively in the resolved
  file with no -Werror tag → genuine defect, hard-fail.

When in doubt (unparseable lines), the gate falls back to hard-fail.

**Standalone gcc fallback**: when no build command is configured (e.g.
autotools trees that can't be completed), standalone `gcc -fsyntax-only`
runs with `-I` paths for the repo root and the conflict file's directory,
so sibling `#include "..."` headers can resolve. Semantic errors and
-Werror promotions are tolerated. A missing compiler → "not checked"
(never a false fail).

---

## CEGIS repair loop

When a candidate fails verification, the failure feeds back as a
counterexample. The model re-resolves with the broken output and the
specific failure, bounded by retry policy.

### Convergence backstops

The CEGIS loop has three convergence detectors to prevent infinite cycling:

- **No-progress guard** — identical hard-failure signature across N
  attempts → escalate.
- **Oscillation backstop** — same exact resolved text seen more than the
  budget allows → escalate.
- **Convergence backstop** — same normalized text (whitespace/comments
  stripped) seen ≥ threshold → escalate (cosmetic-variation cycling).
- **Convergence escape hatch** — a candidate blocked ONLY by advisory
  warnings (not hard errors) that has cycled ≥ threshold is ACCEPTED.

### Failed-patch memory

When retrying, the prompt includes up to 3 prior failed-attempt summaries
labeled "PRIOR FAILED ATTEMPTS (do NOT repeat these fixes)". This gives
the model negative evidence and prevents it from repeating the same fix.

### Deterministic repair beam

Before re-invoking the model, seven model-free repair mechanisms run in
order:

1. **Brace repair** — single-edit brace fix at splice junction
2. **Prefix dedup** — strips consecutive duplicate statement lines
3. **Boundary echo strip** — removes exact boundary echoes (left/right overlap)
4. **Import/directive dedup** — when a duplicate-import/directive error is present
5. **C/C++ compiler-diagnostic repair** — classifies the gcc message
   (missing semicolon → append `;`, missing close brace → balance braces,
   extra close brace → delete, stray character → strip, unterminated
   literal → close, duplicate entity → delete redefinition) and generates
   a single-token fix at the diagnosed line
6. **Side-consistency repair** — restores dropped common lines, deletes
  invented lines (C/C++ only)
7. **Side-consensus repair** — when both sides agree on a structural
  property the candidate violates

All repairs are re-validated through the full pipeline. After the repair
budget is exhausted, a final deterministic-only pass (no model) runs as a
last attempt — this also runs when the model's re-resolve escalated.

---

## Wall-time budgets

Two budgets bound resolution latency:

- **Per-unit budget** (`max_wall_time_per_unit_seconds`) — caps the total
  model/CEGIS iteration time for ONE unit. Excludes verification time
  (compilation, tests) so a slow first build doesn't eat the model's retry
  budget.
- **Per-file budget** (`max_wall_time_per_file_seconds`) — an outer cap on
  total resolution + repair time per FILE. This prevents the nested-retry
  budget explosion where the whole-file repair loop creates nested
  `_resolve_unit` calls, each with a fresh per-unit budget. The file-level
  deadline threads through both Phase 1 (per-unit resolution) and Phase 2
  (whole-file repair) so the cumulative time is bounded regardless of how
  the retry budgets split.

Both default to 0 (disabled); the live-eval configuration enables them
with values tuned for the weak model's latency.

---

## Escalation

When capybase cannot resolve a conflict with high confidence, it
escalates: the rebase is aborted, the repo returns to its pre-rebase HEAD,
and a review bundle is written explaining why it stopped and how to resume.
Escalation is always the safe outcome — it is never a silent wrong merge.

Escalation happens when:
- All pre-LLM layers declined and the CEGIS loop exhausted its budget
- The oversized guard fired (conflict exceeds the model's context window)
- The model self-reported `needs_human=true` (after recovery retries)
- The convergence backstops detected non-progress
- The wall-time deadline was reached
- The silent-resurrection scan flagged content that came back after
  deliberate deletion

---

## Live-eval measurement

The C corpus comprises 205 cases mined from redis (55), json-c (17), and
sqlite (133). 73 cases are evaluable (under the 48K marker threshold);
the remaining 132 are oversized beyond the model's single-pass capacity.

The live eval drives the full system path: each case is materialized as a
real git repo with conflict markers, the orchestrator resolves it
end-to-end (extraction → resolution → file write → build gate), and the
result is compared against the human merge (the oracle) via token-Jaccard
similarity.

Verdicts per case:
- **PASS** — orchestrator did not escalate; resolved file is marker-free,
  brace-balanced, AND sim ≥ 0.95.
- **NEAR_MATCH** — marker-free and brace-balanced, but sim 0.80–0.95.
- **ESCALATE** — orchestrator escalated (human required). The safe outcome.
- **ORACLE_DIVERGENT** — marker/brace failure or sim < 0.80.

# capybase C/C++ — Review Feedback Assessment

This document assesses each suggestion from the two reviews against capybase's
current implementation. Suggestions are categorized as:

- **Already implemented** — the reviewers missed an existing capability
- **Actionable** — genuinely new, compatible with our constraints, worth building
- **Noted for later** — valuable but deferred (scope, dependency, or risk)
- **Conflicts with constraints** — would violate a design principle

---

## Already implemented (reviewers missed these)

The capabilities document described these mechanisms, but the reviewers either
didn't see them or didn't recognize their coverage. No action needed — these
are called out so the revised feedback can account for them.

### 1. Include/directive deduplication (directive_union rule)
**Reviewer 1 §1c, Reviewer 2 §1 "Include Sorting/Deduplication"**

capybase already has the `directive_union` structural resolver rule (rule 15
in the cascade). It collapses duplicate `#include`/`#define` directives when
both sides add the same one. Additionally, the file-linker import deduplication
(`deduplicate_imports`) runs as step 4 in the deterministic repair beam, removing
duplicate `use`/`#include` statements introduced by the model's per-unit
resolution. Both are model-free and run before the LLM is ever called.

What we do NOT do is sort includes into groups (system vs project). This is
intentional — reordering includes can change semantics in C (macro definition
order matters), and the compiler validates correctness without it.

### 2. Duplicate definition detection
**Reviewer 2 §1 "Duplicate static Detection"**

capybase already detects duplicate definitions at two levels:
- Per-unit: the `_classify_ccs_parse_error` classifier maps gcc's
  `redefinition of` / `duplicate definition` messages to the `duplicate_entity`
  category, and the deterministic cc-repair beam deletes the redefinition line.
- Whole-file: the Phase B duplicate-definition semantic check runs after
  splicing all units, catching cross-unit duplicates.

### 3. Self-consistency / diverse sampling
**Reviewer 2 §2 "Self-Consistency Sampling over CEGIS"**

capybase already has `enable_self_consistency` and `diverse_sampling` config
flags (in `FutureConfig`). When enabled, the engine generates multiple samples
(with temperature variation) and selects the first that passes validation.
These are **wired but off by default** — the live eval uses `samples=1` because
the weak model's latency (~100s/generation) makes parallel sampling expensive.
The infrastructure exists; enabling it is a config change, not new code.

### 4. JSON mode (constrained output)
**Reviewer 2 §2 "Constrained Decoding"**

capybase already uses JSON mode (`json_mode: bool = True` in the model config).
Every completion sends `response_format: {type: json_object}`, forcing the
model to output valid JSON with `resolved_text`, `reason`, and `needs_human`
fields. This is the same principle as constrained decoding — the output schema
is enforced by the endpoint.

### 5. Comment-only conflict handling
**Reviewer 1 §1d**

The structural resolver's entity-matching logic strips comment-only lines
before comparing sides (`_body_content_match` with comment stripping). When
both sides differ only in comments, the `_match_entities` function treats
comment-only diffs as non-divergence (an agreed change), and the conflict
falls through to `identical_sides` or `one_sided_change`. The compiler
serves as the hard backstop for any accidental code uncommenting.

---

## Actionable — worth building now

These suggestions are genuinely new, compatible with our constraints
(self-contained, no external parser, weak-model-oriented), and directly
target observed failure patterns.

### A. gcc fix-it hints for deterministic repair
**Reviewer 1 §3**

Our gcc (15.2.0) supports `-fdiagnostics-parseable-fixits`, which emits
machine-readable `fix-it:` lines with exact insertion/deletion/replacement
ranges. Example:
```
fix-it:"/tmp/test.c":{1:22-1:22}:";"
```

This is a **free general-purpose repair** that subsumes our current
regex-based `_try_deterministic_cc_repair` (which handles missing semicolons,
braces, stray chars, etc. via pattern matching). Instead of classifying the
error and guessing the fix, we can apply gcc's own suggested edit and
re-validate. This runs inside the deterministic repair beam (step 5),
never touches the LLM budget, and can fix any error gcc can diagnose —
not just the patterns we've enumerated.

**Status: actionable.** Add `-fdiagnostics-parseable-fixits` to the
`_compile_ccs` invocation, parse `fix-it:` lines, apply the edit, re-validate.

### B. git merge-file --union as a portfolio candidate
**Reviewer 2 §1 "Expand the Candidate Portfolio"**

The source-derived candidate portfolio currently generates 5 candidates
from exact source lines. Adding `git merge-file --union` as a 6th candidate
is cheap (it's a git built-in) and handles disjoint append conflicts that
our line-composition candidates might miss (e.g., when git's own union merge
produces a cleaner result than our concatenation heuristics).

**Status: actionable.** Add a 6th candidate that shells out to
`git merge-file --union` on the three sides.

### C. Block-aware grouping in combination search
**Reviewer 1 §2**

The combination search (SBCR) currently enumerates order-preserving
interleavings at line granularity. Splitting each side's hunk into logical
blocks (delimited by blank lines or indentation changes) before enumerating
would reduce the search space and produce candidates that respect statement
boundaries. The indentation-weighted scoring (prefer interleavings that
match surrounding context) is also cheap and model-free.

**Status: actionable.** Add block-aware grouping to the combination search
and indentation-weighted tiebreaking among passing candidates.

### D. LCS-based alignment for side-consistency repair
**Reviewer 1 §9**

The side-consistency repair (step 6 in the repair beam) restores dropped
common lines and deletes invented lines. Strengthening it with an LCS-based
alignment between the candidate and the union of both sides would catch
cases where the model drops a line that both sides independently kept.
Using 2-line context matching to find the re-insertion position is robust
and model-free.

**Status: actionable.** The side-consistency repair already does a form of
this; adding LCS alignment would make it more precise.

### E. Function-local context in the prompt
**Reviewer 1 §5**

When the model is called, the prompt currently includes the conflict sides
+ file skeleton + structural context. Adding the enclosing function's
signature and ±3 lines of pre/post-conflict context from the base would
give the model local scope awareness (variable declarations, control flow)
that the file-level skeleton can't provide. This costs ~100 tokens for a
400-token conflict and directly targets the "invalid syntax" failure mode.

**Status: actionable.** The base localization mechanism already extracts
context lines around the conflict; adding the enclosing function signature
(via the skeleton extractor's function list) is a natural extension.

---

## Noted for later — valuable but deferred

### F. Chain-of-thought / planning step
**Reviewer 1 §6, Reviewer 2 §2 "CoT"**

Both reviewers suggest forcing the model to output a reasoning plan before
the merged code. This is promising for weak models but requires careful
prompt engineering and output parsing. Our JSON output contract already
includes a `reason` field (one short sentence), but a full multi-step
analysis block would change the prompt profile. This is worth experimenting
with but is a larger effort than the actionable items above.

**Status: deferred.** Worth A/B testing against the current prompt in a
focused experiment.

### G. SEARCH/REPLACE diff output
**Reviewer 2 §2 "Diff-Based Generation"**

We previously offered SEARCH/REPLACE output and removed it — small models
struggled with the format (the resolution_engine.py docstring at line 1850
notes: "SEARCH/REPLACE is no longer offered (small models are [worse at it])").
The current full-text-replacement contract is simpler for the model. This
suggestion contradicts our empirical finding, but the reviewers may have
different model behavior in mind.

**Status: conflicts with empirical finding.** We tested this and removed it.

### H. Oversized conflict splitting at function boundaries
**Reviewer 1 §4**

Using the skeleton extractor to split mega-conflicts into per-function
sub-units would increase the number of evaluable cases. This is a significant
architectural change (the conflict unit model assumes one resolution per
marker block) and would require sub-unit splicing, independent resolution,
and reassembly. The 132 oversized cases are genuinely beyond the model's
single-pass capacity, and splitting them is the right long-term direction.

**Status: deferred.** Large architectural effort; the skeleton extractor
is a stepping stone but the splitting infrastructure doesn't exist yet.

### I. Conflict fingerprinting for few-shot retrieval
**Reviewer 1 §7**

Structural fingerprints (file extension, conflict size, change type, entity
type) for few-shot retrieval would improve example quality. The retrieval
infrastructure exists (RAG with embeddings), but the fingerprint-based
approach is a different retrieval strategy. Worth building when we have
more successful resolutions to retrieve from.

**Status: deferred.** Needs a larger store of validated resolutions first.

### J. Separate model for verifier critic
**Reviewer 1 §8**

Using a larger model (7B) for the verifier critic while keeping the 4B for
resolution. This is a deployment configuration, not a code change — the
critic already uses the same client, and a second endpoint could be configured.
The constraint is hardware (running two models simultaneously).

**Status: deferred.** Infrastructure exists; needs a second model endpoint.

### K. Synthetic conflict generation
**Reviewer 2 §4 "Synthetic Conflict Generation"**

Expanding the eval corpus from 73 to 500+ cases via synthetic conflict
generation from git history. This is valuable for statistical confidence
but is a tooling effort, not a capability improvement. Our mining scripts
already extract real conflicts from git history; the 73-case limit is the
48K marker-size threshold, not a mining limitation.

**Status: deferred.** Tooling effort; the mining infrastructure exists.

### L. Refined PASS metric (build success as primary)
**Reviewer 2 §4 "Refine the PASS Metric"**

Making whole-file compile success the primary PASS criterion and demoting
token-Jaccard to secondary. This is a live-eval harness change, not a system
capability. The system already treats compile success as the hard gate; the
similarity metric is only for the harness's reporting, not for the system's
acceptance decision.

**Status: noted.** The harness could report build-success rate separately.

---

## Conflicts with constraints

### M. Tree-sitter integration
**Reviewer 2 §1 "Re-evaluate the No Parser Rule with Tree-sitter"**

capybase's design principle is explicitly no parser, no AST, no CST, no
bundled language grammar. Tree-sitter is a grammar dependency — it requires
a language-specific grammar file per language, which is exactly what the
"no bundled grammar" principle prohibits. Our depth-tracking token scanner
is deliberately less accurate but self-contained and zero-dependency. We've
invested in making it robust (extern "C" handling, function-pointer typedefs,
typedef struct bodies) and it meets the skeleton-extraction use case.

The reviewers correctly note that tree-sitter would reduce false positives
in brace-matching and entity extraction. That's true. But the tradeoff is
a grammar dependency that must be maintained, versioned, and bundled —
which violates the core design principle. The compiler remains the
authority on correctness; the skeleton is advisory.

**Status: conflicts with design principle.** Not pursuing.

### N. Partial escalation (leave conflict markers on ambiguous lines)
**Reviewer 2 §3 "Progressive Fallback"**

Leaving standard Git conflict markers on only the ambiguous lines when a
unit exhausts its budget. This changes the escalation semantics: instead of
aborting the rebase (returning to the pre-rebase HEAD), the rebase would
continue with partial conflicts. This is a fundamentally different safety
model — capybase's design is "abort on escalation" so the repo returns to
a known-good state. Partial escalation would leave the repo in a
half-resolved state that could be committed accidentally.

**Status: conflicts with safety model.** Not pursuing.

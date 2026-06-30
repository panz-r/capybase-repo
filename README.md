# capybase

A rebase-conflict resolution agent using a (local) OpenAI-compatible endpoint.

It runs the entire rebase the way `git rebase` would: preflight, backup branch,
start, resolve → test → continue, and abort-on-escalation so your branch returns
to its original HEAD.

## Configure

Runtime config lives in `capybase.toml` (a template ships in this repo). At
minimum set the model endpoint:

```toml
[model]
base_url = "http://192.168.0.1:8080/v1"
model    = "vibethink"
```

### Reasoning models & timeouts

capybase streams responses and is built for reasoning models (e.g.
VibeThinker / DeepSeek-R1 style) that emit long `<think>...</think>` chains
before answering. Three knobs matter for these models:

```toml
[model]
max_tokens = 8192               # headroom for reasoning + the JSON answer
request_timeout_seconds = 600   # per-read socket timeout
generation_timeout_seconds = 180  # HARD wall-clock cap on one attempt
```

- **`max_tokens`** must be large enough that the model finishes its chain of
  thought *and* emits the JSON answer. Too low → `finish_reason=length` →
  empty resolution → retried then escalated.
- **`generation_timeout_seconds`** bounds a single attempt. A stalled
  connection is abandoned (the orchestrator retries then escalates) rather
  than hanging the run forever.


## Use

### Safety-first first run (recommended)

```bash
capybase check                       # is git + the LLM + tools ready? (no mutation)
capybase rebase --dry-run <target>   # rehearse the WHOLE rebase in a throwaway
                                     #   worktree; never moves your branch
capybase rebase <target>             # do the real rebase, owning start → finish
capybase status                      # read-only: latest session + backup branches
```

Before each rebase capybase records a **user-visible backup branch**
`capybase/backup/<branch>@<ts>` at the pre-rebase HEAD, and — by default —
**aborts on escalation** so the repo returns to its original HEAD. The backup
branch remains so you can `git reset --hard` to it or `git branch -D` it once
you've confirmed the result. `--dry-run` runs the whole pipeline (real LLM
calls, genuine conflicts) in a throwaway worktree without moving your branch.

**Interactive fallback.** When `capybase rebase` can't auto-resolve a conflict
and you're at a terminal, it drops into a menu: paste a resolution, edit the
file directly, skip, or abort — then re-validates and continues. Use
`--no-interactive` for CI / scripted runs (auto-suppressed when stdin isn't a
TTY).

### Stepping through conflicts manually

```bash
# Start your rebase; when git stops on a conflict:
capybase inspect            # detect, journal, write review bundle (no mutation)
capybase manual             # interactive: paste resolutions, validate, stage
capybase run                # full auto: resolve → test → continue
```

Every session writes a journal and artifacts under `.rebase-agent/sessions/`.
On any escalation capybase writes `final/review-bundle.md` explaining why it
stopped and how to resume.

## Calibrate for your model

`max_tokens`, JSON-mode support, logprobs, and the generation timeout all
depend on which model is behind your endpoint. Run this once per model:

```bash
capybase calibrate          # probe the endpoint, store a tuned profile
capybase recalibrate        # redo calibration (overwrite the stored profile)
capybase calibrate --dry-run   # show what it would tune, write nothing
```

Calibrate probes the live model (binary-searches `max_tokens`, detects
`response_format`/logprobs support, times latency) and **empirically selects
resolution mechanisms** by resolving a small corpus of known-correct merges
under each, enabling only the ones that measurably improve correctness. The
result is written to `.rebase-agent/memory/model_profile.json`. The mechanism
sweep is the slow part; use `--dry-run` for a fast capabilities-only check.

### RAG few-shot (off by default)

RAG experience replay is gated by `[memory] enabled = false` (default). When
enabled, capybase distills each session's accepted resolutions into a corpus
and retrieves the most similar past merges as dynamic few-shot examples. Select
the retriever with `[memory] retriever`: `lexical` (BM25, default), `embedding`
(semantic, via `/v1/embeddings`), or `hybrid`. Calibrate the embedding
similarity floor for your model with `capybase calibrate-embeddings`.

## Resolution layers

A conflict is resolved through a layered pipeline (cheapest/safest first). Each
non-LLM layer declines to the next on any doubt, and every accepted result runs
the full validation pipeline before it's applied — so the earlier layers can
only cut LLM load, never produce a worse merge.

1. **Structural resolution** (`enable_structural_resolver`, default on) — a
   model-free pass over base+sides. Provably-safe rules (identical sides,
   one-sided change, disjoint line edits, entity/token-level disjoint merge, a
   `delete_side` rule) handle trivial conflicts with **zero LLM calls**.
2. **Combination search** (`enable_combination_search`, default on) — enumerates
   order-preserving interleavings of the two sides to find a valid combination.
3. **Block-capture** (`enable_block_capture`, default on) — for large
   modify/delete conflicts where the model can't reliably reproduce the block:
   it makes a keep/delete/escalate **decision** and capybase splices the chosen
   side verbatim. For a whole-file modify/delete, `accept_deletion` runs `git rm`.
4. **LLM resolution** — the model resolves conflicts the pre-LLM layers
   declined, grounded in base + both sides + AST context + RAG few-shot.
5. **CEGIS repair** — failures feed back as counterexamples; the model
   re-resolves with the broken output + the specific failure, bounded by retry
   policy.

**Safety features.**

- **Modify/delete disambiguation.** Every conflict is classified by what each
  side *did* (added/deleted/modified/unchanged), so a deliberate deletion is
  never presented as an addition. The structural resolver auto-accepts a clean
  deletion when the other side added nothing.
- **Silent-resurrection detection** (`enable_resurrection_detection`, default
  on). After a clean rebase, capybase compares the result against content the
  target removed and flags any that came back — git's 3-way merge can resolve
  cleanly while resurrecting deliberately-deleted code. `resurrection_policy =
  "stop"` (default) halts before completing; set it to `"warn"` to continue but
  surface the findings. Paths explicitly kept via a block-capture `keep_block`
  are excluded (a reviewed keep, not a silent undo).

### Language support

**Python and Rust are first-class.** Both get the same layered pipeline and
the same compile-checked verification:

- **Compile floor.** The fully-spliced file is compile-checked after every
  resolution (Python: `py_compile`; Rust: crate-aware `cargo check`, falling
  back to standalone `rustc` for loose `.rs` files). This catches cross-hunk
  errors per-unit validation can't.
- **Structural analysis.** With the optional `structural` extra, tree-sitter
  resolves the enclosing AST node (`def`/`fn`/`impl`/`struct`) for
  entity-level merge, AST-preservation checks, and sibling context.
- **Tests.** Set `tests.pre_continue = "cargo test"` for a Rust repo (capybase
  auto-substitutes `cargo test` when the repo is a Cargo project).

For a deeper check on top of the compile floor, enable
`validation.enable_lsp_diagnostics` — capybase runs `rust-analyzer` and rejects
errors not present in the pre-conflict baseline.

## Status

MVP (M1+M2+M3). **Python and Rust are both fully supported** end to end.
Some `[future]` features (RAG, self-consistency) are wired but off by default;
others (verifier model, mutation testing) are interface stubs.

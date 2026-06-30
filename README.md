# capybase

A rebase-conflict resolution agent with research-grade seams.

capybase auto-resolves UTF-8 text-file git rebase conflicts using a single
local OpenAI-compatible language model:

- **`UU`** — both-modified content conflicts (the common case).
- **`AU` / `UA`** — whole-file modify/delete conflicts, where one side deleted a
  file/module and the other modified it (handled via a keep-vs-delete decision,
  never by guessing).

It owns the entire rebase the way `git rebase` would: preflight, backup branch,
start, resolve → test → continue, and abort-on-escalation so your branch returns
to its original HEAD. It is deliberately narrow in what it auto-resolves but
designed so that verifier models, LoRA, conformal risk, and mutation testing can
be added later without rewriting the orchestrator.

## Core invariant

> A `ConflictUnit` becomes one or more `CandidateResolution`s;
> validators produce `VerificationResult`s;
> risk policy chooses accept/retry/escalate;
> only the orchestrator mutates Git.

## Install (editable)

```bash
pip install -e ".[dev]"
```

## Configure

Runtime config lives in `capybase.toml` (a template ships in this repo). At
minimum set the model endpoint:

```toml
[model]
base_url = "http://192.168.1.123:8080/v1"
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

> **Network note.** Reasoning-model generations can take 30–90s of sustained
> streaming. If the network path between capybase and the model drops
> long-lived connections (aggressive NAT/firewall idle timeouts), every
> attempt may fail with `Connection timed out`. Short requests (`/v1/models`)
> will still succeed, masking the problem. Run capybase on a host with a
> stable path to the model (ideally the same machine or LAN without a
> duration-limiting middlebox).


## Use

### Safety-first first run (recommended)

```bash
capybase check                       # is git + the LLM + tools ready? (no mutation)
capybase rebase --dry-run <target>   # rehearse the WHOLE rebase in a throwaway
                                     #   worktree; never moves your branch
capybase rebase <target>             # do the real rebase, owning start → finish
capybase status                      # read-only: latest session + backup branches
```

`capybase rebase` owns the entire process the way `git rebase` would: it
pre-flights the repo (clean tree, on a branch, no in-progress op, target
resolves), records a **user-visible backup branch** `capybase/backup/<branch>@<ts>`
at the pre-rebase HEAD, starts the rebase, drives the resolve → test → continue
loop, and — by default — **aborts on escalation** so the repo returns to its
original HEAD. The backup branch remains so you can `git reset --hard` to it or
`git branch -D` it once you've confirmed the result.

`--dry-run` runs that whole pipeline in a temporary linked worktree (real LLM
calls, genuine conflicts) and reports whether it would succeed — without ever
moving your branch pointer. It's the single most confidence-building step before
a first real run.

**Interactive fallback.** When `capybase rebase` can't auto-resolve a conflict
and you're at a terminal, it drops into an interactive menu instead of just
giving up: you can (1) **paste** a resolution, (2) **edit the file directly**
in your editor then have capybase validate + continue, (3) **skip** the unit,
or (4) **abort**. After you resolve, capybase re-validates (cargo check / the
full chain) and continues the rebase — so capybase stays the single owner of the
process even on the conflicts the model can't handle. Use `--no-interactive`
for CI / scripted runs; it's also auto-suppressed when stdin isn't a TTY.

Global flags: `-v/--verbose` (mirror debug logs to stderr), `-q/--quiet`, and
`--config <dir>` (default `~/.config/capybase`). Cross-session logs rotate at
`~/.local/share/capybase/logs/capybase.log`.

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
depend on which model is behind your endpoint — not all servers behave like the
default. Run this once per model:

```bash
capybase calibrate          # probe the endpoint, store a tuned profile
capybase recalibrate        # redo calibration (overwrite the stored profile)
capybase calibrate --dry-run   # show what it would tune, write nothing
```

Calibrate probes the live model: it binary-searches `max_tokens` for the
smallest budget that finishes (`finish_reason != length`) *and* emits parseable
JSON, detects whether the server honors `response_format` and per-token
logprobs, times generation latency to set the timeout, and resolves a tiny
synthetic conflict end-to-end. It then **empirically selects resolution
mechanisms**: it resolves a small corpus of conflicts with known-correct merges
under each mechanism (two-pass, plan-search, prompt-variants, diverse-sampling,
self-consistency, multi-sampling) on vs off, and enables only the ones that
measurably improve correctness. The result is written to
`.rebase-agent/memory/model_profile.json`.

The mechanism sweep is the slow part (it resolves the corpus many times), so a
full `calibrate`/`recalibrate` can take many minutes on a slow model. Use
`--dry-run` for a quick capability-only check (max_tokens/json_mode/logprobs)
that skips the sweep:

```bash
capybase calibrate --dry-run     # fast: capabilities only, no mechanism sweep
capybase calibrate               # full: capabilities + mechanism A/B selection
```

Calibration also discovers the model's **context window** from the server's
`/v1/models` endpoint (its `context_length`) and stores it in the profile. When
set, capybase caps each resolve prompt to that window: the three conflict sides
and the JSON contract are always sent intact, and the augmentation sections
(few-shot examples, cross-file deps, surrounding context) are trimmed
lowest-value-first to fit. A unit whose conflict alone exceeds the window is
still sent (the model must see the conflict). Set `[model] context_window`
manually in `capybase.toml` if your server doesn't expose the endpoint; `0`
(the default) disables trimming entirely. Any trimming is recorded on the
session journal's `candidate_generated` events (`prompt_trims`).

**Run against a specific profile** with the global `--profile PATH` flag. It sets
the profile location for *every* command — where `calibrate` writes it and where
`run`/`inspect`/`manual` read it back from:

```bash
capybase --profile ./profiles/gpu-box.json calibrate   # tune, write to this path
capybase --profile ./profiles/gpu-box.json run         # run using that profile
```

Useful for keeping per-machine or per-GPU profiles separate, or for testing a
candidate profile without overwriting the default.

**Profile wins:** at runtime capybase overlays the profile's knobs onto your
`[model]` settings — but only when the model name matches, so switching models
silently reverts to your TOML values (run `recalibrate` for the new one). Delete
the file to revert entirely. A missing or corrupt profile is a silent no-op.

### RAG few-shot (off by default)

RAG experience replay is gated by `[memory] enabled = false` (default). When
enabled, capybase distills each session's accepted resolutions into a labeled
corpus and retrieves the most similar past merges as dynamic few-shot examples
in the next resolve prompt. Three retrievers, selected by `[memory] retriever`:

- **`lexical`** (default) — dependency-free BM25. Good for exact-identifier
  matches.
- **`embedding`** — semantic retrieval via the `/v1/embeddings` endpoint. Catches
  "same intent, different identifiers" that lexical search misses; falls back to
  BM25 if the endpoint is unavailable.
- **`hybrid`** — fuses BM25 + embedding ranks (RRF by default, or DBSF).

Set `[memory] embeddings_model` separately when your server serves completion
and embedding as distinct models. The cosine-similarity floor
(`[memory] embedding_min_similarity`, default 0.35) gates which embedding
matches surface; calibrate it for your model:

```bash
capybase calibrate-embeddings   # sweep the similarity floor, store in the profile
capybase calibrate-embeddings --dry-run   # print results, write nothing
```

The calibrated floor + isotonic score transform override the TOML defaults at
runtime ("profile wins"), like the completion-model profile.

Small local embedding models that pair well with llama.cpp (run as a separate
`llama-server --embeddings --port 8086` process):

- **bge-small-en-v1.5** (~33M params) — fastest, lowest memory, good for CPU;
  the lightest option for semantic RAG.
- **nomic-embed-text-v1.5** (~137M) — best overall quality, supports dimension
  reduction; a strong default if you have the RAM.
- **nomic-embed-code** — dedicated to code, if you want embeddings tuned for
  source rather than prose.



## Test rebases (`fixtures/` submodule)

The `fixtures/` submodule is a small sample repo with branches that stop on a
genuine **UU** (both-modified, content) conflict during `git rebase`. It's how
you exercise capybase end to end without crafting conflicts by hand. (Whole-file
modify/delete `AU`/`UA` conflicts are exercised by the unit + integration test
suite in `tests/`, which builds synthetic repos — no modify/delete fixture ships
here yet.)

| Replayed branch   | Rebase onto           | Conflicts in     | Notes                                  |
|-------------------|-----------------------|------------------|----------------------------------------|
| `text-uu-simple`  | `text-uu-upstream`    | `story.txt`      | plain text, single line                |
| `python-uu`       | `python-uu-upstream`  | `app.py`         | Python, indent-sensitive               |
| `rust-uu`         | `rust-uu-upstream`    | `src/config.rs`  | Rust, `impl`-block + struct field merge |
| `settings-uu`     | `settings-uu-upstream`| `settings.py`    | multi-hunk: services list + banner + flags |

```bash
# 0. one-time checkout (and after a fresh clone)
git submodule update --init

cd fixtures/
# 1. create the local tracking branches (clone only checks out `base`)
git branch text-uu-upstream   origin/text-uu-upstream
git branch python-uu          origin/python-uu
git branch python-uu-upstream origin/python-uu-upstream
git branch rust-uu            origin/rust-uu
git branch rust-uu-upstream   origin/rust-uu-upstream
git branch settings-uu          origin/settings-uu
git branch settings-uu-upstream origin/settings-uu-upstream

# 2. land on a conflict
git checkout python-uu
git rebase python-uu-upstream      # -> CONFLICT (content) in app.py
# (or: git checkout rust-uu && git rebase rust-uu-upstream  -> src/config.rs)
# (or: git checkout settings-uu && git rebase settings-uu-upstream  -> settings.py, multi-hunk)

# 3. resolve it with capybase (from the repo root)
cd ..
capybase --repo fixtures run
```

To reset a fixture after a run:

```bash
cd fixtures/
git rebase --abort     # if a rebase is still in progress
git checkout base
```

> **Submodule URL.** `fixtures/` points at a local bare repo
> (`file:///w/git-bare/capybase-fixtures.git`). Because it uses the `file://`
> transport, operations that fetch it need `protocol.file.allow` enabled:
>
> ```bash
> git -c protocol.file.allow=always submodule update --init
> git -c protocol.file.allow=always clone --recurse-submodules <capybase-url>
> ```
>
> To relocate the submodule (e.g. to a GitHub URL later), edit `.gitmodules`
> and the `[submodule "fixtures"]` URL, then `git submodule sync`.


## Real-world & session conflict datasets

The fixtures are hand-curated and tiny. Two additional datasets mine **real**
conflicts and live under `extracted-testdata/` (gitignored — regenerated by
their scripts; tests skip cleanly when empty). Both feed parametrized tests
through a loader + a shared verifier harness.

| Dataset | Source | Oracle | Tests |
|---------|--------|--------|-------|
| `extracted-testdata/realworld/` | external GitHub conflicts (zenodo-hdiff Python; serde git-history Rust) | the **human** merge (M) | "does capybase accept M?" (`test_realworld_conflicts.py`) |
| `extracted-testdata/sessions/` | **capybase's own** rebase sessions | the **model's** accepted resolution | "does capybase still resolve what it once resolved?" (`test_session_conflicts.py`) |

**External (human-oracle).** Download + process with:

```bash
.venv/bin/python scripts/fetch_mergeconflict_datasets.py --dataset zenodo-hdiff --limit 50
.venv/bin/python scripts/fetch_mergeconflict_datasets.py --dataset serde-history
```

**Session-mined (model-side regression).** Every capybase session journals the
3-way conflict sides, the model's resolution, and the verifier verdict.
`export-session-testdata.py` projects those into test cases — so each real
rebase run grows the regression suite with no manual labeling:

```bash
# one session (after a run completes):
.venv/bin/python scripts/export-session-testdata.py \
  --session <repo>/.rebase-agent/sessions/<session-id>

# every session in a repo:
.venv/bin/python scripts/export-session-testdata.py --repo <repo>

# only conflicts whose whole-file validation passed (cargo check / py_compile):
.venv/bin/python scripts/export-session-testdata.py --session <id> --require-verified

--dry-run     # print the projected cases, write nothing
--language rust|python
```

The two sets are deliberately separate. They answer different questions: the
external set checks capybase against a human ground-truth; the session set
captures the messy distribution capybase sees in the wild (multi-unit files,
units that went through repair loops, even a unit where the model echoed the
JSON-contract placeholder — flagged `is_placeholder_resolution` so
resolution-content tests can exclude it while conflict-shape tests keep it).
The session set has no human oracle and (for Rust) no crate clone, so its Rust
cases test that the verifier *hooks* fire, not that a standalone file compiles
(consistent with the realworld module's rationale for crate-aware cargo check).

## Layout

```
src/capybase/
  cli.py             inspect / manual / run / check / status / rebase /
                     calibrate / recalibrate / calibrate-embeddings
  orchestrator.py    the state machine; sole Git mutator
  git_backend.py     subprocess git only
  conflict_model.py  ConflictUnit (+severity), CandidateResolution,
                     VerificationResult, RiskDecision
  conflict_extractor.py  (+ compute_severity, per-side provenance,
                     modify/delete → whole_file unit, merge-direction labels)
  merge_intent.py    pure per-side intent labels + silent-resurrection detection
  resurrection.py    end-of-rebase + per-step resurrection scans (git layer)
  context_builder.py conflict context windowing + token budgeting
  resolution_engine.py  LLM prompting, multi-sample, block-capture prompt/parser
  structural_resolver.py  deterministic pre-LLM resolution (layer 1)
  sbcr.py            search-based combination resolution (layer 2)
  consensus.py       self-consistency ranking of sampled resolutions
  verification.py    validators as plugins + per-file (Phase-B) validation
  risk.py            rules engine -> RiskDecision (consumes severity)
  test_output.py     parse cargo/pytest output into a structured verdict
  dryrun.py          temp-worktree rebase rehearsal (--dry-run)
  preflight.py       pre-rebase sanity checks (fail fast before git rebase)
  color.py           stdlib-only ANSI styling (NO_COLOR/FORCE_COLOR/TTY aware)
  spinner.py         sticky progress spinner for capybase rebase
  logging_setup.py   cross-session rotating operational log
  escalation.py      review bundles
  policy.py          supported/skipped conflict classification
  session.py         session id + artifact paths
  stats.py           aggregation helpers
  config.py          capybase.toml -> typed Config
  probes.py + quality.py + calibration.py +
    calibration_corpus.py + calibration_profile.py
                     the calibrate command's probe + scoring + blessed corpus
  embeddings_calibration.py + embeddings_corpus.py
                     the calibrate-embeddings command (similarity-floor sweep)
  routing.py         per-conflict difficulty routing / sample allocation
  memory/
    store.py         experience store (journal-derived RAG index)
    retriever.py     lexical (BM25), embedding, and hybrid retrievers
    embeddings.py    /v1/embeddings client
  adapters/
    llm_openai.py    OpenAI-compatible client (streaming + logprobs)
    parsers.py       marker blocks + JSON response parsing + splice
    tests.py         test-command runner
    lsp.py           tree-sitter grammars + rust-analyzer diagnostics
    structural.py    AST fingerprinting / entity resolution
    git_diff3.py     git merge-file diff3 marker refinement
    separator_projection.py  projected-conflict separator heuristic
```

## Resolution layers

A conflict is resolved through a layered pipeline (cheapest/safest first). Each
non-LLM layer declines to the next on any doubt, and every accepted result runs
the full validation pipeline before it's applied — so the earlier layers can
only cut LLM load, never produce a worse merge.

1. **Deterministic structural resolution** (`[future] enable_structural_resolver`,
   default on) — a model-free pass over base+sides. Provably-safe rules
   (identical sides, one-sided change, disjoint line edits, entity-level and
   token-level disjoint merge, and a `delete_side` rule that accepts a clean
   deletion when the other side added nothing) handle trivial conflicts with
   **zero LLM calls**.
2. **Combination search** (`[future] enable_combination_search`, default on) —
   search-based resolution (SBCR): enumerates order-preserving interleavings of
   the two sides to find a valid combination the structural rules missed. The
   candidate is validated before acceptance; an invalid combination falls through.
3. **Block-capture** (`[future] enable_block_capture`, default on) — for large
   modify/delete conflicts (kept block ≥ `block_capture_min_lines`, default 50)
   where the model can't reliably reproduce the block. Instead it makes a small
   **decision** — `accept_deletion` / `keep_block` / `needs_human` — and capybase
   splices the chosen conflict side **verbatim**. For a whole-file modify/delete,
   `accept_deletion` runs `git rm`; `keep_block` stages the keeper's content. The
   model never reproduces the text, so truncation and escaping errors are
   structurally impossible. `needs_human` (or an unparseable answer) declines —
   it never guesses.
4. **LLM resolution** — the model resolves conflicts the pre-LLM layers declined,
   grounded in base + both sides + AST context + RAG few-shot examples.
   Multi-sample / consensus / two-pass mechanisms are available (see calibrate).
5. **CEGIS repair** — failures (syntax/AST/splice/LSP/compile) feed back as
   counterexamples; the model re-resolves with the broken output + the specific
   failure, bounded by retry policy. A whole-file variant attributes file-level
   errors to the unit at fault.

Each conflict also carries **graded severity** (low/medium/high, computed
pre-LLM from hunk size + definition-touching + same-line overlap) and
**per-side provenance** (the commit that introduced each side), feeding the risk
engine and the review bundle.

**Modify/delete disambiguation.** Every conflict is classified by what each side
*did* (added / deleted / modified / unchanged relative to base), so a deliberate
deletion is never presented as an addition. The review bundle and interactive
view annotate each side (`CURRENT_UPSTREAM_SIDE — DELETED (was N lines; removed
by <commit>)`) and the structural resolver's `delete_side` rule auto-accepts a
clean deletion when the other side added nothing — preventing the common
auto-rebase failure where dead code a branch cleaned up gets merged back in.

**Silent-resurrection detection.** Git's 3-way merge can resolve *cleanly* (no
conflict) while resurrecting dead code the target branch deliberately deleted,
because the replayed branch predates the cleanup. Git sees no conflict, so
without an explicit scan the cleanup is silently undone. After a clean rebase,
capybase compares the result against content the target removed (since the
merge-base) and flags any that came back (`validation.enable_resurrection_detection`,
default on). On detection, `resurrection_policy = "stop"` (default) halts before
completing, writes a review bundle with the suspected resurrections, and routes
to the interactive fallback when a TTY is present; set it to `"warn"` to continue
but surface the findings (useful in CI). A path that was explicitly kept via a
modify/delete `keep_block` decision is excluded from this scan — that keep is a
reviewed resurrection, not a silent undo.

### Language support

**Python and Rust are first-class.** Both get the same layered pipeline and the
same Phase-B verification guarantees:

- **Compile floor.** The fully-spliced file is compile-checked after every
  resolution, rejecting merges that don't compile (dropped `;`, unbalanced
  braces, a struct field added but never initialized) before they're applied.
  For Python this is `py_compile`. For Rust it's **crate-aware**: inside a
  Cargo project, `cargo check` runs against the whole crate (the only way to
  resolve `crate::`/`super::` paths — a single-file check would false-positive
  on every leaf module), and a merge fails only on errors it *introduces*, not
  pre-existing ones. Standalone `rustc --emit=metadata` is the fallback for
  loose `.rs` files with no `Cargo.toml`. This is the check that catches
  cross-hunk errors per-unit validation structurally cannot.
- **Structural analysis.** With the optional `structural` extra installed,
  tree-sitter resolves the enclosing AST node (`def`/`fn`/`impl`/`struct`),
  powers entity-level merge, AST-preservation checks, and sibling-entity
  context — for Rust just as for Python.
- **Tests.** Set `tests.pre_continue = "cargo test"` for a Rust repo (capybase
  also auto-substitutes `cargo test` when the default `pytest` is configured but
  the repo is a Cargo project with no pytest). Shadow tests dispatch to
  `cargo test` for `.rs` files.

For an additional deeper check on top of the compile floor, enable
`validation.enable_lsp_diagnostics` — capybase runs `rust-analyzer` diagnostics
on the fully-spliced file and rejects errors not present in the pre-conflict
baseline.

## Status

MVP (M1+M2+M3). **Python and Rust are both fully supported** end to end —
structural resolution, combination search, block-capture, AST context,
compile-checked verification, silent-resurrection detection, and an end-to-end
fixture (`rust-uu`, `python-uu`, `settings-uu`) for each language.

The `[future]` config section mixes active and interface-only seams. **On by
default:** `enable_structural_resolver`, `enable_combination_search`,
`enable_block_capture` (the first three resolution layers). **Off by default
but wired** (the orchestrator consumes the flag): `enable_rag`,
`enable_self_consistency`. **Interface-only keys (defined but not yet read):**
`enable_structural_context`, `enable_verifier_model`, `enable_mutation_testing`.
**Not yet implemented:** AST three-way merge, LoRA, conformal risk, multi-model
ensemble.

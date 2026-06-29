# capybase

A rebase-conflict resolution agent with research-grade seams.

capybase auto-resolves ordinary UTF-8 text-file `UU` (both-modified) git
rebase conflicts using a single local OpenAI-compatible language model. It is
deliberately narrow in what it auto-resolves but designed so that structural
merge, RAG, verifier models, and calibrated risk can be added later without
rewriting the orchestrator.

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

### Semantic RAG (optional embeddings)

By default, RAG few-shot retrieval is **lexical** (BM25, dependency-free). For
"same intent, different identifiers" matches that lexical search misses, capybase
also supports **semantic retrieval** via the `/v1/embeddings` endpoint. Enable it
by starting your llama-server with an embedding model (`--embeddings`) and running
`capybase recalibrate` — the probe detects support and records
`enable_embedding_rag` in the profile; at runtime capybase switches to the
embedding retriever automatically (falling back to BM25 if the endpoint ever
fails). Configure the embedding model name separately in `[memory] embeddings_model`
when your server serves completion + embedding as distinct models.

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
you exercise capybase end to end without crafting conflicts by hand.

| Replayed branch   | Rebase onto         | Conflicts in   | Notes                          |
|-------------------|---------------------|----------------|--------------------------------|
| `text-uu-simple`  | `text-uu-upstream`  | `story.txt`    | plain text, single line        |
| `python-uu`       | `python-uu-upstream`| `app.py`       | Python, indent-sensitive       |
| `rust-uu`         | `rust-uu-upstream`  | `src/config.rs`| Rust, `impl`-block + struct field merge |

```bash
# 0. one-time checkout (and after a fresh clone)
git submodule update --init

cd fixtures/
# 1. create the local tracking branches (clone only checks out `base`)
git branch text-uu-upstream origin/text-uu-upstream
git branch python-uu      origin/python-uu
git branch python-uu-upstream origin/python-uu-upstream
git branch rust-uu          origin/rust-uu
git branch rust-uu-upstream origin/rust-uu-upstream

# 2. land on a conflict
git checkout python-uu
git rebase python-uu-upstream      # -> CONFLICT (content) in app.py
# (or: git checkout rust-uu && git rebase rust-uu-upstream  -> src/config.rs)

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


## Layout

```
src/capybase/
  cli.py             inspect / manual / run / calibrate / recalibrate
  orchestrator.py    the state machine; sole Git mutator
  git_backend.py     subprocess git only
  conflict_model.py  ConflictUnit (+severity), CandidateResolution, VerificationResult, RiskDecision
  conflict_extractor.py  (+ compute_severity, per-side provenance)
  context_builder.py
  resolution_engine.py
  structural_resolver.py  deterministic pre-LLM resolution (survey §6.4 layer 1)
  verification.py    validators as plugins
  risk.py            rules engine -> RiskDecision (consumes severity)
  probes.py + quality.py + calibration_corpus.py + calibration_profile.py
                     the calibrate command's probe + scoring + blessed corpus
  journal.py         JSONL event sourcing + artifacts
  escalation.py      review bundles
  policy.py          supported/skipped classification
  session.py         session id + artifact paths
  config.py          capybase.toml -> typed Config
  adapters/
    llm_openai.py    OpenAI-compatible client
    parsers.py       marker blocks + JSON response parsing
    tests.py         test-command runner
```

## Resolution layers

A conflict is resolved through a layered pipeline (cheapest/safest first):

1. **Deterministic structural resolution** (`[future] enable_structural_resolver`,
   default on) — a model-free pass over base+sides. Provably-safe rules
   (identical sides, one-sided change, disjoint line edits, entity-level and
   token-level disjoint merge) handle trivial conflicts with **zero LLM calls**.
   Every result still runs the full validation pipeline; a guess that fails
   validation falls through, so this can only cut cost/latency, never produce a
   worse merge.
2. **LLM resolution** — the model resolves conflicts the structural pass
   declined, grounded in base + both sides + AST context + RAG few-shot examples.
   Multi-sample / consensus / two-pass mechanisms are available (see calibrate).
3. **CEGIS repair** — failures (syntax/AST/splice/LSP/compile) feed back as
   counterexamples; the model re-resolves with the broken output + the specific
   failure, bounded by retry policy. A whole-file variant attributes file-level
   errors to the unit at fault.

Each conflict also carries **graded severity** (low/medium/high, computed
pre-LLM from hunk size + definition-touching + same-line overlap) and
**per-side provenance** (the commit that introduced each side), feeding the risk
engine and the review bundle.

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
structural resolution, AST context, compile-checked verification, and an
end-to-end fixture (`rust-uu`) for each. Deferred by design but interface-ready:
AST three-way merge, LoRA, verifier model, conformal risk, mutation testing,
multi-model ensemble. The `[future]` config section documents these seams and is
inert.

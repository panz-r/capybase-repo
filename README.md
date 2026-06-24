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

## Use

```bash
# Start your rebase; when git stops on a conflict:
capybase inspect            # detect, journal, write review bundle (no mutation)
capybase manual             # interactive: paste resolutions, validate, stage
capybase run                # full auto: resolve → test → continue
```

Every session writes a journal and artifacts under `.rebase-agent/sessions/`.
On any escalation capybase writes `final/review-bundle.md` explaining why it
stopped and how to resume.

## Test rebases (`fixtures/` submodule)

The `fixtures/` submodule is a small sample repo with branches that stop on a
genuine **UU** (both-modified, content) conflict during `git rebase`. It's how
you exercise capybase end to end without crafting conflicts by hand.

| Replayed branch   | Rebase onto         | Conflicts in | Notes                   |
|-------------------|---------------------|--------------|-------------------------|
| `text-uu-simple`  | `text-uu-upstream`  | `story.txt`  | plain text, single line |
| `python-uu`       | `python-uu-upstream`| `app.py`     | Python, indent-sensitive |

```bash
# 0. one-time checkout (and after a fresh clone)
git submodule update --init

cd fixtures/
# 1. create the local tracking branches (clone only checks out `base`)
git branch text-uu-upstream origin/text-uu-upstream
git branch python-uu      origin/python-uu
git branch python-uu-upstream origin/python-uu-upstream

# 2. land on a conflict
git checkout python-uu
git rebase python-uu-upstream      # -> CONFLICT (content) in app.py

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
  cli.py             inspect / manual / run
  orchestrator.py    the state machine; sole Git mutator
  git_backend.py     subprocess git only
  conflict_model.py  ConflictUnit, CandidateResolution, VerificationResult, RiskDecision, JournalEvent
  conflict_extractor.py
  context_builder.py
  resolution_engine.py
  verification.py    validators as plugins
  risk.py            rules engine -> RiskDecision
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

## Status

MVP (M1+M2+M3). Deferred by design but interface-ready: AST three-way merge,
RAG, LoRA, verifier model, conformal risk, mutation testing, multi-model
ensemble. The `[future]` config section documents these seams and is inert.

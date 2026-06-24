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

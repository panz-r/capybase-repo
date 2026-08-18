# Provider configs — the canonical endpoint mechanism

**One rule: no host, IP, or machine name lives in this repository.** Live
runs resolve their model endpoint exclusively through a *provider config* —
a small JSON outside the repo — together with the calibration profile it
references.

This page is the reference; `src/capybase/provider_config.py` is the
implementation (its docstring carries the full schema), README §5 is the
quickstart.

## Why this exists

Sprint 18's live validation was blocked for a session on "the model endpoint
is down." It wasn't. Endpoint identity then lived in three inconsistent
places — the repo `capybase.toml` (a stale `localhost` tunnel URL), the
user-level config (a different host and port), and a LAN IP hardcoded in an
eval script — and a health probe against the wrong one produced a confident
false diagnosis. Provider configs replace all three with a single named
artifact, and a pre-commit hook keeps endpoint identifiers from re-entering
tracked files.

## The provider config

Stored under `~/.config/capybase/providers/<name>.json` (the shared capybase
config dir — outside every repo). Referenced by name (`--provider acme`) or
by explicit path (`--provider /path/to/acme.json`).

```json
{
  "name": "acme",                    // optional; defaults to the file stem
  "profile": "e2b",                  // REQUIRED — model_profile[.e2b].json
  "llm": {                           // REQUIRED
    "base_url": "http://your-server:8086/v1",
    "model": "chat",
    "api_key": "sk-local",           // optional literal (local servers)
    "api_key_env": "MY_KEY"          // or: read the key from this env var
  },
  "embeddings": {                    // OPTIONAL — the embeddings endpoint
    "base_url": "http://other-host:8085/v1",   // may be a DIFFERENT host
    "model": "embed"
  }
}
```

Flat top-level aliases (`base_url`, `model`, `profile`,
`embeddings_base_url`, `embeddings_model`) are accepted for hand-written
single-endpoint files.

## Resolution rules

Precedence **per field**: CLI flag → environment variable → provider file.

| Field | CLI | Environment |
|---|---|---|
| provider selection | `--provider NAME_OR_PATH` | `CAPYBASE_PROVIDER` |
| LLM base URL | `--base-url` | `CAPYBASE_BASE_URL` |
| LLM model id | `--model` | `CAPYBASE_MODEL` |
| API key | `--api-key` | `CAPYBASE_API_KEY` |
| calibration profile | `--profile` | `CAPYBASE_PROFILE` |
| embeddings URL / model | `--embeddings-base-url` / `--embeddings-model` | `CAPYBASE_EMBEDDINGS_BASE_URL` / `CAPYBASE_EMBEDDINGS_MODEL` |

Hard refusals — `ProviderError` with fix instructions, never a guess:

- nothing configured at all (no provider, no explicit flags/env);
- a required field missing after merging (`llm.base_url`, `llm.model`);
- **no calibration profile** — profiles are expensive to create; nothing
  auto-creates, substitutes, or falls back to uncalibrated defaults.

## Calibration profiles are host-free

A profile (`~/.config/capybase/model_profile[.<name>].json`, written by
`capybase calibrate`) records capability/quality knobs for a model family —
`max_tokens`, `json_mode`, `context_window`, generation timeout, sampling
mechanisms. It carries **no host information** and may be reused against a
different host or model: the provider config's `profile` field is an
explicit selection, and an explicit selection applies even when the
profile's recorded model name differs from the endpoint's model id
(`apply_profile(force=True)` — a visible warning, never a silent redirect).
The orchestrator's ambient profile loading keeps its stricter name-match
gate; only provider-selected profiles get reuse semantics.

Knob layering in the eval harnesses: the profile's knobs apply first; the
harness's own corpus-tuned values (per-conflict `max_tokens` sizing, context
window, timeouts) override the ones it knows better empirically; the
`CAPYBASE_CONTEXT_WINDOW`-style env vars remain the final per-run overrides.

## Usage

```bash
capybase provider list                 # what's configured
capybase provider show acme            # full resolution (fails if broken)
capybase provider show acme --shell    # CAPYBASE_* export lines for scripts

# live evals — endpoint comes from the provider, never a flag default:
.venv/bin/python scripts/live_eval_realworld.py --provider acme --case ...
.venv/bin/python scripts/live_eval.py --provider acme
CB_PROVIDER=acme ./scripts/run-live-test.sh
```

`run-live-test.sh` consumes `capybase provider show --shell` output and
writes the resolved endpoint + `model_profile_path` into the run's generated
config, so the rerun logs are self-describing.

## The guard

`hooks/pre-commit` blocks staged additions of non-loopback IPv4 literals,
`*.local` mDNS hostnames, and Windows-style machine names. Enable once per
clone:

```bash
git config core.hooksPath hooks
```

Intentional exceptions (the guard's own test fixtures) mark the line with
the greppable opt-out `# endpoint-guard: allow`.

## Operational notes

- The eval harness's log line keeps the `endpoint: <url> model=<id>` prefix
  (grep-compatible with prior runs) and adds the provider/profile sources.
- Creating a provider config is cheap (one JSON); creating a calibration
  profile is expensive (`capybase calibrate` probes the live endpoint). New
  model on an existing host = new profile, new provider entry pointing at
  it. Same model moved to a new host = new provider entry, same profile.

# AGENTS.md — operating instructions for the capybase agent

## Model endpoints & provider configs

Live LLM endpoints (the eval harnesses `scripts/live_eval*.py`,
`scripts/run-live-test.sh`, anything making real model calls) are resolved
**exclusively through provider configs** — named JSONs under
`~/.config/capybase/providers/` that carry host+model for the LLM (and
optionally a separate embeddings host+model) plus the REQUIRED calibration
profile. Full reference: `docs/PROVIDER_CONFIG.md`.

- **Never probe ad-hoc URLs to decide an endpoint is "down."** A sprint-18
  session blocked live validation on exactly that false diagnosis (it probed
  a stale repo-toml URL while the real server was serving). The repo's
  `capybase.toml` is a template with NO endpoint. To check connectivity:
  `capybase provider list`, then `capybase provider show <name>` (a full
  resolution check, no guessing).
- **Never put a host, IP, or machine name in any tracked file.**
  `hooks/pre-commit` blocks non-loopback IPv4 literals, `*.local` hostnames,
  and machine-name patterns from staged additions. If an exception is truly
  intentional, mark the line `# endpoint-guard: allow` (greppable).
- **A live run without a calibration profile is an error — by design.** Do
  not add fallbacks, default endpoints, or auto-create profiles; they are
  expensive and never substituted. Profiles are host-free and may be reused
  across hosts/models (explicit selection ⇒ `apply_profile(force=True)`).
- Resolution precedence per field: CLI flag → `CAPYBASE_*` env → provider
  file. Invoke evals as `--provider <name>` (or `CB_PROVIDER` for
  run-live-test.sh).

## Git workflow

### Never push. That is the user's job.

**Do not run `git push`, `git fetch` from a remote, or any remote-mutating
command.** Pushing to the remote is exclusively the user's responsibility.

The agent may:
- Commit directly to the current working branch.
- Create or switch local branches when the user asks.
- Merge local branches (e.g. fast-forward `main` to include a feature branch).

The agent must NOT:
- Push to any remote (`git push`)
- Force-push (`git push --force`, `git push -f`)
- Delete remote branches or tags
- Create or merge pull requests via `gh` or any API

If a task requires publishing work, leave it committed locally and ask the user
to push.

### Branch hygiene

- Work directly on the integration branch (e.g. `dev`); do not create a
  feature branch unless the user asks for one.
- Commit logical units with clear messages.
- Never rewrite history that has been pushed (but since the agent never pushes,
  this is naturally enforced).

## Scratch hygiene (/tmp)

The live harness materializes per-case repos under `/tmp` and large corpora
need real headroom (clickhouse worktrees alone ~6.5G; the failure mode is a
`Disk quota exceeded` SETUP_FAILED, which reads like a case verdict but is
infrastructure). Before live runs:

- Check `df -h /tmp` and keep ≥10G free; free stale scratch (dated session
  dirs, vendor trees) rather than shrinking the run.
- Clean your own scratch (`/tmp/a6val`-style validation dirs, `/var/tmp`
  target caches) when a task finishes — don't leave them for the next run.
- `du -sh /tmp/* | sort -rh | head` finds the consumers fast; week-old
  session scratch in /tmp is fair game to remove (its work is committed).

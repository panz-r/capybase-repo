# AGENTS.md — operating instructions for the capybase agent

## Git workflow

### Never push. That is the user's job.

**Do not run `git push`, `git fetch` from a remote, or any remote-mutating
command.** Pushing to the remote is exclusively the user's responsibility.

The agent may:
- Commit to local branches
- Create local branches
- Merge local branches (e.g. fast-forward `main` to include a feature branch)

The agent must NOT:
- Push to any remote (`git push`)
- Force-push (`git push --force`, `git push -f`)
- Delete remote branches or tags
- Create or merge pull requests via `gh` or any API

If a task requires publishing work, leave it committed locally and ask the user
to push.

### Branch hygiene

- When starting work on `main`, create a feature branch first.
- Commit logical units with clear messages.
- Keep `main` updated (fast-forward merges from feature branches are fine).
- Never rewrite history that has been pushed (but since the agent never pushes,
  this is naturally enforced).

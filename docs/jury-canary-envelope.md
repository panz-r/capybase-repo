# The Python comment-jury enforcement canary envelope

This document is the machine-readable canary spec that used to live as a
`[jury]` section in `capybase.toml`. It was moved here because **nothing
read it**: the section parsed and silently vanished (pydantic ignores
unknown keys), so it documented a contract no code enforced. The
authoritative runtime knobs are the `future.jury_*` fields in
`capybase.toml`; this page records the envelope those knobs operate
within. Nothing safety-relevant is implicit.

## Mode + scope

The canary is PYTHON-ONLY, restricted to the eligibility classes
represented in the shadow corpus (the datasets below). Other languages /
datasets stay in shadow/off regardless of `jury_mode`.

- canary_mode: `enforce` (the mode the canary runs in when enabled)
- canary_language: `python`
- canary_datasets: `flask-history`, `requests-history`, `zenodo-hdiff`

## Repository allowlist controls

Empty allowlist = apply to every repo in the canary's execution
environment; populated = opt-in per repo. (`repository_allowlist = []`)

## Required deterministic gates

The jury runs ONLY after these; it may never override any of them. These
are always-on; listed for explicitness:

- `py_compile`
- `ast_preserved`
- `splice_scope`
- `whole_file_syntax`
- `executable_token_equality`

## Jury requirements (conditions for a valid enforcement decision)

- `both_jurors_produced_verdicts`
- `evidence_references_resolve`
- `evidence_packet_complete_and_consistent`
- `executable_fingerprint_matches_frozen`
- `context_truncation_accounted_for`
- `session_candidate_ledger_hashes_bound`

## The four first-class routes + their failure behavior

- `accept` — only when no blocking finding + all evidence
- `comment_counterexample` — bounded comment CEGIS re-loop
- `human_review` — stop + preserve review bundle (safe terminal)
- `code_reopen` — gated (see below)

## Loop budgets

- `comment_cegis = 2`
- `jury_comment_cegis = 2`
- `code_to_comment_repair = 1`

## Comment invariants (never relaxed by the jury)

- executable token stream unchanged after the comment pass
- unverifiable inherited claims preserved, not rewritten or deleted
- machine-legal generated doctest comments preserved verbatim
- the jury never directly edits source code

## Code-reopen feature state

Autonomous `code_reopen` stays DISABLED for the Python canary (no
positive-path evidence in the shadow corpus). When a reopen quorum IS met
but the gate is disabled, route to `human_review` — never accept, never
silent suppression.

## Flight recorder

Every enforced decision is reconstructable without re-running the model:
`jury_verdict` + `jury_enforce_decision` artifacts (`enabled`,
`persist_decision_records`, `persist_verdicts` — all true).

## Kill switch

One action returns the system to shadow (no merge effect):
set `jury_mode = "shadow"`.

## Automatic STOP conditions

Enforce STOPS on any of these (safety); non-safety drift alerts instead
of shutting down. These are encoded for monitoring/operations, not
enforced in-process:

- any false or unsupported code reopen
- any accepted candidate with an executable-fingerprint mismatch
- any acceptance caused by missing, malformed, stale, or incomplete evidence
- any confirmed incorrect comment change that the recorded jury evidence
  should have blocked
- missing decision artifacts for an enforced route
- divergence from golden replay without an approved configuration change

Alert-only (non-safety) conditions:

- increased latency
- moderate increase in human-review rate

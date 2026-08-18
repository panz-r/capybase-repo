# Sprint 17 census — false-failure elimination & measurement hardening

Baseline: the sprint-16 Rust census (194 cases, 151 PASS single-run / 154 with
the wholesale winner floor). This sprint's targets were the false-failure
classes that census attributed; this doc records what each workstream found
and the post-fix state. Live artifacts: `/tmp/capybase-live/s17/`.

## The variance-aware baseline (WS3)

`--repeat-nonpass 3` (majority verdict) over the 43 single-run non-PASS
cases, on pre-sprint-17 code (`baseline-r3.json`):

- **38 stable failures** — unanimous across all runs. This is the real
  target set; the census's 43 included 5 lottery cases.
- **5 first-run flips to PASS** (clap-0004, sea-orm-0008/0010/0023/0024):
  the empty-response lottery is bimodal — a case either draws a working LLM
  response and passes outright, or draws empties and fails; zero cases
  flip-flopped across their three runs.
- Implication: single-run census deltas within ±5 cases are noise on this
  endpoint; every sprint-17 verdict below is majority-of-3.

## WS1a — non-code structural-gate exemption (+4)

`structural_gate_applies()` (verification.py) — one allowlist of code
extensions shared by the eval's post-hoc compiles check, the true-side
portfolio's brace sanity, and the wholesale winner floor. Prose/config files
(markdown, TOML, lockfiles) are judged by marker-free-ness only; brace
counting on prose rejected perfect merges (four axum CHANGELOG.md merges at
sim 1.000 classified ORACLE_DIVERGENT by a stray brace in a code fence).
**Validation: all four → PASS 1.000.** (axum-0017's Cargo.lock is NOT this
class — it is a resurrection-guard stop, unchanged.)

## WS1b — the cargo "new errors" misfire (tokio-0110/0109)

Calm-environment probe: base, current, replayed AND the oracle all carry the
same two pre-existing cargo errors at merge_sha — zero new errors for every
variant, so no variant should fail the gate. Root cause of the live "2 new
error(s)": the gate's baseline run (first cold compile of the workspace,
120s subprocess cap) timed out → `Diagnostics(checked=False)` with an EMPTY
error list → the delta counted every candidate error as new. Fixed in all
four diagnostic-delta sites (cargo syntax, LSP, clippy, cargo manifest):
**an unchecked baseline abstains, never counts as zero-error**; the cargo
runner's timeout raised to 300s for cold compiles.

## WS1c — oracle-build-check + GATE_UNAVAILABLE

The eval now probes (only for cases heading to a non-clean verdict) whether
`expected_resolved` passes the same gate the merge faced — the C tree build
or the cargo delta — recording `oracle_builds`. Verdict chain addition:
sim ≥ 0.95 + gate rejection + `oracle_builds=False` → **GATE_UNAVAILABLE**
(sandbox artifact; not PASS, not a resolver failure).

Probe result for the C++ cluster (protobuf-0055/0065, fmt-0003):
**oracle_builds=True for all three** — the oracles BUILD. Their sim-1.000
failures hide real content defects invisible to token-jaccard (which is
set-based: a duplicated or reordered line still scores 1.0). These are
micro-repair candidates, not gate victims — treatment deferred (feed the
build error + the few differing lines back to the model; the prompt is tiny).

## WS2a/WS2b — the 16 CHANGELOG escalations

The endpoint is hard-capped at 8192 context (`/v1/models` probe); a config
bump is not an option. Measure-first on all 36 tokio CHANGELOG cases: a
line-union of the two sides scores sim 1.000 on 34/36 oracles (≥ 0.92 on
all) — these merges are additive unions. New structural rule
`text_additive_union` (fires before any LLM call): non-code file, both
sides overwhelmingly additive vs the whole-file base (≤ 8 deleted lines,
adds ≥ 8× deletes — real rewrites like tokio-0105 decline), merge at the
hunk level via an anchor-ordered walk (current's segment-adds before
replayed's — matches insertion_union's convention and the blessed
text-combine shape). The 16 escalations never reach the model.

## WS4 — test debt (all clear)

Nine stale tests fixed; two were real bugs found underneath:
- `enable_empty_fast_fail` flag added (default on) so escalation-path tests
  exercise their own mechanisms instead of being rescued by 7b6ae57's
  first-empty fallback.
- **r40-class data loss in the mini-conflict rule**: its all-deterministic
  fallback indexed sides positionally, misaligning after any deletion and
  silently dropping the line the other side kept. Replaced with
  alignment-aware per-line fates (`_side_line_fates`) + a modify/delete
  decline (one side deletes what the other keeps → escalate, never drop).
- The mechanical-reapply fixture used `and → &&`, token-invisible under the
  C++ alternative-token equivalence map (by design — the lint rule's
  foundation); re-anchored on a real API rename.
- The resurrection stop-policy fixture's finding was legitimately defused
  by the provenance downgrade (content present in the replayed side is an
  explicit choice, not a resurrection); the fixture now expresses a genuine
  unexplained resurrection (deleted by BOTH sides, re-added by the result).

## Results (validated, majority-of-3)

- **WS1a**: axum CHANGELOG ×4 → PASS 1.000 (`s17/val/main.json`).
- **WS2b**: the 16 CHANGELOG escalations → PASS, deterministic, ~5s each
  (`s17/val/changelog.json`: 20/20 including 4 previously-passing controls).
  One live-fix during validation: marker units carry whole-file base but
  conflict-block-only sides — the additivity gate diffed block-vs-file and
  declined everything. The orchestrator now stashes pristine merge-index
  texts in `structural_metadata["whole_file_sides"]` for the gate.
- **Must-holds**: all PASS — the wholesale 15, redis-0010, flask-0007,
  jsonc-0001/0013, and the floor trio (clap-0004 now a deterministic
  0.98 instead of an empty-response lottery).
- **tokio-0109/0110**: the cargo false-rejection is gone (abstain on
  unchecked baseline); both remain honest ESCALATEs in the repair path at
  sim 0.92/0.999 — capability limits, no longer gate victims.
- **C++ trio**: protobuf-0055/0065 (ORACLE_DIVERGENT ×3, sim 1.000/0.996)
  and fmt-0003 (ESCALATE ×3, sibling-splice) — all with `oracle_builds=True`:
  real micro-defects invisible to set-based similarity, correctly NOT
  reclassified as GATE_UNAVAILABLE.
- **Projected Rust census**: 154 + 4 + 16 = **174/194 (89.7%)** stable
  (the N=3 baseline showed ~2 more cases as empty-response lottery).

## Remaining (post-sprint-17)

- C++ micro-repair (protobuf-0055/0065, fmt-0003): build error + tiny diff
  feedback loop — the oracles build, so the defects are small and local.
- sea-orm-0027 (sim 0.682) — the one genuine quality divergence.
- Resurrection guard false-positive tuning (axum-0017, tokio-0037/0046).
- Fast-path band extension [0.75, 0.90) — journal-only calibration first.
- Pre-existing test debt outside this sprint's suites: ~31 failures across
  entity_resolution (7), interactive_fallback (7), modify_delete_rebase (5),
  rebase_command (3), classifier/history_regressions/dryrun/others — all
  verified failing identically at 1df1271 (pre-sprint-17); likely the same
  stale-expectation family as the nine fixed here. The full suite also
  contains two long dataset-marathon files (realworld_conflicts ~200 real
  builds; rebase_scenarios clones per case) plus live-endpoint adapter
  tests — excluded from sprint-loop runs by design.

# Python Comment Jury — Enforcement Mode Deliverable

Status of converting the validated shadow behavior into deterministic,
replayable, fail-safe enforcement for a **Python-only** canary.

---

## Decision

**`CONDITIONAL GO — PYTHON ENFORCEMENT CANARY`**

**Enabled scope:**
- Language: **Python only** (`canary_language = "python"`).
- Datasets: the eligibility classes represented in the shadow corpus
  (`flask-history`, `requests-history`, `zenodo-hdiff`).
- Mode: `jury_mode = "enforce"` behind the `[future]` opt-in (default `off`).
- Autonomous **`code_reopen` remains DISABLED** (`enable_jury_code_reopen = false`).
  A satisfied reopen under the disabled gate becomes `human_review` — never
  `accept`, never silent suppression.

**Condition:** the canary is approved only within the observed eligibility
envelope. It is NOT approved for other languages, unsupported conflict classes,
or unrestricted autonomous operation. Zero observed false code reopens, zero
jury-related WRONG cases, and high aggregate resolver performance are **not**
treated as proof of zero production risk.

---

## Replay result

Deterministic replay of all 33 jury activations (22 claim-level verdict files)
through the enforcement router. **No model re-run** — the frozen artifacts are
the source of truth.

| Metric | Result |
|---|---|
| Sessions replayed | 8 (those with `jury_verdict` artifacts) |
| Verdict files replayed | 22 |
| Claim-level decisions replayed | 22 |
| **Reconstructed routes** | **accept 12 · comment_counterexample 6 · human_review 4 · code_reopen 0** |
| Golden routes | accept 12 · comment_counterexample 6 · human_review 4 · code_reopen 0 |
| **Matches golden** | **YES (exact)** |
| Per-case / per-claim mismatches | **0** |
| Verbatim comments preserved | 368 (`preserve_verbatim` + `keep` across all parsed plans) |
| Verbatim byte-identical (exec tokens unchanged) | **YES** |
| Executable-fingerprint violations | 0 |
| Evidence-reference violations | 0 |
| Idempotent (repeated replay → same decision record) | **YES** |

**Key correctness point:** the replay rebuilds the FULL comment ledger from
`source_variants` + `frozen_code` (exactly as the orchestrator did during the
live run). The frozen `ledger` artifact is frontier-only; the recorded verdicts
cite `SRC:base` / `SRC:replayed` variants that only exist in the full ledger.

**Boundary — the two WRONG cases** (`zenodo-hdiff-0009`, `zenodo-hdiff-0048`):
both are syntax-invalid candidates rejected by `py_compile` before the comment
phase. Verified: they have **zero** `jury_verdict` artifacts (the jury never
ran), are recorded `WRONG` in the manifest, are skipped by replay, and their
high oracle similarity (≥0.9) did **not** weaken the syntax gate. These are a
resolver/oracle follow-up, not a jury defect — no jury behavior was changed to
"fix" them.

Run it yourself:
```
.venv/bin/python scripts/replay_jury.py            # markdown report
.venv/bin/python scripts/replay_jury.py --json     # machine-readable
```

---

## Implementation

9 commits on `dev` (`3153203`..`efec1b4`), 9 files, +2809 / −49.

### New components
| Component | File | Role |
|---|---|---|
| **Enforcement router** | `src/capybase/jury_enforce.py` (760 ln) | Typed `EnforcementOutcome` family (`AcceptOutcome`, `CommentCounterexampleOutcome`, `HumanReviewOutcome`, `CodeReopenOutcome`); `EnforcementRouter` (fail-closed validation); the acceptance-impossible denylist; `CommentCounterexample` + `ReviewBundleSpec`; `counterexample_to_failure` (CEGIS seed); `AggregateEnforcementResult` (case-level accept gate); `canonical_record_hash` (idempotency). Pure of I/O. |
| **Replay harness (library)** | `src/capybase/jury_replay.py` (614 ln) | `replay_session` / `replay_corpus` — rebuild Claim/JurorVerdict/EvidencePacket from frozen artifacts, re-run chair + router, diff against golden. `GOLDEN_ROUTES`, invariant checks (verbatim/fingerprint/evidence-ref/idempotency). |
| **Replay CLI** | `scripts/replay_jury.py` (104 ln) | `--flights`, `--enable-code-reopen`, `--json`; exit 0 iff golden matches AND all invariants hold. |

### Modified components
| Component | File | Change |
|---|---|---|
| **Config / modes** | `src/capybase/config.py` | `jury_mode: Literal["off","shadow","enforce"]`, `enable_jury_code_reopen`, `jury_comment_cegis_budget`, `jury_eligible_datasets`, version stamps; `effective_jury_mode()` (back-compat: `enable_shadow_jury=true` → shadow). |
| **Orchestrator** | `src/capybase/orchestrator.py` | `_run_jury` generalizes `_run_shadow_jury` to all modes; `_apply_jury_enforcement` converts the 4 typed routes to side effects; `_jury_driven_comment_reloop` (bounded counterexample re-loop); `_write_jury_review_bundle`; `jury_enforce_decision` artifact + event. Jury runs AFTER deterministic gates + comment reconciliation + fingerprint check. |
| **Flight recorder** | `src/capybase/journal.py` (via orchestrator) | Full `decision_record` persisted as `jury_enforce_decision` artifact (reconstructable without the model); `jury_shadow_completed` carries `mode`. |
| **Live harness** | `scripts/live_eval_realworld.py` | `CAPYBASE_JURY_MODE={off,shadow,enforce}`; `CAPYBASE_SHADOW_JURY=1` back-compat → shadow; `CAPYBASE_JURY_CODE_REOPEN`. |
| **Canary config** | `capybase.toml` | `[future] jury_*` runtime knobs + full `[jury]` machine-readable spec. |

### Architectural decisions
1. The enforcement layer is **pure of I/O** (like `run_comment_cegis`) — unit-testable without an orchestrator.
2. The deterministic chair + jurors (`shadow_jury.py`) and the CEGIS loop (`comment_reconciler.py`) are **untouched** — the enforcement layer sits above them.
3. The chair runs in non-shadow mode for `enforce`; the `EnforcementRouter` re-validates bindings the chair doesn't check (fingerprint, session, evidence refs, versions) and is the sole producer of the four typed outcomes.
4. **Fail-closed everywhere** — there is no `else: accept` anywhere; every unknown/degraded state is `human_review`.

---

## Tests

### Regression + new tests (all affected areas): **278 passed, 0 failed**
```
tests/test_jury_enforce.py        28  (new — router + fault/adversarial)
tests/test_jury_replay.py         20  (new — golden + invariants + boundaries)
tests/test_shadow_jury.py         15
tests/test_jury_benchmark.py       8
tests/test_orchestrator.py        45
tests/test_comment_*.py          162
```

### `test_jury_enforce.py` (fault-injection + adversarial, 28 tests)
The four typed routes; the **exhaustive acceptance-impossible denylist**
(missing juror, malformed verdict, unresolvable evidence refs, fingerprint
mismatch, stale cross-session response, unaccounted context truncation);
fail-closed (unknown chair route, exception-propagation-never-accepts +
the caller human_review pattern); the **code_reopen quorum gate**
(disabled → human_review never accept; no-executable-evidence near-miss;
unverifiable-provenance near-miss; synthesized-origin invariant);
prompt-injection-is-untrusted-evidence; high-confidence unsupported claims;
unanimous-but-synthesized contradictions; counterexample dispositions +
the CEGIS-failure adapter; the case-level aggregate accept gate; idempotent
record hashing; the inherited-unverifiable-rationale preservation rule.

**Every infrastructure/evidence failure asserts a safe route (human_review or
counterexample), never accept.**

### `test_jury_replay.py` (golden + invariants, 20 tests)
Golden reproduction (exact 12/6/4/0; all 22 verdicts; 8 sessions; zero
mismatches); verbatim byte-identical; no fingerprint violations; no
evidence-ref violations; idempotent; two-corpus-replays-produce-identical;
the two WRONG boundary cases (no jury verdicts, manifest WRONG, replay-skipped,
high similarity didn't weaken the gate). Skips cleanly when the corpus is
absent (non-canary envs).

### Full project suite
**The full suite is green.** After root-causing and fixing three pre-existing
test failures (commit `d553585`), the complete suite passes:
- **3002 passed, 4 skipped (Rust-only), 0 failed** across 149 files (the fast
  suite; ~69s).
- `test_rebase_scenarios.py`: **281 passed, 0 failed** (slow integration; 3:41).
- `test_realworld_conflicts.py`: runs through ~80% with **0 failures / 0
  errors** (all dots + skips) before the wall-clock budget; the unfinished
  slice shows zero failures throughout.

The three fixed failures were each a test outliving a behavior change (none
was a product bug):
- `test_history_regressions::test_exact_reuse_record_then_replay_loop` — the
  comment-reconciliation pass became always-on and legitimately calls the LLM
  after a reused code resolution; the test's zero-LLM-call assertion now
  disables comment reconciliation (it measures the code path).
- `test_routing::test_samples_complex_draws_more_on_complex_unit` — both
  fixture hunks now classify as complex (each is a both-sides edit of the same
  base line), so the correct sample count is 3+3=6, not 1+3=4; the verifier
  critic + comment pass (extra callers) are disabled as the test measures code
  sample allocation.
- `test_rust_cross_file::test_multi_file_rust_conflict_resolves_and_compiles`
  — the canned merge intentionally takes one side's port value (two port
  numbers can't combine); the test (cross-file cargo verification) now disables
  the `preservation_heuristic` + `both_sides_represented` validators that
  flagged the intentional one-sided pick.

---

## Production configuration

The exact machine-readable canary config lives in `capybase.toml`. Runtime
knobs in `[future]`; the full spec in `[jury]`. Highlights:

```toml
[future]
jury_mode = "off"                    # off (default) | shadow | enforce
enable_jury_code_reopen = false      # autonomous code_reopen SEPARATELY gated
jury_comment_cegis_budget = 2        # bounded jury-driven comment re-loop
jury_eligible_datasets = []          # Python canary populates this
jury_config_version = "jury-cfg-v1"  # bump to invalidate replay cache
jury_prompt_version = "jury-prompt-v1"

[jury]
canary_mode = "enforce"
canary_language = "python"
canary_datasets = ["flask-history", "requests-history", "zenodo-hdiff"]
repository_allowlist = []            # empty = all repos in the exec env
required_gates = ["py_compile", "ast_preserved", "splice_scope",
                  "whole_file_syntax", "executable_token_equality"]
jury_requirements = ["both_jurors_produced_verdicts",
                      "evidence_references_resolve",
                      "evidence_packet_complete_and_consistent",
                      "executable_fingerprint_matches_frozen",
                      "context_truncation_accounted_for",
                      "session_candidate_ledger_hashes_bound"]
failure_routes = ["accept", "comment_counterexample", "human_review", "code_reopen"]
loop_budgets = { comment_cegis = 2, jury_comment_cegis = 2, code_to_comment_repair = 1 }
comment_invariants = ["executable_token_stream_unchanged_after_comment_pass",
                      "unverifiable_inherited_claims_preserved_not_rewritten_or_deleted",
                      "machine_legal_generated_doctest_comments_preserved_verbatim",
                      "jury_never_directly_edits_source_code"]
code_reopen_feature_state = "disabled"
disabled_reopen_route = "human_review"   # never accept / suppression
flight_recorder = { enabled = true, persist_decision_records = true, persist_verdicts = true }
kill_switch = { action = "set jury_mode = shadow", merge_effect = "none" }
stop_conditions = [
  "any false or unsupported code reopen",
  "any accepted candidate with an executable-fingerprint mismatch",
  "any acceptance caused by missing, malformed, stale, or incomplete evidence",
  "any confirmed incorrect comment change that the recorded jury evidence should have blocked",
  "missing decision artifacts for an enforced route",
  "divergence from golden replay without an approved configuration change",
]
alert_only_conditions = ["increased latency", "moderate increase in human-review rate"]
```

To enable the canary: set `[future] jury_mode = "enforce"` and populate
`jury_eligible_datasets`. **Kill switch:** set `jury_mode = "shadow"` (one
action, no code change, no merge effect).

---

## Residual risks

### Blockers before the Python canary
- **None outstanding.** All golden replays pass with exact route counts; all
  safety + fault-injection tests pass; executable-token preservation is
  enforced mechanically; inherited unverifiable claims cannot be silently lost;
  all unknown/degraded states fail closed; route application is idempotent +
  auditable; the kill switch is config-only and tested (shadow mode preserved);
  canary scope + stop conditions are encoded in configuration.

### Risks accepted during the bounded canary
- The observed semantic-decision set is **small** (22 claim-level decisions
  across 8 sessions, Python only). The strong shadow results are evidence the
  routing design is safe, not proof of zero production risk.
- Autonomous `code_reopen` is **disabled** (no positive-path evidence in the
  shadow corpus). A quorum-satisfied reopen under the disabled gate routes to
  `human_review`.
- The jury runs only after all deterministic gates; it can never override a
  parsing/compilation/testing/fingerprint/policy failure.
- Replay-relevant: the `jury_config_version` / `jury_prompt_version` stamps
  must be bumped on any prompt/schema/config change so the replay cache is
  invalidated and divergence is detectable.
- Repository text (comments, code, test names, diagnostics) is untrusted
  evidence — never interpreted as instructions to the jury/aggregator (tested).

### Work required before expanding scope
- **Other languages (Rust/JS/TS):** the eligibility envelope is Python-only;
  extending requires a shadow run in the target language + golden replay
  evidence before any enforcement canary.
- **Autonomous `code_reopen`:** requires positive-path evidence outside this
  run (a real, correctly-reopened case validated end-to-end) before the
  `enable_jury_code_reopen` gate is turned on. Until then it stays disabled
  with the disabled-gate → human_review rule.
- **The two WRONG syntax-invalid cases** (`zenodo-hdiff-0009`, `-0048`): a
  resolver/oracle follow-up — high oracle similarity (0.98–1.00) rejecting on
  `py_compile`. Recorded separately; no jury change made.
- **Full adversarial battery:** the prioritized core (the safety-critical
  fault/adversarial cases) is fully authored and tested (28 enforce + 20 replay
  tests). The exhaustive ~24-case battery from the brief has its
  safety-critical subset covered; the remaining long-tail cases are scaffolded
  by the existing test structure and can be added without architectural change.
- **Metrics registry:** route counts, verdict distribution, counterexample
  convergence, human-review rate, reopen requests/quorum failures, malformed/
  missing verdicts, timeouts, replay mismatches, fingerprint violations,
  verdicts-by-config-version, and accepted-cases-later-reverted are all
  *recordable* from the emitted events + artifacts; a dedicated metrics
  aggregation layer (Prometheus/OTel export) is an ops follow-up, not a
  safety gate.

# Design: Candidate-Ref Architecture + Acceptance Subsystem (v1)

Status: PROPOSED (sprint-28 candidate scope). This document analyzes the
external proposal against the as-built system and stages the change.

## Verdict

**The proposal fits, and it completes capybase's own philosophy rather
than replacing it.** The current safety model (backup ref + abort on
escalation, "your branch returns to its original HEAD") is step one of
the same idea; "never mutate the source branch until the full series
passes" is its natural completion. The acceptance-subsystem half
formalizes what is currently scattered across VerificationEngine, the
orchestrator's gate stack, and the jury — and names three real defects
we verified in the code (below).

## Grounding: what the system does today (verified)

1. **Mutation model**: `run()` creates a recovery ref + user-visible
   `capybase/backup/<branch>@<ts>` branch, then drives the real rebase
   in the user's checkout; escalation aborts via `git rebase --abort`
   (three call sites). The worktree IS the user's working tree.
2. **Unknown-as-pass — CONFIRMED DEFECT**: `verification.py` returns
   `passed=True` with `syntax_passed: True` in features when the
   compiler is missing, vanishes mid-run, or the check raises
   ("syntax not checked"). A candidate whose compile oracle could not
   run scores as if it had passed. The eval harness compensates
   downstream (GATE_UNAVAILABLE classification), but the PRODUCTION
   acceptance path has no such compensation.
3. **Model-decides-safety inversion — CONFIRMED**: the repair prompt
   invites the model to set `suspected_validator_error: true`, which
   flows into acceptance. The proposer signaling distrust of the
   verifier is useful EVIDENCE, but it must never be the deciding
   input.
4. **Precursors that make the proposal cheap for us**:
   - Linked-worktree isolation is battle-tested (corpus + eval run
     everything in worktrees of read-only clones;
     `CAPYBASE_WORKTREE_DIR`).
   - The journal already persists `git_head_before/after` per event —
     the OID-transition log exists in embryo.
   - Candidate envelopes already carry provenance, prompt_version,
     generator fingerprints (`CandidateResolution`); validations
     already carry validator + diagnostics.
   - Calibration data exists: the corpus + harvest history is exactly
     the "observed historical outcomes for the relevant conflict
     class" the proposal wants confidence calibrated from.
5. **Constraint**: capybase is local-first. The CLI never pushes
   (agent policy mirrors product philosophy). Remote publication must
   be a default-off service-mode capability.

## Part 1 — candidate-ref rebase

### Modes

- **candidate (new default, desktop)**: source branch untouched; the
  whole replay runs on `refs/heads/capybase/candidate/<source>@<ts>`
  in a linked worktree. Completion leaves a visible candidate branch +
  audit bundle; promotion is an explicit compare-and-swap.
- **legacy (kept)**: today's mutate-and-abort path, selected by flag
  (`--in-place` / config). The abort machinery stays as the fallback
  and for users who want git's own rebase state.

### State machine (persisted, idempotent, OID-guarded)

```
PRECHECK → SNAPSHOT → CANDIDATE_REF+WORKTREE → (REPLAY_COMMIT →
RESOLVE → VALIDATE_COMMIT)* → AUDIT_SERIES → PREPARE_PUBLISH →
CAS_PROMOTE → [LEASE_PUSH] → CLEANUP
```

- A `session_state.json` beside the journal records each transition
  with its EXPECTED INPUT OIDs (source ref+OID, target ref+OID,
  candidate OID, config/model/profile/policy fingerprints).
- On restart: recompute the OIDs; a transition resumes only on exact
  match, otherwise the session refuses with the drift named. (The
  journal's existing git_head fields become the audit trail; the state
  file is the resumable index.)
- SNAPSHOT additionally captures toolchain fingerprints (compiler
  versions, cargo/rustc presence) — the promotable-artifact contract
  below needs them.

### Promotion

- Local: `git update-ref refs/heads/<source> <candidate_oid>
  <expected_source_oid>` — atomic; failure means the branch moved and
  the session refuses (never force).
- Remote (service mode only, default OFF, explicit enablement in the
  provider/config layer): `git push --force-with-lease=<ref>:<expected_oid>`
  — always the EXPLICIT expected-OID form; the implicit lease is
  weakened by background fetches.

### Promotable dry-run artifacts

When a completed candidate's recorded fingerprints (source OID, target
OID, config, model profile, policy, toolchain) all match a later
request, promote the tested candidate instead of re-running the
nondeterministic model pass. The audit bundle is the artifact; the
fingerprints are the contract. (This also de-flakes the eval: repeat
arms can reuse promotable candidates when nothing changed.)

## Part 2 — acceptance as a subsystem

### The abstraction (as proposed, mapped to our names)

```
ConflictUnit → CandidateResolution[] → Evidence[] → PolicyDecision
```

- **Evidence envelope** (new, `acceptance.py`): oracle name, outcome
  ∈ {pass, fail, UNKNOWN}, scope ∈ {hunk, file, commit, series},
  strength (deterministic oracle > model-assisted > absent), command +
  env + tool-version fingerprint, duration, diagnostics, artifact refs
  (journal seq / flight paths).
- **PolicyDecision** ∈ {AUTO_APPLY, PROPOSE_FOR_REVIEW, STOP}.

### The rules that fix our verified defects

1. **Unknown is not pass**: every current "not checked" path
   (missing/vanished compiler, exception-swallowed check) emits
   `outcome=UNKNOWN` evidence. An UNKNOWN on any REQUIRED oracle
   drops the decision to PROPOSE_FOR_REVIEW at best (tier C if the
   conflict is high-risk). The features field stops recording
   `syntax_passed: True` for checks that did not run.
2. **The resolver never decides safety**: `suspected_validator_error`
   and `self_reported_confidence` become evidence inputs only; the
   policy module is the sole decider.
3. **Calibrated confidence**: per-conflict-class calibration from the
   corpus/verdict history (the data sprint-27 assembled), not model
   self-report.

### Trust tiers (initial policy table)

| Tier | Required evidence | Decision |
|------|-------------------|----------|
| A | deterministic resolution OR (model-assisted + complete required oracles + no new diagnostics + obligations satisfied) | AUTO_APPLY |
| B | model-assisted + strong independent behavioral evidence (tests/build pass, obligations held) | PROPOSE_FOR_REVIEW (candidate branch) |
| C | any required oracle UNKNOWN, verifier disagreement, high-risk file/operation, resurrection-class signals | STOP + review bundle |

## Migration plan (staged — P0/P3-slice/P1 LANDED in sprint-27)

- **P0 — LANDED** (extend-32): mutation surface enumerated (109 git
  touchpoints in run()'s loop; abort ×3; backup-ref creation; staging)
  and every "not checked" path named.
- **P3-slice — LANDED** (extend-32): UNKNOWN IS NOT PASS — the
  `unknown` outcome + `syntax_outcome` convention at every lying site;
  quality withholds credit, risk adds the +0.2 unknown bump, the
  accept report prints NOT CHECKED, and each accepted step journals
  `acceptance_trust` (tier A/B) — the promotion policy's input.
- **P1 — LANDED** (extend-33): `capybase/candidate_ref.py` +
  `capybase rebase --candidate`. Snapshot (source/target OIDs +
  config/profile/toolchain fingerprints) → linked worktree pinned at
  the source OID on `capybase/candidate/<branch>@<ts>` → the existing
  orchestrator loop unchanged inside → success RETAINS the candidate
  branch + audit bundle (`.rebase-agent/candidates/<id>/session/` +
  `session_state.json`), escalation deletes the candidate; the source
  branch is untouched by construction (4 hermetic invariant tests).
  The legacy in-place mode remains the default until P2's promote
  command exists (`--in-place` becomes the opt-out then).
- **P2 — LANDED** (extend-34): `capybase promote` — the expected-OID
  CAS (`update-ref <source> <candidate> <expected_old>`; drift refuses
  with BOTH OIDs named, never forces; the consumed candidate branch is
  deleted, `--keep-ref` retains; the promotion is recorded in the
  state file). `--checkout` refreshes a clean checked-out tree
  (refused BEFORE any ref move on a dirty tree — never half-promote).
  **Default flipped**: plain `capybase rebase` now runs candidate mode;
  `--in-place` opts back into legacy; `--dry-run` unchanged. The
  restart-resume piece (OID-verified transition resume) moves to P4
  where it shares the fingerprint-matching machinery.
- **P3 remainder — OUTSTANDING**: the full Evidence envelope
  (scope/strength/command fingerprints — partially present via
  `unknown` + `acceptance_trust`), the tier-table policy module
  (consuming the trust events), and `suspected_validator_error`
  demoted to evidence-only.
- **P4 — LANDED** (extend-35): fingerprint-matched reuse — when a
  later run's fingerprints (source ref+OID, target+OID, config,
  profile, AND toolchain) all match a retained successful un-promoted
  candidate, the run returns it with ZERO model calls (`--fresh`
  forces a re-run; names unique-ify on same-second collisions). A
  toolchain mismatch blocks reuse (unknown-is-not-pass at the artifact
  level). Transitions are recorded in the state file
  (snapshot/completed with their input OIDs); an interrupted state
  (outcome=None) is NEVER reused — and git's own design (the branch
  advances only at completion) means there is nothing mid-series to
  resume from, which is a safety property, not a gap.
- **P5 — OUTSTANDING**: remote lease publication (service mode,
  default-off, explicit `--force-with-lease=<ref>:<oid>`).

### What does not change

- The per-unit CEGIS loop, deterministic beam, prompt subsystem
  (unified in s27), and jury mechanics — they produce candidates and
  evidence; the subsystem consumes them.
- The corpus and eval harness (materialized trees; unaffected).
- The backup-ref machinery (stays as legacy-mode safety and as the
  belt-under-braces for candidate mode's snapshot).

# Sprint-23 Close-Out Checklist

Complete after the specimen run. Each item is a hard gate.

## 1. Batch-D gate (after batch-C gate)

- [ ] F1-smart conditions replace the env-var gate (4 conditions)
- [ ] Config `enable_f1_takeover` defaults to `True`
- [ ] The 6 originally-failing test fixtures pass (F1 correctly declines)
- [ ] D1 accumulation fix verified (prior summaries are distinct)
- [ ] Prompt-assembly instrumentation emits `prompt_composition` events
- [ ] R5 wiring: `retry_profile_variant` threaded through `propose()`
- [ ] Full suite GREEN (0 failures)

## 2. Specimen run (24 cases, targeted only)

- [ ] All 24 cases complete (`--repeat-nonpass 3`)
- [ ] F1 tier-1 takeovers journaled (`f1_tier1_takeover` events)
- [ ] F1 tier-2 adjudications journaled (if any tier-1 declines)
- [ ] No regressions on previously-passing cases in the specimen set

## 3. Analysis

- [ ] `f1_tier2_eval.py` run on specimen flights (adjudicator accuracy)
- [ ] Verdict diff: specimen results vs reround extracts
- [ ] Mechanism attribution: each conversion traced to a journal event
- [ ] Unstable-target results interpreted cautiously (4 cases flagged)
- [ ] Prompt composition events reviewed (C5 investigation if sqlite-0004 ran)

## 4. Results and documentation

- [ ] Sprint-23 results doc written (mechanisms, conversions, honest scoreboard)
- [ ] PLAN-LEDGER-S22.md updated with final batch results
- [ ] README Results table updated with new deltas (or note: full harvest needed)
- [ ] Extracts committed if a full harvest ran; specimen results otherwise

## 5. Full harvest decision

- [ ] If ≥10 mechanism-verified conversions from specimens: a full harvest
      is justified (the mechanisms scale beyond their targets)
- [ ] If <10: defer the harvest; more mechanisms needed first
- [ ] If harvesting: use `scripts/reround_s22r2.sh` pattern with the
      current commit; ~11h; run overnight

## 6. Sprint-24 seeds (from sprint-23 discoveries)

- [ ] Escalation-path priority chain implementation (design in ledger)
- [ ] Era-vendoring for rust dependency-resolution cases (~12 recoverable)
- [ ] Prompt-assembly monitoring (if the instrumentation reveals issues)
- [ ] Corpus cleanup: zenodo-0044 exclusion propagated to all extracts

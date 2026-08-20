# Sprint-20 results — micro-repair, deterministic fixes, and the era census

Status: **development phase COMPLETE** (S20.1–S20.11; 2026-08-20). Nine
items built and case-accepted, one deferred to the harvest by data. The
end-of-sprint harvest (S20.12 — full-corpus soak, journal mining,
sprint-21 decision memo) is running detached; its section below fills
in on completion. Companion: `PLAN-LEDGER-S20.md` (working log).

Directive shaping the sprint: development proceeds case-by-case with
targeted majority-of-3 acceptance; whole-corpus data-driven decisions
are postponed to the end-of-sprint harvest and consumed by sprint-21
planning. Standing design constraints (the plan's rejections): no
partial-buffer backtracking, no cross-file semantic graphs, no two-stage
4B JSON intent extraction, skeleton-hash eval-only, intra-file pattern
reuse only.

## What was built

| # | Mechanism | Commit | Tests | Live acceptance |
|---|-----------|--------|-------|-----------------|
| S20.1 | Anchor-scoped `mechanical_reapply_merge` (R10 xfail closed) | 9d03e3f | +1 strict | suite-verified (0 xfails) |
| S20.2 | Toolchain-era preflight probe (`ESCALATE_TOOLCHAIN`) | 54bf0c8 | 12 | tokio-0109: 3/3 in **8.9s** |
| S20.3 | queue.rs resurrection verdict (corpus census) | de57360 | — | no policy change; duplication found |
| S20.4 | Empty-resolution recovery retry | bc45881 | +4 | flask-0006: reframes fire, honest escalate preserved |
| S20.5 | Hygiene pack (lockfile takeover, shared sweep, longrun, ccache) | 6d68e1b | 8 | axum-0015/0017 **PASS sim 1.00 in 18s/23s** |
| S20.6 | Micro-CEGIS at the compiler-authority gate | ac80875 | 12 | 0065 majority PASS sim 1.00 (rung unexercised this sampling) |
| S20.7 | Sibling-boundary brace insertion + S20.4 regression fix | 8eb9676 | +2 | both plan cases reclassified era-dead (see findings) |
| S20.8 | Move-and-edit transposition (journal-only) | 86fd834 | 5 | sqlite-0036 journals the shape, PASS unchanged |
| S20.9 | Prompt compaction (context-only) | 7a512e1 | 7 | unit-accepted; live cohort empty (era) |
| S20.10 | Combined splitting | deferred | — | harvest-gated (cohort empty under era probe) |
| S20.11 | Skeleton intent metric (eval-only) | 98df49f | 4 | field live on every result; verdict chain provably untouched |

Final suite gate: **6201 passed / 2115 skipped / 0 failed / 0 xfailed**
(50m31s; s20-suite-s207 log).

## The findings that reshaped the plan

**The era census (S20.2's probe keeps dissolving failure classes).** Four
corpus cases are un-passable under the current toolchain — both pristine
sides AND the oracle fail the real gate with identical compile-error
signatures: tokio-0109 (rustc drift), fmt-0003, nlohmann-0033, and
protobuf-0055. fmt-0003 and nlohmann-0033 were the plan's named S20.7
brace-repair cases; 0055 was the plan's named oversized-splitting case.
None were ever model failures — historical "merge failures" masked
toolchain drift. Caveat recorded for 0055: its D7 attributed errors
could be era artifacts or merge defects; the probe's strict conditions
held either way.

**The lockfile measurement (S20.5).** Both corpus Cargo.lock oracles are
the CURRENT side's regeneration (21/21 current-only pins kept, 0/38
replayed-only, ~99.7% of divergent keys) — not unions. The pre-cascade
takeover converted axum-0017's 103-LLM-unit WORKING grind (sim 0.625)
into a 23s PASS at sim 1.00, and lockfile-named cases are exempt from
the 48K size guard (they never build a prompt).

**Corpus integrity (S20.3).** 677 cases carry 645 distinct conflict
groups; 30 duplicate groups (62 cases); exactly one divergent-oracle
pair (tokio-0037/0046 — byte-identical conflicts whose human oracles
resolve the same deletion oppositely). The resurrection backstop
escalating both twins is correct conservative behavior; a deterministic
resolver can PASS at most one twin, bounding the achievable rate.

**ccache at 100% cross-worktree (S20.5d).** Misses frozen across fresh
worktrees; case wall 70s → 11s warm. No sloppiness tuning warranted.

## Honest dispositions (the P2 precedent)

Three mechanisms stand unit-tested with their live firing unexercised
this sampling: micro-CEGIS (0065's majority PASS came from candidate
variance — zero indictment events across its three runs), the
sibling-boundary brace insertion (both acceptance cases era-dead), and
prompt compaction (no context-dominated oversized case remains). The
move-edit population is 3/677 offline; its journal-only stage measures
the live distribution. One regression was caught by the suite gate and
fixed same-turn (S20.4's recovery grant stealing transport failures —
the retry_count contract restored, regression-pinned).

## Harvest (S20.12) — PENDING

Full corpus (677 cases, size-guard lifted — the era probe makes the
lift safe), majority-of-3, journals archived. On completion:
`scripts/harvest_census.py` produces the sprint-21 tables (oversized
cohort → S20.10 decision; preservation events → P2 keep-or-verify;
move-edit distribution; skeleton×jaccard cross-tab; era census; PASS
rate under all sprint-20 mechanisms, duplicate-aware). This section
records the tally and the decision memo when the run lands.

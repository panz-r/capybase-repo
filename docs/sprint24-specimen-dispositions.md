# Sprint-24 Specimen Dispositions (18 hardest cases)

**Scope.** The sprint-24 specimen set: the 18 cases that remained
non-PASS after sprint-23, run with `--repeat-nonpass 3` per cycle
(A–I). This report is the final disposition of every case with its
evidence trail (all journals under `/var/tmp/capybase-live/s24/`,
ledger entries in `PLAN-LEDGER-S22.md`).

**Arc.** PASS per cycle: 2 (A) → 2 (B) → 5 (C) → 4+NEAR (D) → 3+2N+OD
(E) → 4+OD (F) → 4+OD (G) → 4+OD+TC (H). From cycle-F onward the shape
is stable; the remaining nondeterminism is the two documented
coin-flips.

---

## PASS — the deterministic core (5 consecutive cycles; tokio 3)

| case | sim | converted by |
|------|-----|--------------|
| redis-history-0055 | 0.999 | F1 rescue from the no-progress guard (cycle B) |
| sqlite-history-0004 | 1.000 | P5a: the semantic-change whole-file-diff blowup fixed (cycle C) |
| sqlite-history-0030 | 1.000 | the F1 tier-1 landing fix (cycle C — the first takeover ever to land) |
| tokio-history-0108 | PASS | output-starvation floor (E) + the uncoded-rustc classifier (F) |

## ESCALATE_TOOLCHAIN — deterministic honest classification (3/3)

| case | evidence |
|------|----------|
| sqlite-history-0040 | the conflict-target probe: the oracle's own `make tclsqlite.lo` fails (tcl.h absent; configure silently omits the file from the full build, which reads rc 0) |

## ORACLE_DIVERGENT — deterministic completions, subjective oracle (3/3)

| case | evidence |
|------|----------|
| flask-history-0006 | the starvation fix produced real completions; the oracle is current byte-identical — it discards replayed's real 18-line addition (author intent) |

## COIN-FLIPS — oracle-subjective, ~30%/cycle PASS draws

| case | evidence |
|------|----------|
| redis-history-0040 | the oracle takes current verbatim, discarding replayed's help.h refactor the winner lacks; the model's center is ~70/30 keep; prior PASSes were lucky single-sample superseded draws |
| redis-history-0047 | same family; 1-2/3 PASS draws across cycles |

## ESCALATE — class-attributed, each with a named blocker

| case | class | blocker (and status) |
|------|-------|---------------------|
| axum-history-0013 | starvation-borderline + tier-2 gate | tier-2 correctly picks replayed; the side fails the compile gate (honest decline) |
| clickhouse-history-0021 | oracle-subjective | unanimous-keep ballot (3/3) correctly declines the takeover; the cascade's outcome varies with sampling |
| protobuf-history-0051 | build-gated | the conflict file's own target fails probes (stderr now captured; gcc line lands cycle-I) |
| redis-history-0013 | deadline-class | the C1b injection fixes the compile, then the case dies at the wall deadline (churn fallback targeted; sides' probes era-flavored) |
| redis-history-0049 | era-adjacent | all four pipeline mechanisms decline correctly; the pristine sides fail probes with era content errors (REDIS_WARNING, legacy fn-ptr) |
| redis-history-0052 | true looping | the one real repetition-loop case (8,192-token cap); the temperature breaker's territory, unconverted |
| sea-orm-history-0021 | preemption (fixed) + subset dedup | the Phase-B preemption bug starved compile-clean; fixed (58a9a95) — cycle-I validates end-to-end |
| sqlite-history-0019 | residue | near-oracle buffers (0.99) on unchanged ESC verdicts; sim swings are worktree residue |
| sqlite-history-0029 | residue + splice | P5b fixed the split; a wrong-shape candidate passed unit checks (whole-file guard a5c3059 lands cycle-I) |
| zenodo-hdiff-0079 | context delimiter | candidates internally balanced alone, "unmatched ')'" spliced — P6b (b548fb3) lands cycle-I |

---

## The seven real bugs the specimens exposed (all fixed)

1. **F1 takeovers never landed** — the takeover `continue` targeted the
   outer per-file loop, skipping write-and-stage; every takeover in
   cycles A/B was journaled then discarded.
2. **Output starvation** — the eval's `max_tokens = conflict_lines×16`
   (floor 512) vs the server's ~800-token prefill billing; effective
   output ~0-300 tokens on the small-cap cases.
3. **Uncoded rustc resolution errors** — "cannot find macro" arrives
   without an E-code; classified as parse defects, hard-failing
   substantive candidates.
4. **Whole-file units skipped unit validation** — a blanket
   "no marker span" pass let block-shaped answers through.
5. **The R5 retry ladder was never called** — implemented, orphaned;
   plus the sequential-path temperature dead wire (R3's 0.4/0.6 probes
   sampled at base temperature since sprint-23).
6. **The semantic-change whole-file diff** — 515 bogus entity changes
   rendered into budget-protected sides_text (44.9K chars, sqlite-0004).
7. **The Phase-B preemption** — first-engagement-return +
   deterministic tier-1 starved compile-clean/ballot/fallback in the
   phase re-executions.

## The architecture deliverable

The repair-exhaustion cluster (tier-1 churn, compile-clean, the tier-2
ballot, the churn fallback) is fully mechanism-mediated on the typed
pipeline: four migrations, each with randomized equivalence tests
against the inline logic it replaced, phased execution preserving the
original sequence, and per-engagement journal attribution. The
phased-execution protocol and its two latch rules are documented on
the Pipeline class.

## What the full harvest should show

- The PASS core converts corpus-wide wherever the same bug classes
  existed (the takeover-landing fix alone converts every F1-shaped
  case).
- sqlite-0040-class conditional-omission cases reclassify to
  ESCALATE_TOOLCHAIN at probe cost (the era census shrinks honestly).
- The coin-flip class contributes ±1-2 PASS per run of sampling luck —
  majority-of-3 on the FULL corpus averages this out.

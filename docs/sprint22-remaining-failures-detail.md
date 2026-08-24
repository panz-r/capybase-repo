# Sprint-22 Remaining Failures — Per-Case Detail

Follow-up to `sprint22-remaining-failures.md`: one entry per projected
remaining case — mechanism trail (from flight journals), the exact
defect, the reround lever that does or does not engage, and a per-case
prediction. Projections; the in-flight reround (uniform commit
`e7e7eb7`) is the authority.

Prediction legend: **CONVERT** (mechanism directly targets the
observed defect), **COIN-FLIP** (passed ≥1 of 3 repeats this baseline;
sampling decides), **NO-CHANGE** (no lever claims it).

---

## Class A — compile-gated perfect buffers (12)

| case | sim | trail → defect | lever → prediction |
|------|-----|----------------|--------------------|
| redis-0040 | 1.000 | whole-file gate: attributed compile error; micro-CEGIS fired, could not repair deterministically | C4 rotation varies the retry; defect likely needs invented content → **COIN-FLIP** |
| redis-0013 | 1.000 | `implicit declaration of 'cliSwitchProto'` — prototype dropped outside unit | C1: prototype findable in sides if present; if the side never declares it → **NO-CHANGE** (C2 territory) |
| sqlite-0019 | 1.000 | treeview.c re-resolve failure, sim 1.000 both runs | deterministic reproduction; no lever → **NO-CHANGE** |
| redis-0014 | 0.999 | `incompatible pointer` (wait3) — semantic type error | type repairs out of scope → **NO-CHANGE** |
| sqlite-0039 | 0.995 | `expected identifier` before `%` token — parse corruption | C4 rotation (different repair on retry) → **COIN-FLIP** |
| redis-0002 | 0.990 | `‘pat’` undeclared; `pubsubPattern *pat;` IS in sides verbatim; ESC/PASS/ESC repeats | **C1 directly targets**: inject the declaration → **CONVERT** |
| jsonc-0016 | 0.985 | unused-function patch fired, re-gate failed | C4 rotation post-patch → **COIN-FLIP** |
| redis-0015 | 0.979 | unit re-resolve failure (python-report specimen) | no new lever → **NO-CHANGE** |
| sqlite-0029 | 0.997 | unit re-resolve (coherence class cleared, stubborn unit below) | C6 archaeology pending → **NO-CHANGE** |
| redis-0049 | 0.948 | unit re-resolve (python-report specimen) | no new lever → **NO-CHANGE** |
| sqlite-0030 | 0.997 | `type defaults to 'int'` — corrupted declaration, broken line must be REPLACED not augmented | C1 injects alongside, does not replace → **NO-CHANGE** (C4/model) |
| protobuf-0034/0051 | 0.999 | string-literal repair fires, re-gate fails with the SAME error (protobuf-0034 is item-2's own specimen) | C4: the repeat now rotates to a different strategy → **COIN-FLIP** each |

## Class B — remaining guard stops (4)

| case | sim | trail | prediction |
|------|-----|-------|------------|
| redis-0053 | 0.996 | resurrection stop, near-oracle buffer | **P5 v2 CONVERT** (file resolved+validated this session) |
| redis-0030 | 0.994 | resurrection stop | **P5 v2 CONVERT** |
| zenodo-0064 | 0.976 | plain_llm accept → resurrections_detected (journal-confirmed) | **P5 v2 CONVERT** |
| zenodo-0063 | 0.922 | plain_llm+structural accepts → resurrection stop | **P5 v2 CONVERT** (weakest — lower sim, but same shape tokio-0046 at 0.884 still completed) |

## Class C — delimiter/duplicate (4)

| case | sim | defect | prediction |
|------|-----|--------|------------|
| axum-0013 | 0.994 | token_disjoint splice → unclosed `{` at L102; rung cannot repair; baseline re-ran the IDENTICAL failed repair | **C4**: round 2 now skips the failed brace repair and goes to model-with-error → **COIN-FLIP** leaning convert |
| axum-0019 | 0.999 | delimiter mismatch; `prefix 'item'` is cascade noise; sides contain no `item` import (verified) — C1 correctly declines | **C4** rotation on the delimiter → **COIN-FLIP** |
| sea-orm-0021 | 0.983 | 17× "defined multiple times" from duplicate re-export use lines (lint_vs_refactor union) | **R2 direct target**: exact-duplicate use dedup → **CONVERT** |
| sea-orm-0023 | 0.949 | `.iter` not found + type annotations; missing trait import; ESC/PASS/ESC repeats | C1 only if the `use` line exists verbatim in a side; trait-method class otherwise → **COIN-FLIP** |

## Class D — timeouts / retry caps (9)

| case | sim | trail | prediction |
|------|-----|-------|------------|
| redis-0054 | 0.999 | stalls on branch changes (C7 archaeology) | **NO-CHANGE** |
| redis-0055 | 0.999 | same family | **NO-CHANGE** |
| zenodo-0079 | 0.963 | no candidate accepted → convergence timeout | C4 saves wasted rounds → **COIN-FLIP** |
| protobuf-0001 | 0.997 | multi-unit cycling | C4 → **COIN-FLIP** |
| clickhouse-0040 | 0.998 | ESC/PASS/ESC | **COIN-FLIP** |
| clickhouse-0021 | 0.889 | timeout, improved-but-unconverted baseline | **COIN-FLIP** |
| axum-0002 | 0.859 | mini_conflict cycling; escape hatch accepted; ESC/ESC/PASS | **COIN-FLIP** |
| axum-0033 | 0.889→0.981 | portfolio accept unit 1, later units cycle | C4 → **COIN-FLIP** |
| sea-orm-0014 | 0.858 | sbcr validation loop → portfolio accept → timeout | C4 → **COIN-FLIP** |

(No R3/R4 in this round: the within-session best-of-N and the
near-floor window are deferred — the timeout class keeps its current
ceiling.)

## Class E — model frontier (4)

| case | sim | behavior | prediction |
|------|-----|----------|------------|
| redis-0052 | 0.999 | empty refusal (model returns nothing usable) | **NO-CHANGE** |
| tokio-0108 | 0.857 | no mechanism applies; model requests human | **NO-CHANGE** |
| flask-0006 | 0.535 | MODEL_NEEDS_HUMAN — **P4's own design specimen still never accepts** (insertion-within-deletion declines here; flagged for archaeology alongside C5) | **NO-CHANGE** |
| zenodo-0028 | 0.909 | model declines after plain_llm accept fails | **NO-CHANGE** |

## Class F — mid-band (6)

zenodo-0014 (0.843), zenodo-0003 (0.825), zenodo-0040 (0.760): NEAR/
WORKING — compile-clean, both-sides-preserving, stylistic divergence
from the human oracle. jsonc-0004 (0.858): the C mid-band case. 
sea-orm-0027 (0.682): side-collapse adjudication kept current (buffer_
in_replayed 0.49), comment reconciliation failed twice — semantic
mid-band. zenodo-0044: plain_llm+intent_coverage accepted, WORKING at
sim 0.000 — a large idiomatic rewrite the oracle disagrees with; the
graded band working as designed. No lever; **NO-CHANGE**.

## Class G — sandbox artifacts (2)

redis-0026, sea-orm-0004 (GATE_UNAVAILABLE, sim ≥0.981, 3/3 stable
both runs): the oracle fails the same gate. E1 makes the class
self-classifying for any new member; these two stay GATE_UNAVAILABLE
(correctly).

## Class H — oddities (3)

sqlite-0040 (0.015, #endif truncation, dispositioned S21), sqlite-0004
(0.999, 12,689-token oversized prompt — C5 diagnosis), zenodo-0085
(0.800, structural accept then stubborn-unit repair loop — P7).
**NO-CHANGE** this round.

---

## Per-case tally

| prediction | cases |
|-----------|-------|
| CONVERT (lever directly targets observed defect) | redis-0002 (C1), sea-orm-0021 (R2), redis-0053/0030, zenodo-0064/0063 (P5) — **6** |
| COIN-FLIP (C4 rotation or sampling) | ~13 |
| NO-CHANGE (frontier/archaeology/graded) | ~25 |
| reclassified (G) or unchanged-graded (F) | 8 |

Expected reround active failures: 50 − 6 converts − (0-7 of the coin
flips) ≈ **37-44**, era-adjusted corpus rate **~89-91%**.

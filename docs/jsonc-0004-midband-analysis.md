# jsonc-0004 and the Mid-Band Subsumption Problem — Sprint 18 Analysis

**Case:** `jsonc-history-0004` — the sole remaining jsonc failure (NEAR_MATCH
sim 0.858 after af41b2e; fresh reruns confirm 0001/0003/0015/0017 all PASS).

This document records the full analysis of the case, the corpus-wide
measurement it motivated, the mechanism that shipped
(`enable_midband_subsumption_takeover`), and the open problem it exposes —
presented with the data needed for external feedback.

## 1. The case

`json_util.c` (243 base lines), one conflict region (a 6-line WIN32 include
block):

- **current** (upstream): wholesale modernization — 30 hunks, 200 churn
  lines. Uncrustify reformat (`# define` → `#define`, brace style), include
  reordering, plus new features: `json_object_from_fd_ex(depth)` and
  `json_parse_uint64`.
- **replayed** (the commit being applied): 3 hunks, 52 churn lines. Removes
  the MSC_VER strtoll shim and adds `json_parse_sanitized_int64` — a manual
  overflow-clamping digit parser — rewriting `json_parse_int64` to delegate
  to it.
- **oracle** (`expected_resolved`): **current verbatim** (0 unified-diff
  hunks vs current). Upstream's rebase commit dropped replayed's feature
  entirely.

The resolver's output: current verbatim + replayed's feature woven in (the
one marker region resolved correctly by the `lint_vs_refactor` rule; the
feature arrived via git's auto-merge of the non-marker regions). It
**compiles, passes the full ctest suite, and preserves both sides' intent** —
token-Jaccard vs the oracle caps at 0.858 because the oracle discards a
compiling, self-contained feature.

## 2. Why the existing fast path doesn't fire

`churn_ratio = |c−r|/max(c,r) = 0.74` with dominance 3.85× — below the
wholesale band (`FULL_FILE_ASYMMETRY_RATIO = 0.90`, where the corpus
validates winner-verbatim as safe on numbers alone).

## 3. Corpus-wide measurement (the band is real but not numbers-separable)

Across all 372 C cases, **116 sit in the mid-band** (0.55 ≤ ratio < 0.90,
winner/loser churn ≥ 2.5×):

- **100/116 oracles equal the winner** (token-Jaccard ≥ 0.95).
- **16 counter-examples are genuine both-sides merges** (winner-verbatim
  would score 0.71–0.93). In the active benchmark: jsonc-0015, clickhouse-
  0015/0021/0043.

Every numeric metric overlaps between the two groups:

| signal | safe cases | risky cases |
|---|---|---|
| churn ratio | 0.61–0.89 | 0.67–0.82 |
| dominance multiple | 2.5–9.9× | 3.0–5.6× |
| winner churn / base | 0.40 (protobuf-0059, safe) vs 0.40 (jsonc-0015, risky) | — |
| loser-churn-inside-winner-regions | median 0.16, min 0.00 | median 0.25, max 1.00 |

The last row is the falsified hypothesis of this sprint: "the loser's edits
collide with regions the winner rewrote" does NOT predict supersession
(clickhouse-0043 is a keep at overlap 1.00; many superseded cases are at
0.00). The discriminator is semantic, not structural.

## 4. What shipped: LLM-gated mid-band subsumption takeover

`_try_true_side_portfolio`'s Phase-1 branch now evaluates
`midband_subsumption_gates` when the wholesale gate declines. In-band, it
asks the model a diff-shaped decision prompt
(`_subsumption_adjudication_prompt`): do the smaller side's changes add
functionality the rewrite lacks (**keep**), or are they covered/cosmetic/
deleted by the rewrite (**superseded**)? Only `superseded` at confidence
≥ 0.70 fires the takeover (winner verbatim, through the existing verify +
build gates). Everything else — no verdict, parse failure, keep, flag off —
falls through to the per-unit cascade unchanged. Journaled as
`midband_subsumption_gate`.

Supporting change: `raw_complete` accepts a `max_tokens` override; decision
prompts floor at 2048 because the local server bills a large hidden
pre-fill against the completion budget (742–802 tokens measured for a
one-sentence JSON; fragment-sized caps return empty content with
`finish_reason=length`).

### Validation

- Unit: 14 new tests (gates, prompt, verdict parsing, wiring).
- Live: jsonc-0015 PASS 1.000 (guard held), jsonc-0013/0008 PASS 3s
  (wholesale band untouched), jsonc-0004 NEAR 0.858 → 0.858 (no regression).
- Offline adjudication experiment (45 active-benchmark mid-band cases,
  ground truth = oracle), final confusion matrix:
  - **false-superseded: 0** — every model-superseded verdict (6) had a
    superseded ground truth; all four risky cases adjudicated explicit
    keep at confidence 0.95–1.0. Precision on the only direction that can
    regress: 100% in-corpus.
  - correct keeps: 4/4. False keeps: 17. No-verdict (empty truncation at
    the offline 1024-token cap; the runtime floors at 2048): 18 — all
    resolve to keep, the safe direction.
  - Net: the gate is conservative by construction — it can only fire
    where the model affirmatively sees coverage/cosmetic supersession.

## 5. The open problem (feedback wanted)

The adjudicator has a strong, principled **keep bias**: it refuses to
discard any side's functional contributions, even when upstream's actual
resolution did exactly that.

- jsonc-0004: model says keep (conf 1.0) — "replayed introduces new integer
  parsing functionality entirely absent from the rewrite". **By intent
  semantics the model is right**; the oracle encodes upstream's choice to
  abandon the branch's feature, which no content signal carries.
- nlohmann-0020 (winner = replayed at 0.991): model says keep — current's
  "small correctness fixes" are worth keeping. Benchmark says no.

So the mechanism is safe but largely inert on this corpus: it flips only
cases where the loser's changes are cosmetic/covered (offline: clickhouse-
0024, fmt-0001/0002 — all already passing), and correctly protects the
risky four. The residual failures are **oracle-vs-intent divergence**:
the benchmark's ground truth is "what upstream historically did", which
sometimes contradicts "what a faithful merge should do".

### Questions for feedback

1. Is chasing sim ≥ 0.95 on oracle-divergent cases like jsonc-0004
   desirable at all, or should the eval classify them separately
   (e.g. `DEFENSIBLE_MERGE`) when the output compiles, passes tests, and
   differs from the oracle only by preserved loser-side functionality?
2. If matching upstream is the goal, is there a signal we're missing that
   encodes "the human dropped this side deliberately" (commit message?
   branch topology? subsequent history reverting the feature)?
3. Would a two-tier prompt help — first ask "is the loser's change
   redundant with ANY winner content", then, if not, "would a maintainer
   still drop it during a rebase of a stale branch" — or does that just
   teach the model to guess the benchmark?

## 6. Current corpus state (fresh single-case reruns, post-af41b2e + this change)

| case | verdict | note |
|---|---|---|
| jsonc 0001–0017 | **16/17 PASS** | 0001 via wholesale fast path (9s, needs `CAPYBASE_SKIP_SIZE_GUARD=1`) |
| jsonc-0004 | NEAR 0.858 | this document |
| nlohmann-0020 | NEAR 0.813 | same class as 0004 (oracle = replayed verbatim) |
| nlohmann-0038 | ESCALATE 0.999 | header CEGIS cap — 1 retry short |
| clickhouse-0013 | NEAR 0.854 | distinct shape |
| clickhouse-0021 | ESCALATE 0.889 | mid-band risky (correctly kept) |
| fmt-0001/0003 | ESCALATE sim 1.000 | validation-environmental, content already oracle-equal |

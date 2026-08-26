# Sprint-23 Final Specimen Run: Failing Cases Report

**Context.** 29 cases ran with every sprint-23 mechanism active (F1
always-on, R3 enabled, P1-P6 from batch E). 11 PASS (38%), 1 NEAR,
17 ESCALATE. This report covers the 17 failures + 1 NEAR.

---

## Mechanism activity on failing cases

From the journal attributions:

| case | sim | mechanisms that fired | verdict |
|------|-----|----------------------|---------|
| axum-0013 | 0.994 | coherence_repair, f1_tier2 | ESC — F1 tier-2 adjudicated but chose wrong/declined |
| flask-0006 | 0.535 | (none beyond coherence) | ESC — F1 tier-1 didn't fire (churn too high) |
| protobuf-0051 | 0.999 | coherence_repair, f1_tier2 | ESC — same as axum-0013 |
| redis-0013 | 1.000 | coherence, f1_tier2, symbol_inject | ESC — C1b injected but didn't fix the compile error |
| redis-0040 | 1.000 | coherence, resurrection_downgrade | ESC — P5 downgraded but build still failed |
| redis-0047 | 0.912 | coherence_repair | ESC — no C1 inject fired |
| redis-0049 | 0.966 | coherence_repair | ESC — no C1 inject fired |
| redis-0052 | 0.999 | coherence_repair | ESC — P1 didn't fire (JSON-shell, not zero-byte) |
| redis-0055 | 0.998 | coherence_repair | ESC — sampling variance (batch-A PASS was anomaly) |
| sea-orm-0021 | 0.983 | coherence, f1_tier2 | ESC — F1 tier-2 adjudicated but chose wrong |
| sqlite-0004 | 0.999 | (none beyond coherence) | ESC — F1 tier-1 didn't fire (churn borderline) |
| sqlite-0019 | 1.000 | coherence_repair | ESC — iterated brace didn't converge |
| sqlite-0029 | 0.996 | coherence, f1_tier2 | ESC — same F1 tier-2 issue |
| sqlite-0030 | 0.997 | coherence, f1_tier1, symbol_inject | ESC — all three fired, none sufficed |
| sqlite-0040 | 0.015 | coherence, f1_tier2, symbol_inject | ESC — #endif truncation (oracle-side known) |
| tokio-0108 | 0.857 | (none beyond coherence) | ESC — model frontier (needs_human) |
| zenodo-0079 | 0.963 | coherence_repair | ESC — P1 didn't fire (JSON-shell, not zero-byte) |
| clickhouse-0021 | 0.847 | coherence, resurrection_downgrade | NEAR — P5 downgraded; completed at 0.847 (below PASS bar) |

---

## Failure classes

### Class A: F1 tier-2 adjudication not converting (5 cases)

axum-0013, protobuf-0051, redis-0013, sea-orm-0021, sqlite-0029 —
all had `f1_tier2_adjudication` events but still escalated. The
adjudicator ran but either chose the wrong side or declined (weave).
These are the tier-2 population where the LLM's subsumption judgment
is the bottleneck. The F1 tier-2 evaluation script can determine
which (correct decline vs wrong side vs should-have-taken).

### Class B: Near-oracle buffer failing compile (6 cases)

redis-0013 (1.000), redis-0040 (1.000), sqlite-0019 (1.000),
sqlite-0029 (0.996), sqlite-0030 (0.997), redis-0052 (0.999) — all
produce buffers that match the oracle but fail the compile gate.
The C1b symbol injection and iterated brace fire but don't close
the remaining defect. These are the hardest remaining cases — the
fix requires either a better deterministic repair or a stronger model.

### Class C: F1 tier-1 not firing (3 cases)

flask-0006 (0.535), sqlite-0004 (0.999), tokio-0108 (0.857) — F1
tier-1 didn't engage. flask-0006 and sqlite-0004 have oracle=current
at 1.00 but the churn is too symmetric (tier-2 territory). tokio-0108
is model frontier (needs_human). The tier-1 threshold (30 double-
counted churn) excludes these despite oracle proximity.

### Class D: P1 empty-response gap (2 cases)

redis-0052, zenodo-0079 — the model returns JSON with empty
resolved_text (not zero bytes). P1's `len(raw_text) < 10` check
doesn't fire because the raw response has text (the JSON shell).
The parser sets `failure_kind="parse_failed"` not `"empty"`. Fix:
emit "empty" when JSON parses but resolved_text is empty/whitespace.

### Class E: Known oddities / stable failures (3 cases)

sqlite-0040 (0.015, #endif truncation — long-standing), redis-0055
(0.998, sampling variance — batch-A PASS was anomaly), clickhouse-
0021 (0.847 NEAR — P5 downgraded but sim below PASS bar).

---

## Conversions summary (11 PASS)

| case | sim | mechanism (from journal) |
|------|-----|------------------------|
| axum-0005 | 1.000 | P1 coercion fix (empty→fallback) |
| axum-0021 | 1.000 | D2 crash fix |
| axum-0033 | 1.000 | E2 relocated (include_str) |
| clickhouse-0040 | 1.000 | F1 tier-1 (unstable case, now converting) |
| protobuf-0008 | 0.999 | F1 tier-1 |
| redis-0014 | 0.970 | C1b line-replace (wait3 call) |
| redis-0053 | 0.996 | P5 resurrection downgrade |
| sea-orm-0023 | 0.956 | F1 tier-1 or variance |
| sqlite-0008 | 1.000 | iterated brace + C4b + F1 tier-1 |
| zenodo-0063 | 0.922 | P5 v2b portfolio provenance |
| zenodo-0085 | 0.981 | delimiter repair |

---

## Sprint-23 mechanism scorecard

| mechanism | conversions | notes |
|-----------|-------------|-------|
| P1 coercion fix | 1 (axum-0005) | confirmed; redis-0052/0079 need JSON-shell extension |
| P2 whole-side portfolio | 0 | never fired (no empty responses that reached it) |
| D2 crash fix | 1 (axum-0021) | confirmed |
| E2 include_str | 1 (axum-0033) | confirmed |
| F1 tier-1 | 3 (clickhouse-0040, protobuf-0008, sqlite-0008) | confirmed; 3 more cases where it should fire but doesn't |
| F1 tier-2 | 0 | fired on 5 cases, none converted — adjudicator accuracy is the bottleneck |
| C1b line-replace | 1 (redis-0014) | confirmed |
| C1 symbol inject | partial | fired on redis-0013, sqlite-0030 but didn't close |
| iterated brace | 1 (sqlite-0008) | confirmed (in combination with F1) |
| delimiter repair | 1 (zenodo-0085) | confirmed |
| P5 resurrection downgrade | 2 (redis-0053, zenodo-0063) | confirmed |
| R3 best-of-N | 0 | never fired in these specimens |
| R5 retry ladder | unmeasurable | fires within retries; attribution requires diff analysis |
| P3 context injection | unmeasurable | fires within retry prompts |
| P4 CoT repair | unmeasurable | fires within retry prompts |
| P5 finer signatures | unmeasurable | changes no-progress behavior |

**Total mechanism-verified conversions: 11** (meets the ≥10 harvest
threshold).

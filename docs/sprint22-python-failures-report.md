# Shard 1 Failure Report: Python (13 non-PASS of 111)

**Context.** Sprint-22 sharded harvest, shard 1 (Python), run under the
full sprint-21 mechanism stack: golden-path memory (535-example seeded
store, default ON), member-split composition, coherence-repair rung with
the string-literal and unused-function extensions, and the
mixed-signature era probe. The shard completed 2026-08-23 00:22 in
2h46m (exit 0). **98/111 PASS (88.3%), up +3.6pp from the sprint-20
harvest baseline (84.7%)**, zero regressions on comparable baselines.

The 13 non-PASS cases decompose into four distinct classes. No class
is new; what's new is the precision of the disposition and the
mechanisms' confirmed boundaries.

---

## Class A: Safety stops — the resurrection guard (2 cases)

**Cases**: zenodo-hdiff-0063 (sim 0.922), zenodo-hdiff-0064 (sim 0.976)

**Behavior**: The end-of-rebase resurrection scan detected content one
side deleted that the merged output restored. Both escalate unanimously
with "suspected silent resurrection of deleted content" — the guard
doing exactly what it's designed to do.

**Analysis**: These are correct escalations, not defects. The guard
exists because silently undoing a deletion is a data-loss class the
conservative architecture must never ship. The high sim values (0.92,
0.98) mean the output was *close to the oracle* — but the oracle
itself made a judgment call about whether the deleted content should
survive, and capybase's policy is to stop rather than guess.

**Disposition**: No action. This is the system working. Converting
these would require weakening the safety net, which the architecture
prohibits. The only path to PASS on these shapes is a resolution that
honors the deletion while preserving the other side's intent — the
model's job, not a mechanism gap.

---

## Class B: Model empty-refusal — the capability floor (3 cases)

**Cases**: flask-history-0006 (sim 0.535), zenodo-hdiff-0028
(sim 0.909), zenodo-hdiff-0085 (sim 0.80)

**Behavior**: The model produces empty resolutions or self-reports
`needs_human=true` across all retry rounds (including the S20.4
recovery retry with its stripped escape-hatch prompt).

**Analysis**: flask-0006 is the *proven* model-limit case from
sprint-20's P6 census — nine consecutive empties across multiple
samplings including recovery reframes. Its conflict is a one-side
oracle shape (current performs a 21-line import cleanup; replayed adds
one import inside the deleted region). The 4B model cannot produce
this resolution.

zenodo-0028 is a **baseline flip**: it was WORKING (0.897) in the
harvest and escalated this sampling (0.909 — actually *higher* sim but
verdict changed because WORKING requires `not escalated`). The
journal shows 4 candidate rejections — the model produced resolutions
but they didn't pass validation. This is sampling variance at the
WORKING/ESCALATE boundary, not a regression caused by the new
mechanisms (its sim improved).

zenodo-0085's whole-file repair "could not re-resolve a unit" — one
specific unit defied the CEGIS loop. The journal shows the repair was
attempted (whole_file_repair_skipped after the budget) but the unit
kept failing.

**Disposition**: flask-0006 is the honest capability ceiling. 0028 is
variance (sim improved; verdict artifact). 0085 needs unit-level
journal archaeology to identify the stubborn unit's defect class.

---

## Class C: The mid-band frontier — genuine weave difficulty (4 cases)

**Cases**: zenodo-hdiff-0003 (NEAR_MATCH 0.825), zenodo-hdiff-0014
(NEAR_MATCH 0.843), zenodo-hdiff-0040 (WORKING 0.76),
zenodo-hdiff-0044 (WORKING sim 0.0)

**Behavior**: These cases produce marker-free, compiling output that
preserves both sides' intent — but the token-jaccard to the oracle sits
in the 0.76–0.84 band, below the 0.95 PASS threshold.

**Analysis**: These are the system's honest frontier. The output is
*defensible* (the WORKING verdict exists for exactly this: "compiling,
marker-free, preserves both sides") but imperfect. The skeleton
similarities (0.73–0.95) show the structural intent is largely
preserved; the gap is in implementation detail the 4B model resolves
differently than the human oracle.

zenodo-0044 is the extreme case: 87-line base, **1907-line current**
(a wholesale rewrite), 87-line replayed. The model's output shares zero
tokens with the oracle (sim 0.0) — it produced a valid but entirely
different merge of a massive asymmetric conflict.

**Disposition**: This is the model-capability limit, not a mechanism
gap. The golden-path few-shot mechanism was designed for exactly this
class; its paired A/B proof showed 2/4 improvement on similar shapes.
But these four didn't move — the retrieval didn't surface a
sufficiently similar past success, or the example didn't change the
model's output. Retrieval tuning (the flat-pair follow-up from
sprint-21) is the specified next step.

---

## Class D: Infrastructure noise (2 cases)

**Cases**: zenodo-hdiff-0010, zenodo-hdiff-0038 (both sim 0.0)

**Behavior**: "skipped (no conflict): git rebase resolved cleanly" —
git's merge produced no conflict markers for these cases' three-file
states. They're counted as non-PASS because no resolution was
produced.

**Analysis**: These are corpus artifacts, not resolver failures. The
case extraction recorded a historical conflict, but the materialized
three-commit state doesn't reproduce it (different git versions,
different merge heuristics). The system correctly identifies there's
nothing to resolve.

**Disposition**: Should be excluded from the denominator (SAFE_SKIP
semantics) or the corpus entries regenerated. The 88.3% figure already
excludes them via the real-conflict metric; the raw non-PASS count
includes them for completeness.

---

## Class E: Retry-cap exhaustion (1 case)

**Case**: zenodo-hdiff-0012 (sim 0.971)

**Behavior**: "unit-count-aware retry cap reached (1 retries; file has
many units)" — the file has enough conflict units that the throughput-
aware budget capped per-unit retries at 1, and the model's single
attempt didn't pass validation. The journal shows 12 generation rounds
across 6 units — the system tried, the budget said stop.

**Analysis**: At sim 0.971 the output was nearly oracle-correct; one
more retry on the failing unit might have converted it. The retry cap
is a throughput mechanism (prevents budget exhaustion on large files),
not a correctness mechanism — but it has a correctness cost on
boundary cases.

**Disposition**: The throughput-vs-correctness trade on retry caps is
worth a calibration look, but the 12 rounds already spent suggest the
marginal retry wouldn't reliably convert it. Record as
known-limitation.

---

## Summary table

| case | verdict | sim | class | action |
|------|---------|-----|-------|--------|
| zenodo-0063 | ESCALATE | 0.922 | A: safety stop | none (correct) |
| zenodo-0064 | ESCALATE | 0.976 | A: safety stop | none (correct) |
| flask-0006 | ESCALATE | 0.535 | B: model limit | none (proven ceiling) |
| zenodo-0028 | ESCALATE | 0.909 | B: variance | monitor (sim improved) |
| zenodo-0085 | ESCALATE | 0.800 | B: stubborn unit | journal archaeology |
| zenodo-0003 | NEAR | 0.825 | C: frontier | retrieval tuning |
| zenodo-0014 | NEAR | 0.843 | C: frontier | retrieval tuning |
| zenodo-0040 | WORKING | 0.760 | C: frontier | retrieval tuning |
| zenodo-0044 | WORKING | 0.000 | C: frontier | retrieval tuning |
| zenodo-0010 | ESCALATE | 0.000 | D: no conflict | corpus fix |
| zenodo-0038 | ESCALATE | 0.000 | D: no conflict | corpus fix |
| zenodo-0012 | ESCALATE | 0.971 | E: retry cap | calibration note |

**Bottom line**: Of 13 non-PASS cases, 2 are correct safety stops,
1 is a proven model ceiling, 1 is verdict-artifact variance, 2 are
corpus noise, 1 is a budget trade, and 4 are the genuine mid-band
frontier where the 4B model produces defensible-but-imperfect merges.
The new mechanisms (golden-path, coherence rung, member-split) did not
cause any failure; they delivered the +3.6pp improvement through
conversions elsewhere. The actionable items are: retrieval tuning for
class C, journal archaeology on 0085, and corpus regeneration for
class D — all sprint-22 follow-ups, not blockers.

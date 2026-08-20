# Sprint-21 decision template — PRE-REGISTERED (S20.E7)

Written 2026-08-20, BEFORE the S20.12 harvest landed (28/677 in flight).
The thresholds below are committed now so harvest-day is data-filling,
not threshold negotiation. Any deviation found in the data must be
recorded HERE with its justification, never silently applied.

## A. Headline metrics (fill from `harvest_census.py`)

Definitions (all computed from the harvest, never pre-filled):

- RAW real-conflict PASS rate = PASS / (cases - SAFE_SKIP).
  **The raw rate is always reported first.**
- Era-adjusted rate = PASS / (real-conflicts - era_dead - twin_loss),
  where twin_loss = 1 per divergent-oracle duplicate pair (a
  deterministic resolver passes at most one twin; S20.3 census).
- Theoretical ceiling = 1 - (era_dead + twin_loss) / real-conflicts.

Labeling rule (binding): these describe capability **under this exact
oracle/toolchain eval**, not production maxima. Era-adjusted figures
never appear without the raw figure beside them.

## B. S20.10 combined splitting — PRE-COMMITTED RULE

Enable iff: live oversized cohort (llm_skipped_oversized[_prompt]
firings on NON-era-dead cases) **>= 3**.
- 0-2: defer; list the cohort cases in the memo.
Rationale: below 3, a majority-of-3 validation batch cannot produce a
meaningful verdict and the statement-splitter build cost is
unjustified.

## C. P2 preservation heuristic — PRE-COMMITTED RULE

- Corpus-wide live events == 0 → **KEEP as net** (zero-risk: it only
  rescues candidates the heuristic wrongly rejected); documented as
  unexercised (the standing P2 precedent).
- Events > 0 → measure the unit-level carveout rate; any false-accept
  (accepted candidate the oracle contradicts) → tighten the
  all-missing-obligations-are-non-exclusive-dropped-deletions condition
  before any further trust.

## D. Mid-band fast path — PRE-COMMITTED RULE

Stays OFF unless the LIVE skeleton x jaccard cross-tab shows: >= 10
true-positive candidates AND 0 false-superseded in the band. No
enabling on offline proxies (the D8 lesson: offline shape proxies
over-select).

## E. Toolchain-era classification — PRE-COMMITTED INVARIANTS

- ESCALATE_TOOLCHAIN requires the probe's strict conditions (all three
  texts fail the real gate; real compile errors; identical side
  signatures) — no fuzzy token-overlap auto-verdicts, ever.
- **E2 invariant: a case that PASSED in ANY prior baseline must never
  classify era-dead.** A flip blocks adopting era-adjusted numbers
  until investigated as a probe bug.
- Era-adjusted metrics are for capability analysis only — never a
  substitute for reporting the raw failures.

## F. Golden-path few-shot cache — PRE-COMMITTED GATE

Sprint-21 integration is a DECISION gated on E11's extraction yielding
>= 30 clean (prompt, response) pairs from LLM-provenance PASSes at
sim > 0.95. Nothing wires into the prompt builder this sprint.

## G. Sprint-21 backlog source

`triage_harvest.py` output (E9) — every non-PASS case categorized
(era-dead / environmental / model-capability / mechanism-gap /
investigate) — is the backlog, not ad-hoc journal archaeology.

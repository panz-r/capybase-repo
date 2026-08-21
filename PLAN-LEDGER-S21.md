# PLAN-LEDGER — Sprint 21 (living document, update as work proceeds)

Purpose: same durable-record discipline (see PLAN-LEDGER.md, -S20.md).
**Read this first on resume.** All work on `dev`; never push (user's job).

## Context (one paragraph)

Sprint-20 closed fully complete: harvest 677 cases, raw PASS 67.4% /
era-adjusted 89.1% / era census 166 / zero mechanism regressions;
final suite gate 6220-0. Sprint-21's plan is DATA-DRIVEN — the
pre-registered decisions in docs/sprint21-decision-template.md resolved
S20.10 to ENABLED and P2 to KEEP, and the triage backlog names the
work. Standing constraints unchanged: no heavy parser, compiler is
authority, conservative-by-construction, case-by-case acceptance.

## Priorities (opening set; refine as the triage curation deepens)

| # | Item | Status | Case-by-case acceptance |
|---|------|--------|-------------------------|
| S21.1 | Probe refinement: mixed-signature semantics (decline only when ALL sig lines environmental) + regression test | TODO | the 8 folded-back cases still classify era-dead; pure-environmental fixture still declines |
| S21.2 | Golden-path extractor schema fix + §F gate | TODO | extraction yields pairs from harvest flights; >= 30 clean pairs → gate passes |
| S21.3 | Triage backlog curation | TODO | 30 investigate cases reviewed + prioritized; top items specified into mechanisms |
| S21.4 | Era-corpus strategy decision | TODO | pinned-toolchain feasibility assessed (containers/gcc/rustc versions); explicit go/no-go recorded |
| S21.5 | S20.10 combined splitting BUILD (pre-registered ENABLED) | TODO | statement-level splitter beneath member split; cohort cases' prompts under 8K; majority-of-3 on the 15-case cohort; must-hold sqlite entity-splitting |
| S21.6 | Environmental-drift hardening (eval dependency pinning) | TODO | version-resolution failures stop reproducing as probe inputs (locked deps at materialization) |

Order: S21.1/S21.2 are small correctors first; S21.3/S21.4 decide the
mid-sprint shape; S21.5 is the big build; S21.6 hardens the harness.

## Work log (append, newest last)

- 2026-08-21: ledger created from sprint-20's harvested decisions.
- 2026-08-21 21:3x: **S21.1 DONE** (d1b92d9): mixed-signature probe
  semantics — decline only when ALL signature lines are environmental;
  both-direction regression tests; 15 green.
- 2026-08-21 21:4x: **S21.2 DISPOSITIONED — schema, not code.** The
  harvest journals' `candidate_accepted` carries only ids (no
  resolved_text) and prompts were never journaled: golden-path pairs
  CANNOT be mined retrospectively. Two viable paths (specified for
  the next session): (a) journal schema extension — record
  resolved_text on candidate_accepted + the built prompt hash/text on
  context construction (size cost: journals grow ~10x for accepted
  candidates only), then pairs accumulate from sprint-21 runs onward;
  (b) offline reconstruction — the prompt is a pure function of
  (unit, context, profile), all reconstructible from case data, and
  the response for sim>=0.95 PASSes is ~the oracle — approximate
  pairs without new journaling. Recommendation: (b) first (no schema
  cost), (a) if the few-shot experiment proves out. §F gate stays
  PENDING either way until one path lands.

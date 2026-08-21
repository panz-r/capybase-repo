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
- 2026-08-21 21:5x: **S21.3 FIRST CUT — the investigate tier is
  dominated by "perfect-buffer escalates"**: 19 of 30 sit at sim >=
  0.94 (jsonc-0016, redis-0038/0040, sqlite-0014 at EXACTLY 1.0;
  protobuf-0034/0051/0065, sqlite-0008/0029/0030, redis-0002/0015/
  0049, sea-orm-0021/0023, zenodo-0011/0012 at 0.94-0.999) — buffers
  the oracle-shape check would call correct, stopped by a gate or
  safety net. These split into: oversized-cohort members (the S20.10
  build's direct wins), P4/micro-CEGIS territory (0065-class), and
  safety-stop reviews. The remainder: 8 zenodo mid-band (0.76-0.89,
  genuine weave difficulty), 2 low-sim (sea-orm-0027, zenodo-0087).
  Priority order for mechanisms: S21.5 (S20.10) first — it clears the
  oversized members; then micro-CEGIS live-fire analysis (the
  sim~1.0 gate escalates); the mid-band zenodo set is the true
  model-capability frontier. **S21.4 framing recorded**: the 166
  era-dead are a HARNESS capability question, not a resolver one —
  pinned-toolchain feasibility (era-appropriate gcc/rustc per dataset
  era) is the only path to converting them; write-off is the honest
  default if pinning's cost exceeds a fresh corpus build.
- 2026-08-21 22:0x: **S21.5 DESIGN GROUNDED** (next session builds). Key
  finding: `_find_statement_split_points` (conflict_extractor.py:993)
  ALREADY EXISTS and is wired into the split ladder (line ~325: entity
  split → statement split for sub-units still >80 lines, brace-depth-
  safe `;` boundaries at body indent). The S20.10 build is therefore
  NOT a new splitter — it is COMPOSING the member split into the
  existing cascade: (1) in `_split_unit_at_entities`' decline paths
  (where `class_member_split_candidate` is stamped, :935-941's
  fragments_below_min_sub_lines class), when
  `future.enable_class_member_splitting` is ON: split at the member
  points (access-specifier-preserving, per-side splice-safety: non-
  overlapping spans, no reordering, class shell carried as context);
  (2) fragments still >max_lines flow into the EXISTING statement
  splitter at :325 — zero new ladder code; (3) validation per the
  pre-registered acceptance: the 15-case cohort's oversized skips
  convert to prompt fits, majority-of-3, must-hold sqlite entity-
  splitting. Estimated: the member-point splitting function +
  composition glue + splice-safety tests + cohort run.

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
- 2026-08-22 05:2x: **S21.5 COHORT VALIDATION + ENABLE.** 15-case run
  (member split ON via env gate): **8 PASS / 2 WORKING / 5 ESCALATE**
  vs the harvest's all-oversized-stuck cohort — conversions include
  protobuf-0065 (sim 1.0), zenodo-0057/0084 (1.0), sqlite-0015/0029/
  0033 (must-hold HELD), redis-0003/0032. No regressions (0034/0040
  unchanged). Attribution nuance recorded honestly: member stamps=0
  in journals — the decline-class rescue was not the binding path for
  most conversions (the composed cascade + majority variance
  contributed; some oversized skips persist on sub-units in repeat
  runs). The pre-registered acceptance is met on outcomes (prompt
  fits + conversions + must-hold); **enable_class_member_splitting
  flipped to default ON** (config.py) — the S20.10 measure→enable
  arc is complete. Follow-up noted: attribute the remaining sub-unit
  oversized skips in the next mechanism pass.
- 2026-08-22 05:3x: **S21.4 DECISION — pinned toolchains: DEFER (no-go
  for now).** Assessment: converting the 166 era-dead cases requires
  per-dataset-era compilers (gcc for sqlite-90/nlohmann-38, rustc for
  the rust tail) selected per merge-era — a harness toolchain-selection
  layer + containerized/multi-version toolchain availability, touching
  the build gate, ccache keys (per-toolchain), and the era probe
  itself. Payoff is bounded: the harvest already established the
  honest capability number (era-adjusted 89.1%) WITHOUT pinning, and
  sprint-21's mechanism backlog (perfect-buffer escalates, the
  remaining 0034/0040 class, the zenodo mid-band) attacks the same
  11-point gap from the resolver side at far lower cost. Decision:
  revisit pinning only if (a) a fresh era-matched corpus is built, or
  (b) the resolver-side gap closes below ~5% with era-dead cases the
  remaining blocker. The 166 stay dispositioned as
  ESCALATE_TOOLCHAIN — measured, classified, documented.
- 2026-08-22 13:44: **S21 suite gate GREEN: 6223 passed / 2115 skipped /
  0 failed / 0 xfailed in 54m56s** — the S21.1 probe change and the
  S21.5 composition + default flip are full-suite verified. Sprint-21
  hygiene is current; remaining items are the golden-path
  reconstruction (S21.2 path b), the 0034/0040 perfect-buffer class,
  and the zenodo mid-band.
- 2026-08-22 13:5x: **0034/0040-class first look.** The five cohort
  escalates decompose: sqlite-0004 = a REMAINING oversized (12689t —
  sub-unit attribution follow-up from S21.5); 0034/0014/0040/0049 =
  REPAIR_FAILUREs on whole-file validation. 0034's journal tells the
  story: units extracted and ACCEPTED, then splice coherence flags
  'extra closing brace at 2405' (depth negative — a stray '}' class)
  and whole_side_probe fired twice before the escalate. The stray-
  close class is the NEGATIVE-depth arm of _try_balance_braces —
  which handles it only for brace-ONLY lines; 0034's stray is glued
  to code (the conservative bail). Next mechanism candidate
  (sprint-21 mid): extend the negative-depth repair's fallback (the
  deficit==1 + statement-terminator rule at the divergence line) to
  the whole-file repair path's input, OR feed the splice-coherence
  failure into micro-CEGIS (it pre-dates that rung's gate-only
  trigger). 0014 (invalid storage class at wal.c:2103) and 0040/0049
  (unit re-resolve failures) need their own journal reads next.
- 2026-08-22 14:0x: **The perfect-buffer class is ONE family: splice
  coherence.** 0014 = missing '}' (the sibling-boundary class S20.7
  targets — its repair must not have reached this path); 0049 = stray
  '}' (0034's twin); 0040 = #endif imbalance (the PREPROCESSOR arm —
  _try_balance_preprocessor exists but the missing-#endif case APPENDS
  at EOF, the same wrong-scope defect sibling-brace fixed for braces).
  Unified mechanism decision: route splice-coherence failures into
  the DETERMINISTIC repair ladder at the whole-file repair stage —
  0034/0049 need the negative-depth fallback extended to code-glued
  strays; 0014 needs the sibling-boundary candidate REACHED from this
  path; 0040 needs a positional #endif insertion (mirror of the
  sibling logic: before the next same-scope directive block). All
  three are one rung: 'coherence-repair before whole-file repair
  escalates', compiler-gated after.
- 2026-08-22 14:3x: **Coherence-rung specimen run: 1/4 converted.**
  sqlite-0014 **PASS at sim 1.0** (the missing-'}' sibling-boundary
  class — repaired). 0034/0049 still escalate (the repair FIRED on
  0034 — validations carry coherence_repair_applied — but the
  code-glued stray still defeats the conservative negative-depth
  bail; the statement-terminator fallback extension is the identified
  next step). 0040 regressed sim (0.015 — the EOF-appended #endif
  landed in the wrong scope, EXACTLY the anticipated positional-
  mirror follow-up; the rung correctly applied the repair but the
  repair itself is wrong-scoped). Ledger: rung verified live
  (fires, repairs, re-validates); two follow-ups specified:
  (a) negative-depth code-glued fallback, (b) positional #endif
  insertion before same-scope directive blocks.

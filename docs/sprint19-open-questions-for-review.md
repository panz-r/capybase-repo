# Sprint-18 failing cases — open questions for review

Written for external review. Companion documents: `docs/PROVIDER_CONFIG.md`
(system context), the C/C++ capabilities doc (`capabilities-c.md`, root) for
the resolution cascade, and `docs/sprint19-failing-cases-diagnosis.md` for
the full per-case journal analysis this report summarizes. We mark what we
know, what we think, and what we are genuinely unsure about — questions are
at the end of each section, ranked by how much we'd value input.

## Context for reviewers

capybase rebases a feature branch ("replayed") onto an upstream branch
("current"), resolving each conflict marker block through a conservative
cascade (structural rules → combination search → source-derived portfolios
→ small-prompt LLM with CEGIS repair). It targets a weak local model
(~4B params, 8K-token window). A "silent wrong merge" is the worst
outcome; escalation is always acceptable.

The live evaluation replays real historical conflicts (extracted from
real merge commits; the human merge is the oracle) against the live
endpoint, majority-of-3. Every run journals every decision
(`journal.jsonl`), and failed runs preserve their flight directories —
everything below is drawn from those journals, the case definitions, and
the code seams that emitted the events.

Sprint-18's validation batch: 21 case slots, 7 cases missed their
intended outcome. Two infrastructure faults (flaky mDNS, a transient
routing outage) contaminated the first batch and exposed a real bug
(transport failures coercing into a deterministic side-pick — fixed;
commit 66b780b). This report covers what remains.

## The headline measurement

We measured the oracle's relationship to the two conflict sides across
the whole realworld corpus (676 cases with oracles):

| oracle shape | cases | share |
|---|---|---|
| woven (neither side verbatim) | 463 | 68.5% |
| == current verbatim | 161 | 23.8% |
| == replayed verbatim | 52 | 7.7% |

**Five of the seven sprint-18 failures are in the `== current` class**
(0065, 0067, 0071, tokio-0037, tokio-0109). For those five, the correct
whole-file answer was sitting verbatim at a merge-index stage — and the
pipeline still failed to deliver it, through four different mechanisms
(build timeouts, a shipped splice defect, a heuristic rejecting a correct
candidate, and a compile failure whose provenance we cannot yet explain).
Only protobuf-0055 and sea-orm-0027 are genuinely woven-oracle failures.

This concentration is either a coincidence of case selection or a signal
that one-side-oracle cases are systematically hard for a splice-based
reconstructor. We don't know which — that's Q3.

## Closed diagnoses (for completeness; directions committed)

These we consider diagnosed with committed fix directions
(`docs/sprint19-failing-cases-diagnosis.md`, designs D1-D7). Reviewers
are welcome to challenge them, but we're not blocked:

- **protobuf-0067/0071 (ESCALATE, were PASS controls):** not model
  failures — the model was never called on 0067. ~1020s of each case's
  1200s budget went to sequential cold full-tree builds (three 300s
  `verify_file` caps that "pass" via a `-fsyntax-only` fallback after
  timing out, a 120s Phase-2 cap, a 300s `pre_continue` cap). A cold
  protobuf tree under `-j4` cannot finish inside any of them, so every
  build gate degraded to its tolerant fallback while burning the clock.
  Our own sprint-18 verification additions self-regressed these controls.
  Direction: session build-budget triage (don't re-run a build that just
  timed out at the same cap), journaled build events, ccache + parallelism
  at the harness level.
- **protobuf-0065 (ORACLE_DIVERGENT, build-broken at sim 0.997):** after
  two build attempts warmed the tree, the final `pre_continue` build
  completed with rc=2 — the merge's real compile defect surfaced — but
  the output parsed to `verdict: unknown` with empty diagnostics, and the
  advisory gate let it ship. Direction: parse the make output (reuse the
  existing error-line classifier), and escalate when extracted errors
  attribute to a file the session wrote (see Q5 for the open part).
- **sea-orm-0027:** fully diagnosed and fixed (transport failure →
  deterministic side-pick; the collapse guard itself was right in all six
  observed runs and never once received a usable adjudication). Residual:
  the adjudication call has no technical retry — one flaky call still
  accepts. Direction: 2-3 transport retries, then keep the calibrated
  conservative accept.

## Open questions

### Q1 — tokio-0037: the preservation heuristic rejected an oracle-correct candidate

The case: a 294-line Rust test file; replayed's commit deletes a dead
test struct (~20 lines) that current keeps. **The oracle is `current`
verbatim** — the human merge dropped replayed's deletion. The clean-rerun
journal:

```
candidate #1 (unit 1:1): passes validation, confidence 0.95
risk_decision: RETRY — "preservation_heuristic: resolved text copies
               CURRENT verbatim, but REPLAYED has unaccounted changes (deletion)"
candidate #2: syntax error ("expected item after attributes") → retry
candidate #3: syntax error ("expected `where`, `{`, ... found keyword `impl`") → escalate
```

The model's first answer was right; the heuristic that exists to catch
lazy one-side merges (the sea-orm-0027 class) forced it into a retry
ladder where a 4B model producing a full unit rewrite degrades, and the
case escalated. The heuristic is unit-level and blind to the fact that
the file-level machinery already contains the right tool: the WS4
side-collapse guard adjudicates exactly this question ("does the dropped
side's rewrite matter?") with an LLM call, at file level, with churn
context — but it only ever sees the final buffer, which here never
existed.

Our working directions: (a) accept-then-adjudicate — let a validated
verbatim candidate through and let the file-level guard adjudicate the
one-side shape with full context; (b) best-of-N recovery — when a
heuristic-forced retry produces candidates that validate worse than the
rejected one, restore the rejected candidate instead of escalating;
(c) make the heuristic churn-aware (a loser churn that is a pure
deletion of base content is more likely superseded than a loser churn
that adds new functionality).

Questions: Is accept-then-adjudicate sound, or does letting verbatim
candidates through shift too much burden onto the file-level guard (it
fires only on both-sides-rewrote shapes)? Is best-of-N recovery
acceptable within a conservative-by-construction system (it re-accepts a
candidate a safety heuristic rejected — we'd gate it on the retry
candidates being strictly worse by validation)? Precedents from merge
tools or LLM-repair literature on retry-induced degradation?

### Q2 — tokio-0109: the oracle was directly available; three defects stacked, one unexplained

The case: a 633-line Rust file, both sides enlarged it (base 299 → cur
633 / rep 559). **Oracle == current verbatim** (jaccard 1.0; the oracle
builds). All three runs (first, DNS-contaminated batch; never re-run
clean) escalated through the same stack:

1. units 1:0/1:1 resolved `current_only` via the source portfolio;
   unit 1:2's LLM call died on transport → the now-fixed empty-fallback
   took `current_only` too;
2. the whole-file `cargo check` then failed with **2 new errors**
   (`#[deprecated]` on trait impl blocks; a `ManuallyDrop` misuse);
3. `whole_file_repair` skipped itself: tiered fault attribution found
   the error lines outside all unit spans, and the carve-out that would
   allow one bounded model repair only recognizes `build_test`
   validators — cargo-check failures carry `validator="syntax"`;
4. escalate.

The unexplained part: **neither error token appears in any side of the
conflicted file** (base, current, replayed, or the marker text). A
splice of current-side content plus auto-merged common context cannot
introduce tokens absent from all sides. So the errors enter from outside
the modeled conflict — the surrounding replay series' worktree state, or
a splice artifact we have not reproduced. We cannot currently localize
this remotely; the rerun plan preserves worktrees mid-flight to answer
it.

The design gap regardless of provenance: when the whole-file compile
gate fails, capybase never tries the pristine stage sides as repair
candidates. The true-side portfolio machinery exists (verify both whole
sides, pick via LLM adjudication with a churn fallback) but engages only
on duplicate-pathology or asymmetric-churn (≥0.90) triggers — this case's
churn ratio is 0.18.

Questions: Should "try both pristine stage sides (compiler-gated, one
adjudication when both pass)" be an unconditional whole-file repair rung
on compile failure? How would you gate it against silently dropping
legitimate replayed work in the 68.5% woven class — is the existing
subsumption adjudication sufficient? Any known techniques for
attributing crate-level compile errors to a specific file's resolution
when error locations point elsewhere?

### Q3 — the class question: splice-first reconstruction vs. whole-side availability

Corpus: 31.5% of cases have an oracle that is one side verbatim. The
pipeline's core loop is per-unit resolution → splice into the worktree →
git auto-merge applies the rest → whole-file verify. In every ==current
failure above, that reconstruction was the lossy detour: units chose
correctly, and the file still ended wrong (auto-merge context, splice
artifacts) or the budget died before verification could matter.

We deliberately have no early "just take a side" path below the
calibrated churn bands: the corpus shows churn numbers alone can't
separate "side IS the oracle" from "real woven merge" (79 woven-band
counter-examples overlap the same shape metrics — that calibration killed
our hard-guard temptation in sprint-18 WS4).

Questions: Is there a cheap, safe probe we're missing — e.g., "compile
both pristine sides; if exactly one compiles clean and the buffer fails,
adjudicate the clean side against the buffer" — that stays honest in the
woven class? Do merge-tool precedents (rebase `-s ort -X ours/theirs`,
rerere, wholesale-file merge drivers) offer a principled rule for when
whole-side substitution is safe? What's the right cost model when the
"compile" step is the expensive thing (see 0067/0071)?

### Q4 — protobuf-0055: will entity-splitting actually land this case?

The only woven-oracle C++ failure. The unit's resolution prompt measures
15.5-16.3K tokens against an 8K window; the oversized guard escalates
the unit before any candidate exists, so the file-level repair (where
the micro-patch lives) is unreachable — there is no build error yet to
anchor a micro-patch on. Oracle shape: woven, but 99.6% replayed
(jaccard 0.996 vs 0.850 to current) — nearly "take replayed plus a
thread of current".

We have a measured splitting design
(`docs/oversized-splitting-design-v3.md`, grounded in the sqlite corpus:
entity-boundary sub-conflicts are splice-safe and bring oversized marker
blocks under the window; intra-conflict diffing was refuted by the data).
Open for 0055 specifically: its oversized region is a C++ class
definition with member functions — splitting at top-level entity
boundaries may produce one giant fragment (a class is one entity), which
the v3 sqlite data never exercised.

Questions: Should class-with-methods regions split at member-function
boundaries (access-specifier aware), and does that stay splice-safe in
our sense (non-overlapping spans, no reordering)? Separately — given the
oracle is 99.6% one side, is there a principled "near-verbatim band"
(oracle ≈ one side at jaccard ≥ 0.99) that deserves its own calibrated
path, or is that indistinguishable in-shape from genuine woven merges
that happen to be dominated? We can measure the corpus answer to the
second question and will; we'd value design input on what to do if the
band is clean.

### Q5 — protobuf-0065: gate precedence when the compiler indicts a merge we wrote

The concrete open rule: when the final build gate fails (rc≠0) and
extracted error lines attribute to a file the session wrote, should
capybase escalate even when the user configured `tests.required=False`
(advisory gates)? The config contract says advisory; the design
principle says the compiler is the authority and a silent wrong merge is
the worst outcome. Our instinct: escalate on positively-attributed
in-file errors, keep advisory behavior for unattributable failures
(sibling/environmental) — the attribution classifier already exists.
Questions: is user intent (advisory gate) ever a legitimate override for
a positively-attributed compile defect in the merged file? What
attribution precision would you require (exact file match vs.
merge-touched-file vs. any-file-in-commit)?

### Q6 — build-economics: how far can verification be safely degraded?

For 0067/0071 our direction is: after one full-build timeout at a given
cap, skip further full builds for the session (fall to syntax-only),
journal everything. The alternative is investing in making builds
complete (ccache across majority-of-3 repeats, `-j$(nproc)`, one
prewarming build at preflight). We plan both, but the first is a
verification-weakening trade: on a repo where builds can't complete,
syntax-only is the strongest signal available — we believe re-running a
build that just timed out at the same cap adds zero information.
Questions: do you agree that's sound? Any failure mode where the second
full-build attempt would have succeeded meaningfully (e.g., lock
contention, flaky compilers) that argues for one retry rather than
zero?

## What we will not do (constraints)

- No guard relaxation: the resurrection and side-collapse guards were
  right in every observed instance; their thresholds are corpus-derived.
- No fallback endpoints or auto-created calibration profiles.
- No threshold or gate-semantics change without corpus calibration
  first (the sprint-18 discipline; it killed two tempting designs
  already).

## Artifacts and reproduction

- Case definitions: `extracted-testdata/realworld/<case>.json`
  (base/current/replayed/marker text + the human oracle).
- Flight journals (both batches, preserved):
  `/tmp/capybase-live/s18/val/flights*/flights/<case>/<run>/journal.jsonl`.
- Result JSONs: `/tmp/capybase-live/s18/val/{ws1,guards}*.json`.
- Full diagnosis with code seams:
  `docs/sprint19-failing-cases-diagnosis.md`.
- Planned post-fix rerun (no new mechanisms needed for 0027/0109):
  majority-of-3 under the provider config, worktrees preserved
  mid-flight to localize 0109's unexplained error provenance.

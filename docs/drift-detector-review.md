# Semantic Drift Detector — External Review Brief

## Status

**Under review.** This document describes a feature whose current implementation
produces false positives on every scenario in our live eval (including correct,
deterministically-resolved merges). We are sending it for external review to
survey proven techniques and related research before reworking the approach. The
goal is a drift signal that is trustworthy enough to act on — currently it is
not.

---

## What it is

The semantic drift detector is an **advisory, non-blocking** session-level
monitor that watches for *cumulative* semantic divergence during an automated
rebase. Its purpose is to catch a failure mode that no per-commit validator can
see: the gradual drift of a branch away from its intended purpose across a
sequence of merges, where each individual merge looks correct in isolation but
the cumulative effect diverges from the original goal.

### The failure mode it targets

Consider a rebase that replays 5 commits onto a new base. Each commit's merge is
validated independently — syntax checks pass, entity preservation holds, tests
are green. But suppose the model, across retries, has subtly shifted the merged
code: a method was inlined that should have stayed separate, an import was
dropped and re-added in a different form, a configuration value drifted. No
single commit's validator catches this, because each merge is locally correct
relative to its own base/current/replayed sides. The drift only becomes visible
when you compare the *cumulative outcome* against the *original intent*.

This is the blind spot the detector exists to fill. Every other validator in
the system is per-commit and pairwise (base vs. merged). The drift detector is
the only component that reasons across the whole session.

### Why we want it

In a CEGIS (counterexample-guided inductive synthesis) loop with a constrained
small model, the model's repair attempts can converge on a locally-correct but
globally-wrong solution. The drift detector is meant to surface this as an
advisory — "the merged code has drifted N% from the branch intent" — so a human
or a higher-authority check can intervene. It is explicitly advisory: it never
blocks a merge, never escalates, never mutates state. It only journals a signal.

---

## How it is currently implemented (high level)

The detector compares two embeddings over the course of a rebase session:

1. **The anchor** — embedded once at session start. The anchor text is the
   *branch intent*: a concatenation of the source commits' subjects and body
   summaries, plus a rendered "branch intent" summary block (a natural-language
   description of what the rebase is meant to achieve). This is **prose**.

2. **The probe** — embedded after each commit's outcomes are recorded. The
   probe text is the **merged resolved code** (the actual source text of the
   accepted resolutions for that step, concatenated). This is **code**.

The detector computes the cosine *distance* (1 − cosine similarity) between the
anchor vector and each probe vector. When the distance exceeds a threshold
(currently 0.20, i.e. similarity below 0.80), it emits a drift advisory. A
running cumulative-max is tracked across the session.

The embedding model is a general-purpose text embedder (Qwen3-Embedding-0.6B)
that handles both code and natural language.

---

## How it performs in current evals

**It fires on every scenario, including correct merges.** In the most recent
live eval (4 scenarios, 4/4 correct, 0 wrong-merges, 0 escalations), the drift
detector emitted a `semantic_drift` advisory on all four:

| scenario | resolved how | correct? | drift distance | threshold | fired? |
|---|---|---|---|---|---|
| py_simple | deterministic (exact reuse, 0 LLM calls) | ✅ | 0.29 | 0.20 | yes |
| py_multi_unit | deterministic (exact reuse, 0 LLM calls) | ✅ | 0.41 | 0.20 | yes |
| rust_impl | LLM (whole-file repair, accepted) | ✅ | 0.45 | 0.20 | yes |
| rust_port_test | deterministic (exact reuse, 0 LLM calls) | ✅ | 0.40 | 0.20 | yes |

Two of these scenarios were resolved with **zero model involvement** — a
deterministic replay of a prior accepted resolution. Drift is impossible by
construction in those cases (the output is a verbatim copy of a previously-
validated resolution). Yet the detector fired on them too.

---

## How it is failing (the core problem)

The fundamental flaw is a **cross-domain comparison**: the detector measures the
cosine distance between a **prose intent summary** (the anchor) and **merged
source code** (the probe). Prose and code occupy different regions of the
embedding space even for a perfectly-correct merge — there is no reason a
natural-language description of intent and the source code that realizes it
should be cosine-similar. A high distance is the *expected* baseline, not a
drift signal.

Concretely:

- **The anchor is prose.** Commit subjects ("fix retry handling"), body
  summaries ("Config now tracks max_retries..."), and an intent render block
  ("This rebase adds timeout support and updates the label format...").
- **The probe is code.** `pub struct Config { pub name: String, pub max_retries:
  u32, ... }` — Rust source text.

Even a perfect merge produces code that is embedding-distant from the prose that
describes it. The 0.29–0.45 distances observed are the *floor* for this
comparison, not evidence of drift. The threshold (0.20) is below that floor, so
the detector cannot distinguish a correct merge from a drifted one — it fires on
both.

Additionally:

- **No baseline calibration.** The 0.20 threshold was chosen as "similarity
  0.80 ≈ a conservative starting point" without measurement against a corpus of
  known-correct and known-drifted merges. There is no evidence that any
  threshold separates the two populations, because the comparison itself
  conflates domain difference with semantic drift.
- **Cumulative tracking is uninformative.** The cumulative-max distance just
  records the worst single-commit distance, which — given the false-positive
  baseline — is always above threshold. It provides no signal about *trend*
  (is drift increasing?) or *localization* (which commit caused it?).
- **No distinction by resolution mechanism.** A deterministic exact-reuse (zero
  model involvement, impossible to drift) fires identically to an LLM-generated
  merge (the actual drift-risk case). The detector does not gate on how the
  resolution was produced.

### What would count as real drift

A genuine drift signal would detect: the model silently dropping a feature that
the intent says must be preserved; the merged code reverting to an older form
despite a commit that updated it; the cumulative merge accumulating unrelated
changes. None of these are distinguishable from the cross-domain baseline noise
in the current design.

---

## What we want from the review

We are looking for survey and guidance on:

1. **Proven techniques for detecting semantic drift in code-transformation
   pipelines.** How do existing systems (automated refactoring tools, merge
   bots, program-repair CEGIS loops, continuous-integration drift monitors)
   detect when transformed code has diverged from its intended semantics? What
   signals do they use (tests, behavioral equivalence, static analysis,
   embedding-based similarity) and what are their false-positive rates?

2. **Whether embedding-based code-vs-intent comparison can be salvaged, and
   how.** Is comparing a prose intent to merged code fundamentally unsound, or
   can it work with a different anchor methodology (e.g. embedding the *base*
   code rather than the intent prose; same-domain code-to-code comparison; a
   dual-encoder that maps prose and code into a shared space)? What does the
   research say about cross-modal code/text embedding similarity for
   correctness assessment?

3. **Alternative drift signals worth considering.** Given that our system
   already has per-commit validators (entity preservation, syntax checks, test
   gates), what *additional* session-level signal would genuinely catch the
   cumulative-drift blind spot? Candidates we are aware of but have not
   evaluated: behavioral/test-based drift (run the branch's test suite against
   the cumulative merge), representation-diff against the target branch,
   static-analysis delta, or treating drift detection as an anomaly-detection
   problem over a corpus of known-good rebases.

4. **Calibration methodology.** If an embedding-based approach is retained, how
   should the threshold be calibrated? What corpus and ground-truth labels are
   needed, and what separation between correct and drifted populations is
   achievable in practice?

The output we need is a grounded recommendation on whether to rework the
current embedding-based approach, replace it with a different technique, or
conclude that session-level drift detection is not reliably achievable with the
signals available to a lightweight, model-constrained merge agent.

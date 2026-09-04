# Design: Deterministic-Layer Reuse & Structure (v1)

Status: PROPOSED, first stage IN PROGRESS (sprint-27, pre-harvest
window). Analysis of the external reuse proposal against the as-built
system, with every factual claim verified against the code first.

## Claim verification (all three confirmed)

1. **`_normalize()` erases indentation — CONFIRMED, real correctness
   bug.** `" ".join(line.split())` per line normalizes
   `if ready:\n    start()\nstop()` and `if ready:\n    start()\n
   stop()` identically — `stop()` changing scope reads as "unchanged".
   Feeds: `identical_sides` (merges the sides, picking one text),
   `one_sided_change` (can discard the indented side's change),
   lint-vs-refactor's changed-check, and the zone resolver. For
   Python/YAML (indentation-semantic) this can silently merge or drop
   a semantic change.
2. **Alias maps re-spelled — CONFIRMED.** Four sites maintain their
   own alias knowledge (config.py:1380 `{"py","rs","js","ts"}`,
   resolution_engine's token sets, structural_resolver's language
   list, orchestrator:1639 `("rust","rs")`). Exactly the drift class
   the s27 langs.py consolidation attacks; this completes it.
3. **Global name scans in keyed unions — CONFIRMED.**
   `named_field_union` builds `existing_field_names` from the whole
   resolved text: a same-named field in ANY struct suppresses the
   insertion into the destination struct. Judgment: this is a
   COVERAGE bug, not a safety bug (it fails toward not-inserting →
   escalate). The scope-qualified identity fix belongs with the
   KeyedCollectionMerge engine (stage 2), not a pre-harvest patch.

## Verdict on the proposal

**Adopt the architecture; stage it honestly.** The central principle
is right and matches everything s27 built toward: *syntax discovery
may be language-specific; merge algebra, transactions, evidence, and
acceptance policy should not be.* The D0–D3 safety classes fill a real
gap — the code does assign `self_reported_confidence=0.85` to
deterministic repairs, and our acceptance tier A's "deterministic"
conflates exact algebra with reproducible heuristics (SBCR is
D3-heuristic; exact reuse is D0). The classes compose with the
acceptance policy: tier A should require D0/D1 provenance.

**Adopt now (stage 1, pre-harvest, bounded):**
- The correctness fix + regression test (claim 1) — a real bug, minutes
  to fix, verified by the corpus + gates.
- Catalog-derived alias resolution (claim 2) — completes langs.py.
- `SafetyClass` (D0–D3) as a typed field on candidates; acceptance
  tier A refined to require D0/D1.
- The conformance-test checklist (as tests on whatever engine exists).

**Adopt next (stage 2, post-harvest or as a bounded follow-up):**
- `KeyedCollectionMerge` + `EditTransaction`/`TextEdit`/
  `PrimitiveResult` (the proposal's P1) — the biggest honest dedup;
  ported in its order (manifest arrays → fields → items → attributes →
  imports) under SHADOW MODE with divergence recording before any
  switch. The scope-qualified identity fix (claim 3) rides this.
- One orchestrator-inline repair ported behind the mechanisms registry
  as the pattern proof.
- Capability-based language checks replacing name checks (the
  `LanguageProfile` end-state), extending langs.py incrementally.

**Bounded/deferred with cause:**
- The full package restructure (deterministic/ syntax/ languages/...):
  right target, wrong moment — a wholesale rename churns every import
  pre-harvest. New contracts land in place; the tree reorganization
  happens once the engines exist to fill it.
- LexicalProfile compositional split of the canonical lexer: the lexer
  works and is already the consolidation success story; the PHP family
  drift is real but small. Tier-3.
- P2 (parser decomposition, new-language codecs): post-harvest by
  definition — new-language coverage does not affect a 4-language
  corpus measurement.

## Stage 1 checklist

1. [x] Claim verification (above).
2. [ ] `_normalize` → language-aware: an EquivalencePolicy-shaped fix
   (indentation-sensitive languages preserve leading whitespace;
   brace-family keeps horizontal collapse ONLY as a declared cosmetic
   rule for the lint check; unknown text = line endings only).
   Regression test from the proposal's example. Corpus + gates after.
3. [ ] Alias resolution from one source (langs.py); the four sites.
4. [ ] SafetyClass + acceptance refinement.
5. [ ] Ledger + this document updated.

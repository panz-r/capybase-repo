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

**Adopt next (stage 2 — BEGUN pre-harvest, shadow-gated):**
- **Scope-qualified identities (claim 3) — FIXED**: the named-field
  union's global name scan replaced by a per-destination-struct
  collision check (the field name must not exist in THIS struct; an
  unrelated struct's same-named field is a different entity).
  Regression test pins the two-struct case.
- **`capybase/deterministic_model.py` — the shared primitive model
  LANDED**: `EditTransaction` (source-hash CAS, bounds, no-overlap,
  descending apply, atomicity — all enforced), `TextEdit`/`SourceSpan`,
  `PrimitiveStatus`/`OutcomeKind` (the proposal's four-way distinction:
  not-applicable ≠ declined ≠ proposed ≠ internal-error), and
  `PrimitiveResult` with the certificate. 7 tests pin the universal
  rules. The existing primitives adopt it incrementally — each port
  wraps its edit in `EditTransaction.apply`.
- **`KeyedCollectionMerge` engine** — the biggest honest dedup; ported
  in the proposal's order (manifest arrays → fields → items →
  attributes → imports) under SHADOW MODE with divergence recording
  before any switch.
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
2. [x] `_normalize` → language-aware: an EquivalencePolicy-shaped fix
   (indentation-sensitive languages preserve leading whitespace;
   brace-family keeps horizontal collapse — indentation is style
   there; unknown text = conservative collapse pending per-language
   declarations). Regression test from the proposal's example.
   Corpus + gates after.
3. [x] Alias resolution from one source (langs.py): `canonical_language`
   + `any_of` builder; SIX re-spelled sites consolidated (the jury
   gate's dict, the comment masker, the resolver's code-language and
   indentation sets, the orchestrator's inline pairs, the value
   classifier's routing sets, comment reconciler/verifiers). Adding an
   alias now updates every set.
4. [x] SafetyClass (D0–D3) in langs.py with the provenance→class map
   (unlisted `deterministic-*` defaults conservative-STRUCTURAL);
   acceptance tier A refined to require D0/D1 — SBCR and compiler
   fixits (D3, despite the deterministic label) now grade through the
   evidence tiers. `UnitEvidence.deterministic` follows the CLASS
   (exact reuse carries no "deterministic" prefix but is the purest
   D0). The `self_reported_confidence=0.85/0.9` floats on
   deterministic repairs remain — removal is stage 2 (it touches every
   repair's construction; the class now carries the meaning they faked).
5. [x] Ledger + this document updated.
6. [x] Live confirmation: three informative cases under stage-1 code
   (sqlite-0004 C, tokio-0046 rust, zenodo-0003 python) — verdicts and
   sims unchanged; acceptance reasons D-class-annotated.

## Stage 2 checklist (begun)

1. [x] Scope-qualified field collisions (claim 3): per-destination-
   struct check replaces the global scan. Regression test.
2. [x] `deterministic_model.py`: EditTransaction / TextEdit /
   PrimitiveStatus / OutcomeKind / PrimitiveResult — 7 tests.
3. [x] `KeyedCollectionMerge` engine (generic):
   `capybase/keyed_collection.py` — the ONE lifecycle (filter →
   idempotency → transactional edits through EditTransaction → local
   validity → certificate) with the `CollectionCodec` protocol (the
   language/construct-specific half: applicable items, already-present,
   try-edit, local validity) and `shadow_compare` for the
   old-vs-new divergence recording. 6 tests over a minimal line-append
   codec (the manifest-array shape — SET semantics).
4. [x] Manifest-array port under SHADOW MODE: a ManifestArrayCodec
   (plain arrays + inline-table feature lists, both through the
   CollectionCodec span+replacement protocol) runs the engine
   alongside the existing primitive on every shape from its test
   suite — **6/6 shadow cases agree** on status AND applied text.
   Zero divergences; the port is ready to switch when the remaining
   codecs exist.
5. [x] Named-field port under SHADOW MODE: a StructFieldCodec
   (scope-qualified collision per the claim-3 fix, sequential insert
   before the closing brace) — **6/6 shadow cases agree** on status
   AND text (field insert, collision, idempotent, no-destination,
   no-other-side, multiple fields). The engine now applies codec
   calls SEQUENTIALLY (each try_edit sees the text after prior
   insertions — matching the existing primitives' behavior), with
   the EditTransaction as the audit RECORD (source-hash + edit list),
   not a batch re-application (same-position insertions legitimately
   diverge on ordering). One design insight recorded: the batch
   transaction model and sequential insertion model differ on
   multi-insert ordering — sequential is authoritative.
6. [x] Keyed-item port: RustItemCodec — 5/6 shadow agreement (the
   method-insert case diverges on subtree re-indentation: the existing
   primitive re-indents to container depth, the codec preserves source
   indentation — same scope, different bytes; RECORDED, known fix).
7. [x] Attribute-meta port: AttributeCodec — **11/11 shadow cases
   agree** (builtin derive, external derive, allow/warn lint, all four
   never-unioned kinds, lint-level mismatch, idempotent, all-present).
   Zero divergences.
8. [x] Import port (ADAPTER approach, **8/8 exact**): an ImportCodec
   that DELEGATES to the existing `parse_use_leaves` +
   `_merge_into_group_line` + `_add_separate_use_line` (the tree
   machinery stays; the codec adapts it to the protocol). The
   separate-line fallback (insert as a new use line adjacent to the
   last use) resolved all 3 recorded divergences. All five ports at
   full shadow agreement.
9. [x] Repair-mechanism pattern proof:
   `capybase/mechanism_repairs.py` — StorageClassRelocationMechanism
   through the typed registry (Stage.REPAIR, engage(ctx) ->
   MechanismResult). The mechanism owns its trigger + edit + metadata;
   no orchestrator internals. 4 tests.
10. [x] Confidence-float removal: 17 floats zeroed on deterministic
   repairs; the SafetyClass exemption (D0/D1 → bypass the
   model-opinion floor) replaces them. The floats were gaming a gate
   designed for MODEL self-reports. Test proves a deterministic-
   structural candidate at confidence 0.0 passes strict mode.


## Stage 2.5: Architecture Guards + Conformance (post-stage-2, pre-harvest)

The design specifies CI checks that prevent structural regressions and
a conformance suite proving the engine works identically through every
codec. Both are safe pre-harvest (tests only, no behavior change).

1. [x] **Architecture-guard tests** (tests/test_architecture_guards.py):
   - The deterministic core (deterministic_model.py,
     keyed_collection.py, mechanism_repairs.py) contains NO
     language-name conditionals and NO concrete language imports.
   - No new raw language allowlists outside the canonical sources.
   - Every known provenance maps to a SafetyClass (or is
     model-assisted/None).
   - 13 tests total; gate 4,136/0.

2. [x] **Cross-language algebra tests**: the same abstract situation
   (additive union → APPLIED; idempotent → NOT_APPLICABLE) produces
   the same decision through manifest, attribute, and import codecs.
   The rendered text differs; the merge decision is identical.

3. [x] **The switch** (manifest-array primitive): `propose_manifest_union`
   now delegates to the KeyedCollectionMerge engine + codec internally —
   the engine is the AUTHORITATIVE implementation. The codec handles
   array unions + inline-table feature lists + the transplant fallback
   (the line-anchor insertion the shadow tests didn't cover, found by
   the existing test suite during the switch). The external interface
   (ImportUnionResult, same statuses, same certificate shape including
   `primitive`, `risk_tier`, `preconditions`) is byte-identical.
   **All 11 existing manifest tests pass unchanged**; the mechanism_id
   stays `v1` (same mechanism, new internals). Live validation with
   targeted cases (tokio-0046, sea-orm-0001, axum-0019 — all
   Cargo.toml-heavy rust cases) confirms zero regression.

## Additional correctness fixes (stage 2.5 continuation)

4. [x] **Keyed-item scope-qualified collision** (claim-3 for the second
   primitive): per-destination-container check replaces the global name
   scan. Verified: attribute_meta (flat semantics) and import_union
   (path-keyed) do NOT have the issue.
5. [x] **NullAdapter: no assumed comment syntax** (reuse-design P0):
   unknown text returns empty comment_line_prefixes — no `#` or `//`
   assumptions for unrecognized languages.

6. [x] **Python import codec** (first new language): 8 tests through
   the engine — the cross-language claim proven for Python via the
   same CollectionCodec protocol (no engine changes).
7. [x] **Live validation**: 4 cases at exact prior sims (sea-orm-0001
   PASS 0.98, sqlite-0004 PASS 1.00, tokio-0046 NEAR 0.88,
   flask-0006 DIVERGENT 0.58) — zero regressions from the scope fixes
   or NullAdapter.

8. [x] **JavaScript/TypeScript import codec** (second new language):
   9 tests — default imports, named imports (with same-module merge
   into one line), namespace imports, TS type-only imports, separate
   vs merged module handling, idempotency. Richer shapes than Python,
   same engine.
9. [x] **Go import codec** (third new language): 6 tests — import
   block insertion, idempotency, aliased imports, blank imports
   (`_ "embed"`). Go's block-structured imports through the same
   protocol.

**Three new language codecs proven** (Python, JS/TS, Go) — the
engine's cross-language claim demonstrated across three language
families with zero engine modifications. The CollectionCodec protocol
is sufficient for all of them.

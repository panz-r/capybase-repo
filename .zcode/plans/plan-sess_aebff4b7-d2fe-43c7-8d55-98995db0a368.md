# Fix the grammar-free abstract parser: correctness, robustness, and cleanup

## Goal
Eliminate the seven silent-wrong-output bugs in `abstract_parser.py`, the three quality-degradation issues, and the three cleanup items identified in the review — then lock them down with strengthened unit tests. The parser is a cornerstone (every structural merge path flows through it), so each fix is empirically driven and individually testable.

## Design decisions (from user)
- **Duplicate names (#3): decline + escalate.** When duplicate identities reach the 3-way diff or `entity_disjoint`, return `None` so the conflict escalates to the line/LLM path rather than silently dropping a unit. Matches the parser's "robustness over correctness, never silently wrong" philosophy. Two localized sites, zero rename-detection regression risk.
- **Scope: all 13** (7 correctness + 3 robustness + 3 cleanup).

---

## Part A — Family B (Python) correctness (`parse_family_b`)

### A1. Multi-line signatures — fix #1
Add **bracket-depth continuation awareness**. Track `paren_depth`, `bracket_depth`, `brace_depth` across the scan (string-aware so a `(` inside a string doesn't count). When any is `> 0`, the line is a continuation: **do not** advance `last_line_row`, **do not** run the dedent/close logic — the current open unit simply absorbs the line into its body (which is computed by source slice anyway).

- New module-level helper `_line_continues(raw)` returns the net bracket delta `(opens - closes)` for a line (string-literal-aware via `_STRING_LIT_RE` blanking).
- Maintain `cont_depth: int` in the scan loop: `cont_depth += delta(raw)`; clamp at 0; a line with `cont_depth > 0` *before* processing (or where the delta itself is net-positive on the signature line) is a continuation.
- Careful ordering: the `def long(...)` line itself opens a paren (delta +1); the continuation lines keep depth > 0; the `) -> bool:` line closes it (delta -1 → back to 0) but is itself the *last* continuation line, so it must still be absorbed into the body and NOT trigger a close. Rule: **if `cont_depth > 0` at any point during the line (after applying its delta), the line is a continuation.** This makes `) -> bool:` (which is at depth 1→0) a continuation line, absorbed into the body, and the *next* real code line (`return True`) at indent 4 correctly extends the unit.

### A2. Decorator span over-reach — fix #2
A decorator line is a **scope boundary for the *previous* unit** (the next declaration is a new sibling). Currently `last_line_row = i` is set at line 553 *before* the decorator check at 555, so the previous unit's end advances through the next unit's decorator. Fix: reorder so a decorator line (a) closes the previous unit against `prev_line_row` if the decorator is at-or-below the previous unit's indent, (b) does **not** advance `last_line_row`, and (c) records `pending_decorator_start` as before. This makes `@x.setter` belonging to the second `x` method correctly terminate the first `x` at the prior meaningful row.

### A3. Triple-quoted-string phantoms — fix #6
Add a **string-state tracker** for Family B (it currently has none). Track `triple_str: str | None` (one of `'"""'`, `"'''"`, `None`) across lines. A line inside an open triple-quote string is treated as a continuation (absorbed, no declaration detection) until the closing triple-quote. Toggled on the same `_STRING_LIT_RE` that `_normalize_body` already uses. This prevents `class Fake:` / `def method()` lines inside a docstring from being parsed as units. This composes with A1 (continuation): a line is a continuation if `cont_depth > 0` OR `triple_str is not None`.

### A4. Fragmentation false-positive on large test files — fix #8
In `_assess_confidence`: when computing the fragmentation check, **exclude `is_test` units** from the count, OR (simpler, more robust) skip the fragmentation flag entirely when the *majority* of units are `is_test`. A 60-test module is normal, not pathological. Concretely: `non_test_units = [u for u in units if not u.is_test]`; flag fragmentation using `len(non_test_units) > n_lines / _FRAGMENTATION_RATIO`. A test-only file with 60 tests now parses at confidence 1.0.

---

## Part B — Family A (brace-delimited) correctness (`parse_family_a`)

### B1. Go receiver-method names — fix #4
In `_classify_a_brace`, after finding `last_kw` in `_A_FUNC_KEYWORDS`, if the token immediately after the keyword is `(` (the receiver-paren shape `func (recv) Name(`), the **real method name is the identifier just before the final `(...)` param list**. Reuse the keyword-less-method name-extraction logic (find last balanced `()`, take the token before it). Add a Go-specific (or general) branch: `if after and after[0].startswith("(")`. This recovers `Start` from `func (s *Server) Start()`.

### B2. Go `type X struct` misclassification — fix #5
Go puts the name *before* `struct`/`interface`. In `_classify_a_brace`, when the buffer is `type Name struct` (Go type declaration), the name is the token **between** `type` and the class keyword. Add a guard: if `last_kw in _A_CLASS_KEYWORDS` and `after` (tokens after the keyword) is empty or just `{`, look **backwards** for a name preceded by `type` (Go) — `name = toks[last_kw_idx - 1]` when `toks[last_kw_idx - 2] == "type"`. Also remove `"type"` from `_A_FIELD_KEYWORDS` *for the `_emit_a_field_units` path only* when followed by `struct`/`interface`, so the struct isn't double-counted as a FIELD. (The `type` field-keyword stays valid for Rust/TS type aliases.)

### B3. Raw / byte / hash / verbatim strings — fix #9
Add a **string-prefix table** to the Family A char scan. Before checking `ch == '"'` to enter string state, look back for a prefix rune (`r`, `b`, `rb`/`br` for Rust; `@"`, `$"`, `@$"`, `$@"` for C#; `f"`, `rf"`, `fr"` etc.). When a prefix is detected, enter string state but record the **expected closer** (`"` for most; `"#`-with-matching-`#`-count for Rust raw `r#"..."#`). On the closer scan in the `in_str` block, honor the recorded closer. This makes `r#"{ brace }"#` and `@"line { }"` close correctly. Pure, additive to the state machine — adds one `str_closer: str | None` and a small prefix-detection helper.

### B4. Fold `_emit_a_field_units` into the main pass — fix #10 (cleanup)
Currently fields are detected in a *second* whole-file line scan with its own `_strip_strings_line` mini-scanner (a duplicate string-state tracker that can drift from the main one). Since the main scan already accumulates the token buffer and knows `brace_depth == 0`, emit the FIELD unit directly at the `;` terminator (when a field keyword is present in the buffer and no declaration brace opened). Delete `_emit_a_field_units`, `_strip_strings_line`, and the post-scan call. This removes ~80 lines and the second scanner. **Risk:** must preserve the exact field-regex behavior (modifier prefixes, name capture); the existing `test_family_a_const_is_field` and `test_rust_import_export_surface` pin it.

---

## Part C — Cross-cutting correctness (3-way diff + identity)

### C1. Duplicate-name collision — fix #3 (decline + escalate)
Two sites:
1. `compute_structural_diff_3way` (abstract_parser.py ~1788): after building `base_by_id`/`left_by_id`/`right_by_id`, check for **duplicate identities within a single version's flat unit list**. If any version has two units with the same identity (e.g. two `(method, "f")`), return `None` (decline). The caller in `resolution_engine.py` already treats `None` as "no structural signal" and the conflict escalates. Add a `_has_duplicate_identities(units) -> bool` helper.
2. `entity_disjoint` rule (structural_resolver.py ~1041): before building `base_by_id`, if `base_ents`/`cur_ents`/`rep_ents` contain a duplicate identity, `return None` (decline → escalates to line/LLM). Same helper, imported from `abstract_parser` to avoid duplication.

This is the safest fix: zero change to the rename-detection dicts, zero change to the successful-merge path, and duplicate-name methods (uncommon in conflict zones) get a correct LLM merge instead of a silently-truncated one.

### C2. `added_both` with different bodies — fix #7
In `_classify_alignment`, when `not has_b and has_l and has_r`: sub-classify using `_bodies_differ(left, right)`:
- same body → `ADDED_BOTH` (genuinely agreed addition)
- different body → a new `ADDED_BOTH_CONFLICT` kind, which **counts as a structural conflict** (`structural_conflicts` property includes it). The context annotation renders it as "ADDED BY BOTH SIDES (different bodies) — synthesize". Without this, a same-name same-line two-sided addition with conflicting bodies is silently treated as non-conflicting.

### C3. Broken fingerprint guard — fix #13
`_detect_renames` uses `u.fingerprint != f"l{u.body.count(chr(10))}"` to skip content-less bodies, but this is already broken (verified: a 1-line body has fingerprint `l1:...` vs the guard's `l2` — never equal, guard never skips). Replace with the correct check: a fingerprint is content-less when it matches the pattern `l\d+` (no `:digest`). Use `u.fingerprint and ":" not in u.fingerprint` to skip. This prevents many distinct content-less bodies (e.g. `pass`-only methods, docstring-only functions) from all colliding under fingerprint `l0` and producing false rename pairings.

---

## Part D — Language map + adapter consolidation (cleanup)

### D1. Consolidate the two divergent language maps — fix #11
`conflict_extractor._EXT_LANG` (20 entries) and `abstract_parser._EXT_LANG` (24 entries) disagree. Make `language.py` the single home for extension→language mapping (it's already the designated home for language behavior). Add `EXTENSION_TO_LANGUAGE: dict[str,str]` to `language.py` = the union of both maps. Have `conflict_extractor.detect_language` delegate to it. Have `abstract_parser` import it for `_EXT_LANG` (or keep its family-keyed `_LANG_FAMILY` but source the ext→lang step from `language.py`). Single source of truth; no behavior change for any currently-recognized extension.

### D2. Register adapters for all parser-supported languages — fix #12
The parser supports 20 languages but `language.py` only registers Python + Rust; every other language gets `NullAdapter` (comment_prefix `"#"` — **wrong** for all brace languages, which use `//`). Register adapters for: JS/TS, Go, Java, C/C++, C#, Kotlin, Swift, Scala, Dart, PHP, Ruby. Each is a frozen dataclass with the right `comment_prefix` (`//` for brace langs, `#` for Ruby/PHP-shell), `comment_line_prefixes`, `source_extension`, `definition_patterns` (keyword set per language), `container_has_braces`. This fixes silent wrongness: `consensus.py`/`context_builder.py` comment-line detection and `_find_definition_span` symbol search now work correctly for JS/TS/Go/Java/... instead of silently treating `//` comments as code and failing to find definitions.

---

## Part E — Strengthened tests (`tests/test_abstract_parser.py`)

New test groups, one per fix (TDD: write first, confirm red on current code, then green after fix):

**Family B regression suite (~14 tests):**
- `test_multiline_python_signature_body_included` — fix #1: `def f(\n a,\n) -> bool:` then `return True` → span covers the body line; body contains `return True`.
- `test_multiline_signature_with_nested_call` — continuation through nested `(` `)`.
- `test_decorator_does_not_extend_previous_unit` — fix #2: two `@`-decorated sibling methods; first method's span ends before the second's decorator.
- `test_stacked_decorators_on_one_function` — regression: existing behavior (decorators on *same* fn) unchanged.
- `test_decorator_on_class_method` — fix #2 in the class-context.
- `test_triple_quoted_docstring_no_phantom_units` — fix #6: docstring containing `class Fake:`/`def method()` produces zero nested units.
- `test_triple_quoted_string_assignment` — fix #6 for module-level multi-line strings.
- `test_fragmentation_not_triggered_for_test_file` — fix #8: 60 `test_*` functions → confidence 1.0.
- `test_fragmentation_still_flags_garbage` — regression: a genuinely fragmented (many tiny non-test units in little code) file still flags.

**Family A regression suite (~10 tests):**
- `test_go_receiver_method_name_recovered` — fix #4: `func (s *Server) Start() {}` → METHOD "Start".
- `test_go_type_struct_is_class` — fix #5: `type Server struct {}` → CLASS "Server", not FIELD.
- `test_go_type_interface_is_class` — fix #5 for `type X interface`.
- `test_rust_hash_raw_string_with_braces` — fix #9: `r#"{ }"#` doesn't open/close scopes.
- `test_csharp_verbatim_string_multiline` — fix #9: `@"...{ ... }..."` across lines.
- `test_field_detection_after_fold` — fix #10: `pub const N: u32 = 5;` still a FIELD after the re-scan removal (pins the refactor).
- `test_rust_field_in_main_pass_no_double_count` — fix #10 regression: a `const` inside a function body is NOT a top-level field.

**Cross-cutting + diff suite (~8 tests):**
- `test_duplicate_method_names_decline_diff` — fix #3: two `(method,"f")` in one version → `compute_structural_diff_3way` returns None.
- `test_duplicate_method_names_decline_entity_disjoint` — fix #3 at the `entity_disjoint` rule.
- `test_added_both_different_bodies_is_conflict` — fix #7: same name added both sides, different bodies → `structural_conflicts` includes it.
- `test_added_both_same_body_not_conflict` — fix #7 regression: identical addition stays non-conflicting.
- `test_rename_detector_ignores_contentless_fingerprints` — fix #13: two `pass`-only methods don't pair as a rename.

**Language map + adapter suite (~6 tests, `tests/test_language_adapter.py`):**
- `test_comment_prefix_for_brace_languages` — fix #12: `adapter_for("javascript").comment_prefix == "//"` (and Go/Java/C/C++/...).
- `test_definition_patterns_go` — fix #12: Go adapter patterns find `func name`.
- `test_language_map_single_source` — fix #11: `detect_language(".cc")` and the parser's family detection agree.
- One adapter smoke-test per newly-registered language family.

---

## Implementation order (dependency-respecting)

1. **Part A (Family B correctness)** — A1, A2, A3, A4 in `parse_family_b`/`_assess_confidence`. Localized; the continuation-awareness (A1) and decorator-reorder (A2) interact (both touch the scan loop body), so do them together. A3 (triple-quote) composes with A1 via the shared "is this a continuation line" predicate.
2. **Part B (Family A correctness)** — B1, B2, B3 in `parse_family_a`/`_classify_a_brace`. B4 (field-detection fold) last, since it's the riskiest refactor and benefits from the prior tests pinning field behavior.
3. **Part C (diff + identity)** — C1, C2, C3. Independent of A/B; touches `compute_structural_diff_3way`, `_classify_alignment`, `_detect_renames` in abstract_parser + the decline guard in structural_resolver.
4. **Part D (cleanup)** — D1, D2. Last; D2 registers adapters and is the broadest change but mechanically simple (frozen dataclasses + registration lines).
5. **Part E (tests)** — interleaved with each part (TDD per fix), then a final full-suite run.

## Verification
- After each part: run `tests/test_abstract_parser.py` + `tests/test_language_adapter.py` + `tests/test_entity_resolution.py` (the identity-collision and rename paths).
- Final: full suite (`pytest -q`). Expected: all currently-green tests stay green (the fixes are bug fixes), new tests pass. No `import difflib` regression (unrelated, but worth confirming since the parser is upstream of the diff paths).
- Empirical spot-check: re-run each of the 7 bug reproductions from the review to confirm they now produce correct output.

## Risk assessment
- **A1 (continuation)** is the subtlest — the `cont_depth > 0`-during-line rule must be verified against nested calls, default-arg lists with brackets, and the close-back-to-0 case. Mitigated by the TDD tests and by the fact that body spans are source-sliced (so an off-by-one in the close logic only affects *where a unit ends*, not the body text correctness).
- **B4 (field fold)** touches working code; the two existing field tests + a new no-double-count test guard it. If the fold proves fragile, fall back to keeping `_emit_a_field_units` and just removing the redundant `_strip_strings_line` — but the fold is the cleaner end state.
- **C1 (decline)** could, in principle, escalate conflicts that previously (silently-wrongly) "merged". That's the intended behavior — a silent miss becomes a correct LLM merge. No regression on actually-correct merges (those don't have duplicate identities).
- **D2 (adapters)** changes `comment_prefix` for 18 languages from `#` (wrong) to `//` (correct); any test that asserted `NullAdapter` for these is asserting a bug and will be updated. No production path is harmed — the change is strictly more-correct.

## Commit plan
One commit per part (A, B, C, D) with tests, following the repo's `type(scope): description` convention — e.g. `fix(parser): multi-line signatures, decorator spans, triple-quote phantoms`. Nothing pushed (per AGENTS.md).
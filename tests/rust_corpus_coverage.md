# Rust Conflict-Resolution Test Corpus — Coverage Map

This is the living taxonomy-coverage table for capybase's Rust conflict-resolution
tests. It maps each dimension of the Rust conflict taxonomy to the concrete test(s)
that cover it, and records the gaps this corpus deliberately does **not** close
(so deferred items stay visible instead of silently dropped).

The synthetic catalog (`tests/rust_conflict_catalog.py`) is the spine: each case
carries both a known-good and a known-broken merge, so the verifier's accept and
reject paths are both exercised from one row (see `tests/test_rust_conflict_catalog.py`).

## How the catalog is built

`build_markers(base, current, replayed)` runs `git merge-file` over the three sides
to produce **authentic** `<<<<<<< / ======= / >>>>>>>` blocks — the exact text the
agent sees, not hand-faked markers. This requires only `git` (no Rust toolchain),
so the well-formedness test set runs everywhere; the compile-accept/reject sets
skip on CI without `cargo`/`rustc`.

## Taxonomy × feature matrix

### A. Textual / structural

| Axis | Catalog ID | What it exercises | Verifier |
|---|---|---|---|
| same-line textual | `same_line_value` | classic `<<<<<<<` on the same line | cargo |
| adjacent insert | `adjacent_insert_use` | both sides add different `use` lines at the same position | cargo |
| whitespace / reformat | `whitespace_reformat` | one branch rustfmt'd, other changed logic | cargo |

### B. Rust-specific semantic

| Axis | Catalog ID | What it exercises | Verifier |
|---|---|---|---|
| signature / type | `signature_return_vs_param` | A changes return type, B adds a param → type error on a half-merge | cargo |
| move + edit (crate-path) | `move_struct_to_module` | leaf file uses `crate::` (the standalone-rustc false-positive regression class) | cargo |
| add/add duplicate | `add_add_const` | both sides add a same-named const → duplicate-definition (E0428) on a "keep both" merge | cargo |
| borrowing | `borrow_mut_vs_immut` | `&mut` vs `&` in the same scope → borrowck error (E0502) | cargo |
| move semantics | `move_then_use` | one branch moves a value, the other uses it after → E0382 | cargo |
| lifetimes | `lifetime_mismatch` | a body borrowing from the wrong input lifetime → E0623 | cargo |
| struct field (compile floor) | `add_field_no_init` | struct gains a field but `new()` drops the init → E0063 | cargo |
| trait coherence / blanket impl | `orphan_impl` | `From` + `Into` blanket-impl conflict → E0119 (genuine coherence) | cargo |
| associated types | `associated_type_change` | assoc-type concrete type vs body return → E0308 | cargo |
| derive macros | `conflicting_derives` | `#[derive(Clone)]` on a non-`Clone` field → E0277 | cargo |
| FFI / `extern "C"` | `extern_ffi_signature` | foreign-fn signature/call type mismatch → E0308 (no linking) | cargo |
| Pin / Future impl | `future_impl_poll` | manual `Future`, wrong-type `Poll::Ready` vs `Output` → E0308 | cargo |
| mod / submodule move | `mod_submodule_move` | re-export vs local fn → E0255 on a keep-both merge | cargo |
| use imports | `use_import_conflict` | `use std::fmt;` vs `use std::fmt::Display;` tension | cargo |
| async / await | `await_insertion` | `.await` dropped from a future in an async block → E0271 | cargo |
| unsafe | `unsafe_block_edit` | one branch adds `unsafe`, the other changes an invariant; malformed merge | cargo |
| macros | `macro_body_vs_invoke` | an invocation matching no macro rule → no expansion | cargo |

### C. Build & test correctness

| Axis | Catalog ID / Test | What it exercises | Verifier |
|---|---|---|---|
| manifest / dependency | `cargo_dep_version` | dependency-version mismatch in `Cargo.toml` (manifest verification) | cargo (toml branch) |
| feature flags | `cargo_feature_flag` | `default` referencing an undefined feature → manifest parse error | cargo (toml branch) |
| edition | `edition_2024_default` | a 2024-edition crate resolving a `crate::` leaf | cargo |
| test gate — orchestrator | `test_rust_test_gate_*` | a merge that compiles but fails the project's own `#[cfg(test)]` | cargo test |
| test gate — file level | `compile_but_test_fails` | same, via `verify_file`'s shadow oracle (`enable_shadow_tests`) | cargo test |

### Loose files (standalone rustc)

| Axis | Catalog ID | What it exercises | Verifier |
|---|---|---|---|
| loose `.rs`, no Cargo.toml | `loose_file_script` | the standalone-`rustc` path (no crate context) | rustc |

## Verifier-path coverage (beyond the catalog)

The catalog drives the file-level `verify_file` compile floor. These additional
test files cover the surrounding machinery:

| Concern | Test file |
|---|---|
| standalone rustc (`_compile_rust`, edition inference) | `test_verification_rust.py` |
| cargo-default + false-positive regression + loose-file fallback | `test_verification_rust_cargo.py` |
| Cargo.toml manifest verification (Part C) | `test_verification_rust_cargo.py` |
| shadow-test discovery + `cargo test` runner | `test_rust_shadow.py` |
| clippy opt-in validator | `test_clippy.py` |
| full-orchestrator single-file rebase | `test_rust_end_to_end.py` |
| full-orchestrator multi-file (cross-file/crate) rebase | `test_rust_cross_file.py` |
| cargo/lsp adapters | `test_lsp.py` |

## Verifier-robustness property tests (Method C)

The hand-authored catalog is curated semantic tension; it cannot state invariants
about the verifier's own consistency. The catalog-mutation generator fills this:

| Invariant | Test | What it catches |
|---|---|---|
| no-crash | `test_mutation_does_not_crash_verifier` | `verify_file` raises on a valid-but-odd mutated splice |
| verdict-invariance | `test_mutation_preserves_verdict` | a cosmetic mutation (literal bump, local rename) flips the accept/reject verdict — the verifier is sensitive to something it shouldn't be |
| generator integrity | `test_each_mutation_yields_a_genuine_conflict` | a mutator produces a clean auto-merge (no real conflict) or a multi-block splice |

The mutators are deterministic (enumerated from the catalog structure, no random
seed → reviewable, not flaky). The AST-preservation-stability invariant (a
mutation outside the conflict span leaving `ast_preserved` unchanged) is deferred
to a later round: it runs on the Phase A per-unit path, which `verify_file` does
not exercise.

## Known gaps (deliberately deferred)

These taxonomy cells are **not** covered and are tracked as future work:

- **Verfier-robustness property tests (Method C, catalog-mutation form)**:
  `tests/rust_mutation_generator.py` applies deterministic structure-preserving
  mutations (literal bump, local-identifier rename) to the curated catalog bases
  and asserts no-crash + verdict-invariance via `tests/test_rust_mutation_generator.py`.
  This is the honest, high-value form of "fuzzing": it states invariants the
  hand-authored rows cannot (a cosmetic mutation must preserve the verdict).
  **Remaining Method-C frontier**: full random-AST generation with a
  baseline-comparison oracle (the brief's "filter to cases where the agent fails
  but a baseline succeeds") is still deferred — it needs a second resolver to
  compare against, which the test suite lacks (tests use a fake canned client).
- **Real-world harvesting** (Method D): scraping merged conflicts from public
  Rust repos (serde, tokio, clap). No real-world cases in the corpus.
- **Cross-crate / workspace support**: the manifest check targets a single
  repo-root `Cargo.toml`; nested workspace-member manifests aren't handled.
- **Proc-macro verification** (beyond `#[derive]`): attribute/proc-macro
  expansion conflicts aren't exercised (derive-trait clashes are covered via
  `conflicting_derives`, but custom proc-macro bodies aren't).
- **MIRI / property-based equivalence**: no UB detection or `proptest`
  semantic-equivalence checks (compile + test are the only oracles).
- **`rustfmt --check` formatter gate**: not implemented; formatting is not a
  verification dimension today.

## Maintenance notes

- Adding a catalog case: append to `RUST_CONFLICTS` in
  `rust_conflict_catalog.py`. Both `expected_resolved` and `broken_resolved`
  must replace **only the spanned lines** (the region between `<<<<<<<` and
  `>>>>>>>`); shared lines outside the span stay. The well-formedness test set
  catches span leaks structurally; the accept/reject sets catch compile-semantics
  drift. Verify a new case with the structural check (no toolchain needed) first.
- The baseline comparison (cargo syntax check) now uses `_blank_markers_one_side`
  so an add-add conflict doesn't mask its own duplicate-definition error in the
  baseline — see the commit that introduced `add_add_const`.
- The `shadow_test` flag marks a case whose `broken_resolved` **compiles** but
  fails a `#[cfg(test)]` assertion living in the file as shared context. Such
  cases are excluded from the compile-reject set (their break isn't a compile
  error) and run under the dedicated shadow-gate set with
  `enable_shadow_tests=True`. `_run_shadow_tests` writes the resolved `whole`
  to disk for the cargo-test run (then restores), because `verify_file` runs
  before the orchestrator writes — without this the oracle would test the
  baseline, not the merge.
- A derive-macro case only produces a real conflict when the two derive lines
  **differ textually** (e.g. `#[derive(Debug)]` vs `#[derive(Clone)]`); identical
  derives normalize away under `git merge-file`, yielding no conflict.

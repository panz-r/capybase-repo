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
| trait coherence | `orphan_impl` | conflicting/malformed trait impls | cargo |
| use imports | `use_import_conflict` | `use std::fmt;` vs `use std::fmt::Display;` tension | cargo |
| async / await | `await_insertion` | `.await` dropped from a future in an async block → E0271 | cargo |
| unsafe | `unsafe_block_edit` | one branch adds `unsafe`, the other changes an invariant; malformed merge | cargo |
| macros | `macro_body_vs_invoke` | an invocation matching no macro rule → no expansion | cargo |

### C. Build & test correctness

| Axis | Catalog ID / Test | What it exercises | Verifier |
|---|---|---|---|
| manifest / dependency | `cargo_dep_version` | dependency-version mismatch in `Cargo.toml` (manifest verification) | cargo (toml branch) |
| edition | `edition_2024_default` | a 2024-edition crate resolving a `crate::` leaf | cargo |
| test gate (intent preservation) | `test_rust_test_gate_*` | a merge that compiles but fails the project's own `#[cfg(test)]` | cargo test |

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

## Known gaps (deliberately deferred)

These taxonomy cells are **not** covered and are tracked as future work:

- **Fuzzing harness** (Method C): random valid-AST edits via `syn`/`quote` +
  `git merge` to auto-discover edge cases. No generator exists; the catalog is
  hand-authored.
- **Real-world harvesting** (Method D): scraping merged conflicts from public
  Rust repos (serde, tokio, clap). No real-world cases in the corpus.
- **Cross-crate / workspace support**: the manifest check targets a single
  repo-root `Cargo.toml`; nested workspace-member manifests aren't handled.
- **Proc-macro / derive-macro verification**: `#[derive(...)]` overlaps and
  proc-macro expansion conflicts aren't exercised (cargo expands them, but no
  case targets derive-trait clashes specifically).
- **FFI / `extern` blocks**: `extern "C"` signature conflicts aren't covered.
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

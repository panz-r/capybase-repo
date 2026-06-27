"""Synthetic Rust conflict catalog for the Rust verification test suite.

A parametrized corpus of rebase conflicts covering the dimensions of the Rust
conflict taxonomy (textual/structural, Rust-specific semantic, build/test). Each
case carries the three git sides (``base`` / ``current`` / ``replayed``) plus a
known-correct and a known-broken merge, so both the *accept* and *reject* paths
of the verifier are exercised from one data row.

This mirrors the catalog pattern in ``src/capybase/calibration_corpus.py`` but
is kept test-local (no runtime cost, no shipped code). It is the living coverage
table for the taxonomy — see ``tests/rust_corpus_coverage.md`` for the matrix.

The ``original`` (the marker-marked merge the agent actually sees) is NOT
hand-faked: :func:`build_markers` runs ``git merge-file`` over the three sides to
produce authentic ``<<<<<<< / ======= / >>>>>>>`` blocks (Method A in the plan —
"the exact conflict marker output that the agent will see"). This requires only
``git`` (already a hard dependency); it does NOT need a Rust toolchain.

Conventions
-----------
- Each ``RustConflict`` is a single-hunk file conflict (the corpus favors breadth
  of conflict *shape* over multi-hunk files).
- ``expected_resolved`` / ``broken_resolved`` are the *block-interior* resolved
  texts — exactly what replaces the marker span. ``expected_resolved`` is a
  known-good merge (compiles, and for crate cases is type-correct);
  ``broken_resolved`` is a merge that fails the verifier's compile floor (a
  syntax/type/borrowck error, or a missing struct field).
- ``needs_cargo`` marks cases that only check correctly under ``cargo check``
  (crate-context paths); ``False`` means standalone ``rustc`` suffices (loose
  files). The loose-file case is the only ``needs_cargo=False`` row.
- ``scaffold`` is extra file content the test writes alongside the conflicted
  file so the crate compiles (e.g. a ``lib.rs`` declaring the module, a sibling
  struct). It is written under ``repo_root`` keyed by relative path.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RustConflict:
    """One synthetic Rust rebase conflict.

    ``scaffold`` maps repo-root-relative paths to file content the verifier
    needs (so a crate compiles); the conflicted file itself is written by the
    test from ``original``.
    """

    id: str
    path: str
    language: str
    base: str
    current: str
    replayed: str
    expected_resolved: str
    broken_resolved: str
    taxonomy: tuple[str, ...]
    needs_cargo: bool = True
    scaffold: dict[str, str] = field(default_factory=dict)
    edition: str = "2021"
    notes: str = ""
    shadow_test: bool = False


def build_markers(base: str, current: str, replayed: str) -> str:
    """Produce authentic git conflict markers for the three sides.

    Runs ``git merge-file -p`` (no ``--diff3``) over the three blobs in a temp
    git repo, returning the merged file with real ``<<<<<<< / ======= /
    >>>>>>>`` blocks — the exact text capybase's parser and the agent see. Raises
    ``RuntimeError`` if git reports no conflict (the three sides must diverge so
    a genuine both-modified conflict emerges). Uses the git binary directly; no
    Rust toolchain is needed.

    ``git merge-file`` overwrites its first (``current``) arg in place, so we
    operate on a copy in a tempdir.
    """
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        # A tiny git repo so merge-file honors git config (markers, eol).
        subprocess.run(
            ["git", "init", "-q"], cwd=d, check=True, capture_output=True,
        )
        (d / "base").write_text(base)
        (d / "replayed").write_text(replayed)
        cur = d / "current"
        cur.write_text(current)
        proc = subprocess.run(
            ["git", "merge-file", "-p", "current", "base", "replayed"],
            cwd=d, capture_output=True, text=True,
        )
        # git merge-file exits 1 on conflict — that's the success case here.
        # Exit 0 means a clean merge (no conflict), which is a bug in the
        # fixture: the sides must diverge to produce a both-modified conflict.
        if proc.returncode == 0:
            raise RuntimeError(
                "build_markers: the three sides merged cleanly (no conflict). "
                "Choose base/current/replayed so they diverge."
            )
        return proc.stdout


# ---------------------------------------------------------------------------
# Scaffold helpers
# ---------------------------------------------------------------------------


def _manifest(name: str, edition: str = "2021") -> str:
    """A minimal Cargo.toml for a crate named ``name``."""
    return (
        f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "{edition}"\n'
    )


# crate lib.rs declarations declaring the module under test, so cargo resolves it.
_LIB_CONFIG = "pub mod config;\n"
_LIB_SERVER = "pub mod server;\n"


def _conflict(
    *,
    id: str,
    path: str,
    base: str,
    current: str,
    replayed: str,
    expected_resolved: str,
    broken_resolved: str,
    taxonomy: tuple[str, ...],
    language: str = "rust",
    needs_cargo: bool = True,
    scaffold: dict[str, str] | None = None,
    edition: str = "2021",
    notes: str = "",
    shadow_test: bool = False,
) -> RustConflict:
    return RustConflict(
        id=id, path=path, language=language, base=base, current=current,
        replayed=replayed, expected_resolved=expected_resolved,
        broken_resolved=broken_resolved, taxonomy=taxonomy,
        needs_cargo=needs_cargo, scaffold=scaffold or {}, edition=edition,
        notes=notes, shadow_test=shadow_test,
    )


# ---------------------------------------------------------------------------
# The catalog. Each row picks a high-value cell from the taxonomy matrix.
# ---------------------------------------------------------------------------

RUST_CONFLICTS: list[RustConflict] = [
    # --- A. Textual / structural ---

    _conflict(
        id="same_line_value",
        path="src/config.rs",
        base=(
            "pub struct Config {\n    pub port: u16,\n}\n"
            "impl Config {\n    pub fn port_of() -> u16 { 8080 }\n}\n"
        ),
        current=(
            "pub struct Config {\n    pub port: u16,\n}\n"
            "impl Config {\n    pub fn port_of() -> u16 { 9090 }\n}\n"
        ),
        replayed=(
            "pub struct Config {\n    pub port: u16,\n}\n"
            "impl Config {\n    pub fn port_of() -> u16 { 7070 }\n}\n"
        ),
        expected_resolved="    pub fn port_of() -> u16 { 9090 }",
        broken_resolved="    pub fn port_of() -> u16 { 9090; }",  # stray `;`
        taxonomy=("textual", "same-line"),
        scaffold={"Cargo.toml": _manifest("same_line_value"),
                  "src/lib.rs": _LIB_CONFIG},
        notes="Classic same-line value conflict (the rust_conflicted_repo shape).",
    ),
    _conflict(
        id="adjacent_insert_use",
        path="src/config.rs",
        # Both sides insert a different `use` line at the same position.
        base=(
            "pub struct Config {\n    pub port: u16,\n}\n"
            "impl Config {\n    pub fn p(&self) -> u16 { self.port }\n}\n"
        ),
        current=(
            "use std::sync::Arc;\n\n"
            "pub struct Config {\n    pub port: u16,\n}\n"
            "impl Config {\n    pub fn p(&self) -> u16 { self.port }\n}\n"
        ),
        replayed=(
            "use std::rc::Rc;\n\n"
            "pub struct Config {\n    pub port: u16,\n}\n"
            "impl Config {\n    pub fn p(&self) -> u16 { self.port }\n}\n"
        ),
        # Correct merge: keep both imports (one hunk covers both insertions).
        expected_resolved="use std::sync::Arc;\nuse std::rc::Rc;",
        # Broken: an unresolved import (the dropped side's path is bogus).
        broken_resolved="use std::sync::Arc;\nuse nonexistent_crate::Rc;",
        taxonomy=("textual", "adjacent-insert", "use-statements"),
        scaffold={"Cargo.toml": _manifest("adjacent_insert_use"),
                  "src/lib.rs": _LIB_CONFIG},
        notes="Both sides add different `use` lines at the same position.",
    ),
    _conflict(
        id="whitespace_reformat",
        path="src/config.rs",
        # Current reformats to one line; replayed changes the value. The conflict
        # region is the function body, where the format change and value change
        # overlap.
        base=(
            "pub struct C {\n    pub v: u32,\n}\n"
            "impl C {\n    pub fn double(&self) -> u32 {\n        self.v * 2\n    }\n}\n"
        ),
        current=(
            "pub struct C {\n    pub v: u32,\n}\n"
            "impl C { pub fn double(&self) -> u32 { self.v * 2 } }\n"
        ),
        replayed=(
            "pub struct C {\n    pub v: u32,\n}\n"
            "impl C {\n    pub fn double(&self) -> u32 {\n        self.v * 3\n    }\n}\n"
        ),
        # Correct: take the value change in the one-line form.
        expected_resolved="impl C { pub fn double(&self) -> u32 { self.v * 3 } }",
        # Broken: malformed (missing closing brace).
        broken_resolved="impl C { pub fn double(&self) -> u32 { self.v * 3 ",
        taxonomy=("textual", "whitespace", "reformat"),
        scaffold={"Cargo.toml": _manifest("whitespace_reformat"),
                  "src/lib.rs": _LIB_CONFIG},
        notes="One branch reformats (rustfmt'd), the other changes logic.",
    ),

    # --- B. Rust-specific semantic ---

    _conflict(
        id="signature_return_vs_param",
        path="src/config.rs",
        # Current changes the return type; replayed adds a parameter. The
        # conflict is the function signature line.
        base=(
            "pub fn make_port() -> u16 {\n    8080\n}\n"
        ),
        current=(
            "pub fn make_port() -> u32 {\n    8080\n}\n"
        ),
        replayed=(
            "pub fn make_port(base: u16) -> u16 {\n    base\n}\n"
        ),
        # Correct merge: both changes — new param AND new return type, with a
        # body that fits u32. The span is the 2 differing lines only.
        expected_resolved="pub fn make_port(base: u16) -> u32 {\n    8080",
        # Broken: returns `base` (u16) as u32 without conversion → type error.
        broken_resolved="pub fn make_port(base: u16) -> u32 {\n    base",
        taxonomy=("semantic", "signature", "type"),
        scaffold={"Cargo.toml": _manifest("signature_return_vs_param"),
                  "src/lib.rs": _LIB_CONFIG},
        notes="A changes return type, B adds a parameter; combine both.",
    ),
    _conflict(
        id="move_struct_to_module",
        path="src/server.rs",
        # The leaf file uses crate:: — the exact pattern standalone rustc cannot
        # check (false-positives with E0432). Only cargo (crate-aware) accepts.
        base=(
            "use crate::config::Config;\n"
            "pub fn label(c: &Config) -> u16 { c.port }\n"
        ),
        current=(
            "use crate::config::Config;\n"
            "pub fn label(c: &Config) -> u16 { c.port + 1 }\n"
        ),
        replayed=(
            "use crate::config::Config;\n"
            "pub fn label(c: &Config) -> u16 { c.port + 2 }\n"
        ),
        expected_resolved="pub fn label(c: &Config) -> u16 { c.port + 1 }",
        # Broken: references a field that doesn't exist.
        broken_resolved="pub fn label(c: &Config) -> u16 { c.no_such_field }",
        taxonomy=("structural", "move", "crate-path"),
        scaffold={
            "Cargo.toml": _manifest("move_struct_to_module"),
            "src/lib.rs": "pub mod config;\npub mod server;\n",
            "src/config.rs": "pub struct Config { pub port: u16 }\n",
        },
        notes="crate:: leaf file (the standalone-rustc false-positive regression class).",
    ),
    _conflict(
        id="add_add_const",
        path="src/config.rs",
        # Both sides ADD a const with the SAME name at the same location. Git
        # normalizes the identical `const DEFAULT` line and conflicts only on
        # the differing value. A naive "keep both values" merge duplicates the
        # const → E0428 duplicate definition. The correct merge keeps one value.
        base=(
            "pub struct Config { pub port: u16 }\n"
            "impl Config {\n    pub fn p(&self) -> u16 { self.port }\n}\n"
        ),
        current=(
            "pub struct Config { pub port: u16 }\n"
            "impl Config {\n    pub const DEFAULT: u16 = 9090;\n"
            "    pub fn p(&self) -> u16 { self.port }\n}\n"
        ),
        replayed=(
            "pub struct Config { pub port: u16 }\n"
            "impl Config {\n    pub const DEFAULT: u16 = 7070;\n"
            "    pub fn p(&self) -> u16 { self.port }\n}\n"
        ),
        # Correct: keep ONE const (no duplicate-definition error).
        expected_resolved="    pub const DEFAULT: u16 = 9090;",
        # Broken: a naive "keep both" merge → duplicate const (E0428).
        broken_resolved=(
            "    pub const DEFAULT: u16 = 9090;\n"
            "    pub const DEFAULT: u16 = 7070;"
        ),
        taxonomy=("semantic", "add-add", "duplicate-definition"),
        scaffold={"Cargo.toml": _manifest("add_add_const"),
                  "src/lib.rs": _LIB_CONFIG},
        notes="Both sides add a same-named const → duplicate-definition on merge.",
    ),
    _conflict(
        id="borrow_mut_vs_immut",
        path="src/config.rs",
        # Both sides edit the SAME statement line differently. Current mutates
        # in place; replayed snapshots via an immutable borrow. A naive "keep
        # both" combine holds an immutable borrow across a mutation → borrowck
        # error. The correct merge keeps the mutation alone.
        base=(
            "pub fn bump(v: &mut [u8]) {\n    v[0] += 1;\n}\n"
        ),
        current=(
            "pub fn bump(v: &mut [u8]) {\n    v[0] += 1; v[1] += 1;\n}\n"
        ),
        replayed=(
            "pub fn bump(v: &mut [u8]) {\n    let _first = &v[0];\n}\n"
        ),
        # Correct: keep the mutation (no conflicting borrow).
        expected_resolved="    v[0] += 1; v[1] += 1;",
        # Broken: hold the immutable borrow across the mutation → E0502.
        broken_resolved="    let _first = &v[0];\n    v[0] += 1; v[1] += 1;\n    let _ = _first;",
        taxonomy=("semantic", "borrowing", "borrowck"),
        scaffold={"Cargo.toml": _manifest("borrow_mut_vs_immut"),
                  "src/lib.rs": _LIB_CONFIG},
        notes="&mut vs & in the same scope → borrowck error on combine.",
    ),
    _conflict(
        id="move_then_use",
        path="src/config.rs",
        # Current moves a value; replayed uses it after → E0382 (use after move).
        base=(
            "pub fn go(s: String) -> String {\n    s\n}\n"
        ),
        current=(
            "pub fn go(s: String) -> String {\n    s + \"!\"\n}\n"
        ),
        replayed=(
            "pub fn go(s: String) -> String {\n    let _len = s.len();\n    s\n}\n"
        ),
        # Correct: keep current's move alone. The span is the 2 body lines.
        expected_resolved='    s + "!"',
        # Broken: replayed's use of `s` AFTER it was moved by `s + "!"` → E0382.
        broken_resolved=(
            '    let moved = s + "!";\n'
            "    let _len = s.len();\n"
            "    moved"
        ),
        taxonomy=("semantic", "move-semantics", "borrowck"),
        scaffold={"Cargo.toml": _manifest("move_then_use"),
                  "src/lib.rs": _LIB_CONFIG},
        notes="One branch moves a value, the other uses it after → E0382.",
    ),
    _conflict(
        id="lifetime_mismatch",
        path="src/config.rs",
        # Two branches annotate different lifetimes; a naive combine is invalid.
        base=(
            "pub fn first<'a>(s: &'a [u8]) -> &'a u8 {\n    &s[0]\n}\n"
        ),
        current=(
            "pub fn first<'a, 'b>(s: &'a [u8], _t: &'b [u8]) -> &'a u8 {\n    &s[0]\n}\n"
        ),
        replayed=(
            "pub fn first<'a>(s: &'a [u8]) -> &'a u8 {\n    &s[s.len() - 1]\n}\n"
        ),
        # Correct: keep the two-param signature; the body returns &'a u8. The
        # span is the 2 differing lines (signature + body), no trailing brace.
        expected_resolved=(
            "pub fn first<'a, 'b>(s: &'a [u8], _t: &'b [u8]) -> &'a u8 {\n"
            "    &s[0]"
        ),
        # Broken: the body borrows from `_t` (lifetime 'b) but the return type
        # is &'a u8 → E0623 lifetime mismatch (return tied to wrong input).
        broken_resolved=(
            "pub fn first<'a, 'b>(s: &'a [u8], _t: &'b [u8]) -> &'a u8 {\n"
            "    &_t[0]"
        ),
        taxonomy=("semantic", "lifetimes"),
        scaffold={"Cargo.toml": _manifest("lifetime_mismatch"),
                  "src/lib.rs": _LIB_CONFIG},
        notes="Different lifetime annotations combine to an invalid signature.",
    ),
    _conflict(
        id="add_field_no_init",
        path="src/config.rs",
        # The struct gains a field (auto-merged by git) but a botched merge of
        # new() drops the initializer → E0063 missing field. Both conflict sides
        # are individually valid (each initializes tls), so a correct merge
        # keeps one; a botched merge drops tls.
        base=(
            "pub struct Config {\n    pub port: u16,\n}\n"
            "impl Config {\n    pub fn new() -> Self {\n"
            "        Config { port: 8080 }\n    }\n}\n"
        ),
        current=(
            "pub struct Config {\n    pub port: u16,\n    pub tls: bool,\n}\n"
            "impl Config {\n    pub fn new() -> Self {\n"
            "        Config { port: 9090, tls: true }\n    }\n}\n"
        ),
        replayed=(
            "pub struct Config {\n    pub port: u16,\n    pub tls: bool,\n}\n"
            "impl Config {\n    pub fn new() -> Self {\n"
            "        Config { port: 7070, tls: false }\n    }\n}\n"
        ),
        # Correct merge: keep the tls initializer. The span is the single
        # initializer line.
        expected_resolved="        Config { port: 9090, tls: false }",
        # Broken: struct has `tls` (auto-merged) but new() drops it → E0063.
        broken_resolved="        Config { port: 9090 }",
        taxonomy=("semantic", "compile-floor", "struct-field"),
        scaffold={"Cargo.toml": _manifest("add_field_no_init"),
                  "src/lib.rs": _LIB_CONFIG},
        notes="Struct gains a field but new() drops the init → E0063.",
    ),
    _conflict(
        id="orphan_impl",
        path="src/config.rs",
        # Trait/coherence. Side A adds ``impl From<u32> for Wrap``; side B adds
        # ``impl Into<Wrap> for u32``. Each is individually valid, but ``Into``
        # has a blanket impl from ``From`` (``impl<T, U> Into<U> for T where T:
        # From<U>``), so keeping BOTH impls → E0119 conflicting implementations.
        # The two impl lines differ textually, so git produces a real conflict.
        # (A genuine coherence error, not a syntax break.)
        base=(
            "pub struct Wrap(pub u32);\n"
        ),
        current=(
            "pub struct Wrap(pub u32);\n"
            "impl From<u32> for Wrap {\n    fn from(v: u32) -> Self { Wrap(v) }\n}\n"
        ),
        replayed=(
            "pub struct Wrap(pub u32);\n"
            "impl std::convert::Into<Wrap> for u32 {\n"
            "    fn into(self) -> Wrap { Wrap(self) }\n}\n"
        ),
        # Correct: keep ONE impl (no coherence conflict). The From impl is the
        # canonical form; the span is the 2 differing impl lines.
        expected_resolved=(
            "impl From<u32> for Wrap {\n    fn from(v: u32) -> Self { Wrap(v) }"
        ),
        # Broken: keep BOTH impls → E0119 (Into blanket-impl conflict).
        broken_resolved=(
            "impl From<u32> for Wrap {\n    fn from(v: u32) -> Self { Wrap(v) }\n}\n"
            "impl std::convert::Into<Wrap> for u32 {\n"
            "    fn into(self) -> Wrap { Wrap(self) }\n}"
        ),
        taxonomy=("semantic", "trait", "coherence", "blanket-impl"),
        scaffold={"Cargo.toml": _manifest("orphan_impl"),
                  "src/lib.rs": _LIB_CONFIG},
        notes="From + Into blanket-impl conflict → E0119 (genuine coherence).",
    ),
    _conflict(
        id="use_import_conflict",
        path="src/config.rs",
        # Current adds `use std::fmt;` (module); replayed adds
        # `use std::fmt::Display;` (item) — a glob/name-shadowing tension when
        # both are present and one references the ambiguous name.
        base=(
            "pub struct Config { pub port: u16 }\n"
        ),
        current=(
            "use std::fmt;\n"
            "pub struct Config { pub port: u16 }\n"
            "impl fmt::Debug for Config {\n"
            "    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {\n"
            '        write!(f, "port={}", self.port)\n'
            "    }\n}\n"
        ),
        replayed=(
            "use std::fmt::Display;\n"
            "pub struct Config { pub port: u16 }\n"
        ),
        # Correct: keep the Debug impl (it brings in the fmt module import).
        expected_resolved="use std::fmt;",
        # Broken: reference Display which neither side actually implements.
        broken_resolved="use std::fmt::Display;",
        taxonomy=("semantic", "use-statements", "import"),
        scaffold={"Cargo.toml": _manifest("use_import_conflict"),
                  "src/lib.rs": _LIB_CONFIG},
        notes="use std::fmt; vs use std::fmt::Display; import tension.",
    ),
    _conflict(
        id="await_insertion",
        path="src/config.rs",
        # Both sides edit the awaited expression's value line. The base awaits a
        # future; current and replayed keep the await and change the value. A
        # merge that drops the .await returns a Future where u32 is expected.
        base=(
            "async fn fetch() -> u32 {\n    fut(8080).await\n}\n"
            "fn fut(v: u32) -> impl std::future::Future<Output = u32> {\n"
            "    async move { v }\n}\n"
        ),
        current=(
            "async fn fetch() -> u32 {\n    fut(9090).await\n}\n"
            "fn fut(v: u32) -> impl std::future::Future<Output = u32> {\n"
            "    async move { v }\n}\n"
        ),
        replayed=(
            "async fn fetch() -> u32 {\n    fut(7070).await\n}\n"
            "fn fut(v: u32) -> impl std::future::Future<Output = u32> {\n"
            "    async move { v }\n}\n"
        ),
        # Correct: keep the .await on current's value. The span is 1 line.
        expected_resolved="    fut(9090).await",
        # Broken: drop the .await → fetch returns a Future, not u32 → E0271.
        broken_resolved="    fut(9090)",
        taxonomy=("semantic", "async", "await"),
        scaffold={"Cargo.toml": _manifest("await_insertion"),
                  "src/lib.rs": _LIB_CONFIG},
        notes=".await dropped from an awaited future in an async block.",
    ),
    _conflict(
        id="unsafe_block_edit",
        path="src/config.rs",
        # Current wraps in unsafe, replayed changes the invariant inside. The
        # correct merge keeps the unsafe wrapper around the changed op.
        base=(
            "pub fn read(buf: &[u8]) -> u8 {\n    buf[0]\n}\n"
        ),
        current=(
            "pub fn read(buf: &[u8]) -> u8 {\n    unsafe { *buf.as_ptr() }\n}\n"
        ),
        replayed=(
            "pub fn read(buf: &[u8]) -> u8 {\n    buf[buf.len().saturating_sub(1)]\n}\n"
        ),
        # Correct: keep the unsafe wrapper on the changed index op. The span
        # is the body lines (the signature line stays outside the span).
        expected_resolved=(
            "    unsafe { *buf.as_ptr().add(buf.len().saturating_sub(1)) }"
        ),
        # Broken: malformed (unbalanced braces).
        broken_resolved="    unsafe { *buf.as_ptr()",
        taxonomy=("semantic", "unsafe"),
        scaffold={"Cargo.toml": _manifest("unsafe_block_edit"),
                  "src/lib.rs": _LIB_CONFIG},
        notes="One branch adds an unsafe block, the other changes an invariant.",
    ),
    _conflict(
        id="macro_body_vs_invoke",
        path="src/config.rs",
        # A declarative macro with two patterns (one-arg and two-arg). Both sides
        # edit the invocation value on the one-arg form (individually valid). A
        # botched merge produces an invocation matching NO pattern (three args)
        # → E0586 / no matching rule. This is the macro-rule-vs-invocation
        # tension: the merge must keep a valid invocation shape.
        base=(
            "macro_rules! pick {\n    ($a:expr) => { $a };\n"
            "    ($a:expr, $b:expr) => { $a + $b };\n}\n"
            "pub fn chosen() -> u32 {\n    pick!(8080)\n}\n"
        ),
        current=(
            "macro_rules! pick {\n    ($a:expr) => { $a };\n"
            "    ($a:expr, $b:expr) => { $a + $b };\n}\n"
            "pub fn chosen() -> u32 {\n    pick!(9090)\n}\n"
        ),
        replayed=(
            "macro_rules! pick {\n    ($a:expr) => { $a };\n"
            "    ($a:expr, $b:expr) => { $a + $b };\n}\n"
            "pub fn chosen() -> u32 {\n    pick!(7070)\n}\n"
        ),
        # Correct: one-arg invocation with current's value. The span is 1 line.
        expected_resolved="    pick!(9090)",
        # Broken: three-arg invocation → no matching macro rule.
        broken_resolved="    pick!(9090, 1, 2)",
        taxonomy=("semantic", "macros"),
        scaffold={"Cargo.toml": _manifest("macro_body_vs_invoke"),
                  "src/lib.rs": _LIB_CONFIG},
        notes="Macro pattern vs invocation: a merge that matches no rule.",
    ),
    _conflict(
        id="conflicting_derives",
        path="src/config.rs",
        # Derive-macro overlap. The field type NotClone is Debug (manual impl)
        # but NOT Clone. Side A derives Debug (satisfied); side B derives Clone
        # (E0277). The two derive-attribute lines differ textually, so git
        # produces a real conflict hunk (identical derives would normalize away).
        base=(
            "struct NotClone(u32);\n"
            "impl std::fmt::Debug for NotClone {\n"
            "    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {\n"
            '        write!(f, "nc")\n'
            "    }\n}\n"
            "pub struct Config { pub port: u16, pub nc: NotClone }\n"
        ),
        current=(
            "struct NotClone(u32);\n"
            "impl std::fmt::Debug for NotClone {\n"
            "    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {\n"
            '        write!(f, "nc")\n'
            "    }\n}\n"
            "#[derive(Debug)]\n"
            "pub struct Config { pub port: u16, pub nc: NotClone }\n"
        ),
        replayed=(
            "struct NotClone(u32);\n"
            "impl std::fmt::Debug for NotClone {\n"
            "    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {\n"
            '        write!(f, "nc")\n'
            "    }\n}\n"
            "#[derive(Clone)]\n"
            "pub struct Config { pub port: u16, pub nc: NotClone }\n"
        ),
        # Correct: keep the Debug derive (satisfied — NotClone: Debug holds).
        expected_resolved="#[derive(Debug)]",
        # Broken: derive Clone → E0277 (NotClone: Clone is not satisfied).
        broken_resolved="#[derive(Clone)]",
        taxonomy=("semantic", "derive-macro", "trait-bound"),
        scaffold={"Cargo.toml": _manifest("conflicting_derives"),
                  "src/lib.rs": _LIB_CONFIG},
        notes="Conflicting #[derive(...)] on a non-Clone field → E0277.",
    ),
    _conflict(
        id="extern_ffi_signature",
        path="src/config.rs",
        # FFI: an extern "C" foreign-fn signature. Both sides edit the call site
        # value; one side also changes the foreign-fn signature's parameter
        # type. cargo check catches a signature/call mismatch as a type error
        # WITHOUT linking (the symbol need not exist). The span is the 2 lines
        # (signature + body call) that both differ.
        base=(
            'extern "C" { fn get_port(x: u32) -> u16; }\n'
            "pub fn label() -> u16 { unsafe { get_port(8080) } }\n"
        ),
        current=(
            'extern "C" { fn get_port(x: *const u8) -> u16; }\n'
            "pub fn label() -> u16 { unsafe { get_port(&8080u8) } }\n"
        ),
        replayed=(
            'extern "C" { fn get_port(x: u32) -> u16; }\n'
            "pub fn label() -> u16 { unsafe { get_port(9090) } }\n"
        ),
        # Correct: the new signature with the matching pointer call.
        expected_resolved=(
            'extern "C" { fn get_port(x: *const u8) -> u16; }\n'
            "pub fn label() -> u16 { unsafe { get_port(&9090u8) } }"
        ),
        # Broken: new signature (*const u8) but old integer call → E0308.
        broken_resolved=(
            'extern "C" { fn get_port(x: *const u8) -> u16; }\n'
            "pub fn label() -> u16 { unsafe { get_port(9090) } }"
        ),
        taxonomy=("semantic", "ffi", "extern"),
        scaffold={"Cargo.toml": _manifest("extern_ffi_signature"),
                  "src/lib.rs": _LIB_CONFIG},
        notes="extern \"C\" signature/call mismatch → E0308 (cargo, no linking).",
    ),
    _conflict(
        id="associated_type_change",
        path="src/config.rs",
        # Associated type: both sides edit the ``type Out`` line (the concrete
        # type for the trait's assoc type). ``provide()``'s body must return a
        # value of that concrete type. The correct merge keeps the u64 type with
        # a u64 body; a botched merge takes u64 type with a u32 body → E0308.
        # The span is the 2 diverging lines (type Out + body).
        base=(
            "pub trait Provider { type Out; fn provide() -> Self::Out; }\n"
            "pub struct P;\n"
            "impl Provider for P {\n"
            "    type Out = u32;\n"
            "    fn provide() -> Self::Out { 1u32 }\n"
            "}\n"
        ),
        current=(
            "pub trait Provider { type Out; fn provide() -> Self::Out; }\n"
            "pub struct P;\n"
            "impl Provider for P {\n"
            "    type Out = u64;\n"
            "    fn provide() -> Self::Out { 1u64 }\n"
            "}\n"
        ),
        replayed=(
            "pub trait Provider { type Out; fn provide() -> Self::Out; }\n"
            "pub struct P;\n"
            "impl Provider for P {\n"
            "    type Out = u32;\n"
            "    fn provide() -> Self::Out { 2u32 }\n"
            "}\n"
        ),
        # Correct: u64 assoc type with a u64 body.
        expected_resolved=(
            "    type Out = u64;\n"
            "    fn provide() -> Self::Out { 2u64 }"
        ),
        # Broken: u64 assoc type but a u32 body → E0308.
        broken_resolved=(
            "    type Out = u64;\n"
            "    fn provide() -> Self::Out { 2u32 }"
        ),
        taxonomy=("semantic", "trait", "associated-type"),
        scaffold={"Cargo.toml": _manifest("associated_type_change"),
                  "src/lib.rs": _LIB_CONFIG},
        notes="Associated-type concrete type vs body return → E0308.",
    ),
    _conflict(
        id="future_impl_poll",
        path="src/config.rs",
        # A manual Future impl. Both sides edit the Poll::Ready value; one side
        # changes the ready value's type. A merge with the wrong type for the
        # declared Output → E0308. The span is the single poll body line.
        base=(
            "use std::future::Future;\n"
            "use std::pin::Pin;\n"
            "use std::task::{Context, Poll};\n"
            "pub struct Ready(u32);\n"
            "impl Future for Ready {\n"
            "    type Output = u32;\n"
            "    fn poll(self: Pin<&mut Self>, _cx: &mut Context) -> Poll<Self::Output> {\n"
            "        Poll::Ready(1)\n"
            "    }\n}\n"
        ),
        current=(
            "use std::future::Future;\n"
            "use std::pin::Pin;\n"
            "use std::task::{Context, Poll};\n"
            "pub struct Ready(u32);\n"
            "impl Future for Ready {\n"
            "    type Output = u32;\n"
            "    fn poll(self: Pin<&mut Self>, _cx: &mut Context) -> Poll<Self::Output> {\n"
            "        Poll::Ready(2)\n"
            "    }\n}\n"
        ),
        replayed=(
            "use std::future::Future;\n"
            "use std::pin::Pin;\n"
            "use std::task::{Context, Poll};\n"
            "pub struct Ready(u32);\n"
            "impl Future for Ready {\n"
            "    type Output = u32;\n"
            "    fn poll(self: Pin<&mut Self>, _cx: &mut Context) -> Poll<Self::Output> {\n"
            "        Poll::Ready(1u16)\n"
            "    }\n}\n"
        ),
        # Correct: current's value with the declared u32 Output.
        expected_resolved="        Poll::Ready(2)",
        # Broken: u16 ready value for a u32 Output → E0308.
        broken_resolved="        Poll::Ready(1u16)",
        taxonomy=("semantic", "async", "future-impl"),
        scaffold={"Cargo.toml": _manifest("future_impl_poll"),
                  "src/lib.rs": _LIB_CONFIG},
        notes="Manual Future impl: wrong-type Poll::Ready vs Output → E0308.",
    ),
    _conflict(
        id="mod_submodule_move",
        path="src/config.rs",
        # Simulated submodule move (single-file). The submodule pre-exists in
        # scaffold. Base has a local fn; current re-exports it from the submodule
        # (the "move"); replayed edits the local body. A naive "keep both" merge
        # holds the re-export AND the local fn → E0255 (name defined twice). The
        # span is the single diverging line.
        base=(
            "pub fn helper() -> u32 { 8080 }\n"
        ),
        current=(
            "pub use crate::submod::helper;\n"
        ),
        replayed=(
            "pub fn helper() -> u32 { 9090 }\n"
        ),
        # Correct: keep the re-export (the function moved to the submodule).
        expected_resolved="pub use crate::submod::helper;",
        # Broken: keep BOTH the re-export and the local fn → E0255.
        broken_resolved=(
            "pub use crate::submod::helper;\n"
            "pub fn helper() -> u32 { 9090 }"
        ),
        taxonomy=("structural", "mod", "submodule-move"),
        scaffold={
            "Cargo.toml": _manifest("mod_submodule_move"),
            "src/lib.rs": "pub mod config;\npub mod submod;\n",
            "src/submod.rs": "pub fn helper() -> u32 { 100 }\n",
        },
        notes="Re-export vs local fn → E0255 on a naive keep-both merge.",
    ),

    # --- C. Build & test correctness (compile-but-test-fails, cell 7) ---
    #
    # A merge that COMPILES but fails the project's own #[cfg(test)] assertion.
    # The compile floor (cargo check) accepts both the expected and broken
    # merge; only the shadow-test oracle (cargo test) catches the broken one.
    # The #[cfg(test)] module is shared context (outside the conflict span).

    _conflict(
        id="compile_but_test_fails",
        path="src/config.rs",
        shadow_test=True,
        # A const PORT whose value conflicts. The #[cfg(test)] module (shared
        # context, outside the span) asserts PORT == 9090. Both the expected and
        # broken merge COMPILE cleanly; only cargo test distinguishes them.
        base=(
            "pub const PORT: u16 = 8080;\n"
            "\n"
            "#[cfg(test)]\n"
            "mod tests {\n"
            "    use super::*;\n"
            "    #[test]\n"
            "    fn port_is_9090() {\n"
            "        assert_eq!(PORT, 9090);\n"
            "    }\n"
            "}\n"
        ),
        current=(
            "pub const PORT: u16 = 9090;\n"
            "\n"
            "#[cfg(test)]\n"
            "mod tests {\n"
            "    use super::*;\n"
            "    #[test]\n"
            "    fn port_is_9090() {\n"
            "        assert_eq!(PORT, 9090);\n"
            "    }\n"
            "}\n"
        ),
        replayed=(
            "pub const PORT: u16 = 7070;\n"
            "\n"
            "#[cfg(test)]\n"
            "mod tests {\n"
            "    use super::*;\n"
            "    #[test]\n"
            "    fn port_is_9090() {\n"
            "        assert_eq!(PORT, 9090);\n"
            "    }\n"
            "}\n"
        ),
        # Correct: keep 9090 (the value the test guards) → compiles AND passes.
        expected_resolved="pub const PORT: u16 = 9090;",
        # Broken: keep 7070 → compiles cleanly, but cargo test panics on the
        # assertion. Only the shadow-test oracle catches this.
        broken_resolved="pub const PORT: u16 = 7070;",
        taxonomy=("semantic", "compile-but-test-fails", "intent-preservation"),
        scaffold={"Cargo.toml": _manifest("compile_but_test_fails"),
                  "src/lib.rs": _LIB_CONFIG},
        notes="Compiles but fails the #[cfg(test)] assertion (shadow-test oracle).",
    ),

    # --- Edition coverage ---

    _conflict(
        id="edition_2024_default",
        path="src/config.rs",
        base=(
            "use crate::config2::Meta;\n"
            "pub fn describe(m: &Meta) -> u32 { m.id }\n"
        ),
        current=(
            "use crate::config2::Meta;\n"
            "pub fn describe(m: &Meta) -> u32 { m.id + 1 }\n"
        ),
        replayed=(
            "use crate::config2::Meta;\n"
            "pub fn describe(m: &Meta) -> u32 { m.id + 2 }\n"
        ),
        expected_resolved="pub fn describe(m: &Meta) -> u32 { m.id + 1 }",
        broken_resolved="pub fn describe(m: &Meta) -> u32 { m.id + no_such }",
        taxonomy=("edition", "2024", "crate-path"),
        edition="2024",
        scaffold={
            "Cargo.toml": _manifest("edition_2024_default", edition="2024"),
            "src/lib.rs": "pub mod config2;\npub mod config;\n",
            "src/config2.rs": "pub struct Meta { pub id: u32 }\n",
        },
        notes="A 2024-edition crate resolving a crate:: leaf.",
    ),

    # --- Cargo.toml manifest (drives the toml verification branch) ---

    _conflict(
        id="cargo_dep_version",
        path="Cargo.toml",
        language="toml",
        base=(
            '[package]\nname = "depver"\nversion = "0.1.0"\nedition = "2021"\n\n'
            "[dependencies]\n"
            'sibling = { path = "../sibling", version = "1.0.0" }\n'
        ),
        current=(
            '[package]\nname = "depver"\nversion = "0.1.0"\nedition = "2021"\n\n'
            "[dependencies]\n"
            'sibling = { path = "../sibling", version = "1.5.0" }\n'
        ),
        replayed=(
            '[package]\nname = "depver"\nversion = "0.1.0"\nedition = "2021"\n\n'
            "[dependencies]\n"
            'sibling = { path = "../sibling", version = "2.0.0" }\n'
        ),
        # Correct: resolve to the version the sibling crate actually publishes.
        expected_resolved='sibling = { path = "../sibling", version = "2.0.0" }',
        # Broken: malformed TOML (missing closing quote) → cargo aborts.
        broken_resolved='sibling = { path = "../sibling", version = "2.0.0 }',
        taxonomy=("cargo-toml", "dependency", "version"),
        needs_cargo=True,
        scaffold={
            "src/lib.rs": "pub fn ping() -> u32 { 1 }\n",
        },
        notes="Dependency-version mismatch in Cargo.toml (manifest verification).",
    ),
    _conflict(
        id="cargo_feature_flag",
        path="Cargo.toml",
        language="toml",
        # A feature-flag conflict: both sides change the `default` feature set.
        # The correct merge references a defined feature; a botched merge
        # references an undefined feature → cargo aborts with a manifest parse
        # error on stderr (no JSON stream), caught by _check_cargo's fallback.
        base=(
            '[package]\nname = "feattest"\nversion = "0.1.0"\nedition = "2021"\n\n'
            "[features]\n"
            'default = []\n'
            'foo = []\n'
        ),
        current=(
            '[package]\nname = "feattest"\nversion = "0.1.0"\nedition = "2021"\n\n'
            "[features]\n"
            'default = ["foo"]\n'
            'foo = []\n'
        ),
        replayed=(
            '[package]\nname = "feattest"\nversion = "0.1.0"\nedition = "2021"\n\n'
            "[features]\n"
            'default = ["bar"]\n'
            'foo = []\n'
        ),
        # Correct: reference the defined feature.
        expected_resolved='default = ["foo"]',
        # Broken: reference an undefined feature → cargo manifest parse error.
        broken_resolved='default = ["bar"]',
        taxonomy=("cargo-toml", "feature-flags"),
        needs_cargo=True,
        scaffold={
            "src/lib.rs": "pub fn ping() -> u32 { 1 }\n",
        },
        notes="Feature-flag conflict: default references an undefined feature.",
    ),

    # --- Loose file (standalone rustc path) ---

    _conflict(
        id="loose_file_script",
        path="cfg.rs",
        base=(
            "pub fn greet(name: &str) -> String {\n"
            '    format!("hi {}", name)\n'
            "}\n"
        ),
        current=(
            "pub fn greet(name: &str) -> String {\n"
            '    format!("hello {}", name)\n'
            "}\n"
        ),
        replayed=(
            "pub fn greet(name: &str) -> String {\n"
            '    format!("howdy {}", name)\n'
            "}\n"
        ),
        expected_resolved='    format!("hello and howdy {}", name)',
        broken_resolved='    format!("hello {}", name',  # unclosed macro
        taxonomy=("loose-file", "standalone-rustc"),
        needs_cargo=False,
        notes="Loose .rs with no Cargo.toml → standalone rustc path.",
    ),
]


# A quick lookup by id for tests that want a specific case.
CONFLICT_BY_ID: dict[str, RustConflict] = {c.id: c for c in RUST_CONFLICTS}


def cargo_conflicts() -> list[RustConflict]:
    """The subset of cases that require cargo (crate-context checks)."""
    return [c for c in RUST_CONFLICTS if c.needs_cargo]


def loose_conflicts() -> list[RustConflict]:
    """The subset of cases that use standalone rustc (loose files)."""
    return [c for c in RUST_CONFLICTS if not c.needs_cargo]

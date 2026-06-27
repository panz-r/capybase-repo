"""Catalog-mutation generator for verifier-robustness property tests (Method C).

Round 3's honest form of "fuzzing": rather than generate random valid Rust from
scratch (poor ROI — no baseline-comparison oracle exists, and random ASTs mostly
auto-merge or fail to compile), this takes the curated catalog bases and applies
**structure-preserving mutations** to probe verifier invariants the hand-authored
rows structurally cannot state:

1. **No-crash**: the verifier returns a result (never raises) on valid-but-odd
   splices derived from a mutation.
2. **Verdict-invariance**: a structure-preserving mutation (literal bump,
   identifier rename) applied *consistently* across all three git sides must
   PRESERVE the catalog case's accept/reject verdict. A flip means the verifier
   is sensitive to something it shouldn't be — a real bug signal.

(The AST-preservation-stability invariant — a mutation outside the conflict span
leaving ``ast_preserved`` unchanged — runs on the Phase A per-unit path, which
the catalog's ``verify_file`` harness does not exercise; it is deferred to a
later round that builds that harness.)

Deterministic by design: mutations are *enumerated* from the catalog structure
(literals found, identifiers present), never random. Same input → same mutations
every run, so these are reviewable property tests, not flaky fuzzers. Each
mutator returns the mutated ``(base, current, replayed)`` triple, or ``None`` to
signal "doesn't apply to this case" (skipped, not a failure).

Reuses the catalog's :func:`build_markers` contract: a generated mutation is
only kept if its three sides still produce a genuine ``git merge-file`` conflict
(no clean auto-merge).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tests.rust_conflict_catalog import RUST_CONFLICTS, RustConflict, build_markers


# ---------------------------------------------------------------------------
# Mutation data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Mutation:
    """One generated mutation of a catalog case.

    ``label`` identifies the mutator (for test parametrization ids).
    ``base``/``current``/``replayed`` are the mutated git sides; ``build_markers``
    over them has already been confirmed to produce a genuine conflict.
    ``expected_resolved``/``broken_resolved`` carry the catalog case's
    resolved-span texts, mutated the SAME way the sides were, so the accept and
    reject oracles still apply. ``target`` names the invariant the mutator probes
    ("no_crash" | "verdict_invariance").
    """

    label: str
    base: str
    current: str
    replayed: str
    expected_resolved: str
    broken_resolved: str
    target: str  # "no_crash" | "verdict_invariance"


# ---------------------------------------------------------------------------
# Mutators. Each applies a structure-preserving edit consistently across the
# three sides + the two resolved texts. Returns None when not applicable.
# ---------------------------------------------------------------------------

_INT_RE = re.compile(r"\b\d+\b")


def _bump_literal(text: str) -> str | None:
    """Bump the first integer literal in ``text`` to a different valid value.

    Returns None if no integer literal is present. The bump is deterministic
    (value+1000, wrapping suffix to stay distinctive) so repeated runs are
    stable. A value change is identity-preserving for the verifier: a correct
    merge already absorbs a literal change, and a broken merge's error is about
    structure/type, not the specific value.
    """
    m = _INT_RE.search(text)
    if m is None:
        return None
    n = int(m.group())
    # +1000 keeps it distinct and valid for any u*/i* context (small enough to
    # stay in u16 range for the catalog's port fields, large enough to differ).
    bumped = n + 1000
    # Replace only the first occurrence (search, not sub) for determinism.
    return text[: m.start()] + str(bumped) + text[m.end():], m.group()


def bump_numeric_literal(c: RustConflict) -> tuple[str, str, str, str, str] | None:
    """Bump the first integer literal present in ALL three sides + both resolves.

    The literal must appear in base, current, replayed, AND the resolved texts
    (else the splice wouldn't carry the bump). Returns the mutated 5-tuple
    (base, current, replayed, expected, broken), or None if not applicable.

    Rust-only: a Cargo.toml literal is a version/feature string tied to a
    scaffold sibling crate, and bumping it desyncs the manifest from what cargo
    can resolve (a mutator artifact, not a verifier finding). Value-local
    integers (a port default, a retry count) live only in .rs source.
    """
    if c.language != "rust":
        return None
    for src in (c.base, c.current, c.replayed):
        if _INT_RE.search(src) is None:
            return None
    # Find a literal value common to all three sides to bump consistently.
    base_match = _INT_RE.search(c.base)
    cur_match = _INT_RE.search(c.current)
    rep_match = _INT_RE.search(c.replayed)
    if not (base_match and cur_match and rep_match):
        return None
    lit = base_match.group()
    # Must be the same literal across sides for a consistent bump.
    if cur_match.group() != lit or rep_match.group() != lit:
        return None
    bumped = str(int(lit) + 1000)
    b = c.base.replace(lit, bumped, 1)
    cu = c.current.replace(lit, bumped, 1)
    rp = c.replayed.replace(lit, bumped, 1)
    ex = c.expected_resolved.replace(lit, bumped, 1) if lit in c.expected_resolved else c.expected_resolved
    br = c.broken_resolved.replace(lit, bumped, 1) if lit in c.broken_resolved else c.broken_resolved
    return (b, cu, rp, ex, br)


def _rename_first_identifier(text: str) -> tuple[str, str] | None:
    """Rename the first ``snake_case`` identifier token in ``text``.

    Returns (new_text, chosen_old_name) or None. Targets parameter/local names
    (snake_case per Rust convention), not types (CamelCase) or keywords. The
    rename is consistent: the same old→new mapping applies everywhere the token
    appears (whole-word replacement), so semantics are preserved.
    """
    # snake_case identifiers: lowercase letters/digits/underscores, length>=2,
    # not purely numeric. Skip if it's a known keyword.
    m = re.search(r"\b([a-z][a-z0-9_]{1,})\b", text)
    if m is None:
        return None
    name = m.group(1)
    if name in _KEYWORDS_AND_TYPES:
        # try the next match
        m2 = re.search(r"\b([a-z][a-z0-9_]{1,})\b", text[m.end():])
        if m2 is None:
            return None
        name = m2.group(1)
        if name in _KEYWORDS_AND_TYPES:
            return None
    return text, name  # caller does the whole-word replace with the suffix


# Rust keywords AND primitive type names the renamer must never touch. Primitive
# numeric types (u16, i32, f64, ...) look like snake_case identifiers to a regex
# but renaming them produces a non-existent type → a false "verdict flip" that's
# really a mutator artifact. bool/char/str likewise.
_KEYWORDS_AND_TYPES = {
    # keywords
    "fn", "let", "mut", "pub", "use", "mod", "impl", "struct", "enum", "trait",
    "for", "in", "if", "else", "match", "while", "loop", "return", "self",
    "crate", "super", "as", "ref", "move", "where", "type", "const", "static",
    "unsafe", "extern", "async", "await", "dyn", "true", "false",
    # primitive / built-in types (regex sees them as snake_case)
    "bool", "char", "str", "f32", "f64",
    "i8", "i16", "i32", "i64", "i128", "isize",
    "u8", "u16", "u32", "u64", "u128", "usize",
    # common std prelude names that are lowercase (renaming breaks resolution)
    "vec", "string", "option", "result", "ok", "err", "some", "none",
    "println", "print", "format", "write", "writeln", "vec",
    "ready", "poll", "pin", "context",  # Future-impl scaffold names
}


def _is_path_member(text: str, name: str) -> bool:
    """True if ``name`` appears in a path/type-annotation context anywhere in
    text. Renaming such a name desyncs from the scaffold (e.g.
    ``crate::submod::helper`` where the submodule exports ``helper``), so the
    renamer skips it. Catches a name used as a path segment (``name::`` or
    ``::name``), a type annotation (``name:``), or a path tail (``:: name``).
    """
    esc = re.escape(name)
    return bool(
        re.search(rf"\b{esc}\s*::", text)
        or re.search(rf"::\s*{esc}\b", text)
        or re.search(rf"\b{esc}\s*:", text)
    )


def rename_identifier(c: RustConflict) -> tuple[str, str, str, str, str] | None:
    """Rename a snake_case LOCAL identifier consistently across sides + resolves.

    Picks a local identifier (a value: variable/param/local fn) present in all
    three sides AND both resolved texts, appends ``_x``, replaces whole-word.
    Returns the mutated 5-tuple or None.

    Exclusions (each prevents a false "verdict flip" that's really a mutator
    artifact, not a verifier finding):
    - non-rust cases (Cargo.toml identifiers aren't Rust locals),
    - keywords/primitive types/prelude names (``u16``, ``vec``...),
    - names in path (``::``) or type-annotation (``:``) context — these reference
      scaffold-external definitions and renaming desyncs the merged file from
      its crate,
    - names not in BOTH resolved texts — the rename must carry through the
      splice to actually exercise the verdict on the merged content.
    """
    if c.language != "rust":
        return None
    all_text = c.base + c.current + c.replayed + c.expected_resolved + c.broken_resolved

    def local_idents(s: str) -> set[str]:
        return {
            m.group(0)
            for m in re.finditer(r"\b[a-z][a-z0-9_]{1,}\b", s)
        } - _KEYWORDS_AND_TYPES

    # Must be present in all three sides AND both resolves (carry-through).
    in_sides = local_idents(c.base) & local_idents(c.current) & local_idents(c.replayed)
    in_resolve = local_idents(c.expected_resolved) & local_idents(c.broken_resolved)
    candidates = in_sides & in_resolve
    if not candidates:
        return None
    # Drop any name used in a path/type-annotation context anywhere (desync risk).
    candidates = {n for n in candidates if not _is_path_member(all_text, n)}
    if not candidates:
        return None
    name = sorted(candidates)[0]
    new = name + "_x"
    b = _word_replace(c.base, name, new)
    cu = _word_replace(c.current, name, new)
    rp = _word_replace(c.replayed, name, new)
    ex = _word_replace(c.expected_resolved, name, new)
    br = _word_replace(c.broken_resolved, name, new)
    return (b, cu, rp, ex, br)


def _word_replace(text: str, old: str, new: str) -> str:
    """Whole-word replace of ``old`` with ``new`` (word boundaries)."""
    return re.sub(rf"\b{re.escape(old)}\b", new, text)


# ---------------------------------------------------------------------------
# generate_mutations: apply each mutator to a case, keep only genuine conflicts.
# ---------------------------------------------------------------------------


_MUTATORS = (
    ("bump_literal", bump_numeric_literal, "verdict_invariance"),
    ("rename_ident", rename_identifier, "verdict_invariance"),
)


def generate_mutations(case: RustConflict) -> list[Mutation]:
    """Yield the applicable mutations of ``case`` that still conflict.

    Each mutator's mutated sides are fed to ``build_markers``; only those that
    produce a genuine ``git merge-file`` conflict (no clean auto-merge) are kept.
    This guarantees every returned Mutation is resolvable to a real conflict
    block, exactly like a curated catalog row — but combinatorially generated.
    """
    out: list[Mutation] = []
    for label, mutator, target in _MUTATORS:
        result = mutator(case)
        if result is None:
            continue
        b, cu, rp, ex, br = result
        # The mutation must still produce a conflict (else it's a clean auto-
        # merge and there's nothing to resolve). build_markers raises if it
        # merges cleanly.
        try:
            build_markers(b, cu, rp)
        except RuntimeError:
            continue
        out.append(
            Mutation(
                label=label, base=b, current=cu, replayed=rp,
                expected_resolved=ex, broken_resolved=br, target=target,
            )
        )
    return out


# Pre-compute the full (case_id, mutation) table for parametrization. Built once
# at import so test ids are stable and collection is cheap.
ALL_MUTATIONS: list[tuple[str, Mutation]] = [
    (c.id, m) for c in RUST_CONFLICTS for m in generate_mutations(c)
]
MUTATIONS_BY_CASE: dict[str, list[Mutation]] = {
    c.id: generate_mutations(c) for c in RUST_CONFLICTS
}

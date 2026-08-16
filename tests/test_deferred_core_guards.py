"""Deferred-core recursion guards — the protobuf-0065 ballooning class.

``_try_generalized_mini_conflict`` shrinks a conflict to an ambiguous core
and defers it; the orchestrator resolves the core through the FULL cascade,
which can re-trigger mini-conflict on the core. On misaligned sides
(base-indexed slices into differently-lengthed side arrays) the emitted
core can be degenerate or a fixpoint: the recursion then runs to Python's
stack limit (~327 levels), the RecursionError is swallowed by a broad
except, and each level's misaligned assembly accumulates content —
protobuf-0065 wrote a 431KB file from a 89KB expectation (4.82x) while
token-set similarity read 0.9996 (duplicates collapse in set semantics).

These tests pin the guards that kill the class:
- emitters decline empty/degenerate cores (nothing to defer);
- emitters decline non-shrinking cores (a fixpoint core has no value);
- resolve_structurally skips the mini-conflict family beyond a depth cap;
- the depth plumbing is one metadata key.
"""

from __future__ import annotations

from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.structural_resolver import resolve_structurally


def _mkunit(base_t, cur_t, rep_t, meta=None):
    return ConflictUnit(
        session_id="s", step_index=0, path="p.cc", language="cpp",
        unit_id="p.cc:1:0", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base_t),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=cur_t),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=rep_t),
        original_worktree_text="x", marker_span=(0, 0),
        structural_metadata=dict(meta or {}),
    )


# A conflict with a real ambiguous middle (both sides changed the same base
# lines differently) and deterministic tails — the shape mini-conflict
# exists for.
_BASE = "\n".join([f"line{i}" for i in range(12)])
_CUR = _BASE.replace("line5", "cur5").replace("line6", "cur6")
_REP = _BASE.replace("line5", "rep5").replace("line6", "rep6")


def test_mini_conflict_still_fires_on_healthy_shape():
    res = resolve_structurally(_mkunit(_BASE, _CUR, _REP))
    assert res is not None
    assert res.rule in ("mini_conflict", "partial_disjoint_merge")
    if res.deferred_core is not None:
        core_b, core_c, core_r = res.deferred_core
        # The healthy core is non-degenerate and strictly smaller.
        assert core_c.strip() or core_r.strip()
        assert len(core_c.splitlines()) < len(_CUR.splitlines())


def test_mini_conflict_declines_at_depth_cap():
    # Beyond the depth cap the mini-conflict family must decline so the
    # core resolves via portfolio/SBCR/LLM instead of recursing.
    res = resolve_structurally(_mkunit(
        _BASE, _CUR, _REP, meta={"deferred_core_depth": 2}))
    assert res is None or res.rule not in (
        "mini_conflict", "partial_disjoint_merge",
        "mini_conflict_deterministic")


def test_depth_cap_allows_first_levels():
    res = resolve_structurally(_mkunit(
        _BASE, _CUR, _REP, meta={"deferred_core_depth": 1}))
    assert res is not None  # depth 1 (a first-level core) still resolves


def test_degenerate_core_emission_declined():
    # A misalignment-shaped input: sides shorter than base at the ambiguous
    # indices, so the base-indexed slices produce empty/degenerate cores.
    # Whatever the rule does, it must not emit a deferred core with no
    # resolvable content.
    base_t = "\n".join([f"b{i}" for i in range(15)])
    cur_t = "\n".join([f"c{i}" for i in range(6)])   # much shorter
    rep_t = "\n".join([f"r{i}" for i in range(7)])
    res = resolve_structurally(_mkunit(base_t, cur_t, rep_t))
    if res is not None and res.deferred_core is not None:
        core_b, core_c, core_r = res.deferred_core
        assert core_c.strip() or core_r.strip(), (
            "emitted a deferred core with nothing to resolve")
        # And the core must be a strict shrink of the input conflict.
        nb_in = sum(1 for t in (cur_t, rep_t) for l in t.splitlines() if l.strip())
        nb_core = sum(
            1 for t in (core_c, core_r) for l in t.splitlines() if l.strip())
        assert nb_core < nb_in, "emitted a non-shrinking (fixpoint) core"

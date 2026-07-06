"""Tests for the cross-commit dependency guardian (survey §3.1 / Phase 3).

Closes the per-commit blind spot: commit A renames ``foo``→``bar``, commit B
still calls ``foo``. No per-commit validator sees both, so the final rebased
branch breaks silently. These tests exercise the pure functions with synthetic
commit contents (no repo required).
"""

from __future__ import annotations

import pytest

from capybase.adapters import structural
from capybase.cross_commit import (
    audit_cross_commit_dependencies,
    build_commit_symbols,
    build_dependency_graph,
)

pytestmark = pytest.mark.skipif(
    not structural.is_available("python"),
    reason="tree-sitter Python grammar unavailable",
)


def _files(**named: str) -> dict[str, str]:
    """Build a {path: text} dict; names map to '<name>.py' paths by default."""
    return {f"{k}.py": v for k, v in named.items()}


def test_build_commit_symbols_captures_defines_and_uses():
    """A commit defining ``foo`` and calling ``bar`` (defined elsewhere) records
    foo in defines and bar in uses (a name used-but-not-defined is a dependency)."""
    files = _files(app="def foo():\n    return bar()\n")
    syms = build_commit_symbols(files)
    assert ("function", "foo") in syms.defines
    assert "bar" in syms.uses
    # foo is locally defined → NOT a use (no self-dependency).
    assert "foo" not in syms.uses


def test_build_dependency_graph_creates_edge_for_cross_commit_use():
    """Commit A defines ``helper``; commit B (later) calls ``helper`` → an edge
    A→B exists (B depends on A's definition)."""
    a = build_commit_symbols(_files(a="def helper():\n    return 1\n"))
    b = build_commit_symbols(_files(b="def main():\n    return helper()\n"))
    order = ["A", "B"]
    edges = build_dependency_graph({"A": a, "B": b}, order)
    helper_edges = [e for e in edges if e.symbol == "helper"]
    assert len(helper_edges) == 1
    assert helper_edges[0].definer == "A"
    assert helper_edges[0].user == "B"


def test_no_edge_when_earlier_commit_uses_later_definition():
    """Edges go earlier→later only. An earlier commit using a name a LATER commit
    defines is not a forward dependency (and is typically a builtin/external)."""
    a = build_commit_symbols(_files(a="def main():\n    return helper()\n"))
    b = build_commit_symbols(_files(b="def helper():\n    return 1\n"))
    edges = build_dependency_graph({"A": a, "B": b}, ["A", "B"])
    # 'helper' is used by A but defined by B (later) → no A→B edge for helper.
    assert not any(e.symbol == "helper" and e.definer == "B" for e in edges)


def test_audit_flags_missing_definition_after_rename():
    """The headline case: commit A defines ``foo``; commit B calls ``foo``. The
    final tree renamed ``foo``→``bar`` (so ``foo`` is gone by name) → the edge's
    ``foo`` is missing from the final tree → a break is reported."""
    a = build_commit_symbols(_files(a="def foo():\n    return 1\n"))
    b = build_commit_symbols(_files(b="def main():\n    return foo()\n"))
    edges = build_dependency_graph({"A": a, "B": b}, ["A", "B"])
    # Final tree: foo was renamed to bar; foo() is gone by name.
    final = {
        "main.py": structural.enumerate_entities(
            "def main():\n    return foo()\n", "python"),
        "a.py": structural.enumerate_entities(
            "def bar():\n    return 1\n", "python"),  # renamed away from foo
    }
    breaks = audit_cross_commit_dependencies(edges, final)
    foo_breaks = [b for b in breaks if b.symbol == "foo"]
    assert len(foo_breaks) == 1
    assert foo_breaks[0].break_type == "missing_definition"
    assert foo_breaks[0].definer == "A"
    assert foo_breaks[0].user == "B"


def test_audit_clean_when_symbol_survives_by_name():
    """When the dependency symbol survives in the final tree by name, no break."""
    a = build_commit_symbols(_files(a="def foo():\n    return 1\n"))
    b = build_commit_symbols(_files(b="def main():\n    return foo()\n"))
    edges = build_dependency_graph({"A": a, "B": b}, ["A", "B"])
    # Final tree keeps foo by name → resolved.
    final = {
        "a.py": structural.enumerate_entities(
            "def foo():\n    return 1\n", "python"),
    }
    breaks = audit_cross_commit_dependencies(edges, final)
    assert breaks == []


def test_audit_clean_when_no_cross_commit_dependency():
    """A window where no commit uses a symbol another defines → no edges, no
    breaks, even if the final tree differs."""
    a = build_commit_symbols(_files(a="def foo():\n    return 1\n"))
    b = build_commit_symbols(_files(b="def bar():\n    return 2\n"))
    edges = build_dependency_graph({"A": a, "B": b}, ["A", "B"])
    assert edges == []
    assert audit_cross_commit_dependencies(edges, {}) == []


def test_audit_dedups_repeated_symbol_across_users():
    """The same symbol used by multiple later commits produces one break per
    (symbol, definer, user) — not duplicated per edge lookup."""
    a = build_commit_symbols(_files(a="def foo():\n    return 1\n"))
    b1 = build_commit_symbols(_files(b1="def main():\n    return foo()\n"))
    b2 = build_commit_symbols(_files(b2="def other():\n    return foo()\n"))
    edges = build_dependency_graph({"A": a, "B1": b1, "B2": b2}, ["A", "B1", "B2"])
    final = {"a.py": structural.enumerate_entities("def bar():\n    return 1\n", "python")}
    breaks = audit_cross_commit_dependencies(edges, final)
    foo_breaks = [b for b in breaks if b.symbol == "foo"]
    assert len(foo_breaks) == 2  # one per distinct user (B1, B2)
    users = {b.user for b in foo_breaks}
    assert users == {"B1", "B2"}


def test_render_human_readable():
    """The break renders a human-readable line naming the commits + symbol."""
    from capybase.cross_commit import DependencyBreak

    b = DependencyBreak(
        symbol="foo", definer="abcdef12", user="12345678",
        break_type="missing_definition",
    )
    r = b.render()
    assert "foo" in r and "12345678"[:8] in r and "abcdef12"[:8] in r


def test_unsupported_language_files_are_skipped():
    """A commit whose touched files are all unsupported languages produces an
    empty symbol set (graceful degradation, no crash)."""
    syms = build_commit_symbols({"app.js": "function foo() { return bar(); }"})
    assert syms.defines == frozenset()


# ---------------------------------------------------------------------------
# Intent evolution trace (survey §3.2 / Phase 2)
# ---------------------------------------------------------------------------

from capybase.cross_commit import (  # noqa: E402
    audit_evolution,
    build_evolution_chains,
)


def _evol_files(app_py: str) -> dict:
    return {"app.py": app_py}


def test_evolution_chain_built_for_multi_commit_entity():
    """An entity defined in ≥2 commits gets an evolution chain; the last step's
    body fingerprint is the expected final body."""
    # commit A: foo returns 1; commit B: foo returns 2 (body evolved).
    a = _evol_files("def foo():\n    return 1\n")
    b = _evol_files("def foo():\n    return 2\n")
    chains = build_evolution_chains({"A": a, "B": b}, ["A", "B"], "python")
    foo_chains = [c for c in chains if c.name == "foo"]
    assert len(foo_chains) == 1
    chain = foo_chains[0]
    assert len(chain.steps) == 2
    assert chain.steps[0].commit_oid == "A"
    assert chain.steps[1].commit_oid == "B"
    # Expected final body = last step's (commit B's) fingerprint.
    assert chain.expected_body_fingerprint == chain.steps[-1].body_fingerprint


def test_evolution_chain_excludes_single_commit_entities():
    """An entity defined in only one commit has no evolution chain (nothing to
    lose). Only multi-commit entities span a chain."""
    a = _evol_files("def foo():\n    return 1\n")
    b = _evol_files("def foo():\n    return 1\n\ndef bar():\n    return 2\n")
    chains = build_evolution_chains({"A": a, "B": b}, ["A", "B"], "python")
    names = {c.name for c in chains}
    # foo spans both → chain; bar only in B → no chain.
    assert "foo" in names
    assert "bar" not in names


def test_audit_flags_gap_when_merge_reverts_evolution():
    """The headline case: foo evolved across A→B (return 1 → return 2), but the
    final merge kept A's version (return 1). The audit flags the gap — the merge
    lost the last evolution step no per-commit validator sees."""
    a = _evol_files("def foo():\n    return 1\n")
    b = _evol_files("def foo():\n    return 2\n")
    chains = build_evolution_chains({"A": a, "B": b}, ["A", "B"], "python")
    # Final tree has foo = return 1 (A's version) → diverges from B's.
    final = {"app.py": structural.enumerate_entities(
        "def foo():\n    return 1\n", "python")}
    gaps = audit_evolution(chains, final)
    foo_gaps = [g for g in gaps if g.name == "foo"]
    assert len(foo_gaps) == 1
    assert foo_gaps[0].commit_count == 2
    assert foo_gaps[0].expected_from_commit == "B"


def test_audit_clean_when_merge_matches_latest_evolution():
    """When the final merge matches the entity's LAST evolution step, no gap."""
    a = _evol_files("def foo():\n    return 1\n")
    b = _evol_files("def foo():\n    return 2\n")
    chains = build_evolution_chains({"A": a, "B": b}, ["A", "B"], "python")
    # Final tree has foo = return 2 (B's version) → matches → no gap.
    final = {"app.py": structural.enumerate_entities(
        "def foo():\n    return 2\n", "python")}
    gaps = audit_evolution(chains, final)
    assert gaps == []


def test_audit_no_gap_when_entity_gone_from_final():
    """An entity absent from the final tree isn't an evolution GAP (the dependency
    guardian covers missing symbols); the evolution audit skips it."""
    a = _evol_files("def foo():\n    return 1\n")
    b = _evol_files("def foo():\n    return 2\n")
    chains = build_evolution_chains({"A": a, "B": b}, ["A", "B"], "python")
    # Final tree has no foo at all.
    final = {"app.py": structural.enumerate_entities("def other():\n    pass\n", "python")}
    gaps = audit_evolution(chains, final)
    assert gaps == []


def test_audit_render_human_readable():
    """The gap renders a human-readable line naming the entity + commit count."""
    from capybase.cross_commit import EvolutionGap

    g = EvolutionGap(
        name="foo", kind="function", commit_count=3,
        expected_from_commit="abcdef12", actual_body_fingerprint="x",
        expected_body_fingerprint="y",
    )
    r = g.render()
    assert "foo" in r and "3 commit" in r and "abcdef12"[:8] in r


def test_evolution_degrades_when_parser_unavailable(monkeypatch):
    """When the structural parser is unavailable, build_evolution_chains returns
    []. No crash. The guardian gates on is_available before enumerating."""
    # Force is_available False (the capability probe the guardian checks first).
    monkeypatch.setattr(structural, "is_available", lambda lang: False)
    a = _evol_files("def foo():\n    return 1\n")
    b = _evol_files("def foo():\n    return 2\n")
    chains = build_evolution_chains({"A": a, "B": b}, ["A", "B"], "python")
    assert chains == []


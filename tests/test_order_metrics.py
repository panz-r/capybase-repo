"""S28-167 — order-sensitive secondary oracle metrics (EVAL ONLY).

The token-Jaccard sim is order-blind: identical token multisets score
1.0 in any line order, so a scrambled merge of the oracle's lines would
PASS identically to a real one (14 s28 PASS rows at sim >= 0.99 carry
oracle line-presence 0.017-0.889 — legitimate regenerations, but the
PASS class was unauditable for order defects). These fields record the
order evidence beside matches_oracle; never a production gate — the
compiler is the authority.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "live_eval_realworld_order",
        Path(__file__).resolve().parent.parent / "scripts" / "live_eval_realworld.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["live_eval_realworld_order"] = mod
    spec.loader.exec_module(mod)  # type: ignore[arg-type]
    return mod


_M = _load_module()


def test_identical_text_scores_perfect():
    oracle = "int f(int a) {\n    return a + 1;\n}\n\nint g() {\n    return 2;\n}\n"
    assert _M._oracle_line_presence(oracle, oracle) == 1.0
    assert _M._oracle_order_score(oracle, oracle) == 1.0


def test_scrambled_merge_presence_perfect_order_low():
    """The exact defect S28-167 closes: the oracle's lines in scrambled
    statement order score a PERFECT token sim — the order score is what
    exposes it."""
    oracle = (
        "#include <vector>\n"
        "int helper(int x) { return x * 2; }\n"
        "int main() {\n"
        "    std::vector<int> v;\n"
        "    v.push_back(helper(21));\n"
        "    return v[0];\n"
        "}\n"
    )
    scrambled = (
        "int main() {\n"
        "    return v[0];\n"
        "}\n"
        "int helper(int x) { return x * 2; }\n"
        "#include <vector>\n"
        "std::vector<int> v;\n"
        "v.push_back(helper(21));\n"
    )
    # order-blindness pinned: the token metric cannot see this defect
    assert _M._token_jaccard(scrambled, oracle) == 1.0
    assert _M._oracle_line_presence(scrambled, oracle) == 1.0
    order = _M._oracle_order_score(scrambled, oracle)
    assert order is not None and order < 0.5, order


def test_regeneration_low_presence_low_order_consistent():
    """The legitimate-regeneration class (duckdb-0097): different code,
    same vocabulary. BOTH fields read low — the cross-tab flags reorder
    defects only when presence is HIGH and order is LOW, so this class
    is not misflagged."""
    oracle = "void parse_json() {\n    lexer_next();\n    node_take();\n    emit_tree();\n}\n"
    regenerated = (
        "static table_t g_tbl;\n"
        "static void slots_reset(void) {\n"
        "    memset(g_tbl.slot, 0, sizeof g_tbl.slot);\n"
        "    g_tbl.generation += 1;\n"
        "}\n"
    )
    p = _M._oracle_line_presence(regenerated, oracle)
    o = _M._oracle_order_score(regenerated, oracle)
    assert p is not None and p <= 1 / 5, p  # only the closing brace is shared
    assert o is not None and o <= 1 / 5, o


def test_multiset_counting_repeats_never_inflate():
    """A line the oracle carries once counts once, even when the output
    repeats it — presence is a multiset intersection, not a set one."""
    oracle = "alpha\nbeta\ngamma\n"
    output = "alpha\nalpha\nalpha\nalpha\nzeta\n"
    p = _M._oracle_line_presence(output, oracle)
    assert p is not None and abs(p - 1 / 3) < 1e-9, p


def test_whitespace_and_blank_normalization():
    """The side_preservation convention: EDGE whitespace is ignored,
    internal runs are significant, blank lines never count."""
    oracle = "int  x;\n\n   y = 1;   \n"  # 2 countable lines
    output = "\nint  x;\n     y = 1;\n"  # edge pad + blanks don't dent it
    p = _M._oracle_line_presence(output, oracle)
    assert p == 1.0, p
    # an internal-whitespace change is a different line (edge-strip only)
    assert _M._oracle_line_presence("int x;\n", oracle) == 0.0


def test_empty_and_degenerate_cases_return_none():
    assert _M._oracle_order_fields("", "a\nb\n") == (None, None)
    assert _M._oracle_line_presence("a\n", "") is None
    assert _M._oracle_order_score("a\n", "") is None
    assert _M._oracle_line_presence("", "") is None
    assert _M._oracle_order_score("", "") is None


def test_monster_file_guard_returns_none():
    """S28-164 doctrine: the quadratic matcher never runs on monster
    files — the fields read as n/a there, like side_preservation."""
    big = "\n".join(f"line{i}" for i in range(30_001))
    assert _M._oracle_order_score(big, big) is None


def test_verdict_chain_untouched_by_order_metrics():
    """The metrics are diagnostic only: nothing in _verdict_chain reads
    them — a scrambled merge still gets exactly the verdict its sim
    earned (the doctrine: the compiler judges, the fields audit)."""
    r = _M.CaseResult(id="x", language="cpp", dataset="d")
    r.escalated = False
    r.marker_free = True
    r.compiles = True
    r.matches_oracle = 1.0  # order-blind PASS
    r.oracle_line_presence = 1.0
    r.oracle_order_score = 0.31  # scrambled
    assert _M._verdict_chain(r) == "PASS"  # unchanged


def test_fields_flow_into_results_json():
    """The rows serialize via __dict__ — the harvest cross-tab needs the
    fields present on the dumped row dicts."""
    r = _M.CaseResult(id="x", language="cpp", dataset="d")
    r.oracle_line_presence = 0.017
    r.oracle_order_score = 0.42
    import json
    row = json.loads(json.dumps(r.__dict__))
    assert row["oracle_line_presence"] == 0.017
    assert row["oracle_order_score"] == 0.42

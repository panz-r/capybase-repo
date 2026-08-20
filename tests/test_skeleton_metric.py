"""Sprint-20 S20.11 — control-flow skeleton intent metric (EVAL ONLY).

Flags idiomatic rewrites: outputs whose token similarity to the oracle
is low but whose structural intent (ordered control-flow/definition
keyword stream) is preserved. Informs future metric design; never a
production gate — the compiler is the authority.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "live_eval_realworld_skeleton",
        Path(__file__).resolve().parent.parent / "scripts" / "live_eval_realworld.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["live_eval_realworld_skeleton"] = mod
    spec.loader.exec_module(mod)  # type: ignore[arg-type]
    return mod


_M = _load_module()


def test_idiomatic_rewrite_high_skeleton_low_jaccard():
    """The plan's exact scenario: same control flow, everything else
    rewritten (naming, formatting, idiom) — jaccard collapses, the
    skeleton doesn't."""
    oracle = (
        "def process(items):\n"
        "    for item in items:\n"
        "        if item.is_valid():\n"
        "            return item\n"
        "    return None\n"
    )
    rewrite = (
        "def handle(entries: List[Entry]) -> Optional[Entry]:\n"
        "    for entry in entries:\n"
        "        if entry.valid is True:\n"
        "            return entry\n"
        "    return None\n"
    )
    j = _M._token_jaccard(rewrite, oracle)
    s = _M._skeleton_similarity(rewrite, oracle)
    assert j < 0.60, j
    assert s >= 0.85, s


def test_different_control_flow_low_skeleton():
    a = "def f(x):\n    for i in x:\n        if i:\n            return i\n    return None\n"
    b = "def f(x):\n    while x:\n        try:\n            break\n        except:\n            continue\n"
    assert _M._skeleton_similarity(a, b) < 0.6


def test_empty_and_no_keywords_safe():
    assert _M._skeleton_similarity("", "if x:") == 0.0
    assert _M._skeleton_similarity("a b c", "") == 0.0
    # no keywords on either side → 0.0 (no structural signal)
    assert _M._skeleton_similarity("x = 1\n", "y = 2\n") == 0.0


def test_verdict_chain_untouched_by_skeleton():
    """The metric is diagnostic only: a high skeleton similarity never
    upgrades a verdict (the field is recorded beside matches_oracle and
    nothing in _verdict_chain reads it)."""
    r = _M.CaseResult(id="x", language="python", dataset="d")
    r.escalated = False
    r.marker_free = True
    r.compiles = True
    r.matches_oracle = 0.5  # low jaccard
    r.skeleton_similarity = 0.99  # high skeleton
    assert _M._verdict_chain(r) == "ORACLE_DIVERGENT"  # unchanged

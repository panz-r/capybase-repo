"""S28-29: VALIDATION_EXHAUSTED split out of TIMEOUT_CONVERGENCE.

The terminal-reason classifier routed "could not resolve ... (error/
syntax/delimiter)" reasons into TIMEOUT_CONVERGENCE, whose docstring
meaning is "CEGIS loop failed to converge (no-progress / wall-time)".
Both leg-1 rows carrying the label were actually validation exhaustion
(zenodo-hdiff-0019: four candidates all failing py_compile on an
IndentationError in 70s; nlohmann-json-history-0038: candidates failing
the build) — different prescription (capability/repair, not budget).
Vectors are the exact live reason strings from the s28 harvest.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "live_eval_realworld_taxonomy",
        Path(__file__).resolve().parent.parent / "scripts" / "live_eval_realworld.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["live_eval_realworld_taxonomy"] = mod
    spec.loader.exec_module(mod)  # type: ignore[arg-type]
    return mod


_ZENODO_0019 = (
    "could not resolve conflict_0019.py:1:0 (last failures: Sorry: "
    "IndentationError: expected an indented block after 'else' statement "
    "on li)"
)
_NLOHMANN_0038 = (
    "could not resolve single_include/nlohmann/json.hpp:1:0#s0 (last "
    "failures: /var/tmp/tmp1r1j56h2.hpp:13839:1: error: version control "
    "conflict marker in file)"
)


def test_validation_exhausted_syntax_vector():
    """zenodo-hdiff-0019's IndentationError exhaustion is a capability/
    repair outcome, not a wall-time one (pre-fix: TIMEOUT_CONVERGENCE)."""
    mod = _load_module()
    assert mod._classify_terminal_reason(_ZENODO_0019) == "VALIDATION_EXHAUSTED"


def test_validation_exhausted_compile_vector():
    """nlohmann-json-history-0038's build-failure exhaustion likewise."""
    mod = _load_module()
    assert mod._classify_terminal_reason(_NLOHMANN_0038) == "VALIDATION_EXHAUSTED"


def test_genuine_wall_time_still_timeout_convergence():
    """A real no-progress/wall-time reason keeps TIMEOUT_CONVERGENCE —
    the split must not swallow the budget-exhaustion class.
    S28-202: the bare convergence phrase (no wall language) is NOT
    timeout evidence — it routes to the generic VALIDATION_EXHAUSTED
    (the zenodo class: 69-428s against a 1200s budget, never near a
    wall); with elapsed evidence (>=90% of budget) it stays a
    timeout."""
    mod = _load_module()
    assert mod._classify_terminal_reason(
        "could not resolve f.py: no hard-failure progress across 4 rounds "
        "(wall-time budget exhausted)") == "TIMEOUT_CONVERGENCE"
    assert mod._classify_terminal_reason(
        "could not resolve f.py: CEGIS convergence threshold not met"
    ) == "VALIDATION_EXHAUSTED"
    assert mod._classify_terminal_reason(
        "could not resolve f.py: CEGIS convergence threshold not met",
        elapsed_s=1150.0, budget_s=1200.0,
    ) == "TIMEOUT_CONVERGENCE"


def test_could_not_resolve_without_failures_still_model_empty():
    """The plain could-not-resolve branch (no error/syntax/delimiter
    marker) keeps routing to MODEL_EMPTY."""
    mod = _load_module()
    assert mod._classify_terminal_reason(
        "could not resolve f.py:0#s0") == "MODEL_EMPTY"


def test_docstring_documents_the_new_class():
    mod = _load_module()
    doc = mod._classify_terminal_reason.__doc__ or ""
    assert "VALIDATION_EXHAUSTED" in doc, (
        "the classifier's docstring enumerates the disjoint terminal "
        "categories; the new class must be listed")

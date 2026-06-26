"""P1 (survey §1.3): histogram diff as the merge-file refinement default.

The merge-file adapter now selects the xdiff backend via ``-c
diff.algorithm=<alg>``. These tests pin the contract:

1. The algorithm is validated against an allowlist — an unknown value falls
   back to histogram, never reaching the subprocess as an arbitrary flag.
2. Each selectable backend is accepted by real git without error.
3. The default (histogram) is applied when no argument is passed.
4. The config knob threads through to the subprocess.
"""

from __future__ import annotations

import pytest

from capybase.adapters.git_diff3 import (
    DEFAULT_DIFF_ALGORITHM,
    DIFF_ALGORITHMS,
    _validated_algorithm,
    is_available,
    merge_file_diff3,
)

pytestmark = pytest.mark.skipif(not is_available(), reason="git not available")


# ---------------------------------------------------------------------------
# _validated_algorithm — the allowlist guard
# ---------------------------------------------------------------------------


def test_default_is_histogram():
    """The survey's #1 recommendation is the default, not Myers."""
    assert DEFAULT_DIFF_ALGORITHM == "histogram"


def test_known_algorithms_pass_through():
    for alg in DIFF_ALGORITHMS:
        assert _validated_algorithm(alg) == alg


def test_unknown_algorithm_falls_back_to_default():
    """An unknown value must NEVER reach git — that would be flag injection."""
    assert _validated_algorithm("rm -rf /") == DEFAULT_DIFF_ALGORITHM
    assert _validated_algorithm("patience --evil") == DEFAULT_DIFF_ALGORITHM
    assert _validated_algorithm("") == DEFAULT_DIFF_ALGORITHM


def test_none_falls_back_to_default():
    assert _validated_algorithm(None) == DEFAULT_DIFF_ALGORITHM


# ---------------------------------------------------------------------------
# Real-git acceptance — every selectable backend runs without error
# ---------------------------------------------------------------------------


_CONFlict_base = "def f():\n    return 1\n"
_CONFLICT_OURS = "def f():\n    return 2\n"
_CONFLICT_THEIRS = "def f():\n    return 3\n"


@pytest.mark.parametrize("alg", DIFF_ALGORITHMS)
def test_every_algorithm_accepted_by_git(alg):
    """git must accept each selectable backend (no exit-code >1 error)."""
    blocks = merge_file_diff3(
        _CONFlict_base, _CONFLICT_OURS, _CONFLICT_THEIRS, diff_algorithm=alg
    )
    # All backends agree this is one genuine conflict (both change the same line).
    assert blocks is not None
    assert len(blocks) == 1
    assert "return 1" in blocks[0].base
    assert "return 2" in blocks[0].ours
    assert "return 3" in blocks[0].theirs


def test_unknown_algorithm_still_produces_result():
    """An unknown value falls back to histogram silently rather than failing."""
    blocks = merge_file_diff3(
        _CONFlict_base,
        _CONFLICT_OURS,
        _CONFLICT_THEIRS,
        diff_algorithm="not-a-real-algorithm",
    )
    assert blocks is not None
    assert len(blocks) == 1


def test_default_call_uses_histogram():
    """Calling with no algorithm arg behaves identically to explicit histogram."""
    default_blocks = merge_file_diff3(_CONFlict_base, _CONFLICT_OURS, _CONFLICT_THEIRS)
    hist_blocks = merge_file_diff3(
        _CONFlict_base, _CONFLICT_OURS, _CONFLICT_THEIRS, diff_algorithm="histogram"
    )
    assert default_blocks == hist_blocks


# ---------------------------------------------------------------------------
# Config threading
# ---------------------------------------------------------------------------


def test_config_diff_algorithm_default_is_histogram():
    from capybase.config import StructuralConfig

    cfg = StructuralConfig()
    assert cfg.diff_algorithm == "histogram"


def test_config_diff_algorithm_validates_choice():
    from capybase.config import StructuralConfig

    for alg in DIFF_ALGORITHMS:
        cfg = StructuralConfig(diff_algorithm=alg)
        assert cfg.diff_algorithm == alg


def test_config_rejects_unknown_algorithm():
    """pydantic Literal enforces the allowlist at config-parse time, so a typo
    in capybase.toml surfaces immediately rather than silently using Myers."""
    from pydantic import ValidationError

    from capybase.config import StructuralConfig

    with pytest.raises(ValidationError):
        StructuralConfig(diff_algorithm="bogus")

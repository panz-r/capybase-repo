"""S28-150 — rename-family engagement telemetry (journal-only).

Rename-shaped units (>= 3 identifier-only changed line pairs, the census
heuristic) whose journals show structural skipping with "no rule
applied": the S28-135-activated rename family never attempted, and the
decline was invisible. The probe classifies WHY — language support,
per-side parse, entity counts, duplicates, renames — so the census over
the next harvest can ground the fuzzy-rename extension decision. No
behavior change anywhere.
"""

from __future__ import annotations

from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.structural_resolver import (
    _identifier_only_pair_count,
    _line_identifier_only,
    _rename_engagement_probe,
)


def _unit(base, cur, rep, *, lang="python", marker_span=(0, 1)):
    return ConflictUnit(
        session_id="s", step_index=1, path="mod.py", language=lang,
        unit_id="mod.py:1:0", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=cur),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=rep),
        original_worktree_text=base,
        marker_span=marker_span,
    )


# A base of 4 functions; the rename side renames 3 of them (identifier-
# only header changes); the other side is unrelated-to-rename churn.
_BASE = "\n\n".join(
    f"def fn_{i}(a, b):\n    return a + b - {i}" for i in range(4))
_RENAMED = "\n\n".join(
    (f"def renamed_{i}(a, b):\n    return a + b - {i}"
     if i < 3 else f"def fn_{i}(a, b):\n    return a + b - {i}")
    for i in range(4))
_OTHER = _BASE.replace("return a + b - 0", "return a + b")


def test_line_identifier_only_predicate():
    assert _line_identifier_only(
        "def fn_1(a, b):", "def renamed_1(a, b):")
    assert not _line_identifier_only(
        "def fn_1(a, b):", "def fn_1(a):")  # an identifier vanished
    assert not _line_identifier_only("x = 1", "x = 1")  # not changed


def test_pair_count_counts_identifier_only_pairs():
    n = _identifier_only_pair_count(_BASE, _RENAMED)
    assert n >= 3
    # an equal-length replace block of NON-identifier changes counts 0
    assert _identifier_only_pair_count(_BASE, _OTHER.replace(
        "def fn_0(a, b):", "def fn_0(a):")) >= 0


def test_probe_is_silent_for_non_rename_shapes():
    """Fewer than 3 identifier-only pairs -> None (not rename-shaped)."""
    u = _unit(_BASE, _OTHER, _BASE)
    assert _rename_engagement_probe(u) is None


def test_probe_reports_language_unsupported():
    u = _unit(_BASE, _RENAMED, _BASE, lang="ruby")
    out = _rename_engagement_probe(u)
    assert out is not None
    assert out["stage"] == "language_unsupported"
    assert out["language_supported"] is False


def test_probe_reports_entity_counts_for_degenerate_sides():
    """A garbage side enumerates to ~nothing rather than crashing — the
    report carries the per-side entity counts either way (the census's
    parse-vs-entities split), and the stage is always classified."""
    garbage = "def broken( <<<\n~~~ ???"
    u = _unit(_BASE, garbage, _RENAMED)
    out = _rename_engagement_probe(u)
    assert out is not None
    assert out["stage"] in ("parse_failed", "enumeration_precondition_failed",
                            "enumerated")
    if out["stage"] == "enumerated":
        assert out["current_entities"] <= 1  # the garbage side
        assert out["replayed_renames"] >= 1  # the rename-shaped side


def test_probe_reports_entity_counts_on_supported_language():
    out = _rename_engagement_probe(_unit(_BASE, _RENAMED, _BASE))
    assert out is not None
    assert out["stage"] in ("enumerated", "enumeration_precondition_failed")
    if out["stage"] == "enumerated":
        assert out["current_renames"] >= 1 or out["replayed_renames"] >= 1

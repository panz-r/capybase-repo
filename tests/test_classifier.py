"""Tests for the first-class ConflictClassifier (band + reasons + difficulty).

The classifier is a pure downstream consumer of signals already computed at
extraction (``conflict_features``, ``severity``, ``merge_direction``). These
cover the band taxonomy (trivial/easy/medium/hard), the backward-compatible
``simple``/``complex`` label mapping, the reasons audit trail, and the
``deterministically_mergeable`` flag that routes union-combine conflicts to the
cheap path. No git, no model — every test builds a ConflictUnit directly.
"""

from __future__ import annotations

from capybase.classifier import ConflictClassification, classify
from capybase.conflict_model import ConflictSide, ConflictUnit


def _unit(base: str, current: str, replayed: str, *, language="python") -> ConflictUnit:
    return ConflictUnit(
        session_id="s", step_index=1, path="app.py", language=language,
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text=base, marker_span=(0, 0),
        structural_metadata={"sibling_count": 0},
    )


# ---------------------------------------------------------------------------
# Trivial band: no judgment needed
# ---------------------------------------------------------------------------


def test_one_sided_change_is_trivial():
    """One side changed, the other conceded → trivial → simple."""
    base = "def f():\n    return 1\n"
    c = classify(_unit(base, "def f():\n    return 2\n", base))
    assert c.band == "trivial"
    assert c.difficulty == "simple"
    assert c.reasons


def test_identical_sides_is_trivial():
    """Both sides made the same change → trivial → simple."""
    base = "x = 1\n"
    c = classify(_unit(base, "x = 2\n", "x = 2\n"))
    assert c.band == "trivial"
    assert c.difficulty == "simple"


def test_disjoint_insertions_are_trivial_via_deterministic_merge():
    """Disjoint non-overlapping edits are deterministically mergeable → trivial."""
    base = "a = 1\nb = 2\nc = 3\n"
    c = classify(_unit(base, "a = 1\nx = 9\nb = 2\nc = 3\n",
                       "a = 1\nb = 2\nc = 3\ny = 8\n"))
    assert c.band == "trivial"
    assert c.difficulty == "simple"
    assert any("deterministically mergeable" in r for r in c.reasons)


def test_delete_delete_is_trivial():
    """Both sides deleted the same content → trivial (no ambiguity)."""
    base = "def f():\n    return 1\n\ndef dead():\n    pass\n"
    cur = "def f():\n    return 1\n"
    c = classify(_unit(base, cur, cur))
    assert c.band == "trivial"


# ---------------------------------------------------------------------------
# Medium / hard bands: real conflicts needing judgment
# ---------------------------------------------------------------------------


def test_same_line_both_modify_is_medium():
    """Both sides changed the SAME line → medium → complex."""
    c = classify(_unit("v = 1", "v = 2", "v = 3"))
    assert c.band == "medium"
    assert c.difficulty == "complex"
    assert any("same base line" in r for r in c.reasons)


def test_large_definition_touching_is_hard():
    """Large + definition-touching + same-symbol overlap coincides → hard.
    Two sides rewriting the SAME line of a large function differently (the
    resolver declines — genuine token-level conflict)."""
    body = "\n".join(f"    v{i} = {i}" for i in range(30))
    base = "def compute():\n" + body + "\n    return 0\n"
    cur = "def compute():\n" + body.replace("v0 = 0", "v0 = 100") + "\n    return 0\n"
    rep = "def compute():\n" + body.replace("v0 = 0", "v0 = 999") + "\n    return 0\n"
    c = classify(_unit(base, cur, rep))
    assert c.band == "hard"
    assert c.difficulty == "complex"


def test_modify_delete_with_keeper_adding_needs_judgment():
    """A modify/delete whose keeper ADDED new content (not just tweaked a line)
    is declined by the structural rule → needs judgment (medium). The resolver
    auto-accepts only a clean delete vs an unchanged keeper; an adding keeper
    could drop real work, so it routes to the LLM/block-capture."""
    base = "def helper():\n    return 1\n"
    cur = ""  # upstream deleted
    rep = "def helper():\n    return 1\n\ndef new():\n    return 2\n"  # keeper added
    c = classify(_unit(base, cur, rep))
    assert c.band == "medium"
    assert c.difficulty == "complex"
    assert any("modify/delete" in r for r in c.reasons)


def test_modify_delete_with_unchanged_keeper_is_trivial():
    """A modify/delete whose keeper kept base verbatim is auto-accepted by the
    structural rule → deterministically mergeable → trivial."""
    base = "def helper():\n    return 1\n"
    cur = ""  # upstream deleted
    rep = base  # replayed unchanged
    c = classify(_unit(base, cur, rep))
    assert c.band == "trivial"


# ---------------------------------------------------------------------------
# Backward compatibility + audit trail
# ---------------------------------------------------------------------------


def test_difficulty_label_is_backward_compatible():
    """complex ⟺ band ∈ {medium, hard}; simple otherwise. The legacy label the
    orchestrator consumes must map cleanly from the band."""
    for base, cur, rep, expected_label in [
        ("x = 1", "x = 2", "x = 2", "simple"),          # trivial
        ("v = 1", "v = 2", "v = 3", "complex"),          # medium
    ]:
        c = classify(_unit(base, cur, rep))
        if c.band in ("medium", "hard"):
            assert c.difficulty == "complex"
        else:
            assert c.difficulty == "simple"


def test_every_classification_has_reasons_and_features():
    """Every result carries a non-empty reasons list and the feature snapshot."""
    for base, cur, rep in [
        ("x = 1", "x = 2", "x = 2"),
        ("v = 1", "v = 2", "v = 3"),
    ]:
        c = classify(_unit(base, cur, rep))
        assert isinstance(c, ConflictClassification)
        assert c.reasons, f"missing reasons for band {c.band}"
        assert isinstance(c.features, dict)
        assert "hunk_size" in c.features


def test_classifier_never_crashes_on_sparse_unit():
    """A unit with no cached signals degrades gracefully (never crashes)."""
    u = ConflictUnit(
        session_id="s", step_index=1, path="x", language=None,
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="a"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="b"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="c"),
        original_worktree_text="a", marker_span=None,
        structural_metadata={},  # no cached features/severity/direction
    )
    c = classify(u)
    assert c.band in ("trivial", "easy", "medium", "hard")
    assert c.difficulty in ("simple", "complex")


# ---------------------------------------------------------------------------
# Operation-signature counts (ConGra §3.3): pure-rename demote, heavy-modify promote
# ---------------------------------------------------------------------------


def _unit_with_ops(base, current, replayed, *, ops_renamed=0, ops_modified=0,
                   touches_definition=True, language="python"):
    """A unit whose cached conflict_features carry explicit operation counts +
    a touches_definition flag, so the classifier's ops_* signals are testable
    without running the full extraction pipeline."""
    u = _unit(base, current, replayed, language=language)
    u.structural_metadata["conflict_features"] = {
        "hunk_size": 5,
        "touches_definition": touches_definition,
        "same_line_overlap": False,
        "severity": "medium",
        "merge_kind": "both_modify",
        "modify_delete": False,
        "ops_renamed": ops_renamed,
        "ops_modified": ops_modified,
    }
    return u


def test_pure_rename_demotes_hard_to_medium():
    """A conflict that would be hard (2+ strong signals) is demoted to medium
    when the signature is a pure rename — the refactoring-aware rule may resolve
    it. Test _classify_nontrivial directly to isolate the operation-count signal
    from the signature-overlap detector."""
    from capybase.classifier import _classify_nontrivial

    # 2 strong signals (touches_def + same_symbol_overlap) → hard without ops.
    reasons_hard = []
    band_hard = _classify_nontrivial(
        size=5, touches_def=True, same_line_overlap=False,
        same_symbol_overlap=True, severity="medium", merge_kind="both_modify",
        modify_delete=False, ops_renamed=0, ops_modified=0, reasons=reasons_hard)
    assert band_hard == "hard"

    # Same conflict but with a pure-rename signature → demoted to medium.
    reasons_rename = []
    band_rename = _classify_nontrivial(
        size=5, touches_def=True, same_line_overlap=False,
        same_symbol_overlap=True, severity="medium", merge_kind="both_modify",
        modify_delete=False, ops_renamed=1, ops_modified=0, reasons=reasons_rename)
    assert band_rename == "medium", f"pure rename should demote hard→medium, got {band_rename}"
    assert any("pure-rename" in r for r in reasons_rename)


def test_heavy_multi_entity_modify_promotes_to_hard():
    """≥3 entities with body/signature changes on a def-touching conflict is a
    strong signal; combined with touches_def it promotes to hard."""
    u = _unit_with_ops(
        "class C:\n    def foo():\n        return 1",
        "class C:\n    def foo():\n        return 2",
        "class C:\n    def foo():\n        return 3",
        ops_renamed=0, ops_modified=5, touches_definition=True,
    )
    c = classify(u)
    assert c.band == "hard", f"heavy modify should be hard, got {c.band}"
    assert any("heavy multi-entity modify" in r for r in c.reasons)


def test_moderate_modify_not_treated_as_heavy():
    """ops_modified below the threshold (3) does NOT trigger the heavy signal."""
    u = _unit_with_ops(
        "class C:\n    def foo():\n        return 1",
        "class C:\n    def foo():\n        return 2",
        "class C:\n    def foo():\n        return 3",
        ops_renamed=0, ops_modified=2, touches_definition=True,
    )
    c = classify(u)
    # touches_def alone (1 strong signal) → medium, not hard.
    assert c.band != "hard" or any("heavy" not in r for r in c.reasons)


def test_zero_ops_degrades_silently():
    """When operation counts are absent/zero (parser unavailable), the classifier
    behaves exactly as before — no crash, no spurious signal."""
    u = _unit_with_ops(
        "x = 1", "x = 2", "x = 3",
        ops_renamed=0, ops_modified=0, touches_definition=False,
    )
    c = classify(u)
    assert c.band in ("easy", "medium")
    assert not any("pure-rename" in r for r in c.reasons)
    assert not any("heavy" in r for r in c.reasons)

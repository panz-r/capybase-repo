"""Tests for the conflict feature spine (survey §6.7 routing, §4.2 balance).

P2: ``conflict_features`` flattens a conflict's characteristics into one stable
dict that is recorded at extraction and surfaced into every
``VerificationResult.features``. This is the unified input vector the
calibration flywheel and any future learned router consume — previously these
signals (size, balance, overlap, sibling count) were computed piecemeal and
discarded.
"""

from __future__ import annotations

import math

from capybase.conflict_extractor import conflict_features
from capybase.conflict_model import ConflictSide, ConflictUnit


def _unit(base, current, replayed, **meta):
    u = ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text="", marker_span=None,
    )
    for k, v in meta.items():
        u.structural_metadata[k] = v
    return u


# ---------------------------------------------------------------------------
# conflict_features — individual signals
# ---------------------------------------------------------------------------


def test_hunk_size_counts_nonblank_lines_across_sides():
    u = _unit("a\nb\n", "c\n", "d\ne\nf\n")
    f = conflict_features(u)
    assert f["hunk_size"] == 6  # 2 + 1 + 3 nonblank


def test_hunk_size_ignores_blank_lines():
    u = _unit("\n\n", "\n\n", "\n\n")
    assert conflict_features(u)["hunk_size"] == 0


def test_balance_is_one_for_equal_sides():
    u = _unit("x", "a\nb\n", "c\nd\n")
    assert conflict_features(u)["balance"] == 1.0


def test_balance_below_one_for_unequal_sides():
    u = _unit("x", "a\nb\n", "c\n")  # 2 vs 1
    assert conflict_features(u)["balance"] == 0.5


def test_balance_zero_when_a_side_empty():
    u = _unit("x", "a\n", "")
    assert conflict_features(u)["balance"] == 0.0


def test_imbalance_ratio_is_one_for_balanced():
    u = _unit("x", "a\nb\n", "c\nd\n")
    assert conflict_features(u)["imbalance_ratio"] == 1.0


def test_imbalance_ratio_reflects_dominant_side():
    u = _unit("x", "a\nb\nc\nd\n", "e\n")  # 4 vs 1
    assert conflict_features(u)["imbalance_ratio"] == 4.0


def test_imbalance_ratio_inf_when_a_side_empty():
    u = _unit("x", "a\n", "")
    assert math.isinf(conflict_features(u)["imbalance_ratio"])


def test_touches_definition_from_enclosing_symbol():
    u = _unit("x", "y", "z")
    u.enclosing_symbol = "def greet():"
    assert conflict_features(u)["touches_definition"] is True


def test_touches_definition_from_structural_metadata():
    u = _unit("x", "y", "z")
    u.structural_metadata["enclosing_node_text"] = "def greet(): ..."
    assert conflict_features(u)["touches_definition"] is True


def test_touches_definition_false_when_absent():
    u = _unit("x", "y", "z")
    assert conflict_features(u)["touches_definition"] is False


def test_same_line_overlap_true_when_both_sides_edit_same_base_line():
    base = "def f():\n    return 1\n"
    u = _unit(base, "def f():\n    return 2\n", "def f():\n    return 3\n")
    assert conflict_features(u)["same_line_overlap"] is True


def test_same_line_overlap_false_for_disjoint_edits():
    base = "def f():\n    a = 1\n    b = 2\n"
    u = _unit(base, "def f():\n    a = 9\n    b = 2\n", "def f():\n    a = 1\n    b = 9\n")
    assert conflict_features(u)["same_line_overlap"] is False


def test_sibling_count_from_metadata():
    u = _unit("x", "y", "z")
    u.structural_metadata["sibling_count"] = 3
    assert conflict_features(u)["sibling_count"] == 3


def test_sibling_count_defaults_to_zero():
    u = _unit("x", "y", "z")
    assert conflict_features(u)["sibling_count"] == 0


def test_severity_and_language_recorded():
    u = _unit("x", "y", "z")
    u.severity = "high"
    u.language = "rust"
    f = conflict_features(u)
    assert f["severity"] == "high"
    assert f["language"] == "rust"


def test_language_unknown_when_none():
    u = _unit("x", "y", "z")
    u.language = None
    assert conflict_features(u)["language"] == "unknown"


# ---------------------------------------------------------------------------
# Extraction integration: features recorded on the unit
# ---------------------------------------------------------------------------


def test_extractor_records_conflict_features():
    """The extractor populates structural_metadata['conflict_features']."""
    from capybase.conflict_extractor import ConflictExtractor

    base = "def f():\n    return 1\n"
    current = "def f():\n    return 2\n"
    replayed = "def f():\n    return 3\n"

    class FakeGit:
        def read_stage_blob(self, path, stage):
            return base.encode("utf-8") if stage != 3 else replayed.encode("utf-8")

        def read_worktree_file(self, path):
            return (
                b"def f():\n<<<<<<< H\n    return 2\n=======\n    return 3\n>>>>>>> b\n"
            )

    ex = ConflictExtractor(FakeGit(), structural_config=None)
    units = ex.extract_file_units("app.py", 1, "s")
    assert len(units) == 1
    cf = units[0].structural_metadata.get("conflict_features")
    assert isinstance(cf, dict)
    assert "balance" in cf
    assert "hunk_size" in cf
    assert "same_line_overlap" in cf


# ---------------------------------------------------------------------------
# Verification integration: features surfaced into VerificationResult
# ---------------------------------------------------------------------------


def test_verification_surfaces_conflict_features():
    """VerificationResult.features is seeded with the conflict feature spine."""
    from capybase.verification import ValidationConfig, VerificationEngine

    unit = _unit("def f():\n    return 1\n", "def f():\n    return 2\n", "def f():\n    return 3\n")
    unit.structural_metadata["conflict_features"] = conflict_features(unit)

    cand = _candidate("def f():\n    return 23\n")
    engine = VerificationEngine.default(ValidationConfig())
    result = engine.verify(unit, cand)
    # The spine keys are present on the aggregated features...
    for key in ("balance", "hunk_size", "same_line_overlap", "touches_definition"):
        assert key in result.features, f"missing {key}"
    # ...and the validator's own keys are still there too.
    assert "hard_failure_count" in result.features


def test_verification_works_without_conflict_features():
    """A unit with no recorded features still verifies (backward compatible)."""
    from capybase.verification import ValidationConfig, VerificationEngine

    unit = _unit("x", "y", "z")  # no conflict_features in metadata
    cand = _candidate("z\n")
    engine = VerificationEngine.default(ValidationConfig())
    result = engine.verify(unit, cand)
    assert isinstance(result.features, dict)
    # Validator keys still present even without the spine.
    assert "hard_failure_count" in result.features


def test_conflict_features_take_precedence_on_collision():
    """If a validator emits a key matching the spine, the spine value wins —
    the input vector must stay stable for the calibration flywheel."""
    from capybase.verification import (
        ValidationConfig,
        Validator,
        VerificationCheckResult,
        VerificationContext,
        VerificationEngine,
    )

    class CollisionValidator(Validator):
        name = "collision"

        def verify(self, ctx: VerificationContext) -> VerificationCheckResult:
            return VerificationCheckResult(
                passed=True,
                severity="error",
                name=self.name,
                message="",
                detail={},
                features={"hunk_size": 99999},  # tries to clobber the spine
            )

    unit = _unit("x\n", "y\n", "z\n")
    cf = conflict_features(unit)
    unit.structural_metadata["conflict_features"] = cf
    engine = VerificationEngine([CollisionValidator()], ValidationConfig())
    result = engine.verify(unit, _candidate("z\n"))
    assert result.features["hunk_size"] == cf["hunk_size"]


# ---------------------------------------------------------------------------
# Operation-count features (ConGra §3.3) + entity-diff caching (B1/B2)
# ---------------------------------------------------------------------------


def test_operation_counts_present_in_spine():
    """The five operation-count keys are always present (0 when the parser is
    unavailable). They give the classifier a discriminative operation view."""
    cf = conflict_features(_unit("x = 1\n", "x = 2\n", "x = 3\n"))
    for key in ("ops_added", "ops_removed", "ops_modified", "ops_renamed", "ops_moved"):
        assert key in cf, f"missing operation-count key: {key}"
        assert isinstance(cf[key], int)


def test_operation_counts_detect_added_entity():
    """A side that adds a new function shows ops_added ≥ 1."""
    base = "def f():\n    return 1\n"
    replayed = "def f():\n    return 1\n\ndef g():\n    return 2\n"
    cf = conflict_features(_unit(base, base, replayed))
    assert cf["ops_added"] >= 1  # g() was added


def test_operation_counts_detect_rename():
    """A pure rename (same body, new name, old name gone) shows ops_renamed ≥ 1."""
    base = "def loadData():\n    rows = fetch()\n    return rows\n"
    replayed = "def fetchData():\n    rows = fetch()\n    return rows\n"
    cf = conflict_features(_unit(base, base, replayed))
    assert cf["ops_renamed"] >= 1


def test_entity_diff_cached_not_reparsed(monkeypatch):
    """conflict_features computes each BASE→side diff ONCE and caches it. The
    cache must be hit on a second read so downstream consumers don't re-parse."""
    from capybase.adapters import structural
    from capybase.conflict_extractor import _cached_entity_diff

    calls = {"n": 0}
    real = structural.semantic_diff

    def counting(old, new, lang):
        calls["n"] += 1
        return real(old, new, lang)

    monkeypatch.setattr(structural, "semantic_diff", counting)
    base = "def f():\n    return 1\n"
    replayed = "def f():\n    return 2\n"
    unit = _unit(base, base, replayed)
    # First call computes + caches both diffs.
    _ = conflict_features(unit)
    first = calls["n"]
    # Second read of the cached diff must NOT re-parse.
    _ = _cached_entity_diff(unit, "replayed")
    _ = _cached_entity_diff(unit, "current")
    assert calls["n"] == first, (
        f"cache miss: semantic_diff called {calls['n']} times, expected {first}"
    )
    # The cache is populated on structural_metadata.
    assert "entity_changes" in unit.structural_metadata
    assert "replayed" in unit.structural_metadata["entity_changes"]


def test_cached_entity_diff_survives_parser_unavailable():
    """When the parser is unavailable, the cache stores None so a repeated call
    doesn't re-attempt a failing parse (and the counts degrade to 0)."""
    from capybase.conflict_extractor import _cached_entity_diff

    # An unrecognized language → semantic_diff returns None.
    unit = _unit("x\n", "y\n", "z\n")
    unit.language = None
    unit.structural_metadata.clear()
    changes = _cached_entity_diff(unit, "replayed")
    assert changes is None
    # A repeat read returns the cached None without re-attempting the parse.
    again = _cached_entity_diff(unit, "replayed")
    assert again is None
    # And the counts derived from a None diff are all zero.
    from capybase.conflict_extractor import _count_change
    assert _count_change(None, "added") == 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _candidate(text):
    from capybase.conflict_model import CandidateResolution

    return CandidateResolution(
        candidate_id="c", unit_id="u", model_name="m", prompt_version="v",
        resolved_text=text,
    )

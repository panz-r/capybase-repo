"""Tests for quality scoring: normalization, per-candidate correctness, and the
lexicographic comparator that drives mechanism A/B decisions.

No network — correctness is checked against the blessed corpus, and the
comparator is a pure function. The resolve-side (evaluate_setting) is tested
via the fake-client tests in test_probes.py.
"""

from __future__ import annotations

import pytest

from capybase.calibration_corpus import CALIBRATION_CONFLICTS, CalibrationConflict
from capybase.conflict_model import CandidateResolution, VerificationResult
from capybase.conflict_model import ConflictUnit, ContextBundle
from capybase.quality import (
    ConflictScore,
    SettingScore,
    _is_correct,
    compare_scores,
    evaluate_setting,
    normalize_resolved,
    score_candidate,
)


def _cand(text: str, unit_id: str = "u") -> CandidateResolution:
    return CandidateResolution(
        candidate_id="t", unit_id=unit_id, model_name="m",
        prompt_version="v", resolved_text=text,
    )


def _conflict(expected: str, unit_id: str = "u") -> CalibrationConflict:
    return CalibrationConflict(
        title="t",
        unit=ConflictUnit(
            session_id="s", step_index=0, path="p.py", unit_id=unit_id,
            base=__import__("capybase").conflict_model.ConflictSide(label="BASE", text="b"),
            current=__import__("capybase").conflict_model.ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="c"),
            replayed=__import__("capybase").conflict_model.ConflictSide(label="REPLAYED_COMMIT_SIDE", text="r"),
            original_worktree_text="b",
        ),
        expected_text=expected,
    )


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        # Spacing variance around structural punctuation collapses to ONE form.
        ('["a", "b"]', '["a","b"]'),
        ('["a","b"]', '["a","b"]'),
        ('[ "a",\n "b" ]', '["a","b"]'),
        # Leading/trailing whitespace stripped.
        ('  x = 1\n\n  ', 'x=1'),
    ],
)
def test_normalize_resolved_collapses_whitespace(text, expected):
    assert normalize_resolved(text) == expected


def test_normalize_equal_spacing_variants_match():
    """The whole point: differently-spaced equivalent text compares equal."""
    a = normalize_resolved('SERVICES = ["core", "s", "r"]')
    b = normalize_resolved('SERVICES=["core","s","r"]')
    assert a == b


def test_normalize_preserves_semantic_content():
    # Punctuation CHARS are kept (only their adjacent spaces removed); case and
    # quote content are untouched.
    assert normalize_resolved('X = "Y"') == 'X="Y"'


# ---------------------------------------------------------------------------
# correctness
# ---------------------------------------------------------------------------


def test_is_correct_exact_match():
    assert _is_correct('["core", "s", "r"]', '["core", "s", "r"]')


def test_is_correct_whitespace_variance():
    assert _is_correct('["core","s","r"]', '["core", "s", "r"]')


def test_is_correct_containment_wrapped_merge():
    # The model wraps the merge with surrounding lines; the blessed content is
    # still present verbatim (normalized) → correct.
    assert _is_correct('header\n["core", "s", "r"]\nfooter', '["core", "s", "r"]')


def test_is_correct_rejects_missing_side():
    # Drops the replayed side → wrong.
    assert not _is_correct('["core", "s"]', '["core", "s", "r"]')


def test_is_correct_rejects_picks_one_side():
    # Just the current side, not both → wrong.
    assert not _is_correct('["core", "s"]', '["core", "s", "r"]')


def test_is_correct_empty_expected_is_false():
    assert not _is_correct("anything", "")


# ---------------------------------------------------------------------------
# score_candidate
# ---------------------------------------------------------------------------


def test_score_candidate_correct_no_verification():
    conflict = _conflict('x = 3')
    s = score_candidate(_cand("x = 3"), conflict)
    assert s.correct is True
    assert s.proxy == 0.0  # no verification features


def test_score_candidate_incorrect():
    conflict = _conflict('x = 3')
    s = score_candidate(_cand("x = 2"), conflict)
    assert s.correct is False


def test_score_candidate_uses_verification_proxy():
    conflict = _conflict('x = 3')
    ver = VerificationResult(
        candidate_id="t", unit_id="u", passed=True,
        features={"syntax_passed": True, "ast_preserved": True,
                  "splice_scope_ok": True, "copied_one_side": False},
    )
    s = score_candidate(_cand("x = 3"), conflict, verification=ver)
    assert s.correct
    assert s.proxy == 3.0  # three positive signals


def test_score_candidate_proxy_penalizes_copied_one_side_and_lsp_errors():
    ver = VerificationResult(
        candidate_id="t", unit_id="u", passed=True,
        features={"copied_one_side": True, "lsp_new_error_count": 2},
    )
    s = score_candidate(_cand("x"), _conflict("x"), verification=ver)
    assert s.proxy == -3.0  # -1 copied, -2 lsp errors


# ---------------------------------------------------------------------------
# compare_scores (the A/B decision rule)
# ---------------------------------------------------------------------------


def _ss(correct: int, proxy: float = 0.0, latency: float = 0.0, total: int = 5) -> SettingScore:
    return SettingScore(n_correct=correct, proxy_sum=proxy, mean_latency_ms=latency,
                        per_conflict=[ConflictScore("t", False, 0, 0) for _ in range(total)])


def test_compare_correctness_dominates():
    a = _ss(correct=4)
    b = _ss(correct=2)
    assert compare_scores(a, b) > 0
    assert compare_scores(b, a) < 0


def test_compare_proxy_breaks_correctness_tie():
    a = _ss(correct=3, proxy=2.0)
    b = _ss(correct=3, proxy=-1.0)
    assert compare_scores(a, b) > 0


def test_compare_latency_breaks_proxy_tie():
    a = _ss(correct=3, proxy=1.0, latency=100)
    b = _ss(correct=3, proxy=1.0, latency=200)
    assert compare_scores(a, b) > 0  # lower latency wins


def test_compare_equal_scores_returns_zero():
    a = _ss(correct=3, proxy=1.0, latency=100)
    b = _ss(correct=3, proxy=1.0, latency=100)
    assert compare_scores(a, b) == 0


def test_compare_used_with_max():
    """``max(..., key=cmp_to_key(compare_scores))`` must pick the most-correct."""
    from functools import cmp_to_key

    scores = [_ss(correct=1), _ss(correct=5), _ss(correct=3)]
    best = max(scores, key=cmp_to_key(compare_scores))
    assert best.n_correct == 5


# ---------------------------------------------------------------------------
# evaluate_setting (integration with the corpus + an injectable resolver)
# ---------------------------------------------------------------------------


def test_evaluate_setting_scores_a_perfect_resolver():
    """A resolver that returns the blessed text for each conflict scores full."""
    def resolve_one(conflict, context, model_cfg):
        return _cand(conflict.expected_text, conflict.unit.unit_id), None, 50.0

    from capybase.config import ModelConfig
    score = evaluate_setting(resolve_one, ModelConfig())
    assert score.n_correct == score.total == len(CALIBRATION_CONFLICTS)
    assert score.mean_latency_ms == 50.0


def test_evaluate_setting_counts_misses():
    def resolve_one(conflict, context, model_cfg):
        # Always returns the wrong answer.
        return _cand("WRONG", conflict.unit.unit_id), None, 10.0

    from capybase.config import ModelConfig
    score = evaluate_setting(resolve_one, ModelConfig())
    assert score.n_correct == 0


def test_evaluate_setting_treats_exception_as_a_miss():
    def resolve_one(conflict, context, model_cfg):
        raise RuntimeError("boom")

    from capybase.config import ModelConfig
    score = evaluate_setting(resolve_one, ModelConfig())
    assert score.n_correct == 0
    assert all("error" in s.detail for s in score.per_conflict)

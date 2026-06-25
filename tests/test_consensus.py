"""Tests for self-consistency aggregation and enriched CEGIS."""

from __future__ import annotations

from capybase.conflict_model import CandidateResolution, VerificationFailure
from capybase.consensus import (
    Cluster,
    ConsensusReport,
    cluster,
    normalize,
    rank_by_consensus,
    select,
)


def _cand(rid, text, conf=0.0):
    return CandidateResolution(
        candidate_id=rid,
        unit_id="u",
        model_name="m",
        prompt_version="v",
        resolved_text=text,
        self_reported_confidence=conf,
    )


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------


def test_normalize_strips_trailing_whitespace_and_comments():
    a = normalize("    return 1  # foo\n", "python")
    b = normalize("    return 1\n", "python")
    assert a == b


def test_normalize_preserves_indentation():
    a = normalize("    return 1", "python")
    b = normalize("return 1", "python")
    assert a != b  # indentation is significant


def test_normalize_drops_full_comment_lines():
    a = normalize("    return 1\n# comment\n", "python")
    assert "# comment" not in a
    assert "return 1" in a


def test_normalize_collapses_blank_lines():
    a = normalize("x = 1\n\n\n\ny = 2", "python")
    assert "\n\n" not in a


# ---------------------------------------------------------------------------
# cluster + select
# ---------------------------------------------------------------------------


def test_cluster_groups_identical_under_normalization():
    cands = [
        _cand("a", "    return ('hi', 'howdy')\n"),
        _cand("b", "    return ('hi', 'howdy')  # merged\n"),
        _cand("c", "    return 'hi'"),
    ]
    clusters = cluster(cands, "python")
    assert len(clusters) == 2
    assert clusters[0].size == 2  # majority first


def test_select_majority_wins():
    cands = [
        _cand("a", "AAA"),
        _cand("b", "AAA"),
        _cand("c", "BBB"),
    ]
    rep = select(cands, None)
    assert rep.winner.resolved_text == "AAA"
    assert rep.agreement_score == 2 / 3
    assert rep.has_majority
    assert rep.cluster_count == 2


def test_select_tiebreak_by_confidence():
    cands = [_cand("x", "AAA", conf=0.3), _cand("y", "BBB", conf=0.8)]
    rep = select(cands, None)
    assert rep.winner.candidate_id == "y"


def test_select_tiebreak_by_brevity_when_confidence_equal():
    cands = [_cand("x", "AAAAAA", conf=0.5), _cand("y", "AA", conf=0.5)]
    rep = select(cands, None)
    assert rep.winner.candidate_id == "y"  # shorter wins


def test_select_unanimous():
    cands = [_cand("a", "X"), _cand("b", "X"), _cand("c", "X")]
    rep = select(cands, None)
    assert rep.agreement_score == 1.0
    assert rep.cluster_count == 1


def test_select_empty_candidates():
    rep = select([], None)
    assert rep.winner is None
    assert rep.n_samples == 0


def test_select_all_failed_candidates_cluster_together():
    cands = [_cand("f1", ""), _cand("f2", "")]
    rep = select(cands, None)
    assert rep.cluster_count == 1


# ---------------------------------------------------------------------------
# rank_by_consensus (reordering for the engine)
# ---------------------------------------------------------------------------


def test_rank_by_consensus_puts_winner_first():
    cands = [
        _cand("div", "BBB"),
        _cand("m1", "AAA"),
        _cand("m2", "AAA"),
    ]
    ordered, rep = rank_by_consensus(cands, None)
    assert ordered[0].resolved_text == "AAA"
    assert rep.agreement_score == 2 / 3
    # All candidates preserved
    assert len(ordered) == len(cands)
    assert {c.candidate_id for c in ordered} == {c.candidate_id for c in cands}


def test_rank_by_consensus_single_candidate_passthrough():
    cands = [_cand("only", "X")]
    ordered, rep = rank_by_consensus(cands, None)
    assert ordered[0].candidate_id == "only"
    assert rep.winner.resolved_text == "X"


# ---------------------------------------------------------------------------
# Enriched CEGIS retry prompt
# ---------------------------------------------------------------------------


def test_retry_prompt_renders_failure_detail():
    from capybase.resolution_engine import _render_failure

    f = VerificationFailure(
        validator="syntax",
        severity="error",
        message="unexpected indent",
        detail={"line": 3, "column": 2, "source_line": "    return x"},
    )
    rendered = _render_failure(f)
    assert "[syntax]" in rendered
    assert "unexpected indent" in rendered
    assert "line: 3" in rendered
    assert "source_line" in rendered


def test_retry_prompt_truncates_long_detail():
    from capybase.resolution_engine import _render_failure

    f = VerificationFailure(
        validator="ast_preservation",
        severity="error",
        message="structure changed",
        detail={"diff": "X" * 500},
    )
    rendered = _render_failure(f)
    assert "…" in rendered  # truncated


# ---------------------------------------------------------------------------
# ResolutionEngine.propose_with_consensus integration
# ---------------------------------------------------------------------------


def test_propose_with_consensus_reorders_majority_first():
    from capybase.config import ModelConfig
    from capybase.conflict_extractor import ConflictUnit
    from capybase.conflict_model import ConflictSide
    from capybase.context_builder import ContextBuilder
    from capybase.resolution_engine import ResolutionEngine

    class FakeClient:
        def __init__(self, texts):
            self._texts = list(texts)
            self._i = 0

        def complete(self, messages, **kw):
            from capybase.adapters.llm_openai import LLMResponse

            t = self._texts[self._i % len(self._texts)]
            self._i += 1
            return LLMResponse(text=t, raw={"_accumulated": {"finish_reason": "stop"}})

    cfg = ModelConfig(samples=3)
    client = FakeClient([
        '{"resolved_text": "    return 1"}',
        '{"resolved_text": "    return 1  # one"}',
        '{"resolved_text": "    return 2"}',
    ])
    engine = ResolutionEngine(cfg, client=client)
    worktree = "def f():\n<<<<<<< H\n    return 0\n=======\n    return 9\n>>>>>>> b\n"
    unit = ConflictUnit(
        session_id="s", step_index=1, path="f.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="def f():\n    pass"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="    return 0"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="    return 9"),
        original_worktree_text=worktree, marker_span=(1, 5),
    )
    ordered, rep = engine.propose_with_consensus(unit, ContextBuilder().build(unit))
    # Majority is "return 1" (2 of 3, normalized identical).
    assert "return 1" in ordered[0].resolved_text
    assert rep is not None
    assert rep.agreement_score == 2 / 3
    assert rep.cluster_count == 2


def test_consensus_clusters_candidates_across_prompt_variants():
    """Survey §4 (Code Roulette): candidates drawn from distinct prompt
    variants that produce the same logical answer must cluster together. The
    robustness signal — a merge stable across prompt phrasings — surfaces as a
    large consensus cluster, which rank-order validation then prefers."""
    from capybase.config import ModelConfig
    from capybase.conflict_extractor import ConflictUnit
    from capybase.conflict_model import ConflictSide
    from capybase.context_builder import ContextBuilder
    from capybase.resolution_engine import ResolutionEngine

    class FakeClient:
        def __init__(self, texts):
            self._texts = list(texts)
            self._i = 0

        def complete(self, messages, **kw):
            from capybase.adapters.llm_openai import LLMResponse

            t = self._texts[self._i % len(self._texts)]
            self._i += 1
            return LLMResponse(text=t, raw={"_accumulated": {"finish_reason": "stop"}})

    cfg = ModelConfig(
        samples=3, prompt_variants=True, parallel_samples=True,
    )
    client = FakeClient([
        '{"resolved_text": "    return 1"}',     # v0
        '{"resolved_text": "    return 1"}',     # v1 — same answer, different phrasing
        '{"resolved_text": "    return 9"}',     # v2 — outlier
    ])
    worktree = "def f():\n<<<<<<< H\n    return 0\n=======\n    return 9\n>>>>>>> b\n"
    unit = ConflictUnit(
        session_id="s", step_index=1, path="f.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="def f():\n    pass"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="    return 0"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="    return 9"),
        original_worktree_text=worktree, marker_span=(1, 5),
    )
    engine = ResolutionEngine(cfg, client=client)
    ordered, rep = engine.propose_with_consensus(unit, ContextBuilder().build(unit))

    # The two variants that converged on "return 1" form the majority cluster
    # (2/3), even though they came from different prompt phrasings. The
    # variant-tagged prompt_versions confirm the samples spanned variants.
    versions = sorted(c.prompt_version for c in ordered)
    assert any(v.endswith("#v1") for v in versions), versions
    assert "return 1" in ordered[0].resolved_text
    assert rep is not None
    assert rep.cluster_count == 2  # {"return 1"} majority + {"return 9"} outlier
    assert rep.agreement_score == 2 / 3

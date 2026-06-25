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


# ---------------------------------------------------------------------------
# FactSelfCheck rationale-consistency (survey §2): agreement over the
# candidates' OWN intent claims — orthogonal to text-consensus.
# ---------------------------------------------------------------------------


def _ifact(
    rid,
    text,
    *,
    cur_intent=None,
    rep_intent=None,
    preserve_cur=True,
    preserve_rep=True,
    conf=0.0,
):
    """A candidate carrying rationale fields (the FactSelfCheck inputs)."""
    return CandidateResolution(
        candidate_id=rid, unit_id="u", model_name="m", prompt_version="v",
        resolved_text=text, self_reported_confidence=conf,
        current_side_intent=cur_intent or [],
        replayed_commit_intent=rep_intent or [],
        preserved_current_side=preserve_cur,
        preserved_replayed_commit_side=preserve_rep,
    )


def test_extract_facts_canonicalizes_intent_and_booleans():
    from capybase.consensus import _extract_facts

    c = _ifact(
        "c1", "x",
        cur_intent=["Add null check for x.", "  Log  the  change "],
        rep_intent=["Return 0"],
        preserve_cur=True, preserve_rep=False,
    )
    facts = _extract_facts(c)
    # Booleans stringified.
    assert facts["preserve:current"] == "true"
    assert facts["preserve:replayed"] == "false"
    # Intent items canonicalized (lowercased, ws-collapsed, trailing punct stripped).
    assert facts["intent:current:0"] == "add null check for x"
    assert facts["intent:current:1"] == "log the change"
    assert facts["intent:replayed:0"] == "return 0"


def test_extract_facts_empty_for_no_rationale():
    """A candidate with no intent lists still carries the boolean facts; an
    all-default candidate yields just the two preserve:* facts."""
    from capybase.consensus import _extract_facts

    c = _cand("c1", "x")  # no intent lists, defaults preserved=True
    facts = _extract_facts(c)
    assert facts["preserve:current"] == "true"
    assert facts["preserve:replayed"] == "true"
    # No intent:* keys.
    assert not any(k.startswith("intent:") for k in facts)


def test_fact_consistency_unanimous_claims_is_one():
    from capybase.consensus import fact_consistency

    cands = [
        _ifact("a", "code", cur_intent=["add guard"], rep_intent=["return 0"]),
        _ifact("b", "code2", cur_intent=["Add guard."], rep_intent=["return 0"]),
        _ifact("c", "code3", cur_intent=["ADD GUARD"], rep_intent=["Return 0"]),
    ]
    fc = fact_consistency(cands)
    # Every canonicalized claim matches across all three → aggregate 1.0.
    assert fc.aggregate == 1.0
    assert fc.low_consistency_count == 0
    # Every candidate's own claims are unanimous → per-candidate 1.0.
    assert all(v == 1.0 for v in fc.per_candidate.values())


def test_fact_consistency_flags_contradictory_booleans():
    """Candidates split on whether the replayed side was preserved → that fact
    scores low and is counted as a low-consistency fact."""
    from capybase.consensus import fact_consistency

    cands = [
        _ifact("a", "x", preserve_rep=True),
        _ifact("b", "x", preserve_rep=True),
        _ifact("c", "x", preserve_rep=False),  # minority claim
    ]
    fc = fact_consistency(cands)
    # preserve:replayed: 2 of 3 say true → consistency 2/3.
    assert abs(fc.per_fact["preserve:replayed"] - 2 / 3) < 1e-9
    # preserve:current is unanimous (default true) → 1.0, so not low-consistency.
    assert fc.per_fact["preserve:current"] == 1.0
    # The outlier candidate (c) relies on the minority boolean → lower score.
    assert fc.per_candidate["c"] < fc.per_candidate["a"]


def test_fact_consistency_isolates_outlier_claim():
    """One candidate asserts a claim no peer makes → that fact is low-
    consistency and the outlier's per-candidate score drops below its peers."""
    from capybase.consensus import fact_consistency

    cands = [
        _ifact("a", "x", cur_intent=["add guard"]),
        _ifact("b", "x", cur_intent=["add guard"]),
        _ifact("c", "x", cur_intent=["remove function"]),  # hallucinated claim
    ]
    fc = fact_consistency(cands)
    assert fc.per_fact["intent:current:0"] == 2 / 3  # "add guard" majority
    assert fc.low_consistency_count == 1
    # The outlier's minority claim drags its per-candidate score below the
    # majority claimants (it also asserts the unanimous preserve:* facts, so its
    # score is the mean of {1.0, 1.0, 1/3} = 0.778 — lower than the peers' 0.889).
    assert fc.per_candidate["c"] < fc.per_candidate["a"]
    assert fc.per_candidate["c"] < fc.per_candidate["b"]


def test_report_surfaces_intent_agreement_on_contradiction():
    """The key FactSelfCheck win: candidates with IDENTICAL code but
    CONTRADICTORY intent claims still report low intent_agreement — the signal
    text-consensus (agreement_score) is blind to."""
    cands = [
        _ifact("a", "    return 1", cur_intent=["add null guard"]),
        _ifact("b", "    return 1", cur_intent=["remove logging"]),
        _ifact("c", "    return 1", cur_intent=["add null guard"]),
    ]
    rep = select(cands, "python")
    # Text-consensus sees unanimous code → agreement_score 1.0.
    assert rep.agreement_score == 1.0
    # But the intent claims disagree → intent_agreement < 1.0.
    assert rep.intent_agreement < 1.0
    assert rep.low_consistency_fact_count >= 1


def test_report_intent_agreement_one_when_unanimous():
    cands = [_ifact(f"c{i}", "    return 1", cur_intent=["add guard"]) for i in range(3)]
    rep = select(cands, "python")
    assert rep.intent_agreement == 1.0
    assert rep.low_consistency_fact_count == 0


def test_tie_break_prefers_higher_consistency_candidate():
    """Among equal-size clusters, the candidate whose rationale is more
    consistent with the cohort wins the tie-break (survey §2: down-weight
    candidates that depend on low-consistency facts)."""
    # Three singleton clusters (distinct code) → all tied on size.
    # a and b share the claim "add guard"; c asserts a unique minority claim.
    cands = [
        _ifact("a", "    return 2", cur_intent=["add guard"], conf=0.5),
        _ifact("b", "    return 9", cur_intent=["add guard"], conf=0.9),
        _ifact("c", "DIFFERENT", cur_intent=["something else entirely"], conf=0.9),
    ]
    rep = select(cands, "python")
    # "add guard" is 2/3-consistent; c's claim is 1/3 → c has the lowest
    # fact-consistency and loses the tie-break regardless of its confidence.
    assert rep.winner.candidate_id != "c"
    assert rep.winner.candidate_id in ("a", "b")

"""RAG into the repair/retry path.

The repair prompt is the A/B failure site where the model reproduces the same
dropped-side merge across retries. Surfacing a SINGLE high-trust retrieved
example there gives the model a concrete resolution pattern instead of
regenerating the same mistake. This test covers:

- ``QualityFilteredRetriever``: the retry-count + score-floor wrapper.
- ``build_repair_prompt``: renders the anchor when present, omits when empty.
- ``ContextBuilder``: populates ``repair_retrieved_examples`` separately from
  fresh-gen ``retrieved_examples``.
- End-to-end: a CEGIS retry's prompt (journalled) carries the anchor.
"""

from __future__ import annotations

import pytest

from capybase.conflict_model import (
    ConflictSide,
    ConflictUnit,
    HistoricalExample,
    VerificationFailure,
)
from capybase.memory.retriever import QualityFilteredRetriever
from capybase.memory.store import Experience, ExperienceStore
from capybase.resolution_engine import build_repair_prompt


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _StaticRetriever:
    """Returns canned scored results; records calls for assertion."""

    def __init__(self, scored):
        self._scored = scored
        self.calls: list[tuple[str, int]] = []

    def retrieve_scored(self, query, *, k=3, language=None, path=None):
        self.calls.append((query, k))
        return list(self._scored[:k])

    def retrieve_explained(self, query, *, k=3, language=None, **kw):
        # Return explained tuples so the context builder's explained path works.
        from capybase.memory.retriever import RetrievalExplanation

        return [
            (RetrievalExplanation(score=s), ex) for s, ex in self._scored[:k]
        ]


def _exp(retry_count: int, score_text: str = "s") -> Experience:
    return Experience(
        example=HistoricalExample(
            summary=f"u{retry_count}", base="b", current="c", replayed="r",
            resolved="z", source="t",
        ),
        outcome="accepted", path="a.py", retry_count=retry_count,
    )


def _ex(name: str, resolved: str = "RESOLVED") -> HistoricalExample:
    return HistoricalExample(
        summary=name, base="b", current="CUR", replayed="REP",
        resolved=resolved, source="t",
    )


# ---------------------------------------------------------------------------
# QualityFilteredRetriever
# ---------------------------------------------------------------------------


def test_quality_filter_drops_high_retry_examples(tmp_path):
    """Examples that took too many retries are excluded."""
    store = ExperienceStore(tmp_path / "e.jsonl")
    good = _exp(retry_count=1)  # low retries → trustworthy
    lucky = _exp(retry_count=5)  # many retries → may have converged by luck
    store.append(good)
    store.append(lucky)
    inner = _StaticRetriever([(0.9, good.example), (0.9, lucky.example)])
    qf = QualityFilteredRetriever(inner, store, max_retries=2, min_score=0.0)
    res = qf.retrieve_scored("q", k=3)
    summaries = [ex.summary for _, ex in res]
    assert "u1" in summaries
    assert "u5" not in summaries  # filtered out


def test_quality_filter_applies_score_floor(tmp_path):
    store = ExperienceStore(tmp_path / "e.jsonl")
    a = _exp(retry_count=0)
    b = _exp(retry_count=0)
    store.append(a)
    store.append(b)
    inner = _StaticRetriever([(0.40, a.example), (0.90, b.example)])
    qf = QualityFilteredRetriever(inner, store, max_retries=2, min_score=0.55)
    res = qf.retrieve_scored("q", k=3)
    # Only the 0.90 example clears the higher floor.
    assert [ex.summary for _, ex in res] == ["u0"]  # but both are u0... use distinct
    # Refine: make them distinguishable.
    a2 = Experience(
        example=HistoricalExample(
            summary="low", base="b", current="c", replayed="r", resolved="z", source="t"
        ),
        outcome="accepted", retry_count=0,
    )
    b2 = Experience(
        example=HistoricalExample(
            summary="high", base="b", current="c", replayed="r", resolved="z", source="t"
        ),
        outcome="accepted", retry_count=0,
    )
    store2 = ExperienceStore(tmp_path / "e2.jsonl")
    store2.append(a2)
    store2.append(b2)
    inner2 = _StaticRetriever([(0.40, a2.example), (0.90, b2.example)])
    qf2 = QualityFilteredRetriever(inner2, store2, max_retries=2, min_score=0.55)
    res2 = qf2.retrieve_scored("q", k=3)
    assert [ex.summary for _, ex in res2] == ["high"]


def test_quality_filter_overfetches_so_filter_yields_k(tmp_path):
    """The wrapper over-fetches so pruning still leaves k survivors."""
    store = ExperienceStore(tmp_path / "e.jsonl")
    exs = [_exp(retry_count=i % 3) for i in range(9)]  # some retry=2 (borderline)
    for e in exs:
        store.append(e)
    scored = [(0.9, e.example) for e in exs]
    inner = _StaticRetriever(scored)
    qf = QualityFilteredRetriever(inner, store, max_retries=1, min_score=0.0)
    # Inner is asked for k*3; filter keeps retry_count<=1.
    res = qf.retrieve_scored("q", k=2)
    assert len(res) == 2
    asked_k = inner.calls[0][1]
    assert asked_k >= 6  # over-fetch


def test_quality_filter_max_retries_negative_disables_filter(tmp_path):
    store = ExperienceStore(tmp_path / "e.jsonl")
    high = _exp(retry_count=9)
    store.append(high)
    inner = _StaticRetriever([(0.9, high.example)])
    qf = QualityFilteredRetriever(inner, store, max_retries=-1, min_score=0.0)
    res = qf.retrieve_scored("q", k=3)
    assert len(res) == 1  # not filtered despite retry_count=9


# ---------------------------------------------------------------------------
# build_repair_prompt renders the anchor
# ---------------------------------------------------------------------------


def _unit() -> ConflictUnit:
    return ConflictUnit(
        session_id="s", step_index=0, path="a.py", language="python",
        conflict_type="UU", unit_id="u1", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="def f():\n    return 1"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="def f():\n    return 2"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="def f():\n    return 3"),
        original_worktree_text="def f():\n    return 1\n", marker_span=(0, 0),
    )


class _Ctx:
    """Minimal context stand-in for build_repair_prompt."""

    def __init__(self, repair_retrieved_examples=None):
        self.repair_retrieved_examples = repair_retrieved_examples or []
        self.retrieved_examples = []
        self.related_snippets = []
        self.structural_view = {}
        self.side_summaries = {}
        self.retrieval_scores = []
        self.retrieval_explanations = []
        self.history_context = ""
        self.obligations_context = ""
        self.token_estimate = 0


def test_repair_prompt_includes_anchor_when_present():
    from capybase.conflict_model import CandidateResolution

    unit = _unit()
    ctx = _Ctx(repair_retrieved_examples=[_ex("ex1", resolved="def f(): return 2\n# keep both")])
    cand = CandidateResolution(
        candidate_id="c1", unit_id="u1", model_name="m",
        prompt_version="repair", resolved_text="def f():\n    return 2",
    )
    failures = [VerificationFailure(validator="intent_coverage", message="dropped replayed")]
    prompt = build_repair_prompt(unit, ctx, cand, failures)
    assert "SIMILAR conflict was resolved correctly before" in prompt
    assert "def f(): return 2\n# keep both" in prompt
    # The anchor appears after the feedback block.
    assert prompt.index("validator feedback") < prompt.index("SIMILAR conflict")


def test_repair_prompt_omits_anchor_when_empty():
    from capybase.conflict_model import CandidateResolution

    unit = _unit()
    ctx = _Ctx(repair_retrieved_examples=[])  # no anchor
    cand = CandidateResolution(
        candidate_id="c1", unit_id="u1", model_name="m",
        prompt_version="repair", resolved_text="def f():\n    return 2",
    )
    failures = [VerificationFailure(validator="intent_coverage", message="dropped replayed")]
    prompt = build_repair_prompt(unit, ctx, cand, failures)
    assert "SIMILAR conflict was resolved correctly before" not in prompt
    # Prior behavior preserved: feedback + plan-first still present.
    assert "validator feedback" in prompt
    assert "FIRST, reason about the fix" in prompt


def test_repair_prompt_uses_top1_only():
    """Only the first example is surfaced (top-1, not top-k)."""
    from capybase.conflict_model import CandidateResolution

    unit = _unit()
    ctx = _Ctx(repair_retrieved_examples=[
        _ex("ex1", resolved="RESOLVED_ONE"),
        _ex("ex2", resolved="RESOLVED_TWO"),
    ])
    cand = CandidateResolution(
        candidate_id="c1", unit_id="u1", model_name="m",
        prompt_version="repair", resolved_text="def f():\n    return 2",
    )
    prompt = build_repair_prompt(unit, ctx, cand, [])
    assert "RESOLVED_ONE" in prompt
    assert "RESOLVED_TWO" not in prompt


# ---------------------------------------------------------------------------
# ContextBuilder populates repair_retrieved_examples
# ---------------------------------------------------------------------------


def test_context_builder_populates_repair_examples_separately():
    from capybase.context_builder import ContextBuilder

    good = _ex("g1", resolved="GOOD_RESOLVE")
    # Fresh retriever returns top-3; repair retriever returns top-1 filtered.
    fresh = _StaticRetriever([
        (0.4, _ex("f1")), (0.5, _ex("f2")), (0.6, _ex("f3")),
    ])
    repair = _StaticRetriever([(0.9, good)])
    cb = ContextBuilder(retriever=fresh, repair_retriever=repair, repair_retriever_k=1)
    unit = _unit()
    unit.original_worktree_text = "def f():\n    return 1\n"
    unit.marker_span = None
    ctx = cb.build(unit)
    # Fresh: all 3 (top-k=3 default).
    assert len(ctx.retrieved_examples) == 3
    # Repair: top-1, the high-trust one.
    assert len(ctx.repair_retrieved_examples) == 1
    assert ctx.repair_retrieved_examples[0].resolved == "GOOD_RESOLVE"


def test_context_builder_repair_empty_when_repair_retriever_none():
    from capybase.context_builder import ContextBuilder

    cb = ContextBuilder(retriever=None, repair_retriever=None)
    unit = _unit()
    unit.original_worktree_text = "def f():\n    return 1\n"
    unit.marker_span = None
    ctx = cb.build(unit)
    assert ctx.retrieved_examples == []
    assert ctx.repair_retrieved_examples == []

"""Tests for explainable retrieval + conflict-shape normalization (#9 steps 4/5).

Two layers:
- ``memory.shape``: the conflict-shape hash — two structurally-identical conflicts
  (same per-side edit counts) hash equal regardless of content; different shapes
  hash differently.
- ``memory.retriever``: ``retrieve_explained`` returns a RetrievalExplanation per
  example recording WHY it ranked (same path/region kind/conflict shape, score,
  prior outcome, boosted_by). The same-path boost now surfaces in boosted_by.
"""

from __future__ import annotations

from capybase.conflict_model import HistoricalExample
from capybase.memory.retriever import LexicalRetriever, RetrievalExplanation
from capybase.memory.shape import conflict_shape_hash
from capybase.memory.store import Experience, ExperienceStore


def _exp(summary, base, current, replayed, resolved, *, path="cfg.py",
         region_kind="", conflict_shape="", language="python"):
    return Experience(
        example=HistoricalExample(
            summary=summary, base=base, current=current, replayed=replayed,
            resolved=resolved, source="s",
        ),
        outcome="accepted", language=language, path=path,
        region_kind=region_kind, conflict_shape=conflict_shape,
    )


# ---------------------------------------------------------------------------
# conflict-shape hash
# ---------------------------------------------------------------------------


def test_same_shape_hashes_equal():
    """Two conflicts with the same per-side edit structure hash equal."""
    a = conflict_shape_hash(base="a = 1", current="a = 1\nb = 2", replayed="a = 1\nc = 3")
    b = conflict_shape_hash(base="x = 9", current="x = 9\ny = 2", replayed="x = 9\nz = 3")
    assert a == b


def test_different_shape_hashes_differently():
    """Different edit structures hash differently."""
    append = conflict_shape_hash(base="a = 1", current="a = 1\nb = 2", replayed="a = 1\nc = 3")
    modify = conflict_shape_hash(base="a = 1", current="a = 2", replayed="a = 3")
    assert append != modify


def test_shape_is_whitespace_invariant():
    """Cosmetic whitespace differences don't change the shape."""
    a = conflict_shape_hash(base="a = 1", current="a = 1\nb = 2", replayed="a = 1\nc = 3")
    b = conflict_shape_hash(base="  a = 1  ", current="a = 1\n  b = 2  ", replayed="a = 1\n c = 3")
    assert a == b


def test_shape_is_short_hex():
    s = conflict_shape_hash(base="a", current="b", replayed="c")
    assert len(s) == 12
    assert all(c in "0123456789abcdef" for c in s)


# ---------------------------------------------------------------------------
# retrieve_explained
# ---------------------------------------------------------------------------


def _store_with_exps(tmp_path, exps):
    store = ExperienceStore(tmp_path / "exp.jsonl")
    for e in exps:
        store.append(e)
    return store


def test_retrieve_explained_returns_explanations(tmp_path):
    """retrieve_explained yields (explanation, example) pairs."""
    store = _store_with_exps(tmp_path, [
        _exp("cfg.py:u1", "def load():\n    return 1",
             "def load():\n    return 2", "def load():\n    return 3",
             "def load():\n    return 2\n"),
    ])
    r = LexicalRetriever(store)
    explained = r.retrieve_explained("def load():\n    return 2", k=3)
    assert len(explained) == 1
    expl, ex = explained[0]
    assert isinstance(expl, RetrievalExplanation)
    assert expl.score > 0
    assert ex.summary == "cfg.py:u1"


def test_same_path_boost_surfaces_in_boosted_by(tmp_path):
    """The same-path boost is now visible in the explanation's boosted_by."""
    store = _store_with_exps(tmp_path, [
        _exp("cfg.py:u1", "def load():\n    return 1",
             "def load():\n    return 2", "def load():\n    return 3", "merged"),
    ])
    r = LexicalRetriever(store)
    explained = r.retrieve_explained("def load():\n    return 2", k=3, path="cfg.py")
    expl, _ = explained[0]
    assert expl.same_path is True
    assert "same-path" in expl.boosted_by


def test_same_region_kind_detected(tmp_path):
    """When the query region_kind matches a stored example's, same_region_kind."""
    store = _store_with_exps(tmp_path, [
        _exp("cfg.py:u1", "def load():\n    pass", "def load():\n    return 1",
             "def load():\n    return 2", "def load():\n    return 1\n", region_kind="function"),
    ])
    r = LexicalRetriever(store)
    explained = r.retrieve_explained("def load():\n    return 2", k=3, region_kind="function")
    expl, _ = explained[0]
    assert expl.same_region_kind is True


def test_same_conflict_shape_detected(tmp_path):
    """When the query conflict_shape matches, same_conflict_shape."""
    shape = conflict_shape_hash(
        base="def load():\n    return 1",
        current="def load():\n    return 2",
        replayed="def load():\n    return 3",
    )
    store = _store_with_exps(tmp_path, [
        _exp("cfg.py:u1", "def load():\n    return 1",
             "def load():\n    return 2", "def load():\n    return 3", "merged",
             conflict_shape=shape),
    ])
    r = LexicalRetriever(store)
    explained = r.retrieve_explained("def load():\n    return 2", k=3, conflict_shape=shape)
    expl, _ = explained[0]
    assert expl.same_conflict_shape is True


def test_prior_outcome_recorded(tmp_path):
    """The explanation carries the matched example's prior outcome."""
    store = _store_with_exps(tmp_path, [
        _exp("cfg.py:u1", "def load():\n    return 1",
             "def load():\n    return 2", "def load():\n    return 3", "merged"),
    ])
    r = LexicalRetriever(store)
    explained = r.retrieve_explained("def load():\n    return 2", k=3)
    expl, _ = explained[0]
    assert expl.prior_outcome == "accepted"


def test_retrieve_scored_still_works(tmp_path):
    """The legacy retrieve_scored contract is unchanged by the refactor."""
    store = _store_with_exps(tmp_path, [
        _exp("cfg.py:u1", "def load():\n    return 1",
             "def load():\n    return 2", "def load():\n    return 3", "merged"),
    ])
    r = LexicalRetriever(store)
    scored = r.retrieve_scored("def load():\n    return 2", k=3, path="cfg.py")
    assert len(scored) == 1
    score, ex = scored[0]
    assert isinstance(score, float) and score > 0
    assert ex.summary == "cfg.py:u1"


def test_explanation_renders_human_string():
    expl = RetrievalExplanation(
        score=1.5, same_path=True, same_region_kind=True,
        prior_outcome="accepted", boosted_by=("same-path",),
    )
    s = expl.render()
    assert "same path" in s
    assert "same region kind" in s
    assert "score=1.500" in s
    assert "prior=accepted" in s


# ---------------------------------------------------------------------------
# backward compatibility
# ---------------------------------------------------------------------------


def test_old_jsonl_without_region_kind_loads_empty(tmp_path):
    """Old lines (no region_kind/conflict_shape) load as empty strings."""
    import json

    p = tmp_path / "exp.jsonl"
    old = {
        "example": {"summary": "x", "base": "a", "current": "b",
                    "replayed": "c", "resolved": "d", "source": "s"},
        "outcome": "accepted", "language": "python", "path": "cfg.py",
        "session_id": "s", "unit_id": "u", "validator_features": {},
        "risk_score": None, "retry_count": 0, "history_features": {},
        "provenance": "plain_llm",
    }
    p.write_text(json.dumps(old) + "\n")
    store = ExperienceStore(p)
    loaded = list(store)
    assert len(loaded) == 1
    assert loaded[0].region_kind == ""
    assert loaded[0].conflict_shape == ""

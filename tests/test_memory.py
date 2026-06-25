"""Tests for the memory flywheel: experience store, retrieval, and integration.

These exercise the RAG seam: storing labeled outcomes, retrieving similar past
merges by BM25, injecting them into the context bundle, and rendering them as
few-shot in the prompt. The store is pure JSONL (no database), so tests use
temp files.
"""

from __future__ import annotations

import tempfile

import pytest

from capybase.conflict_model import (
    CandidateResolution,
    ConflictSide,
    ConflictUnit,
    HistoricalExample,
)
from capybase.context_builder import ContextBuilder
from capybase.memory.retriever import LexicalRetriever, tokenize
from capybase.memory.store import Experience, ExperienceStore
from capybase.resolution_engine import build_resolve_prompt


# ---------------------------------------------------------------------------
# ExperienceStore
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    return ExperienceStore(tmp_path / "exp.jsonl")


def _exp(summary, outcome="accepted", language="python", resolved="ok"):
    return Experience(
        example=HistoricalExample(
            summary=summary,
            base="greet return hi",
            current="greet return hi",
            replayed="greet return howdy",
            resolved=resolved,
        ),
        outcome=outcome,
        language=language,
        path="app.py",
    )


def test_store_append_and_iterate(store):
    store.append(_exp("a"))
    store.append(_exp("b"))
    all_exp = list(store)
    assert len(all_exp) == 2
    assert all_exp[0].example.summary == "a"


def test_store_len(store):
    store.append(_exp("a"))
    store.append(_exp("b"))
    assert len(store) == 2


def test_store_accepted_rejected_partition(store):
    store.append(_exp("good", "accepted"))
    store.append(_exp("bad", "rejected"))
    store.append(_exp("ugly", "escalated"))
    assert len(store.accepted()) == 1
    assert len(store.rejected()) == 2


def test_store_roundtrip_preserves_fields(store):
    e = Experience(
        example=HistoricalExample(summary="x", base="b", current="c", replayed="r", resolved="s"),
        outcome="accepted",
        language="rust",
        path="src/main.rs",
        session_id="sess1",
        unit_id="u1",
        validator_features={"syntax_passed": True, "lsp_error_count": 0},
        risk_score=0.1,
        retry_count=2,
    )
    store.append(e)
    loaded = list(store)[0]
    assert loaded.outcome == "accepted"
    assert loaded.language == "rust"
    assert loaded.path == "src/main.rs"
    assert loaded.validator_features["syntax_passed"] is True
    assert loaded.risk_score == 0.1
    assert loaded.retry_count == 2


def test_store_for_repo_resolves_relative(tmp_path):
    s = ExperienceStore.for_repo(str(tmp_path), ".rebase-agent/memory/x.jsonl")
    assert s.path == tmp_path / ".rebase-agent" / "memory" / "x.jsonl"


def test_store_skips_corrupt_lines(store, tmp_path):
    store.append(_exp("good"))
    # Append a corrupt line manually.
    with open(store.path, "a", encoding="utf-8") as fh:
        fh.write("not valid json\n")
    store.append(_exp("good2"))
    all_exp = list(store)
    assert len(all_exp) == 2  # corrupt line skipped


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------


def test_tokenize_splits_camel_case():
    assert tokenize("getUserName") == ["get", "user", "name"]


def test_tokenize_splits_snake_case():
    assert "max" in tokenize("max_retries")
    assert "retries" in tokenize("max_retries")


def test_tokenize_drops_stopwords():
    toks = tokenize("def return self class")
    assert toks == []  # all stopwords


def test_tokenize_handles_empty():
    assert tokenize("") == []


# ---------------------------------------------------------------------------
# LexicalRetriever
# ---------------------------------------------------------------------------


@pytest.fixture()
def populated_store(store):
    store.append(
        Experience(
            example=HistoricalExample(
                summary="greet", base="greet return hi", current="greet return hi",
                replayed="greet return howdy", resolved="return ('hi','howdy')",
            ),
            outcome="accepted", language="python", path="app.py",
        )
    )
    store.append(
        Experience(
            example=HistoricalExample(
                summary="config", base="retries=3", current="retries=5",
                replayed="timeout=10", resolved="retries=5 timeout=10",
            ),
            outcome="accepted", language="python", path="cfg.py",
        )
    )
    store.append(
        Experience(
            example=HistoricalExample(
                summary="farewell", base="farewell return bye", current="farewell return bye",
                replayed="farewell return adieu", resolved="return ('bye','adieu')",
            ),
            outcome="accepted", language="python", path="app.py",
        )
    )
    # A rejected one — must be excluded from retrieval.
    store.append(_exp("rejected", outcome="rejected"))
    return store


def test_retriever_ranks_relevant_first(populated_store):
    ret = LexicalRetriever(populated_store)
    results = ret.retrieve("return howdy greeting", k=2)
    assert len(results) >= 1
    assert results[0].summary == "greet"
    assert all(isinstance(r, HistoricalExample) for r in results)


def test_retriever_excludes_rejected(populated_store):
    ret = LexicalRetriever(populated_store)
    results = ret.retrieve("rejected", k=5)
    assert all(r.summary != "rejected" for r in results)


def test_retriever_language_filter(populated_store):
    ret = LexicalRetriever(populated_store)
    results = ret.retrieve("return greeting", k=3, language="rust")
    assert results == []  # all examples are python


def test_retriever_empty_query(populated_store):
    ret = LexicalRetriever(populated_store)
    assert ret.retrieve("", k=3) == []


def test_retriever_empty_store(store):
    ret = LexicalRetriever(store)
    assert ret.retrieve("anything", k=3) == []


def test_retriever_refresh_picks_up_new_experiences(populated_store):
    ret = LexicalRetriever(populated_store)
    ret.retrieve("test", k=1)  # build index
    populated_store.append(
        Experience(
            example=HistoricalExample(
                summary="newtopic", base="newtopic foo", current="newtopic foo",
                replayed="newtopic bar", resolved="foo bar",
            ),
            outcome="accepted", language="python", path="x.py",
        )
    )
    ret.refresh()
    results = ret.retrieve("newtopic foo", k=1)
    assert any(r.summary == "newtopic" for r in results)


# ---------------------------------------------------------------------------
# Context builder integration
# ---------------------------------------------------------------------------


def _unit(base, current, replayed, worktree, span=(1, 5)):
    return ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text=worktree, marker_span=span,
    )


def test_context_builder_injects_retrieved_examples(populated_store):
    ret = LexicalRetriever(populated_store)
    cb = ContextBuilder(context_lines=5, retriever=ret, retriever_k=2, min_examples=0)
    worktree = "def greet():\n<<<<<<< H\n    return 'hi'\n=======\n    return 'howdy'\n>>>>>>> b\n"
    unit = _unit("def greet():\n    pass", "    return 'hi'", "    return 'howdy'", worktree)
    ctx = cb.build(unit)
    assert len(ctx.retrieved_examples) >= 1
    # The greet example should be retrieved.
    assert any(e.summary == "greet" for e in ctx.retrieved_examples)


def test_context_builder_no_retriever_leaves_examples_empty():
    cb = ContextBuilder(context_lines=5)
    worktree = "def f():\n<<<<<<< H\n1\n=======\n2\n>>>>>>> b\n"
    unit = _unit("def f():\n    pass", "1", "2", worktree)
    ctx = cb.build(unit)
    assert ctx.retrieved_examples == []


def test_resolve_prompt_renders_few_shot(populated_store):
    ret = LexicalRetriever(populated_store)
    cb = ContextBuilder(context_lines=5, retriever=ret, retriever_k=2, min_examples=0)
    worktree = "def greet():\n<<<<<<< H\n    return 'hi'\n=======\n    return 'howdy'\n>>>>>>> b\n"
    unit = _unit("def greet():\n    pass", "    return 'hi'", "    return 'howdy'", worktree)
    ctx = cb.build(unit)
    prompt = build_resolve_prompt(unit, ctx)
    assert "Similar past merges" in prompt
    assert "RESOLVED" in prompt


def test_resolve_prompt_no_few_shot_when_empty():
    cb = ContextBuilder(context_lines=5)
    worktree = "def f():\n<<<<<<< H\n1\n=======\n2\n>>>>>>> b\n"
    unit = _unit("def f():\n    pass", "1", "2", worktree)
    ctx = cb.build(unit)
    prompt = build_resolve_prompt(unit, ctx)
    assert "Similar past merges" not in prompt

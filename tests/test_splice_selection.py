"""S28-139: ordered splice selection — the model picks and orders existing
blocks; capybase materializes verbatim.

Pure-protocol suite (decomposition, prompt, strict parse, materialization)
plus one end-to-end orchestrator test with a fake client, mirroring the
S28-136 pattern. The gate is SBCR's ambiguity band; protocol violations latch
the unit to normal generation (the factoring pattern).
"""

from __future__ import annotations

import json

from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.splice_selection import (
    build_splice_prompt,
    decompose_chunks,
    materialize,
    parse_splice_answer,
)


# ---------------------------------------------------------------------------
# Decomposition
# ---------------------------------------------------------------------------

def test_decompose_numbered_blocks_per_side_in_order():
    ch = decompose_chunks("a1\na2\n\na3\n", "r1\nr2\n")
    assert [c["id"] for c in ch] == ["A1", "A2", "R1"]
    assert ch[0]["lines"] == ["a1", "a2"]
    assert ch[1]["lines"] == ["a3"] and ch[1]["blanks_before"] == 1


def test_decompose_rejects_non_additive_shapes():
    assert decompose_chunks("", "r1\n") is None      # one side empty
    assert decompose_chunks("\n\n", "r1\n") is None  # current blank-only


def test_decompose_rejects_too_fragmented():
    cur = "".join(f"x{i}\n\n" for i in range(10))  # 10 single-line blocks
    assert decompose_chunks(cur, "r1\n", max_chunks=8) is None
    assert decompose_chunks(cur, "r1\n", max_chunks=24) is not None


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def test_prompt_lists_every_block_and_the_contract():
    ch = decompose_chunks("alpha\nbeta\n", "gamma\n")
    prompt = build_splice_prompt("CURRENT_UPSTREAM", "REPLAYED_COMMIT", ch)
    assert "A1" in prompt and "A2" in prompt and "R1" in prompt
    assert "alpha" in prompt and "gamma" in prompt
    assert '"order"' in prompt and "exactly once" in prompt
    assert "Never write block code" in prompt


# ---------------------------------------------------------------------------
# Strict parse
# ---------------------------------------------------------------------------

def _chunks():
    return decompose_chunks("a1\na2\n\na3\n", "r1\nr2\n")


def test_parse_accepts_order_with_preamble():
    ch = _chunks()
    text = 'Reason: replays first.\n{"order": ["R1", "A1", "A2"]}'
    order, glue = parse_splice_answer(text, ch)
    assert order == ["R1", "A1", "A2"] and glue == []


def test_parse_rejects_unknown_missing_or_duplicate_ids():
    ch = _chunks()
    bad = [
        '{"order": ["A1", "A2"]}',                      # missing R1
        '{"order": ["A1", "A2", "R1", "R9"]}',          # unknown id
        '{"order": ["A1", "A1", "R1", "A2"]}',          # duplicate
        '{"order": []}',
        '{"order": "A1"}',
        'not json at all',
        '',
    ]
    for text in bad:
        assert parse_splice_answer(text, ch) is None, text


def test_parse_glue_valid_anchored_and_capped():
    ch = _chunks()
    order, glue = parse_splice_answer(
        '{"order": ["A1", "A2", "R1"], "glue": ['
        '{"after": "start", "lines": ["# header"]},'
        '{"after": "A1", "lines": ["x = 1", "y = 2"]},'
        '{"after": "end", "lines": ["# tail"]}]}', ch)
    assert order == ["A1", "A2", "R1"]
    assert [g["after"] for g in glue] == ["start", "A1", "end"]
    # Too many insertions
    many = ",".join(f'{{"after": "A1", "lines": ["l"]}}' for _ in range(5))
    assert parse_splice_answer(
        f'{{"order": ["A1", "A2", "R1"], "glue": [{many}]}}', ch) is None
    # Oversized glue (4 lines > cap 3)
    assert parse_splice_answer(
        '{"order": ["A1", "A2", "R1"], "glue": ['
        '{"after": "A1", "lines": ["1", "2", "3", "4"]}]}', ch) is None
    # Bad anchor
    assert parse_splice_answer(
        '{"order": ["A1", "A2", "R1"], "glue": ['
        '{"after": "A9", "lines": ["x"]}]}', ch) is None


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------

def test_materialize_preserves_same_side_blank_separators():
    ch = _chunks()
    out = materialize(ch, *parse_splice_answer(
        '{"order": ["R1", "A1", "A2"]}', ch))
    assert out == "r1\nr2\na1\na2\n\na3"  # the a2|a3 blank restored


def test_materialize_cross_side_joins_butt_directly():
    ch = _chunks()
    out = materialize(ch, *parse_splice_answer(
        '{"order": ["A1", "R1", "A2"]}', ch))
    assert out == "a1\na2\nr1\nr2\na3"


def test_materialize_reordered_nonadjacent_gets_no_separator():
    ch = _chunks()
    out = materialize(ch, *parse_splice_answer(
        '{"order": ["A2", "A1", "R1"]}', ch))
    assert out == "a3\na1\na2\nr1\nr2"


def test_materialize_glue_lands_at_anchor():
    ch = _chunks()
    out = materialize(ch, *parse_splice_answer(
        '{"order": ["A1", "A2", "R1"], '
        '"glue": [{"after": "A2", "lines": ["# glue"]}]}', ch))
    assert out == "a1\na2\n# glue\n\na3\nr1\nr2"


def test_materialize_verbatim_round_trip_in_side_order():
    """Ordering A1,A2,R1 with no glue restores the sides' own concatenation —
    the bytes are the sides', never the model's."""
    ch = _chunks()
    out = materialize(ch, ["A1", "A2", "R1"], [])
    assert out == "a1\na2\n\na3\nr1\nr2"


# ---------------------------------------------------------------------------
# End-to-end through the orchestrator stage
# ---------------------------------------------------------------------------

def _unit(fitness: float | None) -> ConflictUnit:
    u = ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="def f():\n    return 1\n"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE",
                             text="a = 1\n\nb = 2\n"),   # A1, A2
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE",
                              text="c = 3\n"),            # R1
        original_worktree_text="def f():\n    return 1\n",
        marker_span=(0, 1),
    )
    if fitness is not None:
        u.structural_metadata["_sbcr_fitness"] = fitness
    return u


def _config(repo, client):
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator
    from capybase.resolution_engine import ResolutionEngine

    cfg = Config()
    cfg.model.model = "fake"
    cfg.model.samples = 1
    cfg.model.enable_self_consistency = False
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    cfg.validation.enable_per_unit_syntax_check = False
    cfg.features.structural_resolution = False
    cfg.features.combination_search = False
    cfg.future.enable_block_capture = False
    cfg.future.enable_source_portfolio = False
    cfg.future.enable_ordered_splice = True
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    orch.step = 1
    return orch


def _splice_answer(messages, **_kw):
    from capybase.adapters.llm_openai import LLMResponse
    return LLMResponse(text=json.dumps({"order": ["A1", "A2", "R1"]}))


def test_resolve_unit_accepts_ordered_splice(repo):
    """Generation fails (the fake answer parses as neither candidate nor
    repair), then the post-failure rescue engages: the unit is accepted via
    ordered_splice with the materialized blocks."""
    class _Fake:
        def complete(self, messages, *, model, temperature, max_tokens,
                     json_mode):
            return _splice_answer(messages)

    orch = _config(repo, _Fake())
    unit = _unit(fitness=0.5)
    outcome = orch._resolve_unit(unit, max_retries=0)
    assert outcome.accepted is not None
    assert outcome.accepted.provenance == "ordered_splice"
    assert outcome.accepted.resolved_text == "a = 1\n\nb = 2\nc = 3"


def test_resolve_unit_out_of_band_fitness_skips_stage(repo):
    """Below the ambiguity band the splice stage never sends its prompt —
    the LLM loop runs normally and the ADDITIVE prompt is never seen."""
    seen_prompts: list[str] = []

    class _Normal:
        def complete(self, messages, *, model, temperature, max_tokens,
                     json_mode):
            seen_prompts.append(str(messages))
            from capybase.adapters.llm_openai import LLMResponse
            return LLMResponse(text=json.dumps(
                {"resolved_text": "z", "explanation": "gen",
                 "self_reported_confidence": 0.0}))

    orch = _config(repo, _Normal())
    unit = _unit(fitness=0.2)
    orch._resolve_unit(unit, max_retries=0)
    assert not any("ADDITIVE" in p for p in seen_prompts), (
        "the splice prompt must not be sent out of band")


def test_protocol_violation_latches_unit_to_generation(repo):
    """Generation fails, the rescue's garbage answer latches
    _splice_protocol_failed; a SECOND resolve skips the stage entirely (one
    SPLICE call total — the LLM loop's own fallback calls don't count)."""
    splice_calls: list = []

    class _Garbage:
        def complete(self, messages, *, model, temperature, max_tokens,
                     json_mode):
            from capybase.adapters.llm_openai import LLMResponse
            if "ADDITIVE" in str(messages):
                splice_calls.append(messages)
                return LLMResponse(text="I cannot answer in JSON, sorry!")
            return LLMResponse(text="definitely not the candidate schema")

    orch = _config(repo, _Garbage())
    unit = _unit(fitness=0.5)
    orch._resolve_unit(unit, max_retries=0)
    assert len(splice_calls) == 1
    assert unit.structural_metadata.get("_splice_protocol_failed") is True
    orch._resolve_unit(unit, max_retries=0)
    assert len(splice_calls) == 1  # the latch prevented a second stage attempt

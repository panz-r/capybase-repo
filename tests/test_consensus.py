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


def test_r43_normalize_crlf_to_lf():
    """r43 (MEDIUM): normalize never converted CRLF → LF, so two byte-identical
    resolutions differing only in line endings (a Windows worktree's CRLF blobs
    echoed by the model) got different normalization keys → split clusters →
    degraded agreement score (and in a tie, a flipped winner). Now normalizes
    CRLF (and bare CR) to LF before everything else."""
    lf = "def foo():\n    return 1"
    crlf = "def foo():\r\n    return 1"
    assert normalize(lf, "python") == normalize(crlf, "python"), (
        f"CRLF vs LF split clusters: {normalize(lf, 'python')!r} != {normalize(crlf, 'python')!r}"
    )
    # No CR survives normalization.
    assert "\r" not in normalize(crlf, "python")
    # A cluster of mixed-line-ending samples is unanimous.
    from capybase.conflict_model import CandidateResolution
    cands = [
        CandidateResolution(candidate_id="a", unit_id="u", model_name="m",
                            prompt_version="v", resolved_text=lf),
        CandidateResolution(candidate_id="b", unit_id="u", model_name="m",
                            prompt_version="v", resolved_text=lf),
        CandidateResolution(candidate_id="c", unit_id="u", model_name="m",
                            prompt_version="v", resolved_text=crlf),
    ]
    clusters = cluster(cands, "python")
    assert len(clusters) == 1, f"mixed line endings split into {len(clusters)} clusters"


def test_normalize_rust_raw_hash_count_not_prematurely_closed():
    """Bug #8 (concrete, fixed by canonical-lexer migration): a Rust raw string
    ``r##"..."##`` (2-hash) must NOT be prematurely closed by an interior line
    containing ``"###`` (3 hashes) — per the Rust Reference, the closer's hash
    count must EXACTLY equal the opener's. The bespoke _multi_string_closes
    helper returned True for any ``"#+`` line, closing early and causing the
    subsequent lines to be treated as code (blank-line collapse, comment-strip)
    — silently corrupting the normalization key for any Rust raw string whose
    interior contained a longer hash run. The canonical char-scan tracks exact
    hash counts."""
    # r##" opens (2 hashes). Interior has a "### line (3 hashes — NOT the
    # closer for a 2-hash string) and a blank line. All must be preserved as
    # string interior.
    src = (
        'let a = r##"\n'
        'content "### hashes\n'
        '\n'
        'more content\n'
        '"##;\n'
        'let b = 1;\n'
    )
    n = normalize(src, "rust")
    # The blank line and the "### line are string interior — must survive.
    assert "more content" in n, f"raw string prematurely closed: {n!r}"
    assert "let b = 1" in n


def test_normalize_cpp_raw_string_interior_preserved():
    """C++ raw strings R\"DELIM(...)DELIM\" must have their interior preserved
    verbatim — the prior helper didn't handle C++ raw at all."""
    src = (
        'void f() {\n'
        '  auto s = R"x(\n'
        '  multi-line content\n'
        '  with a # line\n'
        '  )x";\n'
        '  g();\n'
        '}\n'
    )
    n = normalize(src, "cpp")
    assert "multi-line content" in n
    assert "with a # line" in n
    assert "g();" in n


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
# FactSelfCheck rationale-consistency: agreement over the
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
    consistent with the cohort wins the tie-break ( down-weight
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


def test_propose_with_consensus_forwards_prev_candidate_to_repair_prompt():
    """A self-consistency RETRY (failures + prev_candidate) must use the targeted
    repair prompt, not the generic retry prompt.

    Regression: propose_with_consensus did not accept/forward prev_candidate, so
    retries under self-consistency dropped the CEGIS counterexample feedback and
    degraded to a from-scratch retry. For a small local model the targeted repair
    (show the broken candidate + the specific error) is more valuable than
    another vote.
    """
    from capybase.config import ModelConfig
    from capybase.conflict_extractor import ConflictUnit
    from capybase.conflict_model import (
        CandidateResolution, ConflictSide, VerificationFailure,
    )
    from capybase.context_builder import ContextBuilder
    from capybase.resolution_engine import ResolutionEngine

    captured_prompts: list[str] = []

    class CaptureClient:
        def __init__(self, text: str):
            self._text = text

        def complete(self, messages, **kw):
            from capybase.adapters.llm_openai import LLMResponse
            # The prompt is the last user message content.
            captured_prompts.append(messages[-1].get("content", ""))
            return LLMResponse(
                text=self._text,
                raw={"_accumulated": {"finish_reason": "stop"}},
            )

    cfg = ModelConfig(samples=3)
    client = CaptureClient('{"resolved_text": "    return 1"}')
    engine = ResolutionEngine(cfg, client=client)
    worktree = (
        "def f():\n<<<<<<< H\n    return 0\n=======\n    return 9\n>>>>>>> b\n"
    )
    unit = ConflictUnit(
        session_id="s", step_index=1, path="f.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="def f():\n    pass"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="    return 0"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="    return 9"),
        original_worktree_text=worktree, marker_span=(1, 5),
    )
    prev = CandidateResolution(
        candidate_id="u:c0", unit_id="u", model_name="fake",
        prompt_version="cegis_repair.v1", resolved_text="    return BROKEN_ATTEMPT",
    )
    failures = [VerificationFailure(validator="syntax", severity="error",
                                   message="invalid syntax (return BROKEN_ATTEMPT)")]

    engine.propose_with_consensus(
        unit, ContextBuilder().build(unit),
        failures=failures, prev_candidate=prev, n_samples=3,
    )
    # Every captured prompt must be the targeted REPAIR prompt (carries the
    # broken candidate verbatim), never the generic retry prompt.
    assert captured_prompts, "no prompt was sent"
    for p in captured_prompts:
        assert "YOUR PREVIOUS ATTEMPT (needs fixing):" in p, (
            "self-consistency retry used the generic retry prompt instead of the "
            "targeted repair prompt — prev_candidate was not forwarded"
        )
        assert "BROKEN_ATTEMPT" in p  # the broken candidate is shown to the model


def test_r34_normalize_does_not_truncate_string_with_hash():
    """F3 (HIGH): normalize's trailing-comment regex matched a ``#`` inside a
    string literal (``x = \"a # b\"``), truncating the string and corrupting the
    clustering key — two resolutions with different string values could merge."""
    result = normalize('msg = "a # b"  # real comment', "python")
    assert '"a # b"' in result, f"string truncated at inner #; got {result!r}"
    # The real trailing comment must still be stripped.
    assert "# real comment" not in result
    # Rust // inside a string.
    result2 = normalize('let s = "a // b";  // real', "rust")
    assert '"a // b"' in result2, f"string truncated at inner //; got {result2!r}"


def test_r35_python_floor_division_not_stripped():
    """F3 (MEDIUM, pre-existing): ``//`` is floor division in Python, not a
    comment. normalize stripped it, making ``n // 2`` and ``n // 3`` cluster
    together under the same key."""
    r1 = normalize("total = n // 2", "python")
    r2 = normalize("total = n // 3", "python")
    assert r1 != r2, f"floor-division operand stripped; both normalize to {r1!r}"
    assert "2" in r1, f"floor-div operand lost; got {r1!r}"


def test_r35_canonicalize_preserves_hash_lines_in_strings():
    """F5 (MEDIUM): canonicalize_context dropped #-led lines inside multi-line
    strings (docstrings), corrupting the context window shown to the model."""
    from capybase.context_builder import canonicalize_context
    text = 'def foo():\n    """\n    Intro.\n    # heading in docstring\n    """\n    return 1\n'
    result = canonicalize_context(text, "python")
    assert "# heading in docstring" in result, (
        f"docstring #-line dropped; got {result!r}"
    )


# ---------------------------------------------------------------------------
# Round 39 — consensus / context: state-aware comment & string handling
# ---------------------------------------------------------------------------


def test_r39_star_prefix_does_not_drop_rust_deref():
    """r39 (HIGH): the ``*`` entry in comment_line_prefixes (meant to catch
    ``/* */`` block-comment continuation lines like `` * foo``) also matched
    valid Rust pointer dereferences (``*p = 5;``) and JS multi-line
    multiplications, silently dropping code from normalized output. A bare
    ``*``-leading line is far more often code than a comment; only ``/*``/``*/``
    unambiguously indicate a comment line."""
    src = "fn foo(p: *mut i32) {\n    *p = 5;\n}\n"
    out = normalize(src, language="rust")
    assert "*p = 5;" in out, f"deref code line dropped by '*' prefix; got {out!r}"


def test_r39_canonicalize_star_prefix_does_not_drop_deref():
    """r39 (HIGH): same bug in canonicalize_context — the context window shown
    to the model dropped ``*ptr;`` lines."""
    from capybase.context_builder import canonicalize_context
    src = "unsafe {\n    *ptr;\n}\n"
    out = canonicalize_context(src, language="rust")
    assert "*ptr;" in out, f"deref dropped from context; got {out!r}"


def test_r39_normalize_preserves_docstring_interior_lines():
    """r39 (HIGH): normalize dropped lines inside a multi-line string that
    LOOKED like comments (``#``-led), because it made line-local comment
    decisions with no multi-line-string state. A docstring's interior is string
    CONTENT and must be preserved — otherwise two resolutions differing only in
    docstring text falsely cluster together."""
    src1 = 'def foo():\n    """\n    # alpha\n    """\n    return 1'
    src2 = 'def foo():\n    """\n    # beta\n    """\n    return 1'
    assert normalize(src1, "python") != normalize(src2, "python"), (
        "docstring-interior comment lines dropped → false clustering"
    )
    # The interior line is preserved verbatim.
    assert "# alpha" in normalize(src1, "python")


def test_r39_normalize_preserves_blank_runs_in_strings():
    """r39 (HIGH): normalize's blank-line collapse (``\\n\\s*\\n+``) ran over
    the WHOLE joined output, collapsing blank-line runs INSIDE multi-line
    strings — corrupting string contents and causing false clustering."""
    src1 = 'msg = """\n\n\nhello"""'   # 3 internal blanks
    src2 = 'msg = """\nhello"""'
    assert normalize(src1, "python") != normalize(src2, "python"), (
        "blank runs inside strings collapsed → false clustering"
    )
    # The internal blank lines survive (3 newlines = 2 blank lines preserved).
    out = normalize(src1, "python")
    assert "\n\n\n" in out, f"internal blank lines collapsed; got {out!r}"


def test_r39_php_hash_comments_stripped():
    """r39 (MEDIUM): PHP supports BOTH ``//`` and ``#`` line comments, but the
    Family-A classification set ``hash_is_comment = False`` for PHP, so ``#``
    comments survived normalization (while ``//`` comments were stripped) —
    inconsistent clustering."""
    with_hash = '<?php\n$x = 1; # php comment\n?>'
    clean = '<?php\n$x = 1;\n?>'
    assert normalize(with_hash, "php") == normalize(clean, "php"), (
        "PHP # comment not stripped → inconsistent clustering"
    )

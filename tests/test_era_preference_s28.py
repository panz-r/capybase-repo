"""S28-312b/316 era-preference arm — the vote, the election, the note.

The era-MIXED class (0056: ngx_queue x29 AND QUEUE x28 coexisting; the
per-unit resolutions alternated sides — locally plausible everywhere, a
mixed-era file) gets a pre-draw consistency signal: each accepted
candidate votes which side's DISTINCTIVE vocabulary it matches, and the
file's running election (UNIT PLURALITY — the S28-316 critical
resolution; the token-mass election picked the WRONG era on the
fixture) rides a short note into later units' prompts. Advisory only:
the model may override, and the normal gates judge the result.
"""

from __future__ import annotations

from types import SimpleNamespace

from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.orchestrator import (
    _era_consistency_note,
    _era_election,
    _era_vote,
)
from capybase.resolution_engine import _resolve_prompt_parts
from capybase.conflict_model import ContextBundle


def _unit_era():
    # the 0056 shape: each side carries a distinctive API vocabulary
    return ConflictUnit(
        session_id="s", step_index=0, path="src/queue.c", language="c",
        conflict_type="UU", unit_id="u1", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="void init(void);\n"),
        current=ConflictSide(
            label="CURRENT_UPSTREAM_SIDE",
            text="QUEUE q; QUEUE_init(&q); QUEUE_push(&q, node);\n"),
        replayed=ConflictSide(
            label="REPLAYED_COMMIT_SIDE",
            text="ngx_queue_t q; ngx_queue_init(&q); ngx_queue_insert(&q, n);\n"),
        original_worktree_text="void init(void);\n", marker_span=(0, 0),
    )


def _cand(text):
    return SimpleNamespace(resolved_text=text)


# ---------------------------------------------------------------------------
# the vote: distinctive-token matching
# ---------------------------------------------------------------------------

def test_vote_current_when_candidate_matches_current_vocab():
    assert _era_vote(_unit_era(), _cand(
        "QUEUE q; QUEUE_init(&q); QUEUE_push(&q, node);\n")) == "current"


def test_vote_replayed_when_candidate_matches_replayed_vocab():
    assert _era_vote(_unit_era(), _cand(
        "ngx_queue_t q; ngx_queue_init(&q);\n")) == "replayed"


def test_tie_votes_nothing():
    # one distinctive token from each side -> tie -> None
    assert _era_vote(_unit_era(), _cand("QUEUE_init; ngx_queue_init;\n")) \
        is None


def test_shared_vocabulary_only_votes_nothing():
    u = ConflictUnit(
        session_id="s", step_index=0, path="f.c", language="c",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="int x;\n"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="int x;\n"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="int x;\n"),
        original_worktree_text="int x;\n", marker_span=(0, 0),
    )
    assert _era_vote(u, _cand("int x;\n")) is None


# ---------------------------------------------------------------------------
# the election: unit plurality, never token mass
# ---------------------------------------------------------------------------

def test_election_by_unit_plurality():
    assert _era_election(["current", "current", "replayed"]) == "current"
    assert _era_election(["replayed", "replayed", "current"]) == "replayed"


def test_tie_and_thin_evict():
    assert _era_election(["current", "replayed"]) is None
    assert _era_election(["current"]) is None
    assert _era_election([]) is None
    assert _era_election([None, "current", None, "replayed"]) is None


# ---------------------------------------------------------------------------
# the note + the prompt render
# ---------------------------------------------------------------------------

def test_note_names_the_side_and_the_margin():
    note = _era_consistency_note(
        ["replayed", "replayed", "current"], "replayed")
    assert "REPLAYED_COMMIT_SIDE" in note
    assert "2 of the 3" in note
    assert "vote 1-2" in note


def test_prompt_renders_the_note_when_stashed():
    u = _unit_era()
    u.structural_metadata["era_election"] = {
        "side": "replayed",
        "votes": ["replayed", "replayed", "current"],
        "note": _era_consistency_note(
            ["replayed", "replayed", "current"], "replayed"),
    }
    prompt = build = None
    from capybase.resolution_engine import build_resolve_prompt
    build = build_resolve_prompt(
        u, ContextBundle(primary_text="void init(void);\n"))
    assert "ERA CONSISTENCY" in build
    assert "REPLAYED_COMMIT_SIDE" in build


def test_prompt_byte_identical_without_the_stash():
    u = _unit_era()
    from capybase.resolution_engine import build_resolve_prompt
    p1 = build_resolve_prompt(u, ContextBundle(primary_text="x\n"))
    u2 = _unit_era()
    u2.structural_metadata["era_election"] = {
        "side": "current", "votes": ["current", "current"],
        "note": _era_consistency_note(["current", "current"], "current")}
    p2 = build_resolve_prompt(u2, ContextBundle(primary_text="x\n"))
    assert "ERA CONSISTENCY" in p2
    assert "ERA CONSISTENCY" not in p1

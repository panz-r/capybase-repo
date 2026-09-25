"""S28-245 (queue item 3) — whole-file sides for the side/API notes.

The side-consistent note greps the unit's current/replayed FRAGMENTS
(3-10 lines for the scikit family, short hunks for duckdb) — and the
failing symbols' declarations live in the FILE. Trial15: the armed
notes fired zero times on the duckdb loops for exactly this. The fix:
the orchestrator stashes the whole-file sides on the unit
(structural_metadata["whole_file_sides"], the structural-path
precedent) and the note greps the stash, falling back to the
fragments. Same flag as the note itself.
"""

from __future__ import annotations

from types import SimpleNamespace

import capybase.resolution_engine as re_mod
from capybase.conflict_model import ConflictSide, ConflictUnit


def _unit(fragment_cur: str, fragment_rep: str, stash: dict | None = None):
    u = ConflictUnit(
        session_id="s", step_index=1, path="src/peg/autocomplete_core.cpp",
        language="cpp",
        conflict_type="UU", unit_id="u", unit_kind="whole_file",
        base=ConflictSide(label="BASE", text=""),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE",
                             text=fragment_cur),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE",
                              text=fragment_rep),
        original_worktree_text="", marker_span=(0, 0),
    )
    if stash:
        u.structural_metadata.update(stash)
    return u


def _fail(msg: str):
    return SimpleNamespace(message=msg)


# The 0127 dry-run expectation: the fragment hunks do NOT carry the
# declaration of the failing symbol; the whole-file sides DO.
_FRAG_CUR = "if (!compiled_grammar.GetTokenizer().TokenizeInput(behavior)) {"
_FRAG_REP = "\treturn {};\n}"
_STASH = {
    "current": (
        "class CompiledGrammar {\n"
        " public:\n"
        "\tTokenizer GetTokenizer();\n"
        "};\n"
    ),
    "replayed": (
        "\tauto g = compiled_grammar->GetKeywordHelper();\n"
        "\tif (!g) { return {}; }\n"
    ),
}


def test_note_off_by_default():
    re_mod._SIDE_CONVENTION_ENABLED = False
    try:
        u = _unit(_FRAG_CUR, _FRAG_REP, {"whole_file_sides": _STASH})
        f = _fail("'class duckdb::shared_ptr<duckdb::CompiledGrammar>' "
                  "has no member named 'GetTokenizer'")
        assert re_mod._side_convention_note(u, [f]) == ""
    finally:
        re_mod._SIDE_CONVENTION_ENABLED = False


def test_stash_wins_when_fragments_cannot_match():
    re_mod._SIDE_CONVENTION_ENABLED = True
    try:
        u = _unit(_FRAG_CUR, _FRAG_REP, {"whole_file_sides": _STASH})
        f = _fail("'class duckdb::shared_ptr<duckdb::CompiledGrammar>' "
                  "has no member named 'GetTokenizer'")
        note = re_mod._side_convention_note(u, [f])
        # the note fires with the DECLARATION the fragment could not see
        assert "GetTokenizer" in note
        assert "CURRENT" in note and "REPLAYED" in note
    finally:
        re_mod._SIDE_CONVENTION_ENABLED = False


def test_fragment_fallback_when_no_stash():
    """Fragments carrying the declaration still work without a stash —
    the pre-existing behavior is untouched."""
    re_mod._SIDE_CONVENTION_ENABLED = True
    try:
        frag_cur = "\tif (grammar.GetTokenizer()) {\n"
        frag_rep = "\tif (grammar->GetTokenizer()) {\n"
        u = _unit(frag_cur, frag_rep)
        f = _fail("'class X' has no member named 'GetTokenizer'")
        note = re_mod._side_convention_note(u, [f])
        assert "GetTokenizer" in note
    finally:
        re_mod._SIDE_CONVENTION_ENABLED = False


def test_stash_partial_keys_fall_through_per_side():
    """A stash missing one side falls back to the fragment for that
    side only: here the declaration comes from the stashed current
    side while the replayed fragment has no GetTokenizer line — so the
    note reads the one-sided (api-removal) shape."""
    re_mod._SIDE_CONVENTION_ENABLED = True
    try:
        u = _unit(_FRAG_CUR, _FRAG_REP,
                  {"whole_file_sides": {"current": _STASH["current"]}})
        f = _fail("'class duckdb::shared_ptr<duckdb::CompiledGrammar>' "
                  "has no member named 'GetTokenizer'")
        note = re_mod._side_convention_note(u, [f])
        assert "GetTokenizer" in note
        assert "ONLY in the CURRENT side" in note
        assert "Tokenizer GetTokenizer();" in note
    finally:
        re_mod._SIDE_CONVENTION_ENABLED = False


# ---------------------------------------------------------------------------
# S28-234 gap 2 (queue item 5): the seam note prefers the assembled buffer
# ---------------------------------------------------------------------------

def test_seam_scan_prefers_the_assembled_buffer():
    from capybase.resolution_engine import _seam_scan_text
    u = _unit("x = compute(\n", ")\n",
              {"assembled_buffer": "data = [\n    1, 2,\n]\nx = compute(\n"})
    # the error's line 4 is assembly coordinates — unresolvable in the
    # prefix+fragment shape (2 lines) but exact in the assembled buffer
    scan = _seam_scan_text(u, "x = compute(\n")
    assert scan == "data = [\n    1, 2,\n]\nx = compute(\n"


def test_seam_scan_falls_back_to_prefix_plus_candidate():
    from capybase.resolution_engine import _seam_scan_text
    u = ConflictUnit(
        session_id="s", step_index=1, path="f.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=""),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="x = compute(\n"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=")\n"),
        original_worktree_text="data = [\n    1, 2,\n]\n",
        marker_span=(4, 5),
    )
    scan = _seam_scan_text(u, "x = compute(\n")
    # span[0]=4 includes the worktree's trailing empty split element,
    # so the pre-context ends with its own newline — one blank line
    assert scan == "data = [\n    1, 2,\n]\n\nx = compute(\n"


def test_seam_note_fires_through_assembly_coordinates():
    """The s0005 shape: the failing line (5) sits in the assembled file;
    the note names the opener the fragment alone could never see."""
    re_mod._SEAM_AWARE_ENABLED = True
    try:
        u = ConflictUnit(
            session_id="s", step_index=1, path="f.py", language="python",
            conflict_type="UU", unit_id="u", unit_kind="whole_file",
            base=ConflictSide(label="BASE", text=""),
            current=ConflictSide(label="CURRENT_UPSTREAM_SIDE",
                                 text="x = compute(\n"),
            replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=""),
            original_worktree_text="", marker_span=(0, 0),
        )
        u.structural_metadata.update(
            {"assembled_buffer": "data = [\n    1, 2,\n]\nx = compute(\n"})
        f = _fail("f.py:5:1: closing parenthesis ')' does not match "
                  "opening parenthesis '['")
        note = re_mod._seam_aware_note(u, [f], "x = compute(\n")
        assert "SEAM CONTEXT" in note
        # the opener named is the assembled file's, with its true line
        assert "opened at line 4" in note
    finally:
        re_mod._SEAM_AWARE_ENABLED = False

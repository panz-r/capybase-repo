"""S28-180 — side-consistent repair feedback (pilot-gated, default OFF).

The five semantic side-mismatch rows (0137/0113/0052/0129/0039) failed
because the file mixed the two sides' API conventions while the repair
feedback carried only the bare gcc error. The enrichment attaches BOTH
sides' declaration of the failing symbol — same repair rounds, sharper
evidence per round (the anti-S28-74: information, not budget). Contract:
zero new model requests.
"""

from __future__ import annotations

from types import SimpleNamespace

from capybase.conflict_model import (
    ConflictSide,
    ConflictUnit,
    VerificationFailure,
)
import capybase.resolution_engine as re_mod


def _unit():
    return ConflictUnit(
        session_id="s", step_index=0, path="f.cpp", language="cpp",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="struct T {};\n"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE",
                             text="struct T {};\nT* make();\n"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE",
                              text="struct T {};\nT& make();\n"),
        original_worktree_text="struct T {};\n", marker_span=(0, 0),
    )


def _fail(msg):
    return VerificationFailure(validator="syntax", message=msg)


def test_divergent_symbol_gets_both_variants():
    re_mod.set_side_consistent_feedback(True)
    try:
        note = re_mod._side_convention_note(
            _unit(),
            [_fail("no matching function for call to 'make()'")])
    finally:
        re_mod.set_side_consistent_feedback(False)
    assert note is not None and "side convention note" in note
    assert "CURRENT side:" in note and "T* make();" in note
    assert "REPLAYED side:" in note and "T& make();" in note


def test_toggle_off_is_a_noop():
    re_mod.set_side_consistent_feedback(False)
    assert re_mod._side_convention_note(
        _unit(),
        [_fail("no matching function for call to 'make()'")]) == ""


def test_identical_declarations_no_note():
    """When both sides declare the symbol the same way there is no
    convention conflict — the note must stay silent."""
    re_mod.set_side_consistent_feedback(True)
    try:
        unit = _unit()
        unit.current = ConflictSide(label="CURRENT_UPSTREAM_SIDE",
                                    text="struct T {};\nT& make();\n")
        unit.replayed = ConflictSide(label="REPLAYED_COMMIT_SIDE",
                                     text="struct T {};\nT& make();\n")
        assert re_mod._side_convention_note(
            unit, [_fail("no matching function for call to 'make()'")]) == ""
    finally:
        re_mod.set_side_consistent_feedback(False)


def test_no_matching_symbol_no_note():
    re_mod.set_side_consistent_feedback(True)
    try:
        assert re_mod._side_convention_note(
            _unit(), [_fail("unterminated triple-quoted string")]) == ""
    finally:
        re_mod.set_side_consistent_feedback(False)


def test_member_shape_is_recognized():
    re_mod.set_side_consistent_feedback(True)
    try:
        note = re_mod._side_convention_note(
            _unit(), [_fail("no member named 'make' in 'struct T'")])
    finally:
        re_mod.set_side_consistent_feedback(False)
    assert note is not None and "make" in note



# ---------------------------------------------------------------------------
# S28-230: the comment mask + the one-sided removal note
# ---------------------------------------------------------------------------

def _mk_unit(path, lang, cur, rep):
    return ConflictUnit(
        session_id="s", step_index=0, path=path, language=lang,
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=""),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=cur),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=rep),
        original_worktree_text="", marker_span=(0, 0))


def test_note_ignores_comment_prose(monkeypatch):
    """duckdb-0129's noise: the word 'get' inside a // comment posed as
    the replayed side's declaration. The mask blanks comments first."""
    monkeypatch.setattr(re_mod, "_SIDE_CONVENTION_ENABLED", True)
    unit = _mk_unit("a.cpp", "cpp",
                    "void f(Helper h) {\n  h.get();\n}\n",
                    "// all that will get us out is a $\nvoid f(Helper h) {\n}\n")
    f = SimpleNamespace(message="error: 'class Helper' has no member named 'get'",
                        validator="v", detail={})
    note = re_mod._side_convention_note(unit, [f])
    assert "will get us out" not in (note or "")


def test_one_sided_removal_note(monkeypatch):
    """duckdb-0126's removal shape: the failing member exists in only
    ONE side — the note names the removal and shows the surviving
    side's own call sites."""
    monkeypatch.setattr(re_mod, "_SIDE_CONVENTION_ENABLED", True)
    unit = _mk_unit("p.cpp", "cpp",
                    "void f(Cache c) {\n  c.GetTokenizer().Run();\n}\n",
                    "void f(Cache c) {\n  c.Run();\n}\n")
    f = SimpleNamespace(message="error: 'struct Cache' has no member named 'GetTokenizer'",
                        validator="v", detail={})
    note = re_mod._side_convention_note(unit, [f])
    assert "api removal note" in note
    assert "ONLY in the CURRENT side" in note


def test_both_sides_note_still_fires(monkeypatch):
    """The original S28-180 contract is unchanged by the mask."""
    monkeypatch.setattr(re_mod, "_SIDE_CONVENTION_ENABLED", True)
    unit = _mk_unit("p.cpp", "cpp",
                    "void f() {\n  state.tokens.clear();\n}\n",
                    "void g(vector<T> tokens) {\n  tokens.clear();\n}\n")
    f = SimpleNamespace(message="error: 'struct S' has no member named 'tokens'",
                        validator="v", detail={})
    note = re_mod._side_convention_note(unit, [f])
    assert "side convention note" in (note or "")


def test_finder_guard_rejects_control_flow(monkeypatch):
    """duckdb-0063: 'return rule;' matched the plain-variable branch —
    the injection ladder inserted the statement as garbage."""
    from capybase.verification import find_symbol_declaration_lines
    cur = "optional_ptr<Rule> rule;\n"
    rep = "void f() {\n  return rule;\n}\n"
    d = find_symbol_declaration_lines("rule", "cpp", cur, rep, "")
    assert d == ["optional_ptr<Rule> rule;"]


def test_side_note_rides_the_retry_carrier(monkeypatch):
    """S28-237 (trial15): the side/API notes had the seam note's
    carrier gap — the duckdb loops retry fresh-gen, so the note must
    ride retry_prompt_with_trims as well."""
    from capybase.resolution_engine import build_retry_prompt
    from capybase.conflict_model import ContextBundle
    monkeypatch.setattr(re_mod, "_SIDE_CONVENTION_ENABLED", True)
    unit = _mk_unit("p.cpp", "cpp",
                    "void f(Cache c) {\n  c.GetTokenizer().Run();\n}\n",
                    "void f(Cache c) {\n  c.Run();\n}\n")
    f = SimpleNamespace(message="error: 'struct Cache' has no member named 'GetTokenizer'",
                        validator="v", detail={})
    prompt = build_retry_prompt(unit, ContextBundle(primary_text="x"), [f])
    assert "api removal note" in prompt

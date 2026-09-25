"""S28-189/S28-204/205 — seam-aware repair feedback (pilot-gated, OFF).

The seam family: fragments fail on context they cannot SEE. scikit-0005
stalled six identical draws on a paren whose opener sat in the splice
seam's PRE-CONTEXT (string-seam); prusaslicer-0115 and redis-0032
spliced bare statements outside any function body (scope-seam). The
note is a one-line deterministic context reveal riding the repair round
that already happens — zero new model requests (the S28-180 contract).
"""

from __future__ import annotations

from types import SimpleNamespace

import capybase.resolution_engine as re_mod
from capybase.conflict_model import ConflictSide, ConflictUnit


def _unit(lang="python", worktree=None, span=None):
    return ConflictUnit(
        session_id="s", step_index=0, path="f.py", language=lang,
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=""),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=""),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=""),
        original_worktree_text=worktree or "",
        marker_span=span or (0, 0),
    )


# ---------------------------------------------------------------------------
# the scanners
# ---------------------------------------------------------------------------

def test_nearest_unclosed_opener_finds_the_seam_opener():
    text = ("data = [\n"
            "    1, 2,\n"
            "]\n"
            "x = compute(\n")
    # line 4 (0-based 3): the `(` at line 4 is open -> nearest opener
    assert re_mod._nearest_unclosed_opener(text, 4, "python") == ("(", 3)
    # the seam shape: the fragment's `)` cannot match the enclosing `[`
    seam = "data = [\n    pack(x)\n)\n"
    assert re_mod._nearest_unclosed_opener(seam, 2, "python") == ("[", 0)
    # everything closed -> None
    closed = "a = [1, 2]\nb = f(x)\n"
    assert re_mod._nearest_unclosed_opener(closed, 1, "python") is None


def test_nearest_unclosed_opener_ignores_strings_and_comments():
    text = ("s = \"([{\"  # ({\n"
            "tpl = '''(\n"
            "    more ([{\n"
            "'''\n")
    assert re_mod._nearest_unclosed_opener(text, 4, "python") is None


def test_splice_scope_depth_counts_masked_braces():
    text = ("namespace ns {\n"
            "void f() {\n"
            "  for (;;) { }\n")
    assert re_mod._splice_scope_depth(text, 3, "cpp") == 2
    assert re_mod._splice_scope_depth(text, 2, "cpp") == 2


def test_fragment_opens_statement():
    assert re_mod._fragment_opens_statement("for (auto& x : y) {\n")
    assert re_mod._fragment_opens_statement("\n  if (x) return;\n")
    assert not re_mod._fragment_opens_statement("int counter = 0;\n")
    assert not re_mod._fragment_opens_statement("// just a comment\n")


# ---------------------------------------------------------------------------
# the note, flag-gated
# ---------------------------------------------------------------------------

def test_note_off_by_default():
    re_mod._SEAM_AWARE_ENABLED = False
    u = _unit()
    f = SimpleNamespace(message="f.py:3:5: closing parenthesis ')' does not "
                                "match opening parenthesis '['")
    assert re_mod._seam_aware_note(u, [f], "x = compute(\n)") == ""
    re_mod._SEAM_AWARE_ENABLED = True  # restored by the next tests' care


def test_string_seam_note_reports_the_opener():
    """The scikit-0005 shape: the pre-context's `[` is never closed; the
    fragment is internally balanced until its trailing `)` — which the
    compiler reads against the invisible `[`."""
    re_mod._SEAM_AWARE_ENABLED = True
    try:
        worktree = "data = [\n"
        u = _unit("python", worktree=worktree, span=(1, 2))
        f = SimpleNamespace(message="f.py:3:1: closing parenthesis ')' does "
                                    "not match opening parenthesis '['")
        cand = "    item = pack(buf)\n)\n"
        note = re_mod._seam_aware_note(u, [f], cand)
        assert "SEAM CONTEXT" in note and "`[`" in note
        assert "line 1" in note  # the opener's line in the spliced file
    finally:
        re_mod._SEAM_AWARE_ENABLED = False


def test_scope_seam_note_states_the_scope():
    re_mod._SEAM_AWARE_ENABLED = True
    try:
        u = _unit("cpp")
        f = SimpleNamespace(message="y.cpp:5:1: error: expected unqualified-id "
                                    "before 'for'", validator="compile",
                                detail="")
        cand = ("int g_x = 0;\n"
                "\n"
                "namespace ns {\n"
                "\n"
                "for (int i = 0; i < n; ++i) {\n"
                "    total += i;\n"
                "}\n")
        note = re_mod._seam_aware_note(u, [f], cand)
        assert "SEAM CONTEXT" in note and "OUTSIDE any function" in note
    finally:
        re_mod._SEAM_AWARE_ENABLED = False


def test_scope_seam_declines_inside_a_function():
    re_mod._SEAM_AWARE_ENABLED = True
    try:
        u = _unit("c")
        f = SimpleNamespace(message="y.c:9:1: error: expected identifier or "
                                    "'(' before 'if'", validator="compile",
                                detail="")
        cand = ("void f(void) {\n"
                "    int i;\n"
                "\n"
                "if (i) { }\n"
                "}\n")
        # failing line 9 is beyond the candidate; no spurious depth read
        assert re_mod._seam_aware_note(u, [f], cand) == ""
    finally:
        re_mod._SEAM_AWARE_ENABLED = False


def test_seam_note_declines_on_balanced_precontext():
    re_mod._SEAM_AWARE_ENABLED = True
    try:
        u = _unit("python", worktree="a = 1\n", span=(1, 2))
        f = SimpleNamespace(message="f.py:2:1: closing parenthesis ')' does "
                                    "not match opening parenthesis '['",
                                validator="compile", detail="")
        # the pre-context is balanced — no seam to report
        assert re_mod._seam_aware_note(u, [f], "print(x)\n") == ""
    finally:
        re_mod._SEAM_AWARE_ENABLED = False


def test_repair_prompt_carries_the_seam_note(monkeypatch):
    """The note rides the existing repair prompt (the S28-180 slot
    pattern) — no new request, the enrichment lands in the prompt."""
    from capybase.resolution_engine import build_repair_prompt
    re_mod._SEAM_AWARE_ENABLED = True
    try:
        u = _unit("cpp")
        ctx = SimpleNamespace(repair_retrieved_examples=[],
                              high_trust_constraints=None)
        cand = SimpleNamespace(resolved_text=(
            "namespace ns {\n"
            "for (;;) { }\n"
            "}\n"))
        f = SimpleNamespace(
            message="f.cpp:2:1: error: expected unqualified-id before 'for'",
            kind="compile", severity="error", validator="compile",
            detail="")
        prompt = build_repair_prompt(u, ctx, cand, [f], None, 0)
        assert "SEAM CONTEXT" in prompt
    finally:
        re_mod._SEAM_AWARE_ENABLED = False


def test_retry_prompt_carries_the_seam_note(monkeypatch):
    """Seam v2 (S28-227/229): the note rides the FRESH-GEN retry carrier
    too — the python_syntax stall family retries here, never reaching
    the targeted repair path (pilot6: zero carriers for the note)."""
    from capybase.resolution_engine import build_retry_prompt
    from capybase.conflict_model import ContextBundle
    re_mod._SEAM_AWARE_ENABLED = True
    try:
        unit = _unit("cpp")
        ctx = ContextBundle(primary_text="x")
        f = SimpleNamespace(
            message="y.cpp:2:1: error: expected unqualified-id before 'for'",
            validator="compile", detail={})
        prompt = build_retry_prompt(
            unit, ctx, [f], None,
            seam_candidate_text=(
                "namespace ns {\n"
                "for (;;) { }\n"
                "}\n"))
        assert "SEAM CONTEXT" in prompt
    finally:
        re_mod._SEAM_AWARE_ENABLED = False

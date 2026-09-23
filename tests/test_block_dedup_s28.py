"""S28-206 — the identical-block dedup rung (pilot-gated).

fmt-0003's splice echo (a cross-era fixed point): TEST(ChronoTest,
InvalidWidthId) at lines 299 AND 304, byte-identical, surviving every
machinery change — gtest's macro expansion reports "redefinition of
class ..._Test" on sim-1.00 content. The rung removes the later copy
of any non-adjacent byte-identical >=3-line block under a
redefinition-class failure; the whole-file gate revalidates.
"""

from __future__ import annotations

from types import SimpleNamespace

from capybase.orchestrator import _try_identical_block_dedup


def _unit():
    import capybase.orchestrator as orch_mod
    from capybase.conflict_model import ConflictSide, ConflictUnit
    return ConflictUnit(
        session_id="s", step_index=0, path="t.cc", language="cpp",
        conflict_type="UU", unit_id="t.cc:1:0", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="x\n"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="x\n"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="x\n"),
        original_worktree_text="x\n", marker_span=(0, 0),
    )


def _fail(msg):
    return [SimpleNamespace(message=msg)]


def _accepted():
    return [(_unit(), SimpleNamespace(resolved_text="x",
                                      candidate_id="t.cc:1:0:llm"))]


_ECHO = (
    "int a() { return 1; }\n"
    "\n"
    "TEST(ChronoTest, InvalidWidthId) {\n"
    "  EXPECT_THROW(x);\n"
    "}\n"
    "\n"
    "TEST(ChronoTest, InvalidWidthId) {\n"
    "  EXPECT_THROW(x);\n"
    "}\n"
    "\n"
    "int z() { return 3; }\n"
)


def test_dedup_fires_on_redefinition_with_echo(monkeypatch):
    import capybase.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "_resolved_buffer",
                        lambda original, accepted: _ECHO)
    det, diag = _try_identical_block_dedup(
        _fail("t.cc:304:6: error: redefinition of 'class ChronoTest_InvalidWidthId_Test'"),
        "orig", _accepted())
    assert det is not None and diag == "deduped"
    text = det[0][1].resolved_text
    assert text.count("TEST(ChronoTest, InvalidWidthId)") == 1
    assert text.count("EXPECT_THROW(x);") == 1
    assert "int z() { return 3; }" in text  # the tail survives


def test_no_redefinition_class_declines(monkeypatch):
    import capybase.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "_resolved_buffer",
                        lambda original, accepted: _ECHO)
    det, diag = _try_identical_block_dedup(
        _fail("t.cc:9:1: error: expected ';'"), "orig", _accepted())
    assert det is None and diag == "not_redefinition"


def test_no_duplicate_declines(monkeypatch):
    import capybase.orchestrator as orch_mod
    monkeypatch.setattr(
        orch_mod, "_resolved_buffer",
        lambda original, accepted: "int a(){return 1;}\nint b(){return 2;}\n")
    det, diag = _try_identical_block_dedup(
        _fail("t.cc:1:1: error: redefinition of 'x'"), "orig", _accepted())
    assert det is None and diag == "no_duplicate_block"


def test_blank_run_not_deduped(monkeypatch):
    import capybase.orchestrator as orch_mod
    monkeypatch.setattr(
        orch_mod, "_resolved_buffer",
        lambda original, accepted: "int a(){return 1;}\n\n\n\n\nint b(){return 2;}\n")
    det, diag = _try_identical_block_dedup(
        _fail("t.cc:1:1: error: redefinition of 'x'"), "orig", _accepted())
    assert det is None  # blank-only repeats are legitimate spacing


def test_adjacent_overlap_not_deduped(monkeypatch):
    import capybase.orchestrator as orch_mod
    # two identical 2-line blocks separated by < block size -> size-3
    # window sees no non-adjacent identical triple; nothing fires
    monkeypatch.setattr(
        orch_mod, "_resolved_buffer",
        lambda original, accepted: "a\nb\nc\na\nb\nc\n")
    det, diag = _try_identical_block_dedup(
        _fail("t.cc:1:1: error: redefinition of 'x'"), "orig", _accepted())
    # abc/abc IS a non-adjacent 3-line identical block -> fires
    assert det is not None
    assert det[0][1].resolved_text == "a\nb\nc\n"

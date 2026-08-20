"""Sprint-20 S20.9 — intelligent prompt compaction (context-only stripping).

When the assembled prompt overflows the window, strip full-line comments
and blank-run padding from the LARGE context sections (structural
anchor, dependency snippets, surrounding text) BEFORE the drop cascade —
more semantic signal per token. The conflict sides, contract, and
skeleton are never touched.
"""

from __future__ import annotations

from types import SimpleNamespace

from capybase.resolution_engine import (
    _compact_context_text,
    _fit_to_budget,
    estimate_tokens,
)


class TestCompactor:
    def test_strips_full_line_comments_cpp(self):
        text = (
            "// leading comment\n"
            "int x = 1;\n"
            "\n"
            "\n"
            "/* block\n"
            "   continues */\n"
            "int y = 2; // trailing inline stays\n"
            "  * continuation-style comment\n"
            "int z = 3;\n"
        )
        out = _compact_context_text(text, "cpp")
        lines = out.split("\n")
        assert "int x = 1;" in lines
        assert "int y = 2; // trailing inline stays" in lines
        assert "int z = 3;" in lines
        assert not any(ln.strip().startswith(("//", "/*", "*")) for ln in lines)
        # blank runs collapse to a single blank
        assert out.count("\n\n\n") == 0

    def test_strips_hash_comments_python(self):
        text = "# comment\nvalue = 1\n\n\nother = 2\n"
        out = _compact_context_text(text, "python")
        assert "# comment" not in out
        assert "value = 1" in out and "other = 2" in out

    def test_preserves_trailing_newlines_and_passthrough(self):
        assert _compact_context_text("", "cpp") == ""
        text = "a\n\n\nb\n\n"
        out = _compact_context_text(text, "cpp")
        assert out.endswith("\n\n")  # trailing newline count preserved
        assert "a" in out and "b" in out

    def test_code_lines_verbatim(self):
        text = "  if (x) {\n    return /* inline */ y;\n  }\n"
        out = _compact_context_text(text, "cpp")
        assert "    return /* inline */ y;" in out  # inline comments stay


def _unit():
    return SimpleNamespace(
        original_worktree_text="", base=SimpleNamespace(text=""),
        language="cpp", structural_metadata={})


def _budget(available: int, total: int = 8192):
    return SimpleNamespace(enabled=True, available=available, total=total)


_ANNOTATED_ANCHOR = (
    "Logical block you are merging inside (structural parse):\n"
    "void big() {\n"
    + "".join(f"    // padding comment line {i} with words words words\n" for i in range(40))
    + "    int code_line = 1;\n"
    + "\n" * 5
    + "}\n\n"
)


class TestBudgetIntegration:
    def test_compaction_fires_and_saves_the_anchor(self):
        anchor_full = estimate_tokens(_ANNOTATED_ANCHOR)
        anchor_compact = estimate_tokens(_compact_context_text(_ANNOTATED_ANCHOR, "cpp"))
        assert anchor_compact < anchor_full * 0.5  # the fixture is comment-heavy
        essential = estimate_tokens("CURRENT\nx = 1\n=======\ny = 2\n")
        overhead = estimate_tokens("intro contract rules")
        # budget between the compacted and full totals: without compaction
        # the cascade would DROP the anchor; with it, the anchor survives.
        avail = overhead + essential + (anchor_compact + anchor_full) // 2
        anchor, _sib, _deps, _fs, _pt, _hist, _obl, trims, _skel = _fit_to_budget(
            budget=_budget(avail),
            intro="intro", contract="contract", rules="rules",
            sides_text="CURRENT\nx = 1\n=======\ny = 2\n",
            structural_anchor=_ANNOTATED_ANCHOR,
            siblings_block="", deps="", few_shot="", primary_text="",
            unit=_unit(), history="", obligations="",
        )
        compaction = [t for t in trims if t["section"] == "compaction"]
        assert compaction, trims
        assert anchor and "code_line" in anchor  # survived, comments gone
        assert "// padding comment" not in anchor

    def test_no_overflow_no_compaction(self):
        _a, _s, _d, _f, _p, _h, _o, trims, _sk = _fit_to_budget(
            budget=_budget(10 ** 6),
            intro="intro", contract="contract", rules="rules",
            sides_text="x\n",
            structural_anchor=_ANNOTATED_ANCHOR,
            siblings_block="", deps="", few_shot="", primary_text="",
            unit=_unit(), history="", obligations="",
        )
        assert not [t for t in trims if t["section"] == "compaction"]

    def test_budget_disabled_passthrough(self):
        _a, _s, _d, _f, _p, _h, _o, trims, _sk = _fit_to_budget(
            budget=SimpleNamespace(enabled=False, available=0, total=0),
            intro="i", contract="c", rules="r", sides_text="x",
            structural_anchor=_ANNOTATED_ANCHOR,
            siblings_block="", deps="", few_shot="", primary_text="",
            unit=_unit(), history="", obligations="",
        )
        assert not trims

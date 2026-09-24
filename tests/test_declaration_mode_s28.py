"""S28-197 — the local-declaration injection mode (pilot-gated).

The tokenizer family's fixed points (0126/0128/0130 + parser_cache/rule):
the pristine sides carry the missing local's declaring line verbatim
(`Tokenizer tokenizer(behavior, keyword_helper);` — replayed:376), the
model's splice dropped it, and the rung's line_replace mode whack-a-moles
34/34 (each use-site swap renames the missing symbol). The mode inserts
the side's declaring line before the symbol's first use; the whole-file
compile gate owns the verdict. Flag-gated default OFF.
"""

from __future__ import annotations

from types import SimpleNamespace

from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.orchestrator import Orchestrator
from capybase.verification import inject_local_declaration


class _RecJournal:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, payload, **_kw):
        self.events.append((event, payload))


def _unit():
    return ConflictUnit(
        session_id="s", step_index=0, path="src/p.cpp", language="cpp",
        conflict_type="UU", unit_id="src/p.cpp:1:0",
        unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="int main(){\n}\n"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE",
                             text="int main(){\n  tokenizer.TokenizeInput();\n}\n"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE",
                              text="int main(){\n  Tokenizer tokenizer(behavior, keyword_helper);\n  tokenizer.TokenizeInput();\n}\n"),
        original_worktree_text="", marker_span=(0, 0),
    )


# ---------------------------------------------------------------------------
# the local-placement helper
# ---------------------------------------------------------------------------

def test_inserts_before_first_use():
    buf = "int main(){\n  tokenizer.TokenizeInput();\n}\n"
    decl = "Tokenizer tokenizer(behavior, keyword_helper);"
    out = inject_local_declaration(buf, decl, "tokenizer")
    lines = out.splitlines()
    assert lines[1].strip() == decl
    assert lines[2].strip().startswith("tokenizer.TokenizeInput")


def test_already_present_dedupes():
    decl = "Tokenizer tokenizer(behavior, keyword_helper);"
    buf = f"int main(){{\n  {decl}\n  tokenizer.TokenizeInput();\n}}\n"
    assert inject_local_declaration(buf, decl, "tokenizer") is None


def test_no_use_site_returns_none():
    assert inject_local_declaration(
        "int main(){\n}\n",
        "Tokenizer tokenizer(behavior, keyword_helper);", "tokenizer") is None


def test_malformed_decl_returns_none():
    assert inject_local_declaration(
        "int main(){\n  tokenizer.x();\n}\n", "", "tokenizer") is None
    assert inject_local_declaration(
        "int main(){\n  tokenizer.x();\n}\n", "a\nb", "tokenizer") is None


# ---------------------------------------------------------------------------
# the rung's flag-gated mode
# ---------------------------------------------------------------------------

def _orch(*, flag: bool, sides):
    orch = Orchestrator.__new__(Orchestrator)
    orch.journal = _RecJournal()
    orch.step = 1
    orch.config = SimpleNamespace(future=SimpleNamespace(
        enable_declaration_restoration=flag))
    orch._micro_stage_sides = lambda path: (sides, "")
    return orch


SIDES = {
    "current": "int main(){\n  cache.GetTokenizer().TokenizeInput(behavior);\n}\n",
    "replayed": ("int main(){\n"
                 "  Tokenizer tokenizer(behavior, keyword_helper);\n"
                 "  tokenizer.TokenizeInput();\n}\n"),
}


def _failures(msg="src/p.cpp:2:3: error: 'tokenizer' was not declared in this scope"):
    return [SimpleNamespace(message=msg)]


def _accepted():
    cand = SimpleNamespace(
        resolved_text="int main(){\n  tokenizer.TokenizeInput();\n}\n",
        candidate_id="src/p.cpp:1:0:llm")
    return [(_unit(), cand)]


def test_declaration_mode_fires_under_flag(monkeypatch):
    import capybase.orchestrator as orch_mod
    orch = _orch(flag=True, sides=SIDES)
    monkeypatch.setattr(
        orch_mod, "_resolved_buffer",
        lambda original, accepted: "int main(){\n  tokenizer.TokenizeInput();\n}\n")
    out = orch._try_symbol_injection_repair(
        "src/p.cpp", "orig", _accepted(), _failures(), 0)
    assert out is not None
    repaired = out[0][1].resolved_text
    assert "Tokenizer tokenizer(behavior, keyword_helper);" in repaired
    assert (repaired.index("Tokenizer tokenizer(")
            < repaired.index("tokenizer.TokenizeInput"))
    kinds = [p.get("kind") for e, p in orch.journal.events
             if e == "symbol_inject_applied"]
    assert kinds == ["declaration_local"]


def test_flag_off_keeps_legacy_path(monkeypatch):
    import capybase.orchestrator as orch_mod
    orch = _orch(flag=False, sides=SIDES)
    monkeypatch.setattr(
        orch_mod, "_resolved_buffer",
        lambda original, accepted: "int main(){\n  tokenizer.TokenizeInput();\n}\n")
    out = orch._try_symbol_injection_repair(
        "src/p.cpp", "orig", _accepted(), _failures(), 0)
    kinds = [p.get("kind") for e, p in orch.journal.events
             if e == "symbol_inject_applied"]
    assert "declaration_local" not in kinds
    if out is not None:
        assert out[0][1].prompt_version != "deterministic_local_declaration"


def test_not_declared_class_skips_line_replace_under_flag(monkeypatch):
    """Under the flag a not-declared failure never takes the use-site
    swap — the whack-a-mole mode (S28-197's core correction)."""
    import capybase.orchestrator as orch_mod
    orch = _orch(flag=True, sides=SIDES)
    monkeypatch.setattr(
        orch_mod, "_resolved_buffer",
        lambda original, accepted: "int main(){\n  tokenizer.TokenizeInput();\n}\n")
    orch._try_symbol_injection_repair(
        "src/p.cpp", "orig", _accepted(), _failures(), 0)
    kinds = [p.get("kind") for e, p in orch.journal.events
             if e == "symbol_inject_applied"]
    assert "line_replace" not in kinds


# ---------------------------------------------------------------------------
# S28-197 v2: the hoist variant
# ---------------------------------------------------------------------------

def test_hoist_moves_decl_above_use():
    from capybase.verification import hoist_local_declaration
    buf = ("int main(){\n"
           "  tokenizer.TokenizeInput();\n"
           "  Tokenizer tokenizer(behavior);\n"
           "}\n")
    out = hoist_local_declaration(buf, "tokenizer")
    lines = out.splitlines()
    decl_i = next(i for i, l in enumerate(lines) if "Tokenizer tokenizer" in l)
    use_i = next(i for i, l in enumerate(lines) if "TokenizeInput" in l)
    assert decl_i < use_i
    assert len(lines) == 4  # a move: line count unchanged


def test_hoist_declines_when_ordered_or_absent():
    from capybase.verification import hoist_local_declaration
    ordered = ("int main(){\n"
               "  Tokenizer tokenizer(behavior);\n"
               "  tokenizer.TokenizeInput();\n}\n")
    assert hoist_local_declaration(ordered, "tokenizer") is None
    assert hoist_local_declaration("int main(){\n}\n", "tokenizer") is None


# ---------------------------------------------------------------------------
# S28-197 v3: the block transplant
# ---------------------------------------------------------------------------

def test_block_insert_moves_the_whole_block():
    from capybase.verification import inject_local_block
    buf = "int main(){\n  state.Match();\n}\n"
    block = ["auto tokens = lex();",
             "MatchState state(tokens, suggestions);"]
    out = inject_local_block(buf, block, "state")
    lines = out.splitlines()
    assert lines[1].strip() == "auto tokens = lex();"
    assert lines[2].strip() == "MatchState state(tokens, suggestions);"
    assert lines[3].strip() == "state.Match();"


def test_block_already_present_dedupes():
    from capybase.verification import inject_local_block
    block = ["auto tokens = lex();",
             "MatchState state(tokens, suggestions);"]
    buf = "int main(){\n" + "\n".join(block) + "\n  state.Match();\n}\n"
    assert inject_local_block(buf, block, "state") is None


def test_expand_block_collects_referenced_locals():
    from capybase.verification import expand_declaration_block
    side = ("int main(){\n"
            "  auto tokens = TokenizeAll();\n"
            "  auto suggestions = allocator.Make();\n"
            "  MatchState state(tokens, suggestions);\n"
            "  state.Match();\n"
            "}\n")
    block = expand_declaration_block(side, "MatchState state(tokens, suggestions);", "state")
    assert any("auto tokens" in ln for ln in block)
    assert any("auto suggestions" in ln for ln in block)
    assert len(block) <= 5


def test_expand_block_bounded_and_terminates():
    from capybase.verification import expand_declaration_block
    side = "\n".join(f"auto v{i} = f(v{i-1});" for i in range(20))
    side += "\nMatchState state(v19);\n"
    block = expand_declaration_block(side, "MatchState state(v19);", "state")
    assert len(block) <= 5  # bounded despite the chain


def test_expand_window_reaches_the_member_block():
    """The 0127 pilot read: the referenced locals sit as the enclosing
    scope's member block 8-11 lines ABOVE the use — the ±5 window
    missed them (block_lines=1). The ±12 window reaches them."""
    from capybase.verification import expand_declaration_block
    lines = ["struct C {"]
    lines += [f"  int filler{i};" for i in range(8)]
    lines += [
        "  vector<MatcherSuggestion> suggestions;",
        "  ParseResultAllocator parse_allocator;",
        "  idx_t max_token_index = 0;",
        "  TokenIterator token_iterator(tokens);",
        "  MatchState state(token_iterator, suggestions, parse_allocator, max_token_index);",
    ]
    side = "\n".join(lines)
    block = expand_declaration_block(
        side, "MatchState state(token_iterator, suggestions, parse_allocator, max_token_index);", "state")
    assert any("suggestions;" in ln for ln in block)
    assert any("parse_allocator;" in ln for ln in block)
    assert any("max_token_index" in ln for ln in block)
    assert len(block) <= 5


def test_hoist_declines_cross_function():
    """The pilot2 0126 read: `auto& cache` in an EARLIER function must
    neither hoist (it would break the donor) nor read as 'ordered'."""
    from capybase.verification import hoist_local_declaration
    buf = ("int a(){\n"
           "  auto &cache = GetCache();\n"
           "  return cache.x;\n"
           "}\n"
           "\n"
           "int b(){\n"
           "  cache.TokenizeInput();\n"
           "  return 0;\n"
           "}\n")
    # the use is AFTER the decl in file order but in another function:
    # the hoist declines (not 'ordered' — the use's scope needs its own)
    assert hoist_local_declaration(buf, "cache") is None


def test_hoist_fires_same_function_only():
    from capybase.verification import hoist_local_declaration
    buf = ("int b(){\n"
           "  cache.TokenizeInput();\n"
           "  auto &cache = GetCache();\n"
           "  return cache.x;\n"
           "}\n")
    out = hoist_local_declaration(buf, "cache")
    assert out is not None
    assert out.splitlines()[1].strip() == "auto &cache = GetCache();"


def test_same_function_boundary_detection():
    from capybase.verification import _same_function
    lines = ["int a(){", "  int x;", "}", "", "int b(){", "  int y;", "}"]
    assert _same_function(lines, 1, 1) is True
    assert _same_function(lines, 1, 5) is False  # crossed the boundary

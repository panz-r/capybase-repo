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


# ---------------------------------------------------------------------------
# S28-197 v3.4: the pilot4 reads — the suffix false positive, the
# namespace-blind scope test, failing-use anchoring, the artifact split
# ---------------------------------------------------------------------------

def test_decl_shape_rejects_suffix_collisions():
    """pilot4 0126: `local_cache = make_uniq<ParserCache>();` matched as
    a declaration of `cache` (the type token absorbed the `local_`
    prefix), giving the guard a false _existing."""
    from capybase.verification import _declaration_line_idx
    lines = ["\tlocal_cache = make_uniq<ParserCache>();",
             "\tParserCache cache;",
             "\tauto &cache = GetCache();"]
    assert _declaration_line_idx(lines, "cache") == 1
    assert _declaration_line_idx(lines[:1], "cache") is None


def test_same_function_namespace_aware():
    """pilot4 0126: the whole file sits inside `namespace duckdb {}` —
    depth never returns to 0, so the s27 depth-0 test called ANY two
    lines same-function (line 34 vs 375) and the guard skipped."""
    from capybase.verification import _same_function
    ns = ["namespace duckdb {",
          "void a() {",
          "  auto &cache = GetCache();",
          "}",
          "void b() {",
          "  cache.GetTokenizer();",
          "}",
          "}"]
    assert _same_function(ns, 2, 5) is False   # different functions
    assert _same_function(ns, 4, 5) is True    # same function
    assert _same_function(ns, 0, 5) is True    # namespace scope: visible


def test_same_function_block_visibility():
    """The enclosing-depth drop IS C++ visibility: a local declared in an
    if-block is out of scope once the block closes."""
    from capybase.verification import _same_function
    src = ["void f() {",
           "  if (x) {",
           "    Tokenizer t(a, b);",
           "  }",
           "  t.use();",
           "}"]
    assert _same_function(src, 2, 4) is False  # block closed in between
    assert _same_function(src, 1, 4) is True   # function-level: visible


def test_hint_anchors_at_the_failing_use():
    """pilot4 0126: first-use anchoring put the insert in the DONOR
    function (its own `cache` reference at line 43); the failing use
    sat at 375. The hint pins the insert to the failing scope."""
    buf = ("void donor() {\n"
           "  auto &cache = GetCache();\n"
           "  cache.GetMatcher();\n"
           "}\n"
           "void failing() {\n"
           "  cache.GetTokenizer();\n"
           "}\n")
    out = inject_local_declaration(
        buf, "ParserCache cache;", "cache", use_idx_hint=5)
    lines = out.splitlines()
    assert lines[5].strip() == "ParserCache cache;"
    assert lines[6].strip().startswith("cache.GetTokenizer")


def test_hint_ignored_when_line_lacks_the_symbol():
    buf = "int main() {\n  tokenizer.x();\n}\n"
    out = inject_local_declaration(
        buf, "Tokenizer t(a);", "tokenizer", use_idx_hint=0)
    assert out is not None
    assert out.splitlines()[1].startswith("Tokenizer")


def test_block_insert_hint_anchors_at_the_failing_use():
    from capybase.verification import inject_local_block
    buf = ("void donor() {\n"
           "  auto &cache = GetCache();\n"
           "}\n"
           "void failing() {\n"
           "  cache.x();\n"
           "}\n")
    out = inject_local_block(
        buf, ["ParserCache cache;", "Helper helper;"], "cache",
        use_idx_hint=4)
    lines = out.splitlines()
    assert lines[4].startswith("ParserCache cache;")
    assert lines[5].strip() == "Helper helper;"


ARTIFACT_SIDES = {
    "current": "int other() {\n  phantom2.Symbol();\n}\n",
    "replayed": "int other2() {\n  int x = 4;\n}\n",
}


def test_artifact_use_line_falls_to_line_replace(monkeypatch):
    """v3.4 split: a failing use line that exists in NO side is a splice
    artifact — the declaration family declines (no declaration can fix
    an invented use site) and line_replace owns the round."""
    import capybase.orchestrator as orch_mod
    orch = _orch(flag=True, sides=ARTIFACT_SIDES)
    buf = "int main(){\n  phantom.Symbol();\n}\n"
    monkeypatch.setattr(orch_mod, "_resolved_buffer",
                        lambda original, accepted: buf)
    orch._try_symbol_injection_repair(
        "src/p.cpp", "orig", _accepted(),
        _failures("src/p.cpp:2:3: error: 'phantom' was not declared in this scope"),
        0)
    kinds = [p.get("kind") for e, p in orch.journal.events
             if e == "symbol_inject_applied"]
    assert "declaration_local" not in kinds
    assert "declaration_hoist" not in kinds
    assert "line_replace" in kinds


def test_verbatim_use_keeps_the_declaration_class(monkeypatch):
    """The complementary half of the split: a failing use line that IS
    verbatim in a side (the 0127 family) keeps v3.3 semantics —
    declaration insert, no line_replace."""
    import capybase.orchestrator as orch_mod
    orch = _orch(flag=True, sides=SIDES)
    monkeypatch.setattr(
        orch_mod, "_resolved_buffer",
        lambda original, accepted: "int main(){\n  tokenizer.TokenizeInput();\n}\n")
    orch._try_symbol_injection_repair(
        "src/p.cpp", "orig", _accepted(), _failures(), 0)
    kinds = [p.get("kind") for e, p in orch.journal.events
             if e == "symbol_inject_applied"]
    assert "declaration_local" in kinds
    assert "line_replace" not in kinds


# ---------------------------------------------------------------------------
# S28-197 v3.5: the pilot4 completion reads — the anchor dropped from the
# block, the donor-function presence blocking the transplant
# ---------------------------------------------------------------------------

SIDE_0127 = """
void donor(vector<MatcherToken> &tokens) {
}
vector<AutoCompleteSuggestion> Generate(AutoCompleteCatalogProvider &provider) {
\tauto &parser_cache = provider.GetParserCache();
\tvector<MatcherToken> tokens;
\tvector<MatcherSuggestion> suggestions;
\tParseResultAllocator parse_allocator;
\tidx_t max_token_index = 0;
\tMatchState state(tokens, suggestions, parse_allocator, max_token_index);
\tvector<UnicodeSpace> unicode_spaces;
\tAutoCompleteTokenizerBehavior behavior(sql, state);
\ttokenizer.TokenizeInput();
}
"""

BUFFER_0127 = """void OnLastToken(const Tokenizer &t, TokenizeState state, string w) {
}
vector<AutoCompleteSuggestion> Generate(AutoCompleteCatalogProvider &provider) {
\tauto &parser_cache = provider.GetParserCache();
\tvector<MatcherToken> tokens;
\tvector<MatcherSuggestion> suggestions;
\tParseResultAllocator parse_allocator;
\tvector<UnicodeSpace> unicode_spaces;
\tstring clean_sql;
\tconst string &sql_ref = sql;
\tAutoCompleteTokenizerBehavior behavior(sql_ref, state);
\ttokenizer.TokenizeInput();
}
"""


def test_expand_keeps_the_anchor_and_stays_contiguous():
    """pilot4 0127: the v3.1 walk rebuilt the block by side membership,
    and the stripped anchor never matched its own tab-prefixed side
    line — the shipped 'block' was 4 generic locals with NO symbol
    declaration. A same-text local in a donor function (line 3 here)
    must also stay OUT (contiguity)."""
    from capybase.verification import expand_declaration_block
    decl = ("MatchState state(tokens, suggestions, parse_allocator, "
            "max_token_index);")
    block = expand_declaration_block(SIDE_0127, decl, "state")
    # side order: referenced locals first, the anchor LAST (its ctor
    # arguments need the locals declared first) — raw, tab-prefixed.
    assert block[-1].strip() == decl
    assert block[-1].startswith("\t")
    assert any("max_token_index = 0" in b for b in block)
    assert len(block) == 5                     # contiguous 199..203 shape
    assert all("donor" not in b for b in block)


def test_transplant_not_blocked_by_donor_generic_local():
    """pilot4 0127: the v3 presence guard keyed on block_lines[0] — the
    first GENERIC local, which the buffer's donor function already had
    — so the whole transplant returned None and the rung fell through
    to derived prototypes. v3.5 keys on the anchor, scope-aware."""
    from capybase.verification import expand_declaration_block, inject_local_block
    decl = ("MatchState state(tokens, suggestions, parse_allocator, "
            "max_token_index);")
    block = expand_declaration_block(SIDE_0127, decl, "state")
    out = inject_local_block(
        BUFFER_0127, block, "state", use_idx_hint=10)
    assert out is not None
    nl = out.splitlines()
    # the 5-line block sits before the failing use (0-based 10):
    # four locals then the anchor, then the use line
    assert nl[14].strip() == decl
    assert nl[15].strip().startswith("AutoCompleteTokenizerBehavior")
    # the donor copies remain; the transplant added the missing pair
    assert sum(1 for ln in nl if "vector<MatcherToken> tokens;" in ln) == 2
    assert sum(1 for ln in nl if ln.strip().startswith("MatchState state(")) == 1


def test_transplant_blocked_by_same_scope_anchor():
    """The complementary pin: when the use's OWN scope already declares
    the symbol, the transplant must still decline (no duplicates)."""
    from capybase.verification import expand_declaration_block, inject_local_block
    decl = ("MatchState state(tokens, suggestions, parse_allocator, "
            "max_token_index);")
    block = expand_declaration_block(SIDE_0127, decl, "state")
    buf = BUFFER_0127.replace(
        "\tvector<UnicodeSpace> unicode_spaces;",
        "\tvector<UnicodeSpace> unicode_spaces;\n\t" + decl)
    assert inject_local_block(buf, block, "state", use_idx_hint=11) is None


def test_single_line_dedup_scope_aware():
    """inject_local_declaration's dedup had the same buffer-global
    flaw: an identical line in a DONOR function returned None (the
    insert never happened). Only a same-scope copy declines now."""
    buf = ("void donor() {\n"
           "  ParserCache cache;\n"
           "  cache.warm();\n"
           "}\n"
           "void failing() {\n"
           "  cache.GetTokenizer();\n"
           "}\n")
    out = inject_local_declaration(buf, "ParserCache cache;", "cache",
                                   use_idx_hint=5)
    assert out is not None
    assert out.splitlines()[5].strip() == "ParserCache cache;"
    same_scope = ("void failing() {\n"
                  "  ParserCache cache;\n"
                  "  cache.GetTokenizer();\n"
                  "}\n")
    assert inject_local_declaration(
        same_scope, "ParserCache cache;", "cache", use_idx_hint=2) is None

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

"""Sprint 18 WS1: the C/C++ micro-repair loop.

Three mechanisms targeting the sim≈1.0-but-build-broken class
(protobuf-0055/0065, fmt-0003 — oracle builds, candidate doesn't, token
similarity can't see the defect):

1. **Phase-2 full-build fallback** — datasets without per-object Makefile
   rules (protobuf, fmt, json-c) had NO tests.required-independent build
   gate; build-broken merges shipped silently.
2. **Duplicate-definition eradication** — gcc "redefinition of X" means the
   spliced file carries X twice (kept the pre-merge copy AND emitted the new
   one). Deletes exactly one region, provably safe.
3. **Micro-LLM patch** — when the CEGIS re-resolve can't fit the window
   (protobuf-0055: 15.5K tokens vs 8K), the build error still localizes the
   defect to ±10 lines; patch just those.
"""

from __future__ import annotations

from capybase.conflict_model import (
    CandidateResolution,
    ConflictSide,
    ConflictUnit,
    VerificationFailure,
)


# ---------------------------------------------------------------------------
# Phase-2 full-build fallback command selection
# ---------------------------------------------------------------------------

def test_fallback_accepts_make_and_cmake_builds():
    from capybase.orchestrator import _phase2_fallback_build_cmd
    assert _phase2_fallback_build_cmd("make -j4") == "make -j4"
    assert _phase2_fallback_build_cmd("cmake --build build") == "cmake --build build"
    # Compound command recognized by its build words.
    assert _phase2_fallback_build_cmd("./configure && make -j4") == "./configure && make -j4"


def test_fallback_rejects_non_builds():
    from capybase.orchestrator import _phase2_fallback_build_cmd
    assert _phase2_fallback_build_cmd("true") == ""
    assert _phase2_fallback_build_cmd("") == ""
    assert _phase2_fallback_build_cmd("python3 -m py_compile app.py") == ""
    assert _phase2_fallback_build_cmd("pytest tests/") == ""


def test_fallback_respects_disable_flag():
    from capybase.orchestrator import _phase2_fallback_build_cmd
    assert _phase2_fallback_build_cmd("make -j4", enabled=False) == ""


def test_fallback_flag_configured_on_validation():
    from capybase.config import Config
    cfg = Config()
    assert cfg.validation.cc_phase2_full_build_fallback is True


# ---------------------------------------------------------------------------
# Duplicate-definition eradication: region finding
# ---------------------------------------------------------------------------

def _cpp_unit(base_text: str, cur_text: str, rep_text: str,
              marker_span=(1, 3)) -> ConflictUnit:
    return ConflictUnit(
        session_id="s", step_index=0, path="a.cc", language="cpp",
        conflict_type="UU", unit_id="a.cc:1:0", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base_text),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=cur_text),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=rep_text),
        original_worktree_text=base_text, marker_span=marker_span,
    )


def _cand(unit: ConflictUnit, text: str) -> CandidateResolution:
    return CandidateResolution(
        candidate_id=f"{unit.unit_id}:c1", unit_id=unit.unit_id,
        model_name="fake", prompt_version="t", resolved_text=text,
    )


def test_regions_finds_two_function_definitions():
    from capybase.orchestrator import _dup_eradication_regions
    code = (
        "int helper(int x) {\n"        # 0: def 1
        "    return x + 1;\n"          # 1
        "}\n"                          # 2
        "void other() {\n"             # 3
        "    helper(3);\n"             # 4: call site — NOT a region
        "}\n"                          # 5
        "int helper(int x) {\n"        # 6: def 2 (duplicate)
        "    return x + 1;\n"          # 7
        "}\n"                          # 8
    )
    lines = code.split("\n")
    regions = _dup_eradication_regions(lines, "helper")
    assert regions == [(0, 2), (6, 8)], f"got {regions}"


def test_regions_ignores_calls_and_declarations():
    from capybase.orchestrator import _dup_eradication_regions
    code = (
        "int helper(int x);\n"         # 0: forward declaration — not a region
        "void caller() {\n"            # 1
        "    int y = helper(1);\n"     # 2: use — not a region
        "}\n"                          # 3
        "int helper(int x) {\n"        # 4: the only definition
        "    return x;\n}\n"
    )
    lines = code.split("\n")
    assert _dup_eradication_regions(lines, "helper") == [(4, 6)]


def test_regions_finds_variable_definitions():
    from capybase.orchestrator import _dup_eradication_regions
    code = (
        "const int kMax = 10;\n"       # 0
        "int other = kMax;\n"          # 1: USE — not a definition of kMax
        "const int kMax = 10;\n"       # 2: duplicate definition
    )
    lines = code.split("\n")
    assert _dup_eradication_regions(lines, "kMax") == [(0, 0), (2, 2)]


# ---------------------------------------------------------------------------
# Duplicate-definition eradication: the repair
# ---------------------------------------------------------------------------

def _redef_failure(line: int, entity: str) -> VerificationFailure:
    return VerificationFailure(
        validator="build_test", severity="error",
        message=f"a.cc:{line}:5: error: redefinition of '{entity}'",
    )


def test_eradication_removes_identical_duplicate():
    from capybase.orchestrator import _try_duplicate_eradication_repair
    base = "int helper(int x) {\n    return x + 1;\n}\n"
    # The spliced buffer kept the base copy AND emitted an identical new one.
    dup = (
        "int helper(int x) {\n    return x + 1;\n}\n"
        "int helper(int x) {\n    return x + 1;\n}\n"
    )
    # marker_span (0, 2): the whole base file is the conflict block, the
    # resolution replaced it with the duplicated buffer.
    unit = _cpp_unit(base, "int helper(int x) {\n    return x + 1;\n}",
                     "int helper(int x) {\n    return x + 1;\n}",
                     marker_span=(0, 2))
    out = _try_duplicate_eradication_repair(
        [_redef_failure(4, "helper")], base, [(unit, _cand(unit, dup))], 0)
    assert out is not None
    text = out[0][1].resolved_text
    assert text.count("int helper(int x)") == 1
    assert out[0][1].provenance == "deterministic_dup_eradication"


def test_eradication_removes_the_pre_merge_copy_not_the_new_definition():
    from capybase.orchestrator import _try_duplicate_eradication_repair, \
        _resolved_buffer
    base = (
        "int helper(int x) {\n    return x + 1;\n}\n"
        "void tail() {}\n"
    )
    # The merge kept base's helper (old semantics) AND emitted replayed's
    # NEW helper body (fixed semantics). The pre-merge copy is the one to
    # drop — the freshly generated definition is the merge's intent.
    spliced = (
        "int helper(int x) {\n    return x + 1;\n}\n"   # kept-base copy (in original)
        "int helper(int x) {\n    return x + 2;\n}\n"   # new definition
        "void tail() {}\n"
    )
    # Splice contract: replacing base line 2 ('}') with '} + the new def'
    # produces exactly `spliced`.
    unit = _cpp_unit(
        base, "}",
        "int helper(int x) {\n    return x + 2;\n}",
        marker_span=(2, 2),
    )
    cand = _cand(unit, "}\nint helper(int x) {\n    return x + 2;\n}")
    assert _resolved_buffer(base, [(unit, cand)]) == spliced  # splice sanity
    out = _try_duplicate_eradication_repair(
        [_redef_failure(4, "helper")], base, [(unit, cand)], 0)
    assert out is not None
    text = out[0][1].resolved_text
    assert "return x + 2;" in text, "the NEW definition must survive"
    assert "return x + 1;" not in text, "the kept-base copy must be removed"
    assert text.count("int helper(") == 1
    assert "void tail() {}" in text  # surrounding content untouched


def test_eradication_declines_when_neither_region_is_pre_merge_copy():
    from capybase.orchestrator import _try_duplicate_eradication_repair
    base = "void unrelated() {}\n"
    # Two DIFFERENT definitions, neither present in the pre-merge file —
    # could be an overload-like divergence; the LLM decides, not us.
    spliced = (
        "int helper(int x) {\n    return x + 1;\n}\n"
        "int helper(int x, int y) {\n    return x + y;\n}\n"
    )
    unit = _cpp_unit(base, "int helper(int x) {\n    return x + 1;\n}",
                     "int helper(int x, int y) {\n    return x + y;\n}")
    assert _try_duplicate_eradication_repair(
        [_redef_failure(4, "helper")], base, [(unit, _cand(unit, spliced))], 0
    ) is None


def test_eradication_declines_without_redefinition_error():
    from capybase.orchestrator import _try_duplicate_eradication_repair
    base = "int helper(int x) {\n    return x + 1;\n}\n"
    spliced = base + "int helper(int x) {\n    return x + 1;\n}\n"
    unit = _cpp_unit(base, base, base)
    other_error = VerificationFailure(
        validator="build_test", severity="error",
        message="a.cc:2:5: error: expected ';' before 'return'",
    )
    assert _try_duplicate_eradication_repair(
        [other_error], base, [(unit, _cand(unit, spliced))], 0) is None


def test_eradication_parses_gcc_quoted_signatures():
    """gcc quotes the full signature: "redefinition of 'int helper(int)'".
    The entity name is extracted from before the paren, type dropped."""
    from capybase.orchestrator import _try_duplicate_eradication_repair
    base = "int helper(int x) {\n    return x + 1;\n}\n"
    spliced = base + "int helper(int x) {\n    return x + 1;\n}\n"
    unit = _cpp_unit(base, base, base, marker_span=(0, 2))
    sig_failure = VerificationFailure(
        validator="build_test", severity="error",
        message="a.cc:4:5: error: redefinition of 'int helper(int)'",
    )
    out = _try_duplicate_eradication_repair(
        [sig_failure], base, [(unit, _cand(unit, spliced))], 0)
    assert out is not None
    assert out[0][1].resolved_text.count("int helper(") == 1


# ---------------------------------------------------------------------------
# Micro-LLM patch
# ---------------------------------------------------------------------------

def _orch_with_client(tmp_repo, client_responses: list[str]):
    """A minimal orchestrator on a scratch repo with a scripted engine client."""
    import json
    from capybase.adapters.llm_openai import LLMResponse
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator
    from capybase.resolution_engine import ResolutionEngine

    class _ScriptedClient:
        def __init__(self, responses):
            self._r = list(responses)
            self.calls = []

        def complete(self, messages, **kw):
            self.calls.append({"messages": messages, **kw})
            t = self._r.pop(0) if self._r else "{}"
            return LLMResponse(text=t)

    cfg = Config()
    cfg.model.model = "fake"
    client = _ScriptedClient(client_responses)
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(tmp_repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    return orch, client


def test_micro_patch_fixes_error_window(tmp_path):
    """A redefinition error in a big buffer: the micro patch sends only the
    ±10-line window and splices the corrected excerpt back."""
    import json
    import tempfile
    from pathlib import Path
    from capybase.orchestrator import _resolved_buffer
    from tests.conftest import git

    repo = Path(tempfile.mkdtemp())
    git(repo, "init", "-q", "-b", "main")

    # original: 8 filler lines, keepme at line 8 (0-based), 8 tail lines.
    filler_top = "\n".join(f"// filler line {i}" for i in range(1, 9))
    filler_bottom = "\n".join(f"// tail {i}" for i in range(1, 9))
    original = f"{filler_top}\nvoid keepme() {{}}\n{filler_bottom}\n"
    # The resolution duplicated a helper block at the keepme line: the
    # spliced buffer carries it twice (2nd def header at 1-based line 12).
    dup_block = (
        "int helper(int x) {\n    return x + 1;\n}\n"
        "int helper(int x) {\n    return x + 1;\n}\n"
    )
    spliced = f"{filler_top}\n{dup_block}void keepme() {{}}\n{filler_bottom}\n"
    err_line = 12
    unit = _cpp_unit(
        original, "void keepme() {}", dup_block, marker_span=(8, 8),
    )
    # The resolution re-states the original line AND adds the duplicated
    # block — splice(replace line 8) then equals `spliced`.
    cand = _cand(unit, dup_block + "void keepme() {}")
    assert _resolved_buffer(original, [(unit, cand)]) == spliced  # splice sanity
    failures = [VerificationFailure(
        validator="build_test", severity="error",
        message=f"a.cc:{err_line}:5: error: redefinition of 'int helper(int)'",
    )]
    # The model returns the corrected window (the whole 2nd definition —
    # header, body, closing brace — removed, keeping braces balanced).
    win_start, win_end = err_line - 11, err_line + 10
    window_lines = spliced.split("\n")[win_start:win_end]
    corrected = "\n".join(
        ln for i, ln in enumerate(window_lines)
        if not (err_line - 1 <= win_start + i <= err_line + 1)
    )
    orch, client = _orch_with_client(
        repo, [json.dumps({"resolved_text": corrected})])
    out = orch._micro_patch_repair("a.cc", original, [(unit, cand)], failures)
    assert out is not None
    patched = out[0][1].resolved_text
    assert patched != spliced
    assert "filler line 5" in patched           # window context preserved
    assert patched.startswith("// filler line 1")
    assert out[0][1].provenance == "micro_patch_repair"
    # The prompt was micro-sized and carried the error.
    sent = client.calls[0]["messages"][1]["content"]
    assert "redefinition" in sent
    assert "ERRORHERE" in sent
    assert len(sent) < 4000  # micro, not CEGIS-scale


def test_micro_patch_declines_without_error_line(tmp_path):
    import tempfile
    from pathlib import Path
    from tests.conftest import git

    repo = Path(tempfile.mkdtemp())
    git(repo, "init", "-q", "-b", "main")
    original = "int helper(int x) {\n    return x + 1;\n}\n"
    unit = _cpp_unit(original, original, original)
    failures = [VerificationFailure(
        validator="build_test", severity="error",
        message="error: build failed",
    )]
    orch, client = _orch_with_client(repo, [])
    out = orch._micro_patch_repair(
        "a.cc", original, [(unit, _cand(unit, original))], failures)
    assert out is None
    assert client.calls == []  # no model call without a localized error


def test_micro_patch_declines_on_empty_model_response(tmp_path):
    import tempfile
    from pathlib import Path
    from tests.conftest import git

    repo = Path(tempfile.mkdtemp())
    git(repo, "init", "-q", "-b", "main")
    original = "int helper(int x) {\n    return x + 1;\n}\n" * 4
    unit = _cpp_unit(original, original, original)
    failures = [VerificationFailure(
        validator="build_test", severity="error",
        message="a.cc:6:5: error: redefinition of 'int helper(int)'",
    )]
    orch, _client = _orch_with_client(repo, ["", "not json at all {{{"])
    assert orch._micro_patch_repair(
        "a.cc", original, [(unit, _cand(unit, original))], failures) is None


# ---------------------------------------------------------------------------
# Provenance registration (Refinement 4: mechanisms are auditable)
# ---------------------------------------------------------------------------

def test_new_provenance_values_registered():
    from capybase.provenance import PROVENANCE_VALUES
    assert "deterministic_dup_eradication" in PROVENANCE_VALUES
    assert "micro_patch_repair" in PROVENANCE_VALUES

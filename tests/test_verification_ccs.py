"""Tests for the C/C++ compile floor (gcc/clang -fsyntax-only).

These exercise ``_compile_ccs`` directly and (in later-added test sections) the
``CcsSyntaxValidator`` per-unit check and the ``verify_file`` C/C++ branch. The
gcc/g++-backed tests skip when the compiler is absent (CI without a toolchain);
the wiring / graceful-degrade tests run unconditionally via monkeypatch.
"""

from __future__ import annotations

import shutil

import pytest

from capybase.verification import (
    VerificationEngine,
    _compile_ccs,
    _is_ccs_resolution_error,
    VerificationContext,
    CcsSyntaxValidator,
)
from capybase.conflict_model import (
    ConflictSide,
    ConflictUnit,
    CandidateResolution,
)
from capybase.config import ValidationConfig


GCC = shutil.which("gcc")
GXX = shutil.which("g++")
skip_no_gcc = pytest.mark.skipif(GCC is None, reason="gcc not installed")
skip_no_gxx = pytest.mark.skipif(GXX is None, reason="g++ not installed")


# ---------------------------------------------------------------------------
# _compile_ccs (real gcc/g++)
# ---------------------------------------------------------------------------


@skip_no_gcc
def test_compile_c_clean_source():
    ok, msg = _compile_ccs("int main(void) { return 0; }\n", cc_path="gcc",
                           std="c11", suffix=".c")
    assert ok is True
    assert msg == "cc ok"


@skip_no_gcc
def test_compile_c_detects_syntax_error():
    # Missing semicolon — a true parse error gcc must catch.
    src = "int main(void) { return 0 }\n"
    ok, msg = _compile_ccs(src, cc_path="gcc", std="c11", suffix=".c")
    assert ok is False
    # gcc format: "file:line:col: error: ..."; the message carries the error.
    assert "error" in msg
    assert "expected" in msg


@skip_no_gxx
def test_compile_cpp_clean_source():
    ok, msg = _compile_ccs("int main() { return 0; }\n", cc_path="g++",
                           std="c++17", suffix=".cpp")
    assert ok is True
    assert msg == "cc ok"


@skip_no_gxx
def test_compile_cpp_detects_syntax_error():
    # Unterminated string — a parse error.
    src = 'int main() { char *s = "unterminated; return 0; }\n'
    ok, msg = _compile_ccs(src, cc_path="g++", std="c++17", suffix=".cpp")
    assert ok is False
    assert "error" in msg


@skip_no_gcc
def test_compile_ccs_std_rejects_bogus():
    # An unrecognized -std= makes gcc emit a flag error (gcc: error: ...).
    ok, msg = _compile_ccs("int main(void){return 0;}\n", cc_path="gcc",
                           std="c999", suffix=".c")
    assert ok is False
    assert "error" in msg or "unrecognized" in msg


@skip_no_gcc
def test_compile_ccs_header_file_compiles_standalone():
    # Headers (.h) are valid translation units under -fsyntax-only (declarations
    # only); gcc needs no .c driver wrapper. This pins the header edge case.
    hdr = "#ifndef H\n#define H\nint add(int a, int b);\n#endif\n"
    ok, msg = _compile_ccs(hdr, cc_path="gcc", std="c11", suffix=".h")
    assert ok is True, msg


@skip_no_gcc
def test_compile_ccs_missing_binary_raises_file_not_found():
    # A missing compiler raises FileNotFoundError — the caller gates on this to
    # report "not checked" rather than a false failure.
    with pytest.raises(FileNotFoundError):
        _compile_ccs("int main(void){return 0;}\n",
                     cc_path="definitely-not-a-real-compiler-xyz",
                     std="c11", suffix=".c")


# ---------------------------------------------------------------------------
# _is_ccs_resolution_error (semantic vs parse classification; no toolchain)
# ---------------------------------------------------------------------------


def test_is_ccs_resolution_error_classifies_semantic():
    # Each semantic pattern → True (deferred to Phase B; not a per-unit defect).
    semantic = [
        "x.c:5:3: error: use of undeclared identifier 'foo'",
        "x.c:2:12: error: implicit declaration of function 'foo' "
        "[-Wimplicit-function-declaration]",
        "x.cpp:10:5: error: 'bar' was not declared in this scope",
        "x.c:3:2: error: 'T' has not been declared",
        "x.cpp:8:3: error: no matching function for call to 'f'",
        "x.cpp:9:3: error: cannot convert 'int' to 'char*'",
        "x.c:4:8: error: invalid use of incomplete type 'struct S'",
        "x.cpp:12:4: error: 'x' is not a member of 'Foo'",
        "x.cpp:1:1: error: 'Bar' does not name a type",
        "x.cpp:7:3: error: 'class Foo' has no member named 'baz'",
        "x.cpp:1: undefined reference to `symbol'",
        # Missing project-internal headers — standalone gcc has no -I flags, so
        # any sibling #include is unresolved. The C analog of "undeclared
        # identifier": an artifact of compiling out of TU context. Surfaced in
        # the C live-eval (redis server.h, sqlite sqliteInt.h) as false-positive
        # hard failures that escalated sim-0.99 merges.
        "src/pubsub.c:30:10: fatal error: server.h: No such file or directory",
        "x.c:14:10: fatal error: sqliteInt.h: No such file or directory",
    ]
    for msg in semantic:
        assert _is_ccs_resolution_error(msg), f"expected True for: {msg!r}"


def test_ccs_syntax_validator_defers_missing_header():
    """A C fragment that #includes a project-internal header (server.h,
    sqliteInt.h) must NOT hard-fail the per-unit CcsSyntaxValidator — the header
    is unresolved only because standalone gcc has no -I flags. The whole-file
    build command (make) is the authoritative oracle for these. Regression guard
    for the C live-eval escalations (redis pubsub.c, sqlite mutex_w32.c)."""
    import capybase.verification as vmod
    # Force _resolve_tool to a sentinel so the gcc path engages (not the
    # absent-compiler skip). The missing-header error then defers via the
    # semantic filter.
    monkey = pytest.MonkeyPatch()
    monkey.setattr(vmod, "_resolve_tool", lambda name: "/usr/bin/gcc")
    try:
        worktree = (
            "int compute(int n) {\n"
            "<<<<<<< H\n"
            "    return n + 1;\n"
            "=======\n"
            "    return n + 2;\n"
            ">>>>>>> b\n"
            "}\n"
        )
        u = _unit(worktree=worktree, language="c", marker_span=(1, 3))
        # resolved_text is valid C, but standalone gcc will fail on the (absent)
        # sibling header. The semantic filter must defer it → passed=True.
        res = _verify(CcsSyntaxValidator(), u,
                      _candidate('#include "server.h"\n    return n + 1;\n'))
        assert res.passed, res.message  # deferred, not failed
        assert res.features["ccs_syntax_checked"] is True
        assert res.features["syntax_passed"] is True
    finally:
        monkey.undo()


def test_is_ccs_resolution_error_surfaces_parse_errors():
    # Parse errors don't match any semantic pattern → False (surfaced as defects).
    parse = [
        "x.c:5:3: error: expected ';' before '}' token",
        "x.c:1:1: error: expected '=', ',', ';', 'asm' or '__attribute__'",
        "x.c:2:5: error: stray '\\342' in program",
        "x.c:3:1: error: unterminated string literal",
        "x.c:4:8: error: missing terminating \" character",
        "x.c:5:3: error: expected expression before '}' token",
        "x.c:6:1: error: expected declaration specifiers or '...' before 'x'",
    ]
    for msg in parse:
        assert not _is_ccs_resolution_error(msg), f"expected False for: {msg!r}"


def test_is_ccs_resolution_error_empty_and_none():
    assert not _is_ccs_resolution_error("")
    assert not _is_ccs_resolution_error(None)  # type: ignore[arg-type]


def test_is_ccs_resolution_error_case_insensitive():
    # gcc/clang message case can vary by locale/version; matching is case-blind.
    assert _is_ccs_resolution_error("Error: Use of Undeclared Identifier 'x'")
    assert _is_ccs_resolution_error("X.C:1:1: ERROR: Cannot Convert 'int'")


# ---------------------------------------------------------------------------
# CcsSyntaxValidator (per-unit; mirrors test_syntax_repair.py shape)
# ---------------------------------------------------------------------------


def _unit(*, base="", current="", replayed="", worktree=None, language="c",
          marker_span=(0, 0)):
    wt = worktree if worktree is not None else base
    ext = ".cpp" if language in ("cpp", "c++") else ".c"
    return ConflictUnit(
        session_id="s", step_index=0, path=f"a{ext}", language=language,
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text=wt, marker_span=marker_span,
    )


def _candidate(resolved=""):
    return CandidateResolution(
        candidate_id="c", unit_id="u", model_name="m",
        prompt_version="v", resolved_text=resolved,
    )


def _verify(validator, unit, candidate):
    ctx = VerificationContext(unit=unit, candidate=candidate, config=ValidationConfig())
    return validator.verify(ctx)


# A minimal self-contained C TU: a function whose body is the conflict region.
# marker_span covers the function body so the spliced text is a complete TU.
_C_WORKTREE = (
    "int compute(int n) {\n"
    "<<<<<<< H\n"
    "    return n + 1;\n"
    "=======\n"
    "    return n + 2;\n"
    ">>>>>>> b\n"
    "}\n"
)
_C_SPAN = (1, 3)  # the three marker lines


@skip_no_gcc
def test_ccs_syntax_validator_passes_valid_c():
    u = _unit(worktree=_C_WORKTREE, language="c", marker_span=_C_SPAN)
    res = _verify(CcsSyntaxValidator(), u, _candidate("    return n + 1;\n"))
    assert res.passed, res.message
    assert res.features["ccs_syntax_checked"] is True
    assert res.features["syntax_passed"] is True


@skip_no_gcc
def test_ccs_syntax_validator_catches_parse_error():
    # Missing semicolon — a true parse error gcc must surface as a defect.
    u = _unit(worktree=_C_WORKTREE, language="c", marker_span=_C_SPAN)
    res = _verify(CcsSyntaxValidator(), u, _candidate("    return n + 1\n"))
    assert not res.passed
    assert res.severity == "error"
    assert res.features["ccs_syntax_checked"] is True
    assert res.features["syntax_passed"] is False
    assert "error" in res.message


@skip_no_gcc
def test_ccs_syntax_validator_skips_semantic_error():
    # An undeclared identifier is a resolution error (deferred, not a defect):
    # standalone -fsyntax-only can't know whether 'foo' is defined in a header
    # the full TU would include.
    u = _unit(worktree=_C_WORKTREE, language="c", marker_span=_C_SPAN)
    res = _verify(CcsSyntaxValidator(), u, _candidate("    return foo(n);\n"))
    assert res.passed, res.message  # deferred, not failed
    assert res.features["ccs_syntax_checked"] is True
    assert res.features["syntax_passed"] is True


def test_ccs_syntax_validator_skips_non_ccs():
    # A python unit → no-op pass, ccs_syntax_checked=False.
    u = _unit(worktree="x = 1\n", language="python", marker_span=(0, 0))
    res = _verify(CcsSyntaxValidator(), u, _candidate("x = 1\n"))
    assert res.passed
    assert res.features["ccs_syntax_checked"] is False


def test_ccs_syntax_validator_skips_when_compiler_absent(monkeypatch):
    # The graceful-degrade contract: a missing compiler is NEVER a false fail.
    # Not skip-gated — runs unconditionally via monkeypatch.
    import capybase.verification as vmod
    monkeypatch.setattr(vmod, "_resolve_tool", lambda name: None)
    u = _unit(worktree=_C_WORKTREE, language="c", marker_span=_C_SPAN)
    res = _verify(CcsSyntaxValidator(), u, _candidate("    return n + 1\n"))
    assert res.passed
    assert res.features["ccs_syntax_checked"] is False
    assert res.features["syntax_passed"] is True


def test_ccs_syntax_validator_skips_unbalanced_braces():
    # A splice that leaves braces unbalanced defers to Phase B (a per-unit
    # fragment inside a larger construct is structurally incomplete). The brace
    # guard runs BEFORE tool resolution, so this needs no compiler — it tests
    # that imbalance short-circuits to a pass regardless of gcc availability.
    worktree = "void f() {\n<<<<<<< H\n    {\n=======\n    {\n>>>>>>> b\n}\n"
    u = _unit(worktree=worktree, language="c", marker_span=(1, 3))
    # resolved_text opens a brace but doesn't close it → unbalanced after splice.
    res = _verify(CcsSyntaxValidator(), u, _candidate("    g(); {\n"))
    assert res.passed, res.message
    assert res.features["ccs_syntax_checked"] is False
    assert res.features["syntax_passed"] is True


@skip_no_gxx
def test_ccs_syntax_validator_handles_cpp():
    # C++ uses g++ and cpp_std; a valid merge passes.
    worktree = (
        "int compute(int n) {\n"
        "<<<<<<< H\n"
        "    return n + 1;\n"
        "=======\n"
        "    return n + 2;\n"
        ">>>>>>> b\n"
        "}\n"
    )
    u = _unit(worktree=worktree, language="cpp", marker_span=_C_SPAN)
    res = _verify(CcsSyntaxValidator(), u, _candidate("    return n + 1;\n"))
    assert res.passed, res.message
    assert res.features["ccs_syntax_checked"] is True


@skip_no_gcc
def test_ccs_syntax_validator_registered_in_default_engine():
    # The validator is auto-registered via VerificationEngine.default; a C unit
    # with a parse error fails through the engine loop.
    engine = VerificationEngine.default(ValidationConfig())
    u = _unit(worktree=_C_WORKTREE, language="c", marker_span=_C_SPAN)
    res = engine.verify(u, _candidate("    return n + 1\n"))
    assert not res.passed
    assert any(f.validator == "ccs_syntax" for f in res.hard_failures)


# ---------------------------------------------------------------------------
# verify_file C/C++ branch (Phase B whole-file compile gate)
# ---------------------------------------------------------------------------

# A self-contained C conflict: a function whose body is the conflict region.
_C_FILE_CONFLICT = (
    "int compute(int n) {\n"
    "<<<<<<< H\n"
    "    return n + 1;\n"
    "=======\n"
    "    return n + 2;\n"
    ">>>>>>> b\n"
    "}\n"
)
_C_FILE_CORRECT = "    return n + 1;"   # valid C
_C_FILE_BROKEN = "    return n + 1"     # missing semicolon → parse error

# A self-contained C++ conflict.
_CPP_FILE_CONFLICT = (
    "int compute(int n) {\n"
    "<<<<<<< H\n"
    "    return n + 1;\n"
    "=======\n"
    "    return n + 2;\n"
    ">>>>>>> b\n"
    "}\n"
)
_CPP_FILE_BROKEN = "    return n + 1"   # missing semicolon


def _span_of_markers(original: str) -> tuple[int, int]:
    """Return the (start, end) marker span of the only conflict block."""
    lines = original.split("\n")
    start = next(i for i, l in enumerate(lines) if l.startswith("<<<<<<<"))
    end = next(i for i, l in enumerate(lines) if l.startswith(">>>>>>>"))
    return (start, end)


@skip_no_gcc
def test_verify_file_c_accepts_compiling_merge(tmp_path):
    span = _span_of_markers(_C_FILE_CONFLICT)
    eng = VerificationEngine.default(ValidationConfig())
    res = eng.verify_file(
        "src/cfg.c", "c", _C_FILE_CONFLICT, [(span, _C_FILE_CORRECT)],
        repo_root=str(tmp_path),
    )
    assert res.passed, [f.message for f in res.hard_failures]
    assert res.features["syntax_checked"] is True
    assert res.features["syntax_passed"] is True


@skip_no_gcc
def test_verify_file_c_rejects_noncompiling_merge(tmp_path):
    span = _span_of_markers(_C_FILE_CONFLICT)
    eng = VerificationEngine.default(ValidationConfig())
    res = eng.verify_file(
        "src/cfg.c", "c", _C_FILE_CONFLICT, [(span, _C_FILE_BROKEN)],
        repo_root=str(tmp_path),
    )
    assert not res.passed
    syntax_fails = [f for f in res.hard_failures if f.validator == "syntax"]
    assert len(syntax_fails) == 1
    assert "error" in syntax_fails[0].message


@skip_no_gxx
def test_verify_file_cpp_rejects_noncompiling_merge(tmp_path):
    span = _span_of_markers(_CPP_FILE_CONFLICT)
    eng = VerificationEngine.default(ValidationConfig())
    res = eng.verify_file(
        "src/cfg.cpp", "cpp", _CPP_FILE_CONFLICT, [(span, _CPP_FILE_BROKEN)],
        repo_root=str(tmp_path),
    )
    assert not res.passed
    assert any(f.validator == "syntax" for f in res.hard_failures)


def test_verify_file_c_missing_compiler_is_not_checked(monkeypatch, tmp_path):
    # The graceful-degrade contract: a missing compiler is NEVER a false fail.
    # Not skip-gated — runs unconditionally via monkeypatch. Feeds BROKEN source
    # and asserts the tool didn't run and no syntax failure was added.
    import capybase.adapters.lsp as lsp_mod
    monkeypatch.setattr(lsp_mod, "_resolve", lambda cmd: None)
    span = _span_of_markers(_C_FILE_CONFLICT)
    eng = VerificationEngine.default(ValidationConfig())
    res = eng.verify_file(
        "src/cfg.c", "c", _C_FILE_CONFLICT, [(span, _C_FILE_BROKEN)],
        repo_root=str(tmp_path),
    )
    assert res.features["syntax_checked"] is False
    assert not any(f.validator == "syntax" for f in res.hard_failures)


@skip_no_gcc
def test_verify_file_c_disabled_when_require_syntax_off(tmp_path):
    # With require_syntax_if_supported=False, a broken merge is NOT hard-failed
    # (the check still runs and records syntax_passed=False, but no failure).
    span = _span_of_markers(_C_FILE_CONFLICT)
    cfg = ValidationConfig()
    cfg.require_syntax_if_supported = False
    eng = VerificationEngine.default(cfg)
    res = eng.verify_file(
        "src/cfg.c", "c", _C_FILE_CONFLICT, [(span, _C_FILE_BROKEN)],
        repo_root=str(tmp_path),
    )
    assert res.passed  # no hard failure despite the broken merge
    assert res.features["syntax_checked"] is True
    assert res.features["syntax_passed"] is False


@skip_no_gcc
def test_verify_file_c_unbalanced_braces_get_rich_diagnostic(tmp_path):
    # Consistency with rust: C/C++ is a brace language (Family A), so a splice
    # that leaves braces unbalanced is caught by the fast brace-coherence gate
    # (BEFORE the gcc run) with a line-specific diagnostic — not deferred to a
    # generic gcc error. A merge that opens a brace but never closes it.
    conflict = (
        "int compute(int n) {\n"
        "<<<<<<< H\n"
        "    if (n > 0) {\n"
        "        return n;\n"
        "=======\n"
        "    if (n > 0) {\n"
        "        return n;\n"
        ">>>>>>> b\n"
        "}\n"
    )
    # resolved_text opens an inner brace but never closes it → unbalanced.
    span = _span_of_markers(conflict)
    eng = VerificationEngine.default(ValidationConfig())
    res = eng.verify_file(
        "src/cfg.c", "c", conflict, [(span, "    if (n > 0) {\n        return n;\n")],
        repo_root=str(tmp_path),
    )
    assert not res.passed
    brace_fails = [f for f in res.hard_failures if f.validator == "syntax"]
    assert len(brace_fails) == 1
    # The rich diagnostic names the brace delta, not a generic gcc error.
    assert "unclosed" in brace_fails[0].message or "brace" in brace_fails[0].message
    assert "brace_imbalance_line" in brace_fails[0].detail


# ---------------------------------------------------------------------------
# verify_file C branch: user-supplied build command (cc_build_command)
# ---------------------------------------------------------------------------


@skip_no_gcc
def test_verify_file_c_build_command_passes(tmp_path):
    """When cc_build_command is set, verify_file runs it (not standalone gcc).
    A trivial passing command (true) → syntax_passed=True."""
    span = _span_of_markers(_C_FILE_CONFLICT)
    cfg = ValidationConfig()
    cfg.cc_build_command = "true"  # always succeeds
    eng = VerificationEngine.default(cfg)
    res = eng.verify_file(
        "src/cfg.c", "c", _C_FILE_CONFLICT, [(span, _C_FILE_CORRECT)],
        repo_root=str(tmp_path),
    )
    assert res.passed, [f.message for f in res.hard_failures]
    assert res.features["syntax_checked"] is True
    assert res.features["syntax_passed"] is True


@skip_no_gcc
def test_verify_file_c_build_command_fails(tmp_path):
    """A failing build command (false) → syntax_passed=False, hard failure."""
    span = _span_of_markers(_C_FILE_CONFLICT)
    cfg = ValidationConfig()
    cfg.cc_build_command = "false"  # always fails
    eng = VerificationEngine.default(cfg)
    res = eng.verify_file(
        "src/cfg.c", "c", _C_FILE_CONFLICT, [(span, _C_FILE_CORRECT)],
        repo_root=str(tmp_path),
    )
    assert not res.passed
    assert res.features["syntax_checked"] is True
    assert res.features["syntax_passed"] is False
    syntax_fails = [f for f in res.hard_failures if f.validator == "syntax"]
    assert len(syntax_fails) == 1


@skip_no_gcc
def test_verify_file_c_build_command_restores_file(tmp_path):
    """The save/write/restore dance: after the build check, the file on disk is
    restored to its pre-check state (verify_file runs before the orchestrator
    writes the final buffer)."""
    # Write the conflict file to the repo so there's something to save/restore.
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    conflict_path = tmp_path / "src" / "cfg.c"
    conflict_path.write_text(_C_FILE_CONFLICT)
    original_on_disk = conflict_path.read_text()
    span = _span_of_markers(_C_FILE_CONFLICT)
    cfg = ValidationConfig()
    cfg.cc_build_command = "true"
    eng = VerificationEngine.default(cfg)
    eng.verify_file(
        "src/cfg.c", "c", _C_FILE_CONFLICT, [(span, _C_FILE_CORRECT)],
        repo_root=str(tmp_path),
    )
    # The file on disk must be unchanged after the check (restored).
    assert conflict_path.read_text() == original_on_disk, (
        "verify_file must restore the file after the build check"
    )


@skip_no_gcc
def test_verify_file_c_build_command_empty_falls_back_to_gcc(tmp_path):
    """When cc_build_command is empty (the default), verify_file falls back to
    standalone gcc -fsyntax-only (the existing behavior, unchanged)."""
    span = _span_of_markers(_C_FILE_CONFLICT)
    cfg = ValidationConfig()
    assert cfg.cc_build_command == ""  # default
    eng = VerificationEngine.default(cfg)
    res = eng.verify_file(
        "src/cfg.c", "c", _C_FILE_CONFLICT, [(span, _C_FILE_CORRECT)],
        repo_root=str(tmp_path),
    )
    assert res.features["syntax_checked"] is True
    assert res.passed  # valid C compiles under standalone gcc

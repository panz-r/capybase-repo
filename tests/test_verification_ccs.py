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
    _parse_cc_error_location,
    _is_cc_werror_warning,
    _missing_make_target,
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
        # gcc wording for undefined typedef (project-internal type defined in a
        # sibling header standalone gcc can't see). The gcc analog of clang's
        # "does not name a type". Surfaced in the C live-eval (sqlite vdbe.h,
        # btree.h, vdbeInt.h referencing u8, BtCursor, sqlite3_vfs).
        "src/vdbe.h:42:3: error: unknown type name 'u8'",
        "src/vdbeInt.h:64:3: error: unknown type name 'BtCursor'",
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
    # Sprint-21 coherence rung: the buffer may be deterministically
    # repaired now (the rung's purpose) — then it PASSES with the
    # repair flag set; unrepaired imbalances still fail.
    if res.passed:
        assert res.features.get("coherence_repair_applied")
    else:
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
    assert res.passed  # acceptance-neutral (minimal installs must not fail)
    assert res.features["ccs_syntax_checked"] is False
    # P3-slice (s27): UNKNOWN IS NOT PASS — a check that never ran no
    # longer claims syntax_passed=True; the evidence records the truth.
    assert "syntax_passed" not in res.features
    assert res.features["syntax_outcome"] == "unknown"
    assert res.unknown is True


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
    # Deferred to the whole-file gate: neither pass nor unknown here (no
    # credit, no double-count when the whole-file oracle runs).
    assert "syntax_passed" not in res.features
    assert res.unknown is False


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
    # Sprint-21 coherence rung: a deterministically repairable imbalance
    # now gets REPAIRED (the rung's purpose) — that outcome passes with
    # the repair flag; an unrepairable one still fails with the rich
    # diagnostic. Both are correct; assert which one happened.
    if res.passed:
        assert res.features.get("coherence_repair_applied")
    else:
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


# ---------------------------------------------------------------------------
# Error localization: _parse_cc_error_location + _is_cc_werror_warning
# ---------------------------------------------------------------------------

def test_parse_cc_error_location_sibling_file():
    """A gcc error in a sibling file (tool/lemon.c) returns the sibling stem."""
    stem, line = _parse_cc_error_location(
        "tool/lemon.c:753:6: error: conflicting types for 'FindRulePrecedences'"
    )
    assert stem == "lemon"
    assert line == 753


def test_parse_cc_error_location_conflict_file():
    """A gcc error in the conflict file (src/delete.c) returns its stem."""
    stem, line = _parse_cc_error_location(
        "src/delete.c:42:3: error: expected ';' before '}' token"
    )
    assert stem == "delete"
    assert line == 42


def test_parse_cc_error_location_unparseable():
    """Non-gcc error lines (e.g. cmake errors) return (None, None)."""
    stem, line = _parse_cc_error_location(
        "Error: build is not a directory"
    )
    assert stem is None
    assert line is None


def test_parse_cc_error_location_header_file():
    """Header file errors parse correctly."""
    stem, line = _parse_cc_error_location(
        "json_object.h:15:5: error: unknown type name 'foo'"
    )
    assert stem == "json_object"
    assert line == 15


def test_is_cc_werror_warning_promoted():
    """-Werror=... tags are detected as warning promotions."""
    assert _is_cc_werror_warning(
        "error: 'calloc' sizes specified with sizeof... [-Werror=calloc-transposed-args]"
    )
    assert _is_cc_werror_warning(
        "error: right-hand operand of comma... [-Werror=unused-value]"
    )
    assert _is_cc_werror_warning(
        "error: passing argument 3... [-Werror=incompatible-pointer-types]"
    )


def test_is_cc_werror_warning_real_error():
    """Real errors (no -Werror tag) are NOT classified as warnings."""
    assert not _is_cc_werror_warning(
        "error: expected ';' before '}' token"
    )
    assert not _is_cc_werror_warning(
        "src/delete.c:42:3: error: conflicting types for 'foo'"
    )


def test_missing_make_target_extraction():
    """C17: make's missing-rule failure names its target file.

    protobuf-0051: upstream's merge_sha deleted field_access_listener.cc
    while leaving it in src/Makefile.am — the whole-tree gate fails with
    'No rule to make target' for ANY conflict-file content. The named file
    lets the gate classify it (sibling = infra, conflict file = defect)
    instead of falling back to the meaningless 'Error 1' driver line."""
    target = _missing_make_target([
        "make[2]: Entering directory '/repo/src'",
        "make[2]: *** No rule to make target "
        "'google/protobuf/field_access_listener.cc', needed by "
        "'google/protobuf/field_access_listener.lo'.  Stop.",
        "make[1]: *** [Makefile:1917: all-recursive] Error 1",
    ])
    assert target == "google/protobuf/field_access_listener.cc"
    # conflict-file target extracts too (classified as a real defect)
    assert _missing_make_target([
        "make: *** No rule to make target 'descriptor.cc', needed by "
        "'descriptor.lo'.  Stop.",
    ]) == "descriptor.cc"
    # ordinary make output: no match
    assert _missing_make_target([
        "make[1]: *** [Makefile:1917: all-recursive] Error 1",
    ]) is None


def test_is_cc_werror_warning_plain_warning():
    """Plain -W warnings (no -Werror=) are NOT classified as error promotions."""
    assert not _is_cc_werror_warning(
        "warning: unused variable 'x' [-Wunused-variable]"
    )


# ---------------------------------------------------------------------------
# Build-gate error localization: sibling-file errors pass, conflict-file
# errors fail. Uses shell commands that emit gcc-style error output.
# ---------------------------------------------------------------------------

def test_build_gate_sibling_file_error_passes(tmp_path):
    """A build that fails ONLY on a sibling file (tool/lemon.c) should PASS —
    the merge didn't touch lemon.c, so the error is pre-existing infrastructure."""
    span = _span_of_markers(_C_FILE_CONFLICT)
    # Emit a gcc-style sibling error to stderr, exit 1.
    cfg = ValidationConfig()
    cfg.cc_build_command = (
        'echo "tool/lemon.c:753:6: error: conflicting types for '
        "'FindRulePrecedences'\" >&2; false"
    )
    eng = VerificationEngine.default(cfg)
    res = eng.verify_file(
        "src/delete.c", "c", _C_FILE_CONFLICT, [(span, _C_FILE_CORRECT)],
        repo_root=str(tmp_path),
    )
    # The conflict file is delete.c; lemon.c is a sibling → compile-pass.
    assert res.features["syntax_checked"] is True
    assert res.features["syntax_passed"] is True
    assert res.passed


def test_build_gate_conflict_file_error_fails(tmp_path):
    """A build that fails on the CONFLICT file (src/delete.c) should FAIL —
    genuine error in the merged code."""
    span = _span_of_markers(_C_FILE_CONFLICT)
    cfg = ValidationConfig()
    cfg.cc_build_command = (
        'echo "src/delete.c:42:3: error: expected \';\' before \'}\' token" >&2; false'
    )
    eng = VerificationEngine.default(cfg)
    res = eng.verify_file(
        "src/delete.c", "c", _C_FILE_CONFLICT, [(span, _C_FILE_CORRECT)],
        repo_root=str(tmp_path),
    )
    assert res.features["syntax_checked"] is True
    assert res.features["syntax_passed"] is False
    assert not res.passed


def test_build_gate_werror_warning_passes(tmp_path):
    """A build that fails ONLY on -Werror warnings should PASS — the code
    compiled successfully but triggered a strictness flag."""
    span = _span_of_markers(_C_FILE_CONFLICT)
    cfg = ValidationConfig()
    cfg.cc_build_command = (
        'echo "arraylist.c:36:43: error: \'calloc\' sizes specified... '
        '[-Werror=calloc-transposed-args]" >&2; false'
    )
    eng = VerificationEngine.default(cfg)
    res = eng.verify_file(
        "json_object.c", "c", _C_FILE_CONFLICT, [(span, _C_FILE_CORRECT)],
        repo_root=str(tmp_path),
    )
    assert res.features["syntax_checked"] is True
    assert res.features["syntax_passed"] is True
    assert res.passed


def test_build_gate_mixed_sibling_and_conflict_fails(tmp_path):
    """When BOTH sibling and conflict-file errors exist, the conflict-file
    error takes precedence → FAIL (conservative: never pass a real defect)."""
    span = _span_of_markers(_C_FILE_CONFLICT)
    cfg = ValidationConfig()
    cfg.cc_build_command = (
        'echo "tool/lemon.c:753:6: error: conflicting types" >&2; '
        'echo "src/delete.c:42:3: error: expected \';\'" >&2; false'
    )
    eng = VerificationEngine.default(cfg)
    res = eng.verify_file(
        "src/delete.c", "c", _C_FILE_CONFLICT, [(span, _C_FILE_CORRECT)],
        repo_root=str(tmp_path),
    )
    assert res.features["syntax_passed"] is False
    assert not res.passed


def test_build_gate_sibling_error_with_make_driver_line_passes(tmp_path):
    """When stderr has both a sibling gcc error AND a make-driver summary line
    (``make[2]: *** [Makefile:89: hiredis.o] Error 1``), the driver line must
    NOT be attributed to the conflict file. The gcc line classifies it as a
    sibling error → compile-pass.

    This is the redis hiredis va_arg pattern: deps/hiredis.c has a pre-existing
    error, make reports it via a driver line + the gcc line, but the conflict
    file (src/aof.c) is fine."""
    span = _span_of_markers(_C_FILE_CONFLICT)
    cfg = ValidationConfig()
    cfg.cc_build_command = (
        'echo "hiredis.c:700:31: error: second argument to \'va_arg\' is of incomplete type \'void\'" >&2; '
        'echo "make[2]: *** [Makefile:89: hiredis.o] Error 1" >&2; false'
    )
    eng = VerificationEngine.default(cfg)
    res = eng.verify_file(
        "src/aof.c", "c", _C_FILE_CONFLICT, [(span, _C_FILE_CORRECT)],
        repo_root=str(tmp_path),
    )
    # hiredis.c is a sibling of aof.c → compile-pass.
    assert res.features["syntax_checked"] is True
    assert res.features["syntax_passed"] is True
    assert res.passed


# ---------------------------------------------------------------------------
# _compile_ccs include_paths: header files can resolve sibling includes
# ---------------------------------------------------------------------------

@skip_no_gcc
def test_compile_ccs_include_paths_resolves_sibling_header(tmp_path):
    """When include_paths are provided, gcc can resolve #include of a sibling
    header in that directory. Without the path, it fails with 'No such file'."""
    # Create a sibling header that defines a type.
    sibling = tmp_path / "mytypes.h"
    sibling.write_text("typedef int my_type;\n")
    # Source that includes the sibling header.
    src = '#include "mytypes.h"\nmy_type x = 0;\n'
    # Without include_paths: gcc can't find mytypes.h → fatal error.
    ok_no_ip, msg_no_ip = _compile_ccs(src, cc_path=GCC, std="c11", suffix=".c")
    assert not ok_no_ip
    assert "No such file" in msg_no_ip or "fatal error" in msg_no_ip
    # With include_paths pointing at the dir containing mytypes.h: resolves.
    ok_ip, msg_ip = _compile_ccs(
        src, cc_path=GCC, std="c11", suffix=".c",
        include_paths=[str(tmp_path)],
    )
    assert ok_ip, f"expected compile success with include_paths, got: {msg_ip}"


# ---------------------------------------------------------------------------
# Standalone gcc fallback: -Werror tolerance
# ---------------------------------------------------------------------------

@skip_no_gcc
def test_verify_file_gcc_fallback_werror_passes(tmp_path, monkeypatch):
    """The standalone gcc fallback (no cc_build_command) should tolerate
    -Werror warning promotions. These are warnings the project's flags promoted
    to errors — the code compiled but triggered a strictness flag, not a defect.

    We monkeypatch _compile_ccs to return a -Werror error to exercise the
    fallback path's tolerance logic without needing code that actually triggers
    a -Werror promotion."""
    import capybase.verification as ver
    span = _span_of_markers(_C_FILE_CONFLICT)
    original = ver._compile_ccs

    def _fake_compile(source, **kw):
        # Return a -Werror line as if gcc emitted it.
        return False, "x.c:36:43: error: 'calloc' sizes specified... [-Werror=calloc-transposed-args]"

    monkeypatch.setattr(ver, "_compile_ccs", _fake_compile)
    try:
        cfg = ValidationConfig()
        assert cfg.cc_build_command == ""  # forces gcc fallback
        eng = VerificationEngine.default(cfg)
        res = eng.verify_file(
            "src/cfg.c", "c", _C_FILE_CONFLICT, [(span, _C_FILE_CORRECT)],
            repo_root=str(tmp_path),
        )
        # -Werror promotion → compile-pass (not a real defect).
        assert res.features["syntax_passed"] is True
        assert res.passed
    finally:
        ver._compile_ccs = original


# ---------------------------------------------------------------------------
# Header files skip the per-unit CCS gate
# ---------------------------------------------------------------------------

@skip_no_gcc
def test_ccs_syntax_validator_compiles_header_files():
    """Header files (.h/.hpp) are now syntax-checked via -fsyntax-only instead
    of being skipped. A clean header with only declarations should compile and
    pass. The semantic-error filter defers 'unknown type name' errors (artifacts
    of standalone compilation without sibling headers), so only genuine parse
    errors surface as hard failures."""
    from capybase.verification import CcsSyntaxValidator, VerificationContext
    unit = ConflictUnit(
        session_id="s", step_index=1, path="src/vdbe.h", language="c",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="typedef struct Foo Foo;"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="typedef struct Foo Foo;"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="typedef struct Foo Foo;"),
        original_worktree_text="typedef struct Foo Foo;",
        marker_span=(0, 0),
    )
    cand = CandidateResolution(
        candidate_id="c1", unit_id="u", model_name="m",
        prompt_version="v", resolved_text="typedef struct Foo Foo;",
    )
    cfg = ValidationConfig()
    ctx = VerificationContext(unit=unit, candidate=cand, config=cfg)
    validator = CcsSyntaxValidator()
    result = validator.verify(ctx)
    # A clean declaration-only header should pass — either compiled OK or
    # deferred (compiler not available). Either way, passed=True.
    assert result.passed
    # The header should NOT be skipped with the old "header file" message.
    assert "header file" not in result.message.lower()


@skip_no_gcc
def test_ccs_syntax_validator_does_not_skip_c_files():
    """C source files (.c) DO get the per-unit CCS gate — only headers skip."""
    from capybase.verification import CcsSyntaxValidator, VerificationContext
    unit = ConflictUnit(
        session_id="s", step_index=1, path="src/foo.c", language="c",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="int x = 1;\n"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="int x = 1;\n"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="int x = 1;\n"),
        original_worktree_text="int x = 1;\n",
        marker_span=(0, 1),
    )
    cand = CandidateResolution(
        candidate_id="c1", unit_id="u", model_name="m",
        prompt_version="v", resolved_text="int x = 1;",
    )
    cfg = ValidationConfig()
    ctx = VerificationContext(unit=unit, candidate=cand, config=cfg)
    validator = CcsSyntaxValidator()
    result = validator.verify(ctx)
    # .c files ARE checked (ccs_syntax_checked=True).
    assert result.features.get("ccs_syntax_checked") is True


def test_whole_file_unit_block_shaped_answer_fails():
    """sqlite-0029 regression: a whole-file unit (marker_span None) whose
    candidate is BLOCK-interior content — the model answered a whole-file
    prompt with the conflict region only, resolved_text starting with a
    file-scope `if(`. The old blanket "no marker span" pass let it skip
    unit validation entirely; the file-level build caught it too late for
    a cheap retry. The raw text IS the file for whole-file units — a
    parse error must fail here."""
    from capybase.verification import CcsSyntaxValidator

    unit = _unit(marker_span=None, worktree="int f(void);\n")
    unit = unit.model_copy(update={"unit_kind": "whole_file"})
    # Block-shaped answer: function-body interior at file scope.
    cand = _candidate(
        "  if( pTab->tabFlags & TF_HasNotNull ){\n"
        "    onError = OE_Abort;\n"
        "  }\n"
    )
    result = _verify(CcsSyntaxValidator(), unit, cand)
    assert not result.passed, (
        "block-interior content for a whole-file unit must fail validation"
    )


def test_whole_file_unit_valid_tu_passes():
    """A whole-file unit whose candidate is a complete, valid TU passes the
    same pipeline (the intended shape for whole-file prompts)."""
    from capybase.verification import CcsSyntaxValidator

    unit = _unit(marker_span=None, worktree="int f(void);\n")
    unit = unit.model_copy(update={"unit_kind": "whole_file"})
    cand = _candidate(
        "int compute(int n) {\n"
        "  if (n > 0) { return n; }\n"
        "  return -n;\n"
        "}\n"
    )
    result = _verify(CcsSyntaxValidator(), unit, cand)
    assert result.passed

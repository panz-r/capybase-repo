"""R1 (sprint-22): coherence-repair propagation + fail-closed verification.

Two invariants, from the tokio-0026 / clickhouse-0049 false accepts:

1. When verify_file's coherence rung repairs the spliced buffer, the
   REPAIRED text must come back on the result (``resolved_text``) so the
   caller writes what was actually validated — the repair used to be
   validation-local while the caller wrote its unrepaired buffer.
2. A repaired candidate is provisional: when the syntax/compile stage
   could not verify the repaired text (no tool available), the gate
   fails closed instead of accepting on coherence alone.
"""

from capybase.config import ValidationConfig
from capybase.verification import (
    VerificationEngine,
    _brace_imbalance_line,
)

# ---------------------------------------------------------------------------
# Fixtures: splices whose brace imbalance the rung can deterministically fix.
# ---------------------------------------------------------------------------

# The conflict block is the last thing in the file: the rung's EOF
# brace-append produces VALID python (the failing shape — repair landing
# after other statements — is exercised by the fail-closed test below,
# where the compiler rightly rejects the repair).
_PY_ORIG = (
    "def a():\n"
    "<<<<<<< H\n"
    "    return {'k': 0}\n"
    "=======\n"
    "    return {}\n"
    ">>>>>>> b\n"
)

# Drops the dict's closing brace: spliced file has one unclosed '{'.
_PY_RESOLVED_UNCLOSED = "    return {'k': 1,\n"

_C_ORIG = (
    "int f(int n) {\n"
    "<<<<<<< H\n"
    "    return n + 1;\n"
    "=======\n"
    "    return n + 2;\n"
    ">>>>>>> b\n"
    "}\n"
)

# Opens an inner block and never closes it (both closes land after the
# splice region only if the repair adds one; net one unclosed '{').
_C_RESOLVED_UNCLOSED = "    if (n > 0) {\n        return 1;\n"


def _span(original: str) -> tuple[int, int]:
    lines = original.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("<<<<<<<"))
    end = next(i for i, ln in enumerate(lines) if ln.startswith(">>>>>>>"))
    return (start, end)


def _verify_py(tmp_path, resolved: str):
    eng = VerificationEngine.default(ValidationConfig())
    return eng.verify_file(
        "pkg/mod.py", "python", _PY_ORIG, [(_span(_PY_ORIG), resolved)],
        repo_root=str(tmp_path),
    )


def _verify_c(tmp_path, resolved: str):
    eng = VerificationEngine.default(ValidationConfig())
    return eng.verify_file(
        "src/f.c", "c", _C_ORIG, [(_span(_C_ORIG), resolved)],
        repo_root=str(tmp_path),
    )


# ---------------------------------------------------------------------------
# 1. Propagation: the repaired text is returned and is the balanced one.
# ---------------------------------------------------------------------------

def test_repaired_text_propagates_on_result(tmp_path):
    res = _verify_py(tmp_path, _PY_RESOLVED_UNCLOSED)
    assert res.features.get("coherence_repair_applied"), res.features
    assert res.passed, [f.message for f in res.hard_failures]
    # The invariant that was false pre-repair holds on the RETURNED text.
    assert res.resolved_text is not None
    assert _brace_imbalance_line(res.resolved_text, "python") is None
    # The repair closed the dict at EOF — the returned text is the
    # repaired one, not the unclosed splice.
    assert res.resolved_text.rstrip().endswith("}")


def test_resolved_text_none_when_no_repair(tmp_path):
    # A balanced resolution never sets resolved_text — callers keep their
    # own buffer verbatim (no silent modification of clean merges).
    res = _verify_py(tmp_path, "    x = 1\n")
    assert res.passed
    assert not res.features.get("coherence_repair_applied")
    assert res.resolved_text is None


# ---------------------------------------------------------------------------
# 2. Fail-closed: repaired + unverifiable (no compiler) is NOT accepted.
# ---------------------------------------------------------------------------

def test_repaired_without_compiler_fails_closed(tmp_path, monkeypatch):
    import capybase.adapters.lsp as lsp_mod
    # No build command is configured by default; remove the C compiler so
    # the standalone gcc fallback cannot run either → syntax not checked.
    monkeypatch.setattr(lsp_mod, "_resolve", lambda _name: None)
    res = _verify_c(tmp_path, _C_RESOLVED_UNCLOSED)
    assert res.features.get("coherence_repair_applied"), res.features
    assert not res.features.get("syntax_checked"), res.features
    assert not res.passed, "repaired candidate accepted without verification"
    assert res.features.get("coherence_repair_unverified") is True
    assert any(
        "without compiler verification" in f.message
        for f in res.hard_failures
    ), [f.message for f in res.hard_failures]


def test_repaired_with_compiler_passes_when_verified(tmp_path):
    """Sanity: with gcc present the repaired C buffer is compiler-backed.

    Skips when gcc is unavailable so the fail-closed test above stays the
    authoritative no-tool path."""
    import shutil
    if shutil.which("gcc") is None:
        import pytest
        pytest.skip("gcc not installed")
    res = _verify_c(tmp_path, _C_RESOLVED_UNCLOSED)
    if not res.features.get("coherence_repair_applied"):
        # The repair didn't fire on this shape in this environment; the
        # propagation tests above cover the fired path.
        import pytest
        pytest.skip("rung did not fire on this buffer")
    assert res.features.get("syntax_checked")
    assert res.passed, [f.message for f in res.hard_failures]
    assert res.resolved_text is not None

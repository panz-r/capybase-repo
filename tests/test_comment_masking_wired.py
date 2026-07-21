"""Tests for wiring mask_deferable_comments into the code-resolution prompt (I1/I2/I3).

The upstream half of the two-level architecture: deferred comments are hidden
from the code-resolution model (more context, less confusion from stale prose),
while non-deferable comments (MACHINE/LEGAL/GENERATED/DOCTEST) stay visible.

The round-trip invariant: blank_strings_and_comments(masked) ==
blank_strings_and_comments(original) — the model operates on the same executable
code, just without the prose distraction.
"""

from __future__ import annotations

from capybase.adapters.string_lexer import (
    blank_strings_and_comments, mask_deferable_comments,
)


# ---------------------------------------------------------------------------
# I3 — the round-trip invariant (the design doc's §2 maskability check)
# ---------------------------------------------------------------------------


def test_mask_preserves_executable_tokens_rust():
    """Masking deferred comments does not change the executable token stream.
    The model sees the same code, just without the prose."""
    original = (
        "fn foo() {\n"
        "    // this is prose about MAX_RETRIES\n"
        "    let x = MAX_RETRIES;\n"
        "}\n"
    )
    masked, _ = mask_deferable_comments(original, "rust")
    assert blank_strings_and_comments(masked, "rust") == blank_strings_and_comments(original, "rust")


def test_mask_preserves_executable_tokens_python():
    original = (
        "def foo():\n"
        "    # prose about the algorithm\n"
        "    x = 1\n"
        "    return x\n"
    )
    masked, _ = mask_deferable_comments(original, "python")
    assert blank_strings_and_comments(masked, "python") == blank_strings_and_comments(original, "python")


def test_mask_preserves_executable_tokens_javascript():
    original = (
        "function foo() {\n"
        "    // prose comment\n"
        "    let x = 1;\n"
        "    return x;\n"
        "}\n"
    )
    masked, _ = mask_deferable_comments(original, "javascript")
    assert blank_strings_and_comments(masked, "javascript") == blank_strings_and_comments(original, "javascript")


# ---------------------------------------------------------------------------
# I1 — masking hides deferred prose, preserves non-deferable directives
# ---------------------------------------------------------------------------


def test_mask_blanks_deferred_prose():
    """Deferred prose comments are blanked (replaced with spaces) so the model
    doesn't see stale narration."""
    original = (
        "fn foo() {\n"
        "    // TODO: refactor this later\n"
        "    let x = 1;\n"
        "}\n"
    )
    masked, deferred = mask_deferable_comments(original, "rust")
    assert len(deferred) == 1
    assert "TODO: refactor this later" not in masked
    assert "let x = 1;" in masked  # code visible


def test_mask_preserves_machine_directives():
    """Machine-significant comments (lint suppressions, build tags) survive
    masking — they affect compilation/tooling and must stay visible to the
    code model."""
    original = (
        "#![allow(dead_code)]\n"
        "fn foo() {\n"
        "    // prose comment\n"
        "    let x = 1;\n"
        "}\n"
    )
    masked, deferred = mask_deferable_comments(original, "rust")
    # The directive survives.
    assert "allow(dead_code)" in masked
    # The prose is blanked.
    assert "prose comment" not in masked
    assert len(deferred) == 1  # only the prose is deferred


def test_mask_preserves_legal_headers():
    """License/copyright headers survive masking."""
    original = (
        "// Copyright 2024 Acme. All rights reserved.\n"
        "fn foo() {\n"
        "    // prose\n"
        "    1\n"
        "}\n"
    )
    masked, deferred = mask_deferable_comments(original, "rust")
    assert "Copyright 2024" in masked
    assert len(deferred) == 1  # only prose


def test_mask_length_preserving():
    """The masked text is the SAME LENGTH as the original — byte offsets stay
    valid for downstream tools (the reconciler's CST editor depends on this)."""
    original = (
        "fn foo() {\n"
        "    // a prose comment here\n"
        "    let x = 1;\n"
        "}\n"
    )
    masked, _ = mask_deferable_comments(original, "rust")
    assert len(masked) == len(original)


# ---------------------------------------------------------------------------
# I2 — config gate (mask_deferred_comments in StructuralConfig)
# ---------------------------------------------------------------------------


def test_config_has_mask_flag_default_true():
    """StructuralConfig.mask_deferred_comments defaults to True (always-on per
    the user's intent, with zero overhead when no comments are present)."""
    from capybase.config import StructuralConfig
    cfg = StructuralConfig()
    assert cfg.mask_deferred_comments is True


# ---------------------------------------------------------------------------
# _mask_sides_if_enabled — the prompt-builder integration
# ---------------------------------------------------------------------------


def test_mask_sides_blanks_deferred_prose_in_sides():
    """The conflict sides shown to the model have their deferred prose masked,
    so the model doesn't get confused by stale narration in either side."""
    from capybase.resolution_engine import _mask_sides_if_enabled

    class _FakeUnit:
        language = "rust"
        base = type("S", (), {"text": "fn foo() {\n    // old prose\n    1\n}\n"})()
        current = type("S", (), {"text": "fn foo() {\n    // current prose\n    1\n}\n"})()
        replayed = type("S", (), {"text": "fn foo() {\n    // replayed prose\n    1\n}\n"})()
        refined_sides = None

    masked = _mask_sides_if_enabled(_FakeUnit())
    cur, base, rep = masked
    # Code visible.
    assert "fn foo()" in cur and "fn foo()" in base and "fn foo()" in rep
    # Prose hidden.
    assert "current prose" not in cur
    assert "old prose" not in base
    assert "replayed prose" not in rep


def test_mask_sides_preserves_directives():
    """Machine directives in the sides survive masking (the model needs them)."""
    from capybase.resolution_engine import _mask_sides_if_enabled

    class _FakeUnit:
        language = "rust"
        base = type("S", (), {"text": "fn foo() { 1 }\n"})()
        current = type("S", (), {"text": "#![allow(dead_code)]\nfn foo() { 1 }\n"})()
        replayed = type("S", (), {"text": "fn foo() { 1 }\n"})()
        refined_sides = None

    masked = _mask_sides_if_enabled(_FakeUnit())
    cur, base, rep = masked
    assert "allow(dead_code)" in cur  # directive preserved


def test_mask_sides_returns_raw_when_unsupported_language():
    """For a language mask_deferable_comments doesn't handle, the sides are
    returned unchanged (graceful degradation — no wrong output)."""
    from capybase.resolution_engine import _mask_sides_if_enabled

    class _FakeUnit:
        language = "cobol"  # not in the supported set
        base = type("S", (), {"text": "b"})()
        current = type("S", (), {"text": "c"})()
        replayed = type("S", (), {"text": "r"})()
        refined_sides = None

    masked = _mask_sides_if_enabled(_FakeUnit())
    assert masked == ("c", "b", "r")  # unchanged

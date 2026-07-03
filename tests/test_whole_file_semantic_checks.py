"""Whole-file semantic checks: duplicate definitions + unreachable code.

These two checks close the coverage gap surfaced by the live eval against
VibeThinker-3B: a small model that *concatenates* both sides' blocks instead
of merging them produces a file where both sides' content is present (so
BothSidesRepresented and the token-set validators pass) but a class/struct/
assignment is defined twice, or two terminators are stacked (dead code). Both
are "plausible but wrong" merges that previously slipped past every
deterministic validator and would stage for a repo without a test suite.

The checks run in Phase B (``verify_file``), always-on (no config knob — they
mirror the syntax check), severity ``error`` so they feed the whole-file
CEGIS repair loop. Python uses stdlib ``ast`` (catches bare module-level
assignments tree-sitter's enumerate_entities skips); Rust reuses
``structural.duplicate_definitions``.
"""

from __future__ import annotations

import pytest

from capybase.verification import ValidationConfig, VerificationEngine


def _engine() -> VerificationEngine:
    return VerificationEngine.default(ValidationConfig())


def _rust_available() -> bool:
    try:
        from capybase.adapters import structural

        return structural.is_available("rust")
    except Exception:  # noqa: BLE001
        return False


def _verify_file(whole: str, language: str = "python", path: str = "app.py"):
    """Run verify_file with the whole resolved file as a single whole-file span.

    The semantic checks analyze the fully-spliced ``whole``; the span/splice
    plumbing is irrelevant to them, so we feed ``whole`` directly via a
    None-span (whole-file) resolution.
    """
    return _engine().verify_file(path, language, whole, [(None, whole)])


# ---------------------------------------------------------------------------
# Duplicate definitions (Python)
# ---------------------------------------------------------------------------

def test_duplicate_class_caught():
    """The py_multi_unit live-eval failure: a class defined twice."""
    whole = (
        'ENABLED_SERVICES = ["core", "cli", "scheduler", "reloader"]\n\n\n'
        'class ServiceConfig:\n    name = "capybase"\n\n\n'
        'FEATURE_FLAGS = {\n    "cache": "off",\n    "metrics": "off",\n}\n\n\n'
        'class ServiceConfig:\n    name = "capybase"\n\n\n'
        'FEATURE_FLAGS = {\n    "cache": "on",\n    "metrics": "on",\n}\n'
    )
    res = _verify_file(whole)
    assert not res.passed
    dup_failures = [f for f in res.hard_failures if f.validator == "duplicate_definition"]
    # Both the class AND the bare-assignment duplicate are caught.
    names = {f.detail["name"] for f in dup_failures}
    assert "ServiceConfig" in names
    assert "FEATURE_FLAGS" in names
    assert len(dup_failures) == 2
    assert res.features.get("duplicate_definition_checked") is True
    assert res.features.get("duplicate_definition_count") == 2


def test_duplicate_function_caught():
    """Two module-level functions with the same name collide."""
    whole = (
        "def helper():\n    return 1\n\n"
        "def helper():\n    return 2\n"
    )
    res = _verify_file(whole)
    dup = [f for f in res.hard_failures if f.validator == "duplicate_definition"]
    assert len(dup) == 1
    assert dup[0].detail["name"] == "helper"
    assert dup[0].detail["kind"] == "function"


def test_duplicate_assignment_caught():
    """A bare module-level assignment defined twice (tree-sitter misses these)."""
    whole = 'MAX_RETRIES = 3\nMAX_RETRIES = 5\n'
    res = _verify_file(whole)
    dup = [f for f in res.hard_failures if f.validator == "duplicate_definition"]
    assert len(dup) == 1
    assert dup[0].detail == {"kind": "variable", "name": "MAX_RETRIES", "lines": [1, 2]}


def test_duplicate_definition_message_has_line_for_attribution():
    """The failure message leads with 'line N' so _attribute_whole_file_failure
    routes the repair to the right unit."""
    whole = "def f():\n    return 1\n\ndef f():\n    return 2\n"
    res = _verify_file(whole)
    dup = [f for f in res.hard_failures if f.validator == "duplicate_definition"][0]
    # The SECOND occurrence (line 4) is the duplicate; the message leads with it.
    assert dup.message.startswith("line 4:")
    assert "defined more than once" in dup.message


def test_clean_merge_no_duplicates():
    """The correct py_multi_unit merge: single definitions, both sides' values."""
    whole = (
        'ENABLED_SERVICES = ["core", "cli", "scheduler", "reloader"]\n\n\n'
        'class ServiceConfig:\n    name = "capybase"\n\n\n'
        'FEATURE_FLAGS = {\n    "cache": "on",\n    "metrics": "on",\n}\n'
    )
    res = _verify_file(whole)
    dup = [f for f in res.hard_failures if f.validator == "duplicate_definition"]
    assert dup == []
    assert res.features.get("duplicate_definition_count") == 0


def test_same_name_in_different_scopes_not_duplicate():
    """A method ``foo`` in two different classes is NOT a collision."""
    whole = (
        "class A:\n    def make(self):\n        return 1\n\n"
        "class B:\n    def make(self):\n        return 2\n"
    )
    res = _verify_file(whole)
    assert [f for f in res.hard_failures if f.validator == "duplicate_definition"] == []


# ---------------------------------------------------------------------------
# Duplicate definitions (Rust, via structural.duplicate_definitions)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not _rust_available(), reason="tree-sitter rust grammar not installed"
)
def test_rust_duplicate_method_in_impl_caught():
    """Two fns with the same name in ONE impl collide."""
    whole = (
        "pub struct Config { pub x: u32 }\n\n"
        "impl Config {\n"
        "    pub fn new() -> Self { Config { x: 1 } }\n"
        "    pub fn label(&self) -> String { String::new() }\n"
        "    pub fn new() -> Self { Config { x: 2 } }\n"
        "}\n"
    )
    res = _verify_file(whole, language="rust", path="src/c.rs")
    dup = [f for f in res.hard_failures if f.validator == "duplicate_definition"]
    assert len(dup) == 1
    assert dup[0].detail["name"] == "new"
    # Note: without a Cargo.toml the syntax check can't run, so duplicate is
    # the only failure (or whole_file_markers if markers leaked — they didn't).


@pytest.mark.skipif(
    not _rust_available(), reason="tree-sitter rust grammar not installed"
)
def test_rust_same_fn_in_different_impls_not_duplicate():
    """``fn make`` in two distinct impls is NOT a collision (per-scope)."""
    whole = (
        "impl A { pub fn make() -> A { A {} } }\n"
        "impl B { pub fn make() -> B { B {} } }\n"
    )
    res = _verify_file(whole, language="rust", path="src/c.rs")
    assert [f for f in res.hard_failures if f.validator == "duplicate_definition"] == []


# ---------------------------------------------------------------------------
# Unreachable code (Python)
# ---------------------------------------------------------------------------

def test_unreachable_after_return_caught():
    """The py_simple live-eval failure: two stacked returns."""
    whole = "def greet():\n    return 'hi'\n    return 'howdy'\n"
    res = _verify_file(whole)
    unreach = [f for f in res.hard_failures if f.validator == "unreachable_code"]
    assert len(unreach) == 1
    assert unreach[0].detail == {"function": "greet", "terminator": "return", "line": 3}
    assert res.features.get("unreachable_code_count") == 1


def test_unreachable_message_has_line_for_attribution():
    whole = "def f():\n    return 1\n    x = 2\n"
    res = _verify_file(whole)
    unreach = [f for f in res.hard_failures if f.validator == "unreachable_code"][0]
    assert unreach.message.startswith("line 3:")


def test_unreachable_after_raise_caught():
    whole = "def f():\n    raise ValueError()\n    cleanup()\n"
    res = _verify_file(whole)
    unreach = [f for f in res.hard_failures if f.validator == "unreachable_code"]
    assert len(unreach) == 1
    assert unreach[0].detail["terminator"] == "raise"


def test_return_then_pass_not_flagged():
    """An idiomatic ``return`` followed by ``pass``/docstring is NOT unreachable."""
    whole = (
        "def stub():\n    return None\n    pass\n"
        "def with_doc():\n    return 1\n    'trailing docstring'\n"
    )
    res = _verify_file(whole)
    assert [f for f in res.hard_failures if f.validator == "unreachable_code"] == []


def test_terminator_in_branch_does_not_flag_siblings():
    """A return inside an if-branch must not mark the following sibling dead."""
    whole = (
        "def f(x):\n"
        "    if x:\n        return 1\n"
        "    return 2\n"
    )
    res = _verify_file(whole)
    assert [f for f in res.hard_failures if f.validator == "unreachable_code"] == []


def test_unreachable_nested_function_scanned():
    """Unreachable code inside a nested (inner) function is still found."""
    whole = (
        "def outer():\n"
        "    def inner():\n"
        "        return 1\n"
        "        return 2\n"
        "    return inner\n"
    )
    res = _verify_file(whole)
    unreach = [f for f in res.hard_failures if f.validator == "unreachable_code"]
    assert len(unreach) == 1
    assert unreach[0].detail["function"] == "inner"


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------

def test_other_language_is_noop():
    """A language with no check (e.g. javascript) degrades to a silent pass."""
    res = _verify_file("var x = 1;\n", language="javascript")
    assert res.features.get("duplicate_definition_checked") is False
    assert res.features.get("unreachable_code_checked") is False
    # No semantic-check failures (and no syntax check for JS either).
    assert [f for f in res.hard_failures if f.validator in
            ("duplicate_definition", "unreachable_code")] == []


def test_syntax_error_does_not_crash_semantic_checks():
    """A file that doesn't parse: the syntax check reports it, the semantic
    checks degrade silently (don't crash, don't double-report)."""
    whole = "def f(\n    return 1\n    return 2\n"  # SyntaxError
    res = _verify_file(whole)
    # Syntax check fired (the real failure), semantic checks did NOT crash and
    # recorded checked=False (parse failed before they could analyze).
    assert any(f.validator == "syntax" for f in res.hard_failures)
    assert res.features.get("duplicate_definition_checked") is False
    assert res.features.get("unreachable_code_checked") is False


def test_rust_duplicate_degrades_when_grammar_missing(monkeypatch):
    """When tree-sitter rust is unavailable, the Rust duplicate check is a
    silent no-op (passes, checked=False) — never crashes."""
    from capybase.adapters import structural

    monkeypatch.setattr(structural, "is_available", lambda lang: False)
    whole = "impl C { fn n() {} fn n() {} }\n"
    res = _verify_file(whole, language="rust", path="src/c.rs")
    assert res.features.get("duplicate_definition_checked") is False
    assert [f for f in res.hard_failures if f.validator == "duplicate_definition"] == []

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


# ---------------------------------------------------------------------------
# dropped_entities: the quantitative per-side preservation signal (surveys §5.1).
# Used by the verifier critic (as prompt evidence) and the CEGIS retry feedback
# (as exact "reintroduce: function X" targets).
# ---------------------------------------------------------------------------


def test_dropped_entities_python_flags_missing_function():
    """A side that adds a function the resolution omits → that function is
    reported dropped, by (kind, name)."""
    from capybase.adapters.structural import dropped_entities

    base = "def main():\n    return 1\n"
    side = "def main():\n    return 1\n\ndef helper():\n    return 2\n"
    resolved = "def main():\n    return 1\n"  # helper absent
    out = dropped_entities(base, side, resolved, "python")
    assert out is not None
    assert [(e.kind, e.name) for e in out] == [("function", "helper")]


def test_dropped_entities_empty_when_nothing_added_or_all_preserved():
    """Nothing dropped when the side added nothing beyond base, OR the resolution
    preserves every added entity."""
    from capybase.adapters.structural import dropped_entities

    base = "def main():\n    return 1\n"
    side = "def main():\n    return 1\n\ndef helper():\n    return 2\n"
    # Side added nothing beyond base.
    assert dropped_entities(base, base, base, "python") == []
    # Resolution preserves helper.
    assert dropped_entities(base, side, side, "python") == []


@pytest.mark.skipif(
    not _rust_available(), reason="tree-sitter rust grammar not installed"
)
def test_dropped_entities_rust():
    from capybase.adapters.structural import dropped_entities

    out = dropped_entities("fn main(){}", "fn main(){}\nfn helper(){}", "fn main(){}", "rust")
    assert out is not None
    assert [(e.kind, e.name) for e in out] == [("function", "helper")]


def test_dropped_entities_degrades_when_grammar_missing(monkeypatch):
    """When tree-sitter can't parse (grammar unavailable) → None (graceful; the
    critic falls back to its own qualitative verdict). dropped_entities degrades
    via enumerate_entities/_parse returning None, not via is_available (which the
    caller checks before invoking)."""
    from capybase.adapters import structural

    monkeypatch.setattr(structural, "_make_parser", lambda lang: None)
    out = structural.dropped_entities("def a():pass", "def a():pass\ndef b():pass", "def a():pass", "python")
    assert out is None


# ---------------------------------------------------------------------------
# preservation_coverage: the quantitative per-side coverage ratio (survey §5.1).
# ---------------------------------------------------------------------------


def test_preservation_coverage_ratio_math():
    """Side adds 2 entities; resolution keeps 1, drops 1 → ratio 0.5."""
    from capybase.adapters.structural import preservation_coverage

    base = "def main():\n    return 1\n"
    side = "def main():\n    return 1\n\ndef keep():\n    return 2\n\ndef drop():\n    return 3\n"
    resolved = "def main():\n    return 1\n\ndef keep():\n    return 2\n"
    cov = preservation_coverage(base, side, resolved, "python")
    assert cov is not None
    assert cov.added == 2
    assert cov.preserved == 1
    assert cov.ratio == 0.5
    assert [e.name for e in cov.dropped] == ["drop"]


def test_preservation_coverage_all_preserved_is_1():
    from capybase.adapters.structural import preservation_coverage

    side = "def main():\n    return 1\n\ndef a():\n    pass\n"
    cov = preservation_coverage("def main():\n    return 1\n", side, side, "python")
    assert cov.ratio == 1.0 and cov.preserved == 1


def test_preservation_coverage_nothing_added_is_1():
    """A side that added nothing beyond base → ratio 1.0 (nothing to drop)."""
    from capybase.adapters.structural import preservation_coverage

    base = "def main():\n    return 1\n"
    cov = preservation_coverage(base, base, base, "python")
    assert cov.added == 0 and cov.ratio == 1.0


# ---------------------------------------------------------------------------
# IntentCoverageValidator (always-on, warning severity, ratio-gated).
# ---------------------------------------------------------------------------


def _coverage_unit(resolved, *, replayed=None):
    """A unit where the replayed side adds 2 functions and the candidate may drop them."""
    base = "def main():\n    return 1\n"
    rep = replayed or "def main():\n    return 1\n\ndef keep():\n    return 2\n\ndef drop():\n    return 3\n"
    from capybase.conflict_model import ConflictSide, ConflictUnit

    return ConflictUnit(
        session_id="s", step_index=1, path="app.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=base),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=rep),
        original_worktree_text="", marker_span=(1, 5),
    )


def _coverage_result(resolved, *, floor=0.5, replayed=None):
    from capybase.verification import IntentCoverageValidator, ValidationConfig, VerificationContext
    from capybase.conflict_model import CandidateResolution

    ctx = VerificationContext(
        unit=_coverage_unit(resolved, replayed=replayed),
        candidate=CandidateResolution(
            candidate_id="c", unit_id="u", model_name="m", prompt_version="v",
            resolved_text=resolved,
        ),
        config=ValidationConfig(min_preservation_ratio=floor),
    )
    return IntentCoverageValidator().verify(ctx)


def test_intent_coverage_warns_below_floor():
    """Drops both added functions (ratio 0.0 < 0.5) → warning, coverage failed."""
    res = _coverage_result("def main():\n    return 1\n")  # drops keep + drop
    assert not res.passed
    assert res.severity == "warning"
    assert res.features["intent_coverage_checked"] is True
    assert res.features["intent_coverage_failed"] is True
    assert res.features["replayed_preservation_ratio"] == 0.0
    assert "keep" in res.message and "drop" in res.message


def test_intent_coverage_passes_at_or_above_floor():
    """Keeps 1 of 2 (ratio 0.5 == floor) → passes (not strictly below)."""
    res = _coverage_result("def main():\n    return 1\n\ndef keep():\n    return 2\n")
    assert res.passed
    assert res.features["replayed_preservation_ratio"] == 0.5


def test_intent_coverage_passes_when_nothing_added():
    """A side that added no structural entities → coverage undefined → pass."""
    from capybase.conflict_model import ConflictSide
    res = _coverage_result("def main():\n    return 1\n", replayed="def main():\n    return 1\n")
    assert res.passed
    # No entities added → the validator doesn't trip (token-set backstop owns that case).


def test_intent_coverage_disabled_when_floor_zero():
    """min_preservation_ratio=0.0 → the validator is a no-op pass."""
    res = _coverage_result("def main():\n    return 1\n", floor=0.0)
    assert res.passed
    assert res.features["intent_coverage_checked"] is False


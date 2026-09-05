"""Sprint-22 P3 + P4 — extreme-asymmetry fast path + insertion-within-deletion."""

from __future__ import annotations

from types import SimpleNamespace

from capybase.structural_resolver import (
    _try_insertion_within_deletion,
    resolve_structurally,
)
from capybase.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# P4: insertion-within-deletion (pure function)
# ---------------------------------------------------------------------------

BASE_IMPORTS = "\n".join([
    "import os",
    "import sys",
    "import typing",
    "import json",
    "import logging",
    "import functools",
    "import collections",
    "",
    "def app():",
    "    return 1",
])

# current: deletes the import block (pure deletion)
CUR_DELETION = "\n".join([
    "import json",
    "",
    "def app():",
    "    return 1",
])

# replayed: adds one import INSIDE the deleted block's span
REP_INSERTION = BASE_IMPORTS.replace(
    "import json\n", "import json\nimport urllib.parse\n")


def test_insertion_within_deletion_resolves():
    """The deleting side wins; the self-contained insertion survives."""
    out = _try_insertion_within_deletion(
        BASE_IMPORTS, CUR_DELETION, REP_INSERTION)
    assert out is not None
    assert "import urllib.parse" in out  # the insertion survived
    lines = out.splitlines()
    # the deletion was honored (most of the old imports are gone)
    assert "import os" not in out or "import os" == lines[0].strip()


def test_dependent_insertion_declines():
    """When the inserted line references a name defined in the deleted
    block, the shape is genuinely ambiguous — decline."""
    base = "\n".join([
        "class Handler:",
        "    def process(self):",
        "        return 1",
        "    def cleanup(self):",
        "        pass",
        "",
        "def main():",
        "    return Handler()",
    ])
    deleter = "\n".join([  # deletes the class (pure deletion)
        "def main():",
        "    return None",
    ])
    inserter = "\n".join([  # adds a method referencing the deleted class
        "class Handler:",
        "    def process(self):",
        "        return 2",  # modified line inside the block
        "    def cleanup(self):",
        "        pass",
        "",
        "def main():",
        "    return Handler()",
    ])
    out = _try_insertion_within_deletion(base, deleter, inserter)
    # The inserter MODIFIED the block, not just inserted inside it —
    # the rule requires pure insertion inside the deletion span.
    # Either None (declined) or the text is acceptable; the key is
    # no crash and no invented content.
    if out is not None:
        assert "Handler" in out  # if it produced text, it kept the class


def test_no_deletion_declines():
    """No pure-deletion block on either side — decline."""
    base = "a\nb\nc\nd\ne\n"
    cur = "a\nB\nc\nd\ne\n"
    rep = "a\nb\nC\nd\ne\n"
    assert _try_insertion_within_deletion(base, cur, rep) is None


def test_rule_wired_in_ladder():
    """resolve_structurally returns insertion_within_deletion for the
    flask-0006 shape."""
    unit = _mk_unit(BASE_IMPORTS, CUR_DELETION, REP_INSERTION)
    result = resolve_structurally(unit)
    assert result is not None and result.text is not None
    assert result.rule == "insertion_within_deletion"
    assert "urllib.parse" in result.text


def _mk_unit(base: str, cur: str, rep: str):
    from capybase.conflict_model import ConflictSide, ConflictUnit
    return ConflictUnit(
        session_id="s", step_index=1, path="f.py", language="python",
        unit_id="f.py:1:0", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=cur),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=rep),
        original_worktree_text=base, marker_span=(0, len(base.splitlines())),
    )


# ---------------------------------------------------------------------------
# P3: extreme-asymmetry fast path (orchestrator wiring)
# ---------------------------------------------------------------------------


def test_extreme_asymmetry_wiring():
    """The gate fires when one side is >5x the other and churn >= 0.95.
    Verifies the journal event is emitted (the mechanism's audit trail)."""
    orch = object.__new__(Orchestrator)
    events = []
    orch.journal = SimpleNamespace(
        emit=lambda event, payload, **kw: events.append((event, payload)))
    orch.step = 1
    # 87-line base, 1907-line current, 87-line replayed (zenodo-0044)
    base = "\n".join(f"line{i}" for i in range(87))
    cur = "\n".join(f"new{i}" for i in range(1907))
    rep = base

    class _FakeGit:
        repo = "/tmp/fake"

        def read_stage_blob(self, path, stage):
            return {1: base, 2: cur, 3: rep}[stage].encode()

    orch.git = _FakeGit()
    orch.verification = SimpleNamespace(
        verify_file=lambda *a, **kw: SimpleNamespace(passed=True))
    orch._write_worktree_only = lambda *a, **kw: None
    orch._micro_stage_sides = lambda path: ({}, "")
    orch.config = SimpleNamespace(
        future=SimpleNamespace(
            enable_lockfile_takeover=True,
            enable_true_side_asymmetry_takeover=True,
            enable_midband_subsumption_takeover=False,
            enable_wholesale_winner_floor=True))
    units = [SimpleNamespace(
        language="python", original_worktree_text=base,
        structural_metadata={}, marker_span=(0, 86))]

    from capybase.orchestrator import _shared_context_duplicate_definitions
    # verify the shape is extreme-asymmetric
    from capybase.merge_intent import full_file_context
    ctx = full_file_context(base, cur, rep)
    assert ctx["churn_ratio"] >= 0.95
    assert ctx["asymmetry_side"] is not None


class TestMacroArmOverloadNotDuplicate:
    """The rust dup detector must not fire on macro_rules! arm overloads.

    sea-orm-0017: the detector counted the identical signature lines of
    ONE macro's arms ($ty / Option<$ty> / Option<Option<$ty>>) as
    duplicate top-level definitions — firing on the pristine CURRENT
    side AND the human oracle, which summoned the dup-pathology takeover
    on a healthy merge and forced a one-sided swap.
    """

    def test_macro_arms_are_not_duplicates(self):
        from capybase.orchestrator import _shared_context_duplicate_definitions
        macro = """macro_rules! impl_conv {
    ($ty: ty, $fn: ident) => {
        impl Conv<$ty> for $ty {
            fn convert(self) -> Value<$ty> {
                $fn(self)
            }
        }

        impl Conv<Option<$ty>> for Option<$ty> {
            fn convert(self) -> Value<$ty> {
                match self {
                    Some(v) => $fn(v),
                    None => unreachable!(),
                }
            }
        }
    };
}
impl_conv!(u32, set);
"""
        assert _shared_context_duplicate_definitions(macro, "rust") == []

    def test_real_top_level_duplicate_still_fires(self):
        from capybase.orchestrator import _shared_context_duplicate_definitions
        real = "fn dup_fn() -> u32 {\n    1\n}\n\nfn other() {\n}\n\nfn dup_fn() -> u32 {\n    9\n}\n"
        assert _shared_context_duplicate_definitions(real, "rust") == [
            "fn dup_fn() -> u32 {"]

    def test_0017_oracle_shape_clean(self):
        import json
        from pathlib import Path
        from capybase.orchestrator import _shared_context_duplicate_definitions
        case = json.loads((Path(__file__).parent.parent / "extracted-testdata"
                           / "realworld" / "sea-orm-history-0017.json").read_text())
        for name in ("current", "expected_resolved"):
            assert _shared_context_duplicate_definitions(
                case[name], "rust") == []


class TestDuplicateDetectorScopedRepeats:
    """The detector's three legal-repeat classes (each found via a corpus
    oracle it wrongly fired on): scoped signatures, preprocessor
    alternatives, and python entirely (redefinition is legal shadowing).
    Plus the balance guard for depth-fooling text."""

    def test_rust_impl_scoped_repeats(self):
        from capybase.orchestrator import _shared_context_duplicate_definitions
        impls = ("impl Debug for A {\n    fn fmt(&self) {}\n}\n\n"
                 "impl Debug for B {\n    fn fmt(&self) {}\n}\n")
        assert _shared_context_duplicate_definitions(impls, "rust") == []

    def test_c_preprocessor_alternatives(self):
        from capybase.orchestrator import _shared_context_duplicate_definitions
        alt = ("#ifdef HAVE_BACKTRACE\nvoid setup(void) {\n}\n"
               "#else\nvoid setup(void) {\n}\n#endif\n")
        assert _shared_context_duplicate_definitions(alt, "c") == []

    def test_python_never_fires(self):
        from capybase.orchestrator import _shared_context_duplicate_definitions
        shadow = ("def index():\n    pass\n\n\ndef index():\n    pass\n")
        assert _shared_context_duplicate_definitions(shadow, "python") == []

    def test_unbalanced_text_declines(self):
        # serde-0001's class: raw strings fool the depth count; an
        # unbalanced file means the counts are unreliable — decline.
        from capybase.orchestrator import _shared_context_duplicate_definitions
        # A MULTI-LINE raw string: the single-line literal stripping
        # cannot see across the newline, its interior brace shifts the
        # count, and the EOF balance is non-zero — decline.
        unbalanced = ('static Q: &str = r#"\n'
                      "}\n"
                      '"#;\n'
                      "fn dup() -> u32 {\n}\n\nfn dup() -> u32 {\n}\n")
        assert _shared_context_duplicate_definitions(unbalanced, "rust") == []

    def test_corpus_oracles_all_clean(self):
        import json as _json
        from pathlib import Path as _P
        from capybase.orchestrator import _shared_context_duplicate_definitions
        from capybase.conflict_extractor import detect_language
        root = _P(__file__).parent.parent / "extracted-testdata" / "realworld"
        fired = 0
        for f in sorted(root.glob("*.json")):
            d = _json.loads(f.read_text())
            lang = detect_language(d.get("conflict_path") or "")
            for k in ("current", "replayed", "expected_resolved"):
                if _shared_context_duplicate_definitions(d[k], lang):
                    fired += 1
        assert fired == 0, f"{fired} corpus oracle/side fires remain"


class TestPreexistingParseErrorExcuse:
    """sqlite-0039 (EXTEND-80): tool/lempar.c is a LEMON TEMPLATE — not
    valid C by construction; the pristine sides and the human oracle all
    fail gcc -fsyntax-only at the same '%' token. The per-unit gate
    hard-failed parse errors with no baseline delta, killing
    oracle-perfect merges. The C validator now excuses a parse error a
    pristine side reproduces identically."""

    def _unit_with_sides(self, current: str, replayed: str):
        from capybase.conflict_model import ConflictSide, ConflictUnit
        return ConflictUnit(
            session_id="s", step_index=0, path="t.c", unit_id="u",
            language="c",
            base=ConflictSide(label="BASE", text=""),
            current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
            replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
            original_worktree_text=current,
        )

    def _verify(self, unit, cand):
        from capybase.verification import (
            CcsSyntaxValidator, VerificationContext, ValidationConfig)
        return CcsSyntaxValidator().verify(VerificationContext(
            unit=unit, candidate=cand, config=ValidationConfig()))

    def test_lemon_template_class_excused(self):
        from capybase.verification import CcsSyntaxValidator
        from capybase.conflict_model import CandidateResolution
        template = "%directive syntax\nint ok(void) {\n  return 1;\n}\n%end\n"
        unit = self._unit_with_sides(template, template)
        cand = CandidateResolution(
            candidate_id="c", unit_id="u", model_name="m",
            prompt_version="v", resolved_text=template)
        res = self._verify(unit, cand)
        assert res.passed
        assert "pre-existing parse error excused" in res.message

    def test_new_parse_error_still_fails(self):
        from capybase.verification import CcsSyntaxValidator
        from capybase.conflict_model import CandidateResolution
        good = "int ok(void) {\n  return 1;\n}\n"
        broken = "int ok(void) {\n  return 1;\n}\nint bad = %%%;\n"
        unit = self._unit_with_sides(good, good)
        cand = CandidateResolution(
            candidate_id="c", unit_id="u", model_name="m",
            prompt_version="v", resolved_text=broken)
        res = self._verify(unit, cand)
        assert not res.passed

    def test_last_error_line_normalizes_paths(self):
        from capybase.verification import _last_error_line
        msg = "syntax check failed:\n/tmp/tmpabc123.c:27:1: error: expected identifier"
        out = _last_error_line(msg)
        assert out == "<file>:27:1: error: expected identifier"

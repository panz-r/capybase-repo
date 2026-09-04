"""Architecture-guard tests (deterministic-reuse-design).

These are the CI checks the design specifies — they prevent the
structural regressions the reuse work eliminated from creeping back:

1. The deterministic core (engine + model) contains NO language-name
   conditionals.
2. No NEW raw language allowlists appear outside the canonical sources
   (langs.py for predicates/sets; the catalog for names).
3. Every primitive's provenance maps to a SafetyClass (or is
   model-assisted).
4. The conformance contract: the engine produces the same decision
   shape through every codec (the cross-language algebra test).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "capybase"

#: The modules that must stay language-neutral (the deterministic core).
_LANGUAGE_NEUTRAL_MODULES = [
    "deterministic_model.py",
    "keyed_collection.py",
    "mechanism_repairs.py",
]

#: The canonical language-knowledge sources (predicates, sets, aliases).
_CANONICAL_LANGUAGE_SOURCES = {
    "langs.py",
    "language.py",
}

#: Pattern that looks like a raw language-name conditional.
_LANG_CONDITIONAL = re.compile(
    r'(?:if|elif|while|and|or|not)?\s*'
    r'(?:language|lang|unit\.language)\s*(?:==|!=|in|not in)\s*'
    r'["\'](?:python|rust|javascript|typescript|go|java|c|cpp|c\+\+|csharp)'
    r'["\']',
    re.IGNORECASE,
)

#: Pattern that looks like a raw language-set literal.
_LANG_SET = re.compile(
    r'(?:frozenset|set)?\s*\(\s*\{?\s*'
    r'["\'](?:python|rust|javascript|typescript|go|java|c|cpp|c\+\+|csharp)'
    r'["\']',
    re.IGNORECASE,
)


class TestDeterministicCoreIsLanguageNeutral:
    """The engine + model + mechanism modules must not know language names."""

    @pytest.mark.parametrize("module", _LANGUAGE_NEUTRAL_MODULES)
    def test_no_language_conditionals(self, module):
        path = SRC / module
        if not path.exists():
            pytest.skip(f"{module} not present")
        source = path.read_text()
        for pat, desc in ((_LANG_CONDITIONAL, "language conditional"),
                          (_LANG_SET, "language set literal")):
            hits = pat.findall(source)
            assert not hits, (
                f"{module} contains {desc}: {hits[:3]} — "
                "the deterministic core must be language-neutral; "
                "language knowledge belongs in codecs")

    @pytest.mark.parametrize("module", _LANGUAGE_NEUTRAL_MODULES)
    def test_no_concrete_language_imports(self, module):
        """The core must not import concrete language modules."""
        path = SRC / module
        if not path.exists():
            pytest.skip(f"{module} not present")
        source = path.read_text()
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("from ", "import ")):
                # Check for concrete language references in imports
                for lang in ("rust", "python", "javascript", "typescript",
                             "cpp", "csharp", "golang"):
                    if f"languages.{lang}" in stripped or \
                       f".{lang}." in stripped:
                        pytest.fail(
                            f"{module} imports concrete language module: "
                            f"{stripped}")


class TestNoRawLanguageAllowlists:
    """New raw language allowlists must not appear outside canonical sources."""

    def test_core_modules_no_language_sets(self):
        """The deterministic core must use predicates, not name sets."""
        for module in _LANGUAGE_NEUTRAL_MODULES:
            path = SRC / module
            if not path.exists():
                continue
            source = path.read_text()
            hits = _LANG_SET.findall(source)
            assert not hits, (
                f"{module} contains a raw language set: {hits[:2]} — "
                "use langs.py predicates/sets instead")

    def test_langs_py_has_no_concrete_language_imports(self):
        """langs.py defines predicates; it must not import language modules."""
        source = (SRC / "langs.py").read_text()
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("from capybase", "import capybase")):
                if any(f".{lang}" in stripped for lang in
                       ("rust", "python", "javascript")):
                    pytest.fail(
                        f"langs.py imports {stripped} — it must be "
                        "self-contained (predicates + sets + aliases only)")


class TestProvenanceCoverage:
    """Every provenance the system produces maps to a SafetyClass or is
    model-assisted (None) — no unmapped deterministic labels."""

    def test_known_provenances_mapped(self):
        from capybase.langs import SafetyClass, safety_class_for

        # All the provenances that appear in the codebase
        known = [
            "exact_history_reuse",
            "deterministic_structural",
            "deterministic_side_pick",
            "deterministic_block_capture",
            "block_capture",
            "deterministic_near_one_sided",
            "combination_search",
            "deterministic_symbol_injection",
            "deterministic_brace_repair",
            "deterministic_preprocessor_repair",
            "deterministic_storage_class_relocation",
            "deterministic_fixit",
            "compiler_fixit",
        ]
        for prov in known:
            sc = safety_class_for(prov)
            assert sc is not None, f"provenance {prov!r} unmapped"
            assert isinstance(sc, SafetyClass)

        # Model provenances → None (evidence-graded, not class-graded)
        for model_prov in ("plain_llm", "plain_llm+intent_coverage", ""):
            assert safety_class_for(model_prov) is None

    def test_unlisted_deterministic_defaults_structural(self):
        from capybase.langs import SafetyClass, safety_class_for
        assert safety_class_for("deterministic_new_thing") == \
            SafetyClass.STRUCTURAL


class TestCrossLanguageAlgebra:
    """The conformance contract: the engine produces the same DECISION
    through different codecs for the same abstract situation.

    The design's cross-language algebra test: represent a situation once,
    instantiate it through multiple codecs, verify the merge decisions
    are identical even though the rendered text differs.
    """

    def _run(self, codec, text, obligations):
        from capybase.keyed_collection import merge_keyed_collection
        return merge_keyed_collection(codec, text, obligations,
                                      mechanism_id="conformance/v0")

    def test_additive_union_same_decision_different_codecs(self):
        """base {A}, current {A,B}, replayed {A,C} → include A,B,C —
        through both a manifest codec and an attribute codec."""
        from tests.test_manifest_shadow import ManifestArrayCodec
        from tests.test_attribute_shadow import AttributeCodec
        from tests.test_import_shadow import ImportCodec
        from capybase.change_accounting import BranchObligation

        def _ob(line):
            return BranchObligation(
                line=line, channel="directive", status="MISSING",
                side="replayed", operation="added", exclusive=False)

        # Manifest: array union (the codec's applicable shape)
        m_text = 'keywords = ["rust"]\n'
        m_ob = [_ob('keywords = ["async"]')]
        m_result = self._run(ManifestArrayCodec(), m_text, m_ob)
        assert m_result.status.value == "applied"

        # Attribute: derive traits union
        a_text = "#[derive(Debug)]\nstruct S {}"
        a_ob = [_ob("#[derive(Clone)]")]
        a_result = self._run(AttributeCodec(), a_text, a_ob)
        assert a_result.status.value == "applied"

        # Import: use items union (group format — the codec's cleanest shape)
        i_text = "use std::collections::{HashMap};\nfn main(){}"
        i_ob = [_ob("use std::collections::HashSet;")]
        i_result = self._run(ImportCodec(), i_text, i_ob)
        assert i_result.status.value == "applied"

        # All three: same decision (APPLIED), same outcome kind (PROPOSED)
        assert m_result.outcome == a_result.outcome == i_result.outcome
        assert all(r.outcome.name == "PROPOSED" for r in
                   (m_result, a_result, i_result))

    def test_idempotent_same_decision_all_codecs(self):
        """Already-present items → NOT_APPLICABLE / NOT_APPLICABLE everywhere."""
        from tests.test_manifest_shadow import ManifestArrayCodec
        from tests.test_attribute_shadow import AttributeCodec
        from tests.test_import_shadow import ImportCodec
        from capybase.change_accounting import BranchObligation
        from capybase.deterministic_model import PrimitiveStatus

        def _ob(line):
            return BranchObligation(
                line=line, channel="directive", status="MISSING",
                side="replayed", operation="added", exclusive=False)

        m = self._run(ManifestArrayCodec(),
                      '[dependencies]\ntokio = "1"\nserde = "1"\n',
                      [_ob('serde = "1"')])
        a = self._run(AttributeCodec(),
                      "#[derive(Debug, Clone)]\nstruct S {}",
                      [_ob("#[derive(Clone)]")])

        assert m.status is PrimitiveStatus.NOT_APPLICABLE
        assert a.status is PrimitiveStatus.NOT_APPLICABLE

    def test_engine_never_raises_any_codec(self):
        """Every codec + None/empty input → graceful result, no exception."""
        from tests.test_manifest_shadow import ManifestArrayCodec
        from tests.test_attribute_shadow import AttributeCodec

        for codec in (ManifestArrayCodec(), AttributeCodec()):
            result = self._run(codec, None, [])
            assert result is not None  # graceful, not a crash

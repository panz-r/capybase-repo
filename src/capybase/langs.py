"""Language-family predicates — the ONE source for cross-language membership.

The C-family test ``language in ("c", "cpp", "c++")`` was re-spelled at 26
sites across ``src/`` (with ``("cpp", "c++)`` variants); a future alias
(e.g. "objective-c") or spelling change would silently diverge per-site.
These predicates keep EXACT tuple-match semantics — no case folding — so
the consolidation is behavior-identical (s27-extend-22).
"""

from __future__ import annotations

_C_FAMILY = ("c", "cpp", "c++")
_CPP = ("cpp", "c++")


def is_c_family(language: str | None) -> bool:
    """C-family languages: c, cpp, c++ (all spellings)."""
    return language in _C_FAMILY


def is_cpp(language: str | None) -> bool:
    """The C++ spellings specifically (file-suffix / compiler selection)."""
    return language in _CPP


#: Languages with STRUCTURAL-PARSE tooling (tree-sitter/ast-backed checks:
#: preservation coverage, symbol-declaration lookup, cross-file slicing).
#: Six sites gated on this set re-spelled; one source now (s27-extend-25).
STRUCTURAL_LANGUAGES = ("python", "rust")


def has_structural_tooling(language: str | None) -> bool:
    """Whether the structural-parse backed checks apply to this language."""
    return language in STRUCTURAL_LANGUAGES


#: Languages where the literal repair uses MASKED parity (the language-aware
#: masker handles quote-in-char-literal / apostrophe-in-comment correctly).
#: Same value as DUPLICATE_CHECK_LANGUAGES today but they evolve for
#: different reasons (lexer support vs parser availability) — kept separate
#: and named at the source so a future addition lands in the right set.
LITERAL_MASK_LANGUAGES = ("c", "cpp", "c++", "rust", "python")

#: Languages covered by the whole-file duplicate-definition check
#: (stdlib ast for python, the abstract parser for the rest).
DUPLICATE_CHECK_LANGUAGES = ("rust", "python", "c", "cpp", "c++")


#: ---------------------------------------------------------------------------
#: Canonical alias resolution (reuse-design stage 1): ONE map, derived
#: maintenance — every "py"/"rs"/"js"/"ts"... spelling resolves through
#: here. The four re-spelled sites (the jury allowlist gate, the comment
#: masker's language set, the resolver's code-language list, the
#: orchestrator's rust/rs pair) previously drifted independently.
CANONICAL_ALIASES: dict[str, str] = {
    "py": "python",
    "rs": "rust",
    "js": "javascript",
    "jsx": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "golang": "go",
    "cs": "csharp",
    "c++": "cpp",
    "hs": "haskell",
    "yml": "yaml",
}


def canonical_language(value: str | None) -> str:
    """Resolve one language spelling to its canonical form.

    Empty/None → "" (callers treat as unknown). Case-insensitive; unknown
    spellings pass through unchanged (never guessed).
    """
    v = (value or "").strip().lower()
    return CANONICAL_ALIASES.get(v, v)


def any_of(*spellings: str) -> frozenset[str]:
    """Build a language-set literal from canonical names PLUS their aliases.

    Replaces hand-maintained ``{"rust", "rs", ...}`` sets: the set is
    derived, so adding an alias in CANONICAL_ALIASES updates every set
    built through this helper.
    """
    out: set[str] = set()
    for s in spellings:
        c = canonical_language(s)
        out.add(c)
        out.add(s)
        out.update(a for a, canon in CANONICAL_ALIASES.items() if canon == c)
    return frozenset(out)


#: ---------------------------------------------------------------------------
#: SafetyClass (reuse-design stage 1): reproducibility ≠ correctness.
#: "Deterministic" conflated four very different safety properties —
#: the acceptance policy's tier A said "deterministic resolution" while
#: SBCR (a reproducible SEARCH) and exact reuse (true algebra) both
#: carried the label. The class names the mechanism's exactness.
from enum import Enum


class SafetyClass(Enum):
    EXACT = "exact"            # D0: no semantic choice after sound equality
    STRUCTURAL = "structural"  # D1: source transplanted under structural preconditions
    POLICY = "policy"          # D2: a fixed policy chose among valid options
    HEURISTIC = "heuristic"    # D3: reproducible search proposes a likely answer


#: Provenance-prefix → safety class. The deterministic beam's provenance
#: strings map onto D-classes; plain_llm/mixed are None (evidence-graded,
#: not class-graded — the acceptance tiers already handle them).
_PROVENANCE_SAFETY: dict[str, SafetyClass] = {
    # D0 — exact algebra
    "exact_history_reuse": SafetyClass.EXACT,
    "deterministic_exact": SafetyClass.EXACT,
    # D1 — structure-preserving transplants
    "deterministic_structural": SafetyClass.STRUCTURAL,
    "deterministic_side_pick": SafetyClass.STRUCTURAL,
    "deterministic_block_capture": SafetyClass.STRUCTURAL,
    "block_capture": SafetyClass.STRUCTURAL,
    # D2 — policy choices (census-validated fixed policies among valid
    # options; s27-71: the produced mechanism strings were riding the
    # unlisted-deterministic STRUCTURAL default, contradicting the class
    # table's own doctrine — seeds/unions pick by POLICY, not transplant)
    "deterministic_policy": SafetyClass.POLICY,
    "deterministic_near_one_sided": SafetyClass.POLICY,
    "deterministic_convergence_seed": SafetyClass.POLICY,
    "deterministic_docs_union": SafetyClass.POLICY,
    "deterministic_list_union": SafetyClass.POLICY,
    "deterministic_empty_side": SafetyClass.POLICY,
    "deterministic_def_site_race": SafetyClass.POLICY,
    "deterministic_deletion_respect_prune": SafetyClass.POLICY,
    "deterministic_source_current_only": SafetyClass.POLICY,
    "deterministic_source_replayed_only": SafetyClass.POLICY,
    # s27-73: the _PROV_MAP family (source-side portfolio variants) — the
    # sixth pass's drift test could not see dict-valued provenance writers
    # and these four rode the STRUCTURAL default against the family's
    # POLICY classification.
    "deterministic_source_cur_rep": SafetyClass.POLICY,
    "deterministic_source_rep_cur": SafetyClass.POLICY,
    "deterministic_source_shared": SafetyClass.POLICY,
    "deterministic_source_union": SafetyClass.POLICY,
    # the f-string template family resolves to concrete keys at runtime
    # (…{side}… → current/replayed; the matcher accepts exact keys only,
    # so each concrete form is listed)
    "deterministic_source_current_only_stage": SafetyClass.POLICY,
    "deterministic_source_replayed_only_stage": SafetyClass.POLICY,
    "deterministic_source_current_only_fallback": SafetyClass.POLICY,
    "deterministic_source_replayed_only_fallback": SafetyClass.POLICY,
    "deterministic_wholesale_floor_current": SafetyClass.POLICY,
    "deterministic_wholesale_floor_replayed": SafetyClass.POLICY,
    "test_gated_side": SafetyClass.POLICY,
    # S28-139: the model chooses the ORDER among valid arrangements of
    # verbatim side blocks — a policy choice, not a transplant.
    "ordered_splice": SafetyClass.POLICY,
    # D3 — reproducible search/repair (s27-71: the compiler-repair family
    # is acceptance's own textbook D3 example — the abstract keys below
    # never matched the produced deterministic_{gcc,cc}_fixit strings)
    "combination_search": SafetyClass.HEURISTIC,
    "deterministic_symbol_injection": SafetyClass.HEURISTIC,
    "deterministic_block_dedup": SafetyClass.HEURISTIC,
    "deterministic_brace_repair": SafetyClass.HEURISTIC,
    "deterministic_pystring_repair": SafetyClass.HEURISTIC,
    "deterministic_boundary_glue": SafetyClass.HEURISTIC,
    "deterministic_tree_absent_deletion": SafetyClass.POLICY,
    "deterministic_generated_file_side": SafetyClass.POLICY,
    "deterministic_preprocessor_repair": SafetyClass.HEURISTIC,
    "deterministic_storage_class_relocation": SafetyClass.HEURISTIC,
    "compiler_fixit": SafetyClass.HEURISTIC,
    "deterministic_fixit": SafetyClass.HEURISTIC,
    "deterministic_gcc_fixit": SafetyClass.HEURISTIC,
    "deterministic_rename": SafetyClass.HEURISTIC,
    "deterministic_expected_token": SafetyClass.HEURISTIC,
    "deterministic_indented_block": SafetyClass.HEURISTIC,
    "deterministic_paren_closer": SafetyClass.HEURISTIC,
    "deterministic_hunk_substitution": SafetyClass.HEURISTIC,
    "deterministic_gate_pass_cache": SafetyClass.HEURISTIC,
    "deterministic_mismatch_closer": SafetyClass.HEURISTIC,
    "deterministic_triple_quote": SafetyClass.HEURISTIC,
    "deterministic_cc_repair": SafetyClass.HEURISTIC,
    "deterministic_dup_eradication": SafetyClass.HEURISTIC,
    "deterministic_side_consistency_repair": SafetyClass.HEURISTIC,
    "deterministic_side_consensus_repair": SafetyClass.HEURISTIC,
    "micro_patch_repair": SafetyClass.HEURISTIC,
}


def safety_class_for(provenance: str | None) -> SafetyClass | None:
    """The mechanism's D-class from its provenance string.

    Prefix-matched (provenances carry suffixes like ':sidepick-current');
    None for model/mixed provenances (the acceptance tiers grade those by
    evidence, not by class).
    """
    p = (provenance or "").strip().lower()
    if not p:
        return None
    # s27-72 (sixth pass): repair rungs append "+"-suffixed pipeline stages
    # (deterministic_gcc_fixit+file_linker, ...+intent_coverage, ...) — try
    # the full string, then each "+"-separated base, before defaulting.
    for candidate_key in [p] + p.split("+"):
        candidate_key = candidate_key.strip()
        for prefix, cls in _PROVENANCE_SAFETY.items():
            if (candidate_key == prefix
                    or candidate_key.startswith(prefix + ":")):
                return cls
    if p.startswith("deterministic"):
        return SafetyClass.STRUCTURAL  # conservative default for unlisted det.
    return None

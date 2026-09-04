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
    # D2 — policy choices
    "deterministic_policy": SafetyClass.POLICY,
    "deterministic_near_one_sided": SafetyClass.POLICY,
    # D3 — reproducible search/repair
    "combination_search": SafetyClass.HEURISTIC,
    "deterministic_symbol_injection": SafetyClass.HEURISTIC,
    "deterministic_brace_repair": SafetyClass.HEURISTIC,
    "deterministic_preprocessor_repair": SafetyClass.HEURISTIC,
    "deterministic_storage_class_relocation": SafetyClass.HEURISTIC,
    "compiler_fixit": SafetyClass.HEURISTIC,
    "deterministic_fixit": SafetyClass.HEURISTIC,
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
    for prefix, cls in _PROVENANCE_SAFETY.items():
        if p == prefix or p.startswith(prefix + ":"):
            return cls
    if p.startswith("deterministic"):
        return SafetyClass.STRUCTURAL  # conservative default for unlisted det.
    return None

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

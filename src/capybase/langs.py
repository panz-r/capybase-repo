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

"""Sesame-style structural separator projection (survey §1.2).

On brace/semicolon languages (Rust, C, C++, Java, JavaScript, Go, etc.) git's
line-diff anchors poorly when the *only* difference between two versions is a
trailing ``{``, ``}``, ``(``, ``)``, or ``;`` — producing spurious conflicts or
over-wide conflict regions. Sesame (arXiv:2407.18888) shows that projecting
these separators onto their own lines *before* an unstructured merge lets the
line-diff anchor on real statement/structure boundaries, yielding ~41% fewer
conflicts and ~88% fewer false positives vs raw diff3 — with **no AST**.

This is "projected syntax": structural hints encoded into the text so a simple
line merge operates over them. Capybase layers it onto the diff3 refinement
path (advisory only — it rewrites the recorded side texts for a sharper prompt;
splicing still uses worktree coordinates).

Python is exempt: it is indentation/colon-based, so the brace/semicolon set is
meaningless. The projection is a no-op for unsupported languages.
"""

from __future__ import annotations

# Language families the projection applies to. The survey's separator set is
# for brace/semicolon languages; Python (indentation/colon) is deliberately
# excluded — projecting ``:`` would shred Python structure. ``rust``/``c``/
# ``cpp``/``java``/``javascript``/``typescript``/``go``/``csharp``/``php`` all
# use braces for blocks and semicolons to terminate statements.
_SEPARATOR_LANGUAGES = frozenset(
    {
        "rust",
        "c",
        "cpp",
        "c++",
        "java",
        "javascript",
        "js",
        "typescript",
        "ts",
        "go",
        "golang",
        "csharp",
        "c#",
        "php",
        "kotlin",
        "swift",
        "scala",
    }
)

# The separators Sesame projects: the block/paren delimiters and the statement
# terminator. Each is placed on its own line so a line-merger treats them as
# independent structural units rather than incidental trailing characters.
_SEPARATORS = frozenset({"{", "}", "(", ")", ";"})


def supports(language: str | None) -> bool:
    """True if ``language`` is a brace/semicolon language the projection helps."""
    return bool(language) and language.lower() in _SEPARATOR_LANGUAGES


def project_separators(text: str, language: str | None) -> str:
    """Split each separator char onto its own line (survey §1.2).

    Transforms ``fn f() { x; }`` into ``fn f ( ) { x ; }`` (each separator on its
    own line). This makes a line-merger anchor on statement/block boundaries: a
    change to the *body* no longer entangles the enclosing braces, and a single
    trailing ``;`` difference becomes a clean one-line region instead of a
    whole-statement conflict. Whitespace within lines is preserved verbatim
    (only the separator chars are split out); the round-trip is approximate by
    design because capybase uses the projection for *alignment*, not output.

    A no-op (returns ``text`` unchanged) for unsupported languages or when the
    text contains no separators.
    """
    if not supports(language) or not text:
        return text
    if not any(c in text for c in _SEPARATORS):
        return text
    out_lines: list[str] = []
    for line in text.split("\n"):
        out_lines.extend(_split_line(line))
    return "\n".join(out_lines)


def _split_line(line: str) -> list[str]:
    """Split a single line on separator chars, keeping each separator on its line.

    ``"fn f() { x; }"`` → ``["fn f", "(", ")", "{", " x", ";", " }"]``. Non-
    separator fragments keep their original text (including leading/trailing
    spaces); empty fragments between adjacent separators become bare separator
    lines (e.g. ``"()"`` → ``["(", ")"]``), which is exactly what the merger
    needs to treat each delimiter as independent.
    """
    parts: list[str] = []
    buf = ""
    for ch in line:
        if ch in _SEPARATORS:
            parts.append(buf)
            parts.append(ch)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    # Drop the empty strings that arise from separators at line start/end, but
    # KEEP the ones between adjacent separators (they carry the structural split).
    # An empty fragment adjacent to a separator is meaningful (it marks that the
    # separator stood alone), so we only drop a trailing empty after the last sep.
    return [p for p in parts if p != ""]

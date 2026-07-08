"""Grammar-free, language-agnostic structural parser (survey groundwork §3).

Replaces tree-sitter grammars with two single-pass state machines:

- **Family A** (brace-delimited, C-syntax family): Rust, JavaScript, TypeScript,
  Go, Java, C/C++, C#, Swift, Kotlin, ... A character-scan state machine that
  tracks brace depth + string/comment state and classifies a declaration-level
  ``{`` by the keyword prefix before it.
- **Family B** (indentation-delimited, off-side rule): Python, Ruby (top-level),
  ... A line-by-line scan tracking indent level.

The parser answers the FIVE questions that drive merge correctness (survey
§"What Structural Information Actually Drives Merge Correctness"):

1. Scope boundaries (each unit's start/end line).
2. Unit identity (stable name; body fingerprint for rename detection).
3. Unit kind (coarse: FUNCTION / METHOD / CLASS / FIELD / MODULE_STMT /
   UNKNOWN_BLOCK).
4. Export surface (the unit's own name when public).
5. Import surface (the file's import/use statements).

What it deliberately does NOT do: expression-level structure (the LLM handles
that), type information (LSP provides it on demand), lossless round-trip (the
output guides generation/validation, never reconstructs code).

Robustness over correctness: the parser must not fail on malformed or
partially-merged code. Every ambiguous scope boundary emits ``UNKNOWN_BLOCK``
rather than raising, and ``parse_confidence`` signals downstream consumers when
to fall back to LSP. Conflict-marker lines (``<<<<<<<`` / ``=======`` /
``>>>>>>>``) are scope-boundary signals: close any open unit and emit an
``UNKNOWN_BLOCK`` for the region. Pure Python, no external dependencies.
"""

from __future__ import annotations

import hashlib
import re
from bisect import bisect_right
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Coarse unit-kind vocabulary
# ---------------------------------------------------------------------------
#
# These are LANGUAGE-NEUTRAL — the whole point of the abstract parser is that
# matching/identity never depends on grammar-specific node-type strings. Every
# detected declaration maps to exactly one of these. TEST is a name-prefix
# sub-classification (``is_test`` on the unit), not a separate kind.

KIND_FUNCTION = "function"
KIND_METHOD = "method"
KIND_CLASS = "class"
KIND_FIELD = "field"
KIND_MODULE_STMT = "module_stmt"
KIND_UNKNOWN = "unknown_block"

#: Families. ``"A"`` = brace-delimited, ``"B"`` = indentation-delimited,
#: ``"C"`` = declarative/data (not yet implemented).
FAMILY_A = "A"
FAMILY_B = "B"
FAMILY_C = "C"

#: Language string → family. Only ``python`` and ``rust`` are exposed as
#: ``is_available`` in Round 1 (to keep the existing skip-path tests green),
#: but the families for the other Family-A languages are declared here so the
#: dispatch is correct and language expansion (Round 3) is a one-line flip.
_LANG_FAMILY: dict[str, str] = {
    # Family B (indentation-delimited)
    "python": FAMILY_B,
    # Family A (brace-delimited)
    "rust": FAMILY_A,
    "javascript": FAMILY_A,
    "typescript": FAMILY_A,
    "js": FAMILY_A,
    "ts": FAMILY_A,
    "jsx": FAMILY_A,
    "tsx": FAMILY_A,
    "go": FAMILY_A,
    "java": FAMILY_A,
    "c": FAMILY_A,
    "cpp": FAMILY_A,
    "c++": FAMILY_A,
    "csharp": FAMILY_A,
    "cs": FAMILY_A,
    "kotlin": FAMILY_A,
    "swift": FAMILY_A,
    "scala": FAMILY_A,
    "dart": FAMILY_A,
    "php": FAMILY_A,
}

#: File extension → language (fallback when ``language`` is None).
_EXT_LANG: dict[str, str] = {
    ".py": "python",
    ".rs": "rust",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".cs": "csharp",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".swift": "swift",
    ".scala": "scala",
    ".dart": "dart",
    ".php": "php",
}

#: Languages capybase advertises structural support for. Family A (brace-
#: delimited: Rust, JS, TS, Go, Java, C/C++, C#, Kotlin, Swift, Scala, Dart,
#: PHP) and Family B (indentation-delimited: Python) are all supported by the
#: grammar-free state machines. Family C (declarative/data) is not yet
#: implemented. ``detect_family`` knows the broader map; ``is_available``
#: gates the public API.
_SUPPORTED_LANGUAGES = frozenset(_LANG_FAMILY.keys())


def detect_family(language: str | None, path: str | None = None) -> str | None:
    """The structural family (``"A"``/``"B"``) for a language/path, or ``None``.

    Language name wins over path extension. Returns ``None`` when neither yields
    a known family — callers treat that as "no structural signal, degrade."
    """
    lang = (language or "").strip().lower()
    if lang and lang in _LANG_FAMILY:
        return _LANG_FAMILY[lang]
    if path:
        dot = path.lower().rfind(".")
        if dot >= 0:
            ext = path.lower()[dot:]
            lang2 = _EXT_LANG.get(ext)
            if lang2 and lang2 in _LANG_FAMILY:
                return _LANG_FAMILY[lang2]
    return None


def language_for_family_member(language: str | None, path: str | None = None) -> str | None:
    """Resolve a concrete language name from ``language``/``path``.

    Used by callers that need the canonical language string (e.g. to pass to
    ``is_available``). Returns ``None`` when unrecognized.
    """
    lang = (language or "").strip().lower()
    if lang and lang in _LANG_FAMILY:
        return lang
    if path:
        dot = path.lower().rfind(".")
        if dot >= 0:
            ext = path.lower()[dot:]
            return _EXT_LANG.get(ext)
    return None


# ---------------------------------------------------------------------------
# Data model (survey groundwork §"Abstract Grammar Schema Design")
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StructuralUnit:
    """One detected top-level or container-child declaration.

    The unit of entity-level merge and structural diff. ``span`` is a 0-based
    inclusive ``(start_row, end_row)`` line range, matching capybase's
    marker_span convention (NOT the doc's 1-indexed — kept 0-based to match
    the existing span arithmetic in the consumers). ``body`` is the exact
    source text of the unit. ``children`` are nested units (methods inside a
    class). ``fingerprint`` is a rename-detection digest (stable under
    whitespace/formatting changes; the header is stripped so a rename leaves
    it unchanged).
    """

    kind: str
    name: str | None
    span: tuple[int, int]
    body: str
    children: list["StructuralUnit"] = field(default_factory=list)
    fingerprint: str = ""
    is_test: bool = False
    #: True for container-only scopes (impl/mod/namespace). Such a unit is a
    #: distinct SCOPE for duplicate-detection and sibling-neighborhood queries
    #: (so ``fn make`` in ``impl A`` doesn't collide with ``fn make`` in
    #: ``impl B``), but it is NOT itself enumerated as an entity — mirroring
    #: tree-sitter where ``impl_item`` has no entity kind. Consumers that flatten
    #: the tree to entities SKIP container-only units but preserve their
    #: children's grouping.
    is_container_scope: bool = False

    @property
    def identity(self) -> tuple[str, str]:
        """Stable key ``(kind, name)``; anonymous blocks use ``"<anon>"``."""
        return (self.kind, self.name or "<anon>")


@dataclass(frozen=True)
class FileIR:
    """The abstract structural representation of one source file.

    ``units`` are the top-level declarations (methods/fields nested under their
    class as ``children``). ``parse_confidence`` in ``[0.0, 1.0]`` signals when
    a consumer should fall back to LSP (low confidence ⇒ degraded parse). The
    parser never raises — a file it can't make sense of still produces a FileIR,
    just with low confidence / UNKNOWN_BLOCK units.

    ``imports`` and ``exports`` are the file's dependency surfaces (survey §"What
    Structural Information Actually Drives Merge Correctness"): imports are
    external names brought in (``import``/``use``/``require``/``#include``);
    exports are top-level public names defined here. The survey notes "a simple
    imports-only tool outperformed complex structured tools" — this surface
    enables cross-commit dependency checks without re-scanning the file.
    """

    family: str
    units: list[StructuralUnit]
    parse_confidence: float = 1.0
    source: str = ""
    language: str | None = None
    imports: list[str] = field(default_factory=list)
    exports: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Body fingerprint — the rename-detection digest
# ---------------------------------------------------------------------------

#: Matches Python/Rust/JS string literals (best-effort normalization so two
#: bodies differing only in a string value still pair on rename). Conservative:
#: a false "same body" is a missed rename (low cost); a false "different body"
#: just falls through to the Jaccard fallback.
_STRING_LIT_RE = re.compile(
    r'"""[\s\S]*?"""|'  # py triple double
    r"'''[\s\S]*?'''|"  # py triple single
    r'"(?:\\.|[^"\\])*"|'  # double-quoted
    r"'(?:\\.|[^'\\])*'"  # single-quoted
)


def unit_body_fingerprint(body: str) -> str:
    """A normalized, rename-insensitive digest of a unit's body content.

    Stable under whitespace, comment, and formatting changes, and RENAME-
    SENSITIVE ONLY IN THE HEADER (the first line is stripped), so two functions
    differing only in name produce the SAME fingerprint — the basis for pairing
    a renamed entity to its base original. This mirrors the existing
    ``structural._split_header_body`` contract (header-stripped, whitespace-
    collapsed body) so the existing rename-detection thresholds (Jaccard 0.80)
    stay calibrated.

    The digest folds in the line count + a SHA1 of the normalized body content
    so it is short to store/compare yet discriminating.
    """
    body = body or ""
    if not body:
        return ""
    lines = body.split("\n")
    # Strip the header (first line: the def/fn/class signature) so a rename
    # (which only changes the header's name token) leaves the digest unchanged.
    rest = "\n".join(lines[1:])
    norm = _normalize_body(rest)
    # Count MEANINGFUL lines (non-blank, non-comment) so adding a comment line
    # doesn't perturb the digest — the fingerprint is stable under comment
    # additions (the AstPreservationValidator relies on this).
    meaningful = sum(1 for ln in lines[1:] if _has_code_content(ln))
    if not norm:
        return f"l{meaningful}"
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]
    return f"l{meaningful}:{digest}"


#: Matches a comment-only line (Python ``#``, Rust/JS/Go/C ``//``, block ``/* */``).
_COMMENT_LINE_RE = re.compile(
    r"^\s*(?:#|//|/\*|\*/).*$"
)


def _has_code_content(line: str) -> bool:
    """True if a line carries actual code (not blank or a pure comment)."""
    if not line.strip():
        return False
    if _COMMENT_LINE_RE.match(line):
        return False
    return True


def _strip_inline_comment(line: str) -> str:
    """Strip an inline ``// ...`` or ``# ...`` comment (string-aware, best-effort).

    A ``//`` or ``#`` inside a string literal must NOT be treated as a comment
    start. We blank string literals first, then look for the comment marker.
    """
    blanked = _STRING_LIT_RE.sub("'_'", line)
    for marker in ("//", "#"):
        idx = blanked.find(marker)
        if idx >= 0:
            line = line[:idx]
            blanked = blanked[:idx]
    return line


def _normalize_body(text: str) -> str:
    """Whitespace-collapse + string/comment-literal-neutralize a body region.

    ``" ".join(split())`` collapses all whitespace runs to single spaces (stable
    across indentation/reformatting). String literals are blanked and comments
    stripped first so two bodies differing only in string values or comments
    normalize equal — rename detection and AST preservation shouldn't be thrown
    off by ``return 'hello'`` vs ``return 'hi'`` or an added ``# note``.
    """
    if not text:
        return ""
    # Drop pure-comment lines and strip inline comments, then blank string lits.
    kept = []
    for ln in text.split("\n"):
        if not _has_code_content(ln):
            continue
        kept.append(_strip_inline_comment(ln))
    joined = "\n".join(kept)
    blanked = _STRING_LIT_RE.sub("'_'", joined)
    return " ".join(blanked.split())


def _normalize_header(text: str) -> str:
    """Whitespace-collapse a header line (string-literals NOT blanked)."""
    return " ".join((text or "").split())


# ---------------------------------------------------------------------------
# Family B — indentation-delimited (Python et al.)
# ---------------------------------------------------------------------------

# Indentation-delimited declaration patterns. Matched at line start (after the
# indent). ``async def`` / ``def`` → FUNCTION; ``class`` → CLASS. Methods are
# detected structurally: any FUNCTION nested (indent > 0) inside an open CLASS.
_B_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_B_CLASS_RE = re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\s*[\(:]")
_B_IMPORT_RE = re.compile(
    r"^\s*(?:import\s+\S|from\s+[A-Za-z_][\w.]*\s+import\s)"
)
# Decorator line (Python). Attribution to the following decl is done in the scan
# by remembering the leading decorator indent.
_B_DECORATOR_RE = re.compile(r"^\s*@")
# Conflict markers.
_B_CONFLICT_MARKERS = ("<", "=", ">")


def _is_conflict_marker_line(line: str) -> bool:
    """True for ``<<<<<<<``/``=======``/``>>>>>>>`` conflict marker lines.

    These are scope-boundary signals (survey §"Conflict marker awareness"):
    close any open unit and treat the region as UNKNOWN_BLOCK. Never crash.
    """
    s = line.lstrip()
    return (
        s.startswith("<<<<<<<")
        or s.startswith("=======")
        or s.startswith(">>>>>>>")
    )


def _indent_width(line: str) -> int:
    """Leading-whitespace column count (tabs = 8, per Python convention)."""
    n = 0
    for ch in line:
        if ch == " ":
            n += 1
        elif ch == "\t":
            n += 8 - (n % 8)
        else:
            break
    return n


def _is_blank_or_comment(line: str, lang: str | None) -> bool:
    """Blank or a comment line (doesn't affect indent-based scope boundaries)."""
    if not line.strip():
        return True
    if lang and lang.startswith(("python", "ruby")):
        return line.lstrip().startswith("#")
    return False


@dataclass
class _OpenUnit:
    """In-progress unit (Family B) tracked on the scan stack.

    Only the declaration START is recorded during the scan; the END span + body
    are computed at finalization from the source, so we never miscount nested
    lines (a class's span covers its methods because the close logic assigns each
    closed child's span, then the parent's end = the last child's end).
    """

    kind: str
    name: str | None
    start_row: int
    indent: int
    is_test: bool
    children: list[StructuralUnit] = field(default_factory=list)


def _slice_body(source: str, lines: list[str], start: int, end: int) -> str:
    """Exact source slice for rows ``[start, end]`` (0-based, inclusive)."""
    return "\n".join(lines[start : end + 1])


def _build_line_index(source: str) -> list[int]:
    """Byte offsets where each line starts (0-based row → byte offset).

    ``index[0]`` is always 0 (line 0 starts at byte 0). For a source with N
    newlines, this produces N+1 entries. Used by :func:`_row_at` for O(log n)
    byte-offset → row conversion — avoids the O(n) ``str.count('\\n', 0, idx)``
    scan that was called once per declaration push and once per unit close.
    """
    index = [0]
    pos = source.find("\n")
    while pos >= 0:
        index.append(pos + 1)
        pos = source.find("\n", pos + 1)
    return index


def _row_at(line_index: list[int], byte_idx: int) -> int:
    """Convert a byte offset to a 0-based row number via binary search.

    O(log n) instead of the O(n) ``source.count('\\n', 0, byte_idx)``. The
    ``line_index`` is precomputed once per parse by :func:`_build_line_index``.
    """
    # bisect_right finds the insertion point — the number of line starts at or
    # before byte_idx, minus 1 = the row number (0-based).
    return bisect_right(line_index, byte_idx) - 1


# ---------------------------------------------------------------------------
# Import / export surface extraction (survey §"What Structural Information
# Actually Drives Merge Correctness" — requirement 4 + 5)
# ---------------------------------------------------------------------------

# Family B (Python) export: top-level names not starting with `_`.
_B_EXPORT_RE = re.compile(r"^(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")
_B_PUBLIC_NAME = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)\s*=")  # module-level const


def _extract_imports_exports_b(source: str, units: list[StructuralUnit]) -> tuple[list[str], list[str]]:
    """Collect the import and export surfaces for a Family-B file.

    Imports: the names from ``import``/``from...import`` lines (already detected
    as MODULE_STMT units — extract their names). Exports: top-level public
    function/class/constant names (not ``_``-prefixed)."""
    imports: list[str] = []
    for u in units:
        if u.kind == KIND_MODULE_STMT and u.name and u.name != "<import>":
            imports.append(u.name)
    exports: list[str] = []
    for u in units:
        if u.kind in (KIND_FUNCTION, KIND_CLASS) and u.name and not u.name.startswith("_"):
            exports.append(u.name)
    return imports, exports


def _extract_imports_exports_a(source: str, units: list[StructuralUnit]) -> tuple[list[str], list[str]]:
    """Collect the import and export surfaces for a Family-A file.

    Imports: names from ``use``/``import``/``require``/``#include`` MODULE_STMT
    units. Exports: top-level public names — ``pub``/``export``/``public``
    functions/classes/fields (Rust/JS/Java), or any non-private top-level name.
    """
    imports: list[str] = []
    for u in units:
        if u.kind == KIND_MODULE_STMT and u.name and u.name != "<import>":
            imports.append(u.name)
    exports: list[str] = []
    for u in units:
        if u.kind in (KIND_FUNCTION, KIND_CLASS, KIND_FIELD) and u.name:
            # Family-A publicness: check if the body starts with a pub/export
            # modifier. Rust: ``pub fn``/``pub struct``/``pub const``. JS/TS:
            # ``export``. Java: ``public``. A name starting with uppercase is
            # conventionally public in most Family-A languages.
            first_line = u.body.split("\n", 1)[0] if u.body else ""
            is_public = any(
                kw in first_line.split()
                for kw in ("pub", "export", "public", "EPORTED")
            )
            if is_public or u.kind == KIND_CLASS:
                exports.append(u.name)
    return imports, exports


def parse_family_b(source: str, language: str | None = "python") -> FileIR:
    """Parse an indentation-delimited (Family B) source into a :class:`FileIR`.

    Line-by-line scan tracking the open-unit stack and indent level. A unit's
    scope ends when a non-blank, non-comment line appears at indent ``<=`` the
    unit's opening indent and isn't a line-continuation. Methods are FUNCTION
    units nested inside a CLASS (indent > 0 with an open CLASS parent). Imports
    are MODULE_STMT. Conflict-marker lines close all open units. Decorator lines
    are attached to the following declaration (its ``start_row`` moves up to the
    decorator). Never raises.

    Span/body are computed AFTER the scan by source position: a parent unit's
    end_row is the last row before its container closes (i.e. the end of its
    last child, or its own start if it has none), and its body is the source
    slice over its span. This keeps nested-method spans faithful without fragile
    line-buffer accumulation.
    """
    lines = source.split("\n")
    n = len(lines)
    units: list[StructuralUnit] = []
    stack: list[_OpenUnit] = []
    # Tracks, for each open unit, the END row it should currently claim (updated
    # as we walk: every non-blank line extends the deepest open unit's reach).
    last_line_row = -1
    pending_decorator_indent: int | None = None
    pending_decorator_start: int | None = None

    def close_units_at_or_below(indent: int, end_row: int) -> None:
        # Close any open unit whose opening indent is >= indent (scope ended).
        # ``end_row`` is the last row that belonged to the closing units (the row
        # BEFORE the line that triggered the dedent — the dedenting line opens a
        # NEW scope, it isn't part of the closing one).
        while stack and stack[-1].indent >= indent:
            u = stack.pop()
            u_end = end_row if end_row >= u.start_row else u.start_row
            _finalize_unit(u, u_end, lines, units, stack)

    for i, raw in enumerate(lines):
        if _is_conflict_marker_line(raw):
            # Close against the last meaningful row (before this marker).
            close_units_at_or_below(0, last_line_row)
            stack.clear()
            pending_decorator_indent = None
            pending_decorator_start = None
            last_line_row = i - 1
            continue
        if _is_blank_or_comment(raw, language):
            continue

        indent = _indent_width(raw)
        # Snapshot the last meaningful row BEFORE this line — it's the end row
        # for any unit this line's dedent closes (this line opens a new scope).
        prev_line_row = last_line_row
        last_line_row = i

        if _B_DECORATOR_RE.match(raw):
            if pending_decorator_indent is None:
                pending_decorator_indent = indent
                pending_decorator_start = i
            continue

        # A dedent below a pending decorator ⇒ the decorator was standalone.
        if pending_decorator_indent is not None and indent < pending_decorator_indent:
            pending_decorator_indent = None
            pending_decorator_start = None

        close_units_at_or_below(indent, prev_line_row)

        # Imports.
        if _B_IMPORT_RE.match(raw):
            name = _extract_import_name(raw)
            start = pending_decorator_start if pending_decorator_start is not None else i
            body = _slice_body(source, lines, start, i)
            units.append(
                StructuralUnit(
                    kind=KIND_MODULE_STMT,
                    name=name,
                    span=(start, i),
                    body=body,
                    fingerprint=unit_body_fingerprint(body),
                )
            )
            pending_decorator_indent = None
            pending_decorator_start = None
            continue

        # Function / method.
        m = _B_DEF_RE.match(raw)
        if m:
            name = m.group(1)
            parent_class = next(
                (u for u in reversed(stack) if u.kind == KIND_CLASS), None
            )
            kind = KIND_METHOD if parent_class is not None else KIND_FUNCTION
            start = pending_decorator_start if pending_decorator_start is not None else i
            stack.append(
                _OpenUnit(
                    kind=kind,
                    name=name,
                    start_row=start,
                    indent=indent,
                    is_test=bool(name and name.startswith("test_")),
                )
            )
            pending_decorator_indent = None
            pending_decorator_start = None
            continue

        # Class.
        m = _B_CLASS_RE.match(raw)
        if m:
            name = m.group(1)
            start = pending_decorator_start if pending_decorator_start is not None else i
            stack.append(
                _OpenUnit(
                    kind=KIND_CLASS,
                    name=name,
                    start_row=start,
                    indent=indent,
                    is_test=bool(name and name.startswith("Test")),
                )
            )
            pending_decorator_indent = None
            pending_decorator_start = None
            continue

    # EOF: close everything still open. end_row = last meaningful line (trailing
    # blanks/comments don't extend a unit's body).
    close_units_at_or_below(0, last_line_row)

    confidence = _assess_confidence(source, units)
    imports, exports = _extract_imports_exports_b(source, units)
    return FileIR(
        family=FAMILY_B,
        units=units,
        parse_confidence=confidence,
        source=source,
        language=language,
        imports=imports,
        exports=exports,
    )


def _dedent_body(body: str) -> str:
    """Strip leading indentation so the body begins at the declaration token.

    Mirrors tree-sitter's ``node.text`` convention: a method inside a class has
    its ``start_byte`` at the ``def``/``fn`` keyword, so the body's first line
    carries no leading indentation even though the source line is indented.
    Subsequent lines keep their original (deeper) indentation. This keeps the
    body faithful to what the structural resolver splices — it re-indents on
    reassembly, so a body that already leads with the declaration keyword (not
    whitespace) round-trips correctly.
    """
    if not body:
        return body
    lines = body.split("\n")
    # Strip leading whitespace from the first line only.
    lines[0] = lines[0].lstrip()
    return "\n".join(lines)


def _finalize_unit(
    u: _OpenUnit,
    end_row: int,
    lines: list[str],
    units: list[StructuralUnit],
    stack: list[_OpenUnit],
) -> None:
    """Pop a finished open unit: compute span/body from source, attach to parent."""
    body = _slice_body("\n".join(lines), lines, u.start_row, end_row)
    body = _dedent_body(body)
    su = StructuralUnit(
        kind=u.kind,
        name=u.name,
        span=(u.start_row, max(end_row, u.start_row)),
        body=body,
        children=list(u.children),
        fingerprint=unit_body_fingerprint(body),
        is_test=u.is_test,
    )
    if stack:
        stack[-1].children.append(su)
    else:
        units.append(su)


def _extract_import_name(line: str) -> str:
    """A best-effort label for an import statement (for identity/diff)."""
    s = line.strip()
    # ``from X import a, b`` → ``X``; ``import X as y`` → ``X``; ``import X`` → ``X``.
    m = re.match(r"from\s+([A-Za-z_][\w.]*)", s)
    if m:
        return m.group(1)
    m = re.match(r"import\s+([A-Za-z_][\w.]*)", s)
    if m:
        return m.group(1)
    return "<import>"


# ---------------------------------------------------------------------------
# Family A — brace-delimited (Rust / JS / TS / Go / Java / C / ...)
# ---------------------------------------------------------------------------

# Keyword sets for Family-A declaration classification. A declaration-level ``{``
# is one preceded (in the current token buffer, ignoring whitespace) by one of
# these patterns. ``class``-like keywords → CLASS; function-like → FUNCTION
# (METHOD when nested inside a CLASS or container).
_A_CLASS_KEYWORDS = (
    "class", "struct", "interface", "trait", "enum", "union",
)
# Container-only keywords: ``impl``/``mod`` open a scope whose children are the
# real entities (methods/fields), but the container itself is NOT emitted as an
# entity — mirroring tree-sitter, where ``impl_item`` has no ``_KIND_BY_NODE_TYPE``
# entry (only its ``implementation_list`` body is enumerated). This matters for
# identity stability: an ``impl Config`` block must not collide with the
# ``struct Config`` definition under the same (class, "Config") identity.
_A_CONTAINER_KEYWORDS = ("impl", "mod", "namespace", "module")
# ``def`` appears here for Python-in-JS-template edge cases and is harmless;
# the canonical Family-A function keywords lead.
_A_FUNC_KEYWORDS = (
    "fn", "func", "function", "def", "async", "pub", "export",
)
# Field-like (top-level bindings): ``const``/``static``/``type``/``var``/``let``
# declarations without a following ``{`` (so they don't open a scope) — detected
# separately from the brace machine.
_A_FIELD_KEYWORDS = ("const", "static", "type", "var", "let", "final")
# Import-like top-level statements.
_A_IMPORT_PATTERNS = (
    re.compile(r"^\s*(?:use\s+\S|import\s+\S|export\s+\{[^}]*\}\s+from|require\s*\(|#include\s+)"),
)


@dataclass
class _OpenAUnit:
    """In-progress Family-A unit (an open declaration scope at some brace depth).

    ``container_only`` marks container keywords (impl/mod/namespace) whose scope
    holds real entities but which are themselves NOT emitted as entities (their
    children are attached to the enclosing non-container parent, or top-level).
    """

    kind: str
    name: str | None
    start_row: int  # row of the declaration keyword/signature
    start_byte: int  # byte offset of the declaration start (for body slicing)
    open_brace_depth: int  # the brace_depth value just AFTER the opening {
    body_start_byte: int  # byte offset of the opening {
    children: list[StructuralUnit] = field(default_factory=list)
    is_test: bool = False
    container_only: bool = False
    # Attribute/decorator lines consumed immediately before this decl.
    attr_start_row: int | None = None


def parse_family_a(source: str, language: str | None = "rust") -> FileIR:
    r"""Parse a brace-delimited (Family A) source into a :class:`FileIR`.

    Single-pass character scan tracking:
    - ``brace_depth`` (current ``{`` nesting),
    - string state (``'`` / ``"`` / backtick / char / none),
    - comment state (line ``//`` / block ``/* */`` / ``#`` for rust attributes
      & C preprocessor at line start / none),
    - a per-line token buffer used to classify a declaration-level ``{``.

    A ``{`` is declaration-level when (a) the preceding non-whitespace token run
    ends in a declaration keyword, and (b) it's not inside a string/comment. A
    bare ``{`` (object literal, closure body) without a keyword prefix is
    expression-level and only increments depth (its contents are abstracted
    away). Methods are FUNCTION units nested inside a CLASS. ``use``/``import``
    /``#include`` at depth 0 are MODULE_STMT. Conflict markers close all open
    scopes. ``#define``/macros → MODULE_STMT (no scope detection inside).
    Never raises.
    """
    src = source
    units: list[StructuralUnit] = []
    stack: list[_OpenAUnit] = []

    # Token buffer: accumulated identifier/operator run since the last ``;`` or
    # ``}`` or statement boundary — used to classify the next ``{``.
    buf = ""

    brace_depth = 0
    paren_depth = 0  # track () so we don't misread a ``{`` inside a call
    # String/comment state machine.
    in_str: str | None = None  # one of "'", '"', "`", "char", or None
    in_line_comment = False
    in_block_comment = False
    # Pending attribute/macro line (#[...] / @decorator) preceding a decl.
    pending_attr_row: int | None = None
    pending_attr_buf = ""

    i = 0
    n = len(src)
    row = 0  # current line (0-based)
    line_start = 0  # byte offset of current line start

    # Precompute the line-offset index for O(log n) byte→row conversion
    # (Improvement #4: eliminates the O(n) str.count('\n',...) scan that ran
    # once per declaration push and once per unit close).
    _line_index = _build_line_index(src)

    def cur_row_at(byte_idx: int) -> int:
        return _row_at(_line_index, byte_idx)

    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""

        # --- newline: advance row, close line comment ---
        if ch == "\n":
            row += 1
            line_start = i + 1
            if in_line_comment:
                in_line_comment = False
                # A line comment ends a statement run (token buffer boundary).
                buf = ""
            else:
                # A newline acts as a token separator so declarations on
                # consecutive lines don't concatenate in the buffer (e.g.
                # ``package main\nfunc main()`` must not become ``packagemain
                # func main()``). Mirrors the ``isspace()`` accumulation below.
                if buf and not buf.endswith(" "):
                    buf += " "
            i += 1
            continue

        # --- comment / string handling takes precedence ---
        if in_line_comment:
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_str is not None:
            if ch == "\\":
                i += 2  # skip escaped char
                continue
            if in_str == "char" and ch == "'":
                in_str = None
                i += 1
                continue
            if in_str == "'" and ch == "'":
                in_str = None
                i += 1
                continue
            if in_str == '"' and ch == '"':
                in_str = None
                i += 1
                continue
            if in_str == "`" and ch == "`":
                in_str = None
                i += 1
                continue
            i += 1
            continue

        # Not in string/comment. Detect transitions.
        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch == '"':
            in_str = '"'
            i += 1
            continue
        if ch == "`":
            in_str = "`"
            i += 1
            continue
        if ch == "'":
            # Rust/JS char-literal vs lifetime/attribute — treat as string start
            # only when it plausibly opens a char (next char isn't part of an
            # identifier). Conservative: skip if preceded by alnum/_ (lifetime).
            prev = src[i - 1] if i > 0 else ""
            if prev.isalnum() or prev == "_":
                # Likely a Rust lifetime ('a) — don't enter string state.
                i += 1
                continue
            in_str = "char"
            i += 1
            continue

        # Conflict markers (only at line start, depth 0).
        if i == line_start or (i > line_start and src[line_start:i].strip() == ""):
            line_head = src[line_start : line_start + 7]
            if (
                line_head.startswith("<<<<<<<")
                or line_head.startswith("=======")
                or line_head.startswith(">>>>>>>")
            ):
                # Close all open units.
                while stack:
                    _close_a_unit(stack.pop(), brace_depth + 1, src, units, stack, _line_index)
                # Reset statement state.
                buf = ""
                # Skip the rest of this line.
                nl = src.find("\n", i)
                i = nl + 1 if nl >= 0 else n
                row += 1
                if nl >= 0:
                    line_start = nl + 1
                continue

        # --- track braces / parens ---
        if ch == "{":
            # Classify this brace.
            classified = _classify_a_brace(buf, stack, language)
            if classified is not None:
                # Declaration-level brace. Push a new unit.
                kind, name, container_only = classified
                decl_start = _find_decl_start(src, i, line_start)
                attr_row = pending_attr_row
                pending_attr_row = None
                unit = _OpenAUnit(
                    kind=kind,
                    name=name,
                    start_row=cur_row_at(decl_start),
                    start_byte=decl_start,
                    open_brace_depth=brace_depth + 1,
                    body_start_byte=i,
                    attr_start_row=attr_row,
                    is_test=bool(name and (name.startswith("test") or name.startswith("Test"))),
                    container_only=container_only,
                )
                stack.append(unit)
            # Either way, depth increases.
            brace_depth += 1
            buf = ""
            i += 1
            continue

        if ch == "}":
            brace_depth -= 1
            if brace_depth < 0:
                brace_depth = 0  # unbalanced (malformed) — clamp, never crash.
            # Close any unit whose scope this brace ends. Pass the closing brace
            # index so the body slice can be computed precisely.
            while stack and brace_depth < stack[-1].open_brace_depth:
                _close_a_unit(stack.pop(), i, src, units, stack, _line_index)
            buf = ""
            i += 1
            continue

        if ch == "(":
            paren_depth += 1
            buf += ch
            i += 1
            continue
        if ch == ")":
            paren_depth = max(0, paren_depth - 1)
            buf += ch
            i += 1
            continue

        # Statement terminators reset the token buffer (a new statement begins).
        if ch == ";":
            buf = ""
            i += 1
            continue

        # Preprocessor / attribute lines (Rust ``#``, C ``#``, JS/TS decorators
        # via leading ``@`` are handled by the keyword buffer). At line start +
        # depth 0, ``#`` begins a preprocessor/attribute line.
        if ch == "#" and i == line_start and brace_depth == 0:
            line_end = src.find("\n", i)
            if line_end < 0:
                line_end = n
            attr_line = src[i:line_end]
            if attr_line.strip().startswith("#[") or attr_line.strip().startswith("#!"):
                # Rust attribute → attach to the following decl.
                pending_attr_row = row
            elif attr_line.strip().startswith("#include") or attr_line.strip().startswith("#define"):
                # MODULE_STMT for #include / #define.
                start_row = row
                units.append(
                    StructuralUnit(
                        kind=KIND_MODULE_STMT,
                        name=_extract_a_import_name(attr_line.strip()),
                        span=(start_row, start_row),
                        body=attr_line,
                        fingerprint=unit_body_fingerprint(attr_line),
                    )
                )
            buf = ""
            i = line_end + 1
            row += 1
            if i <= n and src[line_end] == "\n":
                line_start = line_end + 1
            continue

        # Import / use statements (depth 0): detect at line start.
        if brace_depth == 0 and paren_depth == 0 and i == line_start:
            line_end = src.find("\n", i)
            if line_end < 0:
                line_end = n
            line_text = src[i:line_end]
            if _A_IMPORT_PATTERNS[0].match(line_text) or any(
                p.match(line_text) for p in _A_IMPORT_PATTERNS
            ):
                start_row = row
                units.append(
                    StructuralUnit(
                        kind=KIND_MODULE_STMT,
                        name=_extract_a_import_name(line_text.strip()),
                        span=(start_row, start_row),
                        body=line_text,
                        fingerprint=unit_body_fingerprint(line_text),
                    )
                )
                buf = ""
                i = line_end + 1
                row += 1
                if src[line_end] == "\n":
                    line_start = line_end + 1
                continue

        # Field-like declarations at depth 0 without an opening brace: detect on
        # the ``;`` terminator. We accumulate into buf and check at ``;`` above —
        # but we only emit a FIELD unit if no brace opened. Handle here:
        if ch == "=" and brace_depth == 0 and paren_depth == 0:
            # Possible field decl: ``pub const NAME = ...`` / ``let x = ...``.
            # Peek back at buf for a field keyword.
            if _buf_has_field_keyword(buf):
                # Don't emit yet — wait for the terminating ``;`` or newline.
                pass

        # Accumulate into the token buffer (whitespace-normalized).
        if ch.isspace():
            if buf and not buf.endswith(" "):
                buf += " "
        else:
            buf += ch
        i += 1

    # EOF: close any still-open units (malformed/unclosed — never crash).
    while stack:
        _close_a_unit(stack.pop(), n, src, units, stack, _line_index)

    # Sweep: emit FIELD units for top-level ``const``/``static``/``type`` decls
    # that didn't open a brace (``pub const N: u32 = 5;``). Re-scan line-based.
    _emit_a_field_units(units, src, language)

    confidence = _assess_confidence(source, units)
    imports, exports = _extract_imports_exports_a(source, units)
    return FileIR(
        family=FAMILY_A,
        units=units,
        parse_confidence=confidence,
        source=source,
        language=language,
        imports=imports,
        exports=exports,
    )


def _close_a_unit(
    unit: _OpenAUnit,
    close_brace_idx: int,
    src: str,
    units: list[StructuralUnit],
    stack: list[_OpenAUnit],
    line_index: list[int] | None = None,
) -> None:
    """Finalize a Family-A unit: slice body, compute span/fingerprint, attach.

    ``close_brace_idx`` is the byte index of the ``}`` that closed this unit's
    scope (or ``len(src)`` at EOF for an unclosed/malformed unit). The body is
    sliced from the declaration start through that brace — precise, no
    re-walk needed. Container-only units (impl/mod/namespace) are NOT emitted
    as entities; their children pass through to the enclosing scope (mirroring
    tree-sitter, where ``impl_item`` has no entity kind — only its body list is
    enumerated). Child attachment is by brace-depth containment: a unit is a
    child of the deepest still-open frame opened at a shallower brace depth.
    """
    end_byte = close_brace_idx + 1 if close_brace_idx < len(src) else len(src)
    # If we closed on a ``}``, that's already included; else (EOF) take through.
    if close_brace_idx < len(src) and src[close_brace_idx] == "}":
        end_byte = close_brace_idx + 1
    start_byte = unit.start_byte
    start_row = unit.start_row
    if unit.attr_start_row is not None and unit.attr_start_row < start_row:
        # Include preceding attribute lines in the span/body.
        start_row = unit.attr_start_row
        ab = src.rfind("\n", 0, unit.start_byte)
        start_byte = ab + 1 if ab >= 0 else 0
    body = src[start_byte:end_byte]
    body = _dedent_body(body)
    if line_index is not None:
        end_row = _row_at(line_index, min(end_byte, len(src) - 1)) if src else 0
    else:
        end_row = src.count("\n", 0, end_byte)
    if end_row < start_row:
        end_row = start_row

    children = list(unit.children)

    # Container-only units are not emitted as entities: hand their children to
    # Container-only units (impl/mod/namespace) are emitted into the tree as a
    # DISTINCT SCOPE (is_container_scope=True) so their children stay grouped —
    # ``fn make`` in ``impl A`` must not collide with ``fn make`` in ``impl B``.
    # They are NOT themselves enumerated as entities (consumers skip
    # container-scope units when flattening to entities).
    su = StructuralUnit(
        kind=unit.kind,
        name=unit.name,
        span=(start_row, end_row),
        body=body,
        children=children,
        fingerprint=unit_body_fingerprint(body),
        is_test=unit.is_test,
        is_container_scope=unit.container_only,
    )
    # Attach to the deepest open frame opened at a SHALLOWER brace depth (the
    # lexical parent). A unit opened at depth d is a child of the frame whose
    # open_brace_depth is < d. ``stack`` is ordered open-first, so the top is
    # the most-recently-opened (deepest) frame.
    parent = None
    for frame in reversed(stack):
        if frame.open_brace_depth < unit.open_brace_depth:
            parent = frame
            break
    if parent is not None:
        parent.children.append(su)
    else:
        units.append(su)


def _classify_a_brace(
    buf: str, stack: list[_OpenAUnit], language: str | None
) -> tuple[str, str | None, bool] | None:
    """Classify a declaration-level ``{`` from the preceding token buffer.

    Returns ``(kind, name, container_only)`` when the buffer ends in a
    declaration pattern, else ``None`` (a bare/object-literal brace). ``name``
    is the identifier following the declaration keyword (the function/class
    name); ``None`` for an anonymous block. ``container_only`` is True for
    impl/mod/namespace (containers whose children are the real entities).
    """
    # The buffer is a whitespace-normalized run ending where the ``{`` is. Strip
    # a trailing ``{``-adjacent tokens we may have already added.
    b = buf.strip().rstrip("{").strip()
    if not b:
        return None
    toks = b.split()
    if not toks:
        return None
    # Find the LAST declaration keyword in the buffer.
    last_kw_idx = -1
    last_kw = ""
    for idx in range(len(toks) - 1, -1, -1):
        t = toks[idx]
        if (
            t in _A_CLASS_KEYWORDS
            or t in _A_FUNC_KEYWORDS
            or t in _A_CONTAINER_KEYWORDS
        ):
            last_kw_idx = idx
            last_kw = t
            break
    if last_kw_idx < 0:
        return None  # no declaration keyword → expression-level brace

    # Determine the name: the identifier right after the keyword.
    name: str | None = None
    after = toks[last_kw_idx + 1 :]
    # For ``impl X for Y`` / ``trait X : Y`` the name is X (first after impl/trait).
    if after:
        cand = after[0]
        # Strip trailing punctuation like ``(``, ``<``, ``:``.
        cand = re.split(r"[<(:\[{]", cand, maxsplit=1)[0]
        cand = cand.strip(" \t")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cand):
            name = cand

    # Container-only keywords (impl/mod/namespace): open a scope but emit nothing.
    if last_kw in _A_CONTAINER_KEYWORDS:
        # Mark as class-kind for span/body purposes; container_only suppresses
        # the entity emission and passes children through.
        return (KIND_CLASS, name, True)

    # Class vs function vs method.
    if last_kw in _A_CLASS_KEYWORDS:
        kind = KIND_CLASS
        return (kind, name, False)
    # FUNCTION unless nested inside an open CLASS or container → METHOD.
    in_container = any(
        (u.kind == KIND_CLASS or u.container_only) for u in reversed(stack)
    )
    kind = KIND_METHOD if in_container else KIND_FUNCTION
    return (kind, name, False)


def _find_decl_start(src: str, brace_idx: int, line_start: int) -> int:
    """Byte offset where the declaration on this line begins.

    Walks back to the start of the current statement. The token buffer is reset
    at every ``;``, ``}``, AND ``{`` (an opening brace starts a new inner scope),
    so the declaration begins at the latest of those before ``brace_idx``. This
    matters for the first method inside an ``impl``: its ``fn`` keyword's buffer
    starts right after the impl's opening ``{``, so its decl start is the ``fn``
    line — NOT the impl line.
    """
    cut = max(
        src.rfind(";", 0, brace_idx),
        src.rfind("}", 0, brace_idx),
        src.rfind("{", 0, brace_idx),
        0,
    )
    # Skip the separator itself.
    if cut > 0:
        cut += 1
    # Skip leading whitespace/newlines.
    while cut < brace_idx and src[cut] in " \t\r\n":
        cut += 1
    return cut


def _buf_has_field_keyword(buf: str) -> bool:
    return any(kw in buf.split() for kw in _A_FIELD_KEYWORDS)


def _emit_a_field_units(
    units: list[StructuralUnit], src: str, language: str | None
) -> None:
    """Emit top-level FIELD units for ``const``/``static``/``type`` declarations.

    These don't open a brace, so the brace machine doesn't see them. We re-scan
    line-by-line at depth 0 (heuristic: a line starting with an optional
    ``pub``/``export`` modifier + a field keyword + a name + a ``;`` or ``=``).
    Only runs for languages that have such constructs (rust/go/ts/js). Emitted
    FIELD units are appended after the brace-detected ones; callers that rely on
    source order tolerate this (identity is by name, not position).
    """
    if language in (None, "c", "h"):
        # C ``#define`` already handled; plain C top-level vars are rare and
        # not entity-level-merge-relevant. Skip to avoid noise.
        return
    field_re = re.compile(
        r"^\s*(?:(?:pub|export|public|private|static|final|readonly|unsafe|inline)\s+)*"
        r"(?:const|static|type|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\b"
    )
    existing_names = {u.name for u in units if u.kind == KIND_FIELD}
    lines = src.split("\n")
    depth = 0
    in_s: str | None = None
    in_lc = False
    in_bc = False
    for idx, line in enumerate(lines):
        # Cheap depth tracker over the whole file so we only emit at top level.
        # (Re-uses the same string/comment rules but line-coarse.)
        stripped = line.strip()
        if in_bc:
            if "*/" in stripped:
                in_bc = False
            continue
        if in_lc:
            in_lc = False
        if in_s is not None:
            # crude: a string open from a previous line — close on this line.
            if in_s in stripped:
                in_s = None
            continue
        if stripped.startswith("//"):
            in_lc = True
            continue
        if stripped.startswith("/*"):
            if "*/" not in stripped:
                in_bc = True
            continue
        if depth == 0:
            m = field_re.match(line)
            if m and m.group(1) not in existing_names:
                existing_names.add(m.group(1))
                units.append(
                    StructuralUnit(
                        kind=KIND_FIELD,
                        name=m.group(1),
                        span=(idx, idx),
                        body=line,
                        fingerprint=unit_body_fingerprint(line),
                    )
                )
        # Update depth from this line's braces (string-aware-ish).
        for c in _strip_strings_line(stripped):
            if c == "{":
                depth += 1
            elif c == "}":
                depth = max(0, depth - 1)


def _strip_strings_line(line: str) -> str:
    """Remove string/char literals from a single line (cheap depth tracking)."""
    out = []
    i = 0
    n = len(line)
    q: str | None = None
    while i < n:
        c = line[i]
        if q is not None:
            if c == "\\":
                i += 2
                continue
            if c == q:
                q = None
            i += 1
            continue
        if c in ('"', "'", "`"):
            q = c
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _extract_a_import_name(line: str) -> str:
    """Best-effort label for a Family-A import/use/include statement."""
    s = line.strip()
    m = re.match(r"use\s+([A-Za-z_][\w:]*)", s)
    if m:
        return m.group(1)
    # JS/TS: `import { foo, bar } from './mod'` → the module path.
    m = re.match(r"import\s+.*?\s+from\s+['\"]([^'\"]+)", s)
    if m:
        return m.group(1)
    # JS/TS: `import './mod'` or `import name from './mod'`.
    m = re.match(r"import\s+(?:([A-Za-z_]\w*)\s+)?from\s+['\"]([^'\"]+)", s)
    if m:
        return m.group(2) or m.group(1) or "<import>"
    m = re.match(r"import\s+(?:type\s+)?([A-Za-z_][\w{}]*)", s)
    if m:
        return m.group(1)
    m = re.match(r"export\s+\{[^}]*\}\s+from\s+['\"]([^'\"]+)", s)
    if m:
        return m.group(1)
    # Go: `import "fmt"` → the module path.
    m = re.match(r'import\s+["\x27]([^"\x27]+)', s)
    if m:
        return m.group(1)
    m = re.match(r"#include\s+[<\"]([^>\"]+)[>\"]", s)
    if m:
        return m.group(1)
    m = re.match(r"#define\s+([A-Za-z_]\w*)", s)
    if m:
        return m.group(1)
    m = re.match(r"require\s*\(\s*[\"']([^\"']+)", s)
    if m:
        return m.group(1)
    return "<import>"


# ---------------------------------------------------------------------------
# Parse-confidence assessment + entry point
# ---------------------------------------------------------------------------

#: Median line length above which a file is almost certainly minified/generated
#: → no reliable structural signal.
_MINIFIED_LINE_LEN = 200

#: Sanity bound: more top-level units than 1/5 of the line count (on a sizable
#: file) is suspicious (pathological fragmentation) → low confidence.
_FRAGMENTATION_RATIO = 5


def _assess_confidence(source: str, units: list[StructuralUnit]) -> float:
    """Heuristic confidence in the parse (survey §"Robustness over correctness").

    - Minified/generated (median line length > 200) → 0.0 (fall back to LSP).
    - Pathological fragmentation (> lines/5 top-level units on a sizable file)
      → 0.3 (suspicious; likely mis-detection).
    - Otherwise 1.0.
    """
    if not source:
        return 1.0
    line_lens = [len(ln) for ln in source.split("\n")]
    line_lens.sort()
    median = line_lens[len(line_lens) // 2] if line_lens else 0
    if median > _MINIFIED_LINE_LEN:
        return 0.0
    n_lines = len(line_lens)
    # Only flag fragmentation on substantial files (small files legitimately have
    # many short units — a test file with 8 tiny functions at 40 lines is fine).
    if n_lines >= 100 and len(units) > max(1, n_lines // _FRAGMENTATION_RATIO):
        return 0.3
    return 1.0


def parse_file(
    source: str,
    language: str | None = None,
    path: str | None = None,
) -> FileIR | None:
    """Parse ``source`` into a :class:`FileIR`, or ``None`` when unrecognized.

    Dispatches on :func:`detect_family`. Returns ``None`` (no structural signal)
    when the family can't be determined. Never raises. Minified/generated files
    yield a low-``parse_confidence`` FileIR with no units.
    """
    family = detect_family(language, path)
    if family is None:
        return None
    lang = language_for_family_member(language, path) or language
    try:
        if family == FAMILY_B:
            return parse_family_b(source, lang)
        if family == FAMILY_A:
            return parse_family_a(source, lang)
    except Exception:  # noqa: BLE001 — robustness over correctness; never raise.
        return FileIR(
            family=family,
            units=[],
            parse_confidence=0.0,
            source=source,
            language=lang,
        )
    # Family C not yet implemented.
    return None


# ---------------------------------------------------------------------------
# Region queries — the operations the structural API needs
# ---------------------------------------------------------------------------


def _all_units_flat(ir: FileIR) -> list[StructuralUnit]:
    """All non-container-scope units in ``ir`` (top-level + nested), source order.

    Container-scope units (impl/mod/namespace) are SKIPPED — they're distinct
    scopes, not entities — but their children are still walked (so the methods
    of an impl appear, just not the impl itself). This mirrors tree-sitter,
    where ``impl_item`` is a container whose body list is enumerated but the
    impl is never emitted as an entity.
    """
    out: list[StructuralUnit] = []

    def walk(us: list[StructuralUnit]) -> None:
        for u in us:
            if not u.is_container_scope:
                out.append(u)
            if u.children:
                walk(u.children)

    walk(ir.units)
    return out


def enclosing_unit(ir: FileIR, span: tuple[int, int]) -> StructuralUnit | None:
    """The DEEPEST unit enclosing ``span[0]`` (the container/scope for the span).

    Mirrors the existing ``enclosing_node`` contract: walk down through units
    whose span encloses the anchor row, returning the deepest definition-typed
    unit. ``None`` when no unit encloses the anchor.
    """
    anchor = span[0]
    best: StructuralUnit | None = None

    def consider(u: StructuralUnit) -> None:
        nonlocal best
        s0, s1 = u.span
        if s0 <= anchor <= s1:
            if best is None or (u.span[0] >= best.span[0] and u.span[1] <= best.span[1]):
                best = u

    for u in _all_units_flat(ir):
        consider(u)
    return best


def enclosing_container(ir: FileIR, span: tuple[int, int]) -> StructuralUnit | None:
    """The container whose DIRECT children include the anchor's enclosing unit.

    Mirrors ``enumerate_entities(container_span=...)``: descend to the deepest
    unit enclosing the anchor, then return that unit's PARENT (the container
    whose children are the entity neighborhood). Returns ``None`` (module root)
    when the anchor's enclosing unit is top-level.
    """
    deepest = enclosing_unit(ir, span)
    if deepest is None:
        return None  # module root
    # Find the parent of ``deepest`` by walking for a unit whose children include
    # it (i.e. a container whose span encloses ``deepest`` at a shallower level).
    parent: StructuralUnit | None = None

    def search(us: list[StructuralUnit]) -> None:
        nonlocal parent
        for u in us:
            if u.identity == deepest.identity:
                return  # found at this level; parent is whoever holds this list
            if u.children:
                # Is ``deepest`` among this unit's children? If so, this unit is
                # the parent.
                if any(c.identity == deepest.identity for c in u.children):
                    parent = u
                    return
                search(u.children)

    search(ir.units)
    return parent


def units_in_container(ir: FileIR, span: tuple[int, int]) -> list[StructuralUnit]:
    """The CHILDREN of the container enclosing ``span`` (the entity neighborhood).

    Mirrors ``enumerate_entities(container_span=...)``: for an anchor inside a
    class method, return the class's direct children (the sibling methods). For
    an anchor at module scope (inside a top-level function), return the module's
    top-level units. Returns the top-level units when no unit encloses the anchor.
    """
    container = enclosing_container(ir, span)
    if container is None:
        return list(ir.units)
    return list(container.children) if container.children else []

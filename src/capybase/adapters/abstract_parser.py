"""Grammar-free, language-agnostic structural parser.

Replaces tree-sitter grammars with two single-pass state machines:

- **Family A** (brace-delimited, C-syntax family): Rust, JavaScript, TypeScript,
  Go, Java, C/C++, C#, Swift, Kotlin, ... A character-scan state machine that
  tracks brace depth + string/comment state and classifies a declaration-level
  ``{`` by the keyword prefix before it.
- **Family B** (indentation-delimited, off-side rule): Python, Ruby (top-level),
  ... A line-by-line scan tracking indent level.

The parser answers the FIVE questions that drive merge correctness
(scope, identity, kind, body, surfaces):

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

# ``char_ratio`` is a leaf dependency (capybase.diff imports nothing from the
# parser), so this creates no cycle. Used by the canonical rename-pairing core
# (consolidation #2): one name-similarity measure, shared by the 3-way diff,
# the structural resolver, and ``semantic_diff`` — previously each had its own.
from capybase.diff import char_ratio as _char_ratio

# Single source of truth for extension → language. Aliased locally so
# the family dispatch reads naturally; the authoritative map lives in
# ``language.EXTENSION_TO_LANGUAGE``.
from capybase.adapters.language import EXTENSION_TO_LANGUAGE as _EXT_LANG

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
#: ``is_available`` in (to keep the existing skip-path tests green),
#: but the families for the other Family-A languages are declared here so the
#: dispatch is correct and language expansion is a one-line flip.
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

#: File extension → language: now the single source of truth in
#: ``language.EXTENSION_TO_LANGUAGE``. Re-exported above as ``_EXT_LANG``.

#: Languages capybase advertises structural support for. Family A (brace-
#: delimited: Rust, JS, TS, Go, Java, C/C++, C#, Kotlin, Swift, Scala, Dart,
#: PHP) and Family B (indentation-delimited: Python) are all supported by the
#: grammar-free state machines. Family C (declarative/data) is not yet
#: implemented. ``detect_family`` knows the broader map; ``is_available``
#: gates the public API (currently just Python and Rust, to keep the skip-path
#: tests green — expanding it is a one-line flip).
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
# Data model
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

    ``imports`` and ``exports`` are the file's dependency surfaces: imports are
    external names brought in (``import``/``use``/``require``/``#include``);
    exports are top-level public names defined here. As prior work notes, "a
    simple imports-only tool outperformed complex structured tools" — this
    surface enables cross-commit dependency checks without re-scanning the file.
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
    norm = normalize_body(rest)
    # Count MEANINGFUL lines (non-blank, non-comment) so adding a comment line
    # doesn't perturb the digest — the fingerprint is stable under comment
    # additions (the AstPreservationValidator relies on this).
    meaningful = sum(1 for ln in lines[1:] if _has_code_content(ln))
    if not norm:
        return f"l{meaningful}"
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]
    return f"l{meaningful}:{digest}"


def _fingerprint_has_content(fingerprint: str) -> bool:
    """True when a body fingerprint carries a content digest (not just a count).

    Fingerprints are ``l{count}`` for content-less bodies (only blank/comment
    lines after the header) or ``l{count}:{digest}`` for bodies with real code.
    The content-less form is shared by many distinct empty bodies (``pass``-only
    methods, docstring-only functions) and must NOT be used for rename pairing —
    two unrelated empty methods would otherwise pair as a false rename. Fix #13.
    """
    return bool(fingerprint) and ":" in fingerprint


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


def normalize_body(text: str) -> str:
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

    These are scope-boundary signals:
    close any open unit and treat the region as UNKNOWN_BLOCK. Never crash.
    """
    s = line.lstrip()
    return (
        s.startswith("<<<<<<<")
        or s.startswith("=======")
        or s.startswith(">>>>>>>")
    )


#: Brackets whose unclosed state makes a line a continuation of the previous
#: logical line. Only ``()`` counts: signatures wrap inside parens
#: (``def f(\n  a,\n) -> bool:``), but ``{``/``[`` on their own lines are
#: collection literals, not continuation triggers — a malformed dangling ``{``
#: (a merge artifact) must not swallow the next declaration.
_OPEN_BRACKETS = "("
_CLOSE_BRACKETS = ")"


def _line_bracket_delta(raw: str) -> int:
    """Net parenthesis change on a line (string- and comment-aware).

    ``opens - closes`` for ``()`` only, ignoring parens inside string literals
    (blanked first) and inside inline comments (stripped first — a comment
    like ``# see func(`` would otherwise corrupt the continuation depth). Used by
    Family B to track whether a line continues a multi-line signature (``delta``
    doesn't return to zero until the final closing ``)``).
    """
    # Strip inline comments first: a ``# ...`` / ``// ...`` comment may
    # contain unbalanced brackets that must not count.
    stripped = _strip_inline_comment(raw)
    blanked = _STRING_LIT_RE.sub("'_'", stripped)
    delta = 0
    for ch in blanked:
        if ch in _OPEN_BRACKETS:
            delta += 1
        elif ch in _CLOSE_BRACKETS:
            delta -= 1
    return delta


def _ends_with_backslash_continuation(raw: str) -> bool:
    """True when a line ends with a backslash line-continuation.

    Python (and shell) treat a trailing ``\\`` (not escaped, not in a comment or
    string) as joining the next logical line. An odd run of trailing backslashes
    continues; an even run is an escaped backslash (no continuation). String- and
    comment-aware so a ``\\`` inside a string literal or comment doesn't count.
    """
    # Blank strings/comments first so a trailing ``\\`` in those contexts is ignored.
    stripped = _strip_inline_comment(raw)
    blanked = _STRING_LIT_RE.sub("'_'", stripped).rstrip()
    if not blanked or not blanked.endswith("\\"):
        return False
    # Count the trailing backslash run; odd = continuation.
    n = 0
    k = len(blanked) - 1
    while k >= 0 and blanked[k] == "\\":
        n += 1
        k -= 1
    return n % 2 == 1


_TRIPLE_QUOTES = ('"""', "'''")


def _update_triple_quote_state(raw: str, open_triple: str | None) -> str | None:
    """Advance the multi-line triple-quote state across one line.

    ``open_triple`` is the currently-open triple-quote marker (three double or
    three single quotes) carried from a previous line, or ``None`` when no
    multi-line string is open. Returns the new state: ``None`` when the string
    closed on this line (or no string was open and none opened), else the
    still-open marker. A line is "inside a triple-quote string" when the
    returned state is non-None OR the line itself opened a string that didn't
    close.

    Single-line triple-quotes (opening and closing on one line) open AND close
    on the same line → net no state change.
    """
    i = 0
    n = len(raw)
    state = open_triple
    while i < n:
        # If a string is open, look for its closer; everything until then is string content.
        if state is not None:
            idx = raw.find(state, i)
            if idx < 0:
                # Closer not on this line — string continues.
                return state
            # Closer found: string ends here, resume normal scanning after it.
            i = idx + len(state)
            state = None
            continue
        # No string open: scan for a triple-quote opener.
        # Find the earliest of the two markers at/after i.
        earliest = -1
        opened = None
        for mq in _TRIPLE_QUOTES:
            j = raw.find(mq, i)
            if j >= 0 and (earliest < 0 or j < earliest):
                earliest = j
                opened = mq
        if earliest < 0:
            return None  # nothing more on this line
        i = earliest + len(opened)
        state = opened
        # Loop continues: the opener may be closed later on the same line.
    return state


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
# Import / export surface extraction — the structural information that
# actually drives merge correctness (requirement 4 + 5)
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
            # ``export``. Java: ``public``. CommonJS: ``module.exports``. (The
            # earlier ``"EPORTED"`` entry here was a dead typo — ``.split()``
            # never produces it — and ``exported`` isn't a keyword in any target
            # language; removed.)
            first_line = u.body.split("\n", 1)[0] if u.body else ""
            toks = first_line.split()
            is_public = any(kw in toks for kw in ("pub", "export", "public"))
            if not is_public and "module.exports" in first_line:
                is_public = True
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
    # Continuation tracking: a line is a continuation when unclosed
    # brackets (``def f(`` … ``) -> bool:``) or an open triple-quote string span
    # it across newlines. Such lines are absorbed into the enclosing unit's body
    # (computed by source slice) and never trigger a dedent/close — the
    # ``) -> bool:`` of a wrapped signature sits at indent 0 but belongs to the
    # signature, and a ``class Fake:`` inside a docstring is string content.
    cont_depth = 0  # net unclosed ``()`` carried from prior lines (signature wrap)
    open_triple: str | None = None  # open multi-line triple-quote marker, if any
    pending_backslash = False  # prior line ended with a ``\`` continuation
    join_buffer = ""  # accumulated text of a ``\``-continued line

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
        # backslash line-continuation. A prior line ending in ``\`` is joined
        # to this one so the def/class regexes see the full logical line
        # (``def \\<newline>foo():`` → ``def foo():``). The joined text drives
        # detection; span/body use the real source (the ``\\`` line's index is
        # the unit's start). Only the ``def``/``class`` keyword need be on the
        # prior line; the continuation carries the name.
        if pending_backslash:
            # Strip the trailing backslash from the accumulated text and join.
            raw = join_buffer + raw.lstrip()
            pending_backslash = False
        else:
            join_buffer = ""
        # If this line itself ends with a continuation, buffer it and move on —
        # the NEXT line will be the joined detection target.
        if _ends_with_backslash_continuation(raw):
            join_buffer = raw[:-1]  # drop the trailing backslash
            pending_backslash = True
            # Still advance last_line_row so a prior open unit's span covers it.
            last_line_row = i
            continue

        if _is_conflict_marker_line(raw):
            # Close against the last meaningful row (before this marker).
            close_units_at_or_below(0, last_line_row)
            stack.clear()
            pending_decorator_indent = None
            pending_decorator_start = None
            last_line_row = i - 1
            # Reset continuation state — a conflict marker is a hard boundary.
            cont_depth = 0
            open_triple = None
            continue
        if _is_blank_or_comment(raw, language):
            continue

        # Triple-quote continuation: if a multi-line string is open from a prior
        # line, this line is string content (even if it looks like ``class X:``).
        # Advance the state and absorb the line; do NOT close units or detect
        # declarations. The closing line of the string is absorbed too (it's
        # still part of the string literal until the closer is consumed).
        if open_triple is not None:
            open_triple = _update_triple_quote_state(raw, open_triple)
            continue

        indent = _indent_width(raw)
        # Snapshot the last meaningful row BEFORE this line — it's the end row
        # for any unit this line's dedent closes (this line opens a new scope).
        prev_line_row = last_line_row
        last_line_row = i

        if _B_DECORATOR_RE.match(raw):
            # A decorator belongs to the NEXT declaration. It is a scope boundary
            # for the PREVIOUS unit at the same indent: close that unit against
            # the prior meaningful row so it doesn't absorb this decorator.
            # Do NOT advance last_line_row past this line.
            if stack and stack[-1].indent >= indent:
                close_units_at_or_below(indent, prev_line_row)
            last_line_row = prev_line_row  # this line is not a body line
            if pending_decorator_indent is None:
                pending_decorator_indent = indent
                pending_decorator_start = i
            continue

        # Bracket continuation: a line is a continuation when brackets
        # were ALREADY open coming into it (``was_continuing``). The signature
        # opener ``def f(`` itself is processed normally (it opens the unit) and
        # leaves ``cont_depth > 0``; subsequent lines — including the closing
        # ``) -> bool:`` at indent 0 — are absorbed until brackets close.
        delta = _line_bracket_delta(raw)
        was_continuing = cont_depth > 0
        cont_depth = max(0, cont_depth + delta)
        # A line is a continuation (not a scope boundary) when brackets were
        # already open coming in. The opener line (which leaves cont_depth > 0
        # after its own delta) is NOT a continuation — it must be classified.
        is_continuation = was_continuing
        # If a multi-line triple-quote OPENS on this line (and doesn't close),
        # every following line is a continuation until the closer. The opener
        # line itself is still processed (e.g. a ``x = \"\"\"`` assignment).
        new_open_triple = _update_triple_quote_state(raw, None)
        if new_open_triple is not None:
            open_triple = new_open_triple

        if is_continuation:
            # Absorb into the enclosing unit; do not close or open scopes. The
            # body is reconstructed by source slice, so we only need the
            # last_line_row (already advanced above) to extend the unit's span.
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
    "class", "struct", "interface", "trait", "enum", "union", "object",
)
# Container-only keywords: ``impl``/``mod`` open a scope whose children are the
# real entities (methods/fields), but the container itself is NOT emitted as an
# entity — mirroring tree-sitter, where ``impl_item`` has no ``_KIND_BY_NODE_TYPE``
# entry (only its ``implementation_list`` body is enumerated). This matters for
# identity stability: an ``impl Config`` block must not collide with the
# ``struct Config`` definition under the same (class, "Config") identity.
_A_CONTAINER_KEYWORDS = ("impl", "mod", "namespace", "module")
# ``def`` appears here for Python-in-JS-template edge cases and is harmless;
# the canonical Family-A function keywords lead. ``fun`` covers Kotlin —
# without it every top-level Kotlin function was dropped (the keywordless
# heuristic only accepts C free functions at file scope).
_A_FUNC_KEYWORDS = (
    "fn", "func", "fun", "function", "def", "async", "pub", "export",
)
# Field-like (top-level bindings): ``const``/``static``/``type``/``var``/``let``
# declarations without a following ``{`` (so they don't open a scope) — detected
# separately from the brace machine.
_A_FIELD_KEYWORDS = ("const", "static", "type", "var", "let", "final")

# Control-flow keywords whose ``(...)`` + ``{`` shape mimics a keyword-less method
# signature (``if (x) {``, ``while (y) {``, ``switch (s) {``). Used by the
# keyword-less method heuristic in :func:`_classify_a_brace` to reject braces
# that look like a method (identifier + parens + brace) but are actually control
# flow. ``do``/``else``/``try``/``finally`` have no parens but are included as
# defense-in-depth for ``do {`` / ``else {`` at a method-direct depth.
_A_CONTROL_FLOW_KEYWORDS = frozenset({
    "if", "else", "while", "for", "switch", "case", "catch", "do", "try",
    "finally", "synchronized", "lock", "using", "with", "unsafe",
})
# Import-like top-level statements. The first pattern matches declaration-led
# forms (``use``/``import``/``#include`` leading the line). The second
# matches CommonJS ``require()`` as an EXPRESSION — ``const fs = require('fs')``
# — where ``require`` doesn't lead the line. Both are gated by the depth-0 +
# line-start check at the call site, so a ``require()`` inside a function body
# (brace_depth > 0) is correctly NOT detected as an import.
_A_IMPORT_PATTERNS = (
    re.compile(r"^\s*(?:use\s+\S|import\s+\S|export\s+\{[^}]*\}\s+from|require\s*\(|#include\s+)"),
    # ``<binding> = require('...')`` — require as a RHS expression. Allows
    # any prefix (const/let/var/export NAME =, or bare NAME =) before require.
    re.compile(r"^\s*(?:\w+\s+)*\w+\s*=\s*require\s*\(\s*['\"]"),
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


#: String-prefix runes that introduce a non-plain string literal whose closing
#: quote rule differs from a bare ``"``. Used by :func:`_match_string_prefix` to
#: recover Rust raw strings (``r``/``b``/``rb``/``br`` + optional ``#`` run),
#: where an embedded ``"`` in the content must NOT close the string.
_RAW_PREFIX_RUNES = frozenset("rRbB")


def _match_string_prefix(src: str, quote_idx: int) -> int | None:
    """Detect a string prefix ending at ``src[quote_idx] == '"'``.

    Returns the number of trailing ``#`` chars for a Rust raw string
    (``r#"..."#`` → N; ``r"..."`` → 0), or ``0`` for a recognized prefix that
    still closes on a plain ``"`` (byte strings ``b"..."``, ordinary ``"``).
    Returns ``None`` when no prefix is present (the caller treats it as a plain
    quote with hash count 0).

    Recognized Rust raw forms: ``r#*"`` / ``b"`` / ``br#*"`` / ``rb#*"`` — the
    rune ``r`` (possibly preceded by ``b``) optionally followed by 1+ ``#``.
    A raw string's closer is ``"`` + the same number of ``#``.
    """
    j = quote_idx - 1
    # Count trailing ``#`` (the raw-string hash count).
    hash_count = 0
    while j >= 0 and src[j] == "#":
        hash_count += 1
        j -= 1
    # Collect the identifier-run of prefix runes (r, b, br, rb — only these).
    runes = []
    while j >= 0 and src[j] in _RAW_PREFIX_RUNES:
        runes.append(src[j])
        j -= 1
    if not runes:
        # No prefix rune. ``#`` before a bare ``"`` (e.g. ``#"``) isn't a raw
        # string — treat as plain quote.
        return 0
    # Word-boundary check: the rune run must be preceded by a non-identifier
    # character (or start of input). Otherwise the runes are the tail of an
    # identifier (``myr#"..."#`` — the ``r`` is part of ``myr``), not a prefix.
    # Misreading it as a raw prefix corrupts the string state for the rest of
    # the file (the scanner looks for a ``"#`` closer that never comes).
    if j >= 0 and (src[j].isalnum() or src[j] == "_"):
        return 0
    prefix = "".join(reversed(runes)).lower()
    # Valid raw-string prefixes: ``r`` or ``br``/``rb`` (raw / byte-raw). A bare
    # ``b`` is a byte string that closes on a plain ``"`` (hash_count must be 0).
    if prefix in ("r", "br", "rb"):
        return hash_count
    if prefix == "b" and hash_count == 0:
        return 0
    # Unrecognized rune combination — treat as plain (no special closer).
    return 0


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
    # When in_str == '"' and str_hash_count > 0, we're inside a Rust raw string
    # (``r#"..."#``) that closes on ``"`` followed by exactly this many ``#``.
    # A bare ``"`` in the content does NOT close it. 0 = ordinary quote.
    str_hash_count = 0
    in_line_comment = False
    in_block_comment = False
    # Statement-start byte for the in-pass field emitter. Tracks the start
    # of the current top-level statement: advances past ``;`` and past a ``}``
    # that closes back to depth 0, but NOT past ``{``/``}`` inside a statement
    # (so a braced initializer ``const P = Point { ... };`` keeps its start at
    # ``const``). Unlike the token buffer (reset at every brace), this survives
    # internal braces so the field name can be recovered at the terminating ``;``.
    stmt_start_byte = 0
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
                # Escapes only apply to NON-raw strings. A raw string (hash
                # count > 0) treats backslash literally — skip the escape skip.
                if in_str == '"' and str_hash_count > 0:
                    i += 1
                    continue
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
                # Raw string closer: ``"`` must be followed by exactly
                # ``str_hash_count`` ``#`` chars. An embedded ``"`` in
                # the content (with no matching ``#`` run) does NOT close it.
                if str_hash_count > 0:
                    tail = src[i + 1 : i + 1 + str_hash_count]
                    if len(tail) == str_hash_count and tail == "#" * str_hash_count:
                        in_str = None
                        str_hash_count = 0
                        i += 1 + str_hash_count
                        continue
                    # Not the closer — content quote. Fall through to advance.
                    i += 1
                    continue
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
            # Detect raw/byte raw strings (Rust) and other prefixed strings so
            # the closer matches the opener. A Rust raw string
            # ``r#"..."#`` closes on ``"`` + N ``#``; without this, an embedded
            # ``"`` in the content closes early and corrupts brace counting.
            # Look back at the run of identifier chars / ``#`` preceding ``"``.
            prefix = _match_string_prefix(src, i)
            if prefix is not None:
                # prefix is (kind, hash_count). kind "raw" → hash_count ``#``;
                # we model all as ordinary ``"`` strings with an optional hash.
                in_str = '"'
                str_hash_count = prefix
            else:
                in_str = '"'
                str_hash_count = 0
            i += 1
            continue
        if ch == "`":
            in_str = "`"
            i += 1
            continue
        if ch == "'":
            # Rust char-literal vs lifetime (and JS/Python quoted strings which
            # never reach here as a bare ``'`` outside a string). A char literal
            # is ``'X'`` (single content char + closing ``'``); a lifetime is
            # ``'ident`` with NO closing ``'`` (e.g. ``'a``, ``'static``).
            # Discriminator: a ``'`` followed by an identifier char where
            # the char AFTER the identifier run is NOT a closing ``'`` is a
            # lifetime — skip it entirely. This is robust across all the
            # contexts lifetimes appear (``&'a``, ``x: &'static``, ``<'a>``,
            # ``('a)``) because none of them are preceded by alnum/_ (the old
            # rule), and they're all followed by an identifier longer than one
            # char OR a single identifier char followed by a non-``'`` token.
            nxt1 = src[i + 1] if i + 1 < n else ""
            nxt2 = src[i + 2] if i + 2 < n else ""
            prev = src[i - 1] if i > 0 else ""
            # Lifetime: ' + identifier-start char, NOT immediately closed by '.
            # ``'a'`` (char lit) → nxt1='a', nxt2='\'' → NOT a lifetime.
            # ``'a`` / ``'static`` (lifetime) → nxt1 ident, nxt2 != '\'' → lifetime.
            # ``'\n'`` (char lit) → nxt1='\\' → not ident → falls to char path.
            if (
                (nxt1.isalpha() or nxt1 == "_")
                and nxt2 != "'"
                and not (prev.isalnum() or prev == "_")
            ):
                # Rust lifetime ('a / 'static) — don't enter string state.
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
            classified = _classify_a_brace(buf, stack, language, brace_depth)
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
            closed_unit = False
            while stack and brace_depth < stack[-1].open_brace_depth:
                _close_a_unit(stack.pop(), i, src, units, stack, _line_index)
                closed_unit = True
            # When a ``}`` closes a top-level DECLARATION scope back to depth 0
            # (a unit was popped), the next statement starts fresh. But a ``}``
            # closing an object-literal inside a still-open statement (e.g.
            # ``const P = Point { ... };``) must NOT advance the tracker — the
            # statement continues to its ``;``.
            if brace_depth == 0 and closed_unit:
                stmt_start_byte = i + 1
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
            # In-main-pass field detection: at top level, a
            # statement ending in ``;`` that matches the field-declaration shape
            # emits a FIELD unit here — no second whole-file re-scan needed.
            #
            # extract the name from the SOURCE SLICE (stmt_start_byte → ``;``),
            # NOT the token buffer — the buffer is reset at every ``{``/``}``,
            # so a const with a braced initializer (``const P = Point { ... };``)
            # would lose its keyword by the ``;`` and be silently dropped.
            # ``stmt_start_byte`` survives internal braces (only advances at ``;``
            # or when a ``}`` closes to depth 0), so it always points at the
            # declaration keyword.
            if brace_depth == 0 and not stack and language not in (None, "c", "h"):
                stmt_text = src[stmt_start_byte : i + 1]
                fname = _field_name_from_buf(stmt_text)
                if fname is not None:
                    end_row = cur_row_at(i)
                    start_row_f = cur_row_at(stmt_start_byte)
                    body = stmt_text
                    # Dedup against already-emitted units (a brace-opened decl
                    # with the same name, e.g. a Go ``type X struct``).
                    existing_f = {u.name for u in units if u.name}
                    if fname not in existing_f:
                        units.append(
                            StructuralUnit(
                                kind=KIND_FIELD,
                                name=fname,
                                span=(start_row_f, end_row),
                                body=body,
                                fingerprint=unit_body_fingerprint(body),
                            )
                        )
            # A ``;`` ends the statement; the next one starts after it. Only
            # advance at TOP LEVEL: a ``;`` inside braces (e.g. the
            # ``return 1;`` inside ``const f = function() { ... };``) must NOT
            # move the tracker, or the outer ``;`` would slice from mid-statement
            # and the field name recovery would fail. The tracker is only
            # meaningful for top-level statements.
            if brace_depth == 0:
                stmt_start_byte = i + 1
            buf = ""
            i += 1
            continue

        # Preprocessor / attribute lines (Rust ``#``, C ``#``, JS/TS decorators
        # via leading ``@`` are handled by the keyword buffer). At line start +
        # depth 0, ``#`` begins a preprocessor/attribute line.
        if ch == "#" and i == line_start and brace_depth == 0:
            attr_line = src[i:src.find("\n", i)] if "\n" in src[i:] else src[i:]
            stripped = attr_line.strip()
            if stripped.startswith("#[") or stripped.startswith("#!"):
                # Rust attribute → attach to the following decl (no unit emitted).
                pending_attr_row = row
            elif stripped.startswith("#include") or stripped.startswith("#define"):
                # MODULE_STMT for #include / #define (emits + advances).
                i, row, line_start = _emit_module_stmt_line(src, i, units, row, n)
                buf = ""
                stmt_start_byte = i
                continue
            else:
                # Other ``#`` line (e.g. a lone shebang or malformed) — consume
                # the whole line as a complete statement, no unit emitted.
                i, row, line_start, _ = _consume_line_as_statement(src, i, n, row, line_start)
                buf = ""
                stmt_start_byte = i
                continue

        # Import / use statements (depth 0): detect at line start.
        if brace_depth == 0 and paren_depth == 0 and i == line_start:
            line_text = src[i:src.find("\n", i)] if "\n" in src[i:] else src[i:]
            if _A_IMPORT_PATTERNS[0].match(line_text) or any(
                p.match(line_text) for p in _A_IMPORT_PATTERNS
            ):
                i, row, line_start = _emit_module_stmt_line(src, i, units, row, n)
                buf = ""
                stmt_start_byte = i
                continue

        # Field-like declarations at depth 0 without an opening brace: detect on
        # the ``;`` terminator. We accumulate into buf and check at ``;`` above —
        # but we only emit a FIELD unit if no brace opened.

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

    # Field units are emitted in the main scan at the ``;`` terminator,
    # so no second whole-file re-scan is needed here.

    # newline/expression-body declarations the brace machine can't catch
    # (Kotlin ``data class Name(...)`` with no body, ``fun m() = expr`` with an
    # expression body instead of a block). These have no ``{`` so the brace scan
    # never fires. A guarded line-scan supplements them — deduped against the
    # brace-machine units so a ``fun`` with a block body isn't double-counted.
    _emit_a_expression_body_units(source, units, language, _line_index)

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


def _go_declaration_name(
    toks: list[str], last_kw_idx: int, last_kw: str,
) -> tuple[str | None, bool]:
    """Recover the declaration name for Go-specific signature shapes (fixes #4, #5).

    Go diverges from the generic after-keyword name lookup in two common cases:

    - **Receiver method**: ``func (recv) Name(...)`` — the token after
      ``func`` is ``(`` (the receiver), not the name. The real name is the
      identifier just before the final parameter-list ``(...)``. A Go receiver
      method is syntactically top-level (not nested in the type body), but
      semantically a method of the receiver's type — so this returns
      ``is_receiver_method=True`` so the caller classifies it as METHOD.

    - **Type declaration**: ``type Name struct/interface{...}`` — Go
      puts the name BEFORE ``struct``/``interface`` (the class keyword), so the
      generic lookup finds nothing after the keyword. The name is the token
      between ``type`` and the keyword.

    Returns ``(name, is_receiver_method)``. ``name`` is None when no Go-specific
    shape applies (leaving the generic name in place).
    """
    # Receiver method: ``func (recv) Name (params)`` — only for func keywords.
    if last_kw in ("func",):
        joined = " ".join(toks)
        # Find the last balanced ``(...)`` run ending the buffer.
        paren_open = -1
        depth_p = 0
        for idx in range(len(joined) - 1, -1, -1):
            c = joined[idx]
            if c == ")":
                depth_p += 1
            elif c == "(":
                if depth_p > 0:
                    depth_p -= 1
                    if depth_p == 0:
                        paren_open = idx
                        break
        # Receiver shape: ``func ( recv ) Name (params)`` — TWO paren groups.
        # The non-receiver ``func Name(params)`` has ONE. Detect the receiver by
        # checking whether the token right after ``func`` is ``(``.
        after_kw = toks[last_kw_idx + 1 :] if last_kw_idx + 1 < len(toks) else []
        is_receiver = bool(after_kw) and after_kw[0].startswith("(")
        if is_receiver:
            # Receiver method: name is the identifier just before the param list.
            if paren_open > 0:
                before = joined[:paren_open].rstrip()
                if before:
                    name_tok = before.split()[-1]
                    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name_tok):
                        return name_tok, is_receiver
        else:
            # Non-receiver: ``func Name(params)`` or generic ``func Name[T any](params)``.
            # The name is the identifier right after ``func``. For generics the
            # type-param list ``[T any]`` may be glued to the name
            # (``Map[T``) — strip it. The previous code used the
            # "last balanced ()" + take-token-before approach, which misread
            # generic signatures (the inner ``func(T) U`` param type's parens
            # confused the scan → name=None). The name is always the token
            # directly after ``func``.
            #
            # subtlety: the reverse keyword scan in the caller may have
            # found an INNER ``func`` (from a param type like ``f func(T) U``)
            # rather than the declaration's ``func``. The declaration's ``func``
            # is the FIRST ``func`` token in the buffer (it leads the statement).
            # Find it and take the name right after.
            decl_func_idx = last_kw_idx
            for k, t in enumerate(toks):
                if t == "func" or re.split(r"[<(:\[{]", t, maxsplit=1)[0] == "func":
                    decl_func_idx = k
                    break
            if decl_func_idx + 1 < len(toks):
                cand = toks[decl_func_idx + 1]
                cand = re.split(r"\[", cand, maxsplit=1)[0]
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cand):
                    return cand, False

    # Type declaration: ``type Name struct/interface`` — name is between ``type``
    # and the class keyword. For Go generics (``type Container[T any] struct``)
    # the type-param list splits into extra tokens, so scan backwards from the
    # class keyword for the ``type`` keyword; the name is the token right after it
    # (with any ``[...]`` stripped).
    if last_kw in _A_CLASS_KEYWORDS:
        for back in range(last_kw_idx - 1, -1, -1):
            if toks[back] == "type" and back + 1 <= last_kw_idx:
                cand = toks[back + 1]
                cand = re.split(r"\[", cand, maxsplit=1)[0]
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cand):
                    return cand, False
                break

    return None, False


def _classify_a_brace(
    buf: str, stack: list[_OpenAUnit], language: str | None,
    brace_depth: int = 0,
) -> tuple[str, str | None, bool] | None:
    """Classify a declaration-level ``{`` from the preceding token buffer.

    Returns ``(kind, name, container_only)`` when the buffer ends in a
    declaration pattern, else ``None`` (a bare/object-literal brace). ``name``
    is the identifier following the declaration keyword (the function/class
    name); ``None`` for an anonymous block. ``container_only`` is True for
    impl/mod/namespace (containers whose children are the real entities).

    Two classification paths:

    1. **Keyword-prefixed** (the common case): the buffer contains a declaration
       keyword (``fn``/``func``/``function``/``def``/``class``/``struct``/...).
       The name is the identifier after the keyword.

    2. **Keyword-less method** (Java/C/C++/C#/Dart): when NO keyword is found,
       a heuristic recognizes the ``<type> <name> (<params>) {`` shape at the
       depth where methods live (directly inside a class/container body, or at
       file scope). This is gated by ``brace_depth`` so control-flow braces
       (``if (x) {`` inside a method body, which sit one level deeper) are
       excluded by construction. See :func:`_classify_keywordless_method`.
    """
    # The buffer is a whitespace-normalized run ending where the ``{`` is. Strip
    # a trailing ``{``-adjacent tokens we may have already added.
    b = buf.strip().rstrip("{").strip()
    if not b:
        return None
    toks = b.split()
    if not toks:
        return None
    # an initializer brace — ``const P = Point { ... }``, ``let m = Map { ... }``,
    # ``const cfg = { ... }`` — is an object/struct literal inside a field declaration,
    # NOT a scope-opening declaration. A buffer containing a field keyword + ``=`` whose
    # token after ``=`` is an object/struct literal start (a bare ``{`` or a capitalized
    # type name) is a braced initializer; return None so it's treated as depth-only
    # (the field is emitted at the terminating ``;``).
    #
    # this is NARROWER than the original  (which fired for any
    # ``field-kw + =``). The old guard also rejected function/class/arrow
    # expressions assigned to a binding — ``const f = function() { ... }``,
    # ``const C = class { ... }``, ``const h = () => { ... }`` — silently dropping
    # the whole declaration. Those must fall through to normal classification (the
    # ``{`` IS a real declaration brace for the inner function/class). So the guard
    # only fires when the token after ``=`` is NOT a function/class keyword and NOT
    # a ``(`` (arrow function), i.e. it's genuinely an object/struct literal.
    #
    # Note: tokens are whitespace-split, so ``function()`` may be a single token
    # (no space) — match by prefix, not equality.
    if "=" in toks and any(t in _A_FIELD_KEYWORDS for t in toks):
        eq_idx = toks.index("=")
        after_eq = toks[eq_idx + 1 :] if eq_idx + 1 < len(toks) else []
        first_after = after_eq[0] if after_eq else ""
        # ``= function`` / ``= function()`` / ``= class`` → a function/class
        # EXPRESSION; let it classify. ``= ( ... ) =>`` → arrow function; let it
        # classify (``= (`` is the tell — the ``=>`` isn't in the buffer at ``{``).
        if (
            first_after.startswith("function")
            or first_after.startswith("class")
            or first_after.startswith("(")
        ):
            pass  # function/class/arrow expression — NOT an initializer literal
        else:
            return None  # genuine object/struct-literal initializer
    # JS/TS arrow function with a block body — ``const f = (...) => {`` or
    # ``const f = () => {``. The buffer ends in ``=>`` (the arrow), with a
    # binding name before ``=``. No declaration keyword is present, so the
    # keyword path and the keywordless heuristic (which requires ``)`` at the
    # end) both miss it. Recognize the arrow shape explicitly and recover the
    # binding name via ``_binding_name_before`` (treating ``=>`` as the
    # pseudo-keyword position).
    if toks and toks[-1] == "=>" and "=" in toks:
        eq_idx = toks.index("=")
        # The name is the token before ``=`` (recovered only if it's a binding).
        if eq_idx >= 1:
            cand = toks[eq_idx - 1]
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cand):
                # Require a field keyword before the name (so it's a binding,
                # not a reassignment ``x = (...) => {``).
                for j in range(eq_idx - 2, -1, -1):
                    if toks[j] in _A_FIELD_KEYWORDS or toks[j] in ("export", "pub", "public"):
                        kind = KIND_METHOD if stack and any(
                            (u.kind == KIND_CLASS or u.container_only) for u in reversed(stack)
                        ) else KIND_FUNCTION
                        return (kind, cand, False)
                    if toks[j] in (";", "}", "{"):
                        break
    # Find the LAST declaration keyword in the buffer. A keyword may be glued
    # to the following ``(`` / ``<`` / ``:`` in the whitespace-normalized
    # tokens (``function()`` / ``fn<T>``), so match by prefix: a token whose
    # head (before the first ``(``/``<``/``:``) is a declaration keyword counts.
    last_kw_idx = -1
    last_kw = ""
    for idx in range(len(toks) - 1, -1, -1):
        t = toks[idx]
        head = re.split(r"[<(:\[{]", t, maxsplit=1)[0]
        if (
            t in _A_CLASS_KEYWORDS
            or t in _A_FUNC_KEYWORDS
            or t in _A_CONTAINER_KEYWORDS
            or (head != t and head in _A_FUNC_KEYWORDS)
            or (head != t and head in _A_CLASS_KEYWORDS)
        ):
            last_kw_idx = idx
            last_kw = head if head != t else t
            break
    if last_kw_idx < 0:
        # No declaration keyword. Try the keyword-less method heuristic — the
        # path that recovers Java/C/C++/C# methods (whose signatures lead with
        # a return type, not a keyword). Returns None for control flow / object
        # literals / anything that isn't a method-shaped declaration.
        return _classify_keywordless_method(toks, stack, brace_depth)

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

    # function/class EXPRESSION assigned to a binding —
    # ``const NAME = function() {`` / ``let NAME = class {``. The keyword is
    # ``function``/``class`` and the name after it is empty (anonymous) or the
    # expression's own name (e.g. ``class Inner``), but the entity the binding
    # defines is NAME. Recover it by scanning BACKWARDS from the keyword for a
    # ``field-kw NAME =`` shape. This makes ``const Foo = class {...}`` produce
    # CLASS ``Foo`` (not an anonymous class), so the binding is tracked as an
    # entity for merge identity.
    if name is None and last_kw in ("function", "class"):
        name = _binding_name_before(toks, last_kw_idx)

    # Go-specific name recovery (fixes #4 and #5): Go puts names in positions the
    # generic after-keyword lookup misses. ``go_is_receiver`` signals a receiver
    # method (``func (recv) Name()``), which is top-level syntactically but
    # semantically a METHOD of the receiver's type.
    go_is_receiver = False
    if language == "go":
        go_name, go_is_receiver = _go_declaration_name(toks, last_kw_idx, last_kw)
        if go_name is not None:
            name = go_name

    # Container-only keywords (impl/mod/namespace): open a scope but emit nothing.
    if last_kw in _A_CONTAINER_KEYWORDS:
        # Mark as class-kind for span/body purposes; container_only suppresses
        # the entity emission and passes children through.
        return (KIND_CLASS, name, True)

    # Class vs function vs method.
    if last_kw in _A_CLASS_KEYWORDS:
        kind = KIND_CLASS
        return (kind, name, False)
    # FUNCTION unless nested inside an open CLASS or container, OR a Go receiver
    # method (top-level syntactically, but a method semantically) → METHOD.
    in_container = any(
        (u.kind == KIND_CLASS or u.container_only) for u in reversed(stack)
    )
    if go_is_receiver or in_container:
        return (KIND_METHOD, name, False)
    return (KIND_FUNCTION, name, False)


def _classify_keywordless_method(
    toks: list[str], stack: list[_OpenAUnit], brace_depth: int,
) -> tuple[str, str | None, bool] | None:
    """Recognize a keyword-less method/function declaration before a ``{``.

    Handles Java/C/C++/C#/Dart methods and C free functions, whose signatures
    have no declaration keyword (``int foo() { ... }``, ``void bar() { ... }``).
    The default keyword-recognition in :func:`_classify_a_brace` misses these
    entirely — every method is absorbed into its class. This recovers them via a
    conservative four-guard heuristic:

    1. **Method-depth**: the ``{`` directly enters a container body. ``brace_depth``
       must equal the top open frame's ``open_brace_depth`` (a direct child of
       the class/struct/impl), OR be 0 with no open container (a C free
       function). Control flow inside a method body sits at ``brace_depth >=
       container_depth + 1`` and is excluded here — this is the critical guard,
       because an ``if``/``while`` inside an *unrecognized* method shares the
       same ``[class]`` stack as the method itself, so the stack alone can't
       tell them apart.
    2. **Signature shape**: the buffer ends in ``<name> (<...>)`` — an
       identifier (the method name) immediately followed by a parenthesized
       parameter list, immediately before the ``{``.
    3. **Name is an identifier**: the token before ``(`` is a valid name.
    4. **Not control flow**: the token before ``(`` is not a control-flow keyword
       (``if``/``while``/``for``/``switch``/...). Defense-in-depth for the rare
       case a control-flow construct appears at method-depth.

    Returns ``(METHOD or FUNCTION, name, False)`` on a match, else ``None`` (a
    bare/object-literal/control-flow brace). The kind is METHOD when inside a
    container, FUNCTION at file scope (C free functions).
    """
    if not toks:
        return None
    # Guard 1: method-depth. The ``{`` must directly enter a container body.
    if stack:
        top = stack[-1]
        is_method_depth = (brace_depth == top.open_brace_depth)
        in_container = any(
            (u.kind == KIND_CLASS or u.container_only) for u in reversed(stack)
        )
    else:
        # No open container — only a file-scope free function (C) qualifies.
        is_method_depth = (brace_depth == 0)
        in_container = False
    if not is_method_depth:
        return None

    # Guard 2: signature shape ``<name> (<...>)``. The last token must end with
    # ``)`` (the closing paren of the param list) and the token before it must
    # contain ``(`` (so there's a param list). We look for the ``name (`` pair.
    # The buffer is whitespace-normalized; ``foo()`` may be one token or ``foo``
    # + ``()`` depending on spacing. Find the last ``(`` to locate the param list.
    # Re-join to scan the raw shape (tokens were split on whitespace).
    joined = " ".join(toks)
    # Find the parameter list: the last ``(`` ... ``)`` run ending the buffer.
    paren_open = -1
    depth_p = 0
    for idx in range(len(joined) - 1, -1, -1):
        c = joined[idx]
        if c == ")":
            depth_p += 1
            if paren_open < 0:
                # Must end in ``)`` for a signature (params before ``{``).
                if idx != len(joined) - 1:
                    return None
        elif c == "(":
            if depth_p > 0:
                depth_p -= 1
                if depth_p == 0:
                    paren_open = idx
                    break
    if paren_open < 0:
        return None  # no balanced param list → not a signature
    # The name is the identifier immediately before ``(``.
    name_part = joined[:paren_open].rstrip()
    if not name_part:
        return None
    # The name is the last whitespace-separated token of the pre-param run.
    name_tok = name_part.split()[-1] if name_part.split() else ""
    # Guard 3: valid identifier.
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name_tok):
        return None
    # Guard 4: not a control-flow keyword.
    if name_tok in _A_CONTROL_FLOW_KEYWORDS:
        return None
    kind = KIND_METHOD if in_container else KIND_FUNCTION
    return (kind, name_tok, False)


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


def _binding_name_before(toks: list[str], kw_idx: int) -> str | None:
    """Recover the binding name for a ``field-kw NAME = function/class {`` shape.

    Used by :func:`_classify_a_brace` when a function/class EXPRESSION is
    assigned to a binding (``const Foo = class { ... }``, ``let h = function() {``).
    The keyword after ``=`` is ``function``/``class``; the entity the merge cares
    about is the binding NAME, not the (often anonymous) expression. Scans
    backwards from ``kw_idx`` looking for ``... NAME =`` immediately before the
    ``=`` that precedes the keyword, where NAME follows a field keyword
    (``const``/``let``/``var``/``static``/``final``). Returns ``None`` when the
    shape doesn't match (the keyword wasn't part of a binding expression).
    """
    # Walk back from the keyword: expect ``=`` right before it (possibly with
    # the expression's own name between, e.g. ``const Foo = class Inner {``).
    # Find the ``=`` that opens the expression.
    eq_idx = -1
    for j in range(kw_idx - 1, -1, -1):
        if toks[j] == "=":
            eq_idx = j
            break
        # Stop at a statement boundary / another declaration — no binding here.
        if toks[j] in (";", "}", "{") or toks[j] in _A_FUNC_KEYWORDS:
            return None
    if eq_idx < 1:
        return None  # nothing before the ``=``
    # NAME is the token immediately before ``=``.
    name_tok = toks[eq_idx - 1]
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name_tok):
        return None
    # Require a field keyword somewhere before the NAME (so ``a = function()`` —
    # a reassignment, not a binding — isn't misread). Look back a few tokens.
    for j in range(eq_idx - 2, -1, -1):
        if toks[j] in _A_FIELD_KEYWORDS or toks[j] in ("export", "pub", "public"):
            return name_tok
        if toks[j] in (";", "}", "{"):
            break  # statement boundary — no field keyword found
    return None


#: Regex matching a top-level field declaration's accumulated token buffer, e.g.
#: ``pub const N : u32 = 5`` or ``let x = 1`` or ``type Foo = Bar``. Captures the
#: declared name. Used by the in-main-pass field emitter — mirrors the
#: line-regex the old ``_emit_a_field_units`` re-scan used, but operates on the
#: whitespace-normalized buffer at the ``;`` terminator.
_A_FIELD_RE = re.compile(
    r"^(?:(?:pub|export|public|private|static|final|readonly|unsafe|inline)\s+)*"
    r"(?:const|static|type|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\b"
)


def _field_name_from_buf(buf: str) -> str | None:
    """The declared name in a token buffer ending at ``;``, or ``None``.

    Returns the name when the buffer matches a top-level field-declaration shape
    (optional modifiers + a field keyword + an identifier); ``None`` otherwise.
    Used by the in-main-pass field emitter so fields are detected in
    the same scan that tracks brace depth, eliminating the second whole-file
    re-scan (``_emit_a_field_units``) and its divergent string-state tracker.
    """
    m = _A_FIELD_RE.match(buf.strip().rstrip(";").strip())
    if m:
        return m.group(1)
    return None


#: Patterns for newline/expression-body declarations the brace machine misses
#:. These have no ``{`` (bodyless or expression-body), so the brace scan
#: never fires. Matched at line start (after optional modifiers). Each pattern
#: captures the declared name. Only applied for languages whose syntax uses
#: these forms (Kotlin, Scala) — the brace machine handles the rest.
_A_EXPR_BODY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Kotlin/Scala expression-body fun: ``fun name(...) [: Type] = expr``
    # (no block body — those are caught by the brace machine).
    ("function", re.compile(
        r"^\s*(?:public\s+|private\s+|protected\s+|internal\s+|open\s+|abstract\s+|override\s+|suspend\s+|inline\s+)*"
        r"fun\s+([A-Za-z_][A-Za-z0-9_]*)\s*\([^)]*\)[^=]*=\s*\S"
    )),
    # Kotlin data class (bodyless): ``data class Name(...)`` — no ``{``.
    ("class", re.compile(
        r"^\s*(?:public\s+|private\s+|protected\s+|internal\s+|open\s+|abstract\s+|sealed\s+)*"
        r"data\s+class\s+([A-Za-z_][A-Za-z0-9_]*)\s*[\({:]"
    )),
    # Kotlin/Scala bodyless class: ``class Name(...)`` with NO following ``{``
    # on the same line (a one-line primary-constructor-only class). The brace
    # machine catches multi-line ``class Name {``; this catches the bodyless
    # form. The caller's dedup ensures a braced class isn't double-counted.
    ("class", re.compile(
        r"^\s*(?:public\s+|private\s+|protected\s+|internal\s+|open\s+|abstract\s+|sealed\s+|final\s+)*"
        r"class\s+([A-Za-z_][A-Za-z0-9_]*)\s*\([^)]*\)\s*(?::\s*[^{=]+)?(?=\s*(?:$|//[^\n]*))"
    )),
]


def _emit_a_expression_body_units(
    source: str,
    units: list[StructuralUnit],
    language: str | None,
    line_index: list[int],
) -> None:
    """Supplement the brace machine with newline/expression-body declarations.

    The brace scanner only fires on ``{`` — it cannot see declarations with no
    block body: Kotlin ``data class Point(...)``, ``fun m(): Int = expr``, or a
    bodyless ``class C(val x)``. These are common in idiomatic Kotlin. This
    line-scan catches them via :data:`_A_EXPR_BODY_PATTERNS` and appends them to
    ``units`` (mutating in place), deduped against the brace-machine units by
    ``(kind, name)`` so a ``fun``/``class`` WITH a block body (already emitted)
    isn't double-counted. Only runs for Kotlin/Scala; a no-op for other langs.
    """
    if language not in ("kotlin", "scala"):
        return
    lines = source.split("\n")
    # Existing identities (brace-machine units, top-level + nested) — dedup key
    # so a ``fun``/``class`` WITH a block body isn't double-counted.
    existing: set[tuple[str, str]] = set()

    def _collect(us: list[StructuralUnit]) -> None:
        for u in us:
            existing.add(u.identity)
            if u.children:
                _collect(u.children)

    _collect(units)
    for row, line in enumerate(lines):
        # Skip lines inside a string/comment heuristically — a ``fun`` inside a
        # multi-line string would be a false positive. Best-effort: require the
        # match to start at the line's first non-space token.
        for kind, pat in _A_EXPR_BODY_PATTERNS:
            m = pat.match(line)
            if not m:
                continue
            name = m.group(1)
            ident = (kind, name)
            if ident in existing:
                continue  # brace machine already caught this (block body)
            body = line
            units.append(
                StructuralUnit(
                    kind=kind,
                    name=name,
                    span=(row, row),
                    body=body,
                    fingerprint=unit_body_fingerprint(body),
                )
            )
            existing.add(ident)
            break  # one declaration per line


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
    # CommonJS require-as-expression — ``const fs = require('fs')``.
    # Extract the module path (the import surface tracks dependencies, so the
    # path is the useful name; the binding name ``fs`` is incidental).
    m = re.search(r"require\s*\(\s*[\"']([^\"']+)", s)
    if m:
        return m.group(1)
    return "<import>"


def _emit_module_stmt_line(
    src: str,
    line_start_byte: int,
    units: list[StructuralUnit],
    row: int,
    n: int,
) -> tuple[int, int, int]:
    """Emit a MODULE_STMT unit for one source line; return updated scan state.

    Consolidates the duplicated "find line end → emit MODULE_STMT → advance"
    sequence shared by the ``#``-preprocessor handler and the import/use handler
    in :func:`parse_family_a` (consolidation #5). Both built a ``StructuralUnit``
    from a single line and then did the same row/line_start/i advancement — now
    the import handler calls this helper directly, and the ``#`` handler calls
    it for the ``#include``/``#define`` MODULE_STMT case.

    Args:
        src: the full source text.
        line_start_byte: byte offset where the line begins.
        units: the accumulator to append the emitted unit to.
        row: the current 0-based line number.
        n: ``len(src)`` (for the no-trailing-newline bounds guard).

    Returns:
        ``(i, row, line_start_byte)`` — the updated scan position after the line
        is consumed: ``i`` points past the newline, ``row`` is incremented, and
        ``line_start_byte`` is the start of the next line (or unchanged when the
        line had no trailing newline).
    """
    line_end = src.find("\n", line_start_byte)
    if line_end < 0:
        line_end = n
    line_text = src[line_start_byte:line_end]
    units.append(
        StructuralUnit(
            kind=KIND_MODULE_STMT,
            name=_extract_a_import_name(line_text.strip()),
            span=(row, row),
            body=line_text,
            fingerprint=unit_body_fingerprint(line_text),
        )
    )
    i = line_end + 1
    row += 1
    # Guard the bounds: when the line was the last line (no trailing newline),
    # ``line_end == n`` and ``src[line_end]`` would overflow.
    if line_end < n and src[line_end] == "\n":
        line_start_byte = line_end + 1
    return i, row, line_start_byte


def _consume_line_as_statement(
    src: str,
    i: int,
    n: int,
    row: int,
    line_start: int,
) -> tuple[int, int, int, int]:
    """Advance the scan past the current line, treating it as a complete statement.

    Returns ``(next_i, next_row, next_line_start, line_end)`` where ``line_end``
    is the byte offset of the line's end (the newline, or ``n`` for the last
    line). ``stmt_start_byte`` should be set to ``next_i`` by the caller (a
    complete statement ends at the newline). Used by the ``#``-preprocessor
    handler to advance past lines that are NOT MODULE_STMTs (Rust attributes)
    but still consume the whole line + reset the statement tracker.
    """
    line_end = src.find("\n", i)
    if line_end < 0:
        line_end = n
    next_i = line_end + 1
    next_row = row + 1
    next_line_start = line_end + 1 if (line_end < n and src[line_end] == "\n") else line_start
    return next_i, next_row, next_line_start, line_end


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
    """Heuristic confidence in the parse.

    - Minified/generated (median line length > 200) → 0.0 (fall back to LSP).
    - Pathological fragmentation (> lines/5 NON-test top-level units on a sizable
      file) → 0.3 (suspicious; likely mis-detection). ``is_test`` units are
      excluded: a large test module with many small ``test_*`` functions is
      normal, not fragmentation.
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
    # Exclude test units: a 60-test module is a legitimate test file, not
    # pathological fragmentation (previously flagged at confidence 0.3).
    non_test = [u for u in units if not u.is_test]
    if n_lines >= 100 and len(non_test) > max(1, n_lines // _FRAGMENTATION_RATIO):
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


def all_units_flat(ir: FileIR) -> list[StructuralUnit]:
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


def has_duplicate_identities(units) -> bool:
    """True when ``units`` contains two entries with the same ``.identity``.

    Duplicate identities (e.g. two ``(method, "f")`` from Java/C++/Python
    overloads or re-definitions) collide silently in the identity-keyed dicts
    of :func:`compute_structural_diff_3way` and the entity_disjoint rule,
    dropping all but one unit — a silent missed-conflict data-loss bug.
    Callers use this to detect the collision and decline (escalate to the LLM
    path) rather than silently truncating. Works on any objects with an
    ``.identity`` attribute (``StructuralUnit`` or the public ``Entity``).
    """
    seen: set = set()
    for u in units:
        ident = u.identity
        if ident in seen:
            return True
        seen.add(ident)
    return False


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

    for u in all_units_flat(ir):
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




# ---------------------------------------------------------------------------
# Rename detection — the CANONICAL core (consolidation #2)
# ---------------------------------------------------------------------------
#
# Canonical rename detection. The rule: a base entity whose body reappears
# under a NEW similar name on a side, with the old name gone, is a rename —
# not a base-kept + side-added pair. The body signal is header-stripped and
# normalized (``entity_body_content``); name similarity uses ``_char_ratio``.
#
# One body signal, one name-similarity measure, one threshold — shared by all
# callers (the 3-way diff, the structural resolver, and ``semantic_diff``).

#: Minimum name-similarity (char_ratio) to confirm a body-content rename pair,
#: OR the body must be substantial (>= this many chars) so two distinct entities
#: sharing a trivial body (``pass`` / ``return 1``) don't false-pair.
RENAME_NAME_SIMILARITY_THRESHOLD = 0.6

#: A body-content signal must be at least this long to count as "substantial"
#: enough to confirm a rename without name similarity (mirrors the prior
#: ``len(body) >= 8`` guard in the resolver and ``_body_is_substantial``).
_RENAME_SUBSTANTIAL_BODY_MIN = 8


def _scope_opener_brace(line: str) -> int:
    """Index of the first ``{`` opening a body, or -1.

    Skips braces inside strings/char-literals and inside ``()``/``[]`` (e.g. an
    array literal ``[1, 2]`` or a generic ``Vec<{...}>`` — rare on a header line).
    The first brace at paren/bracket depth 0 is the body opener. Used by
    :func:`split_header_body` for single-line Family-A declarations.
    """
    depth = 0
    i = 0
    n = len(line)
    quote: str | None = None
    while i < n:
        c = line[i]
        if quote is not None:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ('"', "'", "`"):
            quote = c
            i += 1
            continue
        if c in "([":
            depth += 1
        elif c in ")]":
            depth = max(0, depth - 1)
        elif c == "{" and depth == 0:
            return i
        i += 1
    return -1


def _scope_opener_colon(line: str) -> int:
    """Index of the first ``:`` at bracket-depth 0, or -1.

    For a Family-B one-liner (``def foo(a: int) -> str: body``) the body-opening
    colon is the first ``:`` NOT inside ``()``/``[]``/``{}`` — so type-annotation
    colons inside the parameter list are skipped. Returns -1 when there is none
    at depth 0 (e.g. ``const N = 5;``). Used by :func:`split_header_body`.
    """
    depth = 0
    i = 0
    n = len(line)
    quote: str | None = None
    while i < n:
        c = line[i]
        if quote is not None:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ('"', "'", "`"):
            quote = c
            i += 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth = max(0, depth - 1)
        elif c == ":" and depth == 0:
            return i
        i += 1
    return -1


def split_header_body(body: str) -> tuple[str, str]:
    """Split a unit's body text into (header, rest), whitespace-normalized.

    The CANONICAL header/body split for rename detection (consolidation #2). A
    rename changes the def/fn header (``def foo`` → ``def bar``) but leaves the
    body content identical, so rename detection compares the header-STRIPPED
    body. (Note: ``structural._split_header_body`` intentionally diverges — it
    preserves comments because body-CHANGE detection must be comment-sensitive,
    while rename detection is comment-stable.)

    For a MULTI-LINE body the header is ``lines[0]`` (the def/fn/class line) and
    the rest is the body lines below it — the common case. For a SINGLE-LINE body
    (``fn foo() { 1 }``, ``def foo(): return 1``) ``lines[0]`` is the WHOLE body,
    so the naive split leaves ``rest=""`` — which makes every one-liner body edit
    register as a signature change and breaks rename pairing. We instead split a
    one-liner at its scope opener: the first ``{`` (Family A) or the first ``:``
    at bracket-depth 0 (Family B), so the signature lands in the header and the
    inline body lands in ``rest``. Detected from the text itself (no language
    param); falls back to the whole-line-as-header when no opener is found.
    """
    body = body or ""
    if not body:
        return "", ""
    lines = body.split("\n")
    if len(lines) > 1:
        # Multi-line: header is the declaration line, rest is the body below it.
        header = lines[0]
        rest = "\n".join(lines[1:])
        return _normalize_header(header), normalize_body(rest)
    # Single-line body: split at the scope opener so the body content isn't
    # folded into the header. Family A opens with ``{``; Family B with ``:``.
    line = lines[0]
    brace = _scope_opener_brace(line)
    if brace >= 0:
        header = line[: brace + 1]
        rest = line[brace + 1 :]
        # Drop the single matching closing ``}`` (the function's own brace) so the
        # rest is the body content, not ``... }``. Best-effort; nested braces in
        # the body are left as-is (both sides compare identically regardless).
        r = rest.rstrip()
        if r.endswith("}"):
            rest = r[:-1]
        return _normalize_header(header), normalize_body(rest)
    colon = _scope_opener_colon(line)
    if colon >= 0:
        header = line[: colon + 1]
        rest = line[colon + 1 :]
        return _normalize_header(header), normalize_body(rest)
    # No opener found (e.g. a field ``const N = 5;``): keep prior behavior.
    return _normalize_header(line), ""


def entity_body_content(body: str) -> str:
    """The header-stripped, comment/string-normalized body content.

    A rename changes the def/fn/class header (``def loadData`` → ``def
    fetchData``) but leaves the body content identical, so rename detection must
    compare bodies with the HEADER STRIPPED. The remaining text is normalized
    via :func:`normalize_body` (whitespace-collapsed, comments dropped, string
    literals blanked) so a rename that picked up an incidental comment or a
    changed string value still pairs with its base original — while a genuine
    body change (``fetch()`` → ``save()``) still differs and correctly does NOT
    pair.

    This is the CANONICAL body signal for rename detection (consolidation #2).
    It delegates the header/body split to :func:`split_header_body` (which is
    one-liner-aware), then normalizes the body part. Works on the raw ``.body``
    string any unit/entity carries.
    """
    if not body:
        return ""
    _, rest = split_header_body(body)
    return rest


def name_similarity(a: str | None, b: str | None) -> float:
    """Character-level similarity ratio of two names in ``[0, 1]``.

    Uses :func:`capybase.diff.char_ratio` (LCS-based, C-accelerated). 1.0 = same
    name; →0 = unrelated. The canonical measure for the rename name-guard
    (consolidation #2).
    """
    if not a or not b:
        return 0.0
    return _char_ratio(a, b)


def detect_renames_2way(
    base_units: list,
    side_units: list,
    *,
    fuzzy_body_threshold: float | None = None,
) -> tuple[dict, set]:
    """Detect renames of base entities appearing on ONE side (canonical core).

    A rename is: a base entity whose OLD name is GONE from ``side_units``, but
    whose body content (header-stripped, normalized) reappears under a NEW name
    on the side. This is the false-merge source prior findings: without it, a
    rename is treated as "base keeps old name + side adds new name" → a
    duplicate entity.

    The body-content match is the strong signal (identical content under a new
    name is near-certain evidence of a rename); the name-similarity guard is a
    secondary check so two genuinely-different entities that happen to share a
    body aren't conflated. Because the content match is exact, even a semantic
    rename (``loadData``→``fetchData``, low string similarity) is recognized —
    content-equality is the reliable rename signal.

    ``fuzzy_body_threshold`` (when not ``None``) enables a Jaccard fallback: a
    side entity with no exact body match is paired to the base entity (whose old
    name is gone) with the highest token-Jaccard similarity at or above the
    threshold, provided the body is substantial. This lets a rename that ALSO
    edits the body still pair — used by ``semantic_diff`` (threshold 0.80). The
    resolver and 3-way diff leave it ``None`` (exact-only, conservative).

    Works on ANY objects carrying ``.identity``, ``.kind``, ``.name``, and
    ``.body`` (both :class:`StructuralUnit` and the public ``Entity``), so the
    3-way diff, the structural resolver, and ``semantic_diff`` all share this
    one implementation.

    Returns ``(renames, base_ids_removed)``:
    - ``renames``: maps the side's NEW identity ``(kind, new_name)`` → the base
      identity ``(kind, old_name)`` it replaced.
    - ``base_ids_removed``: the base identities that disappeared because they
      were renamed away (so a merge walk doesn't re-emit the old name).
    """
    # Index base entities by (kind, body-content) for exact-content matching.
    base_by_content: dict = {}
    # For the Jaccard fallback: base entities with substantial content, keyed for
    # token-set similarity. Only built when fuzzy matching is requested.
    base_body_tokens: dict = {}
    for e in base_units:
        content = entity_body_content(e.body)
        key = (e.kind, content)
        # If two base entities share body content, keep the first; renames are
        # ambiguous in that case and we decline to guess.
        base_by_content.setdefault(key, e)
        if (
            fuzzy_body_threshold is not None
            and content
            and len(content) >= _RENAME_SUBSTANTIAL_BODY_MIN
        ):
            base_body_tokens.setdefault(key, frozenset(content.split()))
    side_names_by_kind: dict = {}
    for e in side_units:
        side_names_by_kind.setdefault(e.kind, set()).add(e.name)

    def _confirms_rename(base_match, side_entity, content: str) -> bool:
        """The name/substantial-body guard: is this a real rename, not a
        coincidental body-content collision between two distinct entities?"""
        return bool(content) and (
            name_similarity(base_match.name, side_entity.name) >= RENAME_NAME_SIMILARITY_THRESHOLD
            or len(content) >= _RENAME_SUBSTANTIAL_BODY_MIN
        )

    renames: dict = {}
    removed: set = set()
    consumed_base_ids: set = set()
    for e in side_units:
        content = entity_body_content(e.body)
        # 1) Exact body-content match (the primary, high-precision signal).
        key = (e.kind, content)
        base_match = base_by_content.get(key)
        if base_match is not None and base_match.identity != e.identity:
            # The base entity's old name must be GONE from this side (renamed,
            # not duplicated). If the old name still exists, this is a copy.
            if base_match.name in side_names_by_kind.get(e.kind, set()):
                base_match = None
        else:
            base_match = None
        # 2) Jaccard fallback: a rename that also edited the body. Only when no
        # exact match fired and fuzzy matching is enabled.
        if base_match is None and fuzzy_body_threshold is not None and content:
            tk = frozenset(content.split())
            best: tuple[float, object] | None = None
            for bkey, oks in base_body_tokens.items():
                if bkey[0] != e.kind:
                    continue
                b_unit = base_by_content[bkey]
                if b_unit.identity in consumed_base_ids:
                    continue
                # Old name must be gone from this side (renamed away, not copied).
                if b_unit.name in side_names_by_kind.get(e.kind, set()):
                    continue
                inter = len(tk & oks)
                union = len(tk | oks)
                if union == 0:
                    continue
                j = inter / union
                if (
                    j >= fuzzy_body_threshold
                    and len(content) >= _RENAME_SUBSTANTIAL_BODY_MIN
                    and (best is None or j > best[0])
                ):
                    best = (j, b_unit)
            if best is not None:
                base_match = best[1]
        if base_match is None:
            continue
        if base_match.identity in consumed_base_ids:
            continue
        if not _confirms_rename(base_match, e, content):
            continue
        renames[e.identity] = base_match.identity
        removed.add(base_match.identity)
        consumed_base_ids.add(base_match.identity)
    return renames, removed


# ---------------------------------------------------------------------------
# Diff + rendering — re-exported from structural_diff / structural_context
# ---------------------------------------------------------------------------
# The 3-way diff and prompt rendering now live in their own modules (split in
# consolidation #3). The genuinely-public API is re-exported here for backward
# compatibility (``ap.compute_structural_diff_3way`` etc.). Private internals
# (``_CHANGE_KIND_*``, ``_CHANGE_LABELS``, ``_render_import_surface``) are NOT
# re-exported — callers that need them import from the owning module directly.
# These imports run at the BOTTOM of the module (after all parser symbols above
# are defined); both modules import only already-defined names from here, so
# there is no import-time cycle.
from capybase.adapters.structural_context import render_structural_context  # noqa: E402,F401
from capybase.adapters.structural_diff import (  # noqa: E402,F401
    AlignedUnit,
    StructuralDiff3Way,
    compute_structural_diff_3way,
)

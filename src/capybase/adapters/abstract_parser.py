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

# Single source of truth for extension → language (fix #11). Aliased locally so
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

#: File extension → language: now the single source of truth in
#: ``language.EXTENSION_TO_LANGUAGE`` (fix #11). Re-exported above as ``_EXT_LANG``.

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


#: Brackets whose unclosed state makes a line a continuation of the previous
#: logical line. Only ``()`` counts (G2): signatures wrap inside parens
#: (``def f(\n  a,\n) -> bool:``), but ``{``/``[`` on their own lines are
#: collection literals, not continuation triggers — a malformed dangling ``{``
#: (a merge artifact) must not swallow the next declaration.
_OPEN_BRACKETS = "("
_CLOSE_BRACKETS = ")"


def _line_bracket_delta(raw: str) -> int:
    """Net parenthesis change on a line (string- and comment-aware).

    ``opens - closes`` for ``()`` only, ignoring parens inside string literals
    (blanked first) and inside inline comments (stripped first, G1 — a comment
    like ``# see func(`` would otherwise corrupt the continuation depth). Used by
    Family B to track whether a line continues a multi-line signature (``delta``
    doesn't return to zero until the final closing ``)``).
    """
    # Strip inline comments first (G1): a ``# ...`` / ``// ...`` comment may
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
    """True when a line ends with a backslash line-continuation (G4).

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


def _line_opens_or_continues_triple(raw: str, open_triple: str | None) -> bool:
    """True if, after this line, a triple-quote string is still open.

    Captures both "was already open and stays open" and "opens fresh and
    doesn't close on this line". A line for which this is True is a
    continuation — its content is string literal, not declarations.
    """
    return _update_triple_quote_state(raw, open_triple) is not None


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
    # Continuation tracking (fix #1 + #6): a line is a continuation when unclosed
    # brackets (``def f(`` … ``) -> bool:``) or an open triple-quote string span
    # it across newlines. Such lines are absorbed into the enclosing unit's body
    # (computed by source slice) and never trigger a dedent/close — the
    # ``) -> bool:`` of a wrapped signature sits at indent 0 but belongs to the
    # signature, and a ``class Fake:`` inside a docstring is string content.
    cont_depth = 0  # net unclosed ``()`` carried from prior lines (signature wrap)
    open_triple: str | None = None  # open multi-line triple-quote marker, if any
    pending_backslash = False  # prior line ended with a ``\`` continuation (G4)
    join_buffer = ""  # accumulated text of a ``\``-continued line (G4)

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
        # G4: backslash line-continuation. A prior line ending in ``\`` is joined
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
            # the prior meaningful row so it doesn't absorb this decorator (fix
            # #2 — previously the previous unit's end advanced through the next
            # unit's decorator). Do NOT advance last_line_row past this line.
            if stack and stack[-1].indent >= indent:
                close_units_at_or_below(indent, prev_line_row)
            last_line_row = prev_line_row  # this line is not a body line
            if pending_decorator_indent is None:
                pending_decorator_indent = indent
                pending_decorator_start = i
            continue

        # Bracket continuation (fix #1): a line is a continuation when brackets
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


#: String-prefix runes that introduce a non-plain string literal whose closing
#: quote rule differs from a bare ``"``. Used by :func:`_match_string_prefix` to
#: recover Rust raw strings (``r``/``b``/``rb``/``br`` + optional ``#`` run),
#: where an embedded ``"`` in the content must NOT close the string (fix #9).
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
    # Word-boundary check (G3): the rune run must be preceded by a non-identifier
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
    # A bare ``"`` in the content does NOT close it (fix #9). 0 = ordinary quote.
    str_hash_count = 0
    in_line_comment = False
    in_block_comment = False
    # Statement-start byte for the in-pass field emitter (R1). Tracks the start
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
                # ``str_hash_count`` ``#`` chars (fix #9). An embedded ``"`` in
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
            # the closer matches the opener (fix #9). A Rust raw string
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
            # statement continues to its ``;`` (R1).
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
            # In-main-pass field detection (fix #10 + R1): at top level, a
            # statement ending in ``;`` that matches the field-declaration shape
            # emits a FIELD unit here — no second whole-file re-scan needed.
            #
            # R1: extract the name from the SOURCE SLICE (stmt_start_byte → ``;``),
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
            # A ``;`` ends the statement; the next one starts after it.
            stmt_start_byte = i + 1
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
            # A ``#``-line is a complete statement; the next starts after it (R1).
            stmt_start_byte = i
            row += 1
            # Guard the bounds: when the ``#`` line was the last line (no trailing
            # newline), ``line_end == n`` and ``src[line_end]`` would overflow.
            if line_end < n and src[line_end] == "\n":
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
                # An import line is a complete statement; the next starts after (R1).
                stmt_start_byte = i
                row += 1
                # When the import was the last line (no trailing newline),
                # ``line_end == n`` and ``src[line_end]`` would overflow; guard it.
                if line_end < n and src[line_end] == "\n":
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

    # Field units are now emitted in the main scan at the ``;`` terminator (fix
    # #10), so no second whole-file re-scan is needed here.

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

    - **Receiver method** (fix #4): ``func (recv) Name(...)`` — the token after
      ``func`` is ``(`` (the receiver), not the name. The real name is the
      identifier just before the final parameter-list ``(...)``. A Go receiver
      method is syntactically top-level (not nested in the type body), but
      semantically a method of the receiver's type — so this returns
      ``is_receiver_method=True`` so the caller classifies it as METHOD.

    - **Type declaration** (fix #5): ``type Name struct/interface{...}`` — Go
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
        if paren_open > 0:
            before = joined[:paren_open].rstrip()
            if before:
                name_tok = before.split()[-1]
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name_tok):
                    return name_tok, is_receiver

    # Type declaration: ``type Name struct/interface`` — name is between ``type``
    # and the class keyword (both tokens must be present around the name).
    if last_kw in _A_CLASS_KEYWORDS and last_kw_idx >= 2:
        if toks[last_kw_idx - 2] == "type":
            cand = toks[last_kw_idx - 1]
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cand):
                return cand, False

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
    # R1: an initializer brace — ``const P = Point { ... }``, ``let m = Map { ... }``
    # — is an object/struct literal inside a field declaration, NOT a scope-opening
    # declaration. A buffer containing a field keyword + ``=`` before the ``{`` is
    # a braced initializer; return None so it's treated as depth-only (the field
    # is emitted at the terminating ``;``). Without this, ``Point { ... }`` is
    # misclassified as a keyword-less method/function and absorbs the statement.
    if "=" in toks and any(t in _A_FIELD_KEYWORDS for t in toks):
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


def _find_stmt_start(src: str, term_idx: int, line_index: list[int] | None = None) -> int:
    """Byte offset where the statement ending at ``term_idx`` (a ``;``) begins.

    Like :func:`_find_decl_start` but for ``;``-terminated field declarations:
    walks back past the previous ``;``/``}``/``{`` separator and leading
    whitespace. Used by the in-main-pass field emitter (fix #10) to slice the
    field body from the source at the ``;`` terminator.
    """
    cut = max(
        src.rfind(";", 0, term_idx),
        src.rfind("}", 0, term_idx),
        src.rfind("{", 0, term_idx),
        0,
    )
    if cut > 0:
        cut += 1
    while cut < term_idx and src[cut] in " \t\r\n":
        cut += 1
    return cut


def _buf_has_field_keyword(buf: str) -> bool:
    return any(kw in buf.split() for kw in _A_FIELD_KEYWORDS)


#: Regex matching a top-level field declaration's accumulated token buffer, e.g.
#: ``pub const N : u32 = 5`` or ``let x = 1`` or ``type Foo = Bar``. Captures the
#: declared name. Used by the in-main-pass field emitter (fix #10) — mirrors the
#: line-regex the old ``_emit_a_field_units`` re-scan used, but operates on the
#: whitespace-normalized buffer at the ``;`` terminator.
_A_FIELD_RE = re.compile(
    r"^(?:(?:pub|export|public|private|static|final|readonly|unsafe|inline)\s+)*"
    r"(?:const|static|type|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\b"
)


def _field_name_from_buf(buf: str) -> str | None:
    """The declared field name in a token buffer ending at ``;``, or ``None``.

    Returns the name when the buffer matches a top-level field-declaration shape
    (optional modifiers + a field keyword + an identifier); ``None`` otherwise.
    Used by the in-main-pass field emitter (fix #10) so fields are detected in
    the same scan that tracks brace depth, eliminating the second whole-file
    re-scan (``_emit_a_field_units``) and its divergent string-state tracker.
    """
    m = _A_FIELD_RE.match(buf.strip().rstrip(";").strip())
    if m:
        return m.group(1)
    return None


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
    - Pathological fragmentation (> lines/5 NON-test top-level units on a sizable
      file) → 0.3 (suspicious; likely mis-detection). ``is_test`` units are
      excluded: a large test module with many small ``test_*`` functions is
      normal, not fragmentation (fix #8).
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
    # pathological fragmentation (fix #8 — previously flagged at confidence 0.3).
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


def _has_duplicate_identities(units) -> bool:
    """True when ``units`` contains two entries with the same ``.identity``.

    Duplicate identities (e.g. two ``(method, "f")`` from Java/C++/Python
    overloads or re-definitions) collide silently in the identity-keyed dicts
    of :func:`compute_structural_diff_3way` and the entity_disjoint rule,
    dropping all but one unit — a silent missed-conflict data-loss bug (fix #3).
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


# ---------------------------------------------------------------------------
# 3-way structural diff (Improvement #5 — survey Phase 3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlignedUnit:
    """One unit aligned across the three versions (base/left/right).

    Each field is the :class:`StructuralUnit` from that version, or ``None``
    when absent. ``change_kind`` classifies the alignment for the LLM prompt.
    """
    base: StructuralUnit | None
    left: StructuralUnit | None
    right: StructuralUnit | None
    change_kind: str  # see _CHANGE_KIND_* constants below

    @property
    def name(self) -> str:
        """The best available name for this aligned unit (for display)."""
        for u in (self.left, self.right, self.base):
            if u is not None and u.name:
                return u.name
        return "<anon>"

    @property
    def kind(self) -> str:
        """The best available kind for this aligned unit."""
        for u in (self.left, self.right, self.base):
            if u is not None:
                return u.kind
        return KIND_UNKNOWN


_CHANGE_KIND_UNCHANGED = "unchanged"
_CHANGE_KIND_MODIFIED_LEFT = "modified_left"
_CHANGE_KIND_MODIFIED_RIGHT = "modified_right"
_CHANGE_KIND_MODIFIED_BOTH = "modified_both"
_CHANGE_KIND_ADDED_LEFT = "added_left"
_CHANGE_KIND_ADDED_RIGHT = "added_right"
_CHANGE_KIND_ADDED_BOTH = "added_both"
#: Both sides added a unit of the same name with DIFFERENT bodies — a genuine
#: conflict (each side's addition is incompatible). Distinct from
#: ``added_both`` (identical bodies, an agreed addition). Fix #7.
_CHANGE_KIND_ADDED_BOTH_CONFLICT = "added_both_conflict"
_CHANGE_KIND_DELETED_LEFT = "deleted_left"
_CHANGE_KIND_DELETED_RIGHT = "deleted_right"
_CHANGE_KIND_DELETED_BOTH = "deleted_both"
_CHANGE_KIND_RENAMED = "renamed"


@dataclass(frozen=True)
class StructuralDiff3Way:
    """3-way alignment of a file's structural units across base/left/right.

    ``aligned`` is the list of :class:`AlignedUnit` entries, each carrying the
    base/left/right unit (or None) and a ``change_kind`` classification. This is
    the data structure the structural context annotation (Improvement #6) is
    built from — it tells the model "both sides modified the same function" or
    "left added a unit, right added a different unit" (no structural conflict).
    """
    base_units: list[StructuralUnit]
    left_units: list[StructuralUnit]
    right_units: list[StructuralUnit]
    aligned: list[AlignedUnit]
    family: str
    language: str | None = None

    @property
    def structural_conflicts(self) -> list[AlignedUnit]:
        """Alignments where BOTH sides modified the SAME unit, or both sides
        added the same name with conflicting bodies (potential conflict)."""
        return [
            a for a in self.aligned
            if a.change_kind in (_CHANGE_KIND_MODIFIED_BOTH, _CHANGE_KIND_ADDED_BOTH_CONFLICT)
        ]

    @property
    def required_units(self) -> list[str]:
        """Names of units that must appear in the merged output."""
        out: list[str] = []
        for a in self.aligned:
            if a.change_kind in (
                _CHANGE_KIND_UNCHANGED, _CHANGE_KIND_MODIFIED_LEFT,
                _CHANGE_KIND_MODIFIED_RIGHT, _CHANGE_KIND_MODIFIED_BOTH,
                _CHANGE_KIND_ADDED_LEFT, _CHANGE_KIND_ADDED_RIGHT,
                _CHANGE_KIND_ADDED_BOTH, _CHANGE_KIND_ADDED_BOTH_CONFLICT,
                _CHANGE_KIND_RENAMED,
            ):
                if a.name != "<anon>":
                    out.append(a.name)
        return out


def _normalize_body_ws_only(text: str) -> str:
    """Whitespace-collapse WITHOUT blanking string literals or stripping comments.

    Used by :func:`_bodies_differ` for change detection: a string-value change
    (``return 'hi'`` vs ``return 'bye'``) IS a real body change for merge
    purposes, so we preserve string content. Only whitespace is normalized so
    reformatting doesn't register as a change.
    """
    if not text:
        return ""
    # Strip comment-only lines (they don't carry merge-relevant content), then
    # collapse whitespace — but keep string literals intact.
    kept = [ln for ln in text.split("\n") if _has_code_content(ln)]
    return " ".join(" ".join(kept).split())


def _bodies_differ(a: StructuralUnit, b: StructuralUnit) -> bool:
    """True if two units' bodies differ.

    Uses a whitespace-normalized comparison that preserves string-literal
    content (unlike the body fingerprint, which blanks strings for rename
    matching). This ensures a string-value edit registers as a real change.
    """
    return _normalize_body_ws_only(a.body) != _normalize_body_ws_only(b.body)


def compute_structural_diff_3way(
    base: str, left: str, right: str, language: str | None = None,
) -> StructuralDiff3Way | None:
    """Compute a 3-way structural alignment across base/left/right source texts.

    Parses each version into a :class:`FileIR`, flattens to top-level units, and
    aligns by ``(kind, name)`` with fingerprint fallback for rename detection.
    Each alignment is classified (``modified_both``, ``added_left``, etc.) to
    drive the structural context annotation. Returns ``None`` when parsing fails
    or the language has no family mapping.
    """
    ir_base = parse_file(base, language=language)
    ir_left = parse_file(left, language=language)
    ir_right = parse_file(right, language=language)
    if ir_base is None or ir_left is None or ir_right is None:
        return None
    # A structural annotation built from a minified/garbage parse (confidence
    # 0.0) is worse than no annotation — it would feed the LLM empty/wrong
    # structure as authoritative. Decline when any side is untrustworthy.
    if ir_base.parse_confidence == 0.0 or ir_left.parse_confidence == 0.0 \
            or ir_right.parse_confidence == 0.0:
        return None
    family = ir_base.family
    # Flatten to include nested children (methods inside classes/impls) so the
    # alignment operates at the entity level the LLM merges at — not just
    # top-level containers. Container-scope units (impl/mod) are skipped but
    # their children are walked (mirrors _all_units_flat).
    base_units = _all_units_flat(ir_base)
    left_units = _all_units_flat(ir_left)
    right_units = _all_units_flat(ir_right)

    # Decline on duplicate identities (fix #3): two units sharing an identity
    # (e.g. Java/C++/Python method overloads, re-definitions) would collide
    # silently in the identity-keyed dicts below, dropping all but one — a
    # missed-conflict data-loss bug. Decline so the caller escalates to the LLM.
    if (
        _has_duplicate_identities(base_units)
        or _has_duplicate_identities(left_units)
        or _has_duplicate_identities(right_units)
    ):
        return None

    # Index by identity (kind, name) for O(1) lookup.
    base_by_id = {u.identity: u for u in base_units}
    left_by_id = {u.identity: u for u in left_units}
    right_by_id = {u.identity: u for u in right_units}

    # Collect all identities across the three versions, preserving source order
    # (base first, then left additions, then right additions).
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    for u in base_units:
        if u.identity not in seen:
            seen.add(u.identity)
            ordered.append(u.identity)
    for u in left_units:
        if u.identity not in seen:
            seen.add(u.identity)
            ordered.append(u.identity)
    for u in right_units:
        if u.identity not in seen:
            seen.add(u.identity)
            ordered.append(u.identity)

    aligned: list[AlignedUnit] = []
    for ident in ordered:
        b = base_by_id.get(ident)
        l = left_by_id.get(ident)
        r = right_by_id.get(ident)
        kind = _classify_alignment(b, l, r)
        aligned.append(AlignedUnit(base=b, left=l, right=r, change_kind=kind))

    # Rename detection: left or right units not matched by identity but with a
    # matching body fingerprint to a base unit. This is a secondary pass — the
    # identity-matched alignments are already done; here we pair unmatched units.
    _detect_renames(base_units, left_units, right_units, aligned)

    return StructuralDiff3Way(
        base_units=base_units,
        left_units=left_units,
        right_units=right_units,
        aligned=aligned,
        family=family,
        language=language,
    )


def _classify_alignment(
    base: StructuralUnit | None,
    left: StructuralUnit | None,
    right: StructuralUnit | None,
) -> str:
    """Classify a 3-way alignment into a change-kind label."""
    has_b = base is not None
    has_l = left is not None
    has_r = right is not None

    if has_b and has_l and has_r:
        l_changed = _bodies_differ(base, left)
        r_changed = _bodies_differ(base, right)
        if l_changed and r_changed:
            return _CHANGE_KIND_MODIFIED_BOTH
        if l_changed:
            return _CHANGE_KIND_MODIFIED_LEFT
        if r_changed:
            return _CHANGE_KIND_MODIFIED_RIGHT
        return _CHANGE_KIND_UNCHANGED
    if not has_b and has_l and has_r:
        # Both sides added a unit of this name. Sub-classify: identical bodies
        # = an agreed addition (not a conflict); differing bodies = a genuine
        # conflict (fix #7 — previously both were ``added_both`` and neither was
        # flagged as a structural conflict, silently missing the clash).
        if _bodies_differ(left, right):
            return _CHANGE_KIND_ADDED_BOTH_CONFLICT
        return _CHANGE_KIND_ADDED_BOTH
    if not has_b and has_l and not has_r:
        return _CHANGE_KIND_ADDED_LEFT
    if not has_b and not has_l and has_r:
        return _CHANGE_KIND_ADDED_RIGHT
    if has_b and not has_l and not has_r:
        return _CHANGE_KIND_DELETED_BOTH
    if has_b and not has_l and has_r:
        # Deleted by left, present in right (and base) — right kept it.
        return _CHANGE_KIND_DELETED_LEFT if _bodies_differ(base, right) else _CHANGE_KIND_UNCHANGED
    if has_b and has_l and not has_r:
        # Deleted by right, present in left (and base) — left kept it.
        return _CHANGE_KIND_DELETED_RIGHT if _bodies_differ(base, left) else _CHANGE_KIND_UNCHANGED
    return _CHANGE_KIND_UNCHANGED


def _detect_renames(
    base_units: list[StructuralUnit],
    left_units: list[StructuralUnit],
    right_units: list[StructuralUnit],
    aligned: list[AlignedUnit],
) -> None:
    """Detect renamed units via body-fingerprint matching and append them.

    A unit in left (or right) whose body fingerprint matches a base unit that
    was NOT identity-matched is a rename. This is conservative — it only pairs
    on exact body-fingerprint match (the header-stripped digest), so a rename +
    heavy body edit won't pair (it'll appear as added+removed, which is safe)."""
    # Index base units by body fingerprint for lookup. Skip content-less bodies
    # (fingerprint ``l{count}`` with no ``:digest``): many distinct empty bodies
    # (pass-only methods, docstring-only functions) share ``l0`` and would pair
    # as false renames. Fix #13 — the old guard ``f"l{body.count(chr(10))}"`` was
    # already broken (it compared against the wrong count and never matched).
    base_by_fp: dict[str, StructuralUnit] = {}
    for u in base_units:
        if _fingerprint_has_content(u.fingerprint):
            base_by_fp[u.fingerprint] = u

    # Find base units already matched by identity (so we don't re-pair them).
    matched_base_ids = {a.base.identity for a in aligned if a.base is not None}

    # Check left units for renames of unmatched base units.
    matched_left_ids = {a.left.identity for a in aligned if a.left is not None}
    for lu in left_units:
        if lu.identity in matched_left_ids or not lu.fingerprint:
            continue
        base_match = base_by_fp.get(lu.fingerprint)
        if base_match and base_match.identity not in matched_base_ids:
            # Rename: base_match → lu (in left). Check if right also has it.
            ru = next((u for u in right_units if u.identity == lu.identity), None)
            aligned.append(AlignedUnit(
                base=base_match, left=lu, right=ru,
                change_kind=_CHANGE_KIND_RENAMED,
            ))
            matched_base_ids.add(base_match.identity)
            matched_left_ids.add(lu.identity)

    # Check right units for renames.
    matched_right_ids = {a.right.identity for a in aligned if a.right is not None}
    for ru in right_units:
        if ru.identity in matched_right_ids or not ru.fingerprint:
            continue
        base_match = base_by_fp.get(ru.fingerprint)
        if base_match and base_match.identity not in matched_base_ids:
            lu = next((u for u in left_units if u.identity == ru.identity), None)
            aligned.append(AlignedUnit(
                base=base_match, left=lu, right=ru,
                change_kind=_CHANGE_KIND_RENAMED,
            ))
            matched_base_ids.add(base_match.identity)
            matched_right_ids.add(ru.identity)


# ---------------------------------------------------------------------------
# Structural context annotation (Improvement #6 — survey §"Structural context
# annotation for the LLM prompt")
# ---------------------------------------------------------------------------

#: Human-readable labels for change kinds, for the prompt annotation.
_CHANGE_LABELS = {
    _CHANGE_KIND_UNCHANGED: "unchanged",
    _CHANGE_KIND_MODIFIED_LEFT: "MODIFIED by current/upstream",
    _CHANGE_KIND_MODIFIED_RIGHT: "MODIFIED by replayed",
    _CHANGE_KIND_MODIFIED_BOTH: "MODIFIED BY BOTH SIDES",
    _CHANGE_KIND_ADDED_LEFT: "ADDED by current/upstream",
    _CHANGE_KIND_ADDED_RIGHT: "ADDED by replayed",
    _CHANGE_KIND_ADDED_BOTH: "ADDED BY BOTH SIDES",
    _CHANGE_KIND_ADDED_BOTH_CONFLICT: "ADDED BY BOTH SIDES (different bodies)",
    _CHANGE_KIND_DELETED_LEFT: "deleted by current/upstream",
    _CHANGE_KIND_DELETED_RIGHT: "deleted by replayed",
    _CHANGE_KIND_DELETED_BOTH: "deleted by both",
    _CHANGE_KIND_RENAMED: "RENAMED",
}


def _render_import_surface(diff: StructuralDiff3Way) -> str:
    """Render the import-surface change block, or "" when no import changed.

    Surveys of structured-merge tools find import handling is the single
    highest-value structural operation: an imports-only merger outperformed
    complex structured tools. The correct merge of imports is almost always the
    UNION of both sides' additions (each side's imports are independently
    needed) minus only genuine removes. This block makes that explicit instead
    of leaving the model to infer it from generic per-unit lines.

    Output shape (only the populated lines appear)::

        Import surface: CURRENT adds json; REPLAYED adds sys — union them
        → merged imports must include: os, json, sys

    A remove by one side is called out separately so the model knows the union
    is NOT always the whole set. Returns "" when no import unit changed (the
    common no-import-conflict case) so the annotation is unchanged there.
    """
    cur_adds: list[str] = []
    rep_adds: list[str] = []
    cur_drops: list[str] = []
    rep_drops: list[str] = []
    # The full set of imports that must survive in the merge: every import
    # present in base, left, or right, minus those a side deliberately removed.
    survivors: list[str] = []
    seen: set[str] = set()

    def remember(name: str) -> None:
        if name and name not in seen and name != "<import>":
            seen.add(name)
            survivors.append(name)

    for a in diff.aligned:
        if a.kind != KIND_MODULE_STMT:
            continue
        ck = a.change_kind
        if ck == _CHANGE_KIND_ADDED_LEFT:
            cur_adds.append(a.name)
        elif ck == _CHANGE_KIND_ADDED_RIGHT:
            rep_adds.append(a.name)
        elif ck == _CHANGE_KIND_ADDED_BOTH:
            cur_adds.append(a.name)
            rep_adds.append(a.name)
        elif ck == _CHANGE_KIND_DELETED_LEFT:
            rep_drops.append(a.name)
        elif ck == _CHANGE_KIND_DELETED_RIGHT:
            cur_drops.append(a.name)
        elif ck == _CHANGE_KIND_DELETED_BOTH:
            pass  # removed by both — not a survivor
        # Track survivors (union of all sides' present imports).
        if a.base is not None:
            remember(a.name)
        if a.left is not None:
            remember(a.name)
        if a.right is not None:
            remember(a.name)

    if not cur_adds and not rep_adds and not cur_drops and not rep_drops:
        return ""  # no import-surface change — leave the annotation unchanged

    parts: list[str] = []
    if cur_adds:
        parts.append(f"CURRENT adds {', '.join(cur_adds)}")
    if rep_adds:
        parts.append(f"REPLAYED adds {', '.join(rep_adds)}")
    if cur_drops:
        parts.append(f"CURRENT removes {', '.join(cur_drops)}")
    if rep_drops:
        parts.append(f"REPLAYED removes {', '.join(rep_drops)}")
    head = "Import surface: " + "; ".join(parts)
    # When both sides only ADD imports, the merge rule is unambiguous: union.
    if not cur_drops and not rep_drops and (cur_adds or rep_adds):
        head += " — union them (imports are additive; keep every side's adds)"
    out = [head]
    if survivors:
        out.append(f"→ merged imports must include: {', '.join(survivors)}")
    return "\n".join(out)



def render_structural_context(
    diff: StructuralDiff3Way,
    conflict_span: tuple[int, int] | None = None,
) -> str:
    """Render a structural context annotation block for the LLM prompt.

    Produces a compact summary of the 3-way structural alignment: which units
    exist, what each side changed, whether there are structural conflicts (both
    sides modified the same unit), and which units must appear in the merge.
    Omitted (returns "") when the diff has no useful signal (e.g. single-unit
    files with no changes). ``conflict_span`` optionally annotates which unit
    the conflict markers fall inside.

    This directly addresses the "dropped replayed side" failure mode: the model
    sees unit boundaries and required outputs explicitly before generating.
    """
    lines: list[str] = []
    # Only show units that changed (not unchanged) — the model doesn't need to
    # see a list of everything that stayed the same.
    changed = [a for a in diff.aligned if a.change_kind != _CHANGE_KIND_UNCHANGED]
    if not changed:
        return ""  # no structural signal — nothing changed at the entity level

    lang_label = diff.language or diff.family
    lines.append(f"STRUCTURAL CONTEXT (language-family: {lang_label}/{diff.family}):")

    # Base structure overview (compact) — imports are summarized in their own
    # dedicated block below, so exclude them here to avoid double-listing.
    base_summary = ", ".join(
        f"[{u.kind.upper()}] {u.name} lines {u.span[0]+1}-{u.span[1]+1}"
        for u in diff.base_units
        if u.name and not u.is_container_scope and u.kind != KIND_MODULE_STMT
    )
    if base_summary:
        lines.append(f"Base structure: {base_summary}")

    # Import-surface block (survey: "imports-only tool outperformed complex
    # structured tools — import conflict handling is the single highest-value
    # structural operation"). Imports are the one unit kind where the correct
    # merge is almost always the UNION of both sides' adds minus genuine removes;
    # make that instruction explicit instead of leaving the model to infer it
    # from a generic "[MODULE_STMT] json: ADDED" line. Emits only when at least
    # one import unit changed.
    import_block = _render_import_surface(diff)
    if import_block:
        lines.append(import_block)

    # Per-unit change summary — skip imports (already handled above) so the
    # entity changes read as the code changes they are, not import noise.
    for a in changed:
        if a.kind == KIND_MODULE_STMT:
            continue
        label = _CHANGE_LABELS.get(a.change_kind, a.change_kind)
        lines.append(f"  {a.kind.upper()} {a.name}: {label}")

    # Structural conflicts: units both sides modified.
    conflicts = diff.structural_conflicts
    if conflicts:
        names = ", ".join(c.name for c in conflicts)
        lines.append(
            f"Structural conflicts: {len(conflicts)} unit(s) modified by both sides ({names}) — "
            "synthesize both changes."
        )
    else:
        lines.append(
            "Structural conflicts: NONE (modifications are in separate units) — "
            "preserve each side's changes independently."
        )

    # Required units.
    required = diff.required_units
    if required:
        lines.append(f"Required: preserve these units in the merged output: {', '.join(required)}")

    # Span annotation: which unit does the conflict fall inside?
    if conflict_span is not None:
        # Find the unit in base whose span contains the conflict anchor.
        anchor = conflict_span[0]
        enclosing = None
        for u in diff.base_units:
            if u.span[0] <= anchor <= u.span[1]:
                if enclosing is None or (u.span[0] >= enclosing.span[0] and u.span[1] <= enclosing.span[1]):
                    enclosing = u
        if enclosing and enclosing.name:
            lines.append(f"This conflict is inside: {enclosing.kind.upper()} {enclosing.name}")

    return "\n".join(lines)

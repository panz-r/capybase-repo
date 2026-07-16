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


def unit_body_fingerprint(body: str, *, lang: str | None = None) -> str:
    """A normalized, rename-insensitive digest of a unit's body content.

    Stable under whitespace, comment, and formatting changes, and RENAME-
    SENSITIVE ONLY IN THE HEADER (the first line is stripped), so two functions
    differing only in name produce the SAME fingerprint — the basis for pairing
    a renamed entity to its base original. This mirrors the existing
    ``structural._split_header_body`` contract (header-stripped, whitespace-
    collapsed body) so the existing rename-detection thresholds (Jaccard 0.80)
    stay calibrated.

    ``lang`` selects the comment marker: ``//`` for Family-A brace languages
    (Rust/JS/Go/...), ``#`` for Python/Ruby. Defaults to Python (``None``).
    Comment-stability requires the RIGHT marker — a Rust ``// note`` must be
    stripped, a Python ``//`` (floor division) must not.

    The digest folds in the line count + a SHA1 of the normalized body content
    so it is short to store/compare yet discriminating.
    """
    body = body or ""
    if not body:
        return ""
    lines = body.split("\n")
    # Strip the header via the scope-opener-aware split so a one-liner body
    # (def foo(): return 1\n) correctly yields the inline body content as
    # ``rest``, not an empty string. This keeps the fingerprint consistent with
    # entity_body_content (which uses the same split).
    _, rest = _raw_header_body_split(body)
    norm = normalize_body(rest, lang=lang)
    # Count MEANINGFUL lines (non-blank, non-comment) so adding a comment line
    # doesn't perturb the digest — the fingerprint is stable under comment
    # additions (the AstPreservationValidator relies on this).
    meaningful = len(_filter_code_lines(rest.split("\n"), lang=lang))
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


#: Matches a comment-only line for Family-A (Rust/JS/Go/C ``//``, block ``/* */``).
_A_COMMENT_LINE_RE = re.compile(r"^\s*(?://|/\*|\*/).*$")
#: Matches a comment-only line for Family-B (Python/Ruby ``#``).
_B_COMMENT_LINE_RE = re.compile(r"^\s*#.*$")


def _has_code_content(line: str, *, lang: str | None = None) -> bool:
    """True if a line carries actual code (not blank or a pure comment).

    ``lang`` selects the comment marker: ``//`` for Family-A brace languages,
    ``#`` for Python/Ruby. A Python ``// count`` (floor division) is NOT a
    comment; a Rust ``#[attr]`` is NOT a comment. Defaults to Python.

    NOTE: this is line-local (no block-comment state). Callers that need to
    recognize multi-line ``/* ... */`` interior lines as comments must use
    :func:`_filter_code_lines` instead.
    """
    if not line.strip():
        return False
    if _lang_is_family_a(lang):
        if _A_COMMENT_LINE_RE.match(line):
            return False
    else:
        if _B_COMMENT_LINE_RE.match(line):
            return False
    return True


def _filter_code_lines(lines: list[str], *, lang: str | None = None) -> list[str]:
    """Return the code portions of each line, with multi-line block-comment state.

    Tracks ``in_block`` across lines and scans mid-line for ``/*`` and ``*/``
    so that:
    - code BEFORE a mid-line opener (``let x = 1; /* note``) survives;
    - code AFTER a closer (``*/ let y = 2;``) survives;
    - single-line ``/* ... */`` comments are stripped, leaving any trailing code;
    - interior lines of a multi-line block comment are stripped.

    String literals are blanked first so a ``/*`` inside a string doesn't open
    block-comment state. Used by the body-normalization paths
    (normalize_body, unit_body_fingerprint) so a rename editing a block comment
    stays comment-stable. Python/Ruby (no block comments) fall back to the
    line-local check.
    """
    if not _lang_is_family_a(lang):
        return [ln for ln in lines if _has_code_content(ln, lang=lang)]
    out: list[str] = []
    in_block = False
    for ln in lines:
        # Blank string literals first so /* or */ inside a string doesn't count
        # as a comment boundary. We scan the BLANKED line for boundaries but
        # extract code from the ORIGINAL line so string values are preserved
        # (downstream callers may be string-preserving, e.g. _bodies_differ).
        # Use a LENGTH-PRESERVING replacement so indices in the blanked line
        # align with the original — a fixed-length replacement would shift
        # every index after a variable-length string and corrupt the extraction.
        blanked = _STRING_LIT_RE.sub(lambda m: "_" * len(m.group(0)), ln)
        segments: list[str] = []
        j = 0
        while j < len(blanked):
            if in_block:
                close = blanked.find("*/", j)
                if close < 0:
                    break  # rest of line is comment interior
                j = close + 2
                in_block = False
            else:
                open_ = blanked.find("/*", j)
                if open_ < 0:
                    segments.append(ln[j:])  # code from the ORIGINAL line
                    break
                segments.append(ln[j:open_])  # code before the opener (original)
                j = open_ + 2
                in_block = True
        code = "".join(segments)
        # Keep the line only if its code portion (after stripping block-comment
        # regions) has real code content — a residual pure line-comment
        # (/* block */ // line) must be dropped. This also drops blank residues.
        if code.strip() and _has_code_content(code, lang=lang):
            out.append(code)
    return out


def _strip_inline_comment(line: str, *, lang: str | None = None) -> str:
    r"""Strip an inline comment (string-aware, best-effort).

    Which marker counts as a comment depends on the language:
    - Python/Ruby (Family B, the default): only ``#``. ``//`` is floor division
      (Python) or operator (Ruby) and must NOT be stripped — stripping it would
      corrupt body fingerprints (``x = total // count`` would normalize to
      ``x = total``) and cause false rename pairings.
    - Family-A brace languages (Rust/JS/TS/Go/Java/C/C++/C#/...): only ``//``.
      ``#`` is a preprocessor/attribute marker (Rust ``#[attr]``, C ``#include``),
      not a comment.

    A marker inside a string literal is never treated as a comment start: string
    literals are blanked first, then the marker is searched in the blanked text.
    The blanking is LENGTH-PRESERVING so the marker index in the blanked line
    aligns with the original — a fixed-length replacement would shift the index
    past any variable-length string and slice the original at the wrong spot.
    """
    blanked = _STRING_LIT_RE.sub(lambda m: "_" * len(m.group(0)), line)
    marker = "//" if _lang_is_family_a(lang) else "#"
    idx = blanked.find(marker)
    if idx >= 0:
        line = line[:idx]
    return line


def _lang_is_family_a(lang: str | None) -> bool:
    """True when ``lang`` uses ``//`` line comments (the Family-A brace family).

    ``None`` defaults to Family B (Python/Ruby): every current caller of
    :func:`_strip_inline_comment` is a Family-B path (body normalization,
    bracket-delta, backslash-continuation), so the default must be Python-correct.
    """
    if lang is None:
        return False
    return _LANG_FAMILY.get(lang.strip().lower()) == FAMILY_A


def normalize_body(text: str, *, lang: str | None = None) -> str:
    """Whitespace-collapse + string/comment-literal-neutralize a body region.

    ``" ".join(split())`` collapses all whitespace runs to single spaces (stable
    across indentation/reformatting). String literals are blanked and comments
    stripped first so two bodies differing only in string values or comments
    normalize equal — rename detection and AST preservation shouldn't be thrown
    off by ``return 'hello'`` vs ``return 'hi'`` or an added comment.

    ``lang`` selects the comment marker (``//`` for Family-A brace languages,
    ``#`` for Python/Ruby); defaults to Python. Stripping the WRONG marker
    corrupts the body: a Rust ``// note`` left in (or a Python ``//`` floor-
    division stripped) breaks rename pairing.
    """
    if not text:
        return ""
    # Drop pure-comment lines (with multi-line block-comment state) and strip
    # inline comments, then blank string lits.
    kept = [
        _strip_inline_comment(ln, lang=lang)
        for ln in _filter_code_lines(text.split("\n"), lang=lang)
    ]
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
    r"^\s*(?:import\s+\S|from\s+(?:\.[\w.]*|[A-Za-z_][\w.]*)\s+import\s)"
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


def _collect_imports(units: list[StructuralUnit]) -> list[str]:
    """Names of the MODULE_STMT import units (``<import>`` sentinel skipped).

    Shared by Family A and B: both detect imports as MODULE_STMT units during
    the scan; only the export logic differs.
    """
    return [
        u.name for u in units
        if u.kind == KIND_MODULE_STMT and u.name and u.name != "<import>"
    ]


def _extract_imports_exports_b(source: str, units: list[StructuralUnit]) -> tuple[list[str], list[str]]:
    """Collect the import and export surfaces for a Family-B file.

    Imports: the names from ``import``/``from...import`` lines (already detected
    as MODULE_STMT units — extract their names). Exports: top-level public
    function/class/constant names (not ``_``-prefixed)."""
    imports = _collect_imports(units)
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
    imports = _collect_imports(units)
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
            # Recognize pub, pub(crate), pub(super), pub(in path), export, public.
            is_public = any(
                kw == "pub" or kw.startswith("pub(") or kw in ("export", "public")
                for kw in toks
            )
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
            _finalize_unit(u, u_end, lines, units, stack, language)

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

        # Triple-quote continuation: if a multi-line string is open from a prior
        # line, this line is string content (even if it looks like ``class X:``).
        # This check MUST precede the conflict-marker check below — a ``=======``
        # or ``<<<<<<<`` line inside an open triple-quoted string is string
        # content, not a real conflict marker. Closing it here would truncate the
        # enclosing unit's span/body (a docstring containing a diff example or a
        # markdown table is the plausible trigger).
        if open_triple is not None:
            open_triple = _update_triple_quote_state(raw, open_triple)
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
                    fingerprint=unit_body_fingerprint(body, lang=language),
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
    language: str | None,
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
        fingerprint=unit_body_fingerprint(body, lang=language),
        is_test=u.is_test,
    )
    if stack:
        stack[-1].children.append(su)
    else:
        units.append(su)


def _extract_import_name(line: str) -> str:
    """A best-effort label for an import statement (for identity/diff).

    For absolute imports (``from sys import path``) the label is the module
    (``sys``). For RELATIVE imports (``from . import a``, ``from .m import b``)
    the label includes the imported names (``.a``, ``.m.b``) so two ``from .
    import`` lines with different names don't collide on the same identity
    — which would force a blanket duplicate-identity decline for common
    ``__init__.py`` patterns.
    """
    s = line.strip()
    # Relative imports: include names to distinguish multiple ``from . import``.
    m = re.match(r"from\s+(\.[\w.]*)\s+import\s+(.+)", s)
    if m:
        return f"{m.group(1)}.{m.group(2).strip()}"
    # Absolute ``from X import ...`` → just the module name.
    m = re.match(r"from\s+(\.[\w.]*|[A-Za-z_][\w.]*)", s)
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
_A_CONTAINER_KEYWORDS = ("impl", "mod", "namespace", "module", "extern")
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
    re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:use\s+\S|import\s+\S|export\s+\{[^}]*\}\s+from|require\s*\(|#include\s+)"),
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
    # True for a ``macro_rules!`` body: its contents are expansion *templates*,
    # not real entities, so neither the brace machine nor the associated-item
    # emitter should classify declarations inside it.
    is_macro_body: bool = False


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


#: Hex-digit characters (0-9, a-f, A-F). Used to detect C++14 digit separators
#: in numeric literals (1'000, 0x1F'0000, 0b1010'1010) — a hex-digit ' hex-digit
#: run is a separator, not a char literal.
_HEXDIGITS = frozenset("0123456789abcdefABCDEF")


@dataclass
class _AStrState:
    """The mutable string/comment state for the Family-A char scan.

    Extracted from the ``parse_family_a`` loop so the string/comment state
    machine is testable in isolation. ``in_str`` is one of ``'"'``, ``"'"``,
    ``"`"```, ``"char"``, or ``None``. When ``in_str == '"'`` and
    ``hash_count > 0`` we're inside a Rust raw string (``r#"..."#``).
    """
    in_str: str | None = None
    hash_count: int = 0
    in_line_comment: bool = False
    in_block_comment: bool = False


def _advance_string_comment(
    src: str, i: int, n: int, ch: str, nxt: str, st: _AStrState,
) -> tuple[int, bool]:
    """Process one char's string/comment state transition.

    Returns ``(new_i, handled)``: when ``handled`` is True the char was consumed
    by the string/comment machine (the caller continues the loop at ``new_i``);
    when False the char is ordinary code (the caller processes it).
    """
    # --- newline: advance row handled by caller; here only close line comment ---
    if ch == "\n":
        if st.in_line_comment:
            st.in_line_comment = False
        # The newline-as-token-separator buffer logic stays in the caller.
        return i + 1, True

    if st.in_line_comment:
        return i + 1, True
    if st.in_block_comment:
        if ch == "*" and nxt == "/":
            st.in_block_comment = False
            return i + 2, True
        return i + 1, True
    if st.in_str is not None:
        if ch == "\\":
            # Escapes only apply to NON-raw strings. A raw string (hash
            # count > 0) treats backslash literally — skip the escape skip.
            if st.in_str == '"' and st.hash_count > 0:
                return i + 1, True
            return i + 2, True  # skip escaped char
        if st.in_str == "char" and ch == "'":
            st.in_str = None
            return i + 1, True
        if st.in_str == "'" and ch == "'":
            st.in_str = None
            return i + 1, True
        if st.in_str == '"' and ch == '"':
            # Raw string closer: ``"`` must be followed by exactly
            # ``hash_count`` ``#`` chars. An embedded ``"`` in the content
            # (with no matching ``#`` run) does NOT close it.
            if st.hash_count > 0:
                hc = st.hash_count
                tail = src[i + 1 : i + 1 + hc]
                if len(tail) == hc and tail == "#" * hc:
                    st.in_str = None
                    st.hash_count = 0
                    # Advance past the ``"`` AND the matching ``#``-run. (Capture
                    # hc before clearing — reading st.hash_count after the zero
                    # above would return i+1, leaking the closing #'s into the
                    # token buffer.)
                    return i + 1 + hc, True
                # Not the closer — content quote. Fall through to advance.
                return i + 1, True
            st.in_str = None
            return i + 1, True
        if st.in_str == "`" and ch == "`":
            st.in_str = None
            return i + 1, True
        return i + 1, True

    # Not in string/comment. Detect transitions into one.
    if ch == "/" and nxt == "/":
        st.in_line_comment = True
        return i + 2, True
    if ch == "/" and nxt == "*":
        st.in_block_comment = True
        return i + 2, True
    if ch == '"':
        # Detect raw/byte raw strings (Rust) and other prefixed strings so
        # the closer matches the opener. A Rust raw string ``r#"..."#`` closes
        # on ``"`` + N ``#``; without this, an embedded ``"`` in the content
        # closes early and corrupts brace counting.
        prefix = _match_string_prefix(src, i)
        if prefix is not None:
            # prefix is the hash_count (0 = ordinary, >0 = raw with N #).
            st.in_str = '"'
            st.hash_count = prefix
        else:
            st.in_str = '"'
            st.hash_count = 0
        return i + 1, True
    if ch == "`":
        st.in_str = "`"
        return i + 1, True
    if ch == "'":
        # Rust char-literal vs lifetime (and JS/Python quoted strings which
        # never reach here as a bare ``'`` outside a string). A char literal
        # is ``'X'`` (single content char + closing ``'``); a lifetime is
        # ``'ident`` with NO closing ``'`` (e.g. ``'a``, ``'static``).
        nxt1 = src[i + 1] if i + 1 < n else ""
        nxt2 = src[i + 2] if i + 2 < n else ""
        prev = src[i - 1] if i > 0 else ""
        # Lifetime: ' + identifier-start char, NOT immediately closed by '.
        if (
            (nxt1.isalpha() or nxt1 == "_")
            and nxt2 != "'"
            and not (prev.isalnum() or prev == "_")
        ):
            # Rust lifetime ('a / 'static) — don't enter string state.
            return i + 1, True
        # C++14 digit separator (1'000'000, 0x1F'0000, 0b1010'1010): a hex-digit
        # ' hex-digit run. Don't enter char-literal state — it would swallow the
        # digits until the next ' and corrupt the brace scan, silently dropping
        # subsequent declarations. Covers decimal AND hex letters (A-F).
        # The nxt2 != "'" guard distinguishes a digit separator from a char
        # literal: b'a' / b'0' have prev/nxt1 both hex (b, a) BUT a closing '
        # at nxt2 — without this guard, the byte-char-literal b'X' (idiomatic
        # in Rust match arms) would skip char state and the closing ' would
        # swallow the rest of the file.
        if prev in _HEXDIGITS and nxt1 in _HEXDIGITS and nxt2 != "'":
            return i + 1, True
        st.in_str = "char"
        return i + 1, True
    return i, False


# Keyword sets for in-container associated-item detection (bodyless decls).
# A ``;`` inside a container may terminate an associated const (keyword-led) or
# a bodyless method signature (keyword-led ``fn``/``func`` with ``(...)``). The
# container itself must be one that holds associated items, not a method body.
_ASSOC_FIELD_KEYWORDS = ("const", "static", "let")
_ASSOC_FUNC_KEYWORDS = ("fn", "func", "fun", "def")


def _in_assoc_item_container(stack: list["_OpenAUnit"]) -> bool:
    """True if the innermost open frame is a container that holds associated items.

    Associated-item containers are: class/struct/interface/trait/enum (CLASS
    kind) and the container-only scopes impl/mod/namespace/module. A METHOD or
    FUNCTION frame is NOT an associated-item container — a ``;`` inside a method
    body (e.g. ``return 1;``) must not be read as a bodyless declaration. This
    guards the in-container ``;`` emitter so it only fires in trait/impl/mod/
    class/interface bodies, never inside a method body.
    """
    if not stack:
        return False
    top = stack[-1]
    # A ``macro_rules!`` body contains expansion templates, not real entities —
    # a ``;`` inside it (e.g. a template arm terminator) must not be read as a
    # bodyless declaration.
    if top.is_macro_body:
        return False
    if top.container_only:
        return True
    return top.kind == KIND_CLASS


def _assoc_item_func_name(stmt_text: str) -> str | None:
    """Recover the name from a bodyless function/method signature.

    Handles ``fn foo(...)`` / ``func foo(...)`` / ``fun foo(...)`` terminated by
    ``;`` (Rust trait methods, extern FFI declarations, bodyless specs). Returns
    the name or ``None`` if the text isn't a function-keyword-led signature.
    """
    # Normalize whitespace and strip the trailing ``;``.
    t = stmt_text.strip().rstrip(";").strip()
    toks = t.split()
    if len(toks) < 2:
        return None
    # Find a function keyword; the name is the next identifier token.
    for idx, tok in enumerate(toks):
        kw = re.split(r"[<(:\[{]", tok, maxsplit=1)[0]
        if kw in _ASSOC_FUNC_KEYWORDS and idx + 1 < len(toks):
            cand = toks[idx + 1]
            cand = re.split(r"[<(:\[{]", cand, maxsplit=1)[0]
            cand = cand.strip(" \t")
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cand):
                return cand
    return None


def _emit_assoc_item_at_semicolon(
    src: str,
    stmt_start: int,
    semi_idx: int,
    units: list[StructuralUnit],
    language: str | None,
    stack: list["_OpenAUnit"],
    cur_row_at,
) -> None:
    """Emit a bodyless associated-item declaration terminated at ``semi_idx``.

    Called from the in-container ``;`` handler. Slices ``src[stmt_start:semi+1]``
    and tries, in order: (1) a field declaration (``const``/``static``/``let``) →
    FIELD; (2) a bodyless function signature (``fn foo(...)``) → METHOD. Emits at
    most one unit. The unit is attached as a CHILD of the innermost open
    container frame (``stack[-1].children``), mirroring the brace-machine path,
    so dedup is per-container — two ``const ID`` in sibling impls both survive.
    Dedup also checks open sibling frames (a method with a ``{`` body already
    opened) so a bodyless signature isn't doubled against its braced twin.
    """
    if not stack:
        return
    start = stmt_start
    while start < semi_idx and src[start] == "\n":
        start += 1
    stmt_text = src[start : semi_idx + 1]
    if not stmt_text.strip():
        return
    end_row = cur_row_at(semi_idx)
    start_row = cur_row_at(start)
    # Dedup against THIS container's scope: its already-emitted children plus any
    # sibling declarations still open on the stack (a braced method sharing the
    # name). NOT the global units list — that would drop same-name items across
    # sibling containers (the very thing container scopes exist to allow).
    existing = _container_sibling_names(stack)
    su: StructuralUnit | None = None
    # (1) Field-shaped: ``const``/``static``/``let`` binding.
    fname = _field_name_from_buf(stmt_text)
    if fname is not None and fname not in existing:
        su = StructuralUnit(
            kind=KIND_FIELD,
            name=fname,
            span=(start_row, end_row),
            body=stmt_text,
            fingerprint=unit_body_fingerprint(stmt_text, lang=language),
        )
    else:
        # (2) Bodyless function/method signature: ``fn foo(&self);`` etc.
        mname = _assoc_item_func_name(stmt_text)
        if mname is not None and mname not in existing:
            su = StructuralUnit(
                kind=KIND_METHOD,
                name=mname,
                span=(start_row, end_row),
                body=stmt_text,
                fingerprint=unit_body_fingerprint(stmt_text, lang=language),
            )
    if su is not None:
        stack[-1].children.append(su)


def _container_sibling_names(stack: list["_OpenAUnit"]) -> set[str]:
    """Names already declared in the innermost container's scope.

    Combines (a) the already-emitted children of the innermost open frame and
    (b) the names of any sibling frames still open directly inside the same
    container. Used by the in-container associated-item emitter for per-scope
    dedup (so two ``const ID`` in sibling impls both survive, but a bodyless
    signature isn't doubled against its braced twin).
    """
    names: set[str] = set()
    if not stack:
        return names
    top = stack[-1]
    for child in top.children:
        if child.name:
            names.add(child.name)
    # An open frame is a sibling if it sits directly inside the same container
    # (open_brace_depth == top's). In practice this is the still-open braced
    # method whose body we might be shadowing — reserve its name. Exclude the
    # container frame itself (``top``): its own name is NOT a sibling item, and
    # reserving it would silently drop an associated item named after its
    # container (e.g. ``trait serialize { fn serialize(&self); }``).
    for frame in stack:
        if frame is top:
            continue
        if frame.open_brace_depth == top.open_brace_depth and frame.name:
            names.add(frame.name)
    return names


def _go_method_start_row(src: str, newline_idx: int, body: str, close_row: int) -> int:
    """Approximate the start row of a Go interface method signature.

    For a multi-line signature, the method name is on an earlier line than the
    closing ``)``. Walk backwards from the closing line, counting source lines
    until we find the one containing the method name (the first identifier token
    of ``body``). Returns ``close_row`` if the name isn't found (single-line case
    or malformed input).
    """
    name = body.split("(")[0].split()[-1] if "(" in body else (body.split()[-1] if body.split() else "")
    if not name:
        return close_row
    row = close_row
    pos = newline_idx
    while pos > 0:
        nl = src.rfind("\n", 0, pos)
        if nl < 0:
            line = src[:pos]
            pos = -1
        else:
            line = src[nl + 1 : pos]
            pos = nl
        if re.search(r"\b" + re.escape(name) + r"\b\s*(?:\(|$)", line):
            return row
        row -= 1
        if row < 0:
            break
    return close_row


def _go_buf_is_interface_body(stack: list["_OpenAUnit"], src: str) -> bool:
    """True if the innermost open frame is a Go interface body."""
    if not stack:
        return False
    top = stack[-1]
    if top.kind != KIND_CLASS:
        return False
    return "interface" in src[top.start_byte : top.body_start_byte + 1]


def _emit_go_interface_method_from_buf(
    buf: str,
    stack: list["_OpenAUnit"],
    src: str,
    semi_idx: int,
    cur_row_at,
) -> bool:
    """Emit a Go interface method from a ``;``-terminated buffer (single-line).

    Handles the single-line interface form
    ``type R interface { Read(p []byte) error; Close() error }`` where methods
    are ``;``-separated with no newlines between them. The newline-based emitter
    can't catch these. Returns True if a method was emitted.
    """
    if not stack or not buf.strip():
        return False
    body = buf.strip().rstrip(";").strip()
    if not body:
        return False
    name = _go_method_name_from_buf(body)
    if not name or name in _container_sibling_names(stack):
        return False
    row = cur_row_at(semi_idx)
    stack[-1].children.append(
        StructuralUnit(
            kind=KIND_METHOD,
            name=name,
            span=(row, row),
            body=body,
            fingerprint=unit_body_fingerprint(body, lang="go"),
        )
    )
    return True


def _go_method_name_from_buf(buf: str) -> str | None:
    """Recover a Go method/function name from a buffered signature line.

    Go method specs may have two paren groups — params and named results:
    ``Read(p []byte) (n int, err error)`` — so the name is the first identifier
    before the FIRST ``(`` (the parameter list), not before the last. Generic
    methods put a type-parameter list before the params: ``Map[T any](s []T)`` —
    the name is before the ``[``. Returns the name or ``None``.
    """
    t = buf.strip()
    # Find the first top-level ``(`` (the param list opener).
    paren = t.find("(")
    if paren <= 0:
        return None
    name_part = t[:paren].strip()
    if not name_part:
        return None
    # Strip a trailing generic type-parameter list ``[T any]``: the name is the
    # identifier before it (``Map`` in ``Map[T any]``).
    if name_part.endswith("]"):
        br = name_part.rfind("[")
        if br > 0:
            name_part = name_part[:br].strip()
    # The name is the last whitespace-separated token of the pre-param run (a
    # return type may precede it: ``error DoThing(...)`` — name is DoThing).
    cand = name_part.split()[-1] if name_part.split() else ""
    cand = cand.strip(" \t")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cand):
        # Reject control-flow keywords masquerading as a name.
        if cand in _A_CONTROL_FLOW_KEYWORDS:
            return None
        return cand
    return None


def _maybe_emit_go_interface_method(
    buf: str,
    language: str | None,
    stack: list["_OpenAUnit"],
    brace_depth: int,
    units: list[StructuralUnit],
    src: str,
    newline_idx: int,
    cur_row_at,
) -> bool:
    """Emit a Go interface method spec at a newline. Returns True if emitted.

    Go interface methods are newline-terminated signatures with no body brace and
    no ``;``: ``Read(p []byte) (n int, err error)``. Only fires when the scanner
    is DIRECTLY inside a Go interface body (the innermost open frame is a CLASS
    whose declaration keyword was ``interface``, and the brace depth is exactly
    the interface's open depth). ``newline_idx`` is the byte index of the newline
    that ended the method's line; the body is sliced back to the line's start.

    Multi-line signatures (params spanning lines) are handled by declining when
    the paren depth in ``buf`` is unbalanced — the method is incomplete and will
    be emitted at its closing line. The emitted unit is attached to the
    interface frame's children (per-scope dedup via _container_sibling_names).
    """
    if language != "go" or not stack or not buf.strip():
        return False
    top = stack[-1]
    # Directly inside the interface body: brace_depth == top.open_brace_depth.
    if brace_depth != top.open_brace_depth or top.kind != KIND_CLASS:
        return False
    # Confirm the container is an interface (not a struct) by checking the
    # source at the frame's start for the ``interface`` keyword.
    if "interface" not in src[top.start_byte : top.body_start_byte + 1]:
        return False
    # Decline on an unbalanced param list: the signature spans multiple lines
    # (``Read(p []byte,\n  q int) error``) and isn't complete yet. Emitting now
    # would produce a truncated method now and a phantom from the continuation.
    if not _go_buf_params_balanced(buf):
        return False
    name = _go_method_name_from_buf(buf)
    if not name:
        return False
    if name in _container_sibling_names(stack):
        return False
    # The body is the full signature from the token buffer. ``buf`` accumulates
    # the complete signature across continuation lines (joined with spaces) and
    # already excludes line-comment content (consumed by the state machine), so
    # it captures multi-line signatures correctly and is comment-free. Slicing
    # the raw source line instead would truncate multi-line signatures to just
    # the closing line AND retain trailing comments.
    body = buf.strip()
    if not body:
        return False
    # Span: the closing line of the signature (where parens balanced). The start
    # row of a multi-line signature is approximated by walking back the number
    # of newlines the buffer spans.
    row = cur_row_at(src.rfind("\n", 0, newline_idx) + 1) if newline_idx > 0 else 0
    start_row = _go_method_start_row(src, newline_idx, body, row)
    top.children.append(
        StructuralUnit(
            kind=KIND_METHOD,
            name=name,
            span=(start_row, row),
            body=body,
            fingerprint=unit_body_fingerprint(body, lang=language),
        )
    )
    return True


def _go_buf_params_balanced(buf: str) -> bool:
    """True if the paren depth in ``buf`` is net-zero (a complete signature).

    A Go interface method whose params span multiple lines has an unbalanced
    ``(`` at the first line's newline (the closing ``)`` is on a later line).
    Declining here defers emission to the line where the signature completes.
    """
    depth = 0
    for ch in buf:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
    return depth == 0


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
    bracket_depth = 0  # track [] so a ``;`` inside [T; N] isn't a statement terminator
    # String/comment state machine (extracted to _AStrState + _advance_string_comment).
    strst = _AStrState()
    # Statement-start byte for the in-pass field emitter. Tracks the start
    # of the current top-level statement: advances past ``;`` and past a ``}``
    # that closes back to depth 0, but NOT past ``{``/``}`` inside a statement
    # (so a braced initializer ``const P = Point { ... };`` keeps its start at
    # ``const``). Unlike the token buffer (reset at every brace), this survives
    # internal braces so the field name can be recovered at the terminating ``;``.
    stmt_start_byte = 0
    # In-container statement-start byte: like stmt_start_byte but scoped to the
    # body of an open container (impl/trait/mod/extern/interface). Tracks where
    # the current associated-item statement began so bodyless declarations
    # (Rust ``const N: u32;``, ``fn foo(&self);``, extern ``fn bar();``) can be
    # recovered at their ``;`` terminator. Reset at ``;`` and at the ``{``/``}``
    # that opens/closes a container body — NOT at internal braces inside a single
    # statement (so a braced initializer inside a container keeps its start).
    inner_stmt_start = 0
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

        # --- newline: advance row + line_start; the string/comment machine
        # (called below) closes any line comment. The token-buffer boundary
        # logic stays here because it depends on row/line_start. ---
        if ch == "\n":
            was_line_comment = strst.in_line_comment
            new_i, _ = _advance_string_comment(src, i, n, ch, nxt, strst)
            row += 1
            line_start = new_i
            if was_line_comment:
                # A line comment ends a statement run (token buffer boundary).
                # Exception: if the buffer holds a *pending signature* — it
                # contains a balanced ``(...)`` parameter list with no ``;`` yet
                # seen — the brace that opens the body is on a LATER line
                # (Allman style with a trailing inline comment:
                # ``int getCount() const // c\n { ... }``). Wiping here would
                # drop the whole unit when that ``{`` is classified with an
                # empty buffer. Preserve the signature across the newline
                # instead; the next ``{``/``;``/``}`` will still reset it.
                if _buf_has_pending_signature(buf):
                    if not buf.endswith(" "):
                        buf += " "
                else:
                    buf = ""
            else:
                # A newline acts as a token separator so declarations on
                # consecutive lines don't concatenate in the buffer (e.g.
                # ``package main\nfunc main()`` must not become ``packagemain
                # func main()``). Mirrors the ``isspace()`` accumulation below.
                if buf and not buf.endswith(" "):
                    buf += " "
            # Go interface method specs: Go interfaces declare methods as
            # signatures with NO body and NO ``;`` terminator (newline-ended):
            #   type R interface { Read(p []byte) error\n Close() error }
            # The ``;`` handler can't catch them (no ``;``) and the brace
            # machine can't either (no ``{``). At a newline directly inside a
            # Go interface body, classify the buffer as a bodyless method.
            # ``i`` is the newline byte — the method's line ends at it.
            if _maybe_emit_go_interface_method(
                buf, language, stack, brace_depth, units,
                src, i, cur_row_at,
            ):
                buf = ""  # consumed — start the next method's signature fresh
            elif (
                language == "go"
                and stack
                and brace_depth == stack[-1].open_brace_depth
                and _go_buf_is_interface_body(stack, src)
                and _go_buf_params_balanced(buf)
            ):
                # Inside a Go interface body, the line had balanced parens but
                # wasn't a method signature (e.g. an embedded interface
                # ``io.Reader``, a blank line). Reset buf so its tokens don't
                # bleed into the NEXT method's body (which is sourced from buf).
                # A line with UNBALANCED parens is a partial multi-line method —
                # keep buf so the continuation completes the signature.
                buf = ""
            i = new_i
            continue

        # --- string/comment state machine (takes precedence over brace logic) ---
        new_i, handled = _advance_string_comment(src, i, n, ch, nxt, strst)
        if handled:
            i = new_i
            continue

        # Conflict markers (only at line start, brace depth 0). A marker inside
        # a function body (depth > 0) is content, not a real conflict boundary —
        # firing at depth > 0 would close the enclosing unit mid-body, truncating
        # its span/body to a fragment.
        if brace_depth == 0 and (i == line_start or (i > line_start and src[line_start:i].strip() == "")):
            line_head = src[line_start : line_start + 7]
            if (
                line_head.startswith("<<<<<<<")
                or line_head.startswith("=======")
                or line_head.startswith(">>>>>>>")
            ):
                # Close all open units.
                while stack:
                    _close_a_unit(stack.pop(), brace_depth + 1, src, units, stack, language, _line_index)
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
            # Inside a ``macro_rules!`` body, braces are template syntax, not
            # declarations — skip classification so ``fn``/``const`` fragments in
            # the macro body don't leak as phantom entities.
            in_macro = any(f.is_macro_body for f in stack)
            classified = None if in_macro else _classify_a_brace(buf, stack, language, brace_depth)
            if classified is not None:
                # Declaration-level brace. Push a new unit.
                kind, name, container_only = classified
                decl_start = _find_decl_start(src, i, line_start)
                attr_row = pending_attr_row
                pending_attr_row = None
                # Detect a macro_rules! body: mark it so its template contents are
                # not classified as real entities.
                is_macro = _buf_is_macro_rules(buf, language)
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
                    is_macro_body=is_macro,
                )
                stack.append(unit)
            # Either way, depth increases.
            brace_depth += 1
            buf = ""
            # A declaration brace opens a new body — the next associated-item
            # statement (if any) starts right after this brace. An object/struct-
            # literal brace (``classified is None``) does NOT reset the tracker:
            # it's internal to the current statement (``const O: P = P { ... };``),
            # and resetting would lose the declaration start before the ``;``.
            # Mirrors stmt_start_byte's "survives internal braces" discipline.
            if classified is not None:
                inner_stmt_start = i + 1
            i += 1
            continue

        if ch == "}":
            brace_depth -= 1
            if brace_depth < 0:
                brace_depth = 0  # unbalanced (malformed) — clamp, never crash.
            # Before closing a Go interface frame, emit any pending method in the
            # buffer — a single-line interface's LAST method is terminated by ``}``
            # not ``;`` or ``\n``: ``interface { Read(); Close() }``.
            if (
                language == "go"
                and stack
                and brace_depth < stack[-1].open_brace_depth
                and buf.strip()
                and _go_buf_is_interface_body(stack, src)
            ):
                _emit_go_interface_method_from_buf(buf, stack, src, i, cur_row_at)
            # Close any unit whose scope this brace ends. Pass the closing brace
            # index so the body slice can be computed precisely.
            closed_unit = False
            while stack and brace_depth < stack[-1].open_brace_depth:
                _close_a_unit(stack.pop(), i, src, units, stack, language, _line_index)
                closed_unit = True
            # When a ``}`` closes a top-level DECLARATION scope back to depth 0
            # (a unit was popped), the next statement starts fresh. But a ``}``
            # closing an object-literal inside a still-open statement (e.g.
            # ``const P = Point { ... };``) must NOT advance the tracker — the
            # statement continues to its ``;``.
            if brace_depth == 0 and closed_unit:
                stmt_start_byte = i + 1
            # A ``}`` that closed a container body: the next associated-item
            # statement in the enclosing container (if any) starts after this
            # brace. Only advance when a unit was actually popped (a real scope
            # close), not for an object-literal brace inside a statement.
            if closed_unit and brace_depth > 0:
                inner_stmt_start = i + 1
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
        if ch == "[":
            bracket_depth += 1
            buf += ch
            i += 1
            continue
        if ch == "]":
            bracket_depth = max(0, bracket_depth - 1)
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
            if brace_depth == 0 and bracket_depth == 0 and not stack and language not in (None, "c", "h"):
                stmt_start = stmt_start_byte
                # Skip a leading newline so the body doesn't start with "\n"
                # (which would make _raw_header_body_split treat the declaration
                # as a multi-line body and fail to strip the header → name leaks
                # into the fingerprint → rename detection breaks for 2nd+ fields).
                while stmt_start < i and src[stmt_start] == "\n":
                    stmt_start += 1
                stmt_text = src[stmt_start : i + 1]
                fname = _field_name_from_buf(stmt_text)
                if fname is not None:
                    end_row = cur_row_at(i)
                    start_row_f = cur_row_at(stmt_start)
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
                                fingerprint=unit_body_fingerprint(body, lang=language),
                            )
                        )
            # A ``;`` ends the statement; the next one starts after it. Only
            # advance at TOP LEVEL: a ``;`` inside braces (e.g. the
            # ``return 1;`` inside ``const f = function() { ... };``) must NOT
            # move the tracker, or the outer ``;`` would slice from mid-statement
            # and the field name recovery would fail. The tracker is only
            # meaningful for top-level statements.
            if brace_depth == 0 and bracket_depth == 0:
                stmt_start_byte = i + 1
            # In-container associated-item detection: a ``;`` inside an open
            # container (impl/trait/mod/extern/interface) may terminate a
            # bodyless declaration — a Rust associated const (``const N: u32;``),
            # a Rust trait method signature (``fn foo(&self);``), or an extern
            # FFI declaration (``fn bar();``). These have no ``{`` body, so the
            # brace machine never classifies them; recover them from the source
            # slice here. Mirrors the top-level path: slice from the statement
            # start (survives internal braces) to the ``;``.
            elif (
                brace_depth > 0
                and bracket_depth == 0
                and stack
                and _in_assoc_item_container(stack)
                and language not in (None, "c", "h")
            ):
                # Go interface methods are keywordless and ``;``-separated on a
                # single line: ``type R interface { Read(); Close() }``. The
                # generic associated-item emitter (which looks for fn/const
                # keywords) can't recover them; try the Go method-name extractor
                # first, then fall through to the generic path.
                if language == "go" and _go_buf_is_interface_body(stack, src):
                    emitted = _emit_go_interface_method_from_buf(
                        buf, stack, src, i, cur_row_at
                    )
                    if not emitted:
                        _emit_assoc_item_at_semicolon(
                            src, inner_stmt_start, i, units, language, stack, cur_row_at
                        )
                else:
                    _emit_assoc_item_at_semicolon(
                        src, inner_stmt_start, i, units, language, stack, cur_row_at
                    )
                inner_stmt_start = i + 1
            # Only reset buf and bracket_depth at a REAL statement boundary
            # (brace_depth == 0 AND bracket_depth == 0). A ``;`` inside [T; N]
            # array types (bracket_depth > 0) is a dimension separator, not a
            # statement terminator — resetting buf there would wipe the function
            # signature and drop the function. Also reset bracket_depth for
            # malformed-input recovery, but only AFTER the buf gate so the gate
            # sees the pre-reset value.
            was_at_bracket_depth_zero = bracket_depth == 0
            if brace_depth == 0:
                bracket_depth = 0
            if was_at_bracket_depth_zero:
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
        _close_a_unit(stack.pop(), n, src, units, stack, language, _line_index)

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
    language: str | None,
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
        fingerprint=unit_body_fingerprint(body, lang=language),
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


def _last_balanced_paren_open(joined: str) -> int:
    """Index of the ``(`` opening the last balanced ``(...)`` group, or -1.

    Walks backwards from the end: the final param-list ``(...)`` of a signature
    is the last group whose parens balance. Returns -1 when there is no balanced
    paren group. Shared by the Go receiver/type and the keywordless-method name
    recovery (both need "the param list at the end of the declaration").
    """
    depth_p = 0
    for idx in range(len(joined) - 1, -1, -1):
        c = joined[idx]
        if c == ")":
            depth_p += 1
        elif c == "(":
            if depth_p > 0:
                depth_p -= 1
                if depth_p == 0:
                    return idx
    return -1


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
        paren_open = _last_balanced_paren_open(joined)
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


def _is_initializer_literal(toks: list[str]) -> bool:
    """True when the buffer is a braced object/struct-literal initializer.

    ``const P = Point { ... }``, ``let m = Map { ... }``, ``const cfg = { ... }``
    — a field keyword + ``=`` whose token after ``=`` is an object/struct-literal
    start (a bare ``{`` or a capitalized type name). Such a ``{`` is depth-only
    (NOT a scope-opening declaration); the field is emitted later at the ``;``.

    Function/class/arrow expressions assigned to a binding
    (``const f = function() {`` / ``= class {`` / ``= () => {``) are NOT
    initializers — the ``{`` IS a real declaration brace for the inner
    function/class, so those fall through to normal classification.
    """
    if "=" not in toks or not any(t in _A_FIELD_KEYWORDS for t in toks):
        return False
    eq_idx = toks.index("=")
    after_eq = toks[eq_idx + 1 :] if eq_idx + 1 < len(toks) else []
    first_after = after_eq[0] if after_eq else ""
    # ``= function`` / ``= class`` / ``= ( ... ) =>`` → a function/class/arrow
    # expression; let it classify. Anything else is an object/struct literal.
    return not (
        first_after.startswith("function")
        or first_after.startswith("class")
        or first_after.startswith("(")
    )


def _recover_arrow_binding(toks: list[str], stack: list[_OpenAUnit]) -> tuple[str, str] | None:
    """Recover the binding name for a JS/TS arrow function with a block body.

    ``const f = (...) => {`` / ``const f = () => {`` — the buffer ends in ``=>``
    with a binding name before ``=``, no declaration keyword present. Returns
    ``(kind, name)`` where kind is METHOD when nested in a class/container,
    else FUNCTION; or ``None`` when the shape isn't an arrow binding.

    ``=>`` is treated as the pseudo-keyword position; the name is recovered via
    the field-keyword-before-name rule (so a reassignment ``x = (...) => {``
    without a field keyword is rejected).
    """
    if not toks or toks[-1] != "=>" or "=" not in toks:
        return None
    eq_idx = toks.index("=")
    if eq_idx < 1:
        return None
    cand = toks[eq_idx - 1]
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cand):
        return None
    # Require a field keyword (or export modifier) before the name so it's a
    # binding declaration, not a reassignment.
    for j in range(eq_idx - 2, -1, -1):
        if toks[j] in _A_FIELD_KEYWORDS or toks[j] in ("export", "pub", "public"):
            kind = KIND_METHOD if any(
                (u.kind == KIND_CLASS or u.container_only) for u in reversed(stack)
            ) else KIND_FUNCTION
            return (kind, cand)
        if toks[j] in (";", "}", "{"):
            break
    return None


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
    # Object/struct-literal initializer: the ``{`` is depth-only, not a scope.
    if _is_initializer_literal(toks):
        return None
    # JS/TS arrow function with a block body — ``const f = (...) => {``.
    # No declaration keyword is present, so recover the binding name explicitly.
    arrow = _recover_arrow_binding(toks, stack)
    if arrow is not None:
        kind, name = arrow
        return (kind, name, False)
    # Rust ``macro_rules! Name { ... }`` — a macro definition. The ``!`` glues to
    # ``macro_rules`` (no declaration keyword set matches it), so handle it here:
    # the name is the identifier token after ``macro_rules!``. An optional leading
    # visibility prefix (``pub``/``pub(crate)``) is skipped. Tracked as a CLASS
    # (it opens a braced body and is a named top-level entity). The body is marked
    # is_macro_body by the caller so its template fragments don't leak as entities.
    if language == "rust":
        mtoks = toks
        # Strip a leading visibility prefix (pub / pub(crate)) if present.
        if mtoks and (mtoks[0] == "pub" or mtoks[0].startswith("pub(")):
            mtoks = mtoks[1:]
        if mtoks and mtoks[0].startswith("macro_rules!"):
            if len(mtoks) >= 2:
                cand = mtoks[1]
                cand = re.split(r"[<(:\[{]", cand, maxsplit=1)[0].strip(" \t")
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cand):
                    return (KIND_CLASS, cand, False)
            return (KIND_CLASS, None, False)
    # Find the LAST declaration keyword in the buffer. A keyword may be glued
    # to the following ``(`` / ``<`` / ``:`` in the whitespace-normalized
    # tokens (``function()`` / ``fn<T>``), so match by prefix: a token whose
    # head (before the first ``(``/``<``/``:``) is a declaration keyword counts.
    last_kw_idx = -1
    last_kw = ""
    # Find the position of ``->`` (return-type arrow) if present. Anything
    # AFTER ``->`` is a type expression (impl Trait, dyn Error, Box<T>), NOT a
    # declaration context. Without this guard, ``fn foo() -> impl Iterator<...>``
    # misclassifies the brace as an ``impl`` container scope.
    arrow_idx = -1
    for ai, at in enumerate(toks):
        if at == "->" or at.startswith("->"):
            arrow_idx = ai
            break
    scan_end = arrow_idx if arrow_idx >= 0 else len(toks)
    # A declaration keyword must come BEFORE the parameter list. Any keyword
    # token that appears after the LAST ``)`` is a trailing modifier (Dart
    # ``async``, C++ ``const``/``noexcept``), not an introducer — without this
    # bound, Dart ``void main() async`` found ``async`` as the rightmost
    # func-keyword, treated it as the leading introducer, and lost the name.
    last_paren_close = -1
    for pci in range(scan_end - 1, -1, -1):
        if toks[pci] == ")" or toks[pci].endswith(")"):
            last_paren_close = pci
            break
    if last_paren_close >= 0 and scan_end > last_paren_close + 1:
        scan_end = last_paren_close + 1
    for idx in range(scan_end - 1, -1, -1):
        t = toks[idx]
        head = re.split(r"[<(:\[{]", t, maxsplit=1)[0]
        if (
            t in _A_CLASS_KEYWORDS
            or t in _A_FUNC_KEYWORDS
            or t in _A_CONTAINER_KEYWORDS
            or (head != t and head in _A_FUNC_KEYWORDS)
            or (head != t and head in _A_CLASS_KEYWORDS)
            or (head != t and head in _A_CONTAINER_KEYWORDS)
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
        # ``extern`` is special: it's a container ONLY in the block form
        # ``extern "C" { ... }`` (where the ABI string was consumed, leaving
        # ``extern`` as the sole token). The C++ function form
        # ``extern int foo() { }`` has a full signature after ``extern`` — it's
        # a function, not a container. If tokens follow ``extern`` that include
        # a ``(`` (a param list), fall through to normal classification instead
        # of swallowing the function into a phantom container scope.
        if last_kw == "extern":
            after_extern = toks[last_kw_idx + 1 :]
            if any("(" in t for t in after_extern):
                # C++ extern *function* form: ``extern int foo() { }`` — treat
                # ``extern`` as a visibility prefix and classify the remaining
                # signature via the keywordless-method path (which recovers
                # the real name ``foo`` from the param list).
                return _classify_keywordless_method(after_extern, stack, brace_depth)
            else:
                return (KIND_CLASS, name, True)
        else:
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


# Trailing tokens that may legitimately appear after a method's parameter list
# and before the opening ``{``. Stripped by :func:`_strip_trailing_signature_tokens`
# so the ``endswith(")")`` shape guard in :func:`_classify_keywordless_method`
# recognizes the signature. C++ qualifiers (``const``/``noexcept``/``override``/
# ``final``, stackable), C++ trailing-return arrow ``-> RetType``, and Dart async
# markers (``async``/``async*``/``sync*``). ``noexcept(expr)`` and ``final`` are
# included; ``override``/``final``/``consteval``/``constexpr`` are not declaration
# keywords (they're specifiers), so they aren't in _A_FUNC_KEYWORDS.
_A_TRAILING_METHOD_QUALIFIERS = frozenset({
    # C++ cv/ref/exception/override specifiers (stackable: ``const noexcept``).
    "const", "volatile", "noexcept", "override", "final", "constexpr",
    "consteval", "mutable", "throw",
    # Dart coroutine markers (``async``/``async*``/``sync*``/``yield``).
    "async", "async*", "sync", "sync*", "yield",
    # Java ``throws X`` is handled separately (multi-token); kept out here.
})


def _strip_trailing_signature_tokens(joined: str) -> str:
    """Remove tokens that may trail a method's ``(...)`` parameter list.

    Handles two shapes that would otherwise fail the ``endswith(")")`` shape
    guard in :func:`_classify_keywordless_method`:

    1. **C++ trailing return type** ``-> RetType`` — strip everything from the
       first top-level ``->`` onward (the return type can be complex:
       ``-> std::vector<int> const &``). Only a ``->`` that is a standalone
       token (whitespace around it) counts, so ``a->b`` member access earlier
       in the signature is left intact.
    2. **C++ / Dart trailing qualifiers** — ``const``/``noexcept``/``override``/
       ``final``/``async``/... stacked after the params. Also handles Java's
       ``throws P, Q`` clause and Dart's ``async*`` token.

    The result always ends at the closing ``)`` of the parameter list when the
    buffer represents one of these shapes; otherwise it is returned unchanged.
    """
    tokens = joined.split()
    # (0) C++ member-init list: ``Foo() : base(), member(42) { }`` — the init
    # list trails the param list after a ``:``. Cut everything from the first
    # ``:`` that follows a ``)`` (the param-list close). Without this, the last
    # ``(...)`` in the init list (e.g. ``base()``) is misread as the param list
    # and the method is named after the init member. A ``:`` before any ``)``
    # is a base-class clause (``class C : B``) or a type annotation, not an init
    # list — leave it intact.
    paren_seen = False
    for idx, tok in enumerate(tokens):
        if ")" in tok:
            paren_seen = True
        if paren_seen and tok == ":":
            tokens = tokens[:idx]
            break
    # (1) Trailing return type: cut at the first standalone ``->`` token (C++)
    # or ``=>`` (C# expression-bodied members: ``string Get() => expr;``).
    for idx, tok in enumerate(tokens):
        if tok.startswith("->") or tok == "=>":
            tokens = tokens[:idx]
            break
    # (2) Trailing qualifier keywords. Drop them from the tail while they keep
    # appearing (stackable: ``const noexcept override``). Handles BOTH the glued
    # form (``noexcept(false)``) and the spaced form (``noexcept (false)`` — valid
    # C++ with whitespace before the argument list), which tokenize as separate
    # tokens ``noexcept ( false )``.
    # Special-case Java ``throws A, B`` and C++20 ``requires (expr)`` /
    # ``requires Concept``: once seen, drop the keyword AND everything after
    # (the clause spans to the ``{`` and can contain nested parens/concepts).
    out: list[str] = []
    clipping = False
    for tok in tokens:
        if clipping:
            continue
        if tok in ("throws", "requires"):
            clipping = True
            continue
        out.append(tok)
    out = _strip_trailing_qualifier_run(out)
    return " ".join(out)


def _strip_trailing_qualifier_run(toks: list[str]) -> list[str]:
    """Strip a run of trailing method qualifiers from ``toks`` (in place safe).

    Handles bare keywords (``const``/``noexcept``/...), the glued parenthesized
    form (``noexcept(false)``), AND the spaced form (``noexcept ( false )``).
    Loops so stacked qualifiers (``const noexcept (false) override``) are all
    stripped. Stops at the first non-qualifier tail token.
    """
    while toks:
        last = toks[-1]
        # Glued or bare qualifier: single-token pop.
        if _is_trailing_qualifier_token(last):
            toks.pop()
            continue
        # Spaced parenthesized form: ``... noexcept ( ... )`` — the tail is ``)``.
        # Scan back to the matching ``(`` and check the token before it is a
        # qualifier keyword. If so, pop the whole ``keyword ( ... )`` group.
        if last == ")" or last.endswith(")"):
            open_idx = _match_paren_open(toks, len(toks) - 1)
            if open_idx > 0 and toks[open_idx - 1] in _A_TRAILING_METHOD_QUALIFIERS:
                del toks[open_idx - 1 :]
                continue
        break
    return toks


def _match_paren_open(toks: list[str], close_idx: int) -> int:
    """Index in ``toks`` of the ``(`` matching the ``)`` at ``close_idx``, or -1.

    Walks backwards matching paren depth across tokens (a token may be ``(``,
    ``)``, or contain them glued like ``foo()``). Used by the spaced-qualifier
    stripper to find the opener of a trailing ``keyword ( ... )`` group.
    """
    depth = 0
    for idx in range(close_idx, -1, -1):
        tok = toks[idx]
        # Count net parens in this token.
        depth += tok.count(")")
        depth -= tok.count("(")
        if depth == 0:
            return idx
    return -1


def _cpp_operator_name(name_part: str) -> str | None:
    """Build a distinguishable C++ operator-overload method name.

    Returns a name like ``operator+``, ``operator<<``, ``operator int``,
    ``operator void*``, or ``None`` if ``name_part`` doesn't contain an operator
    declaration. Handles two shapes:

    - **Symbol operators**: ``operator+``, ``operator<<``, ``operator()``, where
      ``operator`` is glued to the operator symbols in the last token.
    - **Conversion operators**: ``operator int``, ``operator void*``, where
      ``operator`` is followed by a target TYPE (possibly multiple tokens).

    The returned name is distinguishable across different operators so they don't
    collide under the identity ``(kind, name)`` (which would force a blanket
    structural-diff decline for operator-rich classes).
    """
    toks = name_part.split()
    if not toks:
        return None
    # Find the ``operator`` token (may be glued: ``operator+``, or standalone).
    op_idx = -1
    for idx, tok in enumerate(toks):
        if tok == "operator" or tok.startswith("operator"):
            # Must be the operator keyword, not a method named ``operatorX``.
            # ``operator`` followed by non-identifier chars, or ``operator`` as a
            # standalone token followed by more tokens (conversion operator).
            rest = tok[len("operator"):]
            if tok == "operator" or (rest and not rest[0].isalnum() and rest[0] != "_"):
                op_idx = idx
                break
    if op_idx < 0:
        return None
    # Everything from ``operator`` onward is the operator name.
    op_tail = " ".join(toks[op_idx:])
    # Normalize whitespace: ``operator  int`` -> ``operator int``.
    op_tail = re.sub(r"\s+", " ", op_tail).strip()
    return op_tail


def _strip_trailing_generics(name_part: str) -> str:
    """Strip a trailing balanced ``<...>`` generic-parameter list from name_part.

    Walks backwards from the end matching angle-bracket depth. When the depth
    returns to zero at a ``<``, the identifier before it is the method name.
    Handles nested generics (``f<vector<int>>``) and space-separated args
    (``f<T, U>``) that split across whitespace tokens. Does NOT strip
    ``operator<<`` / ``operator>>`` (those are NOT a balanced ``<...>`` pair —
    the depth never returns to zero because there's no matching ``>`` before the
    ``<<``; the name stays ``operator<<`` and the caller's identifier regex
    handles the ``operator`` prefix).
    """
    if not name_part.endswith(">"):
        return name_part
    depth = 0
    for idx in range(len(name_part) - 1, -1, -1):
        ch = name_part[idx]
        if ch == ">":
            depth += 1
        elif ch == "<":
            depth -= 1
            if depth == 0:
                # Found the matching ``<``. The name is everything before it.
                return name_part[:idx].rstrip()
    # Unbalanced ``<`` (depth never returned to zero) — leave unchanged so the
    # caller's identifier regex can reject it (e.g. ``operator<<``).
    return name_part


def _is_trailing_qualifier_token(tok: str) -> bool:
    """True if ``tok`` is a trailing method qualifier (bare or parenthesized).

    Handles the bare form (``const``/``noexcept``/``override``/...) AND the
    parenthesized C++ form ``noexcept(expr)`` / ``throw(...)`` — where the
    qualifier keyword is glued to its argument list. Without this, ``void f()
    noexcept(false)`` left the parenthesized qualifier in place, so the
    ``endswith(")")`` guard misread ``(false)`` as the param list and produced a
    phantom method named ``noexcept``.
    """
    if tok in _A_TRAILING_METHOD_QUALIFIERS:
        return True
    # Parenthesized form: ``noexcept(...)`` / ``throw(...)``. The keyword is the
    # head before the first ``(``.
    head = tok.split("(", 1)[0]
    return head in _A_TRAILING_METHOD_QUALIFIERS and tok.endswith(")")


def _buf_is_macro_rules(buf: str, language: str | None) -> bool:
    """True if ``buf`` is a ``macro_rules! Name`` declaration (Rust).

    Detects both ``macro_rules! name`` and ``pub macro_rules! name`` / ``pub(crate)
    macro_rules! name``. Used by the ``{`` handler to mark the opened body as a
    macro body so its template fragments (``fn``/``const`` inside the macro) are
    not classified as real entities.
    """
    if language != "rust" or not buf:
        return False
    toks = buf.split()
    if toks and (toks[0] == "pub" or toks[0].startswith("pub(")):
        toks = toks[1:]
    return bool(toks) and toks[0].startswith("macro_rules!")


def _buf_has_pending_signature(buf: str) -> bool:
    """True if ``buf`` holds an unterminated method/function signature.

    Used by the Family-A line-comment-newline handler to decide whether to
    preserve the token buffer across the newline. A pending signature is one
    that contains a balanced ``(...)`` parameter list (paren depth returned to
    zero) but no statement terminator — the opening ``{`` of the body is
    expected on a later line (Allman brace style).

    The check is conservative: it requires at least one ``(`` AND net-zero paren
    depth across the whole buffer (so an unbalanced ``(`` from an unfinished
    expression is NOT treated as a signature). This correctly distinguishes:

    - ``fn foo()`` / ``int getCount() const`` → pending signature (preserve)
    - ``let x = 1;`` → terminated (``;`` already reset buf, so buf is empty)
    - ``x = foo() + bar`` → no balanced trailing param list as a *signature*;
      though this CAN contain ``()``, the call-site behavior is unchanged because
      such a line almost never precedes an Allman ``{``.
    """
    if not buf:
        return False
    # An assignment (``=``) before the param list means this is an expression
    # statement (``x = foo()``), not a declaration — reject so the buffer is
    # wiped at the comment newline instead of over-preserved. A real signature
    # never has a top-level ``=`` before its ``()``.
    # Find the first ``(`` (the param list); an ``=`` before it is an assignment.
    paren = buf.find("(")
    if paren > 0 and "=" in buf[:paren]:
        return False
    depth = 0
    saw_open = False
    for ch in buf:
        if ch == "(":
            depth += 1
            saw_open = True
        elif ch == ")":
            depth -= 1
    return saw_open and depth == 0


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
    # Strip tokens that may legitimately trail the parameter list: C++ method
    # qualifiers (``const``/``noexcept``/``override``/``final``, stacked), Dart
    # async markers (``async``/``async*``/``sync*``/``yield``), and the C++
    # trailing return type (``-> RetType``). Without this, a signature like
    # ``int get() const`` or ``void f() async`` ends with a keyword rather than
    # ``)``, so the ``endswith(")")`` guard below silently dropped the whole
    # method — a very common C++/Dart shape.
    joined = _strip_trailing_signature_tokens(joined)
    # Must end in ``)`` for a signature (params before ``{``).
    if not joined.endswith(")"):
        return None
    # Find the parameter list: the last ``(`` ... ``)`` run ending the buffer.
    paren_open = _last_balanced_paren_open(joined)
    if paren_open < 0:
        return None  # no balanced param list → not a signature
    # The name is the identifier immediately before ``(``.
    name_part = joined[:paren_open].rstrip()
    if not name_part:
        return None
    # Strip a trailing generic-parameter list ``<...>`` from the name_part.
    # Must use angle-bracket DEPTH matching across the whole name_part (which
    # may span multiple whitespace-separated tokens), not a single-token rfind.
    # Without depth matching, ``f<vector<int>>`` (nested) and ``f<T, U>`` (space-
    # separated args split across tokens) were silently dropped. Also recovers
    # ``operator<<`` / ``operator>>`` (the ``<<`` is not a generic list).
    name_part = _strip_trailing_generics(name_part)
    # The name is the last whitespace-separated token of the pre-param run.
    name_tok = name_part.split()[-1] if name_part.split() else ""
    # C++ destructor: keep the ``~`` prefix so ``~Widget`` is distinguished from
    # the constructor ``Widget`` (otherwise every RAII class has a duplicate
    # identity and the 3-way diff blanket-declines).
    is_dtor = name_tok.startswith("~")
    # C++ operator overloads, including conversion operators. The ``operator``
    # keyword may be the last token (``operator+``, ``operator<<``) OR followed
    # by the target type (``operator int``, ``operator void*``). Build a
    # distinguishable name: ``operator+`` / ``operator int`` / ``operator<<``.
    # Without this, conversion operators were misnamed (``int``) or dropped
    # (``*``), and distinct operators (``+`` vs ``-``) collided.
    op_name = _cpp_operator_name(name_part)
    if op_name is not None:
        name_tok = op_name
    elif is_dtor:
        # Keep the ``~`` — it's a valid distinguishing prefix.
        pass
    # Guard 3: valid identifier (or a recognized special name above).
    if op_name is None and not is_dtor:
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
    r"^(?:(?:pub(?:\([^)]*\))?|export|public|private|static|final|readonly|unsafe|inline|mut|extern)\s+)*"
    r"(?:const|static|type|let|var)\s+(?:mut\s+)?([A-Za-z_][A-Za-z0-9_]*)\b"
    # Anchor: the name must be followed by a type annotation (``:``) or an
    # assignment (``=``) or end-of-statement. This rejects Java/C++ typed fields
    # (``static final int N``) where backtracking would match ``static`` as the
    # field keyword and capture the following TYPE token as the name. In valid
    # Rust/JS field declarations the name is always followed by ``:``/``=``.
    r"\s*(?:[:=]|$)"
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
                    fingerprint=unit_body_fingerprint(body, lang=language),
                )
            )
            existing.add(ident)
            break  # one declaration per line


def _extract_a_import_name(line: str) -> str:
    """Best-effort label for a Family-A import/use/include statement."""
    s = line.strip()
    # Strip an optional ``pub`` / ``pub(crate)`` / ``pub(super)`` re-export prefix.
    s = re.sub(r"^pub(?:\([^)]*\))?\s+", "", s)
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


def _raw_header_body_split(body: str) -> tuple[str, str]:
    """Split body text into raw (header, rest) — no normalization applied.

    The structural skeleton shared by every header/body split: multi-line
    bodies split at the first line; one-liner bodies split at the scope opener
    (``{`` for Family A, ``:`` at depth 0 for Family B) so an inline body isn't
    folded into the header. Callers normalize the two halves their own way
    (rename detection strips comments; change detection preserves them).
    """
    body = body or ""
    if not body:
        return "", ""
    lines = body.split("\n")
    # A one-liner body with a trailing newline splits into [line, ""] — treat
    # it as a single-line body (split at the scope opener) so the inline body
    # content isn't lost. Only genuinely multi-line bodies (>1 non-empty line)
    # take the multi-line path.
    if len(lines) > 1 and any(ln.strip() for ln in lines[1:]):
        return lines[0], "\n".join(lines[1:])
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
        return header, rest
    colon = _scope_opener_colon(line)
    if colon >= 0:
        return line[: colon + 1], line[colon + 1 :]
    # No opener found (e.g. a field ``const N = 5;``): whole line is the header.
    return line, ""


def split_header_body(body: str, *, lang: str | None = None) -> tuple[str, str]:
    """Split a unit's body text into (header, rest), comment-stripped.

    The CANONICAL header/body split for rename detection: a rename changes the
    def/fn header (``def foo`` → ``def bar``) but leaves the body content
    identical, so rename detection compares the header-STRIPPED body. Comments
    and string-literal values are stripped/blanked so two bodies differing only
    in those normalize equal — rename detection is comment-stable. (Change
    detection in ``structural._split_header_body`` intentionally diverges: it
    preserves comments via a whitespace-only collapse.)

    ``lang`` selects the comment marker (``//`` for Family-A, ``#`` for
    Python/Ruby); defaults to Python.
    """
    header, rest = _raw_header_body_split(body)
    return _normalize_header(header), normalize_body(rest, lang=lang)


def entity_body_content(body: str, *, lang: str | None = None) -> str:
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
    string any unit/entity carries. ``lang`` selects the comment marker;
    defaults to Python.
    """
    if not body:
        return ""
    _, rest = split_header_body(body, lang=lang)
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
    lang: str | None = None,
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

    ``lang`` selects the comment marker for body-content normalization
    (``//`` for Family-A brace languages, ``#`` for Python/Ruby). Defaults to
    Python. Pass the entity's language so a Rust ``// note`` is stripped
    (comment-stable pairing) — otherwise a rename that also edits a comment
    won't pair, disagreeing with the parse-time fingerprint which IS
    lang-aware.

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
        content = entity_body_content(e.body, lang=lang)
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
        content = entity_body_content(e.body, lang=lang)
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

"""Canonical string/comment lexer — the single source of truth for blanking.

Prior to this module, string/comment blanking was scattered across 7 sites in
6 files (``_blank_all_strings``, ``_blank_text_strings``, ``_mask_strings_and_
comments``, ``_strip_trailing_comment``, ``_multi_string_open_count``/``_closes``,
``_body_below_header``, ``_blank_line_strings``) with 3 tiers of coverage that
DRIFTED: some handled Rust raw strings, others C++ raw, others neither; some
preserved f-string interpolations, others blanked them. Every raw-string /
f-string / C++-raw silent-wrong-output bug traced to one site using a narrower
definition of "string" than another.

This module exposes ONE function — :func:`blank_strings_and_comments` — backed
by the character-scan state machine originally written for ``parse_family_a``
(``_advance_string_comment`` + ``_AStrState``). The char-scan was already the
correct, complete implementation (Rust raw hash-count matching, C++ raw
delimiter matching, escapes, line/block comments, f-string interpolation); it
was just private to the parser loop. Lifting it here makes it available to every
blanking site so the definition of "string" and "comment" is uniform everywhere.

Coverage (all handled by the single char-scan):

* **Regular strings** — ``"..."`` and ``'...'`` (with ``\\`` escapes).
* **Python triple-quotes** — ``\"\"\"...\"\"\"`` and ``'''...'''`` (multi-line).
* **Rust raw strings** — ``r"..."``, ``r#"..."#``, ``r##"..."##`` (any hash
  count); the closer must match the opener's hash count exactly. Also ``br#"..."#``
  / ``rb#"..."#``.
* **C++ raw strings** — ``R"DELIM(...)DELIM"`` (DELIM any d-char run, incl.
  empty) with optional encoding prefixes ``u8``/``L``/``u``/``U`` (so ``u8R"x(...)x"``,
  ``LR"(...)"``, etc.). The closer is ``)DELIM"``.
* **Rust char literals** — ``'X'`` (NOT lifetimes ``'a`` / ``'static``, which are
  left as code; NOT C++14 digit separators ``1'000``).
* **Comments** — ``//`` line and ``/* */`` block (Family-A brace languages) OR
  ``#`` line (Family-B Python/Ruby), selected by the ``lang`` parameter.

The blanking is **length-preserving** (each consumed char becomes ``"_"`` for
strings / ``" "`` for comments by default) so byte offsets in the blanked text
align with the original — the inline-comment-stripping and region-extraction
sites depend on this alignment.
"""

from __future__ import annotations

from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Language → comment-style dispatch
# ---------------------------------------------------------------------------

#: Languages using ``//`` line comments and ``/* */`` block comments (Family A).
#: Every other language defaults to ``#`` line comments (Family B: Python/Ruby).
_FAMILY_A_LANGS: frozenset[str] = frozenset({
    "rust", "rs", "javascript", "js", "typescript", "ts", "jsx", "tsx",
    "go", "golang", "java", "c", "cpp", "c++", "csharp", "cs",
    "kotlin", "swift", "scala", "dart", "php",
})


def _lang_uses_slash_comments(lang: str | None) -> bool:
    """True when ``lang`` uses ``//`` / ``/* */`` comments (Family A).

    ``None`` defaults to Family B (Python/Ruby, ``#`` comments) — matching the
    long-standing default of every blanking site that predates this module.
    """
    if lang is None:
        return False
    return (lang or "").strip().lower() in _FAMILY_A_LANGS


# ---------------------------------------------------------------------------
# String-prefix detection (Rust raw + C++ raw)
# ---------------------------------------------------------------------------

#: String-prefix runes that introduce a non-plain string literal whose closing
#: quote rule differs from a bare ``"``. Used to recover Rust raw strings.
_RAW_PREFIX_RUNES = frozenset("rRbB")

#: Hex-digit characters — used to detect C++14 digit separators (``1'000``).
_HEXDIGITS = frozenset("0123456789abcdefABCDEF")


def _match_string_prefix(src: str, quote_idx: int) -> int:
    """Detect a Rust string prefix ending at ``src[quote_idx] == '"'``.

    Returns the number of trailing ``#`` chars for a Rust raw string
    (``r#"..."#`` → N; ``r"..."`` → 0), or ``0`` for a recognized prefix that
    still closes on a plain ``"`` (byte strings ``b"..."``, ordinary ``"``).
    Returns ``0`` when no prefix is present (the caller treats it as a plain
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
    runes: list[str] = []
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


def _match_cpp_raw_prefix(src: str, quote_idx: int, n: int) -> str | None:
    """Detect a C++ raw-string opener ending at ``src[quote_idx] == '"'``.

    C++ raw strings have the form ``[u8|L|u|U]R"DELIM(...)DELIM"`` where DELIM
    is an optional identifier (the raw-string delimiter). The closer is
    ``)DELIM"``. Returns the delimiter string (possibly empty) when this is a
    C++ raw string opener, or ``None`` otherwise.
    """
    j = quote_idx - 1
    if j < 0 or src[j] != "R":
        return None
    k = j - 1
    # Optional encoding prefix: u8, L, u, U (case-sensitive per the standard).
    if k >= 0 and src[k] == "8" and k - 1 >= 0 and src[k - 1] in ("u", "U", "L"):
        k -= 2
    elif k >= 0 and src[k] in ("L", "u", "U"):
        k -= 1
    # Word boundary: the char before the (encoding-prefixed) R must be a
    # non-identifier char (or start of input).
    if k >= 0 and (src[k].isalnum() or src[k] == "_"):
        return None
    # Look ahead from after the quote for DELIM + "(". DELIM is an optional run
    # of d-chars (anything except whitespace, parens, backslash; up to 16 chars).
    a = quote_idx + 1
    delim_start = a
    while a < n and src[a] not in ("(", ")", " ", "\t", "\n"):
        delim = src[delim_start:a]
        if len(delim) > 16:
            return None
        a += 1
    if a >= n or src[a] != "(":
        return None
    return src[delim_start:a]


# ---------------------------------------------------------------------------
# Char-scan state machine
# ---------------------------------------------------------------------------


@dataclass
class _LexState:
    """Mutable string/comment state for the char-scan.

    ``in_str`` is one of ``'"'``, ``"'"``, ``"`"`` (JS template), ``"char"``
    (Rust char literal), ``"triple_d"`` / ``"triple_s"`` (Python triple-quoted),
    or ``None``. When ``in_str == '"'`` and ``hash_count > 0`` we're inside a
    Rust raw string (``r#"..."#``). When ``in_cpp_raw`` is True we're inside a
    C++ raw string (whose delimiter may be empty — ``R"(...)"`` — so a separate
    flag is needed rather than testing ``cpp_raw_delim`` for truthiness).
    """
    in_str: str | None = None
    hash_count: int = 0
    in_cpp_raw: bool = False
    cpp_raw_delim: str = ""
    in_line_comment: bool = False
    in_block_comment: bool = False
    # Python f-string: when non-None, we're inside the ``{...}`` interpolation of
    # an f-string whose opener quote is this char (so the interpolation's code
    # is NOT blanked even though we're "inside a string"). Nested braces track
    # depth via ``fstring_depth``.
    in_fstring_interp: str | None = None
    fstring_depth: int = 0


def _is_fstring_prefix(src: str, quote_idx: int) -> bool:
    """True when the quote at ``quote_idx`` is preceded by an f-string prefix.

    Recognized: ``f"``, ``rf"``, ``fr"``, ``F"``, ``RF"``, etc. — a standalone
    ``f`` (optionally combined with ``r``/``R``) immediately before the quote,
    with a word boundary before the prefix run.
    """
    j = quote_idx - 1
    if j < 0:
        return False
    # Collect the prefix rune run (f, r, R, F — only these, in any order, up to 3).
    runes: list[str] = []
    while j >= 0 and src[j] in ("f", "F", "r", "R") and len(runes) < 3:
        runes.append(src[j])
        j -= 1
    if not runes or "f" not in (r.lower() for r in runes):
        return False
    # Word boundary: the rune run must be preceded by a non-identifier char.
    if j >= 0 and (src[j].isalnum() or src[j] == "_"):
        return False
    return True


def _blank_strings_and_comments_scan(
    src: str,
    *,
    slash_comments: bool,
    hash_comments: bool,
    blank_strings: bool,
    blank_comments: bool,
    preserve_fstring_interpolation: bool,
    string_char: str = "_",
    comment_char: str = " ",
) -> str:
    """The core char-scan. Produces blanked text, length-preserving.

    See :func:`blank_strings_and_comments` for the public wrapper. This internal
    takes explicit comment-style flags so it can be reused for mixed cases.
    """
    n = len(src)
    st = _LexState()
    out: list[str] = []
    i = 0
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""

        # --- newline: emit as-is, close line comment ---
        if ch == "\n":
            if st.in_line_comment:
                st.in_line_comment = False
            out.append(ch)
            i += 1
            continue

        # --- inside a line comment: blank until newline ---
        if st.in_line_comment:
            out.append(comment_char if blank_comments else ch)
            i += 1
            continue

        # --- inside a block comment: blank until ``*/`` ---
        if st.in_block_comment:
            if ch == "*" and nxt == "/":
                st.in_block_comment = False
                out.append(comment_char if blank_comments else ch)
                out.append(comment_char if blank_comments else "/")
                i += 2
                continue
            out.append(comment_char if blank_comments else ch)
            i += 1
            continue

        # --- inside an f-string interpolation: emit code verbatim until ``}`` ---
        if st.in_fstring_interp is not None and preserve_fstring_interpolation:
            # Nested braces inside the interpolation (e.g. ``f"{d['x']}"`` or
            # ``f"{ {x:1} }"``) — track depth so the interpolation's own ``}``
            # doesn't end it prematurely.
            if ch == "{":
                st.fstring_depth += 1
                out.append(ch)
                i += 1
                continue
            if ch == "}":
                st.fstring_depth -= 1
                if st.fstring_depth == 0:
                    # End of interpolation — return to string state.
                    st.in_str = st.in_fstring_interp
                    st.in_fstring_interp = None
                    out.append(ch)
                    i += 1
                    continue
                out.append(ch)
                i += 1
                continue
            # A quote INSIDE the interpolation opens a nested string — recurse
            # via the normal string-entry logic below by NOT treating this char
            # as interpolation code. Fall through to the string detection, but
            # remember we're in an interpolation so the nested string's closer
            # returns us here. (Simplified: blank the nested string content.)
            # For now, emit interpolation code verbatim (the common case).
            out.append(ch)
            i += 1
            continue

        # --- inside a string literal: blank until the closer ---
        if st.in_str is not None:
            # Backslash escapes (NON-raw strings only).
            if ch == "\\":
                if st.in_str == '"' and st.hash_count > 0:
                    # Raw strings treat backslash literally.
                    out.append(string_char if blank_strings else ch)
                    i += 1
                    continue
                out.append(string_char if blank_strings else ch)
                if i + 1 < n:
                    out.append(string_char if blank_strings else src[i + 1])
                i += 2
                continue
            # Char-literal closer: 'X'
            if st.in_str == "char" and ch == "'":
                st.in_str = None
                out.append(string_char if blank_strings else ch)
                i += 1
                continue
            # Single-quoted string closer.
            if st.in_str == "'" and ch == "'":
                st.in_str = None
                out.append(string_char if blank_strings else ch)
                i += 1
                continue
            # JS template literal closer.
            if st.in_str == "`" and ch == "`":
                st.in_str = None
                out.append(string_char if blank_strings else ch)
                i += 1
                continue
            # Double-quoted string: handle closers (plain / Rust raw / C++ raw)
            # and f-string interpolation entry. Only fires when ch == '"'.
            if st.in_str == '"' and ch == '"':
                # C++ raw closer: ``"`` preceded by ``)`` + delim. The delimiter
                # may be empty (``R"(...)"``), so we check a dedicated flag
                # (``in_cpp_raw``) rather than the delim string's truthiness.
                if st.in_cpp_raw:
                    delim = st.cpp_raw_delim
                    need = ")" + delim
                    start = i - len(need)
                    if start >= 0 and src[start:i] == need:
                        st.in_str = None
                        st.in_cpp_raw = False
                        st.cpp_raw_delim = ""
                        out.append(string_char if blank_strings else ch)
                        i += 1
                        continue
                    # Embedded quote — not the closer; fall through to blank.
                # Rust raw closer: ``"`` + EXACTLY ``hash_count`` ``#`` chars
                # (not more). ``"###`` (3 hashes) does NOT close ``r##"..."##``
                # (2 hashes) — the closing hash run must equal the opener's
                # exactly (Rust Reference).
                elif st.hash_count > 0:
                    hc = st.hash_count
                    tail = src[i + 1 : i + 1 + hc]
                    after = src[i + 1 + hc] if i + 1 + hc < n else ""
                    if (
                        len(tail) == hc
                        and tail == "#" * hc
                        and after != "#"  # not part of a longer hash run
                    ):
                        st.in_str = None
                        st.hash_count = 0
                        out.append(string_char if blank_strings else ch)
                        for _ in range(hc):
                            out.append(string_char if blank_strings else "#")
                        i += 1 + hc
                        continue
                    # Not the closer; fall through to blank.
                else:
                    # Plain double-quote closer.
                    st.in_str = None
                    out.append(string_char if blank_strings else ch)
                    i += 1
                    continue
            # Triple-quote closer (Python ``"""`` / ``'''``).
            if st.in_str in ("triple_d", "triple_s"):
                marker = '"""' if st.in_str == "triple_d" else "'''"
                if src[i : i + 3] == marker:
                    st.in_str = None
                    out.append(string_char if blank_strings else ch)
                    out.append(string_char if blank_strings else ch)
                    out.append(string_char if blank_strings else ch)
                    i += 3
                    continue
                # f-string interpolation entry inside a triple-quote string.
                if (
                    preserve_fstring_interpolation
                    and ch == "{"
                    and st.in_fstring_interp is None
                ):
                    st.in_fstring_interp = st.in_str
                    st.fstring_depth = 1
                    out.append(ch)
                    i += 1
                    continue
                # Content char in triple-quote — blank and advance.
                out.append(string_char if blank_strings else ch)
                i += 1
                continue
            # f-string interpolation entry inside a regular double-quote string.
            if (
                st.in_str == '"'
                and preserve_fstring_interpolation
                and ch == "{"
                and st.cpp_raw_delim == ""
                and st.hash_count == 0
                and st.in_fstring_interp is None
            ):
                st.in_fstring_interp = '"'
                st.fstring_depth = 1
                out.append(ch)
                i += 1
                continue
            # Ordinary content char inside any string — blank and advance.
            out.append(string_char if blank_strings else ch)
            i += 1
            continue

        # --- not in string/comment: detect transitions into one ---
        if slash_comments and ch == "/" and nxt == "/":
            st.in_line_comment = True
            out.append(comment_char if blank_comments else ch)
            out.append(comment_char if blank_comments else "/")
            i += 2
            continue
        if slash_comments and ch == "/" and nxt == "*":
            st.in_block_comment = True
            out.append(comment_char if blank_comments else ch)
            out.append(comment_char if blank_comments else "*")
            i += 2
            continue
        if hash_comments and ch == "#":
            st.in_line_comment = True
            out.append(comment_char if blank_comments else ch)
            i += 1
            continue
        if ch == '"':
            # Python triple-quote opener?
            if src[i : i + 3] == '"""':
                st.in_str = "triple_d"
                out.append(string_char if blank_strings else ch)
                out.append(string_char if blank_strings else ch)
                out.append(string_char if blank_strings else ch)
                i += 3
                continue
            # C++ raw string opener?
            cpp_delim = _match_cpp_raw_prefix(src, i, n)
            if cpp_delim is not None:
                st.in_str = '"'
                st.in_cpp_raw = True
                st.cpp_raw_delim = cpp_delim
                out.append(string_char if blank_strings else ch)
                i += 1
                continue
            # Rust raw / byte string?
            prefix = _match_string_prefix(src, i)
            st.in_str = '"'
            st.hash_count = prefix
            # f-string interpolation entry on the NEXT ``{`` is handled in the
            # in_str branch above (we only mark the prefix here).
            out.append(string_char if blank_strings else ch)
            i += 1
            continue
        if ch == "'":
            # Triple single-quote (Python)?
            if src[i : i + 3] == "'''":
                st.in_str = "triple_s"
                out.append(string_char if blank_strings else ch)
                out.append(string_char if blank_strings else ch)
                out.append(string_char if blank_strings else ch)
                i += 3
                continue
            nxt1 = src[i + 1] if i + 1 < n else ""
            nxt2 = src[i + 2] if i + 2 < n else ""
            prev = src[i - 1] if i > 0 else ""
            # Rust lifetime ('a / 'static) — only in Family-A (Rust). In
            # Python/JS, 'x' is always a string/char literal, never a lifetime.
            if (
                slash_comments  # Family-A only
                and (nxt1.isalpha() or nxt1 == "_")
                and nxt2 != "'"
                and not (prev.isalnum() or prev == "_")
            ):
                # Rust lifetime — code, not a string.
                out.append(ch)
                i += 1
                continue
            # C++14 digit separator (1'000'000): hex ' hex, no closing '.
            if prev in _HEXDIGITS and nxt1 in _HEXDIGITS and nxt2 != "'":
                out.append(ch)
                i += 1
                continue
            st.in_str = "char"
            out.append(string_char if blank_strings else ch)
            i += 1
            continue
        if ch == "`":
            # JS template literal.
            st.in_str = "`"
            out.append(string_char if blank_strings else ch)
            i += 1
            continue

        # Ordinary code char.
        out.append(ch)
        i += 1

    return "".join(out)


def blank_strings_and_comments(
    text: str,
    lang: str | None = None,
    *,
    blank_strings: bool = True,
    blank_comments: bool = True,
    preserve_fstring_interpolation: bool = False,
    string_char: str = "_",
    comment_char: str = " ",
) -> str:
    """Blank string-literal and comment contents (length-preserving).

    The canonical lexer for all structural-analysis blanking sites. Handles
    every string form (regular, triple-quote, Rust raw, C++ raw, char literals,
    JS template literals) and both comment styles (``//``/``/* */`` for Family A,
    ``#`` for Family B), selected by ``lang``.

    Args:
        text: the source text.
        lang: the language (selects comment style). ``None`` defaults to
            Family B (Python/Ruby, ``#`` comments) — matching the historical
            default of every blanking site.
        blank_strings: when True (default), replace string-literal contents with
            ``string_char`` (length-preserving; default ``"_"``).
        blank_comments: when True (default), replace comment contents with
            ``comment_char`` (length-preserving; default ``" "``).
        preserve_fstring_interpolation: when True, the ``{...}`` interpolation
            of an f-string is emitted VERBATIM (as code) rather than blanked.
            The validator path needs this (it must see ``{foo()}`` calls so a
            dropped side-effect is detectable); the fingerprint path leaves it
            False (the whole f-string is blanked uniformly).

    Returns:
        The blanked text (same length as ``text``).
    """
    slash = _lang_uses_slash_comments(lang)
    hash_c = not slash  # Family B uses # comments; Family A does not.
    return _blank_strings_and_comments_scan(
        text,
        slash_comments=slash,
        hash_comments=hash_c,
        blank_strings=blank_strings,
        blank_comments=blank_comments,
        preserve_fstring_interpolation=preserve_fstring_interpolation,
        string_char=string_char,
        comment_char=comment_char,
    )


def blank_strings(text: str, lang: str | None = None, *,
                  preserve_fstring_interpolation: bool = False,
                  string_char: str = "_") -> str:
    """Blank ONLY string-literal contents (comments preserved). Thin wrapper."""
    return blank_strings_and_comments(
        text, lang,
        blank_strings=True, blank_comments=False,
        preserve_fstring_interpolation=preserve_fstring_interpolation,
        string_char=string_char,
    )


def blank_comments(text: str, lang: str | None = None) -> str:
    """Blank ONLY comment contents (strings preserved). Thin wrapper."""
    return blank_strings_and_comments(
        text, lang,
        blank_strings=False, blank_comments=True,
    )


def blank_raw_strings(text: str) -> str:
    """Blank Rust and C++ raw strings only (regular strings and comments
    preserved). Used by sites that run a SEPARATE regular-string blanking pass
    afterward (the order matters: raw first, then regular, so the regular pass
    doesn't mis-handle an embedded quote inside a raw string).

    Language-agnostic (raw strings are Rust/C++-specific; in other languages
    this is a no-op pass that the regular-string pass handles correctly).
    """
    # Run the full scan but with only raw-string blanking active. We achieve
    # "raw only" by blanking strings AND comments, then the caller re-runs its
    # own regular-string pass. Simpler: just run the unified scanner with
    # blank_strings=True for the raw forms and False for regular. The scanner
    # doesn't distinguish — so we use the regex-based raw-only patterns from
    # abstract_parser (kept as the fast path for this specific two-pass case).
    import re
    # C++ raw: R"DELIM(...)DELIM" (DELIM any run of non-paren/quote/backslash).
    cpp = re.compile(
        r'(?<![A-Za-z0-9_])(?:u8R|uR|UR|LR|R)"([^()\\]*)\(.*?\)\1"',
        re.DOTALL,
    )
    text = cpp.sub(lambda m: "_" * len(m.group(0)), text)
    # Rust raw: r#"..."#, r"..." (lowercase prefix, #* delimiter).
    rust = re.compile(
        r'(?<![A-Za-z0-9_])(?:br|rb|r)(#*)"((?:(?!"\1).)*?)"\1',
        re.DOTALL,
    )
    return rust.sub(lambda m: "_" * len(m.group(0)), text)


def mask_deferable_comments(
    text: str, lang: str | None = None
) -> tuple[str, list[tuple[int, int, str]]]:
    """Mask ONLY deferable (prose) comments, preserving non-deferable ones verbatim.

    Runs :func:`enumerate_comment_spans` + :func:`classify_spans`, then blanks
    each DEFERRED comment's content with spaces (length-preserving). MACHINE
    (directives), LEGAL (license), GENERATED (codegen), and DOCTEST (executable
    examples) comments survive verbatim — they're code-significant.

    Returns ``(masked_text, deferred_spans)`` where ``deferred_spans`` is the
    list of ``(start, end, original_text)`` for the blanked comments — the
    comment ledger's input for the reconciliation pass.

    The masked text is length-preserving: ``len(masked) == len(text)`` and every
    non-comment byte is unchanged. This is the "comment-free code view" sent to
    the code-resolution model (more useful context, less confusion from stale
    prose). The original text + deferred_spans remain in the sidecar ledger for
    restoration.
    """
    from capybase.adapters.comment_classifier import (
        classify_spans, CommentClass,
    )
    spans = enumerate_comment_spans(text, lang)
    classified = classify_spans(spans, text, lang)
    # Build the masked text: copy original, blank each DEFERRED span.
    masked = list(text)  # mutable char list
    deferred: list[tuple[int, int, str]] = []
    for cc in classified:
        if cc.cls == CommentClass.DEFERRED:
            for j in range(cc.start, cc.end):
                # Don't blank newlines (preserve line structure).
                if masked[j] != "\n":
                    masked[j] = " "
            deferred.append((cc.start, cc.end, cc.text))
    return "".join(masked), deferred


def enumerate_comment_spans(
    text: str, lang: str | None = None
) -> list[tuple[int, int, str]]:
    """Every comment region in ``text`` as ``(start_byte, end_byte_exclusive, text)``.

    Runs the canonical char-scan state machine to detect comment regions (``//``
    line, ``/* */`` block for Family A; ``#`` line for Family B), tracking byte-
    exact spans. Comments INSIDE string literals are NOT counted (the string
    absorbs them). Rust nested block comments (``/* /* */ */``) are handled.

    This is the foundation for the deferred-comment-reconciliation system:
    classify → mask → reconcile. The spans align exactly with the original text
    (``text[start:end] == comment_text``), so the masked view and the restoration
    are offset-correct.

    Args:
        text: the source text.
        lang: the language (selects comment style). ``None`` defaults to Family B.

    Returns:
        A list of ``(start, end, comment_text)`` tuples in source order.
    """
    n = len(text)
    slash = _lang_uses_slash_comments(lang)
    hash_c = not slash
    st = _LexState()
    spans: list[tuple[int, int, str]] = []
    comment_start: int | None = None  # byte offset where the current comment began
    # Rust nested block-comment depth (/* /* */ */ is ONE comment). Family A
    # block comments nest in Rust but NOT in C/C++/JS. Track depth for Rust only.
    block_depth = 0

    i = 0
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        # --- newline: close a line comment ---
        if ch == "\n":
            if st.in_line_comment:
                st.in_line_comment = False
                if comment_start is not None:
                    spans.append((comment_start, i, text[comment_start:i]))
                    comment_start = None
            i += 1
            continue

        if st.in_line_comment:
            i += 1
            continue

        if st.in_block_comment:
            # Rust nested block comments: /* /* */ */ is one comment. Track depth.
            if slash and lang == "rust" and ch == "/" and nxt == "*":
                block_depth += 1
                i += 2
                continue
            if ch == "*" and nxt == "/":
                if slash and lang == "rust" and block_depth > 0:
                    block_depth -= 1
                    if block_depth > 0:
                        # Still nested — don't close yet.
                        i += 2
                        continue
                st.in_block_comment = False
                end = i + 2
                if comment_start is not None:
                    spans.append((comment_start, end, text[comment_start:end]))
                    comment_start = None
                i += 2
                continue
            i += 1
            continue

        # Inside a string — skip (don't detect comments).
        if st.in_str is not None:
            if ch == "\\":
                if st.in_str == '"' and st.hash_count > 0:
                    i += 1
                    continue
                i += 2
                continue
            if st.in_str == "char" and ch == "'":
                st.in_str = None
                i += 1
                continue
            if st.in_str == "'" and ch == "'":
                st.in_str = None
                i += 1
                continue
            if st.in_str == "`" and ch == "`":
                st.in_str = None
                i += 1
                continue
            if st.in_str == '"':
                if st.in_cpp_raw:
                    delim = st.cpp_raw_delim
                    need = ")" + delim
                    start_check = i - len(need)
                    if start_check >= 0 and text[start_check:i] == need:
                        st.in_str = None
                        st.in_cpp_raw = False
                        st.cpp_raw_delim = ""
                        i += 1
                        continue
                    i += 1
                    continue
                if st.hash_count > 0:
                    hc = st.hash_count
                    tail = text[i + 1 : i + 1 + hc]
                    after = text[i + 1 + hc] if i + 1 + hc < n else ""
                    if len(tail) == hc and tail == "#" * hc and after != "#":
                        st.in_str = None
                        st.hash_count = 0
                        i += 1 + hc
                        continue
                    i += 1
                    continue
                st.in_str = None
                i += 1
                continue
            if st.in_str in ("triple_d", "triple_s"):
                marker = '"""' if st.in_str == "triple_d" else "'''"
                if text[i : i + 3] == marker:
                    st.in_str = None
                    i += 3
                    continue
                i += 1
                continue
            i += 1
            continue

        # Inside f-string interpolation — skip (code inside {}).
        if st.in_fstring_interp is not None and st.fstring_depth > 0:
            if ch == "{":
                st.fstring_depth += 1
                i += 1
                continue
            if ch == "}":
                st.fstring_depth -= 1
                if st.fstring_depth == 0:
                    st.in_str = st.in_fstring_interp
                    st.in_fstring_interp = None
                i += 1
                continue
            i += 1
            continue

        # --- transitions INTO a comment ---
        if slash and ch == "/" and nxt == "/":
            st.in_line_comment = True
            comment_start = i
            i += 2
            continue
        if slash and ch == "/" and nxt == "*":
            st.in_block_comment = True
            block_depth = 1  # nested block-comment depth (Rust `/* /* */ */`)
            comment_start = i
            i += 2
            continue
        if hash_c and ch == "#":
            st.in_line_comment = True
            comment_start = i
            i += 1
            continue

        # --- transitions INTO a string (so we don't mis-detect // inside) ---
        if ch == '"':
            if text[i : i + 3] == '"""':
                st.in_str = "triple_d"
                i += 3
                continue
            cpp_delim = _match_cpp_raw_prefix(text, i, n)
            if cpp_delim is not None:
                st.in_str = '"'
                st.in_cpp_raw = True
                st.cpp_raw_delim = cpp_delim
                i += 1
                continue
            st.in_str = '"'
            st.hash_count = _match_string_prefix(text, i)
            i += 1
            continue
        if ch == "'":
            if text[i : i + 3] == "'''":
                st.in_str = "triple_s"
                i += 3
                continue
            nxt1 = text[i + 1] if i + 1 < n else ""
            nxt2 = text[i + 2] if i + 2 < n else ""
            prev = text[i - 1] if i > 0 else ""
            if (
                slash
                and (nxt1.isalpha() or nxt1 == "_")
                and nxt2 != "'"
                and not (prev.isalnum() or prev == "_")
            ):
                i += 1
                continue
            if prev in _HEXDIGITS and nxt1 in _HEXDIGITS and nxt2 != "'":
                i += 1
                continue
            st.in_str = "char"
            i += 1
            continue
        if ch == "`":
            st.in_str = "`"
            i += 1
            continue

        i += 1

    # If the file ends mid-line-comment (no trailing newline), emit the span.
    if comment_start is not None and (st.in_line_comment or st.in_block_comment):
        spans.append((comment_start, n, text[comment_start:n]))
    return spans


def multiline_string_line_mask(text: str, lang: str | None = None) -> list[bool]:
    """For each line of ``text``, True if the line is INSIDE a multi-line string.

    A multi-line string is one that opens on a prior line and hasn't closed by
    the start of this line (Python triple-quote, Rust raw ``r#"..."#``, C++ raw
    ``R"DELIM(...)DELIM"``, JS template literal). A line whose OWN opener is
    closed on the same line is NOT "inside" (the string didn't span).

    Used by the consensus normalizer (and any other site that needs to preserve
    multi-line string interior verbatim — docstrings, raw SQL, etc.) to decide
    which lines to skip comment-stripping / blank-collapse on. This replaces the
    bespoke ``_multi_string_open_count`` / ``_multi_string_closes`` heuristics,
    which (a) didn't handle C++ raw strings and (b) matched closers by a
    hash-count-blind ``"#+`` regex (closing a 2-hash string on a 3-hash line).

    The mask is computed by running the char-scan and tracking, per newline,
    whether the scan was mid-string at that point.
    """
    n = len(text)
    st = _LexState()
    # A line is "interior" if the scan is inside a SPANNING string (triple-quote,
    # raw, template) at the START of the line. Single-line strings (regular
    # "..." / '...' / char) never span, so they don't contribute.
    slash = _lang_uses_slash_comments(lang)
    hash_c = not slash
    # Process char by char, but only track state transitions (don't build
    # blanked output — we only need the mask).
    lines = text.split("\n")
    mask = [False] * len(lines)
    line_idx = 0
    i = 0
    # Mark line_idx as interior if we're in a spanning string at its start.
    # "Spanning" = triple-quote, raw (hash_count > 0), cpp_raw, or template.
    def _in_spanning() -> bool:
        return (
            st.in_str in ("triple_d", "triple_s", "`")
            or (st.in_str == '"' and (st.hash_count > 0 or st.in_cpp_raw))
        )
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "\n":
            line_idx += 1
            if line_idx < len(lines) and _in_spanning():
                mask[line_idx] = True
            i += 1
            continue
        # Replicate the state transitions (simplified — no blanking needed).
        if st.in_line_comment:
            i += 1
            continue
        if st.in_block_comment:
            if ch == "*" and nxt == "/":
                st.in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if st.in_str is not None:
            if ch == "\\":
                if st.in_str == '"' and st.hash_count > 0:
                    i += 1
                    continue
                i += 2
                continue
            if st.in_str == "char" and ch == "'":
                st.in_str = None
                i += 1
                continue
            if st.in_str == "'" and ch == "'":
                st.in_str = None
                i += 1
                continue
            if st.in_str == "`" and ch == "`":
                st.in_str = None
                i += 1
                continue
            if st.in_str == '"':
                if st.in_cpp_raw:
                    delim = st.cpp_raw_delim
                    need = ")" + delim
                    start = i - len(need)
                    if start >= 0 and text[start:i] == need:
                        st.in_str = None
                        st.in_cpp_raw = False
                        st.cpp_raw_delim = ""
                        i += 1
                        continue
                    i += 1
                    continue
                if st.hash_count > 0:
                    hc = st.hash_count
                    tail = text[i + 1 : i + 1 + hc]
                    after = text[i + 1 + hc] if i + 1 + hc < n else ""
                    if (
                        len(tail) == hc and tail == "#" * hc and after != "#"
                    ):
                        st.in_str = None
                        st.hash_count = 0
                        i += 1 + hc
                        continue
                    i += 1
                    continue
                st.in_str = None
                i += 1
                continue
            if st.in_str in ("triple_d", "triple_s"):
                marker = '"""' if st.in_str == "triple_d" else "'''"
                if text[i : i + 3] == marker:
                    st.in_str = None
                    i += 3
                    continue
                i += 1
                continue
            i += 1
            continue
        # Transitions INTO string/comment.
        if slash and ch == "/" and nxt == "/":
            st.in_line_comment = True
            i += 2
            continue
        if slash and ch == "/" and nxt == "*":
            st.in_block_comment = True
            i += 2
            continue
        if hash_c and ch == "#":
            st.in_line_comment = True
            i += 1
            continue
        if ch == '"':
            if text[i : i + 3] == '"""':
                st.in_str = "triple_d"
                i += 3
                continue
            cpp_delim = _match_cpp_raw_prefix(text, i, n)
            if cpp_delim is not None:
                st.in_str = '"'
                st.in_cpp_raw = True
                st.cpp_raw_delim = cpp_delim
                i += 1
                continue
            st.in_str = '"'
            st.hash_count = _match_string_prefix(text, i)
            i += 1
            continue
        if ch == "'":
            if text[i : i + 3] == "'''":
                st.in_str = "triple_s"
                i += 3
                continue
            nxt1 = text[i + 1] if i + 1 < n else ""
            nxt2 = text[i + 2] if i + 2 < n else ""
            prev = text[i - 1] if i > 0 else ""
            if (
                slash
                and (nxt1.isalpha() or nxt1 == "_")
                and nxt2 != "'"
                and not (prev.isalnum() or prev == "_")
            ):
                i += 1
                continue
            if prev in _HEXDIGITS and nxt1 in _HEXDIGITS and nxt2 != "'":
                i += 1
                continue
            st.in_str = "char"
            i += 1
            continue
        if ch == "`":
            st.in_str = "`"
            i += 1
            continue
        i += 1
    return mask


__all__ = [
    "blank_strings_and_comments",
    "blank_strings",
    "blank_comments",
    "blank_raw_strings",
    "multiline_string_line_mask",
]

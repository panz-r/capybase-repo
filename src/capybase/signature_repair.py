"""Sibling-signature injection (S28-54) — the weave-band repair lever.

The duckdb weave band (8 REPAIR_FAILURE/ESCALATE members) fails 1-2
compile errors from passing; 5/8 of the error identifiers have
declaration-shaped lines findable in a conflict side (naive-regex
census, S28-54). This module is the licensed deterministic pre-arm:

1. Extract ``'X' was not declared in this scope`` (and ``has no member
   named 'X'``) identifiers + their reported usage lines from the gcc
   build failures.
2. Find a declaration-shaped line for X in the conflict sides.
3. Insert it immediately before the reported usage line, re-indented to
   match, so the name is in scope at the usage.

GUARDS (the license's both-direction contracts):
- identifier already declared in the buffer -> NO injection (decline);
- identifier declaration absent from both sides -> decline.

The injection is a candidate generator, never a decider: the orchestrator
hook re-validates the injected buffer through the same whole-file gate,
and on failure restores the pre-injection state and continues the normal
repair ladder.
"""

from __future__ import annotations

import re

# gcc diagnostic shapes (the S28-54 census vectors, verbatim):
#   'parser_cache' was not declared in this scope; did you mean 'ParserCache'?
#   'class duckdb::TokenizerBehavior' has no member named 'keyword_helper'
# The harvest diagnostics carry UNICODE quotation marks (U+2018/U+2019);
# ASCII apostrophes accepted for raw-compiler variants.
_NOT_DECLARED_RE = re.compile("[‘']([^'’]+)['’] was not declared in this scope")
_NO_MEMBER_RE = re.compile("has no member named [‘']([^'’]+)['’]")
# Location prefix of a gcc diagnostic: <path>:<line>:<col>: — the path is
# repo-relative (sometimes ../../-prefixed); only the line is usable.
_LOCATION_RE = re.compile(r":(\d+):\d+:")

# A declaration-shaped line: optional whitespace, a TYPE-ish prefix
# (identifiers, ::, templates, &, *, commas, spaces — no '=' '(' '.' '!'),
# then the identifier as a whole word, then '=', '(' or ';' — and the line
# ends a statement (';'). This accepts the census's real declarations
#   auto &parser_cache = DatabaseInstance::GetDatabase(context).GetParserCache();
#   ParserCache cache;
#   Tokenizer tokenizer(behavior, compiled_grammar.GetKeywordHelper());
# and rejects the real usages
#   parser_cache.GetTokenizer().TokenizeInput(behavior);
#   compiled_grammar = cache.GetMatcher();
#   state = tokenizer.keyword_helper.IsKeyword(last_word) ? ... ;
_DECL_TYPO = r"[A-Za-z_][\w:<>,&*\s]*"


def _decl_pattern(identifier: str) -> re.Pattern:
    return re.compile(
        r"^\s*" + _DECL_TYPO + r"\b" + re.escape(identifier) + r"\b\s*(?:=|\(|;)"
    )


def extract_signature_gaps(messages: list[str]) -> list[tuple[str, int | None]]:
    """Extract (identifier, reported_line) pairs from gcc failure messages.

    Order-preserving, de-duplicated by (identifier, line). ``reported_line``
    is None when the message carries no parseable ``:line:col:`` location.
    """
    out: list[tuple[str, int | None]] = []
    seen: set[tuple[str, int | None]] = set()
    for msg in messages or []:
        m_loc = _LOCATION_RE.search(msg or "")
        line = int(m_loc.group(1)) if m_loc else None
        for pat in (_NOT_DECLARED_RE, _NO_MEMBER_RE):
            for m in pat.finditer(msg or ""):
                key = (m.group(1), line)
                if key not in seen:
                    seen.add(key)
                    out.append(key)
    return out


def has_signature_gap(messages: list[str]) -> bool:
    """True when any failure message names a missing declaration — the
    shape the wf repair budget raise (1 -> 3) is licensed for."""
    return bool(extract_signature_gaps(messages))


def find_declaration_line(side_texts: list[str], identifier: str) -> str | None:
    """First declaration-shaped line for ``identifier`` across the sides.

    Sides are searched in the given order (the orchestrator passes them
    most-likely-first). Returns the line WITHOUT its trailing newline.
    """
    pat = _decl_pattern(identifier)
    for text in side_texts or []:
        for line in (text or "").splitlines():
            if pat.match(line) and line.rstrip().endswith(";"):
                return line.strip()  # indent re-applied from the usage line
    return None


def _reindent(decl_line: str, usage_line: str) -> str:
    """The declaration verbatim, re-indented to the usage line's indent."""
    return " " * (len(usage_line) - len(usage_line.lstrip())) + decl_line.lstrip()


def inject_declaration(
    buffer: str,
    identifier: str,
    reported_line: int | None,
    decl_line: str,
) -> str | None:
    """Insert ``decl_line`` before the identifier's usage line in ``buffer``.

    Returns the new buffer, or None when a guard declines:
    - the buffer already declares ``identifier`` (present-in-resolution
      guard — no duplicate declarations);
    - the usage line cannot be located (``reported_line`` out of range or
      not naming the identifier, with no usable fallback line).

    The declaration is re-indented to the usage line's indentation so it
    lands inside the usage's block, not at column 0.
    """
    pat = _decl_pattern(identifier)
    for line in buffer.splitlines():
        if pat.match(line) and line.rstrip().endswith(";"):
            return None  # already declared in the resolution — no injection

    lines = buffer.splitlines()
    target: int | None = None
    if reported_line is not None and 1 <= reported_line <= len(lines):
        idx = reported_line - 1
        if re.search(r"\b" + re.escape(identifier) + r"\b", lines[idx]):
            target = idx
    if target is None:
        # Fallback: first line that names the identifier in a non-comment
        # position. Keeps the arm usable when the diagnostic's path prefix
        # shifts line numbers (coherence repairs rewrote the buffer).
        for i, line in enumerate(lines):
            s = line.strip()
            if s.startswith("//") or s.startswith("*"):
                continue
            if re.search(r"\b" + re.escape(identifier) + r"\b", line):
                target = i
                break
    if target is None:
        return None

    usage = lines[target]
    indent = usage[: len(usage) - len(usage.lstrip())]  # verbatim whitespace
    lines.insert(target, indent + decl_line.lstrip())
    return "\n".join(lines) + ("\n" if buffer.endswith("\n") else "")

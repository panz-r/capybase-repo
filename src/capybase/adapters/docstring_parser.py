"""Docstring parameter parser + function-signature extractor (Part R, §9).

Provides the two halves of the DOC_SIGNATURE_MISMATCH verifier:

1. :func:`enumerate_function_signatures` — extracts ``(name, [params])`` for
   every function/method in a Python file via :mod:`ast`. The canonical source
   for docstring-position detection (the same ast-walk pattern K3's
   :func:`enumerate_docstring_spans` uses).
2. :func:`parse_docstring_params` — parses Sphinx reST (``:param x:``), Google
   (``Args:\\n    x:``), and NumPy (``Parameters\\n----\\nx :``) docstring
   formats into a :class:`DocstringInfo` with ``params``, ``returns``, ``raises``.

Non-Python languages yield empty results (graceful degradation — Rust rustdoc
has no structured param convention, so the verifier is a no-op there and the
LLM jury would be the right tool for that case).

Pure functions, no I/O, no LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class DocstringInfo:
    """The parsed parameter/return/raise documentation from a docstring."""
    params: set[str] = field(default_factory=set)
    returns: bool = False
    raises: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# R1 — Python signature extraction via ast
# ---------------------------------------------------------------------------


def enumerate_function_signatures(
    text: str, lang: str | None = None,
) -> list[tuple[str, list[str]]]:
    """Extract ``(function_name, [param_names])`` for every function/method.

    Uses Python's :mod:`ast` module — the canonical source for parameter lists.
    Returns signatures in source order. Non-Python languages yield ``[]``.
    Syntax errors also yield ``[]`` (the verifier never breaks on unparseable
    input).

    The param list includes ``self``/``cls`` (the function node's real args);
    the verifier filters conventionally-excluded names when comparing against
    documented params.
    """
    if lang not in ("python", "py"):
        return []
    try:
        import ast
    except ImportError:  # pragma: no cover — ast is stdlib
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    out: list[tuple[str, list[str]]] = []

    def _visit(node) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
            params = _extract_arg_names(node.args)
            out.append((name, params))
        for child in ast.iter_child_nodes(node):
            _visit(child)

    _visit(tree)
    return out


def _extract_arg_names(args) -> list[str]:
    """Extract all parameter names from an ``ast.arguments`` node.

    Covers: positional-only, regular args, ``*args``, keyword-only, ``**kwargs``.
    Defaults and annotations are discarded (we only need names for the
    doc-matching check).
    """
    names: list[str] = []
    # Positional-only params (Python 3.8+).
    for a in getattr(args, "posonlyargs", []) or []:
        names.append(a.arg)
    # Regular positional params.
    for a in args.args or []:
        names.append(a.arg)
    # *args.
    if args.vararg is not None:
        names.append(args.vararg.arg)
    # Keyword-only params.
    for a in args.kwonlyargs or []:
        names.append(a.arg)
    # **kwargs.
    if args.kwarg is not None:
        names.append(args.kwarg.arg)
    return names


#: Conventionally-excluded param names — these are almost never documented but
#: appear in the function node's args. Filtering them avoids spurious
#: "undocumented param" mismatches.
_EXCLUDED_PARAMS = frozenset({"self", "cls"})


def signature_params_for_enclosing(
    text: str, lang: str | None, comment_byte_offset: int,
) -> set[str]:
    """The params of the function whose body contains ``comment_byte_offset``.

    Used by the DOC_SIGNATURE_MISMATCH verifier: given a docstring/comment at a
    byte offset, find its enclosing function and return that function's param
    names (excluding ``self``/``cls``). Returns an empty set when the offset
    isn't inside a function or the language is unsupported.
    """
    if lang not in ("python", "py"):
        return set()
    try:
        import ast
    except ImportError:  # pragma: no cover
        return set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()

    comment_line = text[:comment_byte_offset].count("\n") + 1  # 1-based

    def _find(node) -> set[str] | None:
        # Walk depth-first; return the innermost function containing the line.
        best: set[str] | None = None
        for child in ast.iter_child_nodes(node):
            inner = _find(child)
            if inner is not None:
                best = inner
        # Check this node itself.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = node.lineno
            end = node.end_lineno or node.lineno
            if start <= comment_line <= end:
                params = {n for n in _extract_arg_names(node.args)
                          if n not in _EXCLUDED_PARAMS}
                # The innermost function found in children wins; only use this
                # node's params if no child function contained the line.
                if best is None:
                    return params
        return best

    result = _find(tree)
    return result or set()


# ---------------------------------------------------------------------------
# R3 — Docstring param parser (Sphinx / Google / NumPy)
# ---------------------------------------------------------------------------

#: Sphinx reST: ``:param foo:``, ``:parameter foo:``, ``:arg foo:``.
_SPHINX_PARAM_RE = re.compile(
    r"^\s*:(?:param|parameter|arg|argument|keyword|key)\s+(\w+)\s*:",
    re.MULTILINE,
)
#: Sphinx reST: ``:returns:``, ``:return:``, ``:yields:``, ``:yield:``.
_SPHINX_RETURNS_RE = re.compile(
    r"^\s*:(?:returns?|yields?)\s*:", re.MULTILINE,
)
#: Sphinx reST: ``:raises ValueError:``.
_SPHINX_RAISES_RE = re.compile(
    r"^\s*:(?:raises?|excepts?|exceptions?)\s+(\w+)\s*:", re.MULTILINE,
)

#: Google-style section headers.
_GOOGLE_SECTION_RE = re.compile(r"^(\s*)(Args|Arguments|Parameters|Returns|Yields|Raises):\s*$", re.MULTILINE)

#: NumPy section header + underline (e.g. ``Parameters\n----------``).
_NUMPY_SECTION_RE = re.compile(r"^(\s*)(\w[\w ]*)\s*\n\1([-=]{2,})\s*$", re.MULTILINE)

#: A documented param name in Google-style (``name``, ``name (type)``, ``name:``).
_GOOGLE_PARAM_RE = re.compile(r"^\s+(\*{0,2}\w+)\s*(?:\([^)]*\))?\s*:")

#: A documented param name in NumPy-style (``name : type``). The leading
#: whitespace is optional — NumPy params can be at the section's base indent.
_NUMPY_PARAM_RE = re.compile(r"^\s*(\*{0,2}\w+)\s*:\s*\S")


def parse_docstring_params(
    text: str, lang: str | None = None,
) -> DocstringInfo:
    """Parse parameter/return/raise documentation from a docstring.

    Handles the three common Python docstring conventions (Sphinx reST, Google,
    NumPy). Non-Python languages yield an empty :class:`DocstringInfo` (Rust
    rustdoc has no structured param convention — the verifier is a no-op there).

    The parser is line-oriented and conservative: ambiguous content yields
    empty results rather than false positives. A docstring may use ONE
    convention; mixed conventions are not specially handled (the first match
    wins, which in practice is fine because real docstrings don't mix).
    """
    if lang not in ("python", "py"):
        return DocstringInfo()
    if not text:
        return DocstringInfo()

    info = DocstringInfo()

    # Sphinx reST — the easiest to detect (:param foo: is unambiguous).
    if _SPHINX_PARAM_RE.search(text):
        info.params = {m.group(1) for m in _SPHINX_PARAM_RE.finditer(text)}
        info.returns = bool(_SPHINX_RETURNS_RE.search(text))
        info.raises = {m.group(1) for m in _SPHINX_RAISES_RE.finditer(text)}
        return info

    # Google style — detect a section header, then parse indented entries.
    google_match = _GOOGLE_SECTION_RE.search(text)
    if google_match:
        _parse_google_sections(text, info)
        return info

    # NumPy style — detect a header + underline.
    numpy_match = _NUMPY_SECTION_RE.search(text)
    if numpy_match:
        _parse_numpy_sections(text, info)
        return info

    # No recognized convention — empty (no false positives).
    return info


def _parse_google_sections(text: str, info: DocstringInfo) -> None:
    """Parse Google-style Args:/Returns:/Raises: sections."""
    # Split into lines; walk section by section.
    lines = text.split("\n")
    current_section: str | None = None
    section_indent = 0
    for line in lines:
        m = _GOOGLE_SECTION_RE.match(line)
        if m:
            indent = len(m.group(1))
            current_section = m.group(2)
            section_indent = indent
            continue
        if current_section is None:
            continue
        # An entry under the current section: indented more than the header.
        stripped = line.strip()
        if not stripped:
            continue
        if not line.startswith(" " * (section_indent + 1)):
            # Dedented back to or past the header → end of section.
            current_section = None
            continue
        if current_section in ("Args", "Arguments", "Parameters"):
            pm = _GOOGLE_PARAM_RE.match(line)
            if pm:
                name = pm.group(1).lstrip("*")
                if name:
                    info.params.add(name)
        elif current_section in ("Returns", "Yields"):
            info.returns = True
        elif current_section == "Raises":
            # First word on the line is the exception name (optionally with `:`).
            first = stripped.split(":")[0].split("(")[0].strip()
            if first and first[0].isalpha():
                info.raises.add(first)


def _parse_numpy_sections(text: str, info: DocstringInfo) -> None:
    """Parse NumPy-style Parameters/Returns/Raises sections.

    A NumPy section is a header line immediately followed by an underline of
    ``-`` or ``=``. The body is indented (continuation lines under each entry).
    We split the text into (header, body) pairs and dispatch each to the
    matching consumer.
    """
    lines = text.split("\n")
    # First, find all header positions (a line followed by an underline).
    header_positions: list[tuple[int, str]] = []  # (line_idx, header_text)
    for i in range(len(lines) - 1):
        header = lines[i].strip()
        underline = lines[i + 1].strip()
        if (header and underline
                and set(underline) <= {"-", "="}
                and len(underline) >= 2
                and not lines[i].startswith(" ")):  # header at col 0
            header_positions.append((i, header))
    # For each header, the body is from i+2 until the next header (or EOF).
    for idx, (pos, header) in enumerate(header_positions):
        body_start = pos + 2
        body_end = header_positions[idx + 1][0] if idx + 1 < len(header_positions) else len(lines)
        body = lines[body_start:body_end]
        _consume_numpy_body(header, body, info)


def _consume_numpy_body(header: str, body: list[str], info: DocstringInfo) -> None:
    """Parse the body lines of a NumPy section."""
    h = header.lower()
    if "parameter" in h or "arg" in h:
        # Each entry is ``name : type`` possibly followed by indented description.
        # Only lines that look like ``name : type`` (indented, identifier-first)
        # are params; indented description lines are not.
        for ln in body:
            pm = _NUMPY_PARAM_RE.match(ln)
            if pm:
                name = pm.group(1).lstrip("*")
                if name:
                    info.params.add(name)
    elif "return" in h or "yield" in h:
        if any(ln.strip() for ln in body):
            info.returns = True
    elif "raise" in h or "except" in h:
        # Each entry is an exception name at col 0 (within the section), possibly
        # followed by an indented description. Only non-indented alpha-first lines.
        for ln in body:
            stripped = ln.strip()
            if not stripped or ln.startswith(" "):
                continue  # blank or indented (description) — skip
            first = stripped.split(":")[0].split("(")[0].strip()
            if first and first[0].isalpha():
                info.raises.add(first)


__all__ = [
    "DocstringInfo",
    "enumerate_function_signatures",
    "signature_params_for_enclosing",
    "parse_docstring_params",
]

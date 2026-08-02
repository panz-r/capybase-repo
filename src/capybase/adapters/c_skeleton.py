"""Lightweight C skeleton extractor — entity names without a full parser.

A depth-tracking token scanner that extracts top-level C entities (includes,
defines, typedefs, structs, functions, globals) for prompt context. Not a C
parser — it's a pragmatic structural index, the same spirit as ctags but
self-contained. Designed for the "file skeleton block" in oversized-file
prompts: gives the model global entity awareness (~300-500 tokens) without
the whole file.

Principle: parse enough structure to choose context. Let the compiler decide
correctness.

Handles:
- ``//`` and ``/* */`` comments (stripped)
- String/char literals (masked — braces/parens inside don't count)
- Line continuations (``\\`` at EOL — joined before scanning)
- Preprocessor directives (isolated — macro bodies don't affect brace depth)
- ``#include``, ``#define``, ``typedef``, ``struct/union/enum``,
  function definitions/declarations, global variables

Does NOT handle (safely degrades to "skip"):
- Macro-generated code, X-macros
- C++ templates/classes/namespaces
- Full declarator grammar (function pointers, K&R params)
- Expression/statement parsing inside function bodies
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Token model
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"(?P<ws>\s+)"
    r"|(?P<line_comment>//[^\n]*)"
    r"|(?P<block_comment>/\*[\s\S]*?\*/)"
    r"|(?P<string>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')"
    r"|(?P<pp_line>\#[^\n]*)"
    r"|(?P<ident>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<num>0[xX][0-9a-fA-F]+|\d+)"
    r"|(?P<punct>[{}()\[\];,#=<>+\-*/&|!~^%?.:])"
    r"|(?P<other>\S)"
)


@dataclass
class _Token:
    kind: str   # ident, num, punct, pp_line, other
    text: str


def _tokenize(text: str) -> list[_Token]:
    """Tokenize C source into a flat token list.

    Comments and whitespace are stripped. String/char literals are masked
    (replaced with a placeholder so their content doesn't affect depth
    tracking). Preprocessor lines are emitted as single ``pp_line`` tokens.
    """
    # Join line continuations: replace ``\<newline>`` with a space so a
    # multi-line #define or statement becomes one logical line.
    text = re.sub(r"\\\n", " ", text)
    tokens: list[_Token] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            pos += 1
            continue
        pos = m.end()
        kind = m.lastgroup
        val = m.group()
        if kind in ("ws", "line_comment", "block_comment"):
            continue
        if kind == "string":
            tokens.append(_Token("punct", '"'))  # mask content as empty string
            continue
        if kind == "pp_line":
            tokens.append(_Token("pp_line", val))
            continue
        if kind == "ident":
            tokens.append(_Token("ident", val))
        elif kind == "num":
            tokens.append(_Token("num", val))
        elif kind == "punct":
            tokens.append(_Token("punct", val))
        else:
            tokens.append(_Token("other", val))
    return tokens


# ---------------------------------------------------------------------------
# Depth-tracking scanner
# ---------------------------------------------------------------------------

_C_TYPE_KEYWORDS = frozenset({
    "void", "char", "short", "int", "long", "float", "double",
    "signed", "unsigned", "_Bool", "const", "volatile", "static",
    "extern", "register", "auto", "inline", "struct", "union", "enum",
})

_C_CONTROL_KEYWORDS = frozenset({
    "if", "else", "for", "while", "do", "switch", "case", "default",
    "break", "continue", "return", "goto", "sizeof", "typeof",
})


def _extract_skeleton_from_tokens(tokens: list[_Token]) -> "SkeletonResult":
    """Walk tokens at brace_depth=0, classifying top-level declarations."""
    includes: list[str] = []
    macros: list[str] = []
    typedefs: list[str] = []
    structs: list[str] = []
    functions: list[str] = []
    globals_: list[str] = []

    i = 0
    n = len(tokens)
    brace_depth = 0
    paren_depth = 0
    bracket_depth = 0
    # extern "C" { ... } tracking: C headers wrap their API in this block for
    # C++ interop. The { opens a brace that swallows ALL function declarations
    # inside — the scanner enters brace_depth=1 and skips everything until the
    # matching }. We detect the pattern and skip both braces so the declarations
    # inside are scanned at depth 0 (where they belong). The design document
    # (§8.10) calls this out: "Do not let it confuse function name extraction."
    extern_c_depth = 0  # >0 means we're inside an extern "C" { ... } scope
    # Declaration buffer: list of (token, all_tokens_index) at depth 0.
    buf: list[tuple[_Token, int]] = []

    while i < n:
        tok = tokens[i]

        # Skip preprocessor lines (they don't affect code depth).
        if tok.kind == "pp_line":
            if brace_depth == 0 and paren_depth == 0 and bracket_depth == 0:
                _classify_preprocessor(tok.text, includes, macros)
            i += 1
            continue

        if tok.kind != "punct":
            if brace_depth == 0 and paren_depth == 0 and bracket_depth == 0:
                buf.append((tok, i))
            i += 1
            continue

        # Punctuation token.
        if tok.text == "{":
            # Detect extern "C" { (or extern "C++" {): C headers wrap their
            # API in this for C++ interop. If the buffer ends with the extern
            # "C" pattern, skip this brace so declarations inside are scanned
            # at depth 0. The matching } is skipped via extern_c_depth below.
            # Note: the tokenizer masks string contents to '"', so "C" becomes
            # just a '"' punct token — the pattern is: extern (ident) + " (punct).
            if (
                brace_depth == 0
                and extern_c_depth == 0
                and paren_depth == 0
                and bracket_depth == 0
                and len(buf) >= 2
            ):
                # Last two tokens in buffer: 'extern' (ident) + '"' (punct, masked string).
                last_tok = buf[-1][0]
                prev_tok = buf[-2][0]
                if (
                    last_tok.kind == "punct" and last_tok.text == '"'
                    and prev_tok.kind == "ident" and prev_tok.text == "extern"
                ):
                    # This is extern "C" { — skip the brace, don't enter depth.
                    extern_c_depth = 1
                    buf = []  # discard the extern "C" tokens
                    i += 1
                    continue
            if brace_depth == 0 and paren_depth == 0 and bracket_depth == 0:
                _classify_buffer(buf, tokens, "{",
                                 functions, structs, typedefs, globals_)
                buf = []
            brace_depth += 1
            i += 1
            continue

        if tok.text == "}":
            # If we're in an extern "C" scope and this closes it, skip the brace.
            if extern_c_depth > 0 and brace_depth == 0:
                extern_c_depth = 0
                i += 1
                continue
            brace_depth = max(0, brace_depth - 1)
            i += 1
            continue

        if tok.text == "(":
            if brace_depth == 0:
                paren_depth += 1
                buf.append((tok, i))
            else:
                paren_depth += 1
            i += 1
            continue

        if tok.text == ")":
            paren_depth = max(0, paren_depth - 1)
            if brace_depth == 0:
                buf.append((tok, i))
            i += 1
            continue

        if tok.text == "[":
            if brace_depth == 0:
                bracket_depth += 1
                buf.append((tok, i))
            else:
                bracket_depth += 1
            i += 1
            continue

        if tok.text == "]":
            bracket_depth = max(0, bracket_depth - 1)
            if brace_depth == 0:
                buf.append((tok, i))
            i += 1
            continue

        if tok.text == ";":
            if brace_depth == 0 and paren_depth == 0 and bracket_depth == 0:
                buf.append((tok, i))
                _classify_buffer(buf, tokens, ";",
                                 functions, structs, typedefs, globals_)
                buf = []
            i += 1
            continue

        # Other punctuation at depth 0.
        if brace_depth == 0 and paren_depth == 0 and bracket_depth == 0:
            buf.append((tok, i))
        i += 1

    # Handle trailing buffer (file without final ; — incomplete).
    if buf:
        _classify_buffer(buf, tokens, "",
                         functions, structs, typedefs, globals_)

    return SkeletonResult(
        includes=includes, macros=macros, typedefs=typedefs,
        structs=structs, functions=functions, globals=globals_,
    )


def _classify_preprocessor(
    line: str,
    includes: list[str],
    macros: list[str],
) -> None:
    """Classify a ``#`` preprocessor line."""
    stripped = line.strip()
    if stripped.startswith("#include"):
        # Extract the include path.
        rest = stripped[len("#include"):].strip()
        if rest.startswith("<") and rest.endswith(">"):
            includes.append(rest[1:-1])
        elif rest.startswith('"') and rest.endswith('"'):
            includes.append(rest[1:-1])
        elif rest:
            includes.append(rest)
    elif stripped.startswith("#define"):
        rest = stripped[len("#define"):].strip()
        # Macro name is the first identifier.
        m = re.match(r"([A-Za-z_]\w*)", rest)
        if m:
            name = m.group(1)
            # Check if function-like: name immediately followed by (.
            after = rest[m.end():]
            if after.startswith("("):
                macros.append(f"{name}(...)")
            else:
                macros.append(name)


def _classify_buffer(
    buf: list[tuple[_Token, int]],
    all_tokens: list[_Token],
    terminator: str,
    functions: list[str],
    structs: list[str],
    typedefs: list[str],
    globals_: list[str],
) -> None:
    """Classify a declaration buffer (tokens at depth 0 ending in ; or {)."""
    if not buf:
        return

    buf_tokens = [t for t, _ in buf]

    # Skip if buffer is just punctuation (e.g., stray ;).
    idents = [t for t in buf_tokens if t.kind == "ident"]
    if not idents:
        return

    # Check for typedef.
    if idents[0].text == "typedef":
        # Function-pointer typedef: typedef T (*name)(...);
        # The name is inside the (*name) group — it's NOT in the buffer's
        # idents list (the scanner strips tokens inside parens). Scan the raw
        # token stream for the (*ident) pattern within this declaration's range.
        buf_start_idx = buf[0][1] if buf else 0
        buf_end_idx = buf[-1][1] if buf else 0
        for k in range(buf_start_idx, min(buf_end_idx + 1, len(all_tokens) - 3)):
            if (all_tokens[k].kind == "punct" and all_tokens[k].text == "("
                    and all_tokens[k + 1].kind == "punct"
                    and all_tokens[k + 1].text == "*"
                    and all_tokens[k + 2].kind == "ident"
                    and all_tokens[k + 3].kind == "punct"
                    and all_tokens[k + 3].text == ")"):
                fp_name = all_tokens[k + 2].text
                if fp_name not in typedefs:
                    typedefs.append(fp_name)
                return
        # Simple typedef: last identifier before the terminator is the alias.
        name = idents[-1].text if idents[-1].text not in _C_TYPE_KEYWORDS else None
        if name:
            typedefs.append(name)
        # Also check for struct/union/enum tag inside.
        for j, t in enumerate(idents):
            if t.text in ("struct", "union", "enum"):
                if j + 1 < len(idents) and idents[j + 1].text not in _C_TYPE_KEYWORDS:
                    tag = idents[j + 1].text
                    if tag not in structs and tag != name:
                        structs.append(tag)
                break
        return

    # Check for struct/union/enum definition.
    for j, t in enumerate(idents):
        if t.text in ("struct", "union", "enum") and j + 1 < len(idents):
            next_ident = idents[j + 1].text
            if next_ident not in _C_TYPE_KEYWORDS and next_ident not in _C_CONTROL_KEYWORDS:
                if terminator == "{":
                    # struct Foo { ... } — definition with body.
                    if next_ident not in structs:
                        structs.append(next_ident)
                    return
                # Forward declaration: struct Foo; — don't record (too noisy).

    # Find a function pattern: identifier followed by ( at depth 0 in the buffer.
    # Walk the buffer tracking paren depth.
    p_depth = 0
    for j in range(len(buf_tokens)):
        tok = buf_tokens[j]
        if tok.kind == "punct" and tok.text == "(":
            if p_depth == 0 and j > 0 and buf_tokens[j - 1].kind == "ident":
                name_tok = buf_tokens[j - 1]
                # Reject control keywords (if, while, for, switch, sizeof...).
                if name_tok.text in _C_CONTROL_KEYWORDS:
                    continue
                # Reject if it's just a type keyword followed by ( (cast).
                if name_tok.text in _C_TYPE_KEYWORDS:
                    p_depth += 1
                    continue
                # Found a function name! Extract params from all_tokens.
                # buf[j] = ( ( token, _ ) ; the stored index is the position
                # in all_tokens where this ( appears.
                _, paren_raw_idx = buf[j]
                param_parts: list[str] = []
                pd = 1
                pk = paren_raw_idx + 1
                while pk < len(all_tokens) and pd > 0:
                    pt = all_tokens[pk]
                    if pt.kind == "pp_line":
                        pk += 1
                        continue
                    if pt.text == "(":
                        pd += 1
                        param_parts.append("(")
                    elif pt.text == ")":
                        pd -= 1
                        if pd > 0:
                            param_parts.append(")")
                    else:
                        if param_parts and param_parts[-1] not in ("(", ","):
                            param_parts.append(" ")
                        param_parts.append(pt.text)
                    pk += 1
                params = "".join(param_parts)
                # Collapse: no space before commas, single space after, and
                # fold adjacent pointer stars (`* *` -> `**`) since the
                # inter-token spacer can split a declarator's stars.
                params = re.sub(r"\s*([,()])\s*", lambda m: m.group(1), params).strip()
                params = re.sub(r"\*\s+\*", "**", params)
                params = re.sub(r"\s*,\s*", ", ", params)
                params = re.sub(r"\s+", " ", params)
                if params:
                    functions.append(f"{name_tok.text}({params})")
                else:
                    functions.append(f"{name_tok.text}(void)")
                return  # only record the first function per buffer
            p_depth += 1
        elif tok.kind == "punct" and tok.text == ")":
            p_depth = max(0, p_depth - 1)

    # Not a function — check for global variable.
    # The last identifier (before ; or =) that's not a type keyword.
    for t in reversed(idents):
        if t.text not in _C_TYPE_KEYWORDS and t.text not in _C_CONTROL_KEYWORDS:
            globals_.append(t.text)
            return


# ---------------------------------------------------------------------------
# Result type + rendering
# ---------------------------------------------------------------------------


@dataclass
class SkeletonResult:
    """Extracted top-level entity names from a C file."""
    includes: list[str] = field(default_factory=list)
    macros: list[str] = field(default_factory=list)
    typedefs: list[str] = field(default_factory=list)
    structs: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    globals: list[str] = field(default_factory=list)

    @property
    def entity_count(self) -> int:
        return (len(self.includes) + len(self.macros) + len(self.typedefs)
                + len(self.structs) + len(self.functions) + len(self.globals))

    def render(self, max_tokens: int = 500) -> str:
        """Render a compact skeleton block for the LLM prompt.

        Each line is one entity, grouped by category. Duplicate names within
        a category are collapsed (a real file commonly re-#includes or
        re-defines); the first occurrence's position is preserved. Truncated
        when the estimated token count (chars/4) exceeds ``max_tokens``.
        """
        if self.entity_count == 0:
            return ""
        max_chars = max_tokens * 4
        lines: list[str] = ["File skeleton (global declarations):"]
        char_count = len(lines[0])

        def add_category(label: str, items: list[str]) -> bool:
            nonlocal char_count
            if not items:
                return True
            # Order-preserving dedup. Function signatures vary by params, so
            # we dedup on the name-before-paren to keep overloads distinct.
            seen: set[str] = set()
            uniq: list[str] = []
            for it in items:
                key = it.split("(", 1)[0] if "(" in it else it
                if key in seen:
                    continue
                seen.add(key)
                uniq.append(it)
            shown = uniq if len(uniq) <= 30 else uniq[:30] + [f"... ({len(uniq) - 30} more)"]
            line = f"  {label}: {', '.join(shown)}"
            if char_count + len(line) > max_chars:
                return False
            lines.append(line)
            char_count += len(line) + 1
            return True

        if not add_category("Includes", self.includes): return "\n".join(lines)
        if not add_category("Macros", self.macros): return "\n".join(lines)
        if not add_category("Types", self.typedefs): return "\n".join(lines)
        if not add_category("Structs/Enums", self.structs): return "\n".join(lines)
        if not add_category("Functions", self.functions): return "\n".join(lines)
        if not add_category("Globals", self.globals): return "\n".join(lines)

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_skeleton(text: str) -> SkeletonResult:
    """Extract the top-level entity skeleton from a C source file.

    Returns a :class:`SkeletonResult` with includes, macros, typedefs,
    structs, functions, and globals. Never raises — degrades gracefully
    to an empty result on any parsing failure.
    """
    if not text or not text.strip():
        return SkeletonResult()
    try:
        tokens = _tokenize(text)
        return _extract_skeleton_from_tokens(tokens)
    except Exception:  # noqa: BLE001 — skeleton is advisory; never crash
        return SkeletonResult()

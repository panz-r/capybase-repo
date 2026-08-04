"""Lightweight C++ skeleton extractor — entity names without a full parser.

A depth-tracking token scanner that extracts top-level C++ entities (includes,
defines, namespaces, classes with their method signatures, templates, free
functions, globals) for prompt context. Not a C++ parser — it's a pragmatic
structural index, the same spirit as ctags but self-contained. Designed for the
"file skeleton block" in oversized-file prompts: gives the model global entity
awareness (~300-500 tokens) without the whole file.

Reuses the tokenizer (``_tokenize``), the result type (``SkeletonResult`` +
``render``), and the preprocessor classifier (``_classify_preprocessor``) from
``c_skeleton`` — these are language-agnostic. This module adds the three
extensions C++ needs over C:

1. **``namespace`` transparency** — a namespace opens a transparent scope (like
   ``extern "C"``): the ``{`` is skipped so declarations inside surface at
   brace_depth 0. Nested namespaces are tracked via a depth counter.
2. **``class``/``struct`` body descent** — a class definition's members
   (methods) are *inside* the braces. The scanner descends into the body and
   captures method signatures as ``ClassName::MethodName(params)``. Fields and
   access specifiers (``public:``/``private:``) are skipped.
3. **``template<...>`` prefix skipping** — the angle brackets aren't ``{}``/
   ``()``/``[]`` so they don't affect the depth tracker. The scanner strips a
   leading ``template < ... >`` before classifying the real declaration beneath.

Principle: parse enough structure to choose context. Let the compiler decide
correctness. Never raises — degrades gracefully to an empty result.
"""

from __future__ import annotations

import re

from capybase.adapters.c_skeleton import (
    SkeletonResult,
    _C_CONTROL_KEYWORDS,
    _C_TYPE_KEYWORDS,
    _classify_preprocessor,
    _tokenize,
)


# ---------------------------------------------------------------------------
# C++ keyword sets
# ---------------------------------------------------------------------------

_CPP_TYPE_KEYWORDS = _C_TYPE_KEYWORDS | frozenset({
    "class", "template", "typename", "namespace", "using",
    "virtual", "override", "final", "constexpr", "noexcept",
    "nullptr", "auto", "decltype", "explicit", "mutable",
})

_CPP_ACCESS_KEYWORDS = frozenset({"public", "private", "protected"})

# Container keywords that open a transparent scope (skip the brace so members
# surface at depth 0). Mirrors the C scanner's extern "C" handling.
_CPP_NAMESPACE_KEYWORDS = frozenset({"namespace"})

# Type-definition keywords whose body we descend into to extract methods.
_CPP_CLASS_KEYWORDS = frozenset({"class", "struct", "union"})

# Operator-overload detection: ``operator`` followed by punctuation.
_OPERATOR_PUNCT = frozenset("+-*/%=<>!&|^~,.()[]")


def _is_operator_name(tokens: list, start: int) -> tuple[bool, str]:
    """Check if tokens[start:] is an ``operator<symbol>`` and return the name.

    Returns ``(True, "operator+"``) when the token at ``start`` is the ident
    ``operator`` and the next token(s) form an overloadable operator. Otherwise
    ``(False, "")``. Handles multi-char operators (``<<``, ``()``, ``[]``).
    """
    if start >= len(tokens):
        return False, ""
    t = tokens[start]
    if t.kind != "ident" or t.text != "operator":
        return False, ""
    # Collect the operator symbol from the following punctuation tokens.
    parts: list[str] = []
    k = start + 1
    while k < len(tokens) and tokens[k].kind == "punct" and tokens[k].text in _OPERATOR_PUNCT:
        parts.append(tokens[k].text)
        k += 1
        # ``()`` and ``[]`` are two-token operators; stop after the closing half.
        if len(parts) == 2 and parts[0] in ("(", "["):
            break
    if not parts:
        # ``operator new`` / ``operator delete`` — keyword operators.
        if k < len(tokens) and tokens[k].kind == "ident" and tokens[k].text in ("new", "delete"):
            return True, f"operator {tokens[k].text}"
        return False, ""
    return True, "operator" + "".join(parts)


# ---------------------------------------------------------------------------
# Class-body member extraction
# ---------------------------------------------------------------------------

def _extract_class_members(
    tokens: list,
    start: int,
    end: int,
    class_name: str,
    functions: list[str],
) -> int:
    """Scan a class/struct body [start, end) for method declarations.

    Captures method signatures as ``ClassName::MethodName(params)``. Skips
    fields (``Type field;`` — too noisy for a skeleton), access specifiers
    (``public:`` etc.), and nested type definitions (they're handled by the
    outer scanner when the depth tracker sees their ``{``).

    Returns the index past the matching ``}`` that closes the class body.
    """
    i = start
    brace_depth = 0
    paren_depth = 0
    buf: list = []

    while i < end:
        tok = tokens[i]

        # Access specifiers: skip (they're labels, not declarations).
        if tok.kind == "ident" and tok.text in _CPP_ACCESS_KEYWORDS:
            # Expect a ':' next; skip both.
            if i + 1 < end and tokens[i + 1].kind == "punct" and tokens[i + 1].text == ":":
                i += 2
                buf = []
                continue

        if tok.kind == "punct":
            if tok.text == "{":
                brace_depth += 1
                buf = []
                i += 1
                continue
            if tok.text == "}":
                if brace_depth == 0:
                    # This is the class's closing brace.
                    return i + 1
                brace_depth -= 1
                buf = []
                i += 1
                continue
            if tok.text == "(":
                paren_depth += 1
                buf.append((tok, i))
                i += 1
                continue
            if tok.text == ")":
                paren_depth = max(0, paren_depth - 1)
                buf.append((tok, i))
                i += 1
                continue
            if tok.text == ";":
                if brace_depth == 0 and paren_depth == 0:
                    _classify_member(buf, tokens, class_name, functions)
                    buf = []
                i += 1
                continue

        if brace_depth == 0 and paren_depth == 0:
            buf.append((tok, i))
        i += 1

    return end


def _classify_member(
    buf: list,
    all_tokens: list,
    class_name: str,
    functions: list[str],
) -> None:
    """Classify a member declaration buffer inside a class body.

    Captures methods (``Type name(params)``) as ``Class::name(params)``. Skips
    fields (no parens = a field declaration, not a method).
    """
    if not buf:
        return
    buf_tokens = [t[0] if isinstance(t, tuple) else t for t in buf]
    idents = [t for t in buf_tokens if t.kind == "ident"]
    if not idents:
        return

    # Skip constructors/destructors: name == class_name (or ~class_name).
    # These ARE methods but their signature is just ClassName(params) — capture
    # them as ClassName::ClassName(params) / ClassName::~ClassName(params).

    # Look for the function pattern: ident followed by ( at depth 0.
    p_depth = 0
    for j in range(len(buf_tokens)):
        tok = buf_tokens[j]
        if tok.kind == "punct" and tok.text == "(":
            if p_depth == 0 and j > 0:
                name_tok = buf_tokens[j - 1]
                # Skip type keywords and access specifiers.
                if name_tok.text in _CPP_TYPE_KEYWORDS or name_tok.text in _CPP_ACCESS_KEYWORDS:
                    p_depth += 1
                    continue
                # Extract the method name (handle operator overloads).
                is_op, op_name = _is_operator_name(buf_tokens, j - 1)
                if is_op:
                    method_name = op_name
                elif name_tok.kind == "ident":
                    method_name = name_tok.text
                else:
                    p_depth += 1
                    continue
                # Extract params from the raw token stream.
                _, paren_raw_idx = buf[j] if isinstance(buf[j], tuple) else (None, 0)
                params = _extract_params(all_tokens, paren_raw_idx) if paren_raw_idx else ""
                qualified = f"{class_name}::{method_name}({params})"
                if qualified not in functions:
                    functions.append(qualified)
                return
            p_depth += 1
        elif tok.kind == "punct" and tok.text == ")":
            p_depth = max(0, p_depth - 1)


def _extract_params(all_tokens: list, paren_idx: int) -> str:
    """Extract parameter text from the raw token stream starting at ``paren_idx``
    (the position of the opening ``(``)."""
    param_parts: list[str] = []
    pd = 1
    pk = paren_idx + 1
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
    params = re.sub(r"\s*([,()])\s*", lambda m: m.group(1), params).strip()
    params = re.sub(r"\*\s+\*", "**", params)
    params = re.sub(r"\s*,\s*", ", ", params)
    params = re.sub(r"\s+", " ", params)
    return params


# ---------------------------------------------------------------------------
# C++ depth-tracking scanner
# ---------------------------------------------------------------------------

def _extract_cpp_skeleton_from_tokens(tokens: list) -> SkeletonResult:
    """Walk tokens tracking C++ scopes, classifying declarations."""
    includes: list[str] = []
    macros: list[str] = []
    typedefs: list[str] = []
    structs: list[str] = []
    functions: list[str] = []
    globals_: list[str] = []
    usings: list[str] = []

    i = 0
    n = len(tokens)
    brace_depth = 0
    paren_depth = 0
    bracket_depth = 0
    # Namespace transparency: namespaces open a scope whose members surface at
    # brace_depth 0. Track how many namespace braces we've skipped.
    namespace_depth = 0
    # extern "C" { } tracking (same as C scanner — C++ headers use it too).
    extern_c_depth = 0
    # template<> prefix: when seen, skip the angle-bracket contents before
    # classifying the real declaration beneath.
    in_template_prefix = False
    template_angle_depth = 0
    # Declaration buffer.
    buf: list = []

    while i < n:
        tok = tokens[i]

        # template<...> prefix skipping: accumulate nothing until the closing >.
        if in_template_prefix:
            if tok.kind == "punct":
                if tok.text == "<":
                    template_angle_depth += 1
                elif tok.text == ">":
                    template_angle_depth -= 1
                    if template_angle_depth <= 0:
                        in_template_prefix = False
                        template_angle_depth = 0
                # ``>>`` can close two levels (e.g. vector<vector<int>>).
                elif tok.text == ">>" and template_angle_depth >= 2:
                    template_angle_depth -= 2
                elif tok.text == ">=":
                    if template_angle_depth >= 1:
                        template_angle_depth -= 1
                        if template_angle_depth <= 0:
                            in_template_prefix = False
                            template_angle_depth = 0
            i += 1
            continue

        # Detect template keyword to enter prefix-skipping mode.
        if (
            tok.kind == "ident"
            and tok.text == "template"
            and brace_depth == 0
            and paren_depth == 0
            and bracket_depth == 0
        ):
            in_template_prefix = True
            template_angle_depth = 0
            i += 1
            continue

        # Preprocessor lines.
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

        # Punctuation.
        if tok.text == "{":
            # Detect namespace Name { — transparent scope, skip the brace.
            # Works at any namespace_depth (nested namespaces: namespace a {
            # namespace b { ... } }).
            if (
                brace_depth == 0
                and extern_c_depth == 0
                and paren_depth == 0
                and bracket_depth == 0
                and _buf_starts_with_keyword(buf, _CPP_NAMESPACE_KEYWORDS)
            ):
                namespace_depth += 1
                buf = []
                i += 1
                continue
            # Detect extern "C" { — same transparent-scope technique.
            if (
                brace_depth == 0
                and extern_c_depth == 0
                and namespace_depth == 0
                and paren_depth == 0
                and bracket_depth == 0
                and len(buf) >= 2
            ):
                last_tok = buf[-1][0] if isinstance(buf[-1], tuple) else buf[-1]
                prev_tok = buf[-2][0] if isinstance(buf[-2], tuple) else buf[-2]
                if (
                    getattr(last_tok, "kind", "") == "punct" and last_tok.text == '"'
                    and getattr(prev_tok, "kind", "") == "ident" and prev_tok.text == "extern"
                ):
                    extern_c_depth = 1
                    buf = []
                    i += 1
                    continue

            if brace_depth == 0 and paren_depth == 0 and bracket_depth == 0:
                # Check for class/struct/union Name { — descend into body.
                class_name = _extract_class_name(buf)
                if class_name is not None:
                    if class_name not in structs:
                        structs.append(class_name)
                    # Descend into the class body to extract methods.
                    i = _extract_class_members(
                        tokens, i + 1, n, class_name, functions,
                    )
                    buf = []
                    continue
                # Regular brace open at depth 0 (function body, etc.).
                _classify_cpp_buffer(
                    buf, tokens, "{",
                    functions, structs, typedefs, globals_, usings,
                )
                buf = []
            brace_depth += 1
            i += 1
            continue

        if tok.text == "}":
            # Namespace or extern "C" closing brace.
            if namespace_depth > 0 and brace_depth == 0:
                namespace_depth -= 1
                i += 1
                continue
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
                _classify_cpp_buffer(
                    buf, tokens, ";",
                    functions, structs, typedefs, globals_, usings,
                )
                buf = []
            i += 1
            continue

        # Other punctuation at depth 0.
        if brace_depth == 0 and paren_depth == 0 and bracket_depth == 0:
            buf.append((tok, i))
        i += 1

    # Trailing buffer.
    if buf:
        _classify_cpp_buffer(
            buf, tokens, "",
            functions, structs, typedefs, globals_, usings,
        )

    # Fold usings into includes for rendering (they serve the same "what's
    # available" purpose and SkeletonResult has no separate usings field).
    result = SkeletonResult(
        includes=includes + usings,
        macros=macros, typedefs=typedefs, structs=structs,
        functions=functions, globals=globals_,
    )
    return result


# ---------------------------------------------------------------------------
# Buffer classification helpers
# ---------------------------------------------------------------------------

def _buf_starts_with_keyword(buf: list, keywords: frozenset) -> bool:
    """True if the buffer's FIRST identifier token is in ``keywords``.

    Used to detect ``namespace Name {`` — the keyword leads, followed by an
    optional name. The buffer at the ``{`` is ``[namespace, Name]``.
    """
    for t in buf:
        tok = t[0] if isinstance(t, tuple) else t
        if tok.kind == "ident":
            return tok.text in keywords
    return False


def _extract_class_name(buf: list) -> str | None:
    """Extract the class/struct/union name from a buffer ending before ``{``.

    Returns the name (e.g. ``Foo`` from ``class Foo``) or None if the buffer
    isn't a class/struct/union definition. Handles inheritance (``class Foo
    : public Bar``) and attributes (``class [[deprecated]] Foo``).
    """
    idents = []
    for t in buf:
        tok = t[0] if isinstance(t, tuple) else t
        if tok.kind == "ident":
            idents.append(tok.text)
    if not idents:
        return None
    # Find the class/struct/union keyword, then take the next non-keyword ident.
    for j, name in enumerate(idents):
        if name in _CPP_CLASS_KEYWORDS:
            # Skip attribute tokens and base-specifier colons; the name is the
            # next identifier that isn't a type/access keyword.
            for k in range(j + 1, len(idents)):
                if idents[k] not in _CPP_TYPE_KEYWORDS and idents[k] not in _CPP_ACCESS_KEYWORDS:
                    return idents[k]
            return None
    return None


def _classify_cpp_buffer(
    buf: list,
    all_tokens: list,
    terminator: str,
    functions: list[str],
    structs: list[str],
    typedefs: list[str],
    globals_: list[str],
    usings: list[str],
) -> None:
    """Classify a C++ declaration buffer at depth 0 ending in ; or {."""
    if not buf:
        return
    buf_tokens = [t[0] if isinstance(t, tuple) else t for t in buf]
    idents = [t for t in buf_tokens if t.kind == "ident"]
    if not idents:
        return

    # ``using`` declarations.
    if idents[0].text == "using":
        # using namespace std;  /  using std::vector;
        parts = [t.text for t in buf_tokens if t.kind in ("ident",)]
        # Reconstruct: using namespace X / using X::Y
        name_parts: list[str] = []
        for t in buf_tokens[1:]:  # skip 'using'
            if t.kind == "ident":
                name_parts.append(t.text)
            elif t.text == ":" and name_parts:
                name_parts[-1] += ":"  # scope resolution marker
            elif t.text == ":" and not name_parts:
                pass  # stray
        if name_parts:
            usings.append(" ".join(name_parts))
        return

    # typedef (reuse C logic — function-pointer typedefs work the same).
    if idents[0].text == "typedef":
        # Function-pointer typedef: scan for (*name) pattern.
        buf_start_idx = buf[0][1] if isinstance(buf[0], tuple) else 0
        buf_end_idx = buf[-1][1] if isinstance(buf[-1], tuple) else 0
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
        # Simple typedef: last non-keyword identifier.
        name = idents[-1].text if idents[-1].text not in _CPP_TYPE_KEYWORDS else None
        if name:
            typedefs.append(name)
        return

    # Check for struct/union/enum/class definition (forward decl or with body).
    for j, t in enumerate(idents):
        if t.text in ("struct", "union", "enum", "class") and j + 1 < len(idents):
            next_ident = idents[j + 1].text
            if next_ident not in _CPP_TYPE_KEYWORDS and next_ident not in _CPP_CONTROL_KEYWORDS:
                if terminator == "{":
                    if next_ident not in structs:
                        structs.append(next_ident)
                    return

    # Function pattern: ident followed by ( at depth 0.
    p_depth = 0
    for j in range(len(buf_tokens)):
        tok = buf_tokens[j]
        if tok.kind == "punct" and tok.text == "(":
            if p_depth == 0 and j > 0:
                name_tok = buf_tokens[j - 1]
                # Check for operator overload.
                is_op, op_name = _is_operator_name(buf_tokens, j - 1)
                if is_op:
                    _, paren_raw_idx = buf[j] if isinstance(buf[j], tuple) else (None, 0)
                    params = _extract_params(all_tokens, paren_raw_idx) if paren_raw_idx else ""
                    sig = f"{op_name}({params})"
                    if sig not in functions:
                        functions.append(sig)
                    return
                if name_tok.text in _C_CONTROL_KEYWORDS or name_tok.text in _CPP_TYPE_KEYWORDS:
                    p_depth += 1
                    continue
                if name_tok.kind == "ident":
                    _, paren_raw_idx = buf[j] if isinstance(buf[j], tuple) else (None, 0)
                    params = _extract_params(all_tokens, paren_raw_idx) if paren_raw_idx else ""
                    sig = f"{name_tok.text}({params})" if params else f"{name_tok.text}(void)"
                    if sig not in functions:
                        functions.append(sig)
                    return
            p_depth += 1
        elif tok.kind == "punct" and tok.text == ")":
            p_depth = max(0, p_depth - 1)

    # Global variable: last non-keyword identifier.
    for t in reversed(idents):
        if t.text not in _CPP_TYPE_KEYWORDS and t.text not in _C_CONTROL_KEYWORDS and t.text not in _CPP_ACCESS_KEYWORDS:
            globals_.append(t.text)
            return


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_skeleton(text: str) -> SkeletonResult:
    """Extract the top-level entity skeleton from a C++ source file.

    Returns a :class:`SkeletonResult` (reused from c_skeleton) with includes,
    macros, typedefs, structs (class names), functions (free functions +
    ``Class::method`` signatures), and globals. ``using`` declarations are
    folded into ``includes`` for rendering. Never raises — degrades gracefully
    to an empty result on any parsing failure.
    """
    if not text or not text.strip():
        return SkeletonResult()
    try:
        tokens = _tokenize(text)
        return _extract_cpp_skeleton_from_tokens(tokens)
    except Exception:  # noqa: BLE001 — skeleton is advisory; never crash
        return SkeletonResult()

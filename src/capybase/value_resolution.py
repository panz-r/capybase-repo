"""Detect "base operation preserved, value/expression resolved" conflicts.

A merge conflict where both sides preserve the SAME statement shape and only a
value/expression diverged is a **value resolution**: picking either side is a
correct merge, NOT a dropped intent. Examples:

- ``return 'hi'`` (current) vs ``return 'howdy'`` (replayed), base
  ``return 'hello'`` — the ``return`` statement is preserved on both sides; only
  the returned value differs. One ``return`` per branch of control flow can
  execute, so combining the values is rarely meaningful — picking a side is the
  semantically-correct resolution.
- ``a = 5`` (current) vs ``a = f(x) - 2`` (replayed), base ``a = 1`` — both
  sides assign to ``a``; the assignment target (the base operation) is preserved
  and only the RHS expression diverges. Two assignments to the same target in
  sequence don't compose — picking a side is correct.

Without this classification, capybase's ``BothSidesRepresentedValidator`` and
``PreservationHeuristicValidator`` (token-set + verbatim-copy heuristics) flag a
correct one-sided merge as "dropped a side," driving retries and eventual
escalation on a resolution that was already right.

The detection is pure (no I/O, no model). Python uses stdlib ``ast`` (the
established fragment-tolerant parse pattern); Family-A languages use a
leading-keyword + target-equality regex. Never raises — a parse/match failure
returns ``None`` (callers apply their existing heuristics unchanged).
"""

from __future__ import annotations

import ast
import re
import textwrap
from dataclasses import dataclass


@dataclass(frozen=True)
class ValueResolution:
    """A conflict where the base operation is preserved and a value diverged.

    ``kind`` is ``"return"`` / ``"assignment"`` / ``"augassign"``. ``target`` is
    the assignment target (e.g. ``"a"``, ``"self.x"``) for assignments, or ``""``
    for returns (a return has no target). When present, a one-sided merge
    (picking either side's value) is the correct resolution.
    """

    kind: str
    target: str = ""

    def as_feature(self) -> str:
        """A compact string for the conflict-features spine, or '' for returns."""
        return f"{self.kind}:{self.target}" if self.target else self.kind


# ---------------------------------------------------------------------------
# Python: stdlib ast, fragment-tolerant
# ---------------------------------------------------------------------------


def _safe_parse_fragment(text: str) -> ast.Module | None:
    """Parse a (possibly-indented) Python fragment into a module.

    Tries the text directly, then wraps it in a dummy ``def`` body (after
    dedent) so a bare ``return`` or leading-whitespace fragment parses — and
    UNWRAPS the dummy def so the returned module's statements are the
    fragment's statements (not a wrapper ``FunctionDef``). Returns ``None`` for
    genuinely malformed input (never raises).
    """
    if not text or not text.strip():
        return None
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError):
        pass
    dedented = textwrap.dedent(text)
    wrapped = "def __vr_fragment__():\n" + textwrap.indent(dedented, "    ")
    try:
        mod = ast.parse(wrapped)
    except (SyntaxError, ValueError):
        return None
    # Unwrap: the dummy def is the only top-level statement; lift its body into
    # a module so callers see the fragment's real statements.
    if mod.body and isinstance(mod.body[0], ast.FunctionDef):
        body = mod.body[0].body
    else:
        body = mod.body
    return ast.Module(body=list(body), type_ignores=[])


def _deepest_last_stmt(node: ast.stmt) -> ast.stmt:
    """Descend into a container's body to its last leaf statement.

    A conflict's base side may carry its enclosing signature (e.g. the whole
    ``def greet():\n    return ...`` function) while the current/replayed sides
    are just the hunk interior (``    return ...``). Comparing the function def
    to a bare return would mismatch; we instead compare the function's DEEPEST
    last statement (the return) against the sides' last statement. Returns the
    node unchanged when it has no nested body.
    """
    cur = node
    while True:
        body = getattr(cur, "body", None)
        if isinstance(body, list) and body:
            cur = body[-1]
            continue
        break
    return cur


#: AST nodes whose ``.body`` is GUARDED — the body executes conditionally on a
#: test/iterator/with-item. When the descent passes through one, the guard must
#: match across all three sides or the inner statement's divergence is NOT a pure
#: value resolution (a one-sided merge would drop the differing guard logic).
_GUARDED_BODY_TYPES = (
    ast.If, ast.For, ast.While, ast.With, ast.AsyncFor, ast.AsyncWith,
)
# Python <3.11 compat: Try also has a guarded body (the try block).
try:
    _GUARDED_BODY_TYPES = (*_GUARDED_BODY_TYPES, ast.Try, ast.ExceptHandler)
except AttributeError:  # pragma: no cover
    pass


def _descent_guards(node: ast.stmt) -> list[ast.AST]:
    """The guard expressions (``If.test``, ``For.iter``, ``With.items``, ...)
    encountered descending to the deepest last statement, in descent order.

    Empty for an unguarded descent (the leaf is reached through function/module
    bodies only, or is the top node itself). Used to verify the surrounding
    conditional/loop context matches across the three sides — ``if flag: y=1``
    vs ``if not flag: y=3`` have the same assignment target but DIFFERENT
    guards, so a one-sided merge would drop a branch's condition logic.
    """
    guards: list[ast.AST] = []
    cur: ast.AST = node
    while True:
        if isinstance(cur, _GUARDED_BODY_TYPES):
            # Extract the guard: If.test / While.test / For.iter / With.items /
            # Try (no scalar guard — the body is guarded by "no exception",
            # treat the whole try node as the guard so differing try-blocks
            # don't false-classify).
            if isinstance(cur, (ast.If, ast.While)):
                guards.append(cur.test)  # type: ignore[attr-defined]
            elif isinstance(cur, (ast.For, ast.AsyncFor)):
                guards.append(cur.iter)  # type: ignore[attr-defined]
            elif isinstance(cur, (ast.With, ast.AsyncWith)):
                guards.append(tuple(cur.items))  # type: ignore[attr-defined]
            elif isinstance(cur, (ast.Try, ast.ExceptHandler)):
                guards.append(cur)  # the node itself as a structural guard
        body = getattr(cur, "body", None)
        if isinstance(body, list) and body:
            cur = body[-1]
            continue
        break
    return guards


def _classify_python(base: str, cur: str, rep: str) -> ValueResolution | None:
    """Value-resolution classification via the Python ast.

    All three sides' last statement must be the same node type. ``Return`` → a
    return value resolution. ``Assign`` / ``AugAssign`` → an assignment value
    resolution only when the target (and, for augmented, the operator) is
    identical across all three. Any other shape, a target mismatch, or a parse
    failure → ``None`` (not a value resolution).

    The base side may be a whole function/module (carrying its signature) while
    the current/replayed sides are hunk interiors — so each side's last statement
    is resolved via :func:`_deepest_last_stmt` before the type comparison.
    """
    mb, mc, mr = (
        _safe_parse_fragment(base),
        _safe_parse_fragment(cur),
        _safe_parse_fragment(rep),
    )
    if mb is None or mc is None or mr is None:
        return None
    if not (mb.body and mc.body and mr.body):
        return None
    bs, cs, rs = (
        _deepest_last_stmt(mb.body[-1]),
        _deepest_last_stmt(mc.body[-1]),
        _deepest_last_stmt(mr.body[-1]),
    )
    # All three must be the same statement type.
    if not (type(bs) is type(cs) is type(rs)):
        return None
    # If the descent passed through conditional/loop guards, the guards must
    # match across all three sides. ``if flag: y=1`` vs ``if not flag: y=3``
    # share the assignment target ``y`` but the conditions differ — a one-sided
    # merge would drop a branch's condition logic (silent wrong merge). Compare
    # the guard ASTs by their unparsed source (structural equality at the same
    # descent depth).
    gb, gc, gr = (
        _descent_guards(mb.body[-1]),
        _descent_guards(mc.body[-1]),
        _descent_guards(mr.body[-1]),
    )
    if not (len(gb) == len(gc) == len(gr)):
        return None
    for gb_i, gc_i, gr_i in zip(gb, gc, gr):
        try:
            sb, sc, sr = (
                ast.unparse(gb_i),
                ast.unparse(gc_i),
                ast.unparse(gr_i),
            )
        except Exception:  # noqa: BLE001 - unparse can fail on exotic nodes
            return None
        if not (sb == sc == sr):
            return None
    if isinstance(bs, ast.Return):
        return ValueResolution(kind="return", target="")
    if isinstance(bs, ast.Assign):
        try:
            tb = ast.unparse(bs.targets[0])
            tc = ast.unparse(cs.targets[0])
            tr = ast.unparse(rs.targets[0])
        except Exception:  # noqa: BLE001 - unparse can fail on exotic nodes
            return None
        if tb == tc == tr:
            return ValueResolution(kind="assignment", target=tb)
        return None
    if isinstance(bs, ast.AugAssign):
        try:
            tb = ast.unparse(bs.target)
            tc = ast.unparse(cs.target)
            tr = ast.unparse(rs.target)
        except Exception:  # noqa: BLE001
            return None
        if tb == tc == tr and type(bs.op) is type(cs.op) is type(rs.op):
            return ValueResolution(kind="augassign", target=tb)
        return None
    return None


# ---------------------------------------------------------------------------
# Family A (Rust / JS / TS / Go / ...): leading-keyword + target-equality regex
# ---------------------------------------------------------------------------

#: A Family-A ``return`` statement: leading keyword (after optional modifiers)
#: is ``return``. Matched on the first non-whitespace token run of each side.
_A_RETURN_RE = re.compile(r"^\s*(?:(?:pub|export|public|private|static|unsafe|inline)\s+)*return\b")

#: A Family-A assignment: an optional ``let``/``var``/``const``/``mut`` binding
#: OR a bare assignment, capturing the TARGET (a simple name or ``obj.field``).
#: ``let x = ...`` / ``x = ...`` / ``var x: Type = ...`` all capture ``x``.
_A_ASSIGN_RE = re.compile(
    r"^\s*(?:(?:pub|export|public|private|static|unsafe|inline)\s+)*"
    r"(?:(?:let|var|const)\s+(?:mut\s+)?)?"  # optional binding keyword
    r"(?P<target>[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*)"  # name or obj.field
    r"\s*(?::\s*[^=]+)?="  # optional type annotation, then =
)


def _classify_family_a(base: str, cur: str, rep: str) -> ValueResolution | None:
    """Value-resolution classification for brace-delimited languages (regex).

    Heuristic but precise enough for the common cases: a ``return`` value
    resolution (all sides lead with ``return``) or an assignment value
    resolution (all sides bind/assign the SAME target, RHS diverges). Different
    leading keywords or different targets → ``None``.

    Phase 5.2 tightening: rejects multi-statement sides (a side with ``;`` or a
    newline separating two statements would let the classifier match the first
    and license dropping the second), and bare-vs-valued return divergence (a
    bare ``return`` vs ``return 5`` is a control-flow change, not a value
    resolution — picking one side changes whether the function returns a value).
    """
    b, c, r = (base or "").strip(), (cur or "").strip(), (rep or "").strip()
    if not (b and c and r):
        return None
    # Phase 5.2: reject multi-statement sides. A side with a top-level ``;`` or
    # a newline (Family-A statement separators) carries more than one statement;
    # a one-sided merge would drop the others. Strip trailing ``;`` first (a
    # single statement's terminator is fine), then check for any remaining
    # separator. This is conservative — a ``;`` inside a string/bracket would
    # false-decline, but the union/disjoint rules catch those cases anyway.
    def _is_single_statement(s: str) -> bool:
        # Strip a single trailing ``;`` (the common statement terminator).
        s = s.rstrip()
        if s.endswith(";"):
            s = s[:-1]
        # Any remaining ``;`` or newline ⇒ multi-statement.
        return ";" not in s and "\n" not in s
    if not all(_is_single_statement(s) for s in (b, c, r)):
        return None
    # Return value resolution: all three lead with `return`.
    if _A_RETURN_RE.match(b) and _A_RETURN_RE.match(c) and _A_RETURN_RE.match(r):
        # Phase 5.2: bare-vs-valued return is a control-flow change. All three
        # must be bare returns OR all three must return a value.
        def _is_bare_return(s: str) -> bool:
            m = _A_RETURN_RE.match(s)
            if m is None:
                return False
            # Everything after the ``return`` keyword (and optional modifiers).
            after = s[m.end():].strip().rstrip(";").strip()
            return after == ""
        bare_b, bare_c, bare_r = _is_bare_return(b), _is_bare_return(c), _is_bare_return(r)
        if bare_b == bare_c == bare_r:
            return ValueResolution(kind="return", target="")
        return None  # mixed bare/valued — control-flow change
    # Assignment value resolution: all three assign/bind the SAME target.
    mb_, mc_, mr_ = _A_ASSIGN_RE.match(b), _A_ASSIGN_RE.match(c), _A_ASSIGN_RE.match(r)
    if mb_ and mc_ and mr_:
        tb, tc, tr = mb_.group("target"), mc_.group("target"), mr_.group("target")
        if tb == tc == tr:
            # The full binding signature (everything up to and including the
            # target — ``let mut x``, ``let x``, ``const N``, ``self.x``) must
            # match across all three sides. ``let mut x = 1`` vs ``let x = 2``
            # share the target ``x`` but the ``mut`` keyword is semantically
            # significant in Rust (affects the borrow checker); a one-sided
            # merge picking the non-mut side would silently change mutability.
            # Comparing the prefix up to the target end catches any modifier
            # difference (mut, visibility, const-vs-let, type annotation).
            sig_b = b[: mb_.end("target")].strip()
            sig_c = c[: mc_.end("target")].strip()
            sig_r = r[: mr_.end("target")].strip()
            if sig_b == sig_c == sig_r:
                return ValueResolution(kind="assignment", target=tb)
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

#: Languages routed to the ast-based Python classifier.
_PY_LANGS = frozenset({"python", "py"})

#: Languages routed to the regex-based Family-A classifier (brace-delimited).
_A_LANGS = frozenset({
    "rust", "rs", "javascript", "js", "typescript", "ts", "jsx", "tsx",
    "go", "golang", "java", "c", "cpp", "c++", "csharp", "cs",
    "kotlin", "swift", "scala", "dart", "php",
})


def classify_value_resolution(
    base: str, current: str, replayed: str, language: str | None
) -> ValueResolution | None:
    """Classify a 3-way conflict as a value resolution, or ``None``.

    Returns a :class:`ValueResolution` when both sides preserve the same
    statement shape and only a value/expression diverged (a one-sided merge is
    correct). Returns ``None`` for genuine distinct additions, different
    statement shapes, unknown languages, or parse failures (callers apply their
    existing heuristics). Dispatches on ``language``: Python → ast, Family-A →
    regex. Never raises.
    """
    lang = (language or "").strip().lower()
    try:
        if lang in _PY_LANGS:
            return _classify_python(base or "", current or "", replayed or "")
        if lang in _A_LANGS:
            return _classify_family_a(base or "", current or "", replayed or "")
    except Exception:  # noqa: BLE001 - robustness; never raise on classification
        return None
    return None

"""Change accounting: derive the actionable obligations when a merge candidate
copies one side verbatim.

The preservation heuristic flags any resolution that exactly equals one side.
But "candidate == one side" is not itself proof of a lost intent — the other
side's base-relative changes may be:

- already present in the copied side (EQUIVALENT — the copy is correct);
- comment/documentation-only (DEFERRED — the comment pass handles them);
- formatting/trivia (IGNORED — no semantic content);
- genuinely missing executable code (MISSING — the actionable case).

This module computes the *specific* missing obligations so the CEGIS repair
loop can give the model a constructive counterexample ("integrate THIS line")
instead of the generic "you copied one side" feedback that small models can't
act on (they re-propose the same side and converge).

Pure of I/O. Line-based diff (difflib) with whitespace-normalized comparison,
so a re-indented line matches as PRESENT rather than MISSING. This is the
pragmatic starting point; a structural/AST-anchored representation is a
future refinement (the line-based fallback is deliberately conservative — it
only flags a line MISSING when no whitespace-normalized form of it appears in
the candidate).

The change channels (the design's "split changes into channels"):

  executable  — real code; the actionable missing changes
  comment     — /// // //! # → deferred (not a code-phase obligation)
  directive   — #[...] attributes → executable-significant (kept as obligations)
  formatting  — pure whitespace/punctuation → ignored (no obligation)
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# The obligation record
# ---------------------------------------------------------------------------


#: The channels a base-relative change can fall into.
CHANNELS = frozenset({"executable", "comment", "directive", "formatting"})

#: The disposition of a change relative to the candidate.
STATUSES = frozenset({"MISSING", "PRESENT"})


@dataclass(frozen=True)
class BranchObligation:
    """One base-relative change from the dropped side, classified + located.

    ``line`` is the verbatim changed line. ``channel`` drives whether it's an
    actionable code-phase obligation (executable/directive) or deferred/ignored
    (comment/formatting). ``status`` is MISSING (absent from the candidate —
    the actionable signal) or PRESENT (already accounted for — no obligation).
    ``side`` is which branch introduced it ("current" or "replayed").

    ``exclusive`` is True when the candidate already has a line at the same
    structural position (same leading identifier — field name, assignment
    target, function name) with a DIFFERENT value. This means the two sides
    propose mutually-exclusive alternatives (e.g. two type signatures for the
    same field: ``PhantomData<fn(B) -> S>`` vs ``PhantomData<fn() -> S>``).
    An exclusive obligation is NOT an integration task — the model should
    CHOOSE one side's value (both are valid), not try to combine them. Telling
    a small model to "integrate" an exclusive conflict is asking for the
    impossible, which is why it re-proposes the same side and converges.
    """
    line: str
    channel: str
    status: str
    side: str
    # The operation: "added" (line is new in the branch vs base) or "removed"
    # (line was in base, the branch deleted it). A removed line that's missing
    # from the candidate is usually fine (the branch intended to delete it); an
    # added line that's missing is the actionable case.
    operation: str = "added"
    exclusive: bool = False


# ---------------------------------------------------------------------------
# Channel classification
# ---------------------------------------------------------------------------


#: Rust/JS/TS attributes and compiler directives — executable-significant.
#: In Rust these are #[derive(...)], #[cfg(...)], #[allow(...)], etc. In
#: Python decorators (@dec) and type comments. These affect compilation /
#: generated code, so they are NOT deferred like ordinary comments.
_DIRECTIVE_RE = re.compile(
    r"^\s*#\["
    r"|" r"^\s*#!\["  # inner attributes #![...]
    r"|" r"^\s*@"      # Python decorators
)

#: Comment markers across supported languages.
_COMMENT_RE = re.compile(
    r"^\s*("
    r"///?"        # Rust doc + line comment: /// //!
    r"|" r"//!"    # Rust inner doc
    r"|" r"//"     # C-family line comment
    r"|" r"#"      # Python/shell line comment (but NOT #[ or @)
    r"|" r"/\*"    # C-family block comment open
    r"|" r"\*/"    # block close
    r"|" r"\*"     # block-comment continuation lines
    r")"
)

#: Pure formatting — whitespace, braces, parens, semicolons, commas. These carry
#: no semantic content and create no obligation when dropped.
_FORMATTING_RE = re.compile(r"^[\s{}()\[\];,]*$")


def classify_channel(line: str) -> str:
    """Classify a single line into a change channel.

    Priority: directive (#[...]/@dec) before comment (# is checked AFTER #[),
    because a Rust ``#[cfg(...)]`` starts with ``#`` but is a directive, not a
    comment. Formatting (pure punctuation) is checked last.
    """
    if not line.strip():
        return "formatting"
    if _DIRECTIVE_RE.match(line):
        return "directive"
    if _COMMENT_RE.match(line):
        return "comment"
    if _FORMATTING_RE.match(line):
        return "formatting"
    return "executable"


# ---------------------------------------------------------------------------
# The core: derive missing obligations from a one-sided copy
# ---------------------------------------------------------------------------


def _norm(line: str) -> str:
    """Whitespace-normalized form for PRESENT comparison (re-indented matches)."""
    return " ".join(line.split())


#: Extract the leading structural identifier from a code line — the field name,
#: assignment target, or function/variable name that anchors the line's position.
#: Used to detect EXCLUSIVE conflicts: when the candidate and the missing
#: obligation share the same anchor but differ in value, they're mutually-
#: exclusive alternatives (e.g. ``_marker: PhantomData<fn(B) -> S>`` vs
#: ``_marker: PhantomData<fn() -> S>`` — same field, different type).
_ANCHOR_RE = re.compile(
    r"^\s*(?:pub\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+)?"  # modifiers
    r"(?:fn\s+|def\s+|class\s+|struct\s+|enum\s+|impl\s+)?"      # keyword (optional)
    r"([A-Za-z_]\w*)"                                            # the identifier
)

#: Import/use/include statements. For these the anchor is the FULL import path
#: (``crate::a`` vs ``crate::b`` are DIFFERENT additions, not exclusive), not
#: just the ``use`` keyword (which would make every import look exclusive).
_IMPORT_RE = re.compile(
    r"^\s*(?:pub\s+)?(?:use\s+|import\s+|from\s+|#include\s+)"  # import keyword
    r"(.+?)"                                                     # the path
    r"\s*(?:;|::\{|\s+as\s|$)"                                   # terminator
)


def _anchor_of(line: str) -> str:
    """The leading structural identifier of a line (for exclusive-conflict
    detection). Returns "" when the line has no clear anchor.

    For import/use/include statements, the anchor is the full import path
    (so ``use crate::a;`` and ``use crate::b;`` are DIFFERENT — both can
    coexist). For other statements, it's the field/var/function name (so
    ``_marker: PhantomData<fn(B) -> S>`` and ``_marker: PhantomData<fn() -> S>``
    share the anchor ``_marker`` → exclusive)."""
    # Imports: anchor on the full path, not the keyword.
    m = _IMPORT_RE.match(line)
    if m:
        return "import:" + m.group(1).strip().rstrip(";").strip()
    m = _ANCHOR_RE.match(line)
    return m.group(1) if m else ""


def _structural_suffix(line: str) -> str:
    """Everything AFTER the leading identifier in a line — the structural
    'shape' that remains when the name is stripped. Used to detect rename-type
    exclusive conflicts where two lines have DIFFERENT leading identifiers but
    the SAME trailing structure (e.g. ``Self { stream }`` vs ``Sse { stream }``
    — a type rename; the constructor body is identical)."""
    m = _ANCHOR_RE.match(line)
    if not m:
        return _norm(line)
    return _norm(line[m.end():])


#: Matches a brace-grouped item list line — either a full ``use path::{A, B}``
#: import OR a continuation line ``path::{A, B},`` from a multi-line use
#: statement. The key signal is ``identifiers::{comma, separated, items}``.
_IMPORT_LIST_RE = re.compile(
    r"^\s*(?:pub\s+)?(?:use\s+)?"
    r"([\w:]+)"          # the path prefix (crate::module / util)
    r"::\s*\{([^}]*)\}"  # the brace-grouped item list
    r"\s*,?\s*;?\s*$"    # optional trailing comma/semicolon
)


def _is_import_list_line(line: str) -> bool:
    """True when the line is a ``use path::{A, B, C}`` import-list form."""
    return bool(_IMPORT_LIST_RE.match(line))


def _import_list_items(line: str) -> frozenset[str]:
    """The items inside a ``use path::{A, B, C}`` brace group, as a set.
    Returns an empty set when the line isn't an import-list form."""
    m = _IMPORT_LIST_RE.match(line)
    if not m:
        return frozenset()
    items = m.group(2)
    return frozenset(i.strip() for i in items.split(",") if i.strip())


#: Matches a ``#[derive(...)]`` or ``#[derive(...)]`` attribute and captures
#: the comma-separated trait list inside the parens. Handles both outer
#: (``#[derive(...)]``) and inner (``#![derive(...)]``) attributes.
_DERIVE_RE = re.compile(
    r"^\s*#!?\[derive\s*\(([^)]*)\)\]"
)


def _is_derive_attr(line: str) -> bool:
    """True when the line is a ``#[derive(...)]`` attribute."""
    return bool(_DERIVE_RE.match(line))


def _derive_trait_set(line: str) -> frozenset[str]:
    """The set of trait names inside a ``#[derive(Debug, Clone)]`` attribute.
    Returns an empty set when the line isn't a derive attribute."""
    m = _DERIVE_RE.match(line)
    if not m:
        return frozenset()
    traits = m.group(1)
    return frozenset(t.strip() for t in traits.split(",") if t.strip())


def derive_missing_obligations(
    base: str, current: str, replayed: str, resolved: str,
) -> list[BranchObligation]:
    """When ``resolved`` equals one side, compute the OTHER side's base-relative
    changes that are absent from the resolved text.

    Returns the MISSING obligations (executable + directive only — comment
    changes are deferred to the comment pass and don't block the code candidate;
    formatting changes are ignored). PRESENT changes (already in the candidate)
    are NOT returned (no obligation). When the resolution doesn't exactly equal
    either side, returns [] (this analysis only applies to exact-side copies).

    The returned list is the actionable set: each is a specific line the model
    should integrate (or mark equivalent/superseded). An empty result means the
    copy is fully accounted for — the preservation heuristic should PASS.
    """
    cur = current or ""
    rep = replayed or ""
    res = resolved or ""
    # Determine which side was copied (resolved == one side, whitespace-trimmed).
    # An empty resolved == empty side IS a copy (the model resolved to the
    # deletion side). Allow it when at least one side is non-empty (both empty
    # is a degenerate conflict with no signal).
    if res.strip() == cur.strip() and (cur.strip() or rep.strip()):
        copied_side, other_text, other_label = "current", rep, "replayed"
    elif res.strip() == rep.strip() and (rep.strip() or cur.strip()):
        copied_side, other_text, other_label = "replayed", cur, "current"
    else:
        return []  # not an exact-side copy; this analysis doesn't apply

    # Diff base → other side: what did the dropped side change?
    base_lines = (base or "").splitlines()
    other_lines = other_text.splitlines()
    diff = difflib.unified_diff(base_lines, other_lines, lineterm="", n=0)

    # The candidate's normalized line set (for PRESENT detection).
    res_norm = {_norm(l) for l in res.splitlines() if l.strip()}
    # The candidate's anchor → line map (for EXCLUSIVE detection: a missing
    # line whose anchor matches a candidate line at the same position is an
    # alternative, not an addition).
    res_anchors: dict[str, str] = {}
    for l in res.splitlines():
        a = _anchor_of(l)
        if a and l.strip():
            res_anchors.setdefault(a, _norm(l))

    obligations: list[BranchObligation] = []
    seen_norm: set[str] = set()  # dedupe by normalized content (a line modified
    #                                 in multiple contexts shouldn't appear twice)
    for line in diff:
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            changed = line[1:]
            op = "added"
        elif line.startswith("-"):
            changed = line[1:]
            op = "removed"
        else:
            continue
        channel = classify_channel(changed)
        # Deferred / ignored channels never produce a code-phase obligation.
        if channel in ("comment", "formatting"):
            continue
        # Dedupe: a line can appear in multiple diff hunks (e.g. the same field
        # in a struct definition + its constructor). Only the first occurrence
        # is an obligation; duplicates add noise without new signal.
        norm = _norm(changed)
        if norm in seen_norm:
            continue
        seen_norm.add(norm)
        # PRESENT if any whitespace-normalized form of the line is in the
        # candidate (re-indented additions count as accounted for).
        status = "MISSING" if norm not in res_norm else "PRESENT"
        if op == "added" and status == "MISSING":
            # An ADDED line absent from the candidate = a dropped addition.
                # EXCLUSIVE detection: does the candidate already have a line
                # at the same structural anchor (same field/var/fn name) with a
                # DIFFERENT value? If so, this is a mutually-exclusive choice,
                # not an integration task — flag it so the feedback tells the
                # model to CHOOSE, not integrate.
                anchor = _anchor_of(changed)
                exclusive = bool(
                    anchor and anchor in res_anchors
                    and res_anchors[anchor] != norm
                )
                # Import-list refinement: two ``path::{a, b}`` lines with the
                # same path prefix but different brace items are ADDITIVE (one
                # adds items to the list), NOT exclusive — even though they
                # share the anchor. Only treat as exclusive when neither is a
                # superset of the other (a genuine replacement). This catches
                # ``util::{MapErrLayer, Oneshot}`` vs ``util::{BoxCloneService,
                # MapErrLayer, Oneshot}`` — the second ADDS BoxCloneService.
                if exclusive and _is_import_list_line(changed):
                    for res_line in res.splitlines():
                        if _norm(res_line) != norm and _is_import_list_line(res_line):
                            added_items = _import_list_items(changed)
                            cand_items = _import_list_items(res_line)
                            if added_items and cand_items:
                                if added_items >= cand_items or cand_items >= added_items:
                                    exclusive = False  # one is a superset → additive
                                    break
                # Derive-attribute refinement: two ``#[derive(...)]`` lines are
                # usually ADDITIVE (one adds traits to the set), not exclusive.
                # ``#[derive(Debug, Clone)]`` vs ``#[derive(Debug, Serialize)]``
                # share Debug → the second ADDS Serialize. Only flag exclusive
                # when the trait sets are completely DISJOINT (a genuine
                # Debug-vs-Clone choice where picking one is a semantic decision).
                # This mirrors the import-list superset check above.
                if exclusive and _is_derive_attr(changed):
                    missing_traits = _derive_trait_set(changed)
                    if missing_traits:
                        for res_line in res.splitlines():
                            if (_norm(res_line) != norm
                                    and _is_derive_attr(res_line)):
                                cand_traits = _derive_trait_set(res_line)
                                if cand_traits:
                                    # Any shared trait → additive (union), not
                                    # exclusive. The missing derive adds traits
                                    # the candidate doesn't have yet.
                                    if missing_traits & cand_traits:
                                        exclusive = False
                                        break
                                    # No overlap but one is a superset of the
                                    # other → also additive (replacement superset).
                                    if (missing_traits >= cand_traits
                                            or cand_traits >= missing_traits):
                                        exclusive = False
                                        break
                # Rename-type exclusive: different leading identifiers but the
                # SAME trailing structure (e.g. ``Self { stream }`` vs
                # ``Sse { stream }`` — a type rename). When the anchors differ
                # but the structural suffix matches, it's still exclusive.
                if not exclusive:
                    suffix = _structural_suffix(changed)
                    if suffix and len(suffix) >= 4:
                        for res_line in res.splitlines():
                            if (_structural_suffix(res_line) == suffix
                                    and _norm(res_line) != norm
                                    and _anchor_of(res_line) != anchor):
                                exclusive = True
                                break
                obligations.append(BranchObligation(
                    line=changed, channel=channel, status=status,
                    side=other_label, operation=op, exclusive=exclusive,
                ))
        elif op == "removed" and status == "PRESENT":
            # A REMOVED line still present in the candidate = a dropped
            # deletion. The other side intended to DELETE this line (e.g.
            # removed an unsafe fallback call), but the model copied the side
            # that kept it — the deletion was not applied. This is a genuine
            # obligation: the candidate should remove the line. (The analysis's
            # DELETE operation — previously invisible because removed lines
            # produced no "missing line" to request.)
            obligations.append(BranchObligation(
                line=changed, channel=channel, status="DROPPED_DELETION",
                side=other_label, operation="removed", exclusive=False,
            ))
    return obligations


def derive_deferred_comments(
    base: str, current: str, replayed: str, resolved: str,
) -> list[BranchObligation]:
    """The comment-channel changes from the dropped side (deferred to the
    comment-reconciliation pass).

    These do NOT block the executable candidate, but they are recorded so the
    comment pass knows the dropped side introduced comment changes that need
    reconciling. Returns only the MISSING comment obligations (comment changes
    already present in the candidate are satisfied).
    """
    cur = current or ""
    rep = replayed or ""
    res = resolved or ""
    if res.strip() == cur.strip() and (cur.strip() or rep.strip()):
        other_text, other_label = rep, "replayed"
    elif res.strip() == rep.strip() and (rep.strip() or cur.strip()):
        other_text, other_label = cur, "current"
    else:
        return []

    base_lines = (base or "").splitlines()
    other_lines = other_text.splitlines()
    diff = difflib.unified_diff(base_lines, other_lines, lineterm="", n=0)
    res_norm = {_norm(l) for l in res.splitlines() if l.strip()}

    deferred: list[BranchObligation] = []
    for line in diff:
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            changed = line[1:]
            op = "added"
        elif line.startswith("-"):
            continue  # removed comments aren't obligations
        else:
            continue
        if classify_channel(changed) != "comment":
            continue
        if _norm(changed) not in res_norm:
            deferred.append(BranchObligation(
                line=changed, channel="comment", status="MISSING",
                side=other_label, operation=op,
            ))
    return deferred


__all__ = [
    "CHANNELS", "STATUSES",
    "BranchObligation",
    "classify_channel",
    "derive_missing_obligations",
    "derive_deferred_comments",
]

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
    if res.strip() == cur.strip() and cur.strip():
        copied_side, other_text, other_label = "current", rep, "replayed"
    elif res.strip() == rep.strip() and rep.strip():
        copied_side, other_text, other_label = "replayed", cur, "current"
    else:
        return []  # not an exact-side copy; this analysis doesn't apply

    # Diff base → other side: what did the dropped side change?
    base_lines = (base or "").splitlines()
    other_lines = other_text.splitlines()
    diff = difflib.unified_diff(base_lines, other_lines, lineterm="", n=0)

    # The candidate's normalized line set (for PRESENT detection).
    res_norm = {_norm(l) for l in res.splitlines() if l.strip()}

    obligations: list[BranchObligation] = []
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
        # PRESENT if any whitespace-normalized form of the line is in the
        # candidate (re-indented additions count as accounted for).
        status = "MISSING" if _norm(changed) not in res_norm else "PRESENT"
        if status == "MISSING":
            # A removed line that's missing from the candidate is usually the
            # branch's intentional deletion — not an obligation to integrate.
            # Only ADDED executable/directive lines that are absent are
            # actionable (the model dropped a real addition).
            if op == "added":
                obligations.append(BranchObligation(
                    line=changed, channel=channel, status=status,
                    side=other_label, operation=op,
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
    if res.strip() == cur.strip() and cur.strip():
        other_text, other_label = rep, "replayed"
    elif res.strip() == rep.strip() and rep.strip():
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

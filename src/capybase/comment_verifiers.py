"""Deterministic verifiers for the comment reconciliation plan (Part G1).

Each verifier produces a :class:`CommentFailure` — a concrete §9-style
counterexample naming the offending lineage and the specific defect. The CEGIS
loop in :meth:`Orchestrator._reconcile_comments` feeds these back to the model
on the next attempt (mirroring ``build_repair_prompt``'s feedback threading).

Pure functions — no LLM, no I/O. The hard executable-token-equality invariant
in :func:`apply_comment_plan` is still the ultimate safety net; these verifiers
catch defects BEFORE the splice (cheap) and produce better counterexamples than
the bare ``ApplyError`` (which only signals "code changed", not which lineage
is at fault or why).

The five §9 verifiers implemented here:

- ``STALE_IDENTIFIER``   — a rewrite references an identifier not in the code.
- ``INVALID_ANCHOR``     — an action targets a lineage not in the frontier.
- ``UNACCOUNTED_COMMENT`` — a frontier lineage with no disposition.
- ``DUPLICATE_COMMENT``  — one lineage with >1 disposition.
- ``DIRECTIVE_CHANGED``  — a non-deferable comment was rewritten (defensive).

Out of scope (need an LLM jury or signature parser — future phase):
``UNSUPPORTED_CLAIM``, ``DOC_SIGNATURE_MISMATCH``, ``DOCTEST_FAILURE``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from capybase.adapters.comment_classifier import NON_DEFERABLE
from capybase.comment_reconciler import CommentPlan, LedgerEntry


# ---------------------------------------------------------------------------
# Failure kinds (the §9 identifiers)
# ---------------------------------------------------------------------------

STALE_IDENTIFIER = "STALE_IDENTIFIER"
INVALID_ANCHOR = "INVALID_ANCHOR"
UNACCOUNTED_COMMENT = "UNACCOUNTED_COMMENT"
DUPLICATE_COMMENT = "DUPLICATE_COMMENT"
DIRECTIVE_CHANGED = "DIRECTIVE_CHANGED"
STYLE_VIOLATION = "STYLE_VIOLATION"
DOC_SIGNATURE_MISMATCH = "DOC_SIGNATURE_MISMATCH"


@dataclass(frozen=True)
class CommentFailure:
    """A single verifier finding — a concrete counterexample for the CEGIS loop.

    ``kind`` is the §9 identifier; ``lineage_id`` names the offending comment
    lineage ("" when the failure is plan-wide, e.g. all-unaccounted);
    ``message`` is the human-readable detail rendered into the next prompt's
    ``### prior-attempt feedback`` block.
    """
    kind: str
    lineage_id: str
    message: str


# ---------------------------------------------------------------------------
# Identifier extraction (the STALE_IDENTIFIER check)
# ---------------------------------------------------------------------------

#: Comment-syntax prefixes stripped before tokenizing a comment's text, so the
#: STALE check doesn't treat ``//`` or ``#`` as bogus identifiers.
_COMMENT_PREFIX_RE = re.compile(r"^\s*(?://+|#!|#=|#|/\*|\*/|\*|\"\"\"|''')+\s*")

#: An identifier-shaped token: letter/underscore start, alphanumeric+underscore
#: continuation, length ≥ 2 (filters out single-letter noise). Matches both
#: ``CamelCase`` and ``snake_case``.
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")


def _looks_like_symbol(tok: str) -> bool:
    """Does ``tok`` look like a real symbol reference (vs. ordinary prose)?

    A comment's STALE check should only fire on tokens that plausibly NAME a
    code identifier. The shape heuristics:

    - ``ALL_CAPS_WITH_UNDERSCORES`` (≥4 chars, contains ``_``, all-upper) —
      constants and macros. Real symbols. (e.g. ``MAX_RETRIES``, ``E_INVALID``.)
    - ``snake_case_with_underscore`` (contains ``_``, ≥4 chars) — Rust/Python
      identifier convention. Real symbols. (e.g. ``retry_count``, ``build_url``.)
    - ``CamelCase`` / ``PascalCase`` (≥2 uppercase letters, no underscore,
      mixed case) — types/classes/structs. Real symbols. (e.g. ``RetryCount``,
      ``HttpClient``.)
    - ``ALLCAPS`` (≥4 chars, no underscore, all upper) — usually a constant or
      acronym-as-identifier. Real symbols. (e.g. ``DEFAULT``, ``TIMEOUT``.)
    - Plain lowercase words (``foo``, ``value``, ``count``) — almost always
      prose, NOT symbol references. Filter.

    The cost of a false positive (calling a prose word stale) is teaching the
    model to write vaguer comments. The cost of a false negative (missing a
    real stale identifier) is a stale comment surviving one extra iteration —
    the executable-token invariant still catches code corruption. So we err
    conservative: only flag identifier-shaped tokens.
    """
    n = len(tok)
    if n < 4:
        return False
    has_under = "_" in tok
    upper = sum(1 for c in tok if c.isupper())
    lower = sum(1 for c in tok if c.islower())
    digits = sum(1 for c in tok if c.isdigit())
    # ALL_CAPS_WITH_UNDERSCORES: e.g. MAX_RETRIES, E_INVALID_ARG
    if has_under and tok.isupper():
        return True
    # snake_case: contains underscore, not all upper (e.g. retry_count, build_url)
    if has_under and lower > 0:
        return True
    # CamelCase / PascalCase: mixed case, no underscore, ≥2 upper (e.g. HttpClient)
    if not has_under and upper >= 2 and lower >= 1:
        return True
    # ALLCAPS no underscore (e.g. TIMEOUT, DEFAULT) — but only if not pure-digit
    if not has_under and tok.isupper() and upper >= 4 and digits == 0:
        return True
    return False


def _comment_identifiers(comment_text: str) -> set[str]:
    """Identifier-shaped tokens in ``comment_text`` that are candidates for the
    STALE check — i.e. tokens that plausibly NAME a code symbol.

    Filters ordinary prose via :func:`_looks_like_symbol`. ``MAX_RETRIES``,
    ``RetryCount``, ``retry_count``, ``TIMEOUT`` survive; ``foo``, ``value``,
    ``count``, ``still``, ``valid`` are filtered.
    """
    stripped = _COMMENT_PREFIX_RE.sub("", comment_text)
    return {m.group(0) for m in _IDENT_RE.finditer(stripped)
            if _looks_like_symbol(m.group(0))}


def _code_identifier_set(resolved_text: str, lang: str) -> set[str]:
    """The set of identifier tokens defined or referenced in ``resolved_text``.

    Combines :func:`structural.referenced_symbols` (call/use sites) with
    :func:`structural.enumerate_entities` (definition names) so a comment
    referencing either a definition or a use site is satisfied. Degrades
    gracefully (returns an empty set) when the language has no structural
    support — in that case the STALE check is skipped (no false positives).
    """
    out: set[str] = set()
    try:
        from capybase.adapters import structural
        refs = structural.referenced_symbols(resolved_text, lang) or []
        out.update(refs)
        ents = structural.enumerate_entities(resolved_text, lang, recursive=True) or []
        for e in ents:
            name = getattr(e, "name", "") or ""
            if name:
                out.add(name)
    except Exception:  # noqa: BLE001 — degrade to empty (skip check)
        return set()
    return out


# ---------------------------------------------------------------------------
# The verifiers
# ---------------------------------------------------------------------------


def _check_invalid_anchor(plan, frontier_lineage_ids) -> list[CommentFailure]:
    out = []
    for a in plan.actions:
        if a.lineage_id and a.lineage_id not in frontier_lineage_ids:
            out.append(CommentFailure(
                kind=INVALID_ANCHOR, lineage_id=a.lineage_id,
                message=(
                    f"lineage {a.lineage_id} is not in the frontier "
                    f"(valid ids: {sorted(frontier_lineage_ids)}). "
                    f"The action was ignored."
                ),
            ))
    return out


def _check_unaccounted(plan, frontier_lineage_ids) -> list[CommentFailure]:
    dispositioned = {a.lineage_id for a in plan.actions}
    out = []
    for lid in sorted(frontier_lineage_ids):
        if lid not in dispositioned:
            out.append(CommentFailure(
                kind=UNACCOUNTED_COMMENT, lineage_id=lid,
                message=(
                    f"lineage {lid} received no disposition. Every frontier "
                    f"comment must get exactly one operation."
                ),
            ))
    return out


def _check_duplicate(plan) -> list[CommentFailure]:
    counts: dict[str, int] = {}
    for a in plan.actions:
        counts[a.lineage_id] = counts.get(a.lineage_id, 0) + 1
    out = []
    for lid, n in counts.items():
        if n > 1:
            out.append(CommentFailure(
                kind=DUPLICATE_COMMENT, lineage_id=lid,
                message=(
                    f"lineage {lid} received {n} dispositions; exactly one "
                    f"is allowed."
                ),
            ))
    return out


def _check_directive_changed(plan, frontier_by_id) -> list[CommentFailure]:
    """Defensive: if a non-deferable comment (MACHINE/LEGAL/GENERATED/DOCTEST)
    somehow ended up in the frontier AND the plan rewrites it, that's a
    directive corruption. The ledger filters these out at build time; this
    catches a classifier regression that leaks one through."""
    out = []
    for a in plan.actions:
        if a.operation not in ("rewrite", "move", "merge", "delete"):
            continue
        entries = frontier_by_id.get(a.lineage_id, [])
        for e in entries:
            if e.cls in NON_DEFERABLE:
                out.append(CommentFailure(
                    kind=DIRECTIVE_CHANGED, lineage_id=a.lineage_id,
                    message=(
                        f"lineage {a.lineage_id} is a {e.cls.value} comment "
                        f"(non-deferable) and must not be rewritten. "
                        f"Use 'keep' or 'preserve_verbatim'."
                    ),
                ))
                break
    return out


def _check_stale_identifier(plan, code_idents) -> list[CommentFailure]:
    """A rewrite/move/merge whose ``text`` references an identifier absent from
    the resolved code. ``keep``/``preserve_verbatim`` are exempt (the model
    didn't introduce the staleness)."""
    if not code_idents:
        return []  # language without structural support — skip (no FPs)
    out = []
    for a in plan.actions:
        if a.operation not in ("rewrite", "move", "merge"):
            continue
        text = a.text or ""
        mentioned = _comment_identifiers(text)
        stale = sorted(mentioned - code_idents)
        if stale:
            out.append(CommentFailure(
                kind=STALE_IDENTIFIER, lineage_id=a.lineage_id,
                message=(
                    f"the rewritten comment references identifier(s) not "
                    f"present in the resolved code: {stale}. Either update "
                    f"the comment to reference current identifiers, or "
                    f"rewrite the prose without naming removed symbols."
                ),
            ))
    return out


#: Maximum line length for the STYLE_VIOLATION line-length check. Mirrors the
#: common style-guide default; a rewrite producing a line longer than this when
#: no source variant did is a style regression.
_STYLE_MAX_LINE = 120

#: A rewrite longer than this multiple of the longest source variant is
#: "rambling" (the model padded the comment with irrelevant content).
_RAMBLING_FACTOR = 5


def _check_style_violation(plan, frontier_by_id) -> list[CommentFailure]:
    """§9 STYLE_VIOLATION — deterministic style heuristics (no LLM).

    Catches degenerate rewrites the executable-token invariant can't see:
    - **Rambling**: rewrite text > ``_RAMBLING_FACTOR`` × the longest source
      variant (the model padded with irrelevant content).
    - **Comment-syntax leakage**: the rewrite text contains ``//`` or ``#``
      mid-line (a comment-within-a-comment — the model tried to nest syntax).
    - **Degenerate text**: the rewrite is just punctuation/whitespace or a
      single word.
    - **Line-length**: the rewrite produces a line > ``_STYLE_MAX_LINE`` chars
      when no source variant had a line that long.

    ``keep``/``preserve_verbatim`` are exempt (no new text).
    """
    out: list[CommentFailure] = []
    for a in plan.actions:
        if a.operation not in ("rewrite", "move", "merge"):
            continue
        text = (a.text or "").strip()
        if not text:
            continue
        entries = frontier_by_id.get(a.lineage_id, [])
        # The source variants for this lineage.
        source_texts = [e.text for e in entries]
        source_lens = [len((t or "").strip()) for t in source_texts]
        max_source = max(source_lens) if source_lens else 0
        issues: list[str] = []
        # Rambling.
        if max_source > 0 and len(text) > max_source * _RAMBLING_FACTOR:
            issues.append(
                f"rewrite is {len(text)} chars, {_RAMBLING_FACTOR}× the longest "
                f"source variant ({max_source} chars) — likely rambling"
            )
        # Comment-syntax leakage: // or # mid-line (after the first char).
        for line in text.split("\n"):
            stripped = line.lstrip()
            # Find // or # not at the start (a nested comment marker).
            mid = stripped[1:] if len(stripped) > 1 else ""
            if "//" in mid or (mid and "#" in mid):
                issues.append(
                    f"rewrite contains comment-syntax (// or #) mid-line: "
                    f"{line.strip()[:60]!r}"
                )
                break
        # Degenerate text: just punctuation or a single short word.
        alnum = sum(1 for c in text if c.isalnum())
        if alnum < 3:
            issues.append(
                f"rewrite text is degenerate (little alphanumeric content): "
                f"{text[:40]!r}"
            )
        # Line-length: any line > _STYLE_MAX_LINE when no source had one.
        source_max_line = 0
        for st in source_texts:
            for ln in (st or "").split("\n"):
                if len(ln) > source_max_line:
                    source_max_line = len(ln)
        for line in text.split("\n"):
            if len(line) > _STYLE_MAX_LINE and source_max_line <= _STYLE_MAX_LINE:
                issues.append(
                    f"rewrite produces a {len(line)}-char line; no source "
                    f"variant exceeded {_STYLE_MAX_LINE} chars"
                )
                break
        if issues:
            out.append(CommentFailure(
                kind=STYLE_VIOLATION, lineage_id=a.lineage_id,
                message="; ".join(issues),
            ))
    return out


def _check_doc_signature_mismatch(plan, frontier, resolved_text, lang) -> list[CommentFailure]:
    """§9 DOC_SIGNATURE_MISMATCH — documented params don't match the signature.

    For each rewrite/move/merge action on a Python docstring/comment whose
    enclosing function we can identify: parse the documented params from the
    action's text and compare against the function's actual signature params.
    Flag documented-but-not-in-signature params (the comment names a parameter
    that doesn't exist — the model hallucinated or the signature changed).

    No-op for non-Python (Rust rustdoc has no structured param convention).
    ``keep``/``preserve_verbatim`` are exempt. Degrades gracefully when the
    enclosing function can't be identified (no false positives).
    """
    if lang not in ("python", "py"):
        return []
    try:
        from capybase.adapters.docstring_parser import (
            parse_docstring_params, signature_params_for_enclosing,
        )
    except ImportError:
        return []
    out: list[CommentFailure] = []
    for a in plan.actions:
        if a.operation not in ("rewrite", "move", "merge"):
            continue
        text = a.text or ""
        # Find the resolved-version entry for this action (it carries the byte
        # offset we need to locate the enclosing function).
        entry = None
        for e in frontier:
            if e.lineage_id == a.lineage_id and e.version == "resolved":
                entry = e
                break
        if entry is None:
            continue
        # The enclosing function's signature params.
        sig_params = signature_params_for_enclosing(resolved_text, lang, entry.start)
        if not sig_params:
            continue  # not inside a function, or unsupported — skip
        # The documented params.
        parsed = parse_docstring_params(text, lang)
        if not parsed.params:
            continue  # no recognized param convention in the rewrite — skip
        # Documented but not in the signature → mismatch.
        extra = sorted(parsed.params - sig_params)
        if extra:
            out.append(CommentFailure(
                kind=DOC_SIGNATURE_MISMATCH, lineage_id=a.lineage_id,
                message=(
                    f"the rewritten docstring documents parameter(s) not in the "
                    f"function's signature: {extra} (signature has {sorted(sig_params)}). "
                    f"Update the docs to match the current parameters."
                ),
            ))
    return out


def verify_comment_plan(
    plan: CommentPlan,
    frontier: list[LedgerEntry],
    resolved_text: str,
    lang: str,
) -> list[CommentFailure]:
    """Run all deterministic verifiers on ``plan`` against ``frontier``.

    Returns a list of :class:`CommentFailure` (empty when the plan is clean).
    Pure — no LLM, no I/O. The CEGIS loop feeds these back to the model on the
    next attempt.

    Order: structural problems first (INVALID_ANCHOR, UNACCOUNTED,
    DUPLICATE, DIRECTIVE_CHANGED), then content checks (STALE_IDENTIFIER).
    Each verifier is independent; all findings are returned.
    """
    if not plan or not plan.actions:
        return []
    frontier_by_id: dict[str, list[LedgerEntry]] = {}
    for e in frontier:
        frontier_by_id.setdefault(e.lineage_id, []).append(e)
    frontier_lineage_ids = set(frontier_by_id.keys())

    failures: list[CommentFailure] = []
    failures.extend(_check_invalid_anchor(plan, frontier_lineage_ids))
    failures.extend(_check_unaccounted(plan, frontier_lineage_ids))
    failures.extend(_check_duplicate(plan))
    failures.extend(_check_directive_changed(plan, frontier_by_id))
    # STALE_IDENTIFIER is the most expensive (parses the whole resolved file);
    # only run when the plan is otherwise structurally valid (no point naming
    # stale idents in a comment whose lineage is invalid anyway).
    if not any(f.kind in (INVALID_ANCHOR, DIRECTIVE_CHANGED) for f in failures):
        code_idents = _code_identifier_set(resolved_text, lang)
        failures.extend(_check_stale_identifier(plan, code_idents))
    # STYLE_VIOLATION runs on every plan (cheap, no resolved-file parse).
    failures.extend(_check_style_violation(plan, frontier_by_id))
    # DOC_SIGNATURE_MISMATCH runs on Python rewrites whose enclosing function
    # we can identify. Degrades gracefully (no-op) for other languages.
    failures.extend(_check_doc_signature_mismatch(plan, frontier, resolved_text, lang))
    return failures


__all__ = [
    "CommentFailure",
    "verify_comment_plan",
    "STALE_IDENTIFIER",
    "INVALID_ANCHOR",
    "UNACCOUNTED_COMMENT",
    "DUPLICATE_COMMENT",
    "DIRECTIVE_CHANGED",
    "STYLE_VIOLATION",
    "DOC_SIGNATURE_MISMATCH",
]

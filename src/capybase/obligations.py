"""Side-obligation contract: what each side did, as line-level obligations.

Before the LLM resolves a conflict, the orchestrator derives a compact set of
invariants per side (what it ADDED, CHANGED, or REMOVED vs base). The contract
serves two purposes:

1. **Prompt grounding.** The resolve prompt shows the model a "must preserve"
   block per side (``CURRENT must preserve: added scheduler; changed port to
   9090``), so the model knows the *load-bearing* changes — not just the raw
   side text. This is finer than the conflict-shape label
   :func:`merge_intent.direction` already emits.

2. **Post-merge validation.** :class:`ObligationValidator` checks the candidate
   against the obligations: a side's *added* or *changed* content must appear in
   the resolution (a deliberate deletion is honored, not flagged). This catches
   the failure modes the token-set/verbatim heuristics miss: a dropped
   *modification* of an existing line (no new distinctive token) and a dropped
   *block* relocated or lost.

Everything here is a pure function of the three side texts (base/current/
replayed) — no git, no model, no I/O — so it's exhaustively unit-testable. The
diff is :mod:`difflib`-based (no new dependencies), matching the resolver and
:mod:`merge_intent`.

The replace-opcode gap: no existing helper returns BOTH the removed base lines
AND the replacement for a ``replace``. :func:`extract_obligations` does, so a
modification is captured as ``changed`` with both halves — the load-bearing case
the older validators structurally cannot see.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from capybase.diff import line_matcher


@dataclass(frozen=True)
class SideObligations:
    """What ONE side did vs base, as line-level obligations.

    ``added``: whole lines the side inserted (not present in base). ``changed``:
    base lines the side replaced, each as ``(old_base_line, new_side_line)``.
    ``removed``: base lines the side cleanly deleted. Each list holds the actual
    line *content* (stripped of trailing newlines), not indices — so the
    validator can check membership in the resolution and the prompt can render
    them. ``empty`` is a convenience for a side that changed nothing.
    """

    added: list[str] = field(default_factory=list)
    changed: list[tuple[str, str]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.added or self.changed or self.removed)

    def summary_lines(self) -> list[str]:
        """A compact human-readable rendering of the obligations.

        Used by the prompt's "must preserve" block and the review bundle. Each
        obligation is rendered with clear visual separation: ``changed`` uses a
        two-line from/to format so small models don't blend the old and new
        states (the single-line ``old -> new`` format wraps and confuses on long
        values). Content is truncated so a large block stays legible. Empty →
        an empty list (the caller omits the block).
        """
        out: list[str] = []
        for ln in self.added:
            out.append(f"added {_trunc(ln)}")
        for old, new in self.changed:
            out.append(f"changed:")
            out.append(f"  from: {_trunc(old)}")
            out.append(f"  to:   {_trunc(new)}")
        for ln in self.removed:
            out.append(f"removed {_trunc(ln)}")
        return out


@dataclass(frozen=True)
class Obligations:
    """Both sides' obligations vs base."""

    current: SideObligations
    replayed: SideObligations


def extract_obligations(unit: "object") -> Obligations:
    """Derive the per-side obligation contract for a conflict unit.

    Diffs each side against the base via :mod:`difflib`. Pure; never raises
    (a unit with missing side text yields empty obligations).

    **Base scoping** (critical for multi-hunk files): ``unit.base.text`` is the
    *entire merge-base file* (the git stage-1 blob), while ``current`` and
    ``replayed`` are just the conflict region's lines. Diffing the whole-file
    base against a narrow hunk produces garbage obligations — "removed:
    everything except these 3 lines" — which actively misleads the model. When
    diff3 refinement is available (``unit.refined_sides``), the refined base is
    the same shape (hunk interior) as the sides, so the diff is meaningful.
    Falls back to the raw ``unit.base.text`` when no refinement is recorded
    (single-hunk conflicts where base == whole file are still correct because
    the sides also span the whole region in that case).

    Returns an :class:`Obligations` carrying the current (upstream) and replayed
    side obligations. An unchanged side yields empty obligations (it conceded —
    nothing to preserve beyond base).
    """
    # Prefer the diff3-refined sides: the refined base is scoped to the conflict
    # hunk (same shape as current/replayed), so the obligation diff is accurate.
    # Without refinement, unit.base.text is the whole merge-base file — fine for
    # single-hunk conflicts where the whole file IS the conflict region, but
    # garbage for multi-hunk files where the hunk is a small slice.
    refined = None
    try:
        refined = unit.refined_sides
    except (AttributeError, TypeError):
        pass
    if refined is not None:
        current, base, replayed = refined
    else:
        base = _text(unit, "base")
        current = _text(unit, "current")
        replayed = _text(unit, "replayed")
    return Obligations(
        current=_side_obligations(base, current),
        replayed=_side_obligations(base, replayed),
    )


def _side_obligations(base: str, side: str) -> SideObligations:
    """What ``side`` did to ``base``, as added/changed/removed line content.

    Walks histogram-diff (:func:`capybase.diff.line_matcher`) opcodes on the line lists:
    - ``insert`` → ``added`` (the side's new lines).
    - ``delete`` → ``removed`` (base lines the side dropped).
    - ``replace`` → ``changed`` (paired old/new lines). A multi-line replace is
      split into per-line pairs (zip), with any excess lines routed to
      ``added`` (side longer) or ``removed`` (base longer) — so nothing is lost.
    """
    base_lines = base.splitlines()
    side_lines = side.splitlines()
    if base_lines == side_lines:
        return SideObligations()  # unchanged side — nothing to preserve

    added: list[str] = []
    changed: list[tuple[str, str]] = []
    removed: list[str] = []

    matcher = line_matcher(base_lines, side_lines)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            added.extend(side_lines[j1:j2])
        elif tag == "delete":
            removed.extend(base_lines[i1:i2])
        elif tag == "replace":
            old = base_lines[i1:i2]
            new = side_lines[j1:j2]
            # Pair up the replaced lines; route the unpaired tail to the right
            # bucket so a length-changing replace still records every line.
            for o, n in zip(old, new):
                changed.append((o, n))
            if len(new) > len(old):
                added.extend(new[len(old):])
            elif len(old) > len(new):
                removed.extend(old[len(new):])
    return SideObligations(added=added, changed=changed, removed=removed)


def obligations_satisfied(
    obligations: Obligations, resolved: str
) -> tuple[bool, list[str]]:
    """Whether ``resolved`` preserves both sides' load-bearing obligations.

    A side's ``added`` and ``changed`` content must appear in the resolution.
    ``removed`` (a deliberate deletion) is HONORED, not required — flagging a
    clean deletion as "missing" would conflict with the modify/delete machinery.
    Returns ``(satisfied, dropped)`` where ``dropped`` lists the specific
    obligations the resolution failed to carry (for the validator's message and
    the review bundle). Empty obligations impose no requirement.
    """
    resolved_lines = resolved.splitlines()
    present = {ln.strip() for ln in resolved_lines if ln.strip()}
    dropped: list[str] = []

    for label, side in (("CURRENT", obligations.current), ("REPLAYED", obligations.replayed)):
        # ADDED content: a whole new line the side introduced must appear in the
        # resolution (a relocated-but-present line still satisfies it).
        for ln in side.added:
            if ln.strip() and ln.strip() not in present:
                dropped.append(f"{label} added: {_trunc(ln)}")
        # CHANGED content: a base line the side edited must NOT still be the old
        # base line in the resolution (that would mean the edit was silently
        # reverted). We do NOT require the side's exact new line to appear —
        # when BOTH sides changed the same base line, the correct resolution is a
        # synthesis of both edits (neither side's exact line), and that synthesis
        # is correct as long as it didn't revert to base. This catches the
        # failure mode the token-set heuristics miss (a modification reverted)
        # without penalizing a valid combined merge.
        for old, new in side.changed:
            if old.strip() and old.strip() in present and new.strip() not in present:
                dropped.append(f"{label} changed (reverted to base): {_trunc(old)} -> {_trunc(new)}")

    return (not dropped, dropped)


def render_obligation_block(obligations: Obligations) -> str:
    """A "must preserve" prompt block for the obligations, or '' if empty.

    Emits one section per non-empty side, each listing its obligations. Returns
    an empty string when both sides are empty (an unchanged/identical conflict)
    so the caller omits the block entirely. Designed to drop into the resolve
    prompt's ``sides_text`` (budget-protected, never trimmed).
    """
    lines: list[str] = []
    if not obligations.current.empty:
        lines.append("CURRENT_UPSTREAM_SIDE must preserve:")
        lines.extend(f"  - {s}" for s in obligations.current.summary_lines())
    if not obligations.replayed.empty:
        lines.append("REPLAYED_COMMIT_SIDE must preserve:")
        lines.extend(f"  - {s}" for s in obligations.replayed.summary_lines())
    if not lines:
        return ""
    return "Side obligations (the load-bearing changes — preserve each):\n" + "\n".join(lines) + "\n\n"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _text(unit: "object", side: str) -> str:
    side_obj = getattr(unit, side, None)
    return getattr(side_obj, "text", "") or ""


def _trunc(line: str, limit: int = 60) -> str:
    """A single-line, length-capped rendering of an obligation line."""
    s = " ".join((line or "").split())  # collapse internal whitespace/newlines
    if len(s) > limit:
        return s[: limit - 1] + "…"
    return s

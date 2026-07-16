"""Structural context annotation for the LLM prompt.

Separated from :mod:`capybase.adapters.abstract_parser` (consolidation #3) so
the parser/diff computation and the prompt-rendering presentation have distinct
homes. This module is a pure consumer of :class:`StructuralDiff3Way` — it
produces a compact text annotation describing what each side changed, whether
there are structural conflicts, and which units must appear in the merge.

``render_structural_context`` is re-exported through ``abstract_parser`` so
existing ``ap.render_structural_context`` call sites keep working.
"""

from __future__ import annotations

# KIND_MODULE_STMT is a parser constant (lives in abstract_parser). The diff
# types (StructuralDiff3Way, _CHANGE_KIND_*) live in structural_diff. Both
# imports are one-directional; abstract_parser re-exports
# render_structural_context from its bottom (after its own symbols are defined),
# so there is no import-time cycle.
from capybase.adapters.abstract_parser import KIND_MODULE_STMT
from capybase.adapters.structural_diff import (
    StructuralDiff3Way,
    _ALL_CHANGE_KINDS,
    _CHANGE_KIND_ADDED_BOTH,
    _CHANGE_KIND_ADDED_BOTH_CONFLICT,
    _CHANGE_KIND_ADDED_LEFT,
    _CHANGE_KIND_ADDED_RIGHT,
    _CHANGE_KIND_DELETED_BOTH,
    _CHANGE_KIND_DELETED_LEFT,
    _CHANGE_KIND_DELETED_RIGHT,
    _CHANGE_KIND_MODIFIED_BOTH,
    _CHANGE_KIND_MODIFIED_LEFT,
    _CHANGE_KIND_MODIFIED_RIGHT,
    _CHANGE_KIND_RENAMED,
    _CHANGE_KIND_UNCHANGED,
)

#: Human-readable labels for change kinds, for the prompt annotation.
_CHANGE_LABELS = {
    _CHANGE_KIND_UNCHANGED: "unchanged",
    _CHANGE_KIND_MODIFIED_LEFT: "MODIFIED by current/upstream",
    _CHANGE_KIND_MODIFIED_RIGHT: "MODIFIED by replayed",
    _CHANGE_KIND_MODIFIED_BOTH: "MODIFIED BY BOTH SIDES",
    _CHANGE_KIND_ADDED_LEFT: "ADDED by current/upstream",
    _CHANGE_KIND_ADDED_RIGHT: "ADDED by replayed",
    _CHANGE_KIND_ADDED_BOTH: "ADDED BY BOTH SIDES",
    _CHANGE_KIND_ADDED_BOTH_CONFLICT: "ADDED BY BOTH SIDES (different bodies)",
    _CHANGE_KIND_DELETED_LEFT: "deleted by current/upstream",
    _CHANGE_KIND_DELETED_RIGHT: "deleted by replayed",
    _CHANGE_KIND_DELETED_BOTH: "deleted by both",
    _CHANGE_KIND_RENAMED: "RENAMED",
}
# Enforce that every change kind has a label — adding a kind to
# ``_ALL_CHANGE_KINDS`` without a label here fails loudly at import (the
# parallel-list smell is now an enforced invariant, not a silent drift risk).
assert set(_CHANGE_LABELS) == _ALL_CHANGE_KINDS, (
    f"_CHANGE_LABELS keys {set(_CHANGE_LABELS)!r} != _ALL_CHANGE_KINDS "
    f"{set(_ALL_CHANGE_KINDS)!r} — every change kind needs a label"
)


def _render_import_surface(diff: StructuralDiff3Way) -> str:
    """Render the import-surface change block, or "" when no import changed.

    Surveys of structured-merge tools find import handling is the single
    highest-value structural operation: an imports-only merger outperformed
    complex structured tools. The correct merge of imports is almost always the
    UNION of both sides' additions (each side's imports are independently
    needed) minus only genuine removes. This block makes that explicit instead
    of leaving the model to infer it from generic per-unit lines.

    Output shape (only the populated lines appear)::

        Import surface: CURRENT adds json; REPLAYED adds sys — union them
        → merged imports must include: os, json, sys

    A remove by one side is called out separately so the model knows the union
    is NOT always the whole set. Returns "" when no import unit changed (the
    common no-import-conflict case) so the annotation is unchanged there.
    """
    cur_adds: list[str] = []
    rep_adds: list[str] = []
    cur_drops: list[str] = []
    rep_drops: list[str] = []
    # The full set of imports that must survive in the merge: every import
    # present in base, left, or right, minus those a side deliberately removed.
    survivors: list[str] = []
    seen: set[str] = set()

    def remember(name: str) -> None:
        if name and name not in seen and name != "<import>":
            seen.add(name)
            survivors.append(name)

    for a in diff.aligned:
        if a.kind != KIND_MODULE_STMT:
            continue
        ck = a.change_kind
        if ck == _CHANGE_KIND_ADDED_LEFT:
            cur_adds.append(a.name)
        elif ck == _CHANGE_KIND_ADDED_RIGHT:
            rep_adds.append(a.name)
        elif ck == _CHANGE_KIND_ADDED_BOTH:
            cur_adds.append(a.name)
            rep_adds.append(a.name)
        elif ck == _CHANGE_KIND_ADDED_BOTH_CONFLICT:
            # Both sides added this import with divergent bodies — a conflict,
            # NOT a simple union. Surface it in both lists with a conflict note.
            cur_adds.append(f"{a.name} (CONFLICT — divergent)")
            rep_adds.append(f"{a.name} (CONFLICT — divergent)")
        elif ck == _CHANGE_KIND_DELETED_LEFT:
            rep_drops.append(a.name)
        elif ck == _CHANGE_KIND_DELETED_RIGHT:
            cur_drops.append(a.name)
        elif ck == _CHANGE_KIND_DELETED_BOTH:
            pass  # removed by both — not a survivor
        # Track survivors (union of all sides' present imports).
        if a.base is not None:
            remember(a.name)
        if a.left is not None:
            remember(a.name)
        if a.right is not None:
            remember(a.name)

    if not cur_adds and not rep_adds and not cur_drops and not rep_drops:
        return ""  # no import-surface change — leave the annotation unchanged

    parts: list[str] = []
    if cur_adds:
        parts.append(f"CURRENT adds {', '.join(cur_adds)}")
    if rep_adds:
        parts.append(f"REPLAYED adds {', '.join(rep_adds)}")
    if cur_drops:
        parts.append(f"CURRENT removes {', '.join(cur_drops)}")
    if rep_drops:
        parts.append(f"REPLAYED removes {', '.join(rep_drops)}")
    head = "Import surface: " + "; ".join(parts)
    # When both sides only ADD imports (no conflicts), the merge rule is
    # unambiguous: union. But if any import is a divergent conflict, don't
    # emit the "union them" suffix — it contradicts the conflict annotation.
    has_conflict = any(
        "(CONFLICT" in s for s in cur_adds + rep_adds
    )
    if not cur_drops and not rep_drops and (cur_adds or rep_adds) and not has_conflict:
        head += " — union them (imports are additive; keep every side's adds)"
    out = [head]
    if survivors:
        out.append(f"→ merged imports must include: {', '.join(survivors)}")
    return "\n".join(out)



def render_structural_context(
    diff: StructuralDiff3Way,
    conflict_span: tuple[int, int] | None = None,
) -> str:
    """Render a structural context annotation block for the LLM prompt.

    Produces a compact summary of the 3-way structural alignment: which units
    exist, what each side changed, whether there are structural conflicts (both
    sides modified the same unit), and which units must appear in the merge.
    Omitted (returns "") when the diff has no useful signal (e.g. single-unit
    files with no changes). ``conflict_span`` optionally annotates which unit
    the conflict markers fall inside.

    This directly addresses the "dropped replayed side" failure mode: the model
    sees unit boundaries and required outputs explicitly before generating.
    Returns ``""`` on a ``None`` diff (the structural analysis declined).
    """
    if diff is None:
        return ""
    lines: list[str] = []
    # Only show units that changed (not unchanged) — the model doesn't need to
    # see a list of everything that stayed the same.
    changed = [a for a in diff.aligned if a.change_kind != _CHANGE_KIND_UNCHANGED]
    if not changed:
        return ""  # no structural signal — nothing changed at the entity level

    lang_label = diff.language or diff.family
    lines.append(f"STRUCTURAL CONTEXT (language-family: {lang_label}/{diff.family}):")

    # Base structure overview (compact) — imports are summarized in their own
    # dedicated block below, so exclude them here to avoid double-listing.
    base_summary = ", ".join(
        f"[{u.kind.upper()}] {u.name} lines {u.span[0]+1}-{u.span[1]+1}"
        for u in diff.base_units
        if u.name and not u.is_container_scope and u.kind != KIND_MODULE_STMT
    )
    if base_summary:
        lines.append(f"Base structure: {base_summary}")

    # Import-surface block "imports-only tool outperformed complex
    # structured tools — import conflict handling is the single highest-value
    # structural operation"). Imports are the one unit kind where the correct
    # merge is almost always the UNION of both sides' adds minus genuine removes;
    # make that instruction explicit instead of leaving the model to infer it
    # from a generic "[MODULE_STMT] json: ADDED" line. Emits only when at least
    # one import unit changed.
    import_block = _render_import_surface(diff)
    if import_block:
        lines.append(import_block)

    # Per-unit change summary — skip imports (already handled above) so the
    # entity changes read as the code changes they are, not import noise.
    for a in changed:
        if a.kind == KIND_MODULE_STMT:
            continue
        label = _CHANGE_LABELS.get(a.change_kind, a.change_kind)
        # For added_both_conflict, show both sides' names (a divergent rename
        # conflict has two distinct names the LLM must reconcile).
        if a.change_kind == _CHANGE_KIND_ADDED_BOTH_CONFLICT and a.left and a.right and a.left.name != a.right.name:
            lines.append(f"  {a.kind.upper()} {a.left.name} / {a.right.name}: {label}")
        else:
            lines.append(f"  {a.kind.upper()} {a.name}: {label}")

    # Structural conflicts: units both sides modified.
    conflicts = diff.structural_conflicts
    if conflicts:
        names = ", ".join(
            f"{c.left.name}/{c.right.name}"
            if c.change_kind == _CHANGE_KIND_ADDED_BOTH_CONFLICT and c.left and c.right and c.left.name != c.right.name
            else c.name
            for c in conflicts
        )
        lines.append(
            f"Structural conflicts: {len(conflicts)} unit(s) modified by both sides ({names}) — "
            "synthesize both changes."
        )
    else:
        lines.append(
            "Structural conflicts: NONE (modifications are in separate units) — "
            "preserve each side's changes independently."
        )

    # Required units.
    required = diff.required_units
    if required:
        lines.append(f"Required: preserve these units in the merged output: {', '.join(required)}")

    # Span annotation: which unit does the conflict fall inside?
    if conflict_span is not None:
        # Find the unit in base whose span contains the conflict anchor.
        anchor = conflict_span[0]
        enclosing = None
        for u in diff.base_units:
            if u.span[0] <= anchor <= u.span[1]:
                if enclosing is None or (u.span[0] >= enclosing.span[0] and u.span[1] <= enclosing.span[1]):
                    enclosing = u
        if enclosing and enclosing.name:
            lines.append(f"This conflict is inside: {enclosing.kind.upper()} {enclosing.name}")

    return "\n".join(lines)

"""3-way structural diff (phase 3).

Separated from :mod:`capybase.adapters.abstract_parser` (consolidation #3) so
the parser/IR and the 3-way diff computation have distinct homes. This module
is a pure consumer of the parser's :class:`StructuralUnit` / :class:`FileIR` —
it aligns units across base/left/right by ``(kind, name)``, classifies each
alignment (``modified_both``, ``added_left``, etc.), and detects renames via
body-fingerprint matching.

The diff types (``AlignedUnit``, ``StructuralDiff3Way``, the ``_CHANGE_KIND_*``
constants) are re-exported through ``abstract_parser`` so existing
``ap.compute_structural_diff_3way`` / ``ap.StructuralDiff3Way`` / ``ap._CHANGE_KIND_*``
call sites keep working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from capybase.adapters.abstract_parser import (
    KIND_UNKNOWN,
    StructuralUnit,
    _fingerprint_has_content,
    _has_code_content,
    all_units_flat,
    has_duplicate_identities,
    parse_file,
)


# ---------------------------------------------------------------------------
# Aligned unit + change-kind vocabulary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlignedUnit:
    """One unit aligned across the three versions (base/left/right).

    Each field is the :class:`StructuralUnit` from that version, or ``None``
    when absent. ``change_kind`` classifies the alignment for the LLM prompt.
    """
    base: StructuralUnit | None
    left: StructuralUnit | None
    right: StructuralUnit | None
    change_kind: str  # see _CHANGE_KIND_* constants below

    @property
    def name(self) -> str:
        """The best available name for this aligned unit (for display)."""
        for u in (self.left, self.right, self.base):
            if u is not None and u.name:
                return u.name
        return "<anon>"

    @property
    def kind(self) -> str:
        """The best available kind for this aligned unit."""
        for u in (self.left, self.right, self.base):
            if u is not None:
                return u.kind
        return KIND_UNKNOWN


_CHANGE_KIND_UNCHANGED = "unchanged"
_CHANGE_KIND_MODIFIED_LEFT = "modified_left"
_CHANGE_KIND_MODIFIED_RIGHT = "modified_right"
_CHANGE_KIND_MODIFIED_BOTH = "modified_both"
_CHANGE_KIND_ADDED_LEFT = "added_left"
_CHANGE_KIND_ADDED_RIGHT = "added_right"
_CHANGE_KIND_ADDED_BOTH = "added_both"
#: Both sides added a unit of the same name with DIFFERENT bodies — a genuine
#: conflict (each side's addition is incompatible). Distinct from
#: ``added_both`` (identical bodies, an agreed addition). Fix #7.
_CHANGE_KIND_ADDED_BOTH_CONFLICT = "added_both_conflict"
_CHANGE_KIND_DELETED_LEFT = "deleted_left"
_CHANGE_KIND_DELETED_RIGHT = "deleted_right"
_CHANGE_KIND_DELETED_BOTH = "deleted_both"
_CHANGE_KIND_RENAMED = "renamed"

#: All change kinds. The single source of truth — ``required_units`` and
#: ``structural_conflicts`` derive from this (and ``_CHANGE_LABELS`` in
#: structural_context asserts its keys match), so adding a new kind is a
#: one-line append here, not a parallel-list edit in three places.
_ALL_CHANGE_KINDS = frozenset({
    _CHANGE_KIND_UNCHANGED,
    _CHANGE_KIND_MODIFIED_LEFT,
    _CHANGE_KIND_MODIFIED_RIGHT,
    _CHANGE_KIND_MODIFIED_BOTH,
    _CHANGE_KIND_ADDED_LEFT,
    _CHANGE_KIND_ADDED_RIGHT,
    _CHANGE_KIND_ADDED_BOTH,
    _CHANGE_KIND_ADDED_BOTH_CONFLICT,
    _CHANGE_KIND_DELETED_LEFT,
    _CHANGE_KIND_DELETED_RIGHT,
    _CHANGE_KIND_DELETED_BOTH,
    _CHANGE_KIND_RENAMED,
})
#: Change kinds whose unit SURVIVES in the merge (everything except
#: ``deleted_both`` — the only case where the unit is truly gone). Drives
#: ``StructuralDiff3Way.required_units``.
_SURVIVING_CHANGE_KINDS = _ALL_CHANGE_KINDS - {_CHANGE_KIND_DELETED_BOTH}
#: Change kinds that count as a structural conflict (both sides touched the
#: same unit, or both added the same name with incompatible bodies). Drives
#: ``StructuralDiff3Way.structural_conflicts``.
_CONFLICT_CHANGE_KINDS = frozenset({_CHANGE_KIND_MODIFIED_BOTH, _CHANGE_KIND_ADDED_BOTH_CONFLICT})


@dataclass(frozen=True)
class StructuralDiff3Way:
    """3-way alignment of a file's structural units across base/left/right.

    ``aligned`` is the list of :class:`AlignedUnit` entries, each carrying the
    base/left/right unit (or None) and a ``change_kind`` classification. This is
    the data structure the structural context annotation (Improvement #6) is
    built from — it tells the model "both sides modified the same function" or
    "left added a unit, right added a different unit" (no structural conflict).
    """
    base_units: list[StructuralUnit]
    left_units: list[StructuralUnit]
    right_units: list[StructuralUnit]
    aligned: list[AlignedUnit]
    family: str
    language: str | None = None

    @property
    def structural_conflicts(self) -> list[AlignedUnit]:
        """Alignments where BOTH sides modified the SAME unit, or both sides
        added the same name with conflicting bodies (potential conflict)."""
        return [
            a for a in self.aligned
            if a.change_kind in _CONFLICT_CHANGE_KINDS
        ]

    @property
    def required_units(self) -> list[str]:
        """Names of units that must appear in the merged output (deduplicated).

        Includes ``deleted_left`` / ``deleted_right``: those mean one side
        deleted the unit but the OTHER side kept (and possibly modified) it —
        so the unit SURVIVES in the merge as the keeping side's version.
        Excluding them risked the LLM dropping a surviving unit. Only
        ``deleted_both`` (both sides removed it) is truly absent from the merge.

        For ``added_both_conflict``, BOTH the left and right names are emitted
        (a divergent-name rename conflict has two distinct surviving names).
        Deduplicated to avoid listing the same name twice.
        """
        names: list[str] = []
        for a in self.aligned:
            if a.change_kind not in _SURVIVING_CHANGE_KINDS:
                continue
            # Skip imports (MODULE_STMT) — they have their own Import-surface
            # block in the rendered context, not the Required-units list.
            if a.kind == "module_stmt":
                continue
            if a.change_kind == _CHANGE_KIND_ADDED_BOTH_CONFLICT:
                # Both sides' names survive (divergent rename targets).
                for side in (a.left, a.right):
                    if side is not None and side.name and side.name != "<anon>":
                        names.append(side.name)
            elif a.name and a.name != "<anon>":
                names.append(a.name)
        return list(dict.fromkeys(names))


# ---------------------------------------------------------------------------
# Body comparison (change-detection, NOT rename-detection)
# ---------------------------------------------------------------------------


def _normalize_body_ws_only(text: str, *, lang: str | None = None) -> str:
    """Whitespace-collapse WITHOUT blanking string literals or stripping comments.

    Used by :func:`_bodies_differ` for change detection: a string-value change
    (``return 'hi'`` vs ``return 'bye'``) IS a real body change for merge
    purposes, so we preserve string content. Only whitespace is normalized so
    reformatting doesn't register as a change.

    ``lang`` selects which comment-only lines are stripped (``//`` for Family-A,
    ``#`` for Python/Ruby) — otherwise a Rust ``#[cfg(test)]`` or C ``#define``
    line is wrongly dropped as a Python comment, masking a real change.
    """
    if not text:
        return ""
    # Strip comment-only lines (with multi-line block-comment state), then
    # collapse whitespace — but keep string literals intact.
    from capybase.adapters.abstract_parser import _filter_code_lines
    kept = _filter_code_lines(text.split("\n"), lang=lang)
    return " ".join(" ".join(kept).split())


def _bodies_differ(a: StructuralUnit, b: StructuralUnit, *, lang: str | None = None) -> bool:
    """True if two units' bodies differ.

    Uses a whitespace-normalized comparison that preserves string-literal
    content (unlike the body fingerprint, which blanks strings for rename
    matching). This ensures a string-value edit registers as a real change.
    """
    return _normalize_body_ws_only(a.body, lang=lang) != _normalize_body_ws_only(b.body, lang=lang)


# ---------------------------------------------------------------------------
# Alignment + classification
# ---------------------------------------------------------------------------


def compute_structural_diff_3way(
    base: str, left: str, right: str, language: str | None = None,
) -> StructuralDiff3Way | None:
    """Compute a 3-way structural alignment across base/left/right source texts.

    Parses each version into a :class:`FileIR`, flattens to top-level units, and
    aligns by ``(kind, name)`` with fingerprint fallback for rename detection.
    Each alignment is classified (``modified_both``, ``added_left``, etc.) to
    drive the structural context annotation. Returns ``None`` when parsing fails
    or the language has no family mapping.
    """
    ir_base = parse_file(base, language=language)
    ir_left = parse_file(left, language=language)
    ir_right = parse_file(right, language=language)
    if ir_base is None or ir_left is None or ir_right is None:
        return None
    # A structural annotation built from a minified/garbage parse (confidence
    # 0.0) is worse than no annotation — it would feed the LLM empty/wrong
    # structure as authoritative. Decline when any side is untrustworthy.
    if ir_base.parse_confidence == 0.0 or ir_left.parse_confidence == 0.0 \
            or ir_right.parse_confidence == 0.0:
        return None
    family = ir_base.family
    # Flatten to include nested children (methods inside classes/impls) so the
    # alignment operates at the entity level the LLM merges at — not just
    # top-level containers. Container-scope units (impl/mod) are skipped but
    # their children are walked (mirrors all_units_flat).
    base_units = all_units_flat(ir_base)
    left_units = all_units_flat(ir_left)
    right_units = all_units_flat(ir_right)

    # Decline on duplicate identities: two units sharing an identity
    # (e.g. Java/C++/Python method overloads, re-definitions) would collide
    # silently in the identity-keyed dicts below, dropping all but one — a
    # missed-conflict data-loss bug. Decline so the caller escalates to the LLM.
    if (
        has_duplicate_identities(base_units)
        or has_duplicate_identities(left_units)
        or has_duplicate_identities(right_units)
    ):
        return None

    # Index by identity (kind, name) for O(1) lookup.
    base_by_id = {u.identity: u for u in base_units}
    left_by_id = {u.identity: u for u in left_units}
    right_by_id = {u.identity: u for u in right_units}

    # Collect all identities across the three versions, preserving source order
    # (base first, then left additions, then right additions).
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    for u in base_units:
        if u.identity not in seen:
            seen.add(u.identity)
            ordered.append(u.identity)
    for u in left_units:
        if u.identity not in seen:
            seen.add(u.identity)
            ordered.append(u.identity)
    for u in right_units:
        if u.identity not in seen:
            seen.add(u.identity)
            ordered.append(u.identity)

    aligned: list[AlignedUnit] = []
    for ident in ordered:
        b = base_by_id.get(ident)
        l = left_by_id.get(ident)
        r = right_by_id.get(ident)
        kind = _classify_alignment(b, l, r, lang=language)
        aligned.append(AlignedUnit(base=b, left=l, right=r, change_kind=kind))

    # Rename detection: left or right units not matched by identity but with a
    # matching body fingerprint to a base unit. This is a secondary pass — the
    # identity-matched alignments are already done; here we pair unmatched units.
    _detect_renames(base_units, left_units, right_units, aligned, lang=language)

    return StructuralDiff3Way(
        base_units=base_units,
        left_units=left_units,
        right_units=right_units,
        aligned=aligned,
        family=family,
        language=language,
    )


def _classify_alignment(
    base: StructuralUnit | None,
    left: StructuralUnit | None,
    right: StructuralUnit | None,
    *,
    lang: str | None = None,
) -> str:
    """Classify a 3-way alignment into a change-kind label."""
    has_b = base is not None
    has_l = left is not None
    has_r = right is not None

    if has_b and has_l and has_r:
        l_changed = _bodies_differ(base, left, lang=lang)
        r_changed = _bodies_differ(base, right, lang=lang)
        if l_changed and r_changed:
            return _CHANGE_KIND_MODIFIED_BOTH
        if l_changed:
            return _CHANGE_KIND_MODIFIED_LEFT
        if r_changed:
            return _CHANGE_KIND_MODIFIED_RIGHT
        return _CHANGE_KIND_UNCHANGED
    if not has_b and has_l and has_r:
        # Both sides added a unit of this name. Sub-classify: identical bodies
        # = an agreed addition (not a conflict); differing bodies = a genuine
        # conflict (previously both were ``added_both`` and neither was flagged
        # as a structural conflict, silently missing the clash).
        if _bodies_differ(left, right, lang=lang):
            return _CHANGE_KIND_ADDED_BOTH_CONFLICT
        return _CHANGE_KIND_ADDED_BOTH
    if not has_b and has_l and not has_r:
        return _CHANGE_KIND_ADDED_LEFT
    if not has_b and not has_l and has_r:
        return _CHANGE_KIND_ADDED_RIGHT
    if has_b and not has_l and not has_r:
        return _CHANGE_KIND_DELETED_BOTH
    if has_b and not has_l and has_r:
        # Deleted by left, present in right (and base) — right kept it.
        return _CHANGE_KIND_DELETED_LEFT if _bodies_differ(base, right, lang=lang) else _CHANGE_KIND_UNCHANGED
    if has_b and has_l and not has_r:
        # Deleted by right, present in left (and base) — left kept it.
        return _CHANGE_KIND_DELETED_RIGHT if _bodies_differ(base, left, lang=lang) else _CHANGE_KIND_UNCHANGED
    return _CHANGE_KIND_UNCHANGED


# ---------------------------------------------------------------------------
# 3-way rename detection (body-fingerprint keyed)
# ---------------------------------------------------------------------------


def _detect_renames(
    base_units: list[StructuralUnit],
    left_units: list[StructuralUnit],
    right_units: list[StructuralUnit],
    aligned: list[AlignedUnit],
    *,
    lang: str | None = None,
) -> None:
    """Detect renamed units via body-fingerprint matching and re-pair them.

    A unit classified as ``added_*`` (no base counterpart by identity) whose body
    fingerprint matches a ``deleted_*`` base unit is a rename — the identity pass
    saw the new name as a pure addition and the old name as a deletion, but the
    bodies match, so it's really a rename. This re-pairs those alignments into
    ``RENAMED`` entries (mutating ``aligned`` in place: the stale added/deleted
    entries are removed and the rename entry appended).

    Conservative — pairs only on exact body-fingerprint match (the header-stripped
    digest), so a rename + heavy body edit won't pair (stays added+removed, safe).

    The body fingerprint is the hash of :func:`entity_body_content` (the canonical
    rename signal, consolidation #2), so this 3-way pairing is consistent with the
    2-way :func:`detect_renames_2way` the resolver and ``semantic_diff`` share —
    fingerprint equality ⟺ body-content equality. This version keys on the
    precomputed digest (cheaper: fingerprints are baked into the units at parse
    time) and applies a 3-way-specific "deleted by BOTH sides" constraint that the
    2-way core doesn't have, so it is kept as a distinct 3-way entry point rather
    than forced through the 2-way signature.
    """
    # Index DELETED base units by body fingerprint. A base unit is a rename
    # candidate only when it's gone from the side in question (classified as a
    # deletion), NOT when it's identity-matched (present under its original name).
    # Skip content-less bodies: distinct empty bodies share ``l0``.
    base_by_fp: dict[str, list[StructuralUnit]] = {}
    for u in base_units:
        if _fingerprint_has_content(u.fingerprint):
            # Collect ALL base units sharing a fingerprint (duplicate bodies).
            # _try_pair_side iterates to find a deleted, unconsumed one, so a
            # rename of ANY dup-bodied base is found — not just the first.
            base_by_fp.setdefault(u.fingerprint, []).append(u)

    # Base identities deleted by each side (no side entry under the original name).
    # A base unit is a rename candidate only when it's gone from the side in
    # question (classified as a deletion), NOT when it's identity-matched.
    deleted_base_ids = {
        a.base.identity for a in aligned
        if a.base is not None and a.left is None and a.right is None
    }

    # Rename candidates on each side: units classified as added (no base) whose
    # fingerprint matches a deleted base unit. Pair them and remove the stale
    # added/deleted alignments, replacing with a RENAMED entry.
    consumed_base_ids: set = set()
    consumed_side_ids: set = set()
    new_entries: list[AlignedUnit] = []
    indices_to_drop: set[int] = set()
    # Track which base each side renamed, and to what new identity — so a
    # both-sides rename to DIFFERENT names (left foo->bar, right foo->baz) can
    # be surfaced as a conflict rather than silently leaving the second as a
    # plain addition.
    rename_by_base: dict = {}  # base_id -> (new_identity, side_unit, side)

    def _try_pair_side(side_unit: StructuralUnit, side: str) -> None:
        nonlocal new_entries
        if side_unit.identity in consumed_side_ids:
            return
        # A side unit already identity-matched to a base (same name) is NOT a
        # rename candidate — it's a genuine modify/unchanged, even if its body
        # fingerprint coincidentally matches another base unit (duplicate bodies).
        if side_unit.identity in identity_matched_side_ids:
            return
        if not _fingerprint_has_content(side_unit.fingerprint):
            return
        base_match = base_by_fp.get(side_unit.fingerprint)
        if not base_match:
            return
        # Iterate the dup-bodied candidates to find a deleted, unconsumed one.
        # This finds a rename of ANY base sharing the fingerprint — not just the
        # first. When all candidates are consumed by the OTHER side, check for a
        # cross-side conflict (same base, different new name on each side).
        chosen: StructuralUnit | None = None
        conflict_base: StructuralUnit | None = None
        for cand in base_match:
            if cand.identity in consumed_base_ids:
                # Already consumed — remember it for the cross-side-conflict
                # check (below) if no fresh candidate is found.
                if conflict_base is None:
                    prior = rename_by_base.get(cand.identity)
                    if prior is not None and prior[0] != side_unit.identity:
                        conflict_base = cand
                continue
            if cand.identity not in deleted_base_ids:
                continue
            chosen = cand
            break
        if chosen is None:
            # No fresh deleted candidate. If a consumed base was renamed by the
            # OTHER side to a different name, that's a cross-side conflict.
            if conflict_base is not None:
                prior = rename_by_base.get(conflict_base.identity)
                # Only a CROSS-side prior (different side) is a real conflict —
                # a same-side prior means two same-side units matched the same
                # base, which is just a duplicate (leave the 2nd as added_*).
                if prior is not None and prior[2] != side:
                    new_entries = [
                        e for e in new_entries
                        if not (e.change_kind == _CHANGE_KIND_RENAMED
                                and e.base is not None
                                and e.base.identity == conflict_base.identity)
                    ]
                    if side == "left":
                        conflict = AlignedUnit(
                            base=conflict_base, left=side_unit, right=prior[1],
                            change_kind=_CHANGE_KIND_ADDED_BOTH_CONFLICT,
                        )
                    else:
                        conflict = AlignedUnit(
                            base=conflict_base, left=prior[1], right=side_unit,
                            change_kind=_CHANGE_KIND_ADDED_BOTH_CONFLICT,
                        )
                    new_entries.append(conflict)
                    consumed_side_ids.add(side_unit.identity)
            return
        base_match = chosen
        # Find the other side's entry for this new name (agreed rename?).
        other = left_units if side == "right" else right_units
        other_match = next((u for u in other if u.identity == side_unit.identity), None)
        # When BOTH sides have the new name, sub-classify: an AGREED rename
        # (both sides renamed identically) is non-conflicting, but if the two
        # sides' bodies DIVERGE, it's a rename-conflict. Collapsing a divergent
        # pair into a single RENAMED entry would drop the conflict (RENAMED is
        # not in _CONFLICT_CHANGE_KINDS), telling the LLM there's nothing to
        # resolve. In that case, skip the pairing and leave the
        # added_both_conflict classification the identity pass already produced.
        if other_match is not None and _bodies_differ(side_unit, other_match, lang=lang):
            return
        # Build the RENAMED entry.
        if side == "left":
            new_entries.append(AlignedUnit(
                base=base_match, left=side_unit, right=other_match,
                change_kind=_CHANGE_KIND_RENAMED,
            ))
        else:
            new_entries.append(AlignedUnit(
                base=base_match,
                left=next((u for u in left_units if u.identity == side_unit.identity), None),
                right=side_unit,
                change_kind=_CHANGE_KIND_RENAMED,
            ))
        consumed_base_ids.add(base_match.identity)
        consumed_side_ids.add(side_unit.identity)
        rename_by_base[base_match.identity] = (side_unit.identity, side_unit, side)

    # Side identities already identity-matched to a base unit (same name) are
    # NOT rename candidates — they're genuine modifications/unchanged, not
    # renames. Without this guard, a side unit whose body happens to match a
    # DIFFERENT base unit's fingerprint (two base fns with identical bodies)
    # would be mis-paired as a rename of that other base.
    identity_matched_side_ids = {
        a.left.identity for a in aligned
        if a.left is not None and a.base is not None
    } | {
        a.right.identity for a in aligned
        if a.right is not None and a.base is not None
    }

    # Mark the stale added/deleted alignment indices for removal as we pair.
    # First, index alignments by their side identities for quick lookup.
    for lu in left_units:
        _try_pair_side(lu, "left")
    for ru in right_units:
        _try_pair_side(ru, "right")

    if not new_entries:
        return

    # Remove the stale added_left/added_right entries for consumed side units,
    # and the deleted_both entries for consumed base units.
    for idx, a in enumerate(aligned):
        # Drop added entries whose side unit was consumed as a rename target.
        if a.left is not None and a.base is None and a.left.identity in consumed_side_ids:
            indices_to_drop.add(idx)
        elif a.right is not None and a.base is None and a.right.identity in consumed_side_ids:
            indices_to_drop.add(idx)
        # Drop the deleted_both entry for a consumed base unit.
        elif (
            a.base is not None and a.left is None and a.right is None
            and a.base.identity in consumed_base_ids
        ):
            indices_to_drop.add(idx)
    # Rebuild aligned without the stale entries, then append the renames.
    aligned[:] = [a for idx, a in enumerate(aligned) if idx not in indices_to_drop]
    aligned.extend(new_entries)

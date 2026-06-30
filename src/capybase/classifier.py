"""First-class conflict classification: a difficulty band + explainable reasons.

A pure downstream consumer of the cheap pre-LLM signals already computed at
extraction (``conflict_features``, ``severity``, ``merge_direction``). It never
touches git or the model, so it's exhaustively unit-testable in isolation and
adds no cost to the resolve loop — the signals are already cached on the unit's
``structural_metadata`` by :mod:`conflict_extractor`.

Why a classifier (beyond the old bare ``simple/complex`` label)?

- **Auditable routing.** Every classification carries the *reasons* that drove
  it (e.g. "both sides edit the same identifier (foo)", "hunk is large (52
  lines, touches def)"). Routing decisions can be reviewed and tuned without
  reading the orchestrator.
- **Finer bands for policy.** ``trivial`` / ``easy`` / ``medium`` / ``hard``
  let future work (#10 unattended policy, #6 test selection) branch more
  precisely than a binary label. The legacy ``simple``/``complex`` is preserved
  (``complex`` ⟺ band ∈ {medium, hard}) so nothing that consumed the old label
  breaks.
- **Deterministic-merge awareness.** A ``deterministically_mergeable`` flag
  (from :mod:`structural_resolver`'s feasibility probe) lets the classifier mark
  a union-combine conflict as ``trivial`` even when both sides changed — those
  need no LLM judgment at all.

Everything here is a pure function of the unit (already-extracted data); there
is no re-computation of expensive signals beyond a light signature scan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# Legacy label kept for backward compatibility with the routing/risk consumers.
# ``complex`` ⟺ the band is medium or hard; ``simple`` otherwise.
Difficulty = Literal["simple", "complex"]

# The richer routing band. Ordered by required scrutiny:
#   trivial — no judgment needed (identical/one-sided/deterministically-mergeable)
#   easy    — disjoint small edits, no shared-symbol tension
#   medium  — touches a definition, or same-line overlap, or moderate size
#   hard    — large + definition-touching + same-symbol overlap, or a modify/delete
#             whose keeper modified (a real keep-vs-delete judgment)
Band = Literal["trivial", "easy", "medium", "hard"]

# Hunk-size thresholds (in non-blank side lines). Kept modest: a 3B local model
# resolves small hunks reliably; the thresholds gate where its reliability drops.
_MEDIUM_HUNK_LINES = 16
_HARD_HUNK_LINES = 40


@dataclass(frozen=True)
class ConflictClassification:
    """A conflict's routing verdict + the reasons behind it.

    ``difficulty`` is the drop-in replacement for the legacy ``simple``/``complex``
    label (``complex`` ⟺ band ∈ {medium, hard}). ``band`` is the finer-grained
    routing target. ``reasons`` is a human-readable audit trail — each string
    cites the feature that drove the band, so a routing decision can be reviewed
    without reading code. ``features`` is the reused signal snapshot (size,
    severity, merge_kind, etc.) for downstream policy/calibration.
    """

    difficulty: Difficulty
    band: Band
    reasons: list[str] = field(default_factory=list)
    features: dict[str, Any] = field(default_factory=dict)


def classify(unit: "object", config: "object | None" = None) -> ConflictClassification:
    """Classify a conflict into a difficulty band with explainable reasons.

    Pure: reads already-cached extraction signals (``conflict_features``,
    ``merge_direction``, ``severity``) off the unit's ``structural_metadata``,
    recomputing only the cheap same-identifier-overlap signal from signatures if
    needed. ``config`` is accepted for signature parity with the legacy
    ``classify_difficulty`` and future thresholds; it's currently unused (the
    thresholds here are module constants).

    Returns a :class:`ConflictClassification` with the legacy ``difficulty``
    label (backward-compatible), the richer ``band``, and the ``reasons`` list.
    Never raises — a unit missing its cached signals degrades to ``medium``
    with a "missing signals" reason rather than crashing routing.
    """
    feats = _features(unit)
    reasons: list[str] = []

    size = _as_int(feats.get("hunk_size"))
    touches_def = bool(feats.get("touches_definition"))
    same_line_overlap = bool(feats.get("same_line_overlap"))
    severity = str(feats.get("severity") or "medium")
    merge_kind = str(feats.get("merge_kind") or "both_modify")
    modify_delete = bool(feats.get("modify_delete"))
    det_mergeable = _deterministically_mergeable(unit)

    # --- Trivial: no judgment needed ----------------------------------------
    # Identical/near-identical sides, one-sided change, or a deterministic union
    # the resolver can merge with zero LLM calls. These route to the cheap path.
    if det_mergeable:
        reasons.append("deterministically mergeable (a union/one-sided rule applies)")
        band: Band = "trivial"
    elif merge_kind in ("both_unchanged", "one_unchanged"):
        reasons.append(f"one/both sides unchanged ({merge_kind})")
        band = "trivial"
    elif merge_kind == "delete_delete":
        reasons.append("both sides deleted (no ambiguity)")
        band = "trivial"
    else:
        band = _classify_nontrivial(
            size=size,
            touches_def=touches_def,
            same_line_overlap=same_line_overlap,
            same_symbol_overlap=_same_symbol_overlap(unit),
            severity=severity,
            merge_kind=merge_kind,
            modify_delete=modify_delete,
            reasons=reasons,
        )

    difficulty: Difficulty = "complex" if band in ("medium", "hard") else "simple"
    return ConflictClassification(
        difficulty=difficulty,
        band=band,
        reasons=reasons,
        features=feats,
    )


def _classify_nontrivial(
    *,
    size: int,
    touches_def: bool,
    same_line_overlap: bool,
    same_symbol_overlap: bool,
    severity: str,
    merge_kind: str,
    modify_delete: bool,
    reasons: list[str],
) -> Band:
    """Band a conflict that needs *some* judgment (not deterministically trivial).

    Hard is reserved for the cases a small model is most likely to get wrong:
    large + definition-touching + same-symbol overlap, or a modify/delete whose
    keeper *modified* (a genuine keep-vs-delete judgment, not an auto-accept).
    """
    # Hard signals (accumulate reasons; any one can promote to hard).
    hard = False
    if size >= _HARD_HUNK_LINES:
        reasons.append(f"hunk is large ({size} lines)")
        hard = True
    if touches_def:
        reasons.append("touches a function/class definition")
        hard = True
    if same_symbol_overlap:
        reasons.append("both sides edit the same identifier")
        hard = True
    if same_line_overlap:
        reasons.append("both sides changed the same base line(s)")
        hard = True
    if severity == "high":
        reasons.append("graded high-severity")
        hard = True
    # A modify/delete with a *modified* keeper is a real judgment; a delete vs
    # an unchanged keeper is auto-accepted by the structural rule (trivial).
    if modify_delete and merge_kind == "modify_delete":
        reasons.append("modify/delete with a modified keeper (keep-vs-delete judgment)")
        hard = True

    # Promote to hard only when multiple strong signals coincide (a single
    # large-but-disjoint hunk is medium, not hard). The structural resolver's
    # deterministic rules already handle the genuinely-trivial cases, so by the
    # time we're here a single signal is "needs care" not "needs the model at
    # its best". Two+ coincident signals ⇒ hard.
    strong = sum(
        1 for s in (
            size >= _HARD_HUNK_LINES, touches_def, same_symbol_overlap,
            same_line_overlap, severity == "high",
            modify_delete and merge_kind == "modify_delete",
        ) if s
    )
    if strong >= 2:
        return "hard"

    # Medium: definition-touching, same-line/same-symbol overlap, or moderate size.
    if touches_def or same_line_overlap or same_symbol_overlap:
        return "medium"
    if size >= _MEDIUM_HUNK_LINES:
        reasons.append(f"hunk is moderate ({size} lines)")
        return "medium"
    if severity == "medium":
        reasons.append("graded medium-severity")
        return "medium"

    # Easy: small, disjoint, no shared-symbol tension.
    reasons.append("small disjoint edits, no shared-symbol tension")
    return "easy"


# ---------------------------------------------------------------------------
# Signal extraction helpers (pure; cache-or-recompute like _merge_kind_of)
# ---------------------------------------------------------------------------


def _features(unit: "object") -> dict[str, Any]:
    """The conflict-feature spine, read from the cache or recomputed live.

    Extraction populates ``structural_metadata["conflict_features"]``; reading
    it avoids re-diffing. When absent (a hand-built unit in a test) recompute
    via :func:`conflict_extractor.conflict_features` so the classifier stays a
    pure function of the unit.
    """
    meta = getattr(unit, "structural_metadata", {}) or {}
    cached = meta.get("conflict_features")
    if isinstance(cached, dict):
        return dict(cached)
    try:
        from capybase.conflict_extractor import conflict_features

        return conflict_features(unit)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 - degrade gracefully, never crash routing
        return {}


def _as_int(val: Any) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _same_symbol_overlap(unit: "object") -> bool:
    """Whether both sides edited/added the SAME named identifier (function/class).

    A stronger signal than same-line overlap: two sides renaming or rewriting
    the same ``def foo`` is harder than two disjoint edits even if they land on
    different lines. Computed from the signature sets of each side's *changes*
    vs base (so a side that merely kept an entity doesn't count).
    """
    try:
        from capybase.resolution_engine import _extract_signatures

        base = _names(_extract_signatures(getattr(unit, "base").text or ""))
        cur = getattr(unit, "current").text or ""
        rep = getattr(unit, "replayed").text or ""
        cur_names = _names(_extract_signatures(cur))
        rep_names = _names(_extract_signatures(rep))
        # An entity both sides TOUCH (present in their side but not in base, OR
        # present in all three — i.e. both sides reference the same def). We care
        # about the case where both sides' edits converge on one identifier.
        cur_changed = cur_names - base
        rep_changed = rep_names - base
        return bool(cur_changed & rep_changed) or bool(
            cur_names & rep_names & (cur_changed or rep_changed)
            and _texts_differ_on(cur, rep, cur_names & rep_names)
        )
    except Exception:  # noqa: BLE001 - advisory signal
        return False


def _names(signatures: list[str]) -> set[str]:
    """Strip the ``kind:`` prefix from signature labels → bare names."""
    out: set[str] = set()
    for s in signatures:
        if ":" in s:
            out.add(s.split(":", 1)[1].strip())
    return out


def _texts_differ_on(cur: str, rep: str, names: set[str]) -> bool:
    """Whether cur and rep actually differ on the body of any shared ``names``.

    Guards against flagging overlap when both sides merely *reference* the same
    unchanged identifier (common base dependency) — only count it if at least
    one shared name's surrounding text differs between the sides.
    """
    if not names:
        return False
    # Coarse: if the sides are textually identical, there's no overlap to worry
    # about regardless of shared names. (A real per-symbol diff is overkill for
    # a cheap pre-LLM signal.)
    return cur.strip() != rep.strip()


def _deterministically_mergeable(unit: "object") -> bool:
    """Whether the structural resolver can merge this unit with zero LLM calls.

    A pure feasibility probe (the resolver never commits; it returns text or
    None). Used to mark union-combine conflicts ``trivial`` so they route to the
    cheap path. Lazily imported so this module has no hard dependency on the
    resolver (tests can build units without it). Any failure → False (the
    classifier is never blocked by the resolver being unavailable).
    """
    try:
        from capybase.structural_resolver import deterministically_mergeable

        return bool(deterministically_mergeable(unit))  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 - advisory flag
        return False

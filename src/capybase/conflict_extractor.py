"""Conflict extraction: build ConflictUnits from a conflicted worktree file.

Given the stage 1/2/3 blobs (BASE / CURRENT_UPSTREAM_SIDE /
REPLAYED_COMMIT_SIDE) and the conflict-marked worktree text, produce one
``ConflictUnit`` per ``<<<<<<< ... >>>>>>>`` marker block. Each unit carries
its exact ``marker_span`` so the orchestrator can later splice an accepted
resolution into the file precisely.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from capybase.adapters.parsers import MarkerBlock, parse_marker_blocks
from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.merge_intent import direction
from capybase.git_backend import (
    STAGE_BASE,
    STAGE_CURRENT,
    STAGE_REPLAYED,
    GitBackend,
    UnmergedPath,
)

if TYPE_CHECKING:
    from capybase.config import FutureConfig, StructuralConfig

# Language inference from file extension. The single source of truth is
# ``language.EXTENSION_TO_LANGUAGE``; aliased locally as ``_EXT_LANG`` so the
# detect_language reads naturally.
from capybase.adapters.language import EXTENSION_TO_LANGUAGE as _EXT_LANG


def detect_language(path: str) -> str | None:
    dot = path.rfind(".")
    if dot == -1:
        return None
    ext = path[dot:].lower()
    # Git conflict paths may carry a ':line:col' suffix (e.g.
    # 'src/foo.rs:1:0'). Strip it so the extension lookup succeeds — without
    # this, every conflict from `git diff --name-only -U0` style paths has
    # language=None, which silently skips the comment pass + shadow jury.
    if ":" in ext:
        ext = ext.split(":")[0]
    return _EXT_LANG.get(ext)


def looks_like_text(data: bytes) -> bool:
    """Heuristic: reject NUL bytes (binary). Allow valid UTF-8 or latin-1."""
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        try:
            data.decode("latin-1")
            return True
        except UnicodeDecodeError:
            return False


class ConflictExtractor:
    def __init__(
        self,
        git: GitBackend,
        *,
        structural_config: "StructuralConfig | None" = None,
        future_config: "FutureConfig | None" = None,
    ) -> None:
        self.git = git
        self.structural_config = structural_config
        self.future_config = future_config

    def extract_file_units(
        self,
        path: str,
        step_index: int,
        session_id: str,
        *,
        unmerged: UnmergedPath | None = None,
    ) -> list[ConflictUnit]:
        """Extract all ConflictUnits from one conflicted file.

        Reads stages 1/2/3 and the worktree text. If the file has no marker
        blocks but is unmerged (e.g. add/add handled by content merge), an
        empty list is returned and the caller escalates.

        Modify/delete (mode ``AU``/``UA``) is the whole-file variant: one side
        deleted the path, the other modified it. There are no ``<<<<<<<``
        markers, and the deleting side has *no* stage blob (so the unconditional
        three-stage read below would raise). We detect it first and emit a
        single ``whole_file`` unit whose deleting side is empty text; the
        downstream pipeline (structural → block-capture) decides keep vs.
        delete. ``marker_span`` is ``None`` — the resolved text IS the file.
        """
        mode = unmerged.mode if unmerged is not None else "UU"
        if mode in ("AU", "UA"):
            return self._extract_whole_file_units(
                path, step_index, session_id, mode, unmerged
            )

        base_bytes = self.git.read_stage_blob(path, STAGE_BASE)
        current_bytes = self.git.read_stage_blob(path, STAGE_CURRENT)
        replayed_bytes = self.git.read_stage_blob(path, STAGE_REPLAYED)
        worktree_bytes = self.git.read_worktree_file(path)

        base_text = base_bytes.decode("utf-8", errors="replace")
        current_text = current_bytes.decode("utf-8", errors="replace")
        replayed_text = replayed_bytes.decode("utf-8", errors="replace")
        worktree_text = worktree_bytes.decode("utf-8", errors="replace")

        blocks = parse_marker_blocks(worktree_text)
        units: list[ConflictUnit] = []
        base_oid = current_oid = replayed_oid = None
        if unmerged is not None:
            base_oid = unmerged.stages.get(STAGE_BASE)
            current_oid = unmerged.stages.get(STAGE_CURRENT)
            replayed_oid = unmerged.stages.get(STAGE_REPLAYED)

        base_side = ConflictSide(
            label="BASE", text=base_text, blob_oid=base_oid
        )

        for idx, block in enumerate(blocks):
            unit_id = _unit_id(path, step_index, idx)
            units.append(
                ConflictUnit(
                    session_id=session_id,
                    step_index=step_index,
                    path=path,
                    language=detect_language(path),
                    conflict_type=unmerged.mode if unmerged else "UU",
                    unit_id=unit_id,
                    unit_kind="text_marker_block",
                    base=base_side,
                    current=ConflictSide(
                        label="CURRENT_UPSTREAM_SIDE",
                        text=block.current_text,
                        blob_oid=current_oid,
                    ),
                    replayed=ConflictSide(
                        label="REPLAYED_COMMIT_SIDE",
                        text=block.replayed_text,
                        blob_oid=replayed_oid,
                    ),
                    original_worktree_text=worktree_text,
                    marker_span=block.span,
                    enclosing_symbol=_enclosing_symbol(worktree_text, block),
                    risk_tags=[],
                )
            )
        # Entity-boundary splitting: expand each oversized marker-block unit
        # that spans multiple top-level C/C++ entities into one sub-unit per
        # entity. The sub-spans partition the parent marker_span exactly, so the
        # existing multi-hunk splice reassembles them unchanged. Runs BEFORE the
        # enrichment passes (provenance, sibling metadata, structural, diff3,
        # severity, features, merge-direction) so every sub-unit is enriched as a
        # first-class unit. A no-op (returns [unit]) when disabled, the language
        # has no abstract parser, or the block doesn't span >1 entity.
        units = self._split_units(units)
        # Per-side provenance: attribute each side's blob to the
        # commit that introduced it. Advisory — never blocks resolution. The blob
        # OIDs come from the unmerged index (set above); this just enriches them.
        for u in units:
            try:
                u.structural_metadata["provenance"] = {
                    "base": _blob_provenance(self.git, u.base.blob_oid),
                    "current": _blob_provenance(self.git, u.current.blob_oid),
                    "replayed": _blob_provenance(self.git, u.replayed.blob_oid),
                }
            except Exception:  # noqa: BLE001 - provenance is advisory
                pass
        # Record sibling units in each unit's structural_metadata so downstream
        # (context builder, future RAG/structural views) knows there are other
        # resolvable conflict blocks in the same file. This is the seam that
        # lets the context window avoid bleeding across a sibling marker block
        # — without it the model may see another block's raw ``<<<<<<<`` lines
        # as ordinary context and be confused.
        if len(units) > 1:
            siblings = [
                {"unit_id": u.unit_id, "marker_span": list(u.marker_span)}
                if u.marker_span is not None
                else {"unit_id": u.unit_id, "marker_span": None}
                for u in units
            ]
            for u in units:
                u.structural_metadata["sibling_units"] = siblings
                u.structural_metadata["sibling_count"] = len(units)
        # Enrich units with abstract-parser structural data when configured and the
        # grammar is available. For each unit we resolve the lowest enclosing
        # AST node (the specific def/impl/struct) and record its text, type,
        # signature, and a base fingerprint of the original file. This lets the
        # context builder show a logical block instead of a blind line window,
        # and the AST-preservation validator prove unchanged nodes stay
        # structurally identical after splicing. Silently skipped when the lib
        # is absent or the language has no grammar — units keep unit_kind
        # "text_marker_block" and downstream code falls back to line windows.
        if self.structural_config and self.structural_config.enabled:
            _enrich_structural(units, worktree_text, base_text, self.structural_config)
        # Diff3 marker refinement: recompute the tightest
        # conflict boundaries via `git merge-file`. This is logically SEPARATE
        # from the structural enrichment above — it only rewrites the side/base
        # texts recorded for resolution (advisory; splicing still uses worktree
        # coordinates). It must run even when [structural] is disabled, because
        # the accurate refined base is what scopes the SBCR combination search
        # (a non-empty refined base = modification conflict; empty = addition).
        # Gated by its own flag (default on) so it can be disabled for diagnostics.
        if self.structural_config and self.structural_config.refine_with_diff3:
            _refine_with_diff3(
                units,
                base_side.text,
                current_text,
                replayed_text,
                self.structural_config.diff_algorithm,
                project_separators=self.structural_config.project_separators,
                language=detect_language(path),
            )
        # Grade each unit's severity from pre-LLM signals. Done
        # AFTER structural enrichment so the definition-touching signal is known.
        # Pure function; never fails (defaults to "medium" on any error).
        for u in units:
            try:
                u.severity = compute_severity(u)
            except Exception:  # noqa: BLE001 - severity is advisory
                u.severity = "medium"
        # Conflict feature spine: flatten the conflict's
        # characteristics (size, balance, imbalance, touches-def, overlap,
        # sibling count, severity) into one stable dict on each unit. This is
        # the unified input vector the calibration flywheel and any learned
        # router consume; previously these signals were computed piecemeal and
        # discarded. Advisory — never blocks resolution.
        for u in units:
            try:
                u.structural_metadata["conflict_features"] = conflict_features(u)
            except Exception:  # noqa: BLE001 - features are advisory
                pass
        # Merge-intent classification (modify/delete disambiguation): label what
        # each side DID relative to base — so the bundle/interactive view never
        # presents a deliberate deletion as if it were an addition, and the
        # ``delete_side`` structural rule can act on a proven modify/delete. The
        # full SideDirections is stashed on structural_metadata (kind + a
        # ready-to-render summary + which side deleted); the kind is also folded
        # into the feature spine above for calibration. Advisory — pure, cheap.
        self._enrich_merge_direction(units)
        return units

    def _enrich_merge_direction(self, units: list[ConflictUnit]) -> None:
        """Stash the ``direction()`` classification on each unit's metadata.

        Shared by the marker-block and whole-file extraction paths so the
        structural resolver's ``delete_side`` rule and block-capture see a
        consistent ``kind``/``deleting_side`` regardless of unit shape.
        Advisory — never blocks extraction.
        """
        for u in units:
            try:
                d = direction(
                    u.base.text or "", u.current.text or "", u.replayed.text or ""
                )
                u.structural_metadata["merge_direction"] = {
                    "kind": d.kind,
                    "current": d.current,
                    "replayed": d.replayed,
                    "summary": d.summary,
                    "deleting_side": d.deleting_side,
                }
            except Exception:  # noqa: BLE001 - classification is advisory
                pass

    # ------------------------------------------------------------------
    # Entity-boundary splitting (see docs/oversized-splitting-design-v3.md)
    # ------------------------------------------------------------------

    # Languages whose abstract parser yields reliable top-level entity spans
    # for splitting. The design targets C/C++ (Family A); the abstract parser
    # already returns function/struct/field spans at parse_confidence 1.0.
    _SPLITTABLE_LANGS = frozenset({"c", "cpp", "c++"})

    def _split_units(self, units: list[ConflictUnit]) -> list[ConflictUnit]:
        """Expand oversized multi-entity units into per-entity sub-units.

        Always-on but adaptive: for each marker-block unit it attempts to split
        at top-level entity boundaries; units that can't or shouldn't split are
        returned unchanged. The decision to fire lives in ``_split_unit_at_entities``
        (splittable language, region above ``entity_split_min_lines``, >1 entity),
        not behind a master flag. Whole-file units (``marker_span is None``) are
        never split — there is no marker span to partition.
        """
        fut = self.future_config
        out: list[ConflictUnit] = []
        for u in units:
            if u.marker_span is None or u.language not in self._SPLITTABLE_LANGS:
                out.append(u)
                continue
            subs = _split_unit_at_entities(
                u,
                min_region_lines=fut.entity_split_min_lines if fut else 40,
                min_sub_lines=fut.entity_split_min_sub_lines if fut else 8,
            )
            # Statement-level splitting: when entity splitting still leaves
            # oversized sub-units (>80 non-blank lines inside a function body),
            # split further at safe statement boundaries (lines ending with ;
            # at body indent, outside nested blocks). This turns a 200-line
            # LLM call into 5-10 tiny resolutions.
            final_subs: list[ConflictUnit] = []
            for sub in subs:
                stmt_pts = _find_statement_split_points(sub)
                if stmt_pts is None:
                    final_subs.append(sub)
                    continue
                # Split the sub-unit at statement boundaries using the existing
                # proportional sub-span infrastructure. Conservative: if any
                # step fails, keep the original sub-unit.
                try:
                    n_stmt = len(stmt_pts) + 1
                    s_start, s_end = sub.marker_span
                    spans = _proportional_sub_spans(s_start, s_end, [1] * n_stmt)
                    stmt_subs = [
                        _build_sub_unit(
                            sub, span, k, n_stmt,
                            sub.current.text or "", sub.replayed.text or "",
                            sub.base.text or "", n_stmt,
                        )
                        for k, span in enumerate(spans)
                    ]
                    final_subs.extend(stmt_subs)
                except Exception:  # noqa: BLE001
                    final_subs.append(sub)
            out.extend(final_subs)
        return out

    def _extract_whole_file_units(
        self,
        path: str,
        step_index: int,
        session_id: str,
        mode: str,
        unmerged: UnmergedPath | None,
    ) -> list[ConflictUnit]:
        """Extract a single ``whole_file`` unit from a modify/delete conflict.

        ``mode`` is ``AU`` (stage 2 absent → upstream/current deleted; replayed
        modified) or ``UA`` (stage 3 absent → replayed deleted; upstream
        modified). The deleting side has no stage blob, so it is represented as
        empty ``text``; the keeper side is read from its stage. The worktree
        carries git's "version of <modified side> left in tree" — that is the
        keeper's full text and becomes ``original_worktree_text``.

        ``marker_span`` is ``None`` (the resolution IS the whole file); the
        resolved-text-as-whole-file path in the orchestrator/verifier handles
        the absent span. ``merge_direction`` is populated so block-capture's
        modify/delete gate fires.
        """
        stages = unmerged.stages if unmerged is not None else {}
        base_oid = stages.get(STAGE_BASE)
        current_oid = stages.get(STAGE_CURRENT)
        replayed_oid = stages.get(STAGE_REPLAYED)

        # base (stage 1) is present for both AU/UA; the modified stage carries
        # the keeper. read_stage_blob raises on a missing stage, so only read
        # the ones we know exist.
        base_text = self.git.read_stage_blob(path, STAGE_BASE).decode(
            "utf-8", errors="replace"
        )
        if mode == "AU":
            # current (upstream) deleted → empty; replayed modified → keeper.
            current_text = ""
            replayed_text = (
                self.git.read_stage_blob(path, STAGE_REPLAYED)
                .decode("utf-8", errors="replace")
            )
        else:  # UA: replayed deleted → empty; current (upstream) modified → keeper.
            current_text = (
                self.git.read_stage_blob(path, STAGE_CURRENT)
                .decode("utf-8", errors="replace")
            )
            replayed_text = ""

        worktree_text = self.git.read_worktree_file(path).decode(
            "utf-8", errors="replace"
        )

        unit = ConflictUnit(
            session_id=session_id,
            step_index=step_index,
            path=path,
            language=detect_language(path),
            conflict_type=mode,
            unit_id=_unit_id(path, step_index, 0),
            unit_kind="whole_file",
            base=ConflictSide(label="BASE", text=base_text, blob_oid=base_oid),
            current=ConflictSide(
                label="CURRENT_UPSTREAM_SIDE", text=current_text, blob_oid=current_oid
            ),
            replayed=ConflictSide(
                label="REPLAYED_COMMIT_SIDE", text=replayed_text, blob_oid=replayed_oid
            ),
            original_worktree_text=worktree_text,
            marker_span=None,
            enclosing_symbol=None,
            risk_tags=[],
        )
        units = [unit]
        # Provenance (the marker path does the same): the deleter's blob_oid is
        # None (no stage blob), so its provenance is empty; the keeper's carries
        # the commit that introduced it. Advisory — block-capture's "deleting
        # commit" context degrades gracefully when absent.
        try:
            unit.structural_metadata["provenance"] = {
                "base": _blob_provenance(self.git, base_oid),
                "current": _blob_provenance(self.git, current_oid),
                "replayed": _blob_provenance(self.git, replayed_oid),
            }
        except Exception:  # noqa: BLE001 - provenance is advisory
            pass
        self._enrich_merge_direction(units)
        return units

    # Convenience: extract across every unmerged path, classifying along the
    # way. Returns (units_by_path, skipped) where skipped holds paths that are
    # not supported (binary, unknown mode, no markers).
    def extract_all(
        self,
        step_index: int,
        session_id: str,
        *,
        supported_types: set[str],
    ) -> tuple[dict[str, list[ConflictUnit]], list["SkippedPath"]]:
        skipped: list[SkippedPath] = []
        units_by_path: dict[str, list[ConflictUnit]] = {}
        unmerged = self.git.list_unmerged_paths()
        for entry in unmerged:
            if entry.mode not in supported_types:
                skipped.append(
                    SkippedPath(entry.path, f"unsupported conflict mode {entry.mode}")
                )
                continue
            if not self._is_text_path(entry.path):
                skipped.append(SkippedPath(entry.path, "non-text file"))
                continue
            try:
                units = self.extract_file_units(
                    entry.path, step_index, session_id, unmerged=entry
                )
            except Exception as exc:  # noqa: BLE001 - surface as skip reason
                skipped.append(SkippedPath(entry.path, f"extraction error: {exc}"))
                continue
            if not units:
                skipped.append(
                    SkippedPath(entry.path, "unmerged but no marker blocks")
                )
            else:
                units_by_path[entry.path] = units
        return units_by_path, skipped

    def _is_text_path(self, path: str) -> bool:
        try:
            return looks_like_text(self.git.read_worktree_file(path))
        except Exception:  # noqa: BLE001
            return False


class SkippedPath:
    """A conflicted path capybase will not attempt (with a reason)."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover
        return f"SkippedPath(path={self.path!r}, reason={self.reason!r})"


def _unit_id(path: str, step_index: int, idx: int) -> str:
    return f"{path}:{step_index}:{idx}"


def _blob_provenance(git: object, blob_oid: str | None) -> dict:
    """Resolve a blob OID to its introducing commit (sha + subject). Returns an
    empty-record dict on absence/failure — provenance is advisory."""
    if not blob_oid:
        return {"sha": "", "subject": ""}
    sha, subject = git.last_touch_blob(blob_oid)  # type: ignore[attr-defined]
    return {"sha": sha, "subject": subject}


# ----------------------------------------------------------------------
# Entity-boundary sub-conflict splitting
# ----------------------------------------------------------------------
#
# Splits one oversized marker-block unit that spans multiple top-level C/C++
# entities into one sub-unit per entity. The sub-spans partition the parent
# ``marker_span`` exactly (non-overlapping, reverse-sortable for splice), so
# the existing multi-hunk splice reassembles them with no change.
#
# Entity boundaries come from the grammar-free abstract parser
# (``adapters.abstract_parser.parse_file``), which returns top-level entity
# spans for C at parse_confidence 1.0. A split point is an entity start line
# STRICTLY inside the marker block (not the block's own first line). We never
# split inside the marker scaffolding (the ``<<<<<<<`` / ``=======`` /
# ``>>>>>>>`` lines) — split points are content lines.

def _side_entity_split_points(side_text: str, language: str) -> list[int]:
    """Content-line offsets (0-based) at which a top-level entity starts in ``side_text``.

    Parses ONE conflict side in isolation so the abstract parser sees real code
    rather than duplicated conflict sides. Returns entity-start line offsets
    strictly greater than 0 (the first entity at offset 0 is never a split point
    — it is the leading fragment's first line). Empty when parsing is unavailable
    or no interior entity boundary exists.
    """
    if not side_text or not side_text.strip():
        return []
    try:
        from capybase.adapters import abstract_parser
    except Exception:  # noqa: BLE001
        return []
    ir = abstract_parser.parse_file(side_text, language=language)
    if ir is None or ir.parse_confidence == 0.0:
        return []
    points = sorted({u.span[0] for u in ir.units if u.span[0] > 0})
    # Preprocessor safety (C/C++): never split inside an #if/#ifdef/#ifndef
    # block. A split point there strands the #if in one sub-unit and its #endif
    # in another, so the splice reassembly produces an unbalanced preprocessor
    # tree (observed on sqlite-0040: a cross-sub-unit #endif imbalance the
    # Phase 2 repair couldn't attribute). Drop any split point whose line sits
    # at a non-zero conditional depth; the splitter then either splits at the
    # remaining safe boundaries or declines (resolves as one block).
    if language in ("c", "cpp", "c++") and points:
        points = _drop_points_inside_preprocessor_conditional(side_text, points)
    return points


def _drop_points_inside_preprocessor_conditional(
    side_text: str, points: list[int]
) -> list[int]:
    """Filter ``points`` to those at C preprocessor conditional depth 0.

    Tracks ``#if`` / ``#ifdef`` / ``#ifndef`` (depth +1) against ``#endif``
    (depth -1); ``#else`` / ``#elif`` do not change depth. A split point at a
    line where depth > 0 would divide an ``#if`` from its matching ``#endif``,
    so it is removed. ``points`` are 0-based content-line offsets into
    ``side_text``. Best-effort: an unbalanced ``#if``/``#endif`` count in the
    fragment text leaves the tail at non-zero depth, which conservatively drops
    later points (safe — the splitter then declines rather than mis-splitting).
    """
    lines = side_text.split("\n")
    n = len(lines)
    depth_at_line: list[int] = [0] * n
    depth = 0
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith("#"):
            directive = stripped[1:].lstrip().split(None, 1)[0] if stripped[1:].lstrip() else ""
            if directive in ("if", "ifdef", "ifndef"):
                depth_at_line[i] = depth  # the #if line itself is at current depth
                depth += 1
                continue
            if directive == "endif":
                depth = max(0, depth - 1)
        depth_at_line[i] = depth
    return [p for p in points if p < n and depth_at_line[p] == 0]




def _build_sub_unit(
    parent: ConflictUnit,
    sub_index: int,
    sub_span: tuple[int, int],
    cur_text: str,
    rep_text: str,
    base_text: str,
    n_subs: int,
    *,
    parent_meta: dict | None = None,
) -> ConflictUnit:
    """Construct one sub-``ConflictUnit`` from a partitioned span + sliced sides."""
    meta = {
        "parent_unit_id": parent.unit_id,
        "sub_unit_index": sub_index,
        "sub_unit_count": n_subs,
    }
    if parent_meta:
        meta.update(parent_meta)
    return ConflictUnit(
        session_id=parent.session_id,
        step_index=parent.step_index,
        path=parent.path,
        language=parent.language,
        conflict_type=parent.conflict_type,
        unit_id=f"{parent.unit_id}#s{sub_index}",
        unit_kind=parent.unit_kind,
        base=ConflictSide(
            label=parent.base.label, text=base_text, blob_oid=parent.base.blob_oid
        ),
        current=ConflictSide(
            label=parent.current.label, text=cur_text, blob_oid=parent.current.blob_oid
        ),
        replayed=ConflictSide(
            label=parent.replayed.label,
            text=rep_text,
            blob_oid=parent.replayed.blob_oid,
        ),
        original_worktree_text=parent.original_worktree_text,
        marker_span=sub_span,
        enclosing_symbol=parent.enclosing_symbol,
        risk_tags=list(parent.risk_tags),
        severity=parent.severity,
        structural_metadata=meta,
    )


def _compute_parent_deletion_meta(unit: ConflictUnit) -> dict:
    """Compute parent-level deletion metadata for sub-units.

    Returns a dict with:
    - ``parent_has_deletions``: True if either side deleted >5 non-blank base
      lines relative to the other (a refactor vs small edit pattern).
    - ``parent_current_deleted_count``: number of base lines current deleted.
    - ``parent_replayed_deleted_count``: number of base lines replayed deleted.

    This lets downstream rules decline on sub-units whose parent had massive
    deletions — even when the fragment itself looks balanced. (Fixes the
    nlohmann-0020 Frankenstein merge: replayed deleted 102 lines, but each
    sub-unit fragment looked like a pure insertion.)
    """
    from difflib import SequenceMatcher

    base_lines = (unit.base.text or "").splitlines()
    cur_lines = (unit.current.text or "").splitlines()
    rep_lines = (unit.replayed.text or "").splitlines()

    # Count non-blank base lines deleted by each side (delete opcodes only).
    def _count_deleted(base_l: list[str], side_l: list[str]) -> int:
        sm = SequenceMatcher(None, base_l, side_l, autojunk=False)
        return sum(
            (i2 - i1)
            for tag, i1, i2, _j1, _j2 in sm.get_opcodes()
            if tag == "delete"
        )

    cur_del = _count_deleted(base_lines, cur_lines)
    rep_del = _count_deleted(base_lines, rep_lines)
    # Threshold: >5 non-blank base lines deleted by at least one side signals
    # a substantial cleanup/refactor — enough to be dangerous for union rules.
    has_deletions = max(cur_del, rep_del) > 5

    return {
        "parent_has_deletions": has_deletions,
        "parent_current_deleted_count": cur_del,
        "parent_replayed_deleted_count": rep_del,
    }


def _split_unit_at_entities(
    unit: ConflictUnit,
    *,
    min_region_lines: int,
    min_sub_lines: int,
) -> list[ConflictUnit]:
    """Split ``unit`` at top-level entity boundaries, or ``[unit]`` if not viable.

    Model
    -----
    The marker block's two side texts (current, replayed) each contain the same
    logical entities (func_a, func_b, ...) but at *different* absolute line
    offsets within their own text. So entity boundaries are found by parsing
    each side IN ISOLATION. The marker block's worktree line range
    ``[start, end]`` is partitioned into N sub-spans (the "slots" the splice
    writes into); each side is sliced into N fragments at its own entity
    boundaries, and the i-th fragment of each side becomes the i-th sub-unit's
    sides. Because both sides carry the same entity order, the fragments align.

    The sub-spans are sized proportionally to the CURRENT side's fragment sizes
    so a sub-unit's worktree slot roughly matches the content it resolves. The
    splice only uses ``(marker_span, resolved_text)`` — the side texts are for
    the prompt — so exact slot/content alignment is not required for splice
    correctness, only non-overlapping partition of ``[start, end]``.

    Returns ``[unit]`` (unchanged) when: the region is below
    ``min_region_lines``, no interior entity boundary exists, fewer than 2 viable
    fragments remain, or any defensive check fails. Best-effort; never blocks
    resolution.
    """
    try:
        start, end = unit.marker_span  # type: ignore[misc]
    except (TypeError, ValueError):
        return [unit]
    region_lines = end - start + 1
    if region_lines < min_region_lines:
        return [unit]

    worktree = unit.original_worktree_text
    wt_lines = worktree.split("\n")
    if start < 0 or end >= len(wt_lines) or start > end:
        return [unit]

    lang = unit.language or ""
    cur_text = unit.current.text or ""
    rep_text = unit.replayed.text or ""

    # Find entity boundaries on each side independently.
    cur_pts = _side_entity_split_points(cur_text, lang)
    rep_pts = _side_entity_split_points(rep_text, lang)

    # A side "carries structure" when it has interior entity boundaries we can
    # split on. The side WITH structure drives the fragment count; the other is
    # sliced to match (empty when it has no entities — the lopsided-add case
    # where one side added N functions and the other is a stale comment/deletion).
    cur_has_struct = bool(cur_pts)
    rep_has_struct = bool(rep_pts)

    if cur_has_struct and rep_has_struct:
        # Symmetric conflict: both sides carry the entities. They must agree on
        # the entity count for the fragments to align — a mismatch means the two
        # sides genuinely disagree on structure (a rename, an add/remove), and
        # splitting would mis-align the sides. Decline; resolve as one block.
        if len(rep_pts) != len(cur_pts):
            return [unit]
        cur_frags = _fragment_at_points(cur_text, cur_pts)
        rep_frags = _fragment_at_points(rep_text, rep_pts)
    elif cur_has_struct and not rep_has_struct:
        # Lopsided add: current carries the entities, replayed does not. Split
        # CURRENT at its entity boundaries; replayed is the single fragment it
        # already is, broadcast across the same fragment count.
        cur_frags = _fragment_at_points(cur_text, cur_pts)
        rep_frags = _broadcast_fragment(rep_text, len(cur_frags))
    elif rep_has_struct and not cur_has_struct:
        # Lopsided add (mirror): replayed carries the entities. Split REPLAYED;
        # current is broadcast across the same count.
        rep_frags = _fragment_at_points(rep_text, rep_pts)
        cur_frags = _broadcast_fragment(cur_text, len(rep_frags))
    else:
        # Neither side has interior entity boundaries — nothing useful to split.
        return [unit]

    # Defensive parity: fragments must align in count.
    if len(cur_frags) != len(rep_frags) or len(cur_frags) < 2:
        return [unit]

    # Drop fragments smaller than min_sub_lines on BOTH sides (a fragment that
    # is tiny in both sides carries no real content). Merge such a fragment into
    # its predecessor. Build the keep-mask over the fragment list.
    keep = _merge_tiny_fragments(cur_frags, rep_frags, min_sub_lines)
    if sum(keep) < 2:
        return [unit]
    # Re-extract the kept fragments (merging absorbed ones' text).
    cur_kept, rep_kept = _apply_keep(cur_frags, rep_frags, keep)

    n_subs = len(cur_kept)
    if n_subs < 2:
        return [unit]

    # Partition the worktree line range [start, end] into n_subs contiguous,
    # non-overlapping sub-spans sized proportionally to the NON-EMPTY side's kept
    # fragment line counts. This is the splice contract: the sub-spans must
    # cover [start, end] exactly with no gaps or overlaps.
    weights = [
        max(len(c.split("\n")), len(r.split("\n"))) for c, r in zip(cur_kept, rep_kept)
    ]
    sub_spans = _proportional_sub_spans(start, end, weights)

    # The base side: the parent's base is usually the WHOLE merge-base file, not
    # a hunk. Inheriting it verbatim breaks the deterministic cascade — a sub-
    # unit with empty current + one replayed function would look like a
    # base-vs-replayed conflict against a 300K-char base and be declined by the
    # structural resolver (which then forces a model call the reviewers expected
    # to be free). So we do NOT inherit the whole-file base. Instead:
    #   - If the parent base has the SAME entity count at its own boundaries, we
    #     fragment it in parallel (a true 3-way modify at the entity level).
    #   - Otherwise the sub-unit base is empty — a pure add/add at the entity
    #     level, which the structural resolver's one-sided rule resolves with
    #     zero model calls (exactly the "free" resolution the design intends).
    base_kept = _fragment_base(unit.base.text or "", lang, n_subs, cur_pts, rep_pts,
                               cur_has_struct, rep_has_struct)

    # Parent-aware deletion context: compute the parent's deletion map once,
    # then stamp it on every sub-unit. This lets downstream rules (insertion_
    # union, source_portfolio, asymmetry detection) make parent-aware decisions
    # instead of relying on the sub-unit fragment's own (misleading) side ratio.
    # A sub-unit fragment can look like a pure insertion even when the parent
    # had one side deleting 100+ base lines (a refactor). Without this context,
    # insertion_union/source_portfolio produce Frankenstein merges.
    parent_meta = _compute_parent_deletion_meta(unit)

    sub_units: list[ConflictUnit] = []
    for k in range(n_subs):
        sub_units.append(
            _build_sub_unit(
                unit, k, sub_spans[k], cur_kept[k], rep_kept[k], base_kept[k], n_subs,
                parent_meta=parent_meta,
            )
        )
    return sub_units


def _find_statement_split_points(
    unit: ConflictUnit, *, max_lines: int = 80, min_splits: int = 3,
) -> list[int] | None:
    """Find safe statement-level split points inside an oversized sub-unit.

    Scans the unit's worktree text (within its marker_span) for lines that:
    - end with ``;`` (statement end)
    - are at the same indentation level as the function body's start
    - are NOT inside any nested brace pair (brace depth == body depth)

    Returns a list of 0-based worktree line indices, or None when the unit
    is small enough or not enough safe points exist. The caller uses these
    to split the unit into statement-level sub-units.
    """
    if unit.marker_span is None:
        return None
    wt = (unit.original_worktree_text or "").splitlines()
    start, end = unit.marker_span
    region = wt[start:end + 1] if end < len(wt) else wt[start:]
    nb = sum(1 for l in region if l.strip())
    if nb <= max_lines:
        return None  # small enough — no need to split further
    # Determine the body indentation: find the first line that is inside
    # the function body (depth >= 1 after processing its braces) and not
    # the function signature itself.
    body_indent = 0
    _scan_depth = 0
    for line in region:
        if not line.strip():
            continue
        _scan_depth_before = _scan_depth
        for ch in line.strip():
            if ch == "{":
                _scan_depth += 1
            elif ch == "}":
                _scan_depth -= 1
        # The first line where depth transitions to >= 1 is the opening
        # brace line. The NEXT non-blank line at depth >= 1 gives body indent.
        if _scan_depth >= 1 and _scan_depth_before >= 1:
            body_indent = len(line) - len(line.lstrip())
            break
    # Track brace depth and find statement boundaries at body level
    depth = 0
    split_points: list[int] = []
    for i, line in enumerate(region):
        stripped = line.strip()
        if not stripped:
            continue
        # Track brace depth changes
        for ch in stripped:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
        # A safe split point: line ends with ;, is at body indent, depth == 1
        # (inside the function body but not inside a nested block)
        indent = len(line) - len(line.lstrip())
        if (
            stripped.endswith(";")
            and indent == body_indent
            and depth == 1
        ):
            split_points.append(start + i)  # absolute worktree line index
    if len(split_points) < min_splits:
        return None
    return split_points


def _fragment_base(
    base_text: str,
    language: str,
    n_subs: int,
    cur_pts: list[int],
    rep_pts: list[int],
    cur_has_struct: bool,
    rep_has_struct: bool,
) -> list[str]:
    """Derive each sub-unit's base text for correct 3-way vs add/add semantics.

    The parent base is usually the whole merge-base file. Inheriting it verbatim
    makes a one-sided-addition sub-unit look like a base-vs-side conflict and
    defeats the deterministic cascade. Instead:

    * When the conflict is SYMMETRIC (both sides carry the same entities) AND
      the base also has the same entity count, fragment the base in parallel so
      each sub-unit is a true 3-way modify at the entity level.
    * Otherwise (lopsided add, or base entity count disagrees) return ``n_subs``
      empty strings — each sub-unit is a pure add/add at the entity level, which
      the structural resolver's one-sided rule resolves with zero model calls.

    The whole-file base remains accessible via ``original_worktree_text`` for
    the prompt builder's anchor-based localization if ever needed.
    """
    if not base_text or not base_text.strip():
        return [""] * n_subs
    # Only attempt parallel base fragmentation for symmetric conflicts.
    if not (cur_has_struct and rep_has_struct):
        return [""] * n_subs
    base_pts = _side_entity_split_points(base_text, language)
    # Require the base to agree on entity count with the sides; else the base
    # is structurally different (e.g. the entities are new) -> treat as add/add.
    if len(base_pts) != len(cur_pts) or len(base_pts) + 1 != n_subs:
        return [""] * n_subs
    frags = _fragment_at_points(base_text, base_pts)
    # Defensive length guard.
    if len(frags) != n_subs:
        return [""] * n_subs
    return frags


def _fragment_at_points(text: str, points: list[int]) -> list[str]:
    """Split ``text`` into fragments at the given 0-based content line offsets.

    ``points`` are the interior start lines; fragment i covers the lines from
    the previous point (or 0) up to the line before the next point (or EOF).
    """
    if not text:
        return [""]
    lines = text.split("\n")
    bounds = [0] + [p for p in points if 0 < p < len(lines)] + [len(lines)]
    frags: list[str] = []
    for i in range(len(bounds) - 1):
        lo, hi = bounds[i], bounds[i + 1]
        frags.append("\n".join(lines[lo:hi]))
    return frags


def _broadcast_fragment(text: str, n: int) -> list[str]:
    """Place ``text`` in the FIRST of ``n`` aligned fragments; the rest are empty.

    Used in the lopsided-add case: the no-structure side (e.g. a stale comment
    or a deletion marker) precedes the first entity the other side added, so its
    entire content belongs to the leading fragment. The remaining fragments map
    to entities this side does not carry, so they are empty. If ``n <= 0``,
    returns ``[""]``.
    """
    if n <= 0:
        return [""]
    return [text or ""] + [""] * (n - 1)


def _merge_tiny_fragments(
    cur_frags: list[str], rep_frags: list[str], min_sub_lines: int
) -> list[bool]:
    """Return a keep-mask merging fragments tiny in BOTH sides into predecessors.

    A fragment is "tiny" when BOTH its current and replayed versions have fewer
    than ``min_sub_lines`` non-blank lines. Such a fragment is absorbed into the
    preceding kept fragment. The first fragment is always kept.
    """
    def _nblank(s: str) -> int:
        return sum(1 for ln in s.split("\n") if ln.strip())

    keep = [True] * len(cur_frags)
    for i in range(1, len(cur_frags)):
        if _nblank(cur_frags[i]) < min_sub_lines and _nblank(rep_frags[i]) < min_sub_lines:
            keep[i] = False  # absorb into predecessor
    return keep


def _apply_keep(
    cur_frags: list[str], rep_frags: list[str], keep: list[bool]
) -> tuple[list[str], list[str]]:
    """Apply a keep-mask, concatenating absorbed fragments into their predecessor."""
    cur_out: list[str] = []
    rep_out: list[str] = []
    for i, k in enumerate(keep):
        if k:
            cur_out.append(cur_frags[i])
            rep_out.append(rep_frags[i])
        else:
            # absorb into the last kept
            cur_out[-1] = cur_out[-1] + "\n" + cur_frags[i] if cur_out else cur_frags[i]
            rep_out[-1] = rep_out[-1] + "\n" + rep_frags[i] if rep_out else rep_frags[i]
    return cur_out, rep_out


def _proportional_sub_spans(
    block_start: int, block_end: int, weights: list[int]
) -> list[tuple[int, int]]:
    """Partition ``[block_start, block_end]`` into contiguous sub-spans by weight.

    ``weights[i]`` sizes the i-th sub-span (line count). Weights <= 0 are
    treated as 1 so every sub-span is at least one line. The spans tile the
    block exactly with no gaps or overlaps (any remainder from integer rounding
    goes into the last span).
    """
    total = block_end - block_start + 1
    n = len(weights)
    w = [max(1, x) for x in weights]
    wsum = sum(w)
    spans: list[tuple[int, int]] = []
    cursor = block_start
    for i, wi in enumerate(w):
        if i == n - 1:
            lo, hi = cursor, block_end
        else:
            size = max(1, round(total * wi / wsum))
            hi = min(block_end, cursor + size - 1)
            lo = cursor
        spans.append((lo, hi))
        cursor = hi + 1
    # Defensive: if rounding pushed cursor past block_end, clamp earlier spans.
    # Re-walk to guarantee exact tiling.
    spans2: list[tuple[int, int]] = []
    c = block_start
    for i in range(n):
        if i == n - 1:
            spans2.append((c, block_end))
        else:
            hi = spans[i][1]
            if hi >= block_end:
                # collapse the rest into remaining; but we want exactly n spans.
                hi = c
            spans2.append((c, hi))
            c = hi + 1
    # Final guarantee: force exact contiguous tiling regardless of rounding.
    fixed: list[tuple[int, int]] = []
    c = block_start
    for i in range(n):
        if i == n - 1:
            fixed.append((c, block_end)); break
        # size from original proportional intent, clamped to leave room
        size = max(1, spans2[i][1] - spans2[i][0] + 1)
        size = min(size, block_end - c - (n - 1 - i) + 1)
        fixed.append((c, c + size - 1))
        c = c + size
    return fixed




def compute_severity(unit: "ConflictUnit") -> str:
    """Grade a conflict's severity from cheap pre-LLM signals.

    A pure function of data already on the unit — no model, no git. Returns
    ``"low"``/``"medium"``/``"high"`` for triage/routing/attribution. The signals:

    - **Hunk size**: total non-empty lines across the three sides. Large hunks
      are harder to merge correctly.
    - **Touches a definition** (``enclosing_symbol`` set / definition-typed
      enclosing node): changes to function/class signatures are higher-stakes.
    - **Both sides changed the SAME lines** (real conflict): a genuine
      both-modified overlap is harder than a disjoint-edits case.

    "high" = large AND touches a definition; "low" = small with no same-line
    overlap; otherwise "medium". These are hand-sensible defaults; the goal is a
    stable pre-resolution triage signal, not a precise oracle.
    """
    base = (unit.base.text or "").splitlines()
    cur = (unit.current.text or "").splitlines()
    rep = (unit.replayed.text or "").splitlines()

    # Signal 1: hunk size (total meaningful lines).
    size = sum(1 for lines in (base, cur, rep) for ln in lines if ln.strip())
    large = size >= 30

    # Signal 2: touches a definition (enclosing symbol resolved OR a definition-
    # typed enclosing node recorded by the structural enricher).
    touches_def = bool(unit.enclosing_symbol) or any(
        unit.structural_metadata.get(k)
        for k in ("enclosing_node_text", "enclosing_node_signature")
    )

    # Signal 3: both sides changed the SAME base lines (real overlap). Use
    # histogram diff to map each side's edits onto base line indices; if they
    # intersect, it's a genuine same-line conflict (harder) vs a disjoint case.
    cur_changed = _changed_base_line_indices(base, cur)
    rep_changed = _changed_base_line_indices(base, rep)
    same_line_overlap = bool(cur_changed & rep_changed)

    if large and touches_def:
        return "high"
    if same_line_overlap:
        return "medium" if not large else "high"
    if size <= 6 and not touches_def:
        return "low"
    return "medium"


def conflict_features(unit: ConflictUnit) -> dict[str, float | int | str | bool]:
    """Flatten a conflict's characteristics into a stable feature vector.

    Surveys §6.7 (routing/hybridization) and §4.2 (balance) frame the choice of
    resolver as a function of conflict *characteristics*: size, imbalance,
    language, whether it touches a definition, whether both sides changed the
    same lines. Capybase computes these piecemeal (``compute_severity``,
    ``sbcr.balance``, the difficulty classifier) and then discards the raw
    signals — so the calibration flywheel, any future learned router, and offline
    eval have no single stable input vector.

    This pure function unifies those signals into one dict recorded on the unit
    (``structural_metadata["conflict_features"]``) and surfaced into every
    ``VerificationResult.features``, so downstream consumers read one spine
    instead of each recomputing ad-hoc signals. It reuses the exact computations
    already in ``compute_severity`` and ``sbcr.balance`` — no new heuristics.
    """
    from capybase.sbcr import balance as _balance

    base = (unit.base.text or "").splitlines()
    cur = (unit.current.text or "").splitlines()
    rep = (unit.replayed.text or "").splitlines()

    # Hunk size: total non-empty lines across the three sides (same definition
    # as compute_severity, the documented "large" signal).
    size = sum(1 for lines in (base, cur, rep) for ln in lines if ln.strip())

    cur_n = sum(1 for ln in cur if ln.strip())
    rep_n = sum(1 for ln in rep if ln.strip())
    bal = float(_balance(unit))
    # imbalance_ratio: how many times larger the bigger side is (>=1.0). 1.0 =
    # balanced; large = one side dominates (the §4.2 LLM-favored regime). Inf
    # when one side is empty, clamped to a finite sentinel for feature hygiene.
    if min(cur_n, rep_n) == 0:
        imbalance = float("inf")
    else:
        imbalance = max(cur_n, rep_n) / min(cur_n, rep_n)

    touches_def = bool(unit.enclosing_symbol) or any(
        unit.structural_metadata.get(k)
        for k in ("enclosing_node_text", "enclosing_node_signature")
    )

    # Entity-level operation counts (ConGra-style operation signatures, §3.3):
    # derived from the BASE→REPLAYED entity diff. Computed ONCE here and cached
    # on structural_metadata["entity_changes"] so every downstream consumer
    # (_commit_change_type_of, the LLM prompt's _semantic_change_block, and these
    # counts) reads from one parse instead of re-parsing 3-4× per unit. The diff
    # is None when the parser is unavailable → counts degrade to 0.
    rep_changes = _cached_entity_diff(unit, "replayed")
    cur_changes = _cached_entity_diff(unit, "current")

    return {
        "hunk_size": size,
        "current_side_lines": cur_n,
        "replayed_side_lines": rep_n,
        "balance": bal,
        "imbalance_ratio": imbalance,
        "touches_definition": bool(touches_def),
        "same_line_overlap": bool(_same_line_overlap(base, cur, rep)),
        "sibling_count": int(unit.structural_metadata.get("sibling_count", 0) or 0),
        "severity": unit.severity,
        "language": unit.language or "unknown",
        # Merge-intent classification (modify/Delete disambiguation): the conflict
        # shape from :func:`merge_intent.direction`. Read off structural_metadata
        # when already computed at extraction (avoids re-diffing); fall back to a
        # live computation so this stays a pure function of the unit.
        "merge_kind": _merge_kind_of(unit),
        "modify_delete": _merge_kind_of(unit) == "modify_delete",
        # Commit change-type: the semantic ROLE of the replayed
        # commit (test_only/config_update/feature/bugfix/refactor/unknown),
        # classified deterministically from path + the BASE→REPLAYED entity diff.
        # Grounds retry budgets (bugfix→more retries, refactor→fewer) and the LLM
        # prompt ("this is a bugfix — preserve behavior") in the commit's role.
        # Degrades to "unknown" when the structural parser is unavailable. Fed
        # the CACHED replayed diff so the BASE→REPLAYED parse happens once per
        # unit, not twice.
        "commit_change_type": _commit_change_type_of(unit, rep_changes),
        # Operation signatures (ConGra §3.3): per-entity change-type counts over
        # the BASE→REPLAYED diff. Gives the difficulty classifier and any future
        # learned router a discriminative operation view (pure-rename vs heavy
        # body-modify vs additive). 0 across the board when the parser is down.
        "ops_added": _count_change(rep_changes, "added"),
        "ops_removed": _count_change(rep_changes, "removed"),
        "ops_modified": _count_change(rep_changes, ("signature_changed", "body_changed")),
        "ops_renamed": _count_change(rep_changes, "renamed"),
        "ops_moved": _count_change(rep_changes, "moved"),
        # Value-resolution classification: when both sides preserve the SAME
        # statement shape (a return, an assignment to the same target) and only a
        # value/expression diverged, picking either side is the CORRECT merge
        # (the base operation is preserved; only the value is resolved). A
        # non-empty string ("return" / "assignment:a" / "augassign:count") gates
        # the both-sides-represented + preservation-heuristic validators so they
        # don't flag a correct one-sided merge as "dropped a side." Empty when
        # the conflict is genuine distinct additions or a shape mismatch.
        "value_resolution": _value_resolution_of(unit),
    }


def _same_line_overlap(base, cur, rep) -> bool:
    """Whether both sides changed the SAME base lines (a genuine overlap).

    Shared with ``compute_severity``'s logic: a real same-line conflict is
    harder than a disjoint-edits case. Extracted so the feature spine and the
    severity grader agree on the definition.
    """
    return bool(
        _changed_base_line_indices(base, cur) & _changed_base_line_indices(base, rep)
    )


def _changed_base_line_indices(base_lines: list[str], other_lines: list[str]) -> set[int]:
    """The set of ``base_lines`` indices that ``other_lines`` modifies.

    Shared by :func:`compute_severity` and :func:`_same_line_overlap`: a real
    same-line conflict is harder than a disjoint-edits case. Uses histogram diff
    (:mod:`capybase.diff`) to map each side's edits onto base line indices.
    """
    from capybase.diff import line_matcher

    changed: set[int] = set()
    for tag, i1, i2, _j1, _j2 in line_matcher(base_lines, other_lines).get_opcodes():
        if tag != "equal":
            changed.update(range(i1, i2))
    return changed


def _merge_kind_of(unit: ConflictUnit) -> str:
    """The merge-intent ``kind`` for ``unit`` (e.g. ``modify_delete``).

    Reads the classification off ``structural_metadata["merge_direction"]`` when
    :func:`direction` already computed it at extraction; otherwise computes it
    live so :func:`conflict_features` stays a pure function of the unit. Returns
    ``"both_modify"`` (a safe default) if anything goes wrong — the feature is
    advisory and must never crash the feature-spine computation.
    """
    cached = unit.structural_metadata.get("merge_direction")
    if isinstance(cached, dict) and cached.get("kind"):
        return str(cached["kind"])
    try:
        return direction(
            unit.base.text or "", unit.current.text or "", unit.replayed.text or ""
        ).kind
    except Exception:  # noqa: BLE001 - advisory feature
        return "both_modify"


def _commit_change_type_of(
    unit: ConflictUnit, rep_changes: list | None = None,
) -> str:
    """The semantic ROLE of ``unit``'s replayed commit.

    Classifies the replayed commit (test_only/config_update/feature/bugfix/
    refactor/unknown) via :func:`structural.classify_commit_change` over the
    BASE→REPLAYED entity diff + the unit's path. The replayed side IS the commit
    being replayed, so its diff against base captures what the commit changed.
    Returns ``"unknown"`` on any failure (advisory; must never crash the feature
    spine). Pure function of the unit.

    ``rep_changes`` optionally supplies a pre-computed BASE→REPLAYED entity diff
    (cached by :func:`conflict_features`) so the parse is shared with the
    operation-count features. When ``None`` the diff is computed here.

    Performance guard: when the base text is >200 lines (typical for marker
    units where ``unit.base.text`` is the WHOLE file), skip — the entity diff
    is both meaningless (24K base vs 2-line side) and expensive (0.5s parse ×
    90 units = 45s). Returns "unknown" which is the correct degradation.
    """
    # Performance guard: same as _cached_entity_diff. classify_commit_change
    # would compute its own semantic_diff (parsing 24K lines) when changes=None.
    base_line_count = (unit.base.text or "").count("\n")
    if base_line_count > 200 and rep_changes is None:
        return "unknown"
    try:
        from capybase.adapters import structural

        return structural.classify_commit_change(
            unit.base.text or "", unit.replayed.text or "",
            unit.path, unit.language or "",
            changes=rep_changes,
        )
    except Exception:  # noqa: BLE001 - advisory feature
        return "unknown"


def _cached_entity_diff(unit: ConflictUnit, side: str) -> list | None:
    """The BASE→``side`` entity diff, memoized on ``structural_metadata``.

    ``side`` is ``"current"`` or ``"replayed"``. The diff is computed once (by
    :func:`structural.semantic_diff`) and cached under
    ``structural_metadata["entity_changes"][side]`` so the feature spine, the
    commit-change-type classifier, and the LLM prompt's semantic-change block all
    share one parse per side instead of re-parsing 3-4× per unit. Returns ``None``
    when the parser is unavailable or the side fails to parse (callers degrade to
    zero-counts / "unknown").

    Performance guard: when the base text is >200 lines (typical for marker
    units where ``unit.base.text`` is the WHOLE file), the entity diff is
    both meaningless (24K base vs 2-line side = everything "deleted") and
    expensive (parsing 24K lines × 90 units = 50+ seconds). Skip it — the
    operation counts degrade to 0 and commit_change_type degrades to
    "unknown", which is the correct behavior for whole-file bases.
    """
    meta = unit.structural_metadata
    cache = meta.get("entity_changes")
    if not isinstance(cache, dict):
        cache = {}
        meta["entity_changes"] = cache
    if side in cache:
        return cache[side]
    # Performance guard: skip entity diff for large bases (whole-file marker
    # units). The semantic_diff would parse the entire file for each of 90+
    # units, adding 50+ seconds to extraction. The diff is meaningless anyway
    # (whole file vs 2-line hunk = everything deleted).
    base_line_count = (unit.base.text or "").count("\n")
    if base_line_count > 200:
        cache[side] = None
        return None
    try:
        from capybase.adapters import structural

        side_text = unit.current.text if side == "current" else unit.replayed.text
        changes = structural.semantic_diff(
            unit.base.text or "", side_text or "", unit.language or "",
        )
    except Exception:  # noqa: BLE001 - advisory
        changes = None
    # Cache even None so a repeated call doesn't re-attempt a failing parse.
    cache[side] = changes
    return changes


def _count_change(changes: list | None, types) -> int:
    """Count entity-diff entries whose ``change_type`` is in ``types``.

    ``types`` is a single change_type string or a tuple of them. Returns 0 when
    the diff is None (parser unavailable) — the operation counts degrade to zero,
    which downstream consumers (the classifier) treat as "no signal".
    """
    if not changes:
        return 0
    if isinstance(types, str):
        types = (types,)
    return sum(1 for c in changes if c.change_type in types)


def _value_resolution_of(unit: ConflictUnit) -> str:
    """The value-resolution classification of ``unit`` ("" when not applicable).

    Returns the compact feature string from
    :func:`value_resolution.classify_value_resolution` ("return" /
    "assignment:a" / "augassign:count") when both sides preserve the same
    statement shape and only a value diverged; "" otherwise (genuine distinct
    additions, shape mismatch, parse failure, unknown language).

    The base side in capybase's data model is the WHOLE base file, while the
    current/replayed sides are the marker-block interiors (hunk fragments). For
    statement-shape comparison we need the base HUNK — the region corresponding
    to the conflict — so this re-derives it via diff3 (the same source the
    refiner uses) and falls back to the whole-base text when diff3 is
    unavailable. Pure function of the unit; never raises.
    """
    try:
        from capybase.value_resolution import classify_value_resolution

        base_text = unit.base.text or ""
        # Prefer a diff3-refined base hunk if one was already recorded (tighter,
        # and matches the conflict region rather than the whole base file).
        refined = unit.structural_metadata.get("diff3_refined")
        if isinstance(refined, dict) and refined.get("base") is not None:
            base_text = refined["base"]
        else:
            # Derive the base hunk via diff3 over the three sides so the base is
            # the same shape (hunk interior) as current/replayed.
            base_hunk = _base_hunk_via_diff3(
                unit.base.text or "", unit.current.text or "",
                unit.replayed.text or "",
            )
            if base_hunk is not None:
                base_text = base_hunk
        vr = classify_value_resolution(
            base_text, unit.current.text or "", unit.replayed.text or "",
            unit.language,
        )
        return vr.as_feature() if vr else ""
    except Exception:  # noqa: BLE001 - advisory feature
        return ""


def _base_hunk_via_diff3(base: str, current: str, replayed: str) -> str | None:
    """The base region of the conflict hunk, re-derived via diff3.

    Returns the ``block.base`` of the (single) conflict block diff3 produces, or
    ``None`` when diff3 yields zero or multiple blocks (ambiguous — leave the
    caller on the whole-base text). Advisory; never raises.
    """
    try:
        from capybase.adapters.git_diff3 import merge_file_diff3
    except Exception:  # noqa: BLE001
        return None
    try:
        blocks = merge_file_diff3(base, current, replayed)
    except Exception:  # noqa: BLE001
        return None
    if blocks and len(blocks) == 1:
        return blocks[0].base
    return None


def _enclosing_symbol(worktree_text: str, block: MarkerBlock) -> str | None:
    """Best-effort enclosing symbol by Python indentation heuristics.

    A fallback signal used when the structural parser is unavailable; when
    structural context IS enabled, ``_enrich_structural`` overwrites
    ``unit.enclosing_symbol`` with the AST-resolved enclosing node. Looks
    upward for a ``def``/``class`` line whose indentation is strictly less
    than the first non-empty conflict line.
    """
    lines = worktree_text.split("\n")
    body_indent = _leading_indent(block.current_text.split("\n"))
    for ln in range(block.start - 1, -1, -1):
        line = lines[ln]
        ind = _leading_indent([line])
        if body_indent is None:
            continue
        if ind is not None and ind < body_indent:
            stripped = line.strip()
            if stripped.startswith(("def ", "class ", "async def ")):
                return stripped.split("(", 1)[0].split(" ", 1)[-1]
    return None


def _leading_indent(lines: list[str]) -> int | None:
    for line in lines:
        if not line.strip():
            continue
        return len(line) - len(line.lstrip(" "))
    return None


def _entity_name_from_signature(signature: str | None) -> str | None:
    """Bare name of the enclosing definition, to exclude it from siblings.

    Turns a signature header (``def save(self, v):`` / ``fn load(&self) -> T`` /
    ``class C:``) into just ``save`` / ``load`` / ``C`` so the sibling list
    doesn't re-show the very entity being resolved.

    Thin delegate to the shared ``structural.declaration_name`` (consolidated
    with context_builder._enclosing_name so the two cannot drift).
    """
    from capybase.adapters.structural import declaration_name
    return declaration_name(signature)


def _match_blocks_to_units(
    blocks: list, units: list, base_text: str,
) -> list | None:
    """Best-effort positional matching of diff3 blocks to conflict units.

    When git merge-file produces a different block count than the worktree
    marker parser (e.g. 79 vs 78), match each unit to its corresponding block
    by finding the block whose `.ours` text best overlaps with the unit's
    current.text (the marker hunk).

    Returns a list of blocks aligned 1:1 with units, or None if matching
    fails (e.g., too many blocks can't be matched).
    """
    from difflib import SequenceMatcher
    aligned: list = []
    used_block_indices: set[int] = set()
    for unit in units:
        unit_cur = (unit.current.text or "").strip()
        best_idx = -1
        best_ratio = 0.0
        for bi, block in enumerate(blocks):
            if bi in used_block_indices:
                continue
            block_ours = (block.ours or "").strip()
            if not block_ours and not unit_cur:
                best_idx = bi
                best_ratio = 1.0
                break
            # Quick check: if one is a substring of the other, accept
            if block_ours and unit_cur:
                if block_ours in unit_cur or unit_cur in block_ours:
                    best_idx = bi
                    best_ratio = 0.9
                    break
            # Fuzzy match
            ratio = SequenceMatcher(None, block_ours[:200], unit_cur[:200]).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_idx = bi
        if best_idx < 0 or best_ratio < 0.3:
            # Can't match this unit — bail
            return None
        used_block_indices.add(best_idx)
        aligned.append(blocks[best_idx])
    # Validate: each block's base appears in the full base text
    for block in aligned:
        if block.base and block.base not in base_text:
            return None
    return aligned


def _refine_with_diff3(
    units: list[ConflictUnit],
    base_text: str,
    current_text: str,
    replayed_text: str,
    diff_algorithm: str = "histogram",
    *,
    project_separators: bool = False,
    language: str | None = None,
) -> None:
    """Refine conflict side texts with ``git merge-file --diff3``.

    Git's own 3-way merge sometimes resolves adjacent non-conflicting lines
    that the worktree markers still include. Running diff3 on the stage blobs
    gives the tightest possible conflict boundaries. When git's view of a
    conflict is smaller (fewer lines) than the worktree markers, we record the
    refined texts in ``structural_metadata["diff3_refined"]`` so the resolver
    can use them for a sharper prompt. This is advisory — the marker_span and
    original_worktree_text are unchanged (splicing still uses the worktree
    coordinates). All failures are silent no-ops.

    ``diff_algorithm`` selects the xdiff backend ( default
    histogram); passed through to :func:`merge_file_diff3`.

    ``project_separators`` (Sesame): for brace/semicolon languages,
    additionally run a projected diff3 — the three blobs with each ``{}();``
    split onto its own line — and prefer it when it produces fewer/smaller
    conflict blocks than the raw view. The recorded refined texts are the
    *projected* side fragments (advisory; splicing is unaffected). No-op for
    Python and other non-separator languages.
    """
    try:
        from capybase.adapters.git_diff3 import merge_file_diff3
    except Exception:  # noqa: BLE001
        return
    blocks = merge_file_diff3(
        base_text, current_text, replayed_text, diff_algorithm=diff_algorithm
    )
    # Separator-projected pass: re-run diff3 on projected blobs
    # for brace/semicolon languages and prefer it when tighter. The projection
    # lets line-diff anchor on real statement/block boundaries.
    if project_separators and language is not None:
        projected = _maybe_use_projected(
            blocks,
            base_text,
            current_text,
            replayed_text,
            language,
            diff_algorithm,
        )
        # Safety: if the projected merge returns [] (clean merge) but the raw
        # blocks had real conflicts, the projection silently lost them — keep
        # the raw blocks. This is critical for nlohmann-0019 where the
        # separator projection produces 0 blocks from 79 real conflicts.
        if projected is not None and (len(projected) > 0 or len(blocks) == 0):
            blocks = projected
    if not blocks or len(blocks) != len(units):
        # Multi-diff portfolio: try alternative diff algorithms before giving
        # up. Different algorithms (patience, minimal, myers) produce different
        # block alignments; patience in particular avoids poor alignments caused
        # by unimportant matching lines (braces from different functions). This
        # is the fix for cases where histogram fails to align blocks, leaving
        # _prompt_sides with the whole-file base (the root cause of OVERSIZED).
        for alt_algo in ("patience", "minimal", "myers"):
            if alt_algo == diff_algorithm:
                continue
            try:
                alt_blocks = merge_file_diff3(
                    base_text, current_text, replayed_text,
                    diff_algorithm=alt_algo,
                )
            except Exception:  # noqa: BLE001
                continue
            if alt_blocks and len(alt_blocks) == len(units):
                blocks = alt_blocks
                break
    if not blocks or len(blocks) != len(units):
        # diff3 block count mismatch — can't safely associate blocks to units
        # via simple zip. Try a positional best-effort match: for each unit,
        # find the diff3 block whose `.ours` text best matches the unit's
        # current.text (which is already the tight marker hunk). This handles
        # the common case where git coalesces or splits one region differently
        # than the worktree marker parser (e.g., 79 blocks vs 78 units).
        # Without this fallback, the ENTIRE file gets no refinement, breaking
        # the structural resolver, pattern cache, and shape hashing.
        if blocks and len(blocks) >= len(units) - 2 and len(blocks) <= len(units) + 2:
            blocks = _match_blocks_to_units(blocks, units, base_text)
            if blocks is None:
                return
        else:
            return
    # Base-substring validation: verify each diff3 block's base text actually
    # appears in the full base file. Git can coalesce adjacent conflict regions
    # differently than the worktree markers — when the count accidentally
    # matches but the positional association is wrong, stamping the wrong
    # refined sides on units corrupts downstream resolver inputs. If ANY
    # block's base doesn't appear in the file, bail out (don't set refined).
    for block in blocks:
        if block.base and block.base not in base_text:
            return
    for unit, block in zip(units, blocks):
        # Only record if diff3 produced a tighter view (shorter sides).
        cur_lines = block.ours.count("\n") + 1 if block.ours else 0
        wt_lines = unit.current.text.count("\n") + 1 if unit.current.text else 0
        if cur_lines < wt_lines or block.base != unit.base.text:
            unit.structural_metadata["diff3_refined"] = {
                "current": block.ours,
                "base": block.base,
                "replayed": block.theirs,
            }


def _maybe_use_projected(
    raw_blocks: list | None,
    base_text: str,
    current_text: str,
    replayed_text: str,
    language: str,
    diff_algorithm: str,
) -> list | None:
    """Run a separator-projected diff3; prefer it when it's tighter.

    Returns the projected blocks if they have fewer conflict regions or a smaller
    total side-line footprint than ``raw_blocks``; otherwise returns the raw
    blocks unchanged. The projected side texts are the separator-split fragments
    — they carry the same content, just aligned on statement/block boundaries, so
    the resolver/prompt see a tighter conflict window. A no-op (returns raw) when
    the language isn't a separator language or the projected merge fails.
    """
    try:
        from capybase.adapters.git_diff3 import merge_file_diff3
        from capybase.adapters.separator_projection import project_separators, supports
    except Exception:  # noqa: BLE001
        return raw_blocks
    if not supports(language):
        return raw_blocks
    pb, pc, pr = (
        project_separators(base_text, language),
        project_separators(current_text, language),
        project_separators(replayed_text, language),
    )
    # If projection changed nothing (no separators present), skip the extra call.
    if pb == base_text and pc == current_text and pr == replayed_text:
        return raw_blocks
    projected = merge_file_diff3(pb, pc, pr, diff_algorithm=diff_algorithm)
    if projected is None:
        return raw_blocks  # projected merge itself failed → keep the raw view
    # A clean projected merge ([]) is the strongest improvement: the projected
    # alignment recognized the sides as compatible where raw diff3 saw a
    # conflict. Always prefer it.
    if len(projected) == 0:
        return projected
    raw_cost = _blocks_cost(raw_blocks)
    proj_cost = _blocks_cost(projected)
    # Prefer the projected view when it has strictly fewer regions or a strictly
    # smaller total footprint. Ties go to the raw view (no benefit to switching).
    if len(projected) < len(raw_blocks or []) or (
        len(projected) == len(raw_blocks or []) and proj_cost < raw_cost
    ):
        return projected
    return raw_blocks


def _blocks_cost(blocks: list | None) -> int:
    """Total side-line footprint of a set of diff3 blocks (ours+theirs lines).

    A cheaper proxy for "how much conflict text the model sees" — fewer/smaller
    is better. Returns a large sentinel for None so the comparison in
    :func:`_maybe_use_projected` never prefers an absent raw view.
    """
    if not blocks:
        return 1 << 30
    return sum(
        (b.ours.count("\n") + 1 if b.ours else 0)
        + (b.theirs.count("\n") + 1 if b.theirs else 0)
        for b in blocks
    )


def _blank_markers(text: str, language: str | None = None) -> str:
    """Replace conflict-marker blocks with comments so the parser can parse.

    Thin delegate to the canonical ``verification._blank_markers`` (language-
    aware: uses ``//`` for Rust, ``#`` otherwise, and comments out the second
    side's body to avoid duplicate-definition false errors in multi-hunk
    files). The prior local copy was a stale bug: it always used ``#`` and left
    both sides as live code, causing false-positive syntax rejections.
    Imported lazily to avoid a top-level import cycle
    (verification → conflict_extractor is lazy already).
    """
    from capybase.verification import _blank_markers as _canonical
    return _canonical(text, language)


def _enrich_structural(
    units: list[ConflictUnit],
    worktree_text: str,
    base_text: str,
    cfg: "StructuralConfig",
) -> None:
    """Populate ``structural_metadata`` with abstract-parser structural data per unit.

    Lazy-imports the structural adapter so capybase works without the
    ``structural`` extra. For each unit whose language has an available grammar
    we resolve the lowest enclosing node and a base fingerprint. The enclosing
    node is resolved against the BASE blob (clean and parseable) rather than
    the marker-laden worktree: the worktree's raw ``<<<<<<<`` lines produce
    ERROR nodes and a useless enclosing ``module``, while BASE has the same
    line layout outside the conflict and valid structure inside it. The
    fingerprint is likewise computed on BASE so it reflects real structure.

    For the AstPreservationValidator, the base fingerprint is of nodes OUTSIDE
    the conflict span — so after splicing a candidate into the worktree and
    re-fingerprinting, unchanged nodes match. All failures are silent no-ops.
    """
    try:
        from capybase.adapters import structural
    except Exception:  # noqa: BLE001
        return
    for unit in units:
        lang = unit.language
        if lang is None or lang not in cfg.languages:
            continue
        if not structural.is_available(lang):
            continue
        if unit.marker_span is None:
            continue
        # Resolve the lowest enclosing AST node from the BASE blob.
        node = structural.enclosing_node(base_text, unit.marker_span, lang)
        if node is not None:
            lines = node.span[1] - node.span[0] + 1
            # If the enclosing node is huge, the whole-module text is not a
            # useful "isolated block" — keep the line window instead.
            if lines <= cfg.max_enclosing_node_lines:
                unit.structural_metadata["enclosing_node_type"] = node.node_type
                unit.structural_metadata["enclosing_node_span"] = list(node.span)
                unit.structural_metadata["enclosing_node_text"] = node.text
                if node.signature:
                    unit.structural_metadata["enclosing_node_signature"] = node.signature
                    # AST signature is sharper than the indent heuristic.
                    unit.enclosing_symbol = node.signature
                unit.unit_kind = "ast_region"
                # Sibling entities (Rover): the OTHER methods/
                # fields co-located in the same container as this conflict. The
                # model sees the entity neighborhood it must stay consistent with
                # (shared conventions, callers/callees in-file) — prior work's
                # finding that *some* structured context lifts LLM output, at
                # near-zero cost. Enumerated from BASE (the clean, parseable
                # blob), excluding the enclosing entity itself. Advisory.
                try:
                    own_name = _entity_name_from_signature(node.signature)
                    siblings = structural.sibling_signatures(
                        base_text, lang, node.span, exclude=own_name
                    )
                    if siblings:
                        unit.structural_metadata["sibling_entities"] = siblings
                except Exception:  # noqa: BLE001 - siblings are advisory
                    pass
        # Base fingerprint of the file's structure OUTSIDE the conflict span.
        # Computed on the marker-blanked WORKTREE (not the clean BASE): the
        # worktree has the same non-conflict code as BASE, but with conflict
        # markers at each block. Blanking those markers to comments gives a
        # structural skeleton that the spliced result (with sibling markers
        # also blanked) should match. Using BASE directly would never match
        # because BASE has no markers at all, so its node structure differs
        # from the marker-blanked worktree at every conflict position.
        blanked_worktree = _blank_markers(worktree_text, lang)
        fp_outside, _ = structural.fingerprint_region(
            blanked_worktree, lang, unit.marker_span
        )
        if fp_outside is not None:
            unit.structural_metadata["ast_fingerprint_base_outside"] = fp_outside

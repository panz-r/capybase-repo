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
    from capybase.config import StructuralConfig

# Naive but dependency-free language inference from the file extension. Good
# enough for the MVP's syntax-validation gating; structural merge (later) will
# replace this with abstract-parser autodetection.
_EXT_LANG = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".rb": "ruby",
    ".sh": "shell",
    ".bash": "shell",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
}


def detect_language(path: str) -> str | None:
    dot = path.rfind(".")
    if dot == -1:
        return None
    return _EXT_LANG.get(path[dot:].lower())


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
    ) -> None:
        self.git = git
        self.structural_config = structural_config

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
        # Per-side provenance (survey §3.3): attribute each side's blob to the
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
        # Diff3 marker refinement (survey §1.3/§1.4): recompute the tightest
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
        # Grade each unit's severity from pre-LLM signals (survey §3.3). Done
        # AFTER structural enrichment so the definition-touching signal is known.
        # Pure function; never fails (defaults to "medium" on any error).
        for u in units:
            try:
                u.severity = compute_severity(u)
            except Exception:  # noqa: BLE001 - severity is advisory
                u.severity = "medium"
        # Conflict feature spine (survey §6.7/§4.2): flatten the conflict's
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


def compute_severity(unit: "ConflictUnit") -> str:
    """Grade a conflict's severity (survey §3.3) from cheap pre-LLM signals.

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
    import difflib

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
    # difflib to map each side's edits onto base line indices; if they intersect,
    # it's a genuine same-line conflict (harder) vs a disjoint case (easier).
    def _base_changed(base_lines, other_lines):
        changed = set()
        for tag, i1, i2, _j1, _j2 in difflib.SequenceMatcher(
            a=base_lines, b=other_lines, autojunk=False
        ).get_opcodes():
            if tag != "equal":
                changed.update(range(i1, i2))
        return changed

    cur_changed = _base_changed(base, cur)
    rep_changed = _base_changed(base, rep)
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
        # Commit change-type (survey §5.2): the semantic ROLE of the replayed
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
    import difflib

    def _base_changed(base_lines, other_lines):
        changed = set()
        for tag, i1, i2, _j1, _j2 in difflib.SequenceMatcher(
            a=base_lines, b=other_lines, autojunk=False
        ).get_opcodes():
            if tag != "equal":
                changed.update(range(i1, i2))
        return changed

    return bool(_base_changed(base, cur) & _base_changed(base, rep))


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
    """The semantic ROLE of ``unit``'s replayed commit (survey §5.2).

    Classifies the replayed commit (test_only/config_update/feature/bugfix/
    refactor/unknown) via :func:`structural.classify_commit_change` over the
    BASE→REPLAYED entity diff + the unit's path. The replayed side IS the commit
    being replayed, so its diff against base captures what the commit changed.
    Returns ``"unknown"`` on any failure (advisory; must never crash the feature
    spine). Pure function of the unit.

    ``rep_changes`` optionally supplies a pre-computed BASE→REPLAYED entity diff
    (cached by :func:`conflict_features`) so the parse is shared with the
    operation-count features. When ``None`` the diff is computed here.
    """
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
    """
    meta = unit.structural_metadata
    cache = meta.get("entity_changes")
    if not isinstance(cache, dict):
        cache = {}
        meta["entity_changes"] = cache
    if side in cache:
        return cache[side]
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

    MVP-only signal for context/risk; structural merge replaces this later.
    Looks upward for a ``def``/``class`` line whose indentation is strictly
    less than the first non-empty conflict line.
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
    """
    if not signature:
        return None
    s = signature.strip()
    for kw in ("async def", "def", "class", "fn", "struct", "enum", "trait", "mod"):
        if s.startswith(kw + " "):
            s = s[len(kw) + 1 :]
            break
    name = ""
    for ch in s:
        if ch.isalnum() or ch == "_":
            name += ch
        else:
            break
    return name or None


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

    ``diff_algorithm`` selects the xdiff backend (survey §1.3, default
    histogram); passed through to :func:`merge_file_diff3`.

    ``project_separators`` (survey §1.2 Sesame): for brace/semicolon languages,
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
    # Separator-projected pass (survey §1.2): re-run diff3 on projected blobs
    # for brace/semicolon languages and prefer it when tighter. The projection
    # lets line-diff anchor on real statement/block boundaries.
    if project_separators and language is not None:
        blocks = _maybe_use_projected(
            blocks,
            base_text,
            current_text,
            replayed_text,
            language,
            diff_algorithm,
        )
    if not blocks or len(blocks) != len(units):
        # Only refine when diff3 produces exactly the same number of conflict
        # blocks as the worktree — otherwise the correspondence is ambiguous.
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
    """Run a separator-projected diff3; prefer it when it's tighter (survey §1.2).

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


def _blank_markers(text: str) -> str:
    """Replace conflict-marker lines with comments so the parser can parse."""
    out = []
    for line in text.split("\n"):
        if line.startswith(("<<<<<<<", "=======", ">>>>>>>")):
            out.append("# conflict-marker")
        else:
            out.append(line)
    return "\n".join(out)


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
                # Sibling entities (survey §4.1/§5.4 Rover): the OTHER methods/
                # fields co-located in the same container as this conflict. The
                # model sees the entity neighborhood it must stay consistent with
                # (shared conventions, callers/callees in-file) — the survey's
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
        blanked_worktree = _blank_markers(worktree_text)
        fp_outside, _ = structural.fingerprint_region(
            blanked_worktree, lang, unit.marker_span
        )
        if fp_outside is not None:
            unit.structural_metadata["ast_fingerprint_base_outside"] = fp_outside

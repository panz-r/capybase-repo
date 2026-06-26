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
# replace this with tree-sitter autodetection.
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
        """
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
        # Enrich units with tree-sitter structural data when configured and the
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
        # from tree-sitter AST enrichment above — it only rewrites the side/base
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


def _refine_with_diff3(
    units: list[ConflictUnit],
    base_text: str,
    current_text: str,
    replayed_text: str,
    diff_algorithm: str = "histogram",
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
    """
    try:
        from capybase.adapters.git_diff3 import merge_file_diff3
    except Exception:  # noqa: BLE001
        return
    blocks = merge_file_diff3(
        base_text, current_text, replayed_text, diff_algorithm=diff_algorithm
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


def _blank_markers(text: str) -> str:
    """Replace conflict-marker lines with comments so tree-sitter can parse."""
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
    """Populate ``structural_metadata`` with tree-sitter AST data per unit.

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

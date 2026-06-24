"""Conflict extraction: build ConflictUnits from a conflicted worktree file.

Given the stage 1/2/3 blobs (BASE / CURRENT_UPSTREAM_SIDE /
REPLAYED_COMMIT_SIDE) and the conflict-marked worktree text, produce one
``ConflictUnit`` per ``<<<<<<< ... >>>>>>>`` marker block. Each unit carries
its exact ``marker_span`` so the orchestrator can later splice an accepted
resolution into the file precisely.
"""

from __future__ import annotations

from capybase.adapters.parsers import MarkerBlock, parse_marker_blocks
from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.git_backend import (
    STAGE_BASE,
    STAGE_CURRENT,
    STAGE_REPLAYED,
    GitBackend,
    UnmergedPath,
)

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
    def __init__(self, git: GitBackend) -> None:
        self.git = git

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

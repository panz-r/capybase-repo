"""Classification & support policy for conflicted paths.

The orchestrator asks ``Policy`` whether a path/conflict type is supported.
Unsupported paths are collected and escalated without touching the model,
keeping the resolver focused on its supported vertical slice.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from capybase.conflict_extractor import SkippedPath, looks_like_text
from capybase.git_backend import GitBackend, UnmergedPath


@dataclass
class PolicyDecision:
    supported: list[UnmergedPath] = field(default_factory=list)
    skipped: list[SkippedPath] = field(default_factory=list)


class Policy:
    def __init__(
        self,
        git: GitBackend,
        *,
        supported_conflict_types: set[str],
        supported_file_kinds: set[str],
    ) -> None:
        self.git = git
        self.supported_conflict_types = supported_conflict_types
        self.supported_file_kinds = supported_file_kinds

    def classify(self, unmerged: list[UnmergedPath]) -> PolicyDecision:
        decision = PolicyDecision()
        for entry in unmerged:
            if entry.mode not in self.supported_conflict_types:
                decision.skipped.append(
                    SkippedPath(entry.path, f"unsupported conflict mode {entry.mode}")
                )
                continue
            # AU/UA are whole-file modify/delete conflicts: one side deleted the
            # path, the other modified it. Git leaves the modified version in the
            # worktree, so the text check still works — the absent (deleting) side
            # has no stage blob and is represented as empty text by the extractor.
            if "text" in self.supported_file_kinds and not self._is_text(entry.path):
                decision.skipped.append(SkippedPath(entry.path, "non-text file"))
                continue
            decision.supported.append(entry)
        return decision

    def _is_text(self, path: str) -> bool:
        try:
            return looks_like_text(self.git.read_worktree_file(path))
        except Exception:  # noqa: BLE001
            return False

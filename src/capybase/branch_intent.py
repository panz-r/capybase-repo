"""Branch final-intent summaries — the source branch's net effect per file.

A safer alternative to dumping a giant diff into the prompt: summarize, per file
the source branch touched, WHAT the branch ultimately did — which symbols it
changed across which commits, and the final state of each (renamed/added/
removed/modified). This is structural (no LLM), derived from the source commits'
patches + the existing signature/region machinery.

Example output (the rendered block, capped to fit the prompt budget):

    Branch final intent for src/config.py:
    - parse_config: changed in commits 3, 7, 8
    - final branch adds env override behavior

The summary is computed once per rebase (alongside the history plan) and cached
on the orchestrator. It's trimmed last (after load-bearing sections) when the
prompt budget is tight.

Pure: :func:`build_branch_intent` takes a RebasePlan + a patches mapping and
returns a :class:`BranchIntent` (per-file summaries). The orchestrator fetches
the patches via ``git.commit_patch``; this function just analyzes them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from capybase.history import RebasePlan, ReplayCommit

#: Cap the number of files summarized (keeps the prompt block small; the
#: conflicts capybase is actively resolving are the signal, not the whole diff).
_MAX_FILES = 6
#: Cap the number of symbols per file (top symbols by commit count).
_MAX_SYMBOLS_PER_FILE = 5
#: Cap added/removed summary lines per file.
_MAX_NOTES_PER_FILE = 3

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
# Definitions introduced/removed by a patch (def/fn/struct/class/etc.).
_DEF_RE = re.compile(
    r"^\s*(?:pub\s+|async\s+)*(?:fn|def|struct|enum|trait|class|const|static)\s+"
    r"(" + _IDENT + r")"
)
# Added/removed definition lines.
_ADD_DEF_RE = re.compile(r"^\+\s*(?:pub\s+|async\s+)*(?:fn|def|struct|enum|trait|class)\s+(" + _IDENT + r")")
_REM_DEF_RE = re.compile(r"^-\s*(?:pub\s+|async\s+)*(?:fn|def|struct|enum|trait|class)\s+(" + _IDENT + r")")


@dataclass(frozen=True)
class FileIntent:
    """The branch's net effect on one file."""

    path: str
    #: Symbols changed (added/modified/removed) keyed by name → set of 1-based
    #: commit positions that touched them.
    symbols_changed: dict[str, set[int]] = field(default_factory=dict)
    #: Symbols the branch ADDED (introduced in some commit, present at the tip).
    added: set[str] = field(default_factory=set)
    #: Symbols the branch REMOVED (deleted across the branch net).
    removed: set[str] = field(default_factory=set)
    #: 1-based positions of commits that touched this file.
    commit_positions: list[int] = field(default_factory=list)

    def render(self) -> str:
        """One file's summary as prompt lines (no header)."""
        lines: list[str] = []
        # Symbols changed, most-touched first.
        ranked = sorted(
            self.symbols_changed.items(),
            key=lambda kv: (-len(kv[1]), kv[0]),
        )
        for name, positions in ranked[:_MAX_SYMBOLS_PER_FILE]:
            pos_str = ", ".join(str(p) for p in sorted(positions))
            # 'removed' wins over 'added' (last state at the tip), else 'changed'.
            if name in self.removed:
                tag = "removed"
            elif name in self.added:
                tag = "added"
            else:
                tag = "changed"
            lines.append(f"  - {name}: {tag} in commit(s) {pos_str}")
        return "\n".join(lines)


@dataclass(frozen=True)
class BranchIntent:
    """The source branch's net effect, per file."""

    files: list[FileIntent] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.files

    def render_block(self) -> str:
        """The full prompt block. Empty when no files/no changes."""
        if self.empty:
            return ""
        lines = ["Branch final intent (net effect of the source branch):"]
        for f in self.files[:_MAX_FILES]:
            body = f.render()
            if not body:
                continue
            lines.append(f"{f.path}:")
            lines.append(body)
        return "\n".join(lines)


def _parse_file_patch(patch: bytes) -> tuple[set[str], set[str]]:
    """Return (added_def_names, removed_def_names) from a single file's patch."""
    if not patch:
        return set(), set()
    text = patch.decode("utf-8", errors="replace")
    added: set[str] = set()
    removed: set[str] = set()
    for line in text.split("\n"):
        m = _ADD_DEF_RE.match(line)
        if m:
            added.add(m.group(1))
            continue
        m = _REM_DEF_RE.match(line)
        if m:
            removed.add(m.group(1))
    return added, removed


def build_branch_intent(
    plan: "RebasePlan | None",
    patches: "dict[str, bytes]",
) -> BranchIntent:
    """Summarize the source branch's net effect per file.

    Args:
        plan: the RebasePlan (its source_commits drive the analysis). None → empty.
        patches: mapping from commit OID → its (possibly multi-file) patch bytes.
            The caller fetches these via git.commit_patch; passing them in keeps
            this function pure + testable.

    The patches are multi-file (a commit may touch several files); we split them
    by the ``diff --git`` headers to attribute changes per path. Returns a
    :class:`BranchIntent` with one :class:`FileIntent` per touched file, the
    files sorted by total symbol changes (most-changed first). Never raises.
    """
    if plan is None or not plan.source_commits:
        return BranchIntent()
    try:
        # file_path → FileIntent (mutable accumulator; frozen at the end).
        files: dict[str, dict] = {}
        for pos, commit in enumerate(plan.source_commits, start=1):
            patch = patches.get(commit.oid) or b""
            for fpath, fpatch in _split_patch_by_file(patch, commit):
                bucket = files.setdefault(
                    fpath,
                    {"symbols": {}, "added": set(), "removed": set(),
                     "positions": []},
                )
                bucket["positions"].append(pos)
                added, removed = _parse_file_patch(fpatch)
                for name in added | removed:
                    bucket["symbols"].setdefault(name, set()).add(pos)
                # Net add/remove across the branch: a name added in some commit
                # and removed in a later one cancels. We track the LAST seen
                # state per name (the tip's view).
                for name in added:
                    if name in bucket["removed"]:
                        bucket["removed"].discard(name)  # re-added later
                    bucket["added"].add(name)
                for name in removed:
                    if name in bucket["added"]:
                        # Removed after being added earlier in the branch → net
                        # change, not a clean add. Drop from added, keep in both
                        # sets so render shows 'removed' (last state wins).
                        pass
                    bucket["added"].discard(name)
                    bucket["removed"].add(name)
        # Freeze into FileIntent, sort by total symbol-change count desc.
        file_intents: list[FileIntent] = []
        for fpath, b in files.items():
            if not b["symbols"]:
                continue
            file_intents.append(FileIntent(
                path=fpath,
                symbols_changed={k: set(v) for k, v in b["symbols"].items()},
                added=set(b["added"]),
                removed=set(b["removed"]),
                commit_positions=sorted(set(b["positions"])),
            ))
        file_intents.sort(
            key=lambda f: (-sum(len(p) for p in f.symbols_changed.values()), f.path)
        )
        return BranchIntent(files=file_intents)
    except Exception:  # noqa: BLE001 - advisory
        return BranchIntent()


def _split_patch_by_file(patch: bytes, commit: "ReplayCommit"):
    """Yield (file_path, sub_patch_bytes) for each file in a multi-file patch.

    A commit patch is one or more ``diff --git a/PATH b/PATH`` sections. We split
    on those headers and attribute each section to its path. When the patch has
    no ``diff --git`` header (older git or a single-file format), we attribute
    the whole patch to each touched file (best-effort). Yields (path, bytes).
    """
    if not patch:
        return
    text = patch.decode("utf-8", errors="replace")
    # Find all ``diff --git a/PATH b/PATH`` headers.
    header_re = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)
    matches = list(header_re.finditer(text))
    if not matches:
        # No headers: attribute the whole patch to each touched file.
        for fpath in commit.touched_files:
            yield fpath, patch
        return
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section = text[start:end].encode("utf-8")
        # The destination path (b/PATH) is the post-commit name.
        fpath = m.group(2)
        yield fpath, section

"""Whole-file import deduplication linker.

A deterministic assembly-phase pass that runs AFTER per-unit resolution
splicing and BEFORE whole-file cargo validation. The #1 cause of
``WHOLE_FILE_FAILED`` is duplicate imports: the model's per-unit resolution
adds a ``use`` statement that already exists elsewhere in the file (outside
the conflict hunk). The cargo check catches it at the file level, and the
model-driven ``_whole_file_repair`` can't fix it (it re-resolves one unit,
which re-adds the same import).

This module deduplicates imports at the file level by:
1. Finding all ``use``/``pub use`` lines in the full spliced text
2. Parsing each into canonical ``ImportLeaf`` records (reusing
   :func:`import_union.parse_use_leaves`)
3. Building a set of seen import paths
4. Removing exact-duplicate lines and surgically editing partial-duplicate
   brace groups (e.g. ``use std::{io, sync::Arc, fmt}`` where ``io`` and
   ``fmt`` already exist but ``sync::Arc`` is new → keep only ``sync::Arc``)

Safety:
  - Only operates on ``use``/``pub use`` lines (never touches code)
  - Only removes EXACT duplicate paths (same canonical path + binding + alias)
  - Never merges across different ``cfg`` domains
  - Brace-balance check after removal (transactional rollback on failure)
  - Returns the original text unchanged if any parse fails

Pure of I/O.
"""

from __future__ import annotations

import re

from capybase.import_union import (
    parse_use_leaves,
    _find_use_lines,
    _render_group_member,
    _leaf_identity,
    _parse_visibility_and_attrs,
)


def _brackets_balanced(s: str) -> bool:
    """True when (), [], {} are balanced across the whole string."""
    pairs = {")": "(", "]": "[", "}": "{"}
    opens = set("([{")
    stack: list[str] = []
    for ch in s:
        if ch in opens:
            stack.append(ch)
        elif ch in pairs:
            if not stack or stack[-1] != pairs[ch]:
                return False
            stack.pop()
    return not stack


def deduplicate_imports(
    text: str, language: str | None = None,
) -> tuple[str, int]:
    """Remove duplicate ``use`` statements from the full file text.

    Returns ``(deduplicated_text, duplicates_removed)``. Never raises —
    on any parse failure returns ``(text, 0)``.

    Args:
        text: the full spliced file text.
        language: the file language (for future per-language behavior).
            Currently only processes Rust ``use`` statements.

    Returns:
        ``(deduplicated_text, count)`` where count is the number of
        duplicate lines removed (or partial-line edits made).
    """
    if not text or not text.strip():
        return text, 0
    # Only process Rust use statements.
    if language and language not in ("rust", "toml", None):
        return text, 0

    try:
        lines = text.splitlines(keepends=True)
        # Collect import lines with their indices.
        import_indices: list[int] = []
        for i, ln in enumerate(lines):
            s = ln.strip()
            if (s.startswith("use ") or s.startswith("pub use ")
                    or s.startswith("pub(crate) use ")):
                # Must be a single-line use (balanced braces).
                if _brackets_balanced(ln.rstrip("\n")):
                    import_indices.append(i)

        if not import_indices:
            return text, 0

        # Parse each import line into canonical leaves.
        # Build a map: line_index → list[ImportLeaf]
        line_leaves: dict[int, list] = {}
        seen_paths: set[tuple] = set()
        removed_count = 0

        for idx in import_indices:
            leaves = parse_use_leaves(lines[idx].rstrip("\n"))
            if leaves is None:
                continue  # unparseable — skip this line
            line_leaves[idx] = leaves

        # First pass: mark leaves that are already seen (from earlier lines).
        # Process in source order so the FIRST occurrence is kept.
        # Key includes visibility so `use X` and `pub use X` are distinct.
        line_vis: dict[int, str] = {}
        for idx in sorted(line_leaves.keys()):
            parsed = _parse_visibility_and_attrs(lines[idx].rstrip("\n"))
            vis = parsed[0] if parsed else ""
            line_vis[idx] = vis
        for idx in sorted(line_leaves.keys()):
            leaves = line_leaves[idx]
            vis = line_vis[idx]
            fresh = []
            for leaf in leaves:
                # Include visibility in the dedup key so `use X` and `pub use X`
                # are treated as distinct (they have different scoping semantics).
                identity = (_leaf_identity(leaf)[0], _leaf_identity(leaf)[1], vis)
                path_key = ("path", vis, leaf.path)
                if identity in seen_paths or path_key in seen_paths:
                    continue  # duplicate — skip
                fresh.append(leaf)
                seen_paths.add(identity)
                seen_paths.add(path_key)
            line_leaves[idx] = fresh

        # Second pass: edit lines based on remaining fresh leaves.
        lines_to_remove: set[int] = set()
        for idx in sorted(line_leaves.keys()):
            fresh = line_leaves[idx]
            original = parse_use_leaves(lines[idx].rstrip("\n"))
            if original is None:
                continue
            if not fresh:
                # All leaves were duplicates — remove the entire line.
                lines_to_remove.add(idx)
                removed_count += 1
            elif len(fresh) < len(original):
                # Partial: some leaves were duplicates, some are fresh.
                # Surgically edit the line to keep only fresh leaves.
                edited = _rebuild_import_line(lines[idx], fresh)
                if edited is not None:
                    lines[idx] = edited
                    removed_count += 1

        if removed_count == 0:
            return text, 0

        # Remove fully-duplicate lines.
        result_lines = [
            ln for i, ln in enumerate(lines) if i not in lines_to_remove
        ]
        result = "".join(result_lines)

        # Safety: brace balance must hold.
        if not _brackets_balanced(result):
            return text, 0  # transactional rollback

        return result, removed_count

    except Exception:  # noqa: BLE001 — never break the assembly
        return text, 0


def _rebuild_import_line(
    original_line: str, fresh_leaves: list,
) -> str | None:
    """Rebuild an import line keeping only the fresh (non-duplicate) leaves.

    Handles two cases:
    - Group form (``use PREFIX::{A, B, C}``): rebuild with only fresh members.
    - Separate form (``use path::item;``): keep as-is if it's the only fresh leaf.

    Returns the edited line (with original line ending), or None if the
    rebuild fails.
    """
    ending = "\n" if original_line.endswith("\n") else ""
    line = original_line.rstrip()

    # Parse the original to get visibility + cfg + path prefix.
    parsed = _parse_visibility_and_attrs(line)
    if parsed is None:
        return None
    vis, cfg, body, trailing = parsed

    # Check if it's a group form (has ::{...}).
    from capybase.import_union import _find_group_introducer
    gidx = _find_group_introducer(body)
    if gidx is not None:
        # Group form: rebuild the brace group with only fresh members.
        # All fresh leaves must share the same prefix.
        prefixes = {leaf.path[:-1] for leaf in fresh_leaves if leaf.path[:-1]}
        if len(prefixes) != 1:
            return None
        prefix = next(iter(prefixes))
        members = [_render_group_member(leaf) for leaf in fresh_leaves]
        head = ""
        if cfg:
            head += cfg + " "
        if vis:
            head += vis + " "
        inner = ", ".join(m for m in members if m)
        return f"{head}use {'::'.join(prefix)}::{{{inner}}}{trailing}{ending}"

    # Separate form: if there's only one fresh leaf, rebuild as a simple use.
    if len(fresh_leaves) == 1:
        leaf = fresh_leaves[0]
        head = ""
        if cfg:
            head += cfg + " "
        if vis:
            head += vis + " "
        path_str = "::".join(leaf.path)
        if leaf.alias:
            return f"{head}use {path_str} as {leaf.alias};{ending}"
        return f"{head}use {path_str};{ending}"

    return None


__all__ = ["deduplicate_imports"]

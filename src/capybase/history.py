"""History-awareness substrate — read-only data layer for rebase context.

This module gives capybase the first real answer to "where is this conflict in
the commit sequence, what later commits touch the same region, what did the
final branch intend, and have we seen this shape before?" It is deliberately a
*data* layer, not a resolver: history is injected as context, diagnostics, and
(one narrow) future-compatibility probe, never as an override of local
side-obligation validation.

DESIGN CONTRACT (phase-one non-goals + invariants)
--------------------------------------------------

Non-goals (explicitly deferred):
- Jujutsu-style first-class conflicted commits. Vanilla Git only.
- Continuing rebases through unresolved conflicts.
- A generic ``ResolutionMechanism`` interface. The existing ordered pipeline
  (structural → combination → block-capture → LLM → manual) stays; history is
  added as data/context/diagnostics, not a pipeline rewrite.
- Auto-applying old resolutions (``rerere++`` apply). The experience store gains
  history-aware *retrieval* first; trust-scored reuse is a later phase.

Invariants (must always hold):
- **Read-only until validated.** History features never mutate the repo or the
  rebase; they only inform the prompt, the features dict, and (later) an
  advisory future-apply probe in a throwaway worktree.
- **Never overrides local validation.** A history hint never overrides a failed
  side-obligation / syntax / splice check. History can *add* a reason to
  escalate, never a reason to accept something invalid.
- **Degrades to current behavior.** When tree-sitter, the rebase-merge state,
  or the commit sequence is unavailable, every history function returns empty/
  None and the pipeline behaves exactly as it does today.
- **Vanilla-Git compatible.** No reliance on non-standard git features; the
  commit sequence comes from ``git rev-list``, the current replay position from
  ``.git/rebase-merge/stopped-sha``.

The types below are journal-serializable (plain dataclasses / pydantic models)
so a session's history context can be replayed in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Step 2: RebasePlan + ReplayCommit — the source commit sequence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayCommit:
    """One commit in the source sequence being replayed onto the target.

    ``index`` is 0-based into the replay order (oldest first). ``touched_files``
    is the set of paths the commit changed (repo-relative). ``patch_id`` is a
    stable content hash of the commit's diff (``git patch-id``) for matching
    across rebases; empty when unavailable.
    """

    oid: str
    parent_oid: str
    subject: str
    body_summary: str
    touched_files: list[str]
    diffstat: dict[str, int]
    patch_id: str
    index: int  # 0-based position in the replay order

    def to_dict(self) -> dict[str, Any]:
        return {
            "oid": self.oid, "parent_oid": self.parent_oid,
            "subject": self.subject, "body_summary": self.body_summary,
            "touched_files": list(self.touched_files), "diffstat": dict(self.diffstat),
            "patch_id": self.patch_id, "index": self.index,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ReplayCommit":
        return cls(
            oid=str(d.get("oid", "")), parent_oid=str(d.get("parent_oid", "")),
            subject=str(d.get("subject", "")), body_summary=str(d.get("body_summary", "")),
            touched_files=list(d.get("touched_files", [])),
            diffstat=dict(d.get("diffstat", {})),
            patch_id=str(d.get("patch_id", "")),
            index=int(d.get("index", 0)),
        )


@dataclass(frozen=True)
class RebasePlan:
    """The full source-sequence + target picture, captured once at rebase start.

    Generated for both real and dry-run rebases and written to the session
    directory (``rebase_plan.json``) so the orchestrator, validators, and tests
    can replay the same history. This directly addresses vanilla rebase's core
    limitation: it resolves each replayed commit locally, without global
    knowledge of the source sequence or the target history.
    """

    source_commits: list[ReplayCommit]  # oldest-first replay order
    target_base_oid: str  # merge-base of source-tip and target
    target_tip_oid: str  # the onto/upstream tip
    source_tip_oid: str  # the pre-rebase HEAD (branch tip being replayed)
    created_at: str  # ISO-8601 timestamp

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_commits": [c.to_dict() for c in self.source_commits],
            "target_base_oid": self.target_base_oid,
            "target_tip_oid": self.target_tip_oid,
            "source_tip_oid": self.source_tip_oid,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RebasePlan":
        return cls(
            source_commits=[ReplayCommit.from_dict(c) for c in d.get("source_commits", [])],
            target_base_oid=str(d.get("target_base_oid", "")),
            target_tip_oid=str(d.get("target_tip_oid", "")),
            source_tip_oid=str(d.get("source_tip_oid", "")),
            created_at=str(d.get("created_at", "")),
        )

    def commit_by_oid(self, oid: str) -> ReplayCommit | None:
        """The replay commit with this OID, or None."""
        for c in self.source_commits:
            if c.oid == oid:
                return c
        return None

    def index_of(self, oid: str) -> int | None:
        """The 0-based replay index of ``oid``, or None."""
        c = self.commit_by_oid(oid)
        return c.index if c is not None else None


# ---------------------------------------------------------------------------
# Step 4: RegionKey — the lightweight structural coordinate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegionKey:
    """A lightweight structural coordinate for a conflict region.

    Built from the structural metadata capybase already computes at extraction
    (enclosing node type/signature/span + AST fingerprint) — no recomputation.
    This is the "light abstract parsing" coordinate system: it distinguishes
    functions, classes, impls, import blocks, config sections, etc., WITHOUT
    parsing expression internals. Used to identify later commits touching the
    same region, key reuse matches, and scope diagnostics — avoiding fragile
    dependence on line numbers alone.

    ``structural_hash`` is the AST-fingerprint of the file's structure OUTSIDE
    the conflict span (already computed by ``fingerprint_region`` at extraction);
    it's stable under whitespace/formatting changes. When tree-sitter is
    unavailable, ``kind`` falls back to ``"unknown"`` and only path+span carry
    identity (the current behavior).
    """

    path: str
    language: str | None
    kind: str  # function | class | impl | import_block | config_block | text_block | unknown
    name: str | None  # the enclosing symbol/signature (e.g. "ConfigLoader.parse")
    enclosing_node_type: str | None
    start_line: int | None
    end_line: int | None
    structural_hash: str  # the outside-fingerprint; "" when unavailable

    def display(self) -> str:
        """A compact ``path :: kind > name`` coordinate for reports/bundles."""
        parts = [self.path]
        if self.kind and self.kind != "unknown":
            parts.append(self.kind)
        if self.name:
            parts.append(self.name)
        return " :: ".join(parts[:1]) + ((" > " + " > ".join(parts[1:])) if len(parts) > 1 else "")


def region_key_from_unit(unit: Any) -> RegionKey:
    """Build a :class:`RegionKey` from a ConflictUnit's existing metadata.

    Pure: assembles data already stamped onto ``unit.structural_metadata`` at
    extraction (enclosing_node_type/signature/span, ast_fingerprint_base_outside)
    plus the unit's path/language/marker_span. Degrades to an ``unknown``-kind
    key (path + span only) when the structural layer didn't run — matching the
    current behavior for un-enriched units.
    """
    meta = getattr(unit, "structural_metadata", {}) or {}
    node_type = meta.get("enclosing_node_type")
    signature = meta.get("enclosing_node_signature") or getattr(unit, "enclosing_symbol", None)
    span = meta.get("enclosing_node_span")
    fingerprint = meta.get("ast_fingerprint_base_outside") or ""
    marker_span = getattr(unit, "marker_span", None)

    # Derive a coarse kind from the node type / signature (no expression parsing).
    kind = _coarse_kind(node_type, signature)

    # Span: prefer the enclosing-node span (the whole logical block); fall back
    # to the marker span (the conflict region itself).
    start_line = end_line = None
    if isinstance(span, (list, tuple)) and len(span) == 2:
        start_line, end_line = int(span[0]), int(span[1])
    elif isinstance(marker_span, (list, tuple)) and len(marker_span) == 2:
        start_line, end_line = int(marker_span[0]), int(marker_span[1])

    return RegionKey(
        path=getattr(unit, "path", ""),
        language=getattr(unit, "language", None),
        kind=kind,
        name=signature,
        enclosing_node_type=node_type,
        start_line=start_line,
        end_line=end_line,
        structural_hash=fingerprint,
    )


_NODE_KIND_MAP = {
    # tree-sitter node types → coarse RegionKey kind
    "function_definition": "function",
    "function_item": "function",
    "class_definition": "class",
    "struct_item": "class",
    "impl_item": "impl",
    "implementation_list": "impl",
    "enum_item": "class",
    "trait_item": "class",
    "decorated_definition": "function",
}


def _coarse_kind(node_type: str | None, signature: str | None) -> str:
    """Map a tree-sitter node type / signature to a coarse RegionKey kind."""
    if node_type and node_type in _NODE_KIND_MAP:
        return _NODE_KIND_MAP[node_type]
    if signature:
        sig = signature.strip()
        if sig.startswith(("def ", "async def ")):
            return "function"
        if sig.startswith("class "):
            return "class"
        if sig.startswith(("fn ", "pub fn ", "async fn ")):
            return "function"
        if sig.startswith(("struct ", "enum ", "trait ")):
            return "class"
        if sig.startswith("impl "):
            return "impl"
    return "unknown"


# ---------------------------------------------------------------------------
# Step 5: HistoryContext + HistoryQueryService (the query layer)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HistoryContext:
    """The history facts relevant to one conflict, answered by the query service.

    Every field degrades to an empty list / None when the data is unavailable,
    so a resolver/validator that reads it stays correct without history. This is
    the payload that flows into prompt context (step 7), advisory features
    (step 8), and (later) the future-apply probe (step 9).
    """

    current_replay_commit: ReplayCommit | None
    source_commit_index: int | None  # 0-based position; None when unknown
    source_commit_count: int  # total replayed commits
    previous_source_commits_touching_file: list[ReplayCommit]
    future_source_commits_touching_file: list[ReplayCommit]
    future_source_commits_touching_region: list[ReplayCommit]
    recent_target_commits_touching_file: list[ReplayCommit]

    @property
    def has_future_touches(self) -> bool:
        """Whether any later source commit touches the same file (lookahead relevance)."""
        return bool(self.future_source_commits_touching_file)

    @property
    def has_future_region_touches(self) -> bool:
        """Whether any later source commit touches the same REGION (stronger)."""
        return bool(self.future_source_commits_touching_region)

    def to_features(self) -> dict[str, float | int | str | bool]:
        """A compact feature dict for the risk/calibration spine (step 8)."""
        return {
            "history_source_commit_index": self.source_commit_index if self.source_commit_index is not None else -1,
            "history_source_commit_count": self.source_commit_count,
            "history_future_file_touch_count": len(self.future_source_commits_touching_file),
            "history_future_region_touch_count": len(self.future_source_commits_touching_region),
            "history_target_recent_file_touch_count": len(self.recent_target_commits_touching_file),
            "history_has_context": self.current_replay_commit is not None,
        }


class HistoryQueryService:
    """Answers narrow history questions from a :class:`RebasePlan`.

    Constructed once with the plan (and optional recent-target commits); called
    per conflict via :meth:`for_conflict`. Read-only: never touches the repo.
    When the plan is empty (no rebase, or history unavailable), every query
    returns an empty :class:`HistoryContext` — the pipeline degrades to current
    behavior.
    """

    def __init__(
        self,
        plan: RebasePlan | None = None,
        *,
        recent_target_commits: list[ReplayCommit] | None = None,
    ) -> None:
        self._plan = plan
        self._recent_target = recent_target_commits or []

    @classmethod
    def empty(cls) -> "HistoryQueryService":
        """A service with no plan — all queries yield empty context."""
        return cls(plan=None)

    def for_conflict(
        self,
        unit: Any,
        *,
        replayed_commit_oid: str | None = None,
        region_key: RegionKey | None = None,
    ) -> HistoryContext:
        """The history context for one conflict.

        ``replayed_commit_oid`` is the commit currently being replayed (from
        ``.git/rebase-merge/stopped-sha``); None when unknown. ``region_key`` is
        the structural coordinate (from :func:`region_key_from_unit`); when
        omitted it's computed from the unit. Returns an empty context when no
        plan is set.
        """
        if self._plan is None or not self._plan.source_commits:
            return self._empty_context()
        if region_key is None:
            region_key = region_key_from_unit(unit)
        path = getattr(unit, "path", "") or region_key.path

        commits = self._plan.source_commits
        current = None
        idx = None
        if replayed_commit_oid:
            current = self._plan.commit_by_oid(replayed_commit_oid)
            idx = self._plan.index_of(replayed_commit_oid)

        # If we don't know the current commit, we can't slice future/past.
        if idx is None:
            return HistoryContext(
                current_replay_commit=None, source_commit_index=None,
                source_commit_count=len(commits),
                previous_source_commits_touching_file=[],
                future_source_commits_touching_file=[],
                future_source_commits_touching_region=[],
                recent_target_commits_touching_file=[
                    c for c in self._recent_target if path and path in c.touched_files
                ],
            )

        previous_file = [c for c in commits[:idx] if path and path in c.touched_files]
        future_file = [c for c in commits[idx + 1:] if path and path in c.touched_files]
        future_region = [
            c for c in future_file if self._touches_region(c, region_key)
        ]
        return HistoryContext(
            current_replay_commit=current,
            source_commit_index=idx,
            source_commit_count=len(commits),
            previous_source_commits_touching_file=previous_file,
            future_source_commits_touching_file=future_file,
            future_source_commits_touching_region=future_region,
            recent_target_commits_touching_file=[
                c for c in self._recent_target if path and path in c.touched_files
            ],
        )

    def _touches_region(self, commit: ReplayCommit, key: RegionKey) -> bool:
        """Whether a future commit plausibly touches the same region.

        Conservative (over-approximates): a commit touches the region if it
        touches the same FILE and (the region kind is unknown, OR the commit
        subject/body mentions the region name). A precise same-region check
        needs the commit's diff parsed against the RegionKey span — deferred;
        this cheap heuristic catches the common "later commit renames/extends
        the same function" case via the name match.
        """
        if key.kind == "unknown" or not key.name:
            return False  # can't tell without a named region; don't over-flag
        name = key.name.split("(")[0].split("<")[0].strip().split()[-1] if key.name else ""
        name = name.rstrip(":=(<")  # strip trailing syntax punctuation
        if not name:
            return False
        haystack = f"{commit.subject} {commit.body_summary}".lower()
        return name.lower() in haystack

    def _empty_context(self) -> HistoryContext:
        return HistoryContext(
            current_replay_commit=None, source_commit_index=None,
            source_commit_count=0,
            previous_source_commits_touching_file=[],
            future_source_commits_touching_file=[],
            future_source_commits_touching_region=[],
            recent_target_commits_touching_file=[],
        )

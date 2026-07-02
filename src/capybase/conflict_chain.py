"""Conflict-chain detection — related conflicts across the rebase (#9 step 7).

When a rebase replays several commits that each conflict in the SAME region
(same path + region kind/name — e.g. three commits all touching ``parse_config``
as part of an API migration), those conflicts form a "chain". Detecting it lets
capybase escalate earlier, batch context, or warn the user that the branch has a
coherent migration conflict rather than isolated hunks — strategic information a
per-conflict view hides.

A chain is defined by a shared region coordinate (path + kind + name) appearing
in conflicts from 2+ distinct replayed commits. The detector is pure: it takes a
list of :class:`ConflictObservation` (one per resolved conflict, carrying its
region key + replayed-commit position) and returns the chains. The orchestrator
collects the observations as steps resolve and runs the detector for the dry-run
report (#9 step 10) / escalation messaging.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ConflictObservation:
    """One conflict, located in the replay sequence + the code structure.

    ``commit_index`` is the 0-based position of the replayed commit it belonged
    to (``None`` when unknown — non-rebase or pre-plan). ``path``/``kind``/``name``
    are the region coordinate (from :class:`capybase.history.RegionKey`). Two
    observations with the same coordinate form a chain link.
    """

    commit_index: int | None
    path: str
    kind: str = "unknown"
    name: str = ""
    escalated: bool = False


@dataclass(frozen=True)
class ConflictChain:
    """A set of 2+ conflicts sharing a region coordinate across commits."""

    path: str
    kind: str
    name: str
    commit_indices: tuple[int, ...]
    escalated_count: int = 0

    @property
    def coordinate(self) -> str:
        """A compact ``path :: kind > name`` label."""
        return f"{self.path} :: {self.kind}" + (f" > {self.name}" if self.name else "")

    def characterization(self) -> str:
        """A one-line summary for escalation/dry-run messaging.

        e.g. "3 conflicts in cfg.py :: function > parse_config across commits 2, 4, 5".
        """
        commits = ", ".join(str(i + 1) for i in self.commit_indices)  # 1-based for humans
        esc = f" ({self.escalated_count} escalated)" if self.escalated_count else ""
        return (
            f"{len(self.commit_indices)} conflicts in {self.coordinate} "
            f"across commit(s) {commits}{esc}"
        )


@dataclass(frozen=True)
class ConflictChainReport:
    """The chains detected across a rebase."""

    chains: list[ConflictChain] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.chains

    @property
    def has_escalated_chain(self) -> bool:
        """Whether any chain includes an escalated conflict (the strategic case)."""
        return any(c.escalated_count > 0 for c in self.chains)


def detect_conflict_chains(
    observations: list[ConflictObservation],
) -> ConflictChainReport:
    """Group conflicts by shared region coordinate across replayed commits.

    A chain requires the SAME coordinate (path + kind + name) in conflicts from
    2+ DISTINCT commit indices. Returns the chains (largest first by commit
    count), plus escalation counts per chain. Observations with an unknown
    coordinate (``kind == "unknown"`` and no name) or unknown commit index are
    skipped (they can't form a structural chain). Never raises.
    """
    if not observations:
        return ConflictChainReport()
    try:
        # Group by (path, kind, name) → set of distinct commit indices.
        groups: dict[tuple[str, str, str], dict] = {}
        for obs in observations:
            # Skip un-locatable conflicts (no structural coordinate).
            if obs.kind == "unknown" and not obs.name:
                continue
            if obs.commit_index is None:
                continue
            key = (obs.path, obs.kind, obs.name)
            bucket = groups.setdefault(
                key, {"commits": set(), "escalated": 0}
            )
            bucket["commits"].add(obs.commit_index)
            if obs.escalated:
                bucket["escalated"] += 1
        chains: list[ConflictChain] = []
        for (path, kind, name), bucket in groups.items():
            commits = sorted(bucket["commits"])
            # A chain needs 2+ distinct commits sharing the coordinate.
            if len(commits) < 2:
                continue
            chains.append(ConflictChain(
                path=path, kind=kind, name=name,
                commit_indices=tuple(commits),
                escalated_count=bucket["escalated"],
            ))
        # Largest chains first (by commit count, then coordinate for stability).
        chains.sort(key=lambda c: (-len(c.commit_indices), c.coordinate))
        return ConflictChainReport(chains=chains)
    except Exception:  # noqa: BLE001 - advisory
        return ConflictChainReport()

"""Per-mechanism quality metrics (#9 step 9).

Aggregates the experience corpus (which now carries :mod:`provenance` from step 8)
into per-mechanism acceptance + later-failure counts:

    deterministic_structural: 91 accepted, 0 later probe failures
    history_augmented_llm:     17 accepted, 2 future probe failures
    exact_history_reuse:       12 accepted, 0 test failures
    plain_llm:                  8 accepted, 3 escalations

"Later failure" = an accepted experience whose recorded features show a
downstream probe or test failure (already journaled at resolution time; now
aggregated here). This lets capybase tune routing: if history-augmented LLM
isn't outperforming plain LLM on a local 3B model, reduce history prompt weight
or change retrieval examples.

Pure functions of the store — no new persistence. Exposed via the ``capybase
metrics`` CLI subcommand (text table) and as a structured dict for the dry-run
report (#9 step 10).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from capybase.provenance import LEGACY_PROVENANCE, PROVENANCE_VALUES, provenance_label

if TYPE_CHECKING:
    from capybase.memory.store import ExperienceStore


@dataclass(frozen=True)
class MechanismStats:
    """Quality counters for one resolution mechanism."""

    provenance: str
    accepted: int = 0
    escalated: int = 0
    later_probe_failures: int = 0
    later_test_failures: int = 0

    @property
    def total(self) -> int:
        return self.accepted + self.escalated

    @property
    def accept_rate(self) -> float:
        return (self.accepted / self.total) if self.total else 0.0


@dataclass(frozen=True)
class MetricsReport:
    """Per-mechanism quality metrics over the corpus."""

    by_mechanism: dict[str, MechanismStats] = field(default_factory=dict)
    legacy_count: int = 0  # experiences with no provenance (pre-step-8 data)

    def get(self, provenance: str) -> MechanismStats:
        return self.by_mechanism.get(
            provenance, MechanismStats(provenance=provenance)
        )

    def render_table(self) -> str:
        """A human-readable text table (for the CLI + dry-run report)."""
        lines = ["Per-mechanism quality metrics:"]
        header = f"  {'mechanism':<24} {'accept':>8} {'esc':>5} {'probe_f':>8} {'test_f':>7} {'rate':>6}"
        lines.append(header)
        lines.append("  " + "-" * (len(header) - 2))
        any_row = False
        for prov in PROVENANCE_VALUES:
            stats = self.by_mechanism.get(prov)
            if stats is None or stats.total == 0:
                continue
            any_row = True
            lines.append(
                f"  {provenance_label(prov):<24} {stats.accepted:>8} "
                f"{stats.escalated:>5} {stats.later_probe_failures:>8} "
                f"{stats.later_test_failures:>7} {stats.accept_rate:>5.0%}"
            )
        if self.legacy_count:
            lines.append(f"  {'(legacy/unknown)':<24} {self.legacy_count:>8}")
        if not any_row and not self.legacy_count:
            lines.append("  (no recorded resolutions yet)")
        return "\n".join(lines)


def _is_later_probe_failure(feats: dict) -> bool:
    """An accepted experience whose future-apply probe later failed."""
    # The probe result is journaled on the experience's features when available.
    probe_applies = feats.get("future_apply_probe_applies")
    probe_probed = feats.get("future_apply_probe_probed")
    return probe_probed is True and probe_applies is False


def _is_later_test_failure(feats: dict) -> bool:
    """An accepted experience whose step-level tests later failed."""
    return feats.get("tests_passed") is False


def compute_metrics(store: "ExperienceStore | None") -> MetricsReport:
    """Aggregate the corpus into per-mechanism quality metrics.

    Pure function of the store — reads each experience, buckets by provenance,
    and counts accepts/escalations/later-failures. Returns an empty report when
    the store is None or empty. Never raises.
    """
    if store is None:
        return MetricsReport()
    try:
        by_mech: dict[str, MechanismStats] = {}
        legacy = 0
        for exp in store:
            prov = exp.provenance or LEGACY_PROVENANCE
            feats = exp.validator_features or {}
            if prov == LEGACY_PROVENANCE or prov not in PROVENANCE_VALUES:
                # Unknown/legacy provenance — count separately, don't bucket.
                if prov == LEGACY_PROVENANCE:
                    legacy += 1
                else:
                    # A future unknown value: bucket it too so it's visible.
                    cur = by_mech.get(prov, MechanismStats(provenance=prov))
                    if exp.outcome == "accepted":
                        cur = _bump(cur, "accepted")
                    elif exp.outcome == "escalated":
                        cur = _bump(cur, "escalated")
                    by_mech[prov] = cur
                continue
            cur = by_mech.get(prov, MechanismStats(provenance=prov))
            if exp.outcome == "accepted":
                cur = _bump(cur, "accepted")
                if _is_later_probe_failure(feats):
                    cur = _bump(cur, "later_probe_failures")
                if _is_later_test_failure(feats):
                    cur = _bump(cur, "later_test_failures")
            elif exp.outcome == "escalated":
                cur = _bump(cur, "escalated")
            by_mech[prov] = cur
        return MetricsReport(by_mechanism=by_mech, legacy_count=legacy)
    except Exception:  # noqa: BLE001 - metrics are advisory
        return MetricsReport()


def _bump(stats: MechanismStats, field_name: str) -> MechanismStats:
    """Return a copy of ``stats`` with one counter incremented (frozen dataclass)."""
    vals = {
        "provenance": stats.provenance,
        "accepted": stats.accepted,
        "escalated": stats.escalated,
        "later_probe_failures": stats.later_probe_failures,
        "later_test_failures": stats.later_test_failures,
    }
    vals[field_name] = vals[field_name] + 1
    return MechanismStats(**vals)

"""Tests for conflict-chain detection (#9 step 7).

Detects related conflicts across the rebase: 2+ conflicts sharing a region
coordinate (path + kind + name) in distinct replayed commits. Used by the dry-run
report (#9 step 10) + escalation messaging.
"""

from __future__ import annotations

from capybase.conflict_chain import (
    ConflictChainReport,
    ConflictObservation,
    detect_conflict_chains,
)


def _obs(commit_index, path="cfg.py", kind="function", name="parse", escalated=False):
    return ConflictObservation(
        commit_index=commit_index, path=path, kind=kind,
        name=name, escalated=escalated,
    )


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------


def test_no_observations_yields_empty_report():
    assert detect_conflict_chains([]).empty


def test_single_conflict_is_not_a_chain():
    """A chain needs 2+ distinct commits sharing the coordinate."""
    report = detect_conflict_chains([_obs(2)])
    assert report.empty


def test_same_region_across_commits_forms_chain():
    """parse_config conflicts in commits 2, 4, 5 → one chain."""
    report = detect_conflict_chains([
        _obs(2, name="parse_config"),
        _obs(4, name="parse_config"),
        _obs(5, name="parse_config"),
    ])
    assert not report.empty
    assert len(report.chains) == 1
    chain = report.chains[0]
    assert chain.commit_indices == (2, 4, 5)
    assert chain.name == "parse_config"


def test_two_distinct_regions_form_two_chains():
    report = detect_conflict_chains([
        _obs(1, name="alpha"), _obs(3, name="alpha"),
        _obs(1, name="beta"), _obs(4, name="beta"),
    ])
    assert len(report.chains) == 2
    names = {c.name for c in report.chains}
    assert names == {"alpha", "beta"}


def test_same_region_same_commit_does_not_chain():
    """Two conflicts in the SAME commit at the same coordinate aren't a chain
    (a chain spans distinct commits — a migration, not one commit's two hunks)."""
    report = detect_conflict_chains([_obs(2, name="parse"), _obs(2, name="parse")])
    assert report.empty


def test_unknown_coordinate_skipped():
    """Conflicts with no structural coordinate (unknown kind + no name) can't
    form a chain — skipped."""
    report = detect_conflict_chains([
        ConflictObservation(commit_index=1, path="cfg.py", kind="unknown", name=""),
        ConflictObservation(commit_index=2, path="cfg.py", kind="unknown", name=""),
    ])
    assert report.empty


def test_unknown_commit_index_skipped():
    """Conflicts whose replayed commit is unknown can't chain (no position)."""
    report = detect_conflict_chains([
        _obs(None, name="parse"), _obs(None, name="parse"),
    ])
    assert report.empty


# ---------------------------------------------------------------------------
# escalation tracking + characterization
# ---------------------------------------------------------------------------


def test_escalated_chain_flagged():
    """A chain with an escalated conflict is flagged as strategic."""
    report = detect_conflict_chains([
        _obs(1, name="parse"),
        _obs(3, name="parse", escalated=True),
    ])
    assert not report.empty
    assert report.has_escalated_chain
    assert report.chains[0].escalated_count == 1


def test_characterization_human_readable():
    report = detect_conflict_chains([
        _obs(2, name="parse_config"), _obs(5, name="parse_config"),
    ])
    chain = report.chains[0]
    s = chain.characterization()
    assert "parse_config" in s
    assert "cfg.py" in s
    # 1-based commit positions for humans (indices 2,5 → commits 3, 6).
    assert "3, 6" in s


def test_coordinate_label():
    report = detect_conflict_chains([_obs(1, name="load"), _obs(2, name="load")])
    chain = report.chains[0]
    assert "load" in chain.coordinate
    assert "cfg.py" in chain.coordinate


def test_chains_sorted_largest_first():
    """The chain spanning the most commits is listed first."""
    report = detect_conflict_chains([
        # alpha: 2 commits
        _obs(1, name="alpha"), _obs(2, name="alpha"),
        # beta: 3 commits
        _obs(1, name="beta"), _obs(3, name="beta"), _obs(5, name="beta"),
    ])
    assert report.chains[0].name == "beta"
    assert report.chains[1].name == "alpha"

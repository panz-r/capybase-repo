"""Tests for conflict-chain detection (#9 step 7).

Detects related conflicts across the rebase: 2+ conflicts sharing a region
coordinate (path + kind + name) in distinct replayed commits. Used by the dry-run
report (#9 step 10) + escalation messaging.
"""

from __future__ import annotations

from capybase.conflict_chain import (
    ConflictChain,
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


# ---------------------------------------------------------------------------
# #idea 13: strategy recommendations
# ---------------------------------------------------------------------------


def _chain(commit_indices, *, name="parse", escalated=0, path="cfg.py", kind="function"):
    return ConflictChain(
        path=path, kind=kind, name=name,
        commit_indices=tuple(commit_indices),
        escalated_count=escalated,
    )


def test_escalated_chain_recommends_manual():
    """A chain with an escalation recommends resolving manually."""
    chain = _chain([1, 3], escalated=1)  # 0-based → 1-based commits 2,4
    rec = chain.recommendation()
    assert "manually" in rec
    assert "parse" in rec
    assert "2-4" in rec  # 1-based range of commits at indices 1,3


def test_wide_chain_recommends_holistic():
    """A chain spanning ≥4 commits recommends holistic branch-level resolution."""
    chain = _chain([1, 2, 3, 4, 5])  # 5 commits, no escalation
    rec = chain.recommendation()
    assert "holistic" in rec.lower()
    assert "5 commits" in rec


def test_multi_commit_chain_recommends_squash():
    """A 3-commit chain recommends squashing the specific commit range."""
    chain = _chain([2, 4, 5])  # 3 commits, no escalation
    rec = chain.recommendation()
    assert "squash" in rec.lower()
    assert "3-6" in rec  # 1-based: commits 3,5,6 → range 3-6


def test_rename_chain_recommends_split():
    """A chain whose name suggests a rename recommends splitting."""
    chain = _chain([1, 2], name="rename parse to load_config")
    rec = chain.recommendation()
    assert "split" in rec.lower() or "rename" in rec.lower()


def test_default_two_commit_chain_recommends_manual():
    """A 2-commit chain with no escalation falls through to manual resolve."""
    chain = _chain([1, 2])
    rec = chain.recommendation()
    assert "manually" in rec


def test_dryrun_summary_renders_chain_recommendation():
    """summary_history() shows each chain with its specific recommendation."""
    from capybase.dryrun import RehearsalReport, RehearsalStep

    chain = _chain([2, 4, 5])  # 3 commits → squash recommendation
    report = RehearsalReport(
        would_succeed=True, target="main", head_before="aaa", head_after="bbb",
        session_id="s", history_active=True,
    )
    report.steps = [RehearsalStep(step=1, accepted=True)]
    report.conflict_chains = [chain.characterization()]
    report.conflict_chain_objects = [chain]
    out = report.summary_history()
    assert "conflict chain" in out
    assert "squash" in out.lower()
    # The overall recommended action uses the chain's recommendation too.
    assert "squash" in report._recommended_action().lower()

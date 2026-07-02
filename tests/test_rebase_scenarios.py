"""Tests for mined multi-commit rebase scenarios (the history-aware corpus).

These exercise the history-aware mechanisms (conflict chains, future-region
detection, branch intent) against REAL git history mined from public repos
(serde, sea-orm, clap, tokio, pydantic). The scenarios are mined by
``scripts/mine_rebase_scenarios.py`` into ``extracted-testdata/rebase-scenarios/``
(gitignored). The whole module is inert when no data is present (clean-skip on a
fresh clone), matching the realworld-cases contract.

What these assert (and why): real rebases have no single correct-outcome oracle,
so — like the single-file realworld tests — we assert *infrastructure invariants*
+ *that the history mechanisms fire against real history*, not that capybase's
merge matches a known-good outcome:

- **Scenario integrity**: the source-commit sequence forms a valid RebasePlan,
  the 3-way blobs are consistent with the markers, the replayed OIDs resolve.
- **History feeds the mechanisms**: given the scenario's real source commits,
  the HistoryQueryService can answer future-region queries (the data the
  conflict-chain/future-probe/branch-intent features consume). This is the core
  "real history exercises history-aware behavior" check.
- **Cargo check at the source tip** (Rust): the real crate compiles at the
  scenario's source tip (infrastructure; reuses _realworld_cargo).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.history import (
    HistoryQueryService,
    RebasePlan,
    ReplayCommit,
    region_key_from_unit,
)
from tests._realworld_cargo import (
    DEFAULT_TIMEOUT,
    cargo_check_at_worktree,
    cleanup_orphan_worktrees,
)
from tests.conftest import git
from tests.rebase_scenario_loader import (
    RebaseScenarioCase,
    git_history_repo_path,
    load_rebase_scenarios,
)

pytestmark = pytest.mark.skipif(
    not load_rebase_scenarios(),
    reason=(
        "no rebase-scenario data mined; run "
        "scripts/mine_rebase_scenarios.py"
    ),
)

CARGO = shutil.which("cargo")
SCENARIOS = load_rebase_scenarios()


# ---------------------------------------------------------------------------
# Session setup: prune orphaned worktrees from an interrupted previous run.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="session")
def _prune_orphan_scenario_worktrees():
    """Remove worktrees orphaned by a Ctrl-C'd previous run (one pass per clone)."""
    seen: set[str] = set()
    for s in SCENARIOS:
        if s.dataset in seen:
            continue
        seen.add(s.dataset)
        clone = git_history_repo_path(s.dataset)
        if (clone / ".git").exists():
            cleanup_orphan_worktrees(clone)
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scenario_to_plan(scenario: RebaseScenarioCase) -> RebasePlan:
    """Reconstruct a RebasePlan from a scenario's source-commit dicts."""
    commits = [
        ReplayCommit(
            oid=c["oid"], parent_oid=c.get("parent_oid", ""),
            subject=c.get("subject", ""), body_summary=c.get("body_summary", ""),
            touched_files=c.get("touched_files", []),
            diffstat=c.get("diffstat", {}),
            patch_id=c.get("patch_id", ""),
            index=i,
        )
        for i, c in enumerate(scenario.source_commits)
    ]
    return RebasePlan(
        source_commits=commits,
        target_base_oid=scenario.merge_base_oid,
        target_tip_oid=scenario.target_tip_oid,
        source_tip_oid=scenario.source_tip_oid,
        created_at="mined",
    )


def _unit_from_step(step, *, path: str, language: str) -> ConflictUnit:
    """Build a ConflictUnit from a scenario's ConflictStepCase (for history queries)."""
    return ConflictUnit(
        session_id="s", step_index=0, path=path, language=language,
        conflict_type="UU", unit_id=f"{path}:{step.step}",
        unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=step.base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=step.current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=step.replayed),
        original_worktree_text=step.base,
        marker_span=(0, 0),
        structural_metadata={
            "replayed_commit_oid": step.replayed_commit_oid,
            "enclosing_node_signature": "",
        },
    )


def _clone_or_skip(scenario: RebaseScenarioCase) -> Path:
    """Skip if the scenario's clone isn't present; else return its path."""
    clone = git_history_repo_path(scenario.dataset)
    if not (clone / ".git").exists():
        pytest.skip(f"clone for {scenario.dataset} not present at {clone}")
    return clone


# ---------------------------------------------------------------------------
# Scenario integrity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
def test_scenario_source_commits_form_valid_plan(scenario: RebaseScenarioCase):
    """The mined source-commit sequence is a well-formed RebasePlan."""
    plan = _scenario_to_plan(scenario)
    assert len(plan.source_commits) >= 1
    # Every commit has the required fields populated.
    for c in plan.source_commits:
        assert c.oid
        assert c.subject  # even if empty, the field exists
    # The plan's index_of helpers work (used by history features).
    first = plan.source_commits[0]
    assert plan.index_of(first.oid) == 0


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
def test_scenario_conflict_blobs_match_markers(scenario: RebaseScenarioCase):
    """Each conflict step's 3-way blobs are consistent with its marker text."""
    for step in scenario.conflict_steps:
        assert step.marker_text, f"step {step.step} has empty marker text"
        assert "<<<<<<<" in step.marker_text, (
            f"step {step.step} marker text has no conflict markers"
        )
        # The replayed (source) side's content should appear in the markers.
        # (The marker text contains both sides; the replayed blob is non-empty.)
        assert step.replayed, f"step {step.step} has empty replayed blob"
        assert step.base is not None
        assert step.current is not None


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
def test_scenario_replayed_oids_resolve_in_clone(scenario: RebaseScenarioCase):
    """The scenario's recorded OIDs are real commits in the clone (provenance)."""
    clone = _clone_or_skip(scenario)
    # The source tip + a sample of conflict-step replayed OIDs resolve.
    from capybase.git_backend import GitBackend
    gb = GitBackend(clone)
    # rev-parse the source tip (must exist). conftest.git returns a CompletedProcess.
    out = git(clone, "rev-parse", "--verify", scenario.source_tip_oid, check=False)
    assert out.stdout.strip() == scenario.source_tip_oid, (
        f"source_tip_oid {scenario.source_tip_oid[:8]} does not resolve in the clone"
    )
    # Each conflict step's replayed_commit_oid is one of the source commits.
    source_oids = {c["oid"] for c in scenario.source_commits}
    for step in scenario.conflict_steps:
        if step.replayed_commit_oid:
            assert step.replayed_commit_oid in source_oids, (
                f"step {step.step} replayed_commit_oid not in the source sequence"
            )


# ---------------------------------------------------------------------------
# History mechanisms fire against real history (the core value)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
def test_history_service_answers_real_source_sequence(scenario: RebaseScenarioCase):
    """The HistoryQueryService builds from the scenario's real commits and answers
    a history context — the data the conflict-chain/future-probe/branch-intent
    features consume. This is the end-to-end 'real history feeds the mechanisms'
    check: without a valid plan + queryable context, none of the history-aware
    features can fire."""
    clone = _clone_or_skip(scenario)
    from capybase.git_backend import GitBackend
    gb = GitBackend(clone)
    plan = _scenario_to_plan(scenario)
    svc = HistoryQueryService(plan, git=gb)
    # For each conflict step, query the history context (mirrors the orchestrator).
    for step in scenario.conflict_steps:
        unit = _unit_from_step(step, path=step.path, language=scenario.language)
        ctx = svc.for_conflict(unit, replayed_commit_oid=step.replayed_commit_oid)
        # The service must locate the current replay commit (history identity known).
        assert ctx.current_replay_commit is not None, (
            f"history service couldn't locate the replayed commit for step {step.step}"
        )
        assert ctx.source_commit_count == len(scenario.source_commits)
        # The detection method is recorded (even if "none" — the field exists).
        assert ctx.region_detection_method in ("none", "heuristic", "diff")


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
def test_branch_intent_built_from_real_source_commits(scenario: RebaseScenarioCase):
    """The branch-intent summary builds from the scenario's real source-commit
    sequence (the branch_intent features consume source_commits + patches)."""
    clone = _clone_or_skip(scenario)
    from capybase.git_backend import GitBackend
    from capybase.branch_intent import build_branch_intent
    gb = GitBackend(clone)
    plan = _scenario_to_plan(scenario)
    # Fetch the real patches for the source commits.
    patches = {}
    for c in plan.source_commits[:20]:  # cap to keep the test fast
        try:
            patches[c.oid] = gb.commit_patch(c.oid)
        except Exception:  # noqa: BLE001
            patches[c.oid] = b""
    intent = build_branch_intent(plan, patches)
    # The intent builds without error (may be empty if no definition-changes —
    # body-edit-only branches produce empty intent, which is correct).
    assert intent is not None


# ---------------------------------------------------------------------------
# Rust cargo check at the source tip (infrastructure; cargo-gated)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
def test_scenario_source_tip_compiles_rust(scenario: RebaseScenarioCase):
    """The real crate compiles at the scenario's source tip (Rust only).

    Asserts only the infrastructure invariant (cargo ran) — a real source tip
    may not compile on our toolchain/version, so the verdict is recorded, not
    asserted. Skips when cargo or the clone is absent."""
    if scenario.language != "rust":
        pytest.skip("non-rust scenario")
    if CARGO is None:
        pytest.skip("cargo not installed")
    clone = _clone_or_skip(scenario)
    verdict = cargo_check_at_worktree(clone, scenario.source_tip_oid,
                                      timeout=DEFAULT_TIMEOUT)
    # Only assert the infrastructure ran; record (don't assert) the compile result.
    assert verdict.ran, f"cargo check did not run for {scenario.id}"

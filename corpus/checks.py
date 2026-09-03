"""The corpus checks (plain functions — NO pytest).

Ported verbatim from the former tests/test_realworld_conflicts.py bodies;
the skip conditions became early returns (the runner treats a returned
SKIP sentinel as a skip, not a failure). Same oracle policy: assert only
the infrastructure invariants (the floor/build ENGAGED); record compile
verdicts honestly without asserting them.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from capybase.config import Config
from capybase.verification import VerificationEngine
from corpus._realworld_build import (
    C_PREPARE_COMMANDS,
    resolve_c_build_at_sha,
    run_command_at_worktree,
)
from capybase.adapters.parsers import parse_marker_blocks
from capybase.verification import contains_markers
from corpus.realworld_loader import RealWorldCase, git_history_repo_path

SKIP = object()  # sentinel: check not applicable / data absent


def _engine() -> VerificationEngine:
    cfg = Config()
    return VerificationEngine.default(cfg.validation)


def check_marker_parses(case: RealWorldCase, _tmp: Path):
    blocks = parse_marker_blocks(case.marker_original)
    assert len(blocks) >= 1, f"{case.id}: no conflict blocks in marker_original"


def check_human_merge_marker_free(case: RealWorldCase, _tmp: Path):
    assert not contains_markers(case.expected_resolved), (
        f"{case.id}: the human merge M still contains conflict markers"
    )


def check_python_verifier_verdict(case: RealWorldCase, tmp: Path):
    if case.language != "python":
        return SKIP
    eng = _engine()
    res = eng.verify_file(
        case.path, case.language, case.expected_resolved, [],
        repo_root=str(tmp),
    )
    assert res.features.get("syntax_checked") is True, (
        f"{case.id}: py_compile floor did not engage — infrastructure "
        f"regression. features={res.features}"
    )
    if not res.passed:
        msgs = [f.message[:80] for f in res.hard_failures[:2]]
        print(f"  {case.id}: human merge did not pass py_compile: {msgs}")


def check_c_gcc_verdict(case: RealWorldCase, tmp: Path):
    if case.language != "c":
        return SKIP
    eng = _engine()
    res = eng.verify_file(
        case.path, case.language, case.expected_resolved, [],
        repo_root=str(tmp),
    )
    assert res.features.get("syntax_checked") is True, (
        f"{case.id}: gcc floor did not engage — infrastructure regression. "
        f"features={res.features}"
    )
    if not res.passed:
        msgs = [f.message[:80] for f in res.hard_failures[:2]]
        print(f"  {case.id}: human merge did not pass gcc: {msgs}")


def _era_c_build_command(clone: Path, sha: str, dataset: str) -> str:
    """The era-aware prepare+build chain for a commit, as one shell command.

    Resolves (prepare, build) by probing the TREE at ``sha`` (git ls-tree —
    no worktree needed), exactly as the eval harness's build gate does for
    its materialized trees (corpus._realworld_build.resolve_c_build_at_sha;
    era-aware: autoreconf for stale-configure eras, per-dataset CFLAGS and
    include flags). Empty string = no build system known at that commit.
    """
    default_prepare = C_PREPARE_COMMANDS.get(dataset, "")
    if "{jobs}" in default_prepare:
        # Resolve here (not $(nproc)): the chained command runs through a
        # shell, but keep the form identical to the eval's resolution.
        import os as _os
        default_prepare = default_prepare.format(jobs=max(4, _os.cpu_count() or 4))
    prepare, build = resolve_c_build_at_sha(clone, sha, dataset, default_prepare)
    if build == "true":
        return ""  # unknown build system — no whole-tree oracle build
    return f"{prepare} && {build}" if prepare else build


def check_c_build_verdict(case: RealWorldCase, _tmp: Path):
    if case.language != "c":
        return SKIP
    if not case.merge_sha:
        return SKIP
    clone = git_history_repo_path(case.dataset)
    if not (clone / ".git").exists():
        return SKIP  # clone not fetched
    cmd = _era_c_build_command(clone, case.merge_sha, case.dataset)
    if not cmd:
        return SKIP  # no build system detected at this commit
    verdict = run_command_at_worktree(clone, case.merge_sha, cmd, timeout=600)
    assert verdict.ran, (
        f"{case.id}: build did not run (worktree/command failure): "
        f"{verdict.errors}"
    )
    if not verdict.compiled:
        print(f"  {case.id}: human merge did not build ({cmd}): "
              f"{verdict.errors[:2]}")


def check_rust_cargo_verdict(case: RealWorldCase, _tmp: Path):
    if case.language != "rust":
        return SKIP
    if not shutil.which("cargo"):
        return SKIP
    clone = git_history_repo_path(case.dataset)
    if not (clone / ".git").exists():
        return SKIP
    if not case.merge_sha:
        return SKIP
    from corpus._realworld_cargo import cargo_check_at_worktree
    verdict = cargo_check_at_worktree(clone, case.merge_sha)
    assert verdict.ran, (
        f"{case.id}: cargo check did not engage at {case.merge_sha[:12]} — "
        f"infrastructure regression. errors={verdict.errors}"
    )
    print(f"  {case.id}: cargo check at {case.merge_sha[:12]} "
          f"({case.conflict_path}): {verdict.verdict}")
    for e in verdict.errors[:3]:
        print(f"    {e}")


def checks_for(case: RealWorldCase):
    """The (name, fn) checks applicable to a case, in order."""
    yield "marker_parses", check_marker_parses
    yield "merge_marker_free", check_human_merge_marker_free
    yield "verifier_verdict", {
        "python": check_python_verifier_verdict,
        "c": check_c_gcc_verdict,
    }.get(case.language, None) or _noop
    if case.language == "c":
        yield "build_verdict", check_c_build_verdict
    if case.language == "rust":
        yield "cargo_verdict", check_rust_cargo_verdict


def _noop(case: RealWorldCase, tmp: Path):
    return SKIP


# ---------------------------------------------------------------------------
# Session-cases checks (from the former tests/test_session_conflicts.py)
# ---------------------------------------------------------------------------

def check_session_marker_parses(case, _tmp: Path):
    blocks = parse_marker_blocks(case.marker_original)
    assert len(blocks) >= 1, f"{case.id}: no conflict blocks in marker_original"


def check_session_sides_round_trip(case, _tmp: Path):
    blocks = parse_marker_blocks(case.marker_original)
    assert blocks, f"{case.id}: marker did not parse"
    block = blocks[0]
    assert block.current_text == case.current, (
        f"{case.id}: current side mismatch — prompt parser mis-extracted it")
    assert block.replayed_text == case.replayed, (
        f"{case.id}: replayed side mismatch — prompt parser mis-extracted it")
    if case.base.strip():
        assert block.base_text == case.base, (
            f"{case.id}: base side mismatch — prompt parser mis-extracted it")


def check_session_resolution_marker_free(case, _tmp: Path):
    assert not contains_markers(case.accepted_resolution), (
        f"{case.id}: accepted resolution still contains conflict markers")


def check_session_python_floor_engages(case, tmp: Path):
    if case.language != "python":
        return SKIP
    blocks = parse_marker_blocks(case.marker_original)
    assert blocks, f"{case.id}: marker did not parse"
    spans_and_texts = [(blocks[0].span(), case.accepted_resolution)]
    eng = _engine()
    res = eng.verify_file(
        case.path, case.language, case.marker_original, spans_and_texts,
        repo_root=str(tmp),
    )
    assert res.features.get("syntax_checked") is True, (
        f"{case.id}: py_compile floor did not engage — infrastructure "
        f"regression. features={res.features}")


def check_session_placeholder_flag_honest(case, _tmp: Path):
    stub = "<merged replacement text>"
    if case.is_placeholder_resolution:
        assert stub in case.accepted_resolution, (
            f"{case.id}: is_placeholder_resolution=True but the stub is absent")
    else:
        assert stub not in case.accepted_resolution, (
            f"{case.id}: is_placeholder_resolution=False but the stub is present")


# ---------------------------------------------------------------------------
# Rebase-scenario checks (from the former tests/test_rebase_scenarios.py —
# DEF-1 port; bodies verbatim, pytest idioms became SKIP returns)
# ---------------------------------------------------------------------------

def _scenario_to_plan(scenario):
    from capybase.conflict_model import ConflictSide, ConflictUnit
    from capybase.history import ReplayCommit, RebasePlan
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


def _scenario_clone(scenario):
    from corpus.rebase_scenario_loader import git_history_repo_path
    clone = git_history_repo_path(scenario.dataset)
    if not (clone / ".git").exists():
        return None
    return clone


def check_scenario_plan_valid(scenario, _tmp: Path):
    plan = _scenario_to_plan(scenario)
    assert len(plan.source_commits) >= 1
    for c in plan.source_commits:
        assert c.oid
        assert c.subject is not None
    first = plan.source_commits[0]
    assert plan.index_of(first.oid) == 0


def check_scenario_blobs_match_markers(scenario, _tmp: Path):
    for step in scenario.conflict_steps:
        assert step.marker_text, f"step {step.step} has empty marker text"
        assert "<<<<<<<" in step.marker_text, (
            f"step {step.step} marker text has no conflict markers")
        assert step.base or step.current or step.replayed, (
            f"step {step.step} all three blobs empty (malformed)")


def check_scenario_oids_resolve(scenario, _tmp: Path):
    from corpus._gitshim import git
    clone = _scenario_clone(scenario)
    if clone is None:
        return SKIP
    out = git(clone, "rev-parse", "--verify", scenario.source_tip_oid,
              check=False)
    assert out.stdout.strip() == scenario.source_tip_oid, (
        f"source_tip_oid {scenario.source_tip_oid[:8]} does not resolve "
        f"in the clone")
    source_oids = {c["oid"] for c in scenario.source_commits}
    for step in scenario.conflict_steps:
        if step.replayed_commit_oid:
            assert step.replayed_commit_oid in source_oids, (
                f"step {step.step} replayed_commit_oid not in the source "
                f"sequence")


def check_scenario_history_service(scenario, _tmp: Path):
    from capybase.conflict_model import ConflictSide, ConflictUnit
    from capybase.git_backend import GitBackend
    from capybase.history import HistoryQueryService, region_key_from_unit
    clone = _scenario_clone(scenario)
    if clone is None:
        return SKIP
    gb = GitBackend(clone)
    plan = _scenario_to_plan(scenario)
    svc = HistoryQueryService(plan, git=gb)
    for step in scenario.conflict_steps:
        unit = ConflictUnit(
            session_id="corpus", step_index=step.step, path=step.path,
            language=scenario.language, conflict_type="UU",
            unit_id=f"{step.path}:{step.step}",
            unit_kind="text_marker_block",
            base=ConflictSide(label="BASE", text=step.base or ""),
            current=ConflictSide(label="CURRENT_UPSTREAM_SIDE",
                                text=step.current or ""),
            replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE",
                                 text=step.replayed or ""),
            original_worktree_text=step.marker_text or "",
            marker_span=(0, max(0, len((step.marker_text or "").splitlines()) - 1)),
        )
        ctx = svc.for_conflict(unit,
                               replayed_commit_oid=step.replayed_commit_oid)
        assert ctx.current_replay_commit is not None, (
            f"history service couldn't locate the replayed commit for "
            f"step {step.step}")
        assert ctx.source_commit_count == len(scenario.source_commits)
        assert ctx.region_detection_method in ("none", "heuristic", "diff")


def check_scenario_branch_intent(scenario, _tmp: Path):
    from capybase.branch_intent import build_branch_intent
    from capybase.git_backend import GitBackend
    clone = _scenario_clone(scenario)
    if clone is None:
        return SKIP
    gb = GitBackend(clone)
    plan = _scenario_to_plan(scenario)
    patches = {}
    for c in plan.source_commits[:20]:
        try:
            patches[c.oid] = gb.commit_patch(c.oid)
        except Exception:  # noqa: BLE001
            patches[c.oid] = b""
    intent = build_branch_intent(plan, patches)
    assert intent is not None


def check_scenario_source_tip_compiles_rust(scenario, _tmp: Path):
    if scenario.language != "rust":
        return SKIP
    import shutil as _sh
    if not _sh.which("cargo"):
        return SKIP
    clone = _scenario_clone(scenario)
    if clone is None:
        return SKIP
    from corpus._realworld_cargo import DEFAULT_TIMEOUT, cargo_check_at_worktree
    verdict = cargo_check_at_worktree(
        clone, scenario.source_tip_oid, timeout=DEFAULT_TIMEOUT)
    assert verdict.ran, f"cargo check did not run for {scenario.id}"
    print(f"  {scenario.id}: source tip cargo: {verdict.verdict}")


def check_scenario_source_tip_builds_c(scenario, _tmp: Path):
    if scenario.language != "c":
        return SKIP
    clone = _scenario_clone(scenario)
    if clone is None:
        return SKIP
    cmd = _era_c_build_command(clone, scenario.source_tip_oid, scenario.dataset)
    if not cmd:
        return SKIP
    from corpus._realworld_cargo import DEFAULT_TIMEOUT
    verdict = run_command_at_worktree(
        clone, scenario.source_tip_oid, cmd, timeout=DEFAULT_TIMEOUT)
    assert verdict.ran, f"build did not run for {scenario.id}: {verdict.errors}"
    if not verdict.compiled:
        print(f"  {scenario.id}: source tip did not build ({cmd}): "
              f"{verdict.errors[:2]}")


def scenario_checks_for(scenario):
    yield "plan_valid", check_scenario_plan_valid
    yield "blobs_match", check_scenario_blobs_match_markers
    yield "oids_resolve", check_scenario_oids_resolve
    yield "history_service", check_scenario_history_service
    yield "branch_intent", check_scenario_branch_intent
    if scenario.language == "rust":
        yield "tip_cargo", check_scenario_source_tip_compiles_rust
    if scenario.language == "c":
        yield "tip_build", check_scenario_source_tip_builds_c

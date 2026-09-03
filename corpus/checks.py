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
from corpus._realworld_build import C_BUILD_COMMANDS, run_command_at_worktree
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


def check_c_build_verdict(case: RealWorldCase, _tmp: Path):
    if case.language != "c":
        return SKIP
    cmd = C_BUILD_COMMANDS.get(case.dataset)
    if not cmd:
        return SKIP  # no build command registered for this dataset
    if not case.merge_sha:
        return SKIP
    clone = git_history_repo_path(case.dataset)
    if not (clone / ".git").exists():
        return SKIP  # clone not fetched
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

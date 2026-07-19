"""Tests for the pre-continue test-command auto-substitution.

The shipped default is ``"pytest"`` (Python-centric). For a Cargo project with no
Python project manifest, capybase substitutes ``"cargo test"`` so a pure-Rust
repo works out of the box. These cover the three layouts: single crate at root,
Cargo workspace (member crates in subdirs, no root manifest), and a genuine mixed
repo (cargo + pyproject.toml → honor the configured pytest).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from capybase.config import Config
from capybase.orchestrator import Orchestrator, _has_python_project, _repo_has_cargo

from tests.conftest import git


def _orch(repo: Path) -> Orchestrator:
    return Orchestrator(Config(), repo=str(repo), out=lambda *_a, **_k: None)


def test_run_tests_escalates_when_required_but_no_command(repo: Path):
    """Bug #10: when tests.required=True but the per-label command (pre_continue
    or final) is explicitly unset (None/empty), _run_tests silently returned
    True — skipping a user-REQUIRED test gate. A required gate with no command
    is a misconfiguration that should escalate (return False) rather than
    silently pass. The separate 'default command not found in this repo' path
    (a Go/JS repo with no pytest) is a different, narrower exception and stays
    a pass-with-warning."""
    from capybase.orchestrator import StepResult
    cfg = Config()
    cfg.tests.required = True
    cfg.tests.pre_continue = None  # explicitly unset
    cfg.tests.final = None
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    result = StepResult(step_index=0, units_by_path={})
    # With required=True and no command, must NOT silently pass.
    passed = orch._run_tests("pre_continue", result)
    assert passed is False, (
        f"required test gate with no command silently passed: {passed}"
    )


def test_run_tests_passes_when_not_required_and_no_command(repo: Path):
    """Regression guard: when tests.required=False (the permissive case) and no
    command is set, _run_tests correctly passes (no gate configured → continue).
    The bug #10 fix only escalates when required=True."""
    from capybase.orchestrator import StepResult
    cfg = Config()
    cfg.tests.required = False
    cfg.tests.pre_continue = None
    cfg.tests.final = None
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    result = StepResult(step_index=0, units_by_path={})
    passed = orch._run_tests("pre_continue", result)
    assert passed is True, (
        f"non-required gate with no command should pass: {passed}"
    )


def test_repo_has_cargo_root_manifest(repo: Path):
    (repo / "Cargo.toml").write_text('[package]\nname="x"\nversion="0"\n')
    assert _repo_has_cargo(repo)


def test_repo_has_cargo_workspace_subdir(repo: Path):
    """A workspace: member crate in a subdir, no root manifest → has cargo."""
    member = repo / "di-core"
    member.mkdir()
    (member / "Cargo.toml").write_text('[package]\nname="x"\nversion="0"\n')
    assert _repo_has_cargo(repo)
    assert not (repo / "Cargo.toml").exists()  # no root manifest


def test_repo_has_cargo_false_for_plain_repo(repo: Path):
    assert not _repo_has_cargo(repo)


def test_has_python_project_markers(repo: Path):
    assert not _has_python_project(repo)
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert _has_python_project(repo)


def test_workspace_cargo_substitutes_cargo_test(repo: Path, monkeypatch):
    """A Cargo workspace (no root manifest, no pyproject) → cargo test.

    Regression for the di-rac-rebase-test case: the root-only cargo check missed
    the workspace, so the test gate stayed on pytest and failed with
    "No such file or directory: 'pytest'".
    """
    member = repo / "di-core"
    member.mkdir()
    (member / "Cargo.toml").write_text('[package]\nname="x"\nversion="0"\n')
    # No pyproject.toml → not a Python project.
    # The substitution must not depend on pytest being on PATH (capybase's own
    # venv has pytest; that's irrelevant to a Rust repo).
    monkeypatch.setattr("shutil.which", lambda cmd: "/fake/pytest" if cmd == "pytest" else None)
    orch = _orch(repo)
    assert orch._resolve_test_command("pytest") == "cargo test"


def test_root_cargo_substitutes_cargo_test(repo: Path):
    (repo / "Cargo.toml").write_text('[package]\nname="x"\nversion="0"\n')
    orch = _orch(repo)
    assert orch._resolve_test_command("pytest") == "cargo test"


def test_mixed_repo_keeps_pytest(repo: Path):
    """Cargo + a real Python project (pyproject.toml) → honor the configured pytest."""
    (repo / "Cargo.toml").write_text('[package]\nname="x"\nversion="0"\n')
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    orch = _orch(repo)
    assert orch._resolve_test_command("pytest") == "pytest"


def test_explicit_command_is_never_overridden(repo: Path):
    """A command other than the bare 'pytest' default is returned unchanged —
    we never override a deliberate choice (even in a cargo repo)."""
    (repo / "Cargo.toml").write_text('[package]\nname="x"\nversion="0"\n')
    orch = _orch(repo)
    assert orch._resolve_test_command("cargo test") == "cargo test"
    assert orch._resolve_test_command("pytest -x") == "pytest -x"


# ---------------------------------------------------------------------------
# _cargo_test_cwd: where to run cargo in a workspace (the edit-resolved regression)
# ---------------------------------------------------------------------------


def _step_result_with(units_by_path: dict | None = None) -> "StepResult":
    """A minimal StepResult for the cwd-resolution tests."""
    from capybase.orchestrator import StepResult

    r = StepResult(step_index=1)
    r.units_by_path = units_by_path or {}
    return r


def test_cargo_test_cwd_anchors_on_conflicted_path_in_workspace(repo: Path):
    """A conflicted file in a member crate → cargo runs from that crate dir."""
    member = repo / "di-core"
    (member / "src").mkdir(parents=True)
    (member / "Cargo.toml").write_text('[package]\nname="x"\nversion="0"\n')
    orch = _orch(repo)
    cwd = orch._cargo_test_cwd(
        _step_result_with({"di-core/src/tools/edit_file.rs": []}), "cargo check"
    )
    assert cwd is not None
    assert Path(cwd).name == "di-core"


def test_cargo_test_cwd_uses_staged_files_when_no_conflicts(repo: Path):
    """Edit-resolved step: no conflicts, but the resolution is staged → cargo
    must still run from the staged file's crate dir, not the workspace root.

    Regression: the old logic iterated only units_by_path (empty here) → no
    anchor → cargo ran from the workspace root (no Cargo.toml) → "could not find
    Cargo.toml" aborted a correct rebase.
    """
    from capybase.git_backend import GitBackend
    from tests.conftest import git

    member = repo / "di-core"
    (member / "src").mkdir(parents=True)
    (member / "Cargo.toml").write_text('[package]\nname="x"\nversion="0"\n')
    (member / "src" / "lib.rs").write_text("")  # commit something so staging works
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "di-core/src/lib.rs")
    git(repo, "commit", "-q", "-m", "init", check=False)
    # Simulate a staged resolution (the edit-resolved file).
    (member / "src" / "resolved.rs").write_text("pub fn f() {}\n")
    git(repo, "add", "di-core/src/resolved.rs")

    orch = Orchestrator(Config(), repo=str(repo), out=lambda *_a, **_k: None)
    cwd = orch._cargo_test_cwd(_step_result_with(), "cargo check")
    assert cwd is not None
    assert Path(cwd).name == "di-core"


def test_cargo_test_cwd_falls_back_to_any_member_crate(repo: Path):
    """No conflicts AND no staged files (e.g. a clean-apply step) → cargo still
    runs from a member crate, never the workspace root."""
    member = repo / "di-core"
    (member / "src").mkdir(parents=True)
    (member / "Cargo.toml").write_text('[package]\nname="x"\nversion="0"\n')
    orch = _orch(repo)
    cwd = orch._cargo_test_cwd(_step_result_with(), "cargo check")
    assert cwd is not None
    assert Path(cwd).name == "di-core"


def test_cargo_test_cwd_none_for_root_manifest(repo: Path):
    """Single crate at root (root Cargo.toml) → None (cargo runs from root)."""
    (repo / "Cargo.toml").write_text('[package]\nname="x"\nversion="0"\n')
    orch = _orch(repo)
    assert orch._cargo_test_cwd(_step_result_with(), "cargo check") is None


def test_cargo_test_cwd_none_for_non_cargo_command(repo: Path):
    """A non-cargo command → no cwd override (None)."""
    orch = _orch(repo)
    assert orch._cargo_test_cwd(_step_result_with({"a.py": []}), "pytest") is None


# ---------------------------------------------------------------------------
# Default-command-not-found: a repo the default "pytest" doesn't fit (no cargo,
# no pytest — e.g. a Go/JS repo) must NOT block the rebase. The shipped default
# resolving to a missing command is "no test gate for this repo", not a failure.
# An explicit user-configured command that's missing still fails (deliberate).
# ---------------------------------------------------------------------------


def _not_found_run():
    """A TestRunResult shaped like a missing-command run."""
    from capybase.adapters.tests import TestRunResult, TestVerdict

    return TestRunResult(
        passed=False, returncode=-1, stdout="", stderr="'pytest' not found",
        command="pytest",
        verdict=TestVerdict(
            kind="unknown", tool="",
            summary="test command not found: 'pytest'",
        ),
    )


def test_default_command_not_found_skips_gate(repo: Path, monkeypatch):
    """The default pytest, missing on a non-Python/non-Rust repo → gate skipped."""
    from capybase.orchestrator import StepResult

    orch = _orch(repo)  # default config: pre_continue="pytest", no cargo here
    # Force the runner to report "command not found" (no pytest installed).
    monkeypatch.setattr(orch.tests, "run", lambda cmd, cwd=None: _not_found_run())
    ok = orch._run_tests("pre_continue", StepResult(step_index=1))
    assert ok is True  # the gate was skipped, not failed


def test_explicit_command_not_found_fails_gate(repo: Path, monkeypatch):
    """An explicit user command (not the default) that's missing → hard fail."""
    from capybase.orchestrator import StepResult

    cfg = Config()
    cfg.tests.pre_continue = "my-custom-suite"  # explicit, not the default
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    monkeypatch.setattr(orch.tests, "run", lambda cmd, cwd=None: _not_found_run())
    ok = orch._run_tests("pre_continue", StepResult(step_index=1))
    assert ok is False  # a deliberate command that's missing still fails


def test_default_command_failing_tests_still_fail_gate(repo: Path, monkeypatch):
    """The skip only applies to 'command not found', NOT to genuinely failing
    tests. A default-pytest run that exits non-zero (tests fail) must still
    fail the gate — the skip is about a missing tool, not a red suite."""
    from capybase.adapters.tests import TestRunResult, TestVerdict
    from capybase.orchestrator import StepResult

    orch = _orch(repo)
    failing = TestRunResult(
        passed=False, returncode=1, stdout="1 failed", stderr="",
        command="pytest",
        verdict=TestVerdict(kind="test_failure", tool="pytest",
                            summary="1 test failed"),
    )
    monkeypatch.setattr(orch.tests, "run", lambda cmd, cwd=None: failing)
    ok = orch._run_tests("pre_continue", StepResult(step_index=1))
    assert ok is False

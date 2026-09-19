"""Fixture-infrastructure guards: specs are complete, the builder is
deterministic, conflicts reproduce, resolutions validate.

The fixtures dir used to be a submodule pinned to a machine-local
file:/// URL — uncloneable anywhere else and a local path in tracked
state. It is now declarative specs + a deterministic builder
(fixtures/build.py); these tests pin that contract:

  - every spec carries the full oracle tuple (base, both sides, expected
    developer resolution, acceptable alternatives, validation commands)
  - the same spec builds byte-identical commits (identical OIDs)
  - a plain `git rebase` in the built repo produces the pinned conflict
    (same file, same hunk count)
  - the expected resolution AND each acceptable alternative complete the
    stopped rebase and pass the spec's validation commands
  - no machine-local path re-enters tracked fixture state
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
SPECS_DIR = _REPO / "fixtures" / "specs"
BUILDER = _REPO / "fixtures" / "build.py"


def _spec_files() -> list[Path]:
    return sorted(SPECS_DIR.glob("*.json"))


def _git(repo: Path, *args: str, check: bool = True,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    import os
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=check, env=full_env,
    )


def _build(spec_id: str, out_root: Path, force: bool = False) -> Path:
    cmd = [sys.executable, str(BUILDER), "--spec", spec_id, "--out", str(out_root)]
    if force:
        cmd.append("--force")
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return out_root / spec_id


@pytest.fixture(params=[p.stem for p in _spec_files()])
def spec(request) -> dict:
    loaded = json.loads((SPECS_DIR / f"{request.param}.json").read_text())
    if loaded.get("id") != request.param:
        pytest.fail(
            f"{request.param}.json: spec id {loaded.get('id')!r} does not match "
            "its filename (build.py looks specs up by filename stem)")
    return loaded


# ---------------------------------------------------------------------------
# Spec completeness (the oracle tuple the project contract requires)
# ---------------------------------------------------------------------------

def test_spec_carries_full_oracle_tuple(spec):
    for field in ("id", "title", "path", "language", "base", "current",
                  "replayed", "expected_resolved", "acceptable_alternatives",
                  "expected_conflict_hunks", "validation", "notes"):
        assert field in spec, f"{spec.get('id')}: missing '{field}'"
    sides = {spec["base"], spec["current"], spec["replayed"]}
    assert len(sides) == 3, f"{spec['id']}: base/current/replayed must all differ"
    assert spec["expected_resolved"] not in sides, (
        f"{spec['id']}: expected_resolved must be a real merge, not one input")
    for alt in spec["acceptable_alternatives"]:
        # Equaling one side is fine (a side-take is a defensible resolution);
        # equaling base would undo both sides and is not a resolution.
        assert alt != spec["base"], f"{spec['id']}: alternative reverts to base"
        assert alt != spec["expected_resolved"], (
            f"{spec['id']}: alternative duplicates expected_resolved")


def test_every_fixture_has_a_spec_and_vice_versa():
    specs = {p.stem for p in _spec_files()}
    assert specs == {"text-uu-simple", "python-uu", "settings-uu", "rust-uu"}, (
        f"unexpected fixture spec set: {specs}")


# ---------------------------------------------------------------------------
# Builder determinism: same spec -> identical OIDs (reproducibility is the
# point; the old submodule's history was machine-bound)
# ---------------------------------------------------------------------------

def test_builder_is_deterministic(tmp_path):
    a = _build("rust-uu", tmp_path / "a")
    b = _build("rust-uu", tmp_path / "b")
    for branch in ("base", "current", "replayed"):
        oid_a = _git(a, "rev-parse", branch).stdout.strip()
        oid_b = _git(b, "rev-parse", branch).stdout.strip()
        assert oid_a == oid_b, (
            f"{branch} OIDs differ across builds: {oid_a} != {oid_b}")


def test_builder_skips_when_spec_sha_matches(tmp_path):
    out = _build("python-uu", tmp_path)
    r = subprocess.run(
        [sys.executable, str(BUILDER), "--spec", "python-uu", "--out", str(tmp_path)],
        capture_output=True, text=True)
    assert r.returncode == 0
    assert "up to date" in r.stdout
    assert out.is_dir()


# ---------------------------------------------------------------------------
# Conflict reproduction: a plain rebase in the built repo produces the
# pinned UU file and hunk count
# ---------------------------------------------------------------------------

def test_rebase_reproduces_pinned_conflict(tmp_path, spec):
    repo = _build(spec["id"], tmp_path)
    _git(repo, "checkout", "replayed")
    r = _git(repo, "rebase", "current", check=False)
    assert r.returncode != 0, f"{spec['id']}: rebase did not conflict"
    unmerged = _git(repo, "diff", "--name-only", "--diff-filter=U").stdout.split()
    assert unmerged == [spec["path"]], (
        f"{spec['id']}: UU paths {unmerged} != [{spec['path']}]")
    hunks = (repo / spec["path"]).read_text().count("<<<<<<<")
    assert hunks == spec["expected_conflict_hunks"], (
        f"{spec['id']}: {hunks} conflict hunks, spec pins "
        f"{spec['expected_conflict_hunks']}")
    _git(repo, "rebase", "--abort", check=False)


# ---------------------------------------------------------------------------
# Resolution validity: expected + alternatives complete the rebase and pass
# the spec's validation commands
# ---------------------------------------------------------------------------

def _validation_env(repo: Path, tmp: Path) -> dict[str, str]:
    return {
        "{python}": sys.executable,
        "{tmpdir}": str(tmp),
        "{repo}": str(repo),
    }


def _run_validation(spec, repo: Path, tmp: Path, label: str) -> None:
    tokens = _validation_env(repo, tmp)
    for cmd in spec["validation"]:
        filled = cmd
        for token, value in tokens.items():
            filled = filled.replace(token, value)
        argv = filled.split()
        if shutil.which(argv[0]) is None:
            pytest.skip(f"{spec['id']}: validator {argv[0]!r} not installed")
        r = subprocess.run(argv, cwd=repo, capture_output=True, text=True)
        assert r.returncode == 0, (
            f"{spec['id']} {label}: validation {cmd!r} failed:\n{r.stderr[-400:]}")


def test_expected_and_alternative_resolutions_are_valid(tmp_path, spec):
    candidates = [("expected", spec["expected_resolved"])] + [
        (f"alternative[{i}]", alt)
        for i, alt in enumerate(spec["acceptable_alternatives"])
    ]
    scratch = tmp_path / "val"
    scratch.mkdir()
    for i, (label, content) in enumerate(candidates):
        # Fresh repo per candidate: a completed rebase --continue advances
        # the replayed branch past the conflict, so it cannot be reused.
        repo = _build(spec["id"], tmp_path / f"cand{i}", force=True)
        _git(repo, "checkout", "replayed")
        _git(repo, "rebase", "current", check=False)
        (repo / spec["path"]).write_text(content)
        _git(repo, "add", spec["path"])
        r = _git(repo, "rebase", "--continue", check=False, env={
            "GIT_EDITOR": "true",
            # rebase --continue creates a commit; CI runners have no identity
            "GIT_AUTHOR_NAME": "capybase fixture test",
            "GIT_AUTHOR_EMAIL": "fixture@capybase.invalid",
            "GIT_COMMITTER_NAME": "capybase fixture test",
            "GIT_COMMITTER_EMAIL": "fixture@capybase.invalid",
        })
        assert r.returncode == 0, (
            f"{spec['id']} {label}: rebase --continue failed:\n"
            f"{r.stderr[-400:]}")
        final = (repo / spec["path"]).read_text()
        assert final == content, f"{spec['id']} {label}: committed text differs"
        _run_validation(spec, repo, scratch, label)


# ---------------------------------------------------------------------------
# Tracked-state hygiene: no machine-local path may re-enter fixture state
# (the rule the old file:/// submodule violated)
# ---------------------------------------------------------------------------

def test_no_local_paths_in_tracked_fixture_state():
    forbidden = ("file://", "/w/", "/home/", "/tmp/", "capybase-fixtures.git",
                 "C:\\")
    offenders: list[str] = []
    tracked = subprocess.run(
        ["git", "-C", str(_REPO), "ls-files", "fixtures/"],
        capture_output=True, text=True, check=True).stdout.split()
    assert tracked, "fixtures/ must contribute tracked files (specs + builder)"
    for rel in tracked:
        text = (_REPO / rel).read_text(errors="replace")
        for needle in forbidden:
            if needle in text:
                offenders.append(f"{rel}: contains {needle!r}")
    assert not offenders, (
        "machine-local paths in tracked fixture state: " + "; ".join(offenders))


def test_fixtures_is_not_a_submodule():
    ls = subprocess.run(
        ["git", "-C", str(_REPO), "ls-files", "-s", "fixtures"],
        capture_output=True, text=True, check=True).stdout
    assert "160000" not in ls, (
        "fixtures is a gitlink again — submodules with machine-local URLs are "
        "banned here; extend fixtures/specs/ instead")
    assert not (_REPO / ".gitmodules").exists(), (
        ".gitmodules reappeared; a new submodule must use a public URL and a "
        "new guard exception")

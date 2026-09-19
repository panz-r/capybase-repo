#!/usr/bin/env python3
"""Deterministic fixture builder: spec JSON -> real git repos.

Every fixture is a declarative spec (fixtures/specs/<id>.json) carrying
its base content, both branch sides, the expected developer resolution,
acceptable alternatives, and validation commands. This script turns a
spec into a small git repo with three branches:

    base      the common ancestor (one commit)
    current   the rebase target, git "ours" / the upstream side
    replayed  the branch under rebase, git "theirs"

`git checkout replayed && git rebase current` in the built repo
reproduces the fixture's conflict (same file, same hunk count as
pinned by expected_conflict_hunks).

Determinism is the point: fixed author/committer identity and dates,
no hooks, no gpg signing — the same spec builds byte-identical commits
(identical OIDs) on any machine. A build is skipped when the target
already matches the spec's sha256; --force rebuilds.

Usage:
    python fixtures/build.py --all [--force] [--quiet]
    python fixtures/build.py --spec python-uu [--out DIR] [--force]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent
SPECS_DIR = FIXTURES_DIR / "specs"
DEFAULT_BUILT = FIXTURES_DIR / ".built"

# Fixed identity + date: the same spec must hash to the same commits
# everywhere (reproducible OIDs are part of the fixture contract).
GIT_ENV = {
    "GIT_AUTHOR_NAME": "capybase fixture",
    "GIT_AUTHOR_EMAIL": "fixture@capybase.invalid",
    "GIT_COMMITTER_NAME": "capybase fixture",
    "GIT_COMMITTER_EMAIL": "fixture@capybase.invalid",
    "GIT_AUTHOR_DATE": "2005-04-07T22:13:13+00:00",
    "GIT_COMMITTER_DATE": "2005-04-07T22:13:13+00:00",
}

REQUIRED_FIELDS = (
    "id", "title", "path", "language", "base", "current", "replayed",
    "expected_resolved", "expected_conflict_hunks", "validation", "notes",
)


def spec_sha(spec: dict) -> str:
    canonical = json.dumps(spec, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _run(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
        env={**os.environ, **GIT_ENV},
    )


def build_repo(spec: dict, out: Path) -> Path:
    """Build (or rebuild) the fixture repo for `spec` at `out`."""
    fid = spec["id"]
    out.mkdir(parents=True, exist_ok=True)
    hooks = out / ".no-hooks"
    hooks.mkdir(exist_ok=True)

    def init() -> None:
        cmd = ["git", "-C", str(out)]
        r = subprocess.run([*cmd, "init", "-b", "base"], capture_output=True, text=True)
        if r.returncode != 0:  # older git without --initial-branch
            subprocess.run([*cmd, "init"], check=True, capture_output=True)
            subprocess.run([*cmd, "symbolic-ref", "HEAD", "refs/heads/base"],
                           check=True, capture_output=True)
        _run(out, "config", "core.hooksPath", str(hooks))
        _run(out, "config", "commit.gpgsign", "false")

    def commit(branch: str, message: str, content: str) -> None:
        target = out / spec["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        _run(out, "add", spec["path"])
        _run(out, "commit", "-q", "--no-gpg-sign", "-m", message)

    init()
    commit("base", f"{fid}: base", spec["base"])
    _run(out, "checkout", "-q", "-b", "current")
    commit("current", f"{fid}: current (rebase target)", spec["current"])
    _run(out, "checkout", "-q", "-b", "replayed", "base")
    commit("replayed", f"{fid}: replayed", spec["replayed"])
    _run(out, "checkout", "-q", "base")

    (out / ".spec-sha").write_text(spec_sha(spec) + "\n")
    # The scratch commit-tree work is done; drop the hooks placeholder dir
    # contents so the built repo stays minimal.
    return out


def build(spec: dict, out_root: Path, force: bool, quiet: bool) -> Path:
    _require_valid(spec)
    out = out_root / spec["id"]
    marker = out / ".spec-sha"
    if not force and marker.is_file() and marker.read_text().strip() == spec_sha(spec):
        if not quiet:
            print(f"fixture {spec['id']}: up to date ({out})")
        return out
    if out.exists():
        shutil.rmtree(out)
    build_repo(spec, out)
    if not quiet:
        print(f"fixture {spec['id']}: built ({out})")
    return out


def _require_valid(spec: dict) -> None:
    missing = [f for f in REQUIRED_FIELDS if f not in spec]
    if missing:
        raise ValueError(f"spec {spec.get('id', '?')}: missing fields {missing}")
    sides = {spec["base"], spec["current"], spec["replayed"]}
    if len(sides) != 3:
        raise ValueError(f"spec {spec['id']}: base/current/replayed must all differ")
    if spec["expected_resolved"] in sides:
        raise ValueError(
            f"spec {spec['id']}: expected_resolved must differ from all three sides")
    if not isinstance(spec["expected_conflict_hunks"], int) or spec["expected_conflict_hunks"] < 1:
        raise ValueError(f"spec {spec['id']}: expected_conflict_hunks must be a positive int")


def load_spec(spec_id: str) -> dict:
    path = SPECS_DIR / f"{spec_id}.json"
    if not path.is_file():
        raise SystemExit(f"no fixture spec at {path}")
    return json.loads(path.read_text())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--spec", metavar="ID", help="build one fixture by spec id")
    g.add_argument("--all", action="store_true", help="build every spec in fixtures/specs")
    ap.add_argument("--out", type=Path, default=DEFAULT_BUILT,
                    help=f"output root (default {DEFAULT_BUILT})")
    ap.add_argument("--force", action="store_true",
                    help="rebuild even when the spec sha matches")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    ids = ([p.stem for p in sorted(SPECS_DIR.glob("*.json"))] if args.all
           else [args.spec])
    for fid in ids:
        build(load_spec(fid), args.out, args.force, args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())

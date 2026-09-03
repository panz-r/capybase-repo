"""Unit tests for corpus._realworld_build's era-aware build resolver.

Covers the (prepare, build) decision on synthetic trees (no dataset fetch
needed — that part is the corpus suite's own runner). The dir-probing
``resolve_c_build`` and the sha-probing ``resolve_c_build_at_sha`` must
agree on the same tree, and the executable-bit check on ``configure`` must
not crash on trees that HAVE a configure script (the paren regression:
``bool(path).stat()`` crashed the eval's materializer for every
configure-carrying C tree — sqlite cases failed setup instantly).
"""

from __future__ import annotations

import stat as stat_mod
import subprocess
from pathlib import Path

from corpus._realworld_build import (
    C_PREPARE_COMMANDS,
    resolve_c_build,
    resolve_c_build_at_sha,
)


def _make_tree(td: Path, *, configure: bool = False, executable: bool = True,
               configure_ac: bool = False, cmake: bool = False,
               makefile: bool = False) -> Path:
    tree = td / "tree"
    tree.mkdir()
    if cmake:
        (tree / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.5)\n")
    if configure_ac:
        (tree / "configure.ac").write_text("AC_INIT([x], [1])\n")
    if configure:
        f = tree / "configure"
        f.write_text("#!/bin/sh\n")
        mode = f.stat().st_mode
        f.chmod(mode | stat_mod.S_IXUSR if executable else mode & ~0o111)
    if makefile:
        (tree / "Makefile").write_text("all:\n\ttrue\n")
    return tree


def test_configure_tree_does_not_crash(tmp_path: Path):
    """The regression: a tree WITH a configure script crashed with
    ``AttributeError: 'bool' object has no attribute 'stat'``."""
    tree = _make_tree(tmp_path, configure=True)
    prepare, build = resolve_c_build(tree, "sqlite-history", "")
    assert build.startswith("make -j"), (prepare, build)


def test_preconfigured_branch(tmp_path: Path):
    """configure present + executable, no configure.ac → ./configure / make."""
    tree = _make_tree(tmp_path, configure=True, executable=True)
    _, build = resolve_c_build(tree, "x-history", "")
    assert build.startswith("make -j")


def test_non_executable_configure_falls_to_unknown(tmp_path: Path):
    """A non-executable configure (mode 0o644) is not a usable pre-configured
    tree — the detection must not crash and must not take the configure path."""
    tree = _make_tree(tmp_path, configure=True, executable=False)
    _, build = resolve_c_build(tree, "x-history", "")
    assert build == "true"  # unknown build system — honest decline


def test_sha_probe_agrees_with_dir_probe(tmp_path: Path):
    """resolve_c_build_at_sha must classify the same tree the dir probe does."""
    tree = _make_tree(tmp_path, configure_ac=True, configure=True)
    subprocess.run(["git", "init", "-q", str(tree)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(tree), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(
        ["git", "-C", str(tree), "commit", "-q", "-m", "t",
         "--author=t <t@t>", "--date=2005-01-01 +0000"],
        check=True, capture_output=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
             "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)})
    sha = subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True
                         ).stdout.strip()
    dir_pair = resolve_c_build(tree, "sqlite-history", "")
    sha_pair = resolve_c_build_at_sha(tree, sha, "sqlite-history", "")
    assert dir_pair == sha_pair, (dir_pair, sha_pair)
    assert "autoreconf" in sha_pair[0]  # configure.ac wins over pre-configured


def test_ready_makefile_prefers_verified_map(tmp_path: Path):
    """A ready-Makefile dataset with a C_BUILD_COMMANDS entry uses it verbatim
    (the era-verified stack — no cpu-scaled -j rewrite)."""
    tree = _make_tree(tmp_path, makefile=True)
    _, build = resolve_c_build(tree, "redis-history",
                               C_PREPARE_COMMANDS["redis-history"])
    assert build.startswith("make -j4")  # the verified form, not -j<cpu>


def test_empty_tree_honest_decline(tmp_path: Path):
    tree = _make_tree(tmp_path)
    assert resolve_c_build(tree, "x-history", "") == ("", "true")

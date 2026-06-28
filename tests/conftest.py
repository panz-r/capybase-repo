"""Shared pytest fixtures: temp git repos with synthetic rebase conflicts.

These build real, tiny git repositories in tmp_path, then drive a rebase into a
``UU`` (both-modified) conflict so git_backend/orchestrator can be tested
against genuine unmerged index state — not mocks.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from capybase.git_backend import GitBackend


def git(repo: Path, *args: str, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "tester"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "t@example.com"
    env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = "2000-01-01T00:00:00"
    env["GIT_PAGER"] = "cat"
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env,
        capture_output=True,
        text=True,
        input=input_text,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {args} failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    return proc


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An initialized git repo with identity configured."""
    git(tmp_path, "init", "-q", "-b", "main")
    return tmp_path


@pytest.fixture(autouse=True)
def _isolate_model_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the unit suite hermetic w.r.t. the model profile AND the config dir.

    "Profile wins" overlays a saved profile onto every Orchestrator whose model
    name matches. A profile saved on a developer's machine (``capybase
    calibrate``) or committed to the repo must NOT change how the tests behave —
    they use fake clients and canned responses, so tuned max_tokens/timeouts
    would make them pass or fail based on an artifact file rather than the code.

    Neuters ``ModelProfile.load`` globally for the suite: no profile can be
    loaded, so every Orchestrator sees the pure-config values. The original
    loader is stashed on the class so tests that EXERCISE the overlay can opt
    back in via the ``real_profile_loader`` fixture below. The real ``capybase
    run``/``inspect``/``manual`` commands are unaffected (they don't import here).

    Also redirects the capybase config dir (``XDG_CONFIG_HOME``) to a per-test
    tmp path, so any test that calls the CLI without ``--config`` writes its
    calibration/profile artifacts to an isolated dir, never the developer's real
    ``~/.config/capybase``. Tests that assert on a specific config dir still pass
    an explicit ``--config`` (which takes precedence over this env default).
    """
    import capybase.calibration_profile as cp

    # Redirect the config dir to an isolated per-test temp dir, so a CLI call
    # without --config writes calibration/profile artifacts there, never to the
    # developer's real ~/.config/capybase. default_config_dir() reads
    # XDG_CONFIG_HOME live, so this env override is sufficient.
    xdg = tmp_path / "xdg-config"
    xdg.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    if not getattr(cp.ModelProfile, "_real_load", None):
        cp.ModelProfile._real_load = cp.ModelProfile.load
    monkeypatch.setattr(cp.ModelProfile, "load", staticmethod(lambda path: None))


@pytest.fixture
def real_profile_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt IN to the real profile loader for tests that exercise the overlay
    (otherwise the autouse ``_isolate_model_profile`` makes load return None)."""
    import capybase.calibration_profile as cp

    monkeypatch.setattr(cp.ModelProfile, "load", cp.ModelProfile._real_load)


@pytest.fixture
def conflicted_repo(repo: Path) -> dict:
    """A repo stopped at a UU rebase conflict over ``app.py``.

    Layout:
      main  : BASE content
      feat  : diverges from main (REPLAYED commit)
      main  also diverges (CURRENT_UPSTREAM side)

    Replaying ``feat`` onto ``main`` yields a both-modified conflict.
    Returns paths + the ConflictSide texts used.
    """
    base = "def greet():\n    return 'hello'\n"
    upstream = "def greet():\n    return 'hi'\n"          # CURRENT_UPSTREAM_SIDE
    replayed = "def greet():\n    return 'howdy'\n"        # REPLAYED_COMMIT_SIDE

    (repo / "app.py").write_text(base)
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "base")

    # feat branch from base, edit -> replayed.
    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text(replayed)
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "replayed change")

    # switch to main, edit -> upstream (current side).
    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text(upstream)
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "upstream change")

    # Rebase feat onto main -> conflict.
    git(repo, "checkout", "-q", "feat")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected a rebase conflict"
    return {
        "repo": repo,
        "path": "app.py",
        "base": base,
        "current": upstream,
        "replayed": replayed,
    }


@pytest.fixture
def multi_unit_conflicted_repo(repo: Path) -> dict:
    """A repo stopped at a UU rebase conflict with TWO hunks in one file.

    Layout (mirrors the live ``settings-uu`` fixture): a single ``cfg.py``
    with two well-separated conflict regions — a services list and a feature
    flags dict — both modified on both sides such that git emits two distinct
    ``<<<<<<< ... >>>>>>>`` blocks. Replaying ``feat`` onto ``main`` yields a
    multi-unit-per-file conflict.

    Returns paths + the expected merged texts for each hunk.
    """
    base = (
        'ENABLED_SERVICES = ["core", "cli"]\n'
        "\n\n"
        'class ServiceConfig:\n    name = "capybase"\n'
        "\n\n"
        'FEATURE_FLAGS = {\n    "cache": "off",\n    "metrics": "off",\n}\n'
    )
    upstream = (
        'ENABLED_SERVICES = ["core", "cli", "scheduler"]\n'
        "\n\n"
        'class ServiceConfig:\n    name = "capybase"\n'
        "\n\n"
        'FEATURE_FLAGS = {\n    "cache": "off",\n    "metrics": "on",\n}\n'
    )
    replayed = (
        'ENABLED_SERVICES = ["core", "cli", "reloader"]\n'
        "\n\n"
        'class ServiceConfig:\n    name = "capybase"\n'
        "\n\n"
        'FEATURE_FLAGS = {\n    "cache": "on",\n    "metrics": "off",\n}\n'
    )

    (repo / "cfg.py").write_text(base)
    git(repo, "add", "cfg.py")
    git(repo, "commit", "-q", "-m", "base")

    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / "cfg.py").write_text(replayed)
    git(repo, "add", "cfg.py")
    git(repo, "commit", "-q", "-m", "replayed changes")

    git(repo, "checkout", "-q", "main")
    (repo / "cfg.py").write_text(upstream)
    git(repo, "add", "cfg.py")
    git(repo, "commit", "-q", "-m", "upstream changes")

    git(repo, "checkout", "-q", "feat")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected a rebase conflict"
    return {
        "repo": repo,
        "path": "cfg.py",
        "base": base,
        "current": upstream,
        "replayed": replayed,
        # Sensible merges the model/human would produce (combine both sides).
        # These are the *block-interior* resolved texts — exactly what replaces
        # the marker span. The services conflict covers only the assignment
        # line; the flags conflict covers only the two dict-entry lines (the
        # surrounding ``FEATURE_FLAGS = {`` and ``}``` are outside the span).
        "services_merged": 'ENABLED_SERVICES = ["core", "cli", "scheduler", "reloader"]',
        "flags_merged": '    "cache": "on",\n    "metrics": "on"',
    }


@pytest.fixture
def git_backend(repo: Path) -> GitBackend:
    return GitBackend(repo)


@pytest.fixture
def rust_conflicted_repo(repo: Path) -> dict:
    """A repo stopped at a UU rebase conflict over ``src/config.rs``.

    Mirrors the live ``rust-uu`` fixture: a Rust ``impl Config`` block where
    the replayed branch adds a ``timeout_ms`` field (struct def + ``new()``
    initializer + ``label()`` format string) while upstream changes the retry
    count and brackets the name. Replaying ``feat`` onto ``main`` yields a
    both-modified conflict with multiple hunks landing inside one ``impl``,
    exercising the tree-sitter Rust grammar, the ``rustc`` compile floor, and
    multi-unit splice validation.

    Returns paths + the expected correct merged file content.
    """
    base = (
        "pub struct Config {\n"
        '    pub name: String,\n'
        "    pub max_retries: u32,\n"
        "}\n"
        "\n"
        "impl Config {\n"
        "    pub fn new() -> Self {\n"
        "        Config {\n"
        '            name: "capybase".to_string(),\n'
        "            max_retries: 3,\n"
        "        }\n"
        "    }\n"
        "\n"
        "    pub fn label(&self) -> String {\n"
        '        format!("{} (retries={})", self.name, self.max_retries)\n'
        "    }\n"
        "}\n"
    )
    # Upstream (CURRENT): bump retries to 5, bracket the name in label().
    upstream = (
        "pub struct Config {\n"
        '    pub name: String,\n'
        "    pub max_retries: u32,\n"
        "}\n"
        "\n"
        "impl Config {\n"
        "    pub fn new() -> Self {\n"
        "        Config {\n"
        '            name: "capybase".to_string(),\n'
        "            max_retries: 5,\n"
        "        }\n"
        "    }\n"
        "\n"
        "    pub fn label(&self) -> String {\n"
        '        format!("[{}] retries={}", self.name, self.max_retries)\n'
        "    }\n"
        "}\n"
    )
    # Replayed: add timeout_ms field (struct + init + label format).
    replayed = (
        "pub struct Config {\n"
        '    pub name: String,\n'
        "    pub max_retries: u32,\n"
        "    pub timeout_ms: u32,\n"
        "}\n"
        "\n"
        "impl Config {\n"
        "    pub fn new() -> Self {\n"
        "        Config {\n"
        '            name: "capybase".to_string(),\n'
        "            max_retries: 3,\n"
        "            timeout_ms: 10000,\n"
        "        }\n"
        "    }\n"
        "\n"
        "    pub fn label(&self) -> String {\n"
        '        format!("{} (retries={}, timeout={})", self.name, '
        "self.max_retries, self.timeout_ms)\n"
        "    }\n"
        "}\n"
    )
    # The correct merge: keep retries=5, add timeout_ms everywhere.
    correct = (
        "pub struct Config {\n"
        '    pub name: String,\n'
        "    pub max_retries: u32,\n"
        "    pub timeout_ms: u32,\n"
        "}\n"
        "\n"
        "impl Config {\n"
        "    pub fn new() -> Self {\n"
        "        Config {\n"
        '            name: "capybase".to_string(),\n'
        "            max_retries: 5,\n"
        "            timeout_ms: 10000,\n"
        "        }\n"
        "    }\n"
        "\n"
        "    pub fn label(&self) -> String {\n"
        '        format!("[{}] (retries={}, timeout={})", self.name, '
        "self.max_retries, self.timeout_ms)\n"
        "    }\n"
        "}\n"
    )

    (repo / "src").mkdir()
    (repo / "src" / "config.rs").write_text(base)
    git(repo, "add", "src/config.rs")
    git(repo, "commit", "-q", "-m", "base")

    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / "src" / "config.rs").write_text(replayed)
    git(repo, "add", "src/config.rs")
    git(repo, "commit", "-q", "-m", "replayed: add timeout_ms")

    git(repo, "checkout", "-q", "main")
    (repo / "src" / "config.rs").write_text(upstream)
    git(repo, "add", "src/config.rs")
    git(repo, "commit", "-q", "-m", "upstream: raise retries")

    git(repo, "checkout", "-q", "feat")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected a rebase conflict"
    return {
        "repo": repo,
        "path": "src/config.rs",
        "base": base,
        "current": upstream,
        "replayed": replayed,
        "correct": correct,
    }


@pytest.fixture
def rust_multi_file_conflicted_repo(repo: Path) -> dict:
    """A repo stopped at UU conflicts in TWO Rust files of one crate.

    Exercises cross-file (whole-crate) verification: a rebase that stops with
    conflicts in BOTH ``src/config.rs`` and ``src/server.rs`` at once. Each
    file uses ``crate::`` paths, so a per-file ``cargo check`` fails while the
    sibling still holds raw ``<<<<<<<`` markers (``error: encountered diff
    marker``). This is the fixture behind the cross-file batch verification
    fix: all files must be resolved and written before the crate-wide check.

    To produce a *genuine* conflict in two files at once (git auto-merges
    edits to different non-overlapping regions), both branches edit the SAME
    line in each file to different values: config.rs's default port, and
    server.rs's log label — both diverging on the same line from a common base.

    Returns paths + the correct per-file merges.
    """
    base_lib = "pub mod config;\npub mod server;\n"
    base_config = (
        "pub struct Config { pub port: u16 }\n"
        "impl Config {\n"
        "    pub fn new() -> Self { Config { port: 8080 } }\n"
        "}\n"
    )
    base_server = (
        "use crate::config::Config;\n"
        "pub fn label(c: &Config) -> String { format!(\"port={}\", c.port) }\n"
    )
    # Both sides change the SAME line in config.rs (port default) to different
    # values, AND the SAME line in server.rs (label format) to different values.
    # That yields a genuine both-modified conflict in EACH file simultaneously.
    up_config = base_config.replace("port: 8080 }", "port: 9090 }")
    rep_config = base_config.replace("port: 8080 }", "port: 7070 }")
    up_server = base_server.replace('format!("port={}"', 'format!("[port]={}"')
    rep_server = base_server.replace('format!("port={}"', 'format!("PORT={}"')
    # Correct merges: config.rs takes the higher port; server.rs combines both
    # label styles into one coherent string.
    correct_config = up_config.replace("port: 9090 }", "port: 9090 }")
    correct_server = (
        "pub fn label(c: &Config) -> String { format!(\"[PORT]={}\", c.port) }"
    )

    (repo / "src").mkdir()
    (repo / "src" / "lib.rs").write_text(base_lib)
    (repo / "src" / "config.rs").write_text(base_config)
    (repo / "src" / "server.rs").write_text(base_server)
    (repo / "Cargo.toml").write_text(
        '[package]\nname = "multifile"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")

    # feat branch: change the port default AND the label format (the replayed side).
    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / "src" / "config.rs").write_text(rep_config)
    (repo / "src" / "server.rs").write_text(rep_server)
    git(repo, "add", "src/config.rs", "src/server.rs")
    git(repo, "commit", "-q", "-m", "feat: port 7070 + PORT= label")

    # main branch: change the same two lines differently (the upstream side).
    git(repo, "checkout", "-q", "main")
    (repo / "src" / "config.rs").write_text(up_config)
    (repo / "src" / "server.rs").write_text(up_server)
    git(repo, "add", "src/config.rs", "src/server.rs")
    git(repo, "commit", "-q", "-m", "main: port 9090 + [port]= label")

    git(repo, "checkout", "-q", "feat")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected a rebase conflict"
    # Sanity: both files should be unmerged.
    unmerged = git(repo, "diff", "--name-only", "--diff-filter=U").stdout.split()
    assert "src/config.rs" in unmerged and "src/server.rs" in unmerged
    return {
        "repo": repo,
        "paths": ["src/config.rs", "src/server.rs"],
        "correct_config": correct_config,
        "correct_server": correct_server,
    }


@pytest.fixture
def rust_test_gated_repo(repo: Path) -> dict:
    """A cargo crate whose ``#[cfg(test)]`` test guards the resolved value.

    Drives the orchestrator's test gate (``_run_tests`` → ``cargo test``) with a
    REAL failing assertion rather than a ``false`` shim. The crate has a
    ``Config`` with a ``port`` field and a test asserting ``port == 9090``. The
    rebase conflict is the ``new()`` default port: base 8080, upstream 9090,
    replayed 7070. A correct merge keeps 9090 (compiles AND the test passes); a
    wrong merge keeps 7070 (still compiles, but the test fails — the "compiles
    but a test fails" scenario the compile floor alone can't catch).

    This is the first end-to-end proof that the Rust pipeline's *test* gate
    works, and covers the "intent preservation via the project's own test suite"
    axis. Requires cargo (the gate runs ``cargo test``).
    """
    base = (
        "pub struct Config {\n    pub port: u16,\n}\n"
        "impl Config {\n    pub fn new() -> Self { Config { port: 8080 } }\n}\n"
        "\n"
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    use super::*;\n"
        "    #[test]\n"
        "    fn port_is_9090() {\n"
        "        let c = Config::new();\n"
        "        assert_eq!(c.port, 9090);\n"
        "    }\n"
        "}\n"
    )
    # Upstream (CURRENT): bump the default port to 9090 (what the test expects).
    upstream = base.replace("port: 8080 }", "port: 9090 }")
    # Replayed: set the default port to 7070 (diverges on the same line).
    replayed = base.replace("port: 8080 }", "port: 7070 }")
    # Correct merge: keep upstream's 9090 (compiles, test passes).
    correct = "    pub fn new() -> Self { Config { port: 9090 } }"
    # Wrong merge: keep replayed's 7070 (compiles, but the test fails).
    wrong = "    pub fn new() -> Self { Config { port: 7070 } }"

    (repo / "src").mkdir()
    (repo / "src" / "lib.rs").write_text(base)
    (repo / "Cargo.toml").write_text(
        '[package]\nname = "testgated"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")

    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / "src" / "lib.rs").write_text(replayed)
    git(repo, "add", "src/lib.rs")
    git(repo, "commit", "-q", "-m", "feat: port 7070")

    git(repo, "checkout", "-q", "main")
    (repo / "src" / "lib.rs").write_text(upstream)
    git(repo, "add", "src/lib.rs")
    git(repo, "commit", "-q", "-m", "main: port 9090")

    git(repo, "checkout", "-q", "feat")
    r = git(repo, "rebase", "main", check=False)
    assert r.returncode != 0, "expected a rebase conflict"
    return {
        "repo": repo,
        "path": "src/lib.rs",
        "correct": correct,
        "wrong": wrong,
    }


# ---------------------------------------------------------------------------
# ``rebase``-command fixtures: repos BEFORE the rebase, ready for capybase to
# start it itself. These mirror conflicted_repo / rust_conflicted_repo but stop
# short of running ``git rebase main`` — the repo is left on the feature branch
# with a clean worktree, so ``orch.rebase("main")`` owns the start.
# ---------------------------------------------------------------------------


@pytest.fixture
def py_repo_before_rebase(repo: Path) -> dict:
    """A repo on ``feat`` ready to rebase onto ``main`` (clean, no rebase yet).

    Layout (mirrors ``conflicted_repo`` minus the final rebase):
      main  : BASE content (``return 'hello'``)
      feat  : diverged (``return 'howdy'``) — the REPLAYED side
      main  : also diverged (``return 'hi'``) — the CURRENT side
    Replaying ``feat`` onto ``main`` WILL conflict, but capybase starts it.

    Returns the repo, the conflicted path, the three sides, and the resolving
    merge text the test's fake LLM returns.
    """
    base = "def greet():\n    return 'hello'\n"
    upstream = "def greet():\n    return 'hi'\n"          # CURRENT_UPSTREAM_SIDE
    replayed = "def greet():\n    return 'howdy'\n"        # REPLAYED_COMMIT_SIDE
    merged = "def greet():\n    return 'hi' + 'howdy'\n"   # a resolving merge

    (repo / "app.py").write_text(base)
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "base")

    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text(replayed)
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "replayed change")

    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text(upstream)
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "upstream change")

    # Back on feat, clean — capybase will rebase onto main.
    git(repo, "checkout", "-q", "feat")
    return {
        "repo": repo,
        "path": "app.py",
        "base": base,
        "current": upstream,
        "replayed": replayed,
        "merged": merged,
        "merged_block": "    return 'hi' + 'howdy'",
    }


@pytest.fixture
def py_repo_clean_rebase(repo: Path) -> Path:
    """A repo where rebasing ``feat`` onto ``main`` is CLEAN (no conflict).

    feat and main touch disjoint files, so ``git rebase main`` succeeds without
    stopping. Used to exercise capybase's "rebase started, no conflict, finished
    immediately" happy path.
    """
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "base")

    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / "b.txt").write_text("b\n")
    git(repo, "add", "b.txt")
    git(repo, "commit", "-q", "-m", "feat: add b")

    git(repo, "checkout", "-q", "main")
    (repo / "c.txt").write_text("c\n")
    git(repo, "add", "c.txt")
    git(repo, "commit", "-q", "-m", "main: add c")

    git(repo, "checkout", "-q", "feat")
    return repo


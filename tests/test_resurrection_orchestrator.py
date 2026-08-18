"""Integration tests for the orchestrator's silent-resurrection stop.

The end-of-rebase scan runs after a clean rebase and compares the result against
content the target branch deleted. These build a repo whose trees express a
resurrection (target deleted a block; the result re-added it) and assert that
the orchestrator's scan finds it and the ``stop`` policy halts before declaring
success. They exercise the orchestrator methods directly (the scan + handler),
decoupled from whether a particular ``git rebase`` happens to auto-resolve
cleanly — git's diff3 heuristics are inconsistent there, which is exactly why a
dedicated scan is needed.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from capybase.config import Config
from capybase.orchestrator import Orchestrator
from capybase.session import SessionPaths

from tests.conftest import git


def _make_resurrection_repo(repo: Path) -> dict:
    """A repo whose trees express a resurrection (decoupled from git rebase).

      base   : app.py with dead()
      main   : deletes dead() (the cleanup) — the deletion intent
      result : a commit (off base) that keeps dead() — the resurrected tree
    """
    base = (
        "def useful():\n    return 1\n\n"
        "def dead():\n    do_thing()\n    cleanup()\n\n"
    )
    (repo / "app.py").write_text(base)
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "base")
    base_oid = git(repo, "rev-parse", "HEAD").stdout.strip()

    git(repo, "branch", "feat")  # keep base reachable for merge-base
    (repo / "app.py").write_text("def useful():\n    return 1\n\n")
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "main: delete dead() cleanup")
    main_oid = git(repo, "rev-parse", "HEAD").stdout.strip()

    # The replayed branch ALSO deletes dead() — so when the result tree
    # re-adds it, the resurrection is explained by NEITHER side. (With the
    # old shape the replayed branch still carried dead(), and the
    # provenance downgrade correctly defuses the finding as replayed's
    # explicit content — the stop policy never fired.)
    git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text("def useful():\n    return 1\n\n# replayed edit\n")
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "replayed: delete dead() too + edit")
    start_oid = git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "app.py").write_text(base + "# replayed edit\n")
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "result: resurrects dead()")
    result_oid = git(repo, "rev-parse", "HEAD").stdout.strip()

    return {
        "base_oid": start_oid, "onto_oid": main_oid, "result_oid": result_oid,
    }


def _orch(repo: Path, *, policy: str = "stop") -> Orchestrator:
    cfg = Config()
    cfg.validation.enable_resurrection_detection = True
    cfg.validation.resurrection_policy = policy
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    orch.paths = SessionPaths("t", repo_root=repo)
    return orch


def test_resurrection_scan_finds_the_resurrection():
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        git(repo, "init", "-q", "-b", "main")
        ctx = _make_resurrection_repo(repo)
        orch = _orch(repo)
        findings = orch._resurrection_scan(
            start_oid=ctx["base_oid"],
            onto_oid=ctx["onto_oid"],
            result_oid=ctx["result_oid"],
            backup_ref="capybase/backup/x",
        )
        # The genuine-resurrection fixture (dead() deleted by BOTH sides,
        # re-added by the result) can yield multiple adjacent blocks on the
        # one path — all findings are app.py blocks from the dead() cleanup.
        assert findings
        assert all(f.path == "app.py" for f in findings)
        assert any("delete dead()" in f.deleting_commit for f in findings)


def test_stop_policy_escalates_and_writes_bundle():
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        git(repo, "init", "-q", "-b", "main")
        ctx = _make_resurrection_repo(repo)
        orch = _orch(repo, policy="stop")
        findings = orch._resurrection_scan(
            start_oid=ctx["base_oid"], onto_oid=ctx["onto_oid"],
            result_oid=ctx["result_oid"], backup_ref="capybase/backup/x",
        )
        result = orch._handle_resurrections(
            findings, start_oid=ctx["base_oid"], backup_ref="capybase/backup/x"
        )
        assert result.escalated
        assert "resurrection" in (result.reason or "")
        # A review bundle with the suspected-resurrections section was written.
        bundle = orch.paths.final / "review-bundle.md"
        assert bundle.exists()
        text = bundle.read_text()
        assert "suspected resurrections" in text
        assert "app.py" in text
        assert "delete dead()" in text


def test_warn_policy_does_not_escalate():
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        git(repo, "init", "-q", "-b", "main")
        ctx = _make_resurrection_repo(repo)
        orch = _orch(repo, policy="warn")
        findings = orch._resurrection_scan(
            start_oid=ctx["base_oid"], onto_oid=ctx["onto_oid"],
            result_oid=ctx["result_oid"], backup_ref="capybase/backup/x",
        )
        result = orch._handle_resurrections(
            findings, start_oid=ctx["base_oid"], backup_ref="capybase/backup/x"
        )
        assert not result.escalated  # warn continues
        # Bundle still written for post-hoc review.
        bundle = orch.paths.final / "review-bundle.md"
        assert bundle.exists()


def test_scan_disabled_when_feature_off():
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        git(repo, "init", "-q", "-b", "main")
        ctx = _make_resurrection_repo(repo)
        cfg = Config()
        cfg.validation.enable_resurrection_detection = False
        orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
        findings = orch._resurrection_scan(
            start_oid=ctx["base_oid"], onto_oid=ctx["onto_oid"],
            result_oid=ctx["result_oid"], backup_ref="capybase/backup/x",
        )
        assert findings == []


def test_lockfiles_are_exempt_from_resurrection_scanning():
    """Cargo.lock/go.sum 'resurrections' are dependency entries reappearing
    after a version bump — mechanical merge noise, not silently-undone code
    deletion (axum-0017: 103-marker Cargo.lock conflict SAFE_STOPped the
    rebase on 143 lines of version pins). The scan must not produce findings
    for a lockfile, while the SAME content shape in a code file still does.
    """
    with tempfile.TemporaryDirectory() as d:
        # Build the genuine-resurrection shape twice: as Cargo.lock
        # (exempt) and as app.py (flagged).
        for fname, expect_findings in (("Cargo.lock", False), ("app.py", True)):
            repo = Path(d) / fname
            repo.mkdir(parents=True)
            git(repo, "init", "-q", "-b", "main")
            base = (
                "[[package]]\nname = \"a\"\n"
                "def dead():\n    do_thing()\n    cleanup()\n" * 3
            )
            (repo / fname).write_text(base)
            git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "base")
            base_oid = git(repo, "rev-parse", "HEAD").stdout.strip()
            git(repo, "branch", "feat")
            (repo / fname).write_text("[[package]]\nname = \"a\"\n" * 3)
            git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "main: delete")
            main_oid = git(repo, "rev-parse", "HEAD").stdout.strip()
            git(repo, "checkout", "-q", "feat")
            (repo / fname).write_text(base.replace("cleanup()", "cleanup2()"))
            git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "replayed: delete too + edit")
            start_oid = git(repo, "rev-parse", "HEAD").stdout.strip()
            (repo / fname).write_text(base)  # resurrect dead() wholesale
            git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "result: resurrect")
            result_oid = git(repo, "rev-parse", "HEAD").stdout.strip()
            cfg = Config()
            cfg.validation.enable_resurrection_detection = True
            cfg.validation.resurrection_policy = "stop"
            orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
            orch.paths = SessionPaths("t", repo_root=repo)
            findings = orch._resurrection_scan(
                start_oid=start_oid, onto_oid=main_oid,
                result_oid=result_oid, backup_ref="capybase/backup/x",
            )
            assert bool(findings) is expect_findings, (
                f"{fname}: expected findings={expect_findings}, got {findings}")

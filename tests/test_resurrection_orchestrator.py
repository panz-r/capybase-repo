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

    git(repo, "checkout", "-q", "feat")
    (repo / "app.py").write_text(base + "# replayed edit\n")
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "result: keeps dead() + replayed edit")
    result_oid = git(repo, "rev-parse", "HEAD").stdout.strip()

    return {
        "base_oid": base_oid, "onto_oid": main_oid, "result_oid": result_oid,
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
        assert len(findings) == 1
        assert findings[0].path == "app.py"
        assert "delete dead()" in findings[0].deleting_commit


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

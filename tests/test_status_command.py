"""Tests for ``capybase status`` — the read-only session/backup report.

``status`` finds the latest (or ``--session``) session under
``.rebase-agent/sessions/``, reads its journal, and reports the outcome
(completed / escalated / stopped) plus any backup branches. It never mutates.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from capybase.cli import _run_status
from capybase.config import Config

from tests.conftest import git


def _cfg() -> Config:
    cfg = Config()
    cfg.model.model = "fake"
    return cfg


def _write_session(repo: Path, session_id: str, events: list[dict]) -> Path:
    """Write a journal for a synthetic session and return its path."""
    from capybase.session import SESSIONS_DIR

    sdir = repo / SESSIONS_DIR / session_id
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "final").mkdir(exist_ok=True)
    jpath = sdir / "journal.jsonl"
    seq = 0
    for ev in events:
        seq += 1
        record = {
            "seq": seq,
            "timestamp": "2026-01-01T00:00:00Z",
            "session_id": session_id,
            "event_type": ev["event_type"],
            "git_head_before": "",
            "git_head_after": "",
            "step_index": ev.get("step_index", 0),
            "path": "",
            "unit_id": "",
            "payload": ev.get("payload", {}),
        }
        jpath.write_text(jpath.read_text() + json.dumps(record) + "\n"
                         if jpath.exists() else json.dumps(record) + "\n")
    return jpath


def _init_repo(repo: Path) -> str:
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def test_status_no_sessions(repo: Path):
    _init_repo(repo)
    buf = io.StringIO()
    rc = _run_status(_cfg(), repo=str(repo), out=buf)
    text = buf.getvalue()
    assert rc == 0
    assert "no capybase sessions" in text
    assert "op in progress: none" in text


def test_status_completed_session(repo: Path):
    oid = _init_repo(repo)
    _write_session(repo, "abc123", [
        {"event_type": "rebase_started", "payload": {"target": "main", "backup_ref": "refs/heads/capybase/backup/main@x"}},
        {"event_type": "session_completed", "payload": {"head_after": oid}},
    ])
    buf = io.StringIO()
    rc = _run_status(_cfg(), repo=str(repo), out=buf)
    text = buf.getvalue()
    assert rc == 0
    assert "abc123" in text
    assert "completed" in text
    assert "main" in text  # target


def test_status_escalated_session(repo: Path):
    oid = _init_repo(repo)
    _write_session(repo, "xyz789", [
        {"event_type": "rebase_started", "payload": {"target": "main", "backup_ref": "refs/heads/capybase/backup/main@y"}},
        {"event_type": "escalated", "payload": {"reason": "could not resolve app.py:1:0"}},
        {"event_type": "rebase_aborted", "payload": {"reason": "could not resolve app.py:1:0", "backup_ref": "refs/heads/capybase/backup/main@y"}},
    ])
    buf = io.StringIO()
    rc = _run_status(_cfg(), repo=str(repo), out=buf)
    text = buf.getvalue()
    assert rc == 0
    assert "escalated" in text
    assert "could not resolve" in text
    assert "capybase/backup/main@y" in text


def test_status_lists_backup_branches(repo: Path):
    oid = _init_repo(repo)
    git(repo, "branch", "capybase/backup/old@20260101-000000", oid)
    buf = io.StringIO()
    rc = _run_status(_cfg(), repo=str(repo), out=buf)
    text = buf.getvalue()
    assert rc == 0
    assert "backup branches" in text
    assert "capybase/backup/old@20260101-000000" in text
    assert "git branch -D" in text  # delete hint


def test_status_specific_session(repo: Path):
    oid = _init_repo(repo)
    _write_session(repo, "newer", [{"event_type": "rebase_started", "payload": {"target": "trunk"}}])
    _write_session(repo, "older", [{"event_type": "rebase_started", "payload": {"target": "main"}}])
    # Ask for the older one explicitly even though newer is latest.
    buf = io.StringIO()
    rc = _run_status(_cfg(), repo=str(repo), session_id="older", out=buf)
    text = buf.getvalue()
    assert rc == 0
    assert "older" in text
    assert "main" in text


def test_status_reports_in_progress_operation(repo: Path):
    _init_repo(repo)
    git(repo, "branch", "feat")
    git(repo, "checkout", "-q", "feat")
    (repo / "a.txt").write_text("feat\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "feat")
    git(repo, "checkout", "-q", "main")
    (repo / "a.txt").write_text("main\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "main")
    feat = git(repo, "rev-parse", "feat").stdout.strip()
    git(repo, "cherry-pick", feat, check=False)  # conflicts → cherry-pick in progress

    buf = io.StringIO()
    rc = _run_status(_cfg(), repo=str(repo), out=buf)
    text = buf.getvalue()
    assert "cherry-pick" in text
    git(repo, "cherry-pick", "--abort", check=False)

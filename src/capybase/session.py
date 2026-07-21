"""Session identity and on-disk artifact layout.

A session owns a unique id, git refs under ``refs/rebase-agent/<id>/``, and a
directory tree under ``.rebase-agent/sessions/<id>/`` where the journal and
all artifacts live. Layout matches the spec so journals double as future
training/eval data.
"""

from __future__ import annotations

import uuid
from pathlib import Path

ARTIFACT_ROOT = Path(".rebase-agent")
SESSIONS_DIR = ARTIFACT_ROOT / "sessions"


def new_session_id() -> str:
    # Short, sortable, unique enough for local refs.
    return uuid.uuid4().hex[:12]


class SessionPaths:
    """Filesystem locations for one session's artifacts."""

    def __init__(self, session_id: str, repo_root: str | Path = ".") -> None:
        self.session_id = session_id
        self.repo_root = Path(repo_root).resolve()
        self.root = self.repo_root / SESSIONS_DIR / session_id
        self.journal = self.root / "journal.jsonl"
        self.config_copy = self.root / "config.toml"
        self.prompts = self.root / "prompts"
        self.responses = self.root / "responses"
        self.snapshots = self.root / "snapshots"
        self.candidates = self.root / "candidates"
        self.validations = self.root / "validations"
        self.final = self.root / "final"
        # Phase 4 flight recorder: content-addressed comment-pass artifacts
        # (prompt, response, ledger, frontier, candidate before/after, frozen
        # code + fingerprint, structured verifier results, jury evidence
        # packets + verdicts). Keyed by sha256(content)[:16] so identical
        # content dedupes across attempts and the hash is the replay key.
        self.comment_artifacts = self.root / "comment_artifacts"

    def mkdirs(self) -> None:
        for d in (
            self.root,
            self.prompts,
            self.responses,
            self.snapshots,
            self.candidates,
            self.validations,
            self.final,
            self.comment_artifacts,
        ):
            d.mkdir(parents=True, exist_ok=True)

    # Refs under refs/rebase-agent/<session>/...
    @property
    def start_ref(self) -> str:
        return f"refs/rebase-agent/{self.session_id}/start"

    def step_ref(self, step: int) -> str:
        return f"refs/rebase-agent/{self.session_id}/step-{step}"

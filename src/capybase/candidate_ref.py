"""Candidate-ref rebase mode (candidate-ref-architecture-design, P1).

The strongest safety model: NEVER mutate the source branch until the
complete candidate series has passed all acceptance gates. The whole
rebase runs on a visible candidate branch
``capybase/candidate/<source>@<ts>`` in a linked worktree (sharing the
real repo's object store, so the replayed commits and conflicts are
genuine); the user's checked-out branch is untouched by construction.

On success the candidate branch + audit bundle (the session's journal /
prompts / accept reports) are RETAINED — the promotable artifact. On
escalation the candidate is deleted and the source was never touched.

This mirrors :mod:`capybase.dryrun`'s proven worktree lifecycle (preflight
on the real repo, SIGTERM-safe teardown, backup pruning) with the three
differences the design demands: a retained branch, an OID/fingerprint
snapshot (``session_state.json`` — P2's compare-and-swap promotion and
OID-verified resume consume it), and audit retention.

Promotion (P2) will be ``git update-ref refs/heads/<source> <candidate>
<expected_source_oid>``; until then the report prints the exact command.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from capybase.config import Config
from capybase.git_backend import GitBackend, GitError
from capybase.orchestrator import Orchestrator
from capybase.preflight import run_rebase_preflight

_log = logging.getLogger("capybase.candidate_ref")

#: Namespace for retained candidate branches. Distinct from the backup
#: (safety copies) and dryrun (throwaway) namespaces so `git branch`
#: output separates promotable candidates from the rest.
CANDIDATE_BRANCH_PREFIX = "capybase/candidate"

#: Where retained audit bundles + state files live in the REAL repo
#: (the worktree is removed; the artifact must outlive it).
CANDIDATES_DIR = ".rebase-agent/candidates"


@dataclass
class CandidateReport:
    """The outcome of a candidate-ref rebase run."""

    source_ref: str = ""
    source_oid: str = ""
    target: str = ""
    target_oid: str = ""
    candidate_ref: str = ""
    candidate_oid: str = ""
    would_succeed: bool = False
    escalated: bool = False
    reason: str = ""
    session_id: str = ""
    steps: list[dict] = field(default_factory=list)
    llm_calls: int = 0
    state_path: str = ""

    def summary(self) -> str:
        if self.would_succeed:
            lines = [
                f"CANDIDATE: rebase complete on {self.candidate_ref}",
                f"  source {self.source_ref} @ {self.source_oid[:8]} UNTOUCHED",
                f"  candidate @ {self.candidate_oid[:8]} "
                f"({len(self.steps)} step(s), llm_calls={self.llm_calls})",
                f"  audit bundle: {self.state_path}",
                "  promote (P2 will automate; expected-OID CAS):",
                f"    git update-ref {self.source_ref_full} "
                f"{self.candidate_oid} {self.source_oid}",
            ]
        else:
            lines = [
                "CANDIDATE: escalated — nothing to promote",
                f"  source {self.source_ref} @ {self.source_oid[:8]} UNTOUCHED",
                f"  reason: {self.reason}",
            ]
        return "\n".join(lines)

    @property
    def source_ref_full(self) -> str:
        return self.source_ref if self.source_ref.startswith("refs/") \
            else f"refs/heads/{self.source_ref}"


def _config_fingerprint(config: Config) -> str:
    try:
        blob = json.dumps(config.model_dump(), sort_keys=True, default=str)
    except Exception:  # noqa: BLE001 — fingerprint is best-effort
        blob = repr(config)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _profile_fingerprint() -> str:
    try:
        from capybase.prompt_profile import active_profile

        return active_profile().tag() or "default"
    except Exception:  # noqa: BLE001
        return "default"


def _toolchain_fingerprint() -> dict[str, bool]:
    import shutil as _sh

    return {tool: bool(_sh.which(tool))
            for tool in ("git", "gcc", "g++", "rustc", "cargo", "python3")}


def _resolve_oid(git: GitBackend, ref: str) -> str:
    res = git._run(["rev-parse", "--verify", ref], what=f"rev-parse {ref}")
    if not res.ok:
        raise GitError(f"cannot resolve {ref}: {res.stderr.strip()}")
    return res.stdout.strip()


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def run_candidate_rebase(
    config: Config,
    repo: str | Path,
    target: str,
    *,
    autostash: bool = False,
    resolution_engine=None,
) -> CandidateReport:
    """Run the entire rebase on a candidate branch; never touch the source.

    Raises :class:`~capybase.git_backend.GitError` only on a blocking
    preflight failure (before any worktree exists). Escalations are normal
    outcomes captured in the report.
    """
    git = GitBackend(repo)
    report = CandidateReport(target=target)

    # 1. Preflight the REAL repo — never create a worktree on a bad state.
    preflight = run_rebase_preflight(
        git, config, target, autostash=autostash, llm_ping=False)
    fail = preflight.first_blocking_failure
    if fail is not None:
        raise GitError(f"refusing to run: {fail.detail}")

    # 2. SNAPSHOT — the OID/fingerprint contract P2's promotion + resume
    #    verify. Written into the retained audit bundle at completion.
    report.source_ref = git.current_branch() or "HEAD"
    report.source_oid = git.head_oid()
    report.target_oid = _resolve_oid(git, target)
    ts = time.strftime("%Y%m%d-%H%M%S")
    source_slug = report.source_ref.replace("/", "-")
    report.candidate_ref = f"{CANDIDATE_BRANCH_PREFIX}/{source_slug}@{ts}"
    state = {
        "mode": "candidate",
        "created": ts,
        "source_ref": report.source_ref_full,
        "source_oid": report.source_oid,
        "target": target,
        "target_oid": report.target_oid,
        "candidate_ref": f"refs/heads/{report.candidate_ref}",
        "fingerprints": {
            "config": _config_fingerprint(config),
            "profile": _profile_fingerprint(),
            "toolchain": _toolchain_fingerprint(),
        },
        "outcome": None,
        "candidate_oid": None,
    }

    # SIGTERM-safe teardown (mirrors dryrun: without this, a killed run
    # orphans the worktree — Python's default SIGTERM skips finally).
    import signal

    from capybase.adapters.llm_openai import Interrupted

    _signals = (signal.SIGTERM, getattr(signal, "SIGHUP", signal.SIGTERM))
    _prev_handlers: dict[int, object] = {}

    def _interrupt(signum, _frame):
        raise Interrupted(f"capybase interrupted by signal {signum}")

    for _sig in _signals:
        try:
            _prev_handlers[_sig] = signal.signal(_sig, _interrupt)
        except (ValueError, OSError):
            pass

    worktree_path: Path | None = None
    candidate_dir = Path(repo) / CANDIDATES_DIR / f"{source_slug}@{ts}"
    try:
        # 3. Linked worktree on the candidate branch at the source OID.
        worktree_path = Path(tempfile.mkdtemp(prefix="capybase-candidate-"))
        res = git.add_worktree(
            worktree_path, new_branch=report.candidate_ref,
            start_point=report.source_oid)
        if not res.ok:
            raise GitError(f"worktree add failed: {res.stderr.strip()}")

        # 4. The orchestrator's existing loop runs inside, unchanged.
        kwargs = {"repo": str(worktree_path)}
        if resolution_engine is not None:
            kwargs["resolution_engine"] = resolution_engine
        orch = Orchestrator(config, **kwargs)
        report.session_id = orch.session_id
        result = orch.rebase(target, autostash=autostash, abort_on_escalation=True)
        report.would_succeed = not result.escalated
        report.escalated = bool(result.escalated)
        report.reason = result.reason or ""

        # Fold the journal for step/llm counts (the same summarizer the
        # dry-run report uses — one implementation).
        from capybase.dryrun import RehearsalReport, _summarize_journal

        intermediate = RehearsalReport(target=target)
        _summarize_journal(orch.paths.journal, intermediate)
        report.llm_calls = intermediate.llm_calls
        report.steps = [
            {"step": s.step, "files": s.files, "escalated": s.escalated,
             "accepted": s.accepted}
            for s in intermediate.steps
        ]

        # 5. Retain the audit bundle (the worktree is about to go; the
        #    artifact must outlive it) and finalize the state file.
        if orch.paths.root.exists():
            shutil.copytree(
                orch.paths.root, candidate_dir / "session",
                dirs_exist_ok=True)
        state["outcome"] = "success" if report.would_succeed else "escalated"
        if report.would_succeed:
            report.candidate_oid = _resolve_oid(
                git, f"refs/heads/{report.candidate_ref}")
            state["candidate_oid"] = report.candidate_oid
        _write_state(candidate_dir / "session_state.json", state)
        report.state_path = str(candidate_dir / "session_state.json")

        return report
    finally:
        # The worktree is disposable in BOTH outcomes — the candidate
        # branch lives in the shared object store; the audit bundle was
        # copied out above. ORDER MATTERS: the branch deletion must come
        # AFTER remove_worktree (git refuses -D on a branch checked out
        # in a live worktree).
        if worktree_path is not None and worktree_path.exists():
            git.remove_worktree(worktree_path, force=True)
        git.prune_worktrees()
        if not report.would_succeed:
            # Nothing to promote: delete the candidate branch + the
            # session-tagged backups the in-worktree run created (the
            # source never moved, so they're pointless — dryrun's rule).
            _delete_candidate_refs(git, report.candidate_ref)
        for _sig, _prev in _prev_handlers.items():
            try:
                signal.signal(_sig, _prev)  # type: ignore[arg-type]
            except (ValueError, OSError, TypeError):
                pass
        _log.info(
            "candidate run complete: source=%s target=%s outcome=%s",
            report.source_ref, target,
            "success" if report.would_succeed else "escalated",
        )


def _delete_candidate_refs(git: GitBackend, candidate_ref: str) -> None:
    """Delete the candidate branch + session-tagged backup refs."""
    candidate_id = candidate_ref.split("@", 1)[-1]
    try:
        git._run(
            ["branch", "-D", candidate_ref], what="delete candidate branch")
    except Exception:  # noqa: BLE001 — best-effort cleanup
        _log.debug("candidate branch already gone", exc_info=True)
    for ref in list(git.list_backup_refs()):
        if candidate_id and candidate_id in ref:
            try:
                git.delete_ref(ref)
            except Exception:  # noqa: BLE001
                _log.debug("backup ref %s already gone", ref, exc_info=True)

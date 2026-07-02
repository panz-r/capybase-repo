"""Full temp-worktree dry-run rehearsal of a rebase.

The single most confidence-building feature for first real runs: run the
*entire* rebase — preflight, conflict detection, LLM resolution, validation,
tests, ``git rebase --continue`` — in a throwaway linked worktree, and report
whether it would succeed, **without ever moving the user's branch pointer**.

How it works:

1. Run preflight against the *real* repo (git-only checks). Abort early on a
   blocking failure — never create a worktree on a bad state.
2. ``git worktree add`` a linked worktree at HEAD on a throwaway branch
   ``capybase/dryrun/<session>``. It shares the real repo's object store, so the
   replayed commits and conflicts are genuine (not synthetic), and it's cheap
   (no clone).
3. Construct an :class:`~capybase.orchestrator.Orchestrator` pointed at the
   worktree path. capybase writes its ``.rebase-agent/`` session tree *inside*
   the worktree, so the real repo stays untouched.
4. Drive ``orch.rebase(target, ...)`` exactly as the real command would.
5. Read the worktree session's journal to build a per-step report.
6. ``finally``: remove the worktree and delete the throwaway branch. The real
   branch is unchanged regardless of outcome.

The user's branch pointer is never moved; the real repo's working tree is never
written. The only side-effect on the real repo is the creation+removal of a
linked worktree administrative entry (pruned on cleanup).
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from capybase.git_backend import GitBackend, GitError
from capybase.config import Config
from capybase.orchestrator import Orchestrator
from capybase.preflight import run_rebase_preflight
from capybase.session import SessionPaths

_log = logging.getLogger("capybase.dryrun")

#: Namespace for the throwaway dry-run branch. Distinct from the backup branch
#: namespace so ``git branch`` output cleanly separates real backups (from an
#: actual rebase) from transient dry-run branches.
DRYRUN_BRANCH_PREFIX = "capybase/dryrun"


@dataclass
class RehearsalStep:
    """One rebase step (commit) as observed during the dry run."""

    step: int
    files: list[str] = field(default_factory=list)
    escalated: bool = False
    accepted: bool = False
    detail: str = ""
    # History-aware fields (#9 step 10): the mechanisms used + probe results in
    # this step, so the dry-run report can break resolutions down by provenance.
    mechanisms: list[str] = field(default_factory=list)
    future_probes_passed: int = 0
    future_probes_failed: int = 0


@dataclass
class RehearsalReport:
    """The outcome of a full dry-run rehearsal.

    ``would_succeed`` is True iff the rehearsal completed the whole rebase
    without escalating. ``steps`` is the per-commit breakdown. ``errors`` holds
    any blocking preflight failures or escalated reasons. The real branch's
    head is unchanged regardless.

    History-aware fields (#9 step 10): ``mechanism_counts`` tallies resolutions
    by provenance across the rehearsal; ``conflict_chains`` lists detected chains
    (from the orchestrator's detector); ``history_active`` flags whether a
    history plan was in play (gates the richer ``summary_history`` view).
    """

    would_succeed: bool = False
    steps: list[RehearsalStep] = field(default_factory=list)
    llm_calls: int = 0
    errors: list[str] = field(default_factory=list)
    target: str = ""
    head_before: str = ""
    head_after: str = ""
    session_id: str = ""
    mechanism_counts: dict[str, int] = field(default_factory=dict)
    conflict_chains: list[str] = field(default_factory=list)
    history_active: bool = False

    def summary(self) -> str:
        if self.errors and not self.would_succeed:
            head = f"DRY RUN: would NOT succeed — {self.errors[0]}"
        elif self.would_succeed:
            moved = "no change" if self.head_before == self.head_after else "would advance"
            head = f"DRY RUN: would succeed ({len(self.steps)} step(s), {moved})"
        else:
            head = "DRY RUN: would escalate"
        lines = [head]
        for s in self.steps:
            tag = "ACCEPT" if s.accepted else ("ESCALATE" if s.escalated else "?")
            lines.append(f"  step {s.step} [{tag}] {s.detail or ', '.join(s.files) or '(no conflicts)'}")
        if self.errors:
            for e in self.errors[1:]:
                lines.append(f"  error: {e}")
        lines.append(
            f"  target={self.target} head {self.head_before[:8]} -> "
            f"{self.head_after[:8]} | llm_calls={self.llm_calls}"
        )
        return "\n".join(lines)

    def summary_history(self) -> str:
        """The history-aware dry-run report (#9 step 10).

        A planning report: commits replayed, conflicts resolved per mechanism,
        probes, conflict chains, and a rule-based recommended action. Falls back
        to the terse :meth:`summary` when no history plan was active.
        """
        if not self.history_active:
            return self.summary()
        lines = ["History-aware dry run:"]
        # Commits replayed = number of steps that fired (step_started events).
        replayed = len(self.steps)
        lines.append(f"- {replayed} commit(s) replayed")
        # Conflicts encountered = steps with files.
        conflicts = sum(1 for s in self.steps if s.files)
        lines.append(f"- {conflicts} conflict(s) encountered")
        # Per-mechanism resolution breakdown.
        from capybase.provenance import provenance_label

        for prov, count in sorted(
            self.mechanism_counts.items(), key=lambda kv: -kv[1]
        ):
            label = provenance_label(prov) if prov else "(unknown)"
            lines.append(f"  - {count} resolved via {label}")
        # Escalations.
        escalations = sum(1 for s in self.steps if s.escalated)
        if escalations:
            lines.append(f"- {escalations} escalated")
        # Future probes.
        probes_passed = sum(s.future_probes_passed for s in self.steps)
        probes_failed = sum(s.future_probes_failed for s in self.steps)
        if probes_passed or probes_failed:
            lines.append(f"- {probes_passed} future probe(s) passed, {probes_failed} failed")
        # Conflict chains.
        for chain in self.conflict_chains:
            lines.append(f"- conflict chain: {chain}")
        # Rule-based recommended action (not LLM — derived from the data).
        action = self._recommended_action()
        if action:
            lines.append(f"- recommended action: {action}")
        return "\n".join(lines)

    def _recommended_action(self) -> str:
        """A rule-based action recommendation from the chain/probe/escalation data.

        e.g. an escalated chain → 'squash commits X–Y or resolve <region> manually'.
        Empty when the rebase would succeed cleanly (no action needed).
        """
        if self.would_succeed and not self.conflict_chains:
            return ""
        if self.conflict_chains:
            # The first (largest) chain's coordinate + its commit range.
            first = self.conflict_chains[0]
            return f"squash the related commits or resolve the {first.split(' ::')[0]} chain manually"
        if not self.would_succeed and self.errors:
            return "review the escalation and resolve manually before rebasing"
        return ""


def _summarize_journal(journal_path: Path, report: RehearsalReport) -> None:
    """Fold the worktree session's journal into the report's steps/counts."""
    if not journal_path.exists():
        return
    import json

    for line in journal_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        et = ev.get("event_type", "")
        payload = ev.get("payload", {})
        step_idx = ev.get("step_index") or 0
        if et == "candidate_generated":
            report.llm_calls += 1
        elif et == "step_started" and step_idx:
            report.steps.append(RehearsalStep(step=step_idx))
        elif et == "conflict_detected" and report.steps:
            report.steps[-1].files.extend(payload.get("paths", []) or [])
        elif et == "candidate_accepted" and report.steps:
            report.steps[-1].accepted = True
            # Record the resolution mechanism (#9 step 10): the LLM path carries
            # provenance in the payload; the pre-LLM paths carry a `via` label.
            prov = payload.get("provenance") or ""
            if not prov:
                via = payload.get("via", "")
                prov = _via_to_provenance(via)
            if prov:
                report.steps[-1].mechanisms.append(prov)
                report.mechanism_counts[prov] = report.mechanism_counts.get(prov, 0) + 1
        elif et == "exact_reuse_applied" and report.steps:
            # Exact reuse journals its own accept (no candidate_accepted), so
            # count it here too.
            report.steps[-1].accepted = True
            report.steps[-1].mechanisms.append("exact_history_reuse")
            report.mechanism_counts["exact_history_reuse"] = (
                report.mechanism_counts.get("exact_history_reuse", 0) + 1
            )
        elif et == "future_apply_probe" and report.steps:
            probed = payload.get("probed")
            applies = payload.get("applies")
            if probed:
                if applies:
                    report.steps[-1].future_probes_passed += 1
                else:
                    report.steps[-1].future_probes_failed += 1
        elif et == "escalated":
            # Attribute to the current step if any, else record globally.
            reason = payload.get("reason", "")
            if report.steps:
                report.steps[-1].escalated = True
                report.steps[-1].detail = reason
            if reason and reason not in report.errors:
                report.errors.append(reason)


def _via_to_provenance(via: str) -> str:
    """Map a journal ``via`` label to a provenance value (#9 step 10).

    The pre-LLM mechanisms (structural/sbcr/block_capture) journal a ``via``
    label rather than a provenance string; map them so the dry-run report's
    mechanism breakdown is consistent.
    """
    mapping = {
        "structural": "deterministic_structural",
        "sbcr": "combination_search",
        "block_capture": "block_capture",
        "exact_reuse": "exact_history_reuse",
    }
    return mapping.get(via, via)


def rehearse_rebase(
    config: Config,
    repo: str | Path,
    target: str,
    *,
    autostash: bool = False,
    resolution_engine=None,
) -> RehearsalReport:
    """Rehearse a full rebase in a throwaway worktree; return a report.

    The real repo's branch pointer and working tree are never modified. Real
    LLM calls are made (that's the point of the rehearsal); pass a fake
    ``resolution_engine`` for hermetic tests.

    Raises nothing on a rebase escalation — that's a normal rehearsal outcome
    captured in the report. Raises :class:`~capybase.git_backend.GitError` only
    on a blocking preflight failure (so the caller can report it before any
    worktree exists).
    """
    git = GitBackend(repo)
    report = RehearsalReport(target=target, head_before=git.head_oid())

    # 1. Preflight the REAL repo. Never create a worktree on a bad state.
    preflight = run_rebase_preflight(git, config, target, autostash=autostash, llm_ping=False)
    fail = preflight.first_blocking_failure
    if fail is not None:
        report.errors.append(fail.detail)
        _log.warning("dry-run preflight blocked: %s", fail.detail)
        raise GitError(f"refusing to dry-run: {fail.detail}")

    worktree_path: Path | None = None
    dryrun_branch: str | None = None
    # Install a SIGTERM/SIGHUP handler so a killed dry-run (e.g. `timeout`,
    # closing the terminal) still runs the `finally` cleanup below. Python's
    # default SIGTERM terminates immediately WITHOUT running finally, which
    # would orphan the worktree + throwaway branch. SIGINT (Ctrl-C) already
    # raises KeyboardInterrupt (so finally runs) — we only need to convert the
    # terminate-style signals. Restored in finally so we don't leak the handler.
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
            pass  # not in main thread / unsupported — best effort

    try:
        # 2. Linked worktree at HEAD on a throwaway branch. Shares the object
        #    store (cheap), so the replayed commits/conflicts are genuine.
        import uuid

        worktree_path = Path(tempfile.mkdtemp(prefix="capybase-dryrun-"))
        dryrun_session = uuid.uuid4().hex[:12]
        dryrun_branch = f"{DRYRUN_BRANCH_PREFIX}/{dryrun_session}"
        res = git.add_worktree(worktree_path, new_branch=dryrun_branch)
        if not res.ok:
            report.errors.append(f"worktree add failed: {res.stderr.strip()}")
            _log.error("dry-run worktree add failed: %s", res.stderr.strip())
            return report

        # 3. Orchestrator pointed at the worktree. Its .rebase-agent/ tree lands
        #    inside the worktree, so the real repo's tree is untouched.
        kwargs = {"repo": str(worktree_path)}
        if resolution_engine is not None:
            kwargs["resolution_engine"] = resolution_engine
        orch = Orchestrator(config, **kwargs)
        report.session_id = orch.session_id

        # 4. Drive the real rebase path against the worktree.
        result = orch.rebase(target, autostash=autostash, abort_on_escalation=True)
        report.would_succeed = not result.escalated

        # 5. Fold the worktree journal into the report (steps, files, llm_calls).
        _summarize_journal(orch.paths.journal, report)
        # History-aware enrichment (#9 step 10): the orchestrator accumulated
        # conflict-chain observations during the rehearsal; surface the chains +
        # flag history activity so summary_history() produces the planning view.
        try:
            chain_report = orch.detect_conflict_chains()
            report.conflict_chains = [c.characterization() for c in chain_report.chains]
            report.history_active = orch._history_plan is not None
        except Exception:  # noqa: BLE001 - advisory
            pass

        # The orchestrator may escalate without emitting a journal "escalated"
        # event (e.g. a unit that exhausts its retry budget only writes a review
        # bundle + StepResult.reason). So take the escalation reason from the
        # StepResult directly — it's authoritative — and attribute it to the
        # last step (the journal has populated report.steps by now).
        if result.escalated and result.reason:
            report.errors.append(result.reason)
            if report.steps:
                report.steps[-1].escalated = True
                report.steps[-1].detail = result.reason
        report.head_after = git.head_oid()  # real repo HEAD — must be unchanged
        return report
    finally:
        # 6. Tear down the worktree + throwaway branches. Idempotent: a failed
        #    worktree add leaves nothing to remove. Runs even on a SIGTERM (the
        #    handler above converts it to an exception that flows here). The
        #    dry-run's rebase also creates backup branches (capybase/backup/...)
        #    in the shared object store — those are pointless for a dry-run (the
        #    real branch never moved), so prune any backups tagged with this
        #    session's dryrun branch id.
        if worktree_path is not None and worktree_path.exists():
            git.remove_worktree(worktree_path, force=True)
        git.prune_worktrees()
        if dryrun_branch is not None:
            # Delete the throwaway dry-run branch AND any backup branches the
            # orchestrator created during the rehearsal (they carry the dryrun
            # branch id in their name, e.g. capybase/backup/capybase-dryrun-<id>@...).
            dryrun_id = dryrun_branch.split("/")[-1]
            for ref in list(git.list_backup_refs()) + [dryrun_branch]:
                if dryrun_id in ref:
                    try:
                        # Backup refs use the namespace guard; the dryrun branch
                        # doesn't, so try both delete paths.
                        if ref.startswith("capybase/backup/"):
                            git.delete_ref(ref)
                        else:
                            git._run(["branch", "-D", ref], what="delete dryrun branch")
                    except Exception:  # noqa: BLE001 - best-effort cleanup
                        _log.debug("dry-run branch %s already gone", ref, exc_info=True)
        # Restore the prior signal handlers so we don't leak the interrupt hook.
        for _sig, _prev in _prev_handlers.items():
            try:
                signal.signal(_sig, _prev)  # type: ignore[arg-type]
            except (ValueError, OSError, TypeError):
                pass
        _log.info(
            "dry-run complete: target=%s would_succeed=%s steps=%d llm_calls=%d",
            target, report.would_succeed, len(report.steps), report.llm_calls,
        )

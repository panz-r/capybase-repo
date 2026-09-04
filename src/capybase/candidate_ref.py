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
    reused: bool = False
    reused_from: str = ""
    policy_decision: str = ""
    policy_tier: str = ""
    policy_reasons: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.reused:
            return "\n".join([
                f"CANDIDATE (reused): retained candidate matches every "
                f"fingerprint — no model run needed",
                f"  source {self.source_ref} @ {self.source_oid[:8]} "
                f"UNTOUCHED",
                f"  candidate @ {self.candidate_oid[:8]} "
                f"(previously tested)",
                f"  prior state: {self.reused_from}",
                f"  promote: `capybase promote`",
            ])
        if self.would_succeed:
            lines = [
                f"CANDIDATE: rebase complete on {self.candidate_ref}",
                f"  source {self.source_ref} @ {self.source_oid[:8]} UNTOUCHED",
                f"  candidate @ {self.candidate_oid[:8]} "
                f"({len(self.steps)} step(s), llm_calls={self.llm_calls})",
                f"  audit bundle: {self.state_path}",
                (f"  POLICY: {self.policy_decision} (tier "
                 f"{self.policy_tier})"
                 + (f" — {self.policy_reasons[0]}" if self.policy_reasons
                    else "")),
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


def _default_remote(git: GitBackend, source_ref: str) -> str | None:
    """The source branch's default remote (pushRemote > push > origin)."""
    branch = source_ref.removeprefix("refs/heads/")
    for probe in (
        ["config", "--get", f"branch.{branch}.pushRemote"],
        ["config", "--get", f"branch.{branch}.remote"],
        ["config", "--get", "remote.pushDefault"],
        ["config", "--get", "remote.origin.url"],
    ):
        r = git._run(probe, what="remote probe")
        if r.ok and r.stdout.strip():
            if probe[-1].startswith("remote."):
                return probe[-1].split(".")[1]
            return r.stdout.strip()
    return None


def _matching_retained_candidate(
    repo: Path, state: dict,
) -> tuple[dict, Path] | None:
    """A retained SUCCESSFUL candidate whose fingerprints ALL match.

    The reuse contract (design P4): same source ref+OID, same target+OID,
    same config and profile fingerprints, same toolchain environment.
    A toolchain mismatch blocks reuse — the recorded evidence was
    produced under that toolchain, and pretending otherwise would be the
    unknown-is-not-pass mistake at the artifact level.
    """
    root = repo / CANDIDATES_DIR
    if not root.is_dir():
        return None
    want = state["fingerprints"]
    for sp in sorted(root.glob("*/session_state.json"), reverse=True):
        try:
            prior = json.loads(sp.read_text())
        except Exception:  # noqa: BLE001
            continue
        if prior.get("outcome") != "success":
            # outcome=None = an interrupted run (the worktree died
            # mid-series; git only advances the branch at completion, so
            # there is nothing mid-series to resume from by construction
            # — a safety property, not a gap). Never reused.
            continue
        if (prior.get("source_ref") == state["source_ref"]
                and prior.get("source_oid") == state["source_oid"]
                and prior.get("target") == state["target"]
                and prior.get("target_oid") == state["target_oid"]
                and prior.get("fingerprints", {}).get("config")
                == want.get("config")
                and prior.get("fingerprints", {}).get("profile")
                == want.get("profile")
                and prior.get("fingerprints", {}).get("toolchain")
                == want.get("toolchain")
                and not prior.get("promoted")
                and prior.get("candidate_oid")):
            return prior, sp
    return None


def run_candidate_rebase(
    config: Config,
    repo: str | Path,
    target: str,
    *,
    autostash: bool = False,
    resolution_engine=None,
    reuse: bool = True,
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
    # P5: the expected REMOTE OID — the lease's expectation. Recorded at
    # snapshot from the source branch's default remote-tracking ref
    # (branch.<src>.pushRemote > remote.<default>.push > origin). A
    # remote that moves after this breaks the lease at publish (the
    # honest refusal). Absent when no remote is configured (local-only).
    _remote_name = _default_remote(git, report.source_ref)
    _expected_remote_oid = None
    if _remote_name:
        _r = git._run(
            ["rev-parse", "--verify", "--quiet",
             f"refs/remotes/{_remote_name}/"
             f"{report.source_ref.removeprefix('refs/heads/')}"],
            what="snapshot remote-tracking oid")
        if _r.ok:
            _expected_remote_oid = _r.stdout.strip()
    ts = time.strftime("%Y%m%d-%H%M%S")
    source_slug = report.source_ref.replace("/", "-")
    # Unique-ify: two runs in the same second (or a retained candidate
    # from a --fresh rerun) must not collide on the branch/dir name.
    base_ref = f"{CANDIDATE_BRANCH_PREFIX}/{source_slug}@{ts}"
    _uniq = ""
    while True:
        probe = f"{base_ref}{_uniq}"
        res = git._run(
            ["rev-parse", "--verify", "--quiet",
             f"refs/heads/{probe}"], what="candidate name probe")
        if not res.ok:
            report.candidate_ref = probe
            break
        _uniq = ("-2" if _uniq == "" else
                 f"-{int(_uniq[1:]) + 1}")
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
        "remote": _remote_name,
        "expected_remote_oid": _expected_remote_oid,
        "transitions": [
            {"name": "snapshot", "source_oid": report.source_oid,
             "target_oid": report.target_oid, "at": ts},
        ],
    }

    # P4 reuse: a retained candidate that matches EVERY fingerprint is the
    # already-tested answer — skip the nondeterministic model run. The
    # artifact is the same bytes under the same contract; re-running would
    # buy variance, not information.
    if reuse:
        match = _matching_retained_candidate(Path(repo), state)
        if match is not None:
            prior, prior_path = match
            report.reused = True
            report.reused_from = str(prior_path)
            report.would_succeed = True
            report.candidate_oid = prior["candidate_oid"]
            report.candidate_ref = prior["candidate_ref"].removeprefix(
                "refs/heads/")
            report.state_path = str(prior_path)
            _log.info(
                "candidate reuse: %s matches all fingerprints",
                prior_path)
            return report

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

        # The acceptance policy for the WHOLE series (the candidate-ref
        # design: the promotion side consumes the decision). The per-step
        # policy is journaled as acceptance_trust events; aggregate them —
        # any STOP dominates, then any PROPOSE_FOR_REVIEW, else AUTO_APPLY.
        _tiers: dict[str, list[str]] = {"A": [], "B": [], "C": []}
        _decisions = {"A": "AUTO_APPLY", "B": "PROPOSE_FOR_REVIEW",
                      "C": "STOP"}
        try:
            for _line in orch.paths.journal.read_text(
                    encoding="utf-8").splitlines():
                try:
                    _ev = json.loads(_line)
                except json.JSONDecodeError:
                    continue
                if _ev.get("event_type") == "acceptance_trust":
                    _p = _ev.get("payload", {})
                    _t = _p.get("tier", "B")
                    _rs = _p.get("reasons") or []
                    _tiers.setdefault(_t, []).append(
                        "; ".join(str(r) for r in _rs)[:120])
        except Exception:  # noqa: BLE001 — aggregation is best-effort
            pass
        report.policy_tier = ("C" if _tiers["C"]
                              else "B" if _tiers["B"]
                              else "A" if _tiers["A"] else "B")
        report.policy_decision = _decisions[report.policy_tier]
        report.policy_reasons = [
            r for r in (_tiers["C"] + _tiers["B"]) if r][:4]
        state["policy"] = {
            "decision": report.policy_decision,
            "tier": report.policy_tier,
            "reasons": report.policy_reasons,
        }

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
        state.setdefault("transitions", []).append({
            "name": "completed",
            "outcome": state["outcome"],
            "candidate_oid": state["candidate_oid"],
            "at": time.strftime("%Y%m%d-%H%M%S"),
        })
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
        # The in-worktree run's legacy machinery creates backup branches
        # (capybase/backup/capybase-candidate-<id>@...) in the shared
        # store — pointless in candidate mode (the source never moved),
        # in BOTH outcomes. The candidate BRANCH is retained on success
        # (the artifact) and deleted on escalation (nothing to promote).
        _delete_candidate_refs(
            git, report.candidate_ref,
            delete_branch=not report.would_succeed)
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


def _delete_candidate_refs(
    git: GitBackend, candidate_ref: str, *, delete_branch: bool = True,
) -> None:
    """Session cleanup: the candidate branch (escalation only — success
    RETAINS it) + the in-worktree run's legacy backup branches (always:
    the source never moved, so they are pointless in candidate mode)."""
    candidate_id = candidate_ref.split("@", 1)[-1]
    if delete_branch:
        try:
            git._run(
                ["branch", "-D", candidate_ref],
                what="delete candidate branch")
        except Exception:  # noqa: BLE001 — best-effort cleanup
            _log.debug("candidate branch already gone", exc_info=True)
    for ref in list(git.list_backup_refs()):
        if candidate_id and candidate_id in ref:
            try:
                git.delete_ref(ref)
            except Exception:  # noqa: BLE001
                _log.debug("backup ref %s already gone", ref, exc_info=True)


@dataclass
class PromoteResult:
    """The outcome of a compare-and-swap promotion."""

    promoted: bool = False
    refused_reason: str = ""
    source_ref: str = ""
    source_oid_before: str = ""
    candidate_oid: str = ""
    state_path: str = ""
    checked_out_updated: bool = False

    def summary(self) -> str:
        if self.promoted:
            lines = [
                f"PROMOTED: {self.source_ref} -> {self.candidate_oid[:8]}",
                f"  compare-and-swap from {self.source_oid_before[:8]} held"
                " (the branch had not moved)",
                f"  state: {self.state_path}",
            ]
            if self.checked_out_updated:
                lines.append("  checked-out worktree updated (--checkout)")
            else:
                lines.append(
                    "  NOTE: if this branch is checked out, refresh it: "
                    f"`git reset --hard {self.candidate_oid}` "
                    "(or re-checkout) — the ref moved, the tree did not")
            return "\n".join(lines)
        return f"PROMOTE REFUSED: {self.refused_reason}"


def _latest_candidate_state(repo: Path) -> Path | None:
    """The newest retained successful candidate's state file, if any."""
    root = repo / CANDIDATES_DIR
    if not root.is_dir():
        return None
    states = sorted(root.glob("*/session_state.json"), reverse=True)
    for p in states:
        try:
            if json.loads(p.read_text()).get("outcome") == "success":
                return p
        except Exception:  # noqa: BLE001 — malformed state files skipped
            continue
    return None


def promote_candidate(
    repo: str | Path,
    *,
    state_path: str | Path | None = None,
    checkout: bool = False,
    keep_ref: bool = False,
    approve: bool = False,
) -> PromoteResult:
    """Atomically promote a retained candidate onto its source ref.

    The compare-and-swap: ``git update-ref <source> <candidate_oid>
    <expected_source_oid>`` — the promotion lands ONLY if the source
    ref still sits at the OID the candidate was tested from; any drift
    (the branch moved, another tool rewrote it) refuses with both OIDs
    named. Never forces.
    """
    git = GitBackend(repo)
    repo_path = Path(repo)
    result = PromoteResult()

    if state_path is None:
        found = _latest_candidate_state(repo_path)
        if found is None:
            result.refused_reason = (
                "no retained successful candidate found under "
                f"{CANDIDATES_DIR}/ — run `capybase rebase --candidate` first"
            )
            return result
        state_path = found
    state_path = Path(state_path)
    if not state_path.is_file():
        result.refused_reason = f"state file not found: {state_path}"
        return result
    try:
        state = json.loads(state_path.read_text())
    except Exception as exc:  # noqa: BLE001
        result.refused_reason = f"state file unreadable: {exc}"
        return result
    result.state_path = str(state_path)
    if state.get("outcome") != "success":
        result.refused_reason = (
            f"candidate outcome is {state.get('outcome')!r}, not 'success' "
            "— nothing to promote"
        )
        return result

    result.source_ref = state["source_ref"]
    result.source_oid_before = state["source_oid"]
    result.candidate_oid = state["candidate_oid"]

    # Drift check: the source ref must still be exactly where it was.
    res = git._run(
        ["rev-parse", "--verify", result.source_ref],
        what=f"rev-parse {result.source_ref}")
    if not res.ok:
        result.refused_reason = (
            f"source ref {result.source_ref} no longer resolves — "
            "the branch was deleted/renamed since the candidate ran"
        )
        return result
    current = res.stdout.strip()
    if current != result.source_oid_before:
        result.refused_reason = (
            f"source ref DRIFT: {result.source_ref} is at "
            f"{current[:8]}, expected {result.source_oid_before[:8]} "
            "(the branch moved after the candidate was tested) — re-run "
            "the candidate against the new tip; never force"
        )
        return result

    # The candidate commit must still exist.
    res = git._run(
        ["cat-file", "-e", f"{result.candidate_oid}^{{commit}}"],
        what="verify candidate commit")
    if not res.ok:
        result.refused_reason = (
            f"candidate commit {result.candidate_oid[:8]} no longer exists "
            "in the object store (was it gc'd?)"
        )
        return result


    # The acceptance policy gates promotion (the design's tier table):
    # AUTO_APPLY promotes; PROPOSE_FOR_REVIEW / STOP require the human's
    # explicit --approve (the review act). "Unknown is not pass" — a
    # candidate whose oracles could not run never silently lands.
    _policy = state.get("policy") or {}
    _tier = _policy.get("tier", "B")
    if _tier in ("B", "C") and not approve:
        result.refused_reason = (
            f"acceptance policy is {_policy.get('decision', 'PROPOSE_FOR_REVIEW')} "
            f"(tier {_tier})"
            + (f" — {_policy.get('reasons', [''])[0]}" if _policy.get("reasons") else "")
            + " — promote with --approve after review (unknown is not pass)"
        )
        return result

    # --checkout preconditions verified BEFORE the CAS (never half-promote):
    # the update itself is always safe (ref-only); the checkout dance
    # requires the tree to be clean.
    head_branch = git.current_branch()
    checked_out = head_branch is not None and (
        f"refs/heads/{head_branch}" == result.source_ref
        or head_branch == result.source_ref)
    if checkout and checked_out:
        if not git.worktree_is_clean():
            result.refused_reason = (
                "--checkout refused: the working tree has uncommitted "
                "changes (the ref promotion alone is safe; commit or stash, "
                "then re-promote with --checkout, or promote without it)"
            )
            return result

    # THE compare-and-swap.
    res = git._run(
        ["update-ref", result.source_ref, result.candidate_oid,
         result.source_oid_before],
        what="promote update-ref (CAS)")
    if not res.ok:
        result.refused_reason = (
            f"update-ref refused: {res.stderr.strip() or 'unknown error'} "
            "(the ref likely moved between the check and the swap)"
        )
        return result
    result.promoted = True

    if checkout and checked_out:
        git._run(
            ["reset", "--hard", result.candidate_oid],
            what="promote checkout refresh")
        result.checked_out_updated = True

    # Record the promotion in the retained state (the audit trail).
    try:
        state["promoted"] = {
            "at": time.strftime("%Y%m%d-%H%M%S"),
            "to": result.candidate_oid,
            "checkout": result.checked_out_updated,
        }
        _write_state(state_path, state)
    except Exception:  # noqa: BLE001 — state update is advisory
        _log.debug("promote: state update failed", exc_info=True)

    # The candidate branch is consumed (its OID is reachable from the
    # source now); delete unless asked to keep.
    if not keep_ref:
        cand_ref = state.get("candidate_ref")
        if cand_ref:
            try:
                git._run(
                    ["branch", "-D", cand_ref.removeprefix("refs/heads/")],
                    what="delete consumed candidate branch")
            except Exception:  # noqa: BLE001
                _log.debug("candidate branch already gone", exc_info=True)
    return result


@dataclass
class PublishResult:
    """The outcome of a lease-protected remote publication (P5)."""

    published: bool = False
    refused_reason: str = ""
    remote: str = ""
    remote_ref: str = ""
    expected_remote_oid: str = ""
    candidate_oid: str = ""

    def summary(self) -> str:
        if self.published:
            return (
                f"PUBLISHED: {self.remote_ref} -> {self.candidate_oid[:8]} "
                f"on {self.remote} (lease held against "
                f"{self.expected_remote_oid[:8]})"
            )
        return f"PUBLISH REFUSED: {self.refused_reason}"


def publish_candidate(
    repo: str | Path,
    *,
    state_path: str | Path | None = None,
    remote: str | None = None,
    approve: bool = False,
    dry_run: bool = False,
) -> PublishResult:
    """Publish a retained candidate to the remote with an EXPLICIT lease.

    ``git push --force-with-lease=<ref>:<expected_oid> <remote>
    <candidate_oid>:<ref>`` — the explicit expected-OID form only (the
    design: implicit leases are weakened by background fetches updating
    remote-tracking refs). The expectation is the REMOTE OID recorded at
    the candidate's snapshot; a remote that moved since breaks the lease
    and refuses — never forces. The policy consent gate applies (tier
    A, or --approve). Purely additive: nothing in rebase/promote ever
    calls this automatically (capybase stays local-first; publishing is
    the service operator's explicit act).
    """
    git = GitBackend(repo)
    result = PublishResult()

    if state_path is None:
        found = _latest_candidate_state(Path(repo))
        if found is None:
            result.refused_reason = (
                "no retained successful candidate — run "
                "`capybase rebase` (candidate mode) first")
            return result
        state_path = found
    try:
        state = json.loads(Path(state_path).read_text())
    except Exception as exc:  # noqa: BLE001
        result.refused_reason = f"state file unreadable: {exc}"
        return result
    if state.get("outcome") != "success":
        result.refused_reason = (
            f"candidate outcome is {state.get('outcome')!r} — nothing to publish")
        return result
    result.candidate_oid = state["candidate_oid"]
    result.remote_ref = state["source_ref"]
    result.expected_remote_oid = state.get("expected_remote_oid")

    _policy = state.get("policy") or {}
    if _policy.get("tier", "B") in ("B", "C") and not approve:
        result.refused_reason = (
            f"acceptance policy is "
            f"{_policy.get('decision', 'PROPOSE_FOR_REVIEW')} (tier "
            f"{_policy.get('tier', 'B')}) — publish with --approve after "
            f"review (unknown is not pass)")
        return result

    if not result.expected_remote_oid:
        result.refused_reason = (
            "the candidate's snapshot recorded no expected remote OID "
            "(no remote-tracking ref at run time) — configure the remote "
            "and re-run the candidate, or the remote cannot be leased "
            "safely")
        return result
    result.remote = remote or state.get("remote") or "origin"

    # THE explicit-lease push. --force-with-lease=<ref>:<oid> refuses when
    # the remote ref is not exactly at the expected OID (it moved since
    # the snapshot) — the CAS analogue for remotes.
    args = [
        "push", f"--force-with-lease={result.remote_ref}:"
        f"{result.expected_remote_oid}",
    ]
    if dry_run:
        args.append("--dry-run")
    args += [
        result.remote,
        f"{result.candidate_oid}:{result.remote_ref}",
    ]
    res = git._run(args, what="publish lease push")
    if not res.ok:
        result.refused_reason = (
            f"lease push refused: {res.stderr.strip()[:200]} — the remote "
            f"moved since the snapshot (the lease held; never forced). "
            f"Fetch, re-run the candidate against the new remote tip."
        )
        return result
    result.published = True
    try:
        state["published"] = {
            "at": time.strftime("%Y%m%d-%H%M%S"),
            "remote": result.remote,
            "dry_run": dry_run,
        }
        _write_state(Path(state_path), state)
    except Exception:  # noqa: BLE001 — state update is advisory
        _log.debug("publish: state update failed", exc_info=True)
    return result

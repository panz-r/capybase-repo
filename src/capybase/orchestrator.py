"""The orchestrator: the rebase state machine and sole Git mutator.

It knows the 8-step loop and nothing about model internals. It calls into the
stable contracts::

    candidate = resolution_engine.propose(unit, context)
    verdict   = verification.verify(unit, candidate)
    decision  = risk.decide(verdict, retry_count=...)

Three modes share the same inspection core:

* ``inspect``  — M1: detect, extract, journal, write a review bundle, no mutation.
* ``manual``   — M2: print a unit, read a pasted resolution from stdin, splice,
                  validate, stage. No auto-continue.
* ``run``      — M3: full loop — propose/verify/risk → splice/write/stage →
                  tests → ``git rebase --continue``. Retries up to policy max,
                  else escalates and stops.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from capybase.conflict_extractor import ConflictExtractor, SkippedPath
from capybase.conflict_model import (
    CandidateResolution,
    ConflictUnit,
    RiskDecision,
    VerificationResult,
)
from capybase.context_builder import ContextBuilder
from capybase.escalation import write_review_bundle
from capybase.git_backend import GitBackend, GitResult
from capybase.journal import Journal
from capybase.policy import Policy
from capybase.resolution_engine import ResolutionEngine
from capybase.risk import RiskEngine
from capybase.session import SessionPaths, new_session_id
from capybase.verification import ValidationConfig, VerificationEngine
from capybase.adapters.tests import TestRunner
from capybase.config import Config


# A unit is "resolved" once a candidate is accepted.
@dataclass
class UnitOutcome:
    unit: ConflictUnit
    accepted: CandidateResolution | None = None
    decision: RiskDecision | None = None
    validation: VerificationResult | None = None
    attempts: list[CandidateResolution] = field(default_factory=list)


@dataclass
class StepResult:
    step_index: int
    units_by_path: dict[str, list[ConflictUnit]] = field(default_factory=dict)
    skipped: list[SkippedPath] = field(default_factory=list)
    outcomes: list[UnitOutcome] = field(default_factory=list)
    escalated: bool = False
    reason: str | None = None
    tests_passed: bool | None = None
    continued: bool = False


class Orchestrator:
    def __init__(
        self,
        config: Config,
        *,
        repo: str = ".",
        session_id: str | None = None,
        resolution_engine: ResolutionEngine | None = None,
        stdin_reader: Callable[[str], str] | None = None,
        out: Callable[[str], None] = print,
    ) -> None:
        self.config = config
        self.git = GitBackend(repo)
        self.session_id = session_id or new_session_id()
        self.paths = SessionPaths(self.session_id, repo)
        self.paths.mkdirs()
        self.journal = Journal(self.paths)
        self.extractor = ConflictExtractor(self.git)
        self.context_builder = ContextBuilder(config.policy.context_lines)
        self.resolution_engine = resolution_engine or ResolutionEngine(config.model)
        self.verification = VerificationEngine.default(
            ValidationConfig.from_dict(config.validation.model_dump())
        )
        self.risk = RiskEngine(max_retries_per_unit=config.policy.max_retries_per_unit)
        self.policy = Policy(
            self.git,
            supported_conflict_types=set(config.policy.supported_conflict_types),
            supported_file_kinds=set(config.policy.supported_file_kinds),
        )
        self.tests = TestRunner(self.git, timeout_seconds=config.tests.timeout_seconds)
        self.stdin_reader = stdin_reader or _default_stdin_reader
        self.out = out
        self.step = 0

        # Journal session start + snapshot config.
        self.journal.emit(
            "session_started",
            {
                "session_id": self.session_id,
                "config_source": config.source_path,
                "mode": "orchestrator",
            },
        )
        if config.journal.enabled:
            self.paths.config_copy.write_text(
                _toml_dump_config(config), encoding="utf-8"
            )

    # ==================================================================
    # M1: inspect — no mutation
    # ==================================================================

    def inspect(self) -> StepResult:
        """Detect conflicts, extract units, journal, write review bundle.

        Mutates nothing in the repo (only writes to ``.rebase-agent/``)."""
        self.journal.emit("preflight_started", {})
        if not self.git.rebase_in_progress():
            reason = "no rebase in progress; nothing to inspect"
            self.journal.emit("escalated", {"reason": reason})
            bundle = write_review_bundle(self.paths, reason=reason)
            self.out(f"! {reason}\n  review bundle: {bundle}")
            return StepResult(step_index=self.step, escalated=True, reason=reason)
        self.journal.emit("preflight_passed", {})
        result = self._gather_step()
        write_review_bundle(
            self.paths,
            reason="inspect complete (no mutation performed)",
            step_index=result.step_index,
        )
        self._summarize(result)
        return result

    # ==================================================================
    # M2: manual resolver mode
    # ==================================================================

    def manual(self) -> StepResult:
        """Print each unit, accept a pasted resolution, splice, validate, stage.

        Does not continue the rebase automatically."""
        result = self._gather_step()
        if result.escalated:
            return result
        if not result.units_by_path:
            self.out("no supported conflict units to resolve manually.")
            return result

        for path, units in result.units_by_path.items():
            buffer = units[0].original_worktree_text
            # Re-read the *current* worktree text per file so multi-unit files
            # accumulate splices correctly.
            try:
                buffer = self.git.read_worktree_file(path).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
            resolved_units: list[ConflictUnit] = []
            for unit in units:
                self.out(self._render_unit(unit))
                pasted = self.stdin_reader(
                    "paste the resolved text for this block (Ctrl-D to finish):"
                )
                outcome = self._apply_manual_resolution(unit, pasted, buffer)
                result.outcomes.append(outcome)
                if outcome.accepted is None:
                    result.escalated = True
                    result.reason = f"manual resolution rejected for {unit.unit_id}"
                    write_review_bundle(
                        self.paths,
                        reason=result.reason,
                        step_index=result.step_index,
                        unit=unit,
                        validation=outcome.validation,
                    )
                    self._summarize(result)
                    return result
                resolved_units.append(unit)
                # Splice accepted text into buffer for subsequent units.
                from capybase.adapters.parsers import splice_resolution

                buffer = splice_resolution(
                    unit.original_worktree_text, unit.marker_span, outcome.accepted.resolved_text
                )
            # Write + stage the file.
            self._write_and_stage(path, buffer, result)
        self._summarize(result)
        self.out(
            "manual mode done; files staged. Run `git rebase --continue` "
            "when ready (tests not run in manual mode)."
        )
        return result

    def _apply_manual_resolution(
        self, unit: ConflictUnit, pasted: str, buffer: str
    ) -> UnitOutcome:
        cand = CandidateResolution(
            candidate_id=f"{unit.unit_id}:manual",
            unit_id=unit.unit_id,
            model_name="human",
            prompt_version="manual.v1",
            resolved_text=pasted,
            explanation="provided by human via manual mode",
        )
        validation = self.verification.verify(unit, cand)
        self.journal.emit(
            "candidate_validated",
            {
                "candidate_id": cand.candidate_id,
                "passed": validation.passed,
                "hard_failures": [f.message for f in validation.hard_failures],
            },
            step_index=self.step,
            path=unit.path,
            unit_id=unit.unit_id,
        )
        if self.config.journal.enabled and self.config.journal.store_validations:
            self.journal.store_validation(validation)
        outcome = UnitOutcome(unit=unit, validation=validation, attempts=[cand])
        if not validation.passed:
            for hf in validation.hard_failures:
                self.out(f"  ! rejected: [{hf.validator}] {hf.message}")
            return outcome
        outcome.accepted = cand
        self.journal.emit(
            "candidate_accepted",
            {"candidate_id": cand.candidate_id},
            step_index=self.step,
            path=unit.path,
            unit_id=unit.unit_id,
        )
        return outcome

    # ==================================================================
    # M3: full run
    # ==================================================================

    def run(self) -> StepResult:
        """Full auto loop: resolve → stage → test → continue, with retries."""
        # Preflight.
        self.journal.emit("preflight_started", {})
        if not self.git.rebase_in_progress():
            # Not stopped at a conflict: try to start the rebase? In MVP we
            # require the user to have already hit a conflict (inspect-first).
            reason = "no rebase in progress; start your rebase, then run capybase when it stops on a conflict"
            self.journal.emit("escalated", {"reason": reason})
            bundle = write_review_bundle(self.paths, reason=reason)
            self.out(f"! {reason}\n  review bundle: {bundle}")
            return StepResult(step_index=self.step, escalated=True, reason=reason)
        self.journal.emit("preflight_passed", {})

        # Loop over rebase stops until clean or escalated.
        last: StepResult | None = None
        while True:
            self.step += 1
            head_before = self.git.head_oid()
            self.journal.emit(
                "step_started",
                {"step": self.step, "head_before": head_before},
                step_index=self.step,
                git_head_before=head_before,
            )
            result = self._resolve_step()
            result.step_index = self.step
            last = result
            if result.escalated:
                break
            # Tests gate continue.
            test_ok = self._run_tests("pre_continue", result)
            if not test_ok and self.config.tests.required:
                result.escalated = True
                result.reason = "pre-continue tests failed"
                break
            # Continue rebase.
            cont = self.git.continue_rebase()
            self.journal.emit(
                "step_continued",
                {"returncode": cont.returncode, "stderr": cont.stderr[:500]},
                step_index=self.step,
            )
            result.continued = True
            if not self.git.rebase_in_progress():
                # Rebase finished cleanly.
                head_after = self.git.head_oid()
                self.journal.emit(
                    "session_completed",
                    {"head_after": head_after},
                    git_head_after=head_after,
                )
                self.git.record_step_ref(self.session_id, self.step, head_after)
                self.out(f"✓ rebase complete (session {self.session_id})")
                break
            head_after = self.git.head_oid()
            self.git.record_step_ref(self.session_id, self.step, head_after)
            self.journal.emit(
                "step_ref_created",
                {"ref": self.paths.step_ref(self.step), "oid": head_after},
                step_index=self.step,
                git_head_after=head_after,
            )
        self._summarize(last)
        if last and last.escalated:
            write_review_bundle(
                self.paths,
                reason=last.reason or "escalated",
                step_index=last.step_index,
            )
        return last  # type: ignore[return-value]

    # ------------------------------------------------------------------ step core

    def _resolve_step(self) -> StepResult:
        result = self._gather_step()
        if result.escalated:
            return result
        if not result.units_by_path:
            # No conflicts at this stop: nothing to resolve (rare).
            self.out("no conflict units at this stop; continuing.")
            return result

        for path, units in result.units_by_path.items():
            buffer = units[0].original_worktree_text
            accepted_any = False
            escalated_unit: UnitOutcome | None = None
            for unit in units:
                outcome = self._resolve_unit(unit)
                result.outcomes.append(outcome)
                if outcome.accepted is None:
                    escalated_unit = outcome
                    break
                accepted_any = True
                from capybase.adapters.parsers import splice_resolution

                buffer = splice_resolution(
                    unit.original_worktree_text, unit.marker_span, outcome.accepted.resolved_text
                )
            if escalated_unit is not None:
                result.escalated = True
                result.reason = f"could not resolve {escalated_unit.unit.unit_id}"
                write_review_bundle(
                    self.paths,
                    reason=result.reason,
                    step_index=result.step_index,
                    unit=escalated_unit.unit,
                    candidate=escalated_unit.attempts[-1] if escalated_unit.attempts else None,
                    validation=escalated_unit.validation,
                )
                return result
            if accepted_any:
                self._write_and_stage(path, buffer, result)
        # After staging: assert no unmerged paths remain for our files.
        if self.git.has_unmerged_paths():
            result.escalated = True
            result.reason = "unmerged paths remain after staging"
            write_review_bundle(
                self.paths, reason=result.reason, step_index=result.step_index
            )
        return result

    def _resolve_unit(self, unit: ConflictUnit) -> UnitOutcome:
        outcome = UnitOutcome(unit=unit)
        retry_count = 0
        failures = None
        while True:
            context = self.context_builder.build(unit)
            if self.config.journal.enabled and self.config.journal.store_prompts:
                from capybase.resolution_engine import (
                    PROMPT_RETRY,
                    PROMPT_RESOLVE,
                    build_resolve_prompt,
                    build_retry_prompt,
                )

                pv = PROMPT_RETRY if failures else PROMPT_RESOLVE
                prompt = (
                    build_retry_prompt(unit, context, failures)
                    if failures
                    else build_resolve_prompt(unit, context)
                )
                self.journal.store_prompt(unit.unit_id, retry_count, prompt)
            self.journal.emit(
                "context_built",
                {"token_estimate": context.token_estimate},
                step_index=self.step,
                path=unit.path,
                unit_id=unit.unit_id,
            )

            candidates = self.resolution_engine.propose(unit, context, failures=failures)
            # MVP: take the first candidate (samples=1). Self-consistency later.
            cand = candidates[0]
            outcome.attempts.append(cand)
            if self.config.journal.enabled and self.config.journal.store_candidates:
                self.journal.store_candidate(cand)
            if self.config.journal.enabled and self.config.journal.store_raw_responses:
                self.journal.store_response(unit.unit_id, retry_count, cand.raw_response)
            self.journal.emit(
                "candidate_generated",
                {
                    "candidate_id": cand.candidate_id,
                    "needs_human": cand.needs_human,
                    "confidence": cand.self_reported_confidence,
                },
                step_index=self.step,
                path=unit.path,
                unit_id=unit.unit_id,
            )

            validation = self.verification.verify(unit, cand)
            outcome.validation = validation
            if self.config.journal.enabled and self.config.journal.store_validations:
                self.journal.store_validation(validation)
            self.journal.emit(
                "candidate_validated",
                {
                    "candidate_id": cand.candidate_id,
                    "passed": validation.passed,
                    "hard_failures": [f.message for f in validation.hard_failures],
                },
                step_index=self.step,
                path=unit.path,
                unit_id=unit.unit_id,
            )

            decision = self.risk.decide(
                validation, retry_count=retry_count, failure_kind=cand.failure_kind
            )
            outcome.decision = decision
            self.journal.emit(
                "risk_decision",
                {"action": decision.action, "reasons": decision.reasons},
                step_index=self.step,
                path=unit.path,
                unit_id=unit.unit_id,
            )
            if decision.action == "accept":
                outcome.accepted = cand
                self.journal.emit(
                    "candidate_accepted",
                    {"candidate_id": cand.candidate_id},
                    step_index=self.step,
                    path=unit.path,
                    unit_id=unit.unit_id,
                )
                return outcome
            if decision.action == "escalate":
                self.journal.emit(
                    "candidate_rejected",
                    {"candidate_id": cand.candidate_id, "action": "escalate"},
                    step_index=self.step,
                    path=unit.path,
                    unit_id=unit.unit_id,
                )
                return outcome
            # retry
            self.journal.emit(
                "candidate_rejected",
                {"candidate_id": cand.candidate_id, "action": "retry", "retry_count": retry_count},
                step_index=self.step,
                path=unit.path,
                unit_id=unit.unit_id,
            )
            failures = validation.hard_failures or None
            retry_count += 1

    # ------------------------------------------------------------------ helpers

    def _gather_step(self) -> StepResult:
        result = StepResult(step_index=self.step)
        unmerged = self.git.list_unmerged_paths()
        if not unmerged:
            return result
        decision = self.policy.classify(unmerged)
        result.skipped = decision.skipped
        for sk in decision.skipped:
            self.journal.emit(
                "path_skipped",
                {"path": sk.path, "reason": sk.reason},
                step_index=self.step,
                path=sk.path,
            )
        for entry in decision.supported:
            self.journal.emit(
                "conflict_detected",
                {"path": entry.path, "mode": entry.mode},
                step_index=self.step,
                path=entry.path,
            )
            try:
                units = self.extractor.extract_file_units(
                    entry.path, self.step, self.session_id, unmerged=entry
                )
            except Exception as exc:  # noqa: BLE001
                result.skipped.append(
                    SkippedPath(entry.path, f"extraction error: {exc}")
                )
                continue
            if not units:
                result.skipped.append(
                    SkippedPath(entry.path, "unmerged but no marker blocks")
                )
                continue
            result.units_by_path[entry.path] = units
            for u in units:
                self.journal.emit(
                    "conflict_unit_extracted",
                    {
                        "unit_id": u.unit_id,
                        "unit_kind": u.unit_kind,
                        "language": u.language,
                        "enclosing_symbol": u.enclosing_symbol,
                    },
                    step_index=self.step,
                    path=u.path,
                    unit_id=u.unit_id,
                )
        if result.skipped and not result.units_by_path:
            result.escalated = True
            result.reason = "all conflicted paths are unsupported"
        return result

    def _write_and_stage(self, path: str, buffer: str, result: StepResult) -> None:
        if self.config.journal.enabled and self.config.journal.store_snapshots:
            self.journal.store_snapshot(
                f"{path.replace('/', '__')}.before", buffer
            )
        self.git.write_worktree_file(path, buffer.encode("utf-8"))
        self.journal.emit(
            "file_written",
            {"path": path, "bytes": len(buffer)},
            step_index=self.step,
            path=path,
        )
        if self.config.policy.stage_only_validated_paths:
            self.git.stage_paths([path])
            self.journal.emit(
                "file_staged",
                {"path": path},
                step_index=self.step,
                path=path,
            )

    def _run_tests(self, label: str, result: StepResult) -> bool:
        cmd = getattr(self.config.tests, label) if hasattr(self.config.tests, label) else None
        if not cmd:
            return True
        self.journal.emit("tests_started", {"label": label, "command": cmd}, step_index=self.step)
        run = self.tests.run(cmd)
        self.journal.emit(
            "tests_finished",
            {
                "label": label,
                "passed": run.passed,
                "returncode": run.returncode,
                "timed_out": run.timed_out,
                "stdout_tail": run.stdout[-1000:],
                "stderr_tail": run.stderr[-1000:],
            },
            step_index=self.step,
        )
        result.tests_passed = run.passed
        if not run.passed:
            self.out(f"  ! {label} tests failed (rc={run.returncode})")
        return run.passed

    def _summarize(self, result: StepResult | None) -> None:
        if result is None:
            return
        self.out(f"[step {result.step_index}] summary")
        self.out(f"  units by path: {len(result.units_by_path)}")
        self.out(f"  skipped paths: {len(result.skipped)}")
        self.out(f"  outcomes: {len(result.outcomes)}")
        self.out(f"  escalated: {result.escalated}" + (f" ({result.reason})" if result.reason else ""))
        self.out(f"  continued: {result.continued}")
        self.out(f"  journal: {self.paths.journal}")

    def _render_unit(self, unit: ConflictUnit) -> str:
        return (
            f"\n=== {unit.unit_id} ({unit.path}, {unit.conflict_type}) ===\n"
            f"-- BASE --\n{unit.base.text}\n"
            f"-- CURRENT_UPSTREAM_SIDE --\n{unit.current.text}\n"
            f"-- REPLAYED_COMMIT_SIDE --\n{unit.replayed.text}\n"
        )


def _default_stdin_reader(prompt: str) -> str:
    print(prompt, flush=True)
    chunks: list[str] = []
    try:
        while True:
            line = input()
            chunks.append(line)
    except EOFError:
        pass
    return "\n".join(chunks)


def _toml_dump_config(config: Config) -> str:
    """Minimal TOML serializer for the config snapshot (stdlib only)."""
    lines: list[str] = []

    def emit_section(name: str, d: dict) -> None:
        lines.append(f"[{name}]")
        for k, v in d.items():
            lines.append(f"{k} = {_toml_value(v)}")
        lines.append("")

    emit_section("model", config.model.model_dump())
    emit_section("policy", config.policy.model_dump())
    emit_section("tests", config.tests.model_dump())
    emit_section("validation", config.validation.model_dump())
    emit_section("journal", config.journal.model_dump())
    emit_section("future", config.future.model_dump())
    return "\n".join(lines)


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return '"' + str(v).replace('"', '\\"') + '"'

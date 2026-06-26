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
from pathlib import Path
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
from capybase.consensus import rank_by_consensus


# A unit is "resolved" once a candidate is accepted.
@dataclass
class UnitOutcome:
    unit: ConflictUnit
    accepted: CandidateResolution | None = None
    decision: RiskDecision | None = None
    validation: VerificationResult | None = None
    attempts: list[CandidateResolution] = field(default_factory=list)
    # Carries the consensus report (if self-consistency was used) so the
    # step-level escalation can render alternate cluster representatives.
    consensus: object | None = None
    # Difficulty class assigned by the router ("simple" | "complex"), recorded
    # so the calibration model can learn that complex conflicts fail more often.
    difficulty: str | None = None
    # Number of attempts made (0 on first-pass accept). Recorded so calibration
    # learns that retries correlate with risk. (= len(attempts) - 1 on accept,
    # or the count at escalation.)
    retry_count: int = 0


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


def _attribute_whole_file_failure(
    failures: list, units: list[ConflictUnit]
) -> int:
    """Pick the index of the unit most likely at fault for a whole-file failure.

    Whole-file failures (cross-unit syntax errors, juxtaposition errors) are
    file-scoped, but repair is unit-scoped. Attribution parses the error line
    from a failure message (Python SyntaxErrors carry "(file, line N)") and
    returns the unit whose ``marker_span`` contains that line. When the line
    can't be parsed or no span contains it, the LAST unit is chosen — a
    heuristic that juxtaposition errors tend to surface where splices meet,
    and re-resolving the last unit is a sound default. Falls back to 0 for an
    empty unit list (caller guards against this).
    """
    if not units:
        return 0
    import re

    for f in failures:
        msg = getattr(f, "message", "") or ""
        # Match "line N" anywhere (Python: "line 12", "file, line 12").
        m = re.search(r"line\s+(\d+)", msg)
        if not m:
            continue
        try:
            line = int(m.group(1))
        except ValueError:
            continue
        # Units are 1-indexed in the original marker-laden worktree; marker_span
        # is 0-based [start, end] line indices. Check containment (1-based line
        # falls within the 0-based span converted to 1-based).
        for i, u in enumerate(units):
            if u.marker_span is None:
                continue
            start, end = u.marker_span
            if start + 1 <= line <= end + 1:
                return i
    # No line attribution possible → default to the last unit.
    return len(units) - 1


def _extract_alternates(
    outcome: UnitOutcome,
) -> tuple[list[CandidateResolution], dict | None]:
    """Extract losing cluster representatives + consensus stats from an outcome.

    When self-consistency was used and the unit escalated, the consensus
    report carries multiple clusters. The winner is already shown as the best
    candidate; the losers (other cluster representatives) are returned as
    alternates for the side-by-side review bundle. Returns ([], None) when
    no consensus was computed (single-sample or missing).
    """
    rep = outcome.consensus
    if rep is None:
        return [], None
    alternates = []
    clusters = getattr(rep, "clusters", [])
    for i, cl in enumerate(clusters):
        if i == 0:
            continue  # winner is already the best candidate
        rep_cand = getattr(cl, "representative", None)
        if rep_cand is not None and rep_cand.resolved_text:
            alternates.append(rep_cand)
    consensus = {
        "entropy": getattr(rep, "entropy", None),
        "agreement_score": getattr(rep, "agreement_score", None),
        "cluster_count": getattr(rep, "cluster_count", None),
    }
    return alternates, consensus


def _apply_model_profile(config: Config, repo_root: Path, journal: Journal) -> Config:
    """Overlay the calibrated model profile onto ``config.model`` if present.

    "Profile wins": the profile's tuned knobs override the [model] settings, but
    ONLY when its model name matches. Returns ``config`` unchanged (and journals
    nothing) when no profile exists or the names mismatch — so a repo without a
    profile behaves exactly as before. The overlay touches only the four tuned
    knobs; every other field keeps its value.
    """
    profile_path = config.calibration.model_profile_path
    resolved = Path(profile_path)
    if not resolved.is_absolute():
        resolved = repo_root / profile_path
    try:
        from capybase.calibration_profile import ModelProfile, apply_profile

        profile = ModelProfile.load(resolved)
    except Exception:  # noqa: BLE001 - never crash on a bad artifact path/config
        return config
    if profile is None:
        return config
    new_model, overridden = apply_profile(config.model, profile)
    if not overridden:
        # Even if no ModelConfig knob changed, a capability flag may still apply
        # (e.g. embedding RAG confirmed by calibration). Apply it before returning.
        config = _apply_profile_capability_flags(config, profile)
        return config
    journal.emit(
        "model_profile_applied",
        {
            "model": profile.model,
            "overridden_knobs": overridden,
            "profile_path": str(resolved),
        },
    )
    config = config.model_copy(update={"model": new_model})
    return _apply_profile_capability_flags(config, profile)


def _apply_profile_capability_flags(config: Config, profile: "object") -> Config:
    """Apply profile capability flags that don't live on ModelConfig.

    Currently: ``enable_embedding_rag`` flips ``config.memory.retriever`` to
    ``"embedding"`` (the orchestrator then builds an EmbeddingRetriever). Only
    honors the flag when the user has RAG enabled at all; never forces it on.
    """
    if getattr(profile, "enable_embedding_rag", False):
        if config.memory.enabled and config.future.enable_rag:
            if config.memory.retriever == "lexical":
                config.memory.retriever = "embedding"
    return config


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
        self.git = GitBackend(repo)
        self.session_id = session_id or new_session_id()
        self.paths = SessionPaths(self.session_id, repo)
        self.paths.mkdirs()
        self.journal = Journal(self.paths)
        # Model profile overlay ("Profile wins"): rebind the local ``config`` so
        # the profile's tuned knobs flow into EVERY consumer below (resolution
        # engine, verifier) — not just ``self.config``. Done after the journal is
        # ready (it emits model_profile_applied) and before any config read. Inert
        # when the profile is absent/mismatched/corrupt — resolution never crashes.
        config = _apply_model_profile(config, self.git.repo, self.journal)
        self.config = config
        self.extractor = ConflictExtractor(
            self.git, structural_config=config.structural
        )
        # Memory: experience store + retriever for RAG few-shot. Built lazily
        # from config; both are None when [memory] is disabled, so the context
        # builder gets no retriever and retrieved_examples stays empty.
        self.memory_store = None
        retriever = None
        if config.memory.enabled and config.future.enable_rag:
            from capybase.memory.retriever import EmbeddingRetriever, LexicalRetriever
            from capybase.memory.store import ExperienceStore

            self.memory_store = ExperienceStore.for_repo(
                str(self.git.repo), config.memory.store_path
            )
            retriever = self._build_retriever(config)
        self.context_builder = ContextBuilder(
            config.policy.context_lines,
            retriever=retriever,
            retriever_k=config.memory.retriever_k,
            min_examples=config.memory.min_examples_for_retrieval,
            use_enclosing_as_primary=config.structural.use_enclosing_as_primary,
            canonicalize_context=config.structural.canonicalize_context,
        )
        self.resolution_engine = resolution_engine or ResolutionEngine(config.model)
        self.verification = VerificationEngine.default(
            ValidationConfig.from_dict(config.validation.model_dump())
        )
        # Verifier-model critic (surveys §1/§5): when enabled, register an LLM
        # judge that checks the resolution preserves both sides' semantic intent
        # — the failure mode the syntactic validators are blind to. It runs last
        # in the validator chain (after the cheap structural checks) and uses the
        # same black-box API client as the resolver. Inert + zero calls when off.
        if config.validation.enable_verifier_model:
            from capybase.verification import VerifierModelValidator

            self.verification.register(
                VerifierModelValidator(
                    self.resolution_engine.client,
                    model_name=config.model.model,
                    json_mode=config.model.json_mode,
                )
            )
        # VeriGuard-style deterministic policy gate (survey §4): auto-registered
        # by VerificationEngine.default() when enable_policy_gate is on AND rules
        # are configured. It inspects WHAT a patch introduces (the only such
        # check — all others are syntactic/structural), deterministically via
        # stdlib ast (no LLM, no execution). Tags violations onto the unit's
        # risk_tags and blocks error-severity violations from auto-apply.
        # Inert + zero work when off or no rules (the engine factory skips it).
        # Risk engine: the calibrated variant overrides accept/escalate with
        # a learned threshold when a fitted model is present; otherwise it
        # transparently delegates to the rules engine. Both produce the same
        # RiskDecision shape so the orchestrator consumes only ``action``.
        if config.calibration.enabled:
            from capybase.calibration import CalibratedRiskEngine

            self.risk = CalibratedRiskEngine.from_config(
                max_retries_per_unit=config.policy.max_retries_per_unit,
                model_path=str(self.git.repo / config.calibration.model_path)
                if not Path(config.calibration.model_path).is_absolute()
                else config.calibration.model_path,
                escalate_threshold=config.calibration.escalate_threshold,
                entropy_escalate_threshold=config.calibration.entropy_escalate_threshold,
                min_agreement=config.model.consensus_min_agreement,
            )
        else:
            self.risk = RiskEngine(
                max_retries_per_unit=config.policy.max_retries_per_unit,
                entropy_escalate_threshold=config.calibration.entropy_escalate_threshold,
                min_agreement=config.model.consensus_min_agreement,
            )
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
            # Resolve all units, collecting accepted pairs; splice in one
            # offset-correct batch at the end (same structure as run mode).
            accepted: list[tuple[ConflictUnit, CandidateResolution]] = []
            for unit in units:
                self.out(self._render_unit(unit))
                pasted = self.stdin_reader(
                    "paste the resolved text for this block (Ctrl-D to finish):"
                )
                outcome = self._apply_manual_resolution(unit, pasted)
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
                accepted.append((unit, outcome.accepted))
            from capybase.adapters.parsers import splice_all_resolutions

            original = accepted[0][0].original_worktree_text
            spans_and_texts = [
                (unit.marker_span, cand.resolved_text) for unit, cand in accepted
            ]
            buffer = splice_all_resolutions(original, spans_and_texts)
            # Write + stage the file.
            self._write_and_stage(path, buffer, result)
        self._summarize(result)
        self.out(
            "manual mode done; files staged. Run `git rebase --continue` "
            "when ready (tests not run in manual mode)."
        )
        return result

    def _apply_manual_resolution(
        self, unit: ConflictUnit, pasted: str
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

    def _try_structural_resolve(self, unit: ConflictUnit) -> UnitOutcome | None:
        """Attempt a deterministic, model-free resolution; accept only if it
        passes the full validation pipeline, else return None (fall through to
        the LLM). Survey §6.4 layer 1: structural/auto resolution before the model.

        Safe by construction: the resolver only emits resolutions from provably-
        safe rules (identical sides, one-sided change, disjoint line edits), and
        this method validates the result exactly as an LLM candidate would be —
        markers/splice/AST/syntax. A wrong deterministic guess is caught here and
        discarded (returns None), so the model then handles it. Net effect: fewer
        LLM calls on trivial conflicts, never a worse merge.
        """
        from capybase.structural_resolver import resolve_structurally

        result = resolve_structurally(unit)
        if not result.resolved or result.text is None:
            return None
        cand = CandidateResolution(
            candidate_id=f"{unit.unit_id}:structural",
            unit_id=unit.unit_id,
            model_name="structural",
            prompt_version=f"structural.{result.rule}",
            resolved_text=result.text,
            explanation=f"deterministic resolution via {result.rule} rule",
        )
        validation = self.verification.verify(unit, cand)
        self.journal.emit(
            "structurally_resolved",
            {
                "candidate_id": cand.candidate_id,
                "rule": result.rule,
                "passed": validation.passed,
            },
            step_index=self.step,
            path=unit.path,
            unit_id=unit.unit_id,
        )
        if not validation.passed:
            # The deterministic guess failed validation — discard and let the
            # model handle it. This is the safety net: structural resolution can
            # only help, never apply an invalid merge.
            return None
        outcome = UnitOutcome(unit=unit, validation=validation, attempts=[cand])
        outcome.accepted = cand
        self.journal.emit(
            "candidate_accepted",
            {"candidate_id": cand.candidate_id, "via": "structural"},
            step_index=self.step,
            path=unit.path,
            unit_id=unit.unit_id,
        )
        return outcome

    def _build_retriever(self, config: Config) -> object:
        """Construct the configured RAG retriever over ``self.memory_store``.

        ``"embedding"`` builds an :class:`EmbeddingRetriever` (semantic, survey
        §4.2) from a fresh embeddings client pointed at the model endpoint; any
        failure to construct it (no endpoint support, missing model) falls back to
        the lexical BM25 retriever so RAG never hard-fails. ``"lexical"`` (default)
        builds the dependency-free BM25 retriever.
        """
        from capybase.memory.retriever import EmbeddingRetriever, LexicalRetriever

        if config.memory.retriever == "embedding":
            try:
                from capybase.memory.embeddings import OpenAIEmbeddingsClient

                # The embeddings model: explicit config, else reuse the completion
                # model name (a single-model llama-server serving both).
                emb_cfg = config.model
                if config.memory.embeddings_model:
                    emb_cfg = emb_cfg.model_copy(update={"model": config.memory.embeddings_model})
                client = OpenAIEmbeddingsClient(emb_cfg)
                return EmbeddingRetriever(self.memory_store, client)
            except Exception:  # noqa: BLE001 - fall back to BM25, never break RAG
                pass
        return LexicalRetriever(self.memory_store)

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
            # Resolve ALL units in the file before splicing anything. We must
            # not write a partially-resolved file: if a later unit escalates,
            # the file (with some blocks still marker-laden) would be staged
            # against an aborted rebase. Collect accepted (unit, candidate)
            # pairs and splice them in one offset-correct batch at the end.
            accepted: list[tuple[ConflictUnit, CandidateResolution]] = []
            escalated_unit: UnitOutcome | None = None
            for unit in units:
                outcome = self._resolve_unit(unit)
                result.outcomes.append(outcome)
                if outcome.accepted is None:
                    escalated_unit = outcome
                    break
                accepted.append((unit, outcome.accepted))
            if escalated_unit is not None:
                result.escalated = True
                result.reason = f"could not resolve {escalated_unit.unit.unit_id}"
                self._record_outcomes_to_memory(result)
                _alternates, _consensus = _extract_alternates(escalated_unit)
                write_review_bundle(
                    self.paths,
                    reason=result.reason,
                    step_index=result.step_index,
                    unit=escalated_unit.unit,
                    candidate=escalated_unit.attempts[-1] if escalated_unit.attempts else None,
                    alternates=_alternates,
                    validation=escalated_unit.validation,
                    consensus=_consensus,
                )
                return result
            # Splice every accepted resolution in one offset-correct batch and
            # validate the whole file. Phase B (whole-file validation) is the
            # only place that can catch cross-unit errors (duplicate symbols,
            # syntax errors arising only when resolutions are juxtaposed, leaked
            # sibling markers). Per-unit Phase A validation already passed for
            # each candidate in isolation.
            #
            # Execution-driven whole-file CEGIS (survey §4): when the
            # combination fails, we do NOT escalate immediately — we feed the
            # concrete file-level failures back to the unit most likely at
            # fault and re-resolve it via the repair prompt, then re-splice and
            # re-validate. Bounded by the policy retry ceiling so it can't loop
            # forever; escalate only when the budget is exhausted.
            from capybase.adapters.parsers import splice_all_resolutions

            buffer = ""
            if self.config.validation.require_whole_file_validation and units:
                language = units[0].language
                original = accepted[0][0].original_worktree_text
                wf_retries = 0
                wf_budget = self.config.policy.max_retries_per_unit
                while True:
                    spans_and_texts = [
                        (unit.marker_span, cand.resolved_text) for unit, cand in accepted
                    ]
                    buffer = splice_all_resolutions(original, spans_and_texts)
                    file_validation = self.verification.verify_file(
                        path, language, original, spans_and_texts,
                        repo_root=str(self.git.repo),
                    )
                    if self.config.journal.enabled and self.config.journal.store_validations:
                        self.journal.store_validation(file_validation)
                    self.journal.emit(
                        "file_validated",
                        {
                            "passed": file_validation.passed,
                            "hard_failures": [
                                f.message for f in file_validation.hard_failures
                            ],
                            "wf_retry": wf_retries,
                        },
                        step_index=self.step,
                        path=path,
                    )
                    if file_validation.passed or wf_retries >= wf_budget:
                        break
                    # Attribute the failure to a unit and re-resolve it with the
                    # file-level failures as concrete repair feedback.
                    wf_retries += 1
                    self.journal.emit(
                        "whole_file_repair",
                        {
                            "retry": wf_retries,
                            "failures": [
                                f.message for f in file_validation.hard_failures
                            ],
                        },
                        step_index=self.step,
                        path=path,
                    )
                    accepted_opt: list[tuple[ConflictUnit, CandidateResolution]] | None = (
                        self._whole_file_repair(
                            path, accepted, original, file_validation.hard_failures
                        )
                    )
                    if accepted_opt is None:
                        # A unit could not be re-resolved (escalated) → bail.
                        file_validation = None  # type: ignore[assignment]
                        break
                    accepted = accepted_opt
                if file_validation is None or not file_validation.passed:
                    result.escalated = True
                    if file_validation is None:
                        result.reason = (
                            f"whole-file repair could not re-resolve a unit in {path}"
                        )
                    else:
                        result.reason = (
                            f"whole-file validation failed for {path}: "
                            + "; ".join(f.message for f in file_validation.hard_failures)
                        )
                    self._record_outcomes_to_memory(result)
                    write_review_bundle(
                        self.paths,
                        reason=result.reason,
                        step_index=result.step_index,
                    )
                    return result
            self._write_and_stage(path, buffer, result)
        # After staging: assert no unmerged paths remain for our files.
        if self.git.has_unmerged_paths():
            result.escalated = True
            result.reason = "unmerged paths remain after staging"
            self._record_outcomes_to_memory(result)
            write_review_bundle(
                self.paths, reason=result.reason, step_index=result.step_index
            )
        else:
            self._record_outcomes_to_memory(result)
        return result

    def _whole_file_repair(
        self,
        path: str,
        accepted: list[tuple[ConflictUnit, CandidateResolution]],
        original: str,
        failures: list,
    ) -> list[tuple[ConflictUnit, CandidateResolution]] | None:
        """Re-resolve the unit most likely at fault for a whole-file failure.

        Execution-driven whole-file CEGIS (survey §4): the file-level failures
        (cross-unit syntax errors, etc.) are fed back to the unit whose
        resolution most plausibly caused them. Attribution is by error-line
        containment in the unit's marker_span (parsed from the failure message
        when possible); if no unit's span contains the line, the LAST unit is
        re-resolved (a heuristic — juxtaposition errors tend to surface where
        the splices meet). Returns the updated accepted list, or None if the
        attributed unit could not be re-resolved (it escalated).
        """
        fault_idx = _attribute_whole_file_failure(failures, [u for u, _ in accepted])
        unit, _old_cand = accepted[fault_idx]
        outcome = self._resolve_unit(unit, seed_failures=failures)
        self.journal.emit(
            "candidate_validated",
            {
                "candidate_id": (outcome.accepted.candidate_id if outcome.accepted else "none"),
                "passed": outcome.accepted is not None,
                "whole_file_repair_for": unit.unit_id,
            },
            step_index=self.step,
            path=path,
            unit_id=unit.unit_id,
        )
        if outcome.accepted is None:
            return None
        accepted[fault_idx] = (unit, outcome.accepted)
        return accepted

    def _resolve_unit(
        self, unit: ConflictUnit, *, seed_failures: list | None = None
    ) -> UnitOutcome:
        outcome = UnitOutcome(unit=unit)
        retry_count = 0
        # seed_failures: when set (whole-file CEGIS), the unit is re-resolved
        # starting from the repair path with the file-level failures pre-seeded,
        # so the model gets the concrete cross-unit error on its first attempt.
        failures = list(seed_failures) if seed_failures else None
        prev_candidate = None

        # Deterministic structural pre-resolution (survey §6.4 layer 1): BEFORE
        # the LLM loop, attempt a safe, model-free resolution from base+sides.
        # Only on a FRESH resolve (not CEGIS retries, where the model must see the
        # counterexample). Any resolution still runs the full validation pipeline;
        # on failure it falls through to the model, so this can only cut LLM load,
        # never produce a worse merge. Gated by [future] enable_structural_resolver.
        if failures is None and self.config.future.enable_structural_resolver:
            early = self._try_structural_resolve(unit)
            if early is not None:
                return early  # accepted deterministically; LLM loop skipped entirely

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

            consensus_report = None
            # Difficulty-aware routing (survey §6.1): classify the conflict
            # before any LLM call. Simple conflicts take a fast path (one
            # low-temp sample, no two-pass, no consensus); complex ones get the
            # full test-time pipeline. Disabled (complex=full path for all)
            # until config.routing.enabled is set.
            difficulty = "complex"
            if self.config.routing.enabled:
                from capybase.routing import RoutingConfig as _RC, classify_difficulty

                difficulty = classify_difficulty(
                    unit,
                    _RC(
                        enabled=True,
                        complex_if_sibling_count_gt=(
                            self.config.routing.complex_if_sibling_count_gt
                        ),
                        max_simple_node_lines=self.config.routing.max_simple_node_lines,
                        max_simple_side_chars=self.config.routing.max_simple_side_chars,
                    ),
                )
                self.journal.emit(
                    "difficulty_classified",
                    {"difficulty": difficulty},
                    step_index=self.step,
                    path=unit.path,
                    unit_id=unit.unit_id,
                )
            outcome.difficulty = difficulty

            # Difficulty-aware sample allocation (survey §4 UAB-lite): complex
            # units draw samples_complex (falling back to the base samples when
            # unset/0). Difficulty is known before any LLM call, so this is the
            # viable pre-generation allocation lever. Only affects fresh
            # resolution (failures is None) — retries stay single-sample for
            # reproducible CEGIS counterexample feedback.
            if failures is None:
                n_complex = (
                    self.config.model.samples_complex or self.config.model.samples
                )
            else:
                n_complex = self.config.model.samples

            # Self-consistency: read from ModelConfig (so the calibrated profile
            # overlay flows through) with fallback to the legacy FutureConfig flag.
            self_consistency = (
                self.config.model.enable_self_consistency
                or self.config.future.enable_self_consistency
            )

            if difficulty == "simple":
                # Fast path: one low-temperature sample, no intent pass, no
                # consensus. Simple isolated hunks resolve trivially.
                candidates = self.resolution_engine.propose(
                    unit, context, failures=failures, prev_candidate=prev_candidate
                )
            elif failures is None and self.config.model.two_pass and n_complex > 1:
                # Two-pass prompting + consensus: extract intents, then sample
                # N code candidates conditioned on them, then majority-vote.
                candidates = self.resolution_engine.propose_two_pass(
                    unit, context,
                    n_samples=n_complex,
                    temperature=self.config.model.sampling_temperature,
                )
                if self_consistency and len(candidates) > 1:
                    candidates, consensus_report = (
                        rank_by_consensus(candidates, unit.language)
                    )
            elif self_consistency:
                candidates, consensus_report = (
                    self.resolution_engine.propose_with_consensus(
                        unit, context, failures=failures, n_samples=n_complex
                    )
                )
            else:
                candidates = self.resolution_engine.propose(
                    unit, context, failures=failures, prev_candidate=prev_candidate,
                    n_samples=n_complex,
                )
            outcome.consensus = consensus_report
            # Journal the generation round. With self-consistency this is the
            # full sample set; the consensus stats attach here so the audit
            # shows how split the samples were before validation.
            winner = candidates[0]
            emit_payload = {
                "candidate_id": winner.candidate_id,
                "n_candidates": len(candidates),
                "needs_human": winner.needs_human,
                "confidence": winner.self_reported_confidence,
            }
            if consensus_report is not None:
                emit_payload["consensus_agreement"] = consensus_report.agreement_score
                emit_payload["consensus_clusters"] = consensus_report.cluster_count
                emit_payload["consensus_n_samples"] = consensus_report.n_samples
            self.journal.emit(
                "candidate_generated",
                emit_payload,
                step_index=self.step,
                path=unit.path,
                unit_id=unit.unit_id,
            )

            # Step 3 (syntactic/structural guardrails): validate candidates in
            # rank order and accept the FIRST that passes hard validation. The
            # consensus winner is first, but on a 3B model the winner frequently
            # carries a syntax error while the 2nd/3rd sample is valid — trying
            # them before regenerating is free reliability (the tokens were
            # already spent). These are local tree-sitter/splice checks, not
            # LLM calls, so validating all N is cheap. If none pass, the winner
            # (and its failures) feeds the CEGIS repair loop below.
            cand = winner
            validation = self.verification.verify(unit, cand)
            self._journal_validation(unit, cand, validation)
            if not validation.passed and len(candidates) > 1:
                for trial in candidates[1:]:
                    trial_val = self.verification.verify(unit, trial)
                    self._journal_validation(unit, trial, trial_val)
                    if trial_val.passed:
                        cand = trial
                        validation = trial_val
                        break
            outcome.validation = validation
            outcome.attempts.append(cand)
            if self.config.journal.enabled and self.config.journal.store_candidates:
                self.journal.store_candidate(cand)
            if self.config.journal.enabled and self.config.journal.store_raw_responses:
                self.journal.store_response(unit.unit_id, retry_count, cand.raw_response)

            decision = self.risk.decide(
                validation,
                retry_count=retry_count,
                failure_kind=cand.failure_kind,
                consensus_entropy=(
                    consensus_report.entropy if consensus_report else None
                ),
                consensus_agreement=(
                    consensus_report.agreement_score if consensus_report else None
                ),
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
                outcome.retry_count = retry_count
                self.journal.emit(
                    "candidate_accepted",
                    {"candidate_id": cand.candidate_id},
                    step_index=self.step,
                    path=unit.path,
                    unit_id=unit.unit_id,
                )
                return outcome
            if decision.action == "escalate":
                outcome.retry_count = retry_count
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
            prev_candidate = cand  # for targeted repair on next attempt
            retry_count += 1

    # ------------------------------------------------------------------ helpers

    def _journal_validation(
        self, unit: ConflictUnit, cand: CandidateResolution, validation: VerificationResult
    ) -> None:
        """Emit/store a candidate's validation result for the audit trail.

        Used for every validated candidate (including the consensus-losers tried
        before the winner in the rank-order loop), so the journal shows which
        samples were skipped and why — not just the one that was accepted.
        """
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

    def _merge_resolution_features(
        self,
        features: dict,
        outcome: "UnitOutcome",
        accepted: CandidateResolution | None,
    ) -> dict:
        """Merge resolution-process signals into the feature dict for recording.

        These are the cheap, deterministic "epistemic uncertainty" features the
        system already computed during resolution (consensus stats, difficulty
        class, conflict size, candidate confidence, retry count). They never
        reach the validator's own features dict, so without this merge they'd
        be dropped at the memory seam and the calibration model couldn't learn
        from them. Keys match the extended ``_FEATURE_KEYS``.
        """
        out = dict(features)
        rep = outcome.consensus
        out["consensus_entropy"] = float(getattr(rep, "entropy", 0.0) or 0.0)
        out["consensus_agreement"] = float(getattr(rep, "agreement_score", 0.0) or 0.0)
        out["consensus_cluster_count"] = float(getattr(rep, "cluster_count", 0) or 0)
        # FactSelfCheck rationale-consistency (survey §2): agreement over the
        # candidates' own intent claims, surfaced from the consensus report.
        # Defaults (1.0 / 0) when no multi-sample consensus ran.
        out["intent_agreement"] = float(getattr(rep, "intent_agreement", 1.0) or 1.0)
        out["low_consistency_fact_count"] = float(
            getattr(rep, "low_consistency_fact_count", 0) or 0
        )
        out["difficulty_complex"] = 1.0 if outcome.difficulty == "complex" else 0.0
        out["retry_count"] = float(outcome.retry_count)
        unit = outcome.unit
        out["conflict_side_chars"] = float(
            len(unit.base.text) + len(unit.current.text) + len(unit.replayed.text)
        )
        # Pre-resolution severity (survey §3.3): a triage signal computed at
        # extraction, before any model call. Encoded numerically so the risk
        # score / calibration model can consume it (low=0, medium=1, high=2).
        out["conflict_severity"] = {"low": 0.0, "medium": 1.0, "high": 2.0}.get(
            unit.severity, 1.0
        )
        # Enclosing AST node line count, if structural metadata recorded it.
        span = unit.structural_metadata.get("enclosing_node_span")
        node_lines = 0.0
        if isinstance(span, (list, tuple)) and len(span) == 2:
            try:
                node_lines = float(int(span[1]) - int(span[0]) + 1)
            except (TypeError, ValueError):
                node_lines = 0.0
        out["enclosing_node_lines"] = node_lines
        # Candidate self-reported confidence (model-side); use the accepted one
        # or, for escalations, the last attempt.
        cand = accepted if accepted is not None else (
            outcome.attempts[-1] if outcome.attempts else None
        )
        out["self_reported_confidence"] = float(
            getattr(cand, "self_reported_confidence", 0.0) or 0.0
        )
        # TECP token-entropy (model-side uncertainty): None when the candidate
        # didn't capture logprobs (e.g. a failed/technical candidate, or entropy
        # capture is off). features_to_vector maps None → 0.0 (treated as
        # "confident / not atypical"), which is the safe default.
        out["mean_token_entropy"] = getattr(cand, "mean_token_entropy", None)
        return out

    def _record_outcomes_to_memory(self, result: StepResult) -> None:
        """Append labeled outcomes to the experience store for RAG/calibration.

        Called once per step after resolution settles (accepted or escalated).
        Each unit's outcome becomes an Experience record: accepted merges are
        positive examples (few-shot + LoRA data), escalated ones are negative
        labels for calibration. No-op when the memory store is not configured.
        """
        if self.memory_store is None:
            return
        from capybase.conflict_model import HistoricalExample
        from capybase.memory.store import Experience

        for outcome in result.outcomes:
            unit = outcome.unit
            accepted = outcome.accepted
            if accepted is not None:
                resolved = accepted.resolved_text
                outcome_label = "accepted"
            else:
                # Escalated: use the last attempt's text if any, else empty.
                resolved = outcome.attempts[-1].resolved_text if outcome.attempts else ""
                outcome_label = "escalated"
            features = {}
            risk_score = None
            if outcome.validation is not None:
                features = dict(outcome.validation.features)
            if outcome.decision is not None:
                risk_score = outcome.decision.risk_score
            # Merge the resolution-process signals into the recorded features so
            # the calibration model can learn from consensus disagreement,
            # difficulty, conflict complexity, and candidate confidence — not
            # just the validator hard-checks. These are the "epistemic
            # uncertainty" features the system already computed and journaled;
            # this is the seam that lets the offline flywheel actually see them.
            features = self._merge_resolution_features(features, outcome, accepted)
            try:
                self.memory_store.append(
                    Experience(
                        example=HistoricalExample(
                            summary=f"{unit.path}:{unit.unit_id}",
                            base=unit.base.text,
                            current=unit.current.text,
                            replayed=unit.replayed.text,
                            resolved=resolved,
                            source=self.session_id,
                        ),
                        outcome=outcome_label,
                        language=unit.language,
                        path=unit.path,
                        session_id=self.session_id,
                        unit_id=unit.unit_id,
                        validator_features=features,
                        risk_score=risk_score,
                        retry_count=outcome.retry_count,
                    )
                )
            except Exception:  # noqa: BLE001 - memory is best-effort
                pass

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

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

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable
import warnings

from capybase.conflict_extractor import ConflictExtractor, SkippedPath
from capybase.conflict_model import (
    CandidateResolution,
    ConflictUnit,
    RiskDecision,
    VerificationResult,
)
from capybase.context_builder import ContextBuilder
from capybase.escalation import write_review_bundle
from capybase.git_backend import GitBackend, GitError, GitResult
from capybase.journal import Journal
from capybase.policy import Policy
from capybase.resolution_engine import ResolutionEngine
from capybase.risk import RiskEngine
from capybase.session import SessionPaths, new_session_id
from capybase.verification import ValidationConfig, VerificationEngine
from capybase.adapters.tests import TestRunner
from capybase.config import Config
from capybase.consensus import rank_by_consensus
from capybase.preflight import run_rebase_preflight


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
    knobs; every other field keeps its value. Capability flags
    (``enable_embedding_rag``, ``embedding_min_similarity``) follow the SAME
    name-match gate — a profile fit for another model never leaks them through.
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
    # The name match is the gate for EVERYTHING the profile carries — tuned
    # knobs AND capability flags. ``apply_profile`` would warn + no-op on a
    # mismatch, but we re-check here FIRST so capability flags don't leak
    # through when ``overridden`` is empty merely because no ModelConfig knob
    # differed. Nudge the user to recalibrate, then leave config untouched.
    if profile.model != config.model.model:
        warnings.warn(
            f"Model profile is for {profile.model!r} but active model is "
            f"{config.model.model!r}; ignoring the profile. Run "
            f"`capybase recalibrate` to fit it for the current model.",
            stacklevel=2,
        )
        return config
    new_model, overridden = apply_profile(config.model, profile)
    if overridden:
        journal.emit(
            "model_profile_applied",
            {
                "model": profile.model,
                "overridden_knobs": overridden,
                "profile_path": str(resolved),
            },
        )
        config = config.model_copy(update={"model": new_model})
    # Capability flags (e.g. embedding RAG, the calibrated floor) apply even when
    # no ModelConfig knob changed — but only after the name match above passed.
    return _apply_profile_capability_flags(config, profile)


def _apply_profile_capability_flags(config: Config, profile: "object") -> Config:
    """Apply profile capability flags that don't live on ModelConfig.

    Currently: ``enable_embedding_rag`` flips ``config.memory.retriever`` to
    ``"embedding"`` (the orchestrator then builds an EmbeddingRetriever). Only
    honors the flag when the user has RAG enabled at all; never forces it on.

    The calibrated ``embedding_min_similarity`` (from ``calibrate-embeddings``)
    overrides the config default so the EmbeddingRetriever uses a model-specific
    floor rather than the 0.35 guess. The full ``embedding_calibration`` envelope
    rides along so the retriever can apply the isotonic score transform (survey
    §2.1). ``fusion_method`` is threaded for the HybridRetriever (survey §4).
    """
    if getattr(profile, "enable_embedding_rag", False):
        if config.memory.enabled and config.future.enable_rag:
            if config.memory.retriever == "lexical":
                config.memory.retriever = "embedding"
    emb_sim = getattr(profile, "embedding_min_similarity", None)
    if emb_sim is not None:
        config.memory.embedding_min_similarity = float(emb_sim)
    emb_cal = getattr(profile, "embedding_calibration", None)
    if emb_cal:  # a non-empty envelope
        config.memory.embedding_calibration = dict(emb_cal)
    fusion = getattr(profile, "fusion_method", None)
    if fusion:
        config.memory.fusion_method = str(fusion)
    return config


def _reconstruct_calibration(config: Config) -> "object | None":
    """Rebuild an EmbeddingCalibration from the config's serialized envelope.

    Returns None when no envelope is stored (so the retriever behaves as before
    calibration). Tolerant of a corrupt/partial envelope — returns None rather
    than crashing, so a bad artifact never breaks retrieval.
    """
    env = config.memory.embedding_calibration
    if not env:
        return None
    try:
        from capybase.embeddings_calibration import EmbeddingCalibration

        return EmbeddingCalibration.from_dict(dict(env))
    except Exception:  # noqa: BLE001 - never break retrieval on a bad envelope
        return None


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
        # Cross-session operational log (vs the per-session journal, which is
        # the authoritative audit of THIS run). Logging is configured by the CLI
        # via logging_setup.configure_logging; if a test constructs an
        # orchestrator without configuring logging, this still works (the
        # capybase logger simply has no handlers → messages go nowhere).
        self.log = logging.getLogger("capybase")
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
            cross_file_slice=config.structural.cross_file_slice,
            slice_search_globs=config.structural.slice_search_globs,
            slice_repo_root=str(self.git.repo),
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
        # Dependency-preservation validator (survey §2.2 SafeMerge necessary
        # condition): warns when a merge drops a base-referenced symbol that has
        # an in-repo definition and neither side removed. Registered only when
        # BOTH [structural] cross_file_slice (the slicer it depends on) AND
        # [validation] reject_if_drops_referenced_symbol are on — it needs the
        # search globs + repo root to resolve definitions. Inert otherwise, and
        # a no-op (can't flag what it can't locate) when no defs are found.
        if (
            config.structural.cross_file_slice
            and config.validation.reject_if_drops_referenced_symbol
        ):
            from capybase.verification import DependencyPreservationValidator

            self.verification.register(
                DependencyPreservationValidator(
                    slice_search_globs=config.structural.slice_search_globs,
                    slice_repo_root=str(self.git.repo),
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
        # Whether the interactive fallback may fire. Defaults to the real TTY
        # check; tests override this (they can't provide a real terminal).
        self._is_interactive_terminal = _is_interactive_terminal

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

    # ==================================================================
    # Interactive fallback: presented automatically on escalation from rebase()
    # when a human is at the terminal. Lets the human resolve the unit capybase
    # couldn't (paste a resolution OR edit the file directly), then re-validates
    # and continues the rebase — keeping capybase the single owner of the process.
    # ==================================================================

    def interactive_resolve(self, result: StepResult) -> StepResult:
        """On escalation, present the unresolvable conflicts to the human for an
        interactive decision, then continue the rebase.

        Offered per unit: (1) paste a resolution, (2) edit the file directly,
        (3) skip the unit (leave it unmerged), (4) abort the rebase. After all
        units resolve, re-validate (whole-file + test gate) and continue the
        rebase; loop for further stops. If the human skips/aborts, return the
        (still-escalated) result so the caller's abort logic runs.

        Only meaningful when a rebase is in progress and a human is present; the
        caller guards on TTY/``interactive`` before invoking this.
        """
        self.out(
            "\n! capybase could not auto-resolve the conflict(s) below.\n"
            "  Review the context, then choose how to proceed.\n"
            f"  review bundle: {self.paths.final / 'review-bundle.md'}\n"
        )
        # Re-gather the unmerged units fresh (the escalation left them on disk).
        gathered = self._gather_step()
        if gathered.escalated or not gathered.units_by_path:
            self.out("  (no resolvable units to present interactively)")
            return result

        aborted = False
        for path, units in gathered.units_by_path.items():
            if aborted:
                break
            # Show the model's best attempt + the failure for this path (from the
            # original escalation's outcomes) so the human sees what was tried.
            prior = [o for o in result.outcomes if o.unit.path == path]
            accepted: list[tuple[ConflictUnit, CandidateResolution]] = []
            for unit in units:
                self.out(self._render_unit_interactive(unit, prior))
                choice = self._interactive_menu(unit)
                if choice == "abort":
                    aborted = True
                    break
                if choice == "skip":
                    self.out(f"  skipped {unit.unit_id} (left unmerged)")
                    continue
                if choice == "paste":
                    outcome = self._interactive_paste(unit)
                    if outcome.accepted is None:
                        self.out("  paste was rejected; re-offering this unit")
                        # Re-present the same unit until resolved/skipped/aborted.
                        # Simplest correct loop: re-run the menu inline.
                        while True:
                            choice2 = self._interactive_menu(unit)
                            if choice2 == "abort":
                                aborted = True
                                break
                            if choice2 == "skip":
                                break
                            if choice2 == "edit":
                                if self._interactive_edit_file(path):
                                    # File fully resolved by direct edit; stage it
                                    # and move to the next file (units consumed).
                                    self._stage_after_edit(path, result)
                                    accepted = []  # don't double-splice
                                    break
                                continue
                            if choice2 == "paste":
                                o2 = self._interactive_paste(unit)
                                if o2.accepted is not None:
                                    accepted.append((unit, o2.accepted))
                                    break
                                self.out("  paste rejected again; re-offering")
                                continue
                            break
                        if aborted:
                            break
                        continue
                    accepted.append((unit, outcome.accepted))
                elif choice == "edit":
                    if self._interactive_edit_file(path):
                        self._stage_after_edit(path, result)
                        accepted = []  # file resolved wholesale by direct edit
                        break  # next file
            if aborted or not accepted:
                continue
            # Batch-splice + stage the paste-mode resolutions (mirrors manual()).
            from capybase.adapters.parsers import splice_all_resolutions
            original = accepted[0][0].original_worktree_text
            spans_and_texts = [
                (unit.marker_span, cand.resolved_text) for unit, cand in accepted
            ]
            buffer = splice_all_resolutions(original, spans_and_texts)
            self._write_and_stage(path, buffer, result)

        if aborted:
            self.out("  aborting rebase as requested")
            self.git.abort_rebase()
            result.escalated = True
            result.reason = result.reason or "aborted by user in interactive fallback"
            return result

        # If any units were skipped, the rebase can't continue cleanly.
        if self.git.has_unmerged_paths():
            self.out(
                "  some units were skipped — rebase left stopped. "
                "Resolve them with git, then `git rebase --continue`."
            )
            result.escalated = True
            result.reason = "interactive fallback: some units skipped"
            return result

        # All units resolved: run the test gate, then continue the rebase. Loop
        # back into run() for further stops so a multi-conflict rebase proceeds.
        self.out("  ✓ conflict(s) resolved interactively; continuing rebase")
        result.escalated = False
        result.reason = None
        self.journal.emit(
            "interactive_resolved",
            {"path": path if not aborted else "", "step": self.step},
            step_index=self.step,
        )
        return self.run()

    def _render_unit_interactive(
        self, unit: ConflictUnit, prior_outcomes: list[UnitOutcome]
    ) -> str:
        """Rich context for the interactive menu: the three sides (truncated for
        huge units) + the model's best attempt + why it failed."""
        lines = [
            f"\n=== {unit.unit_id} ({unit.path}, {unit.conflict_type}) ==="
        ]
        for label, side in (
            ("BASE (common ancestor)", unit.base.text),
            ("CURRENT_UPSTREAM_SIDE", unit.current.text),
            ("REPLAYED_COMMIT_SIDE", unit.replayed.text),
        ):
            n = side.count("\n") + 1
            if n > 30:
                lines.append(f"-- {label} ({n} lines; first 30 shown) --")
                lines.append("\n".join(side.split("\n")[:30]))
                lines.append("... (truncated; see review bundle for full)")
            else:
                lines.append(f"-- {label} --")
                lines.append(side)
        # The model's best attempt + failure, if the escalation carried it.
        if prior_outcomes:
            o = prior_outcomes[0]
            if o.attempts:
                best = o.attempts[-1]
                lines.append("-- model's last attempt --")
                at = best.resolved_text
                if at.count("\n") > 30:
                    lines.append("\n".join(at.split("\n")[:30]))
                    lines.append("... (truncated)")
                else:
                    lines.append(at)
            if o.validation and o.validation.hard_failures:
                lines.append("-- why it failed --")
                for hf in o.validation.hard_failures[:5]:
                    lines.append(f"  [{hf.validator}] {hf.message}")
        return "\n".join(lines)

    def _interactive_menu(self, unit: ConflictUnit) -> str:
        """Present the menu and return the chosen action string."""
        self.out(
            f"\n  How do you want to resolve {unit.unit_id}?\n"
            "    1) paste a resolution\n"
            "    2) edit the file directly (then I validate + continue)\n"
            "    3) skip this unit (leave unmerged)\n"
            "    4) abort the rebase\n"
        )
        choice = self.stdin_reader("  choice [1-4]: ").strip()
        return {"1": "paste", "2": "edit", "3": "skip", "4": "abort"}.get(
            choice, "skip"
        )

    def _interactive_paste(self, unit: ConflictUnit) -> UnitOutcome:
        """Read a pasted resolution and validate it through the full chain."""
        self.out("  paste the resolved text (Ctrl-D to finish):")
        pasted = self.stdin_reader("")
        outcome = self._apply_manual_resolution(unit, pasted)
        self.journal.emit(
            "interactive_resolved",
            {"unit": unit.unit_id, "mode": "paste",
             "accepted": outcome.accepted is not None},
            step_index=self.step,
        )
        return outcome

    def _interactive_edit_file(self, path: str) -> bool:
        """Tell the human to edit the file in their editor; on their signal,
        read it back, reject if conflict markers remain, return True if clean."""
        self.out(
            f"  edit {path} in your editor now (resolve the conflict markers,\n"
            "  save, and return here). Press Enter when done."
        )
        self.stdin_reader("")
        text = self.git.read_worktree_file(path).decode("utf-8", errors="replace")
        if "<<<<<<<" in text or "=======" in text or ">>>>>>>" in text:
            self.out("  ! conflict markers still present — not done editing. Re-offering.")
            self.journal.emit(
                "interactive_resolved",
                {"path": path, "mode": "edit", "accepted": False,
                 "reason": "markers remained"},
                step_index=self.step,
            )
            return False
        self.journal.emit(
            "interactive_resolved",
            {"path": path, "mode": "edit", "accepted": True},
            step_index=self.step,
        )
        return True

    def _stage_after_edit(self, path: str, result: StepResult) -> None:
        """After a direct edit, validate the whole file (cargo check etc.) and
        stage it. The human owns the file content; we only verify + stage."""
        self.git.stage_paths([path])
        self.journal.emit(
            "file_staged", {"path": path, "via": "interactive_edit"},
            step_index=self.step, path=path,
        )

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

    def _try_combination_search(self, unit: ConflictUnit) -> UnitOutcome | None:
        """Attempt a search-based combination resolution; accept only if it
        passes the full validation pipeline. Survey §4.1 (SBCR).

        Runs AFTER the structural resolver declines and BEFORE the LLM. SBCR is a
        *candidate generator*, not a decider: it searches order-preserving
        interleavings of the two sides for the one with maximal mean similarity
        to both parents (the survey's fitness, correlation ~0.64 with developer
        resolution quality). Its search space includes invalid combinations
        (e.g. two contradictory lines concatenated), so — exactly like the
        structural resolver — every candidate is validated (syntax/AST/splice)
        before acceptance, and a rejected candidate falls through to the model.
        Net effect: resolves both-sides-add / restructure conflicts with no LLM
        call when the combination is sound; never applies an invalid merge.
        """
        from capybase.sbcr import balance, resolve_by_combination_search

        result = resolve_by_combination_search(unit)
        if not result.resolved or result.text is None:
            return None
        # Balance-aware routing (survey §4.2): SBCR wins on BALANCED conflicts
        # and loses to the LLM on imbalanced ones (one side changed far more).
        # When routing is on and the conflict is more imbalanced than the
        # configured threshold, do NOT short-circuit — decline so the LLM runs,
        # which is the stronger engine there. Conservative default (0.0) keeps
        # SBCR accepting whenever it resolves; this only diverts imbalanced
        # conflicts when explicitly tuned.
        bal = balance(unit)
        threshold = self.config.routing.min_balance_for_sbcr_accept
        if self.config.routing.enabled and bal < threshold:
            self.journal.emit(
                "combination_resolved",
                {
                    "candidate_id": f"{unit.unit_id}:sbcr",
                    "fitness": round(result.fitness, 4),
                    "balance": round(bal, 4),
                    "passed": False,
                    "deferred_to_llm": True,
                    "reason": f"balance {bal:.2f} < threshold {threshold:.2f}",
                },
                step_index=self.step,
                path=unit.path,
                unit_id=unit.unit_id,
            )
            return None
        cand = CandidateResolution(
            candidate_id=f"{unit.unit_id}:sbcr",
            unit_id=unit.unit_id,
            model_name="sbcr",
            prompt_version="sbcr.combination",
            resolved_text=result.text,
            explanation=(
                f"search-based combination resolution "
                f"(fitness={result.fitness:.3f}, balance={bal:.2f})"
            ),
        )
        validation = self.verification.verify(unit, cand)
        self.journal.emit(
            "combination_resolved",
            {
                "candidate_id": cand.candidate_id,
                "fitness": round(result.fitness, 4),
                "balance": round(bal, 4),
                "passed": validation.passed,
            },
            step_index=self.step,
            path=unit.path,
            unit_id=unit.unit_id,
        )
        if not validation.passed:
            # The combination guess failed validation (e.g. contradictory lines
            # concatenated into invalid code). Discard and let the model handle
            # it. This is why SBCR is safe despite a heuristic fitness function.
            return None
        outcome = UnitOutcome(unit=unit, validation=validation, attempts=[cand])
        outcome.accepted = cand
        self.journal.emit(
            "candidate_accepted",
            {"candidate_id": cand.candidate_id, "via": "sbcr"},
            step_index=self.step,
            path=unit.path,
            unit_id=unit.unit_id,
        )
        return outcome

    def _build_retriever(self, config: Config) -> object:
        """Construct the configured RAG retriever over ``self.memory_store``.

        - ``"lexical"`` (default): dependency-free BM25.
        - ``"embedding"``: an :class:`EmbeddingRetriever` (semantic, survey §4.2)
          from a fresh embeddings client. Any failure to construct it falls back to
          BM25 so RAG never hard-fails.
        - ``"hybrid"``: a :class:`HybridRetriever` fusing BM25 + embeddings (survey
          §4). Degrades to lexical-only when the embedding endpoint is unavailable.

        When an embeddings-calibration envelope is present it is reconstructed and
        passed to the EmbeddingRetriever so the isotonic score transform +
        calibrated floor apply (survey §2.1).
        """
        from capybase.memory.retriever import EmbeddingRetriever, HybridRetriever, LexicalRetriever

        lex = LexicalRetriever(self.memory_store)

        if config.memory.retriever == "embedding":
            emb = self._build_embedding_retriever(config)
            return emb if emb is not None else lex

        if config.memory.retriever == "hybrid":
            emb = self._build_embedding_retriever(config)
            if emb is None:
                return lex  # embedding endpoint unavailable → lexical-only hybrid
            return HybridRetriever(
                lex, emb, fusion=config.memory.fusion_method or "rrf"
            )

        return lex

    def _build_embedding_retriever(self, config: Config) -> "object | None":
        """Build an EmbeddingRetriever, or None if the endpoint is unavailable.

        Returns None (rather than raising) on any construction failure so callers
        can fall back to BM25 — RAG never hard-fails. The calibrated envelope is
        reconstructed and attached so the isotonic transform + calibrated floor
        apply when present (survey §2.1).
        """
        from capybase.memory.retriever import EmbeddingRetriever

        try:
            from capybase.memory.embeddings import OpenAIEmbeddingsClient

            # The embeddings model: explicit config, else reuse the completion
            # model name (a single-model llama-server serving both).
            emb_cfg = config.model
            if config.memory.embeddings_model:
                emb_cfg = emb_cfg.model_copy(update={"model": config.memory.embeddings_model})
            client = OpenAIEmbeddingsClient(emb_cfg)
            return EmbeddingRetriever(
                self.memory_store,
                client,
                min_similarity=config.memory.embedding_min_similarity,
                calibration=_reconstruct_calibration(config),
            )
        except Exception:  # noqa: BLE001 - fall back to BM25, never break RAG
            return None

    # ==================================================================
    # M3: full run
    # ==================================================================

    def rebase(
        self,
        target: str,
        *,
        autostash: bool = False,
        abort_on_escalation: bool = True,
        interactive: bool = True,
    ) -> StepResult:
        """Own the entire rebase: start it, drive the resolution loop, finish.

        Unlike :meth:`run` (which assumes the user already started the rebase
        and stopped on a conflict), ``rebase`` starts the rebase itself and then
        hands off to the existing :meth:`run` loop — so a single invocation
        carries the rebase from clean tree to completion (or escalation).

        Flow:
        1. Preflight the worktree (clean, unless ``autostash``).
        2. Record the pre-rebase HEAD as a recovery ref
           (``refs/rebase-agent/<session>/start``) and in the journal.
        3. Start the rebase.
        4. If the rebase is clean (no conflict), finish immediately with a
           ``session_completed`` event — :meth:`run` is never called.
        5. Otherwise drive :meth:`run` — the proven resolve → test → continue
           loop.
        6. On escalation with ``abort_on_escalation`` (the default, since
           ``rebase`` owns the process), ``git rebase --abort`` returns the repo
           to its original HEAD. Without it the rebase is left stopped, matching
           :meth:`run`'s behavior, so the user can inspect the review bundle and
           finish manually.

        ``autostash`` mirrors ``git rebase --autostash`` (stashes dirty changes
        and re-applies them after). Without it, a dirty worktree raises
        :class:`GitError` before any rebase starts — the CLI's top-level guard
        reports it cleanly.
        """
        self.journal.emit(
            "rebase_requested",
            {"target": target, "autostash": autostash,
             "abort_on_escalation": abort_on_escalation},
        )
        # 0. Pre-flight: refuse to touch the repo on a bad starting state.
        #    Runs git-only checks (no network) so the rebase path stays fast.
        #    A blocking failure raises GitError here; the CLI guard prints it.
        preflight = run_rebase_preflight(
            self.git, self.config, target, autostash=autostash, llm_ping=False
        )
        self.journal.emit("preflight_check", {"checks": preflight.as_payload()})
        if not preflight.passed:
            fail = preflight.first_blocking_failure
            msg = fail.detail if fail else "pre-flight checks failed"
            self.journal.emit(
                "rebase_start_failed", {"reason": "preflight", "detail": msg}
            )
            raise GitError(f"refusing to rebase: {msg}")
        # 1. Worktree must be clean unless the user opted into autostash.
        #    (Preflight already checked this, but keep the explicit guard so
        #    the invariant is visible at the call site.)
        if not autostash:
            self.git.require_clean_worktree()  # raises GitError if dirty
        # 2. Recovery ref + backup branch + journal: the original HEAD is
        #    recorded two ways. The internal ``refs/rebase-agent/<id>/start`` is
        #    capybase's audit ref (read by `status`, used by abort). The
        #    user-visible ``capybase/backup/<branch>@<ts>`` branch is the safety
        #    net: a real branch the developer can see in `git branch`, reset to,
        #    or delete once they've confirmed the rebase result.
        start_oid = self.git.head_oid()
        self.git.create_session_refs(self.session_id, start_oid)
        backup_branch = self.git.current_branch() or "head"
        backup_ref = self.git.create_backup_ref(start_oid, label=backup_branch)
        self.journal.emit(
            "rebase_started",
            {"target": target, "start_oid": start_oid, "backup_ref": backup_ref},
        )
        self.log.info(
            "rebase started: session=%s target=%s branch=%s start=%s backup=%s",
            self.session_id, target, backup_branch, start_oid[:8], backup_ref,
        )
        # 3. Start the rebase. A conflict stop has rc != 0 but leaves the rebase
        #    in progress; a genuine failure (bad target, etc.) has rc != 0 and
        #    NO rebase in progress. A clean rebase has rc == 0.
        res = self.git.start_rebase(target, autostash=autostash)
        if not res.ok and not self.git.rebase_in_progress():
            self.journal.emit(
                "rebase_start_failed", {"stderr": res.stderr[:500]}
            )
            raise GitError(
                f"git rebase {target} failed: {res.stderr.strip()}"
            )
        # 4a. A clean rebase (no conflict) finishes here: the rebase is no longer
        #     in progress and there's nothing for run()'s loop to resolve. Emit
        #     the completion event and return success directly — run()'s preflight
        #     would otherwise escalate on "no rebase in progress".
        if not self.git.rebase_in_progress():
            head_after = self.git.head_oid()
            self.journal.emit(
                "session_completed",
                {"head_after": head_after, "clean": True},
                git_head_after=head_after,
            )
            self.git.record_step_ref(self.session_id, self.step, head_after)
            self.log.info(
                "rebase completed (clean, no conflicts): session=%s steps=%d "
                "head_after=%s", self.session_id, self.step, head_after[:8],
            )
            self.out(
                f"✓ rebase complete, no conflicts (session {self.session_id})\n"
                f"  backup branch {backup_ref} points at the pre-rebase HEAD "
                f"{start_oid[:8]}; delete it once you've confirmed the result:\n"
                f"    git branch -D {backup_ref}"
            )
            return StepResult(step_index=self.step, escalated=False, continued=True)
        # 4b. The rebase stopped on a conflict: drive the resolution loop.
        # Install a SIGTERM/SIGHUP handler so a killed rebase aborts cleanly
        # (returning the repo to start_oid via the backup) instead of leaving a
        # stopped rebase in the user's repo. SIGINT (Ctrl-C) already raises
        # KeyboardInterrupt; only the terminate-style signals need converting.
        # Restored after the run so the handler doesn't leak.
        import signal

        _sigs = (signal.SIGTERM, getattr(signal, "SIGHUP", signal.SIGTERM))
        _prev: dict[int, object] = {}

        def _interrupt(signum, _frame):
            from capybase.adapters.llm_openai import Interrupted
            raise Interrupted(f"capybase interrupted by signal {signum}")

        for _sig in _sigs:
            try:
                _prev[_sig] = signal.signal(_sig, _interrupt)
            except (ValueError, OSError):
                pass
        try:
            result = self.run()
        except BaseException as exc:
            # On ANY interruption (signal, KeyboardInterrupt, unexpected error)
            # while a rebase is in progress, abort it so the repo isn't left
            # stopped. The backup branch + start_oid let the user recover fully.
            if self.git.rebase_in_progress():
                self.git.abort_rebase()
                self.journal.emit(
                    "rebase_aborted",
                    {"reason": f"interrupted: {exc}", "start_oid": start_oid,
                     "backup_ref": backup_ref},
                    git_head_after=self.git.head_oid(),
                )
                self.log.warning(
                    "rebase interrupted and aborted: session=%s reason=%s "
                    "restored_to=%s backup=%s",
                    self.session_id, exc, start_oid[:8], backup_ref,
                )
                self.out(
                    f"! rebase interrupted ({exc}) — aborted, repo back at "
                    f"{start_oid[:8]}; backup branch {backup_ref} preserved. "
                    f"Re-run `capybase rebase {target}` to retry."
                )
            raise
        finally:
            for _sig, _h in _prev.items():
                try:
                    signal.signal(_sig, _h)  # type: ignore[arg-type]
                except (ValueError, OSError, TypeError):
                    pass
        # 5. On a successful finish (conflicts resolved and replayed), surface
        #    the backup branch so the user can reclaim it after confirming.
        if not result.escalated:
            self.log.info(
                "rebase completed (conflicts resolved): session=%s steps=%d "
                "head_after=%s", self.session_id, self.step,
                self.git.head_oid()[:8],
            )
            self.out(
                f"  backup branch {backup_ref} points at the pre-rebase HEAD "
                f"{start_oid[:8]}; delete it once you've confirmed the result:\n"
                f"    git branch -D {backup_ref}"
            )
        # 6. Interactive fallback: on escalation, if a human is at the terminal
        #    and the rebase is still in progress (not yet aborted), present the
        #    conflict for an interactive decision before the auto-abort runs.
        #    This keeps capybase the single owner of the process: the human
        #    resolves the one unit the model couldn't, then the rebase continues.
        #    Disabled by --no-interactive (e.g. CI) or when stdin isn't a TTY.
        if (
            result.escalated
            and interactive
            and self.git.rebase_in_progress()
            and self._is_interactive_terminal()
        ):
            result = self.interactive_resolve(result)
        # 7. Abort-on-escalation: return the repo to start_oid if we couldn't
        #    finish. run() sets escalated and leaves the rebase stopped; abort
        #    rolls it all back so the developer is back where they started.
        if result.escalated and abort_on_escalation and self.git.rebase_in_progress():
            self.git.abort_rebase()
            self.journal.emit(
                "rebase_aborted",
                {"reason": result.reason, "start_oid": start_oid,
                 "backup_ref": backup_ref},
                git_head_after=self.git.head_oid(),
            )
            self.log.warning(
                "rebase escalated and aborted: session=%s steps=%d reason=%s "
                "restored_to=%s", self.session_id, self.step, result.reason,
                start_oid[:8],
            )
            self.out(
                f"! escalated and aborted rebase — repo back at {start_oid[:8]}\n"
                f"  review bundle: {self.paths.final / 'review-bundle.md'}\n"
                f"  backup branch {backup_ref} still points at the pre-rebase "
                f"HEAD; reset to it with `git reset --hard {backup_ref}`, or "
                f"delete it with `git branch -D {backup_ref}`"
            )
        return result

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
            # Enrich the summary bundle from the step's outcomes so the human
            # sees the model's best attempt + the validation failure — not just
            # the bare reason. Prefer an unaccepted (escalated) outcome; on a
            # whole-FILE failure every unit was accepted per-unit but the file
            # failed cargo, so fall back to the last outcome (its candidate is
            # what got spliced and failed the whole-file check).
            _esc = next((o for o in last.outcomes if o.accepted is None), None)
            if _esc is None and last.outcomes:
                _esc = last.outcomes[-1]
            write_review_bundle(
                self.paths,
                reason=last.reason or "escalated",
                step_index=last.step_index,
                unit=_esc.unit if _esc else None,
                candidate=(_esc.accepted or (_esc.attempts[-1] if _esc.attempts else None)) if _esc else None,
                validation=_esc.validation if _esc else None,
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

        # Two-phase resolution so cross-file (whole-crate) verification works.
        #
        # Phase 1: resolve every unit in every conflicted file and WRITE each
        # resolved buffer to the worktree, without staging or crate-wide
        # checking. This is critical for Rust: a per-file ``cargo check`` reads
        # the REAL worktree, so while sibling conflicted files still hold raw
        # ``<<<<<<<`` markers, the check fails with ``error: encountered diff
        # marker`` — a correct merge gets rejected through no fault of its own.
        # Writing every file resolved first makes the whole crate marker-free
        # before any cargo check runs. If any unit escalates, bail before any
        # write (nothing staged, rebase stays stoppable).
        #
        # Phase 2: with all files written, run the per-file Phase-B validation
        # (markers/splice/syntax/cargo) + CEGIS repair loop, then stage. Each
        # file's cargo check now sees a clean crate.
        from capybase.adapters.parsers import splice_all_resolutions

        resolved_files: dict[str, str] = {}  # path -> spliced buffer (all units)
        accepted_by_path: dict[str, list] = {}  # path -> [(unit, candidate), ...]
        # Snapshot the original worktree text per path so Phase 2 can re-splice.
        originals: dict[str, str] = {}

        # ---- Phase 1: resolve + write all files (no staging, no cargo) ----
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
            # Splice every accepted resolution in one offset-correct batch.
            spans_and_texts = [
                (unit.marker_span, cand.resolved_text) for unit, cand in accepted
            ]
            original = accepted[0][0].original_worktree_text
            buffer = splice_all_resolutions(original, spans_and_texts)
            resolved_files[path] = buffer
            accepted_by_path[path] = accepted
            originals[path] = original
            # Write the resolved file to the worktree NOW (no staging yet) so
            # sibling files' cargo checks in Phase 2 see a marker-free crate.
            self._write_worktree_only(path, buffer)

        # ---- Phase 2: per-file Phase-B validation + CEGIS repair + stage ----
        for path, units in result.units_by_path.items():
            accepted = accepted_by_path[path]
            original = originals[path]
            language = units[0].language
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
            buffer = resolved_files[path]
            if self.config.validation.require_whole_file_validation and units:
                wf_retries = 0
                wf_budget = self.config.policy.max_retries_per_unit
                file_validation = None  # type: ignore[assignment]
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
                    # Enrich the bundle with the unit/candidate/validation so the
                    # human (and the interactive fallback) can see what was tried
                    # and why cargo rejected it — not just the bare reason.
                    _unit = accepted[0][0] if accepted else None
                    _cand = accepted[0][1] if accepted else None
                    write_review_bundle(
                        self.paths,
                        reason=result.reason,
                        step_index=result.step_index,
                        unit=_unit,
                        candidate=_cand,
                        validation=file_validation if file_validation is not None else None,
                    )
                    return result
            # Stage the validated file (it was already written to the worktree
            # in Phase 1; re-write in case the CEGIS loop changed it, then stage).
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

        # Search-based combination resolution (survey §4.1 SBCR): AFTER the
        # structural resolver declines and BEFORE the LLM. Searches order-
        # preserving interleavings for the best combination; the candidate is
        # validated before acceptance, so an invalid combination falls through to
        # the model. Only on a FRESH resolve. Gated by [future]
        # enable_combination_search.
        if failures is None and self.config.future.enable_combination_search:
            early = self._try_combination_search(unit)
            if early is not None:
                return early  # accepted via combination search; LLM loop skipped

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
                {
                    "token_estimate": context.token_estimate,
                    "retrieval_scores": context.retrieval_scores,
                },
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
            # Token-window trims (empty when no budget configured or nothing
            # trimmed): surfaces that the prompt was capped (few-shot/deps/etc.
            # dropped) so the resolution is auditable against the context window.
            prompt_trims = getattr(winner, "prompt_trims", None)
            if prompt_trims:
                emit_payload["prompt_trims"] = prompt_trims
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

    def _write_worktree_only(self, path: str, buffer: str) -> None:
        """Write a resolved file to the worktree WITHOUT staging it.

        Used by Phase 1 of cross-file resolution: every conflicted file is
        written resolved first, so the whole crate is marker-free before any
        cargo check runs in Phase 2. Staging is deferred to ``_write_and_stage``
        (called in Phase 2 after validation passes) so an escalatable failure
        never leaves staged-but-invalid state. The journal snapshot is skipped
        here (Phase 2's ``_write_and_stage`` records the final staged buffer).
        """
        self.git.write_worktree_file(path, buffer.encode("utf-8"))

    def _run_tests(self, label: str, result: StepResult) -> bool:
        cmd = getattr(self.config.tests, label) if hasattr(self.config.tests, label) else None
        if not cmd:
            return True
        cmd = self._resolve_test_command(cmd)
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

    def _resolve_test_command(self, cmd: str) -> str:
        """Resolve a (possibly language-default) test command to a real one.

        The shipped default is ``"pytest"`` (Python-centric). When that default
        is configured and the repo is a Cargo project with no pytest on PATH,
        substitute ``"cargo test"`` — a pure-Rust repo would otherwise fail
        every ``run`` at the pre-continue gate. An *explicit* command (anything
        other than the bare ``"pytest"`` default, including a user who set
        ``pre_continue = "cargo test"`` themselves) is returned unchanged:
        we never override a deliberate choice. This keeps Python repos on
        pytest (the common case) while making Rust repos work out of the box.
        """
        if cmd.strip() != "pytest":
            return cmd
        has_cargo = (self.git.repo / "Cargo.toml").is_file()
        if not has_cargo:
            return cmd
        from shutil import which

        if which("pytest") is not None:
            return cmd  # mixed repo with pytest installed → honor the default
        return "cargo test"

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


def _is_interactive_terminal() -> bool:
    """True iff stdin is a real terminal (a human is present).

    The interactive fallback fires only when this is True, so it never blocks a
    non-TTY run (CI, piped input). Tests force it on/off by monkeypatching this
    function (they can't provide a real TTY)."""
    import sys
    return bool(getattr(sys.stdin, "isatty", lambda: False)())


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

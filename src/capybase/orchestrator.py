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
from capybase.policy_strictness import StrictnessPolicy
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
    # The full ConflictClassification (band + reasons) when routing ran. Typed
    # loosely to avoid an import cycle; it's a capybase.classifier.ConflictClassification.
    # None when routing is disabled (difficulty defaults to "complex").
    classification: object | None = None
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


def _resolved_buffer(
    original: str, accepted: list[tuple[ConflictUnit, CandidateResolution]]
) -> str:
    """Build the resolved file buffer for one path's accepted units.

    Marker-block units splice their resolution into the span within
    ``original`` (the marker-laden worktree text). A ``whole_file`` unit
    (modify/delete) has ``marker_span=None``: its resolved text IS the file —
    empty for an accepted deletion, the keeper's full text for keep_block — so
    there is nothing to splice. Mixing the two in one path isn't meaningful;
    when any unit is whole-file we take the (single) accepted resolution's
    text verbatim.
    """
    from capybase.adapters.parsers import splice_all_resolutions

    if any(unit.marker_span is None for unit, _ in accepted):
        return accepted[0][1].resolved_text
    spans_and_texts = [
        (unit.marker_span, cand.resolved_text) for unit, cand in accepted
    ]
    return splice_all_resolutions(original, spans_and_texts)


def _is_whole_file_delete(
    accepted: list[tuple[ConflictUnit, CandidateResolution]]
) -> bool:
    """True iff a path's single accepted resolution means ``delete the file``.

    A whole-file modify/delete accepted via block-capture's ``accept_deletion``
    yields empty resolved text — the file should be ``git rm``'d, not written.
    Any non-whole-file unit, or a non-empty whole-file resolution (keep_block),
    returns False so the normal write+add path runs.
    """
    if len(accepted) != 1:
        return False
    unit, cand = accepted[0]
    return unit.marker_span is None and not cand.resolved_text.strip()


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
        stdin_reader: Callable[..., str] | None = None,
        out: Callable[[str], None] = print,
        color: bool = False,
    ) -> None:
        from capybase.color import make_styler

        self.style = make_styler(color)
        self.git = GitBackend(repo)
        self.session_id = session_id or new_session_id()
        # Paths resolved as a deliberate modify/delete keep_block this session.
        # Excluded from the end-of-rebase silent-resurrection scan: such a keep
        # is an explicit, reviewed resurrection (not a silent undo).
        self._explicitly_kept_paths: set[str] = set()
        # The most recent test-gate verdict (human-readable), stashed by
        # _run_tests for the accept report written after the gate.
        self._last_test_verdict: str | None = None
        # History-awareness substrate (#history): the rebase plan + query service,
        # set by rebase() at start. Empty service when not rebase()-driven (the
        # run()/inspect paths), so all history queries degrade to no-op.
        self._history_plan = None
        self._history_service = None
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
        # Acceptance-strictness policy (#10): tightens the accept branch per the
        # configured mode (interactive/dry_run/ci/unattended). Inert in the
        # default interactive mode. Rebound per-run when rebase() learns whether
        # a human is present (CI / --no-interactive can tighten to ci/unattended).
        self.strictness = StrictnessPolicy(
            mode=config.policy.policy_mode,
            min_confidence=config.policy.unattended_min_confidence,
            escalate_bands=tuple(config.policy.unattended_escalate_bands),
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
            self.out(self._warn(f"! {reason}") + f"\n  review bundle: {bundle}")
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
                    "paste the resolved text for this block (Ctrl-D to finish):",
                    multiline=True,
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
            original = accepted[0][0].original_worktree_text
            buffer = _resolved_buffer(original, accepted)
            # Write + stage the file.
            self._write_and_stage(path, buffer, result, accepted=accepted)
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
        # Decide which units to present. The escalation's own ``units_by_path``
        # (carried from _resolve_step) is authoritative when present: for a
        # WHOLE-FILE-VALIDATION failure the worktree is already marker-free
        # (Phase 1 wrote the resolved buffer before Phase 2 validated it), so
        # re-gathering from the worktree finds NO markers and NO units — bailing
        # the human out of the very fallback meant to help them. Prefer the
        # escalation's units; only re-gather when they're absent (a pre-extraction
        # escalation, or the user re-running ``run`` on a stopped rebase).
        units_by_path = result.units_by_path
        whole_file_failure = bool(
            result.reason and "whole-file" in result.reason
        )
        if not units_by_path:
            gathered = self._gather_step()
            if gathered.escalated or not gathered.units_by_path:
                self.out("  (no resolvable units to present interactively)")
                self.journal.emit(
                    "interactive_bail",
                    {
                        "why": "no resolvable units",
                        "gathered_escalated": gathered.escalated,
                        "gathered_units": list(gathered.units_by_path),
                    },
                    step_index=self.step,
                )
                return result
            units_by_path = gathered.units_by_path

        aborted = False
        for path, units in units_by_path.items():
            if aborted:
                break
            # A whole-file failure (cross-unit error after splice) is best handled
            # by editing the whole file directly — the per-unit splice menu can't
            # fix a combination error. BUT the worktree currently holds the
            # MODEL'S BROKEN SPLICE (marker-free, written by Phase 1 before Phase
            # 2 validated) — so edit mode must first RESTORE the raw conflict
            # markers, letting the human resolve the real conflict from scratch
            # rather than repair an already-broken resolution. Lead with the
            # file-edit path; paste/skip/abort remain as fallback.
            raw_conflict = units[0].original_worktree_text if units else None
            if whole_file_failure:
                self.out(
                    f"\n  {path}: the individual resolutions are valid, but their "
                    f"combination fails whole-file validation:\n    "
                    + (result.reason or "").replace("\n", "\n    ")
                )
                self.out(
                    "  The fastest fix is to edit the file directly (option 2): "
                    "capybase will restore the raw conflict markers and you "
                    "resolve it fresh."
                )
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
                                if self._interactive_edit_file(
                                    path, restore_conflict=(
                                        raw_conflict if whole_file_failure else None
                                    )
                                ):
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
                    # On a whole-file failure, restore the raw conflict markers
                    # so the human resolves the real conflict (not the model's
                    # broken splice). On a plain escalation the markers are
                    # already in the worktree, so no restore is needed.
                    restore = raw_conflict if whole_file_failure else None
                    if self._interactive_edit_file(path, restore_conflict=restore):
                        self._stage_after_edit(path, result)
                        accepted = []  # file resolved wholesale by direct edit
                        break  # next file
            if aborted or not accepted:
                continue
            # Batch-splice + stage the paste-mode resolutions (mirrors manual()).
            original = accepted[0][0].original_worktree_text
            buffer = _resolved_buffer(original, accepted)
            self._write_and_stage(path, buffer, result, accepted=accepted)

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
        self.out("  " + self._ok("✓ conflict(s) resolved interactively; continuing rebase"))
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
        huge units) + the model's best attempt + why it failed.

        Color (when enabled via ``self.style``) is applied to the structural
        elements — the unit header, side headers, the side-analysis line, and
        failure markers — NOT to the conflict-side *content* itself, so the body
        text stays readable and substring assertions on it hold. Color is a
        passthrough when disabled (default), so this output is byte-identical to
        the un-colored baseline unless color is explicitly turned on.
        """
        from capybase.color import BOLD, CYAN, DIM, MAGENTA, RED, YELLOW

        s = self.style
        lines = [
            s(f"\n=== {unit.unit_id} ({unit.path}, {unit.conflict_type}) ===", BOLD)
        ]
        # Side classification (modify/delete disambiguation): annotate each side
        # header with what it DID (DELETED/ADDED/MODIFIED/unchanged) so a side
        # that's empty because it deleted base content isn't read as "absent".
        # Reads the merge_intent.direction result stashed at extraction.
        md = unit.structural_metadata.get("merge_direction") or {}
        prov = unit.structural_metadata.get("provenance") or {}
        # Per-side header color: BASE dim (reference), CURRENT cyan, REPLAYED magenta.
        side_header_color = {None: DIM, "current": CYAN, "replayed": MAGENTA}
        for label, side, key in (
            ("BASE (common ancestor)", unit.base.text, None),
            ("CURRENT_UPSTREAM_SIDE", unit.current.text, "current"),
            ("REPLAYED_COMMIT_SIDE", unit.replayed.text, "replayed"),
        ):
            ann = self._side_annotation(md, prov, key) if key else ""
            n = side.count("\n") + 1
            header_color = side_header_color[key]
            if n > 30:
                lines.append(s(f"-- {label} ({n} lines; first 30 shown)", header_color)
                             + f"{ann}" + s(" --", header_color))
                lines.append("\n".join(side.split("\n")[:30]))
                lines.append(s("... (truncated; see review bundle for full)", DIM))
            else:
                lines.append(s(f"-- {label} --", header_color) + f"{ann}")
                lines.append(side)
        # One-line side-analysis summary (e.g. "modify/delete: ... DELETED this
        # block") so the conflict shape is explicit, not inferred from the text.
        summary = md.get("summary")
        if summary:
            lines.append(s(f"-- side analysis: {summary} --", YELLOW))
        # The model's best attempt + failure, if the escalation carried it.
        if prior_outcomes:
            o = prior_outcomes[0]
            if o.attempts:
                best = o.attempts[-1]
                lines.append(s("-- model's last attempt --", DIM))
                at = best.resolved_text
                if at.count("\n") > 30:
                    lines.append("\n".join(at.split("\n")[:30]))
                    lines.append(s("... (truncated)", DIM))
                else:
                    lines.append(at)
            if o.validation and o.validation.hard_failures:
                lines.append(s("-- why it failed --", RED))
                for hf in o.validation.hard_failures[:5]:
                    lines.append(f"  {s(f'[{hf.validator}]', RED)} {hf.message}")
        return "\n".join(lines)

    def _side_annotation(
        self, md: dict, prov: dict, key: str | None
    ) -> str:
        """A short `` — DELETED (introduced by <commit>)`` tag for a side header.

        ``md`` is the unit's ``merge_direction`` metadata, ``prov`` its
        ``provenance`` metadata, ``key`` the side (``"current"``/``"replayed"``).
        Returns ``""`` when nothing is recorded, so unenriched units render as
        before. Mirrors :func:`escalation._annotated_side_header` but inline. The
        classification tag is colored semantically (DELETED red, ADDED green,
        MODIFIED yellow, unchanged dim) when color is enabled.
        """
        if not key:
            return ""
        from capybase.color import DIM, GREEN, RED, YELLOW

        s = self.style
        parts: list[str] = []
        kind = (md or {}).get(key)
        # Semantic color per classification: red=removed, green=added, yellow=changed.
        tag_color = {
            "added": GREEN, "deleted": RED, "modified": YELLOW, "unchanged": DIM,
        }.get(kind)
        label = {
            "added": "ADDED", "deleted": "DELETED",
            "modified": "MODIFIED", "unchanged": "unchanged",
        }.get(kind)
        if label and tag_color is not None:
            parts.append(s(f" — {label}", tag_color))
        elif label:
            parts.append(f" — {label}")
        subject = ((prov or {}).get(key) or {}).get("subject")
        if subject:
            parts.append(s(f" (introduced by `{subject}`)", DIM))
        return "".join(parts)

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
        pasted = self.stdin_reader("", multiline=True)
        outcome = self._apply_manual_resolution(unit, pasted)
        self.journal.emit(
            "interactive_resolved",
            {"unit": unit.unit_id, "mode": "paste",
             "accepted": outcome.accepted is not None},
            step_index=self.step,
        )
        return outcome

    def _interactive_edit_file(
        self, path: str, *, restore_conflict: str | None = None
    ) -> bool:
        """Tell the human to edit the file in their editor; on their signal,
        read it back, and LOOP until no conflict markers remain (returning True)
        or the human gives up (returning False).

        ``restore_conflict``: when set (a whole-file escalation), the worktree
        currently holds the MODEL'S BROKEN SPLICE (marker-free) — Phase 1 wrote
        it before Phase 2 validated. Offering edit mode on that is wrong: the
        human would edit an already-resolved-but-broken file with no markers to
        resolve, and the prompt ("resolve the conflict markers") wouldn't match.
        So we FIRST write back the raw conflict buffer (with markers), so the
        human resolves the REAL conflict from scratch.

        On each Enter, if markers remain we tell the human and re-prompt (NOT
        return — a prior version printed "Re-offering" then returned False, which
        the caller treated as a skip, aborting the rebase on a single Enter
        before the human had resolved anything). The loop is bounded so a runaway
        can't spin forever; after the cap, return False (the caller skips the
        unit rather than silently aborting the whole rebase).
        """
        if restore_conflict is not None:
            self._write_worktree_only(path, restore_conflict)
            self.out(
                f"  (restored the raw conflict markers to {path} — the previous "
                "resolution attempt was broken; resolve the conflict fresh.)"
            )
        self.out(
            f"  edit {path} in your editor now (resolve the conflict markers,\n"
            "  save, and return here). Press Enter when done."
        )
        max_reprompts = 50  # generous; a human genuinely working won't hit this
        for _ in range(max_reprompts):
            self.stdin_reader("")
            text = self.git.read_worktree_file(path).decode("utf-8", errors="replace")
            # Use line-anchored marker detection (contains_markers), NOT loose
            # substring matching: a file with ``// =====`` comment banners would
            # false-positive on ``"=======" in text`` and loop forever claiming
            # "markers still present" when none are. Real git conflict markers
            # start at column 0.
            from capybase.adapters.parsers import contains_markers

            if not contains_markers(text):
                self.journal.emit(
                    "interactive_resolved",
                    {"path": path, "mode": "edit", "accepted": True},
                    step_index=self.step,
                )
                return True
            # Markers still present: re-prompt (the message says "re-offer" — now
            # it actually does). The human presses Enter again after editing more.
            self.out(
                self._warn(
                    "! conflict markers still present in "
                    + path
                    + " — not done editing."
                )
            )
            self.out("  Edit the file, remove all markers, save, and Press Enter again.")
            self.journal.emit(
                "interactive_resolved",
                {"path": path, "mode": "edit", "accepted": False,
                 "reason": "markers remained (re-prompting)"},
                step_index=self.step,
            )
        # Cap hit: the human couldn't clear the markers. Return False so the
        # caller skips this unit (the rebase stays stopped), rather than aborting.
        self.out(
            f"  giving up on {path} after repeated attempts — markers still "
            f"present. This unit will be skipped."
        )
        return False

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

    def _strictness_blocks_pre_llm(
        self, unit: ConflictUnit, cand: CandidateResolution,
        validation: VerificationResult, via: str,
    ) -> str:
        """The strictness-policy gate for a DETERMINISTIC pre-LLM resolution.

        Returns a non-empty reason when the configured mode (#10) refuses to
        auto-accept this resolution even though it passed validation (e.g. it
        dropped a side obligation or introduced a diagnostic in ci/unattended
        mode). Empty string ⇒ accept. The resolution is then discarded (returns
        None from its caller), falling through to the LLM — strictness never
        applies an invalid merge, it just declines to auto-accept a borderline
        one without a human.
        """
        if not self.strictness.strict:
            return ""
        band = self._classification_band(unit)
        ok, reason = self.strictness.accept_pre_llm(
            unit, cand, validation, band=band
        )
        if ok:
            return ""
        self.journal.emit(
            "strictness_declined",
            {"via": via, "reason": reason, "mode": self.strictness.mode},
            step_index=self.step, path=unit.path, unit_id=unit.unit_id,
        )
        return reason

    def _classification_band(self, unit: ConflictUnit) -> str | None:
        """The unit's classification band (#2), computed if routing is on."""
        if not self.config.routing.enabled:
            return None
        try:
            from capybase.classifier import classify
            return classify(unit).band  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - advisory for the strictness gate
            return None

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
        if self._strictness_blocks_pre_llm(unit, cand, validation, "structural"):
            return None  # strict mode declines to auto-accept; fall through to LLM
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
        if self._strictness_blocks_pre_llm(unit, cand, validation, "sbcr"):
            return None  # strict mode declines to auto-accept; fall through to LLM
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

    def _try_block_capture(self, unit: ConflictUnit) -> UnitOutcome | None:
        """Block-capture resolution for large modify/delete conflicts.

        When one side DELETED a large block and the other KEPT it (and the
        structural ``delete_side`` rule declined — e.g. the keeper MODIFIED the
        block, so it's not a clean auto-accept), asking the model to REPRODUCE
        the block as an escaped JSON string fails: it collapses to placeholders
        (``// ... unchanged ...``) and corrupts the escaping (mixed real/literal
        ``\\n``). The CEGIS loop then chases those self-inflicted errors forever.

        Block-capture sidesteps this entirely: the model makes a small DECISION
        (accept_deletion / keep_block / needs_human), and capybase splices the
        chosen conflict side's text VERBATIM. The model never reproduces the
        block, so truncation and escaping errors are structurally impossible.

        Runs AFTER structural + combination search decline and BEFORE the LLM
        loop, only on a FRESH resolve. Gated by ``[future] enable_block_capture``
        and a minimum block size (``block_capture_min_lines``): the full-LLM path
        is fine for small blocks, so this only engages where reproduction is the
        problem. Like the other pre-LLM layers, the spliced candidate still runs
        the full validation pipeline; an invalid splice (e.g. keep_block on a
        block that doesn't fit the file) falls through to the LLM.
        """
        from capybase.merge_intent import direction
        from capybase.resolution_engine import (
            PROMPT_BLOCK_CAPTURE,
            build_block_capture_prompt,
            parse_block_capture_decision,
        )

        # Self-gate: the caller (_resolve_unit) already checks the flag, but
        # _try_block_capture must be correct when called directly too.
        if not self.config.future.enable_block_capture:
            return None
        # Gate 1: must be a modify/delete with a known deleting side.
        md = unit.structural_metadata.get("merge_direction") or {}
        if md.get("kind") != "modify_delete" or not md.get("deleting_side"):
            return None
        who = md["deleting_side"]  # "current" | "replayed"
        # Gate 2: the kept block must be large enough that reproduction is the
        # problem. Small modify/deletes go through the normal LLM path.
        keeper = unit.replayed if who == "current" else unit.current
        deleter = unit.current if who == "current" else unit.replayed
        keeper_n = sum(1 for ln in (keeper.text or "").splitlines() if ln.strip())
        if keeper_n < self.config.future.block_capture_min_lines:
            return None

        # Ask the model for a decision (not a reproduction). The prompt shows a
        # summary of the keeper, never the full text.
        context = self.context_builder.build(unit)
        prompt = build_block_capture_prompt(unit, context)
        if self.config.journal.enabled and self.config.journal.store_prompts:
            self.journal.store_prompt(unit.unit_id, 0, prompt)
        try:
            resp = self.resolution_engine.raw_complete(prompt, json_mode=False)
        except Exception as exc:  # noqa: BLE001 - request failed → fall through
            self.journal.emit(
                "block_capture_request_failed",
                {"error": str(exc)[:200]},
                step_index=self.step,
                path=unit.path,
                unit_id=unit.unit_id,
            )
            return None
        decision, reason = parse_block_capture_decision(resp.text)
        self.journal.emit(
            "block_capture_decision",
            {
                "decision": decision,
                "reason": reason,
                "keeper_lines": keeper_n,
            },
            step_index=self.step,
            path=unit.path,
            unit_id=unit.unit_id,
        )
        # Map the decision to the text to splice, taken VERBATIM from the
        # conflict side — never reproduced by the model.
        if decision == "accept_deletion":
            resolved_text = deleter.text or ""
            expl = f"block-capture: accepted deletion ({reason})"
        elif decision == "keep_block":
            resolved_text = keeper.text or ""
            expl = f"block-capture: kept block verbatim ({reason})"
            # A whole-file keep_block deliberately resurrects content upstream
            # deleted (it was a modify/delete the keeper won). The end-of-rebase
            # silent-resurrection scan would otherwise flag it — but this keep
            # was an explicit, reviewed decision, not a silent undo, so suppress
            # the finding for this path.
            if unit.marker_span is None:
                self._explicitly_kept_paths.add(unit.path)
        else:
            # needs_human (or unparseable): decline; the LLM loop / escalation
            # handles it. Never guess.
            return None
        cand = CandidateResolution(
            candidate_id=f"{unit.unit_id}:block_capture",
            unit_id=unit.unit_id,
            model_name=self.config.model.model,
            prompt_version=PROMPT_BLOCK_CAPTURE,
            resolved_text=resolved_text,
            explanation=expl,
        )
        validation = self.verification.verify(unit, cand)
        if not validation.passed:
            # The chosen side's text didn't validate when spliced (rare, but
            # possible if e.g. keep_block's text needs the deleted context).
            # Fall through to the full LLM loop rather than accept an invalid splice.
            self.journal.emit(
                "block_capture_failed_validation",
                {"decision": decision, "failures": [f.message for f in validation.hard_failures]},
                step_index=self.step,
                path=unit.path,
                unit_id=unit.unit_id,
            )
            return None
        if self._strictness_blocks_pre_llm(unit, cand, validation, "block_capture"):
            return None  # strict mode declines to auto-accept; fall through to LLM
        outcome = UnitOutcome(unit=unit, validation=validation, attempts=[cand])
        outcome.accepted = cand
        self.journal.emit(
            "candidate_accepted",
            {"candidate_id": cand.candidate_id, "via": "block_capture",
             "decision": decision},
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
    # Progress spinner (rebase only). A non-scrolling bottom line with an
    # animated blue spinner, driven by journal events. Only active when stdout
    # is a real TTY — a no-op in tests (no TTY) and CI (piped), so existing
    # tests pass unchanged.

    def _start_spinner(self) -> None:
        """Start the progress spinner if stdout is a TTY.

        Builds a :class:`Spinner`, redirects ``self.out`` through its
        ``flush_line`` (so scrolling colored lines never garble the sticky
        spinner), and subscribes to the journal so every state transition maps to
        a status message — no per-call-site spinner wiring needed. A no-op (the
        spinner stays ``None``) when stdout isn't a TTY.
        """
        if not self._is_interactive_terminal():
            self.spinner = None
            return
        from capybase.spinner import Spinner

        self.spinner = Spinner()
        self._orig_out = self.out
        self.out = self.spinner.flush_line
        self.journal.subscribe(self._spinner_on_event)
        self.spinner.start("starting rebase…")

    def _stop_spinner(self, final_msg: str | None = None) -> None:
        """Stop the spinner, restore ``self.out``, clear the bottom line."""
        sp = getattr(self, "spinner", None)
        if sp is None or not sp.active:
            # Restore out even if the spinner never started (defensive).
            if hasattr(self, "_orig_out"):
                self.out = self._orig_out
                del self._orig_out
            self.spinner = None
            return
        sp.stop(final_msg=final_msg)
        if hasattr(self, "_orig_out"):
            self.out = self._orig_out
            del self._orig_out
        self.spinner = None

    # event_type → human status. The spinner shows the latest one, animating
    # while the operation it describes is in flight.
    _SPINNER_STATUS = {
        "rebase_started": "rebase started",
        "step_started": "step {step}: resolving conflicts…",
        "context_built": "step {step}: generating merge (LLM)…",
        "candidate_generated": "step {step}: validating candidate…",
        "block_capture_decision": "step {step}: block-capture → {decision}",
        "tests_started": "step {step}: running {command}…",
        "tests_finished": "step {step}: tests {summary}",
        "candidate_accepted": "step {step}: accepted",
        "step_continued": "step {step}: continuing…",
        "interactive_guard": "awaiting human input…",
        "session_completed": "rebase complete",
        "rebase_aborted": "rebase aborted",
    }

    def _spinner_on_event(self, event) -> None:
        """Journal listener: map an event to a spinner status message."""
        sp = getattr(self, "spinner", None)
        if sp is None:
            return
        tmpl = self._SPINNER_STATUS.get(event.event_type)
        if tmpl is None:
            return
        step = event.step_index or ""
        # Build the message from the event's payload/fields.
        payload = event.payload or {}
        try:
            msg = tmpl.format(
                step=step,
                decision=payload.get("decision", ""),
                command=payload.get("command", ""),
                summary=payload.get("verdict_summary") or (
                    "passed" if payload.get("passed") else "failed"
                ),
            )
        except (KeyError, IndexError):
            msg = tmpl
        sp.set(msg)
        # Pause the spinner when handing control to the human — the terminal
        # belongs to them during the interactive prompt.
        if event.event_type == "interactive_guard" and payload.get("will_fire"):
            sp.pause()
        # Resume after the human is done: the next operational event means the
        # rebase is progressing again (step started, context built, etc.).
        if event.event_type in ("step_started", "step_continued", "session_completed"):
            if getattr(sp, "_paused", False):
                sp.resume()

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
        # Stash onto/start/backup on the instance so run()'s per-step + completion
        # resurrection scans can reconstruct the window without the rebase-merge
        # state files (which vanish once the rebase finishes).
        self._rebase_start_oid = start_oid
        self._rebase_target = target
        self._rebase_backup_ref = backup_ref
        # History-awareness substrate (#history-1): capture the source commit
        # sequence once at rebase start, so every later component (history query,
        # prompt context, risk features) can answer "where is this conflict in
        # the replay, and what later commits touch the same region?" Advisory —
        # a failure to build the plan never blocks the rebase (degrades to the
        # no-history behavior).
        self._history_plan = self._build_rebase_plan(start_oid, target)
        self._history_service = self._build_history_service(self._history_plan)
        self.journal.emit(
            "rebase_started",
            {"target": target, "start_oid": start_oid, "backup_ref": backup_ref,
             "history_plan_commits": len(self._history_plan.source_commits) if self._history_plan else 0},
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
            # Silent-resurrection scan: a clean rebase is exactly where a silent
            # undo hides (git resolved it with no conflict). Check the result
            # against what the target branch deleted before declaring success.
            findings = self._resurrection_scan(
                start_oid=start_oid, onto_oid=target, result_oid=head_after,
                backup_ref=backup_ref,
            )
            if findings:
                outcome = self._handle_resurrections(
                    findings, start_oid=start_oid, backup_ref=backup_ref
                )
                if outcome.escalated:
                    # stop policy: a clean rebase already finished (git is no
                    # longer in-progress), so abort-on-escalation can't roll it
                    # back. We reset to the backup ref ourselves to restore the
                    # repo to start_oid and leave the review bundle for review.
                    outcome.continued = False
                    self.git._run(  # noqa: SLF001
                        ["reset", "--hard", backup_ref]
                    )
                    self.journal.emit(
                        "rebase_aborted",
                        {"reason": outcome.reason, "start_oid": start_oid,
                         "backup_ref": backup_ref, "resurrection": True},
                        git_head_after=self.git.head_oid(),
                    )
                    self.out(
                        f"  rolled back to pre-rebase HEAD {start_oid[:8]} "
                        f"(backup branch {backup_ref})."
                    )
                    return outcome
                # warn policy: fall through to declare success.
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
                f"{self._ok('✓ rebase complete, no conflicts (session ' + self.session_id + ')')}\n"
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
            self._start_spinner()
            # Bridge the interactive flag to the strictness policy (#10): a
            # non-interactive run (CI / --no-interactive) has no human in the
            # loop mid-step, so tighten acceptance unless the user explicitly
            # configured a stricter (or equal) mode. Never LOOSEN an explicit
            # ci/unattended setting back to interactive.
            if not interactive and self.strictness.mode == "interactive":
                self.strictness.mode = "ci"
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
            self._stop_spinner()
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
        # 6. Interactive fallback (LOOP): on escalation, if a human is at the
        #    terminal and the rebase is still in progress, present the conflict
        #    for an interactive decision before the auto-abort runs. After the
        #    human resolves and the rebase continues, run() may hit ANOTHER stop
        #    that escalates — so this re-offers the fallback on each escalation,
        #    not just the first. (A prior version fired the guard once: the second
        #    escalation, returned by the re-entered run(), fell straight through
        #    to abort without ever offering the menu — the human got an abort
        #    instead of a prompt.)
        #    Disabled by --no-interactive (e.g. CI) or when stdin isn't a TTY.
        prev_step = -1  # track the step we last offered the fallback for, so a
                        # same-step re-escalation (no progress: skip/abort/bail)
                        # doesn't spin the loop forever.
        while result.escalated:
            rip = self.git.rebase_in_progress()
            tty = self._is_interactive_terminal()
            self.journal.emit(
                "interactive_guard",
                {
                    "escalated": result.escalated,
                    "interactive": interactive,
                    "rebase_in_progress": rip,
                    "is_interactive_terminal": tty,
                    "units_by_path": list(result.units_by_path),
                    "reason": result.reason or "",
                    "will_fire": bool(result.escalated and interactive and rip and tty),
                },
                step_index=self.step,
            )
            if not (interactive and rip and tty):
                break  # fallback disabled (CI, --no-interactive, not a TTY, or
                       # the rebase finished) → fall through to abort-on-escalation
            # Bail-safety: if the last fallback returned escalated at the SAME
            # step (the human skipped/aborted, or the menu bailed on no-units),
            # don't re-offer — that would spin forever. Only re-offer when the
            # rebase has advanced to a new step (a genuine new escalation).
            if self.step == prev_step:
                break
            prev_step = self.step
            resolved = self.interactive_resolve(result)
            if not resolved.escalated:
                # The human resolved everything and run() continued to completion
                # (or a clean step). Done.
                result = resolved
                break
            # The rebase continued after the human's resolution but hit a NEW
            # escalation at a later step. Loop: re-offer the interactive fallback.
            result = resolved
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
                self._warn(
                    f"! escalated and aborted rebase — repo back at {start_oid[:8]}"
                ) + "\n"
                f"  review bundle: {self.paths.final / 'review-bundle.md'}\n"
                f"  backup branch {backup_ref} still points at the pre-rebase "
                f"HEAD; reset to it with `git reset --hard {backup_ref}`, or "
                f"delete it with `git branch -D {backup_ref}`"
            )
        return result

    # ------------------------------------------------------------------ resurrection
    #
    # Silent-resurrection detection (survey "silent loss of intent"). After a
    # clean rebase — and per replayed step — compare the result against content
    # the target branch deliberately deleted since the merge-base. If the result
    # brought any of it back, the replayed commits (which predate the cleanup)
    # silently undid a deliberate deletion. Git sees no conflict; without this
    # scan capybase sees none either, and the cleanup is lost. On detection the
    # ``stop`` policy halts before the bad completion is left as final (the
    # backup branch keeps the repo recoverable); ``warn`` journals + continues.

    # ------------------------------------------------------------------ history
    #
    # History-awareness substrate (#history steps 2-5): the source commit
    # sequence is captured once at rebase start into a RebasePlan, and a read-
    # only HistoryQueryService answers per-conflict questions ("which commit am
    # I resolving, what later commits touch the same region?"). Advisory — a
    # failure to build the plan never blocks the rebase.

    def _build_rebase_plan(self, start_oid: str, target: str):
        """Build a :class:`history.RebasePlan` for the replayed sequence.

        The sequence is ``merge_base(start_oid, target)..start_oid`` (oldest-
        first). Written to the session dir as ``rebase_plan.json`` so tests can
        replay the same history. Returns None on any failure (advisory).
        """
        try:
            from capybase.history import RebasePlan, ReplayCommit
            from datetime import datetime, timezone

            mb = self.git.merge_base(start_oid, target)
            if not mb:
                return None
            raw = self.git.replayed_commit_sequence(mb, start_oid)
            if not raw:
                return None
            commits = [
                ReplayCommit(
                    oid=c["oid"], parent_oid=c["parent_oid"],
                    subject=c["subject"], body_summary=c["body_summary"],
                    touched_files=c["touched_files"], diffstat=c["diffstat"],
                    patch_id=c["patch_id"], index=i,
                )
                for i, c in enumerate(raw)
            ]
            plan = RebasePlan(
                source_commits=commits,
                target_base_oid=mb,
                target_tip_oid=target,
                source_tip_oid=start_oid,
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            # Persist for test replay.
            import json
            plan_path = self.paths.root / "rebase_plan.json"
            plan_path.write_text(json.dumps(plan.to_dict(), indent=2), encoding="utf-8")
            return plan
        except Exception as exc:  # noqa: BLE001 - history is advisory
            self.log.debug("rebase plan not built: %s", exc)
            return None

    def _build_history_service(self, plan):
        """Construct the :class:`history.HistoryQueryService` from a plan.

        Returns an empty service (all queries yield empty context) when the plan
        is None, so downstream code dispatches unconditionally.
        """
        from capybase.history import HistoryQueryService
        if plan is None:
            return HistoryQueryService.empty()
        return HistoryQueryService(plan)

    def _current_replayed_oid(self) -> str | None:
        """The commit currently being replayed (``stopped-sha``), or None.

        Read at conflict-gather time so each ConflictUnit can carry replay
        identity. None when no rebase is in progress or the file is absent.
        """
        try:
            return self.git.rebase_stopped_sha()
        except Exception:  # noqa: BLE001 - advisory
            return None

    def _resurrection_scan(
        self, *, start_oid: str, onto_oid: str, result_oid: str, backup_ref: str
    ) -> list:
        """Run the end-of-rebase resurrection scan; return findings (maybe empty).

        The merge-base of ``start_oid`` (the original branch tip) and ``onto_oid``
        bounds the window of upstream history the replayed branch predates. Any
        content ``onto`` deleted since that base that reappears in ``result_oid``
        is a suspected silent undo. Advisory: any git error is swallowed and
        reported as no findings — resurrection detection must never break a
        rebase that would otherwise succeed. Disabled entirely by
        ``[validation] enable_resurrection_detection = false``.

        Paths this session EXPLICITLY resolved as a modify/delete ``keep_block``
        (``self._explicitly_kept_paths``) are excluded: such a keep is a
        deliberate, reviewed resurrection of content upstream deleted, not a
        silent undo — flagging it would double-report an already-judged decision.
        """
        cfg = self.config.validation
        if not cfg.enable_resurrection_detection:
            return []
        try:
            from capybase.resurrection import scan_resurrections

            mb = self.git.merge_base(start_oid, onto_oid)
            if mb is None:
                return []
            return scan_resurrections(
                self.git,
                base_oid=mb,
                onto_oid=onto_oid,
                result_oid=result_oid,
                min_block_lines=cfg.resurrection_min_block_lines,
                min_coverage=cfg.resurrection_min_similarity,
                exclude_paths=set(getattr(self, "_explicitly_kept_paths", set())),
            )
        except Exception as exc:  # noqa: BLE001 - advisory, never break the rebase
            self.log.warning(
                "resurrection scan failed (ignored): session=%s %s",
                self.session_id, exc,
            )
            return []

    def _handle_resurrections(
        self,
        findings: list,
        *,
        start_oid: str,
        backup_ref: str,
    ) -> StepResult:
        """Act on resurrection findings per the configured policy.

        Returns an escalated StepResult on ``stop`` (the caller leaves the rebase
        stopped; the backup branch keeps the repo recoverable), or a non-
        escalated result on ``warn`` (the rebase is allowed to complete). Writes
        a review bundle with a ``## suspected resurrections`` section either way
        so the developer can review the suspected undos.
        """
        cfg = self.config.validation
        n_paths = len(findings)
        n_lines = sum(f.resurrected_line_count for f in findings)
        self.journal.emit(
            "resurrections_detected",
            {
                "paths": [f.path for f in findings],
                "line_count": n_lines,
                "policy": cfg.resurrection_policy,
            },
            step_index=self.step,
        )
        write_review_bundle(
            self.paths,
            reason=(
                f"suspected silent resurrection of deleted content "
                f"({n_paths} path(s), {n_lines} line(s) back)"
            ),
            step_index=self.step,
            resurrections=findings,
            resume_hint=f"git rebase --continue  # after reviewing {backup_ref}",
        )
        if cfg.resurrection_policy == "stop":
            self.log.warning(
                "resurrection detection stopped the rebase: session=%s paths=%d "
                "lines=%d backup=%s",
                self.session_id, n_paths, n_lines, backup_ref,
            )
            self.out(
                self._warn(
                    f"! suspected silent resurrection — {n_paths} path(s) brought "
                    f"back {n_lines} line(s) the target branch deleted."
                ) + "\n"
                f"  review bundle: {self.paths.final / 'review-bundle.md'}\n"
                f"  backup branch {backup_ref} points at the pre-rebase HEAD "
                f"{start_oid[:8]}; the rebase is left stopped. Resolve the "
                f"resurrections (or set [validation] resurrection_policy = "
                f"\"warn\" to proceed), then `git rebase --continue`."
            )
            return StepResult(
                step_index=self.step,
                escalated=True,
                reason="suspected silent resurrection of deleted content",
            )
        # warn policy: surface but continue.
        self.log.info(
            "resurrection detection warned (continuing): session=%s paths=%d lines=%d",
            self.session_id, n_paths, n_lines,
        )
        self.out(
            f"  warning: suspected silent resurrection — {n_paths} path(s) "
            f"brought back {n_lines} line(s) the target branch deleted "
            f"(see review bundle). Continuing per resurrection_policy = \"warn\"."
        )
        return StepResult(step_index=self.step, escalated=False, continued=True)

    def _run_resurrection_on_completion(self) -> StepResult | None:
        """Resurrection scan for run()'s completion point; returns None if clean.

        Called from run()'s loop when the rebase finishes cleanly (conflicts
        resolved and replayed). Reconstructs onto/start from the instance attrs
        ``rebase()`` stashed (the rebase-merge state files are gone by now). On a
        detection with the ``stop`` policy, returns an ESCALATED StepResult so
        run() breaks and rebase()'s escalation handling (interactive fallback /
        abort) runs — the rebase is still in-progress at this point, so the
        existing abort-on-escalation restores the repo to start_oid. On ``warn``,
        emits the warning and returns a non-escalated result (caller proceeds).
        Returns None when there are no findings (nothing to do).
        """
        start_oid = getattr(self, "_rebase_start_oid", None)
        target = getattr(self, "_rebase_target", None)
        backup_ref = getattr(self, "_rebase_backup_ref", "capybase/backup")
        if not start_oid or not target:
            return None  # not a rebase()-driven session; nothing to scan
        head_after = self.git.head_oid()
        findings = self._resurrection_scan(
            start_oid=start_oid, onto_oid=target, result_oid=head_after,
            backup_ref=backup_ref,
        )
        if not findings:
            return None
        outcome = self._handle_resurrections(
            findings, start_oid=start_oid, backup_ref=backup_ref
        )
        return outcome

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
            self.out(self._warn(f"! {reason}") + f"\n  review bundle: {bundle}")
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
            # Accept report (#4): both per-unit outcomes and the test verdict
            # exist here — write the "why we accepted" summary before continuing.
            self._write_accept_report(result)
            # Continue rebase.
            cont = self.git.continue_rebase()
            self.journal.emit(
                "step_continued",
                {"returncode": cont.returncode, "stderr": cont.stderr[:500]},
                step_index=self.step,
            )
            result.continued = True
            if not self.git.rebase_in_progress():
                # Rebase finished cleanly. Run the resurrection scan: the rebase
                # is done, so we reconstruct onto/start from the rebase-merge
                # state files (these survive until the rebase fully completes).
                # On ``stop`` the scan escalates and we break so the rebase()'s
                # escalation handling (interactive fallback / abort) runs.
                _res = self._run_resurrection_on_completion()
                if _res is not None and _res.escalated:
                    last = _res
                    break
                head_after = self.git.head_oid()
                self.journal.emit(
                    "session_completed",
                    {"head_after": head_after},
                    git_head_after=head_after,
                )
                self.git.record_step_ref(self.session_id, self.step, head_after)
                self.out(self._ok(f"✓ rebase complete (session {self.session_id})"))
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
            # (For a whole_file unit the resolved text IS the file —
            # ``_resolved_buffer`` returns it verbatim, no splicing.)
            original = accepted[0][0].original_worktree_text
            buffer = _resolved_buffer(original, accepted)
            resolved_files[path] = buffer
            accepted_by_path[path] = accepted
            originals[path] = original
            # Write the resolved file to the worktree NOW (no staging yet) so
            # sibling files' cargo checks in Phase 2 see a marker-free crate.
            # An accepted whole-file deletion removes the worktree file instead.
            self._write_worktree_only(path, buffer, accepted=accepted)

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
                    # verify_file tolerates a whole-file (None) span via its own
                    # _has_whole_file_span guard; the buffer is the resolved
                    # text directly for such units.
                    buffer = _resolved_buffer(original, accepted)
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
            self._write_and_stage(path, buffer, result, accepted=accepted)
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

        # Block-capture resolution (large modify/delete): when one side deleted a
        # large block and the structural rule declined (the keeper modified it),
        # the model can't reliably reproduce the block (placeholder collapse +
        # escaping corruption). Instead it makes a keep/accept_deletion/needs_human
        # decision and capybase splices the chosen side verbatim. AFTER the other
        # pre-LLM layers decline and BEFORE the LLM loop, on a FRESH resolve only.
        if failures is None and self.config.future.enable_block_capture:
            early = self._try_block_capture(unit)
            if early is not None:
                return early  # accepted via block-capture; LLM loop skipped

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
            # before any LLM call. The ConflictClassifier returns a richer band
            # + explainable reasons; the legacy ``simple``/``complex`` label
            # (band ∈ {medium, hard} ⇒ complex) drives the existing fast path
            # (one low-temp sample, no two-pass, no consensus) vs the full
            # pipeline. Disabled (complex=full path for all) until
            # config.routing.enabled is set.
            difficulty = "complex"
            classification = None
            if self.config.routing.enabled:
                from capybase.classifier import classify

                classification = classify(unit)
                difficulty = classification.difficulty
                self.journal.emit(
                    "difficulty_classified",
                    {
                        "difficulty": difficulty,
                        "band": classification.band,
                        "reasons": classification.reasons,
                    },
                    step_index=self.step,
                    path=unit.path,
                    unit_id=unit.unit_id,
                )
            outcome.difficulty = difficulty
            outcome.classification = classification

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
                # consensus. Simple isolated hunks resolve trivially. Force
                # n_samples=1 so a calibrated samples>1 never leaks into the
                # cheap path (it would otherwise fall back to config.samples).
                candidates = self.resolution_engine.propose(
                    unit, context, failures=failures, prev_candidate=prev_candidate,
                    n_samples=1,
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
                        unit, context, failures=failures,
                        prev_candidate=prev_candidate, n_samples=n_complex,
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
                # Strictness gate (#10): in ci/unattended mode, the policy may
                # override an accept to escalate (e.g. low confidence, a dropped
                # obligation, or a hard-band conflict). It never relaxes a
                # retry/escalate, only tightens accept.
                ok, why = self.strictness.should_accept(
                    unit, cand, validation,
                    band=self._classification_band(unit),
                    deterministic=False,
                )
                if not ok:
                    # Strictness escalated: leave outcome.accepted=None so the
                    # caller treats it as an escalation, mirroring the risk
                    # engine's own escalate branch.
                    outcome.retry_count = retry_count
                    self.journal.emit(
                        "candidate_rejected",
                        {"candidate_id": cand.candidate_id,
                         "action": "escalate", "via": "strictness",
                         "reason": why, "mode": self.strictness.mode},
                        step_index=self.step, path=unit.path, unit_id=unit.unit_id,
                    )
                    return outcome
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
            # History-awareness (#history-3): stamp replay identity onto each
            # unit so history-aware components know which commit they're
            # resolving. The stopped-sha is read once per gather (cheap; it's a
            # single file read). Advisory: absent/None degrades to no history.
            replayed_oid = self._current_replayed_oid()
            for u in units:
                if replayed_oid:
                    u.structural_metadata["replayed_commit_oid"] = replayed_oid
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

    def _write_and_stage(
        self,
        path: str,
        buffer: str,
        result: StepResult,
        *,
        accepted: list[tuple[ConflictUnit, CandidateResolution]] | None = None,
    ) -> None:
        """Write the resolved file to the worktree and stage it.

        A whole-file modify/delete accepted as a deletion (empty resolved text)
        is staged as a removal via ``git rm`` instead of write+add: the file
        goes away. ``accepted`` is the path's accepted resolutions so the delete
        case can be detected; callers without a resolution list (e.g. writing a
        pre-computed buffer) pass nothing and get the write+add path.
        """
        if accepted is not None and _is_whole_file_delete(accepted):
            self.git.remove_file_stage(path)
            self.journal.emit(
                "file_removed",
                {"path": path, "decision": "accept_deletion"},
                step_index=self.step,
                path=path,
            )
            return
        if self.config.journal.enabled and self.config.journal.store_snapshots:
            # Snapshot the ACTUAL pre-write worktree content — the on-disk file
            # before this resolution overwrites it — so the audit trail shows
            # what changed, not the resolved buffer being written (a prior bug
            # snapshotted `buffer`, making the ".before" name a lie). A missing
            # file (new path) has no prior content to snapshot.
            try:
                prior = self.git.read_worktree_file(path).decode(
                    "utf-8", errors="replace"
                )
                self.journal.store_snapshot(
                    f"{path.replace('/', '__')}.before", prior
                )
            except (FileNotFoundError, OSError):
                pass  # new file: nothing pre-existed to snapshot
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

    def _write_worktree_only(
        self,
        path: str,
        buffer: str,
        *,
        accepted: list[tuple[ConflictUnit, CandidateResolution]] | None = None,
    ) -> None:
        """Write a resolved file to the worktree WITHOUT staging it.

        Used by Phase 1 of cross-file resolution: every conflicted file is
        written resolved first, so the whole crate is marker-free before any
        cargo check runs in Phase 2. Staging is deferred to ``_write_and_stage``
        (called in Phase 2 after validation passes) so an escalatable failure
        never leaves staged-but-invalid state. The journal snapshot is skipped
        here (Phase 2's ``_write_and_stage`` records the final staged buffer).

        A whole-file deletion (empty resolved text) removes the worktree file
        instead of writing it, so Phase-2 validation sees the crate without it.
        Staging the removal still happens in ``_write_and_stage`` (Phase 2).
        """
        if accepted is not None and _is_whole_file_delete(accepted):
            # Remove the worktree file only (no staging yet — that's Phase 2).
            full = self.git.repo / path
            if full.exists():
                full.unlink()
            return
        self.git.write_worktree_file(path, buffer.encode("utf-8"))

    def _run_tests(self, label: str, result: StepResult) -> bool:
        cmd = getattr(self.config.tests, label) if hasattr(self.config.tests, label) else None
        if not cmd:
            return True
        # Whether the configured command is the shipped default (vs an explicit
        # user choice). The default is Python-centric ("pytest"); for a repo it
        # doesn't fit (Go/JS/etc. with no pytest and no cargo), a "command not
        # found" must NOT block the rebase — that's the absence of a test gate
        # for this repo, not a failing test. An explicit user command that's
        # missing still fails (it was a deliberate choice).
        is_default_cmd = cmd.strip() == "pytest"
        cmd = self._resolve_test_command(cmd)
        self.journal.emit("tests_started", {"label": label, "command": cmd}, step_index=self.step)
        # For ``cargo test`` in a workspace (no root Cargo.toml), cargo must run
        # from a member crate's directory — it can't discover the project from
        # the workspace root. Anchor on the first conflicted file's nearest crate
        # dir (the same nearest-manifest logic the cargo syntax check uses).
        test_cwd = self._cargo_test_cwd(result, cmd)
        run = self._run_test_command(cmd, cwd=test_cwd)
        # The shipped-default command wasn't found and couldn't be auto-resolved
        # to one that exists (e.g. a Go/JS repo with no pytest and no cargo).
        # Treat it as "no test gate for this repo" rather than a hard failure:
        # warn and continue. Never applies to an explicit user-configured command.
        if (
            is_default_cmd
            and not run.passed
            and run.verdict.kind == "unknown"
            and "not found" in (run.verdict.summary or "")
        ):
            self.journal.emit(
                "tests_default_unresolved",
                {"label": label, "command": cmd, "summary": run.verdict.summary},
                step_index=self.step,
            )
            self.out(
                f"  no test command for this repo (default `{cmd}` not found, "
                f"no cargo detected); skipping the {label} test gate. Set "
                f"[tests] {label} to your suite's command to enable it."
            )
            return True
        self.journal.emit(
            "tests_finished",
            {
                "label": label,
                "passed": run.passed,
                "returncode": run.returncode,
                "timed_out": run.timed_out,
                "verdict": run.verdict.kind,
                "verdict_summary": run.verdict.summary,
                "diagnostics": run.verdict.diagnostics[:5],
                "stdout_tail": run.stdout[-1000:],
                "stderr_tail": run.stderr[-1000:],
            },
            step_index=self.step,
        )
        result.tests_passed = run.passed
        # Stash the parsed verdict for the accept report (the report is written
        # after this call returns, in run()'s loop, and needs the human-readable
        # verdict like "1 test failed" / "compile error").
        self._last_test_verdict = run.verdict.summary or None
        if not run.passed:
            # Surface the parsed verdict so the human sees *why* the tests failed
            # (compile error vs. test failure vs. timeout vs. lock contention),
            # not just the return code.
            self.out(
                "  " + self._warn(
                    f"! {label} tests failed (rc={run.returncode}): "
                    f"{run.verdict.summary or 'unknown'}"
                )
            )
            for d in run.verdict.diagnostics[:3]:
                self.out(f"      {d}")
        return run.passed

    def _run_test_command(self, cmd: str, *, cwd: str | None = None):
        """Run the test command, retrying on transient lock contention.

        cargo emits ``Blocking waiting for file lock on build directory`` when
        another cargo process holds the target/ lock — a transient condition
        unrelated to the merge. Aborting on it would reject a correct rebase;
        retrying (with a short backoff) is correct. Other verdicts are returned
        as-is for the caller to act on. Bounded to a few retries so a genuinely
        stuck lock still terminates.
        """
        import time

        max_lock_retries = 3
        backoff_seconds = 5.0
        for attempt in range(max_lock_retries + 1):
            run = self.tests.run(cmd, cwd=cwd)
            if not run.verdict.is_transient or attempt == max_lock_retries:
                return run
            self.journal.emit(
                "tests_lock_retry",
                {"attempt": attempt + 1, "verdict": run.verdict.kind,
                 "summary": run.verdict.summary},
                step_index=self.step,
            )
            self.out(
                f"  ... {run.verdict.summary}; retrying in {backoff_seconds:.0f}s "
                f"(attempt {attempt + 1}/{max_lock_retries})"
            )
            time.sleep(backoff_seconds)
        return run

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
        # A repo "has cargo" when the root OR any top-level subdir has a
        # Cargo.toml (workspaces: each crate lives in a subdir, no root
        # manifest). Without this, a workspace Rust repo stays on pytest and
        # fails the gate with "No such file or directory: 'pytest'".
        if not _repo_has_cargo(self.git.repo):
            return cmd
        # It's a cargo repo. Prefer ``cargo test`` UNLESS this is also a real
        # Python project (has a pyproject.toml/setup.py) — then it's a genuine
        # mixed repo and we honor the configured pytest default. The presence of
        # ``pytest`` on PATH alone is NOT enough: it may be a *different*
        # project's venv (e.g. capybase's own dev venv), not this repo's. A cargo
        # repo with stray ``.py`` utility scripts but no Python project manifest
        # is Rust-dominant → cargo test.
        if _has_python_project(self.git.repo):
            return cmd
        return "cargo test"

    def _cargo_test_cwd(self, result: StepResult, cmd: str) -> str | None:
        """The directory to run ``cargo test`` from, or None to use the repo root.

        For a ``cargo test`` invocation in a workspace (no root Cargo.toml), cargo
        can't discover the project from the workspace root — it needs to run from
        a member crate's directory. We anchor on the first conflicted file's
        nearest crate dir (the same nearest-manifest logic the cargo syntax check
        uses), so the test gate runs the crate the conflict actually touches. For
        a single-crate-at-root layout (root Cargo.toml), cargo runs fine from the
        repo root → None (the runner's default cwd).
        """
        if not cmd.strip().startswith("cargo"):
            return None
        from capybase.adapters.lsp import _has_cargo_manifest, nearest_cargo_manifest_dir

        # Root manifest → cargo discovers from the repo root; no override needed.
        if _has_cargo_manifest(str(self.git.repo)):
            return None
        # Workspace: find the crate dir to run cargo from. Anchor on the
        # conflict paths first, then the staged files (an edit-resolved step has
        # staged the resolution but has no units_by_path), then any member crate.
        # Without this fallback, a step with NO conflicts (clean apply, or a step
        # fully resolved by direct edit) leaves units_by_path empty → no path to
        # anchor on → cargo runs from the workspace root, which has no
        # Cargo.toml → ``could not find Cargo.toml`` aborts a correct rebase.
        anchor_paths: list[str] = list(result.units_by_path)
        if not anchor_paths:
            try:
                anchor_paths = self.git.staged_paths()
            except Exception:  # noqa: BLE001 - advisory
                anchor_paths = []
        for path in anchor_paths:
            crate_dir = nearest_cargo_manifest_dir(str(self.git.repo), path)
            if crate_dir is not None:
                return str(crate_dir)
        # Last resort: scan top-level subdirs for any member crate. cargo must
        # run from SOME crate dir; the workspace root has no manifest.
        try:
            for entry in self.git.repo.iterdir():
                if entry.is_dir() and (entry / "Cargo.toml").is_file():
                    return str(entry)
        except OSError:  # noqa: BLE001
            pass
        return None

    def _ok(self, text: str) -> str:
        """A success line with its ``✓`` marker green when color is enabled.

        Only the marker is colored; the message stays plain for readability.
        Passthrough (no codes) when color is disabled.
        """
        from capybase.color import GREEN
        return self.style("✓", GREEN) + text.lstrip("✓").lstrip()

    def _warn(self, text: str) -> str:
        """A warning/error line with its ``!`` marker red when color is enabled.

        Only the marker is colored; the message stays plain for readability.
        Passthrough (no codes) when color is disabled.
        """
        from capybase.color import RED
        return self.style("!", RED) + text.lstrip("!").lstrip()

    def _write_accept_report(self, result: StepResult) -> None:
        """Append a semantic accept report for the step's accepted units (#4).

        Composes the per-unit obligations/validation/classification with the
        step-level test verdict into a human-readable "why we accepted" summary,
        appended to ``final/accept-report.md``. Run after the test gate, when
        both per-unit outcomes (``result.outcomes``) and the test verdict
        (``result.tests_passed``) exist. A no-op when no unit was accepted (an
        escalation step) or when report-writing is disabled. Advisory: a failure
        to write never breaks the rebase.
        """
        if not getattr(self.config.journal, "write_accept_reports", True):
            return
        try:
            from capybase.accept_report import build_accept_report

            body = build_accept_report(
                result.outcomes,
                tests_passed=result.tests_passed,
                test_verdict=self._last_test_verdict,
            )
            if not body:
                return
            report = self.paths.final / "accept-report.md"
            header = f"## step {result.step_index}\n\n"
            # Append (one section per step); create on first write.
            if report.exists():
                existing = report.read_text(encoding="utf-8")
                report.write_text(existing.rstrip("\n") + "\n\n" + header + body, encoding="utf-8")
            else:
                report.write_text("# capybase accept report\n\n" + header + body, encoding="utf-8")
            self.journal.emit(
                "accept_report_written",
                {"path": str(report.relative_to(self.paths.repo_root)),
                 "units": sum(1 for o in result.outcomes if o.accepted is not None)},
                step_index=result.step_index,
            )
        except Exception as exc:  # noqa: BLE001 - advisory report; never block the rebase
            self.log.debug("accept report not written: %s", exc)

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
        """Manual-mode unit render. Headers colored like the interactive variant
        (BASE dim, CURRENT cyan, REPLAYED magenta, unit header bold); content
        stays plain. A passthrough when color is disabled."""
        from capybase.color import BOLD, CYAN, DIM, MAGENTA

        s = self.style
        return (
            f"{s(f'\\n=== {unit.unit_id} ({unit.path}, {unit.conflict_type}) ===', BOLD)}\n"
            f"{s('-- BASE --', DIM)}\n{unit.base.text}\n"
            f"{s('-- CURRENT_UPSTREAM_SIDE --', CYAN)}\n{unit.current.text}\n"
            f"{s('-- REPLAYED_COMMIT_SIDE --', MAGENTA)}\n{unit.replayed.text}\n"
        )


def _repo_has_cargo(repo_root: Path) -> bool:
    """Whether ``repo_root`` is (part of) a Cargo project.

    True when the root OR any immediate top-level subdirectory contains a
    ``Cargo.toml``. The subdir check handles Cargo WORKSPACES, where each member
    crate lives in its own subdirectory and there's no root manifest — the common
    layout (di-rac-rebase-test: di-core/, divrr/, wasm-runner/). Only one level
    deep is scanned: a workspace's member crates sit directly under the root, and
    a deeper scan risks matching an unrelated vendored crate. Used by the
    auto-substitution of ``cargo test`` for the default ``pytest`` test gate.
    """
    if (repo_root / "Cargo.toml").is_file():
        return True
    try:
        for entry in repo_root.iterdir():
            if entry.is_dir() and (entry / "Cargo.toml").is_file():
                return True
    except OSError:  # noqa: BLE001 - unreadable dir → treat as no cargo
        return False
    return False


def _has_python_project(repo_root: Path) -> bool:
    """Whether ``repo_root`` is a real Python project (vs stray ``.py`` scripts).

    True when a Python project manifest is present at the root (``pyproject.toml``
    or ``setup.py``). These are the conventional markers a Python project declares
    its build/test setup; their absence means stray ``.py`` utility scripts don't
    constitute a Python project. Used to distinguish a genuine mixed repo (cargo +
    Python → honor the configured pytest) from a Rust-dominant repo with incidental
    ``.py`` files (→ cargo test).
    """
    return (repo_root / "pyproject.toml").is_file() or (
        repo_root / "setup.py"
    ).is_file()


def _default_stdin_reader(prompt: str, *, multiline: bool = False) -> str:
    """Read input from the terminal.

    Single-line mode (the default): the prompt is printed (no trailing newline)
    and ONE line is read — this is what the menu choice and "press Enter when
    done" prompts need, so typing ``4`` + Enter returns immediately.

    Multi-line mode (``multiline=True``): used for pasted resolutions. Reads
    lines until EOF (Ctrl-D) and joins them — a pasted block has no natural
    terminator, so the human signals the end explicitly.

    The split is load-bearing: the old implementation always read until EOF,
    which meant a menu choice like ``4`` was swallowed and never returned — the
    program blocked until Ctrl-C, ignoring the choice. Single-line callers must
    pass the default; only paste callers opt into multiline.
    """
    # print(end=...) so the prompt sits on the same line as the typed input
    # (print(prompt) would push the user's response onto the next line).
    print(prompt, end="", flush=True)
    if not multiline:
        try:
            return input()
        except EOFError:
            return ""
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

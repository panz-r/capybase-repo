"""Verification: plugin validators producing structured VerificationResults.

Every check is a small ``Validator`` with a stable ``name`` and a ``verify``
method that returns a ``VerificationCheckResult``. The engine aggregates
checks into one ``VerificationResult`` and collects machine-learnable
``features`` on the way — the future calibration spine.

MVP validators are text-level (no tree-sitter). Later plugins
(``PyrightValidator``, ``CargoCheckValidator``, ``SemgrepValidator``,
``MutationValidator``, ``VerifierModelValidator``, ``ConformalRiskValidator``)
drop in without orchestrator changes.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from capybase.adapters.parsers import (
    contains_markers,
    splice_all_resolutions,
    splice_resolution,
)
from capybase.conflict_model import (
    CandidateResolution,
    ConflictUnit,
    VerificationFailure,
    VerificationResult,
    VerificationWarning,
)


@dataclass
class VerificationContext:
    """All inputs a validator may need."""

    unit: ConflictUnit
    candidate: CandidateResolution
    config: "ValidationConfig"


@dataclass
class VerificationCheckResult:
    name: str
    passed: bool
    severity: str = "error"  # "error" | "warning"
    message: str = ""
    detail: dict = field(default_factory=dict)
    features: dict[str, float | int | str | bool] = field(default_factory=dict)


@runtime_checkable
class Validator(Protocol):
    name: str

    def verify(self, ctx: VerificationContext) -> VerificationCheckResult: ...


# Lightweight config mirror to avoid an import cycle with config.py.
@dataclass
class ValidationConfig:
    require_no_markers: bool = True
    require_exact_splice_scope: bool = True
    require_syntax_if_supported: bool = True
    reject_if_copies_one_side: bool = True
    reject_if_model_needs_human: bool = True
    require_whole_file_validation: bool = True
    require_ast_preservation: bool = True
    enable_lsp_diagnostics: bool = False
    pyright_path: str = "pyright"
    rust_analyzer_path: str = "rust-analyzer"
    cargo_path: str = "cargo"
    lsp_baseline_strict: bool = True
    enable_shadow_tests: bool = False
    # Verifier-model critic (mirrors config.ValidationConfig; the live flags).
    enable_verifier_model: bool = False
    verifier_severity: str = "warning"

    @classmethod
    def from_dict(cls, d: dict) -> "ValidationConfig":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# Individual validators
# ---------------------------------------------------------------------------


class NoConflictMarkersValidator:
    name = "no_conflict_markers"

    def verify(self, ctx: VerificationContext) -> VerificationCheckResult:
        text = ctx.candidate.resolved_text
        leaked = contains_markers(text)
        return VerificationCheckResult(
            name=self.name,
            passed=not leaked,
            message="resolved text still contains conflict markers"
            if leaked
            else "no conflict markers",
            features={"markers_remaining": int(leaked)},
        )


class ExactSpliceScopeValidator:
    """The resolved text, when spliced, must not change lines outside the
    conflict block — i.e. splicing only replaces the marker block."""

    name = "exact_splice_scope"

    def verify(self, ctx: VerificationContext) -> VerificationCheckResult:
        unit = ctx.unit
        if unit.marker_span is None:
            return VerificationCheckResult(
                name=self.name, passed=True, message="no marker span (non-marker unit)"
            )
        before = unit.original_worktree_text
        after = splice_resolution(before, unit.marker_span, ctx.candidate.resolved_text)
        start, end = unit.marker_span
        before_lines = before.split("\n")
        after_lines = after.split("\n")
        # Lines strictly before the block must be identical...
        head_ok = before_lines[:start] == after_lines[:start]
        # ...and the trailing lines (everything after the block) must be too.
        expected_tail = before_lines[end + 1 :]
        actual_tail = after_lines[len(after_lines) - len(expected_tail):] if expected_tail else []
        tail_ok = expected_tail == actual_tail
        passed = head_ok and tail_ok
        return VerificationCheckResult(
            name=self.name,
            passed=passed,
            severity="error",
            message=(
                "splice touched lines outside the conflict block"
                if not passed
                else "splice confined to marker block"
            ),
            detail={"head_preserved": head_ok, "tail_preserved": tail_ok},
            features={"splice_scope_ok": passed},
        )


class PreservationHeuristicValidator:
    """Detect when a candidate copies one side verbatim and drops the other.

    Copying one side wholesale is a strong signal the model didn't actually
    merge — it picked a winner. We flag it so risk policy can retry/escalate.
    """

    name = "preservation_heuristic"

    def verify(self, ctx: VerificationContext) -> VerificationCheckResult:
        cur = ctx.unit.current.text.strip()
        rep = ctx.unit.replayed.text.strip()
        resolved = ctx.candidate.resolved_text.strip()
        copied_current = bool(cur) and resolved == cur
        copied_replayed = bool(rep) and resolved == rep
        copied_one = copied_current or copied_replayed
        return VerificationCheckResult(
            name=self.name,
            passed=not copied_one,
            severity="warning",
            message=(
                "resolved text copies one side verbatim"
                if copied_one
                else "resolved text differs from both sides"
            ),
            detail={
                "copied_current": copied_current,
                "copied_replayed": copied_replayed,
            },
            features={
                "copied_one_side": copied_one,
                "copied_current_side": copied_current,
                "copied_replayed_side": copied_replayed,
            },
        )


class NeedsHumanValidator:
    name = "needs_human"

    def verify(self, ctx: VerificationContext) -> VerificationCheckResult:
        nh = bool(ctx.candidate.needs_human)
        return VerificationCheckResult(
            name=self.name,
            passed=not nh,
            severity="error",
            message="model self-reported needs_human=true" if nh else "model did not request human",
            features={"model_needs_human": nh},
        )


class VerifierModelValidator:
    """LLM critic that checks a resolution preserves BOTH sides' intent.

    This is the verifier-model seam (surveys §1/§5 Proposer-Critic): every
    other validator is syntactic/structural — conflict markers, splice scope,
    AST preservation, syntax, LSP diagnostics, one-side-copy heuristic. None can
    catch a merge that parses cleanly but *semantically drops a side's intent*
    (e.g. it omits a guard one branch added). An LLM judge is the one check for
    that, run on the same black-box API client already in the orchestrator.

    Cost & safety contract:

    - **Opt-in.** Inert (no LLM call) unless ``enable_verifier_model`` is on.
      The gate is read from ``ctx.config`` so it mirrors the LSP/shadow wiring.
    - **Graceful degrade.** Any failure to call the client or parse the verdict
      yields ``verifier_checked=False`` and ``passed=True`` — a flaky or
      malformed critic must never crash resolution or reject a valid merge.
    - **Severity configurable.** Defaults to ``"warning"`` (bias toward
      retry/escalate, not hard-reject); strict deployments set ``"error"``.

    The client is injected at construction (the ``Validator.verify`` protocol
    only receives a ``VerificationContext``, which carries no client).
    """

    name = "verifier_model"

    def __init__(self, client: object, model_name: str = "") -> None:
        # ``client`` is the same LLMClient the resolution engine uses. Typed as
        # ``object`` to avoid an import cycle (adapters → ... → verification);
        # it only needs a ``complete`` method.
        self.client = client
        self.model_name = model_name

    def verify(self, ctx: VerificationContext) -> VerificationCheckResult:
        cfg = ctx.config
        if not getattr(cfg, "enable_verifier_model", False):
            return VerificationCheckResult(
                name=self.name,
                passed=True,
                severity=getattr(cfg, "verifier_severity", "warning"),
                message="verifier model disabled",
                features={"verifier_checked": False},
            )
        from capybase.resolution_engine import build_verifier_prompt
        from capybase.adapters.parsers import parse_resolution_json

        prompt = build_verifier_prompt(ctx.unit, ctx.candidate, _verifier_context(ctx))
        messages = [
            {"role": "system", "content": "You are a strict code reviewer."},
            {"role": "user", "content": prompt},
        ]
        try:
            resp = self.client.complete(
                messages,
                model=self.model_name or _default_model(ctx),
                temperature=0.0,
                max_tokens=512,
                json_mode=True,
            )
        except Exception:  # noqa: BLE001 - degrade, never crash resolution
            return VerificationCheckResult(
                name=self.name,
                passed=True,
                severity=getattr(cfg, "verifier_severity", "warning"),
                message="verifier model call failed; skipped",
                features={"verifier_checked": False},
            )
        data, _ = parse_resolution_json(resp.text or "")
        if not data:
            return VerificationCheckResult(
                name=self.name,
                passed=True,
                severity=getattr(cfg, "verifier_severity", "warning"),
                message="verifier model returned unparseable verdict; skipped",
                features={"verifier_checked": False},
            )
        preserves_current = bool(data.get("preserves_current", True))
        preserves_replayed = bool(data.get("preserves_replayed", True))
        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        preserves_both = preserves_current and preserves_replayed
        dropped = []
        if not preserves_current:
            dropped.append("current")
        if not preserves_replayed:
            dropped.append("replayed")
        return VerificationCheckResult(
            name=self.name,
            passed=preserves_both,
            severity=getattr(cfg, "verifier_severity", "warning"),
            message=(
                "resolution preserves both sides' intent"
                if preserves_both
                else f"verifier: resolution may drop {', '.join(dropped)} side intent"
            ),
            detail={"reason": str(data.get("reason", ""))},
            features={
                "verifier_checked": True,
                "verifier_preserves_current": preserves_current,
                "verifier_preserves_replayed": preserves_replayed,
                "verifier_confidence": confidence,
            },
        )


def _verifier_context(ctx: VerificationContext) -> "ContextBundle":
    """Rebuild a minimal ContextBundle for the critic prompt.

    The critic prompt needs the structural anchor (enclosing node) and primary
    context window. VerificationContext carries only the unit + candidate +
    config, so we reconstruct the lightweight bundle the prompt builder reads.
    """
    from capybase.context_builder import ContextBuilder

    return ContextBuilder().build(ctx.unit)


def _default_model(ctx: VerificationContext) -> str:
    """Best-effort model name when none was injected: read config if present."""
    cfg = getattr(ctx, "config", None)
    name = getattr(cfg, "model", None) or getattr(cfg, "model_name", None)
    return str(name) if name else "default"


class SyntaxValidator:
    """Deprecated per-unit syntax validator.

    Historically this spliced the candidate into ``unit.original_worktree_text``
    and compiled the result. That was structurally broken for multi-unit files:
    the original still holds sibling units' raw marker blocks, so the "whole
    file" being compiled was never the real merged file, and it could never
    catch cross-unit errors. Whole-file syntax checking now lives in
    :meth:`VerificationEngine.verify_file` (Phase B), which splices *all*
    resolutions together first.

    This class is retained only for backward compatibility with any
    externally-constructed validator lists; it is NOT part of the default
    engine anymore.
    """

    name = "syntax"

    def verify(self, ctx: VerificationContext) -> VerificationCheckResult:
        if ctx.unit.marker_span is None:
            return VerificationCheckResult(
                name=self.name, passed=True, message="no marker span", features={"syntax_checked": False}
            )
        whole = splice_resolution(
            ctx.unit.original_worktree_text,
            ctx.unit.marker_span,
            ctx.candidate.resolved_text,
        )
        lang = ctx.unit.language
        if lang == "python":
            ok, msg = _compile_python(whole)
            return VerificationCheckResult(
                name=self.name,
                passed=ok,
                severity="error",
                message=msg,
                features={"syntax_checked": True, "syntax_passed": ok},
            )
        return VerificationCheckResult(
            name=self.name,
            passed=True,
            message=f"syntax check not implemented for {lang}",
            features={"syntax_checked": False, "syntax_passed": True},
        )


class WholeFileMarkerValidator:
    """Deprecated per-unit whole-file marker validator.

    Like ``SyntaxValidator``, this spliced into ``unit.original_worktree_text``
    and was therefore unsatisfiable for any non-last unit in a multi-unit file
    (sibling blocks' markers remained). Whole-file marker checking now lives in
    :meth:`VerificationEngine.verify_file` (Phase B). Retained only for
    backward compatibility with externally-constructed validator lists.
    """

    name = "whole_file_markers"

    def verify(self, ctx: VerificationContext) -> VerificationCheckResult:
        if ctx.unit.marker_span is None:
            whole = ctx.unit.original_worktree_text
        else:
            whole = splice_resolution(
                ctx.unit.original_worktree_text,
                ctx.unit.marker_span,
                ctx.candidate.resolved_text,
            )
        leaked = contains_markers(whole)
        return VerificationCheckResult(
            name=self.name,
            passed=not leaked,
            severity="error",
            message="whole file still contains markers after splice" if leaked else "whole file clean",
            features={"whole_file_markers_remaining": int(leaked)},
        )


class AstPreservationValidator:
    """Prove that AST nodes OUTSIDE the conflict span survive the splice.

    The line-level ``ExactSpliceScopeValidator`` only guards that splicing
    doesn't touch lines beyond the marker block. But a model can still rewrite
    unchanged code *within* the visible window (e.g. collapse two statements,
    delete a comment) as long as the line count matches — a regression
    invisible to line checks. This validator parses the original and the
    spliced-resolved file with tree-sitter, computes the node-type fingerprint
    of every node OUTSIDE the conflict span, and rejects the candidate if they
    differ.

    Inert when tree-sitter or the language grammar is unavailable, or when the
    extractor did not record a base fingerprint (structural context disabled).
    """

    name = "ast_preservation"

    def verify(self, ctx: VerificationContext) -> VerificationCheckResult:
        unit = ctx.unit
        lang = unit.language
        if lang is None or unit.marker_span is None:
            return VerificationCheckResult(
                name=self.name,
                passed=True,
                message="ast preservation skipped (no language or span)",
                features={"ast_checked": False, "ast_preserved": True},
            )
        base_outside = unit.structural_metadata.get("ast_fingerprint_base_outside")
        if not base_outside:
            # Structural context was off or the grammar was unavailable when the
            # unit was extracted. Nothing to compare against — pass silently.
            return VerificationCheckResult(
                name=self.name,
                passed=True,
                message="ast preservation skipped (no base fingerprint)",
                features={"ast_checked": False, "ast_preserved": True},
            )
        try:
            from capybase.adapters import structural
        except Exception:  # noqa: BLE001
            return VerificationCheckResult(
                name=self.name,
                passed=True,
                message="ast preservation skipped (tree-sitter unavailable)",
                features={"ast_checked": False, "ast_preserved": True},
            )
        if not structural.is_available(lang):
            return VerificationCheckResult(
                name=self.name,
                passed=True,
                message=f"ast preservation skipped (no {lang} grammar)",
                features={"ast_checked": False, "ast_preserved": True},
            )
        # Splice the candidate into the original and re-fingerprint the outside.
        # CRITICAL: for multi-hunk files, the worktree still has sibling conflict
        # marker blocks. Those raw markers corrupt the tree-sitter parse and
        # produce a false AST-preservation failure. Blank them to comments first
        # (same approach as the LSP baseline) so the parse reflects real structure.
        spliced = splice_resolution(
            unit.original_worktree_text, unit.marker_span, ctx.candidate.resolved_text
        )
        spliced = _blank_markers(spliced)
        after_outside, _ = structural.fingerprint_region(
            spliced, lang, unit.marker_span
        )
        if after_outside is None:
            return VerificationCheckResult(
                name=self.name,
                passed=True,
                message="ast preservation skipped (post-splice parse failed)",
                features={"ast_checked": False, "ast_preserved": True},
            )
        preserved = after_outside == base_outside
        return VerificationCheckResult(
            name=self.name,
            passed=preserved,
            severity="error",
            message=(
                "AST structure outside the conflict block changed after splice"
                if not preserved
                else "AST structure outside the conflict block preserved"
            ),
            detail={"base_outside": base_outside, "after_outside": after_outside},
            features={
                "ast_checked": True,
                "ast_preserved": preserved,
            },
        )


def _compile_python(source: str) -> tuple[bool, str]:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(source)
        tmp_path = tf.name
    try:
        proc = subprocess.run(
            ["python3", "-m", "py_compile", tmp_path],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return True, "py_compile ok"
        return False, (proc.stderr.strip() or "py_compile failed").splitlines()[-1]
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class VerificationEngine:
    def __init__(self, validators: list[Validator], config: ValidationConfig) -> None:
        self.validators = validators
        self.config = config

    @classmethod
    def default(
        cls,
        config: ValidationConfig,
        extra_validators: list[Validator] | None = None,
    ) -> "VerificationEngine":
        # Phase A: per-unit validators. Each validates the candidate in
        # isolation against the unit's marker span. The whole-file checks
        # (no_markers, syntax) used to live here too, but they spliced into
        # ``unit.original_worktree_text`` — which still holds the *other*
        # units' raw marker blocks — so they were unsatisfiable for any
        # non-last unit and could never catch cross-unit errors. They now run
        # in Phase B (``verify_file``) against the fully-spliced file.
        validators: list[Validator] = [
            NoConflictMarkersValidator(),
            ExactSpliceScopeValidator(),
            AstPreservationValidator(),
            PreservationHeuristicValidator(),
            NeedsHumanValidator(),
        ]
        # Extra validators (e.g. the opt-in VerifierModelValidator) are appended
        # so they run last — after the cheap structural checks. This keeps the
        # rank-order validation loop cheap for structurally-invalid candidates
        # and only pays the LLM critic call for candidates worth judging.
        if extra_validators:
            validators.extend(extra_validators)
        return cls(validators, config)

    def register(self, validator: Validator) -> None:
        """Append a validator at the end of the chain (runs last)."""
        self.validators.append(validator)

    def verify(self, unit: ConflictUnit, candidate: CandidateResolution) -> VerificationResult:
        ctx = VerificationContext(unit=unit, candidate=candidate, config=self.config)
        hard: list[VerificationFailure] = []
        warnings: list[VerificationWarning] = []
        features: dict[str, float | int | str | bool] = {}
        for v in self.validators:
            res = v.verify(ctx)
            for k, val in res.features.items():
                features[k] = val
            if res.passed:
                continue
            # severity gating: only some validators are enabled by config.
            if not _enabled_for(self.config, v.name):
                continue
            sev = res.severity
            if sev == "error":
                hard.append(
                    VerificationFailure(
                        validator=res.name,
                        severity="error",
                        message=res.message,
                        detail=res.detail,
                    )
                )
            else:
                warnings.append(
                    VerificationWarning(
                        validator=res.name, message=res.message, detail=res.detail
                    )
                )
        # Categorize the one-side-copy heuristic severity per config.
        passed = len(hard) == 0
        features["hard_failure_count"] = len(hard)
        features["warning_count"] = len(warnings)
        return VerificationResult(
            candidate_id=candidate.candidate_id,
            unit_id=unit.unit_id,
            passed=passed,
            hard_failures=hard,
            warnings=warnings,
            features=features,
        )

    # ------------------------------------------------------------------
    # Phase B: whole-file validation against the fully-spliced file.
    # ------------------------------------------------------------------

    def verify_file(
        self,
        path: str,
        language: str | None,
        original: str,
        resolutions: list[tuple[tuple[int, int], str]],
        *,
        repo_root: str = ".",
    ) -> VerificationResult:
        """Validate the file after *all* units in it have been resolved.

        Splices every resolution into ``original`` (offset-correctly, in
        reverse line order) and runs the checks that only make sense on a
        complete file: no leftover conflict markers anywhere, and — for
        supported languages — a compile/syntax check on the real final text.

        This is the only place that can catch cross-unit errors (e.g. two
        hunks both defining the same symbol, or a syntax error that only
        appears when two resolutions are adjacent). The per-unit Phase A
        validators structurally cannot, because each only ever sees one
        block spliced into a file whose other blocks are still raw markers.

        When LSP diagnostics are enabled, this also runs pyright/rust-analyzer
        on the fully-spliced file and rejects candidates that introduce NEW
        type/compile errors (errors absent from the pre-conflict baseline).
        ``repo_root`` is the cwd for the tool (needed for cargo projects and
        locating shadow test files).

        Returns the same ``VerificationResult`` shape so ``RiskEngine.decide``
        and the orchestrator consume it unchanged. ``unit_id``/``candidate_id``
        are file-scoped (``<path>:file``) since this result is not tied to one
        candidate.
        """
        file_id = f"{path}:file"
        hard: list[VerificationFailure] = []
        features: dict[str, float | int | str | bool] = {}

        if not resolutions:
            whole = original
        else:
            whole = splice_all_resolutions(original, resolutions)

        # Whole-file marker check — now meaningful: no sibling blocks remain.
        leaked = contains_markers(whole)
        features["whole_file_markers_remaining"] = int(leaked)
        if leaked and self.config.require_no_markers:
            hard.append(
                VerificationFailure(
                    validator="whole_file_markers",
                    severity="error",
                    message="whole file still contains conflict markers after splice",
                    detail={},
                )
            )

        # Syntax check on the real, fully-spliced file.
        syntax_checked = False
        syntax_ok = True
        if language == "python":
            syntax_checked = True
            ok, msg = _compile_python(whole)
            syntax_ok = ok
            if not ok and self.config.require_syntax_if_supported:
                hard.append(
                    VerificationFailure(
                        validator="syntax",
                        severity="error",
                        message=msg,
                        detail={},
                    )
                )
        features["syntax_checked"] = syntax_checked
        features["syntax_passed"] = syntax_ok

        # LSP / type-checker diagnostics (Phase B): reject NEW errors.
        self._run_lsp_diagnostics(
            path, language, original, whole, repo_root, hard, features
        )

        # Shadow tests (Phase B): best-effort run of tests for this module.
        self._run_shadow_tests(path, repo_root, hard, features)

        passed = len(hard) == 0
        features["hard_failure_count"] = len(hard)
        features["warning_count"] = 0
        return VerificationResult(
            candidate_id=file_id,
            unit_id=file_id,
            passed=passed,
            hard_failures=hard,
            warnings=[],
            features=features,
        )

    # ------------------------------------------------------------------
    # Phase B helpers: LSP diagnostics and shadow tests.
    # ------------------------------------------------------------------

    def _run_lsp_diagnostics(
        self,
        path: str,
        language: str | None,
        original: str,
        whole: str,
        repo_root: str,
        hard: list[VerificationFailure],
        features: dict[str, float | int | str | bool],
    ) -> None:
        """Run an LSP and reject NEW errors introduced by the resolution.

        Computes a baseline by checking the pre-conflict ``original`` (with
        conflict markers — we strip them to a comment so the baseline parses),
        then checks the resolved ``whole``. Only errors NOT in the baseline are
        failures: pre-existing issues in the repo are the developer's problem,
        not the merge's. All LSP work is skipped when disabled or the tool is
        absent (``checked=False``).
        """
        if not self.config.enable_lsp_diagnostics:
            features["lsp_checked"] = False
            features["lsp_error_count"] = 0
            features["lsp_new_error_count"] = 0
            return
        try:
            from capybase.adapters import lsp as lsp_mod
        except Exception:  # noqa: BLE001
            features["lsp_checked"] = False
            return
        runner = lsp_mod.runner_for(
            language,
            config=lsp_mod.LspConfig(
                pyright_path=self.config.pyright_path,
                rust_analyzer_path=self.config.rust_analyzer_path,
                cargo_path=self.config.cargo_path,
            ),
        )
        if runner is None:
            features["lsp_checked"] = False
            return
        # Baseline: the original file with conflict markers blanked to comments
        # so it parses. We only care about errors OUTSIDE the conflict regions
        # for the baseline (those pre-date the merge).
        baseline_src = _blank_markers(original)
        baseline = runner.check(baseline_src, path=path, repo_root=repo_root)
        after = runner.check(whole, path=path, repo_root=repo_root)
        if not after.checked:
            features["lsp_checked"] = False
            features["lsp_error_count"] = 0
            features["lsp_new_error_count"] = 0
            return
        features["lsp_checked"] = True
        features["lsp_error_count"] = after.error_count
        # New errors = after errors not present in baseline (by message).
        baseline_msgs = {d.message for d in baseline.errors}
        new_errors = [d for d in after.errors if d.message not in baseline_msgs]
        features["lsp_new_error_count"] = len(new_errors)
        if new_errors:
            msg = "; ".join(d.message[:80] for d in new_errors[:3])
            hard.append(
                VerificationFailure(
                    validator="lsp_diagnostics",
                    severity="error",
                    message=f"LSP introduced {len(new_errors)} new error(s): {msg}",
                    detail={
                        "new_errors": [d.message for d in new_errors[:5]],
                        "tool": after.tool,
                    },
                )
            )

    def _run_shadow_tests(
        self,
        path: str,
        repo_root: str,
        hard: list[VerificationFailure],
        features: dict[str, float | int | str | bool],
    ) -> None:
        """Best-effort: run tests/test_<module>.py for the modified file.

        Locates a test file by convention (``tests/test_<basename>.py``) and
        runs it via pytest. A failure is a WARNING, not a hard error — the
        merge may be correct even if pre-existing tests fail for unrelated
        reasons. This records ``shadow_tests_passed`` as a calibration feature.
        """
        features.setdefault("shadow_tests_run", False)
        features.setdefault("shadow_tests_passed", True)
        if not self.config.enable_shadow_tests:
            return
        test_path = _locate_shadow_test(path, repo_root)
        if test_path is None:
            return
        try:
            proc = subprocess.run(
                ["python3", "-m", "pytest", test_path, "-q"],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=repo_root,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return
        features["shadow_tests_run"] = True
        ok = proc.returncode == 0
        features["shadow_tests_passed"] = ok
        if not ok:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()
            tail_str = tail[-1][:120] if tail else "tests failed"
            hard.append(
                VerificationFailure(
                    validator="shadow_tests",
                    severity="warning",
                    message=f"shadow tests failed: {tail_str}",
                    detail={"test_path": test_path, "returncode": proc.returncode},
                )
            )


def _enabled_for(cfg: ValidationConfig, name: str) -> bool:
    table = {
        "no_conflict_markers": cfg.require_no_markers,
        "whole_file_markers": cfg.require_no_markers,
        "exact_splice_scope": cfg.require_exact_splice_scope,
        "ast_preservation": cfg.require_ast_preservation,
        "preservation_heuristic": cfg.reject_if_copies_one_side,
        "needs_human": cfg.reject_if_model_needs_human,
        "syntax": cfg.require_syntax_if_supported,
        "verifier_model": cfg.enable_verifier_model,
    }
    return table.get(name, True)


def _blank_markers(text: str) -> str:
    """Replace conflict-marker lines with comments so the baseline parses.

    The pre-conflict ``original`` (the worktree with raw markers) isn't valid
    Python/Rust. For the LSP baseline we only need it to parse so we can
    collect pre-existing errors outside the conflict — blanking each marker
    line to a comment preserves line numbers and lets the parser recover.
    """
    out = []
    for line in text.split("\n"):
        if line.startswith(("<<<<<<<", "=======", ">>>>>>>")):
            out.append("# conflict-marker")
        else:
            out.append(line)
    return "\n".join(out)


def _locate_shadow_test(path: str, repo_root: str) -> str | None:
    """Find a test file for ``path`` by convention: tests/test_<basename>.py.

    ``src/config.rs`` → looks for ``tests/test_config.py`` (Rust has no pytest,
    so this is a no-op for non-Python; the feature is Python-centric for now).
    Returns the absolute path if it exists, else None.
    """
    from pathlib import Path

    p = Path(path)
    if p.suffix != ".py":
        return None
    candidate = Path(repo_root) / "tests" / f"test_{p.stem}.py"
    if candidate.is_file():
        return str(candidate)
    return None

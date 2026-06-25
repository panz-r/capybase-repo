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
        spliced = splice_resolution(
            unit.original_worktree_text, unit.marker_span, ctx.candidate.resolved_text
        )
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
    def default(cls, config: ValidationConfig) -> "VerificationEngine":
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
        return cls(validators, config)

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


def _enabled_for(cfg: ValidationConfig, name: str) -> bool:
    table = {
        "no_conflict_markers": cfg.require_no_markers,
        "whole_file_markers": cfg.require_no_markers,
        "exact_splice_scope": cfg.require_exact_splice_scope,
        "ast_preservation": cfg.require_ast_preservation,
        "preservation_heuristic": cfg.reject_if_copies_one_side,
        "needs_human": cfg.reject_if_model_needs_human,
        "syntax": cfg.require_syntax_if_supported,
    }
    return table.get(name, True)

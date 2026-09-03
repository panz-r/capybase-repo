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
import re
import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
import ast
from typing import Iterator, Protocol, runtime_checkable

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


# Default generation budget for the verifier critic's verdict call. Fits a
# non-reasoning model's short JSON verdict; reasoning models override this via
# the model config's max_tokens (threaded at registration) so their <think>
# chain doesn't exhaust the budget before the verdict is emitted.
_CRITIC_DEFAULT_MAX_TOKENS = 1024


# Lightweight config mirror to avoid an import cycle with config.py.
@dataclass
class ValidationConfig:
    require_no_markers: bool = True
    require_exact_splice_scope: bool = True
    require_syntax_if_supported: bool = True
    reject_if_copies_one_side: bool = True
    # Both-sides-represented: flag a
    # candidate that drops a side's additions entirely. Companion to
    # reject_if_copies_one_side — that catches verbatim copies; this catches
    # tweaked-but-still-one-sided merges. Advisory warning (feeds risk/retry).
    reject_if_drops_a_side: bool = True
    # Side-obligation contract (#3): flag a candidate that reverts a side's
    # MODIFICATION of an existing line back to base, or drops a side's added line.
    # Advisory warning (feeds retry). Kept in sync with config.py's pydantic
    # ValidationConfig.reject_if_drops_obligation.
    reject_if_drops_obligation: bool = True
    # Dependency preservation (necessary condition): warn
    # when a merge drops a base-referenced symbol that has an in-repo definition
    # and neither side removed. Companion to both-sides-represented — that
    # guards a side's additions; this guards a shared base dependency. Advisory
    # warning. Only active when the orchestrator registers the validator with
    # slice config; inert otherwise (the table gate is a second safety).
    reject_if_drops_referenced_symbol: bool = True
    reject_if_model_needs_human: bool = True
    require_whole_file_validation: bool = True
    require_ast_preservation: bool = True
    # Intent-coverage floor (mirrors config.ValidationConfig; see docs there).
    min_preservation_ratio: float = 0.5
    enable_lsp_diagnostics: bool = False
    pyright_path: str = "pyright"
    rust_analyzer_path: str = "rust-analyzer"
    cargo_path: str = "cargo"
    # Rust compile floor (mirrors config.ValidationConfig; the live flags).
    rustc_path: str = "rustc"
    rust_edition: str = ""
    # Rust error codes to suppress in the delta (mirrors config.ValidationConfig).
    rust_suppress_codes: list[str] = field(default_factory=list)
    # C/C++ compile floor (mirrors config.ValidationConfig; gcc/clang -fsyntax-only).
    cc_path: str = "gcc"
    cxx_path: str = "g++"
    c_std: str = "c11"
    cpp_std: str = "c++17"
    # When set, the whole-file C/C++ verify_file branch runs this build command
    # in the repo dir (save/write/restore the resolved file) instead of
    # standalone gcc -fsyntax-only. The authoritative oracle for real-world C
    # (resolves sibling #include headers standalone gcc can't). Empty (default)
    # = standalone gcc (the existing behavior). Set by the orchestrator from
    # tests.pre_continue, or by the live-eval driver from C_BUILD_COMMANDS.
    cc_build_command: str = ""
    # Clippy lint check (mirrors config.ValidationConfig; the live flags).
    enable_clippy: bool = False
    clippy_severity: str = "warning"
    lsp_baseline_strict: bool = True
    enable_shadow_tests: bool = False
    # Verifier-model critic (mirrors config.ValidationConfig; the live flags).
    # OPT-OUT: default ON in production; the hermetic test suite opts out via
    # the autouse _isolate_verifier_critic conftest fixture (fake clients can't
    # answer critic prompts, so the check would be meaningless noise there).
    enable_verifier_model: bool = True
    verifier_severity: str = "warning"
    # Critic guardrail phases (mirror config.ValidationConfig).
    enable_verifier_assertion: bool = True
    enable_verifier_reflection: bool = True
    enable_verifier_guardrail: bool = True
    verifier_reflection_coverage_floor: float = 0.9
    enable_recovery_retry: bool = True
    enable_per_unit_syntax_check: bool = True
    # VeriGuard policy gate (mirrors config.ValidationConfig).
    enable_policy_gate: bool = False
    policy_rules: tuple = ()  # tuple of config.PolicyRule; default empty = no-op
    # LLM code-smell checks (mirrors config.ValidationConfig).
    enable_code_smell_checks: bool = False
    code_smell_severity: str = "warning"

    @classmethod
    def from_dict(cls, d: dict) -> "ValidationConfig":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in d.items() if k in known}
        # policy_rules cross the config boundary as plain dicts (config.PolicyRule
        # is pydantic; this dataclass is not). Reconstruct PolicyRule objects so
        # the gate's attribute access (rule.kind, rule.pattern, ...) works.
        if "policy_rules" in kwargs and kwargs["policy_rules"]:
            from capybase.config import PolicyRule

            rebuilt = []
            for r in kwargs["policy_rules"]:
                if isinstance(r, PolicyRule):
                    rebuilt.append(r)
                elif isinstance(r, dict):
                    rebuilt.append(PolicyRule(**r))
            kwargs["policy_rules"] = tuple(rebuilt)
        return cls(**kwargs)


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


class NonEmptyResolutionValidator:
    """Reject a candidate whose ``resolved_text`` is empty or whitespace-only.

    An empty resolution is never a correct merge — it means the model produced
    nothing (a parse salvage that extracted only prose, or a refusal that slipped
    through as needs_human=False with empty text). Without this guard, an empty
    candidate passes every other validator (no markers, splices to nothing, no
    entities to drop) and produces a corrupted splice that deletes the conflict
    region's content. Surfaced as a hard failure so the CEGIS loop retries with
    concrete feedback ("produced empty resolution") rather than accepting a
    silently-destructive merge.

    Runs FIRST (before the marker/scope checks) so an empty candidate is rejected
    before any splicing or parsing. Does NOT fire on a deliberate deletion (a
    modify/delete where the correct resolution IS empty) — that case is handled
    by the block-capture / structural-resolver layers before the LLM loop, so a
    candidate reaching here with empty text is a model failure, not a deletion.
    """

    name = "non_empty_resolution"

    def verify(self, ctx: VerificationContext) -> VerificationCheckResult:
        text = ctx.candidate.resolved_text or ""
        is_empty = not text.strip()
        # Block-capture's accept_deletion deliberately produces an empty
        # resolved_text (deleting the block IS the correct resolution). Skip the
        # guard for block-capture candidates — the empty text is intentional, not
        # a model failure. Detected via the prompt_version (PROMPT_BLOCK_CAPTURE)
        # or provenance ("block_capture").
        pv = getattr(ctx.candidate, "prompt_version", "") or ""
        prov = getattr(ctx.candidate, "provenance", "") or ""
        # A deliberate deletion is identified by the MECHANISM that produced the
        # candidate, NOT by the conflict shape. Keying on the conflict shape (a
        # modify/delete where one side is empty) over-broadly accepted ANY empty
        # candidate — including a ``plain_llm`` candidate that simply failed to
        # produce text — as a "deliberate deletion" (silent wrong merge / data
        # loss). Only block-capture and the structural resolver's ``delete_side``
        # (which set explicit provenance) are deliberate deletions; an empty LLM
        # candidate is a model failure and must retry.
        is_deliberate_deletion = (
            pv.startswith("block_capture")
            or prov == "block_capture"
            or prov == "deterministic_structural"
            or pv.startswith("structural.")
        )
        if is_empty and is_deliberate_deletion:
            return VerificationCheckResult(
                name=self.name, passed=True,
                message="empty resolution is a deliberate deletion",
                features={"empty_resolution": False},
            )
        return VerificationCheckResult(
            name=self.name,
            passed=not is_empty,
            severity="error",
            message="model produced an empty resolution (no resolved_text); retry",
            features={"empty_resolution": is_empty},
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


def _classify_exclusive_choice(
    ctx: VerificationContext, missing: list,
) -> str:
    """Classify an exclusive-choice conflict into a proof class.

    Not all exclusive choices are equally safe. Version bumps and config
    values are defensible either/or choices (SAFE_SCALAR). Delete-vs-modify
    conflicts may represent deliberate intent (DELETE_MODIFY). Structural
    code conflicts (field types, function signatures) require a primitive or
    compiler proof (STRUCTURAL_CODE). Unrecognized shapes default to the
    generic pass (GENERIC_EXCLUSIVE).

    Uses signals from ``structural_metadata`` (conflict_features,
    merge_direction) that are already computed at extraction time.
    """
    unit = ctx.unit
    cf = unit.structural_metadata.get("conflict_features")
    cf = cf if isinstance(cf, dict) else {}
    md = unit.structural_metadata.get("merge_direction")
    md = md if isinstance(md, dict) else {}
    path = unit.path or ""
    lang = unit.language or ""

    # DELETE_MODIFY: one side deleted the block, the other modified it.
    # This is genuinely ambiguous — the deletion may be intentional. Don't
    # auto-pass without evidence (rename/move/replacement).
    if md.get("kind") == "modify_delete" or md.get("deleting_side"):
        return "DELETE_MODIFY"

    # STRUCTURAL_CODE: the conflict touches a structural definition (struct,
    # enum, fn signature, impl) in a .rs source file. These require a
    # structure-specific primitive or compiler-backed verification.
    if lang == "rust" and path.endswith(".rs"):
        if cf.get("touches_definition"):
            return "STRUCTURAL_CODE"
        # Also check entity-level ops: if both sides modified existing
        # entities (not just added new ones), it's a structural rewrite.
        ops_modified = cf.get("ops_modified", 0)
        if ops_modified and ops_modified > 0:
            return "STRUCTURAL_CODE"

    # SAFE_SCALAR: version bumps, config values, doc URLs, changelog entries.
    # Picking either side is defensible for these scalar/config choices.
    if cf.get("commit_change_type") == "config_update":
        return "SAFE_SCALAR"
    if cf.get("value_resolution"):
        return "SAFE_SCALAR"
    # README.md and Cargo.toml version strings.
    if lang in ("markdown", "toml") or path.endswith((".md", ".toml")):
        return "SAFE_SCALAR"
    # .stderr files (compiler diagnostic snapshots).
    if path.endswith(".stderr"):
        return "SAFE_SCALAR"

    # GENERIC_EXCLUSIVE: unrecognized exclusive shape. Default to the
    # current behavior (PASS) — the model made a defensible choice.
    return "GENERIC_EXCLUSIVE"


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
        # Empty-side copy: when the candidate is empty AND one side is empty,
        # the model resolved to the deletion side. The bool(side) guard above
        # would miss this (empty is falsy). Allow copied_one=True for the
        # empty-side case so change-accounting can classify it (it's usually
        # an exclusive choice — delete vs add — which should PASS).
        if not resolved and not cur:
            copied_current = True
        if not resolved and not rep:
            copied_replayed = True
        copied_one = copied_current or copied_replayed
        # Value-resolution fast path: when both sides preserve the same statement
        # shape and only a value diverged (a return, an assignment to the same
        # target), a verbatim copy of one side is the CORRECT resolution — the
        # base operation is preserved and the value is resolved. Don't flag it.
        # BUT only when the merge IS genuinely one-sided (copied_one) — a
        # SYNTHESIZED resolution (neither side verbatim) is NOT automatically
        # safe even for a value-resolution conflict; it could drop a side-effect
        # call. Phase 5.1: enforce the one-sided-merge contract at the carve-out.
        cf = ctx.unit.structural_metadata.get("conflict_features")
        if (
            isinstance(cf, dict)
            and cf.get("value_resolution")
            and copied_one
        ):
            return VerificationCheckResult(
                name=self.name,
                passed=True,
                severity="warning",
                message="value-resolution conflict: one side's value selected (base op preserved)",
                features={
                    "copied_one_side": True,
                    "value_resolution": True,
                },
            )
        # Change accounting: "candidate == one side" is not itself proof of a
        # lost intent. The other side's base-relative changes may be already
        # present (EQUIVALENT — the copy is correct), comment-only (DEFERRED —
        # the comment pass handles them), formatting (IGNORED), or genuinely
        # missing executable code (the actionable case). Compute the SPECIFIC
        # missing obligations so the repair loop can give the model a
        # constructive counterexample ("integrate THIS line") instead of the
        # generic "you copied one side" that small models can't act on.
        #
        # When the copy is fully accounted for (no missing executable/directive
        # obligations), PASS — this is the fix for false-positive convergence
        # where copying one side IS correct (the other side's changes are all
        # present/equivalent/comment-only). When obligations are missing, carry
        # the exact missing lines in `detail` for the repair prompt.
        if copied_one:
            try:
                from capybase.change_accounting import (
                    derive_missing_obligations, derive_deferred_comments,
                )
                # Use the base HUNK, not the full base file. The conflict unit's
                # base side is the ENTIRE base file (set at extraction time),
                # but current/replayed are the marker-block interiors (hunk-
                # sized). Diffing full_file → hunk produces thousands of
                # spurious "removed" lines that drown the real obligation signal.
                # The diff3-refined base hunk (already computed by the conflict
                # extractor, or re-derivable via _base_hunk_via_diff3) is the
                # correct shape: it matches the current/replayed hunk scope.
                # This mirrors _value_resolution_of's base-text derivation
                # (conflict_extractor.py:704-718).
                base_raw = ctx.unit.base.text or ""
                refined = ctx.unit.structural_metadata.get("diff3_refined")
                if isinstance(refined, dict) and refined.get("base") is not None:
                    base_raw = refined["base"]
                else:
                    from capybase.conflict_extractor import _base_hunk_via_diff3
                    base_hunk = _base_hunk_via_diff3(
                        ctx.unit.base.text or "", cur, rep)
                    if base_hunk is not None:
                        base_raw = base_hunk
                missing = derive_missing_obligations(
                    base_raw, cur, rep, resolved)
                deferred = derive_deferred_comments(
                    base_raw, cur, rep, resolved)
            except Exception:  # noqa: BLE001 — best-effort; fall back to flag
                missing, deferred = None, []
            # When ALL missing obligations are EXCLUSIVE (mutually-exclusive
            # alternatives at the same position — the candidate chose one side's
            # value for a field/type/assignment the other side changed
            # differently), copying one side is a VALID resolution: the model
            # made a defensible choice between two alternatives. PASS — there
            # are no genuinely-dropped ADDITIONS. Only ADDITIVE missing
            # obligations (real new content the candidate lacks) warrant a
            # repair. This is the fix for exclusive-conflict convergence: the
            # heuristic no longer forces a retry on a correct either/or choice.
            additive_missing = ([o for o in missing if not o.exclusive]
                                if missing is not None else None)
            if missing is not None and not additive_missing:
                # The copy is fully accounted for — either no missing
                # obligations at all, or all missing ones are EXCLUSIVE
                # (mutually-exclusive alternatives where the model's choice is
                # defensible).
                all_excl = bool(missing) and all(o.exclusive for o in missing)
                excl_lines = ([o.line.strip() for o in missing[:8]]
                              if all_excl else [])
                if not all_excl:
                    # No missing obligations at all (or only PRESENT/comment-only).
                    # The copy is fully accounted for. CLEAR pass.
                    return VerificationCheckResult(
                        name=self.name,
                        passed=True,
                        severity="warning",
                        message=(
                            "resolved text copies one side verbatim, but every "
                            "executable change from the other side is accounted for "
                            "(present or comment-only-deferred)"
                        ),
                        detail={
                            "copied_current": copied_current,
                            "copied_replayed": copied_replayed,
                            "change_accounting": "all_accounted_for",
                            "deferred_comments": len(deferred),
                        },
                        features={
                            "copied_one_side": True,
                            "copied_current_side": copied_current,
                            "copied_replayed_side": copied_replayed,
                            "change_accounted": True,
                            "preservation_result": "clear",
                        },
                    )
                # All missing obligations are EXCLUSIVE. Apply proof-class
                # classification: not all exclusive choices are equally safe.
                # The review feedback (Phase 5) correctly identifies that
                # version bumps, struct field types, import restructures, and
                # delete-vs-add are fundamentally different merge problems.
                proof_class = _classify_exclusive_choice(ctx, missing)
                if proof_class in ("DELETE_MODIFY", "STRUCTURAL_CODE"):
                    # Unsafe exclusive: the choice may violate branch intent.
                    # Fall through to the additive-missing path (which will
                    # flag the exclusive obligations and retry), rather than
                    # auto-passing. This converts dangerous exclusive choices
                    # back to escalation.
                    pass  # fall through to the warning below
                else:
                    # Safe exclusive (SAFE_SCALAR or GENERIC_EXCLUSIVE):
                    # the model made a defensible choice. PASS — there are no
                    # genuinely-dropped ADDITIONS. preservation_result =
                    # CHOICE_REQUIRED so downstream consumers know this is an
                    # auditable choice, not proof of semantic correctness.
                    return VerificationCheckResult(
                        name=self.name,
                        passed=True,
                        severity="warning",
                        message=(
                            "resolved text copies one side verbatim, but every "
                            "executable change from the other side is an "
                            f"exclusive choice ({proof_class})"
                        ),
                        detail={
                            "copied_current": copied_current,
                            "copied_replayed": copied_replayed,
                            "change_accounting": "choice_required",
                            "exclusive_choices": excl_lines,
                            "exclusive_proof_class": proof_class,
                            "deferred_comments": len(deferred),
                        },
                        features={
                            "copied_one_side": True,
                            "copied_current_side": copied_current,
                            "copied_replayed_side": copied_replayed,
                            "change_accounted": True,
                            "preservation_result": "choice_required",
                            "exclusive_proof_class": proof_class,
                        },
                    )
            if additive_missing:
                # Actionable: name the specific obligations so the model can
                # act on them. Separate ADDITIONS (lines to add) from
                # DELETIONS (lines the other side removed that the candidate
                # still has — should be deleted). EXCLUSIVE obligations already
                # PASSED above.
                add_lines = [o.line.strip() for o in additive_missing
                             if o.operation == "added"][:8]
                del_lines = [o.line.strip() for o in additive_missing
                             if o.status == "DROPPED_DELETION"][:8]
                missing_lines = add_lines + del_lines  # backwards-compatible
                any_exclusive = any(o.exclusive for o in missing)
                copied_label = "CURRENT" if copied_current else "REPLAYED"
                other_label = "REPLAYED" if copied_current else "CURRENT"
                if any_exclusive:
                    conflict_type = "mixed"
                    action = ("Some are mutually-exclusive choices (keep or "
                              "switch) and some are changes to apply.")
                elif del_lines and not add_lines:
                    conflict_type = "deletion"
                    action = "remove the line(s) below from the candidate"
                elif del_lines and add_lines:
                    conflict_type = "mixed_add_del"
                    action = ("add the marked additions AND remove the marked "
                              "deletions from the candidate")
                else:
                    conflict_type = "additive"
                    action = "integrate them into the candidate"
                # Sprint-19 P2, churn-aware heuristic: when the loser side's
                # ONLY unaccounted churn is a pure deletion of base content
                # (no additions, no exclusive choices), a verbatim copy of
                # the other side PASSES — a deletion-only loser churn is
                # more likely superseded than one adding functionality
                # (tokio-0037: oracle == current verbatim; the heuristic's
                # forced retries degraded into syntax errors and the case
                # escalated). The file-level gates still run: the
                # side-collapse guard adjudicates both-rewrite shapes and
                # Phase 2 compiles the spliced buffer. Gated by
                # validation.preservation_deletion_carveout.
                if (conflict_type == "deletion"
                        and getattr(ctx.config,
                                   "preservation_deletion_carveout", True)):
                    return VerificationCheckResult(
                        name=self.name,
                        passed=True,
                        severity="warning",
                        message=(
                            f"resolved text copies {copied_label} verbatim; "
                            f"{other_label}'s only unaccounted change is a "
                            f"pure deletion of base content (likely "
                            f"superseded)"
                        ),
                        detail={
                            "copied_current": copied_current,
                            "copied_replayed": copied_replayed,
                            "deletion_lines": del_lines,
                            "deletion_count": len(del_lines),
                            "copied_side": copied_label.lower(),
                            "conflict_type": conflict_type,
                        },
                        features={
                            "copied_one_side": True,
                            "copied_current_side": copied_current,
                            "copied_replayed_side": copied_replayed,
                            "change_accounted": True,
                            "preservation_result": "deletion_superseded",
                        },
                    )
                return VerificationCheckResult(
                    name=self.name,
                    passed=False,
                    severity="warning",
                    message=(
                        f"resolved text copies {copied_label} verbatim, but "
                        f"{other_label} has unaccounted changes ({conflict_type})"
                    ),
                    detail={
                        "copied_current": copied_current,
                        "copied_replayed": copied_replayed,
                        "missing_lines": missing_lines,
                        "addition_lines": add_lines,
                        "deletion_lines": del_lines,
                        "missing_count": len(additive_missing),
                        "copied_side": copied_label.lower(),
                        "conflict_type": conflict_type,
                        "action": action,
                        "deferred_comments": len(deferred),
                    },
                    features={
                        "copied_one_side": True,
                        "copied_current_side": copied_current,
                        "copied_replayed_side": copied_replayed,
                        "change_accounted": False,
                        "conflict_exclusive": False,
                    },
                )
            # missing is None (change-accounting failed) → fall back to flag.
        # For genuine merges (not copied_one), run the extended change-
        # accounting that checks BOTH sides' additions against the candidate.
        # This catches compile-valid wrong merges where the model silently
        # drops a branch's contribution from a synthesized merge.
        if not copied_one:
            try:
                from capybase.change_accounting import derive_missing_obligations
                base_raw = ctx.unit.base.text or ""
                refined = ctx.unit.structural_metadata.get("diff3_refined")
                if isinstance(refined, dict) and refined.get("base") is not None:
                    base_raw = refined["base"]
                else:
                    from capybase.conflict_extractor import _base_hunk_via_diff3
                    base_hunk = _base_hunk_via_diff3(base_raw, cur, rep)
                    if base_hunk is not None:
                        base_raw = base_hunk
                genuine_missing = derive_missing_obligations(
                    base_raw, cur, rep, resolved)
                if genuine_missing:
                    # The genuine merge dropped branch changes. Produce an
                    # advisory warning with specific line-level detail (for
                    # the BothSidesRepresentedValidator and the jury to use).
                    # Use passed=True so this doesn't force a retry — the
                    # BothSidesRepresentedValidator already handles genuine
                    # merges with its own retry budget. The detail enriches
                    # the warning for auditability.
                    add_lines = [o.line.strip() for o in genuine_missing
                                 if o.operation == "added"][:8]
                    return VerificationCheckResult(
                        name=self.name,
                        passed=True,
                        severity="warning",
                        message=(
                            f"genuine merge may have dropped {len(genuine_missing)} "
                            f"branch change(s) from "
                            f"{', '.join(set(o.side for o in genuine_missing))}"
                        ),
                        detail={
                            "copied_current": False,
                            "copied_replayed": False,
                            "missing_lines": add_lines,
                            "genuine_merge_dropped": True,
                            "missing_count": len(genuine_missing),
                        },
                        features={
                            "copied_one_side": False,
                            "change_accounted": True,
                            "genuine_merge_dropped": True,
                        },
                    )
            except Exception:  # noqa: BLE001 — best-effort
                pass
        message = (
            "resolved text copies one side verbatim"
            if copied_one
            else "resolved text differs from both sides"
        )
        return VerificationCheckResult(
            name=self.name,
            passed=not copied_one,
            severity="warning",
            message=message,
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


class BothSidesRepresentedValidator:
    """Cheap necessary condition for semantic conflict-freedom.

    The expensive formulation (SafeMerge) treats merge as a 4-program relation:
    a candidate M is semantically conflict-free only if, wherever a side diverged
    from base, M carries that side's change. Building the product program to
    *prove* that is out of scope, but there is a cheap *necessary* condition
    capybase can check deterministically: a valid combination must contain at
    least one distinctive line from EACH side that added content. A merge that
    silently drops a side's additions violates §5.1 by construction.

    This complements :class:`PreservationHeuristicValidator`, which only catches
    *verbatim* copies. A candidate can tweak one side (so it no longer matches
    that side verbatim) while still omitting the other side's additions entirely
    — the copy heuristic misses that, but this check flags it.

    Pure token-set logic (no I/O, no parser). A side that only DELETED base
    content (no additions) imposes no requirement here, so pure-deletion sides
    don't trip false positives. Severity ``warning`` (bias toward retry, like the
    copy heuristic) — it's a necessary-not-sufficient signal, so it feeds the
    risk/retry engine rather than hard-rejecting.
    """

    name = "both_sides_represented"

    @staticmethod
    def _token_set(text: str) -> set[str]:
        """Word-tokens of a side, for distinctive-addition matching.

        Matching at LINE granularity is too coarse for line-*modifications*: if
        a side's addition is a modified version of an existing line (e.g.
        appending an element to a list), the whole modified line is treated as
        the "addition" and the merge's different-but-related line won't match
        it. Token granularity recognizes that a merge carrying ``scheduler``
        represents a side that changed the line to add ``scheduler``, even
        though the surrounding punctuation/formatting differs.

        ``\\w+`` (underscores included by default) extracts identifier-like
        tokens, ignoring brackets/quotes/commas/operators — so the distinctive
        *content* a side added (a new element, a new symbol) is what's matched,
        not incidental formatting. Splitting on whitespace alone would keep
        ``"scheduler"]`` as one token and miss the match against a merge that
        wrote ``"scheduler",``.

        COMMENTS and STRING LITERALS are blanked first (language-agnostic — a
        ``#``/``//``/``/* */`` region and a quoted string become spaces). Without
        this, a side's distinctive addition that was DROPPED from the code but
        survived in a comment or string satisfied the "represented" check → a
        false PASS (silent data loss). Mirrors the comment/string blanking in
        :func:`structural.referenced_symbols`.
        """
        from capybase.adapters.structural import _blank_text_strings, _blank_comments
        blanked = _blank_text_strings(text or "")
        blanked = _blank_comments(blanked)
        return set(re.findall(r"\w+", blanked))

    def verify(self, ctx: VerificationContext) -> VerificationCheckResult:
        # Value-resolution fast path: when both sides preserve the same statement
        # shape and only a value diverged (a return, an assignment to the same
        # target), a one-sided merge (picking either side's value) is the correct
        # resolution — the base operation is preserved. The token-set "both sides
        # represented" pressure is wrong here (two return values or two assignments
        # to the same target don't compose), so don't flag a dropped side.
        # Value-resolution fast path (Phase 5.1 contract enforcement): only
        # suppress the both-sides-represented check when the candidate IS a
        # one-sided merge (verbatim copy of one side). A SYNTHESIZED resolution
        # (neither side verbatim) is NOT automatically safe even for a value-
        # resolution conflict — it could drop a side-effect call. The carve-out
        # relieves the token-set pressure that's wrong for two-values-same-target,
        # but only when the merge genuinely picked one value.
        cur_text = (ctx.unit.current.text or "").strip()
        rep_text = (ctx.unit.replayed.text or "").strip()
        resolved_text = (ctx.candidate.resolved_text or "").strip()
        copied_one = (
            (bool(cur_text) and resolved_text == cur_text)
            or (bool(rep_text) and resolved_text == rep_text)
        )
        cf = ctx.unit.structural_metadata.get("conflict_features")
        if (
            isinstance(cf, dict)
            and cf.get("value_resolution")
            and copied_one
        ):
            return VerificationCheckResult(
                name=self.name,
                passed=True,
                severity="warning",
                message="value-resolution conflict: base operation preserved, value/expression resolved",
                detail={"value_resolution": cf["value_resolution"]},
                features={
                    "dropped_a_side": False,
                    "value_resolution": True,
                },
            )
        base = self._token_set(ctx.unit.base.text)
        cur = self._token_set(ctx.unit.current.text)
        rep = self._token_set(ctx.unit.replayed.text)
        merged = self._token_set(ctx.candidate.resolved_text)
        # Distinctive additions: tokens a side added that weren't in base.
        cur_added = cur - base
        rep_added = rep - base
        # A side is "represented" if either it added nothing (pure deletion — no
        # requirement) or the merge carries at least one of its added tokens.
        cur_missing = bool(cur_added) and not (cur_added & merged)
        rep_missing = bool(rep_added) and not (rep_added & merged)
        dropped = cur_missing or rep_missing
        return VerificationCheckResult(
            name=self.name,
            passed=not dropped,
            severity="warning",
            message=(
                "resolved text drops a side's additions"
                if dropped
                else "resolved text represents both sides' additions"
            ),
            detail={
                "current_additions_dropped": cur_missing,
                "replayed_additions_dropped": rep_missing,
            },
            features={
                "dropped_a_side": dropped,
                "dropped_current_additions": cur_missing,
                "dropped_replayed_additions": rep_missing,
            },
        )


class IntentCoverageValidator:
    """Deterministic per-side structural-intent coverage.

    The hard coverage guarantee: of the logical units (function/method/class/
    field) each side ADDED beyond base, the resolution must preserve at least a
    configured fraction. Computed via the abstract parser's ``enumerate_entities`` — no
    LLM, fully deterministic. Complements the LLM critic: where the critic is a
    qualitative judge (uncertain, degrades silently), this is a quantitative
    floor ("2/3 replayed-side units preserved → ratio 0.67") that fires even
    when the critic is skipped or returns a low-confidence pass.

    Warning severity (feeds the critic retry path, same as the other soft drops).
    Only fires when a side added ≥1 structural entity, so value-only conflicts
    (e.g. changing a constant) are unaffected — the token-set
    :class:`BothSidesRepresentedValidator` remains the backstop there.
    Inert when the structural parser is unavailable for the language.
    """

    name = "intent_coverage"

    def verify(self, ctx: VerificationContext) -> VerificationCheckResult:
        unit = ctx.unit
        lang = unit.language
        floor = getattr(ctx.config, "min_preservation_ratio", 0.5)
        if not floor or lang not in ("python", "rust"):
            return VerificationCheckResult(
                name=self.name, passed=True,
                message="intent coverage skipped (disabled or unsupported language)",
                features={"intent_coverage_checked": False},
            )
        try:
            from capybase.adapters import structural
        except Exception:  # noqa: BLE001
            return VerificationCheckResult(
                name=self.name, passed=True,
                message="intent coverage skipped (parser unavailable)",
                features={"intent_coverage_checked": False},
            )
        if not structural.is_available(lang):
            return VerificationCheckResult(
                name=self.name, passed=True,
                message=f"intent coverage skipped (no {lang} grammar)",
                features={"intent_coverage_checked": False},
            )
        base = unit.base.text or ""
        cur = unit.current.text or ""
        rep = unit.replayed.text or ""
        resolved = ctx.candidate.resolved_text or ""
        cur_cov = structural.preservation_coverage(base, cur, resolved, lang)
        rep_cov = structural.preservation_coverage(base, rep, resolved, lang)
        if cur_cov is None or rep_cov is None:
            return VerificationCheckResult(
                name=self.name, passed=True,
                message="intent coverage skipped (parse failed)",
                features={"intent_coverage_checked": False},
            )
        # A side below the floor (and it added something) is a coverage failure.
        cur_bad = cur_cov.added > 0 and cur_cov.ratio < floor
        rep_bad = rep_cov.added > 0 and rep_cov.ratio < floor
        failed = cur_bad or rep_bad
        dropped_names = []
        if cur_bad:
            dropped_names += [f"current:{e.kind} '{e.name}'" for e in cur_cov.dropped]
        if rep_bad:
            dropped_names += [f"replayed:{e.kind} '{e.name}'" for e in rep_cov.dropped]
        return VerificationCheckResult(
            name=self.name,
            passed=not failed,
            severity="warning",
            message=(
                f"intent coverage below floor ({floor:.0%}): dropped "
                f"{', '.join(dropped_names)}"
                if failed
                else "intent coverage above floor for both sides"
            ),
            detail={
                "current_ratio": cur_cov.ratio,
                "current_preserved": cur_cov.preserved,
                "current_total": cur_cov.added,
                "replayed_ratio": rep_cov.ratio,
                "replayed_preserved": rep_cov.preserved,
                "replayed_total": rep_cov.added,
                "dropped": dropped_names,
            },
            features={
                "intent_coverage_checked": True,
                "intent_coverage_failed": failed,
                "current_preservation_ratio": cur_cov.ratio,
                "replayed_preservation_ratio": rep_cov.ratio,
            },
        )


class UnattributedCodeValidator:
    """Deterministic spurious-addition guard.

    The INVERSE of :class:`IntentCoverageValidator`: where coverage checks that
    no side's unit was DROPPED, this checks that the merge added no unit present
    in NONE of the three sides — a hallucinated helper, an extra branch, a
    synthesized function. LLMs add "helpful" logic no side asked for; this is the
    only check for surplus code, completing the "neither dropped nor spurious"
    guarantee. Computed via the abstract parser's ``unattributed_entities`` (no LLM).

    Warning severity (feeds the retry path, like the other soft signals). A unit
    is "unattributed" if its NAME appears in none of base/current/replayed — so a
    legitimate extracted helper (genuinely needed but newly named) also flags;
    the model can justify keeping it on retry, and the message names the specific
    unit so a human can judge. Inert when the structural parser is unavailable for the language.
    """

    name = "unattributed_code"

    def verify(self, ctx: VerificationContext) -> VerificationCheckResult:
        unit = ctx.unit
        lang = unit.language
        if lang not in ("python", "rust"):
            return VerificationCheckResult(
                name=self.name, passed=True,
                message="unattributed code skipped (unsupported language)",
                features={"unattributed_code_checked": False},
            )
        try:
            from capybase.adapters import structural
        except Exception:  # noqa: BLE001
            return VerificationCheckResult(
                name=self.name, passed=True,
                message="unattributed code skipped (parser unavailable)",
                features={"unattributed_code_checked": False},
            )
        if not structural.is_available(lang):
            return VerificationCheckResult(
                name=self.name, passed=True,
                message=f"unattributed code skipped (no {lang} grammar)",
                features={"unattributed_code_checked": False},
            )
        unattributed = structural.unattributed_entities(
            unit.base.text or "", unit.current.text or "",
            unit.replayed.text or "", ctx.candidate.resolved_text or "", lang,
        )
        if unattributed is None:
            return VerificationCheckResult(
                name=self.name, passed=True,
                message="unattributed code skipped (parse failed)",
                features={"unattributed_code_checked": False},
            )
        names = ", ".join(f"{e.kind} '{e.name}'" for e in unattributed)
        failed = bool(unattributed)
        return VerificationCheckResult(
            name=self.name,
            passed=not failed,
            severity="warning",
            message=(
                f"unattributed code: {len(unattributed)} unit(s) in the merge "
                f"appear in neither side: {names}"
                if failed
                else "no unattributed code"
            ),
            detail={"unattributed": [e.name for e in unattributed]},
            features={
                "unattributed_code_checked": True,
                "unattributed_code_count": len(unattributed),
            },
        )


class ObligationValidator:
    """Side-obligation contract (#3): a candidate must preserve each side's edits.

    Derives per-side obligations (what each side added/changed/removed vs base)
    via :func:`capybase.obligations.extract_obligations` and checks the candidate
    carries them. This is the additive layer the token-set/verbatim heuristics
    structurally miss:

    - a side **modified an existing line** (no new distinctive token) —
      :class:`BothSidesRepresentedValidator` (token-set) sees no "addition" and
      passes; this validator flags a resolution that **reverted** the edit to base;
    - a side **added a whole line** that the merge dropped — caught here at line
      granularity (complements the token-set check, which a reformatting can
      defeat).

    A deliberate deletion (a side's ``removed`` obligation) is HONORED, not
    required — flagging a clean delete would conflict with the modify/delete
    machinery. Pure line-diff logic (no I/O, no parser). Severity ``warning``
    (a necessary-not-sufficient signal → feeds retry, like the copy heuristic).

    Gated by ``config.reject_if_drops_obligation`` (default on).
    """

    name = "obligation"

    def verify(self, ctx: VerificationContext) -> VerificationCheckResult:
        from capybase.obligations import (
            extract_obligations,
            obligations_satisfied,
        )

        obligations = extract_obligations(ctx.unit)
        # An unchanged-on-both-sides conflict (or one with no load-bearing edits)
        # imposes no obligation — pass cleanly so the validator is a no-op there.
        if obligations.current.empty and obligations.replayed.empty:
            return VerificationCheckResult(
                name=self.name, passed=True,
                message="no side obligations (both sides unchanged)",
                features={"obligation_checked": False},
            )
        satisfied, dropped = obligations_satisfied(
            obligations, ctx.candidate.resolved_text or ""
        )
        cur_drops = [d for d in dropped if d.startswith("CURRENT")]
        rep_drops = [d for d in dropped if d.startswith("REPLAYED")]
        return VerificationCheckResult(
            name=self.name,
            passed=satisfied,
            severity="warning",
            message=(
                "resolved text drops a side obligation"
                if dropped
                else "resolved text preserves both sides' obligations"
            ),
            detail={"dropped_obligations": dropped[:8]},
            features={
                "obligation_checked": True,
                "dropped_obligation": bool(dropped),
                "dropped_current_obligation": bool(cur_drops),
                "dropped_replayed_obligation": bool(rep_drops),
            },
        )


class FutureObligationValidator:
    """Future-obligation contract (#idea 7): a candidate must keep symbols later
    source commits depend on.

    Mirrors :class:`ObligationValidator` (the side-obligations check) but for
    FUTURE obligations — symbols/imports/keys derived from later replayed commits'
    patches (what the rest of the source branch expects to still exist). The
    obligations are derived orchestrator-side (they need git + a history plan, which
    :class:`VerificationContext` doesn't carry — the :class:`DependencyPreservationValidator`
    injection pattern) and injected via :meth:`set_obligations` before each verify.

    Severity ``warning`` (the ObligationValidator precedent): feeds retry via the
    risk engine, like any other validator warning — NOT a hard reject. This makes a
    candidate that fails future obligations look like any other failed validator
    result: retryable, explainable, calibratable. The features it emits
    (``future_obligation_count`` etc.) flow to risk, accept reports, dry-run, and
    calibration uniformly.
    """

    name = "future_obligation"

    def __init__(self) -> None:
        # Per-unit mutable state: the orchestrator sets the obligations before
        # each verify() call (derived from the unit's snapshot, #idea 5). None
        # when no future obligations apply (the validator is a no-op).
        self._obligations = None

    def set_obligations(self, obligations) -> None:
        """Inject the per-unit FutureObligations (or None for a no-op).

        Called by the orchestrator before verify(); the obligations come from the
        unit's memoized HistoryDecisionContext snapshot (so the git patch-fetch
        runs once per unit, not per verify call).
        """
        self._obligations = obligations

    def verify(self, ctx: VerificationContext) -> VerificationCheckResult:
        from capybase.future_obligations import obligations_satisfied

        obls = self._obligations
        if obls is None or obls.empty:
            return VerificationCheckResult(
                name=self.name, passed=True,
                message="no future obligations (no later commits depend on this region)",
                features={"future_obligation_count": 0, "future_obligation_dropped_count": 0},
            )
        satisfied, dropped = obligations_satisfied(obls, ctx.candidate.resolved_text or "")
        # Split the dropped symbols by obligation kind for the feature spine.
        required = obls.required_symbols
        expected_keys = obls.expected_keys
        dropped_imports = [s for s in dropped if s in required]
        return VerificationCheckResult(
            name=self.name,
            passed=satisfied,
            severity="warning",
            message=(
                "resolution drops symbol(s) a later commit needs"
                if dropped
                else "resolution preserves all future obligations"
            ),
            detail={"dropped_symbols": dropped[:16]},
            features={
                "future_obligation_count": len(obls.obligations),
                "future_obligation_dropped_count": len(dropped),
                "future_obligation_dropped_symbols": ",".join(sorted(dropped))[:200],
                "future_obligation_dropped_imports": len(
                    [o for o in obls.obligations if o.kind == "import" and o.symbol in dropped]
                ),
                "future_obligation_dropped_keys": len(
                    [k for k in expected_keys if not satisfied]
                ),
            },
        )


class DependencyPreservationValidator:
    """SafeMerge necessary-condition: don't drop a base dependency.

    The verification-time complement to the prompt-time dependency context (P1).
    Both-sides-represented ensures a side's *additions* survive, but neither it
    nor any validator catches the Rover/WizardMerge failure mode where the merge
    silently removes a dependency that BASE and both sides relied on — e.g. the
    model drops a ``validate(input)`` call, a safety check, or a resource release
    that base + both edited sides all kept. That is a semantic regression the
    syntactic validators are structurally blind to.

    SafeMerge's full condition (build a 4-program product relation and prove
    conflict-freedom for every input/output) is out of scope, but there is a
    cheap deterministic *necessary* condition: if BASE references a symbol that
    has an in-repo definition, and NEITHER side removed it, then a valid merge
    must still reference it. Dropping it can't be justified by either branch's
    change, so the merge is suspect.

    Severity ``warning`` — a necessary-not-sufficient signal, so it feeds the
    risk/retry engine rather than hard-rejecting (a symbol name can legitimately
    appear in the resolution under a different spelling the heuristic misses).
    Inert by default: it only runs when the orchestrator registers it with slice
    config (search globs + repo root). When no in-repo definitions are found it
    records no warning — it can't flag a drop it never located.
    """

    name = "referenced_symbol_dropped"

    def __init__(
        self,
        slice_search_globs: list[str] | None = None,
        slice_repo_root: str | None = None,
        max_symbols: int = 12,
    ) -> None:
        self.slice_search_globs = slice_search_globs or ["**/*.py", "**/*.rs"]
        self.slice_repo_root = slice_repo_root
        self.max_symbols = max_symbols

    def verify(self, ctx: VerificationContext) -> VerificationCheckResult:
        lang = ctx.unit.language
        if lang not in ("python", "rust"):
            return self._pass("dependency check skipped (unsupported language)")
        try:
            from capybase.adapters import structural
        except Exception:  # noqa: BLE001
            return self._pass("dependency check skipped (structural adapter absent)")

        base_text = ctx.unit.base.text or ""
        cur_text = ctx.unit.current.text or ""
        rep_text = ctx.unit.replayed.text or ""
        merged_text = ctx.candidate.resolved_text or ""

        base_refs = set(_referenced_symbols(base_text, lang))
        if not base_refs:
            return self._pass("base references no symbols")

        # Only symbols that have an IN-REPO definition can be meaningfully
        # flagged — a stdlib/builtin drop is undetectable without resolution,
        # and flagging it would be pure false positive. So resolve base refs to
        # those with a definition, capped to keep the check cheap.
        globs = self._abs_globs()
        try:
            snippets = structural.find_symbol_definitions(
                list(base_refs)[: self.max_symbols], globs, lang, max_per=1
            )
        except Exception:  # noqa: BLE001
            return self._pass("dependency check skipped (slice failed)")
        defined = {s.reason for s in snippets}
        if not defined:
            # No base dependency has a resolvable definition — can't flag a drop.
            return self._pass("no in-repo dependency definitions found")

        merged_tokens = set(_referenced_symbols(merged_text, lang))
        # A symbol is "dropped" if: base referenced it, it has an in-repo
        # definition, NEITHER side removed it (so the drop isn't a branch's
        # intent), and the merge no longer references it.
        cur_tokens = set(_referenced_symbols(cur_text, lang))
        rep_tokens = set(_referenced_symbols(rep_text, lang))
        kept_by_both = base_refs & cur_tokens & rep_tokens
        dropped: list[str] = []
        for sym in sorted(defined):
            if sym in kept_by_both and sym not in merged_tokens:
                dropped.append(sym)

        if not dropped:
            return self._pass("all in-repo base dependencies preserved")
        return VerificationCheckResult(
            name=self.name,
            passed=False,
            severity="warning",
            message=(
                f"resolved text drops base-referenced symbol(s) neither side "
                f"removed: {', '.join(dropped)}"
            ),
            detail={"dropped_symbols": dropped},
            features={
                "dropped_referenced_symbol": True,
                "dropped_symbol_count": len(dropped),
            },
        )

    def _pass(self, msg: str) -> VerificationCheckResult:
        return VerificationCheckResult(
            name=self.name,
            passed=True,
            severity="warning",
            message=msg,
            features={
                "dropped_referenced_symbol": False,
                "dropped_symbol_count": 0,
            },
        )

    def _abs_globs(self) -> list[str]:
        import os

        if not self.slice_repo_root:
            return self.slice_search_globs
        return [
            g if os.path.isabs(g) else os.path.join(self.slice_repo_root, g)
            for g in self.slice_search_globs
        ]


def _referenced_symbols(text: str, language: str) -> list[str]:
    """Identifier extraction shared with the structural adapter.

    Delegates to ``structural.referenced_symbols`` so the validator and the
    context-builder slicer agree on what counts as a "reference". Imported
    lazily; returns an empty list if the adapter is unavailable.
    """
    try:
        from capybase.adapters import structural
    except Exception:  # noqa: BLE001
        return []
    return structural.referenced_symbols(text, language)


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

    This is the verifier-model seam (Proposer-Critic): every
    other validator is syntactic/structural — conflict markers, splice scope,
    AST preservation, syntax, LSP diagnostics, one-side-copy heuristic. None can
    catch a merge that parses cleanly but *semantically drops a side's intent*
    (e.g. it omits a guard one branch added). An LLM judge is the one check for
    that, run on the same black-box API client already in the orchestrator.

    Cost & safety contract:

    - **Opt-out.** Runs by default (``enable_verifier_model`` defaults True —
      it's the only check for silently-dropped intent). Set false to disable.
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

    def __init__(
        self,
        client: object,
        model_name: str = "",
        *,
        json_mode: bool = True,
        max_tokens: int = 0,
        prompt_builder=None,
        name_suffix: str = "",
    ) -> None:
        # ``client`` is the same LLMClient the resolution engine uses. Typed as
        # ``object`` to avoid an import cycle (adapters → ... → verification);
        # it only needs a ``complete`` method.
        self.client = client
        self.model_name = model_name
        self.json_mode = json_mode
        # Generation budget for the verdict call. Reasoning models (e.g.
        # VibeThinker/DeepSeek- style) emit a long <think> chain BEFORE the
        # JSON verdict; a fixed-small budget (the old 512) runs out mid-thought
        # (finish_reason=length) and the verdict is never produced → the critic
        # silently degrades to verifier_checked=False. Threaded from the model
        # config so it scales with the resolver's own budget. 0 = fall back to a
        # default that fits a non-reasoning model's verdict.
        self.max_tokens = max_tokens or _CRITIC_DEFAULT_MAX_TOKENS
        # PoLL jury (§2.1): a second critic with a DIFFERENT prompt focus. The
        # default builder judges intent preservation; a jury member passes a
        # complementary builder (e.g. conflict/contradiction focus) so the union
        # of both critics' flags broadens coverage. Lazy-imported to avoid a
        # cycle (resolution_engine → ... → verification).
        self._prompt_builder = prompt_builder
        # Distinguishes jury members in features/warnings: "verifier_model" (the
        # default preservation critic) vs "verifier_model_conflict". The risk
        # engine matches the ``verifier_model*`` prefix so all jury members route
        # to the critic retry path.
        if name_suffix:
            self.name = f"verifier_model_{name_suffix}"

    def _build_prompt(self, unit, candidate, context):
        if self._prompt_builder is not None:
            return self._prompt_builder(unit, candidate, context)
        from capybase.resolution_engine import build_verifier_prompt
        # Phase 1 (critic guardrail): the deterministic assertion is injected
        # unless the config disables it. The validator recomputes the same
        # signal for the Phase 3 hard-backstop after parsing the verdict.
        assertion_enabled = getattr(self, "_assertion_enabled", True)
        return build_verifier_prompt(
            unit, candidate, context, assertion_enabled=assertion_enabled
        )

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
        from capybase.adapters.parsers import parse_resolution_json

        prompt = self._build_prompt(ctx.unit, ctx.candidate, _verifier_context(ctx))
        messages = [
            {"role": "system", "content": "You are a strict code reviewer."},
            {"role": "user", "content": prompt},
        ]
        try:
            resp = self.client.complete(
                messages,
                model=self.model_name or _default_model(ctx),
                temperature=0.0,
                max_tokens=self.max_tokens,
                json_mode=self.json_mode,
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
        # Value-resolution override (Phase 5.1 contract enforcement): when both
        # sides preserved the same statement shape and only a value diverged, a
        # ONE-SIDED merge IS the correct resolution. The critic judges "did the
        # resolution preserve each side's intent?", which for a value conflict
        # means "did it keep one of the divergent values?" — picking either side
        # satisfies that. BUT only when the candidate IS genuinely one-sided
        # (verbatim copy of one side). A SYNTHESIZED resolution (neither side
        # verbatim) is NOT automatically safe even for a value-resolution — it
        # could drop a side-effect call. The critic must still judge those.
        cur_text = (ctx.unit.current.text or "").strip()
        rep_text = (ctx.unit.replayed.text or "").strip()
        resolved_text = (ctx.candidate.resolved_text or "").strip()
        copied_one = (
            (bool(cur_text) and resolved_text == cur_text)
            or (bool(rep_text) and resolved_text == rep_text)
        )
        cf = ctx.unit.structural_metadata.get("conflict_features")
        if (
            isinstance(cf, dict)
            and cf.get("value_resolution")
            and not preserves_both
            and copied_one
        ):
            preserves_both = True
            dropped = []
        # Critic guardrail telemetry — accumulates across the phases below.
        guardrail_suppressed = False
        guardrail_reason = ""
        reassessed = False
        reassessment_outcome = ""
        reassessment_evidence_verified = False
        if not preserves_both:
            from capybase.resolution_engine import (
                DeterministicPreservation,
                _deterministic_preservation,
                build_verifier_reassessment_prompt,
            )

            cur_lines, base_lines, rep_lines = _verifier_sides(ctx.unit)
            dp = _deterministic_preservation(
                ctx.unit, ctx.candidate, cur_lines, rep_lines, base_lines,
            )
            # Phase 3 — hard backstop: if the deterministic coverage is UNANIMOUSLY
            # perfect (both ratios 1.0, no dropped additions), the math definitively
            # contradicts the critic. Suppress regardless of confidence — zero
            # extra LLM calls. Never fires on a genuine drop (which lowers a ratio).
            if getattr(cfg, "enable_verifier_guardrail", True) and dp.unanimous:
                preserves_both = True
                dropped = []
                guardrail_suppressed = True
                guardrail_reason = (
                    "deterministic preservation unanimous "
                    f"(cur={dp.cur_ratio:.2f}, rep={dp.rep_ratio:.2f}, "
                    "no dropped additions)"
                )
            # Phase 2 — show-your-work reflection: the critic flagged but entity
            # coverage is high (not a clear structural drop). Demand it quote the
            # exact missing/mangled snippet; verify the citation programmatically.
            # Null/fabricated evidence squashes the flag. Skipped below the coverage
            # floor — when entities are genuinely missing, the critic is likely
            # right and a reassessment call would waste budget.
            elif (
                getattr(cfg, "enable_verifier_reflection", True)
                and dp.min_ratio >= getattr(cfg, "verifier_reflection_coverage_floor", 0.9)
            ):
                rev_ok = self._reassess(
                    ctx, data, dp,
                    build_verifier_reassessment_prompt,
                )
                reassessed = True
                if rev_ok is True:
                    # Critic revoked (null/fabricated evidence) → squash.
                    preserves_both = True
                    dropped = []
                    reassessment_outcome = "revoke"
                    reassessment_evidence_verified = True
                elif rev_ok is False:
                    # Critic held with grounded, verifiable evidence → stand.
                    reassessment_outcome = "hold"
                    reassessment_evidence_verified = True
                else:
                    # Reassessment call failed/unparseable → can't override; stand.
                    reassessment_outcome = "skip"
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
                "verifier_guardrail_suppressed": guardrail_suppressed,
                "verifier_guardrail_reason": guardrail_reason,
                "verifier_reassessed": reassessed,
                "verifier_reassessment_outcome": reassessment_outcome,
                "verifier_reassessment_evidence_verified": reassessment_evidence_verified,
            },
        )

    def _reassess(
        self,
        ctx: VerificationContext,
        original_verdict: dict,
        dp: "DeterministicPreservation | None",
        prompt_builder,
    ) -> bool | None:
        """Phase 2 show-your-work reflection (critic guardrail).

        A second LLM call demanding the critic quote the exact missing/mangled
        snippet. Returns:
        - True: the critic REVOKED (evidence null/fabricated) → squash the flag.
        - False: the critic HELD with grounded, verifiable evidence → stand.
        - None: the call failed/unparseable → can't override (stand, no telemetry
          claim).

        Evidence verification is PROGRAMMATIC (substring match against the actual
        sides + resolved text), not another model judgment — so it doesn't inherit
        the critic's bias. A snippet that isn't a verbatim substring of any side
        is fabricated → revoke.
        """
        from capybase.adapters.parsers import parse_resolution_json

        prompt = prompt_builder(ctx.unit, ctx.candidate, original_verdict, dp)
        messages = [
            {"role": "system", "content": "You are re-examining your own verdict rigorously."},
            {"role": "user", "content": prompt},
        ]
        try:
            resp = self.client.complete(
                messages,
                model=self.model_name or _default_model(ctx),
                temperature=0.0,
                max_tokens=self.max_tokens,
                json_mode=self.json_mode,
            )
        except Exception:  # noqa: BLE001 - never crash resolution
            return None
        rdata, _ = parse_resolution_json(resp.text or "")
        if not rdata:
            return None
        accurate = bool(rdata.get("original_verdict_accurate", True))
        evidence = rdata.get("evidence_snippet")
        # If the critic revoked itself, squash.
        if not accurate:
            return True
        # If it held but provided no evidence, it can't ground the claim → squash.
        if not evidence or not str(evidence).strip():
            return True
        # Verify the evidence is a VERBATIM substring of a side or the resolved
        # text. A fabricated citation (not found anywhere) → squash. A genuine
        # snippet from a side that's absent from the resolved text → stand.
        ev = str(evidence)
        cur_lines, base_lines, rep_lines = _verifier_sides(ctx.unit)
        resolved = ctx.candidate.resolved_text or ""
        in_resolved = ev in resolved
        in_current = ev in cur_lines
        in_replayed = ev in rep_lines
        in_base = ev in base_lines
        # Grounded evidence: the snippet is real text (appears in a side) AND is
        # genuinely absent from the resolution (the drop claim is real). If it
        # appears in the resolution, the critic is wrong (it's present) → squash.
        if in_resolved:
            return True  # the "missing" text is actually present → revoke
        # Absent from resolved — is it real text from a side that should be there?
        if in_current or in_replayed or in_base:
            return False  # grounded, verifiable, genuinely absent → stand
        # Not found anywhere → fabricated citation → revoke.
        return True


def _verifier_sides(unit):
    """The conflict sides for the critic prompt (diff3-refined when available)."""
    refined = unit.refined_sides
    if refined is not None:
        return refined
    return unit.current.text, unit.base.text, unit.replayed.text


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


# ---------------------------------------------------------------------------
# VeriGuard-style deterministic policy gate
#
# The only validator that inspects WHAT a patch introduces (every other
# validator is syntactic/structural). Statically extracts import/call facts
# from the candidate's resolved text via stdlib ast (Python only) and evaluates
# them against a configurable ruleset. Fully deterministic at runtime — no LLM,
# no execution. Tags violations onto ConflictUnit.risk_tags (the vestigial seam
# this fills) and returns a VerificationCheckResult like any validator.
# ---------------------------------------------------------------------------


@dataclass
class PolicyFacts:
    """Static facts extracted from a candidate's resolved text."""

    imports: set[str] = field(default_factory=set)
    calls: set[str] = field(default_factory=set)


class _PolicyFactExtractor(ast.NodeVisitor):
    """ast visitor collecting imported modules and call targets (Python)."""

    def __init__(self) -> None:
        self.imports: set[str] = set()
        self.calls: set[str] = set()

    def visit_Import(self, node) -> None:  # noqa: N802 - ast convention
        for alias in node.names:
            if alias.name:
                self.imports.add(alias.name)

    def visit_ImportFrom(self, node) -> None:  # noqa: N802 - ast convention
        if node.module:
            self.imports.add(node.module)

    def visit_Call(self, node) -> None:  # noqa: N802 - ast convention
        name = _dotted_name(node.func)
        if name:
            self.calls.add(name)
        self.generic_visit(node)


def _dotted_name(node) -> str:
    """Render an ast function-reference node as a dotted name (eval, os.system)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _extract_policy_facts(text: str, language: str | None) -> PolicyFacts:
    """Extract import/call facts from Python source. Empty for other languages
    or unparseable text (the syntax validator catches syntax errors separately;
    a parse failure here must never crash the gate).

    The resolved_text is a splice FRAGMENT (the merged code replacing a conflict
    marker block), not a whole module — so it may contain a bare ``return`` or
    leading-indent statements that aren't valid at module scope. We parse it as
    a module first; on SyntaxError we retry wrapped in a dummy function body, so
    the fragment's imports and calls become extractable regardless of scope.
    """
    if language != "python" or not text:
        return PolicyFacts()

    tree = _safe_parse_fragment(text)
    if tree is None:
        return PolicyFacts()
    extractor = _PolicyFactExtractor()
    extractor.visit(tree)
    return PolicyFacts(imports=extractor.imports, calls=extractor.calls)


def _safe_parse_fragment(text: str):
    """Parse ``text`` as a Python module, tolerating splice-fragment scope.

    Thin delegate to the canonical ``value_resolution._safe_parse_fragment``
    with ``unwrap=False`` (the policy-fact extractor recurses via
    ``NodeVisitor.generic_visit``, so the wrapper module is returned as-is).
    """
    from capybase.value_resolution import _safe_parse_fragment as _canonical
    return _canonical(text, unwrap=False)


class PolicyGateValidator:
    """Deterministic safety gate over candidate import/call facts.

    Evaluates a configured ruleset (``PolicyRule``) against statically-extracted
    facts. A ``forbid_import`` rule matches when its pattern is a prefix of any
    imported module; ``forbid_call`` when its pattern is a prefix of any call
    target. Violations tag ``ConflictUnit.risk_tags`` and (at error severity)
    become hard failures that block auto-apply.

    Cost & safety contract:

    - **Opt-in + needs rules.** Inert unless ``enable_policy_gate`` is on AND
      ``policy_rules`` is non-empty. No rules → no-op even when enabled (the
      code ships none; deployments define their own).
    - **Deterministic.** No LLM call, no execution — stdlib ast only.
    - **Graceful.** Non-Python language and unparseable text yield empty facts
      (the gate passes; syntax errors are the syntax validator's job).
    """

    name = "policy_gate"

    def verify(self, ctx: VerificationContext) -> VerificationCheckResult:
        cfg = ctx.config
        rules = list(getattr(cfg, "policy_rules", ()) or ())
        if not getattr(cfg, "enable_policy_gate", False) or not rules:
            return VerificationCheckResult(
                name=self.name,
                passed=True,
                severity="error",
                message="policy gate disabled or no rules configured",
                features={"policy_checked": False},
            )

        facts = _extract_policy_facts(
            ctx.candidate.resolved_text, ctx.unit.language
        )
        features: dict[str, float | int | str | bool] = {"policy_checked": True}
        violations: list[tuple[str, str, str]] = []  # (name, severity, reason)
        max_sev = "warning"  # escalate only on an error-severity violation

        def _rule_field(rule, name, default=""):
            """Read a rule field whether the rule is a PolicyRule object or a
            plain dict (rules can cross the pydantic/dataclass boundary as dicts)."""
            if isinstance(rule, dict):
                return rule.get(name, default)
            return getattr(rule, name, default)

        for rule in rules:
            kind = _rule_field(rule, "kind", "")
            pattern = _rule_field(rule, "pattern", "")
            severity = _rule_field(rule, "severity", "error")
            hit = False
            if kind == "forbid_import":
                hit = any(m == pattern or m.startswith(pattern + ".") or m == pattern
                          for m in facts.imports)
            elif kind == "forbid_call":
                hit = any(m == pattern or m.startswith(pattern + ".")
                          for m in facts.calls)
            if hit:
                rname = _rule_field(rule, "name", pattern)
                violations.append((
                    rname,
                    severity,
                    _rule_field(rule, "reason", "") or f"{kind} {pattern}",
                ))
                features[f"policy_{rname}_violated"] = True

        features["policy_violation_count"] = len(violations)
        if any(sev == "error" for _, sev, _ in violations):
            max_sev = "error"

        # Tag the unit's vestigial risk_tags with the violation names.
        if violations:
            existing = set(ctx.unit.risk_tags)
            for vname, _, _ in violations:
                existing.add(f"policy:{vname}")
            ctx.unit.risk_tags = sorted(existing)

        passed = not violations or max_sev != "error"
        msg = (
            "policy gate: " + "; ".join(reason for _, _, reason in violations)
            if violations else "policy gate: no violations"
        )
        return VerificationCheckResult(
            name=self.name,
            passed=passed,
            severity=max_sev,
            message=msg,
            detail={"violations": [{"name": n, "severity": s, "reason": r}
                                   for n, s, r in violations]},
            features=features,
        )


# ---------------------------------------------------------------------------
# LLM code-smell detection
#
# A cheap pre-test quality filter for smells common in LLM-generated code,
# detected statically via stdlib ast. A sibling of the policy gate: same
# fragment-tolerant parsing (_safe_parse_fragment), same NodeVisitor pattern,
# same Validator -> VerificationCheckResult protocol. Only the AST-clean smells
# are implemented; dataflow smells (scaling/leakage/hyperparameters) need
# richer analysis and are deferred.
# ---------------------------------------------------------------------------


@dataclass
class SmellFinding:
    """One detected code smell."""

    name: str        # canonical smell id, e.g. "nan_comparison"
    detail: str      # short human message


class _SmellDetector(ast.NodeVisitor):
    """ast visitor collecting LLM-specific code smells (Python).

    Three AST-clean detectors (single pass over a fragment):

    - ``nan_comparison``: ``x == np.nan`` / ``x != np.nan``. NaN compares
      unequal to everything in IEEE 754, so these are always False/True — a
      classic LLM bug. The correct idiom is ``np.isnan``.
    - ``chain_indexing``: ``df[a][b]`` — a Subscript whose value is itself a
      Subscript over a likely DataFrame (Name/Attribute). Ambiguous, the
      SettingWithCopyWarning source. ``.loc``/``.iloc`` are not flagged.
    - ``unseeded_randomness``: calls to ``random.*`` / ``numpy.random.*`` with
      no ``random.seed``/``numpy.random.seed`` anywhere in the fragment.
      Affects reproducibility.
    """

    # Module names whose ``.nan`` attribute is a float NaN.
    _NAN_MODULES = {"numpy", "np", "math"}
    # Random-call prefixes (dotted); matched as startswith.
    _RANDOM_PREFIXES = ("random.", "numpy.random.", "np.random.")
    _SEED_CALLS = {"random.seed", "numpy.random.seed", "np.random.seed"}

    def __init__(self) -> None:
        self.findings: list[SmellFinding] = []
        self._random_calls: int = 0
        self._has_seed: bool = False

    # --- NaN comparison -------------------------------------------------
    def visit_Compare(self, node) -> None:  # noqa: N802 - ast convention
        for cmp in node.comparators:
            if self._is_nan(cmp):
                self.findings.append(SmellFinding(
                    name="nan_comparison",
                    detail="comparison to nan is always False/True; use isnan()",
                ))
                break  # one finding per Compare node
        self.generic_visit(node)

    @staticmethod
    def _is_nan(node) -> bool:
        # np.nan / numpy.nan / math.nan
        if isinstance(node, ast.Attribute) and node.attr == "nan":
            base = node.value
            return isinstance(base, ast.Name) and base.id in _SmellDetector._NAN_MODULES
        # bare `nan` name (rare without a binding, but detectable)
        return isinstance(node, ast.Name) and node.id == "nan"

    # --- Pandas chain indexing ------------------------------------------
    def visit_Subscript(self, node) -> None:  # noqa: N802 - ast convention
        inner = node.value
        if (
            isinstance(inner, ast.Subscript)
            and isinstance(inner.value, (ast.Name, ast.Attribute))
            and not isinstance(node.slice, ast.Tuple)
        ):
            self.findings.append(SmellFinding(
                name="chain_indexing",
                detail="chained subscript df[a][b] is ambiguous; use .loc/.iloc",
            ))
        self.generic_visit(node)

    # --- Uncontrolled randomness ----------------------------------------
    def visit_Call(self, node) -> None:  # noqa: N802 - ast convention
        name = _dotted_name(node.func)
        if name:
            if any(name.startswith(p) for p in self._RANDOM_PREFIXES):
                if name not in self._SEED_CALLS:
                    self._random_calls += 1
            if name in self._SEED_CALLS:
                self._has_seed = True
        self.generic_visit(node)

    def finalize(self) -> list[SmellFinding]:
        """Emit the unseeded-randomness finding (needs the full pass first to
        know whether a seed appeared). Call after ``visit``."""
        if self._random_calls > 0 and not self._has_seed:
            self.findings.append(SmellFinding(
                name="unseeded_randomness",
                detail=f"{self._random_calls} random call(s) with no seed set; "
                       "reproducibility at risk",
            ))
        return self.findings


def _detect_code_smells(text: str, language: str | None) -> list[SmellFinding]:
    """Detect LLM code smells in Python source. Empty for other languages or
    unparseable text (reuses the policy gate's fragment-tolerant parser)."""
    if language != "python" or not text:
        return []
    tree = _safe_parse_fragment(text)
    if tree is None:
        return []
    detector = _SmellDetector()
    detector.visit(tree)
    return detector.finalize()


class CodeSmellValidator:
    """Deterministic LLM code-smell checker.

    Statically detects smells common in LLM-generated code (NaN comparison,
    pandas chain indexing, uncontrolled randomness) via stdlib ast and returns a
    VerificationCheckResult like any validator. A sibling of PolicyGateValidator:
    same fragment-tolerant parsing, same NodeVisitor pattern, same opt-in gate.

    Cost & safety contract:

    - **Opt-in.** Inert unless ``enable_code_smell_checks`` is on.
    - **Deterministic.** No LLM call, no execution — stdlib ast only.
    - **Graceful.** Non-Python language and unparseable text yield no findings.
    - **Severity configurable.** Defaults to ``"warning"`` (smells are quality
      issues that bias toward review, not always correctness bugs); strict
      deployments set ``"error"`` to hard-block smelly patches.
    """

    name = "code_smell"

    def verify(self, ctx: VerificationContext) -> VerificationCheckResult:
        cfg = ctx.config
        if not getattr(cfg, "enable_code_smell_checks", False):
            return VerificationCheckResult(
                name=self.name,
                passed=True,
                severity=getattr(cfg, "code_smell_severity", "warning"),
                message="code smell checks disabled",
                features={"smell_checked": False},
            )

        findings = _detect_code_smells(ctx.candidate.resolved_text, ctx.unit.language)
        severity = getattr(cfg, "code_smell_severity", "warning")
        features: dict[str, float | int | str | bool] = {"smell_checked": True}
        for f in findings:
            features[f"smell_{f.name}"] = True
        features["smell_count"] = len(findings)

        # Tag the unit's risk_tags with the smell names.
        if findings:
            existing = set(ctx.unit.risk_tags)
            for f in findings:
                existing.add(f"smell:{f.name}")
            ctx.unit.risk_tags = sorted(existing)

        passed = len(findings) == 0 or severity != "error"
        msg = (
            "code smells: " + "; ".join(f.detail for f in findings)
            if findings else "code smells: none"
        )
        return VerificationCheckResult(
            name=self.name,
            passed=passed,
            severity=severity,
            message=msg,
            detail={"findings": [{"name": f.name, "detail": f.detail}
                                  for f in findings]},
            features=features,
        )


def _resolve_tool(name: str) -> str | None:
    """Resolve an executable name to a path via shutil.which, or None."""
    from shutil import which

    return which(name)


# Rust diagnostics with an ``E0xxx`` code are SEMANTIC (type-check, borrow-check,
# name resolution, missing-field), not parse errors. A per-unit standalone rustc
# compile can't resolve crate dependencies or know the full struct/function
# context, so these surface as artifacts of compiling a fragment out of crate
# context — NOT defects in the merge. Only true PARSE/syntax errors (which rustc
# emits WITHOUT an E0xxx code, e.g. ``error: expected `;`...``) are candidate
# defects at the per-unit level. Semantic errors defer to Phase B (whole-file
# cargo check) where the full crate context is available.
_RUST_SEMANTIC_ERROR = re.compile(r"error\[E\d{4}\]")
# Resolution-shaped diagnostics rustc emits WITHOUT a bracketed E-code
# (macro/name resolution): "error: cannot find macro `X` in this scope",
# "error: use of undeclared crate or module `X`". Same semantic-artifact
# class as E0432/E0433 — crate-internal references a standalone rustc
# fragment cannot resolve (tokio-0108: the model's merge references
# tokio's own `cfg_not_io_util!` macro, defined elsewhere in the crate;
# classified as a parse defect it wrongly hard-failed two substantive
# candidates into a no-progress escalate).
_RUST_RESOLUTION_SHAPES = re.compile(
    r"error: (?:cannot find (?:value|type|macro)|use of undeclared "
    r"(?:crate or module|type|value)|unresolved import)")


def _is_rust_resolution_error(msg: str) -> bool:
    """True when a rustc diagnostic is a semantic (E0xxx) error, not a parse error.

    Per-unit standalone rustc produces semantic errors (missing fields, type
    mismatches, unresolved names) as artifacts of the fragment lacking crate
    context. These are NOT candidate defects. Only parse/syntax errors (emitted
    WITHOUT an ``E0xxx`` code) are real defects at this level — e.g. the
    malformed ``format!`` macro reads ``error: expected`` with no code.
    Resolution-shaped uncoded errors ("cannot find macro", "use of
    undeclared") are semantic too — rustc emits macro resolution failures
    without a bracketed code, but they are artifacts of the missing crate
    context, not parse defects.
    """
    if not msg:
        return False
    return bool(_RUST_SEMANTIC_ERROR.search(msg)
                or _RUST_RESOLUTION_SHAPES.search(msg))


# C/C++ diagnostics that indicate a RESOLUTION / type problem, NOT a parse
# defect. Standalone ``gcc``/``clang -fsyntax-only`` on a fragment can't resolve
# symbols from another translation unit or a header not pre-declared in the
# fragment, so it surfaces these as artifacts of compiling out of context — NOT
# merge defects. Only PARSE errors (``expected ';'``, ``unexpected``, ``stray``,
# ``unterminated``) are real defects at the per-unit level. Semantic errors defer
# to Phase B (whole-file check) where the full translation-unit context is
# available. gcc/clang have no tidy E-code scheme like rustc, so classify by
# message-text prefixes (matched case-insensitively). The list is conservative —
# a prefix not listed here is treated as a parse error (surfaced), erring toward
# catching real defects over suppressing noise.
_CCS_SEMANTIC_PATTERNS = (
    "undeclared identifier",          # gcc/clang C++: use of undeclared identifier
    "' undeclared",                   # gcc C-mode: 'varname' undeclared (first use)
    "implicit declaration of function",  # gcc C-mode: undeclared function (C-specific)
    "was not declared in this scope",  # g++ scope resolution
    "has not been declared",          # g++ forward-decl-only
    "no matching function",           # overload resolution (needs full TU)
    "cannot convert",                 # type conversion (needs full decls)
    "incomplete type",                # forward-declared, definition elsewhere
    "is not a member of",             # struct member resolution
    "does not name a type",           # typedef / class-name resolution (clang)
    "unknown type name",              # gcc: "unknown type name 'u8'" — undefined
                                     # typedef (project-internal type defined in a
                                     # sibling header standalone gcc can't see). The
                                     # gcc wording for what clang calls "does not
                                     # name a type". Surfaced in the C live-eval
                                     # (sqlite vdbe.h, btree.h, vdbeInt.h referencing
                                     # u8, sqlite3_vfs, BtCursor from sqliteInt.h).
    "no member named",                # struct field resolution
    "undefined reference",            # linker-level (defensive; -fsyntax-only skips link)
    "suggest an alternative",         # clang "did you mean?" (resolution, not parse)
    # Missing project-internal headers (#include "server.h", "sqliteInt.h") —
    # standalone -fsyntax-only has no -I flags and runs in /tmp, so any sibling
    # header is unresolved. The C analog of "undeclared identifier": an artifact
    # of compiling a fragment out of translation-unit context, NOT a parse
    # defect. Surfaced in the C live-eval (redis pubsub.c, sqlite mutex_w32.c)
    # as false-positive hard failures that escalated sim-0.99 merges. The
    # whole-file build command (make) is the authoritative oracle for these.
    "fatal error",                    # gcc: "fatal error: X.h: No such file..."
    "no such file or directory",      # trailing detail of the same diagnostic
)
_CCS_SEMANTIC_RE = re.compile(
    "|".join(re.escape(p) for p in _CCS_SEMANTIC_PATTERNS), re.IGNORECASE
)


# GCC/clang parse-error categories that a deterministic repair beam can target.
# Each maps a substring of the gcc error message to a category that determines
# the repair action. Validated against gcc 14's output format. Used by the
# compiler-diagnostic-driven repair beam in orchestrator._try_deterministic_cc_repair.
_CCS_PARSE_ERROR_CATEGORIES: dict[str, str] = {
    # Order matters: longer/more-specific patterns first.
    # Patterns use the SEMANTIC content of gcc's message (not the quote chars,
    # which gcc emits as curly '…' quotes). We match case-insensitively on
    # the message text, stripping/normalizing quotes.
    "expected declaration or statement at end of input": "missing_close_brace",
    "expected } at end of input": "missing_close_brace",
    "expected ; before": "missing_semicolon",
    "expected ;": "missing_semicolon",
    "expected identifier or ( before }": "extra_close_brace",
    "expected identifier or (": "extra_close_brace",
    "stray": "stray_character",
    "unterminated": "unterminated_literal",
    "missing terminating": "unterminated_literal",
    "expected )": "missing_close_paren",
    "expected ]": "missing_close_bracket",
    "redefinition of": "duplicate_entity",
    "duplicate definition": "duplicate_entity",
    "expected expression": "expected_expression",
}


def _classify_ccs_parse_error(msg: str) -> str | None:
    """Classify a gcc/clang parse error message into a repair-actionable category.

    Returns a category string (e.g. ``"missing_semicolon"``) when the message
    matches a known parse-error pattern, or ``None`` when it doesn't classify
    (semantic errors, unknown shapes). Used by the deterministic repair beam
    to select the appropriate repair action.

    Strips ALL quote characters (gcc wraps tokens in curly/single/double
    quotes) before case-insensitive substring matching against
    ``_CCS_PARSE_ERROR_CATEGORIES``, with longer patterns checked first.
    """
    if not msg:
        return None
    # Strip all quote-like chars so patterns match token content directly.
    normalized = msg.translate(str.maketrans({
        "\u2018": " ", "\u2019": " ", "\u201c": " ", "\u201d": " ",
        "'": " ", '"': " ",
    })).lower()
    # Collapse double spaces from the stripping.
    while "  " in normalized:
        normalized = normalized.replace("  ", " ")
    for pattern, category in _CCS_PARSE_ERROR_CATEGORIES.items():
        if pattern.lower() in normalized:
            return category
    return None


def _is_ccs_resolution_error(msg: str) -> bool:
    """True when a gcc/clang diagnostic is semantic (resolution/type), not parse.

    Per-unit standalone ``-fsyntax-only`` produces semantic errors (undeclared
    identifiers, type mismatches, unresolved members) as artifacts of the
    fragment lacking full translation-unit context. These are NOT candidate
    defects. Only parse/syntax errors (``expected ';'``, ``stray '\\xxx'``,
    ``unterminated``) are real defects at this level — they don't match any of
    the semantic patterns and so return False (surfaced).
    """
    return bool(msg and _CCS_SEMANTIC_RE.search(msg))


def _mask_strings_and_comments(text: str, language: str | None) -> str:
    """Mask string/char literals and strip line comments, language-aware.

    Masks strings/chars FIRST (so a comment marker or ``{``/``}`` inside a
    string is hidden), then strips the language-correct line-comment marker.
    Without string-first ordering, a ``//`` or ``#`` inside a string cut it
    open and a brace before it counted as code → phantom imbalance.

    Uses the canonical :mod:`string_lexer` so EVERY string form is handled
    (Rust raw ``r#\"...\"#``, C++ raw ``R\"(...)\"``, Python triple-quotes,
    char literals vs lifetimes) — the prior regex-based mask leaked raw-string
    content, corrupting the brace count when a raw string contained ``{``/``}``.
    """
    from capybase.adapters.string_lexer import blank_strings_and_comments
    # blank_strings_and_comments is length-preserving (strings → '_', comments →
    # ' '). For brace counting, any non-brace placeholder is equivalent; we use
    # the default masking and let callers count braces on the result. The
    # comment-stripping (to end-of-line) the prior code did is subsumed: the
    # lexer blanks the comment content in-place, which is brace-neutral.
    return blank_strings_and_comments(text, language)


#: Extensions whose content is source code for the language-structural gates
#: (brace balance, py_compile, AST sanity). Everything else — markdown,
#: TOML/lockfiles, plain text — has no brace semantics: brace-counting prose
#: rejects perfect merges because a code fence or template placeholder had an
#: unbalanced brace (axum CHANGELOG.md ×4 at sim 1.000, sprint-16 census).
#: Extensionless files (LICENSE, README, CHANGELOG) are not code either.
_STRUCTURAL_CODE_EXTS = frozenset({
    ".rs", ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".ipp",
    ".inl", ".py", ".pyi", ".java", ".go", ".ts", ".tsx", ".js", ".mjs",
})


def structural_gate_applies(path: str | None) -> bool:
    """Whether the file at ``path`` should run the code-structural gates.

    False for markdown/config/prose files: their merge quality is judged by
    marker-free-ness and content similarity alone. The single allowlist used
    by the live eval's post-hoc compiles check, the true-side portfolio's
    brace sanity check, and the wholesale winner floor.
    """
    if not path:
        return False
    ext = os.path.splitext(path)[1].lower()
    return ext in _STRUCTURAL_CODE_EXTS if ext else False


def _braces_balanced(text: str, language: str | None = None) -> bool:
    """Cheap structural sanity check: are ``{}`` braces balanced?

    A per-unit splice that fills a marker span inside a larger construct (e.g.
    a bare ``Config { ... }`` initializer extracted without the surrounding
    ``impl``/``fn``) produces structurally-incomplete code that rustc rejects
    with a spurious parse error (``error: missing `struct` for struct
    definition``) — a false positive, since the merge is correct in context.
    This guard skips the compile when the spliced text has unbalanced braces,
    deferring to Phase B (whole-file cargo) where the full context is available.
    String/comment contents are ignored (naive but sufficient — a literal ``{``
    inside a string that throws off the count is rare and the guard only SKIPS,
    never false-fails). Comments (``//`` to EOL, ``#`` to EOL) are stripped first.
    """
    return _brace_imbalance_line(text, language) is None


def _brace_imbalance_line(text: str, language: str | None = None) -> int | None:
    """The 0-based line where ``{}`` braces first diverge, or None if balanced.

    Tracks running brace depth line-by-line; returns the line of the FIRST
    closing ``}`` that makes depth negative (an extra close), or the LAST line
    if depth ends positive (an unclosed ``{``). Used by the post-splice coherence
    gate (Fix #2a) to attribute the imbalance to a unit and point the repair
    feedback at the right line. Strings/comments are stripped first (same
    normalization as ``_braces_balanced``).
    """
    if not text:
        return None
    cleaned = _mask_strings_and_comments(text, language)
    depth = 0
    last_line = 0
    for line_no, line in enumerate(cleaned.split("\n")):
        last_line = line_no
        for ch in line:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth < 0:
                    return line_no  # extra closing brace here
    if depth != 0:
        return last_line  # unclosed opening brace; point at the end
    return None


def _strip_strings_comments(text: str, language: str | None = None) -> list[str]:
    """Mask string/char literals then strip line comments (language-aware).

    Returns the cleaned lines (without the trailing-join). Shared by the brace
    gate (:func:`_brace_imbalance_line`) and the deterministic repair
    (:func:`_try_balance_braces`) so the two agree on what counts as a brace.
    Masks strings FIRST so a comment marker or brace inside a string is hidden;
    strips only the language-correct comment marker (``//`` for brace langs,
    ``#`` for Python/Ruby, both for PHP).
    """
    return _mask_strings_and_comments(text, language).split("\n")


# Preprocessor directive patterns for C/C++.
_PP_OPEN_RE = re.compile(r"^\s*#\s*(if|ifdef|ifndef)\b")
_PP_ELSE_RE = re.compile(r"^\s*#\s*(else|elif)\b")
_PP_CLOSE_RE = re.compile(r"^\s*#\s*endif\b")


def _preprocessor_imbalance_line(text: str) -> int | None:
    """The 0-based line where ``#if/#endif`` nesting first diverges, or None.

    A push-down automaton over C/C++ preprocessor directives: ``#if``,
    ``#ifdef``, ``#ifndef`` push; ``#endif`` pops. Returns the line of the
    FIRST ``#endif`` that makes the stack go negative (extra close), or the
    LAST line if the stack ends non-empty (unclosed ``#if``). ``#else`` /
    ``#elif`` without a matching ``#if`` is also an error.

    Strings/comments are masked first (so a ``#if`` inside a string literal
    doesn't count). Recognizes ``#`` only at the first non-whitespace position
    on a line (the C preprocessor rule). Handles ``\\`` line continuations
    (splices continued lines before scanning).

    This catches merge artifacts (missing ``#endif``, broken ``#ifdef`` guard)
    that build-pass can't detect when the region is platform-guarded (e.g.
    ``#ifdef SQLITE_MUTEX_W32`` stripped on Linux, hiding conflict markers
    inside the region).
    """
    if not text:
        return None
    # Mask strings/comments so directives inside them don't count.
    masked = _mask_strings_and_comments(text, "c")
    # Splice line continuations: join lines ending with `\` into logical lines.
    # Track the original line number of the first physical line in each splice.
    logical_lines: list[tuple[int, str]] = []
    pending = ""
    pending_line = 0
    for line_no, line in enumerate(masked.split("\n")):
        if pending:
            pending += line
        else:
            pending_line = line_no
            pending = line
        if pending.endswith("\\"):
            pending = pending[:-1]
            continue
        logical_lines.append((pending_line, pending))
        pending = ""
    if pending:
        logical_lines.append((pending_line, pending))

    depth = 0
    last_line = 0
    for orig_line, line in logical_lines:
        last_line = orig_line
        if _PP_OPEN_RE.match(line):
            depth += 1
        elif _PP_ELSE_RE.match(line):
            if depth <= 0:
                return orig_line  # #else/#elif without matching #if
        elif _PP_CLOSE_RE.match(line):
            depth -= 1
            if depth < 0:
                return orig_line  # extra #endif
    if depth != 0:
        return last_line  # unclosed #if/#ifdef/#ifndef
    return None


def _structural_validate(
    text: str, language: str | None = None,
) -> list[VerificationFailure]:
    """Cheap O(n) structural validator — runs BEFORE the expensive build gate.

    Catches defects in milliseconds that the build gate would take seconds to
    discover, AND catches defects the build gate CAN'T see (preprocessor
    imbalance inside platform-guarded regions, conflict markers in inactive
    ``#ifdef`` branches).

    Returns a list of ``VerificationFailure`` (empty if structurally valid).
    Each failure has ``validator="structural"`` and a ``detail`` dict with the
    defect type and line number.

    Checks (all three reviewer feedbacks' "hard checks" list):
    - Conflict markers remaining anywhere (``<<<<<<<`` etc.)
    - Brace imbalance (``{}`` after string/comment masking)
    - Preprocessor imbalance (``#if``/``#endif`` nesting)
    - Parenthesis/bracket imbalance (lightweight scan after masking)

    The validator is advisory: callers decide whether to treat its findings as
    hard failures (skip the build) or as repair feedback (feed to the
    deterministic repair beam before building).
    """
    if not text:
        return []
    failures: list[VerificationFailure] = []

    # 1. Conflict markers — any marker anywhere is a hard failure, regardless
    # of whether the build passes (markers inside #ifdef can be hidden).
    if contains_markers(text):
        failures.append(VerificationFailure(
            validator="structural", severity="error",
            message="conflict markers remaining in the resolved file",
            detail={"defect": "conflict_markers"},
        ))

    # 2. Brace imbalance.
    brace_line = _brace_imbalance_line(text, language)
    if brace_line is not None:
        failures.append(VerificationFailure(
            validator="structural", severity="error",
            message=f"brace imbalance detected at line {brace_line + 1}",
            detail={"defect": "brace_imbalance", "line": brace_line + 1},
        ))

    # 3. Preprocessor imbalance (C/C++ only).
    if language in ("c", "cpp", "c++"):
        pp_line = _preprocessor_imbalance_line(text)
        if pp_line is not None:
            failures.append(VerificationFailure(
                validator="structural", severity="error",
                message=f"preprocessor directive imbalance at line {pp_line + 1}",
                detail={"defect": "preprocessor_imbalance", "line": pp_line + 1},
            ))

    # 4. Parenthesis/bracket imbalance (lightweight — only for C/C++ where
    # unbalanced parens are common model defects).
    if language in ("c", "cpp", "c++"):
        masked = _mask_strings_and_comments(text, language)
        paren_d = masked.count("(") - masked.count(")")
        bracket_d = masked.count("[") - masked.count("]")
        if paren_d != 0:
            failures.append(VerificationFailure(
                validator="structural", severity="error",
                message=f"parenthesis imbalance: {abs(paren_d)} {'missing' if paren_d > 0 else 'extra'} ')'",
                detail={"defect": "paren_imbalance", "delta": paren_d},
            ))
        if bracket_d != 0:
            failures.append(VerificationFailure(
                validator="structural", severity="error",
                message=f"bracket imbalance: {abs(bracket_d)} {'missing' if bracket_d > 0 else 'extra'} ']'",
                detail={"defect": "bracket_imbalance", "delta": bracket_d},
            ))

    return failures


# ---------------------------------------------------------------------------
# C1 (sprint-22): deterministic missing-symbol repair — pure helpers.
#
# The compiler says exactly which symbol is missing; the merge sides
# contain its declaration (the conflict unit just doesn't include it —
# often hundreds of lines away, invisible to the model). These helpers
# connect the two WITHOUT inventing content: only exact declaration
# lines found in base/current/replayed are injected, verbatim, at the
# language-correct import/declaration point, and the result is re-gated
# by the caller's normal compile authority.
# ---------------------------------------------------------------------------

# (language, pattern) — first match group is the missing symbol. The
# unified cross-language table from the shard evidence: C's undeclared
# identifiers/implicit declarations/unknown types, Rust's
# cannot-find/unresolved-import/unknown-prefix/undeclared-module.
_MISSING_SYMBOL_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("c", re.compile(
        r"['\u2018\u2019]([A-Za-z_]\w*)['\u2018\u2019] (?:does not name a type"
        r"|was not declared|is not a member of)")),
    ("c", re.compile(
        r"['\u2018]([A-Za-z_]\w*)['\u2019] undeclared")),
    ("c", re.compile(
        r"implicit declaration of function ['\u2018]([A-Za-z_]\w*)['\u2019]")),
    ("c", re.compile(
        r"unknown type name ['\u2018]([A-Za-z_]\w*)['\u2019]")),
    ("c", re.compile(
        r"type defaults to 'int' in declaration of ['\u2018]([A-Za-z_]\w*)")),
    # P4b (sprint-24): struct-member pattern — redis-0047's
    # "'struct config' has no member named 'interactive'" → inject
    # the member declaration from the parent that has it
    ("c", re.compile(
        r"'struct (?:\w+)' has no member named '(\w+)'")),
    # P4c (sprint-24): incompatible-pointer errors — extract the
    # function name so C1b can search parents for the correct call
    # (redis-0040: "passing argument 2 of 'output_help' from
    # incompatible pointer type" → the correct call exists in replayed)
    ("c", re.compile(
        r"passing argument \d+ of '([A-Za-z_]\w*)' from incompatible")),
    ("rust", re.compile(
        r"cannot find (?:value|type|macro) `([A-Za-z_]\w*)`")),
    ("rust", re.compile(r"unresolved import `([A-Za-z_][\w:]*)`")),
    ("rust", re.compile(r"prefix `([A-Za-z_]\w*)` is unknown")),
    ("rust", re.compile(
        r"use of undeclared (?:crate or module|type|value) `([A-Za-z_]\w*)`")),
)


def parse_missing_symbols(error_text: str, language: str | None) -> list[str]:
    """Missing symbols named by compiler diagnostics, unique.

    Ordered by first appearance in the diagnostic text (the
    first-reported symbol is fixed first). ``language`` selects which
    signature family matches ("rust" vs the C family for c/cpp/c++);
    None tries both (diagnostics are unambiguous in practice)."""
    lang = (language or "").lower()
    hits: list[tuple[int, str]] = []
    for fam, pat in _MISSING_SYMBOL_PATTERNS:
        if lang and fam != ("rust" if lang == "rust" else "c"):
            continue
        for m in pat.finditer(error_text or ""):
            s = m.group(1).rsplit("::", 1)[-1]
            if s:
                hits.append((m.start(), s))
    hits.sort()
    out: list[str] = []
    for _pos, s in hits:
        if s not in out:
            out.append(s)
    return out[:6]


def find_symbol_declaration_lines(
    symbol: str, language: str | None, *texts: str,
) -> list[str]:
    """Injectable single-line declarations of ``symbol`` in ``texts``.

    v1 scope — only lines that are complete declarations on their own:
      * rust: ``use a::b::Symbol;`` / ``use a::{X, Symbol};`` /
        ``mod symbol;``
      * C:   prototypes (``... symbol(...);``), typedefs
        (``typedef ... symbol;``), forward declarations
        (``struct symbol;``)
    Definitions (``=``, ``{`` bodies) and comment lines are excluded —
    a partial snippet cannot be spliced verbatim. Order preserved
    (first-seen wins; callers pass sides in preference order)."""
    lang = (language or "").lower()
    is_rust = lang == "rust"
    out: list[str] = []
    for text in texts:
        if not text:
            continue
        for raw in text.splitlines():
            s = raw.strip()
            if not s or s.startswith(("//", "*", "/*", "#")):
                if not (not is_rust and s.startswith("#include")):
                    continue
            if s in out:
                continue
            if is_rust:
                if s.startswith("mod ") and s.endswith(";"):
                    m_mod = re.match(r"mod\s+([A-Za-z_]\w*)\s*;$", s)
                    if m_mod and m_mod.group(1) == symbol:
                        out.append(s)
                    continue
                if not (s.startswith("use ") and s.endswith(";")):
                    continue
                if symbol not in re.findall(r"[A-Za-z_]\w*", s):
                    continue
                out.append(s)
            else:
                if "{" in s or "=" in s or not s.endswith(";"):
                    continue
                idents = re.findall(r"[A-Za-z_]\w*", s)
                if symbol not in idents:
                    continue
                # prototype: symbol immediately before '('
                if re.search(rf"\b{re.escape(symbol)}\s*\(", s):
                    out.append(s)
                elif s.startswith("typedef") and idents[-1] == symbol:
                    out.append(s)
                elif re.match(
                        rf"(struct|union|enum)\s+{re.escape(symbol)}\s*;$", s):
                    out.append(s)
                elif not re.search(r"[({]", s) and idents[-1] == symbol:
                    # plain variable declaration: ``Type *name;`` — the
                    # redis-0002 shape (a dropped local's declaration is
                    # injectable verbatim; initializers stay excluded via
                    # the '=' check above).
                    out.append(s)
    return out[:4]


def symbol_injection_point(buffer: str, language: str | None) -> int:
    """0-based line index where an import/declaration line belongs.

    rust: after the file's leading attribute/comment block and the
    LAST contiguous ``use``/``mod`` line near the top. C: after the
    last leading ``#include`` line. Fallback: 0 (top of file)."""
    lang = (language or "").lower()
    lines = (buffer or "").splitlines()
    anchor = 0
    if lang == "rust":
        in_imports = False
        for i, raw in enumerate(lines[:400]):
            s = raw.strip()
            if s.startswith(("use ", "mod ")) and s.endswith(";"):
                anchor = i + 1
                in_imports = True
            elif in_imports and (not s or s.startswith(("//", "#["))):
                # blank/comment INSIDE or right after the import block —
                # keep scanning; a later use block extends the anchor.
                continue
            elif in_imports and s and not s.startswith(("//", "#[")):
                break  # first real item after imports — stop
    else:
        for i, raw in enumerate(lines[:400]):
            s = raw.strip()
            if s.startswith("#include"):
                anchor = i + 1
    return anchor


def inject_symbol_declaration(
    buffer: str, decl_line: str, language: str | None,
) -> str | None:
    """Splice ``decl_line`` at the language-correct point, or None.

    None when the declaration is already present (dedup) or malformed."""
    s = (decl_line or "").strip()
    if not s.endswith(";") or "\n" in s:
        return None
    lines = (buffer or "").splitlines()
    norm = s.replace(" ", "")
    for ln in lines:
        if ln.strip().replace(" ", "") == norm:
            return None  # already imported/declared
    at = symbol_injection_point(buffer, language)
    lines.insert(at, s)
    return "\n".join(lines) + ("\n" if (buffer or "").endswith("\n") else "")


def find_replacement_line(
    buffer: str, error_text: str, language: str | None,
    *parent_texts: str,
) -> tuple[int, str] | None:
    """C1b REPLACE mode: find the corrupted line and its parent replacement.

    For type-default/corrupted-line errors (``type defaults to 'int'``,
    ``expected identifier``), the correct line often exists verbatim in a
    parent side. Returns (buffer_line_index, replacement_line) or None.
    Nothing invented — the replacement must appear verbatim in a parent."""
    import difflib as _dl
    import re as _re_c1b

    lines = buffer.split("\n")
    # Locate the error line (file:line:col or line:N patterns)
    m = _re_c1b.search(r":(\d+):\d+", error_text)
    if not m:
        return None
    err_line = int(m.group(1)) - 1  # 0-based
    if err_line < 0 or err_line >= len(lines):
        return None
    bad = lines[err_line]

    for parent in parent_texts:
        if not parent:
            continue
        p_lines = parent.split("\n")
        # Find the most similar parent line (the bad line's counterpart)
        best = max(
            p_lines,
            key=lambda pl: _dl.SequenceMatcher(
                None, bad.strip(), pl.strip(), autojunk=False).ratio(),
            default=None,
        )
        if best is None:
            continue
        ratio = _dl.SequenceMatcher(
            None, bad.strip(), best.strip(), autojunk=False).ratio()
        if 0.3 < ratio < 1.0 and best.strip() and best.strip() != bad.strip():
            return (err_line, best)
    return None


#: C1b guard (G2, redis-0014): statement headers are not definitions. The
#: definition matcher feeds any ``{``-terminated line containing the symbol
#: here; an ``if ((pid = wait3(&statloc,...)) != 0) {`` header mechanically
#: became ``...);`` — a STATEMENT injected at file scope, itself a syntax
#: error ('expected identifier or ( before if'). Only definition-shaped
#: lines (return-type + identifier + params) may yield prototypes.
_PROTO_CTRL_RE = re.compile(
    r"^\s*(?:}?\s*else\s+)?(if|for|while|switch|do|else|return)\b")


def derive_prototype(definition_line: str) -> str | None:
    """C1b: derive a forward declaration from a definition signature.

    ``static int foo(void) {`` → ``static int foo(void);`` — a mechanical
    transform of verbatim side content (redis-0013's cliSwitchProto).
    Control-flow headers are rejected (statement, not declaration)."""
    s = definition_line.rstrip()
    if not s.endswith("{"):
        return None
    if _PROTO_CTRL_RE.match(s):
        return None
    return s[:-1].rstrip() + ";"


def _split_use_group(canonical: str):
    """Split a canonical use statement into ``(prefix, items)`` when it is a
    flat single-level group (``pub use crate::{a,b}`` → ``("pub use
    crate::", {a, b})``); None otherwise (nested groups, plain paths).

    Items are opaque strings — ``error::*`` and ``a as b`` compare literally,
    which is exactly the binding semantics for same-prefix groups: dropping
    ``{A,B}`` when ``{A,B,error::*}`` exists binds A and B identically (a
    ``*`` item glob-imports its module's contents, not sibling items).
    """
    import re as _re

    m = _re.match(
        r"^((?:pub(?:\([^)]*\))?\s+)?use\s+[A-Za-z_][\w:]*::)\{([^{}]*)\}$",
        canonical)
    if not m:
        return None
    items = frozenset(i.strip() for i in m.group(2).split(",") if i.strip())
    if not items:
        return None
    return m.group(1), items


def _use_stmt_canonical_key(stmt_text: str) -> str | None:
    """Canonical comparison form of a use statement, or None when unsafe.

    Whitespace is collapsed; items inside each brace group are sorted;
    trailing ``;`` and ``,`` are stripped. Two statements with the same key
    bind identical names (module paths included in the key verbatim), so
    removing the later one never changes semantics. Returns None when the
    statement has unbalanced/unusually-nested braces — the caller then
    skips dedup for it (conservative).
    """
    import re as _re

    t = _re.sub(r"\s+", " ", stmt_text).strip().rstrip(";").strip()
    if t.count("{") != t.count("}"):
        return None

    def _sort_group(m: "_re.Match[str]") -> str:
        items = [i.strip().rstrip(",") for i in m.group(1).split(",") if i.strip()]
        return "{" + ",".join(sorted(items)) + "}"

    # Innermost-first for nested groups: repeatedly sort brace-free groups.
    # Braces remain in the canonical form (they are use syntax) — the loop
    # just needs to reach a fixed point.
    for _ in range(3):
        new = _re.sub(r"\{([^{}]*)\}", _sort_group, t)
        if new == t:
            break
        t = new
    return t


def _dedup_rust_use_statements(text: str) -> str | None:
    """R2 (sprint-22) + P1c (sprint-24): remove duplicate ``use`` statements.

    Union-merged re-export lists can carry the same ``use`` line twice —
    rustc rejects each duplicate with "the name `X` is defined multiple
    times" (sea-orm-0021: 17 errors from three duplicated re-exports).
    Removing the duplicate never changes semantics. Scope-aware: dedup
    only within the same indentation level (a ``use`` inside a function
    body is a different scope from the top-level one).

    The sweep works over LOGICAL statements (multi-line groups end at
    their closing ``;``) with canonical-form comparison:

    - ``pub use`` lines (the original sweep only matched ``use `` — the
      sea-orm re-export class was invisible to it);
    - order/whitespace-normalized keys (``{b, c}`` ≡ ``{c , b}`` ≡ a
      multi-line group);
    - SUBSET collapse (cycle-G): when two same-(indent, attrs, prefix)
      groups overlap, the smaller is absorbed — every name it binds is
      bound by the larger, in BOTH drop directions. The sea-orm cascade
      merges produce union groups plus residual side fragments (subset
      overlap, not exact duplicates); exact-only matching left the
      "defined multiple times" errors standing.
    - attribute context (``#[cfg(...)]``) is part of the identity: the
      sea-orm oracle has two ``pub use crate::{...}`` groups distinguished
      ONLY by cfg feature — cfg-distinct groups never collide.

    Returns the deduped text, or None when nothing changed.
    """
    if not text:
        return None
    lines = text.split("\n")
    # Parse into logical statements: (start, end_exclusive, attrs_start,
    # indent, attrs, canonical, split) — split is (prefix, items) for flat
    # groups, None otherwise.
    stmts: list[dict] = []
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        s = ln.strip()
        if not s.startswith(("use ", "pub use ", "pub(crate) use ")):
            i += 1
            continue
        attrs_start = i
        while attrs_start > 0 and lines[attrs_start - 1].strip().startswith("#["):
            attrs_start -= 1
        stmt_lines = [ln]
        depth = ln.count("{") - ln.count("}")
        j = i
        while (depth > 0 or not stmt_lines[-1].rstrip().endswith(";")) and j + 1 < n:
            j += 1
            stmt_lines.append(lines[j])
            depth += lines[j].count("{") - lines[j].count("}")
            if depth < 0:
                break  # malformed — treat as non-dedupable
        stmt = "\n".join(stmt_lines)
        canonical = _use_stmt_canonical_key(stmt) if depth == 0 else None
        split = _split_use_group(canonical) if canonical else None
        stmts.append({
            "attrs_start": attrs_start, "start": i, "end": j + 1,
            "indent": len(ln) - len(ln.lstrip()),
            "attrs": tuple(l.strip() for l in lines[attrs_start:i]),
            "canonical": canonical, "split": split,
        })
        i = j + 1

    # Decide keeps: exact duplicates, then subset absorption (both
    # directions) among same-(indent, attrs, prefix) flat groups.
    keep = [True] * len(stmts)
    removed = 0
    seen_exact: set[str] = set()
    kept_groups: list[tuple[int, tuple, str, frozenset, int]] = []
    for idx, st in enumerate(stmts):
        if st["canonical"] is None:
            continue
        key = f"{st['indent']}:{st['attrs']}:{st['canonical']}"
        if key in seen_exact:
            keep[idx] = False
            removed += 1
            continue
        seen_exact.add(key)
        if st["split"] is None:
            continue
        _indent, _attrs, _prefix, _items = (
            st["indent"], st["attrs"], st["split"][0], st["split"][1])
        absorbed = False
        for g_idx, g_indent, g_attrs, g_prefix, g_items in kept_groups:
            if (g_idx != idx and _indent == g_indent and _attrs == g_attrs
                    and _prefix == g_prefix):
                if g_items >= _items:
                    # An earlier kept group already binds everything —
                    # drop this one.
                    keep[idx] = False
                    removed += 1
                    absorbed = True
                    break
                if _items > g_items:
                    # This group absorbs the earlier smaller one.
                    keep[g_idx] = False
                    removed += 1
        if absorbed:
            continue
        kept_groups.append((idx, _indent, _attrs, _prefix, _items))
        # Drop absorbed groups from kept_groups (their keep flag is False).
        kept_groups = [g for g in kept_groups if keep[g[0]]]

    if not removed:
        return None
    drop_ranges = set()
    for idx, st in enumerate(stmts):
        if not keep[idx]:
            drop_ranges.update(range(st["attrs_start"], st["end"]))
    out = [l for k, l in enumerate(lines) if k not in drop_ranges]
    return "\n".join(out)


def _try_repair_string_literal(
    text: str, language: str | None = None
) -> str | None:
    """Sprint-22 pre-eval item 2: repair an unterminated quote literal.

    A splice can leave a line with an odd count of unescaped single or
    double quotes — the classic 'missing terminating ' character' error
    (protobuf-0034's exposed defect). Conservative: only when exactly
    ONE line in the whole file is unbalanced, and only by appending the
    missing terminator to that line (never removing or editing content
    — the model's text is preserved, just closed).

    Parity runs on the MASKED line (strings/comments hidden): an
    apostrophe inside a comment ("the virtual machine's program" —
    sqlite-0113/0118) is not a literal delimiter, but raw parity counted
    it, the repair appended a stray ' AFTER the comment, and that quote
    then poisoned string-masking for the rest of the file — every brace
    below became invisible and the coherence gate reported a phantom
    "missing closing brace" on a balanced, oracle-equal splice. A
    genuinely unterminated literal in code stays visible in the masked
    view (the masker only masks what it can close), so the true case is
    preserved.

    ``language`` gates the Rust lifetime exemption: ``'a`` in Rust is a
    lifetime, not an unterminated char literal (counting it made a
    5-lifetime signature line "unbalanced" and the repair appended a
    stray quote after a ``{``). In C/C++ the same shape IS a broken
    char literal (``char c = 'a;``) and must count.
    """
    if not text:
        return None
    _rust = (language or "").lower() in ("rust",)
    # Masked parity for c-family AND rust (both maskers are
    # language-aware and handle char-literal-with-quote correctly: rust's
    # `'"'` masks as a unit — raw parity counted its inner double-quote
    # and the repair appended a stray `"` to PRISTINE axum-0019 sides).
    # The generic masker (language=None) treats a broken char literal as
    # a string and MASKS it — hiding the defect the repair exists to fix
    # (test_unterminated_char_literal_fixed); python prose has no char
    # literals to repair anyway.
    _use_masked = (language or "").lower() in (
        "c", "cpp", "c++", "rust", "python")
    _masked_lines = (
        _mask_strings_and_comments(text, language).split("\n")
        if _use_masked else text.split("\n"))

    def _quote_parity(line: str) -> tuple[int, int]:
        """(singles, doubles) count of unescaped quotes.

        Lifetime-aware (rust only): a ``'`` that starts an identifier
        run with NO closing ``'`` within a char-literal's width (≤4
        chars) is a Rust lifetime (``'a``, ``'b``, ``'_``), not a
        literal delimiter.
        """
        singles = doubles = 0
        escaped = False
        i = 0
        n = len(line)
        while i < n:
            ch = line[i]
            if escaped:
                escaped = False
                i += 1
                continue
            if ch == "\\":
                escaped = True
                i += 1
                continue
            if ch == "'" and _rust:
                nxt = line[i + 1] if i + 1 < n else ""
                if nxt and (nxt.isalnum() or nxt == "_"):
                    # `'a` — char literal only when a closing `'` follows
                    # within char-literal width; otherwise a lifetime.
                    closer = line.find("'", i + 2, i + 6)
                    if closer != -1:
                        singles += 2  # the pair
                        i = closer + 1
                        continue
                    i += 1  # lifetime — not a delimiter
                    continue
                singles += 1
            elif ch == "'":
                singles += 1
            elif ch == '"':
                doubles += 1
            i += 1
        return singles, doubles

    bad_lines = []
    for i, line in enumerate(_masked_lines):
        s, d = _quote_parity(line)
        if s % 2 == 1 or d % 2 == 1:
            bad_lines.append((i, s, d))
    if len(bad_lines) != 1:
        return None  # multiple or zero — not a single-line fix
    i, s, d = bad_lines[0]
    lines = text.split("\n")
    if s % 2 == 1:
        lines[i] = lines[i] + "'"
    elif d % 2 == 1:
        lines[i] = lines[i] + '"'
    else:
        return None
    # verify: no remaining unbalanced lines. RAW parity here — the
    # masker's own char-literal handling is asymmetric on the repaired
    # shape ('a;' masks unevenly), so masked parity would false-fail the
    # just-repaired line.
    for line in lines:
        s2, d2 = _quote_parity(line)
        if s2 % 2 == 1 or d2 % 2 == 1:
            return None
    return "\n".join(lines)


def _delimiter_imbalance_line(
    text: str, language: str | None,
) -> tuple[int, str] | None:
    """(0-based line, char) of the first unmatched ) or ], or None.

    String/comment stripped via _strip_strings_comments (same
    foundations as the brace checker); parens/brackets stack-walked
    together so nesting mismatches surface."""
    cleaned = _strip_strings_comments(text, language)
    stack: list[tuple[int, int, str]] = []
    _pairs = {"(": ")", "[": "]", ")": "(", "]": "["}
    for li, line in enumerate(cleaned):
        for ci, ch in enumerate(line):
            if ch in "([":
                stack.append((li, ci, ch))
            elif ch in ")]":
                _expected_open = _pairs[ch]  # ")" -> "(", "]" -> "["
                if not stack or stack[-1][2] != _expected_open:
                    return (li, ci)
                stack.pop()
    return None


def _try_repair_delimiter(
    text: str, language: str | None,
) -> str | None:
    """Single-edit repair for one unmatched ) or ].

    An unmatched close means either a stray closer (delete it) or a
    missing opener earlier on the line. Conservative: only the
    stray-closer deletion (the zenodo-0085 shape: a portfolio splice
    left one ')' too many); anything needing an invented opener
    declines."""
    imb = _delimiter_imbalance_line(text, language)
    if imb is None:
        return None
    li, ci = imb
    lines = text.split("\n")
    if li >= len(lines):
        return None
    line = lines[li]
    if ci >= len(line):
        return None
    # delete the stray closer at the reported column
    candidate = line[:ci] + line[ci + 1:]
    lines[li] = candidate
    out = "\n".join(lines)
    if _delimiter_imbalance_line(out, language) is None:
        return out
    return None


def _try_balance_braces_iterated(
    text: str, language: str | None = None, *,
    max_rounds: int = 3,
) -> str | None:
    """Iterated single-imbalance repair for multi-gap failures.

    The single-shot rung declines anything needing more than one edit;
    multi-unclosed-brace failures (sqlite-0019: 2, sqlite-0029: 4) got
    zero deterministic attempts. Each round applies the one-edit repair
    and re-checks; the first non-improving round returns what we have
    (only if fully balanced) or None.

    P6 (sprint-23 batch E): 3-round convergence stop. If the imbalance
    location KEEPS MOVING after 3 repairs, the structure is fundamentally
    corrupted (e.g., a missing `}` for an entire impl block). Deterministic
    patching cannot safely guess the correct structure beyond 3 attempts.
    Escalation is the honest outcome."""
    current = text
    last_imbalance = None
    for round_n in range(max_rounds):
        imb = _brace_imbalance_line(current, language)
        if imb is None:
            return current if current != text else None
        # P6: if the imbalance is at the SAME line as the previous round,
        # the repair isn't converging — stop (fundamental corruption)
        if last_imbalance is not None and imb == last_imbalance:
            break
        last_imbalance = imb
        repaired = _try_balance_braces(current, language)
        if repaired is None or repaired == current:
            break
        current = repaired
    if current != text and _brace_imbalance_line(current, language) is None:
        # P6/cycle B (sprint-24): sanity-check the result — balanced braces
        # don't guarantee valid syntax (sqlite-0019: the inserted brace
        # created "expected identifier before 'if'" despite balancing).
        # A quick heuristic: the result shouldn't have syntax-keyword lines
        # at brace-depth 0 that don't look like top-level constructs.
        if language in ("c", "cpp", "c++"):
            for i, line in enumerate(current.split("\n")):
                stripped = line.strip()
                # A line starting with 'if' or 'for' at depth 0 (no
                # enclosing brace) is a syntax error in C (statements
                # can't appear at file scope)
                if (stripped.startswith(("if ", "if(", "for ", "for(",
                                          "while ", "while(", "return"))
                        and i > 0):
                    # Check if we're at file scope (no enclosing brace)
                    depth = sum(current[:current.index(line)].count("{")
                                - current[:current.index(line)].count("}"))
                    if depth == 0:
                        return None  # bad insertion — revert
        return current
    return None


def delimiter_failure_shape(messages: list[str]) -> str | None:
    """Classify failure messages into P6b's splice-repair shapes.

    Returns ``"delim"`` (unmatched paren/bracket), ``"brace"`` (mismatched
    closing delimiter / brace imbalance / unmatched brace), or ``None``.
    Single source for both the candidate-level P6b check and the
    whole-file repair beam rung (s27-extend-21: they were two copies of
    the same string heuristics).
    """
    for m in messages:
        if ("unmatched '" in m and ("}" not in m) and (")" in m or "]" in m)):
            return "delim"
    for m in messages:
        if ("mismatched closing delimiter" in m
                or "brace imbalance detected" in m
                or ("unmatched '" in m and "}" in m)):
            return "brace"
    return None


def splice_level_delimiter_repair(
    original_worktree_text: str,
    marker_span: tuple[int, int],
    resolved_text: str,
    messages: list[str],
    language: str | None,
) -> tuple[str, str] | None:
    """P6b's splice-level delimiter/brace surgery — the single implementation.

    Splices ``resolved_text`` into the unit's worktree text, repairs the
    delimiter/brace imbalance on the SPLICED WHOLE FILE (position-correct,
    unlike a whole-buffer balancer whose append lands at EOF), remaps the
    marker span through the line diff (the brace repair may delete lines),
    and extracts the repaired REGION back out so the result stays
    splice-safe for any caller.

    Returns ``(repaired_region, form)`` with form in ``{"delim", "brace"}``,
    or None when the messages aren't a repairable shape / the repair didn't
    change anything / the extracted region is empty.

    Used by BOTH the candidate-level P6b check (unit validation failures)
    and the whole-file repair beam rung (whole-file gate failures) —
    s27-extend-21 closed the wiring gap where whole-file-gate failures with
    the same message shape never reached this surgery.
    """
    shape = delimiter_failure_shape(messages)
    if shape is None or not resolved_text.strip() or marker_span is None:
        return None
    from capybase.adapters.parsers import splice_resolution

    try:
        spliced = splice_resolution(
            original_worktree_text, marker_span, resolved_text)
    except Exception:  # noqa: BLE001 — splice on bad spans is caller's error
        return None
    repaired = None
    if shape == "delim":
        if _delimiter_imbalance_line(spliced, language) is not None:
            repaired = _try_repair_delimiter(spliced, language)
            if (repaired is not None
                    and _delimiter_imbalance_line(repaired, language) is not None):
                repaired = None
    if repaired is None:  # brace shape, or the delim repair declined
        braced = _try_balance_braces(spliced, language)
        if braced is not None and braced != spliced:
            repaired = braced
            shape = "brace"
    if repaired is None:
        return None
    # Remap the marker span through the line diff (the brace repair may
    # have deleted lines; the paren repair is single-char, indices never
    # move but the remap is harmless for it).
    start, end = marker_span
    sp_lines = repaired.split("\n")
    orig_lines = spliced.split("\n")
    del_before = del_inside = 0
    if len(orig_lines) != len(sp_lines):
        import difflib as _dl

        sm = _dl.SequenceMatcher(None, orig_lines, sp_lines, autojunk=False)
        for tag, i1, i2, _j1, _j2 in sm.get_opcodes():
            if tag == "delete":
                if i2 <= start:
                    del_before += i2 - i1
                elif i1 < end + 1:
                    del_inside += min(i2, end + 1) - max(i1, start)
    rs = start - del_before
    re_ = end - del_before - del_inside
    region = "\n".join(sp_lines[rs:re_ + 1])
    if not region.strip():
        return None
    return region, shape


def _try_balance_braces(text: str, language: str | None = None) -> str | None:
    """Deterministically repair a single brace imbalance, or return None.

    The live eval exposed a recurring failure: the model merges each hunk of a
    multi-hunk conflict correctly *in isolation*, but the spliced result has an
    extra or missing ``}`` at the hunk junction. Even with repair feedback the
    model reproduces the same error across 4 retries — it can't *see* the
    junction because it only ever sees one unit at a time. This function is the
    cheap deterministic fallback: when the imbalance is a single stray brace
    (one edit away from balanced), fix it directly and skip the LLM call.

    Conservative by design — it acts ONLY when a single edit fully balances the
    text, and never on lines where a brace shares space with code (e.g.
    ``} else {``), where a blind removal would corrupt structure. Two cases:

    * **depth goes negative** (extra ``}``): the line where depth first dips
      below 0 carries a stray close. If that line is a *brace-only* line
      (whitespace + ``}``, possibly a trailing comment), drop it. Otherwise
      return None — the error is structural, not a dropped stray brace.
    * **depth ends positive** (unclosed ``{``): append the deficit of ``}`` to
      the end of the last non-blank, non-brace-only line. This closes a
      truncated construct (the common splice-junction case where the model's
      hunk ends mid-block).

    Re-checks after the edit; returns None if still unbalanced so the caller
    falls through to the LLM repair path. Strings/comments are masked first so
    a literal brace inside a string isn't counted or touched.
    """
    if not text:
        return None
    lines = text.split("\n")
    cleaned = _strip_strings_comments(text, language)

    # Walk depth to classify the imbalance (extra-close vs unclosed-open).
    # Walk the WHOLE text (don't break at the first negative) so the final
    # depth reflects the true deficit — duplicate stray braces (the splice-
    # junction case) drive depth to -2 or lower, and we need to remove all of
    # them, not just the first.
    depth = 0
    neg_line: int | None = None
    for line_no, cline in enumerate(cleaned):
        for ch in cline:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth < 0 and neg_line is None:
                    neg_line = line_no
        # Don't break — keep walking to accumulate the full deficit.

    if neg_line is not None:
        # Extra closing brace(s): depth dipped below zero. The stray closes are
        # the trailing brace-only lines at/after the divergence point — the common
        # splice-junction case where the model's hunk carries duplicate closers.
        # Collect ALL consecutive brace-only lines from the divergence point
        # onward (each is "whitespace + }"), removing only enough to bring depth
        # back to zero. This handles both a single stray and the duplicate-pair
        # case (two extra ``}`` from a duplicated hunk boundary). Conservative:
        # if the divergence line carries real code (not brace-only), bail — the
        # error is structural, not a dropped stray.
        to_remove: list[int] = []
        deficit = abs(depth) if depth < 0 else 1  # how many stray } to drop
        for i in range(neg_line, len(lines)):
            if deficit <= 0:
                break
            raw = lines[i]
            c = _strip_strings_comments(raw, language)[0] if i < len(cleaned) else raw
            if c.strip() == "}":
                to_remove.append(i)
                deficit -= 1
            elif c.strip() == "":
                continue  # skip blank lines between stray braces
            else:
                break  # hit real code → stop collecting stray braces
        if not to_remove or deficit > 0:
            # Fallback: the extra } may be glued to the end of a code line
            # (e.g. ``return foo();}``) rather than on its own brace-only line.
            # Try removing ONE trailing } from the divergence line itself.
            # Conservative: only the last } on the line, only when deficit==1,
            # only when the non-brace content ends with a statement terminator
            # (``;`` or ``:``) — meaning the } is trailing junk after a
            # complete statement, NOT sharing the line with an expression like
            # ``bar() }`` where removal would corrupt structure. The result is
            # always re-validated.
            if deficit == 1 and neg_line < len(lines):
                raw = lines[neg_line]
                cleaned_neg = cleaned[neg_line] if neg_line < len(cleaned) else raw
                _non_brace = cleaned_neg.replace("}", "").replace("{", "").strip()
                _terminates = bool(_non_brace) and _non_brace[-1] in (";", ":")
                last_brace = raw.rfind("}")
                if last_brace >= 0 and _terminates:
                    patched = raw[:last_brace] + raw[last_brace + 1:]
                    candidate = list(lines)
                    candidate[neg_line] = patched
                    result = "\n".join(candidate)
                    if _brace_imbalance_line(result, language) is None:
                        return result
            # Sprint-21 (a) — the 0034/0049 class: the stray '}' sits on a
            # code line WITHOUT a statement terminator (e.g. a bare
            # ``foo()}`` call or a glued close after an expression). Scan
            # FORWARD from the divergence line for the first line that IS
            # brace-terminated junk: content + trailing '}'s where removing
            # the LAST '}' leaves a line whose remaining content is a
            # complete token sequence (ends with ')', ';', identifier, or is
            # empty after brace-stripping with other code lines around it).
            # One edit, deficit==1, always re-validated.
            if deficit == 1:
                for j in range(neg_line, min(neg_line + 25, len(lines))):
                    raw = lines[j]
                    c = cleaned[j] if j < len(cleaned) else raw
                    if "}" not in c:
                        continue
                    stripped = c.replace("}", "")
                    # ')' is deliberately EXCLUDED: 'bar() }' is a legal
                    # one-liner block close, not junk (guard-pinned by
                    # test_balance_braces_code_line_not_touched).
                    _ok_tail = (not stripped.strip()
                                or stripped.rstrip().endswith(
                                    (";", ",", ":")))
                    if not _ok_tail:
                        continue
                    last_brace = raw.rfind("}")
                    patched = raw[:last_brace] + raw[last_brace + 1:]
                    candidate = list(lines)
                    candidate[j] = patched
                    result = "\n".join(candidate)
                    if _brace_imbalance_line(result, language) is None:
                        return result
                    break  # first candidate only — conservative
            return None  # couldn't collect enough stray brace-only lines
        candidate = [l for i, l in enumerate(lines) if i not in set(to_remove)]
        result = "\n".join(candidate)
        if _brace_imbalance_line(result, language) is None:
            return result
        return None

    if depth > 0:
        # Unclosed opening brace(s): insert the deficit of ``}`` closers.
        # The placement matters: for a file ending with structural closers
        # (``};`` for a class, ``}`` for namespaces), inserting at the very
        # last content line (which is often the ``};``) puts the closers in
        # the WRONG scope (after the class/namespace closers, not before).
        # This produces a brace-balanced file that fails the gcc build because
        # the closers are in the wrong scope.
        #
        # Strategy: try multiple insertion points, best-first:
        #  1. Before trailing brace-only lines: walk backward past trailing
        #     ``}``-only and blank lines, then find the last content line
        #     BEFORE those structural closers. This places closers inside
        #     the correct scope (after the function body, before class/ns
        #     closers). This is the nlohmann-0033 case.
        #  2. After the last content line (the original strategy): catches
        #     the case where the file doesn't end with structural closers.
        suffix = "}" * depth

        # Candidate 0 (sprint-20 S20.7): insert at the next SIBLING construct
        # boundary. The fmt-0003 shape: a construct's closer is lost MID-FILE
        # (e.g. a TEST-macro block) and the file continues with sibling
        # constructs and trailing structural closers — EOF or trailing-closer
        # insertion lands in the wrong scope (brace-balanced, gcc-broken).
        # Signal: after the innermost unclosed opener every depth reading is
        # +depth too high, so a sibling that truly starts at scope depth 0
        # reads depth-BEFORE == depth. Guards: the line must look like a
        # construct start (a call/signature followed by '{'), sit at or left
        # of the opener's indentation (same scope — body content indents
        # deeper), and the result must re-validate balanced.
        if depth == 1:
            dbefore: list[int] = []
            _d = 0
            for cline in cleaned:
                dbefore.append(_d)
                _d += cline.count("{") - cline.count("}")
            stack: list[int] = []
            for i, cline in enumerate(cleaned):
                for _ch_i, ch in enumerate(cline):
                    if ch == "{":
                        stack.append(i)
                    elif ch == "}" and stack:
                        stack.pop()
            if stack:
                opener = stack[-1]
                # ``cleaned`` (strings/comments stripped) can have a different
                # line count than ``lines`` on multi-line-literal inputs —
                # every cross-index access is length-guarded (sqlite-0113:
                # IndexError when dbefore ran short).
                _n_lines = min(len(lines), len(cleaned), len(dbefore))
                if opener >= _n_lines:
                    opener = _n_lines - 1 if _n_lines > 0 else 0
                    opener_indent = 0
                else:
                    opener_indent = len(lines[opener]) - len(lines[opener].lstrip())
                for i in range(opener + 1, _n_lines):
                    c = cleaned[i].strip()
                    if (not c or c == "}" or c.startswith(("#", "//"))
                            or dbefore[i] != depth or "{" not in c
                            or "(" not in c):
                        continue
                    indent = len(lines[i]) - len(lines[i].lstrip())
                    if indent > opener_indent:
                        continue  # body content (deeper) — not a sibling
                    candidate_0 = lines[:i] + [suffix] + lines[i:]
                    result_0 = "\n".join(candidate_0)
                    if _brace_imbalance_line(result_0, language) is None:
                        return result_0
                    break  # first sibling only — fall through to 1/2

        # Candidate 1: before trailing brace-only lines.
        # Walk backward past pure-``}`` and blank lines to find the
        # last content line before the structural closers. Also skip lines
        # that are structural closers with trailing punctuation (``};``,
        # ``})``, ``},``) — these close class/struct/namespace scopes and
        # should NOT be treated as "content".
        _trailing_start = len(lines)
        for i in range(len(cleaned) - 1, -1, -1):
            c = cleaned[i].strip()
            if c == "" or c == "}" or c in ("};", "})", "},", "})/", "}}"):
                continue
            # Found the last content line before trailing braces.
            _trailing_start = i + 1
            break
        if _trailing_start < len(lines):
            # There ARE trailing brace-only lines — insert before them.
            candidate_1 = lines[:_trailing_start] + [suffix] + lines[_trailing_start:]
            result_1 = "\n".join(candidate_1)
            if _brace_imbalance_line(result_1, language) is None:
                return result_1

        # Candidate 2: after the last content line (original strategy).
        insert_at = len(lines)
        for i in range(len(cleaned) - 1, -1, -1):
            content = cleaned[i].replace("{", "").replace("}", "").strip()
            if content:
                insert_at = i + 1
                break
        candidate_2 = lines[:insert_at] + [suffix] + lines[insert_at:]
        result_2 = "\n".join(candidate_2)
        if _brace_imbalance_line(result_2, language) is None:
            return result_2
        return None

    # Already balanced.
    return None


def _try_balance_preprocessor(text: str) -> str | None:
    r"""Deterministically repair a single C preprocessor ``#if/#endif`` imbalance.

    The entity-splitting + splice pipeline can produce a whole-file
    ``#endif`` imbalance that no single sub-unit owns — e.g. when a conflict
    region is a mid-file slice that opens an ``#if`` without its ``#endif``
    (the matching directive sits upstream of the marker block). The coherence
    gate (:func:`_preprocessor_imbalance_line`) flags it; this function is the
    cheap deterministic fallback before the LLM repair path. Mirrors
    :func:`_try_balance_braces` for the preprocessor case.

    Conservative by design — acts ONLY when a single edit fully balances, and
    re-validates with :func:`_preprocessor_imbalance_line` before returning.
    Two cases:

    * **depth goes negative** (extra ``#endif``): the line where depth first
      dips below 0 carries a stray close. If that line is a *directive-only*
      line (whitespace + ``#endif``, possibly a trailing comment), drop it.
      Otherwise return None — removing code would corrupt structure.
    * **depth ends positive** (unclosed ``#if``): append the deficit of
      ``#endif`` at the end of the text. This closes a truncated construct
      (the slice case where the region's ``#if`` was left open).

    Returns ``None`` when the imbalance is ambiguous (the error is structural,
    e.g. a directive that shares a line with code, or multiple separate
    stray directives the PDA can't unambiguously resolve) so the caller falls
    through to the LLM repair path. Strings/comments are masked first so a
    directive inside a string isn't counted or touched.
    """
    if not text:
        return None
    # Reuse the PDA to classify the imbalance, but walk the masked physical
    # lines directly (not the logical-line view) so edits map to real lines.
    masked = _mask_strings_and_comments(text, "c")
    physical = masked.split("\n")

    # Walk depth to find the divergence line and the final deficit. Walk the
    # WHOLE text (don't break at the first negative) so the final depth reflects
    # the true deficit — the same rationale as _try_balance_braces.
    depth = 0
    neg_line: int | None = None
    for line_no, line in enumerate(physical):
        if _PP_OPEN_RE.match(line):
            depth += 1
        elif _PP_CLOSE_RE.match(line):
            depth -= 1
            if depth < 0 and neg_line is None:
                neg_line = line_no
        # #else/#elif without #if is an error the PDA flags, but it's not
        # fixable by a single add/remove — defer to the LLM.

    lines = text.split("\n")

    if neg_line is not None:
        # Truncated-slice guard: if the negative-depth #endif is at or near the
        # END of the file (only blanks/whitespace after it), the text is a mid-
        # file slice missing the content that would have rebalanced the count.
        # Removing this trailing #endif balances the depth count but is
        # semantically WRONG — the real fix is the missing content the model
        # must generate (observed on sqlite-0040: the current side had 143
        # directives vs the oracle's 240). Defer to the LLM path rather than
        # producing a depth-balanced-but-wrong file. A stray #endif with real
        # content (code or directives) after it IS the removable case.
        has_content_after = any(
            physical[j].strip() != ""
            for j in range(neg_line + 1, len(physical))
        )
        if not has_content_after:
            return None  # truncated slice → defer to the model
        # Extra #endif(s): depth dipped below zero. Collect ALL consecutive
        # directive-only #endif lines from the divergence point onward,
        # removing only enough to bring depth back to zero. Conservative: if
        # the divergence line carries real code (not a bare #endif), bail.
        to_remove: list[int] = []
        deficit = abs(depth) if depth < 0 else 1  # how many stray #endif to drop
        i = neg_line
        while i < len(lines) and deficit > 0:
            raw = lines[i]
            m = _mask_strings_and_comments(raw, "c")
            stripped = m.strip()
            # A bare #endif line: optional leading ws, #endif, optional comment.
            if _PP_CLOSE_RE.match(stripped) or _PP_CLOSE_RE.match(raw.strip()):
                to_remove.append(i)
                deficit -= 1
            elif stripped == "":
                i += 1  # skip blank lines between stray directives
                continue
            else:
                break  # hit real code → stop collecting stray directives
            i += 1
        if not to_remove or deficit > 0:
            return None  # couldn't collect enough bare #endif lines
        candidate = [l for j, l in enumerate(lines) if j not in set(to_remove)]
        result = "\n".join(candidate)
        if _preprocessor_imbalance_line(result) is None:
            return result
        return None

    if depth > 0:
        # Unclosed #if(s). Sprint-21 (b) — positional insertion, the mirror
        # of the sibling-brace logic: appending at EOF closes the construct
        # in the WRONG scope when the #if opened mid-file and same-scope
        # content follows (sqlite-0040: the EOF append produced a depth-
        # balanced file that regressed sim to 0.015). Insert the deficit
        # #endif BEFORE the next same-scope directive block — the first
        # #if/#else after the point where depth returns to the opener's
        # level — falling back to EOF when no such boundary exists.
        suffix_lines = ["#endif"] * depth
        insert_at = len(lines)
        _d3 = 0
        for i, line in enumerate(physical):
            if _PP_OPEN_RE.match(line):
                _d3 += 1
            elif _PP_CLOSE_RE.match(line):
                _d3 -= 1
                if _d3 == depth - 1:
                    # depth returned to the opener's level: the next
                    # same-level directive is the sibling boundary
                    for j in range(i + 1, len(physical)):
                        if (_PP_OPEN_RE.match(physical[j])
                                or _PP_ELSE_RE.match(physical[j])):
                            insert_at = j
                            break
                    if insert_at != len(lines):
                        break
        candidate = lines[:insert_at] + suffix_lines + lines[insert_at:]
        result = "\n".join(candidate)
        if _preprocessor_imbalance_line(result) is None:
            return result
        return None

    # Already balanced.
    return None


class _StandaloneSyntaxValidator:
    """Base for per-unit standalone-compile syntax validators (CEGIS hardening).

    The three language validators (Rust / C-C++ / Python) share the same 6-step
    skeleton: language guard → marker-span guard → empty-text guard → splice +
    blank sibling markers → (optional) brace-balance guard → resolve tool →
    compile in try/except → (optional) resolution-error filter → result. This
    base centralizes that control flow; each subclass overrides the language-
    specific hooks. Previously each validator copied the boilerplate verbatim
    (~70 lines each), drifting on exception handling and result construction.

    Hooks a subclass MUST override:
      ``_languages``   — the set of languages this validator handles.
      ``_feature_key`` — the features-dict key (e.g. ``"rust_syntax_checked"``).

    Hooks a subclass MAY override:
      ``_skip_before_compile(ctx)``  — extra pre-compile skip (C's header-skip).
      ``_check_braces``              — whether to run the brace-balance guard.
      ``_resolve_compiler(cfg)``     — tool path or None (None → skip).
      ``_compile(spliced, cfg)``     — the (ok, msg) compile call.
      ``_is_resolution_error(msg)``  — filter for context-dependent errors.
    """

    _languages: tuple[str, ...] = ()
    _feature_key: str = ""
    _check_braces: bool = False

    def _skip_before_compile(self, ctx: VerificationContext) -> VerificationCheckResult | None:
        return None

    def _resolve_compiler(self, cfg: object) -> str | None:
        raise NotImplementedError

    def _compile(self, spliced: str, tool: str, cfg: object) -> tuple[bool, str]:
        raise NotImplementedError

    def _is_resolution_error(self, msg: str) -> bool:
        return False

    def verify(self, ctx: VerificationContext) -> VerificationCheckResult:
        fk = self._feature_key
        if ctx.unit.language not in self._languages:
            return VerificationCheckResult(
                name=self.name, passed=True,
                message=f"not {self._lang_label()}; syntax check skipped",
                features={fk: False},
            )
        skip = self._skip_before_compile(ctx)
        if skip is not None:
            return skip
        if not ctx.candidate.resolved_text:
            return VerificationCheckResult(
                name=self.name, passed=True,
                message="empty resolved_text; syntax check skipped",
                features={fk: False},
            )
        if ctx.unit.marker_span is None:
            # Whole-file unit (marker_span None): the resolved_text IS the
            # file. A blanket pass here let a model answer a whole-file
            # prompt with BLOCK-interior content — sqlite-0029's candidate
            # began with `if( pTab->tabFlags & TF_HasNotNull ){` at file
            # scope, skipped unit validation entirely, and only the file-
            # level build caught it (too late for a cheap retry). Compile
            # the raw text through the same pipeline: parse errors (the
            # wrong-shape signature) fail; standalone-unresolvable errors
            # still defer via _is_resolution_error.
            spliced = _blank_markers(
                ctx.candidate.resolved_text, ctx.unit.language)
        else:
            spliced = splice_resolution(
                ctx.unit.original_worktree_text,
                ctx.unit.marker_span,
                ctx.candidate.resolved_text,
            )
            spliced = _blank_markers(spliced, ctx.unit.language)
        if self._check_braces and not _braces_balanced(spliced, ctx.unit.language):
            return VerificationCheckResult(
                name=self.name, passed=True,
                message="spliced text has unbalanced braces; deferring to whole-file check",
                features={fk: False, "syntax_passed": True},
            )
        tool = self._resolve_compiler(ctx.config)
        if tool is None:
            return VerificationCheckResult(
                name=self.name, passed=True,
                message=f"{self._lang_label()} compiler not available; syntax not checked",
                features={fk: False, "syntax_passed": True},
            )
        try:
            ok, msg = self._compile(spliced, tool, ctx.config)
        except FileNotFoundError:
            return VerificationCheckResult(
                name=self.name, passed=True,
                message=f"{self._lang_label()} compiler vanished; syntax not checked",
                features={fk: False, "syntax_passed": True},
            )
        except Exception as exc:  # noqa: BLE001 - never crash resolution
            return VerificationCheckResult(
                name=self.name, passed=True,
                message=f"{self._lang_label()} syntax check error: {exc}",
                features={fk: False, "syntax_passed": True},
            )
        if not ok and self._is_resolution_error(msg):
            return VerificationCheckResult(
                name=self.name, passed=True,
                message=(
                    f"{self._lang_label()} standalone-compile showed resolution/type "
                    f"errors (not a syntax defect); deferring to whole-file check"
                ),
                features={fk: True, "syntax_passed": True},
            )
        return VerificationCheckResult(
            name=self.name,
            passed=ok,
            severity="error",
            message=msg,
            detail={"diagnostic": msg},
            features={fk: True, "syntax_passed": ok},
        )

    def _lang_label(self) -> str:
        return self.name.replace("_syntax", "")


class RustSyntaxValidator(_StandaloneSyntaxValidator):
    """Per-unit Rust syntax check (CEGIS loop hardening).

    Rust syntax errors that slip past the structural validators — a malformed
    ``format!`` macro, a stray brace — never reached the per-unit CEGIS loop:
    they only surfaced in Phase B (``verify_file``), which runs AFTER all units
    resolve. A candidate with a Rust syntax error could be accepted per-unit and
    rejected only later, or (worse) rejected by the critic for an unrelated
    ``unattributed_code`` warning with the syntax error never fed back as a
    diagnostic.

    This validator splices the candidate into the worktree, BLANKS sibling
    conflict markers to comments (so the spliced file parses even in a multi-
    hunk file — the same technique ``AstPreservationValidator`` uses), and runs
    ``_compile_rust`` (``rustc --emit=metadata``). The diagnostic becomes a
    ``VerificationFailure`` that seeds the repair prompt — the model sees the
    exact compile error on the FIRST retry, not a vague unrelated warning.

    Rust-only; no-op (passes) for other languages. Skips when ``rustc``/``cargo``
    is absent or the marker span is unknown. Runs as a hard failure
    (``severity="error"``) so it's retryable like the Python syntax check.
    """

    name = "rust_syntax"
    _languages = ("rust",)
    _feature_key = "rust_syntax_checked"
    _check_braces = True

    def _resolve_compiler(self, cfg: object) -> str | None:
        return _resolve_tool(getattr(cfg, "rustc_path", "rustc"))

    def _compile(self, spliced: str, tool: str, cfg: object) -> tuple[bool, str]:
        edition = getattr(cfg, "rust_edition", "") or "2021"
        return _compile_rust(spliced, rustc_path=tool, edition=edition)

    def _is_resolution_error(self, msg: str) -> bool:
        return _is_rust_resolution_error(msg)


class CcsSyntaxValidator(_StandaloneSyntaxValidator):
    """Per-unit C/C++ syntax check (CEGIS loop hardening).

    The C/C++ analog of ``RustSyntaxValidator``: catches parse-level syntax
    errors (a stray brace, a missing semicolon, an unterminated string) in the
    CEGIS loop so a malformed candidate is fed the exact compile error on the
    FIRST retry — not deferred vaguely to Phase B. Splices the candidate into
    the worktree, blanks sibling conflict markers to ``//`` comments (so the
    spliced TU parses even in a multi-hunk file), and runs
    ``_compile_ccs`` (``gcc``/``g++ -fsyntax-only``).

    C/C++-only (``c``/``cpp``/``c++``); no-op (passes) for other languages.
    Skips header files (never compiled standalone) and when the compiler is
    absent or the marker span is unknown. Defer to Phase B when braces are
    unbalanced. Semantic errors (undeclared identifiers, type mismatches) are
    filtered via ``_is_ccs_resolution_error`` and deferred to the whole-file
    check, mirroring how Rust defers ``E0xxx`` codes to cargo.
    """

    name = "ccs_syntax"
    _languages = ("c", "cpp", "c++")
    _feature_key = "ccs_syntax_checked"
    _check_braces = True

    def _skip_before_compile(self, ctx: VerificationContext) -> VerificationCheckResult | None:
        # Headers CAN be syntax-checked with -fsyntax-only. The semantic-error
        # filter (_is_ccs_resolution_error) defers "unknown type name" etc.
        # (artifacts of standalone compilation without sibling headers) as
        # non-resolution errors. Only genuine parse errors (missing brace,
        # stray punctuation) surface as hard failures. Previously headers were
        # skipped unconditionally — now we attempt validation and rely on the
        # filter + include paths to suppress false positives.
        return None

    def _resolve_compiler(self, cfg: object) -> str | None:
        is_cpp = self._is_cpp  # set in _compile via the ctx language
        cc_default = "g++" if is_cpp else "gcc"
        return _resolve_tool(getattr(cfg, "cxx_path" if is_cpp else "cc_path", cc_default))

    @property
    def _is_cpp(self) -> bool:
        # Resolved per-call in verify via the language; default False (C).
        return getattr(self, "_lang_is_cpp", False)

    def verify(self, ctx: VerificationContext) -> VerificationCheckResult:
        # Stash whether this unit is C++ so _resolve_compiler picks g++/cpp_std.
        self._lang_is_cpp = ctx.unit.language in ("cpp", "c++")
        self._unit_path = ctx.unit.path or ""
        # Source-portfolio candidates copy one side verbatim. "Undeclared
        # identifier" errors in these candidates are REAL — the side's code
        # references variables that don't exist in the merged context (the
        # surrounding code is from the base/common ancestor, not the side's
        # branch). Don't filter them as standalone-compilation false positives.
        self._strict_semantic = (
            getattr(ctx.candidate, "model_name", "") == "source_portfolio"
        )
        return super().verify(ctx)

    def _compile(self, spliced: str, tool: str, cfg: object) -> tuple[bool, str]:
        is_cpp = self._is_cpp
        std = getattr(cfg, "cpp_std" if is_cpp else "c_std", "c++17" if is_cpp else "c11")
        # Use the header suffix when the unit is a header file — gcc infers
        # the language from the suffix, and header suffixes produce more
        # accurate diagnostics than .c/.cpp for header content.
        _path = getattr(self, "_unit_path", "") or ""
        _is_header = _path.endswith((".h", ".hpp", ".hh", ".hxx", ".H"))
        if _is_header:
            suffix = ".hpp" if is_cpp else ".h"
            timeout = 10.0  # headers should compile fast; avoid blocking on complex includes
        else:
            suffix = ".cpp" if is_cpp else ".c"
            timeout = 30.0
        # Include paths: the per-unit Phase A gate doesn't have access to the
        # repo root (only Phase B verify_file does). Headers compile without
        # include paths — the semantic-error filter defers "unknown type name"
        # as non-resolution errors. Only genuine parse errors surface as hard
        # failures, which is the primary goal. Include-path resolution for
        # headers is a future improvement (requires threading repo_root
        # through VerificationContext).
        return _compile_ccs(
            spliced, cc_path=tool, std=std, suffix=suffix,
            timeout=timeout,
        )

    def _is_resolution_error(self, msg: str) -> bool:
        if getattr(self, "_strict_semantic", False):
            # Source-portfolio strict mode: only filter errors that are
            # genuine standalone-compilation false positives (missing
            # project headers, unknown project-internal types). DO filter
            # these so a valid portfolio candidate isn't rejected just
            # because standalone gcc can't see sibling headers.
            for pattern in (
                "unknown type name",      # project typedef in sibling header
                "does not name a type",   # same (clang wording)
                "fatal error",            # missing #include
                "no such file or directory",
                "incomplete type",        # forward-declared type
                "no matching function",   # overload resolution (needs full TU)
                "suggest an alternative", # clang "did you mean?"
            ):
                if pattern in msg:
                    return True
            # "undeclared identifier", "was not declared", "has not been
            # declared", "cannot convert", "is not a member of", "no member
            # named" → DON'T filter. For source_portfolio these are real
            # errors (the side's code doesn't fit the merged context).
            return False
        return _is_ccs_resolution_error(msg)


class PythonSyntaxValidator(_StandaloneSyntaxValidator):
    """Per-unit Python syntax check (CEGIS loop hardening).

    Python syntax errors (an unclosed bracket, a bad indent) would otherwise be
    invisible to the per-unit CEGIS loop — they only surfaced in Phase B
    (``verify_file``), which runs AFTER all units resolve. A candidate with a
    Python syntax error could be accepted per-unit and rejected only later,
    with the model never seeing the diagnostic as targeted repair feedback.

    This validator splices the candidate into the worktree, BLANKS sibling
    conflict markers to comments (multi-unit-safe, same technique as
    ``AstPreservationValidator``), and runs ``_compile_python`` (``py_compile``).
    Python has no separate type-resolution phase, so a fragment compiles cleanly
    in isolation when it's syntactically valid — no crate-context problem, and
    no resolution-error filter is needed (unlike Rust/C). The diagnostic becomes
    a ``VerificationFailure`` that seeds PROMPT_REPAIR.

    Python-only; no-op (passes) for other languages. Skips when the marker span
    is unknown or resolved_text is empty. Runs as a hard failure
    (``severity="error"``) so it's retryable.
    """

    name = "python_syntax"
    _languages = ("python",)
    _feature_key = "python_syntax_checked"

    def _resolve_compiler(self, cfg: object) -> str | None:
        # py_compile is always available (stdlib); return a sentinel so the base
        # class's "tool not available" skip never fires.
        return "python3"

    def _compile(self, spliced: str, tool: str, cfg: object) -> tuple[bool, str]:
        return _compile_python(spliced)


class AstPreservationValidator:
    """Prove that AST nodes OUTSIDE the conflict span survive the splice.

    The line-level ``ExactSpliceScopeValidator`` only guards that splicing
    doesn't touch lines beyond the marker block. But a model can still rewrite
    unchanged code *within* the visible window (e.g. collapse two statements,
    delete a comment) as long as the line count matches — a regression
    invisible to line checks. This validator parses the original and the
    spliced-resolved file with the abstract parser, computes the node-type fingerprint
    of every node OUTSIDE the conflict span, and rejects the candidate if they
    differ.

    Inert when the structural parser is unavailable, or when the
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
                message="ast preservation skipped (parser unavailable)",
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
        # marker blocks. Those raw markers corrupt the abstract parse and
        # produce a false AST-preservation failure. Blank them to comments first
        # (same approach as the LSP baseline) so the parse reflects real structure.
        spliced = splice_resolution(
            unit.original_worktree_text, unit.marker_span, ctx.candidate.resolved_text
        )
        spliced = _blank_markers(spliced, lang)
        # Recompute the post-splice span: the resolved text occupies
        # [start, start + n_resolved_lines - 1], NOT the original marker_span.
        # The original span is offsets into the PRE-splice worktree; after
        # splicing, the line count shifts (a 5-line marker block resolved to 1
        # line shifts every subsequent line up by 4). Reusing the original span
        # classified the wrong lines as inside/outside → a sibling node that was
        # OUTSIDE the original span became INSIDE the shifted one and was dropped
        # from the outside fingerprint, producing a spurious mismatch (false FAIL
        # the model could never satisfy).
        resolved_lines = ctx.candidate.resolved_text.split("\n")
        spliced_span = (
            unit.marker_span[0],
            unit.marker_span[0] + max(0, len(resolved_lines) - 1),
        )
        after_outside, _ = structural.fingerprint_region(
            spliced, lang, spliced_span
        )
        if after_outside is None:
            return VerificationCheckResult(
                name=self.name,
                passed=True,
                message="ast preservation skipped (post-splice parse failed)",
                features={"ast_checked": False, "ast_preserved": True},
            )
        preserved = after_outside == base_outside
        # Complementary injection guard: the outside fingerprint (line-range
        # partitioned with the recomputed span) catches edits to sibling nodes,
        # but a resolution that INJECTS a new top-level entity within its own
        # line range lands "inside" the span and is excluded from the outside
        # digest. Compare the full top-level identity sequence (kind:name) of
        # the blanked worktree vs the spliced result so an injected top-level
        # def/class/struct is caught regardless of where its lines fall.
        if preserved:
            base_ids = _top_level_identities(
                _blank_markers(unit.original_worktree_text, lang), lang
            )
            after_ids = _top_level_identities(spliced, lang)
            if base_ids is not None and after_ids is not None and base_ids != after_ids:
                preserved = False
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


def _has_whole_file_span(
    resolutions: list[tuple[tuple[int, int] | None, str]]
) -> bool:
    """True iff ``resolutions`` carries a whole-file unit (``marker_span`` None).

    A modify/delete unit has no marker span — its resolved text IS the file.
    ``splice_all_resolutions`` cannot represent that (it unpacks each span),
    so the caller routes whole-file units around splicing and uses the
    resolved text directly. A single such unit is the only supported shape;
    mixing it with marker spans would be ambiguous and is treated as
    whole-file here (the first resolution wins).
    """
    return any(span is None for span, _ in resolutions)


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


def _py_compile_errors(source: str) -> list[str]:
    """The list of py_compile error messages (one per diagnostic line).

    Unlike :func:`_compile_python` (which returns only the LAST error line for
    the syntax floor), this returns every ``<file>:<line>: <msg>`` line so a
    diagnostic DELTA (#7) can distinguish a NEW error from a pre-existing one.
    Empty when the source compiles. Used by the no-worse-than-before delta for
    Python: the merge is rejected only when it introduces a syntax error the
    blanked baseline didn't have, not for a pre-existing one in the conflict.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(source)
        tmp_path = tf.name
    try:
        proc = subprocess.run(
            ["python3", "-m", "py_compile", tmp_path],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            return []
        # py_compile emits lines like '  File "...", line N' + 'SyntaxError: ...'.
        # Keep the diagnostic-bearing lines (the SyntaxError/IndentationError/etc.
        # messages), stripping the temp-file path prefix for a stable delta key.
        errs: list[str] = []
        for ln in (proc.stderr or "").splitlines():
            s = ln.strip()
            if s and (s.startswith(tmp_path) or "Error" in s or "Warning" in s):
                # Normalize the temp path out so the message is path-independent.
                errs.append(s.replace(tmp_path, "<file>"))
        return errs or [(proc.stderr or "py_compile failed").strip()]
    finally:
        Path(tmp_path).unlink(missing_ok=True)


#: Rust error codes whose message text DRIFTS between a marker-blanked baseline
#: and a spliced candidate (the splice shifts line numbers / paths, so the same
#: crate-path resolution error renders with a slightly different message). For
#: these codes only, the diagnostic delta keys on the CODE alone (not the
#: message) so a pre-existing E0432 in baseline suppresses an E0432 in the
#: candidate. Other codes (E0425 'cannot find value', E0308 'mismatched types')
#: are NOT drift-tolerant — a candidate E0425 for a different symbol than the
#: baseline's is genuinely new and must NOT be suppressed.
_DRIFT_TOLERANT_CODES = frozenset({"E0432", "E0433"})


def compute_diagnostic_delta(
    baseline_errors, after_errors, *, suppress_codes: set[str] | None = None
) -> list[str]:
    """The errors in ``after`` that were NOT in ``baseline`` (#7).

    The shared no-worse-than-before primitive: every diagnostic check that can
    delta-compare (LSP, cargo, py_compile) reduces to a message-set difference —
    "what errors does the candidate introduce that the blanked-baseline didn't
    already have?". This centralizes that set-difference so the four independent
    helpers stop re-implementing it and a unified ``introduced_diagnostics``
    feature can be derived consistently.

    Args:
        baseline_errors / after_errors: either ``list[str]`` (message strings,
            the legacy form) or ``list[Diagnostic]`` (the structured form). When
            Diagnostics are passed AND carry a ``.code``, the delta keys on the
            CODE so a pre-existing error class (e.g. E0432 "unresolved import")
            suppresses a candidate error of the SAME code even if the message
            text drifted (e.g. a slightly different unresolved path introduced by
            the splice). This prevents a near-correct Rust merge from being
            rejected for crate-path errors that need the full dependency tree
            (a recurring false-positive class for fragment-level rustc).
        suppress_codes: optional set of error codes to drop ENTIRELY from the
            result, even if they're genuinely new. Used for the standalone-rustc
            / no-full-crate context where E0432/E0433 (crate-path resolution)
            are undecidable — the live config sets ``rust_suppress_codes =
            ["E0432", "E0433"]`` to tolerate them.

    Returns the new error messages (order preserved, deduplicated).
    """
    suppress_codes = suppress_codes or set()

    def _key_and_msg(item):
        """Return (dedup_key, display_message, code) for a str or Diagnostic.

        The key is message-based by default (a merge that moves a pre-existing
        error to a new line is not 'new', but a genuinely new message is). For
        codes in ``_DRIFT_TOLERANT_CODES`` (crate-path resolution errors whose
        message text drifts between baseline and candidate because the splice
        shifts line numbers / paths), the key is the CODE alone — so an E0432 in
        baseline suppresses an E0432 in the candidate even if the message text
        differs. This is narrowly scoped: other codes (E0425 'cannot find value',
        E0308 'mismatched types', etc.) are NOT drift-tolerant — a candidate
        E0425 for a DIFFERENT symbol than the baseline's E0425 is genuinely new.
        """
        # Diagnostic-shaped: has .message and .code attributes.
        msg = getattr(item, "message", None)
        if msg is not None:
            code = (getattr(item, "code", "") or "").strip()
            msg_s = str(msg).strip()
            # Drift-tolerant codes: key on code alone (crate-path resolution).
            if code in _DRIFT_TOLERANT_CODES:
                return code, msg_s, code
            # All others: key on message (the existing behavior).
            return msg_s, msg_s, code
        # Bare string.
        msg_s = str(item).strip()
        return msg_s, msg_s, ""

    baseline_keys: set = set()
    for item in baseline_errors:
        k, _m, _c = _key_and_msg(item)
        if k:
            baseline_keys.add(k)
    seen: set[str] = set()
    new_errors: list[str] = []
    for item in after_errors:
        k, msg, code = _key_and_msg(item)
        if not k:
            continue
        if code in suppress_codes:
            continue  # config-suppressed code (e.g. E0432 standalone)
        if k in baseline_keys or k in seen:
            continue
        seen.add(k)
        new_errors.append(msg)
    return new_errors


def reduce_cascade_errors(errors: list[str], *, max_root_causes: int = 3) -> list[str]:
    """Reduce a list of compiler errors to root causes by suppressing cascades.

    When a candidate has one root error (e.g. a missing import), the compiler
    produces many cascaded errors (every use of the missing symbol). Sending
    all of them to a weak model overwhelms it with noise. This function groups
    errors by their error code prefix (e.g. ``E0432``, ``E0599``) and keeps
    only the first error per code — the root cause. Additional distinct codes
    are kept up to ``max_root_causes``.

    This is a lightweight heuristic: it assumes errors with the same code in
    the same compilation are likely cascades from the same root cause. This is
    correct for most Rust compilation failures (a missing import cascades to
    E0425/E0433 for every use; a type mismatch cascades to E0308 for every
    call site).
    """
    if len(errors) <= max_root_causes:
        return errors
    # Extract error codes from the error messages (e.g. "E0432: ...").
    import re
    code_re = re.compile(r"\b(E\d{4})\b")
    seen_codes: set[str] = set()
    root_causes: list[str] = []
    other: list[str] = []
    for err in errors:
        m = code_re.search(err)
        code = m.group(1) if m else None
        if code and code not in seen_codes:
            seen_codes.add(code)
            root_causes.append(err)
        elif not code:
            # No error code — keep it (might be a distinct root cause).
            other.append(err)
        # else: same code as an already-seen root cause → cascade, skip.
    # Combine: root causes first (one per code), then unclassified errors.
    result = root_causes + other
    return result[:max_root_causes] if len(result) > max_root_causes else result


def _append_diagnostic_failure(
    new_errors: list[str],
    hard: list[VerificationFailure],
    config: "ValidationConfig",
    *,
    validator: str,
    message_prefix: str,
    tool: str,
    extra_detail: dict | None = None,
    require_gate: bool = True,
) -> None:
    """Build a ``VerificationFailure`` from a diagnostic delta and append it.

    Consolidates the three formerly-duplicated blocks in
    ``_run_cargo_syntax_check`` / ``_run_cargo_manifest_check`` /
    ``_run_lsp_diagnostics`` that each: reduced the cascade, truncated to 3
    messages for the summary, and built the same ``detail`` shape. Computes
    ``reduce_cascade_errors`` ONCE (the originals recomputed it twice).

    No-op when ``new_errors`` is empty, or (when ``require_gate``) when
    ``config.require_syntax_if_supported`` is False — matching the per-call-site
    guards. NOT used by ``_run_clippy_check`` (which diverges structurally:
    dynamic severity, ``findings`` detail key, routes through
    ``VerificationCheckResult``).
    """
    if not new_errors:
        return
    if require_gate and not config.require_syntax_if_supported:
        return
    reduced = reduce_cascade_errors(new_errors)
    msg = "; ".join(m[:80] for m in reduced[:3])
    detail: dict = {"new_errors": reduced[:5], "tool": tool}
    if extra_detail:
        detail.update(extra_detail)
    hard.append(
        VerificationFailure(
            validator=validator,
            severity="error",
            message=f"{message_prefix}: {len(new_errors)} new error(s): {msg}",
            detail=detail,
        )
    )


# ---------------------------------------------------------------------------
# Whole-file semantic checks (Python, stdlib ast): duplicate definitions and
# unreachable code. These are the two "looks plausible, passes line/token
# validators" failure shapes a small model produces (concatenate both sides'
# blocks → duplicate class; stack two returns → unreachable). Tree-sitter
# (structural.duplicate_definitions) covers Rust; stdlib ast covers Python AND
# catches bare module-level assignments (``FEATURE_FLAGS = ...``) that
# enumerate_entities intentionally skips. Both degrade to [] on any parse error
# (a syntax failure is the syntax check's job to report, not theirs).
# ---------------------------------------------------------------------------

# Coarse node-type → kind label, mirroring structural._KIND_BY_NODE_TYPE so the
# message vocabulary ("class"/"function"/"variable") is consistent across
# Python and Rust findings.
_PY_DEF_KIND = {
    ast.ClassDef: "class",
    ast.FunctionDef: "function",
    ast.AsyncFunctionDef: "function",
}


def _py_duplicate_definitions(source: str) -> list[tuple[str, str, list[int]]] | None:
    """Per-scope duplicate definitions in a Python module (stdlib ast).

    Returns ``(kind, name, line_numbers)`` tuples — one per name defined more
    than once within the SAME scope (module, class body, or function body).
    ``ClassDef``/``FunctionDef``/``AsyncFunctionDef`` collide on their kind;
    bare-name assignments (``X = ...``, ``X: T = ...``) collide as ``variable``
    so a duplicated ``FEATURE_FLAGS = {...}`` is caught (the abstract parser's
    enumerate_entities misses these). A function shadowed by a same-named class is NOT a collision
    (different kind) — that's a legitimate (if odd) redefinition.

    Returns ``None`` on SyntaxError/ValueError so the caller can record
    ``checked=False`` (couldn't analyze) — distinct from ``[]`` (parsed fine,
    no duplicates). The syntax check owns reporting the parse failure itself.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None

    findings: list[tuple[str, str, list[int]]] = []

    def _names_assigned(stmt: ast.stmt) -> list[str]:
        """Bare ``Name`` targets of an Assign/AnnAssign (module/class-level)."""
        targets: list[ast.expr] = []
        if isinstance(stmt, ast.Assign):
            targets = list(stmt.targets)
        elif isinstance(stmt, ast.AnnAssign) and stmt.target is not None:
            targets = [stmt.target]
        out = []
        for t in targets:
            if isinstance(t, ast.Name):
                out.append(t.id)
            # Tuple/multi-target unpacking (``a = b = 1`` or ``a, b = ...``) is
            # rare for top-level config; skip rather than over-match.
        return out

    def _scan_scope(body: list[ast.stmt]):
        seen: dict[tuple[str, str], list[int]] = {}
        for stmt in body:
            kind = _PY_DEF_KIND.get(type(stmt))
            name = getattr(stmt, "name", None)
            if kind and name:
                seen.setdefault((kind, name), []).append(stmt.lineno)
                # Recurse into the def's own body (methods, nested classes).
                _scan_scope(getattr(stmt, "body", []))
                continue
            # Bare assignment: record as "variable" in THIS scope.
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                for nm in _names_assigned(stmt):
                    seen.setdefault(("variable", nm), []).append(stmt.lineno)
            # Function/class bodies are only entered via the def branches above;
            # control-flow blocks (if/for/with) introduce a new scope in Python
            # only for comprehensions, not for ``if`` bodies — but a duplicate
            # inside an ``if`` is conditional, so we don't recurse there.
        for key, rows in seen.items():
            if len(rows) > 1:
                findings.append((key[0], key[1], sorted(rows)))

    _scan_scope(tree.body)
    return findings


# Statement nodes that unconditionally terminate control flow at their scope.
_PY_TERMINATORS = (ast.Return, ast.Raise, ast.Break, ast.Continue)
# Trailing statements after a terminator that carry no executable weight —
# a Pass, a bare docstring expression, or an ellipsis — must NOT trip the check.
_PY_TRIVIAL_AFTER_TERMINATOR = (ast.Pass,)


def _py_unreachable_code(source: str) -> list[tuple[str, str, int]] | None:
    """Statements unreachable due to an earlier unconditional terminator.

    Returns ``(funcname, terminator_kind, line)`` triples — one per
    non-trivial statement that follows a ``return``/``raise``/``break``/
    ``continue`` at the same block level inside a function/method body.
    Module-level code is not scanned (a top-level ``return`` is itself a
    SyntaxError). Recurses into nested functions and the bodies of
    compound statements (if/for/while/with/try) so a terminator buried in a
    branch is still detected, but only flags SIBLINGS after the terminator,
    not the terminator's own nested block.

    Skips trivial trailing nodes (``pass``, docstrings, ``...``) to avoid
    false positives on idiomatic ``return`` then ``pass`` stubs. Returns
    ``None`` on SyntaxError/ValueError (couldn't analyze — distinct from
    ``[]``, which means no unreachable code was found).
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None

    findings: list[tuple[str, str, int]] = []

    def _check_body(body: list[ast.stmt], owner: str):
        terminated = False
        term_kind = ""
        for stmt in body:
            if terminated:
                if _is_trivial_after_terminator(stmt):
                    continue
                findings.append((owner, term_kind, stmt.lineno))
                continue
            if isinstance(stmt, _PY_TERMINATORS):
                terminated = True
                term_kind = type(stmt).__name__.lower()
            # Descend into compound statements so nested terminators (and
            # unreachable code after them) are found, regardless of whether
            # THIS statement terminates.
            _descend(stmt, owner)

    def _descend(stmt: ast.stmt, owner: str):
        """Recurse into any nested function/compound body, keeping ``owner``."""
        # A nested def gets its own owner name (so the message is precise).
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _check_body(stmt.body, stmt.name)
            return
        for attr in ("body", "orelse", "finalbody", "handlers"):
            val = getattr(stmt, attr, None)
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, ast.stmt):
                        _descend(item, owner)
            elif isinstance(val, ast.ExceptHandler):
                _descend(val, owner)

    def _is_trivial_after_terminator(stmt: ast.stmt) -> bool:
        if isinstance(stmt, _PY_TRIVIAL_AFTER_TERMINATOR):
            return True
        # A bare docstring or ``...`` expression-statement.
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            return True
        return False

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _check_body(node.body, node.name)

    return findings


def _compile_rust(
    source: str, *, rustc_path: str = "rustc", edition: str = "2021"
) -> tuple[bool, str]:
    """Syntax/parse-check Rust source via ``rustc --emit=metadata``.

    The ``py_compile`` analog for Rust: writes the source to a temp ``.rs`` file
    and asks ``rustc`` to emit *only* metadata (``--emit=metadata``), which runs
    parsing + macro expansion + name resolution far enough to catch syntax and
    obvious semantic errors WITHOUT producing an object file or needing a
    ``Cargo.toml``. Compiled as ``--crate-type lib`` so a fragment with top-level
    items type-checks. Returns ``(True, "rustc ok")`` on success or
    ``(False, first_error_line)`` on failure — the first ``error``-prefixed line
    of stderr is the actionable diagnostic the CEGIS repair loop wants, more
    useful than rustc's trailing "aborting due to N previous errors".

    Any invocation failure (missing binary, crash) maps to
    ``(False, message)``; the caller gates hard-rejection on the tool actually
    being available (``_resolve``), so a missing ``rustc`` is reported as
    "not checked" rather than a false syntax failure.
    """
    # E2 (sprint-23): include_str!/include_bytes! resolve relative to the
    # ORIGINAL file's directory; this temp-copy compile cannot see them
    # (axum-0005/0033: include_str'd docs read as /tmp/../docs/... — a false
    # syntax failure from EVERY caller: unit validator, whole-file gate,
    # repair loops). Undecidable at this location: report not-checked.
    if re.search(r"include_(?:str|bytes)!\s*\(", source):
        return True, ("rustc temp-copy: include_str/include_bytes "
                      "undecidable from this location; not checked")
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".rs", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(source)
        tmp_path = tf.name
    # Emit metadata to a temp path alongside the source (rustc needs write
    # access to the output dir; a throwaway path in the same tempdir is safe).
    out_path = tmp_path + ".rmeta"
    try:
        proc = subprocess.run(
            [
                rustc_path,
                "--edition",
                edition,
                "--emit=metadata",
                "--crate-type",
                "lib",
                tmp_path,
                "-o",
                out_path,
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return True, "rustc ok"
        err = (proc.stderr or "").strip()
        if not err:
            return False, "rustc failed"
        # Prefer the first real diagnostic line (starts with "error"); it names
        # the actual problem. Fall back to the last non-empty line.
        for line in err.splitlines():
            if line.startswith("error"):
                return False, line
        return False, err.splitlines()[-1]
    except FileNotFoundError:
        # rustc absent — caller treats this as "not checked", not a failure.
        raise
    finally:
        Path(tmp_path).unlink(missing_ok=True)
        Path(out_path).unlink(missing_ok=True)


def _compile_ccs(
    source: str, *, cc_path: str = "gcc", std: str = "c11", suffix: str = ".c",
    timeout: float = 30.0,
    include_paths: list[str] | None = None,
) -> tuple[bool, str]:
    """Syntax/parse-check C/C++ source via ``gcc``/``clang`` ``-fsyntax-only``.

    The ``_compile_rust`` analog for C/C++: writes the source to a temp file and
    asks the compiler to run parsing + semantic analysis WITHOUT producing an
    object file (``-fsyntax-only`` runs the front end only, no codegen, no link).
    ``-std=`` selects the language standard. Returns ``(True, "cc ok")`` on
    success or ``(False, first_error_line)`` on failure.

    ``include_paths`` (when provided) adds ``-I`` flags so gcc can resolve
    project-internal headers (``sqliteInt.h``, ``server.h``) that define the
    types a header file under resolution references. Without these, standalone
    gcc reports "unknown type name 'u8'" for any project-internal typedef —
    a false positive that escalates correct header merges (research §5:
    anchored local context). Only adds search paths; never removes the ability
    to detect real errors.

    Unlike ``rustc`` (whose error lines start with ``error``), gcc/clang prefix
    diagnostics with ``file:line:col:``, so the first line CONTAINING
    ``" error:"`` is the actionable diagnostic the CEGIS repair loop wants.

    A 30s timeout bounds runaway compiles (``-fsyntax-only`` on a single TU
    should be subsecond; 30s is generous). Any invocation failure (missing
    binary, crash, timeout) maps to ``(False, message)``; ``FileNotFoundError``
    is re-raised so the caller can gate hard-rejection on the tool actually
    being available (``_resolve``), keeping a missing compiler a "not checked"
    non-failure rather than a false syntax failure.

    Headers (``.h``/``.hpp``) compile standalone under ``-fsyntax-only``
    (declarations-only are valid translation units), so no ``.c`` driver wrapper
    is needed.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, delete=False, encoding="utf-8"
    ) as tf:
        tf.write(source)
        tmp_path = tf.name
    try:
        cmd = [cc_path, "-fsyntax-only", f"-std={std}"]
        # Add include search paths so header files can resolve sibling includes
        # (e.g. #include "sqliteInt.h" defining u8, BtCursor). Each path becomes
        # a -I flag. Paths that don't exist are silently skipped by gcc, so no
        # validation needed here.
        if include_paths:
            for ip in include_paths:
                cmd.append(f"-I{ip}")
        cmd.append(tmp_path)
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode == 0:
            return True, "cc ok"
        err = (proc.stderr or "").strip()
        if not err:
            return False, "cc failed"
        # gcc/clang format: ``file:line:col: error: msg``. Find the first line
        # carrying a real ``error:`` (a ``warning:`` or caret line isn't it).
        # Fall back to the first non-empty line (e.g. ``gcc: error: ...`` for a
        # bad flag, which has no file prefix).
        for line in err.splitlines():
            if " error:" in line or line.startswith("error"):
                return False, line
        return False, err.splitlines()[0]
    except FileNotFoundError:
        # compiler absent — caller treats this as "not checked", not a failure.
        raise
    except subprocess.TimeoutExpired:
        return False, f"cc timed out after {timeout:g}s"
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# ccache: transparent compiler cache for C/C++ builds
# ---------------------------------------------------------------------------

_ccache_available: bool | None = None


# ---------------------------------------------------------------------------
# Session build state (sprint-19 P3 / D1): one doomed full build per session.
# ---------------------------------------------------------------------------

#: Recoverable failure signatures — a retry at 2× the cap can succeed where
#: the same-cap retry cannot (the failure burns wall clock, not capability).
_RECOVERABLE_BUILD_KINDS = {
    "lock_contention": (
        "waiting for file lock", "blocking waiting for file lock",
        "failed to lock", "another ninja instance", "lock file exists",
    ),
    "compiler_crash": (
        "internal compiler error", "clang: error: unable to execute command",
        "please submit a full bug report", "compiler crashed",
    ),
    "network_transient": (
        "could not resolve host", "failed to fetch", "connection reset",
        "connection refused", "temporary failure in name resolution",
    ),
}


def _classify_build_failure_kind(output: str) -> str:
    """Classify a build failure's recoverability from its output.

    Returns one of ``_RECOVERABLE_BUILD_KINDS``'s keys or ``"generic"``.
    A generic timeout is zero-information at the same cap (the cap is the
    binding constraint, not the tree), so the session degrades to
    syntax-only rather than re-paying it; recoverable kinds get ONE
    retry at double the cap (sprint-19 P3, R1+R2 resolution).
    """
    text = (output or "").lower()
    for kind, patterns in _RECOVERABLE_BUILD_KINDS.items():
        if any(p in text for p in patterns):
            return kind
    return "generic"


class BuildStateTracker:
    """Session-scoped full-build economics (the P3 build state machine).

    ``FULL_BUILD_AVAILABLE → SYNTAX_ONLY``: after the first generic
    full-build timeout, subsequent full builds are skipped in favor of
    the syntax-only fallback — re-running a build that just timed out at
    the same cap adds zero information while burning the case budget
    (protobuf-0067: ~1020 of 1337s went to four sequential doomed
    full-tree builds). Recoverable failures (lock contention, compiler
    crash, network weather) get one retry at 2× the cap before the
    degradation fires. Every probe and transition is journaled through
    the attached event sink (``build_probe`` / ``build_state``).
    """

    def __init__(self, event_sink=None) -> None:
        self.full_build_available = True
        self.timeout_count = 0
        self.recoverable_retry_count = 0
        self.degrade_reason = ""
        self._event_sink = event_sink

    def emit(self, event: str, payload: dict) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink(event, payload)
        except Exception:  # noqa: BLE001 — journaling must never break builds
            pass

    def record_probe(self, cmd: str, duration_s: float, outcome: str,
                     **extra) -> None:
        # None-valued extras (e.g. no stderr captured on a passing probe)
        # are dropped so the payload stays clean.
        extra = {k: v for k, v in extra.items() if v is not None}
        self.emit("build_probe", {
            "cmd": (cmd or "")[:200],
            "duration_s": round(duration_s, 1),
            "outcome": outcome,
            **extra,
        })

    def note_recoverable_retry(self, kind: str, cmd: str,
                               prior_cap: float) -> None:
        self.recoverable_retry_count += 1
        self.emit("build_retry", {
            "kind": kind, "prior_cap_s": prior_cap,
            "new_cap_s": prior_cap * 2,
            "cmd": (cmd or "")[:200],
        })

    def note_timeout(self, kind: str, cmd: str, cap: float) -> None:
        self.timeout_count += 1
        # Degrade on a generic timeout, or on ANY kind once the
        # recoverable retry was spent (the state machine's "second
        # timeout → SYNTAX_ONLY" transition).
        if self.full_build_available and (
                kind == "generic" or self.recoverable_retry_count > 0):
            self.full_build_available = False
            self.degrade_reason = (
                f"full build timed out ({cap:g}s cap) — session degrades to "
                f"syntax-only for further full builds"
            )
            self.emit("build_state", {
                "state": "SYNTAX_ONLY",
                "reason": self.degrade_reason,
                "cmd": (cmd or "")[:200],
                "timeout_count": self.timeout_count,
            })


def _ccache_enabled() -> bool:
    """True when ccache is installed and should be used.

    Cached after first check. ccache speeds up repeated builds by serving
    unchanged translation units from cache — critical for the CEGIS repair
    loop where verify_file recompiles the same tree with a one-line change.
    """
    global _ccache_available
    if _ccache_available is None:
        _ccache_available = shutil.which("ccache") is not None
    return _ccache_available


def _ccache_env(shim_dir: Path | None = None) -> dict[str, str]:
    """Build an environment dict with ccache wired in, or the base env if absent.

    A PATH shim (gcc/g++ -> ``ccache <absolute-compiler>``) routes both
    $(CC)-style and hardcoded compiler calls through the cache. The shim
    MUST reference the compiler by absolute path: ccache resolves a bare
    name through PATH, finds its own shim, marks the call uncacheable,
    then "falls back" to executing that shim — re-entering ccache in an
    infinite loop (observed live: 995/995 uncacheable calls and compile
    trees spinning for hours inside deleted worktrees). CC/CXX are
    deliberately NOT set to ``ccache gcc``/``ccache g++``: the double
    wrap re-triggers the same loop, and the PATH shim alone already
    covers every invocation style.

    Cross-worktree hits: the eval re-materializes a fresh
    ``/var/tmp/capy-rw-*`` worktree per case and per majority repeat,
    and ccache's default hash includes the compilation directory — so
    identical content in two worktrees would MISS (verified live).
    ``CCACHE_NOHASHDIR`` removes the directory from the hash (verified:
    cross-worktree hit) and ``CCACHE_BASEDIR`` rewrites the few absolute
    paths that do appear. Temp files go to disk, not the 6G
    ``/run/user/<uid>`` tmpfs a dozen parallel big-TU preprocessings can
    exhaust. All via setdefault — an explicit env always wins.
    """
    env = os.environ.copy()
    if not _ccache_enabled():
        return env
    env.setdefault("CCACHE_DIR", "/var/tmp/capybase-ccache")
    env.setdefault("CCACHE_NOHASHDIR", "1")
    env.setdefault("CCACHE_BASEDIR", "/var/tmp")
    env.setdefault("CCACHE_TEMPDIR", "/var/tmp/capybase-ccache-tmp")
    env.setdefault("CCACHE_MAXSIZE", "20G")
    try:
        Path(env["CCACHE_TEMPDIR"]).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # fall back to ccache's default temp dir
    if shim_dir is None:
        shim_dir = Path("/var/tmp/capybase-ccache-shim")
    # Resolve the real compilers with the shim dir stripped from PATH so a
    # stale self-referential shim can't resolve to itself.
    _clean_path = env.get("PATH", "")
    _shim_prefix = f"{shim_dir}:"
    if _clean_path.startswith(_shim_prefix):
        _clean_path = _clean_path[len(_shim_prefix):]
    try:
        shim_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return env  # unwritable shim dir -> plain uncached compilers
    for _tool in ("gcc", "g++"):
        _real = shutil.which(_tool, path=_clean_path)
        if _real is None:
            continue
        _shim = shim_dir / _tool
        _content = f'#!/bin/sh\nexec ccache "{_real}" "$@"\n'
        try:
            _current = _shim.read_text()
        except OSError:
            _current = None  # missing (or unreadable) -> create fresh
        if _current != _content:
            try:
                _shim.write_text(_content)
                _shim.chmod(0o755)
            except OSError:
                continue
    if str(shim_dir) not in env.get("PATH", ""):
        env["PATH"] = f"{shim_dir}:{env.get('PATH', '')}"
    return env


def _run_shell_tree(
    cmd: str,
    cwd: str,
    timeout: float,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a shell build command, reaping the WHOLE process tree on timeout.

    ``subprocess.run(shell=True, timeout=...)`` kills only the direct child
    (the shell); make/libtool/ccache descendants survive, get reparented to
    the session reaper, and keep compiling in worktrees the harness later
    deletes — observed as multi-hour spinning orphan trees pinning the box
    at load ~90. Running the child in its own session and SIGKILLing the
    process group on timeout reaps every descendant at once. On timeout,
    raises ``subprocess.TimeoutExpired`` with the partial output attached,
    preserving ``subprocess.run``'s contract for existing callers.
    """
    import signal
    proc = subprocess.Popen(
        cmd, shell=True, cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout=out, stderr=err)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # pid == pgid (own session)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        try:
            exc.stdout, exc.stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            exc.stdout = exc.stderr = None
        raise


# ccache can fail on some repos (incompatible flags, corrupted cache, version
# mismatch). When it does, the error is distinct from normal compile errors.
_CCACHE_FAILURE_PATTERNS = (
    "ccache: error:",
    "ccache: internal error",
    "FAILED: ccache",
)


def _is_ccache_failure(stderr: str) -> bool:
    """True when the build failure is caused by ccache itself, not the code."""
    return any(p in stderr for p in _CCACHE_FAILURE_PATTERNS)


# Compile_commands.json cache: parsed once per repo, keyed by repo_root path.
# Values map lookup key (relative path + basename) -> (command, directory).
_COMPILE_COMMANDS_CACHE: dict[str, dict[str, tuple[str, str]]] = {}


def _load_compile_commands(repo_root: str) -> dict[str, tuple[str, str]] | None:
    """Load and cache compile_commands.json from the repo root or build dirs.

    Returns a dict mapping file path → ``(command, directory)`` — the
    ``command``/``arguments`` field plus the entry's ``directory`` (the cwd
    the command was generated to run from, typically the build dir; cmake
    emits relative ``-I`` flags that only resolve from there). Returns None
    when no compile_commands.json is found or parsing fails.
    """
    if repo_root in _COMPILE_COMMANDS_CACHE:
        return _COMPILE_COMMANDS_CACHE[repo_root] or None
    import json as _json_cc
    # Repo root and the common build-dir layouts (cmake single-config at
    # build/, multi-config at build/<cfg>/).
    root = Path(repo_root)
    candidates = [
        root / "compile_commands.json",
        root / "build" / "compile_commands.json",
    ]
    if (root / "build").is_dir():
        candidates.extend(
            sorted((root / "build").glob("*/compile_commands.json")))
    for cc_path in candidates:
        if not cc_path.exists():
            continue
        try:
            entries = _json_cc.loads(cc_path.read_text(encoding="utf-8"))
            mapping: dict[str, tuple[str, str]] = {}
            for entry in entries:
                f = entry.get("file", "")
                cmd = entry.get("command") or entry.get("arguments")
                if isinstance(cmd, list):
                    cmd = " ".join(cmd)
                directory = entry.get("directory", "") or str(repo_root)
                if f and cmd:
                    # Normalize to relative path for lookup
                    try:
                        rel = str(Path(f).relative_to(repo_root)) if Path(f).is_absolute() else f
                    except ValueError:
                        rel = f  # file outside repo_root (worktree mismatch)
                    mapping[rel] = (cmd, directory)
                    # Also store the basename for fuzzy matching
                    mapping[Path(f).name] = (cmd, directory)
            _COMPILE_COMMANDS_CACHE[repo_root] = mapping
            return mapping if mapping else None
        except Exception:  # noqa: BLE001
            continue
    _COMPILE_COMMANDS_CACHE[repo_root] = {}
    return None


def _cc_include_resolution_failure(stderr: str) -> bool:
    """True when a compile-commands compile died on #include resolution.

    ``fatal error: foo.h: No such file or directory`` means the adapted
    flags didn't resolve includes (wrong cwd/relative -I) — the check
    mis-ran, not the code failed. Callers should treat this as
    "check unavailable" (fall through to the build branch), never as a
    syntax verdict; otherwise a mis-adapted database becomes a new
    poisoned-failure source.
    """
    return "fatal error:" in (stderr or "") and (
        "No such file or directory" in (stderr or "")
    )


def _try_compile_commands(
    repo_root: str, path: str, source_text: str, language: str | None,
) -> tuple[bool, str] | None:
    """Try to compile a single file using the compile_commands.json entry.

    Returns ``(True/False, message)`` if the compile ran, or ``None`` if no
    compile_commands.json entry was found — or the adapted command couldn't
    resolve its includes (treated as check-unavailable, not a failure).

    Replaces the source path in the compile command with a temp file
    containing ``source_text``, runs with ``-fsyntax-only`` (skip linking).
    The original flags (``-I``, ``-D``, ``-std``) are preserved; the command
    runs with the ENTRY's ``directory`` as cwd (cmake's per-entry base —
    relative ``-I`` flags only resolve from there), and relative include
    paths are additionally absolutized against it so cwd variance can't
    break includes.
    """
    cc = _load_compile_commands(repo_root)
    if not cc:
        return None
    # Look up by exact path or basename
    entry = cc.get(path) or cc.get(Path(path).name)
    if not entry:
        return None
    cmd, directory = entry
    import shlex as _shlex_cc
    import subprocess as _sp_cc
    import tempfile as _tf_cc
    try:
        parts = _shlex_cc.split(cmd)
        if not parts:
            return None
        # The compiler is the first token; flags follow. Find the source file
        # in the arguments and replace it with our temp file.
        suffix = ".cpp" if language in ("cpp", "c++") else ".c"
        with _tf_cc.NamedTemporaryFile(suffix=suffix, delete=False, mode="w") as tmp:
            tmp.write(source_text)
            tmp_path = tmp.name
        try:
            # Build new command: compiler + flags (minus old source) + -fsyntax-only + tmp
            old_source = None
            for p in parts[1:]:
                if p.endswith((".c", ".cpp", ".cc", ".cxx", ".C", ".mm")):
                    old_source = p
                    break
            flags = [p for p in parts[1:] if p != old_source]
            # Replace -o output.o with nothing (syntax-only doesn't produce output)
            clean_flags = []
            skip_next = False
            abs_dir = Path(directory).resolve()
            for f in flags:
                if skip_next:
                    skip_next = False
                    # -I <rel> / -isystem <rel> (separate-argument form):
                    # absolutize against the entry directory.
                    clean_flags.append(_cc_abs_path(f, abs_dir))
                    continue
                if f == "-o":
                    skip_next = True
                    continue
                if f.startswith("-o"):
                    continue
                if f == "-I" or f == "-isystem":
                    clean_flags.append(f)
                    skip_next = True
                    continue
                if f.startswith("-I") and len(f) > 2 and not os.path.isabs(f[2:]):
                    clean_flags.append("-I" + str(_cc_abs_path(f[2:], abs_dir)))
                    continue
                if f.startswith("-isystem") and len(f) > 8 and not os.path.isabs(f[8:]):
                    clean_flags.append("-isystem" + str(_cc_abs_path(f[8:], abs_dir)))
                    continue
                clean_flags.append(f)
            new_cmd = [parts[0]] + clean_flags + ["-fsyntax-only", tmp_path]
            proc = _sp_cc.run(
                new_cmd, capture_output=True, text=True, timeout=30,
                cwd=directory if Path(directory).is_dir() else str(repo_root),
            )
            if proc.returncode == 0:
                return True, "compile_commands.json: syntax OK"
            err = (proc.stderr or "").strip()
            if _cc_include_resolution_failure(err):
                # The adapted flags couldn't resolve includes — the check
                # mis-ran. Fall through to the build branch rather than
                # reporting a false syntax failure.
                return None
            return False, err.split("\n")[0] if err else "compile_commands.json: failed"
        finally:
            Path(tmp_path).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        return None


def _cc_abs_path(p: str, base: Path) -> str:
    """Resolve ``p`` against ``base`` unless already absolute or a flag."""
    if os.path.isabs(p) or p.startswith("-"):
        return p
    return str((base / p).resolve())


_CC_ERROR_LINE_RE = re.compile(r":(\d+):\d+:\s*(?:error|warning):")

# Captures the file path BEFORE the ``:line:col:`` position. gcc/clang emit
# ``file:line:col: error: msg`` where ``file`` is a relative or absolute path
# (e.g. ``src/delete.c``, ``tool/lemon.c``, ``/tmp/tmpXXX.c``). This regex
# captures the file component so the build gate can determine whether an error
# is in the conflict file or a sibling file the merge didn't touch.
_CC_ERROR_FILE_RE = re.compile(r"([^\s:][^\s:]*?)\.([chp]+)(?:\+\+)?:\d+:\d+:\s*(?:fatal\s+)?(?:error|warning):", re.IGNORECASE)

# Detects gcc/clang -Werror warning promotions: ``error: ... [-Werror=category]``
# and ``error: ... [-Wcategory]`` — under plain -Werror, gcc 15 renders the
# plain warning tag on the promoted line (redis-0048: ``error: passing
# argument 3 of 'intsetGet' ... [-Wincompatible-pointer-types]``).
#
# The tag ALONE cannot distinguish promotions from real errors: gcc also
# tags STRUCTURAL syntax errors (``error: expected ';' before '}' token
# [-Wtemplate-body]`` — the cc-conflict catalog's cpp_template_body).
# Excused: explicit -Werror=/-Werror+ tags (promotions by construction)
# and plain -W tags in the KNOWN warning categories (semantic warnings).
# Structural categories stay errors: they fire on broken code regardless
# of strictness flags.
_CC_WERROR_TAG_RE = re.compile(r"\[-W(error[=+])?([^\]]+)\]")

#: Warning-option categories observed as promotions in this corpus —
#: semantic warnings that can also appear as plain warnings without
#: -Werror. Structural diagnostics (template-body, return-type, ...)
#: are NOT here: they indicate genuinely broken code.
_PROMOTION_W_CATEGORIES = frozenset({
    "incompatible-pointer-types", "implicit-function-declaration",
    "unused-function", "unused-variable", "unused-value",
    "unused-but-set-variable", "missing-braces", "calloc-transposed-args",
    "format-security", "format", "sign-compare", "int-conversion",
    "pointer-sign", "discarded-qualifiers", "deprecated-declarations",
    "uninitialized", "maybe-uninitialized", "unused-result", "pedantic",
})


def _is_cc_werror_warning(msg: str) -> bool:
    """True when a gcc error line is a -Werror warning promotion.

    gcc emits ``error: ...`` for -Werror promotions, indistinguishable from
    real errors except for the trailing ``[-W...]`` tag — and even the tag
    is ambiguous (structural errors carry -Wtemplate-body). Excused:
    explicit ``-Werror=``/``-Werror+`` tags and plain ``-W`` tags in the
    known warning categories. Everything else is a real error.
    """
    if msg.lstrip().startswith(("warning:", "note:")):
        return False
    m = _CC_WERROR_TAG_RE.search(msg)
    if m is None:
        return False
    if m.group(1):  # -Werror= / -Werror+ form — a promotion by construction
        return True
    return m.group(2) in _PROMOTION_W_CATEGORIES


def _parse_cc_error_line(msg: str) -> int | None:
    """Extract the 1-based line number from a gcc/clang error message.

    gcc format: ``file:line:col: error: msg`` → returns ``line`` as int.
    Returns None when the message doesn't match (e.g. ``cc failed`` with no
    position info). Used by the deterministic repair beam to target fixes.
    """
    if not msg:
        return None
    m = _CC_ERROR_LINE_RE.search(msg)
    if m:
        return int(m.group(1))
    # Fallback: "line N" pattern (used by the brace-coherence gate's message)
    m2 = re.search(r"line\s+(\d+)", msg)
    if m2:
        return int(m2.group(1))
    return None


def _is_missing_build_system(output: str) -> bool:
    """True when a build failed because the build system isn't THERE.

    ``make: *** No targets specified and no makefile found. Stop.`` (and
    friends) mean the invocation couldn't even start — no Makefile in this
    context (a rebase worktree carries tracked sources, not generated
    build artifacts). Distinct from a build that ran and reported compile
    errors: this is "check unavailable", never a merge verdict.
    """
    low = (output or "").lower()
    return (
        "no makefile found" in low
        or "no targets specified and no makefile" in low
        or "can't find cmake cache" in low
    )


_NO_RULE_TARGET_RE = re.compile(
    r"No rule to make target ['\"]([^'\"]+)['\"]"
)


def _missing_make_target(err_lines: list[str]) -> str | None:
    """The file named by make's missing-rule failure, or None.

    ``make[2]: *** No rule to make target 'X.cc', needed by 'X.lo'.  Stop.``
    carries no ``error`` substring and no file:line:col location, so the
    build gate's error-classification loop skips it entirely and the
    conservative fallback promotes the ``make[1]: *** [...] Error 1`` driver
    line to a hard failure. protobuf-0051: upstream's own merge_sha deleted
    field_access_listener.cc while leaving it in src/Makefile.am — the gate
    was unpassable for ANY conflict-file content. The named target lets the
    gate classify it like any other file-scoped failure: outside the conflict
    file = pre-existing build-system inconsistency (infra), inside = defect.
    """
    for ln in err_lines:
        m = _NO_RULE_TARGET_RE.search(ln)
        if m:
            return m.group(1)
    return None


def _parse_cc_error_location(msg: str) -> tuple[str | None, int | None]:
    """Extract ``(file_stem, line)`` from a gcc/clang diagnostic.

    gcc format: ``file:line:col: error: msg``. Returns the file's stem (the
    basename without extension, e.g. ``"lemon"`` from ``tool/lemon.c:753:...``)
    so callers can compare it against the conflict file's stem. Returns
    ``(None, None)`` when the message has no parseable file:line:col: prefix.

    Used by the build gate's error localization: a gcc error in ``tool/lemon.c``
    while resolving ``src/delete.c`` is a sibling-file issue, not a merge defect.
    """
    if not msg:
        return (None, None)
    m = _CC_ERROR_FILE_RE.search(msg)
    if m:
        file_path = m.group(1) + "." + m.group(2)
        stem = Path(file_path).stem
        line_m = re.search(r":(\d+):\d+:", msg[m.start():])
        line = int(line_m.group(1)) if line_m else None
        return (stem, line)
    return (None, None)


# The Rust editions rustc accepts for ``--edition``. 2024 stabilized in Rust
# 1.85 (Feb 2025) and is the default for ``cargo new`` since, so real crates
# now commonly carry ``edition = "2024"``. Kept as a constant so inference and
# any validation share one source of truth.
_RUST_EDITIONS = ("2015", "2018", "2021", "2024")


def _infer_rust_edition(repo_root: str, path: str) -> str:
    """Infer the Rust edition from the nearest ``Cargo.toml``.

    Walks upward from ``path`` toward ``repo_root`` looking for a
    ``Cargo.toml`` with an ``edition = "X"`` field (the conventional place a
    crate declares its edition). Returns the edition string ("2015"/"2018"/
    "2021"/"2024") when found, else "2021" for a loose ``.rs`` file with no
    Cargo.toml. This matters because edition changes parsing rules (e.g. 2015
    vs 2018 module paths, ``async``, ``dyn``, 2024's ``gen`` blocks and
    tightened lints); checking with the wrong edition can produce spurious
    errors. Pure TOML-field grep — no dependency on a TOML parser, tolerant of
    comments/whitespace. Note the cargo path (the default in a cargo project)
    doesn't use this — cargo passes the correct ``--edition`` itself; this
    inference feeds only the loose-file standalone-rustc fallback.

    The walk is strictly bounded by ``repo_root``: it never consults a
    manifest above the project root, so an outer workspace's edition can't
    leak in. If ``path`` is not itself under ``repo_root`` (a misconfigured
    root), no walk happens and the default edition is returned.
    """
    start = Path(path).resolve()
    root = Path(repo_root).resolve()
    # Only walk when path is under (or equal to) repo_root. A path outside the
    # root means the root is misconfigured; defaulting is the safe choice.
    try:
        start.relative_to(root)
    except ValueError:
        return "2021"
    # Walk from the file's directory up through repo_root, consulting each
    # directory's Cargo.toml. Innermost (nearest) manifest wins.
    chain: list[Path] = []
    cur = start.parent if start.is_file() else start
    while cur not in chain:
        chain.append(cur)
        if cur == root:
            break
        cur = cur.parent
        if cur in chain:
            break
    for d in chain:
        manifest = d / "Cargo.toml"
        if not manifest.is_file():
            continue
        try:
            for line in manifest.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                # "edition = \"2021\"" or edition='2018'; ignore commented lines.
                if stripped.startswith("#"):
                    continue
                if stripped.startswith("edition"):
                    _, _, rest = stripped.partition("=")
                    val = rest.strip().strip("'\"")
                    if val in _RUST_EDITIONS:
                        return val
        except OSError:
            continue
    # Loose .rs (no Cargo.toml) or no edition field: 2021 is the safest default
    # for standalone files. 2024 tightened some lints (e.g. unsafe_op_in_unsafe_fn
    # is now deny-by-default) that could spuriously fail older code checked in
    # isolation. The cargo path (the default in a cargo project) doesn't use
    # this inference at all — cargo passes the correct --edition itself.
    return "2021"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class VerificationEngine:
    def __init__(self, validators: list[Validator], config: ValidationConfig) -> None:
        self.validators = validators
        self.config = config
        # Sprint-19 P3: session build economics. Attached at construction
        # (optionally re-attached by the orchestrator with a journaling
        # event sink); verify_file consults it before running full builds.
        self.build_state = BuildStateTracker()

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
            NonEmptyResolutionValidator(),
            NoConflictMarkersValidator(),
            ExactSpliceScopeValidator(),
            AstPreservationValidator(),
            PreservationHeuristicValidator(),
            BothSidesRepresentedValidator(),
            IntentCoverageValidator(),
            UnattributedCodeValidator(),
            ObligationValidator(),
            NeedsHumanValidator(),
        ]
        # Per-unit syntax checks (CEGIS loop hardening): surface a code syntax
        # error (malformed format!, unclosed bracket, bad indent) as a hard
        # failure that seeds PROMPT_REPAIR, so the model sees the compile error
        # and the broken candidate on the first retry. Distinct from
        # require_syntax_if_supported (which gates the Phase B whole-file check):
        # the per-unit check is OPT-OUT via enable_per_unit_syntax_check so the
        # hermetic suite (whose fake clients produce partial snippets) can
        # disable it without disabling Phase B.
        if getattr(config, "enable_per_unit_syntax_check", True):
            validators.append(PythonSyntaxValidator())
            validators.append(RustSyntaxValidator())
            validators.append(CcsSyntaxValidator())
        # Extra validators (e.g. the opt-in VerifierModelValidator) are appended
        # so they run last — after the cheap structural checks. This keeps the
        # rank-order validation loop cheap for structurally-invalid candidates
        # and only pays the LLM critic call for candidates worth judging.
        if extra_validators:
            validators.extend(extra_validators)
        # The VeriGuard policy gate is deterministic and dependency-free (stdlib
        # ast, no client), so the engine's own factory wires it when the config
        # enables it — unlike the VerifierModelValidator, which needs an LLM
        # client and is therefore registered by the orchestrator. No rules → the
        # gate is a no-op even when enabled, so registering it is harmless.
        if getattr(config, "enable_policy_gate", False) and getattr(config, "policy_rules", ()):
            validators.append(PolicyGateValidator())
        # LLM code-smell checks: same shape as the policy gate —
        # deterministic, dependency-free (stdlib ast), so the factory wires it
        # when enabled. A cheap pre-test quality filter.
        if getattr(config, "enable_code_smell_checks", False):
            validators.append(CodeSmellValidator())
        return cls(validators, config)

    def register(self, validator: Validator) -> None:
        """Append a validator at the end of the chain (runs last)."""
        self.validators.append(validator)

    def verify(
        self, unit: ConflictUnit, candidate: CandidateResolution, *,
        fast_verify: bool = False,
    ) -> VerificationResult:
        ctx = VerificationContext(unit=unit, candidate=candidate, config=self.config)
        hard: list[VerificationFailure] = []
        warnings: list[VerificationWarning] = []
        features: dict[str, float | int | str | bool] = {}
        # Conflict feature spine: seed the aggregated features
        # with the pre-resolution characteristics recorded at extraction. This is
        # the unified input vector for the calibration flywheel / any learned
        # router — stable across validators and present even when all validators
        # pass (so accepted merges are still labeled with their inputs). Validator
        # features are merged on top and never overwrite these conflict-level keys.
        cf = unit.structural_metadata.get("conflict_features")
        if isinstance(cf, dict):
            for k, val in cf.items():
                features[k] = val
        # fast_verify: skip expensive validators (AST parser, gcc subprocess,
        # LLM critic) for deterministic-rule candidates. These validators exist
        # to catch LLM defects (hallucinated entities, syntax errors). A
        # deterministic rule provably preserves structure — the full gauntlet is
        # redundant. The whole-file Phase 2 build gate still validates the final
        # output. On large files (25K lines), this cuts per-unit verify from
        # ~20s to ~0.1s. Only the cheap O(n) validators run.
        _fast_skip = frozenset({
            "ast_preservation", "preservation_heuristic", "both_sides_represented",
            "intent_coverage", "unattributed_code", "obligation",
            "needs_human", "verifier_model", "dependency_preservation",
            "future_obligation", "rust_syntax", "ccs_syntax", "python_syntax",
        })
        for v in self.validators:
            if fast_verify and (
                v.name in _fast_skip
                or v.name.startswith("verifier_model")
            ):
                continue
            res = v.verify(ctx)
            for k, val in res.features.items():
                # Conflict-level spine keys (seeded above) take precedence so a
                # validator can't clobber the stable input vector.
                if k not in features:
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
        whole_text: str | None = None,
        pristine_side_texts: list[str] | None = None,
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

        ``whole_text`` (when provided) overrides the splice: the caller has
        already produced the final file text (e.g. after ``file_linker``
        import dedup) and wants THAT text validated — not a re-splice from
        ``resolutions``. Mirrors the whole-file-span branch below. Without
        it, a caller that dedups the spliced buffer would see its dedup
        silently discarded (verify_file re-splices the un-deduped spans).

        Returns the same ``VerificationResult`` shape so ``RiskEngine.decide``
        and the orchestrator consume it unchanged. ``unit_id``/``candidate_id``
        are file-scoped (``<path>:file``) since this result is not tied to one
        candidate.
        """
        file_id = f"{path}:file"
        hard: list[VerificationFailure] = []
        features: dict[str, float | int | str | bool] = {}

        if whole_text is not None:
            # Caller-provided final text (e.g. post file_linker dedup). Bypass
            # the splice so the validated text matches what gets written to
            # disk. See orchestrator's file_linker dedup call sites.
            whole = whole_text
        elif not resolutions:
            whole = original
        elif _has_whole_file_span(resolutions):
            # A whole-file unit (modify/delete) has marker_span=None: the
            # resolved text IS the file, there is nothing to splice. An empty
            # text means the resolution accepts the deletion (the file goes
            # away); a non-empty text is the keeper's full content. Splicing
            # would crash on the None span, so use the resolved text directly.
            whole = resolutions[0][1]
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

        # Post-splice brace-balance gate (Fix #2a — multi-hunk coherence): a
        # cheap structural check that catches the common cross-unit failure where
        # individually-valid resolutions, when spliced together, produce an extra
        # or missing brace (e.g. a candidate closes a block the original already
        # closes). This runs BEFORE the expensive cargo/py_compile cycle, so a
        # brace mismatch is caught in milliseconds instead of 3× 30s cargo runs.
        # The divergence line is recorded in detail for attribution + feedback.
        # Reported under the "syntax" validator name (it IS a syntax error) so
        # the existing test assertions and the risk/retry path treat it uniformly.
        # F2b-ordered (s27-extend): the pristine exemption must precede ANY
        # repair mutation. Previously the repairs ran first and the exemption
        # only cleared the failure — but `whole` was already corrupted, so
        # the syntax check ran on repaired-garbage and failed (axum-0019:
        # the literal repair appended a stray quote to a PRISTINE side,
        # "coherence repair applied without compiler verification", and the
        # whole-side probes declined a compiling side).
        _pristine_imbalanced = bool(pristine_side_texts) and any(
            _brace_imbalance_line(t, language) is not None
            for t in pristine_side_texts)
        if (language in ("rust", "python", "c", "cpp", "c++")
                and self.config.require_syntax_if_supported
                and not _pristine_imbalanced):
            imbalance_line = _brace_imbalance_line(whole, language)
            if imbalance_line is not None:
                # Sprint-21 coherence-repair rung: before failing, attempt the
                # DETERMINISTIC repair ladder on the spliced buffer (the
                # perfect-buffer class — 0034/0049 stray '}', 0014 missing '}'
                # at sim 0.999 — dies here). The repair functions are the same
                # ones the unit-level fallback uses; re-validate after.
                _repaired = _try_balance_braces(whole, language)
                if _repaired is None:
                    _repaired = _try_balance_braces_iterated(
                        whole, language)
                if _repaired is not None and _brace_imbalance_line(
                        _repaired, language) is None:
                    whole = _repaired
                    imbalance_line = None
                    features["coherence_repair_applied"] = True
            # Sprint-22 pre-eval item 2: unterminated string/char literal
            # (0034's exposed defect — 'missing terminating ' character').
            # Runs after (and independently of) the brace repair: a
            # quote-parity fix doesn't affect brace balance.
            _lit_repaired = _try_repair_string_literal(whole, language)
            if _lit_repaired is not None and _lit_repaired != whole:
                whole = _lit_repaired
                features["coherence_repair_applied"] = True
                # re-check braces after the literal fix (content changed)
                imbalance_line = _brace_imbalance_line(whole, language)
                if imbalance_line is not None:
                    _repaired = _try_balance_braces(whole, language)
                    if _repaired is not None:
                        whole = _repaired
                        imbalance_line = None
            # s23 mixed-delimiter repair: parens/brackets get the same
            # rung treatment braces do (zenodo-0085: unmatched ')' got zero
            # repair attempts — the brace rung's remit is {} only).
            if imbalance_line is None and _delimiter_imbalance_line(whole, language) is not None:
                _del_repaired = _try_repair_delimiter(whole, language)
                if (_del_repaired is not None
                        and _delimiter_imbalance_line(_del_repaired, language) is None
                        and _brace_imbalance_line(_del_repaired, language) is None):
                    whole = _del_repaired
                    features["coherence_repair_applied"] = True
            if imbalance_line is not None and pristine_side_texts:
                # F2 (s27): the oracle-shares doctrine at the coherence gate.
                # The brace counter is preprocessor-blind — select.c's braces
                # inside #if branches read as imbalance while gcc compiles
                # clean. When a PRISTINE side text fails the same counter,
                # the file's brace count is intrinsically unreliable and the
                # check cannot attribute anything to the merge: downgrade to
                # a feature and let the real build gate decide.
                if any(_brace_imbalance_line(t, language) is not None
                       for t in pristine_side_texts):
                    features["coherence_check_inconclusive"] = True
                    imbalance_line = None
            if imbalance_line is not None:
                # Fix #1 — enrich the message with the brace delta so the model
                # knows WHICH kind of imbalance it is (extra `}` vs unclosed `{`),
                # not just "unbalanced". The classification matches
                # _try_balance_braces: walk the cleaned depth to see whether it
                # goes negative (extra close) or ends positive (unclosed open).
                cleaned_for_msg = _strip_strings_comments(whole, language)
                _d = 0
                _went_neg = False
                for _cl in cleaned_for_msg:
                    for _ch in _cl:
                        if _ch == "{":
                            _d += 1
                        elif _ch == "}":
                            _d -= 1
                            if _d < 0:
                                _went_neg = True
                if _went_neg:
                    _delta_desc = f"extra closing brace — depth went negative (remove a stray '}}' near line {imbalance_line + 1})"
                elif _d > 0:
                    _delta_desc = f"missing closing brace — {_d} unclosed '{{' (add {_d} '}}' before line {imbalance_line + 1})"
                else:
                    _delta_desc = "brace mismatch"
                hard.append(
                    VerificationFailure(
                        validator="syntax",
                        severity="error",
                        message=(
                            f"splice coherence: unbalanced braces at line "
                            f"{imbalance_line + 1} ({_delta_desc})"
                        ),
                        detail={"brace_imbalance_line": imbalance_line + 1},
                    )
                )
                # Skip the expensive cargo/py_compile cycle — the brace mismatch
                # already explains the failure; the repair feedback is richer when
                # it points at the divergence line directly.
                features["syntax_checked"] = True
                features["syntax_passed"] = False
                features["hard_failure_count"] = len(hard)
                features["warning_count"] = 0
                return VerificationResult(
                    candidate_id=file_id, unit_id=file_id, passed=False,
                    hard_failures=hard, features=features,
                )

        # Preprocessor balance check for C/C++: catches missing #endif, broken
        # #ifdef guards, and #else without matching #if. Build-pass can't detect
        # these when the region is platform-guarded (e.g. #ifdef SQLITE_MUTEX_W32
        # stripped on Linux, hiding conflict markers inside the region).
        if language in ("c", "cpp", "c++") and self.config.require_syntax_if_supported:
            pp_line = _preprocessor_imbalance_line(whole)
            if pp_line is not None:
                # Sprint-21 coherence-repair rung (preprocessor arm —
                # sqlite-0040's #endif class).
                _repaired_pp = _try_balance_preprocessor(whole)
                if _repaired_pp is not None and _preprocessor_imbalance_line(
                        _repaired_pp) is None:
                    whole = _repaired_pp
                    pp_line = None
                    features["coherence_repair_applied"] = True
            if pp_line is not None:
                hard.append(
                    VerificationFailure(
                        validator="syntax",
                        severity="error",
                        message=(
                            f"splice coherence: unbalanced preprocessor directives "
                            f"at line {pp_line + 1} (missing #endif or extra #endif)"
                        ),
                        detail={"preprocessor_imbalance_line": pp_line + 1},
                    )
                )
                features["syntax_checked"] = True
                features["syntax_passed"] = False
                features["hard_failure_count"] = len(hard)
                features["warning_count"] = 0
                return VerificationResult(
                    candidate_id=file_id, unit_id=file_id, passed=False,
                    hard_failures=hard, features=features,
                )

        # R2 (sprint-22): exact-duplicate `use` dedup (rust) — union-merged
        # re-export lists carrying the same use line twice die on "defined
        # multiple times". Runs before the syntax stage so the compiler
        # validates the deduped text; rides the coherence_repair_applied
        # feature so R1's propagation + fail-closed guard cover it.
        if language == "rust" and self.config.require_syntax_if_supported:
            _deduped = _dedup_rust_use_statements(whole)
            if _deduped is not None:
                whole = _deduped
                features["coherence_repair_applied"] = True

        # Syntax check on the real, fully-spliced file.
        syntax_checked = False
        syntax_ok = True
        if language == "python":
            syntax_checked = True
            # No-worse-than-before delta (#7): compare the candidate's py_compile
            # errors against the blanked-baseline's, so a merge is rejected only
            # for a syntax error IT introduces — not a pre-existing one outside
            # the conflict region. The delta is ONLY trusted when the baseline
            # compiles cleanly: if the blanked conflict itself has errors (e.g.
            # two top-level ``return`` statements from juxtaposed sides — the
            # cross-unit case), we can't tell pre-existing from merge-introduced,
            # so we fall back to the strict floor (any candidate error fails).
            after_errs = _py_compile_errors(whole)
            baseline_errs = (
                _py_compile_errors(_blank_markers(original, "python"))
                if contains_markers(original) else []
            )
            if baseline_errs:
                # Baseline itself is broken → can't delta safely → strict floor.
                new_errs = after_errs
            else:
                new_errs = compute_diagnostic_delta(baseline_errs, after_errs)
            syntax_ok = not new_errs
            features["syntax_new_error_count"] = len(new_errs)
            if new_errs and self.config.require_syntax_if_supported:
                hard.append(
                    VerificationFailure(
                        validator="syntax",
                        severity="error",
                        message=f"py_compile: {len(new_errs)} new error(s): "
                        + "; ".join(new_errs[:3]),
                        detail={"new_errors": new_errs[:5]},
                    )
                )
        elif language == "rust":
            # Rust verification is crate-aware, not file-isolated. Standalone
            # ``rustc`` on a single file can't resolve ``crate::`` / ``super::``
            # paths, so it FALSE-POSITIVES on virtually every non-crate-root
            # file (any leaf that does ``use crate::config::Config`` fails with
            # E0432 even when the merge is correct). The only correct check is
            # against the whole crate via ``cargo check``, which the existing
            # RustAnalyzerRunner._check_cargo already does (writes the resolved
            # source to the real path, runs cargo, parses JSON diagnostics).
            #
            # Strategy: prefer cargo (default-on, no flag needed) for any Rust
            # file inside a Cargo project. Only fall back to standalone rustc
            # for a loose ``.rs`` with no Cargo.toml (single-file scripts, the
            # rust-uu fixture). A missing tool → "not checked" (never a false
            # failure). This mirrors Python's always-on py_compile but uses the
            # crate context Rust requires.
            from capybase.adapters.lsp import (
                _has_cargo_manifest,
                _resolve,
                nearest_cargo_manifest_dir,
            )

            # A Rust file is "in a cargo project" when EITHER the repo root has a
            # manifest (single-crate layout) OR the file sits under a member
            # crate's manifest (workspace layout, where each crate lives in a
            # subdir). The latter is the common case `_has_cargo_manifest` alone
            # misses — without it, a workspace leaf (``di-core/src/.../foo.rs``
            # doing ``use crate::tools::...``) falls back to standalone rustc,
            # which false-positives on ``crate::`` paths (E0433) and triggers a
            # phantom repair loop on a correct merge.
            used_cargo = False
            in_cargo = (
                _has_cargo_manifest(repo_root)
                or nearest_cargo_manifest_dir(repo_root, path) is not None
            )
            if in_cargo and _resolve(self.config.cargo_path):
                used_cargo = self._run_cargo_syntax_check(
                    path, original, whole, repo_root, hard, features
                )
            if used_cargo:
                syntax_checked = features.get("syntax_checked", False)
                syntax_ok = features.get("syntax_passed", True)
            else:
                # Loose .rs (no Cargo.toml) or cargo absent: standalone rustc is
                # the only option and is correct here (no crate paths to resolve).
                rustc = _resolve(self.config.rustc_path)
                if rustc is not None:
                    syntax_checked = True
                    edition = self.config.rust_edition or _infer_rust_edition(
                        repo_root, path
                    )
                    try:
                        ok, msg = _compile_rust(
                            whole, rustc_path=rustc, edition=edition
                        )
                    except FileNotFoundError:
                        ok = True  # tool vanished between resolve & run → skip
                        msg = "rustc not available; syntax not checked"
                    # E2 (sprint-23): include_str!/include_bytes! resolve
                    # relative to the ORIGINAL file's directory; a temp-copy
                    # compile cannot see them (axum-0005/0033: include_str'd
                    # docs read as /tmp/../docs/... — a false gate failure).
                    # Undecidable at this location: never a failure here.
                    if re.search(r"include_(?:str|bytes)!\s*\(", whole):
                        ok = True
                        msg = ("rustc standalone: include_str/include_bytes "
                               "undecidable from a temp copy; not checked")
                    # Standalone rustc on a loose .rs can't resolve crate::
                    # / super:: paths (no Cargo.toml context), so it FALSE-
                    # POSITIVES on E0432/E0433 for any correct file that does
                    # ``use crate::...``. These are exactly the codes
                    # rust_suppress_codes is meant to drop (undecidable
                    # standalone); suppress them here so a correct merge isn't
                    # rejected + cycled on a phantom crate-path error. The
                    # cargo path (when a real crate is available) already
                    # suppresses them via compute_diagnostic_delta.
                    if not ok:
                        suppress = set(
                            getattr(self.config, "rust_suppress_codes", [])
                            or [])
                        if suppress and any(
                            f"[{code}]" in msg for code in suppress
                        ):
                            ok = True  # suppressed: undecidable standalone
                            msg = (
                                f"rustc standalone: suppressed crate-path "
                                f"error(s) per rust_suppress_codes (undecidable "
                                f"without a crate context): {msg[:80]}"
                            )
                    syntax_ok = ok
                    if not ok and self.config.require_syntax_if_supported:
                        hard.append(
                            VerificationFailure(
                                validator="syntax",
                                severity="error",
                                message=msg,
                                detail={"edition": edition},
                            )
                        )
                features["syntax_checked"] = syntax_checked
                features["syntax_passed"] = syntax_ok
        elif language in ("c", "cpp", "c++"):
            # C/C++ whole-file verification. Two paths:
            #
            # 1. When a user-supplied build command (``cc_build_command``) is
            #    configured: write the resolved file to its real path in the repo
            #    and run the build (make/cmake) there — the authentic whole-tree
            #    oracle. This resolves sibling ``#include`` headers standalone
            #    gcc can't (``server.h``, ``sqliteInt.h``), which the live-eval
            #    proved is the only honest signal for real-world C. Mirrors the
            #    cargo/clippy save-write-restore dance.
            # 2. Fallback (no build command, e.g. loose files / no build system):
            #    standalone ``gcc``/``g++ -fsyntax-only`` on the spliced file. A
            #    missing compiler → "not checked" (never a false fail).
            #
            # No semantic filter here: the whole file has full translation-unit
            # context (in either path), so an undeclared identifier IS a real
            # defect (mirrors how the cargo path doesn't suppress E0432).
            build_cmd = getattr(self.config, "cc_build_command", "") or ""
            # Build-target narrowing: if a target template is configured,
            # compile ONLY the conflict file's translation unit instead of
            # the full project build. This cuts build verification from ~54s
            # (full make) to ~2-5s (single object), critical for sqlite
            # cases where the full build blows the case timeout. Falls back
            # to the full build if the target rule doesn't exist.
            target_tmpl = getattr(self.config, "cc_build_target_template", "") or ""
            # Don't use build-target narrowing for header files — headers
            # aren't compiled to objects (make vdbe.lo has no rule for vdbe.h).
            # The full build or gcc fallback handles headers correctly.
            if target_tmpl and not path.endswith((".h", ".hpp", ".hh", ".hxx")):
                _stem = Path(path).stem
                try:
                    build_cmd = target_tmpl.format(stem=_stem)
                except (KeyError, IndexError):
                    pass  # malformed template; use full build_cmd
            # Compile_commands.json fast path: if the repo has a
            # compile_commands.json (generated by cmake with
            # -DCMAKE_EXPORT_COMPILE_COMMANDS=ON), look up the exact compile
            # command for this file and run it with -fsyntax-only. This gives
            # the correct -I paths, -D defines, and -std flags without running
            # the full build (75s → 2-5s). Falls back to the build command if
            # the file isn't in compile_commands.json or the compile fails.
            _cc_result = _try_compile_commands(repo_root, path, whole, language)
            _cc_skip_build = False
            if _cc_result is not None:
                _cc_ok, _cc_msg = _cc_result
                syntax_checked = True
                syntax_ok = _cc_ok
                if _cc_ok:
                    features["syntax_checked"] = True
                    features["syntax_passed"] = True
                    _cc_skip_build = True  # skip the full build
                # If _cc_ok is False, fall through to the full build below
                # (which may reveal cross-file issues or better diagnostics).
            if not _cc_skip_build and build_cmd:
                import subprocess as _sp_build
                import time as _bs_time

                _bs = getattr(self, "build_state", None)
                # Full build vs targeted per-file build (make {stem}.o):
                # only FULL builds degrade the session — a targeted
                # timeout says the .o has deep dependencies, not that the
                # tree can't finish under any cap we're willing to pay.
                _is_full_build = not target_tmpl
                target_path = Path(repo_root) / path
                saved = target_path.read_bytes() if target_path.exists() else None
                msg = ""
                _build_env = _ccache_env()

                def _syntax_only_fallback(reason: str) -> tuple[bool, str]:
                    """Standalone gcc -fsyntax-only parse of ``whole``.

                    Shared by the timeout path and the degraded-session
                    skip: strictly less authoritative than a full build
                    (no sibling #include resolution) but completes in
                    seconds and never rejects on infrastructure.
                    """
                    from capybase.adapters.lsp import _resolve as _resolve_cc_fb
                    _cc_fb = _resolve_cc_fb(
                        getattr(self.config, "cxx_path", "g++")
                        if language in ("cpp", "c++")
                        else getattr(self.config, "cc_path", "gcc")
                    )
                    if _cc_fb is None:
                        return False, f"{reason}: no fallback compiler"
                    _std_fb = (getattr(self.config, "cpp_std", "c++17")
                               if language in ("cpp", "c++")
                               else getattr(self.config, "c_std", "c11"))
                    _suffix_fb = ".cpp" if language in ("cpp", "c++") else ".c"
                    _inc_fb = [str(repo_root)]
                    _fdir = (Path(repo_root) / path).parent
                    if str(_fdir) != str(repo_root):
                        _inc_fb.append(str(_fdir))
                    try:
                        ok_fb, msg_fb = _compile_ccs(
                            whole, cc_path=_cc_fb, std=_std_fb,
                            suffix=_suffix_fb, include_paths=_inc_fb,
                        )
                    except FileNotFoundError:
                        return True, "fallback compiler not available"
                    if not ok_fb and _CCS_SEMANTIC_RE.search(msg_fb):
                        return True, (f"cc fallback: semantic pattern skipped "
                                      f"({msg_fb[:50]})")
                    if not ok_fb and _is_cc_werror_warning(msg_fb):
                        return True, f"cc fallback: -Werror skipped ({msg_fb[:50]})"
                    return ok_fb, msg_fb

                if _bs is not None and _is_full_build and not _bs.full_build_available:
                    # Sprint-19 P3: a full build already timed out this
                    # session — re-running it at the same cap adds zero
                    # information while burning the case budget. Skip
                    # straight to the syntax-only fallback (journaled).
                    features["build_skipped_prior_timeout"] = True
                    _bs.record_probe(build_cmd, 0.0, "skipped",
                                     path=path,
                                     reason="prior full-build timeout (session degraded)")
                    _sk_reason = (f"build skipped (prior full-build timeout): "
                                  f"{build_cmd}")
                    syntax_ok, _sk_fb = _syntax_only_fallback(_sk_reason)
                    msg = f"{_sk_reason}; gcc -fsyntax-only: {_sk_fb}"
                    syntax_checked = True
                else:
                    _bs_t0 = _bs_time.monotonic()
                    try:
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        target_path.write_text(whole, encoding="utf-8")
                        syntax_checked = True
                        # Targeted builds (make {stem}.o) get a shorter timeout:
                        # a single .o should compile in <30s. If it takes longer,
                        # it's building deep dependencies (hiredis, lua, sqlite
                        # amalgamation) and will likely hit the full timeout.
                        # Fall back to gcc -fsyntax-only immediately.
                        _build_timeout = 30 if target_tmpl else 300
                        _build_attempts = 0
                        proc = None
                        while proc is None:
                            _build_attempts += 1
                            try:
                                proc = _run_shell_tree(
                                    build_cmd, cwd=str(repo_root),
                                    timeout=_build_timeout, env=_build_env,
                                )
                            except _sp_build.TimeoutExpired as _to_exc:
                                _to_out = ""
                                for _attr in ("stderr", "stdout"):
                                    _chunk = getattr(_to_exc, _attr, None)
                                    if _chunk:
                                        if isinstance(_chunk, bytes):
                                            _chunk = _chunk.decode(
                                                "utf-8", errors="replace")
                                        _to_out += _chunk
                                _kind = _classify_build_failure_kind(_to_out)
                                if (
                                    _bs is not None and _is_full_build
                                    and _kind != "generic"
                                    and _build_attempts < 2
                                ):
                                    # Recoverable (lock contention, compiler
                                    # crash, network): ONE retry at 2× the
                                    # cap — the failure burned wall clock,
                                    # not capability.
                                    _bs.note_recoverable_retry(
                                        _kind, build_cmd, _build_timeout)
                                    _build_timeout *= 2
                                    continue
                                if _bs is not None:
                                    _bs.record_probe(
                                        build_cmd,
                                        _bs_time.monotonic() - _bs_t0,
                                        "timeout", path=path, kind=_kind,
                                        errors=(_to_out or "")[-300:] or None)
                                    if _is_full_build:
                                        _bs.note_timeout(
                                            _kind, build_cmd, _build_timeout)
                                # Build timed out — the targeted/full build's
                                # dependency chain (hiredis, lua, jemalloc,
                                # sqlite amalgamation) can take >300s even for
                                # a single .o target. Fall back to standalone
                                # gcc -fsyntax-only which only PARSES the file
                                # (no dependency compilation, completes in
                                # seconds). This is strictly less authoritative
                                # than a full build (can't resolve sibling
                                # #include headers), but it's better than a
                                # timeout that rejects a correct merge.
                                syntax_ok, _fb_msg = _syntax_only_fallback(
                                    "build timed out")
                                msg = (
                                    f"build timed out ({_build_timeout:g}s): "
                                    f"{build_cmd}; fell back to gcc "
                                    f"-fsyntax-only: {_fb_msg}"
                                )
                                break
                        if proc is not None:
                            # ccache fallback: if ccache itself failed (corrupted
                            # cache, version mismatch, incompatible flags), retry
                            # without ccache so the build isn't blocked by a
                            # tooling issue. This prevents ccache from introducing
                            # a new failure category.
                            if (
                                proc.returncode != 0
                                and _ccache_enabled()
                                and _is_ccache_failure((proc.stderr or "") + (proc.stdout or ""))
                            ):
                                proc = _run_shell_tree(
                                    build_cmd, cwd=str(repo_root), timeout=300,
                                )
                            # Targeted-build fallback: if the Makefile doesn't have
                            # a rule for this target (e.g. cmake projects), retry
                            # with the full build command.
                            _full_cmd = getattr(self.config, "cc_build_command", "") or ""
                            if (
                                proc.returncode != 0
                                and target_tmpl
                                and _full_cmd
                                and _full_cmd != build_cmd
                                and "No rule to make target" in (proc.stderr or "")
                            ):
                                proc = _run_shell_tree(
                                    _full_cmd, cwd=str(repo_root), timeout=300,
                                    env=_build_env,
                                )
                                build_cmd = _full_cmd  # for the journal/detail
                            syntax_ok = proc.returncode == 0
                            if not syntax_ok:
                                stderr = (proc.stderr or "").strip()
                                err_lines = stderr.splitlines()
                                # Distinguish linker errors from compile errors.
                                # Linker errors (collect2, ld returned, undefined
                                # reference) are infrastructure failures — the
                                # model's code compiled fine but the full-project
                                # link failed due to missing deps/sibling objects.
                                # These are NOT model defects; treat as a pass so
                                # the merge isn't rejected on a linker issue the
                                # model can't control.
                                is_linker_error = any(
                                    "collect2" in ln or "ld returned" in ln
                                    or "undefined reference" in ln
                                    for ln in err_lines
                                )
                                if is_linker_error:
                                    syntax_ok = True  # compile passed; link is infra
                                    msg = "build: linker error (not a model defect; compile succeeded)"
                                elif _is_missing_build_system(
                                        (proc.stderr or "") + (proc.stdout or "")):
                                    # The build system isn't materialized in this
                                    # context (no Makefile — e.g. a rebase worktree
                                    # carries tracked sources but not generated
                                    # build artifacts). The build check is
                                    # UNAVAILABLE, not failed: treating it as a
                                    # failure poisons every downstream candidate
                                    # and feeds garbage feedback to the repair
                                    # loop (protobuf-0043: the LLM declined the
                                    # meaningless "make: No targets specified"
                                    # feedback three times and the rebase
                                    # escalated). Fall back to the syntax/dup
                                    # checks that DID run.
                                    syntax_ok = True
                                    features["build_unavailable"] = True
                                    msg = (
                                        "build: build system not materialized "
                                        "(no Makefile) — check skipped"
                                    )
                                else:
                                    # Error localization (research §9): classify each
                                    # gcc error line by WHERE it occurs. A whole-tree
                                    # build (make/cmake) compiles many translation
                                    # units; a pre-existing error in a SIBLING file
                                    # (tool/lemon.c, deps/hiredis.c) is NOT caused by
                                    # the merge and must not reject a correct resolution.
                                    # Similarly, -Werror warning promotions (strict
                                    # flags turning warnings into errors) are not real
                                    # compile failures. Only a genuine error IN the
                                    # conflict file is a model defect.
                                    conflict_stem = Path(path).stem
                                    real_errors = []      # in conflict file, real
                                    sibling_errors = []   # in other files, infra
                                    werror_lines = []     # -Werror promotions, infra
                                    # C17 (sprint-26): make's missing-target
                                    # failure is invisible to the loop below
                                    # (no "error" substring, no file:line:col)
                                    # — classify it by the NAMED target file,
                                    # same doctrine as sibling compile errors.
                                    # protobuf-0051: upstream's own merge_sha
                                    # deleted field_access_listener.cc while
                                    # leaving it in src/Makefile.am — the gate
                                    # was unpassable for ANY content, and a
                                    # sim-0.999 resolution died in the repair
                                    # loop on the meaningless driver line.
                                    _nr_target = _missing_make_target(err_lines)
                                    if _nr_target is not None:
                                        if Path(_nr_target).stem == conflict_stem:
                                            real_errors.append(
                                                "missing make target "
                                                f"{_nr_target} (conflict file "
                                                "absent from the build)")
                                        else:
                                            sibling_errors.append(
                                                "missing make target "
                                                f"{_nr_target} (build-system "
                                                "inconsistency, not the "
                                                "resolved file)")
                                    # make/cmake driver lines: ``make[2]: ***``,
                                    # ``CMake Error``, ``ninja: error``. These are
                                    # build-system summaries, not gcc diagnostics —
                                    # they don't carry a file:line:col: location and
                                    # shouldn't be attributed to the conflict file.
                                    # The actual gcc error line(s) appear separately
                                    # and ARE classified below.
                                    _is_build_driver_line = (
                                        lambda ln: (
                                            ln.startswith("make[")
                                            or ln.startswith("make:")
                                            or "CMake Error" in ln
                                            or ln.startswith("ninja:")
                                            or ln.startswith("*** ")
                                            or "Error 1" in ln
                                            or "Error 2" in ln
                                        )
                                    )
                                    for ln in err_lines:
                                        if "error" not in ln.lower():
                                            continue
                                        # Skip make/cmake/ninja driver summary lines
                                        # — they reference build targets, not source
                                        # files. The gcc diagnostic lines are what
                                        # carry the file:line:col: location.
                                        if _is_build_driver_line(ln):
                                            continue
                                        # -Werror warning promotion?
                                        if _is_cc_werror_warning(ln):
                                            werror_lines.append(ln)
                                            continue
                                        file_stem, _ = _parse_cc_error_location(ln)
                                        if file_stem is not None and file_stem != conflict_stem:
                                            sibling_errors.append(ln)
                                        elif file_stem == conflict_stem:
                                            # Positively identified as in the conflict
                                            # file → genuine defect.
                                            real_errors.append(ln)
                                        # else: file_stem is None (unparseable gcc
                                        # line) — don't classify yet; we may find a
                                        # parseable line later. If ALL error lines
                                        # are unparseable, we fall through to the
                                        # conservative fallback below.
                                    if real_errors:
                                        # Genuine error in the conflict file → hard fail.
                                        msg = real_errors[0]
                                    elif sibling_errors or werror_lines:
                                        # All errors are in sibling files or -Werror
                                        # promotions → the merge compiled fine; the
                                        # build failure is pre-existing infrastructure.
                                        syntax_ok = True
                                        parts = []
                                        if sibling_errors:
                                            sib = sibling_errors[0]
                                            sib_stem, _ = _parse_cc_error_location(sib)
                                            parts.append(
                                                f"sibling-file error in {sib_stem or '?'}"
                                                f" (not the resolved file {conflict_stem})"
                                            )
                                        if werror_lines:
                                            parts.append(
                                                f"{len(werror_lines)} -Werror warning(s)"
                                            )
                                        msg = (
                                            "build: infrastructure failure, not a merge "
                                            f"defect ({'; '.join(parts)})"
                                        )
                                    else:
                                        # No parseable error lines at all — fall back
                                        # to the first error-containing line.
                                        msg = next(
                                            (ln for ln in err_lines if "error" in ln.lower()),
                                            err_lines[0] if err_lines else "build failed",
                                        )
                        if _bs is not None:
                            # Sprint-24 cycle-C: capture a bounded stderr tail
                            # on failing probes. The protobuf-0051 side probes
                            # failed in 0.1s with no diagnostic — whether that
                            # is "No rule to make target", a missing Makefile,
                            # or a real compile error was undeterminable from
                            # cmd/duration/outcome alone. err_lines is defined
                            # on every not-ok path (assigned right after the
                            # returncode check).
                            _probe_extra = {}
                            if not syntax_ok:
                                # Prefer the last ERROR-CONTAINING lines,
                                # EXCLUDING make/ninja driver summaries
                                # (``make[1]: *** [...] Error 1`` matches
                                # "error" but carries zero diagnostic signal —
                                # protobuf-0051's tails were all driver
                                # lines). The gcc diagnostic (which the
                                # driver line references) is the signal.
                                def _is_driver_summary(ln: str) -> bool:
                                    s = ln.strip()
                                    return (
                                        s.startswith("make[")
                                        or s.startswith("make:")
                                        or s.startswith("ninja:")
                                        or "Error 1" in s or "Error 2" in s
                                        or "Waiting for unfinished jobs" in s
                                    )
                                _probe_err_sel = [
                                    ln for ln in err_lines
                                    if "error" in ln.lower()
                                    and not _is_driver_summary(ln)
                                ] or [
                                    ln for ln in err_lines
                                    if not _is_driver_summary(ln)
                                ] or err_lines
                                _probe_tail = "; ".join(_probe_err_sel[-3:]) if _probe_err_sel else (msg or "")
                                _probe_tail = _probe_tail[:300]
                                if _probe_tail:
                                    _probe_extra["errors"] = _probe_tail
                            _bs.record_probe(
                                build_cmd,
                                _bs_time.monotonic() - _bs_t0,
                                "pass" if syntax_ok else "fail",
                                path=path, **_probe_extra)
                    except FileNotFoundError as exc:
                        # Build tool absent → skip (never a false fail), mirroring
                        # the gcc-absent path below.
                        syntax_ok = True
                        msg = f"build command not available: {exc}"
                    finally:
                        # Restore the pre-check worktree state immediately; the
                        # orchestrator writes the final buffer later iff validation
                        # passes. Mirrors _run_clippy_check's restore dance.
                        if saved is not None:
                            target_path.write_bytes(saved)
                        elif target_path.exists():
                            target_path.unlink(missing_ok=True)
                if not syntax_ok and self.config.require_syntax_if_supported:
                    hard.append(
                        VerificationFailure(
                            validator="syntax",
                            severity="error",
                            # detail.source tags this as a whole-file BUILD
                            # failure (vs standalone parse checks) so
                            # downstream gates can treat compile-flavored
                            # failures specially (whole-side repair rung,
                            # repair attribution carve-out) without
                            # string-matching the message.
                            detail={"source": "whole_file_build",
                                    "build_cmd": build_cmd},
                            message=msg or "build failed",
                        )
                    )
            else:
                # Fallback: standalone gcc/g++ -fsyntax-only (the original path).
                from capybase.adapters.lsp import _resolve as _resolve_cc
                is_cpp = language in ("cpp", "c++")
                cc = _resolve_cc(self.config.cxx_path if is_cpp else self.config.cc_path)
                if cc is not None:
                    syntax_checked = True
                    std = self.config.cpp_std if is_cpp else self.config.c_std
                    suffix = ".cpp" if is_cpp else ".c"
                    # Header files (#include "sibling.h") need -I paths to
                    # resolve project-internal type definitions (u8, BtCursor,
                    # sqlite3_vfs defined in sqliteInt.h etc.). Pass the repo
                    # root and the conflict file's directory as include paths so
                    # standalone gcc can resolve sibling includes it otherwise
                    # can't (research §5: anchored local context). For .c/.cpp
                    # files this is harmless (gcc adds search paths, never
                    # removes error-detection capability).
                    _include_paths = []
                    if repo_root:
                        _include_paths.append(str(repo_root))
                        file_dir = (Path(repo_root) / path).parent
                        if str(file_dir) != str(repo_root):
                            _include_paths.append(str(file_dir))
                    try:
                        ok, msg = _compile_ccs(
                            whole, cc_path=cc, std=std, suffix=suffix,
                            include_paths=_include_paths or None,
                        )
                    except FileNotFoundError:
                        ok = True  # tool vanished between resolve & run → skip
                        msg = "C/C++ compiler not available; syntax not checked"
                    # Standalone gcc runs in /tmp with no -I flags, so it cannot
                    # resolve project-internal headers (#include "json.h",
                    # "server.h"). A missing-header fatal error is an artifact
                    # of compiling the fragment out of translation-unit context,
                    # NOT a parse defect — the same principle the per-unit
                    # CcsSyntaxValidator applies via _CCS_SEMANTIC_PATTERNS.
                    # Without this tolerance, the gcc fallback (which fires when
                    # no build command is configured) escalates correct merges
                    # whose only "error" is an unresolved sibling header. The
                    # whole-file build command (make/cmake) is the authoritative
                    # oracle for header resolution; the gcc fallback is a cheap
                    # structural gate, not a header-resolution gate.
                    if not ok and _CCS_SEMANTIC_RE.search(msg):
                        ok = True
                        msg = f"cc: semantic/missing-header pattern skipped in standalone mode ({msg[:60]})"
                    # -Werror warning promotions (research §9): gcc emits
                    # 'error: ... [-Werror=category]' for warnings the project's
                    # build flags promoted to errors. The code compiled
                    # successfully but triggered a strictness flag — not a real
                    # compile failure and not a merge defect. The trailing
                    # [-Werror=...] tag is the only signal that distinguishes
                    # these from genuine errors (gcc emits 'error:' for both).
                    if not ok and _is_cc_werror_warning(msg):
                        ok = True
                        msg = f"cc: -Werror warning promotion skipped in standalone mode ({msg[:60]})"
                    syntax_ok = ok
                    if not ok and self.config.require_syntax_if_supported:
                        hard.append(
                            VerificationFailure(
                                validator="syntax",
                                severity="error",
                                message=msg,
                                detail={"std": std},
                            )
                        )
            features["syntax_checked"] = syntax_checked
            features["syntax_passed"] = syntax_ok
        elif language == "toml" and Path(path).name == "Cargo.toml":
            # A dependency/manifest conflict in Cargo.toml. ``detect_language``
            # classifies it as ``"toml"`` (not ``"rust"``), so it never reached
            # the rust branch above and was previously text-only verified. But a
            # resolved manifest can introduce real errors (an absent/ambiguous
            # version, a feature/dep mismatch, malformed TOML) that only
            # ``cargo`` catches. Run a crate-aware manifest check when this path
            # is the Cargo manifest AND cargo is available. Note we can't gate on
            # ``_has_cargo_manifest`` (a pre-existing on-disk Cargo.toml): the
            # manifest under resolution IS Cargo.toml — it exists in memory
            # (``original``/``whole``) and is written to disk only inside the
            # check. ``_run_cargo_manifest_check`` does the save/write/restore.
            from capybase.adapters.lsp import _resolve

            if _resolve(self.config.cargo_path):
                syntax_checked, syntax_ok = self._run_cargo_manifest_check(
                    path, original, whole, repo_root, hard, features
                )
            # No cargo available → text-only (a generic ``.toml`` config file or
            # a manifest conflict without a toolchain stays unverifiable).
        features["syntax_checked"] = features.get("syntax_checked", syntax_checked)
        features["syntax_passed"] = features.get("syntax_passed", syntax_ok)

        # Semantic whole-file checks: duplicate definitions + unreachable code.
        # Always-on (no config knob — mirror the syntax check), degrading to a
        # silent pass when the parser/grammar is unavailable or on a parse
        # error. These catch the two "plausible but wrong" merge shapes a small
        # model produces that pass line/token validators: a duplicated block
        # (both sides present, just twice) and stacked terminators (dead code).
        self._run_duplicate_definition_check(path, language, whole, hard, features, original=original)
        self._run_unreachable_code_check(path, language, whole, hard, features)

        # LSP / type-checker diagnostics (Phase B): reject NEW errors.
        self._run_lsp_diagnostics(
            path, language, original, whole, repo_root, hard, features
        )

        # Clippy lint check (Phase B, opt-in): flag NEW clippy findings the
        # merge introduces. Rust-only; inert otherwise and when disabled.
        if language == "rust":
            self._run_clippy_check(
                path, original, whole, repo_root, hard, features
            )

        # Shadow tests (Phase B): best-effort run of tests for this module.
        # ``whole`` (the resolved file) is threaded in so the oracle runs
        # against the RESOLVED merge, not the on-disk baseline (see
        # _run_shadow_tests).
        self._run_shadow_tests(path, whole, repo_root, hard, features)

        # R1 (s22) fail-closed guard: a candidate that needed a deterministic
        # repair rung is PROVISIONAL. The syntax/compile stage above ran on
        # the REPAIRED text, so when it executed and passed, acceptance is
        # compiler-backed. When it could NOT run (tool absent, undecidable
        # environment), coherence alone is not acceptance evidence — fail
        # honestly and let the repair loop / escalation decide. The
        # reviewers' unanimous rule: never accept a coherence-repaired
        # candidate on coherence alone.
        if (features.get("coherence_repair_applied")
                and not (features.get("syntax_checked")
                         and features.get("syntax_passed"))):
            features["coherence_repair_unverified"] = True
            hard.append(
                VerificationFailure(
                    validator="syntax",
                    severity="error",
                    message=(
                        "coherence repair applied without compiler "
                        "verification (syntax stage did not run or did not "
                        "pass on the repaired text); provisional candidate "
                        "is not accepted on coherence alone"
                    ),
                    detail={"coherence_repair_unverified": True},
                )
            )
        passed = len(hard) == 0
        features["hard_failure_count"] = len(hard)
        features["warning_count"] = 0
        # Unified no-worse-than-before rollup: the total NEW diagnostics the
        # candidate introduced across every delta-aware check (syntax/lsp/clippy).
        # Each check records its own ``<check>_new_error_count``; this is the
        # single number an auto-accept policy could gate on.
        features["introduced_diagnostics"] = (
            int(features.get("syntax_new_error_count", 0) or 0)
            + int(features.get("lsp_new_error_count", 0) or 0)
            + int(features.get("clippy_new_finding_count", 0) or 0)
        )
        return VerificationResult(
            candidate_id=file_id,
            unit_id=file_id,
            passed=passed,
            hard_failures=hard,
            warnings=[],
            features=features,
            # R1 (s22): propagate the repaired text — the caller must write
            # what was actually validated (see field docstring in
            # conflict_model.py). Only set when a repair rung fired.
            resolved_text=(
                whole if features.get("coherence_repair_applied") else None
            ),
        )

    # ------------------------------------------------------------------
    # Phase B helpers: LSP diagnostics and shadow tests.
    # ------------------------------------------------------------------

    def _run_cargo_syntax_check(
        self,
        path: str,
        original: str,
        whole: str,
        repo_root: str,
        hard: list[VerificationFailure],
        features: dict[str, float | int | str | bool],
    ) -> bool:
        """Run ``cargo check`` as the default Rust syntax/compile check.

        This is the correct, crate-aware verification for Rust (the only way to
        resolve ``crate::``/``super::`` paths), run via the existing
        ``RustAnalyzerRunner._check_cargo`` which writes the resolved source to
        the real file path and parses cargo's JSON diagnostics.

        Uses the same baseline/new-error logic as ``_run_lsp_diagnostics``: a
        merge fails ONLY on errors it introduces, not on pre-existing crate
        errors (a repo that already doesn't compile is the developer's problem).
        The baseline is the pre-conflict ``original`` with conflict markers
        blanked to ONE side (keeping both sides, as ``_blank_markers`` does,
        produces a spurious duplicate-definition error for an add-add conflict
        — two ``pub const DEFAULT`` / two ``fn new()`` — that then masks the
        very error a duplicate-merge would introduce). We compare error
        *messages* between baseline and the resolved file.

        Records into the ``syntax_*`` features (this IS the default syntax check
        for Rust in a cargo project) and returns True when cargo actually ran
        (so the caller knows not to also run standalone rustc). Returns False
        when cargo was absent or the check didn't run — the caller then falls
        back to standalone rustc for loose files.
        """
        try:
            from capybase.adapters import lsp as lsp_mod
        except Exception:  # noqa: BLE001
            return False
        runner = lsp_mod.RustAnalyzerRunner(
            cargo_path=self.config.cargo_path,
            rust_analyzer_path=self.config.rust_analyzer_path,
            # Cold first compile of a fresh workspace tree (tokio: ~2-4 min
            # from an empty target/ with warm CARGO_HOME) blows the 120s
            # default — and a timed-out BASELINE is worse than no baseline
            # (see below), so give the cold pass real headroom.
            timeout=300,
        )
        # Baseline: the original file with conflict markers blanked to ONE side
        # so it parses as valid Rust (no duplicate-definition noise from the
        # second conflict side). See _blank_markers_one_side.
        baseline_src = _blank_markers_one_side(original, "rust")
        baseline = runner.check(baseline_src, path=path, repo_root=repo_root)
        after = runner.check(whole, path=path, repo_root=repo_root)
        if not after.checked or not baseline.checked:
            # cargo absent/failed/timed out on EITHER side → not checked. An
            # unchecked baseline carries an EMPTY error list; deltaing against
            # it counts every candidate error as "new" and rejects even the
            # oracle — tokio-0110: the baseline's cold compile blew the 120s
            # subprocess cap, and a calm-environment probe showed base,
            # current, replayed AND the oracle all carry the same two
            # pre-existing errors at merge_sha (zero new for every variant).
            # An undecidable delta must abstain, never fail.
            features["syntax_checked"] = False
            features["syntax_passed"] = True
            return False
        features["syntax_checked"] = True
        # New errors = after errors absent from the baseline, via the shared
        # no-worse-than-before delta. Pass the Diagnostics (not just
        # .message) so the delta can key on .code — a pre-existing E0432 in
        # baseline suppresses an E0432 in the candidate even if the message text
        # drifted.
        #
        # IMPORTANT (E0433 phase-scoping): do NOT pass suppress_codes here. The
        # cargo path has FULL CRATE CONTEXT — crate-path errors (E0432/E0433)
        # are decidable in this phase (cargo can resolve them). A genuinely-new
        # E0433 from cargo means a broken import and MUST be a hard failure.
        # The suppress_codes are only for the standalone-rustc path (isolated
        # snippet, no crate context, where E0433 is undecidable). Conflating
        # the two would let a broken merge pass final validation.
        new_errors = compute_diagnostic_delta(
            list(baseline.errors),
            list(after.errors),
        )
        syntax_ok = len(new_errors) == 0
        features["syntax_passed"] = syntax_ok
        features["syntax_tool"] = "cargo"
        features["syntax_new_error_count"] = len(new_errors)
        _append_diagnostic_failure(
            new_errors, hard, self.config,
            validator="syntax", message_prefix="cargo check", tool="cargo",
            extra_detail={"source": "whole_file_build"},
        )
        return True

    def _run_cargo_manifest_check(
        self,
        path: str,
        original: str,
        whole: str,
        repo_root: str,
        hard: list[VerificationFailure],
        features: dict[str, float | int | str | bool],
    ) -> tuple[bool, bool]:
        """Run ``cargo check`` against a resolved ``Cargo.toml`` conflict.

        Closes the manifest-verification gap: ``Cargo.toml`` is classified
        ``"toml"`` by ``detect_language``, so it never reached the rust syntax
        branch and was previously text-only verified. A resolved manifest can
        introduce real errors (a typo'd or absent version, a feature/dep
        mismatch, an invalid table) that only ``cargo`` sees.

        Mirrors ``_run_clippy_check``'s proven save/write/restore dance:
        ``whole`` is the in-memory resolved manifest (not yet on disk —
        ``verify_file`` runs before the orchestrator writes), so we write it for
        the "after" run and the marker-blanked ``original`` for the baseline,
        restoring the saved worktree bytes each time. The baseline/new-error
        comparison is the same message-set logic as ``_run_cargo_syntax_check``:
        a merge fails ONLY on manifest errors it introduces, not pre-existing
        ones. Records into ``syntax_*`` with ``syntax_tool="cargo"``.

        Returns ``(syntax_checked, syntax_passed)``. Never a false failure: if
        cargo is absent or the check doesn't run, returns ``(False, True)`` —
        consistent with the rustc-absent path (text-only fallback).
        """
        try:
            from capybase.adapters import lsp as lsp_mod
        except Exception:  # noqa: BLE001
            return False, True
        runner = lsp_mod.RustAnalyzerRunner(
            cargo_path=self.config.cargo_path,
            rust_analyzer_path=self.config.rust_analyzer_path,
        )
        # After state: write the resolved manifest, run cargo check.
        with temp_worktree_file(repo_root, path, whole):
            after = runner._check_cargo(whole, path, repo_root)
        if not after.checked:
            # cargo absent / failed → not checked (never a false fail).
            features["syntax_checked"] = False
            features["syntax_passed"] = True
            return False, True
        # Baseline: marker-blanked original (one side kept so it's valid TOML),
        # cargo-checked, then restored. TOML comments use ``#``, which is the
        # default blanking prefix.
        _baseline_src = _blank_markers_one_side(original)
        with temp_worktree_file(repo_root, path, _baseline_src):
            baseline = runner._check_cargo(_baseline_src, path, repo_root)
        if not baseline.checked:
            # Undecidable delta (unchecked baseline = empty error list) →
            # abstain, never count every candidate error as "new".
            features["syntax_checked"] = False
            features["syntax_passed"] = True
            return False, True
        features["syntax_checked"] = True
        # Phase-scoped: cargo manifest check has full crate context — do NOT
        # pass suppress_codes (E0432/E0433 are decidable here, unlike the
        # isolated standalone-rustc path).
        new_errors = compute_diagnostic_delta(
            list(baseline.errors),
            list(after.errors),
        )
        syntax_ok = len(new_errors) == 0
        features["syntax_passed"] = syntax_ok
        features["syntax_tool"] = "cargo"
        features["syntax_new_error_count"] = len(new_errors)
        _append_diagnostic_failure(
            new_errors, hard, self.config,
            validator="syntax", message_prefix="cargo check", tool="cargo",
            extra_detail={"manifest": True},
        )
        return True, syntax_ok

    def _run_clippy_check(
        self,
        path: str,
        original: str,
        whole: str,
        repo_root: str,
        hard: list[VerificationFailure],
        features: dict[str, float | int | str | bool],
    ) -> None:
        """Run ``cargo clippy`` and flag NEW lint findings the merge introduces.

        Clippy is a quality check (not a compile check — the cargo floor
        already proved the merge compiles). It runs against the whole crate's
        CURRENT worktree state (Phase 2 has written every resolved file), and
        uses the same baseline/new-finding comparison: a merge is flagged only
        for clippy findings NOT present in the pre-conflict ``original``
        (markers blanked), so a repo's pre-existing lint debt is ignored.

        Severity defaults to ``"warning"`` (record the finding, bias toward
        review, don't hard-reject a compiling merge); ``"error"`` blocks
        lint-introducing merges. Opt-in via ``enable_clippy``. Inert when cargo
        is absent, there's no Cargo.toml, or the language isn't Rust.
        """
        features.setdefault("clippy_checked", False)
        features.setdefault("clippy_new_finding_count", 0)
        if not self.config.enable_clippy:
            return
        try:
            from capybase.adapters import lsp as lsp_mod
        except Exception:  # noqa: BLE001
            return
        # Baseline: the original file with markers blanked so clippy runs on a
        # valid (if marker-laden-blanked) crate. We compare clippy findings
        # (by message) between baseline and the resolved worktree.
        # NOTE: clippy is crate-wide, so the baseline/after both reflect the
        # whole crate. ``whole`` (the resolved file) is in memory here — it is
        # NOT yet on disk (verify_file runs before the orchestrator writes) —
        # so we write it temporarily for the "after" run, then the blanked
        # original for the baseline, then restore whatever was on disk.
        # After state: write the resolved file, run clippy.
        with temp_worktree_file(repo_root, path, whole):
            after = lsp_mod.run_clippy(
                repo_root, cargo_path=self.config.cargo_path
            )
        if not after.checked:
            features["clippy_checked"] = False
            return
        # Baseline: temporarily write the marker-blanked (one-side) original,
        # run clippy, then restore the saved worktree state.
        with temp_worktree_file(repo_root, path, _blank_markers_one_side(original, "rust")):
            baseline = lsp_mod.run_clippy(
                repo_root, cargo_path=self.config.cargo_path
            )
        if not baseline.checked:
            # Undecidable delta (unchecked baseline = empty finding list) →
            # abstain, never count every candidate finding as "new".
            features["clippy_checked"] = False
            return
        features["clippy_checked"] = True
        baseline_diags = list(baseline.diagnostics)
        new_findings = compute_diagnostic_delta(
            baseline_diags, list(after.diagnostics),
            suppress_codes=set(getattr(self.config, "rust_suppress_codes", []) or []),
        )
        features["clippy_new_finding_count"] = len(new_findings)
        if new_findings:
            severity = self.config.clippy_severity
            msg = "; ".join(m[:80] for m in new_findings[:3])
            check = VerificationCheckResult(
                name="clippy",
                passed=severity != "error",
                severity=severity,
                message=f"clippy: {len(new_findings)} new finding(s): {msg}",
                detail={"findings": new_findings[:5]},
                features={"clippy_new_findings": True},
            )
            # Reuse the hard/warning classification: error severity → hard fail.
            if severity == "error":
                hard.append(
                    VerificationFailure(
                        validator="clippy",
                        severity="error",
                        message=check.message,
                        detail=check.detail,
                    )
                )

    def _run_duplicate_definition_check(
        self,
        path: str,
        language: str | None,
        whole: str,
        hard: list[VerificationFailure],
        features: dict[str, float | int | str | bool],
        *,
        original: str = "",
    ) -> None:
        """Reject a merge that defines the same name twice in one scope.

        The "duplicate block" failure shape a small model produces when it
        concatenates both sides' versions of a class/struct/function instead of
        merging them: both sides' content is present (so BothSidesRepresented
        and the token-set validators pass), just defined twice. This is almost
        always a wrong merge — a deliberate redefinition is rare in a conflict
        region — so severity is ``error`` and feeds the whole-file repair loop.

        BASELINE-AWARE: duplicates that already existed in the pre-conflict file
        (the marker-blanked ``original``) are NOT flagged — they're pre-existing
        real-world patterns (config overrides, matplotlib fig reassignment, etc.)
        that the oracle itself contains. Only duplicates the MERGE INTRODUCES
        (not in the baseline) are flagged. This follows the same no-worse-than-
        before principle as the syntax diagnostic-delta.

        Python uses stdlib ``ast`` (catches classes/functions AND bare
        module-level assignments like ``FEATURE_FLAGS = {...}`` that
        the abstract parser's enumerate_entities intentionally skips). Rust reuses
        ``structural.duplicate_definitions`` (abstract parser, lazy). Other
        languages / no language: no-op. Degrades to a silent pass on any parse
        gap (a missing grammar or a syntax error — the latter is the syntax
        check's failure to report, not this one's).
        """
        features.setdefault("duplicate_definition_checked", False)
        features.setdefault("duplicate_definition_count", 0)
        if language == "python":
            dupes = _py_duplicate_definitions(whole)
        elif language in ("rust", "c", "cpp", "c++"):
            try:
                from capybase.adapters import structural
            except Exception:  # noqa: BLE001
                return
            if not structural.is_available(language):
                return
            dupes = structural.duplicate_definitions(whole, language)
        else:
            return
        if dupes is None:
            # Parse failed (Python) or the abstract parser couldn't parse (Rust): the
            # syntax check owns reporting that. Record not-checked and stop.
            return
        features["duplicate_definition_checked"] = True
        # Baseline-aware: compute the duplicates in the pre-conflict text and
        # suppress any (kind, name) pair that already existed pre-conflict.
        # This prevents false-positives on real-world patterns like config
        # overrides or block-scoped C enums (sqlite's tclsqlite.c defines
        # TTYPE_enum twice inside one giant function — legal C, one per if
        # block, and the parser models the whole function body as one scope).
        # With markers, the baseline is the marker-blanked original. WITHOUT
        # markers the original is a pure side/pristine text — not a conflict,
        # so nothing is "merge-introduced": the text IS its own baseline and
        # the check must not fire (pristine-side verifications — F1 takeovers,
        # compile-clean side probes — were falsely vetoed on exactly this:
        # 29 sqlite cases carry such patterns and the ORACLE itself has one).
        baseline_keys: set[tuple[str, str]] = set()
        if original:
            baseline_text = (
                _blank_markers(original, language or "python")
                if contains_markers(original) else original
            )
            if language == "python":
                baseline_dupes = _py_duplicate_definitions(baseline_text)
            else:
                try:
                    baseline_dupes = structural.duplicate_definitions(baseline_text, language)
                except Exception:
                    baseline_dupes = None
            if baseline_dupes:
                baseline_keys = {(d[0], d[1]) for d in baseline_dupes}
        # Filter out pre-existing duplicates.
        new_dupes = [(k, n, r) for k, n, r in dupes if (k, n) not in baseline_keys]
        features["duplicate_definition_count"] = len(new_dupes)
        for kind, name, rows in new_dupes:
            # The leading row is the FIRST definition; the message leads with
            # the last duplicate's line so repair attribution (which parses
            # "line N" from the message) lands on the offending (duplicate)
            # occurrence, not the legitimate original.
            loc = rows[-1]
            where = ", ".join(str(r) for r in rows)
            # Variable reassignment (``X = 1`` then ``X = 2``) is LEGAL Python —
            # it's a common real-world pattern (config overrides, matplotlib fig
            # reassignment, state updates). Only FUNCTION/CLASS redefinition is a
            # genuine merge defect (the "both sides concatenated" failure shape).
            # Demote variable duplicates to WARNING (feed the risk engine but
            # don't hard-reject + feed the repair loop).
            sev = "warning" if kind == "variable" else "error"
            hard.append(
                VerificationFailure(
                    validator="duplicate_definition",
                    severity=sev,
                    message=(
                        f"line {loc}: {kind} '{name}' defined more than once "
                        f"in the same scope (at lines {where})"
                    ),
                    detail={"kind": kind, "name": name, "lines": rows},
                )
            )

    def _run_unreachable_code_check(
        self,
        path: str,
        language: str | None,
        whole: str,
        hard: list[VerificationFailure],
        features: dict[str, float | int | str | bool],
    ) -> None:
        """Reject unreachable code after an unconditional terminator.

        Catches the "stacked return" merge where a small model emits both
        sides' return statements one after the other (``return 'hi'`` then
        ``return 'howdy'``) — syntactically valid, both sides "present", but
        the second is dead. A legitimate merge would combine the values, not
        concatenate the statements.

        Python only (stdlib ``ast``); other languages are a no-op for now
        (Rust has no single-call equivalent to this and the cargo floor plus
        clippy cover most dead-code cases there). Severity ``error``. Skips
        trivial trailing nodes (``pass``, docstrings, ``...``) so idiomatic
        stubs don't trip it. Degrades to a silent pass on a syntax error.
        """
        features.setdefault("unreachable_code_checked", False)
        features.setdefault("unreachable_code_count", 0)
        if language != "python":
            return
        findings = _py_unreachable_code(whole)
        if findings is None:
            # Parse failed: the syntax check reports it. Don't double-report.
            return
        features["unreachable_code_checked"] = True
        features["unreachable_code_count"] = len(findings)
        for funcname, term_kind, line in findings:
            hard.append(
                VerificationFailure(
                    validator="unreachable_code",
                    severity="error",
                    message=(
                        f"line {line}: unreachable code after {term_kind} "
                        f"in {funcname}()"
                    ),
                    detail={
                        "function": funcname,
                        "terminator": term_kind,
                        "line": line,
                    },
                )
            )

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

        For Rust, ``cargo check`` already runs as the DEFAULT syntax check in
        ``_run_cargo_syntax_check`` (crate-aware, no flag needed), so this LSP
        path is a no-op for Rust unless ``enable_lsp_diagnostics`` is explicitly
        on — in which case rust-analyzer runs as an additional (deeper) check on
        top of cargo. Without the flag, re-running cargo here would duplicate
        the syntax check and could produce competing results.
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
        baseline_src = _blank_markers(original, language)
        baseline = runner.check(baseline_src, path=path, repo_root=repo_root)
        after = runner.check(whole, path=path, repo_root=repo_root)
        if not after.checked or not baseline.checked:
            # Same abstain rule as the cargo syntax check: an unchecked
            # baseline has an empty error list — deltaing against it counts
            # every candidate error as "new" and rejects even the oracle.
            features["lsp_checked"] = False
            features["lsp_error_count"] = 0
            features["lsp_new_error_count"] = 0
            return
        features["lsp_checked"] = True
        features["lsp_error_count"] = after.error_count
        # New errors = after errors not present in baseline, via the shared
        # no-worse-than-before delta. Pass Diagnostics so code-keyed matching
        # engages, and thread rust_suppress_codes.
        new_errors = compute_diagnostic_delta(
            list(baseline.errors),
            list(after.errors),
            suppress_codes=set(getattr(self.config, "rust_suppress_codes", []) or []),
        )
        features["lsp_new_error_count"] = len(new_errors)
        _append_diagnostic_failure(
            new_errors, hard, self.config,
            validator="lsp_diagnostics", message_prefix="LSP introduced",
            tool=after.tool, require_gate=False,
        )

    def _run_shadow_tests(
        self,
        path: str,
        whole: str,
        repo_root: str,
        hard: list[VerificationFailure],
        features: dict[str, float | int | str | bool],
    ) -> None:
        """Best-effort: run the file's tests for a quick sanity check.

        Dispatches by language:
        - **Python**: runs ``tests/test_<module>.py`` via pytest.
        - **Rust**: runs ``cargo test`` scoped to the module (e.g.
          ``src/config.rs`` → ``cargo test config::``), which compiles + runs
          any ``#[test]`` items in that module. Falls back to a bare
          ``cargo test`` when no Cargo.toml is found or the module has no tests.

        A failure is a WARNING, not a hard error — the merge may be correct
        even if pre-existing tests fail for unrelated reasons. This records
        ``shadow_tests_passed`` as a calibration feature. No-op when disabled,
        when no test file/target is found, or when the toolchain is absent.

        ``whole`` is the RESOLVED file (in memory at the ``verify_file`` level).
        For Rust we must run the test against the resolved content, not whatever
        happens to be on disk — but ``verify_file`` runs before the orchestrator
        writes, so the worktree may hold the conflict-marked baseline. We write
        ``whole`` to the file path for the cargo-test run and restore the prior
        bytes after (the proven save/write/restore dance from _run_clippy_check).
        At the orchestrator level (Phase 2) the file is already written resolved,
        so the write/restore is a transparent no-op there.
        """
        features.setdefault("shadow_tests_run", False)
        features.setdefault("shadow_tests_passed", True)
        if not self.config.enable_shadow_tests:
            return
        located = _locate_shadow_test(path, repo_root)
        if located is None:
            return
        target, lang = located
        if lang == "rust":
            # Run against the RESOLVED file, restoring whatever was on disk.
            target_path = Path(repo_root) / path
            saved = target_path.read_bytes() if target_path.exists() else None
            try:
                if whole is not None:
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    target_path.write_text(whole, encoding="utf-8")
                ok, rc, outpath = _run_rust_shadow_test(target, repo_root)
            finally:
                if saved is not None:
                    target_path.write_bytes(saved)
                elif target_path.exists():
                    target_path.unlink(missing_ok=True)
            if ok is None:
                return  # cargo absent / no Cargo.toml → not run
            features["shadow_tests_run"] = True
            features["shadow_tests_passed"] = ok
            if not ok:
                hard.append(
                    VerificationFailure(
                        validator="shadow_tests",
                        severity="warning",
                        message=f"cargo shadow tests failed: {target}",
                        detail={"test_target": target, "returncode": rc},
                    )
                )
            return
        # Python (default): pytest on the located test file.
        try:
            proc = subprocess.run(
                ["python3", "-m", "pytest", target, "-q"],
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
                    detail={"test_path": target, "returncode": proc.returncode},
                )
            )


def _enabled_for(cfg: ValidationConfig, name: str) -> bool:
    table = {
        "no_conflict_markers": cfg.require_no_markers,
        "whole_file_markers": cfg.require_no_markers,
        "exact_splice_scope": cfg.require_exact_splice_scope,
        "ast_preservation": cfg.require_ast_preservation,
        "preservation_heuristic": cfg.reject_if_copies_one_side,
        "both_sides_represented": cfg.reject_if_drops_a_side,
        "intent_coverage": cfg.min_preservation_ratio > 0.0,
        "unattributed_code": True,
        "obligation": cfg.reject_if_drops_obligation,
        "referenced_symbol_dropped": cfg.reject_if_drops_referenced_symbol,
        "needs_human": cfg.reject_if_model_needs_human,
        "syntax": cfg.require_syntax_if_supported,
        "verifier_model": cfg.enable_verifier_model,
        "policy_gate": cfg.enable_policy_gate,
        "code_smell": cfg.enable_code_smell_checks,
    }
    if name in table:
        return table[name]
    # PoLL jury members are named verifier_model_<focus>; all route through the
    # same enable_verifier_model gate (the jury is on iff the critic is on).
    if name.startswith("verifier_model_"):
        return cfg.enable_verifier_model
    return True


def _blank_markers(text: str, language: str | None = None) -> str:
    """Replace conflict-marker blocks with comments so the baseline parses.

    The pre-conflict ``original`` (the worktree with raw markers) isn't valid
    Python/Rust. For per-unit validation (syntax check, AST fingerprint, LSP
    baseline) we need it to parse so we can collect diagnostics OUTSIDE the
    conflict. This function neutralizes each conflict block by:

    - Keeping the **first** side's body lines as-is (live code).
    - **Commenting out** the second side's body lines (so they don't produce
      duplicate definitions / consecutive-expression errors).
    - Replacing marker lines (``<<<<<<<``, ``=======``, ``>>>>>>>``) with
      comments.

    Line numbers are preserved (each line maps 1:1). The comment syntax is
    language-appropriate: ``//`` for Rust (a bare ``#`` is an attribute, not a
    comment, and breaks the Rust parse), ``#`` otherwise. When ``language`` is
    None, defaults to ``#``.

    **Why the second side is commented out, not just the markers**: in a
    multi-hunk file, a sibling conflict block's BOTH sides left as live code
    produces invalid syntax — e.g. two consecutive ``format!()`` expressions
    with no semicolon (Rust: ``expected ';', found 'format'``), or two
    duplicate function definitions. Commenting out the second side's body
    eliminates these false errors while keeping one valid copy of the code so
    the file parses. (Previously this function only blanked the marker LINES,
    leaving both bodies as live code — a bug that caused false-positive syntax
    rejections on correct candidates in multi-hunk files.)
    """
    from capybase.adapters.language import adapter_for
    comment = adapter_for(language).comment_prefix
    out: list[str] = []
    state = "code"  # code | in_first_side | in_second_side
    for line in text.split("\n"):
        if line.startswith("<<<<<<<"):
            state = "in_first_side"
            out.append(f"{comment} conflict-marker")
            continue
        if line.startswith("======="):
            state = "in_second_side"
            out.append(f"{comment} conflict-marker")
            continue
        if line.startswith(">>>>>>>"):
            state = "code"
            out.append(f"{comment} conflict-marker")
            continue
        if state == "in_second_side":
            # Comment out the second side's body so it doesn't produce
            # duplicate-definition / consecutive-expression syntax errors.
            # Preserve the line (1:1 line-number mapping) as a comment.
            out.append(f"{comment} {line}" if line.strip() else line)
            continue
        out.append(line)
    return "\n".join(out)


@contextmanager
def temp_worktree_file(repo_root: str, path: str, text: str) -> Iterator[Path]:
    """Temporarily write ``text`` to ``repo_root/path``, restoring the original after.

    The save/write/restore idiom for whole-file verification: the resolved file
    (``whole``) is in memory (verify_file runs before the orchestrator writes),
    so to run cargo/clippy/gcc against it we write it to the worktree, run the
    tool, then restore the pre-check bytes. Previously this 8-line try/finally
    was copy-pasted at 4+ sites (cargo manifest, clippy after+baseline, C build);
    this context manager centralizes it so the restore is never skipped on an
    exception (a missed restore would leave a corrupt file for the next case).

    If the file did not exist before, it is removed on exit (we created it).
    Yields the ``Path`` of the written file.
    """
    target = Path(repo_root) / path
    saved = target.read_bytes() if target.exists() else None
    try:
        target.write_text(text, encoding="utf-8")
        yield target
    finally:
        if saved is not None:
            target.write_bytes(saved)
        elif target.exists():
            target.unlink(missing_ok=True)


def _top_level_identities(text: str, language: str | None) -> str | None:
    """The top-level entity identity sequence (``kind:name``) of ``text``.

    Used by the AST-preservation validator's injection guard: a resolution that
    injects a NEW top-level def/class/struct (anywhere in the file) changes the
    identity sequence, which a line-range-partitioned outside fingerprint can
    miss (the injected node lands "inside" the recomputed span). Returns None
    when the parse fails (the caller treats that as "no signal").
    """
    from capybase.adapters import structural
    ir = structural._abstract_parse(text, language)
    if ir is None:
        return None
    return " ".join(f"{u.kind}:{u.name or '<anon>'}" for u in ir.units)


def _blank_markers_one_side(text: str, language: str | None = None) -> str:
    """Blank conflict blocks to ONE side so the baseline parses as valid code.

    Historically distinct from :func:`_blank_markers` (which kept both sides'
    bodies as live code). Now that ``_blank_markers`` also comments out the
    second side's body, the two functions are identical — this is kept as a
    thin alias for backward compatibility with existing call sites.
    """
    return _blank_markers(text, language)


def _locate_shadow_test(path: str, repo_root: str) -> tuple[str, str] | None:
    """Find a test target for ``path`` by convention.

    Returns ``(target, language)`` so the caller dispatches to the right
    runner, or ``None`` when nothing test-shaped is found:

    - **Python** (``src/app.py``): ``tests/test_app.py`` (pytest).
    - **Rust** (``src/config.rs``): an empty target means "run the whole cargo
      test suite" via ``cargo test``. Rust colocates ``#[test]`` items inside
      source modules rather than a separate ``tests/`` file, and a precise
      per-module filter is unreliable (the test path depends on crate
      structure: a ``#[cfg(test)] mod tests`` in the crate-root file is just
      ``tests::``, not ``<stem>::tests::``, so a ``<stem>::`` filter silently
      filters out every test and exits 0). Since shadow tests are an advisory
      sanity check (warning severity, never hard-reject), running the full
      suite is the correct, robust choice — a regression anywhere is worth
      surfacing before continuing a rebase. Returns ``None`` only when the repo
      has no ``Cargo.toml`` (not a cargo project → no cargo tests).

    The Rust case never touches the filesystem for a test file (cargo resolves
    modules), so it returns a target even though no ``tests/`` entry exists.
    """
    from pathlib import Path

    p = Path(path)
    if p.suffix == ".py":
        candidate = Path(repo_root) / "tests" / f"test_{p.stem}.py"
        if candidate.is_file():
            return (str(candidate), "python")
        return None
    if p.suffix == ".rs":
        # Only meaningful inside a cargo project; otherwise no test runner.
        if (Path(repo_root) / "Cargo.toml").is_file():
            return ("", "rust")  # "" → run the whole cargo test suite
        return None
    return None


def _run_rust_shadow_test(
    target: str, repo_root: str, *, timeout: int = 180
) -> tuple[bool | None, int, str]:
    """Run ``cargo test`` and return ``(passed, returncode, target)``.

    ``target`` is currently always "" (run the whole suite); the parameter is
    kept for a future per-module filter once a reliable one is available.
    ``passed`` is None when cargo is absent or the invocation fails (e.g. a
    compile error in unrelated code) — the caller treats that as "not run"
    rather than a failure, mirroring the Python path's tolerance for missing
    pytest. A non-zero return code from cargo (a failed ``#[test]`` assertion
    or a compile error in the merged code) is a failure.
    """
    from shutil import which

    cargo = which("cargo")
    if cargo is None:
        return (None, -1, target)
    argv = ["cargo", "test", "--quiet"]
    if target:
        argv.append(target)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=repo_root,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return (None, -1, target)
    # cargo test exits 0 when tests pass, non-zero on a failed assertion or a
    # compile error in the merged code.
    return (proc.returncode == 0, proc.returncode, target)


def find_misplaced_declaration(
    buffer: str, error_text: str,
) -> tuple[int, str] | None:
    """(0-based line, declaration text) of a function declaration declared
    INSIDE a function body — gcc's "invalid storage class for function".

    redis-0013's wf trace: the model's merge carried cliSwitchProto's
    prototype inside a function (storage-class error at round 0); two
    repair rounds passed before the buffer reached the implicit-
    declaration state C1 handles. Relocating the misplaced declaration
    (remove it; C1's derived-prototype re-places it at file scope)
    short-circuits those rounds.
    """
    import re as _re

    m = _re.search(
        r"invalid storage class for function ['‘']([A-Za-z_]\w*)[''’]",
        error_text)
    if not m:
        return None
    symbol = m.group(1)
    lines = buffer.split("\n")
    depth = 0
    in_comment = False
    for i, raw in enumerate(lines):
        line = raw
        if in_comment:
            if "*/" in line:
                in_comment = False
                line = line.split("*/", 1)[1]
            else:
                depth += line.count("{") - line.count("}")
                continue
        if "/*" in line and "*/" not in line:
            in_comment = True
            line = line.split("/*")[0]
        s = line.strip()
        if s.startswith("#"):
            continue
        if depth > 0 and s.endswith(";") and symbol in s and "(" in s:
            # A declaration of the symbol at non-file scope.
            return i, s
        depth += line.count("{") - line.count("}")
    return None

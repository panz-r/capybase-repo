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
from dataclasses import dataclass, field
from pathlib import Path
import ast
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
    # Both-sides-represented (survey §5.1 cheap necessary condition): flag a
    # candidate that drops a side's additions entirely. Companion to
    # reject_if_copies_one_side — that catches verbatim copies; this catches
    # tweaked-but-still-one-sided merges. Advisory warning (feeds risk/retry).
    reject_if_drops_a_side: bool = True
    # Side-obligation contract (#3): flag a candidate that reverts a side's
    # MODIFICATION of an existing line back to base, or drops a side's added line.
    # Advisory warning (feeds retry). Kept in sync with config.py's pydantic
    # ValidationConfig.reject_if_drops_obligation.
    reject_if_drops_obligation: bool = True
    # Dependency preservation (survey §2.2 SafeMerge necessary condition): warn
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
        # Value-resolution fast path: when both sides preserve the same statement
        # shape and only a value diverged (a return, an assignment to the same
        # target), a verbatim copy of one side is the CORRECT resolution — the
        # base operation is preserved and the value is resolved. Don't flag it.
        cf = ctx.unit.structural_metadata.get("conflict_features")
        if isinstance(cf, dict) and cf.get("value_resolution"):
            return VerificationCheckResult(
                name=self.name,
                passed=True,
                severity="warning",
                message="value-resolution conflict: one side's value selected (base op preserved)",
                features={
                    "copied_one_side": False,
                    "value_resolution": True,
                },
            )
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


class BothSidesRepresentedValidator:
    """Cheap necessary condition for semantic conflict-freedom (survey §5.1).

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
        """
        return set(re.findall(r"\w+", text or ""))

    def verify(self, ctx: VerificationContext) -> VerificationCheckResult:
        # Value-resolution fast path: when both sides preserve the same statement
        # shape and only a value diverged (a return, an assignment to the same
        # target), a one-sided merge (picking either side's value) is the correct
        # resolution — the base operation is preserved. The token-set "both sides
        # represented" pressure is wrong here (two return values or two assignments
        # to the same target don't compose), so don't flag a dropped side.
        cf = ctx.unit.structural_metadata.get("conflict_features")
        if isinstance(cf, dict) and cf.get("value_resolution"):
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
    """Deterministic per-side structural-intent coverage (survey §5.1 signatures).

    The hard coverage guarantee: of the logical units (function/method/class/
    field) each side ADDED beyond base, the resolution must preserve at least a
    configured fraction. Computed via tree-sitter ``enumerate_entities`` — no
    LLM, fully deterministic. Complements the LLM critic: where the critic is a
    qualitative judge (uncertain, degrades silently), this is a quantitative
    floor ("2/3 replayed-side units preserved → ratio 0.67") that fires even
    when the critic is skipped or returns a low-confidence pass.

    Warning severity (feeds the critic retry path, same as the other soft drops).
    Only fires when a side added ≥1 structural entity, so value-only conflicts
    (e.g. changing a constant) are unaffected — the token-set
    :class:`BothSidesRepresentedValidator` remains the backstop there.
    Inert when tree-sitter or the grammar is unavailable.
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
                message="intent coverage skipped (tree-sitter unavailable)",
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
    """Deterministic spurious-addition guard (survey §2.1 unattributed code).

    The INVERSE of :class:`IntentCoverageValidator`: where coverage checks that
    no side's unit was DROPPED, this checks that the merge added no unit present
    in NONE of the three sides — a hallucinated helper, an extra branch, a
    synthesized function. LLMs add "helpful" logic no side asked for; this is the
    only check for surplus code, completing the "neither dropped nor spurious"
    guarantee. Computed via tree-sitter ``unattributed_entities`` (no LLM).

    Warning severity (feeds the retry path, like the other soft signals). A unit
    is "unattributed" if its NAME appears in none of base/current/replayed — so a
    legitimate extracted helper (genuinely needed but newly named) also flags;
    the model can justify keeping it on retry, and the message names the specific
    unit so a human can judge. Inert when tree-sitter or the grammar is absent.
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
                message="unattributed code skipped (tree-sitter unavailable)",
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
    """SafeMerge necessary-condition: don't drop a base dependency (survey §2.2).

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

    This is the verifier-model seam (surveys §1/§5 Proposer-Critic): every
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
        # VibeThinker/DeepSeek-R1 style) emit a long <think> chain BEFORE the
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
        # Value-resolution override: when both sides preserved the same statement
        # shape and only a value diverged (a return, an assignment to the same
        # target), a one-sided merge IS the correct resolution — the base
        # operation is preserved and the value is resolved. The critic judges
        # "did the resolution preserve each side's intent?", which for a value
        # conflict means "did it keep one of the divergent values?" — picking
        # either side satisfies that. So a verdict flagging the dropped side is
        # not a defect here; relax it to a pass so the critic doesn't fight a
        # correct value resolution (the deterministic validators already pass).
        cf = ctx.unit.structural_metadata.get("conflict_features")
        if (
            isinstance(cf, dict)
            and cf.get("value_resolution")
            and not preserves_both
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
# VeriGuard-style deterministic policy gate (survey §4)
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

    Tries (1) the text as-is, then (2) wrapped in a dummy function body (so
    bare ``return``/indented fragments parse). Returns the ast.Module, or None
    if neither parse succeeds (genuinely malformed — the gate degrades to empty
    facts rather than crashing).
    """
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError):
        pass
    # Wrap in a function body: dedent first so the fragment's indentation aligns
    # to one level under `def _f():`. This makes a bare `return` or
    # leading-space fragment a valid function body.
    import textwrap

    dedented = textwrap.dedent(text)
    wrapped = "def __bcf_policy_fragment__():\n" + textwrap.indent(dedented, "    ")
    try:
        return ast.parse(wrapped)
    except (SyntaxError, ValueError):
        return None


class PolicyGateValidator:
    """Deterministic safety gate over candidate import/call facts (survey §4).

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
# LLM code-smell detection (survey §7)
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
    """Deterministic LLM code-smell checker (survey §7).

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
        spliced = _blank_markers(spliced, lang)
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


def compute_diagnostic_delta(
    baseline_errors: list[str], after_errors: list[str]
) -> list[str]:
    """The errors in ``after`` that were NOT in ``baseline`` (#7).

    The shared no-worse-than-before primitive: every diagnostic check that can
    delta-compare (LSP, cargo, py_compile) reduces to a message-set difference —
    "what errors does the candidate introduce that the blanked-baseline didn't
    already have?". This centralizes that set-difference so the four independent
    helpers stop re-implementing it and a unified ``introduced_diagnostics``
    feature can be derived consistently.

    Errors are compared by message string (normalized: stripped). Position
    (line/column) is intentionally NOT part of the key — a merge that moves a
    pre-existing error to a new line is not "new", but a genuinely new message
    is. Returns the new messages (order preserved, deduplicated).
    """
    baseline = {str(m).strip() for m in baseline_errors if str(m).strip()}
    seen: set[str] = set()
    new_errors: list[str] = []
    for m in after_errors:
        key = str(m).strip()
        if key and key not in baseline and key not in seen:
            seen.add(key)
            new_errors.append(key)
    return new_errors


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
    so a duplicated ``FEATURE_FLAGS = {...}`` is caught (tree-sitter misses
    these). A function shadowed by a same-named class is NOT a collision
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
            BothSidesRepresentedValidator(),
            IntentCoverageValidator(),
            UnattributedCodeValidator(),
            ObligationValidator(),
            NeedsHumanValidator(),
        ]
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
        # LLM code-smell checks (survey §7): same shape as the policy gate —
        # deterministic, dependency-free (stdlib ast), so the factory wires it
        # when enabled. A cheap pre-test quality filter.
        if getattr(config, "enable_code_smell_checks", False):
            validators.append(CodeSmellValidator())
        return cls(validators, config)

    def register(self, validator: Validator) -> None:
        """Append a validator at the end of the chain (runs last)."""
        self.validators.append(validator)

    def verify(self, unit: ConflictUnit, candidate: CandidateResolution) -> VerificationResult:
        ctx = VerificationContext(unit=unit, candidate=candidate, config=self.config)
        hard: list[VerificationFailure] = []
        warnings: list[VerificationWarning] = []
        features: dict[str, float | int | str | bool] = {}
        # Conflict feature spine (survey §6.7/§4.2): seed the aggregated features
        # with the pre-resolution characteristics recorded at extraction. This is
        # the unified input vector for the calibration flywheel / any learned
        # router — stable across validators and present even when all validators
        # pass (so accepted merges are still labeled with their inputs). Validator
        # features are merged on top and never overwrite these conflict-level keys.
        cf = unit.structural_metadata.get("conflict_features")
        if isinstance(cf, dict):
            for k, val in cf.items():
                features[k] = val
        for v in self.validators:
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
        self._run_duplicate_definition_check(path, language, whole, hard, features)
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

        passed = len(hard) == 0
        features["hard_failure_count"] = len(hard)
        features["warning_count"] = 0
        # Unified no-worse-than-before rollup (#7): the total NEW diagnostics the
        # candidate introduced across every delta-aware check (syntax/lsp/clippy).
        # Each check records its own ``<check>_new_error_count``; this is the
        # single number a future unattended-accept policy (#10) can gate on.
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
        )
        # Baseline: the original file with conflict markers blanked to ONE side
        # so it parses as valid Rust (no duplicate-definition noise from the
        # second conflict side). See _blank_markers_one_side.
        baseline_src = _blank_markers_one_side(original, "rust")
        baseline = runner.check(baseline_src, path=path, repo_root=repo_root)
        after = runner.check(whole, path=path, repo_root=repo_root)
        if not after.checked:
            # cargo absent or failed to run → not checked (never a false fail).
            features["syntax_checked"] = False
            features["syntax_passed"] = True
            return False
        features["syntax_checked"] = True
        # New errors = after errors absent from the baseline (by message), via the
        # shared no-worse-than-before delta (#7).
        new_errors = compute_diagnostic_delta(
            [d.message for d in baseline.errors],
            [d.message for d in after.errors],
        )
        syntax_ok = len(new_errors) == 0
        features["syntax_passed"] = syntax_ok
        features["syntax_tool"] = "cargo"
        features["syntax_new_error_count"] = len(new_errors)
        if new_errors and self.config.require_syntax_if_supported:
            msg = "; ".join(m[:80] for m in new_errors[:3])
            hard.append(
                VerificationFailure(
                    validator="syntax",
                    severity="error",
                    message=f"cargo check: {len(new_errors)} new error(s): {msg}",
                    detail={
                        "new_errors": new_errors[:5],
                        "tool": "cargo",
                    },
                )
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
        target_path = Path(repo_root) / path
        saved = target_path.read_bytes() if target_path.exists() else None
        # After state: write the resolved manifest, run cargo check.
        try:
            target_path.write_text(whole, encoding="utf-8")
            after = runner._check_cargo(whole, path, repo_root)
        finally:
            if saved is not None:
                target_path.write_bytes(saved)
            elif target_path.exists():
                target_path.unlink(missing_ok=True)
        if not after.checked:
            # cargo absent / failed → not checked (never a false fail).
            features["syntax_checked"] = False
            features["syntax_passed"] = True
            return False, True
        # Baseline: marker-blanked original (one side kept so it's valid TOML),
        # cargo-checked, then restored. TOML comments use ``#``, which is the
        # default blanking prefix.
        try:
            target_path.write_text(
                _blank_markers_one_side(original), encoding="utf-8"
            )
            baseline = runner._check_cargo(_blank_markers_one_side(original), path, repo_root)
        finally:
            if saved is not None:
                target_path.write_bytes(saved)
            elif target_path.exists():
                target_path.unlink(missing_ok=True)
        features["syntax_checked"] = True
        new_errors = compute_diagnostic_delta(
            [d.message for d in baseline.errors],
            [d.message for d in after.errors],
        )
        syntax_ok = len(new_errors) == 0
        features["syntax_passed"] = syntax_ok
        features["syntax_tool"] = "cargo"
        features["syntax_new_error_count"] = len(new_errors)
        if new_errors and self.config.require_syntax_if_supported:
            msg = "; ".join(m[:80] for m in new_errors[:3])
            hard.append(
                VerificationFailure(
                    validator="syntax",
                    severity="error",
                    message=f"cargo check: {len(new_errors)} new error(s): {msg}",
                    detail={
                        "new_errors": new_errors[:5],
                        "tool": "cargo",
                        "manifest": True,
                    },
                )
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
        target_path = Path(repo_root) / path
        saved = target_path.read_bytes() if target_path.exists() else None
        try:
            # After state: write the resolved file, run clippy.
            target_path.write_text(whole, encoding="utf-8")
            after = lsp_mod.run_clippy(
                repo_root, cargo_path=self.config.cargo_path
            )
        finally:
            # Restore the pre-check worktree state immediately; the orchestrator
            # writes the final buffer later iff validation passes.
            if saved is not None:
                target_path.write_bytes(saved)
            elif target_path.exists():
                # saved was None (file didn't exist) → remove what we created.
                target_path.unlink(missing_ok=True)
        if not after.checked:
            features["clippy_checked"] = False
            return
        features["clippy_checked"] = True
        # Baseline: temporarily write the marker-blanked (one-side) original,
        # run clippy, then restore the saved worktree state.
        try:
            target_path.write_text(_blank_markers_one_side(original, "rust"), encoding="utf-8")
            baseline = lsp_mod.run_clippy(
                repo_root, cargo_path=self.config.cargo_path
            )
        finally:
            if saved is not None:
                target_path.write_bytes(saved)
            elif target_path.exists():
                target_path.unlink(missing_ok=True)
        baseline_msgs = (
            [d.message for d in baseline.diagnostics] if baseline.checked else []
        )
        new_findings = compute_diagnostic_delta(
            baseline_msgs, [d.message for d in after.diagnostics]
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
    ) -> None:
        """Reject a merge that defines the same name twice in one scope.

        The "duplicate block" failure shape a small model produces when it
        concatenates both sides' versions of a class/struct/function instead of
        merging them: both sides' content is present (so BothSidesRepresented
        and the token-set validators pass), just defined twice. This is almost
        always a wrong merge — a deliberate redefinition is rare in a conflict
        region — so severity is ``error`` and feeds the whole-file repair loop.

        Python uses stdlib ``ast`` (catches classes/functions AND bare
        module-level assignments like ``FEATURE_FLAGS = {...}`` that
        tree-sitter's enumerate_entities intentionally skips). Rust reuses
        ``structural.duplicate_definitions`` (tree-sitter, lazy). Other
        languages / no language: no-op. Degrades to a silent pass on any parse
        gap (a missing grammar or a syntax error — the latter is the syntax
        check's failure to report, not this one's).
        """
        features.setdefault("duplicate_definition_checked", False)
        features.setdefault("duplicate_definition_count", 0)
        if language == "python":
            dupes = _py_duplicate_definitions(whole)
        elif language == "rust":
            try:
                from capybase.adapters import structural
            except Exception:  # noqa: BLE001
                return
            if not structural.is_available("rust"):
                return
            dupes = structural.duplicate_definitions(whole, "rust")
        else:
            return
        if dupes is None:
            # Parse failed (Python) or tree-sitter couldn't parse (Rust): the
            # syntax check owns reporting that. Record not-checked and stop.
            return
        features["duplicate_definition_checked"] = True
        features["duplicate_definition_count"] = len(dupes)
        for kind, name, rows in dupes:
            # The leading row is the FIRST definition; the message leads with
            # the last duplicate's line so repair attribution (which parses
            # "line N" from the message) lands on the offending (duplicate)
            # occurrence, not the legitimate original.
            loc = rows[-1]
            where = ", ".join(str(r) for r in rows)
            hard.append(
                VerificationFailure(
                    validator="duplicate_definition",
                    severity="error",
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
        if not after.checked:
            features["lsp_checked"] = False
            features["lsp_error_count"] = 0
            features["lsp_new_error_count"] = 0
            return
        features["lsp_checked"] = True
        features["lsp_error_count"] = after.error_count
        # New errors = after errors not present in baseline (by message), via the
        # shared no-worse-than-before delta (#7).
        new_errors = compute_diagnostic_delta(
            [d.message for d in baseline.errors],
            [d.message for d in after.errors],
        )
        features["lsp_new_error_count"] = len(new_errors)
        if new_errors:
            msg = "; ".join(m[:80] for m in new_errors[:3])
            hard.append(
                VerificationFailure(
                    validator="lsp_diagnostics",
                    severity="error",
                    message=f"LSP introduced {len(new_errors)} new error(s): {msg}",
                    detail={
                        "new_errors": new_errors[:5],
                        "tool": after.tool,
                    },
                )
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
    """Replace conflict-marker lines with comments so the baseline parses.

    The pre-conflict ``original`` (the worktree with raw markers) isn't valid
    Python/Rust. For the LSP/cargo/clippy baseline we only need it to parse so
    we can collect pre-existing diagnostics outside the conflict — blanking
    each marker line to a COMMENT preserves line numbers and lets the parser
    recover. The comment syntax is language-appropriate: ``//`` for Rust (a
    bare ``#`` is an attribute, not a comment, and breaks the Rust parse),
    ``#`` otherwise (Python). When ``language`` is None, defaults to ``#``
    (the original behavior, kept for any direct callers).
    """
    from capybase.adapters.language import adapter_for
    comment = adapter_for(language).comment_prefix
    out = []
    for line in text.split("\n"):
        if line.startswith(("<<<<<<<", "=======", ">>>>>>>")):
            out.append(f"{comment} conflict-marker")
        else:
            out.append(line)
    return "\n".join(out)


def _blank_markers_one_side(text: str, language: str | None = None) -> str:
    """Blank conflict blocks to ONE side so the baseline parses as valid code.

    ``_blank_markers`` keeps BOTH sides' content (just marking the fences),
    which produces a duplicate-definition compile error in Rust (two ``fn
    new()`` bodies) that masks the real baseline lints — so any pre-existing
    clippy finding suppressed by that error reads as "new". For a quality
    baseline (clippy) we need valid code: keep the upstream (first) side's
    lines and comment out the replayed (second) side's lines. Line numbers are
    NOT preserved here (the second side is dropped), but clippy findings are
    compared by message, not line — so position shifts don't matter.
    """
    comment = "//" if language == "rust" else "#"
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
            # Drop the second side (comment it out) so only one definition remains.
            out.append(f"{comment} {line}" if line.strip() else line)
            continue
        out.append(line)
    return "\n".join(out)


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

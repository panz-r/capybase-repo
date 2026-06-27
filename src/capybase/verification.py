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
    enable_lsp_diagnostics: bool = False
    pyright_path: str = "pyright"
    rust_analyzer_path: str = "rust-analyzer"
    cargo_path: str = "cargo"
    # Rust compile floor (mirrors config.ValidationConfig; the live flags).
    rustc_path: str = "rustc"
    rust_edition: str = ""
    lsp_baseline_strict: bool = True
    enable_shadow_tests: bool = False
    # Verifier-model critic (mirrors config.ValidationConfig; the live flags).
    enable_verifier_model: bool = False
    verifier_severity: str = "warning"
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

    def __init__(self, client: object, model_name: str = "", *, json_mode: bool = True) -> None:
        # ``client`` is the same LLMClient the resolution engine uses. Typed as
        # ``object`` to avoid an import cycle (adapters → ... → verification);
        # it only needs a ``complete`` method.
        self.client = client
        self.model_name = model_name
        self.json_mode = json_mode

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
            from capybase.adapters.lsp import _has_cargo_manifest, _resolve

            used_cargo = False
            if _has_cargo_manifest(repo_root) and _resolve(self.config.cargo_path):
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
        features["syntax_checked"] = features.get("syntax_checked", syntax_checked)
        features["syntax_passed"] = features.get("syntax_passed", syntax_ok)

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
        The baseline is the pre-conflict ``original`` with markers blanked to
        comments so it parses; we compare error *messages* between baseline and
        the resolved file.

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
        # Baseline: the original file with conflict markers blanked so it parses.
        baseline_src = _blank_markers(original)
        baseline = runner.check(baseline_src, path=path, repo_root=repo_root)
        after = runner.check(whole, path=path, repo_root=repo_root)
        if not after.checked:
            # cargo absent or failed to run → not checked (never a false fail).
            features["syntax_checked"] = False
            features["syntax_passed"] = True
            return False
        features["syntax_checked"] = True
        # New errors = after errors absent from the baseline (by message).
        baseline_msgs = {d.message for d in baseline.errors}
        new_errors = [d for d in after.errors if d.message not in baseline_msgs]
        syntax_ok = len(new_errors) == 0
        features["syntax_passed"] = syntax_ok
        features["syntax_tool"] = "cargo"
        features["syntax_new_error_count"] = len(new_errors)
        if new_errors and self.config.require_syntax_if_supported:
            msg = "; ".join(d.message[:80] for d in new_errors[:3])
            hard.append(
                VerificationFailure(
                    validator="syntax",
                    severity="error",
                    message=f"cargo check: {len(new_errors)} new error(s): {msg}",
                    detail={
                        "new_errors": [d.message for d in new_errors[:5]],
                        "tool": "cargo",
                    },
                )
            )
        return True

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
            ok, rc, outpath = _run_rust_shadow_test(target, repo_root)
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
        "referenced_symbol_dropped": cfg.reject_if_drops_referenced_symbol,
        "needs_human": cfg.reject_if_model_needs_human,
        "syntax": cfg.require_syntax_if_supported,
        "verifier_model": cfg.enable_verifier_model,
        "policy_gate": cfg.enable_policy_gate,
        "code_smell": cfg.enable_code_smell_checks,
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

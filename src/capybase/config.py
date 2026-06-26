"""Typed runtime configuration loaded from capybase.toml.

Packaging metadata lives in pyproject.toml; this module owns the *runtime*
config surface ([model], [policy], [tests], [validation], [journal],
[future]). The `[future]` section documents planned seams and is parsed but
intentionally inert in the MVP.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class ModelConfig(BaseModel):
    provider: Literal["openai_compatible"] = "openai_compatible"
    base_url: str = "http://127.0.0.1:8080/v1"
    api_key: str = "sk-local"
    model: str = "vibethink"
    temperature: float = 0.2
    samples: int = 1
    # Difficulty-aware sample allocation (survey §4 UAB-lite): when routing is
    # enabled and a unit classifies as "complex", draw this many samples instead
    # of the base ``samples``. Concentrates test-time compute where a 3B model
    # genuinely struggles (multi-hunk files, large enclosing AST nodes) without a
    # cross-unit scheduler. 0 (default) = disabled: complex units use ``samples``
    # as today, so behavior is unchanged. Difficulty is the viable signal here
    # because it is computed BEFORE any LLM call (unlike mean_token_entropy, which
    # is post-generation and can't drive the first sample count).
    samples_complex: int = 0
    # Self-consistency: when samples > 1 and future.enable_self_consistency is
    # on, candidates are clustered by normalized text and the majority wins.
    # Below this agreement fraction the merge is flagged low-confidence (the
    # risk engine can treat it as a retry/escalate signal).
    consensus_min_agreement: float = 0.4
    # Two-pass prompting (Step 2): first request extracts semantic intents
    # only (small, fast), second request generates code conditioned on those
    # intents. Only activates when samples > 1 (the multi-candidate path) to
    # avoid doubling requests for the single-sample case.
    two_pass: bool = False
    # PlanSearch (survey §1): in the two-pass path, sample MULTIPLE distinct
    # NL resolution plans (Pass 1) and generate one code candidate per plan
    # (Pass 2), instead of one plan → N code samples. Adds diversity on the
    # planning axis — orthogonal to temperature and prompt-variant sampling.
    # Only engages when two_pass AND samples > 1. Defaults off; falls back to
    # the single-intent path if the plan-search call fails or yields <2 plans.
    plan_search: bool = False
    # Raised temperature for the diverse multi-sampling pass (distinct from
    # the low `temperature` used for focused retries). Higher temp → more
    # diverse candidates → better consensus signal.
    sampling_temperature: float = 0.7
    # Draw samples concurrently in a thread pool (each is a blocking HTTP call).
    # Safe because the LLM adapter is stateless per-call.
    parallel_samples: bool = True
    # Parameter-diversity portfolio (survey §4.1): when sampling N>1, split the
    # samples across the high sampling_temperature (exploratory) and the low
    # base temperature (conservative). Raises the odds that at least one sample
    # is both valid and distinct on a 3B model. Bypasses the server-side batched
    # n path (which forces one temperature) to use N separate requests. Off by
    # default; for N=1 it is a no-op.
    diverse_sampling: bool = False
    # Prompt-variant sampling (survey §4 Code Roulette): when on AND samples > 1
    # AND this is a fresh resolve (no CEGIS retry/repair), draw the samples across
    # semantically-equivalent resolve-prompt phrasings instead of identical prompts
    # at varied temperatures. A candidate stable across prompt variants is a
    # stronger correctness signal, and the existing consensus + rank-order
    # validation already selects the largest stable cluster. Defaults off so
    # behavior is unchanged; retry/repair paths never use variants (they must stay
    # single-template for reproducible counterexample feedback).
    prompt_variants: bool = False
    # Reasoning models emit long <think> chains before answering; 2048 starves
    # them. 8192 leaves headroom for reasoning + the final JSON answer.
    max_tokens: int = 8192
    request_timeout_seconds: int = 600
    # Hard wall-clock deadline for ONE generation attempt (across all streamed
    # tokens). Distinct from request_timeout_seconds (per-read socket timeout):
    # a generation that trickles data forever without finishing must still abort
    # and become a retryable failure. Real completions on a 3B reasoning model
    # take ~30-90s; this gives headroom without hanging for minutes on a stall.
    generation_timeout_seconds: int = 180
    # TECP token-entropy capture (survey §4.1): when on, requests per-token
    # logprobs from the API and reduces them to a scalar mean token-entropy
    # (mean negative log-probability) carried on each candidate. This is the
    # logit-free, black-box uncertainty signal the conformal "flywheel" learns
    # from — never the model weights. Defaults off so deployments that don't
    # need it pay no request-shape cost and see no behavior change; the API
    # simply omits ``logprobs`` from the request body when off.
    capture_token_entropy: bool = False


class PolicyConfig(BaseModel):
    supported_conflict_types: list[str] = Field(default_factory=lambda: ["UU"])
    supported_file_kinds: list[str] = Field(default_factory=lambda: ["text"])
    max_retries_per_unit: int = 2
    allow_skip: bool = False
    allow_delete_conflicted_file: bool = False
    stage_only_validated_paths: bool = True
    context_lines: int = 15


class TestsConfig(BaseModel):
    pre_continue: str | None = "pytest"
    final: str | None = "pytest"
    timeout_seconds: int = 300
    required: bool = True


class PolicyRule(BaseModel):
    """One deterministic safety rule for the VeriGuard-style policy gate.

    The gate statically extracts import/call facts from a candidate patch's
    resolved text (stdlib ``ast``, Python only) and evaluates each rule. A rule
    ``forbid_import`` matches when ``pattern`` is a prefix of any imported
    module path (so ``"subprocess"`` catches ``subprocess.run`` usage too, since
    importing ``subprocess`` is the precondition); ``forbid_call`` matches when
    ``pattern`` is a prefix of any call target (so ``"eval"`` catches the
    builtin and ``"os.system"`` catches the dotted call). All deterministic at
    runtime — no LLM, no execution (survey §4 VeriGuard).
    """

    name: str
    kind: Literal["forbid_import", "forbid_call"]
    pattern: str
    severity: Literal["error", "warning"] = "error"
    reason: str = ""


class ValidationConfig(BaseModel):
    require_no_markers: bool = True
    require_exact_splice_scope: bool = True
    require_syntax_if_supported: bool = True
    reject_if_copies_one_side: bool = True
    reject_if_model_needs_human: bool = True
    # Phase B: validate the fully-spliced file (with *all* units resolved)
    # after per-unit validation passes. This catches cross-unit errors that
    # per-unit checks structurally cannot — leaked markers from sibling
    # blocks, syntax errors that only arise when two resolutions are
    # juxtaposed, duplicate symbols across hunks. Meaningful even for
    # single-unit files; disable only for non-code where it's moot.
    require_whole_file_validation: bool = True
    # AST preservation (requires tree-sitter): prove that nodes OUTSIDE the
    # conflict span are structurally unchanged after splicing. Catches a model
    # silently rewriting or deleting unchanged code that the line-level
    # ExactSpliceScope check misses (it only guards line boundaries). When the
    # grammar is absent this validator is inert.
    require_ast_preservation: bool = True
    # LSP / type-checker diagnostics (requires pyright/rust-analyzer): reject a
    # candidate that introduces NEW type or compilation errors not present in
    # the pre-conflict baseline. Runs in Phase B on the fully-spliced file.
    # Inert when the tool is absent.
    enable_lsp_diagnostics: bool = False
    pyright_path: str = "pyright"
    rust_analyzer_path: str = "rust-analyzer"
    cargo_path: str = "cargo"
    lsp_baseline_strict: bool = True
    # Shadow tests: if a tests/test_<module>.py exists for the modified file,
    # run it before declaring success (best-effort, Phase B).
    enable_shadow_tests: bool = False
    # Verifier-model critic (surveys §1/§5 Proposer-Critic; the reserved
    # `enable_verifier_model` seam): an LLM judge that checks the resolved text
    # preserves BOTH sides' semantic intent — the one failure mode the syntactic
    # validators (markers, splice scope, AST, LSP) are structurally blind to:
    # a merge that parses cleanly but silently drops a side's intent. Uses the
    # same black-box API client already in the orchestrator; no model is trained
    # or hosted. When off (default) the validator is inert and makes no calls.
    enable_verifier_model: bool = False
    # Severity of a critic disagreement: "warning" (default — bias toward
    # retry/escalate but don't hard-reject a syntactically-valid merge) or
    # "error" (strict — treat a dropped-intent verdict as a hard failure).
    verifier_severity: str = "warning"
    # VeriGuard-style deterministic policy gate (survey §4): statically extract
    # import/call facts from each candidate's resolved text and evaluate them
    # against ``policy_rules``. The ONLY check that inspects WHAT a patch
    # introduces (every other validator is syntactic/structural) — catches a
    # clean-but-unsafe merge (e.g. adds subprocess to api/). Fully deterministic
    # at runtime (stdlib ast, no LLM, no execution), Python-only, graceful no-op
    # for other languages. When off OR no rules configured, the gate is inert.
    enable_policy_gate: bool = False
    policy_rules: list[PolicyRule] = Field(default_factory=list)
    # LLM code-smell checks (survey §7): statically detect smells common in
    # LLM-generated code via stdlib ast — NaN comparison (x == np.nan, always
    # False), pandas chain indexing (df[a][b], ambiguous), uncontrolled
    # randomness (random.* with no seed). A cheap pre-test quality filter,
    # deterministic (no LLM, no execution), Python-only, graceful no-op
    # otherwise. Only the AST-clean smells are implemented; dataflow smells
    # (missing scaling, data leakage, implicit hyperparameters) need richer
    # analysis and are deferred. When off (default) the checker is inert.
    enable_code_smell_checks: bool = False
    code_smell_severity: str = "warning"


class JournalConfig(BaseModel):
    enabled: bool = True
    store_prompts: bool = True
    store_raw_responses: bool = True
    store_snapshots: bool = True
    store_candidates: bool = True
    store_validations: bool = True


class FutureConfig(BaseModel):
    """Documents planned seams. Inert in the MVP — parsed, never read by the
    core loop. Provided so config files written today stay valid tomorrow."""

    enable_self_consistency: bool = False
    enable_rag: bool = False
    enable_structural_context: bool = False
    enable_verifier_model: bool = False
    enable_mutation_testing: bool = False


class StructuralConfig(BaseModel):
    """Tree-sitter AST parsing for structural context + preservation checks.

    When enabled and the ``structural`` optional deps are installed, the
    conflict extractor populates ``ConflictUnit.structural_metadata`` with the
    lowest enclosing AST node (e.g. the specific ``def``/``impl``) so the
    resolver and validators see a logical block rather than an arbitrary line
    window. All tree-sitter imports are lazy; when the lib is absent or parsing
    fails, capybase silently degrades to the line-window behavior.
    """

    enabled: bool = False
    languages: list[str] = Field(default_factory=lambda: ["python", "rust"])
    max_enclosing_node_lines: int = 60
    cross_file_slice: bool = True
    slice_search_globs: list[str] = Field(
        default_factory=lambda: ["**/*.py", "**/*.rs"]
    )
    # Use the enclosing AST node as primary_text instead of the line window.
    # When the node fits within max_enclosing_node_lines, the model sees the
    # full logical block (def/impl) rather than an arbitrary text slice.
    use_enclosing_as_primary: bool = True
    # Strip comment lines, docstrings, and blank runs from the context shown
    # to the model. Reduces noise for a 3B model prone to "lost in the middle."
    # Does NOT alter resolved_text — the model still emits exact indentation.
    canonicalize_context: bool = True
    # Refine conflict boundaries with `git merge-file --diff3` to get the
    # tightest possible marker span (git may auto-resolve adjacent lines).
    refine_with_diff3: bool = True


class MemoryConfig(BaseModel):
    """RAG experience replay: retrieve past successful merges as few-shot.

    The journal already stores every prompt/response/candidate/validation
    triple. The memory layer distills accepted resolutions into a labeled
    corpus of ``HistoricalExample`` records, retrieves the most similar past
    merges for a new conflict, and injects them into the prompt as dynamic
    few-shot demonstrations. Disabled by default; activates
    ``future.enable_rag``.
    """

    enabled: bool = False
    store_path: str = ".rebase-agent/memory/experiences.jsonl"
    retriever: str = "lexical"  # "lexical" (BM25) or "embedding" (future)
    retriever_k: int = 3
    # Minimum experiences before retrieval is attempted (avoid noisy few-shot
    # from a near-empty corpus).
    min_examples_for_retrieval: int = 3


class CalibrationConfig(BaseModel):
    """Calibrated risk routing: replace the rules threshold with a learned one.

    Once the experience store accumulates enough labeled outcomes, a lightweight
    classifier (logistic regression / isotonic) is fitted offline over
    ``VerificationResult.features`` and predicts the probability a merge will
    fail. The calibrated engine produces the same ``RiskDecision`` shape but
    overrides the accept/escalate boundary using the fitted threshold. Disabled
    by default until enough data is collected.
    """

    enabled: bool = False
    model_path: str = ".rebase-agent/memory/calibration.json"
    escalate_threshold: float = 0.7
    min_examples_for_calibration: int = 50
    # Consensus entropy above this → escalate (high-entropy splits mean no
    # candidate is trustworthy). 0=unanimous, 1=maximally split. Set high
    # (0.8) because even a 2-of-3 majority produces non-trivial entropy; we
    # only want to escalate when samples are *maximally* split.
    entropy_escalate_threshold: float = 0.8
    # Conformal prediction coverage (1-alpha). When a conformal model is fitted,
    # the p-value threshold guarantees this coverage of accepted merges.
    conformal_alpha: float = 0.1


class RoutingConfig(BaseModel):
    """Difficulty-aware routing (survey §6.1, ICoT/RoutingGen pattern).

    Classifies a conflict as ``simple`` or ``complex`` *before* any LLM call
    using structural signals already on the ConflictUnit. Simple conflicts (a
    single isolated hunk) take a fast path (one low-temp sample, no two-pass,
    no consensus); complex ones (multi-hunk, large nodes) get the full
    test-time pipeline. Concentrates compute where a 3B model struggles and
    cuts ~half the tokens on easy cases. Disabled by default (opt-in).
    """

    enabled: bool = False
    # A file with more than one conflict hunk → complex.
    complex_if_sibling_count_gt: int = 0
    # Enclosing AST node larger than this (lines) → complex.
    max_simple_node_lines: int = 40
    # Combined base+current+replayed side text longer than this (chars) → complex.
    max_simple_side_chars: int = 1200


class Config(BaseModel):
    model: ModelConfig = Field(default_factory=ModelConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    tests: TestsConfig = Field(default_factory=TestsConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    journal: JournalConfig = Field(default_factory=JournalConfig)
    structural: StructuralConfig = Field(default_factory=StructuralConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    calibration: CalibrationConfig = Field(default_factory=CalibrationConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    future: FutureConfig = Field(default_factory=FutureConfig)
    source_path: str | None = None

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        """Load config from ``path``. If ``path`` is None, search for
        ``capybase.toml`` in the current directory, then fall back to built-in
        defaults."""
        resolved = _resolve_config_path(path)
        if resolved is None:
            cfg = cls()
            return cfg
        with open(resolved, "rb") as fh:
            data = tomllib.load(fh)
        cfg = cls.model_validate(data)
        cfg.source_path = str(resolved)
        return cfg


def _resolve_config_path(path: str | Path | None) -> Path | None:
    if path is not None:
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"config file not found: {p}")
        return p
    for candidate in (Path("capybase.toml"), Path("capybase.local.toml")):
        if candidate.is_file():
            return candidate
    return None

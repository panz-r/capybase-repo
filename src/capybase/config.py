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
    # Raised temperature for the diverse multi-sampling pass (distinct from
    # the low `temperature` used for focused retries). Higher temp → more
    # diverse candidates → better consensus signal.
    sampling_temperature: float = 0.7
    # Draw samples concurrently in a thread pool (each is a blocking HTTP call).
    # Safe because the LLM adapter is stateless per-call.
    parallel_samples: bool = True
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


class Config(BaseModel):
    model: ModelConfig = Field(default_factory=ModelConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    tests: TestsConfig = Field(default_factory=TestsConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    journal: JournalConfig = Field(default_factory=JournalConfig)
    structural: StructuralConfig = Field(default_factory=StructuralConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    calibration: CalibrationConfig = Field(default_factory=CalibrationConfig)
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

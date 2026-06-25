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


class Config(BaseModel):
    model: ModelConfig = Field(default_factory=ModelConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    tests: TestsConfig = Field(default_factory=TestsConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    journal: JournalConfig = Field(default_factory=JournalConfig)
    structural: StructuralConfig = Field(default_factory=StructuralConfig)
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

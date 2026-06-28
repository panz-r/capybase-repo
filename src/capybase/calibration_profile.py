"""Model profile: calibrated runtime settings for a specific model.

Distinct from :mod:`capybase.calibration`, which fits a *risk* classifier over
validator features. This module stores the model-capability profile produced by
``capybase calibrate``: the runtime knobs (``max_tokens``, ``json_mode``,
``capture_token_entropy``, ``generation_timeout_seconds``) that depend on which
model is behind the OpenAI-compatible endpoint.

Design contract (``Profile wins``):

- A stored profile **overrides** the corresponding ``ModelConfig`` knobs at
  runtime — but ONLY when its ``model`` name matches the active config's model.
- A name mismatch means the profile was fit for a different model and is
  ignored: the user is expected to ``capybase recalibrate`` for the new one.
- The overlay is reversible: deleting the profile file restores pure-TOML
  behavior. Missing or corrupt profiles are a no-op (never crash resolution).
- The overlay touches only the tuned knobs; every other ``ModelConfig`` field
  keeps its TOML/default value.

Persistence mirrors :class:`capybase.calibration.CalibrationModel`: a flat JSON
blob under ``.rebase-agent/memory/`` with graceful-absence loading.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from capybase.config import ModelConfig


@dataclass
class ModelProfile:
    """Calibrated runtime settings for one model, fit by ``capybase calibrate``.

    Every field is a ``ModelConfig`` knob whose ideal value depends on the
    model/server rather than on user preference. ``apply_profile`` overlays
    exactly these onto a ``ModelConfig``.
    """

    model: str
    max_tokens: int
    json_mode: bool
    capture_token_entropy: bool
    generation_timeout_seconds: int
    # Model context window (input token budget), discovered from the server's
    # /v1/models endpoint (its ``context_length``). 0 = unknown/disabled → the
    # resolve prompt is sent unbounded (no trimming), the backward-compatible
    # default. When set, the prompt is capped to this window (see
    # resolution_engine token-window enforcement).
    context_window: int = 0
    # Mechanism choices (empirically A/B-selected by probe_mechanisms against
    # the blessed corpus). Defaults below = current built-in behavior (samples=1,
    # all mechanisms off), so a profile that omits them (or an older profile) is
    # fully backward-compatible: nothing changes unless calibration turned it on.
    samples: int = 1
    two_pass: bool = False
    plan_search: bool = False
    prompt_variants: bool = False
    diverse_sampling: bool = False
    enable_self_consistency: bool = False
    # Capability flags (calibrate-detected, not mechanism A/B). These don't overlay
    # ModelConfig; the orchestrator reads them to enable endpoint-dependent features.
    enable_embedding_rag: bool = False  # /v1/embeddings endpoint supports embeddings
    # Calibrated embeddings threshold (written by ``calibrate-embeddings``). The
    # value the EmbeddingRetriever uses as its min_similarity floor at runtime —
    # replacing the 0.35 class-constant guess with a model-specific, statistically
    # derived constant (the quantile-gap between related and unrelated scores).
    embedding_min_similarity: float = 0.35
    # The full embeddings-calibration envelope (the three threshold estimates +
    # measured score distributions), for transparency and manual re-tuning. Empty
    # until ``calibrate-embeddings`` is run.
    embedding_calibration: dict[str, Any] = field(default_factory=dict)
    # Hybrid-retrieval fusion method, read when the retriever is "hybrid"
    # (survey §4): "rrf" (default) or "dbsf". Empty/unset → "rrf" at runtime.
    fusion_method: str = ""
    avg_latency_ms: float = 0.0  # observed mean generation latency, for diagnostics
    probed_at: str = ""  # ISO-8601 timestamp
    capybase_version: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # ``notes`` may be empty; keep it so the schema is stable.
        d["notes"] = list(d.get("notes") or [])
        # ``embedding_calibration`` must serialize cleanly; coerce defensively.
        d["embedding_calibration"] = _coerce_calibration(d.get("embedding_calibration"))
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ModelProfile":
        notes = d.get("notes") or []
        if not isinstance(notes, list):
            notes = [str(notes)]
        return cls(
            model=str(d.get("model", "")),
            max_tokens=int(d.get("max_tokens", 0)),
            json_mode=bool(d.get("json_mode", True)),
            capture_token_entropy=bool(d.get("capture_token_entropy", False)),
            generation_timeout_seconds=int(d.get("generation_timeout_seconds", 60)),
            context_window=int(d.get("context_window", 0)),
            samples=int(d.get("samples", 1)),
            two_pass=bool(d.get("two_pass", False)),
            plan_search=bool(d.get("plan_search", False)),
            prompt_variants=bool(d.get("prompt_variants", False)),
            diverse_sampling=bool(d.get("diverse_sampling", False)),
            enable_self_consistency=bool(d.get("enable_self_consistency", False)),
            enable_embedding_rag=bool(d.get("enable_embedding_rag", False)),
            embedding_min_similarity=float(d.get("embedding_min_similarity", 0.35)),
            embedding_calibration=_coerce_calibration(d.get("embedding_calibration")),
            fusion_method=str(d.get("fusion_method", "") or ""),
            avg_latency_ms=float(d.get("avg_latency_ms", 0.0)),
            probed_at=str(d.get("probed_at", "")),
            capybase_version=str(d.get("capybase_version", "")),
            notes=[str(n) for n in notes],
        )

    @classmethod
    def load(cls, path: str | Path) -> "ModelProfile | None":
        """Load a profile from JSON, or return None if absent/corrupt.

        A corrupt or partial file is treated as "no profile": resolution must
        never crash on a bad artifact. The CLI's ``calibrate`` command is the
        way to (re)write a valid one.
        """
        p = Path(path)
        if not p.is_file():
            return None
        try:
            return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, KeyError, ValueError, TypeError, AttributeError):
            return None

    def save(self, path: str | Path) -> None:
        """Write the profile as pretty JSON, creating parent dirs."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")


def _coerce_calibration(value: Any) -> dict[str, Any]:
    """Defensively coerce an embeddings-calibration envelope to a plain dict.

    The envelope is a nested dict of threshold estimates + score distributions.
    A corrupt or non-dict value yields an empty dict (graceful absence), matching
    the profile's never-crash-on-load contract. We don't recurse-validation every
    leaf — only ensure the top-level is a JSON-serializable dict.
    """
    if isinstance(value, dict):
        return dict(value)
    return {}


# Knobs that a profile is allowed to override. Centralized so ``apply_profile``
# and its journaling caller agree on the exact set. Split into the capability
# knobs (probed directly) and the mechanism choices (empirically A/B-selected).
PROFILE_KNOBS = (
    "max_tokens",
    "json_mode",
    "capture_token_entropy",
    "generation_timeout_seconds",
    "context_window",
    "samples",
    "two_pass",
    "plan_search",
    "prompt_variants",
    "diverse_sampling",
    "enable_self_consistency",
)


def apply_profile(
    model_cfg: ModelConfig, profile: "ModelProfile | None"
) -> tuple[ModelConfig, list[str]]:
    """Return a new ``ModelConfig`` with the profile's knobs overlaid.

    "Profile wins" — but ONLY when ``profile.model`` matches ``model_cfg.model``.
    On a name mismatch the profile is ignored (it was fit for a different model)
    and a warning is emitted so the user knows to ``capybase recalibrate``.

    Returns ``(new_config, overridden_knobs)`` where ``overridden_knobs`` lists
    the knob names actually changed by the overlay (empty when no profile or a
    name mismatch). Callers (the orchestrator) journal this list.
    """
    if profile is None:
        return model_cfg, []

    if profile.model != model_cfg.model:
        warnings.warn(
            f"Model profile is for {profile.model!r} but active model is "
            f"{model_cfg.model!r}; ignoring the profile. Run "
            f"`capybase recalibrate` to fit it for the current model.",
            stacklevel=2,
        )
        return model_cfg, []

    updates: dict[str, Any] = {
        "max_tokens": profile.max_tokens,
        "json_mode": profile.json_mode,
        "capture_token_entropy": profile.capture_token_entropy,
        "generation_timeout_seconds": profile.generation_timeout_seconds,
        "context_window": profile.context_window,
        "samples": profile.samples,
        "two_pass": profile.two_pass,
        "plan_search": profile.plan_search,
        "prompt_variants": profile.prompt_variants,
        "diverse_sampling": profile.diverse_sampling,
        "enable_self_consistency": profile.enable_self_consistency,
    }
    overridden = [k for k in PROFILE_KNOBS if getattr(model_cfg, k) != updates[k]]
    # ``model_config_validate`` isn't needed; pydantic re-validates on construct.
    new_cfg = model_cfg.model_copy(update=updates)
    return new_cfg, overridden


def resolve_profile_path(repo_root: str | Path, profile_path: str) -> Path:
    """Resolve a profile path relative to a repo root (mirror of
    ``ExperienceStore.for_repo``). Absolute paths pass through unchanged."""
    p = Path(profile_path)
    if not p.is_absolute():
        p = Path(repo_root) / p
    return p

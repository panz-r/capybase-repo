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
from typing import Any, TYPE_CHECKING

from capybase.config import ModelConfig
from capybase.prompt_profile import DEFAULT_PROFILE, PromptProfile

if TYPE_CHECKING:
    pass


@dataclass
class CapabilityProfile:
    """Endpoint-capability settings: what the server supports / how it behaves.

    Probed directly by ``capybase calibrate`` (max_tokens binary-search, json_mode
    / logprobs / context-window detection, latency timing). These overlay
    ``ModelConfig`` via ``apply_profile``. ``problems()`` validates the
    load-bearing knobs; an invalid capability section invalidates only THIS
    section (a bad max_tokens shouldn't discard a good retrieval calibration).
    """

    max_tokens: int
    json_mode: bool
    capture_token_entropy: bool
    generation_timeout_seconds: int
    context_window: int = 0
    avg_latency_ms: float = 0.0
    # Whether the endpoint supports /v1/embeddings. A capability flag probed by
    # calibrate, but it gates RETRIEVAL behavior (the orchestrator switches to
    # the embedding retriever when set). Lives here because it's probed, not
    # calibrated; the retrieval section consumes it.
    enable_embedding_rag: bool = False

    def problems(self) -> list[str]:
        probs: list[str] = []
        if self.max_tokens <= 0:
            probs.append(f"max_tokens={self.max_tokens} (must be > 0)")
        if self.generation_timeout_seconds <= 0:
            probs.append(
                f"generation_timeout_seconds={self.generation_timeout_seconds} "
                f"(must be > 0)"
            )
        if self.context_window < 0:
            probs.append(f"context_window={self.context_window} (must be >= 0)")
        return probs


@dataclass
class QualityProfile:
    """Resolution-mechanism choices: empirically A/B-selected by calibrate.

    ``samples`` / ``two_pass`` / ``plan_search`` / ``prompt_variants`` /
    ``diverse_sampling`` / ``enable_self_consistency``. These overlay
    ``ModelConfig``. Defaults = current built-in behavior (samples=1, all
    mechanisms off), so a profile that omits them is fully backward-compatible.
    """

    samples: int = 1
    two_pass: bool = False
    plan_search: bool = False
    prompt_variants: bool = False
    diverse_sampling: bool = False
    enable_self_consistency: bool = False

    def problems(self) -> list[str]:
        if self.samples < 1:
            return [f"samples={self.samples} (must be >= 1)"]
        return []


@dataclass
class RetrievalProfile:
    """Retrieval (RAG) calibration: written by ``capybase calibrate-embeddings``.

    These do NOT overlay ``ModelConfig``; the orchestrator threads them into
    ``config.memory.*``. Independent from capability/quality so re-tuning the
    embedding floor never disturbs the LLM mechanism settings, and vice versa.
    """

    embedding_min_similarity: float = 0.35
    embedding_calibration: dict[str, Any] = field(default_factory=dict)
    fusion_method: str = ""

    def problems(self) -> list[str]:
        return []  # retrieval has no load-bearing knobs that break resolution


@dataclass
class PromptProfileSection:
    """Prompt-rendering calibration: written by ``capybase calibrate``.

    Wraps a :class:`~capybase.prompt_profile.PromptProfile` (output layout,
    history framing, instruction position, outline mode, example cap). Does NOT
    overlay ``ModelConfig`` knobs — the orchestrator applies it via
    :func:`~capybase.prompt_profile.set_active_profile` at init (a process
    global, not a config field). Independent from capability/quality/retrieval
    so re-running ``calibrate`` never disturbs the embedding floor, etc.

    A bad value degrades to the default (the prompt profile's ``from_dict``
    ignores unknown values), so there are no load-bearing knobs that can break
    resolution — ``problems()`` is always empty, matching the retrieval section.
    """

    profile: PromptProfile = field(default_factory=lambda: DEFAULT_PROFILE)

    def problems(self) -> list[str]:
        return []  # prompt rendering has no load-bearing knobs that break resolution


@dataclass
class TaskOverridesProfile:
    """Per-task-family profile overrides (feedback §4 task families).

    Holds a ``{task_type → {samples: int, prompt_profile: dict}}`` mapping. When
    the orchestrator resolves a conflict whose ``task_type`` matches a key here,
    it applies the override's ``samples`` + ``PromptProfile`` instead of the
    global profile. Falls back to the global profile when no override exists
    (the common case). Advisory — ``problems()`` always returns ``[]``.
    """

    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)

    def problems(self) -> list[str]:
        return []

    def get(self, task_type: str) -> dict[str, Any] | None:
        """The override for ``task_type``, or None when no override exists."""
        return self.overrides.get(task_type)


@dataclass
class SafetyProfile:
    """Safety / escalation policy calibration (feedback §2.1).

    Carries the retry budgets and escalation thresholds so they're profile-
    calibrated (per-model) rather than config-only. The orchestrator overlays
    these onto ``PolicyConfig`` when the model name matches, before constructing
    the ``RiskEngine``. Defaults match the built-in ``PolicyConfig`` values so a
    profile that omits the section is fully backward-compatible.
    """

    max_retries_per_unit: int = 2
    max_critic_retries_per_unit: int = 0   # 0 = mirror max_retries (RiskEngine convention)
    max_recovery_retries_per_unit: int = 1
    critic_confidence_escalate_threshold: float = 0.8
    escalation_on_parse_failure: bool = True

    def problems(self) -> list[str]:
        return []  # advisory; a bad value degrades to the default

    @property
    def is_default(self) -> bool:
        """True when every field matches the default (no override to apply)."""
        return (
            self.max_retries_per_unit == 2
            and self.max_critic_retries_per_unit == 0
            and self.max_recovery_retries_per_unit == 1
            and self.critic_confidence_escalate_threshold == 0.8
            and self.escalation_on_parse_failure is True
        )


class ModelProfile:
    """Calibrated runtime settings for one model — a composite of four sections.

    ``capability`` (endpoint probing), ``quality`` (mechanism A/B selection),
    ``retrieval`` (RAG calibration), and ``prompt`` (prompt-rendering A/B
    selection) are logically independent: each is produced by a different
    command (``calibrate`` vs ``calibrate-embeddings``), validated separately,
    and may be invalidated without discarding the others. The four are persisted
    together in one file (backward-compatible with the legacy flat format) but
    each section carries its own ``model``-match gate and validation, so
    ``calibrate-embeddings`` rewrites only the retrieval section and never
    disturbs the capability/quality/prompt knobs it didn't re-probe.


    Construction accepts EITHER the section form (``capability=...,
    quality=..., retrieval=..., prompt=...``) OR the legacy flat-kwarg form
    (``max_tokens=..., samples=..., embedding_min_similarity=...``) so existing
    probe/CLI call sites keep working unchanged. ``from_dict`` is the canonical
    loader; direct construction is for the calibrate commands.
    """

    def __init__(
        self,
        model: str = "",
        *,
        capability: CapabilityProfile | None = None,
        quality: QualityProfile | None = None,
        retrieval: RetrievalProfile | None = None,
        prompt: PromptProfileSection | None = None,
        task_overrides: TaskOverridesProfile | None = None,
        safety: SafetyProfile | None = None,
        probed_at: str = "",
        capybase_version: str = "",
        notes: list[str] | None = None,
        # Legacy flat-kwarg form (accepted for source-compat with probe/CLI sites):
        max_tokens: int = 0,
        json_mode: bool = True,
        capture_token_entropy: bool = False,
        generation_timeout_seconds: int = 60,
        context_window: int = 0,
        avg_latency_ms: float = 0.0,
        enable_embedding_rag: bool = False,
        samples: int = 1,
        two_pass: bool = False,
        plan_search: bool = False,
        prompt_variants: bool = False,
        diverse_sampling: bool = False,
        enable_self_consistency: bool = False,
        embedding_min_similarity: float = 0.35,
        embedding_calibration: dict[str, Any] | None = None,
        fusion_method: str = "",
    ) -> None:
        self.model = model
        self.capability = capability if capability is not None else CapabilityProfile(
            max_tokens=max_tokens, json_mode=json_mode,
            capture_token_entropy=capture_token_entropy,
            generation_timeout_seconds=generation_timeout_seconds,
            context_window=context_window, avg_latency_ms=avg_latency_ms,
            enable_embedding_rag=enable_embedding_rag,
        )
        self.quality = quality if quality is not None else QualityProfile(
            samples=samples, two_pass=two_pass, plan_search=plan_search,
            prompt_variants=prompt_variants, diverse_sampling=diverse_sampling,
            enable_self_consistency=enable_self_consistency,
        )
        self.retrieval = retrieval if retrieval is not None else RetrievalProfile(
            embedding_min_similarity=embedding_min_similarity,
            embedding_calibration=embedding_calibration or {},
            fusion_method=fusion_method,
        )
        self.prompt = prompt if prompt is not None else PromptProfileSection()
        self.task_overrides = task_overrides if task_overrides is not None else TaskOverridesProfile()
        self.safety = safety if safety is not None else SafetyProfile()
        self.probed_at = probed_at
        self.capybase_version = capybase_version
        self.notes = notes if isinstance(notes, list) else (
            [str(notes)] if notes is not None else []
        )

    def __eq__(self, other: object) -> bool:
        """Structural equality over model + the four sections + metadata.

        ``ModelProfile`` is no longer a dataclass (it has a flexible dual-form
        ``__init__``), so define equality explicitly for the round-trip tests
        and any caller that compares profiles.
        """
        if not isinstance(other, ModelProfile):
            return NotImplemented
        return (
            self.model == other.model
            and self.capability == other.capability
            and self.quality == other.quality
            and self.retrieval == other.retrieval
            and self.prompt == other.prompt
            and self.task_overrides == other.task_overrides
            and self.safety == other.safety
            and self.probed_at == other.probed_at
            and self.capybase_version == other.capybase_version
            and self.notes == other.notes
        )

    # --- convenience accessors for the flat field names callers still use ---
    # These keep the migration source-compatible: every `profile.max_tokens` /
    # `profile.samples` / `profile.embedding_min_similarity` access still works,
    # routing to the right section. New code should prefer the sections.
    @property
    def max_tokens(self) -> int:
        return self.capability.max_tokens

    @property
    def json_mode(self) -> bool:
        return self.capability.json_mode

    @property
    def capture_token_entropy(self) -> bool:
        return self.capability.capture_token_entropy

    @property
    def generation_timeout_seconds(self) -> int:
        return self.capability.generation_timeout_seconds

    @property
    def context_window(self) -> int:
        return self.capability.context_window

    @property
    def avg_latency_ms(self) -> float:
        return self.capability.avg_latency_ms

    @property
    def enable_embedding_rag(self) -> bool:
        return self.capability.enable_embedding_rag

    @property
    def samples(self) -> int:
        return self.quality.samples

    @property
    def two_pass(self) -> bool:
        return self.quality.two_pass

    @property
    def plan_search(self) -> bool:
        return self.quality.plan_search

    @property
    def prompt_variants(self) -> bool:
        return self.quality.prompt_variants

    @property
    def diverse_sampling(self) -> bool:
        return self.quality.diverse_sampling

    @property
    def enable_self_consistency(self) -> bool:
        return self.quality.enable_self_consistency

    @property
    def embedding_min_similarity(self) -> float:
        return self.retrieval.embedding_min_similarity

    @embedding_min_similarity.setter
    def embedding_min_similarity(self, v: float) -> None:
        self.retrieval.embedding_min_similarity = v

    @property
    def embedding_calibration(self) -> dict[str, Any]:
        return self.retrieval.embedding_calibration

    @embedding_calibration.setter
    def embedding_calibration(self, v: dict[str, Any]) -> None:
        self.retrieval.embedding_calibration = _coerce_calibration(v)

    @property
    def fusion_method(self) -> str:
        return self.retrieval.fusion_method

    @fusion_method.setter
    def fusion_method(self, v: str) -> None:
        self.retrieval.fusion_method = v

    def to_dict(self) -> dict[str, Any]:
        """Serialize as nested sections + flat keys (backward compat).

        Writes both the nested ``capability``/``quality``/``retrieval``/
        ``prompt`` sections AND the legacy flat keys (max_tokens, samples,
        embedding_min_similarity, ...) so older capybase versions and any
        flat-key readers still load the file. New readers prefer the sections.
        """
        cap = asdict(self.capability)
        qual = asdict(self.quality)
        ret = asdict(self.retrieval)
        ret["embedding_calibration"] = _coerce_calibration(ret.get("embedding_calibration"))
        d: dict[str, Any] = {
            "model": self.model,
            "capability": cap,
            "quality": qual,
            "retrieval": ret,
            "prompt": self.prompt.profile.to_dict(),
            "task_overrides": asdict(self.task_overrides),
            "safety": asdict(self.safety),
            # Flat keys (legacy) — mirror the sections for backward compat.
            **{k: v for k, v in cap.items()},
            **{k: v for k, v in qual.items()},
            **{k: v for k, v in ret.items()},
            "avg_latency_ms": self.capability.avg_latency_ms,
            "probed_at": self.probed_at,
            "capybase_version": self.capybase_version,
            "notes": list(self.notes or []),
        }
        return d

    def problems(self) -> list[str]:
        """All sections' problems combined. Empty ⇒ the whole profile is safe."""
        return [
            *self.capability.problems(),
            *self.quality.problems(),
            *self.retrieval.problems(),
            *self.prompt.problems(),
            *self.task_overrides.problems(),
            *self.safety.problems(),
        ]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ModelProfile | None":
        """Build a profile from a dict, or return None if it fails validation.

        Accepts BOTH the nested-section format (``capability``/``quality``/
        ``retrieval`` keys) and the legacy flat format (all keys at top level),
        so an existing ``model_profile.json`` loads unchanged. A section present
        in both forms prefers the nested value. Validation is per-section; an
        invalid load-bearing knob rejects the whole profile (matching the
        "corrupt profile is a no-op" contract).
        """
        notes = d.get("notes") or []
        if not isinstance(notes, list):
            notes = [str(notes)]

        # Each section reads from its nested dict if present, else the flat key
        # (backward compat with the legacy flat format). `_sec(d, name, key)`
        # returns the nested-section value when available, falling back to flat.
        def _sec(name: str, key: str, default: Any) -> Any:
            nested = d.get(name)
            if isinstance(nested, dict) and key in nested:
                return nested[key]
            return d.get(key, default)

        cap = CapabilityProfile(
            max_tokens=int(_sec("capability", "max_tokens", 0)),
            json_mode=bool(_sec("capability", "json_mode", True)),
            capture_token_entropy=bool(_sec("capability", "capture_token_entropy", False)),
            generation_timeout_seconds=int(_sec("capability", "generation_timeout_seconds", 60)),
            context_window=int(_sec("capability", "context_window", 0)),
            avg_latency_ms=float(_sec("capability", "avg_latency_ms", 0.0)),
            enable_embedding_rag=bool(_sec("capability", "enable_embedding_rag", False)),
        )
        qual = QualityProfile(
            samples=int(_sec("quality", "samples", 1)),
            two_pass=bool(_sec("quality", "two_pass", False)),
            plan_search=bool(_sec("quality", "plan_search", False)),
            prompt_variants=bool(_sec("quality", "prompt_variants", False)),
            diverse_sampling=bool(_sec("quality", "diverse_sampling", False)),
            enable_self_consistency=bool(_sec("quality", "enable_self_consistency", False)),
        )
        ret = RetrievalProfile(
            embedding_min_similarity=float(_sec("retrieval", "embedding_min_similarity", 0.35)),
            embedding_calibration=_coerce_calibration(_sec("retrieval", "embedding_calibration", {})),
            fusion_method=str(_sec("retrieval", "fusion_method", "") or ""),
        )
        # The prompt section: a nested dict (the PromptProfile.to_dict() output).
        # Graceful-absence: a missing/corrupt section degrades to the default
        # (PromptProfile.from_dict ignores unknown values), never raises.
        prompt_section = PromptProfileSection()
        raw_prompt = d.get("prompt")
        if isinstance(raw_prompt, dict):
            try:
                prompt_section = PromptProfileSection(profile=PromptProfile.from_dict(raw_prompt))
            except Exception:  # noqa: BLE001 - graceful absence
                pass
        # The task_overrides section: a nested dict {task_type → {samples, ...}}.
        # Graceful-absence: a missing/corrupt section → empty overrides.
        task_overrides_section = TaskOverridesProfile()
        raw_to = d.get("task_overrides")
        if isinstance(raw_to, dict):
            overrides = raw_to.get("overrides")
            if isinstance(overrides, dict):
                task_overrides_section = TaskOverridesProfile(overrides=overrides)
        # The safety section: retry budgets + escalation thresholds.
        # Graceful-absence: a missing/corrupt section → defaults.
        safety_section = SafetyProfile()
        raw_safety = d.get("safety")
        if isinstance(raw_safety, dict):
            try:
                safety_section = SafetyProfile(
                    max_retries_per_unit=int(raw_safety.get("max_retries_per_unit", 2)),
                    max_critic_retries_per_unit=int(raw_safety.get("max_critic_retries_per_unit", 0)),
                    max_recovery_retries_per_unit=int(raw_safety.get("max_recovery_retries_per_unit", 1)),
                    critic_confidence_escalate_threshold=float(raw_safety.get("critic_confidence_escalate_threshold", 0.8)),
                    escalation_on_parse_failure=bool(raw_safety.get("escalation_on_parse_failure", True)),
                )
            except (TypeError, ValueError):  # noqa: BLE001 - graceful absence
                pass
        profile = cls(
            model=str(d.get("model", "")),
            capability=cap,
            quality=qual,
            retrieval=ret,
            prompt=prompt_section,
            task_overrides=task_overrides_section,
            safety=safety_section,
            probed_at=str(d.get("probed_at", "")),
            capybase_version=str(d.get("capybase_version", "")),
            notes=[str(n) for n in notes],
        )
        probs = profile.problems()
        if probs:
            warnings.warn(
                f"Model profile for {profile.model!r} is invalid and will be "
                f"ignored ({'; '.join(probs)}); run `capybase recalibrate`.",
                stacklevel=2,
            )
            return None
        return profile

    @classmethod
    def load(cls, path: str | Path) -> "ModelProfile | None":
        """Load a profile from JSON, or return None if absent/corrupt/invalid.

        A corrupt, partial, or invalid file is treated as "no profile":
        resolution must never crash on a bad artifact, and must never overlay
        an unsafe knob from one. The CLI's ``calibrate`` command is the way to
        (re)write a valid one.
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

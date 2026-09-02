"""Provider config: the canonical host+model identity for live runs.

A *provider config* is a small JSON file, stored OUTSIDE any repository
(default ``~/.config/capybase/providers/<name>.json``), that names:

- the completion (LLM) endpoint: ``base_url`` + ``model`` (+ optional key),
- optionally a SEPARATE embeddings endpoint: ``base_url`` + ``model``
  (embeddings may live on a different host than the LLM),
- the calibration profile to run with (``model_profile[.<name>].json``).

Design contract (the anti-hardcoded-endpoint rules):

- Calibration profiles carry NO host information. A profile is the tuned
  knob set for a model family and may be REUSED against a different host or
  model — endpoint identity belongs to the provider config, capability
  calibration belongs to the profile.
- Live runners (the eval harnesses, run-live-test.sh) resolve their endpoint
  ONLY through :func:`resolve_provider`. Resolution precedence per field:
  explicit CLI flag → environment variable → provider file value.
- Running without a calibration profile is an ERROR, not a fallback.
  Profiles are expensive to create; nothing here ever auto-creates one or
  silently substitutes defaults when one is missing.
- No host, IP, or machine name is baked into this package. When nothing is
  configured the resolver raises :class:`ProviderError` with instructions
  instead of guessing an endpoint.

File format (``providers/<name>.json``)::

    {
      "name": "nova-gemma4",              # optional; defaults to file stem
      "profile": "e2b",                   # REQUIRED — model_profile[.e2b].json
      "llm": {                            # REQUIRED
        "base_url": "http://host:8086/v1",
        "model": "chat",
        "api_key": "sk-local",            # optional literal (local servers)
        "api_key_env": "MY_KEY"           # or: read the key from this env var
      },
      "embeddings": {                     # OPTIONAL — omit when the endpoint
        "base_url": "http://other:8085/v1",   # has no /v1/embeddings
        "model": "embed"
      }
    }

Flat top-level aliases (``base_url``, ``model``, ``api_key``, ``profile``,
``embeddings_base_url``, ``embeddings_model``) are accepted for hand-written
single-endpoint files.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from capybase.calibration_profile import ModelProfile, apply_profile
from capybase.config import Config, ModelConfig, default_config_dir

PROVIDER_SUBDIR = "providers"

# Environment variables. The CAPYBASE_BASE_URL / CAPYBASE_MODEL /
# CAPYBASE_API_KEY trio predates provider configs and stays supported as
# per-field OVERRIDES (explicit user intent), never as silent defaults.
ENV_PROVIDER = "CAPYBASE_PROVIDER"
ENV_BASE_URL = "CAPYBASE_BASE_URL"
ENV_MODEL = "CAPYBASE_MODEL"
ENV_API_KEY = "CAPYBASE_API_KEY"
ENV_PROFILE = "CAPYBASE_PROFILE"

# Placeholder key for OpenAI-compatible local servers that ignore auth.
# Not a secret; only used when nothing more specific is configured.
DEFAULT_API_KEY = "sk-local"


class ProviderError(RuntimeError):
    """Raised when a live run cannot resolve a complete provider config.

    The message always carries the fix (which file/flag/env to set) — a run
    must never continue on guessed endpoint defaults.
    """


def providers_dir(config_dir: str | Path | None = None) -> Path:
    """Directory holding provider JSONs (``<config_dir>/providers``)."""
    base = Path(config_dir).expanduser() if config_dir else default_config_dir()
    return base / PROVIDER_SUBDIR


def profile_path_for(name: str, config_dir: str | Path | None = None) -> Path:
    """Map a profile name to its file: ``e2b`` → ``model_profile.e2b.json``,
    ``default`` (or ``""``) → ``model_profile.json``. A value containing a
    path separator or ending in ``.json`` is used as-is (explicit path)."""
    if not name or name == "default":
        return providers_dir(config_dir).parent / "model_profile.json"
    if os.sep in name or name.endswith(".json"):
        return Path(name).expanduser()
    return providers_dir(config_dir).parent / f"model_profile.{name}.json"


@dataclass
class ProviderConfig:
    """The merged, effective provider identity (host + model + profile ref)."""

    name: str = ""
    profile: str = ""  # profile NAME (or explicit path) — required non-empty
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    api_key_env: str = ""
    embeddings_base_url: str = ""
    embeddings_model: str = ""
    # Where each piece came from, for the run log ("cli", "env", "file:<path>",
    # "default"). Diagnostics only — never consulted by logic.
    provenance: dict[str, str] = field(default_factory=dict)

    def describe(self) -> str:
        """One-line summary for run logs: endpoint, model, profile, sources."""
        src = self.provenance.get("base_url", "?")
        prof = self.provenance.get("profile", "?")
        bits = [f"endpoint: {self.base_url} model={self.model}"]
        bits.append(f"profile={self.profile or '-'} [llm:{src}, profile:{prof}]")
        if self.embeddings_base_url:
            bits.append(
                f"embeddings={self.embeddings_model}@{self.embeddings_base_url}"
            )
        return " ".join(bits)

    def problems(self) -> list[str]:
        probs: list[str] = []
        if not self.base_url:
            probs.append("llm.base_url is empty")
        if not self.model:
            probs.append("llm.model is empty")
        if not self.profile:
            probs.append(
                "profile is empty — a calibration profile is REQUIRED "
                "(create one with `capybase calibrate`; profiles are never "
                "auto-created)"
            )
        return probs


@dataclass
class ResolvedProvider:
    """A validated ProviderConfig plus its loaded calibration profile."""

    provider: ProviderConfig
    profile: ModelProfile | None = None
    profile_path: Path | None = None  # where the profile was loaded from

    @property
    def config(self) -> ProviderConfig:
        return self.provider


def load_provider_file(path: str | Path) -> ProviderConfig:
    """Load and minimally validate one provider JSON file."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise ProviderError(
            f"provider config not found: {p}. Pass a name resolved under "
            f"{providers_dir()} or an explicit file path."
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProviderError(f"provider config {p} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProviderError(f"provider config {p} must be a JSON object")

    llm = data.get("llm") if isinstance(data.get("llm"), dict) else {}
    emb = data.get("embeddings") if isinstance(data.get("embeddings"), dict) else {}
    if not llm and (data.get("base_url") or data.get("model")):
        # Flat single-endpoint file: hoist the top-level aliases into llm.
        llm = {
            k: data[k]
            for k in ("base_url", "model", "api_key", "api_key_env")
            if data.get(k)
        }

    cfg = ProviderConfig(
        name=str(data.get("name") or p.stem),
        profile=str(data.get("profile") or ""),
        base_url=str(llm.get("base_url") or data.get("base_url") or ""),
        model=str(llm.get("model") or data.get("model") or ""),
        api_key=str(llm.get("api_key") or ""),
        api_key_env=str(llm.get("api_key_env") or data.get("api_key_env") or ""),
        embeddings_base_url=str(
            emb.get("base_url") or data.get("embeddings_base_url") or ""
        ),
        embeddings_model=str(
            emb.get("model") or data.get("embeddings_model") or ""
        ),
        provenance={k: f"file:{p}" for k in ("base_url", "model", "profile",
                                             "embeddings_base_url", "embeddings_model")},
    )
    return cfg


def resolve_provider(
    *,
    provider: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    profile: str | None = None,
    embeddings_base_url: str | None = None,
    embeddings_model: str | None = None,
    environ: Mapping[str, str] | None = None,
    config_dir: str | Path | None = None,
) -> ResolvedProvider:
    """Resolve the effective provider config for a live run.

    Precedence per field: CLI argument → environment variable → provider
    file. The provider file itself is selected by the ``provider`` argument
    (a name under ``<config_dir>/providers/`` or an explicit ``.json`` path)
    or ``CAPYBASE_PROVIDER``.

    Raises :class:`ProviderError` — with concrete fix instructions — when no
    endpoint source exists at all, when the required fields are missing after
    merging, or when the referenced calibration profile cannot be loaded.
    Never guesses, never falls back, never auto-creates.
    """
    env = os.environ if environ is None else environ

    # --- select the provider file (name under providers/, or explicit path)
    provider_spec = provider or env.get(ENV_PROVIDER, "").strip() or None
    cfg = ProviderConfig()
    if provider_spec:
        candidate = Path(provider_spec).expanduser()
        if candidate.suffix == ".json" or candidate.parent != Path("."):
            path: Path = candidate
        else:
            path = providers_dir(config_dir) / f"{provider_spec}.json"
        cfg = load_provider_file(path)
        cfg.provenance["provider"] = f"file:{path}"

    def _pick(key: str, cli_val: str | None, env_var: str, current: str) -> str:
        """Merge one field: CLI → env → current (file) value; track source."""
        if cli_val:
            cfg.provenance[key] = "cli"
            return cli_val
        env_val = env.get(env_var, "").strip()
        if env_val:
            cfg.provenance[key] = "env"
            return env_val
        return current

    cfg.base_url = _pick("base_url", base_url, ENV_BASE_URL, cfg.base_url)
    cfg.model = _pick("model", model, ENV_MODEL, cfg.model)
    cfg.profile = _pick("profile", profile, ENV_PROFILE, cfg.profile)
    cfg.embeddings_base_url = _pick(
        "embeddings_base_url", embeddings_base_url, "CAPYBASE_EMBEDDINGS_BASE_URL",
        cfg.embeddings_base_url,
    )
    cfg.embeddings_model = _pick(
        "embeddings_model", embeddings_model, "CAPYBASE_EMBEDDINGS_MODEL",
        cfg.embeddings_model,
    )
    cfg.api_key = _pick("api_key", api_key, ENV_API_KEY, cfg.api_key)
    if not cfg.api_key and cfg.api_key_env:
        cfg.api_key = env.get(cfg.api_key_env, "").strip()
        cfg.provenance["api_key"] = f"env:{cfg.api_key_env}"
    if not cfg.api_key:
        # Local llama-servers ignore auth; this placeholder is the documented
        # convention, not a secret. Provider files should set a real key when
        # the endpoint checks one.
        cfg.api_key = DEFAULT_API_KEY
        cfg.provenance["api_key"] = "default"

    # --- hard refusals (the no-fallback contract) -------------------------
    if provider_spec is None and cfg.base_url == "" and cfg.model == "":
        raise ProviderError(
            "no model endpoint configured. Refusing to guess one. Fix by "
            "either:\n"
            "  1. --provider NAME        (a JSON under "
            f"{providers_dir(config_dir)}/; see `capybase provider list`)\n"
            "  2. CAPYBASE_PROVIDER=NAME (same lookup via environment)\n"
            "  3. explicit --base-url URL --model ID flags\n"
            "Provider files live OUTSIDE the repository; hosts and IPs are "
            "never tracked in repo code (a pre-commit hook enforces this)."
        )
    problems = cfg.problems()
    if problems:
        raise ProviderError(
            f"incomplete provider config{' ' + cfg.name if cfg.name else ''}: "
            + "; ".join(problems)
        )

    # --- load the calibration profile (required, never auto-created) ------
    prof_path = profile_path_for(cfg.profile, config_dir)
    try:
        prof = ModelProfile.load(prof_path)
    except ValueError as exc:
        raise ProviderError(
            f"calibration profile {cfg.profile!r} at {prof_path} is "
            f"INVALID: {exc}. Fix the profile (or re-run "
            "`capybase calibrate`) — invalid settings are never silently "
            "ignored.") from exc
    if prof is None:
        raise ProviderError(
            f"calibration profile {cfg.profile!r} could not be loaded from "
            f"{prof_path}. Profiles are never auto-created or substituted — "
            "run `capybase calibrate` to produce one, or fix the 'profile' "
            "field in the provider config."
        )
    cfg.provenance.setdefault("profile", "cli-or-env")
    return ResolvedProvider(
        provider=cfg, profile=prof, profile_path=prof_path
    )


def apply_to_config(
    cfg: Config, resolved: ResolvedProvider, *, force_profile: bool = True
) -> tuple[Config, list[str]]:
    """Apply a resolved provider onto a Config.

    Sets the completion endpoint (``model.base_url/model/api_key``) and, when
    the provider names one, the embeddings endpoint
    (``memory.embeddings_base_url/embeddings_model``). The calibration
    profile's knobs are applied with ``force_profile=True`` semantics: an
    EXPLICITLY selected profile is intentional reuse and applies even when
    its recorded model name differs from the endpoint's model id (the
    orchestrator's implicit name-match gate only governs ambient profiles).

    Returns ``(config, overridden_knob_names)``.
    """
    p = resolved.provider
    cfg.model.base_url = p.base_url
    cfg.model.model = p.model
    cfg.model.api_key = p.api_key
    if p.embeddings_base_url or p.embeddings_model:
        cfg.memory.embeddings_base_url = p.embeddings_base_url
        if p.embeddings_model:
            cfg.memory.embeddings_model = p.embeddings_model
    # apply_profile returns a NEW ModelConfig (model_copy); assign it back.
    cfg.model, overridden = apply_profile(
        cfg.model, resolved.profile, force=force_profile
    )
    # The provider-named profile is the COMPLETE calibration: its prompt
    # section becomes the process-wide active PromptProfile (the eval/live
    # prompt layout follows the calibration; no repo-local ambient path).
    # Explicit env overrides still win (the calibrate A/B axes).
    _env_axes = (
        "CAPYBASE_PROMPT_LAYOUT", "CAPYBASE_PROMPT_HISTORY",
        "CAPYBASE_PROMPT_POSITION", "CAPYBASE_PROMPT_OUTLINE",
        "CAPYBASE_PROMPT_EXAMPLES", "CAPYBASE_PROMPT_VARIANT",
    )
    if not any(os.environ.get(_v, "").strip() for _v in _env_axes):
        from capybase.prompt_profile import set_active_profile
        # STRICT: an invalid prompt section raises (surfaced by callers as
        # a fatal ProviderError with the concrete axis/value message).
        set_active_profile(resolved.profile.prompt.profile)
    # Safety section: calibrated retry budgets + escalation thresholds onto
    # PolicyConfig (per-model policy, not config-only).
    _safety = getattr(resolved.profile, "safety", None)
    if _safety is not None and not _safety.is_default:
        cfg.policy.max_retries_per_unit = _safety.max_retries_per_unit
        cfg.policy.max_critic_retries_per_unit = (
            _safety.max_critic_retries_per_unit)
        cfg.policy.max_recovery_retries_per_unit = (
            _safety.max_recovery_retries_per_unit)
        cfg.policy.critic_confidence_escalate_threshold = (
            _safety.critic_confidence_escalate_threshold)
        overridden = list(overridden) + [
            "safety.max_retries_per_unit",
            "safety.max_recovery_retries_per_unit",
        ]
    return cfg, overridden

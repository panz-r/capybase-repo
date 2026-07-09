"""Prompt-rendering profile: parameterizes how prompt *content* is rendered.

This is the "prompt-content → rendered-prompt" layer. The resolution engine
derives the analytical content (the three conflict sides, obligations,
structural context, few-shot examples) from the conflict unit; the
:class:`PromptProfile` decides how that content is *arranged and framed* in the
final prompt string — the output layout (JSON vs raw fenced code), the history
framing prose, the instruction ordering, and the outline preamble.

The goal (external design) is a layer that is **expressive enough to auto-tailor
through calibration** to whatever model the user is running. A 3B model that
struggles with JSON escaping can switch to the markdown-code layout; a model
that loses the rules in the middle of a long prompt can move them to the top.
The calibration phase (a follow-up commit) selects the best profile per model;
this module ships the data structure + a process-wide active profile + an
env-driven selector so the layouts can be A/B'd immediately.

Design contract (mirrors :mod:`capybase.calibration_profile`):

- Frozen dataclass; :data:`DEFAULT_PROFILE` reproduces today's ``v6`` prompts
  byte-for-byte. Every axis defaults to the current production value, so the
  layer is inert until a caller opts in.
- One process-wide active profile (:func:`active_profile` /
  :func:`set_active_profile`), mirroring the legacy ``_OUTLINE_VARIANT``
  global. :func:`profile_from_env` reads ``CAPYBASE_PROMPT_LAYOUT`` etc. for
  the live eval; the old ``CAPYBASE_PROMPT_VARIANT`` env var is kept as an
  alias for the outline axis.
- :meth:`PromptProfile.tag` produces a short suffix appended to
  ``prompt_version`` so offline eval can attribute outcomes to the framing —
  the seed data for any future prompt-optimization work.
- :meth:`to_dict` / :meth:`from_dict` round-trip, ready for the calibration
  integration (the follow-up commit persists this as a ``prompt`` section on
  :class:`~capybase.calibration_profile.ModelProfile`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


class OutputLayout(Enum):
    """How the model emits its merged code + metadata.

    - ``JSON_V6``: today's ``v6`` contract — one ```json fenced object whose
      ``resolved_text`` string holds the merged code (escaped). The default.
    - ``MARKDOWN_CODE``: the merged code as a RAW fenced code block, then a
      small JSON object for the metadata fields. Eliminates the JSON-escaping
      burden that breaks small models on code with embedded quotes / newlines.
    """

    JSON_V6 = "json_v6"
    MARKDOWN_CODE = "markdown_code"


class HistoryFraming(Enum):
    """The prose framing around the commit-history context block.

    - ``UNTRUSTED``: today's "untrusted metadata" warning (the default; the
      block already carries this sentence inside ``context.history_context``).
    - ``NEUTRAL``: a softer "Commit context for intent inference:" header.
    - ``STRIPPED``: no framing prose at all — just the raw commit facts.
    """

    UNTRUSTED = "untrusted"
    NEUTRAL = "neutral"
    STRIPPED = "stripped"


class InstructionPosition(Enum):
    """Where the output contract + critical rules sit in the prompt.

    - ``BOTTOM``: the canonical ``v6`` ordering — rules at the very end (the
      recency bias of an autoregressive model puts them closest to the answer).
    - ``TOP_HEAVY``: contract + rules BEFORE the data payload — tests whether
      stating the constraint first improves faithfulness to the splice contract.
    - ``SANDWICHED``: high-level goals at the top, critical syntax rules at the
      bottom — the data sits between a framing preamble and the hard rules.
    """

    BOTTOM = "bottom"
    TOP_HEAVY = "top_heavy"
    SANDWICHED = "sandwiched"


class OutlineMode(Enum):
    """The outline-first preamble variant (small-model experiment).

    Replaces the legacy process-wide ``_OUTLINE_VARIANT`` int global. ``NONE``
    is the baseline prompt (no outline); ``V1``–``V5`` select the five existing
    outline framings (goal / change-relative / checklist / role / contrast).
    """

    NONE = "none"
    V1 = "v1"
    V2 = "v2"
    V3 = "v3"
    V4 = "v4"
    V5 = "v5"


#: The five outline variants that carry a prompt-version tag, in order.
_OUTLINE_TAGGED: tuple[OutlineMode, ...] = (
    OutlineMode.V1, OutlineMode.V2, OutlineMode.V3, OutlineMode.V4, OutlineMode.V5,
)

#: Map from the legacy int variant (1-5) to :class:`OutlineMode`.
_INT_TO_OUTLINE: dict[int, OutlineMode] = {i + 1: m for i, m in enumerate(_OUTLINE_TAGGED)}


@dataclass(frozen=True)
class PromptProfile:
    """The active prompt-rendering configuration.

    Every field defaults to the current production (``v6``) value, so a
    freshly-constructed profile is inert — the rendered prompt is byte-identical
    to today's. Setting any axis to a non-default value activates that rendering
    knob. The profile is read at prompt-build time by the resolution engine and
    at parse time by the response parser (the ``output_layout`` axis drives
    both sides so they stay in sync).
    """

    output_layout: OutputLayout = OutputLayout.JSON_V6
    history_framing: HistoryFraming = HistoryFraming.UNTRUSTED
    instruction_position: InstructionPosition = InstructionPosition.BOTTOM
    outline: OutlineMode = OutlineMode.NONE
    example_limit: int = 2

    def tag(self) -> str:
        """A short suffix recording the non-default axes, for ``prompt_version``.

        Empty for the baseline (every axis default) so production attribution is
        unchanged. Each non-default axis contributes a short token, joined by
        ``+`` (e.g. ``#md+top``). The suffix is appended to the base prompt
        version string (e.g. ``resolve_text_block.v6#md``) so offline eval can
        attribute outcomes to the framing.
        """
        parts: list[str] = []
        if self.output_layout is OutputLayout.MARKDOWN_CODE:
            parts.append("md")
        if self.history_framing is HistoryFraming.NEUTRAL:
            parts.append("hist-neutral")
        elif self.history_framing is HistoryFraming.STRIPPED:
            parts.append("hist-stripped")
        if self.instruction_position is InstructionPosition.TOP_HEAVY:
            parts.append("top")
        elif self.instruction_position is InstructionPosition.SANDWICHED:
            parts.append("sand")
        if self.outline is not OutlineMode.NONE:
            parts.append(f"outline-{self.outline.value}")
        if self.example_limit != 2:
            parts.append(f"ex{self.example_limit}")
        return ("#" + "+".join(parts)) if parts else ""

    # --- serialization (ready for the calibration section) ---

    def to_dict(self) -> dict:
        return {
            "output_layout": self.output_layout.value,
            "history_framing": self.history_framing.value,
            "instruction_position": self.instruction_position.value,
            "outline": self.outline.value,
            "example_limit": self.example_limit,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "PromptProfile":
        """Build a profile from a dict, ignoring unknown/invalid values.

        Mirrors the graceful-absence contract of the other calibration sections:
        a missing or corrupt key falls back to its default rather than raising.
        """
        if not isinstance(d, dict):
            return cls()

        def _enum(EnumCls, key, default):
            raw = d.get(key, default.value)
            try:
                return EnumCls(str(raw))
            except ValueError:
                return default

        try:
            example_limit = int(d.get("example_limit", 2))
        except (TypeError, ValueError):
            example_limit = 2
        return cls(
            output_layout=_enum(OutputLayout, "output_layout", OutputLayout.JSON_V6),
            history_framing=_enum(HistoryFraming, "history_framing", HistoryFraming.UNTRUSTED),
            instruction_position=_enum(InstructionPosition, "instruction_position", InstructionPosition.BOTTOM),
            outline=_enum(OutlineMode, "outline", OutlineMode.NONE),
            example_limit=example_limit,
        )


#: The baseline profile — reproduces today's ``v6`` prompts byte-for-byte.
DEFAULT_PROFILE = PromptProfile()


# ---------------------------------------------------------------------------
# Process-wide active profile
# ---------------------------------------------------------------------------

#: The active profile, or None for the default. Set via :func:`set_active_profile`.
#: Mirrors the legacy ``_OUTLINE_VARIANT`` global: one process-wide selection
#: so the prompt builders and the parser read a single source of truth.
_ACTIVE_PROFILE: PromptProfile | None = None


def active_profile() -> PromptProfile:
    """The process-wide active profile (the default when none has been set)."""
    return _ACTIVE_PROFILE if _ACTIVE_PROFILE is not None else DEFAULT_PROFILE


def set_active_profile(profile: PromptProfile | None) -> None:
    """Select the active prompt profile process-wide.

    ``None`` restores the default (today's ``v6`` prompts). The profile is read
    at prompt-build time by the resolution engine and at parse time by the
    response parser, so the two sides stay in sync automatically.
    """
    global _ACTIVE_PROFILE
    _ACTIVE_PROFILE = profile


def set_outline_variant(variant: int | None) -> None:
    """Back-compat shim: select the outline axis via the legacy int form.

    Maps the old ``1``–``5`` (or ``None``/``0`` for the baseline) onto the
    active profile's :attr:`outline` field, preserving the other axes. This
    keeps the existing ``set_outline_variant`` call site in ``live_eval.py``
    working while the outline variants are unified into the profile layer.
    """
    if not variant:
        # 0/None → baseline outline, keep the other axes as-is.
        base = active_profile()
        set_active_profile(
            PromptProfile(
                output_layout=base.output_layout,
                history_framing=base.history_framing,
                instruction_position=base.instruction_position,
                outline=OutlineMode.NONE,
                example_limit=base.example_limit,
            )
        )
        return
    mode = _INT_TO_OUTLINE.get(variant)
    if mode is None:
        return  # unknown variant → no-op (matches the old guard)
    base = active_profile()
    set_active_profile(
        PromptProfile(
            output_layout=base.output_layout,
            history_framing=base.history_framing,
            instruction_position=base.instruction_position,
            outline=mode,
            example_limit=base.example_limit,
        )
    )


def get_outline_variant() -> int | None:
    """Back-compat shim: the legacy int outline variant, or None for baseline."""
    mode = active_profile().outline
    if mode is OutlineMode.NONE:
        return None
    for n, m in _INT_TO_OUTLINE.items():
        if m is mode:
            return n
    return None


# ---------------------------------------------------------------------------
# Env-driven profile (live eval A/B)
# ---------------------------------------------------------------------------


def _env_enum(EnumCls, env_var: str, default) -> "Enum":
    raw = os.environ.get(env_var, "").strip().lower()
    if not raw:
        return default
    try:
        return EnumCls(raw)
    except ValueError:
        return default


def profile_from_env() -> PromptProfile:
    """Read a profile from ``CAPYBASE_PROMPT_*`` env vars (live eval A/B).

    Recognized vars (all optional; each defaults to today's value):

    - ``CAPYBASE_PROMPT_LAYOUT``      → ``json_v6`` / ``markdown_code``
    - ``CAPYBASE_PROMPT_HISTORY``     → ``untrusted`` / ``neutral`` / ``stripped``
    - ``CAPYBASE_PROMPT_POSITION``    → ``bottom`` / ``top_heavy`` / ``sandwiched``
    - ``CAPYBASE_PROMPT_OUTLINE``     → ``none`` / ``v1``..``v5``
    - ``CAPYBASE_PROMPT_EXAMPLES``    → int (few-shot example cap)
    - ``CAPYBASE_PROMPT_VARIANT``     → legacy alias for the outline axis (1-5)

    Unknown values are ignored (the axis keeps its default) so a typo never
    produces an unparseable profile.
    """
    layout = _env_enum(OutputLayout, "CAPYBASE_PROMPT_LAYOUT", OutputLayout.JSON_V6)
    history = _env_enum(HistoryFraming, "CAPYBASE_PROMPT_HISTORY", HistoryFraming.UNTRUSTED)
    position = _env_enum(InstructionPosition, "CAPYBASE_PROMPT_POSITION", InstructionPosition.BOTTOM)
    outline = _env_enum(OutlineMode, "CAPYBASE_PROMPT_OUTLINE", OutlineMode.NONE)
    # Legacy alias: CAPYBASE_PROMPT_VARIANT=<1-5> selects the outline axis.
    legacy = os.environ.get("CAPYBASE_PROMPT_VARIANT", "").strip()
    if legacy and outline is OutlineMode.NONE:
        try:
            mode = _INT_TO_OUTLINE.get(int(legacy))
            if mode is not None:
                outline = mode
        except ValueError:
            pass
    try:
        example_limit = int(os.environ.get("CAPYBASE_PROMPT_EXAMPLES", "2"))
    except (TypeError, ValueError):
        example_limit = 2
    return PromptProfile(
        output_layout=layout,
        history_framing=history,
        instruction_position=position,
        outline=outline,
        example_limit=example_limit,
    )

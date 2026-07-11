"""Tests for the model-profile runtime overlay in the orchestrator.

These verify the "Profile wins" contract at the integration seam: an
``Orchestrator`` built with a stored profile whose model matches the config
gets the tuned knobs overlaid onto its live ``config.model``; a mismatched or
absent profile changes nothing. The pure transform is covered by
``tests/test_calibration_profile.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from capybase.calibration_profile import ModelProfile
from capybase.config import Config
from capybase.orchestrator import Orchestrator

from tests.conftest import git, real_profile_loader  # noqa: F401 (opt-in fixture)


@pytest.fixture(autouse=True)
def _exercise_overlay(real_profile_loader) -> None:
    """This whole module exercises the profile overlay, so opt back into the real
    loader (the suite-wide conftest fixture otherwise disables it)."""


def _profile(**over) -> ModelProfile:
    base = dict(
        model="vibethink",
        max_tokens=16384,
        json_mode=False,
        capture_token_entropy=True,
        generation_timeout_seconds=240,
        avg_latency_ms=4500.0,
        probed_at="2026-06-26T00:00:00+00:00",
        capybase_version="0.1.0",
        notes=["tuned"],
    )
    base.update(over)
    return ModelProfile(**base)


def _profile_path(repo: Path) -> Path:
    return repo / ".rebase-agent" / "memory" / "model_profile.json"


def _write_profile(repo: Path, profile: ModelProfile) -> Path:
    path = _profile_path(repo)
    profile.save(path)
    return path


def _cfg_with_profile_path(repo: Path, model: str = "vibethink") -> Config:
    """Config pinned to an explicit profile path so these tests are self-contained
    and unaffected by the suite-wide profile-isolation fixture in conftest."""
    cfg = Config()
    cfg.model.model = model
    cfg.calibration.model_profile_path = str(_profile_path(repo))
    return cfg


def test_matching_profile_overlays_knobs_at_init(repo: Path):
    _write_profile(repo, _profile())  # model="vibethink", max_tokens=16384
    cfg = _cfg_with_profile_path(repo)

    orch = Orchestrator(cfg, repo=str(repo))

    assert orch.config.model.max_tokens == 16384  # overlaid
    assert orch.config.model.json_mode is False
    assert orch.config.model.capture_token_entropy is True
    assert orch.config.model.generation_timeout_seconds == 240
    # The overlay must reach the RESOLUTION ENGINE (the real consumer), not just
    # self.config — otherwise the tuned knobs never reach the model calls.
    assert orch.resolution_engine.config.max_tokens == 16384
    assert orch.resolution_engine.config.json_mode is False


def test_matching_profile_emits_journal_event(repo: Path):
    _write_profile(repo, _profile())
    cfg = _cfg_with_profile_path(repo)

    orch = Orchestrator(cfg, repo=str(repo))

    events = orch.journal.read_events()
    applied = [e for e in events if e.event_type == "model_profile_applied"]
    assert len(applied) == 1
    payload = applied[0].payload
    assert payload["model"] == "vibethink"
    assert "max_tokens" in payload["overridden_knobs"]
    assert "model_profile.json" in payload["profile_path"]


def test_mismatched_model_profile_is_noop(repo: Path):
    _write_profile(repo, _profile(model="qwen-coder"))  # different model
    cfg = _cfg_with_profile_path(repo)
    original_max = cfg.model.max_tokens

    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        orch = Orchestrator(cfg, repo=str(repo))

    assert orch.config.model.max_tokens == original_max  # unchanged
    events = orch.journal.read_events()
    assert not [e for e in events if e.event_type == "model_profile_applied"]
    # The user is nudged to recalibrate (the profile was fit for another model).
    assert any("recalibrate" in str(w.message) for w in caught)


def test_absent_profile_is_noop(repo: Path):
    # No profile written at all.
    cfg = _cfg_with_profile_path(repo)
    original_max = cfg.model.max_tokens

    orch = Orchestrator(cfg, repo=str(repo))

    assert orch.config.model.max_tokens == original_max
    events = orch.journal.read_events()
    assert not [e for e in events if e.event_type == "model_profile_applied"]


def test_corrupt_profile_is_noop(repo: Path):
    path = _profile_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not valid json {{{", encoding="utf-8")

    cfg = _cfg_with_profile_path(repo)

    # Must not raise.
    orch = Orchestrator(cfg, repo=str(repo))
    assert orch.config.model.max_tokens == cfg.model.max_tokens  # unchanged


def test_overlay_only_changes_tuned_knobs(repo: Path):
    _write_profile(repo, _profile())
    cfg = _cfg_with_profile_path(repo)
    cfg.model.temperature = 0.42  # a non-tuned knob
    cfg.model.sampling_temperature = 0.95  # a non-tuned knob

    orch = Orchestrator(cfg, repo=str(repo))

    # Tuned knobs overlaid...
    assert orch.config.model.max_tokens == 16384
    # ...but non-profile knobs preserved.
    assert orch.config.model.temperature == 0.42
    assert orch.config.model.sampling_temperature == 0.95


# ---------------------------------------------------------------------------
# Prompt-rendering profile application (PromptProfileSection)
# ---------------------------------------------------------------------------


def test_matching_profile_applies_prompt_section(repo: Path, monkeypatch):
    """A matching profile's prompt section becomes the active prompt profile."""
    import capybase.prompt_profile as pp
    from capybase.calibration_profile import PromptProfileSection
    from capybase.prompt_profile import OutputLayout, PromptProfile, set_active_profile

    set_active_profile(None)  # start clean
    _write_profile(repo, _profile(prompt=PromptProfileSection(
        profile=PromptProfile(output_layout=OutputLayout.MARKDOWN_CODE))))
    cfg = _cfg_with_profile_path(repo)
    # Clear any prompt env vars so the orchestrator applies the section.
    for v in ("CAPYBASE_PROMPT_LAYOUT", "CAPYBASE_PROMPT_HISTORY",
              "CAPYBASE_PROMPT_POSITION", "CAPYBASE_PROMPT_OUTLINE",
              "CAPYBASE_PROMPT_EXAMPLES", "CAPYBASE_PROMPT_VARIANT"):
        monkeypatch.delenv(v, raising=False)

    Orchestrator(cfg, repo=str(repo))

    assert pp.active_profile().output_layout is OutputLayout.MARKDOWN_CODE
    set_active_profile(None)  # reset for other tests


def test_env_override_wins_over_prompt_section(repo: Path, monkeypatch):
    """An explicit CAPYBASE_PROMPT_LAYOUT env var beats the calibrated section."""
    import capybase.prompt_profile as pp
    from capybase.calibration_profile import PromptProfileSection
    from capybase.prompt_profile import OutputLayout, PromptProfile, set_active_profile

    set_active_profile(None)
    _write_profile(repo, _profile(prompt=PromptProfileSection(
        profile=PromptProfile(output_layout=OutputLayout.MARKDOWN_CODE))))
    cfg = _cfg_with_profile_path(repo)
    # The env override forces JSON_V6 — the orchestrator must NOT apply the
    # profile's markdown_code section.
    monkeypatch.setenv("CAPYBASE_PROMPT_LAYOUT", "json_v6")

    Orchestrator(cfg, repo=str(repo))

    # Env override wins: the active profile is NOT the section's markdown_code.
    assert pp.active_profile().output_layout is not OutputLayout.MARKDOWN_CODE
    set_active_profile(None)


def test_absent_prompt_section_leaves_default_active(repo: Path):
    """A profile without a prompt section leaves the default profile active."""
    import capybase.prompt_profile as pp
    from capybase.prompt_profile import set_active_profile

    set_active_profile(None)
    _write_profile(repo, _profile())  # no prompt section → default
    cfg = _cfg_with_profile_path(repo)

    Orchestrator(cfg, repo=str(repo))

    # Equal by value to the default (the section was absent → default profile),
    # though not necessarily the same instance (set_active_profile stores the
    # section's profile, which is a distinct equal object).
    assert pp.active_profile() == pp.DEFAULT_PROFILE
    set_active_profile(None)


# ---------------------------------------------------------------------------
# SafetyProfile application (feedback §2.1)
# ---------------------------------------------------------------------------


def test_safety_profile_overrides_retry_budget(repo: Path, monkeypatch):
    """A non-default SafetyProfile overrides PolicyConfig retry budgets at init."""
    from capybase.calibration_profile import SafetyProfile

    for v in ("CAPYBASE_PROMPT_LAYOUT", "CAPYBASE_PROMPT_HISTORY",
              "CAPYBASE_PROMPT_POSITION", "CAPYBASE_PROMPT_OUTLINE",
              "CAPYBASE_PROMPT_EXAMPLES", "CAPYBASE_PROMPT_VARIANT"):
        monkeypatch.delenv(v, raising=False)

    _write_profile(repo, _profile(safety=SafetyProfile(max_retries_per_unit=7)))
    cfg = _cfg_with_profile_path(repo)
    original = cfg.policy.max_retries_per_unit

    orch = Orchestrator(cfg, repo=str(repo))
    assert orch.config.policy.max_retries_per_unit == 7  # overlaid


def test_safety_profile_default_is_noop(repo: Path):
    """A default SafetyProfile leaves PolicyConfig unchanged."""
    _write_profile(repo, _profile())  # safety section is default
    cfg = _cfg_with_profile_path(repo)
    original = cfg.policy.max_retries_per_unit

    orch = Orchestrator(cfg, repo=str(repo))
    assert orch.config.policy.max_retries_per_unit == original  # unchanged

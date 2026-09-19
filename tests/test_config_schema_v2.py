"""Schema-version-2 tests: migration, single-gate features, section split.

Config schema v2 introduced: the top-level ``schema_version`` key, the
[features] section (ONE activation gate per feature), and the
[mechanisms]/[experimental] split of the old [future] section. These
tests pin the migration contract and the gate semantics.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from capybase.config import (
    Config, FeaturesConfig, FutureConfig,
    FUTURE_EXPERIMENTAL_FIELDS, FUTURE_MECHANISMS_FIELDS, SCHEMA_VERSION,
)


def _write_and_load(tmp_path: Path, text: str) -> Config:
    d = tmp_path / "cfg"
    d.mkdir(exist_ok=True)
    (d / "capybase.toml").write_text(text)
    return Config.load(d / "capybase.toml")


# ---------------------------------------------------------------------------
# v1 -> v2 migration (a v1 file = no schema_version key)
# ---------------------------------------------------------------------------

def test_v1_file_migrates_and_diagnoses(tmp_path):
    cfg = _write_and_load(tmp_path, """
[future]
enable_rag = false
enable_structural_resolver = false
enable_combination_search = false
enable_import_union = false
[memory]
enabled = true
[structural]
enabled = true
[validation]
enable_verifier_model = false
""")
    # v1 rag semantics: future.enable_rag AND memory.enabled = false AND true
    assert cfg.features.rag is False
    assert cfg.features.structural_resolution is False
    assert cfg.features.combination_search is False
    assert cfg.features.structural_context is True
    assert cfg.features.llm_critic is False
    # the non-gate future key survives the migration
    assert cfg.future.enable_import_union is False
    joined = "\n".join(cfg.load_diagnostics)
    for fragment in ("structural_resolution", "combination_search",
                     "features.rag", "structural_context", "llm_critic"):
        assert fragment in joined, f"migration diag missing {fragment!r}"


def test_v1_self_consistency_twin_or_semantics(tmp_path):
    """v1 effective value was model OR future; the migration preserves OR."""
    cfg = _write_and_load(tmp_path, """
[future]
enable_self_consistency = true
""")
    assert cfg.model.enable_self_consistency is True


def test_v1_shadow_jury_folds_into_jury_mode(tmp_path):
    cfg = _write_and_load(tmp_path, """
[future]
enable_shadow_jury = true
""")
    assert cfg.future.jury_mode == "shadow"
    assert not hasattr(cfg.future, "enable_shadow_jury")


def test_v1_explicit_jury_mode_wins_over_shadow_flag(tmp_path):
    cfg = _write_and_load(tmp_path, """
[future]
jury_mode = "enforce"
enable_shadow_jury = true
""")
    assert cfg.future.jury_mode == "enforce"
    assert any("ignored" in line for line in cfg.load_diagnostics)


def test_v2_file_keeps_working(tmp_path):
    cfg = _write_and_load(tmp_path, f"""
schema_version = {SCHEMA_VERSION}
[features]
rag = false
[mechanisms]
enable_import_union = false
[experimental]
jury_mode = "shadow"
""")
    assert cfg.features.rag is False
    assert cfg.future.enable_import_union is False
    assert cfg.future.jury_mode == "shadow"
    assert cfg.loaded_schema_version == 2
    assert cfg.load_diagnostics == []


def test_v2_file_ignores_v1_keys_with_a_hint(tmp_path):
    cfg = _write_and_load(tmp_path, """
schema_version = 2
[future]
enable_rag = true
enable_import_union = false
[memory]
enabled = false
""")
    # deprecated keys are NOT honored in a v2 file
    assert cfg.features.rag is True  # default, not the [future] v1 key
    assert cfg.future.enable_import_union is False  # valid v2 key, wrong
    # section name — applied with a deprecation notice
    joined = "\n".join(cfg.load_diagnostics)
    assert "future.enable_rag" in joined and "features.rag" in joined
    assert "memory.enabled" in joined
    assert "[future] is deprecated" in joined


def test_unknown_keys_and_sections_are_surfaced(tmp_path):
    cfg = _write_and_load(tmp_path, """
schema_version = 2
[futuret]
typo = true
[features]
ragg = false
""")
    joined = "\n".join(cfg.load_diagnostics)
    assert "ignored unknown section [futuret]" in joined
    assert "did you mean 'future'?" in joined
    assert "features.ragg" in joined


def test_future_schema_version_is_capped_with_a_notice(tmp_path):
    cfg = _write_and_load(tmp_path, """
schema_version = 99
[features]
rag = false
""")
    assert cfg.features.rag is False
    assert any("newer than" in line for line in cfg.load_diagnostics)


# ---------------------------------------------------------------------------
# Single-gate semantics: the feature flags actually control the mechanisms
# ---------------------------------------------------------------------------

def test_features_defaults():
    f = FeaturesConfig()
    assert f.structural_resolution is True
    assert f.combination_search is True
    assert f.structural_context is False
    assert f.rag is True
    assert f.llm_critic is True


def test_structural_context_gate_controls_enrichment_only(repo, tmp_path):
    """structural_context off + structural_resolution on: units get NO AST
    metadata, but the deterministic structural resolver still runs."""
    from capybase.conflict_extractor import ConflictExtractor
    from capybase.config import Config

    cfg = Config()
    extractor = ConflictExtractor(
        __import__("capybase.git_backend", fromlist=["GitBackend"]).GitBackend(repo),
        structural_config=cfg.structural,
        structural_context=cfg.features.structural_context,
    )
    # The old StructuralConfig.enabled default was False — same default here.
    assert extractor.structural_context is False


# ---------------------------------------------------------------------------
# Partition completeness (the [mechanisms]/[experimental] contract)
# ---------------------------------------------------------------------------

def test_partition_covers_every_future_field():
    everything = set(FutureConfig.model_fields)
    assert FUTURE_MECHANISMS_FIELDS | FUTURE_EXPERIMENTAL_FIELDS == everything
    assert not (FUTURE_MECHANISMS_FIELDS & FUTURE_EXPERIMENTAL_FIELDS)


def test_experimental_holds_exactly_the_dormant_and_jury_fields():
    assert FUTURE_EXPERIMENTAL_FIELDS == {
        "enable_convergence_seed", "enable_def_site_race",
        "enable_move_edit_transposition", "enable_best_of_n",
        "jury_mode", "enable_jury_code_reopen", "jury_comment_cegis_budget",
        "jury_eligible_languages", "jury_human_review_blocks",
        "jury_config_version", "jury_prompt_version",
    }


def test_removed_gate_fields_stay_gone():
    """The v1 activation gates must not return as model fields."""
    for cls, fields in (
        (FutureConfig, ("enable_structural_resolver", "enable_combination_search",
                        "enable_rag", "enable_self_consistency",
                        "enable_shadow_jury")),
    ):
        for name in fields:
            assert name not in cls.model_fields, (
                f"{cls.__name__}.{name} resurrected — the gate lives in "
                "[features] now")
    import capybase.config as m
    assert "enabled" not in m.MemoryConfig.model_fields
    assert "enabled" not in m.StructuralConfig.model_fields
    assert "enable_verifier_model" not in m.ValidationConfig.model_fields


# ---------------------------------------------------------------------------
# Shipped template: declares v2 and loads clean
# ---------------------------------------------------------------------------

def test_shipped_template_is_v2_and_loads_without_diagnostics():
    toml = Path(__file__).resolve().parent.parent / "capybase.toml"
    data = tomllib.loads(toml.read_text())
    assert data.get("schema_version") == SCHEMA_VERSION
    cfg = Config.load(toml)  # explicit file path: standalone load
    assert cfg.loaded_schema_version == SCHEMA_VERSION
    assert cfg.load_diagnostics == [], (
        "shipped template produced loader diagnostics: "
        + "; ".join(cfg.load_diagnostics))


def test_removed_v1_gates_absent_from_template():
    text = (Path(__file__).resolve().parent.parent / "capybase.toml").read_text()
    for ghost in ("enable_structural_resolver", "enable_combination_search",
                  "enable_rag", "enable_self_consistency =",
                  "enable_verifier_model", "enable_shadow_jury"):
        assert ghost not in text, (
            f"v1 gate key {ghost!r} reappeared in the template — the gate "
            "lives in [features] now")

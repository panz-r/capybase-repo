"""Template/model drift guard: every key in capybase.toml must exist.

The shipped template once carried an entire section (`[jury]`, ~15 keys)
plus three individual keys (`enable_mutation_testing`,
`future.enable_verifier_model`, `jury_eligible_datasets`) with NO backing
fields in the config model — pydantic's extra='ignore' swallowed them
silently, so the file documented configuration that never existed, and
one of the ghosts (`jury_eligible_datasets`) masked the real key name
(`jury_eligible_languages`). This guard makes template→model drift a test
failure. One-directional by design: the template is a self-declared
partial reference, so model fields MAY be absent from it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from capybase.config import (
    Config, FutureConfig, FUTURE_EXPERIMENTAL_FIELDS, FUTURE_MECHANISMS_FIELDS,
)

_REPO = Path(__file__).resolve().parent.parent
_TEMPLATE = _REPO / "capybase.toml"

# toml section name -> the pydantic model class backing it. [mechanisms] and
# [experimental] both back FutureConfig (the schema-v2 section split); their
# field partition has its own completeness test below.
FUTURE_SECTION_ALIASES = ("mechanisms", "experimental")


def _template_sections() -> dict:
    return tomllib.loads(_TEMPLATE.read_text()) or {}


def _model_section_classes() -> dict[str, type]:
    """Map toml section name -> pydantic model class for root Config fields."""
    classes: dict[str, type] = {}
    root = Config()
    for name in Config.model_fields:
        if name == "source_path":  # set by load(), not a toml key
            continue
        classes[name] = type(getattr(root, name))
    for alias in FUTURE_SECTION_ALIASES:
        classes[alias] = type(root.future)
    return classes


@pytest.mark.parametrize(
    "section", sorted(_template_sections()), ids=lambda s: f"[{s}]")
def test_template_section_exists_in_model(section: str):
    classes = _model_section_classes()
    data = _template_sections()[section]
    if not isinstance(data, dict):
        pytest.skip("top-level scalar (e.g. schema_version)")
    assert section in classes, (
        f"[{section}] exists in capybase.toml but not in the config model — "
        "its keys parse and silently vanish. Add the section to the model or "
        "delete it from the template.")


@pytest.mark.parametrize(
    "section", sorted(_template_sections()), ids=lambda s: f"[{s}]")
def test_template_keys_exist_in_model(section: str):
    data = _template_sections()[section]
    if not isinstance(data, dict):
        pytest.skip("non-table value")
    model_cls = _model_section_classes().get(section)
    if model_cls is None:
        pytest.fail(f"[{section}] has no model counterpart (see section test)")
    if section in FUTURE_SECTION_ALIASES:
        fields = (FUTURE_MECHANISMS_FIELDS if section == "mechanisms"
                  else FUTURE_EXPERIMENTAL_FIELDS)
    else:
        fields = model_cls.model_fields
    unknown = [k for k in data if k not in fields]
    assert not unknown, (
        f"{section}.{'/'.join(unknown)} exists in capybase.toml but not in "
        f"{model_cls.__name__} — silently ignored on load. Fix the name or "
        "delete the key.")


def test_experimental_partition_is_complete():
    """Every FutureConfig field lives in exactly one of the two sections."""
    all_fields = set(FutureConfig.model_fields)
    assert FUTURE_MECHANISMS_FIELDS | FUTURE_EXPERIMENTAL_FIELDS == all_fields
    assert not (FUTURE_MECHANISMS_FIELDS & FUTURE_EXPERIMENTAL_FIELDS)


def test_template_parses_and_has_expected_sections():
    sections = set(_template_sections())
    expected = {"model", "policy", "tests", "validation", "journal",
                "structural", "memory", "calibration", "routing",
                "features", "mechanisms", "experimental"}
    missing = expected - sections
    assert not missing, f"template lost expected sections: {missing}"
    assert "future" not in sections, (
        "[future] was split into [mechanisms]/[experimental] in schema v2 — "
        "the template must not carry the deprecated section name")


def test_ghost_keys_stay_gone():
    """The removed ghosts, locked out by name so they cannot quietly return."""
    text = _TEMPLATE.read_text()
    for ghost in ("enable_mutation_testing", "jury_eligible_datasets",
                  "canary_mode", "complex_if_sibling_count_gt",
                  "max_simple_node_lines", "max_simple_side_chars",
                  "allow_delete_conflicted_file", "lsp_baseline_strict",
                  "enable_verifier_assertion"):
        assert ghost not in text, (
            f"ghost key {ghost!r} reappeared in capybase.toml — it has no "
            "backing field (or was removed as dead) and is silently ignored")

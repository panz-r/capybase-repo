"""ValidationConfig bridge parity (S28-349).

The orchestrator bridges config.py's pydantic ``ValidationConfig`` to
verification.py's dataclass twin via ``ValidationConfig.from_dict(model_dump())``
— and ``from_dict`` filters keys by ``cls.__dataclass_fields__``. A field added
to the pydantic twin but not the dataclass is therefore dropped SILENTLY: the
engine's ``getattr(self.config, <field>, default)`` reads the default forever.

That is exactly how S28-345's sequenced syntax preflight died: the env gate set
``cfg.validation.enable_syntax_preflight = True`` (the pydantic side, proven by
the flight config.toml), the orchestrator's bridge dropped the key, and the
branch at the build head never fired — trial41's duckdb sessions burned their
full 300s windows with zero preflight probes (S28-348's open probe, root-caused
here).

This module guards the bridge two ways:

1. PARITY: every pydantic field must exist on the dataclass twin OR be on an
   explicit, reason-annotated allowlist. A new pydantic field without a twin
   fails here at build time instead of silently no-op'ing in a live run.

2. ROUND-TRIP: for every field the twins share, a non-default value set on the
   pydantic side survives ``from_dict``. A bridge that renames or mis-types a
   shared field fails here.
"""

from __future__ import annotations

import dataclasses

from capybase.config import ValidationConfig as PydanticValidationConfig
from capybase.verification import ValidationConfig as DataclassValidationConfig

# Pydantic fields intentionally absent from the dataclass twin, each with the
# reason the drop is safe. Anything not listed here must have a twin field.
ALLOWED_BRIDGE_DROPS = {
    # Re-set by the orchestrator on the bridged instance (orchestrator.py
    # reads the pydantic side and assigns the dataclass attribute directly).
    "cc_build_target_template": "orchestrator re-sets it from the pydantic side",
    # Consumed only from the pydantic config (orchestrator reads
    # self.config.validation.<field> directly; the dataclass never sees it).
    "enable_resurrection_detection": "consumed pydantic-side",
    "resurrection_policy": "consumed pydantic-side",
    "resurrection_history_depth": "consumed pydantic-side",
    "resurrection_min_block_lines": "consumed pydantic-side",
    "resurrection_min_similarity": "consumed pydantic-side",
    "cross_commit_policy": "consumed pydantic-side",
    "enable_cross_commit_guardian": "consumed pydantic-side",
    "enable_evolution_audit": "consumed pydantic-side",
    "session_coverage_slo": "consumed pydantic-side",
    "cc_phase2_full_build_fallback": "consumed pydantic-side",
    # Read via getattr(..., default) with matching defaults on both sides;
    # a non-default value would be silently ignored (latent suppression
    # hazard, recorded in the ledger) — twin it when it first matters.
    "preservation_deletion_carveout": "default-aligned getattr read",
}


def test_every_pydantic_field_has_a_twin_or_an_allowlisted_reason():
    pyd_fields = set(PydanticValidationConfig.model_fields.keys())
    dc_fields = {f.name for f in dataclasses.fields(DataclassValidationConfig)}
    dropped = pyd_fields - dc_fields
    unexplained = dropped - set(ALLOWED_BRIDGE_DROPS)
    assert not unexplained, (
        "pydantic ValidationConfig fields with NO dataclass twin and no "
        f"allowlist entry — the bridge drops them silently (the S28-345 "
        f"preflight failure mode): {sorted(unexplained)}"
    )


def test_allowlist_stays_minimal():
    pyd_fields = set(PydanticValidationConfig.model_fields.keys())
    dc_fields = {f.name for f in dataclasses.fields(DataclassValidationConfig)}
    stale = set(ALLOWED_BRIDGE_DROPS) - (pyd_fields - dc_fields)
    assert not stale, (
        "allowlist entries whose field now has a twin (delete them): "
        f"{sorted(stale)}"
    )


def test_shared_fields_round_trip_non_default_values():
    pyd_fields = set(PydanticValidationConfig.model_fields.keys())
    dc_fields = {f.name for f in dataclasses.fields(DataclassValidationConfig)}
    shared = sorted(pyd_fields & dc_fields)
    assert "enable_syntax_preflight" in shared  # the field that died

    overrides = {}
    for name in shared:
        f = next(f for f in dataclasses.fields(DataclassValidationConfig)
                 if f.name == name)
        if f.type == "bool" or f.type is bool:
            current = getattr(PydanticValidationConfig(), name)
            overrides[name] = not current
        elif name == "rust_suppress_codes":
            overrides[name] = ["E0432"]
        elif name in ("cc_path", "cxx_path", "cc_build_command", "repo_root",
                      "cpp_std", "c_std", "clippy_severity",
                      "code_smell_severity"):
            overrides[name] = f"s28-{name}"
    pyd = PydanticValidationConfig(**overrides)
    bridged = DataclassValidationConfig.from_dict(pyd.model_dump())
    lost = {
        name: (getattr(pyd, name), getattr(bridged, name, "<absent>"))
        for name in overrides
        if getattr(bridged, name, None) != getattr(pyd, name)
    }
    assert not lost, f"bridge lost non-default values: {lost}"

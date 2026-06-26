"""Tests for the dependency-preservation validator (survey §2.2 SafeMerge).

P3: the verification-time complement to P1's prompt-time dependency context.
Both-sides-represented guards a side's additions; this validator guards a
shared base dependency (e.g. a validate() call the model silently removed).
It's a cheap deterministic *necessary* condition for semantic conflict-freedom:
if BASE references a symbol with an in-repo definition, and neither side removed
it, a valid merge must still reference it.

Severity warning → feeds the risk/retry engine, never hard-rejects. Inert when
no in-repo definitions are found (can't flag a drop it never located).
"""

from __future__ import annotations

import os

from capybase.conflict_model import (
    CandidateResolution,
    ConflictSide,
    ConflictUnit,
)
from capybase.verification import (
    DependencyPreservationValidator,
    ValidationConfig,
    VerificationContext,
)


def _unit(base, current, replayed, *, lang="python"):
    return ConflictUnit(
        session_id="s", step_index=1, path="app.py", language=lang,
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text="", marker_span=None,
    )


def _ctx(unit, resolved, cfg=None):
    cand = CandidateResolution(
        candidate_id="c", unit_id="u", model_name="m", prompt_version="v",
        resolved_text=resolved,
    )
    return VerificationContext(unit=unit, candidate=cand, config=cfg or ValidationConfig())


# ---------------------------------------------------------------------------
# Core detection
# ---------------------------------------------------------------------------


def test_flags_dropped_base_dependency(tmp_path):
    """Merge drops validate() that base + both sides kept → warning."""
    (tmp_path / "validators.py").write_text("def validate(x):\n    return x is not None\n")
    unit = _unit("validate(data)\n", "validate(data)\n", "validate(data)\n")
    v = DependencyPreservationValidator(slice_search_globs=[str(tmp_path / "*.py")])
    res = v.verify(_ctx(unit, "data\n"))  # merge dropped the call
    assert not res.passed
    assert res.severity == "warning"
    assert "validate" in res.detail["dropped_symbols"]
    assert res.features["dropped_referenced_symbol"] is True
    assert res.features["dropped_symbol_count"] == 1


def test_passes_when_dependency_kept(tmp_path):
    """Merge keeps validate() → passes."""
    (tmp_path / "validators.py").write_text("def validate(x):\n    return x\n")
    unit = _unit("validate(data)\n", "validate(data)\n", "validate(data)\n")
    v = DependencyPreservationValidator(slice_search_globs=[str(tmp_path / "*.py")])
    res = v.verify(_ctx(unit, "validate(data)\n"))
    assert res.passed


def test_passes_when_dependency_renamed_in_merge(tmp_path):
    """Merge carries the symbol under a different spelling → still detected as kept.

    The merge writes `validators.validate(data)` which contains the token
    `validate`, so the symbol is represented even though the call site changed.
    """
    (tmp_path / "validators.py").write_text("def validate(x):\n    return x\n")
    unit = _unit("validate(data)\n", "validate(data)\n", "validate(data)\n")
    v = DependencyPreservationValidator(slice_search_globs=[str(tmp_path / "*.py")])
    res = v.verify(_ctx(unit, "validators.validate(data)\n"))
    assert res.passed


# ---------------------------------------------------------------------------
# Legitimate drops are NOT flagged (a side intentionally removed the symbol)
# ---------------------------------------------------------------------------


def test_allows_drop_when_a_side_removed_it(tmp_path):
    """If the replayed side intentionally removed validate(), the merge dropping
    it is honoring a branch's intent — not a suspect drop."""
    (tmp_path / "validators.py").write_text("def validate(x):\n    return x\n")
    # base + current kept it, replayed REMOVED it (no longer references validate)
    unit = _unit("validate(data)\n", "validate(data)\n", "data\n")
    v = DependencyPreservationValidator(slice_search_globs=[str(tmp_path / "*.py")])
    res = v.verify(_ctx(unit, "data\n"))  # merge follows the removal
    assert res.passed


def test_allows_drop_when_both_sides_removed_it(tmp_path):
    """Both sides removed the dependency → merge dropping it is consensus."""
    (tmp_path / "validators.py").write_text("def validate(x):\n    return x\n")
    unit = _unit("validate(data)\n", "data\n", "data\n")
    v = DependencyPreservationValidator(slice_search_globs=[str(tmp_path / "*.py")])
    res = v.verify(_ctx(unit, "data\n"))
    assert res.passed


# ---------------------------------------------------------------------------
# Inert / no-op cases
# ---------------------------------------------------------------------------


def test_no_warning_when_base_has_no_refs(tmp_path):
    (tmp_path / "validators.py").write_text("def validate(x):\n    return x\n")
    unit = _unit("1\n", "1\n", "1\n")  # base references no symbols
    v = DependencyPreservationValidator(slice_search_globs=[str(tmp_path / "*.py")])
    res = v.verify(_ctx(unit, "1\n"))
    assert res.passed
    assert res.features["dropped_symbol_count"] == 0


def test_no_warning_when_no_in_repo_definitions(tmp_path):
    """A base ref with NO in-repo definition can't be flagged (would be a pure
    false positive — the symbol may be a stdlib/builtin)."""
    unit = _unit("print(data)\n", "print(data)\n", "print(data)\n")
    v = DependencyPreservationValidator(slice_search_globs=[str(tmp_path / "*.py")])
    res = v.verify(_ctx(unit, "data\n"))  # drops print, but print has no def here
    assert res.passed
    assert res.features["dropped_referenced_symbol"] is False


def test_skips_unsupported_language(tmp_path):
    (tmp_path / "validators.py").write_text("def validate(x):\n    return x\n")
    unit = _unit("validate(data)\n", "validate(data)\n", "validate(data)\n", lang="javascript")
    v = DependencyPreservationValidator(slice_search_globs=[str(tmp_path / "*.py")])
    res = v.verify(_ctx(unit, "data\n"))
    assert res.passed
    assert "unsupported language" in res.message


def test_repo_root_resolves_relative_globs(tmp_path):
    """A relative glob is resolved against slice_repo_root."""
    (tmp_path / "validators.py").write_text("def validate(x):\n    return x\n")
    unit = _unit("validate(data)\n", "validate(data)\n", "validate(data)\n")
    v = DependencyPreservationValidator(
        slice_search_globs=["**/*.py"],
        slice_repo_root=str(tmp_path),
    )
    res = v.verify(_ctx(unit, "data\n"))
    assert not res.passed
    assert "validate" in res.detail["dropped_symbols"]


# ---------------------------------------------------------------------------
# Engine integration: gating + registration
# ---------------------------------------------------------------------------


def test_enabled_for_table_gates_validator():
    """When reject_if_drops_referenced_symbol is False, the warning is suppressed."""
    import tempfile

    tmp = tempfile.mkdtemp()
    open(os.path.join(tmp, "validators.py"), "w").write("def validate(x):\n    return x\n")
    unit = _unit("validate(data)\n", "validate(data)\n", "validate(data)\n")
    unit.structural_metadata["conflict_features"] = {"hunk_size": 3}

    from capybase.verification import VerificationEngine

    engine = VerificationEngine(
        [DependencyPreservationValidator(slice_search_globs=[os.path.join(tmp, "*.py")])],
        ValidationConfig(reject_if_drops_referenced_symbol=False),
    )
    result = engine.verify(unit, CandidateResolution(
        candidate_id="c", unit_id="u", model_name="m", prompt_version="v", resolved_text="data\n"
    ))
    # Gated off → no warning recorded, even though features were computed.
    assert result.warnings == []
    # But the feature is still recorded (always recorded, gated warnings).
    assert result.features["dropped_referenced_symbol"] is True


def test_enabled_validator_produces_warning(tmp_path):
    """When gated ON, a dropped dependency becomes a VerificationWarning."""
    (tmp_path / "validators.py").write_text("def validate(x):\n    return x\n")
    unit = _unit("validate(data)\n", "validate(data)\n", "validate(data)\n")
    from capybase.verification import VerificationEngine

    engine = VerificationEngine(
        [DependencyPreservationValidator(slice_search_globs=[str(tmp_path / "*.py")])],
        ValidationConfig(reject_if_drops_referenced_symbol=True),
    )
    result = engine.verify(unit, CandidateResolution(
        candidate_id="c", unit_id="u", model_name="m", prompt_version="v", resolved_text="data\n"
    ))
    assert any(w.validator == "referenced_symbol_dropped" for w in result.warnings)


def test_conflict_features_not_clobbered_by_validator(tmp_path):
    """Validator features merge without overwriting conflict-spine keys."""
    (tmp_path / "validators.py").write_text("def validate(x):\n    return x\n")
    unit = _unit("validate(data)\n", "validate(data)\n", "validate(data)\n")
    unit.structural_metadata["conflict_features"] = {
        "hunk_size": 3, "balance": 1.0, "dropped_symbol_count": 99,
    }
    from capybase.verification import VerificationEngine

    engine = VerificationEngine(
        [DependencyPreservationValidator(slice_search_globs=[str(tmp_path / "*.py")])],
        ValidationConfig(),
    )
    result = engine.verify(unit, CandidateResolution(
        candidate_id="c", unit_id="u", model_name="m", prompt_version="v", resolved_text="data\n"
    ))
    # Spine value wins (99), validator's computed value (1) doesn't overwrite.
    assert result.features["dropped_symbol_count"] == 99


# ---------------------------------------------------------------------------
# Risk engine: warning-keyed retry
# ---------------------------------------------------------------------------


def test_risk_engine_retries_on_dropped_dependency():
    """A referenced_symbol_dropped warning triggers a retry decision."""
    from capybase.conflict_model import VerificationResult, VerificationWarning
    from capybase.risk import RiskEngine

    unit = _unit("validate(data)\n", "validate(data)\n", "validate(data)\n")
    result = VerificationResult(
        candidate_id="c", unit_id="u", passed=True,
        warnings=[
            VerificationWarning(
                validator="referenced_symbol_dropped",
                message="resolved text drops base-referenced symbol(s): validate",
            )
        ],
        features={"dropped_referenced_symbol": True},
    )
    engine = RiskEngine(max_retries_per_unit=3)
    decision = engine.decide(result, retry_count=0)
    assert decision.action == "retry"
    assert any("dropped" in r.lower() or "dependency" in r.lower() for r in decision.reasons)


def test_risk_engine_accepts_with_warning_when_retries_exhausted():
    """Once retries are exhausted, a passing candidate is accepted-with-warning
    (matches both_sides_represented / copied_one_side semantics — soft signals
    retry while budget remains, then accept rather than hard-block)."""
    from capybase.conflict_model import VerificationResult, VerificationWarning
    from capybase.risk import RiskEngine

    result = VerificationResult(
        candidate_id="c", unit_id="u", passed=True,
        warnings=[
            VerificationWarning(
                validator="referenced_symbol_dropped", message="drops validate"
            )
        ],
        features={"dropped_referenced_symbol": True},
    )
    engine = RiskEngine(max_retries_per_unit=2)
    # Below the limit → retry.
    assert engine.decide(result, retry_count=0).action == "retry"
    assert engine.decide(result, retry_count=1).action == "retry"
    # At the limit → can't retry, no hard failure, no consensus signal → accept.
    assert engine.decide(result, retry_count=2).action == "accept"


# ---------------------------------------------------------------------------
# Config: field present + serializable
# ---------------------------------------------------------------------------


def test_config_has_reject_if_drops_referenced_symbol():
    from capybase.config import ValidationConfig

    cfg = ValidationConfig()
    assert cfg.reject_if_drops_referenced_symbol is True
    cfg2 = ValidationConfig(reject_if_drops_referenced_symbol=False)
    assert cfg2.reject_if_drops_referenced_symbol is False


def test_config_round_trips_through_model_dump():
    from capybase.config import ValidationConfig as PydanticValidationConfig
    from capybase.verification import ValidationConfig as DCValidationConfig

    cfg = PydanticValidationConfig(reject_if_drops_referenced_symbol=False)
    d = cfg.model_dump()
    assert d["reject_if_drops_referenced_symbol"] is False
    # The dataclass mirror picks it up via from_dict introspection.
    dc = DCValidationConfig.from_dict(d)
    assert dc.reject_if_drops_referenced_symbol is False

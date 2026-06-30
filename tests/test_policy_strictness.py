"""Tests for the acceptance-strictness policy (#10).

The same candidate may be accepted in interactive mode but escalated in ci/
unattended mode, with an explicit reason. Covers the :class:`StrictnessPolicy`
wrapper (pure) and the orchestrator wiring (the mode gates both the LLM accept
branch and the deterministic pre-LLM accept paths; --no-interactive tightens the
default to ci).

Signals consumed: the band (#2), dropped obligations (#3), introduced
diagnostics (#7), and the candidate's self-reported confidence.
"""

from __future__ import annotations

import json

from capybase.conflict_model import (
    CandidateResolution,
    ConflictSide,
    ConflictUnit,
    VerificationResult,
)
from capybase.policy_strictness import StrictnessPolicy


def _unit(base="a = 1", current="a = 1\nb = 2", replayed="a = 1\nc = 3") -> ConflictUnit:
    return ConflictUnit(
        session_id="s", step_index=1, path="cfg.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=current),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=replayed),
        original_worktree_text=base, marker_span=(0, 0),
    )


def _cand(resolved="a = 1\nb = 2\nc = 3", *, confidence=0.9) -> CandidateResolution:
    return CandidateResolution(
        candidate_id="u:c", unit_id="u", model_name="fake",
        prompt_version="resolve_text_block.v5", resolved_text=resolved,
        self_reported_confidence=confidence,
    )


def _validation(*, passed=True, features=None) -> VerificationResult:
    return VerificationResult(
        candidate_id="u:c", unit_id="u", passed=passed,
        features=features or {},
    )


# ---------------------------------------------------------------------------
# StrictnessPolicy (pure)
# ---------------------------------------------------------------------------


def test_interactive_mode_always_accepts():
    """The default mode is a pass-through — never overrides."""
    p = StrictnessPolicy(mode="interactive")
    ok, _ = p.should_accept(_unit(), _cand(), _validation())
    assert ok
    assert p.accept_pre_llm(_unit(), _cand(), _validation())[0] is True


def test_unattended_blocks_low_confidence():
    """Unattended mode rejects a low-confidence candidate the engine accepted."""
    p = StrictnessPolicy(mode="unattended", min_confidence=0.8)
    ok, why = p.should_accept(_unit(), _cand(confidence=0.5), _validation())
    assert not ok
    assert "confidence" in why


def test_unattended_accepts_high_confidence_clean_candidate():
    p = StrictnessPolicy(mode="unattended", min_confidence=0.6)
    ok, _ = p.should_accept(
        _unit(), _cand(confidence=0.9),
        _validation(features={"introduced_diagnostics": 0, "dropped_obligation": False}),
        band="easy",
    )
    assert ok


def test_unattended_blocks_dropped_obligation():
    p = StrictnessPolicy(mode="unattended")
    ok, why = p.should_accept(
        _unit(), _cand(confidence=0.99),
        _validation(features={"dropped_obligation": True}),
    )
    assert not ok
    assert "obligation" in why


def test_unattended_blocks_introduced_diagnostics():
    p = StrictnessPolicy(mode="unattended")
    ok, why = p.should_accept(
        _unit(), _cand(confidence=0.99),
        _validation(features={"introduced_diagnostics": 2}),
    )
    assert not ok
    assert "diagnostic" in why


def test_unattended_blocks_hard_band():
    """A hard-band conflict escalates in unattended mode (needs a human)."""
    p = StrictnessPolicy(mode="unattended", escalate_bands=("hard",))
    ok, why = p.should_accept(
        _unit(), _cand(confidence=0.99),
        _validation(features={}), band="hard",
    )
    assert not ok
    assert "hard" in why and "human" in why


def test_unattended_accepts_deterministic_even_low_confidence():
    """A DETERMINISTIC resolution bypasses the confidence floor — it's the
    strongest evidence (no model judgment), so unattended mode trusts it as long
    as it didn't drop an obligation or introduce a diagnostic."""
    p = StrictnessPolicy(mode="unattended", min_confidence=0.9)
    ok, _ = p.should_accept(
        _unit(), _cand(confidence=0.1), _validation(), deterministic=True
    )
    assert ok


def test_ci_mode_blocks_low_confidence_but_not_band():
    """ci mode applies the confidence floor but does NOT escalate on band
    (only unattended escalates hard-band conflicts)."""
    p = StrictnessPolicy(mode="ci", min_confidence=0.7)
    ok_low, _ = p.should_accept(_unit(), _cand(confidence=0.5), _validation())
    assert not ok_low
    ok_hard, _ = p.should_accept(
        _unit(), _cand(confidence=0.9), _validation(), band="hard"
    )
    assert ok_hard  # ci doesn't gate on band


def test_pre_llm_path_blocked_on_dropped_obligation_in_unattended():
    """A deterministic pre-LLM resolution that dropped a side obligation is
    declined in unattended mode (falls through to the LLM)."""
    p = StrictnessPolicy(mode="unattended")
    ok, why = p.accept_pre_llm(
        _unit(), _cand(),
        _validation(features={"dropped_obligation": True}),
    )
    assert not ok
    assert "obligation" in why


def test_pre_llm_path_accepted_in_interactive_regardless():
    p = StrictnessPolicy(mode="interactive")
    ok, _ = p.accept_pre_llm(
        _unit(), _cand(),
        _validation(features={"dropped_obligation": True}),
    )
    assert ok  # interactive never overrides


# ---------------------------------------------------------------------------
# Orchestrator wiring: --no-interactive tightens to ci
# ---------------------------------------------------------------------------


def test_no_interactive_tightens_default_to_ci(py_repo_before_rebase):
    """A non-interactive rebase tightens the default interactive mode to ci:
    a low-confidence candidate the engine accepts is escalated."""
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator
    from capybase.resolution_engine import ResolutionEngine

    repo = py_repo_before_rebase["repo"]
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    # Low-confidence candidate the engine would accept.
    payload = json.dumps({
        "resolved_text": "    return 'hi' + 'howdy'",
        "explanation": "m", "self_reported_confidence": 0.1,
    })
    client = __import__("tests.test_orchestrator", fromlist=["CyclingClient"]).CyclingClient([payload])
    engine = ResolutionEngine(cfg.model, client=client)
    orch = Orchestrator(cfg, repo=str(repo), resolution_engine=engine,
                        out=lambda *_a, **_k: None)
    # Rebase non-interactive → ci → confidence floor escalates the low-conf candidate.
    result = orch.rebase("main", interactive=False)
    assert orch.strictness.mode == "ci"
    assert result.escalated, (
        f"expected ci-mode escalation of a low-confidence candidate, got "
        f"escalated={result.escalated} reason={result.reason!r}"
    )


def test_explicit_unattended_mode_is_respected(repo):
    """An explicit unattended mode is not loosened by --interactive: the bridge
    only tightens interactive→ci, never unattended→ci."""
    from capybase.config import Config
    from capybase.orchestrator import Orchestrator

    cfg = Config()
    cfg.policy.policy_mode = "unattended"
    orch = Orchestrator(cfg, repo=str(repo), out=lambda *_a, **_k: None)
    assert orch.strictness.mode == "unattended"

"""S28-145 + S28-157 — the retry-cap soft-fail grant and the recovery
strategy ladder.

S28-145: the census of the retry-cap rows (php-0005: four units
soft-failing on ONE draw each in a many-unit file, all escalated at
0.949) showed the capped attempts carried ZERO hard failures — the
near-oracle shape the single-failing-unit `_close` grant already covers,
denied in multi-unit files only by the failing-unit count. One latched
extra retry per unit, cap stays the ceiling.

S28-157: needs_human recovery becomes a STRATEGY LADDER — draw 1 = the
reframed recovery prompt, draw 2 = the reduced-context variant (halved
prompt budget; the trimmer drops augmentation, protecting sides +
contract). A model that refused in format A may solve in format B;
needs_human terminates only when every enrolled format has been tried
(census: 282 needs_human events, 65% of those sessions held a later
accept).
"""

from __future__ import annotations

from types import SimpleNamespace

from capybase.conflict_model import (
    ConflictSide,
    ConflictUnit,
    ContextBundle,
    TokenBudget,
    VerificationResult,
)
from capybase.resolution_engine import build_recovery_prompt
from capybase.risk import RiskEngine


def _unit() -> ConflictUnit:
    return ConflictUnit(
        session_id="s", step_index=0, path="a.py", language="python",
        conflict_type="UU", unit_id="u", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="def f():\n    return 1\n"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="def f():\n    return 2\n"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="def f():\n    return 3\n"),
        original_worktree_text="def f():\n    return 1\n", marker_span=(0, 0),
    )


# ---------------------------------------------------------------------------
# S28-157: the reduced-context recovery strategy
# ---------------------------------------------------------------------------

def test_reduced_context_halves_the_prompt_budget():
    import capybase.resolution_engine as re_mod
    captured = {}
    orig = re_mod._resolve_prompt_parts

    def spy(unit, context, budget=None, near_miss=True):
        captured["budget"] = budget
        return orig(unit, context, budget=budget, near_miss=near_miss)

    re_mod._resolve_prompt_parts = spy
    try:
        full = build_recovery_prompt(
            _unit(), ContextBundle(primary_text="x"), failures=None,
            budget=TokenBudget(total=1600, reserved_for_completion=200))
        captured.clear()
        reduced = build_recovery_prompt(
            _unit(), ContextBundle(primary_text="x"), failures=None,
            budget=TokenBudget(total=1600, reserved_for_completion=200),
            reduced_context=True)
    finally:
        re_mod._resolve_prompt_parts = orig
    assert captured["budget"].total == 800  # halved
    assert captured["budget"].available == 600
    assert len(reduced) <= len(full)  # the trimmer dropped augmentation


def test_reduced_context_noop_without_window():
    # total=0 means "no enforcement" — the halving degrades to the reframe
    prompt = build_recovery_prompt(
        _unit(), ContextBundle(primary_text="x"), failures=None,
        budget=TokenBudget(total=0), reduced_context=True)
    assert "resolved_text" in prompt  # still the recovery contract


def test_attempt_prompt_marks_the_reduced_variant():
    import capybase.resolution_engine as re_mod
    eng = re_mod.ResolutionEngine.__new__(re_mod.ResolutionEngine)
    eng.token_budget = TokenBudget(total=1600, reserved_for_completion=200)
    prompt, pv, _trims = eng.build_attempt_prompt(
        _unit(), ContextBundle(primary_text="x"), failures=None,
        pending_recovery=True, recovery_reduced_context=True)
    assert pv == "cegis_recovery_rc.v1"
    prompt, pv, _trims = eng.build_attempt_prompt(
        _unit(), ContextBundle(primary_text="x"), failures=None,
        pending_recovery=True)
    assert pv == "cegis_recovery.v1"


def test_propose_recovery_routes_the_strategy():
    import capybase.resolution_engine as re_mod
    eng = re_mod.ResolutionEngine.__new__(re_mod.ResolutionEngine)
    seen = {}
    eng.build_attempt_prompt = lambda *a, **kw: (
        seen.update(kw) or ("prompt", "pv", []))
    eng._one = lambda unit, context, prompt, pv: "candidate"
    out = eng.propose_recovery(
        _unit(), ContextBundle(primary_text="x"), failures=None,
        strategy="reduced_context")
    assert out == ["candidate"]
    assert seen["recovery_reduced_context"] is True
    eng.propose_recovery(
        _unit(), ContextBundle(primary_text="x"), failures=None)
    assert seen["recovery_reduced_context"] is False  # reframe default


def test_recovery_budget_two_keeps_the_ladder_reachable():
    """The S28-157 ladder needs the second draw: budget 2 grants twice."""
    engine = RiskEngine(max_recovery_retries_per_unit=2, enable_recovery_retry=True)
    r = VerificationResult(
        candidate_id="c", unit_id="u", passed=False,
        features={"model_needs_human": True})
    assert engine.decide(
        r, retry_count=0, failure_kind="model_refusal",
        recovery_retry_count=0).action == "retry"
    assert engine.decide(
        r, retry_count=0, failure_kind="model_refusal",
        recovery_retry_count=1).action == "retry"
    assert engine.decide(
        r, retry_count=0, failure_kind="model_refusal",
        recovery_retry_count=2).action == "escalate"



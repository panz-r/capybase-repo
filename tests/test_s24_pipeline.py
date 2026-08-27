"""Sprint-24 pipeline architecture tests.

Verifies the typed stages, mechanism protocol, pipeline executor,
and the first registered mechanism (F1 tier-1 takeover).
"""
from __future__ import annotations

import pytest

from capybase.mechanisms import F1CompileCleanTakeover, F1Tier1Takeover
from capybase.pipeline import (
    MechanismResult,
    Pipeline,
    RepairExhaustedContext,
    Stage,
)


class FakeJournal:
    def __init__(self):
        self.events = []

    def emit(self, name, payload):
        self.events.append((name, payload))


BASE = "\n".join(f"line_{i}" for i in range(100)) + "\n"


def _side(n: int) -> str:
    lines = BASE.splitlines()
    for i in range(min(n, len(lines))):
        lines[i] = f"changed_{i}"
    return "\n".join(lines) + "\n"


def _ctx(cur: str = "", rep: str = "", buf: str = "", base: str = BASE):
    return RepairExhaustedContext(
        path="test.c", language="c", step_index=1,
        spliced_buffer=buf, sides={"current": cur, "replayed": rep},
        base_text=base,
    )


# ---------------------------------------------------------------------------
# Pipeline executor
# ---------------------------------------------------------------------------

def test_pipeline_executes_registered_mechanism():
    p = Pipeline()
    p.register(F1Tier1Takeover())
    # Low churn on replayed → takeover should fire
    result = p.execute(Stage.POST_REPAIR_EXHAUSTION, _ctx(
        cur=_side(80), rep=_side(5)))
    assert result is not None
    assert result.action == "takeover"
    assert result.metadata["side"] == "current"  # high-churn side


def test_pipeline_returns_none_when_all_decline():
    p = Pipeline()
    p.register(F1Tier1Takeover())
    # Both sides changed significantly → decline
    result = p.execute(Stage.POST_REPAIR_EXHAUSTION, _ctx(
        cur=_side(50), rep=_side(60)))
    assert result is None


def test_pipeline_journals_engagement():
    journal = FakeJournal()
    p = Pipeline(journal=journal)
    p.register(F1Tier1Takeover())
    p.execute(Stage.POST_REPAIR_EXHAUSTION, _ctx(cur=_side(80), rep=_side(5)))
    events = [e for e in journal.events if e[0] == "pipeline_mechanism"]
    assert len(events) == 1
    assert events[0][1]["status"] == "engaged"
    assert events[0][1]["mechanism"] == "f1_tier1_takeover"


def test_pipeline_journals_decline():
    journal = FakeJournal()
    p = Pipeline(journal=journal)
    p.register(F1Tier1Takeover())
    p.execute(Stage.POST_REPAIR_EXHAUSTION, _ctx(cur=_side(50), rep=_side(60)))
    events = [e for e in journal.events if e[0] == "pipeline_mechanism"]
    assert len(events) == 1
    assert events[0][1]["status"] == "declined"


def test_pipeline_handles_mechanism_error():
    class BrokenMechanism:
        @property
        def stage(self):
            return Stage.POST_REPAIR_EXHAUSTION

        @property
        def name(self):
            return "broken"

        def engage(self, ctx):
            raise RuntimeError("boom")

    journal = FakeJournal()
    p = Pipeline(journal=journal)
    p.register(BrokenMechanism())
    result = p.execute(Stage.POST_REPAIR_EXHAUSTION, _ctx())
    assert result is None  # error → decline, never break the pipeline
    events = [e for e in journal.events if e[0] == "pipeline_mechanism"]
    assert events[0][1]["status"] == "error"


# ---------------------------------------------------------------------------
# F1 tier-1 mechanism
# ---------------------------------------------------------------------------

def test_f1_tier1_low_churn_takes_high_side():
    m = F1Tier1Takeover()
    result = m.engage(_ctx(cur=_side(80), rep=_side(5)))
    assert result is not None
    assert result.metadata["side"] == "current"


def test_f1_tier1_low_churn_on_current():
    m = F1Tier1Takeover()
    result = m.engage(_ctx(cur=_side(3), rep=_side(50)))
    assert result is not None
    assert result.metadata["side"] == "replayed"


def test_f1_tier1_both_changed_declines():
    m = F1Tier1Takeover()
    assert m.engage(_ctx(cur=_side(40), rep=_side(60))) is None


def test_f1_tier1_empty_sides_decline():
    m = F1Tier1Takeover()
    assert m.engage(_ctx(cur="", rep="")) is None


def test_f1_tier1_wrong_context_declines():
    """The mechanism only engages on RepairExhaustedContext."""
    m = F1Tier1Takeover()
    # A generic StageContext (not RepairExhaustedContext) → decline
    from capybase.pipeline import StageContext
    wrong_ctx = StageContext(path="x", language="c", step_index=1)
    assert m.engage(wrong_ctx) is None


# ---------------------------------------------------------------------------
# F1 compile-clean mechanism
# ---------------------------------------------------------------------------

def test_compile_clean_one_side_compiles():
    m = F1CompileCleanTakeover(compiling_sides={"current": True, "replayed": False})
    result = m.engage(_ctx(cur=_side(50), rep=_side(50)))
    assert result is not None
    assert result.action == "takeover"
    assert result.metadata["side"] == "current"


def test_compile_clean_both_compile_declines():
    m = F1CompileCleanTakeover(compiling_sides={"current": True, "replayed": True})
    assert m.engage(_ctx(cur=_side(50), rep=_side(50))) is None


def test_compile_clean_neither_compiles_declines():
    m = F1CompileCleanTakeover(compiling_sides={"current": False, "replayed": False})
    assert m.engage(_ctx(cur=_side(50), rep=_side(50))) is None


def test_compile_clean_no_verdicts_declines():
    m = F1CompileCleanTakeover()
    assert m.engage(_ctx(cur=_side(50), rep=_side(50))) is None


def test_f1_tier1_mechanism_matches_inline_semantics():
    """Migration equivalence: the pipeline mechanism's takeover decision
    must equal the legacy inline _near_one_sided_takeover on every shape —
    the orchestrator swap is structural, not behavioral."""
    import random
    from capybase.mechanisms import F1Tier1Takeover
    from capybase.orchestrator import _near_one_sided_takeover
    from capybase.pipeline import RepairExhaustedContext

    rng = random.Random(42)
    mech = F1Tier1Takeover()
    words = ["alpha", "beta", "gamma", "delta", "epsilon"]

    def rand_text(lines: int) -> str:
        return "\n".join(
            f"{rng.choice(words)}_{rng.randint(0, 9)};" for _ in range(lines))

    for trial in range(300):
        base = rand_text(rng.randint(1, 30))
        cur = rand_text(rng.randint(1, 30))
        rep = rand_text(rng.randint(1, 30))
        if rng.random() < 0.3:
            cur = base  # near-one-sided shapes
        if rng.random() < 0.3:
            rep = base
        sides = {"current": cur, "replayed": rep}
        inline = _near_one_sided_takeover(base, sides)
        ctx = RepairExhaustedContext(
            path="f.c", language="c", step_index=1,
            sides=sides, base_text=base)
        result = mech.engage(ctx)
        mech_side = result.metadata.get("side") if result else None
        assert mech_side == inline, (
            f"trial {trial}: mechanism={mech_side} inline={inline} "
            f"(base {len(base.splitlines())}L, cur {len(cur.splitlines())}L, "
            f"rep {len(rep.splitlines())}L)"
        )


def test_compile_clean_mechanism_engages_at_pre_escalate():
    """The no-progress rescue migrated onto the pipeline: the compile-clean
    mechanism takes the single compiling side at PRE_ESCALATE (the unit's
    last chance before the no-progress guard escalates — redis-0055's
    converter) using the same decision as POST_REPAIR_EXHAUSTION."""
    from capybase.mechanisms import F1CompileCleanTakeover
    from capybase.pipeline import Pipeline, PreEscalateContext, Stage

    mech = F1CompileCleanTakeover()
    pipe = Pipeline()
    pipe.register(mech)

    ctx = PreEscalateContext(
        path="f.c", language="c", step_index=1,
        sides={"current": "int a;", "replayed": "int b;"},
        base_text="", escalation_reason="no_progress: stalled")
    mech.set_compiling_sides({"current": True, "replayed": False})
    result = pipe.execute(Stage.PRE_ESCALATE, ctx)
    assert result is not None
    assert result.action == "takeover"
    assert result.metadata["side"] == "current"

    # Both compiling → decline (no single winner; tier-2/churn territory).
    mech.set_compiling_sides({"current": True, "replayed": True})
    assert pipe.execute(Stage.PRE_ESCALATE, ctx) is None

    # The tier-1 mechanism stays repair-exhaustion-only: PRE_ESCALATE with
    # no verdicts set must not fire it (it isn't registered for this stage).
    mech.set_compiling_sides({})
    assert pipe.execute(Stage.PRE_ESCALATE, ctx) is None


def test_tier2_adjudication_mechanism_injection_and_latch():
    """Migration #3: the tier-2 ballot as a mechanism — the decide callable
    is injected (the orchestrator's adjudicator), runs only when enabled
    (Phase C), and returns a takeover with the chosen side."""
    from capybase.mechanisms import F1Tier2Adjudication
    from capybase.pipeline import Pipeline, RepairExhaustedContext, Stage

    calls = []

    def decide(path, language, base_text, sides):
        calls.append((path, sides))
        return "replayed"

    mech = F1Tier2Adjudication(decide)
    pipe = Pipeline()
    pipe.register(mech)
    ctx = RepairExhaustedContext(
        path="f.rs", language="rust", step_index=1,
        sides={"current": "fn a() {}", "replayed": "fn b() {}"},
        base_text="")

    result = pipe.execute(Stage.POST_REPAIR_EXHAUSTION, ctx)
    assert result is not None and result.action == "takeover"
    assert result.metadata["side"] == "replayed"
    assert result.resolved_text == "fn b() {}"
    assert len(calls) == 1

    # Disabled (Phase A/B) → no ballot billed.
    mech.enabled = False
    assert pipe.execute(Stage.POST_REPAIR_EXHAUSTION, ctx) is None
    assert len(calls) == 1

    # decide returning None/garbage → decline, no crash.
    mech.enabled = True
    mech._decide = lambda *a: None
    assert pipe.execute(Stage.POST_REPAIR_EXHAUSTION, ctx) is None
    mech._decide = lambda *a: (_ for _ in ()).throw(RuntimeError("x"))
    assert pipe.execute(Stage.POST_REPAIR_EXHAUSTION, ctx) is None


def test_churn_fallback_mechanism_matches_whole_side_heuristic():
    """Migration #4 equivalence: the mechanism's pick equals
    _whole_side_heuristic's on every shape, engaging only when BOTH sides
    compile."""
    import random
    from capybase.mechanisms import ChurnFallbackTakeover
    from capybase.orchestrator import _whole_side_heuristic
    from capybase.pipeline import Pipeline, RepairExhaustedContext, Stage

    rng = random.Random(7)
    mech = ChurnFallbackTakeover()
    pipe = Pipeline()
    pipe.register(mech)
    words = ["alpha", "beta", "gamma", "delta"]

    for trial in range(200):
        base = "\n".join(f"{rng.choice(words)}_{i};"
                         for i in range(rng.randint(5, 40)))
        cur = "\n".join(
            (f"NEW_{rng.randint(0,9)}_{i};" if i < rng.randint(0, 20)
             else f"{base.splitlines()[i]}")
            for i in range(len(base.splitlines())))
        rep = "\n".join(
            (f"REP_{rng.randint(0,9)}_{i};" if i < rng.randint(0, 20)
             else f"{base.splitlines()[i]}")
            for i in range(len(base.splitlines())))
        sides = {"current": cur, "replayed": rep}
        ctx = RepairExhaustedContext(
            path="f.c", language="c", step_index=1,
            sides=sides, base_text=base)
        mech.set_compiling_sides({"current": True, "replayed": True})
        result = pipe.execute(Stage.POST_REPAIR_EXHAUSTION, ctx)
        expected = _whole_side_heuristic(base, sides)
        got = result.metadata.get("side") if result else None
        assert got == expected, f"trial {trial}: mech={got} heuristic={expected}"
        # Not both compiling → decline.
        mech.set_compiling_sides({"current": True, "replayed": False})
        assert pipe.execute(Stage.POST_REPAIR_EXHAUSTION, ctx) is None


def test_phase_reexecution_not_preempted_by_tier1():
    """The sea-orm-0021 preemption bug: Pipeline.execute returns on FIRST
    engagement, so tier-1 (deterministic — engages whenever near-one-sided)
    preempted compile-clean/the ballot/the fallback from ever running in
    the Phase-B/C/D re-executions. With tier-1 latched off outside Phase A,
    the Phase-B re-execution must reach compile-clean and take the single
    compiling side."""
    from capybase.mechanisms import (
        ChurnFallbackTakeover,
        F1CompileCleanTakeover,
        F1Tier1Takeover,
    )
    from capybase.pipeline import Pipeline, RepairExhaustedContext, Stage

    pipe = Pipeline()
    t1 = F1Tier1Takeover()
    cc = F1CompileCleanTakeover()
    cf = ChurnFallbackTakeover()
    pipe.register(t1)
    pipe.register(cc)
    pipe.register(cf)

    # near-one-sided shape (replayed ≈ base) + exactly current compiling:
    # tier-1 picks replayed (wrong — it fails the gate), compile-clean
    # should then take current.
    ctx = RepairExhaustedContext(
        path="f.rs", language="rust", step_index=1,
        sides={"current": "fn a() {}", "replayed": "fn a() { new }"},
        base_text="fn a() {}")

    # Phase A: tier-1 engages.
    a = pipe.execute(Stage.POST_REPAIR_EXHAUSTION, ctx)
    assert a.mechanism == "f1_tier1_takeover" and a.metadata["side"] == "replayed"

    # (Orchestrator compile-gates the pick; it fails → Phase B.)
    t1.enabled = False
    cc.set_compiling_sides({"current": True, "replayed": False})
    b = pipe.execute(Stage.POST_REPAIR_EXHAUSTION, ctx)
    assert b is not None and b.mechanism == "f1_compile_clean_takeover", (
        "Phase B must reach compile-clean — tier-1's re-engagement "
        f"preempted it (got {b.mechanism if b else None})"
    )
    assert b.metadata["side"] == "current"

    # Phase D shape (both compile, ballot declined): churn fallback runs.
    t1.enabled = False
    cc.set_compiling_sides({"current": True, "replayed": True})
    cf.set_compiling_sides({"current": True, "replayed": True})
    d = pipe.execute(Stage.POST_REPAIR_EXHAUSTION, ctx)
    assert d is not None and d.mechanism == "churn_fallback_takeover"

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

def test_compile_clean_empty_splice_takes_side():
    m = F1CompileCleanTakeover()
    result = m.engage(_ctx(cur=_side(50), rep=_side(50), buf=""))
    assert result is not None
    assert result.action == "takeover"


def test_compile_clean_nonempty_splice_declines():
    m = F1CompileCleanTakeover()
    assert m.engage(_ctx(cur=_side(50), rep=_side(50), buf="content")) is None

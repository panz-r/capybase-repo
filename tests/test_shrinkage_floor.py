"""S28-148 — shrinkage-aware wholesale-winner floor (duckdb-0133 class).

The mass-DELETION rewrite sat in the floor's blind spot: duckdb-0133's
churn_ratio was 0.8944 — the wholesale band's 0.90 gate missed by 0.6pp
— so no file-scope mechanism ever evaluated "the target state is ~12x
smaller" and the case escalated at sim 0.08. The shrinkage arm identifies
the winner by DELETION (winner_lines <= 0.70 * base + churn dominance +
the same coverage bar) and — because numbers alone are not safe in the
mid-band — fires ONLY on a measured degenerate output (buffer present,
winner preservation < 0.5). These tests pin the gate matrix on a stub
orchestrator with a proportionally scaled 0133 fixture.
"""

from __future__ import annotations

from types import SimpleNamespace

from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.orchestrator import Orchestrator


class _RecJournal:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, payload, **_kw):
        self.events.append((event, payload))


class _FakeGit:
    repo = "/tmp/fake-repo"

    def __init__(self, stages):
        self._stages = stages

    def read_stage_blob(self, path, stage):
        return self._stages[stage].encode()


def _mk_unit(base):
    return ConflictUnit(
        session_id="s", step_index=1, path="f.c", language="c",
        unit_id="f.c:1:0", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=base),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=base),
        original_worktree_text=base, marker_span=(0, 1),
    )


_BASE = "\n".join(f"int fn{i}(void) {{ return {i}; }}" for i in range(60))
# current ≈ base (8 edited lines) — the upstream kept the big file
_CUR = "\n".join(
    (f"int fn{i}(void) {{ return {i} + 1; }}" if i < 8
     else f"int fn{i}(void) {{ return {i}; }}")
    for i in range(60))
# replayed = the PEG-rewrite: deleted ~90% of the base, kept a small core
_REP = "\n".join(f"int fn{i}(void) {{ return {i}; }}" for i in range(6))


def _orch():
    orch = object.__new__(Orchestrator)
    orch.journal = _RecJournal()
    orch.step = 1
    orch.git = _FakeGit({1: _BASE, 2: _CUR, 3: _REP})
    orch.config = SimpleNamespace(
        future=SimpleNamespace(enable_wholesale_winner_floor=True))
    return orch


def test_shrinkage_arm_fires_on_measured_degenerate_output():
    from capybase.merge_intent import full_file_context
    ctx = full_file_context(_BASE, _CUR, _REP)
    assert ctx["churn_ratio"] < 0.90          # the wholesale band MISSES
    assert ctx["replayed_churn"] / max(ctx["base_lines"], 1) >= 0.30
    orch = _orch()
    out = orch._wholesale_winner_floor(
        "f.c", None, [_mk_unit(_BASE)], buffer=_CUR)
    assert out is not None
    unit, cand = out[0]
    assert cand.resolved_text == _REP         # the deletion-rewrite wins
    ev = [p for e, p in orch.journal.events if e == "wholesale_winner_floor"]
    assert ev and ev[0]["trigger"] == "shrinkage"
    assert ev[0]["winner"] == "replayed"


def test_shrinkage_arm_declines_without_a_buffer():
    """Numbers alone are not safe in the mid-band — the arm requires the
    measured degenerate output. buffer=None (the escalation-site shape)
    keeps the pre-S28-148 behavior."""
    orch = _orch()
    out = orch._wholesale_winner_floor(
        "f.c", None, [_mk_unit(_BASE)], buffer=None)
    assert out is None


def test_shrinkage_arm_declines_when_output_weaves_the_winner():
    """A woven merge preserves the winner's churn — never floored."""
    orch = _orch()
    out = orch._wholesale_winner_floor(
        "f.c", None, [_mk_unit(_BASE)], buffer=_REP)
    assert out is None


def test_symmetric_midband_never_floors():
    """Both sides changed comparably (no dominance) → not shrinkage, and
    a degenerate-looking buffer cannot summon the floor either."""
    cur = "\n".join(
        (f"int fn{i}(void) {{ return {i} + 1; }}" if i < 30
         else f"int fn{i}(void) {{ return {i}; }}")
        for i in range(60))
    rep = "\n".join(
        (f"int fn{i}(void) {{ return {i} + 2; }}" if i < 30
         else f"int fn{i}(void) {{ return {i}; }}")
        for i in range(60))
    orch = object.__new__(Orchestrator)
    orch.journal = _RecJournal()
    orch.step = 1
    orch.git = _FakeGit({1: _BASE, 2: cur, 3: rep})
    orch.config = SimpleNamespace(
        future=SimpleNamespace(enable_wholesale_winner_floor=True))
    out = orch._wholesale_winner_floor(
        "f.c", None, [_mk_unit(_BASE)], buffer="totally other text\n")
    assert out is None

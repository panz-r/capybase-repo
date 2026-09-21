"""S28-149 — file-level drift rescue (the libuv-0056 class).

When the whole-file validation fails AFTER unit acceptances, the pristine
merge-index stage sides are re-evaluated AS THE FILE against the same
validation; a side that file-validates is taken over the drifted assembly
(validator-authority acceptance — no LLM adjudication). These tests pin
the decision matrix on a stub orchestrator: partition vs the repair rung
lives at the call site (compile-flavored failures keep their arm), so the
rescue method itself is gated only by the flag, the stage sides, and the
validation outcomes. No network; the engine is never consulted.
"""

from __future__ import annotations

from types import SimpleNamespace

from capybase.orchestrator import Orchestrator


class _RecJournal:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, payload, **_kw):
        self.events.append((event, payload))


class _FakeGit:
    def __init__(self, stages: dict[int, str]):
        self._stages = stages
        self.repo = "/tmp/fake-repo"

    def read_stage_blob(self, path: str, stage: int) -> bytes:
        if stage not in self._stages:
            raise RuntimeError(f"no stage {stage}")
        return self._stages[stage].encode()


class _PassVer:
    """verify_file passes only for texts in ok_texts; resolved_text for
    texts in repair_texts (the R1 coherence-repair shape)."""

    def __init__(self, ok_texts: set[str], repair_texts: set[str] | None = None):
        self.ok_texts = ok_texts
        self.repair_texts = repair_texts or set()
        self.calls: list[str | None] = []

    def verify_file(self, path, language, original, units, *, repo_root=None,
                    pristine_side_texts=None, whole_text=None):
        self.calls.append(whole_text)
        if whole_text in self.repair_texts:
            return SimpleNamespace(passed=True, hard_failures=[],
                                   resolved_text=whole_text + "\n// repaired")
        ok = whole_text in self.ok_texts
        return SimpleNamespace(
            passed=ok, hard_failures=[]
            if ok else [SimpleNamespace(message="error: drift")],
            resolved_text=None)


_BASE = "".join(f"base line {i}\n" for i in range(50))
_CUR = _BASE.replace("base line 3\n", "cur line 3\n")
_REP = _BASE.replace("base line 7\n", "rep line 7\n")
_REP_HEAVY = "".join(f"rep line {i}\n" for i in range(120))  # churn winner
_SPLICED = _BASE.replace("base line 3\n", "broken {{{\n")


def _units():
    from capybase.conflict_model import ConflictSide, ConflictUnit
    return [ConflictUnit(
        session_id="s", step_index=1, path="f.c", language="c",
        unit_id="f.c:0:0", unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=_BASE),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=_CUR),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=_REP),
        original_worktree_text=_SPLICED,
        marker_span=(0, 1),
    )]


def _orch(ver, *, enabled=True, stages=None) -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch.resolution_engine = None
    orch.journal = _RecJournal()
    orch.step = 1
    orch.git = _FakeGit(stages if stages is not None
                        else {1: _BASE, 2: _CUR, 3: _REP})
    orch.verification = ver
    orch.config = SimpleNamespace(
        future=SimpleNamespace(enable_side_takeover_rescue=enabled))
    writes: list[str] = []
    orch._write_worktree_only = (
        lambda path, buffer, *, accepted=None: writes.append(buffer))
    orch._writes = writes
    return orch


def _args(orch):
    return ("f.c", "c", _SPLICED, _units(), _SPLICED)


def test_rescue_takes_single_validating_side():
    """The libuv-0056 shape: the assembly drifted, current == oracle —
    the rescue takes the validating side with no model involvement."""
    orch = _orch(_PassVer(ok_texts={_CUR}))
    out = orch._try_side_takeover_rescue(*_args(orch))
    assert out is not None
    accepted, buffer, val = out
    assert buffer == _CUR
    assert val.passed
    unit, cand = accepted[0]
    assert unit.unit_kind == "whole_file" and unit.marker_span is None
    assert cand.resolved_text == _CUR
    assert cand.model_name == "side_takeover_rescue"
    assert cand.provenance == "deterministic_source_current_only_stage"
    swap = [p for e, p in orch.journal.events if e == "side_takeover_rescue"]
    assert swap and swap[0]["side"] == "current"
    assert swap[0]["via"] == "single_validating_side"
    assert swap[0]["both_validated"] is False


def test_rescue_declines_when_no_side_verifies_and_restores():
    orch = _orch(_PassVer(ok_texts=set()))
    out = orch._try_side_takeover_rescue(*_args(orch))
    assert out is None
    decl = [p for e, p in orch.journal.events
            if e == "side_takeover_rescue_declined"]
    assert decl and decl[-1]["reason"] == "no_side_verifies"
    probes = [p for e, p in orch.journal.events
              if e == "side_takeover_rescue_probe"]
    assert len(probes) == 2 and all(not p["passed"] for p in probes)
    # worktree restored to the spliced buffer after the failed probes
    assert orch._writes[-1] == _SPLICED


def test_rescue_both_validate_churn_tiebreak():
    """Both sides file-validate — the churn winner breaks the tie
    (deterministic; no adjudication, no model call)."""
    orch = _orch(_PassVer(ok_texts={_CUR, _REP_HEAVY}),
                 stages={1: _BASE, 2: _CUR, 3: _REP_HEAVY})
    out = orch._try_side_takeover_rescue(*_args(orch))
    assert out is not None
    _acc, buffer, _val = out
    assert buffer == _REP_HEAVY  # replayed rewrote far more of the base
    swap = [p for e, p in orch.journal.events if e == "side_takeover_rescue"]
    assert swap[0]["side"] == "replayed"
    assert swap[0]["via"] == "churn_tiebreak"
    assert swap[0]["both_validated"] is True


def test_rescue_coherence_repair_side_declined():
    """R1 (s22): a side that only passes after a coherence repair is no
    longer pristine — the other side is taken instead."""
    orch = _orch(_PassVer(ok_texts={_CUR, _REP},
                          repair_texts={_CUR}))
    out = orch._try_side_takeover_rescue(*_args(orch))
    assert out is not None
    _acc, buffer, _val = out
    assert buffer == _REP  # current needed a repair; replayed is pristine
    swap = [p for e, p in orch.journal.events if e == "side_takeover_rescue"]
    assert swap[0]["side"] == "replayed"
    assert swap[0]["via"] == "single_validating_side"


def test_rescue_flag_off_declines_without_events():
    orch = _orch(_PassVer(ok_texts={_CUR}), enabled=False)
    assert orch._try_side_takeover_rescue(*_args(orch)) is None
    assert orch.journal.events == []


def test_rescue_no_stage_sides_declines():
    orch = _orch(_PassVer(ok_texts={_CUR}), stages={})
    out = orch._try_side_takeover_rescue(*_args(orch))
    assert out is None
    decl = [p for e, p in orch.journal.events
            if e == "side_takeover_rescue_declined"]
    assert decl and decl[-1]["reason"] == "no_stage_sides"


def test_rescue_single_stage_side_declines():
    """A one-sided index is the portfolio's territory; the drift rescue
    needs both sides to separate drifted assembly from known-good side.
    (A whitespace-only stage blob is dropped by _true_stage_sides — the
    single-side shape that reaches the rescue.)"""
    orch = _orch(_PassVer(ok_texts={_CUR}),
                 stages={1: _BASE, 2: _CUR, 3: "   \n"})
    out = orch._try_side_takeover_rescue(*_args(orch))
    assert out is None
    decl = [p for e, p in orch.journal.events
            if e == "side_takeover_rescue_declined"]
    assert decl and decl[-1]["reason"] == "single_stage_side"

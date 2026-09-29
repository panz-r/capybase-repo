"""S28-337/339 defer-to-ladder — the second header-cap hit reaches the
file-level deterministic ladder before the escalation stands.

0069's anatomy (S28-337): the unit-level header cap exits the resolution
BEFORE the file-level machinery whose deterministic rungs convert the
class — the S28-339 correction names the converter: the ``:sidefix``
side-consistency repair, a ZERO-model-request rung inside
``_whole_file_repair``'s deterministic-only pass. The build carries a
defer marker on the second-cap-hit escalation (the first hit's bridge is
the S28-321/324b recovery grant) and routes it into that ladder at the
per-unit loop's terminal exit.

The tests drive ``_defer_escalated_units_to_ladder`` through the
``Orchestrator.__new__`` harness with a stubbed ladder + gate: the
substrate doctrine (accepted + best attempts), the gate-as-authority
rescue/decline, and the marker gating. Budget constraint (S28-343):
deterministic-only — the ladder is stubbed, and the helper never touches
a model client.
"""

from __future__ import annotations

from types import SimpleNamespace

from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.orchestrator import Orchestrator, UnitOutcome


class _RecJournal:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, payload, **_kw):
        self.events.append((event, payload))


def _orch(*, ladder_result=None, gate_passed=True):
    orch = Orchestrator.__new__(Orchestrator)
    orch.journal = _RecJournal()
    orch.step = 0
    orch.calls: list = []

    def _ladder(path, accepted, original, failures, **kw):
        orch.calls.append({"accepted": list(accepted), "failures": failures,
                           **kw})
        return ladder_result

    def _gate(path, language, original, spans, **kw):
        return SimpleNamespace(passed=gate_passed, hard_failures=[])

    orch._whole_file_repair = _ladder
    orch.verification = SimpleNamespace(verify_file=_gate)
    orch.git = SimpleNamespace(repo="/tmp/does-not-exist")
    return orch


def _unit(uid="u1"):
    return ConflictUnit(
        session_id="s", step_index=0, path="inc/header.h", language="cpp",
        conflict_type="UU", unit_id=uid, unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="int x;\n"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="int y;\n"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="int z;\n"),
        original_worktree_text="int x;\nint tail;\n", marker_span=(0, 0),
    )


def _cand(text="int y;\n", cid="u1:llm"):
    return SimpleNamespace(resolved_text=text, candidate_id=cid,
                           provenance="plain_llm")


def _outcome(*, deferred=True, best=True):
    o = UnitOutcome(unit=_unit())
    o.escalated = True
    o.reason = "header file CEGIS cap reached (1 retry budget for headers)"
    o.deferred_to_ladder = deferred
    if best:
        o.attempts = [_cand()]
    o.validation = SimpleNamespace(hard_failures=[SimpleNamespace(
        validator="c_build", message="'ngx_queue_t' was not declared")])
    return o


def _accepted_pair():
    return (_unit("u0"), _cand("int x;\n", cid="u0:llm"))


def test_marker_routes_into_the_ladder_and_rescues():
    rescued = [(_unit(), _cand("whole file", cid="u1:sidefix"))]
    orch = _orch(ladder_result=rescued, gate_passed=True)
    accepted = [_accepted_pair()]
    out = orch._defer_escalated_units_to_ladder(
        "inc/header.h", accepted, [_outcome()])
    assert out is rescued
    kinds = [e for e, _ in orch.journal.events]
    assert "defer_to_ladder_engaged" in kinds
    assert "defer_to_ladder_rescued" in kinds
    # the substrate doctrine: accepted + the escalated unit's best attempt
    call = orch.calls[0]
    assert len(call["accepted"]) == 2
    assert call["accepted"][-1][1].resolved_text == "int y;\n"
    assert call["failures"], "the escalated unit's failures seed the ladder"
    assert call.get("deterministic_only") is True


def test_no_marker_never_routes():
    orch = _orch(ladder_result=[(_unit(), _cand())])
    out = orch._defer_escalated_units_to_ladder(
        "inc/header.h", [_accepted_pair()], [_outcome(deferred=False)])
    assert out is None
    assert orch.calls == []
    assert orch.journal.events == []


def test_gate_decline_keeps_the_escalation():
    orch = _orch(ladder_result=[(_unit(), _cand("whole file"))],
                 gate_passed=False)
    out = orch._defer_escalated_units_to_ladder(
        "inc/header.h", [_accepted_pair()], [_outcome()])
    assert out is None
    kinds = [e for e, _ in orch.journal.events]
    assert "defer_to_ladder_declined" in kinds
    assert "defer_to_ladder_rescued" not in kinds


def test_ladder_none_declines():
    orch = _orch(ladder_result=None)
    out = orch._defer_escalated_units_to_ladder(
        "inc/header.h", [_accepted_pair()], [_outcome()])
    assert out is None
    kinds = [e for e, _ in orch.journal.events]
    assert "defer_to_ladder_engaged" in kinds
    # S28-352 (the trial42 catch): the ladder-None decline is VISIBLE
    declined = [p for e, p in orch.journal.events
                if e == "defer_to_ladder_declined"]
    assert declined and declined[0]["reason"] == "ladder_none"
    assert "defer_to_ladder_rescued" not in kinds


def test_no_usable_attempt_means_no_substrate():
    orch = _orch(ladder_result=[(_unit(), _cand())])
    out = orch._defer_escalated_units_to_ladder(
        "inc/header.h", [], [_outcome(best=False)])
    assert out is None
    assert orch.calls == []


def test_ladder_error_never_breaks_the_escalation():
    orch = _orch()

    def _boom(*a, **kw):
        raise RuntimeError("ladder blew up")

    orch._whole_file_repair = _boom
    out = orch._defer_escalated_units_to_ladder(
        "inc/header.h", [_accepted_pair()], [_outcome()])
    assert out is None
    kinds = [e for e, _ in orch.journal.events]
    assert "defer_to_ladder_error" in kinds


def test_wiring_marker_and_flag_exist():
    assert UnitOutcome.__dataclass_fields__["deferred_to_ladder"].default \
        is False
    from capybase.config import Config
    assert Config().future.enable_defer_to_ladder is False

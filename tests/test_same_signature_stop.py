"""S28-140 — same-signature repetition stop in the whole-file repair loop.

When the whole-file validation fails with the IDENTICAL hard-failure
signature across N consecutive repair rounds AND a model-drawn candidate
is inside that window, the loop is producing zero new information
(nlohmann-json-0038: three candidates rejected with the same "stray '@'"
at ~the same line). The stop ends the repair loop early — the exhaustion
endgame (wholesale floor, F1 arms, drift rescue) claims the file instead
of the remaining budget's model calls. A deterministic-only repeated
window does NOT stop: the model re-resolve it precedes hasn't run yet.
"""

from __future__ import annotations

from tests.test_orchestrator import (  # noqa: F401 — helper reuse
    FakeConsensusEngine,
    _cand,
    _self_consistency_config,
)


class _CountingEngine(FakeConsensusEngine):
    """Re-serves the same candidate(s) every draw and counts the draws."""

    def __init__(self, candidates):
        super().__init__(candidates)
        self.draws = 0

    def propose_with_consensus(self, unit, context, *, failures=None,
                               prev_candidate=None, n_samples=None,
                               attempt=0):
        self.draws += 1
        return super().propose_with_consensus(
            unit, context, failures=failures,
            prev_candidate=prev_candidate, n_samples=n_samples, attempt=attempt)


_BROKEN = "    return 'hi'("  # passes per-unit, breaks the whole file


def _events(orch):
    return [(e.event_type, e.payload) for e in orch.journal.read_events()]


def _orchestrator(repo, engine, *, stop=True):
    from capybase.orchestrator import Orchestrator

    _cfg = _self_consistency_config(repo)
    _cfg.future.enable_empty_fast_fail = False
    _cfg.policy.max_retries_per_unit = 3
    _cfg.future.enable_same_signature_stop = stop
    return Orchestrator(
        _cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )


def test_stop_ends_loop_after_repeated_signature(conflicted_repo):
    repo = conflicted_repo["repo"]
    engine = _CountingEngine([_cand(_BROKEN, cid="broken")])
    orch = _orchestrator(repo, engine)
    result = orch.run()
    events = _events(orch)
    stops = [p for e, p in events if e == "same_signature_stop"]
    assert stops, "the repeated signature must stop the repair loop"
    assert stops[0]["model_in_window"] is True
    # Initial resolution + ONE repair round — the remaining budget's
    # draws are conserved (the thrash was proven, not re-run).
    assert engine.draws == 2
    # The endgame claims the file: not escalated (a whole-file arm lands).
    assert not result.escalated, result.reason


def test_flag_off_runs_to_budget(conflicted_repo):
    repo = conflicted_repo["repo"]
    engine = _CountingEngine([_cand(_BROKEN, cid="broken")])
    orch = _orchestrator(repo, engine, stop=False)
    orch.run()
    events = _events(orch)
    assert not [p for e, p in events if e == "same_signature_stop"]
    # Without the stop the loop burns its remaining budget on the same
    # defect (initial draw + the repair rounds).
    assert engine.draws > 2

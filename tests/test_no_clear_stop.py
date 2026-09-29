"""S28-158 — no-clear-progress stop (the REPAIR_FAILURE class).

21 harvest rows ended ESCALATE with the final buffer at sim 0.89-0.998
(php-0148 at 0.998, 746-1176s burns): repair rounds that REDUCE the
failure set without ever CLEARING it are a whack-a-mole tail, not
convergence. N model-drawn such rounds end the loop into the exhaustion
endgame. Deterministic-only rounds never count (the model keeps its
chance); a CLEARED round resets the counter.
"""

from __future__ import annotations

from tests.test_orchestrator import (  # noqa: F401 — helper reuse
    FakeConsensusEngine,
    _cand,
    _self_consistency_config,
)
import pytest


class _RotatingEngine(FakeConsensusEngine):
    """Serves each candidate once (then the last, unchanged) and counts
    the draws."""

    def __init__(self, candidates):
        super().__init__(candidates)
        self._queue = list(candidates)
        self.draws = 0

    def propose_with_consensus(self, unit, context, *, failures=None,
                               prev_candidate=None, n_samples=None,
                               attempt=0):
        self.draws += 1
        if self._queue:
            self._candidates = [self._queue.pop(0)]
        return super().propose_with_consensus(
            unit, context, failures=failures,
            prev_candidate=prev_candidate, n_samples=n_samples, attempt=attempt)


# Each breaks the file with a DIFFERENT syntax error — every round's
# failure signature differs (REDUCED effect), none ever clears, and no
# two consecutive rounds repeat (so the same-signature stop stays out of
# the way and the no-clear stop is exercised alone).
_BROKEN = [
    "    return 'hi'(",     # unclosed paren
    "    return 'hi",       # unclosed string
    "    return (hi",       # invalid name expression
    "    return hi +",      # dangling operator
]


def _orchestrator(repo, engine, *, stop=True):
    from capybase.orchestrator import Orchestrator

    _cfg = _self_consistency_config(repo)
    _cfg.future.enable_empty_fast_fail = False
    _cfg.policy.max_retries_per_unit = 3
    _cfg.future.enable_no_clear_progress_stop = stop
    # S28-368: the promoted delimiter guard intercepts this fixture's
    # paren-losing repair edit and resolves via the side fallback (3
    # draws, no escalation) — a BETTER outcome than the escalation these
    # tests document, but a different path. The tests unit-test the STOP:
    # pin the guard off.
    _cfg.future.enable_repair_delimiter_guard = False
    return Orchestrator(
        _cfg, repo=str(repo), resolution_engine=engine,
        out=lambda *_a, **_k: None,
    )


def _events(orch):
    return [(e.event_type, e.payload) for e in orch.journal.read_events()]


def test_no_clear_stop_ends_the_whack_a_mole_tail(conflicted_repo):
    repo = conflicted_repo["repo"]
    engine = _RotatingEngine([_cand(t, cid=f"broken-{i}")
                              for i, t in enumerate(_BROKEN)])
    orch = _orchestrator(repo, engine)
    result = orch.run()
    events = _events(orch)
    stops = [p for e, p in events if e == "no_clear_progress_stop"]
    assert stops, "the reducing-but-never-clearing tail must stop"
    assert stops[0]["rounds"] == 3
    assert stops[0]["last_effect"] == "REDUCED"
    # initial draw + three repair rounds — then the endgame claims it.
    assert engine.draws == 4
    assert not result.escalated, result.reason


def test_no_clear_stop_flag_off_runs_to_budget(conflicted_repo):
    repo = conflicted_repo["repo"]
    engine = _RotatingEngine([_cand(t, cid=f"broken-{i}")
                              for i, t in enumerate(_BROKEN)])
    orch = _orchestrator(repo, engine, stop=False)
    orch.run()
    events = _events(orch)
    assert not [p for e, p in events if e == "no_clear_progress_stop"]
    assert engine.draws > 3

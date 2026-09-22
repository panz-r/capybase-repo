"""S28-170 — repeat-flip census + the compile-evidence-missing guard.

The repeat-protocol forensics (duckdb-0053): all three sessions accept
the IDENTICAL deterministic candidates; the escalating session's
cold-tree build probe times out at the cap, degrades, and
acceptance_trust proposes FOR REVIEW on "compile evidence missing" —
scored ESCALATE while warm repeats PASS at 0.988. Two guards:

- _compile_evidence_missing: requires BOTH the engine's own confession
  (the tier-B proposal naming missing compile evidence) AND the missed
  deadline (a build_state degrade, a timed-out gate, or a timeout
  probe) — the verdict reads UNVERIFIED, excluded from capability
  denominators.
- The repeat-flip census: rank-ordered verdicts so a row whose best
  repeat beats the kept verdict lands in repeat-flip-queue.json (the
  corpus's cheapest re-score population).
"""

from __future__ import annotations

from types import SimpleNamespace

import importlib.util
import sys
from pathlib import Path


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "live_eval_realworld_rfc",
        Path(__file__).resolve().parent.parent / "scripts" / "live_eval_realworld.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["live_eval_realworld_rfc"] = mod
    spec.loader.exec_module(mod)  # type: ignore[arg-type]
    return mod


_M = _load_module()


def _ev(t, **payload):
    return SimpleNamespace(event_type=t, payload=payload)


def _row(**kw):
    r = _M.CaseResult(id="x", language="cpp", dataset="d")
    r.escalated = True
    r.matches_oracle = 0.99
    for k, v in kw.items():
        setattr(r, k, v)
    return r


# ---------------------------------------------------------------------------
# S28-170(1): the compile-evidence-missing guard
# ---------------------------------------------------------------------------

_TRUST = _ev("acceptance_trust", decision="PROPOSE_FOR_REVIEW",
             reasons=["unknown oracle(s) — compile evidence missing for: f.cpp"])
_DEGRADE = _ev("build_state", state="SYNTAX_ONLY", reason="timed out")
_GATE_TIMEOUT = _ev("tests_finished", timed_out=True)
_PROBE_TIMEOUT = _ev("build_probe", outcome="timeout", duration_s=300.0)


def test_trust_plus_timeout_is_compile_evidence_missing():
    assert _M._compile_evidence_missing([_TRUST, _GATE_TIMEOUT])
    assert _M._compile_evidence_missing([_TRUST, _DEGRADE])
    assert _M._compile_evidence_missing([_TRUST, _PROBE_TIMEOUT])


def test_trust_alone_is_not_enough():
    # the confession needs the missed deadline that explains it
    assert not _M._compile_evidence_missing([_TRUST])


def test_timeout_alone_is_not_enough():
    # a timeout without the engine's proposal is just a slow case
    assert not _M._compile_evidence_missing([_GATE_TIMEOUT, _PROBE_TIMEOUT])


def test_other_review_reasons_do_not_qualify():
    other = _ev("acceptance_trust", decision="PROPOSE_FOR_REVIEW",
                reasons=["churn convention tie"])
    assert not _M._compile_evidence_missing([other, _DEGRADE])


def test_verdict_chain_reads_unverified():
    r = _row(compile_evidence_missing=True)
    assert _M._verdict_chain(r) == "UNVERIFIED"


def test_escalated_without_the_guard_stays_escalate():
    r = _row()
    assert _M._verdict_chain(r) == "ESCALATE"


# ---------------------------------------------------------------------------
# S28-170(2): the repeat-flip predicate and the rank order
# ---------------------------------------------------------------------------

def test_pass_repeat_beats_escalate_kept():
    r = _row(verdict="ESCALATE", matches_oracle=0.0,
             best_repeat_verdict="PASS", best_repeat_sim=0.988)
    assert _M._is_repeat_flip(r)


def test_same_rank_sim_flip():
    r = _row(verdict="PASS", matches_oracle=0.90,
             best_repeat_verdict="PASS", best_repeat_sim=0.995)
    assert _M._is_repeat_flip(r)


def test_kept_verdict_that_beats_its_repeats_is_not_a_flip():
    r = _row(verdict="PASS", matches_oracle=0.99,
             best_repeat_verdict="ESCALATE", best_repeat_sim=0.0)
    assert not _M._is_repeat_flip(r)


def test_single_run_rows_never_flip():
    r = _row(verdict="ESCALATE", best_repeat_verdict="", best_repeat_sim=None)
    assert not _M._is_repeat_flip(r)


def test_rank_order():
    assert (_M._verdict_rank("PASS") > _M._verdict_rank("WORKING")
            > _M._verdict_rank("NEAR_MATCH")
            > _M._verdict_rank("GATE_UNAVAILABLE")
            > _M._verdict_rank("ESCALATE")
            > _M._verdict_rank("ORACLE_DIVERGENT"))
    assert _M._verdict_rank("SOMETHING_NEW") == 0  # unknown verdicts sort low

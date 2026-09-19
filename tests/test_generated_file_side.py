"""S28-103: the generated-file side-take arm — census-licensed (zero-FP
block detection on the php family; churn-winner oracle-sim 0.926-1.000 on
all four members). Build-generated conflict files resolve by taking the
churn-winner side; handwritten files decline and the ladder is unchanged.
"""

from __future__ import annotations

from pathlib import Path

from capybase.config import Config
from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.orchestrator import Orchestrator
from capybase.resolution_engine import ResolutionEngine

from tests.conftest import git

_ARG_BLOCK = (
    "/* gen */\n"
    "ZEND_BEGIN_ARG_INFO_EX(arginfo_x, 0, 0, 1)\n"
    "ZEND_ARG_INFO(0, a)\n"
    "ZEND_END_ARG_INFO()\n"
)


def _unit(cur: str, rep: str, base: str = "") -> ConflictUnit:
    """A unit whose worktree text is the real marker block (validation
    splices the candidate into it, so the span must be authentic)."""
    header = "/* generated header */\n"
    owt = (header + "<<<<<<< HEAD\n" + cur + "=======\n" + rep
           + ">>>>>>> branch\n")
    lines = owt.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("<<<<<<<")) 
    end = next(i for i, l in enumerate(lines) if l.startswith(">>>>>>>"))
    return ConflictUnit(
        session_id="s", step_index=0, path="ext/x/arginfo.h",
        language="c", unit_id="ext/x/arginfo.h:0",
        base=ConflictSide(label="BASE", text=base),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text=cur),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text=rep),
        original_worktree_text=owt,
        marker_span=(start, end),
    )


def _orch(repo: Path) -> Orchestrator:
    cfg = Config()
    cfg.model.model = "fake"
    cfg.tests.required = False
    cfg.tests.pre_continue = "true"
    cfg.tests.final = "true"
    return Orchestrator(
        cfg, repo=str(repo), resolution_engine=ResolutionEngine(cfg.model),
        out=lambda *_a, **_k: None,
    )


def _gen_sides(cur_extra: str = "ZEND_ARG_INFO(0, b)\n", rep_extra: str = ""):
    """Two arginfo-carrying sides with DIFFERENT churn: the current side
    carries one more arg line (the newer regeneration)."""
    cur = _ARG_BLOCK.replace("ZEND_END", cur_extra + "ZEND_END")
    rep = _ARG_BLOCK + rep_extra
    base = _ARG_BLOCK
    return cur, rep, base


def test_generated_file_takes_churn_winner_side(repo: Path):
    orch = _orch(repo)
    cur, rep, base = _gen_sides()
    unit = _unit(cur, rep, base)
    outcome = orch._try_generated_file_side(unit)
    assert outcome is not None and outcome.accepted is not None
    cand = outcome.accepted
    assert cand.provenance == "deterministic_generated_file_side"
    # churn: current added a line vs base; replayed identical to base -> current wins
    assert cand.resolved_text == cur
    accepted = [e for e in orch.journal.read_events()
                if e.event_type == "candidate_accepted"]
    assert accepted and accepted[-1].payload["via"] == "generated_file_side"
    assert accepted[-1].payload["side"] == "current"


def test_handwritten_file_declines(repo: Path):
    """Direction 2: no complete arginfo blocks -> the arm declines and the
    cascade/ladder is untouched (bare ZEND_ macros are NOT a signature —
    the census's false-positive lesson)."""
    orch = _orch(repo)
    # bare macros, no BEGIN..END block
    cur = "ZEND_RESULT(x)\nint a;\n" * 3
    rep = "ZEND_RESULT(y)\nint b;\n" * 3
    unit = _unit(cur, rep, "int base;\n")
    assert orch._try_generated_file_side(unit) is None
    # an UNTERMINATED begin block is not a complete signature either
    unit2 = _unit("ZEND_BEGIN_ARG_INFO_EX(arginfo, 0, 0, 1)\nint a;\n",
                  "ZEND_BEGIN_ARG_INFO_EX(arginfo, 0, 0, 1)\nint b;\n", "")
    assert orch._try_generated_file_side(unit2) is None


def test_replayed_churn_winner_is_taken(repo: Path):
    """The policy is churn-driven, not current-biased: when the replayed
    side carries the newer generation, it wins."""
    orch = _orch(repo)
    cur, rep, base = _gen_sides(rep_extra="ZEND_ARG_INFO(0, z)\nZEND_ARG_INFO(0, z2)\n")
    unit = _unit(cur, rep, base)
    outcome = orch._try_generated_file_side(unit)
    assert outcome is not None
    assert outcome.accepted.resolved_text == rep

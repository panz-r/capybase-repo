"""S28-233/243 (queue item 4) — the terminal-path arms.

0052's trial15 ground truth: all sibling units resolve, one unit
exhausts (the no-progress guard on the unterminated docstring seam),
and the file's escalated exit returns BEFORE Phase 2 — the beam's
arms are structurally unreachable there (zero file_validated /
whole_file_repair events in the session journal). The terminal
engagement runs at that exit: substrate = accepted splices + the
escalated unit's best attempt; the pystring closer first; the arm's
whole-file output re-validated before it may rescue.
"""

from __future__ import annotations

from types import SimpleNamespace

from capybase.conflict_model import (
    CandidateResolution,
    ConflictSide,
    ConflictUnit,
)
from capybase.orchestrator import Orchestrator


def _unit(uid: str, span):
    return ConflictUnit(
        session_id="s", step_index=1, path="sklearn/svm/classes.py",
        language="python",
        conflict_type="UU", unit_id=uid, unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text=""),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="x = 1\n"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="y = 2\n"),
        original_worktree_text=(
            "def fit(self):\n"
            "    '''Fit the model.\n"
            "    <<<<<<< H\n"
            "    x = 1\n"
            "    =======\n"
            "    y = 2\n"
            "    >>>>>>> b\n"
            "    '''\n"
            "    return 1\n"
        ),
        marker_span=span,
    )


def _cand(text: str, cid: str = "c1"):
    return CandidateResolution(
        candidate_id=cid, unit_id="u", model_name="m",
        prompt_version="resolve_text_block.v6", resolved_text=text,
        provenance="plain_llm", self_reported_confidence=0.9,
    )


class _FakeJournal:
    def __init__(self):
        self.events = []

    def emit(self, et, payload, **kw):
        self.events.append((et, payload))


def _orchestrator(monkeypatch, gate_passed: bool):
    orch = object.__new__(Orchestrator)
    orch.journal = _FakeJournal()
    orch.step = 1
    orch.git = SimpleNamespace(repo="/tmp")
    orch.verification = SimpleNamespace(
        verify_file=lambda *a, **k: SimpleNamespace(
            passed=gate_passed,
            hard_failures=([SimpleNamespace(message="SyntaxError: bad")]
                           if not gate_passed else [])),
    )
    return orch


def _esc_outcome():
    """The exhausted unit: attempts carry the seam defect; the last
    validation names the unterminated string."""
    u = _unit("sklearn/svm/classes.py:1:5", (2, 7))
    attempts = [_cand("    '''Fit the model.\n    x = 1\n", "a1")]
    validation = SimpleNamespace(hard_failures=[SimpleNamespace(
        message="SyntaxError: unterminated triple-quoted string literal "
                "(detected at line 2)")])
    return SimpleNamespace(unit=u, attempts=attempts,
                           validation=validation)


def _accepted():
    """Two sibling units already resolved (the 0052 shape)."""
    return [
        (_unit("sklearn/svm/classes.py:1:0", (0, 2)),
         _cand("def fit(self):\n", "s0")),
        (_unit("sklearn/svm/classes.py:1:6", (7, 9)),
         _cand("    '''\n    return 1\n", "s6")),
    ]


def test_terminal_arms_rescue_when_the_gate_passes(monkeypatch):
    orch = _orchestrator(monkeypatch, gate_passed=True)
    accepted = _accepted()
    # the arm splices the seam defect into the buffer and closes it —
    # stub the arm to return the closed whole file
    import capybase.orchestrator as om

    def _fake_arm(failures, original, acc, idx):
        assert idx == 2  # the escalated unit is the substrate's last
        wf_u = _unit("sklearn/svm/classes.py:1:5", None)
        wf_u = wf_u.model_copy(update={"marker_span": None,
                                       "unit_kind": "whole_file"})
        return [(wf_u, _cand("def fit(self):\n    '''Fit the model.\n"
                             "    x = 1\n    '''\n    return 1\n",
                             "a1:pystringfix"))], "repaired"

    monkeypatch.setattr(om, "_try_deterministic_pystring_repair", _fake_arm)
    out = orch._terminal_path_arms("sklearn/svm/classes.py", accepted,
                                   _esc_outcome())
    assert out is not None
    assert out[0][1].resolved_text.startswith("def fit(self):")
    kinds = [e[0] for e in orch.journal.events]
    assert "terminal_arms_engaged" in kinds
    assert "terminal_arm_applied" in kinds


def test_terminal_arms_decline_on_non_string_failures(monkeypatch):
    orch = _orchestrator(monkeypatch, gate_passed=True)
    import capybase.orchestrator as om

    def _fake_arm(failures, original, acc, idx):
        return None, "not_string_failure"

    monkeypatch.setattr(om, "_try_deterministic_pystring_repair", _fake_arm)
    out = orch._terminal_path_arms("sklearn/svm/classes.py", _accepted(),
                                   _esc_outcome())
    assert out is None
    kinds = [e[0] for e in orch.journal.events]
    assert "terminal_arms_declined" in kinds


def test_terminal_arms_decline_when_the_gate_rejects(monkeypatch):
    """The rescue must re-validate: a gate-rejected arm output never
    lands."""
    orch = _orchestrator(monkeypatch, gate_passed=False)
    import capybase.orchestrator as om

    def _fake_arm(failures, original, acc, idx):
        wf_u = _unit("sklearn/svm/classes.py:1:5", None).model_copy(
            update={"marker_span": None, "unit_kind": "whole_file"})
        return [(wf_u, _cand("def fit(self):\n", "a1:pystringfix"))], "repaired"

    monkeypatch.setattr(om, "_try_deterministic_pystring_repair", _fake_arm)
    out = orch._terminal_path_arms("sklearn/svm/classes.py", _accepted(),
                                   _esc_outcome())
    assert out is None
    declined = [p for et, p in orch.journal.events
                if et == "terminal_arms_declined"]
    assert declined and declined[0]["reason"] == "gate_rejected"


def test_terminal_arms_decline_without_a_usable_attempt():
    orch = _orchestrator(None, gate_passed=True)
    esc = _esc_outcome()
    esc.attempts = []
    assert orch._terminal_path_arms("sklearn/svm/classes.py", _accepted(),
                                    esc) is None

"""S28-233/243 (queue item 4) — the terminal-path arms.

0052's trial15 ground truth: the session is the per-unit loop only —
zero file_validated / whole_file_repair events; the escalated exit
returns BEFORE Phase 2 and the beam's arms are structurally
unreachable there. The terminal engagement runs at that exit, ONCE
per file, with the FULL near-miss assembly as substrate (the S28-257
pilot: a partial substrate splices a fragment of the file and the
seam is invisible to it — six no_imbalance declines). The pystring
closer runs first; its whole-file output is re-validated before it
may rescue.
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


def _orchestrator(gate_passed: bool):
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


_PYSTR_FAIL = SimpleNamespace(
    message="SyntaxError: unterminated triple-quoted string literal "
            "(detected at line 2)")


def _near_miss_substrate():
    """The FULL near-miss assembly: accepted splices + every exhausted
    unit's best attempt (the S28-257 pilot's substrate fix)."""
    esc_unit = _unit("sklearn/svm/classes.py:1:5", (2, 7))
    esc_attempt = _cand("    '''Fit the model.\n    x = 1\n", "a1")
    esc_failures = [SimpleNamespace(
        message="SyntaxError: unterminated triple-quoted string literal "
                "(detected at line 2)")]
    substrate = [
        (_unit("sklearn/svm/classes.py:1:0", (0, 2)),
         _cand("def fit(self):\n", "s0")),
        (_unit("sklearn/svm/classes.py:1:6", (7, 9)),
         _cand("    '''\n    return 1\n", "s6")),
        (esc_unit, esc_attempt),
    ]
    return substrate, esc_failures


def test_terminal_arms_rescue_when_the_gate_passes(monkeypatch):
    orch = _orchestrator(gate_passed=True)
    substrate, failures = _near_miss_substrate()
    import capybase.orchestrator as om

    def _fake_arm(failures, original, acc, idx):
        assert idx == len(acc) - 1  # the last substrate entry
        assert any("unterminated" in (getattr(f, "message", "") or "")
                   for f in failures)
        wf_u = _unit("sklearn/svm/classes.py:1:5", None).model_copy(
            update={"marker_span": None, "unit_kind": "whole_file"})
        fixed = ("def fit(self):\n    '''Fit the model.\n"
                 "    x = 1\n    '''\n    return 1\n")
        return [(wf_u, _cand(fixed, "a1:pystringfix"))], "repaired"

    monkeypatch.setattr(om, "_try_deterministic_pystring_repair", _fake_arm)
    out = orch._terminal_path_arms("sklearn/svm/classes.py", substrate,
                                   [_PYSTR_FAIL])
    assert out is not None
    assert out[0][1].resolved_text.startswith("def fit(self):")
    kinds = [e[0] for e in orch.journal.events]
    assert kinds.count("terminal_arms_engaged") == 1  # ONCE per file
    assert "terminal_arm_applied" in kinds


def test_terminal_arms_decline_on_non_string_failures(monkeypatch):
    orch = _orchestrator(gate_passed=True)
    substrate, _ = _near_miss_substrate()
    import capybase.orchestrator as om

    def _fake_arm(failures, original, acc, idx):
        return None, "not_string_failure"

    monkeypatch.setattr(om, "_try_deterministic_pystring_repair", _fake_arm)
    out = orch._terminal_path_arms(
        "sklearn/svm/classes.py", substrate,
        [SimpleNamespace(message="SyntaxError: invalid syntax")])
    assert out is None
    declined = [p for et, p in orch.journal.events
                if et == "terminal_arms_declined"]
    assert declined and declined[0]["reason"] == "not_string_failure"


def test_terminal_arms_decline_when_the_gate_rejects(monkeypatch):
    """The rescue must re-validate: a gate-rejected arm output never
    lands."""
    orch = _orchestrator(gate_passed=False)
    substrate, failures = _near_miss_substrate()
    import capybase.orchestrator as om

    def _fake_arm(failures, original, acc, idx):
        wf_u = _unit("sklearn/svm/classes.py:1:5", None).model_copy(
            update={"marker_span": None, "unit_kind": "whole_file"})
        return [(wf_u, _cand("def fit(self):\n", "a1:pystringfix"))], "repaired"

    monkeypatch.setattr(om, "_try_deterministic_pystring_repair", _fake_arm)
    out = orch._terminal_path_arms("sklearn/svm/classes.py", substrate,
                                   [_PYSTR_FAIL])
    assert out is None
    declined = [p for et, p in orch.journal.events
                if et == "terminal_arms_declined"]
    assert declined and declined[0]["reason"] == "gate_rejected"


def test_terminal_arms_decline_on_an_empty_substrate():
    orch = _orchestrator(gate_passed=True)
    assert orch._terminal_path_arms(
        "sklearn/svm/classes.py", [],
        [SimpleNamespace(message="SyntaxError: unterminated")]) is None


# ---------------------------------------------------------------------------
# S28-259.2: anti-reroll v2 — the whitespace-normalized identity
# ---------------------------------------------------------------------------

def test_whitespace_equal():
    from capybase.orchestrator import _whitespace_equal
    # blank-only deltas are rerolls
    assert _whitespace_equal("a = 1\n\n\nb = 2\n", "a = 1\nb = 2\n")
    # indentation-only deltas are rerolls
    assert _whitespace_equal("    x = f(1, 2)\n", "x = f(1, 2)\n")
    # content deltas are not
    assert not _whitespace_equal("a = 1\n", "a = 2\n")
    assert not _whitespace_equal("", "x = 1\n")

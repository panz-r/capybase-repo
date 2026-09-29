"""S28-358 session gate-pass cache — the repeat-diff DIB, built.

The t44 duckdb-0001 anatomy: byte-identical structural candidates
(sha-verified) PASSED the file gate in one repeat and FAILED it in the
other; the failure cascaded (repair -> 300s timeout -> session
degrade) into the side-takeover rescue, which REPLACED the near-oracle
content with the wholesale current side. Nothing remembered the buffer
had passed verify_file once that session.

The build: stash gate-passing buffers (a GATE fact only — never a
preservation heuristic, S28-341); on a later failed gate, re-validate
the stash through the same gate and prefer it on pass. The tests drive
``_gate_pass_cache_lookup`` through the ``Orchestrator.__new__``
harness with a stubbed gate.
"""

from __future__ import annotations

import hashlib

from types import SimpleNamespace

from capybase.conflict_model import ConflictSide, ConflictUnit
from capybase.orchestrator import Orchestrator


class _RecJournal:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, payload, **_kw):
        self.events.append((event, payload))


def _sha(t):
    return hashlib.sha1((t or "").encode()).hexdigest()[:16]


def _unit():
    return ConflictUnit(
        session_id="s", step_index=0, path="src/main/config.cpp",
        language="cpp", conflict_type="UU", unit_id="src/main/config.cpp:1:0",
        unit_kind="text_marker_block",
        base=ConflictSide(label="BASE", text="x\n"),
        current=ConflictSide(label="CURRENT_UPSTREAM_SIDE", text="y\n"),
        replayed=ConflictSide(label="REPLAYED_COMMIT_SIDE", text="z\n"),
        original_worktree_text="x\n", marker_span=(0, 0),
    )


def _orch(pass_for=()):
    """A stubbed gate: passes exactly the texts in `pass_for`."""
    orch = Orchestrator.__new__(Orchestrator)
    orch.journal = _RecJournal()
    orch.step = 0
    orch.git = SimpleNamespace(repo="/tmp/does-not-exist")

    def _gate(path, language, original, spans, *, whole_text=None, **kw):
        return SimpleNamespace(
            passed=whole_text in pass_for, hard_failures=[])

    orch.verification = SimpleNamespace(verify_file=_gate)
    return orch


def _stash(orch, buffers):
    orch._gate_pass_cache = {"src/main/config.cpp": [(_sha(b), b)
                                                     for b in buffers]}


GOOD = "int near_oracle(void) { return 0; }\n"
OTHER = "int other_passing(void) { return 1; }\n"
BAD = "int just_failed(void) { return };\n"


def test_stash_head_rescues_on_pass():
    orch = _orch(pass_for={GOOD})
    _stash(orch, [GOOD])
    units = [_unit()]
    out = orch._gate_pass_cache_lookup(
        "src/main/config.cpp", "cpp", "x\n", units, BAD)
    assert out is not None
    accepted, buf, val = out
    assert buf == GOOD and val.passed
    u, c = accepted[0]
    assert u.marker_span is None and u.unit_kind == "whole_file"
    assert c.provenance == "deterministic_gate_pass_cache"
    assert "gatepasscache" in c.candidate_id
    kinds = [e for e, _ in orch.journal.events]
    assert "gate_pass_cache_rescued" in kinds


def test_just_failed_buffer_is_skipped():
    # the current (failed) buffer is ALSO stashed from earlier in the
    # session — re-validating it would just replay the failure
    orch = _orch(pass_for={OTHER})
    _stash(orch, [BAD, OTHER])
    out = orch._gate_pass_cache_lookup(
        "src/main/config.cpp", "cpp", "x\n", [_unit()], BAD)
    assert out is not None and out[1] == OTHER


def test_all_entries_fail_declines_visibly():
    orch = _orch(pass_for=set())  # the gate passes nothing now
    _stash(orch, [GOOD, OTHER])
    out = orch._gate_pass_cache_lookup(
        "src/main/config.cpp", "cpp", "x\n", [_unit()], BAD)
    assert out is None
    kinds = [e for e, _ in orch.journal.events]
    assert "gate_pass_cache_declined" in kinds
    assert "gate_pass_cache_rescued" not in kinds


def test_empty_stash_declines_without_gate_calls():
    orch = _orch(pass_for={GOOD})
    out = orch._gate_pass_cache_lookup(
        "src/main/config.cpp", "cpp", "x\n", [_unit()], BAD)
    assert out is None
    assert orch.journal.events == []


def test_wiring_flag_and_provenance():
    from capybase.config import Config
    assert Config().future.enable_gate_pass_cache is False
    from capybase.provenance import PROVENANCE_VALUES
    assert "deterministic_gate_pass_cache" in PROVENANCE_VALUES

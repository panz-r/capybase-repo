"""S28-184(1) + S28-182(a)/183 — flight-recorder round preservation and
the guarded whole-file repair boundary.

S28-184(1): repair rounds reuse attempt indices and used to overwrite
their predecessors — the duckdb-0093 paren (recorded response had it,
stored buffer did not) was unattributable as a result. Distinct rounds
now land in .rN siblings; identical rounds dedupe.

S28-182(a)/183: the whole-file repair boundary journals WHAT the edit
did (repair_edit_shape: lines removed/added/removed-nonblank) and —
flag-gated — declines rounds that unbalance a previously-balanced
()/{} pair (the duckdb-0093 shape: a one-char ';' defect became a paren
loss via the repair's reflow).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from capybase.journal import Journal, SessionPaths


def _journal(tmp_path):
    paths = SessionPaths("sess1", tmp_path)
    return Journal(paths=paths)


# ---------------------------------------------------------------------------
# S28-184(1): round preservation
# ---------------------------------------------------------------------------

def test_same_round_same_content_dedupes(tmp_path):
    j = _journal(tmp_path)
    p1 = j.store_prompt("u", 0, "same text")
    p2 = j.store_prompt("u", 0, "same text")
    assert p1 == p2


def test_same_round_different_content_preserved_as_sibling(tmp_path):
    j = _journal(tmp_path)
    j.store_response("u", 0, "first round")
    p2 = j.store_response("u", 0, "repair round — different")
    assert p2.name.endswith(".r1.txt")
    assert "repair round" in p2.read_text(encoding="utf-8")
    original = p2.parent / p2.name.replace(".r1.txt", ".txt")
    assert "first round" in original.read_text(encoding="utf-8")


def test_third_distinct_round_gets_r2(tmp_path):
    j = _journal(tmp_path)
    j.store_prompt("u", 1, "a")
    j.store_prompt("u", 1, "b")
    p3 = j.store_prompt("u", 1, "c")
    assert p3.name.endswith(".r2.txt")


# ---------------------------------------------------------------------------
# S28-182(a)/183: the guarded repair boundary
# ---------------------------------------------------------------------------

class _RecJournal:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, payload, **_kw):
        self.events.append((event, payload))


_BALANCED = "int f() {\n    return 1;\n}\n"
_OTHER = "int g() {\n    return 2;\n}\n"
_UNBALANCED = "int f() {\n    return 1;\n"  # the closer `}` lost


def _orch(*, guard: bool):
    import capybase.orchestrator as orch_mod
    orch = orch_mod.Orchestrator.__new__(orch_mod.Orchestrator)
    orch.journal = _RecJournal()
    orch.step = 0
    orch.config = SimpleNamespace(future=SimpleNamespace(
        enable_repair_delimiter_guard=guard))
    return orch


@pytest.fixture()
def buffer_sequence(monkeypatch):
    """_resolved_buffer returns the listed buffers in call order
    (call 1 = the pre-repair splice, call 2 = the post-repair splice)."""
    import capybase.orchestrator as orch_mod
    state = {"texts": [], "n": 0}

    def _wire(*texts):
        state["texts"] = list(texts)
        state["n"] = 0

    def fake(original, accepted):
        i = min(state["n"], len(state["texts"]) - 1)
        state["n"] += 1
        return state["texts"][i]

    monkeypatch.setattr(orch_mod, "_resolved_buffer", fake)
    return _wire


def test_delimiter_loss_declines_when_guard_on(buffer_sequence):
    import capybase.orchestrator as orch_mod
    orch = _orch(guard=True)
    det = "repaired"
    orch._whole_file_repair = lambda *a, **k: det
    buffer_sequence(_BALANCED, _UNBALANCED)  # balanced -> closer lost
    out = orch._guarded_whole_file_repair("f.cpp", None, "orig", [])
    assert out is None  # declined: the pre-repair state was closer
    events = [e for e, _ in orch.journal.events]
    assert "repair_edit_shape" in events
    loss = [p for e, p in orch.journal.events
            if e == "repair_delimiter_loss"]
    assert loss and "{} balance 0->1" in loss[0]["loss"][0]


def test_delimiter_loss_journaled_but_kept_when_guard_off(buffer_sequence):
    import capybase.orchestrator as orch_mod
    orch = _orch(guard=False)
    det = "repaired"
    orch._whole_file_repair = lambda *a, **k: det
    buffer_sequence(_BALANCED, _UNBALANCED)
    out = orch._guarded_whole_file_repair("f.cpp", None, "orig", [])
    assert out == det  # journal-only when the flag is off
    events = [e for e, _ in orch.journal.events]
    assert "repair_delimiter_loss" in events
    shape = [p for e, p in orch.journal.events
             if e == "repair_edit_shape"][0]
    assert shape["lines_removed"] > 0  # the closer line vanished


def test_balanced_to_balanced_passes_with_shape(buffer_sequence):
    import capybase.orchestrator as orch_mod
    orch = _orch(guard=True)
    det = "repaired"
    orch._whole_file_repair = lambda *a, **k: det
    buffer_sequence(_OTHER, _OTHER)
    out = orch._guarded_whole_file_repair("f.cpp", None, "orig", [])
    assert out == det
    events = [e for e, _ in orch.journal.events]
    assert "repair_delimiter_loss" not in events
    assert "repair_edit_shape" in events


def test_unrepair_declines_silently():
    orch = _orch(guard=True)
    orch._whole_file_repair = lambda *a, **k: None
    out = orch._guarded_whole_file_repair("f.cpp", None, "orig", [])
    assert out is None
    events = [e for e, _ in orch.journal.events]
    assert "repair_edit_shape" not in events

"""Whole-side repair rung — sprint-19 P1 (the tokio-0109/0037 class).

When the spliced buffer fails a whole-file COMPILE gate, the pristine
merge-index stage sides are probed as whole-file candidates. These tests
pin: the compile-flavor classifier (tagged build failures only — splice
coherence stays with the deterministic repairs), the two adjudication
prompts (single-compiling-side subsumption; both-compile with the
explicit "neither" escape), and the rung's decision matrix end-to-end on
a stub orchestrator (declines leave the repair loop's state untouched).
No network; the engine is always a fake.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from capybase.orchestrator import (
    Orchestrator,
    _is_compile_flavored_failure,
    _whole_side_repair_prompt_both,
    _whole_side_repair_prompt_single,
)


def _fail(validator: str, message: str, detail: dict | None = None):
    return SimpleNamespace(
        validator=validator, message=message, detail=detail or {})


# ---------------------------------------------------------------------------
# _is_compile_flavored_failure — the rung's trigger
# ---------------------------------------------------------------------------

def test_classifier_accepts_build_test():
    assert _is_compile_flavored_failure(
        [_fail("build_test", "src/x.c:12:5: error: use of undeclared 'x'")]) is True


def test_classifier_accepts_tagged_whole_file_build():
    assert _is_compile_flavored_failure(
        [_fail("syntax", "src/x.c:9:2: error: expected ';'",
               {"source": "whole_file_build"})]) is True


def test_classifier_accepts_cargo_prefix():
    assert _is_compile_flavored_failure(
        [_fail("syntax", "cargo check: 2 new error(s): ...")]) is True


def test_classifier_rejects_splice_coherence():
    # brace/preprocessor imbalance is the deterministic repair's territory
    assert _is_compile_flavored_failure(
        [_fail("syntax", "splice coherence: unbalanced braces at line 41",
               {"brace_imbalance_line": 41})]) is False


def test_classifier_rejects_standalone_parse_and_empty():
    assert _is_compile_flavored_failure(
        [_fail("syntax", "py_compile: 1 new error(s): invalid syntax")]) is False
    assert _is_compile_flavored_failure([]) is False


# ---------------------------------------------------------------------------
# Adjudication prompts
# ---------------------------------------------------------------------------

def _prompt_texts():
    base = "int a;\nint b;\nint c;\n"
    cur = "int a;\nint b2;\nint c;\n"          # refines b
    rep = "int a;\nint b;\nint c;\nint d;\n"   # adds d
    return base, {"current": cur, "replayed": rep}


def test_single_prompt_carries_both_diffs_and_verdicts():
    base, sides = _prompt_texts()
    p = _whole_side_repair_prompt_single(
        "f.rs", "rust", base, sides, ok_side="current")
    assert "-int b;" in p and "+int b2;" in p      # compiling side's diff
    assert "+int d;" in p                          # failing side's diff
    assert "compiles cleanly" in p
    assert "does NOT compile" in p
    assert '"verdict": "keep" or "superseded"' in p


def test_single_prompt_labels_swap_for_replayed_ok_side():
    base, sides = _prompt_texts()
    p = _whole_side_repair_prompt_single(
        "f.rs", "rust", base, sides, ok_side="replayed")
    assert "REPLAYED (the commit being applied on top) compiles cleanly" in p
    assert ("CURRENT (upstream, being rebased onto)'s changes vs BASE "
            "(this version does NOT compile)") in p


def test_both_prompt_has_neither_escape():
    base, sides = _prompt_texts()
    p = _whole_side_repair_prompt_both("f.rs", "rust", base, sides)
    assert "FAILED to compile" in p
    assert "+int b2;" in p and "+int d;" in p     # both diffs present
    assert '"choice": "current" or "replayed" or "neither"' in p
    assert "weave BOTH sides" in p                 # the woven-class escape


# ---------------------------------------------------------------------------
# _try_whole_side_repair_rung — decision matrix on a stub orchestrator
# ---------------------------------------------------------------------------

class _RecJournal:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, payload, **_kw):
        self.events.append((event, payload))


class _ScriptedEngine:
    """raw_complete pops scripted responses; records every call.

    When the script is exhausted, the LAST response repeats — the
    self-consistency adjudicators draw 3 samples, and a single scripted
    verdict should read as a unanimous verdict (an IndexError mid-loop
    would abort the whole adjudication)."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self._last = ""
        self.calls: list[str] = []
        self.config = SimpleNamespace(max_tokens=8192)

    def raw_complete(self, prompt, *, json_mode=False, temperature=None,
                     max_tokens=None):
        self.calls.append(prompt)
        if self._responses:
            self._last = self._responses.pop(0)
        return SimpleNamespace(text=self._last)


class _FakeGit:
    def __init__(self, stages: dict[int, str]):
        self._stages = stages
        self.repo = "/tmp/fake-repo"

    def read_stage_blob(self, path: str, stage: int) -> bytes:
        if stage not in self._stages:
            raise RuntimeError(f"no stage {stage}")
        return self._stages[stage].encode()


class _PassVer:
    def __init__(self, ok_texts: set[str] | None = None):
        self.ok_texts = ok_texts
        self.calls: list[str | None] = []

    def verify_file(self, path, language, original, units, *, repo_root=None, pristine_side_texts=None,
                    whole_text=None):
        self.calls.append(whole_text)
        ok = self.ok_texts is None or (whole_text in self.ok_texts)
        return SimpleNamespace(
            passed=ok,
            hard_failures=[] if ok else [
                SimpleNamespace(message="error[E0308]: mismatched types")],
        )


_BASE = "".join(f"base line {i}\n" for i in range(50))
_CUR = _BASE.replace("base line 3\n", "cur line 3\n")
_REP = _BASE.replace("base line 7\n", "rep line 7\n")
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


def _orch(engine, ver, *, enabled=True, wall_deadline=None) -> tuple:
    """Stub orchestrator + (path, language, original, units, buffer, deadline)."""
    orch = object.__new__(Orchestrator)
    orch.resolution_engine = engine
    orch.journal = _RecJournal()
    orch.step = 1
    orch.git = _FakeGit({1: _BASE, 2: _CUR, 3: _REP})
    orch.verification = ver
    orch.config = SimpleNamespace(
        future=SimpleNamespace(enable_whole_side_repair_rung=enabled))
    writes: list[str] = []
    orch._write_worktree_only = (
        lambda path, buffer, *, accepted=None: writes.append(buffer))
    orch._writes = writes
    return orch, ("f.c", "c", _SPLICED, _units(), _SPLICED, wall_deadline)


def _declined(orch, reason: str) -> None:
    decl = [p for e, p in orch.journal.events
            if e == "whole_side_repair_declined"]
    assert decl and decl[-1]["reason"] == reason


def test_no_side_verifies_declines_and_restores():
    orch, args = _orch(_ScriptedEngine([]), _PassVer(ok_texts=set()))
    out = orch._try_whole_side_repair_rung(*args[:5], wall_deadline=args[5])
    assert out is None
    _declined(orch, "no_side_verifies")
    probes = [p for e, p in orch.journal.events if e == "whole_side_probe"]
    assert len(probes) == 2 and all(not p["passed"] for p in probes)
    # worktree restored to the spliced buffer after the failed probes
    assert orch._writes[-1] == _SPLICED


def test_single_compiling_side_keep_declines():
    orch, args = _orch(
        _ScriptedEngine(
            ['{"verdict": "keep", "confidence": 0.9, "reason": "new feature"}']),
        _PassVer(ok_texts={_CUR}))
    out = orch._try_whole_side_repair_rung(*args[:5], wall_deadline=args[5])
    assert out is None
    _declined(orch, "adjudication_declined")
    assert orch._writes[-1] == _SPLICED  # restored, not left on the side


def test_single_compiling_side_superseded_swaps():
    orch, args = _orch(
        _ScriptedEngine(
            ['{"verdict": "superseded", "confidence": 0.95,'
             ' "reason": "cosmetic"}']),
        _PassVer(ok_texts={_CUR}))
    out = orch._try_whole_side_repair_rung(*args[:5], wall_deadline=args[5])
    assert out is not None
    accepted, buffer, val = out
    assert buffer == _CUR
    assert len(accepted) == 1
    unit, cand = accepted[0]
    assert unit.unit_kind == "whole_file" and unit.marker_span is None
    assert cand.resolved_text == _CUR
    assert cand.provenance == "deterministic_source_current_only_stage"
    assert cand.model_name == "whole_side_repair"
    swap = [p for e, p in orch.journal.events if e == "whole_side_repair"]
    assert swap and swap[0]["side"] == "current"
    assert swap[0]["via"] == "subsumption_adjudication"


def test_single_compiling_side_low_confidence_declines():
    orch, args = _orch(
        _ScriptedEngine(
            ['{"verdict": "superseded", "confidence": 0.55,'
             ' "reason": "unsure"}']),
        _PassVer(ok_texts={_CUR}))
    assert orch._try_whole_side_repair_rung(
        *args[:5], wall_deadline=args[5]) is None
    _declined(orch, "adjudication_declined")


def test_both_compile_neither_declines():
    orch, args = _orch(
        _ScriptedEngine(
            ['{"choice": "neither", "confidence": 0.9,'
             ' "reason": "must weave both"}']),
        _PassVer())  # everything passes → both sides verify
    out = orch._try_whole_side_repair_rung(*args[:5], wall_deadline=args[5])
    assert out is None
    _declined(orch, "adjudication_declined")
    adj = [p for e, p in orch.journal.events
           if e == "whole_side_repair_adjudication"]
    assert adj and adj[0]["branch"] == "both_compile"


def test_both_compile_confident_pick_swaps():
    orch, args = _orch(
        _ScriptedEngine(
            ['{"choice": "current", "confidence": 0.85,'
             ' "reason": "replayed deletions superseded"}']),
        _PassVer())
    out = orch._try_whole_side_repair_rung(*args[:5], wall_deadline=args[5])
    assert out is not None
    accepted, buffer, val = out
    assert buffer == _CUR
    swap = [p for e, p in orch.journal.events if e == "whole_side_repair"]
    assert swap and swap[0]["via"] == "repair_adjudication"


def test_both_compile_unparseable_declines():
    orch, args = _orch(_ScriptedEngine(["not json"]), _PassVer())
    assert orch._try_whole_side_repair_rung(
        *args[:5], wall_deadline=args[5]) is None
    _declined(orch, "adjudication_declined")


def test_flag_disabled_skips_everything():
    orch, args = _orch(_ScriptedEngine([]), _PassVer(), enabled=False)
    assert orch._try_whole_side_repair_rung(
        *args[:5], wall_deadline=args[5]) is None
    assert orch.journal.events == []  # not even a probe
    assert orch.verification.calls == []


def test_wall_deadline_margin_declines():
    orch, args = _orch(_ScriptedEngine([]), _PassVer(),
                       wall_deadline=time.monotonic() + 30)
    assert orch._try_whole_side_repair_rung(
        *args[:5], wall_deadline=args[5]) is None
    _declined(orch, "wall_deadline")
    assert orch.verification.calls == []


def test_single_stage_side_is_not_rung_territory():
    orch = object.__new__(Orchestrator)
    orch.resolution_engine = _ScriptedEngine([])
    orch.journal = _RecJournal()
    orch.step = 1
    orch.git = _FakeGit({2: _CUR})  # only the current stage exists
    orch.verification = _PassVer()
    orch.config = SimpleNamespace(
        future=SimpleNamespace(enable_whole_side_repair_rung=True))
    orch._write_worktree_only = lambda *a, **k: None
    out = orch._try_whole_side_repair_rung(
        "f.c", "c", _SPLICED, _units(), _SPLICED)
    assert out is None
    assert orch.journal.events == []


def test_probe_journals_duration_and_failures():
    orch, args = _orch(_ScriptedEngine([]), _PassVer(ok_texts=set()))
    orch._try_whole_side_repair_rung(*args[:5], wall_deadline=args[5])
    probes = [p for e, p in orch.journal.events if e == "whole_side_probe"]
    assert all("duration_s" in p for p in probes)
    assert probes[0]["hard_failures"] == ["error[E0308]: mismatched types"]

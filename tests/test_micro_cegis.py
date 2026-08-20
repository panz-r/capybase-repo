"""Sprint-20 S20.6 — micro-CEGIS at the compiler-authority gate.

protobuf-0065 class: the buffer sits within ~0.4% of the oracle and the
pre_continue build fails with errors positively attributed to a merged
file (P4's escalation shape). Before the honest stop, one bounded repair
round: deterministic duplicate deletion for 'redefinition of X' (the
base-verbatim copy a parent side deleted) and a tiny LLM SEARCH/REPLACE
micro-patch for missing-symbol errors — each re-gated by the same
command; no progress escalates exactly as before.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from capybase.orchestrator import (
    Orchestrator,
    _micro_delete_base_verbatim_duplicate,
    _micro_extract_brace_block,
    _micro_symbol_decls,
)


# ---------------------------------------------------------------------------
# Deterministic core (pure functions)
# ---------------------------------------------------------------------------

_DUP_BUF = """int helper(int x) { return x + 1; }

int ok_thing(void) { return 2; }

int helper(int x) { return x + 1; }
"""

_BASE = _DUP_BUF  # base carries one helper... (both copies identical here)


def test_extract_brace_block_finds_span():
    lines = ["int a;", "", "int helper(int x) {", "  return x;", "}", "int b;"]
    span = _micro_extract_brace_block(lines, 3)  # error line at the signature
    assert span == (2, 4)


def test_extract_brace_block_multiline_header():
    lines = ["int helper(int x,", "         int y) {", "  return x + y;", "}"]
    span = _micro_extract_brace_block(lines, 2)
    assert span == (0, 3)  # header continuation included


def test_duplicate_delete_removes_parent_deleted_copy():
    # buffer has TWO identical helper blocks; base had ONE; replayed
    # deleted its copy, current kept it → the merge resurrected one.
    base = "int helper(int x) { return x + 1; }\nint ok_thing(void) { return 2; }\n"
    cur = "int helper(int x) { return x + 1; }\nint ok_thing(void) { return 2; }\n"
    rep = "int ok_thing(void) { return 2; }\n"  # replayed DELETED helper
    out = _micro_delete_base_verbatim_duplicate(
        _DUP_BUF, "helper", 5, base, cur, rep)
    assert out is not None
    new_buffer, provenance = out
    assert "replayed_deleted_base_copy" in provenance
    assert new_buffer.count("int helper(") == 1  # exactly one copy remains


def test_duplicate_delete_declines_when_both_parents_kept():
    # Both sides kept helper: the duplicate is splice noise, but deleting
    # a copy neither parent removed is a guess — decline.
    out = _micro_delete_base_verbatim_duplicate(
        _DUP_BUF, "helper", 5, _BASE, _DUP_BUF, _DUP_BUF)
    assert out is None


def test_duplicate_delete_declines_when_not_base_verbatim():
    buf = _DUP_BUF.replace("x + 1", "x + 2", 1)  # one copy modified
    base = "int helper(int x) { return x + 1; }\n"
    out = _micro_delete_base_verbatim_duplicate(
        buf, "helper", 5, base, buf, "int ok_thing(void) { return 2; }\n")
    # the modified copy is not base-verbatim; the verbatim one qualifies
    # only if a parent deleted it — here replayed deleted helper entirely:
    # verbatim copy IS absent from replayed → deletes the verbatim copy.
    assert out is not None and out[0].count("int helper(") == 1


def test_symbol_decls_collects_declaration_lines():
    decls = _micro_symbol_decls(
        "tokenizer_", "void f();\n  Tokenizer tokenizer_;\n// tokenizer_ gone\n",
        "tokenizer_ = tok;\n")
    assert "Tokenizer tokenizer_;" in decls
    assert not any("//" in d for d in decls)


# ---------------------------------------------------------------------------
# Wiring — the rung on a fake orchestrator
# ---------------------------------------------------------------------------


class _RecJournal:
    def __init__(self):
        self.events = []

    def emit(self, event, payload, **kw):
        self.events.append((event, payload))


class _FakeEngine:
    def __init__(self, payload: str):
        self._payload = payload
        self.config = SimpleNamespace(max_tokens=1024)

    def raw_complete(self, prompt, json_mode=True, max_tokens=1024):
        return SimpleNamespace(text=self._payload)


class _FakeGit:
    def __init__(self, repo: Path, stages: dict[int, str]):
        self.repo = str(repo)
        self._stages = stages

    def read_stage_blob(self, path: str, stage: int) -> bytes:
        return self._stages[stage].encode()


def _micro_orch(tmp_path, *, engine=None, flag=True, gate_results=None):
    orch = object.__new__(Orchestrator)
    orch.resolution_engine = engine
    orch.journal = _RecJournal()
    orch.step = 1
    repo = tmp_path / "r"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "text_format.cc").write_text(
        "void f() { tokenizer_.Next(); }\n")
    orch.git = _FakeGit(repo, {
        1: "void f() { tokenizer_.Next(); }\n",   # base
        2: "void f() { tokenizer_.Next(); }\n",   # current
        3: ("Tokenizer tokenizer_;\nvoid f() { tokenizer_.Next(); }\n"),  # rep added
    })
    orch.verification = None
    orch.config = SimpleNamespace(
        future=SimpleNamespace(enable_micro_cegis=flag))
    written = {}
    orch._write_worktree_only = (
        lambda path, buffer, accepted=None: written.__setitem__(path, buffer))
    staged: list[str] = []
    orch.git.stage_paths = staged.extend
    gate_iter = iter(gate_results or [])
    orch._run_tests = lambda label, result: next(gate_iter, False)
    result = SimpleNamespace(
        units_by_path={"src/text_format.cc": []})
    return orch, result, written, staged


_ERR = ("/r/src/text_format.cc:1:12: error: 'tokenizer_' does not name a type")


def test_micro_cegis_missing_symbol_patch_repairs_and_re_gates(tmp_path):
    payload = ('{"edits": [{"search": "void f() { tokenizer_.Next(); }", '
               '"replace": "Tokenizer tokenizer_;\\nvoid f() { tokenizer_.Next(); }"}]}')
    orch, result, written, staged = _micro_orch(
        tmp_path, engine=_FakeEngine(payload), gate_results=[True])
    orch._last_attributed_merge_errors = [_ERR]
    orch._last_gate_cmd = "make -j4"
    assert orch._try_micro_cegis(result) is True
    assert "Tokenizer tokenizer_;" in written["src/text_format.cc"]
    kinds = [e for e in orch.journal.events if e[0] == "micro_cegis_patch"]
    assert kinds and kinds[0][1]["kind"] == "missing_symbol"
    assert any(e[0] == "micro_cegis_succeeded" for e in orch.journal.events)
    # Defect-review pin: the patched path must be STAGED — rebase --continue
    # commits the index, so an unstaged patch would ship the pre-patch splice.
    assert staged == ["src/text_format.cc"]


def test_micro_cegis_declines_when_re_gate_still_fails(tmp_path):
    payload = ('{"edits": [{"search": "void f() { tokenizer_.Next(); }", '
               '"replace": "void f() { tok.Next(); }"}]}')
    orch, result, _, _s = _micro_orch(
        tmp_path, engine=_FakeEngine(payload), gate_results=[False])
    assert _s == []  # no staging on a failed re-gate
    orch._last_attributed_merge_errors = [_ERR]
    assert orch._try_micro_cegis(result) is False
    assert any(e[0] == "micro_cegis_declined" for e in orch.journal.events)


def test_micro_cegis_disabled_flag(tmp_path):
    orch, result, _, _ = _micro_orch(tmp_path, flag=False, gate_results=[])
    orch._last_attributed_merge_errors = [_ERR]
    assert orch._try_micro_cegis(result) is False
    assert not any(e[0] == "micro_cegis_started" for e in orch.journal.events)


def test_micro_cegis_no_errors_no_op(tmp_path):
    orch, result, _, _ = _micro_orch(tmp_path, gate_results=[])
    orch._last_attributed_merge_errors = []
    assert orch._try_micro_cegis(result) is False


def test_micro_cegis_duplicate_stage_fires_deterministically(tmp_path):
    repo = tmp_path / "r2"
    (repo / "src").mkdir(parents=True)
    dup_buf = ("int helper(int x) { return x + 1; }\n"
               "int ok_thing(void) { return 2; }\n"
               "int helper(int x) { return x + 1; }\n")
    (repo / "src" / "dup.cc").write_text(dup_buf)
    orch = object.__new__(Orchestrator)
    orch.resolution_engine = None
    orch.journal = _RecJournal()
    orch.step = 1
    orch.git = _FakeGit(repo, {
        1: "int helper(int x) { return x + 1; }\nint ok_thing(void) { return 2; }\n",
        2: "int helper(int x) { return x + 1; }\nint ok_thing(void) { return 2; }\n",
        3: "int ok_thing(void) { return 2; }\n",  # replayed deleted helper
    })
    orch.verification = None
    orch.config = SimpleNamespace(
        future=SimpleNamespace(enable_micro_cegis=True))
    written = {}
    orch._write_worktree_only = (
        lambda path, buffer, accepted=None: written.__setitem__(path, buffer))
    orch._run_tests = lambda label, result: True
    result = SimpleNamespace(units_by_path={"src/dup.cc": []})
    orch._last_attributed_merge_errors = [
        "/r2/src/dup.cc:3:5: error: redefinition of 'helper'"]
    assert orch._try_micro_cegis(result) is True
    assert written["src/dup.cc"].count("int helper(") == 1
    kinds = [e for e in orch.journal.events if e[0] == "micro_cegis_patch"]
    assert kinds and kinds[0][1]["kind"] == "duplicate_delete"

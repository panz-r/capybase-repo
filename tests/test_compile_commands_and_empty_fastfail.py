"""Compile-commands hardening + first-empty fast-fail telemetry.

Two reviewer-directed hardenings:

1. ``_try_compile_commands`` adaptation: the entry's ``directory`` (cmake's
   per-entry cwd) is honored, relative ``-I``/``-isystem`` paths are
   absolutized against it, multi-config build dirs are searched, and an
   include-resolution failure ("fatal error: x.h: No such file or
   directory") is check-unavailable (falls through), never a false syntax
   verdict — a mis-adapted database must not become a new poison source.

2. Empty-response fast-fail telemetry: ``LLMResponse`` carries
   finish_reason/usage/latency; candidates surface finish_reason +
   llm_latency_ms into ``candidate_generated`` events.
"""

from __future__ import annotations

import json

from capybase.adapters.llm_openai import LLMResponse, _from_non_stream
from capybase.verification import (
    _cc_include_resolution_failure,
    _load_compile_commands,
)


def _write_cc(tmp_path, entries, name="compile_commands.json"):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(entries), encoding="utf-8")
    return p


def test_load_keeps_directory_and_multiconfig(tmp_path, monkeypatch):
    from capybase import verification as V
    monkeypatch.setattr(V, "_COMPILE_COMMANDS_CACHE", {})
    (tmp_path / "build" / "Release").mkdir(parents=True)
    _write_cc(tmp_path, [
        {"file": "src/a.cc", "command": "g++ -I../src -c src/a.cc",
         "directory": str(tmp_path / "build")},
    ], name="build/Release/compile_commands.json")
    cc = _load_compile_commands(str(tmp_path))
    assert cc is not None
    cmd, directory = cc["src/a.cc"]
    assert cmd.startswith("g++")
    assert directory == str(tmp_path / "build")


def test_try_compile_commands_uses_entry_directory(tmp_path, monkeypatch):
    """A relative -I in the entry resolves against the entry's directory.

    Builds a real header layout: include/answer.h, entry compiling main.cc
    from a build/ dir with -I../include. With the entry directory honored,
    a source that includes answer.h compiles; with the old repo-root cwd it
    would fail include resolution.
    """
    from capybase import verification as V
    monkeypatch.setattr(V, "_COMPILE_COMMANDS_CACHE", {})
    (tmp_path / "include").mkdir()
    (tmp_path / "include" / "answer.h").write_text(
        "#pragma once\n#define ANSWER 42\n", encoding="utf-8")
    _write_cc(tmp_path, [
        {"file": "src/main.cc",
         "command": "g++ -std=c++17 -I../include -c src/main.cc",
         "directory": str(tmp_path / "build")},
    ], name="build/compile_commands.json")
    (tmp_path / "build").mkdir(exist_ok=True)
    ok_src = '#include "answer.h"\nint f() { return ANSWER; }\n'
    res = V._try_compile_commands(str(tmp_path), "src/main.cc", ok_src, "cpp")
    assert res is not None
    assert res[0] is True


def test_try_compile_commands_include_noise_is_unavailable(tmp_path, monkeypatch):
    """Include-resolution failure → None (fall through), not a syntax fail."""
    from capybase import verification as V
    monkeypatch.setattr(V, "_COMPILE_COMMANDS_CACHE", {})
    _write_cc(tmp_path, [
        {"file": "src/main.cc", "command": "g++ -I/nonexistent-include -c src/main.cc",
         "directory": str(tmp_path)},
    ])
    res = V._try_compile_commands(
        str(tmp_path), "src/main.cc", '#include "nope.h"\nint x;\n', "cpp")
    assert res is None


def test_try_compile_commands_real_error_is_a_verdict(tmp_path, monkeypatch):
    """A genuine syntax error in the TU stays a hard False."""
    from capybase import verification as V
    monkeypatch.setattr(V, "_COMPILE_COMMANDS_CACHE", {})
    _write_cc(tmp_path, [
        {"file": "src/main.cc", "command": "g++ -std=c++17 -c src/main.cc",
         "directory": str(tmp_path)},
    ])
    res = V._try_compile_commands(
        str(tmp_path), "src/main.cc", "int f() { return ; }\n", "cpp")
    assert res is not None
    assert res[0] is False


def test_include_resolution_failure_classifier():
    assert _cc_include_resolution_failure(
        "src/a.cc:1:10: fatal error: answer.h: No such file or directory")
    assert not _cc_include_resolution_failure(
        "src/a.cc:3:5: error: expected ';' after expression")
    assert not _cc_include_resolution_failure("")


# ---------------------------------------------------------------------------
# LLMResponse telemetry
# ---------------------------------------------------------------------------

def test_from_non_stream_parses_telemetry():
    raw = {
        "choices": [{"message": {"content": "hi"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 51, "completion_tokens": 2},
        "_latency_ms": 1234.5,
    }
    r = _from_non_stream(raw)
    assert r.text == "hi"
    assert r.finish_reason == "stop"
    assert r.usage_prompt_tokens == 51
    assert r.usage_completion_tokens == 2
    assert r.latency_ms == 1234.5


def test_llmresponse_defaults_absent_telemetry():
    r = LLMResponse(text="")
    assert r.finish_reason is None
    assert r.usage_prompt_tokens is None
    assert r.latency_ms is None


def test_candidate_carries_telemetry():
    from capybase.conflict_model import CandidateResolution
    c = CandidateResolution(
        candidate_id="c", unit_id="u", model_name="t", prompt_version="t",
        resolved_text="x", finish_reason="length", llm_latency_ms=98.6)
    assert c.finish_reason == "length"
    assert c.llm_latency_ms == 98.6
